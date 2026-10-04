"""Job and artifact state. Python owns this schema; the reader never reads it.

Two shapes here are load-bearing and easy to get wrong later:

* ``artifacts`` is keyed by ``content_hash`` and is **global** — not scoped to a
  job. That is the whole L2 cache: re-running a paper after a prompt tweak
  re-renders only what actually changed, and two papers that happen to produce
  an identical scene share one video.
* ``scene_attempts`` keeps **every** attempt, including failures. The plan names
  per-archetype first-pass rate as a core metric and as the economic argument for
  the template library; that metric is a ``GROUP BY`` over this table. Discarding
  failures would discard the only evidence for the product's central bet.

No ``users`` table and no RLS in v1: articles are public and permalinked, so there
is no per-user data to protect. ``submitted_by`` holds a salted IP hash purely for
rate limiting. Accounts in Phase 4 add tables; they do not change these.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, JSON, TypeDecorator


class Base(DeclarativeBase):
    pass


class GUID(TypeDecorator):
    """UUID on Postgres, CHAR(32) elsewhere — so tests can run on SQLite."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return uuid.UUID(str(value)).hex

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


#: JSONB on Postgres, JSON elsewhere.
JSONType = JSONB().with_variant(JSON(), "sqlite")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class Paper(Base):
    """Canonical paper identity. The dedup target for the same URL twice."""

    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    arxiv_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    doi: Mapped[str | None] = mapped_column(String(128), unique=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list] = mapped_column(JSONType, default=list)
    #: Drives whether the paper's own figures may be rehosted or must be
    #: deep-linked. Parsed once, at ingest.
    license: Mapped[str] = mapped_column(String(200), nullable=False)
    redistributable: Mapped[bool] = mapped_column(Boolean, default=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    jobs: Mapped[list[Job]] = relationship(back_populates="paper", cascade="all, delete")


class Job(Base):
    """One pipeline run. ``(paper_id, pipeline_version)`` is the idempotency key."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("paper_id", "pipeline_version", name="uq_job_paper_version"),
        Index("ix_jobs_paper_created", "paper_id", "created_at"),
        # Partial index: the queue-depth query only ever looks at live jobs.
        Index(
            "ix_jobs_live",
            "state",
            postgresql_where="state NOT IN ('complete','failed')",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    #: Nullable until Ingest resolves the paper. A job row is created the moment a
    #: URL is submitted — before the title, licence or source hash are known — so the
    #: reader has something to poll and the rate limiter can see work in flight.
    #: Requiring a Paper up front would mean either a placeholder row with invented
    #: metadata, or (as before) no job row at all until the run finished.
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("papers.id", ondelete="CASCADE"), nullable=True
    )
    #: What was submitted, before it resolves to a paper. Kept so a queued job is
    #: self-describing and a failed ingest still says which URL failed.
    source_url: Mapped[str | None] = mapped_column(Text)
    arxiv_id: Mapped[str | None] = mapped_column(String(32), index=True)
    pipeline_version: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    #: The single row the status poll reads. Kept small on purpose.
    stage_progress: Mapped[dict] = mapped_column(JSONType, default=dict)
    #: The Storyboard document. The reader's only read, via the API.
    storyboard: Mapped[dict | None] = mapped_column(JSONType)
    failure: Mapped[dict | None] = mapped_column(JSONType)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    timings_ms: Mapped[dict] = mapped_column(JSONType, default=dict)
    #: Salted IP hash in v1; a user id later. Never a raw address.
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The stepper's mutual exclusion. On serverless a job advances through many
    #: short invocations, any two of which may arrive at once (two open tabs, a
    #: retry, the cron sweep). Whoever sets this into the future owns the job until
    #: it passes; an invocation killed mid-step simply lets it expire.
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(32))

    paper: Mapped[Paper] = relationship(back_populates="jobs")
    scenes: Mapped[list[SceneRow]] = relationship(
        back_populates="job", cascade="all, delete"
    )


class SceneRow(Base):
    """The fan-out unit: one row per scene per job."""

    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("job_id", "scene_key", name="uq_scene_job_key"),
        Index("ix_scenes_job", "job_id"),
        Index("ix_scenes_archetype", "archetype"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    scene_key: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The metrics dimension. Every dashboard question is grouped by this.
    archetype: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifacts.content_hash", ondelete="SET NULL")
    )
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    degraded_reason: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="scenes")
    attempt_rows: Mapped[list[SceneAttempt]] = relationship(
        back_populates="scene", cascade="all, delete", order_by="SceneAttempt.attempt_no"
    )


class SceneAttempt(Base):
    """Every attempt, kept. This table *is* the observability dashboard."""

    __tablename__ = "scene_attempts"
    __table_args__ = (
        # The idempotency key: a Modal retry after a container death cannot
        # double-charge or double-write an attempt.
        UniqueConstraint("scene_id", "attempt_no", name="uq_attempt_scene_no"),
        Index("ix_attempts_failed_gate", "failed_gate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=_uuid)
    scene_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    generated_src: Mapped[str | None] = mapped_column(Text)
    gate_results: Mapped[dict] = mapped_column(JSONType, default=dict)
    #: 1-4, or NULL on success. The single most queried column in this schema.
    failed_gate: Mapped[int | None] = mapped_column(Integer)
    simplified: Mapped[bool] = mapped_column(Boolean, default=False)
    render_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    scene: Mapped[SceneRow] = relationship(back_populates="attempt_rows")


class ArtifactRow(Base):
    """Content-addressed and global. Not scoped to a job — that is the L2 cache."""

    __tablename__ = "artifacts"
    __table_args__ = (
        # Partial index over GC candidates only, so the sweep never scans the
        # whole table.
        Index(
            "ix_artifacts_gc",
            "last_used_at",
            postgresql_where="ref_count = 0",
        ),
    )

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(40), nullable=False)
    mp4_key: Mapped[str] = mapped_column(Text, nullable=False)
    webm_key: Mapped[str | None] = mapped_column(Text)
    poster_key: Mapped[str] = mapped_column(Text, nullable=False)
    framestrip_key: Mapped[str] = mapped_column(Text, nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    quality: Mapped[str] = mapped_column(String(10), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: Reference counting drives GC eligibility; an artifact reaching zero is a
    #: candidate, not a deletion.
    ref_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


#: The dashboard, as a view. Tells you which template to fix next — created by
#: the migration rather than by the ORM, because it is a reporting artifact.
ARCHETYPE_HEALTH_VIEW = """
CREATE OR REPLACE VIEW archetype_health AS
-- Deliberately does NOT join scene_attempts: a join fans each scene row out by its
-- attempt count, multiplying cost and the state counters by however many tries the
-- scene took. Attempt-level facts come from subqueries so scene-level sums stay per
-- scene. See repo.archetype_health(), which mirrors this.
SELECT s.archetype,
       count(*)                                                   AS scenes,
       avg(CASE WHEN EXISTS (
               SELECT 1 FROM scene_attempts a
               WHERE a.scene_id = s.id
                 AND a.attempt_no = 1
                 AND a.failed_gate IS NULL
           ) THEN 1.0 ELSE 0.0 END)                                AS first_pass_rate,
       avg(s.attempts)                                            AS mean_attempts,
       (SELECT mode() WITHIN GROUP (ORDER BY a.failed_gate)
          FROM scene_attempts a JOIN scenes s2 ON s2.id = a.scene_id
         WHERE s2.archetype = s.archetype AND a.failed_gate IS NOT NULL
       )                                                          AS modal_failing_gate,
       (SELECT avg(a.render_ms)
          FROM scene_attempts a JOIN scenes s2 ON s2.id = a.scene_id
         WHERE s2.archetype = s.archetype AND a.render_ms > 0
       )                                                          AS mean_render_ms,
       sum(s.cost_usd)                                            AS total_cost,
       sum(CASE WHEN s.state = 'passed'   THEN 1 ELSE 0 END)       AS passed,
       sum(CASE WHEN s.state = 'degraded' THEN 1 ELSE 0 END)       AS degraded,
       sum(CASE WHEN s.state = 'failed'   THEN 1 ELSE 0 END)       AS failed
FROM scenes s
GROUP BY s.archetype;
"""

DROP_ARCHETYPE_HEALTH_VIEW = "DROP VIEW IF EXISTS archetype_health;"

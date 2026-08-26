"""Initial schema: papers, jobs, scenes, scene_attempts, artifacts.

Revision ID: 0001
Revises:
Create Date: 2026-08-19

Note the ordering: ``artifacts`` is created before ``scenes`` because
``scenes.content_hash`` references it. That reference direction is deliberate —
artifacts outlive the jobs that produced them, which is what makes the L2 cache
work across papers and re-runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from arcvisual.db.models import ARCHETYPE_HEALTH_VIEW, DROP_ARCHETYPE_HEALTH_VIEW

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("arxiv_id", sa.String(32), unique=True),
        sa.Column("doi", sa.String(128), unique=True),
        sa.Column("slug", sa.String(200), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("authors", JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("license", sa.String(200), nullable=False),
        sa.Column("redistributable", sa.Boolean, server_default=sa.text("false")),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("origin_url", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.create_table(
        "artifacts",
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("template_id", sa.String(40), nullable=False),
        sa.Column("mp4_key", sa.Text, nullable=False),
        sa.Column("webm_key", sa.Text),
        sa.Column("poster_key", sa.Text, nullable=False),
        sa.Column("framestrip_key", sa.Text, nullable=False),
        sa.Column("duration_s", sa.Float, nullable=False),
        sa.Column("quality", sa.String(10), nullable=False),
        sa.Column("bytes", sa.BigInteger, server_default=sa.text("0")),
        sa.Column("ref_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    # Partial index: the GC sweep must never scan the whole artifact table.
    op.create_index(
        "ix_artifacts_gc",
        "artifacts",
        ["last_used_at"],
        postgresql_where=sa.text("ref_count = 0"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable: a job row is created at submit time, before Ingest has resolved
        # the paper. Without that, nothing exists to poll until the run finishes and
        # in-flight work is invisible to the rate limiter.
        sa.Column(
            "paper_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_url", sa.Text()),
        sa.Column("arxiv_id", sa.String(32)),
        sa.Column("pipeline_version", sa.String(40), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("stage_progress", JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("storyboard", JSONB),
        sa.Column("failure", JSONB),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default=sa.text("0")),
        sa.Column("timings_ms", JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("submitted_by", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("paper_id", "pipeline_version", name="uq_job_paper_version"),
    )
    op.create_index("ix_jobs_paper_created", "jobs", ["paper_id", "created_at"])
    op.create_index(
        "ix_jobs_live",
        "jobs",
        ["state"],
        postgresql_where=sa.text("state NOT IN ('complete','failed')"),
    )
    op.create_index("ix_jobs_submitted_by", "jobs", ["submitted_by", "created_at"])
    # Resolving a queued job by arXiv id, before it has a paper_id.
    op.create_index("ix_jobs_arxiv", "jobs", ["arxiv_id", "pipeline_version"])

    op.create_table(
        "scenes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scene_key", sa.String(40), nullable=False),
        sa.Column("archetype", sa.String(40), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, server_default=sa.text("0")),
        sa.Column(
            "content_hash",
            sa.String(64),
            sa.ForeignKey("artifacts.content_hash", ondelete="SET NULL"),
        ),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default=sa.text("0")),
        sa.Column("degraded_reason", sa.Text),
        sa.UniqueConstraint("job_id", "scene_key", name="uq_scene_job_key"),
    )
    op.create_index("ix_scenes_job", "scenes", ["job_id"])
    op.create_index("ix_scenes_archetype", "scenes", ["archetype"])

    op.create_table(
        "scene_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scene_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("params", JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("generated_src", sa.Text),
        sa.Column("gate_results", JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("failed_gate", sa.Integer),
        sa.Column("simplified", sa.Boolean, server_default=sa.text("false")),
        sa.Column("render_ms", sa.Integer, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        # The idempotency key: a container death and retry cannot double-charge.
        sa.UniqueConstraint("scene_id", "attempt_no", name="uq_attempt_scene_no"),
    )
    op.create_index("ix_attempts_failed_gate", "scene_attempts", ["failed_gate"])

    op.execute(ARCHETYPE_HEALTH_VIEW)


def downgrade() -> None:
    op.execute(DROP_ARCHETYPE_HEALTH_VIEW)
    op.drop_table("scene_attempts")
    op.drop_table("scenes")
    op.drop_table("jobs")
    op.drop_table("artifacts")
    op.drop_table("papers")

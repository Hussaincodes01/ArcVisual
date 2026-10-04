"""Persistence for a job run. The only module that writes the schema.

Kept separate from the pipeline so the pipeline stays a pure
``(Storyboard) -> Storyboard`` computation that can run in a test, in the eval
harness, or on Modal without a database. :func:`persist_job` is called once, at
the end, with everything the run produced.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from arcvisual.config import PIPELINE_VERSION, settings
from arcvisual.db.models import (
    ArtifactRow,
    Base,
    Job,
    Paper,
    SceneAttempt,
    SceneRow,
)
from arcvisual.render.pipeline import JobResult
from arcvisual.storyboard import JobState, SceneState

_engine: Engine | None = None


def normalize_dsn(dsn: str) -> str:
    """Pin the driver SQLAlchemy uses for a Postgres URL.

    Hosted Postgres (Neon, Supabase, Vercel's marketplace) hands out
    ``postgres://`` or ``postgresql://`` URLs. SQLAlchemy reads the bare form as a
    request for psycopg2, which is not installed — psycopg 3 is — so an unmodified
    connection string fails at the first query with an import error.
    """
    for prefix in ("postgres://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    return dsn


def _engine_kwargs(dsn: str) -> dict:
    if not dsn.startswith("postgresql"):
        return {}
    return {
        "pool_pre_ping": True,
        # Serverless instances are frozen between requests; a connection idle past
        # the pooler's timeout is dead on thaw. Recycle well before that.
        "pool_recycle": 280,
        "pool_size": 3,
        "max_overflow": 4,
        # Neon's pooled endpoint is PgBouncer in transaction mode, where a server-
        # side prepared statement made on one backend is missing on the next.
        # psycopg prepares automatically after five executions; turn that off.
        "connect_args": {"prepare_threshold": None, "connect_timeout": 10},
    }


def engine(url: str | None = None, *, echo: bool = False) -> Engine:
    global _engine
    if url is not None:
        url = normalize_dsn(url)
        return create_engine(url, echo=echo, future=True, **_engine_kwargs(url))
    if _engine is None:
        dsn = settings().database_url
        if not dsn:
            raise RuntimeError(
                "DATABASE_URL is not set. The pipeline runs without a database; "
                "only persistence needs one."
            )
        dsn = normalize_dsn(dsn)
        _engine = create_engine(dsn, echo=echo, future=True, **_engine_kwargs(dsn))
    return _engine


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine(url), expire_on_commit=False, future=True)


def create_all(url: str | None = None) -> Engine:
    """Create the schema directly. For tests only — production uses Alembic."""
    eng = engine(url)
    Base.metadata.create_all(eng)
    return eng


def hash_submitter(ip: str) -> str:
    """Salted hash of a client address. A raw IP is never stored."""
    salt = settings().ip_hash_salt.encode()
    return hashlib.sha256(salt + ip.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def upsert_paper(session: Session, result: JobResult) -> Paper:
    assert result.storyboard is not None
    return upsert_paper_from_storyboard(session, result.storyboard)


def upsert_paper_from_storyboard(session: Session, sb) -> Paper:
    """The canonical paper row for an ingested storyboard, created or refreshed."""
    meta = sb.paper
    stmt = select(Paper)
    if meta.arxiv_id:
        stmt = stmt.where(Paper.arxiv_id == meta.arxiv_id)
    else:
        stmt = stmt.where(Paper.slug == meta.slug)
    paper = session.scalars(stmt).first()
    if paper is None:
        paper = Paper(
            arxiv_id=meta.arxiv_id,
            doi=meta.doi,
            slug=meta.slug,
            title=meta.title,
            authors=list(meta.authors),
            license=meta.license,
            redistributable=any(f.redistributable for f in sb.figures)
            if sb.figures
            else False,
            source_sha256=meta.source_sha256,
            origin_url=meta.origin_url,
        )
        session.add(paper)
        session.flush()
    else:
        # A new arXiv version means new source bytes, which must invalidate L0.
        paper.source_sha256 = meta.source_sha256
        paper.title = meta.title
    return paper


def persist_job(
    result: JobResult,
    *,
    session: Session,
    submitted_by: str | None = None,
    job_id=None,
) -> Job:
    """Record a completed (or failed) run, including every attempt.

    ``job_id`` adopts the queued row created at submit time, so the id the reader has
    been polling is the id that ends up holding the finished article. Without it a
    second row would appear here and the waiting room would poll the first one
    forever.
    """
    if result.storyboard is None:
        raise ValueError("cannot persist a job that never produced a storyboard")

    paper = upsert_paper(session, result)

    job = session.get(Job, job_id) if job_id is not None else None
    if job is None:
        job = session.scalars(
            select(Job).where(
                Job.paper_id == paper.id, Job.pipeline_version == PIPELINE_VERSION
            )
        ).first()
    else:
        # A queued row already exists for this run; claim it for the resolved paper.
        #
        # (paper_id, pipeline_version) is unique, so a previous run of the same paper
        # collides with this placeholder. Resolve it by dropping the SUPERSEDED row
        # and keeping the placeholder, not the other way round: the placeholder's id
        # is the one already handed to the client and being polled. Folding into the
        # older row deleted that id mid-run, and the reader's waiting room polled
        # itself into `404 unknown_job` for every re-submission of a paper it had
        # already seen — while the work itself completed perfectly.
        #
        # Nothing is lost by preferring the placeholder: this run replaces the
        # previous run's scene rows anyway, and they cascade from the job row.
        superseded = session.scalars(
            select(Job).where(
                Job.paper_id == paper.id,
                Job.pipeline_version == PIPELINE_VERSION,
                Job.id != job.id,
            )
        ).first()
        if superseded is not None:
            session.delete(superseded)
            session.flush()

    if job is None:
        job = Job(paper_id=paper.id, pipeline_version=PIPELINE_VERSION)
        session.add(job)
    else:
        # Re-running the same paper at the same pipeline version replaces the
        # previous run's scene rows rather than accumulating duplicates.
        for row in list(job.scenes):
            session.delete(row)
        session.flush()

    job.paper_id = paper.id
    job.arxiv_id = paper.arxiv_id
    job.state = result.state.value
    job.storyboard = result.storyboard.model_dump(mode="json")
    # The slug rides in the progress payload so the waiting room can redirect to the
    # permalink without a second request. It is the one field the reader needs that
    # is not derivable from the job id.
    # MERGE, don't replace. Fields written during the run — `title` above all — are
    # not reproduced by progress(), so overwriting wholesale made the waiting-room
    # header revert from the paper's title to the generic placeholder at the exact
    # moment the job finished.
    job.stage_progress = {
        **(job.stage_progress or {}),
        **result.storyboard.progress(),
        "slug": result.storyboard.paper.slug,
        "title": result.storyboard.paper.title,
        "stage": result.state.value,
    }
    job.failure = result.failure
    job.cost_usd = result.cost_usd
    job.timings_ms = dict(result.timings_ms)
    if submitted_by is not None:
        job.submitted_by = submitted_by
    if result.state in (JobState.COMPLETE, JobState.FAILED):
        job.completed_at = datetime.now(UTC)
    session.flush()

    for outcome in result.scene_outcomes:
        scene = outcome.scene
        artifact = scene.artifact
        if artifact is not None:
            _touch_artifact(session, scene, artifact)

        row = SceneRow(
            job_id=job.id,
            scene_key=scene.spec.id,
            archetype=scene.spec.archetype.value,
            state=scene.state.value,
            attempts=scene.attempts,
            content_hash=artifact.content_hash if artifact else None,
            cost_usd=outcome.cost_usd,
            degraded_reason=scene.degraded_reason,
        )
        session.add(row)
        session.flush()

        for attempt in outcome.attempts:
            session.add(
                SceneAttempt(
                    scene_id=row.id,
                    attempt_no=attempt.attempt_no,
                    params=attempt.params,
                    generated_src=attempt.generated_src or None,
                    gate_results=attempt.gate_report.model_dump(mode="json"),
                    failed_gate=attempt.failed_gate,
                    simplified=attempt.simplified,
                    render_ms=attempt.render_ms,
                    cost_usd=attempt.cost_usd,
                )
            )

    session.commit()
    return job


def _touch_artifact(session: Session, scene, artifact) -> None:
    """Insert or bump an artifact. Ref counting drives GC eligibility."""
    row = session.get(ArtifactRow, artifact.content_hash)
    if row is None:
        session.add(
            ArtifactRow(
                content_hash=artifact.content_hash,
                template_id=scene.spec.archetype.value,
                mp4_key=artifact.mp4_key,
                webm_key=artifact.webm_key,
                poster_key=artifact.poster_key,
                framestrip_key=artifact.framestrip_key,
                duration_s=artifact.duration_s,
                quality=artifact.quality,
                bytes=artifact.bytes,
                ref_count=1,
            )
        )
    else:
        row.ref_count += 1
        row.last_used_at = datetime.now(UTC)
    session.flush()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def job_for_slug(session: Session, slug: str) -> Job | None:
    """The newest job for this article at the current pipeline version."""
    return session.scalars(
        select(Job)
        .join(Paper, Paper.id == Job.paper_id)
        .where(Paper.slug == slug, Job.pipeline_version == PIPELINE_VERSION)
        .order_by(Job.created_at.desc())
    ).first()


def storyboard_for_slug(session: Session, slug: str) -> dict | None:
    """The reader's single read, served through the API."""
    job = job_for_slug(session, slug)
    return job.storyboard if job else None


def cached_artifact(session: Session, content_hash: str) -> ArtifactRow | None:
    """The L2 cache lookup. A hit means the render is already paid for."""
    return session.get(ArtifactRow, content_hash)


def queue_depth(session: Session) -> int:
    """Live jobs. Backed by the partial index on ``jobs(state)``."""
    return (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state.notin_(["complete", "failed"]))
        )
        or 0
    )


def recent_submissions(session: Session, submitted_by: str, since_hours: int = 1) -> int:
    """Rate-limit counter. Without this the render bill is a stranger's decision.

    Counts rows created in the window regardless of state, which only works because
    :func:`create_queued_job` writes the row at *submit* time. When jobs were only
    persisted on completion this counted finished work exclusively, so a caller could
    fire any number of concurrent submissions and every one of them saw a count of
    zero — the exact burst the limit exists to stop.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    return (
        session.query(Job)
        .filter(Job.submitted_by == submitted_by, Job.created_at >= cutoff)
        .count()
    )


def create_queued_job(
    session: Session,
    *,
    url: str,
    arxiv_id: str | None,
    submitted_by: str | None,
) -> Job:
    """Write a queued job row before any work starts.

    This is what makes the waiting room possible: the API can return a real
    ``job_id`` immediately, and ``GET /api/jobs/:id`` resolves from the first poll
    instead of 404-ing until the pipeline happens to finish.
    """
    job = Job(
        paper_id=None,
        source_url=url,
        arxiv_id=arxiv_id,
        pipeline_version=PIPELINE_VERSION,
        state=JobState.QUEUED.value,
        stage_progress={"stage": "queued"},
        submitted_by=submitted_by,
    )
    session.add(job)
    session.commit()
    return job


def upsert_scene_progress(
    session: Session,
    job_id,
    *,
    scene_key: str,
    archetype: str,
    state: str,
    attempts: int = 0,
    degraded_reason: str | None = None,
) -> None:
    """Record one scene's state the moment it reaches it, not at the end of the job.

    Without this the waiting room shows "no animations queued yet" for the whole
    render phase — the longest one — because scene rows only appeared when
    `persist_job` ran at the very end. The per-scene list is the waiting room's
    central feature and the plan's "animations stream in" promise; both need the rows
    to exist while the job is still running.

    `persist_job` later rewrites these rows with the full outcome, so this is a
    progressive view rather than a second source of truth.
    """
    row = session.scalars(
        select(SceneRow).where(SceneRow.job_id == job_id, SceneRow.scene_key == scene_key)
    ).first()
    if row is None:
        row = SceneRow(
            job_id=job_id, scene_key=scene_key, archetype=archetype, state=state
        )
        session.add(row)
    row.state = state
    row.archetype = archetype
    row.attempts = attempts
    row.degraded_reason = degraded_reason
    session.commit()


def update_job_progress(
    session: Session,
    job_id,
    *,
    state: str | None = None,
    progress: dict | None = None,
) -> None:
    """Advance a live job's state and progress payload.

    Called from the orchestrator's progress callback, which is what turns the
    waiting room from a spinner into a running commentary. Deliberately writes only
    the two small columns — the storyboard is written once, at the end.
    """
    job = session.get(Job, job_id)
    if job is None:
        return
    if state is not None:
        job.state = state
    if progress is not None:
        merged = dict(job.stage_progress or {})
        merged.update(progress)
        job.stage_progress = merged
    session.commit()


def archetype_health(session: Session) -> list[dict]:
    """Per-archetype first-pass rate and cost — the plan's own core metric.

    Computed in SQL rather than in Python so it works against the production
    database from a dashboard, not only from this process.
    """
    rows = session.execute(
        text(
            """
            -- No JOIN to scene_attempts. Joining fans each scene row out by its
            -- attempt count, so sum(s.cost_usd) and the state counters would be
            -- multiplied by however many attempts that scene took -- inflating cost
            -- most for exactly the scenes that struggled, and skewing
            -- first_pass_rate by weighting scenes by their attempt count. The
            -- attempt-level facts come from scalar subqueries instead.
            SELECT s.archetype,
                   count(*) AS scenes,
                   avg(CASE WHEN EXISTS (
                           SELECT 1 FROM scene_attempts a
                           WHERE a.scene_id = s.id
                             AND a.attempt_no = 1
                             AND a.failed_gate IS NULL
                       ) THEN 1.0 ELSE 0.0 END) AS first_pass_rate,
                   avg(s.attempts) AS mean_attempts,
                   sum(CASE WHEN s.state = 'passed' THEN 1 ELSE 0 END) AS passed,
                   sum(CASE WHEN s.state = 'degraded' THEN 1 ELSE 0 END) AS degraded,
                   sum(CASE WHEN s.state = 'failed' THEN 1 ELSE 0 END) AS failed,
                   sum(s.cost_usd) AS total_cost,
                   (SELECT avg(a.render_ms) FROM scene_attempts a
                     JOIN scenes s2 ON s2.id = a.scene_id
                    WHERE s2.archetype = s.archetype AND a.render_ms > 0
                   ) AS mean_render_ms
            FROM scenes s
            GROUP BY s.archetype
            ORDER BY first_pass_rate ASC
            """
        )
    ).mappings()
    return [dict(r) for r in rows]


def scene_states(session: Session, job_id) -> list[dict]:
    """Payload for the progressive-fill poll."""
    rows = session.scalars(
        select(SceneRow).where(SceneRow.job_id == job_id).order_by(SceneRow.scene_key)
    ).all()
    return [
        {
            "key": r.scene_key,
            "archetype": r.archetype,
            "state": r.state,
            "content_hash": r.content_hash,
            "ready": r.state == SceneState.PASSED.value,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Stepped execution (serverless)
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(UTC)


def acquire_lease(session: Session, job_id, ttl_s: float) -> str | None:
    """Claim a job for one bounded step. Returns a token, or None if it is held.

    A single conditional UPDATE, so two invocations racing for the same job cannot
    both win: the database decides, not a read-then-write in Python.
    """
    import uuid as _uuid
    from datetime import timedelta

    from arcvisual.db.models import Job

    token = _uuid.uuid4().hex
    now = _utcnow()
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            or_(Job.lease_until.is_(None), Job.lease_until < now),
        )
        .values(lease_until=now + timedelta(seconds=ttl_s), lease_owner=token)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return token if result.rowcount == 1 else None


def release_lease(session: Session, job_id, token: str) -> None:
    """Give the job back early. Only the holder's token can release it."""
    from arcvisual.db.models import Job

    session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == token)
        .values(lease_until=None, lease_owner=None)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def lease_active(job) -> bool:
    until = getattr(job, "lease_until", None)
    if until is None:
        return False
    if until.tzinfo is None:  # SQLite hands back naive datetimes
        until = until.replace(tzinfo=UTC)
    return until > _utcnow()


def live_job_for_arxiv(session: Session, arxiv_id: str, *, max_age_hours: int = 24):
    """The newest unfinished job for this paper, if one is still worth resuming.

    Two readers submitting the same new paper should share one run, not pay for two.
    The age cap stops a job abandoned days ago (say, before a deploy changed the
    pipeline) from being handed out forever.
    """
    from datetime import timedelta

    from arcvisual.db.models import Job

    cutoff = _utcnow() - timedelta(hours=max_age_hours)
    return session.scalars(
        select(Job)
        .where(
            Job.arxiv_id == arxiv_id,
            Job.pipeline_version == PIPELINE_VERSION,
            Job.state.notin_(["complete", "failed"]),
            Job.created_at >= cutoff,
        )
        .order_by(Job.created_at.desc())
    ).first()


def stale_live_jobs(session: Session, *, limit: int = 3, max_age_hours: int = 24) -> list:
    """Unfinished jobs nobody is currently advancing — the cron sweep's work list."""
    from datetime import timedelta

    from arcvisual.db.models import Job

    now = _utcnow()
    cutoff = now - timedelta(hours=max_age_hours)
    return list(
        session.scalars(
            select(Job)
            .where(
                Job.pipeline_version == PIPELINE_VERSION,
                Job.state.notin_(["complete", "failed"]),
                Job.created_at >= cutoff,
                or_(Job.lease_until.is_(None), Job.lease_until < now),
            )
            .order_by(Job.created_at.asc())
            .limit(limit)
        ).all()
    )


def recent_papers(session: Session, *, limit: int = 12) -> list[dict]:
    """Finished articles, newest first, for the gallery. Small fields only."""
    rows = session.execute(
        select(Job, Paper)
        .join(Paper, Paper.id == Job.paper_id)
        .where(Job.pipeline_version == PIPELINE_VERSION, Job.state == "complete")
        .order_by(Job.completed_at.desc().nullslast(), Job.created_at.desc())
        .limit(limit)
    ).all()
    out = []
    for job, paper in rows:
        sb = job.storyboard or {}
        scenes = [s for s in sb.get("scenes", []) if s.get("state") == "passed"]
        meta = sb.get("paper") or {}
        out.append(
            {
                "slug": paper.slug,
                "title": paper.title,
                "arxiv_id": paper.arxiv_id,
                "authors": list(paper.authors or [])[:4],
                "categories": list(meta.get("categories") or [])[:3],
                "abstract": (meta.get("abstract") or "")[:280],
                "visuals": len(scenes),
                "archetypes": sorted({s["spec"]["archetype"] for s in scenes}),
                "sections": len(sb.get("sections", [])),
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            }
        )
    return out

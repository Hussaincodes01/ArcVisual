"""Advancing a job in bounded, resumable steps — the serverless execution model.

The Modal deployment runs a job start to finish inside one long-lived function. A
serverless platform cannot: an invocation is capped (300s on Vercel Hobby), may be
frozen the moment its response is sent, and has no queue of its own. So the same
pipeline is cut at its natural seams and driven forward one step at a time:

    queued ──ingest──▶ analyzing ──analyze──▶ rendering ──scenes…──▶ complete

Every step reads the job's Storyboard from the database, does a bounded amount of
work, and writes the result back before returning. Nothing lives in memory between
invocations, which buys three properties at once:

* **Resumable.** An invocation killed mid-step loses only the scenes it had in
  flight; their slots are still ``pending`` in the stored document and the next
  step redoes them. Finished work is never repeated.
* **Concurrency-safe.** A step first takes a lease on the job row with a single
  conditional UPDATE. Two tabs, a retry and the cron sweep can all call at once;
  one wins, the others report ``busy`` and simply poll.
* **Progressive.** The article is readable as soon as Analyze lands, and each scene
  appears in it the moment its step records it — the plan's "readable at 90s,
  animations stream in" without a socket.

The step budget leaves room before the platform's hard limit, and a scene is never
*started* inside the final reserve: one scene is a codegen call plus its repair
ladder, and it must finish inside the invocation that began it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arcvisual.config import PIPELINE_VERSION, settings
from arcvisual.storyboard import Scene, SceneState, Stage, Storyboard, assert_monotonic

log = logging.getLogger(__name__)

#: Seconds before the step deadline after which no new scene is started.
SCENE_RESERVE_S = 75
#: Analyze has no repair ladder above it — a failure loses the whole paper — so a
#: transient error (a timeout, a rate limit) gets this many steps before the job is
#: failed. Three, because each step is itself a fresh set of provider retries.
MAX_ANALYZE_ATTEMPTS = 3

TERMINAL = ("complete", "failed")


@dataclass
class StepReport:
    job_id: str
    #: Another invocation holds the lease; nothing was done here.
    busy: bool = False
    steps: list[str] = field(default_factory=list)
    state: str | None = None


def advance(
    job_id: str,
    *,
    session_factory: Callable[[], Any] | None = None,
    budget_s: float | None = None,
    provider: object | None = None,
    renderer: Any = None,
    run_gate2: bool | None = None,
) -> StepReport:
    """Move one job forward for up to ``budget_s`` seconds. Never raises for job
    reasons — a failure is written to the job row, where the reader can see it."""
    from arcvisual.db import repo
    from arcvisual.db.models import Job

    cfg = settings()
    budget = float(budget_s if budget_s is not None else cfg.step_budget_s)
    sf = session_factory or repo.session_factory()
    if run_gate2 is None:
        run_gate2 = not cfg.client_render

    with sf() as session:
        # The lease outlives the budget by enough to cover a scene started at the
        # last moment, and expires on its own if the platform kills this invocation.
        token = repo.acquire_lease(session, job_id, ttl_s=budget + SCENE_RESERVE_S + 30)
    report = StepReport(job_id=str(job_id))
    if token is None:
        report.busy = True
        return report

    deadline = time.monotonic() + budget
    try:
        if provider is None:
            provider = _select_provider()
        while time.monotonic() < deadline:
            with sf() as session:
                job = session.get(Job, job_id)
                state = job.state if job is not None else None
            if state is None or state in TERMINAL:
                break
            if state in ("queued", "ingesting"):
                _step_ingest(sf, job_id)
                report.steps.append("ingest")
            elif state == "analyzing":
                report.steps.append("analyze")
                if not _step_analyze(sf, job_id, provider):
                    # A failed attempt ends this step. Retrying inside the same call
                    # spends the whole attempt budget in milliseconds on the same
                    # transient condition; the next call comes seconds later.
                    break
            elif state == "rendering":
                finished = _step_render(
                    sf,
                    job_id,
                    deadline=deadline,
                    provider=provider,
                    renderer=renderer,
                    run_gate2=run_gate2,
                )
                report.steps.append("render")
                if not finished:
                    break  # out of time; the next step resumes from the stored slots
            else:
                log.warning("job %s in unknown state %r", job_id, state)
                break
    except Exception as exc:  # a bug in a step must not leave the job stuck forever
        log.exception("step failed for job %s", job_id)
        _fail(
            sf,
            job_id,
            {
                "code": "step_crashed",
                "message": f"{type(exc).__name__}: {exc}",
                "user_facing": (
                    "Something went wrong on our side while building this article. "
                    "Please try submitting it again."
                ),
            },
        )
    finally:
        with sf() as session:
            repo.release_lease(session, job_id, token)
            job = session.get(Job, job_id)
            report.state = job.state if job is not None else None
    return report


def _select_provider():
    from arcvisual.providers.registry import get_provider

    return get_provider()


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


def _step_ingest(sf, job_id) -> None:
    from sqlalchemy import select

    from arcvisual.db import repo
    from arcvisual.db.models import Job
    from arcvisual.ingest.arxiv import ingest_url
    from arcvisual.ingest.errors import IngestRejection

    with sf() as session:
        job = session.get(Job, job_id)
        url = job.source_url or (f"arxiv:{job.arxiv_id}" if job.arxiv_id else "")
        job.state = "ingesting"
        job.stage_progress = {**(job.stage_progress or {}), "stage": "ingesting"}
        session.commit()

    t0 = time.perf_counter()
    try:
        sb = ingest_url(url)
    except IngestRejection as exc:
        _fail(sf, job_id, exc.as_failure())
        return
    ms = int((time.perf_counter() - t0) * 1000)

    with sf() as session:
        job = session.get(Job, job_id)
        paper = repo.upsert_paper_from_storyboard(session, sb)
        # (paper_id, pipeline_version) is unique. An older run of this paper at this
        # version — failed, or abandoned — is superseded by this one; the id the
        # reader is polling is THIS row, so it is the one that survives.
        for other in session.scalars(
            select(Job).where(
                Job.paper_id == paper.id,
                Job.pipeline_version == PIPELINE_VERSION,
                Job.id != job.id,
            )
        ).all():
            session.delete(other)
        session.flush()
        job.paper_id = paper.id
        job.arxiv_id = paper.arxiv_id
        job.storyboard = sb.model_dump(mode="json")
        job.state = "analyzing"
        job.timings_ms = {**(job.timings_ms or {}), "ingest": ms}
        job.stage_progress = {
            **(job.stage_progress or {}),
            "stage": "ingested",
            "title": sb.paper.title,
            "slug": sb.paper.slug,
            "sections": len(sb.sections),
        }
        session.commit()


# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #


def _step_analyze(sf, job_id, provider) -> bool:
    """Run Analyze. True when the job moved on (to rendering, or complete)."""
    from arcvisual.analyze.single_pass import analyze
    from arcvisual.db import repo
    from arcvisual.db.models import Job
    from arcvisual.generate.repair import _pending
    from arcvisual.providers.base import ProviderError

    cfg = settings()
    with sf() as session:
        job = session.get(Job, job_id)
        sb = Storyboard.model_validate(job.storyboard)
        attempts = int((job.stage_progress or {}).get("analyze_attempts") or 0) + 1
        job.stage_progress = {
            **(job.stage_progress or {}),
            "stage": "analyzing",
            "analyze_attempts": attempts,
        }
        session.commit()

    before = sb.model_copy(deep=True) if cfg.strict_ownership else None
    t0 = time.perf_counter()
    try:
        sb, analysis = analyze(sb, provider=provider)
    except Exception as exc:
        log.warning("analyze attempt %d failed for %s: %s", attempts, job_id, exc)
        quota = isinstance(exc, ProviderError) and "daily token quota" in str(exc)
        if quota:
            _fail(
                sf,
                job_id,
                {
                    "code": "provider_quota",
                    "message": f"{type(exc).__name__}: {exc}",
                    "user_facing": (
                        "We have used today's free explanation budget. Nothing is "
                        "wrong with your paper — please try again in a few hours."
                    ),
                },
            )
        elif attempts >= MAX_ANALYZE_ATTEMPTS:
            _fail(
                sf,
                job_id,
                {
                    "code": "analyze_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "user_facing": (
                        "We read the paper but could not work out how to explain it. "
                        "This is on us, not the paper."
                    ),
                },
            )
        # Otherwise leave the job in `analyzing`; the next step tries again.
        return False
    if before is not None:
        assert_monotonic(before, sb, Stage.ANALYZE)
    ms = int((time.perf_counter() - t0) * 1000)

    with sf() as session:
        job = session.get(Job, job_id)
        job.cost_usd = float(job.cost_usd or 0) + analysis.cost_usd
        job.timings_ms = {**(job.timings_ms or {}), "analyze": ms}
        progress = {
            **(job.stage_progress or {}),
            "stage": "analyzed",
            "concepts": len(sb.concepts),
            "opportunities": len(sb.opportunities),
            "provider": analysis.provider,
        }
        if not sb.opportunities:
            # Nothing worth animating is a legitimate article, not a failure.
            job.storyboard = sb.model_dump(mode="json")
            _complete(job, sb, progress)
            session.commit()
            return True

        # Placeholder slots, so the article can render skeletons where the scenes
        # will land, and so "what is left" is readable from the document itself.
        sb.scenes = [_pending(o) for o in sb.opportunities]
        job.storyboard = sb.model_dump(mode="json")
        job.state = "rendering"
        job.stage_progress = {
            **progress,
            "scenes_total": len(sb.scenes),
            "scenes_done": 0,
        }
        session.commit()
        for o in sb.opportunities:
            repo.upsert_scene_progress(
                session,
                job_id,
                scene_key=o.id,
                archetype=o.archetype.value,
                state=SceneState.PENDING.value,
            )
    return True


# --------------------------------------------------------------------------- #
# Generate + validate
# --------------------------------------------------------------------------- #


def _step_render(
    sf,
    job_id,
    *,
    deadline: float,
    provider,
    renderer,
    run_gate2: bool,
) -> bool:
    """Build pending scenes until done or out of time. True when the job finished."""
    from arcvisual.db import repo
    from arcvisual.db.models import Job
    from arcvisual.generate.repair import build_scene
    from arcvisual.render.pipeline import _cancelled

    cfg = settings()
    with sf() as session:
        job = session.get(Job, job_id)
        sb = Storyboard.model_validate(job.storyboard)
        spent = float(job.cost_usd or 0)

    done = {s.spec.id for s in sb.scenes if s.is_terminal}
    queue = [o for o in sb.opportunities if o.id not in done]
    if not queue:
        _finalize(sf, job_id)
        return True

    ceiling = cfg.budgets.job_cost_ceiling_usd
    if spent >= ceiling:
        # Past the job tripwire: every remaining slot degrades rather than spending.
        for o in queue:
            _record(
                sf,
                job_id,
                _cancelled(sb, o, f"the job cost ceiling (${ceiling:.2f}) was reached"),
            )
        _finalize(sf, job_id)
        return True

    lanes = max(1, min(cfg.budgets.scene_concurrency, len(queue)))

    def can_start() -> bool:
        return time.monotonic() < deadline - SCENE_RESERVE_S

    with ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="arc-step") as pool:
        running: dict[Future, Any] = {}

        def start_more() -> None:
            while queue and len(running) < lanes and can_start():
                o = queue.pop(0)
                with sf() as session:
                    repo.upsert_scene_progress(
                        session,
                        job_id,
                        scene_key=o.id,
                        archetype=o.archetype.value,
                        state=SceneState.GENERATING.value,
                    )
                running[
                    pool.submit(
                        build_scene,
                        sb,
                        o,
                        provider=provider,
                        renderer=renderer,
                        run_gate2=run_gate2,
                    )
                ] = o

        start_more()
        while running:
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                o = running.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    log.exception("scene %s raised", o.id)
                    outcome = _cancelled(sb, o, f"scene raised: {exc}")
                # Database writes stay on this thread: a Session is not thread-safe,
                # and recording each scene as it lands is what streams it to readers.
                _record(sf, job_id, outcome)
            start_more()

    if queue:
        # Out of time with slots left. Put the ones never started back to pending in
        # the waiting room; the stored document already shows them as pending.
        with sf() as session:
            for o in queue:
                repo.upsert_scene_progress(
                    session,
                    job_id,
                    scene_key=o.id,
                    archetype=o.archetype.value,
                    state=SceneState.PENDING.value,
                )
        return False
    _finalize(sf, job_id)
    return True


def _record(sf, job_id, outcome) -> None:
    """Write one finished scene into the stored document and the metrics tables."""
    from sqlalchemy import select

    from arcvisual.db import repo
    from arcvisual.db.models import Job, SceneAttempt, SceneRow

    scene: Scene = outcome.scene
    with sf() as session:
        job = session.get(Job, job_id)
        sb = Storyboard.model_validate(job.storyboard)
        sb.scenes = [scene if s.spec.id == scene.spec.id else s for s in sb.scenes]
        if all(s.spec.id != scene.spec.id for s in sb.scenes):
            sb.scenes.append(scene)
        job.storyboard = sb.model_dump(mode="json")
        job.cost_usd = float(job.cost_usd or 0) + outcome.cost_usd
        job.stage_progress = {
            **(job.stage_progress or {}),
            "scenes_total": len(sb.scenes),
            "scenes_done": sum(1 for s in sb.scenes if s.is_terminal),
        }

        if scene.artifact is not None:
            repo._touch_artifact(session, scene, scene.artifact)
        row = session.scalars(
            select(SceneRow).where(
                SceneRow.job_id == job.id, SceneRow.scene_key == scene.spec.id
            )
        ).first()
        if row is None:
            row = SceneRow(
                job_id=job.id,
                scene_key=scene.spec.id,
                archetype=scene.spec.archetype.value,
            )
            session.add(row)
        row.state = scene.state.value
        row.attempts = scene.attempts
        row.content_hash = scene.artifact.content_hash if scene.artifact else None
        row.cost_usd = outcome.cost_usd
        row.degraded_reason = scene.degraded_reason
        session.flush()
        existing = {a.attempt_no for a in row.attempt_rows}
        for attempt in outcome.attempts:
            if attempt.attempt_no in existing:
                continue
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


def _finalize(sf, job_id) -> None:
    from arcvisual.db.models import Job

    with sf() as session:
        job = session.get(Job, job_id)
        sb = Storyboard.model_validate(job.storyboard)
        # Reading order, not completion order.
        rank = {o.id: i for i, o in enumerate(sb.opportunities)}
        sb.scenes = sorted(sb.scenes, key=lambda s: rank.get(s.spec.id, len(rank)))
        sb = Storyboard.model_validate(sb.model_dump())
        job.storyboard = sb.model_dump(mode="json")
        _complete(job, sb, dict(job.stage_progress or {}))
        session.commit()


def _complete(job, sb: Storyboard, progress: dict) -> None:
    job.state = "complete"
    job.completed_at = datetime.now(UTC)
    job.failure = None
    job.stage_progress = {
        **progress,
        **sb.progress(),
        "stage": "complete",
        "slug": sb.paper.slug,
        "title": sb.paper.title,
    }


def _fail(sf, job_id, failure: dict) -> None:
    from arcvisual.db.models import Job

    with sf() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.state = "failed"
        job.failure = failure
        job.completed_at = datetime.now(UTC)
        job.stage_progress = {
            **(job.stage_progress or {}),
            "stage": "failed",
            "user_facing": failure.get("user_facing"),
        }
        session.commit()

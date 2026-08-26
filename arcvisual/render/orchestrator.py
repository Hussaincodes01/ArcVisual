"""Running one job to completion, and recording it — shared by every deployment.

Both the Modal orchestrator and the local dev server need exactly the same thing:
run the pipeline, stream progress into the job row the reader is polling, then
persist the finished storyboard onto that same row. Only *where* it runs differs.

Keeping that body here rather than in ``modal_app`` is what makes local development
possible at all: without it, the only way to exercise the submit flow would be to
deploy to Modal, and a product you cannot run on a laptop is a product nobody
iterates on.

The progress writes are best-effort by design. A failed status update must never
take down a job that is otherwise fine — the article is the deliverable, the
running commentary is a courtesy.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Pipeline progress events -> the ``jobs.state`` value the reader polls. Events with
#: no mapping (per-scene ticks) leave the state alone and only update the payload.
STAGE_TO_JOB_STATE: dict[str, str] = {
    "ingesting": "ingesting",
    "ingested": "analyzing",
    "analyzing": "analyzing",
    "analyzed": "rendering",
    "rendering": "rendering",
    "complete": "complete",
    "failed": "failed",
}

#: The only fields allowed into ``stage_progress``. That column is read on every
#: poll, so it stays a handful of scalars rather than growing into a second
#: storyboard. ``slug`` is the one the reader cannot do without — it is how the
#: waiting room knows where to redirect.
PROGRESS_FIELDS = frozenset(
    {
        "slug",
        "title",
        "sections",
        "concepts",
        "opportunities",
        "scenes",
        "scenes_total",
        "scenes_done",
        "id",
        "state",
        "archetype",
        "user_facing",
    }
)


def job_state_for(stage: str) -> str | None:
    return STAGE_TO_JOB_STATE.get(stage)


def progress_fields(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k in PROGRESS_FIELDS and v is not None}


def run_and_persist(
    url: str,
    *,
    submitted_by: str | None = None,
    job_id: str | None = None,
    session_factory: Callable[[], Any] | None = None,
    renderer: Any = None,
    run_gate2: bool = True,
) -> dict:
    """Run one job, streaming progress into its row, and persist the result.

    ``job_id`` is the queued row the API created and handed to the reader, so the
    finished article lands on the id already being polled rather than on a new one.
    """
    from arcvisual.db import repo
    from arcvisual.render.pipeline import run_job

    if session_factory is None:
        try:
            session_factory = repo.session_factory()
        except RuntimeError:
            session_factory = None  # no database configured; run without persistence

    def on_progress(stage: str, payload: dict) -> None:
        if session_factory is None or job_id is None:
            return
        try:
            with session_factory() as session:
                repo.update_job_progress(
                    session,
                    job_id,
                    state=job_state_for(stage),
                    progress={"stage": stage, **progress_fields(payload)},
                )
                # A per-scene tick: write the row now so the waiting room can show
                # it. Waiting for persist_job would leave the list empty through the
                # entire render phase.
                if stage == "scene" and payload.get("id"):
                    repo.upsert_scene_progress(
                        session,
                        job_id,
                        scene_key=str(payload["id"]),
                        archetype=str(payload.get("archetype", "")),
                        state=str(payload.get("state", "pending")),
                        attempts=int(payload.get("attempts") or 0),
                    )
        except Exception as exc:
            log.warning("progress write failed at %s: %s", stage, exc)

    result = run_job(url, renderer=renderer, run_gate2=run_gate2, on_progress=on_progress)

    persisted_id = job_id
    if session_factory is not None and result.storyboard is not None:
        try:
            with session_factory() as session:
                job = repo.persist_job(
                    result,
                    session=session,
                    submitted_by=submitted_by,
                    job_id=job_id,
                )
                persisted_id = str(job.id)
        except Exception as exc:
            log.exception("persist failed")
            result.notes.append(f"persist failed: {exc}")
    elif session_factory is not None and job_id is not None:
        # Ingest rejected the paper, so there is no storyboard to write — but the
        # reader is polling and needs the reason, not a job stuck at "ingesting".
        try:
            with session_factory() as session:
                repo.update_job_progress(
                    session,
                    job_id,
                    state=result.state.value,
                    progress={"stage": "failed", **(result.failure or {})},
                )
                _record_failure(session, job_id, result.failure)
        except Exception as exc:
            log.warning("failure write failed: %s", exc)

    return {
        "job_id": persisted_id,
        "state": result.state.value,
        "slug": result.storyboard.paper.slug if result.storyboard else None,
        "summary": result.summary(),
        "cost_usd": result.cost_usd,
        "cost_attributed": result.cost_attributed,
        "failure": result.failure,
    }


def _record_failure(session: Any, job_id: str, failure: dict | None) -> None:
    from arcvisual.db.models import Job

    job = session.get(Job, job_id)
    if job is not None:
        job.failure = failure
        session.commit()


# --------------------------------------------------------------------------- #
# Local orchestrator
# --------------------------------------------------------------------------- #


@dataclass
class LocalHandle:
    """Stands in for a Modal call handle so the API needs no branch for local."""

    object_id: str
    future: Future | None = None


class ThreadOrchestrator:
    """Runs jobs in background threads. The local stand-in for Modal's ``.spawn()``.

    Deliberately not a queue system. It exists so the submit flow can be driven on a
    laptop; production fans out across Modal containers, and the concurrency cap here
    only stops a local run from starting a dozen simultaneous Manim renders and
    thrashing the machine.
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
        session_factory: Callable[[], Any] | None = None,
        renderer: Any = None,
        run_gate2: bool = True,
    ) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="arcvisual-job"
        )
        self._session_factory = session_factory
        self._renderer = renderer
        self._run_gate2 = run_gate2

    def spawn(
        self,
        url: str,
        submitted_by: str | None = None,
        job_id: str | None = None,
    ) -> LocalHandle:
        future = self._pool.submit(self._run, url, submitted_by, job_id)
        return LocalHandle(
            object_id=job_id or f"local-{uuid.uuid4().hex[:12]}", future=future
        )

    def _run(self, url: str, submitted_by: str | None, job_id: str | None) -> dict:
        try:
            return run_and_persist(
                url,
                submitted_by=submitted_by,
                job_id=job_id,
                session_factory=self._session_factory,
                renderer=self._renderer,
                run_gate2=self._run_gate2,
            )
        except Exception:
            log.exception("local job %s failed", job_id)
            raise

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)

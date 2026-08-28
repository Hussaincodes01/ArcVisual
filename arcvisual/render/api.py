"""The control-plane API. A Modal ASGI app; there is no separate API host.

The reader on Vercel talks only to these endpoints and to the R2 CDN — it holds
no database credentials. That boundary is the most important one in the system:
the reader can be rewritten or replaced without touching the pipeline, and a
reader compromise leaks nothing.

Progressive fill is **polling**, on purpose. SSE over Vercel plus Modal means
holding a connection open through two platforms' idle timeouts to deliver an
event every thirty seconds. ``GET /api/jobs/{id}`` reads one JSONB column,
survives every proxy, and costs nothing to operate. The SSE route is left as a
documented enhancement for when the polling version is measurably annoying.
"""

# NOTE: deliberately no `from __future__ import annotations` in this module.
#
# The routes below are defined INSIDE build_api(), and their annotations refer to
# names local to it — `SubmitBody`, `Request`, the Pydantic bodies. PEP 563 turns
# annotations into strings, and FastAPI resolves those with typing.get_type_hints(),
# which looks them up in the function's module globals where these locals do not
# exist. It then silently falls back to treating each one as a QUERY parameter, so
# every POST route 422s with "Field required" for `body`. Evaluating annotations
# eagerly keeps the real class objects, which is what FastAPI needs.

import logging
import uuid
from typing import Any

from arcvisual.config import PIPELINE_VERSION, settings
from arcvisual.ingest.arxiv import extract_arxiv_id
from arcvisual.ingest.errors import IngestRejection

log = logging.getLogger(__name__)


def build_api(orchestrate: Any | None = None):
    """Construct the FastAPI app. ``orchestrate`` is the Modal function to spawn."""
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field

    from arcvisual.db import repo
    from arcvisual.render import storage

    cfg = settings()
    api = FastAPI(
        title="ArcVisual",
        version=PIPELINE_VERSION,
        description="Paper in. Explained paper out.",
    )
    api.add_middleware(
        CORSMiddleware,
        # The reader is the only browser client; keep the surface narrow.
        allow_origins=_allowed_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    class SubmitBody(BaseModel):
        url: str = Field(min_length=8, max_length=500)

    class Feedback(BaseModel):
        helpful: bool
        note: str | None = Field(default=None, max_length=1000)

    class Takedown(BaseModel):
        reason: str = Field(min_length=10, max_length=2000)
        contact: str = Field(min_length=3, max_length=200)

    def db():
        with repo.session_factory()() as session:
            yield session

    def client_key(
        request: Request,
        forwarded: str | None = Header(default=None, alias="x-forwarded-for"),
    ) -> str:
        """A salted hash of the caller's address. A raw IP is never stored.

        ``X-Forwarded-For`` is client-controlled: anyone can send an arbitrary value
        and get a fresh rate-limit bucket per request. It is therefore only trusted
        when ``ARCVISUAL_TRUSTED_PROXY_HOPS`` says a known proxy sits in front, and
        even then we take the entry that proxy appended (counting from the right)
        rather than the leftmost, which is the half an attacker controls.
        """
        peer = request.client.host if request.client else "unknown"
        hops = cfg.trusted_proxy_hops
        if hops > 0 and forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            # chain[-hops] is the address the outermost trusted proxy observed.
            if len(chain) >= hops:
                peer = chain[-hops]
        return repo.hash_submitter(peer)

    # -- submit ------------------------------------------------------------- #

    @api.post("/api/jobs", status_code=202)
    def submit(body: SubmitBody, session=Depends(db), key: str = Depends(client_key)):
        try:
            arxiv_id = extract_arxiv_id(body.url)
        except IngestRejection as exc:
            # Fail loud at second 5, not minute 12.
            raise HTTPException(status_code=422, detail=exc.as_failure()) from exc

        # Rate limit before any spend. Without this the render bill is a
        # stranger's decision.
        if repo.recent_submissions(session, key) >= cfg.rate_limit_per_hour:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limited",
                    "user_facing": (
                        f"You have submitted {cfg.rate_limit_per_hour} papers in the "
                        "last hour, which is our limit for now. Try again later."
                    ),
                },
            )

        cached = _cached_job(session, arxiv_id)
        if cached is not None:
            # Idempotency: same paper, same pipeline version, same article.
            return {
                "job_id": str(cached.id),
                "slug": _slug_of(session, cached),
                "state": cached.state,
                "cached": True,
            }

        if orchestrate is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "no_worker",
                    "user_facing": "The pipeline is not available right now.",
                },
            )
        # Create the job row BEFORE spawning. The reader needs an id it can poll from
        # the very first request, and the rate limiter needs in-flight work to be
        # visible. Returning a Modal call id instead meant `/api/jobs/<call_id>` never
        # resolved, so the waiting room polled a 404 until the run happened to finish.
        job = repo.create_queued_job(
            session, url=body.url, arxiv_id=arxiv_id, submitted_by=key
        )
        try:
            call = orchestrate.spawn(body.url, key, str(job.id))
        except Exception as exc:
            job.state = "failed"
            job.failure = {
                "code": "spawn_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "user_facing": "We could not start the pipeline. Please try again.",
            }
            session.commit()
            raise HTTPException(status_code=503, detail=job.failure) from exc

        return {
            "job_id": str(job.id),
            "call_id": getattr(call, "object_id", None),
            "arxiv_id": arxiv_id,
            "state": "queued",
            "cached": False,
        }

    # -- status ------------------------------------------------------------- #

    @api.get("/api/jobs/{job_id}")
    def job_status(job_id: str, session=Depends(db)):
        """The polling endpoint. One row, one JSONB column, cheap by design."""
        from arcvisual.db.models import Job

        # A malformed id is a 404, not a 500. `session.get` passes the string into the
        # GUID type decorator, which raises ValueError on anything that is not a UUID —
        # so a stray path segment used to surface as a server error.
        try:
            uuid.UUID(str(job_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=404, detail={"code": "unknown_job"}) from None

        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "unknown_job"})
        return {
            "job_id": str(job.id),
            "state": job.state,
            "pipeline_version": job.pipeline_version,
            "stage_progress": job.stage_progress or {},
            "scenes": repo.scene_states(session, job.id),
            "failure": job.failure,
            "cost_usd": float(job.cost_usd or 0),
        }

    # -- read --------------------------------------------------------------- #

    @api.get("/api/papers/{slug}")
    def paper(slug: str, session=Depends(db)):
        """The reader's single read: the whole Storyboard for one article."""
        sb = repo.storyboard_for_slug(session, slug)
        if sb is None:
            raise HTTPException(status_code=404, detail={"code": "unknown_paper"})
        return {
            "pipeline_version": PIPELINE_VERSION,
            "storyboard": sb,
            "media_base": storage.media_base_url(),
        }

    @api.get("/api/papers/{slug}/scenes/{scene_key}")
    def scene(slug: str, scene_key: str, session=Depends(db)):
        """One slot, for progressive fill without refetching the whole document."""
        sb = repo.storyboard_for_slug(session, slug)
        if sb is None:
            raise HTTPException(status_code=404, detail={"code": "unknown_paper"})
        for entry in sb.get("scenes", []):
            if entry["spec"]["id"] == scene_key:
                return entry
        raise HTTPException(status_code=404, detail={"code": "unknown_scene"})

    # -- trust surface ------------------------------------------------------ #

    @api.post("/api/papers/{slug}/feedback", status_code=202)
    def feedback(slug: str, body: Feedback, key: str = Depends(client_key)):
        # Logged rather than stored in v1: the metric that matters ("this made the
        # paper clearer") needs a denominator we do not have until there is
        # traffic, and a table with three rows in it is worse than a log line.
        log.info(
            "feedback slug=%s helpful=%s note=%r by=%s",
            slug,
            body.helpful,
            (body.note or "")[:200],
            key,
        )
        return {"recorded": True}

    @api.post("/api/papers/{slug}/takedown", status_code=202)
    def takedown(slug: str, body: Takedown, key: str = Depends(client_key)):
        """The takedown path the plan promises authors. Deliberately unauthenticated
        — an author should not need an account to object to an article about their
        own paper."""
        log.warning(
            "TAKEDOWN slug=%s contact=%s reason=%r by=%s",
            slug,
            body.contact,
            body.reason[:500],
            key,
        )
        return {
            "recorded": True,
            "user_facing": (
                "Thank you — we have logged this and will review it. The article "
                "stays up until a human has read your note."
            ),
        }

    # -- ops ---------------------------------------------------------------- #

    @api.get("/api/health")
    def health():
        checks: dict[str, Any] = {"pipeline_version": PIPELINE_VERSION}
        try:
            from sqlalchemy import text

            with repo.session_factory()() as session:
                session.execute(text("SELECT 1"))
                checks["db"] = "ok"
                checks["queue_depth"] = repo.queue_depth(session)
        except Exception as exc:
            checks["db"] = f"error: {type(exc).__name__}"
        from arcvisual.providers import registry as provider_registry

        provider = None
        try:
            provider = provider_registry.get_provider()
        except Exception as exc:
            checks["provider_error"] = str(exc)
        checks["provider"] = provider_registry.describe(provider)
        checks["providers_available"] = provider_registry.available()
        checks["r2"] = "configured" if cfg.r2_account_id else "missing"
        checks["cdn"] = "configured" if cfg.r2_public_base else "missing"
        ok = checks.get("db") == "ok"
        return {"ok": ok, **checks}

    @api.get("/api/media/{key:path}")
    def media(key: str):
        """Serve a locally stored artifact.

        Production puts a CDN in front of a private R2 bucket and this route is never
        hit. It exists so a deployment with no cloud credentials still *serves* the
        videos it renders rather than showing empty players.
        """
        from fastapi.responses import FileResponse

        root = storage.local_media_root()
        target = (root / key).resolve()
        # Containment check: `key` comes from a URL, so a `..` traversal would
        # otherwise read any file the process can reach.
        if not str(target).startswith(str(root)) or not target.is_file():
            raise HTTPException(status_code=404, detail={"code": "unknown_asset"})
        media_type = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(
            target,
            media_type=media_type,
            # Content-addressed: the bytes for a key never change, so this is safe
            # to cache forever and is what the CDN would do anyway.
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @api.get("/api/providers")
    def providers():
        """Which model backends this deployment can use, and what each gives up.

        Exposed to the reader so a job page can say *which* model produced an article
        and whether its cost figure is exact — the same caveats the job report carries.
        """
        from arcvisual.providers import registry as provider_registry

        selected = None
        error = None
        try:
            selected = provider_registry.get_provider()
        except Exception as exc:
            error = str(exc)
        return {
            "requested": cfg.provider,
            "selected": provider_registry.describe(selected),
            "available": provider_registry.available(),
            "auto_preference": list(provider_registry.AUTO_PREFERENCE),
            "error": error,
        }

    @api.get("/api/metrics/archetypes")
    def archetype_metrics(session=Depends(db)):
        """First-pass rate per archetype — the metric that says which template to
        fix next, and the evidence for the template library being worth its cost."""
        return {"archetypes": repo.archetype_health(session)}

    return api


def _allowed_origins() -> list[str]:
    import os

    configured = os.environ.get("ARCVISUAL_ALLOWED_ORIGINS", "")
    if configured:
        return [o.strip() for o in configured.split(",") if o.strip()]
    return ["http://localhost:3000"]


def _cached_job(session, arxiv_id: str):
    """The newest complete job for this paper whose media is STILL THERE.

    ``state == "complete"`` is a claim about the past, not evidence about the
    present. The bytes live outside the database, so anything that clears the media
    store — a disk sweep, a fresh checkout, someone deleting a directory — leaves
    rows that describe artifacts nobody can fetch. Trusting the row alone made the
    reader serve an article of 404s, permanently: every later request hit the same
    cache entry and never re-rendered. The project's own test conventions say it
    best — assert on effects, not on status fields.
    """
    from sqlalchemy import select

    from arcvisual.db.models import Job, Paper

    candidates = session.scalars(
        select(Job)
        .join(Paper, Paper.id == Job.paper_id)
        .where(
            Paper.arxiv_id == arxiv_id,
            Job.pipeline_version == PIPELINE_VERSION,
            Job.state == "complete",
        )
        .order_by(Job.created_at.desc())
    ).all()
    for job in candidates:
        if _media_present(session, job):
            return job
        log.info("cached job %s discarded: its media is no longer on disk", job.id)
    return None


def _media_present(session, job) -> bool:
    """Whether every passed scene in ``job`` still has fetchable bytes.

    Only PASSED scenes are checked. A degraded or failed scene legitimately has no
    video, and requiring one would make every partially-successful article
    uncacheable — which is most of them.
    """
    from sqlalchemy import select

    from arcvisual.db.models import ArtifactRow, SceneRow
    from arcvisual.render import storage

    rows = session.scalars(
        select(SceneRow).where(SceneRow.job_id == job.id, SceneRow.state == "passed")
    ).all()
    if not rows:
        return True  # nothing to serve, nothing to go missing

    for scene in rows:
        content_hash = getattr(scene, "content_hash", None)
        if not content_hash:
            continue
        artifact = session.get(ArtifactRow, content_hash)
        if artifact is None or not storage.has_scene_local(artifact.mp4_key):
            return False
    return True


def _slug_of(session, job) -> str | None:
    from arcvisual.db.models import Paper

    paper = session.get(Paper, job.paper_id)
    return paper.slug if paper else None

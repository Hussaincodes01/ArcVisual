"""The Modal deployment: control-plane API, orchestrator, and render workers.

Everything server-side lives here. There is no separate API host and no separate
worker fleet — the FastAPI app is a Modal ASGI function, the orchestrator is a
long-lived Modal function, and each scene renders in its own container fanned out
with ``.spawn()``. That is what makes the plan's "collapses four services into
one" claim actually true rather than aspirational.

Two container images, deliberately:

* ``control_image`` — small, no Manim. Serves the API and runs the orchestrator,
  so a cold start on the request path is fast.
* ``render_image`` — Manim CE plus TinyTeX plus ffmpeg, multi-GB. Only the render
  workers pay for it, and ``enable_memory_snapshot`` plus a warm
  ``min_containers`` keeps the pull off the critical path. Pulling this image ten
  times in parallel is most of the wall clock on a cold job; it is the render cost
  nobody budgets for.

Import-safe without Modal installed: everything Modal-specific is constructed
lazily so ``arcvisual.gates.g2_runtime`` can import this module to look for a
sandbox renderer and fall back cleanly when there is none.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from arcvisual.config import MANIM_VERSION, PIPELINE_VERSION, settings
from arcvisual.gates.g2_runtime import RenderOutcome, RenderUnavailable
from arcvisual.generate.codegen import scene_class_name
from arcvisual.storyboard import SceneSpec
from arcvisual.templates.base import SceneTrace

log = logging.getLogger(__name__)

APP_NAME = "arcvisual"


def _modal():
    try:
        import modal
    except ImportError as exc:
        raise RenderUnavailable(
            "the modal package is not installed; Gate 2 will fall back to a local "
            "renderer, which is not a sandbox and must not be used in production"
        ) from exc
    return modal


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

_PROJECT_FILES = ["arcvisual", "pyproject.toml"]


def build_images():
    """Both images. Called at deploy time, not at import time."""
    modal = _modal()

    control_image = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install(
            "fastapi[standard]",
            "pydantic>=2.7",
            "sqlalchemy>=2.0",
            "alembic>=1.13",
            "psycopg[binary]>=3.1",
            "anthropic>=0.40",
            "httpx>=0.27",
            "boto3>=1.34",
            "ruff>=0.6",
            "pyyaml>=6",
        )
        .add_local_dir(".", "/root", copy=True, ignore=_ignore())
    )

    render_image = (
        # The upstream image already carries a working LaTeX toolchain, which is
        # the part that is genuinely painful to reproduce.
        modal.Image.from_registry(
            f"manimcommunity/manim:v{MANIM_VERSION}", add_python="3.11"
        )
        .apt_install("ffmpeg", "libcairo2-dev", "libpango1.0-dev")
        .pip_install(
            "manim-ml",
            "manim-physics",
            "manim-dsa",
            "pydantic>=2.7",
            "boto3>=1.34",
            "ruff>=0.6",
        )
        .add_local_dir(".", "/root", copy=True, ignore=_ignore())
        .env({"PYTHONPATH": "/root", "MPLBACKEND": "Agg"})
    )
    return control_image, render_image


def _ignore():
    return [
        "**/.git",
        "**/node_modules",
        "**/.next",
        "**/__pycache__",
        "**/.venv",
        "**/media",
        "**/*.mp4",
        "**/eval-out",
        "reader/**",
    ]


def build_app():
    """Construct the Modal app. Import-time safe; call from a deploy entrypoint."""
    modal = _modal()
    control_image, render_image = build_images()

    app = modal.App(APP_NAME)
    secrets = [
        modal.Secret.from_name("arcvisual-db"),
        modal.Secret.from_name("arcvisual-anthropic"),
        modal.Secret.from_name("arcvisual-r2"),
    ]
    # LaTeX regenerates its .fmt caches per container otherwise, which adds
    # seconds to every single scene.
    latex_cache = modal.Volume.from_name("arcvisual-latex-cache", create_if_missing=True)

    budgets = settings().budgets

    @app.function(
        image=render_image,
        secrets=secrets,
        volumes={"/root/.cache/manim": latex_cache},
        timeout=600,
        memory=budgets.draft_render_memory_mb * 2,
        cpu=2.0,
        max_containers=budgets.render_max_containers,
        min_containers=int(os.environ.get("ARCVISUAL_WARM_RENDERERS", "0")),
        enable_memory_snapshot=True,
        retries=modal.Retries(max_retries=1, backoff_coefficient=1.0),
    )
    def render_scene(
        source: str, spec_json: dict, quality: str, upload: bool = True
    ) -> dict:
        """Render one scene in its own container. The fan-out unit."""
        return _render_in_container(source, spec_json, quality, upload=upload)

    @app.function(
        image=control_image,
        secrets=secrets,
        timeout=budgets.job_deadline_s + 120,
        max_containers=8,
    )
    def orchestrate(
        url: str, submitted_by: str | None = None, job_id: str | None = None
    ) -> dict:
        """One job, start to finish. Long-lived; spawns render_scene per scene.

        The body lives in :mod:`arcvisual.render.orchestrator` because the local dev
        server needs exactly the same behaviour — same progress writes, same
        persistence onto the queued row the reader is polling. Only the renderer
        differs.
        """
        from arcvisual.render.orchestrator import run_and_persist

        return run_and_persist(
            url,
            submitted_by=submitted_by,
            job_id=job_id,
            renderer=SpawnRenderer(render_scene),
        )

    @app.function(image=control_image, secrets=secrets, min_containers=1)
    @modal.asgi_app()
    def api():
        from arcvisual.render.api import build_api

        return build_api(orchestrate)

    return app, {"render_scene": render_scene, "orchestrate": orchestrate, "api": api}


# --------------------------------------------------------------------------- #
# In-container render
# --------------------------------------------------------------------------- #


def _render_in_container(
    source: str, spec_json: dict, quality: str, *, upload: bool = True
) -> dict:
    """Runs inside ``render_image``. Returns a JSON-safe RenderOutcome."""
    import subprocess
    import sys
    import tempfile
    import time

    from arcvisual.cache.hashing import RenderEnv, content_hash, r2_keys
    from arcvisual.render import storage
    from arcvisual.templates import registry

    spec = SceneSpec.model_validate(spec_json)
    workdir = Path(tempfile.mkdtemp(prefix=f"scene-{spec.id}-"))
    module = workdir / "scene.py"
    trace_path = workdir / "trace.json"
    module.write_text(
        source.replace("trace_path='trace.json'", f"trace_path={str(trace_path)!r}"),
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        "-m",
        "manim",
        "render",
        quality,
        "--media_dir",
        str(workdir / "media"),
        "--disable_caching",
        str(module),
        scene_class_name(spec.id),
    ]
    t0 = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=settings().budgets.draft_render_timeout_s,
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, code = True, -1
        out = _as_text(exc.stdout)
        err = _as_text(exc.stderr)
    wall_ms = int((time.perf_counter() - t0) * 1000)

    trace = None
    if trace_path.exists():
        try:
            trace = SceneTrace.from_json(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError):
            trace = None

    media = workdir / "media"
    video = next(media.rglob("*.mp4"), None) if media.exists() else None

    result: dict[str, Any] = {
        "ok": code == 0 and not timed_out and video is not None,
        "exit_code": code,
        "stdout": out[-20000:],
        "stderr": err[-20000:],
        "duration_s": trace.total_duration if trace else 0.0,
        "wall_ms": wall_ms,
        "timed_out": timed_out,
        "oom": "MemoryError" in f"{out}{err}",
        "trace": json.loads(trace.to_json()) if trace else None,
        # Gate 2 asserts on this, not on a path: the container is torn down after
        # the upload, so no local path survives the trip back.
        "produced_video": video is not None,
        "keys": None,
        "sizes": None,
    }

    if result["ok"] and upload and video is not None:
        is_final = quality != settings().render.draft_flag
        assets = storage.derive_assets(video, workdir / "derived")
        if is_final and spec.scrubbable:
            # All-keyframe only where scrubbing actually needs it; the encode is
            # several times larger and autoplay never seeks.
            assets.mp4 = storage.transcode_all_keyframe(
                assets.mp4, workdir / "derived" / "scrub.mp4"
            )
        env = RenderEnv.for_template(
            registry.get(spec.archetype).module,
            quality="final" if is_final else "draft",
        )
        keys = r2_keys(content_hash(spec, env))
        try:
            result["sizes"] = storage.upload_scene(assets, keys)
            result["keys"] = keys
            result["duration_s"] = assets.duration_s or result["duration_s"]
        except Exception as exc:
            log.warning("upload failed for %s: %s", spec.id, exc)
            result["stderr"] += f"\nupload failed: {exc}"

    return result


def _as_text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


# --------------------------------------------------------------------------- #
# Renderer backends
# --------------------------------------------------------------------------- #


class SpawnRenderer:
    """Renders by calling the Modal render function. Used by the orchestrator."""

    def __init__(self, fn) -> None:
        self._fn = fn

    def render(
        self, source: str, spec: SceneSpec, *, quality: str, timeout_s: int
    ) -> RenderOutcome:
        payload = self._fn.remote(source, spec.model_dump(mode="json"), quality)
        return _outcome_from(payload)

    def spawn(self, source: str, spec: SceneSpec, *, quality: str):
        """Fire-and-collect, for fanning out N scenes at once."""
        return self._fn.spawn(source, spec.model_dump(mode="json"), quality)


class ModalSandboxRenderer:
    """Renders untrusted generated code in a network-disabled Modal Sandbox.

    This is the production backend and the reason Gate 2 is a security boundary
    rather than a smoke test: no network, a hard memory cap, a hard wall clock,
    and a filesystem that dies with the sandbox.
    """

    def __init__(self, app_name: str = APP_NAME) -> None:
        modal = _modal()  # raises RenderUnavailable when modal is absent
        self._modal = modal
        self._app_name = app_name
        if (
            not os.environ.get("MODAL_TOKEN_ID")
            and not Path(Path.home() / ".modal.toml").exists()
        ):
            raise RenderUnavailable("Modal is installed but not authenticated")

    def render(
        self, source: str, spec: SceneSpec, *, quality: str, timeout_s: int
    ) -> RenderOutcome:
        modal = self._modal
        _, render_image = build_images()
        budgets = settings().budgets

        with modal.enable_output():
            app = modal.App.lookup(self._app_name, create_if_missing=True)
            sandbox = modal.Sandbox.create(
                app=app,
                image=render_image,
                timeout=timeout_s,
                memory=budgets.draft_render_memory_mb,
                cpu=2.0,
                # The boundary. Generated code gets no egress, full stop.
                block_network=True,
            )
            try:
                sandbox.mkdir("/tmp/scene")
                with sandbox.open("/tmp/scene/scene.py", "w") as fh:
                    fh.write(
                        source.replace(
                            "trace_path='trace.json'",
                            "trace_path='/tmp/scene/trace.json'",
                        )
                    )
                proc = sandbox.exec(
                    "python",
                    "-m",
                    "manim",
                    "render",
                    quality,
                    "--media_dir",
                    "/tmp/scene/media",
                    "--disable_caching",
                    "/tmp/scene/scene.py",
                    scene_class_name(spec.id),
                    workdir="/tmp/scene",
                )
                out, err = proc.stdout.read(), proc.stderr.read()
                code = proc.wait()
                trace = None
                try:
                    with sandbox.open("/tmp/scene/trace.json", "r") as fh:
                        trace = SceneTrace.from_json(fh.read())
                except Exception:
                    trace = None

                # Ask the sandbox whether a video exists, before tearing it down.
                # Gate 2 needs to know a video was PRODUCED, which is a different
                # question from where it lives now — and inferring it from the log
                # would be guessing at Manim's output format.
                produced = False
                try:
                    check = sandbox.exec(
                        "sh",
                        "-c",
                        "find /tmp/scene/media -name '*.mp4' -size +0 | head -1",
                    )
                    produced = bool(check.stdout.read().strip())
                    check.wait()
                except Exception:
                    produced = code == 0
            finally:
                sandbox.terminate()

        return RenderOutcome(
            ok=code == 0,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=trace.total_duration if trace else 0.0,
            wall_ms=0,
            video_path=None,  # the sandbox is gone; nothing local survives it
            produced_video=produced,
            trace=trace,
            timed_out=code == 124,
            oom="MemoryError" in f"{out}{err}",
        )


def _outcome_from(payload: dict) -> RenderOutcome:
    trace = None
    if payload.get("trace"):
        try:
            trace = SceneTrace.from_json(json.dumps(payload["trace"]))
        except (ValueError, TypeError, KeyError):
            trace = None
    return RenderOutcome(
        ok=bool(payload.get("ok")),
        exit_code=int(payload.get("exit_code", -1)),
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
        duration_s=float(payload.get("duration_s") or 0.0),
        wall_ms=int(payload.get("wall_ms") or 0),
        video_path=None,
        produced_video=bool(payload.get("produced_video")),
        trace=trace,
        timed_out=bool(payload.get("timed_out")),
        oom=bool(payload.get("oom")),
        extra={"keys": payload.get("keys"), "sizes": payload.get("sizes")},
    )


# --------------------------------------------------------------------------- #
# Deploy entrypoint
# --------------------------------------------------------------------------- #

if os.environ.get("MODAL_DEPLOY") or __name__ == "__main__":  # pragma: no cover
    app, _fns = build_app()
    print(f"arcvisual {PIPELINE_VERSION} — modal app ready: {APP_NAME}")

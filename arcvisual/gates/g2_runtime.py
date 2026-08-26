"""Gate 2 — runtime and resource stability. A real draft render, 20-60s.

Renders at ``-ql`` (480p15) under hard caps and asserts the render both *finished*
and produced what it claimed. The three things this catches that Gate 1 cannot:

* code that parses and type-checks but raises at render time (a mobject method
  called with the wrong shape, a LaTeX expression Manim cannot compile),
* a render that succeeds but takes far longer or shorter than the beats specify —
  usually a template misusing ``run_time``,
* LaTeX failures, which surface only in the log and would otherwise ship as a
  video with a blank space where an equation should be.

**The sandbox is a security boundary, not a convenience.** Generated code is
untrusted; Gate 1's allowlist is the static half and this is the dynamic half. The
local backend below applies what limits a developer machine can (no network is
*not* enforceable locally, which is exactly why production uses
:class:`ModalSandboxRenderer`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from arcvisual.config import settings
from arcvisual.generate.codegen import scene_class_name
from arcvisual.storyboard import GateResult, SceneSpec
from arcvisual.templates.base import SceneTrace

#: Log patterns worth surfacing to the repair prompt, most specific first.
_LOG_SIGNALS: tuple[tuple[str, str], ...] = (
    # Capture groups are bounded to a single line on purpose. These findings are fed
    # verbatim into the repair prompt, and a greedy `(.+)` under re.DOTALL would
    # swallow the entire remaining log into one "finding" — burning the context the
    # repair actually needs on a wall of Manim progress bars.
    (r"LaTeX Error: ([^\r\n]{1,200})", "LaTeX failed to compile: {0}"),
    (
        r"! Undefined control sequence[^\r\n]*\r?\n[^\r\n]*?(\\[A-Za-z@]+)",
        "unknown LaTeX macro {0}",
    ),
    (
        r"! Missing \$ inserted",
        "LaTeX math-mode error: an expression is missing $ or is malformed",
    ),
    (
        r"latex[^\r\n]{0,80}failed",
        "the LaTeX toolchain failed; check the expression syntax",
    ),
    (r"DeprecationWarning: ([^\r\n]{1,200})", "deprecated Manim API: {0}"),
    (r"MemoryError", "the render ran out of memory; reduce the number of elements"),
    (r"No scenes inside that module", "no Scene subclass was found in the module"),
)

_TOLERANCE = 0.20  # duration must land within +/-20% of the specified beats

#: Log signatures that mean the RENDER ENVIRONMENT is missing a dependency, not that
#: the scene is wrong. The distinction matters commercially: a scene failure is worth
#: repair attempts, an absent toolchain is not — no parameter change installs LaTeX.
#: Measured cost of conflating them: 21 transform_chain scenes x 3 attempts = 63
#: renders spent on a machine with no TeX, and a 0% first-pass baseline recorded for
#: a template that was never given a chance to run.
_ENVIRONMENT_SIGNALS: tuple[tuple[str, str], ...] = (
    (
        "latex failed but did not produce a log file",
        "no LaTeX toolchain is installed in this render environment, so MathTex "
        "cannot be typeset. Install TinyTeX (the manimcommunity/manim image ships "
        "it) — no parameter change can fix this",
    ),
    (
        "Check your LaTeX installation",
        "the LaTeX installation is incomplete in this render environment",
    ),
)

#: A missing-file error only counts as environmental when it names a binary the
#: renderer depends on. A bare FileNotFoundError is usually the scene's own doing
#: (a figure path that does not resolve), and misclassifying it would rob that
#: scene of the repair attempts it could actually use.
_MISSING_BINARIES = ("latex", "pdflatex", "xelatex", "dvisvgm", "ffmpeg", "pango")


log = logging.getLogger(__name__)


@dataclass
class RenderOutcome:
    """What a backend returns. Deliberately free of Manim or Modal types."""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    wall_ms: int
    #: Only set by backends that keep the file locally. A remote sandbox uploads
    #: and discards it, so this stays None there — see `produced_video`.
    video_path: Path | None = None
    #: Whether a video was actually produced, independent of where it now lives.
    #: Gate 2 asserts on THIS, never on `video_path`: a sandboxed render uploads its
    #: output and tears the container down, so a `video_path is None` check would
    #: fail every scene on every remote backend while passing locally.
    produced_video: bool = False
    trace: SceneTrace | None = None
    timed_out: bool = False
    oom: bool = False
    workdir: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def log(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


class Renderer(Protocol):
    """A render backend. Gate 2 does not care where the container lives."""

    def render(
        self, source: str, spec: SceneSpec, *, quality: str, timeout_s: int
    ) -> RenderOutcome: ...


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def run(
    source: str,
    spec: SceneSpec,
    renderer: Renderer | None = None,
    *,
    quality: str | None = None,
) -> tuple[GateResult, RenderOutcome | None]:
    """Draft-render and assert stability. Returns ``(result, outcome)``."""
    cfg = settings()
    renderer = renderer or default_renderer()
    t0 = time.perf_counter()

    try:
        outcome = renderer.render(
            source,
            spec,
            quality=quality or cfg.render.draft_flag,
            timeout_s=cfg.budgets.draft_render_timeout_s,
        )
    except RenderUnavailable as exc:
        return (
            GateResult(
                gate=2,
                passed=False,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                findings=[str(exc)],
                payload={"skipped": True, "reason": "renderer_unavailable"},
            ),
            None,
        )

    findings: list[str] = []
    payload: dict = {
        "exit_code": outcome.exit_code,
        "wall_ms": outcome.wall_ms,
        "rendered_duration_s": outcome.duration_s,
        "expected_duration_s": round(spec.duration_s, 3),
    }

    if outcome.timed_out:
        findings.append(
            f"the render exceeded its {cfg.budgets.draft_render_timeout_s}s wall-clock "
            "limit; reduce the number of animated elements or shorten the beats"
        )
    if outcome.oom:
        findings.append(
            f"the render exceeded its {cfg.budgets.draft_render_memory_mb}MB memory "
            "limit; reduce the number of mobjects on screen"
        )
    if not outcome.ok and not (outcome.timed_out or outcome.oom):
        findings.append(
            f"the render exited with code {outcome.exit_code}. Traceback:\n"
            f"{_last_traceback(outcome.log)}"
        )

    env_problem = environment_failure(outcome.log)
    if env_problem:
        payload["environment_failure"] = env_problem
        findings.append(f"render environment: {env_problem}")
    else:
        findings.extend(scrape_log(outcome.log))

    if outcome.ok and not outcome.produced_video:
        findings.append("the render reported success but produced no video file")

    # Duration drift: a scene that plays for 4s when the beats say 20s has
    # silently dropped most of its content, and "succeeded" while doing so.
    if outcome.ok and outcome.duration_s > 0 and spec.duration_s > 0:
        drift = abs(outcome.duration_s - spec.duration_s) / spec.duration_s
        payload["duration_drift"] = round(drift, 4)
        if drift > _TOLERANCE:
            findings.append(
                f"the rendered scene runs {outcome.duration_s:.1f}s but its beats "
                f"specify {spec.duration_s:.1f}s ({drift:.0%} drift, limit "
                f"{_TOLERANCE:.0%}); align the beat durations with what the "
                "template actually plays"
            )

    if outcome.ok and outcome.duration_s > cfg.render.max_runtime_s:
        findings.append(
            f"the scene runs {outcome.duration_s:.0f}s, beyond the "
            f"{cfg.render.max_runtime_s:.0f}s ceiling; split it or cut beats"
        )

    if outcome.trace is not None:
        payload["boundaries"] = len(outcome.trace.boundaries)
        if outcome.trace.crashed:
            findings.append(
                f"the scene body raised after {len(outcome.trace.boundaries)} "
                f"animations: {outcome.trace.crash_repr}"
            )
        elif not outcome.trace.boundaries:
            findings.append(
                "the scene produced no animations at all; a template that plays "
                "nothing renders as dead air"
            )

    return (
        GateResult(
            gate=2,
            passed=not findings,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            findings=findings,
            payload=payload,
        ),
        outcome,
    )


def environment_failure(log: str) -> str | None:
    """A dependency the environment lacks, or None if the failure is the scene's.

    Checked before the scene-level signals so an unfixable problem is never handed to
    the repair ladder. See :data:`_ENVIRONMENT_SIGNALS` for why that matters.
    """
    lowered = log.lower()
    for signal, message in _ENVIRONMENT_SIGNALS:
        if signal.lower() in lowered:
            return message
    if "filenotfounderror" in lowered and any(b in lowered for b in _MISSING_BINARIES):
        missing = next(b for b in _MISSING_BINARIES if b in lowered)
        return f"the {missing!r} binary is missing from this render environment"
    return None


def scrape_log(log: str) -> list[str]:
    """Pull actionable signals out of a render log."""
    found: list[str] = []
    for pattern, message in _LOG_SIGNALS:
        for match in re.finditer(pattern, log, re.MULTILINE | re.DOTALL):
            groups = [g.strip() for g in match.groups() if g]
            text = message.format(*groups) if groups else message
            if text not in found:
                found.append(text)
            break  # one instance per signal is enough for a repair prompt
    return found


def _last_traceback(log: str, max_lines: int = 24) -> str:
    idx = log.rfind("Traceback (most recent call last)")
    tail = log[idx:] if idx != -1 else log[-2000:]
    return "\n".join(tail.splitlines()[:max_lines])


_FFMPEG_READY = False


def ensure_ffmpeg() -> str | None:
    """Put a usable ffmpeg on PATH, returning its path (or None if impossible).

    Manim shells out to ffmpeg to mux its frames; without it a render "succeeds"
    while writing no video, Gate 2 fails every scene, and the cause is three layers
    away from the symptom.

    This lives on the renderer rather than in the dev server because the renderer is
    what needs it. It was in `serve.py` alone, so `eval/run.py` — the harness whose
    entire job is proving the pipeline works — rendered every scene without ffmpeg
    and reported 0 shipped. Any caller that renders locally now bootstraps it.

    Idempotent and cached: called once per process, on the first local render.
    """
    global _FFMPEG_READY
    if _FFMPEG_READY:
        return shutil.which("ffmpeg")
    found = shutil.which("ffmpeg")
    if found:
        _FFMPEG_READY = True
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        log.warning(
            "ffmpeg is not on PATH and imageio-ffmpeg is unavailable; local renders "
            "will produce no video"
        )
        return None
    try:
        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        bindir = Path(tempfile.mkdtemp(prefix="arcvisual-ffmpeg-"))
        target = bindir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        shutil.copy(exe, target)
        if os.name != "nt":
            target.chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        _FFMPEG_READY = True
        log.info("using bundled ffmpeg from %s", exe)
        return str(target)
    except Exception as exc:
        log.warning("could not stage bundled ffmpeg: %s", exc)
        return None


class RenderUnavailable(RuntimeError):
    """No render backend is usable here (no Manim locally, no Modal configured)."""


# --------------------------------------------------------------------------- #
# Local backend
# --------------------------------------------------------------------------- #


class LocalRenderer:
    """Renders in a subprocess on this machine. For development and CI only.

    A subprocess is *not* a sandbox: it cannot deny network access, and the memory
    cap is best-effort (POSIX ``RLIMIT_AS`` only — Windows has no equivalent).
    Production must use :class:`ModalSandboxRenderer`. The gate is identical
    either way; only the isolation differs, which is the point of the protocol.
    """

    def __init__(self, keep_output: bool = False) -> None:
        self.keep_output = keep_output

    def render(
        self, source: str, spec: SceneSpec, *, quality: str, timeout_s: int
    ) -> RenderOutcome:
        # Manim needs ffmpeg to mux frames into a video. Bootstrapping here means
        # every local caller gets it, not just the dev server.
        ensure_ffmpeg()
        if shutil.which("manim") is None and not _manim_importable():
            raise RenderUnavailable(
                "Manim CE is not installed on this machine, so Gate 2 cannot run "
                "locally. Install the render extra (pip install -e .[render]) or "
                "run the pipeline on Modal."
            )

        workdir = Path(tempfile.mkdtemp(prefix=f"arcvisual-{spec.id}-"))
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
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(Path.cwd()), os.environ.get("PYTHONPATH", "")]
            ),
            # Deny egress at the library level; the real denial is the sandbox.
            "no_proxy": "*",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
        }

        t0 = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
                preexec_fn=_limit_memory(),
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            code = -1
            out = (
                exc.stdout.decode()
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            err = (
                exc.stderr.decode()
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
        wall_ms = int((time.perf_counter() - t0) * 1000)

        trace = None
        if trace_path.exists():
            try:
                trace = SceneTrace.from_json(trace_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, KeyError):
                trace = None

        video = (
            next((workdir / "media").rglob("*.mp4"), None)
            if (workdir / "media").exists()
            else None
        )
        log = f"{out}\n{err}"
        outcome = RenderOutcome(
            ok=code == 0 and not timed_out,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=trace.total_duration if trace else 0.0,
            wall_ms=wall_ms,
            video_path=video,
            produced_video=video is not None,
            trace=trace,
            timed_out=timed_out,
            oom="MemoryError" in log or "Cannot allocate memory" in log,
            workdir=workdir,
        )
        # A successful render's workdir is dead weight once the video and trace are
        # out of it — Manim writes a full media tree of partial frames per scene, and
        # keeping them grows without bound. Failures are kept whole so a human can
        # open the directory and see what the scene actually looked like.
        #
        # The VIDEO IS MOVED OUT FIRST. An earlier version deleted the tree and nulled
        # `video_path` on the theory that a dangling path is worse than none — which
        # was true, but it also meant the pipeline could never store the bytes it had
        # just spent a minute rendering. Scenes passed every gate, recorded a content
        # hash, and served an empty player. Preserving the one file we need costs a
        # few hundred KB; nulling it cost the product its output.
        if outcome.ok and not self.keep_output:
            if video is not None and video.exists():
                kept_dir = Path(tempfile.mkdtemp(prefix="arcvisual-scene-"))
                kept = kept_dir / video.name
                try:
                    shutil.move(str(video), str(kept))
                    outcome.video_path = kept
                except OSError as exc:  # pragma: no cover - disk-level failure
                    log.warning("could not preserve %s: %s", video, exc)
                    outcome.video_path = None
            shutil.rmtree(workdir, ignore_errors=True)
            outcome.workdir = None
        return outcome


def _limit_memory():
    """A POSIX address-space cap, or None where the platform has no equivalent."""
    if not hasattr(os, "fork"):  # Windows
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover
        return None

    limit = settings().budgets.draft_render_memory_mb * 1024 * 1024

    def _apply() -> None:  # pragma: no cover - child process
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


def _manim_importable() -> bool:
    try:
        import manim  # noqa: F401

        return True
    except ImportError:
        return False


def default_renderer() -> Renderer:
    """Modal when configured, otherwise local. Explicit, so nothing silently
    renders untrusted code outside a sandbox in production."""
    if os.environ.get("ARCVISUAL_RENDERER") == "local":
        return LocalRenderer()
    try:
        from arcvisual.render.modal_app import ModalSandboxRenderer

        return ModalSandboxRenderer()
    except (ImportError, RuntimeError):
        return LocalRenderer()

"""Render-snapshot tests. The plan's mitigation for "Manim CE version drift
breaking templates" — a pin plus a suite that actually renders.

These skip without Manim and ffmpeg, which is why the rest of the suite can run
anywhere. When they do run they are the only tests that execute a template body,
so they carry two jobs no static check can:

* proving ``ArcSceneMixin`` records a usable trace, which is Gate 3's entire input;
* keeping each template's ``estimate_duration`` in step with its ``build``. Those
  two are duplicated logic by necessity — the control plane has no Manim and cannot
  probe a timeline — so the duplication needs a test holding it together, or it
  drifts and Gate 2's duration assertion silently becomes noise.

``transform_chain`` additionally needs a LaTeX toolchain for ``MathTex`` and skips
without one.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from arcvisual.analyze.single_pass import analyze
from arcvisual.config import settings
from arcvisual.gates import g2_runtime
from arcvisual.generate.codegen import generate_scene
from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.storyboard import Archetype
from arcvisual.templates import registry
from tests.fixtures import metadata, paper_tarball


def _have_manim() -> bool:
    try:
        import manim  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_ffmpeg() -> bool:
    """Manim needs ffmpeg on PATH even to write PNGs. Use the pip-provided static
    binary if the system has none, so CI does not need a package install."""
    if shutil.which("ffmpeg"):
        return True
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not exe.exists():
        return False
    bindir = Path(tempfile.gettempdir()) / "arcvisual-ffmpeg"
    bindir.mkdir(exist_ok=True)
    target = bindir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not target.exists():
        shutil.copy(exe, target)
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
    return True


def _have_latex() -> bool:
    return any(shutil.which(b) for b in ("latex", "pdflatex", "xelatex"))


requires_render = pytest.mark.skipif(
    not (_have_manim() and _ensure_ffmpeg()),
    reason="needs Manim CE and ffmpeg (install the render extra)",
)

# Rendering is minutes, not milliseconds. Kept out of the default run.
pytestmark = [pytest.mark.slow, requires_render]

#: Templates whose text is Pango only. transform_chain needs LaTeX.
_NO_LATEX_ARCHETYPES = (Archetype.ARCHITECTURE_FLOW, Archetype.PLOT_REVEAL)


@pytest.fixture(scope="module")
def storyboard():
    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    return sb


def _scene_for(storyboard, archetype: Archetype):
    opp = storyboard.opportunities[0].model_copy(update={"archetype": archetype})
    return generate_scene(storyboard, opp)


def _render(storyboard, archetype: Archetype):
    gen = _scene_for(storyboard, archetype)
    result, outcome = g2_runtime.run(
        gen.source, gen.scene.spec, g2_runtime.LocalRenderer()
    )
    return gen, result, outcome


@pytest.mark.parametrize("archetype", _NO_LATEX_ARCHETYPES)
def test_template_renders_and_passes_gate2(storyboard, archetype: Archetype) -> None:
    _, result, outcome = _render(storyboard, archetype)
    assert outcome is not None, "renderer was unavailable"
    assert outcome.ok, f"render failed:\n{outcome.stderr[-2000:]}"
    assert outcome.video_path is not None and outcome.video_path.stat().st_size > 5_000
    assert result.passed, result.findings


@pytest.mark.parametrize("archetype", _NO_LATEX_ARCHETYPES)
def test_estimate_matches_real_render(storyboard, archetype: Archetype) -> None:
    """THE test that keeps estimate_duration honest.

    If it fails after a change to a template body, the body and its duration model
    have diverged — fix ``estimate_duration``, do not loosen the tolerance. A loose
    tolerance here turns Gate 2's duration assertion into noise.
    """
    gen, _, outcome = _render(storyboard, archetype)
    estimate = registry.get(archetype).estimate_duration(gen.params_obj)
    actual = outcome.trace.total_duration
    assert abs(estimate - actual) / actual < 0.05, (
        f"{archetype.value}: estimate_duration says {estimate:.2f}s but the render "
        f"played {actual:.2f}s — build() and estimate_duration() have diverged"
    )


@pytest.mark.parametrize("archetype", _NO_LATEX_ARCHETYPES)
def test_trace_is_usable_by_gate3(storyboard, archetype: Archetype) -> None:
    """Gate 3 lands in Phase 2, but its input has to exist now — otherwise the
    mixin's instrumentation is unverified until it is too late to change cheaply."""
    _, _, outcome = _render(storyboard, archetype)
    trace = outcome.trace
    assert trace is not None and not trace.crashed
    assert trace.boundaries, "a scene that plays nothing renders as dead air"
    assert trace.frame_width > 0 and trace.frame_height > 0

    # Every boundary records geometry, and text carries a pixel height.
    for boundary in trace.boundaries:
        assert boundary.t_end >= boundary.t_start
    text_boxes = [b for bd in trace.boundaries for b in bd.boxes if b.is_text]
    assert text_boxes, "no text was measured; the legibility check would be blind"
    assert all(b.px_height > 0 for b in text_boxes)


@pytest.mark.parametrize("archetype", _NO_LATEX_ARCHETYPES)
def test_everything_stays_inside_the_safe_frame(storyboard, archetype: Archetype) -> None:
    """Gate 3's frame-containment assertion, run early against a real trace.

    ``fit_inside`` enforces the margin and params cannot reach it, so this should
    hold for any parameter set — which is the claim being tested.
    """
    _, _, outcome = _render(storyboard, archetype)
    trace = outcome.trace
    margin = settings().render.safe_margin
    half_w = trace.frame_width / 2 - margin
    half_h = trace.frame_height / 2 - margin

    offenders = [
        (
            bd.index,
            b.key,
            round(b.left, 2),
            round(b.right, 2),
            round(b.bottom, 2),
            round(b.top, 2),
        )
        for bd in trace.boundaries
        for b in bd.boxes
        if b.left < -half_w - 1e-6
        or b.right > half_w + 1e-6
        or b.bottom < -half_h - 1e-6
        or b.top > half_h + 1e-6
    ]
    assert not offenders, f"mobjects outside the safe frame: {offenders[:5]}"


@pytest.mark.parametrize("archetype", _NO_LATEX_ARCHETYPES)
def test_text_is_legible(storyboard, archetype: Archetype) -> None:
    """Gate 3's legibility floor: >= 18px equivalent at 1080p after all scaling."""
    _, _, outcome = _render(storyboard, archetype)
    floor = settings().render.min_text_px_at_1080p
    too_small = [
        (b.key, b.kind, round(b.px_height, 1))
        for bd in outcome.trace.boundaries
        for b in bd.boxes
        if b.is_text and b.px_height < floor
    ]
    assert not too_small, f"text below {floor}px at 1080p: {too_small[:5]}"


@pytest.mark.skipif(not _have_latex(), reason="transform_chain needs a LaTeX toolchain")
def test_transform_chain_renders(storyboard) -> None:
    gen, result, outcome = _render(storyboard, Archetype.TRANSFORM_CHAIN)
    assert outcome.ok, f"render failed:\n{outcome.stderr[-2000:]}"
    assert result.passed, result.findings
    estimate = registry.get(Archetype.TRANSFORM_CHAIN).estimate_duration(gen.params_obj)
    assert (
        abs(estimate - outcome.trace.total_duration) / outcome.trace.total_duration < 0.05
    )

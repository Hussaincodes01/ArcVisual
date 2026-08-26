"""Gate 3 — spatial coherence.

Every test here constructs a trace that a *successful* render could plausibly have
produced. That is the point of the gate: each of these scenes exits zero, writes a
video, and reports a sensible duration. Gate 2 passes all of them. They are still
broken, and only the bbox trace shows it.
"""

from __future__ import annotations

import pytest

from arcvisual.gates import g3_spatial
from arcvisual.storyboard import Archetype, Beat, SceneSpec, SourceSpan, quote_hash
from arcvisual.templates.base import BBox, Boundary, SceneTrace

FRAME_W, FRAME_H = 14.22, 8.0


def _spec() -> SceneSpec:
    return SceneSpec(
        id="s1",
        archetype=Archetype.PLOT_REVEAL,
        claim="a claim long enough to satisfy the schema minimum",
        concept_id="c0",
        span=SourceSpan(section_id="s000", start=0, end=5, quote_sha256=quote_hash("x")),
        params={},
        beats=(Beat(t=0.0, dur=2.0, caption="one"),),
    )


def _box(key="Text#0", *, left=-2.0, right=2.0, bottom=-1.0, top=1.0, text=True, px=40.0):
    return BBox(
        key=key,
        kind="Text",
        left=left,
        right=right,
        bottom=bottom,
        top=top,
        is_text=text,
        px_height=px if text else 0.0,
    )


def _trace(boxes, *, duration=4.0, boundaries=None, **kw) -> SceneTrace:
    if boundaries is None:
        boundaries = [
            Boundary(
                index=0, t_start=0.0, t_end=1.5, animation_names=["Create"], boxes=boxes
            ),
            Boundary(
                index=1,
                t_start=1.5,
                t_end=duration,
                animation_names=["FadeIn"],
                boxes=boxes,
            ),
        ]
    return SceneTrace(
        scene_id="s1",
        archetype="plot_reveal",
        frame_width=FRAME_W,
        frame_height=FRAME_H,
        total_duration=duration,
        boundaries=boundaries,
        **kw,
    )


# --------------------------------------------------------------------------- #


def test_a_healthy_scene_passes() -> None:
    result = g3_spatial.run(_trace([_box()]), _spec())
    assert result.passed, result.findings
    assert result.payload["min_text_px"] == 40.0


def test_missing_trace_is_a_failure_not_a_pass() -> None:
    """A gate that cannot check anything must not report success — that is how a
    broken scene ships while the dashboard shows green."""
    result = g3_spatial.run(None, _spec())
    assert not result.passed
    assert "must render through ArcSceneMixin" in result.findings[0]


# -- containment ------------------------------------------------------------- #


def test_content_past_the_frame_edge_blocks() -> None:
    """Manim animates a mobject past the camera edge and exits zero. Nothing else
    in the pipeline notices."""
    off = _box(left=-9.0, right=-7.5)  # frame half-width is 7.11
    result = g3_spatial.run(_trace([off]), _spec())
    assert not result.passed
    assert any("past the safe frame" in f for f in result.findings)


def test_content_inside_the_safe_margin_passes() -> None:
    edge = _box(left=-6.7, right=6.7, bottom=-3.6, top=3.6)
    assert g3_spatial.run(_trace([edge]), _spec()).passed


# -- legibility -------------------------------------------------------------- #


def test_text_below_the_legibility_floor_blocks() -> None:
    result = g3_spatial.run(_trace([_box(px=11.0)]), _spec())
    assert not result.passed
    assert any("below the 18px floor" in f for f in result.findings)
    # The message must point at the cause, not just the symptom.
    assert any("remove elements rather than shrinking" in f for f in result.findings)


def test_non_text_is_exempt_from_the_legibility_floor() -> None:
    """A 4px-tall gridline is fine; a 4px-tall label is not."""
    thin = _box(key="Line#0", bottom=-0.01, top=0.01, text=False)
    assert g3_spatial.run(_trace([thin, _box()]), _spec()).passed


# -- overlap ----------------------------------------------------------------- #


def test_overlapping_labels_are_reported() -> None:
    a = _box("Text#0", left=-1.0, right=1.0, bottom=-0.5, top=0.5)
    b = _box("Text#1", left=-0.9, right=1.1, bottom=-0.4, top=0.6)
    result = g3_spatial.run(_trace([a, b]), _spec())
    assert any("overlap" in f for f in result.findings)


def test_declared_overlaps_are_not_reported() -> None:
    """A label inside its own box is the normal case in architecture_flow; flagging
    it would make the gate useless there."""
    a = _box("Text#0", left=-1.0, right=1.0, bottom=-0.5, top=0.5)
    b = _box("Text#1", left=-0.9, right=1.1, bottom=-0.4, top=0.6)
    trace = _trace([a, b], declared_overlaps=[["Text#0", "Text#1"]])
    assert not g3_spatial.run(trace, _spec()).findings


def test_overlap_is_advisory_not_blocking() -> None:
    """It has real false positives — an annotation deliberately placed near a curve —
    so it is surfaced without spending a repair attempt."""
    a = _box("Text#0", left=-1.0, right=1.0, bottom=-0.5, top=0.5)
    b = _box("Text#1", left=-0.9, right=1.1, bottom=-0.4, top=0.6)
    result = g3_spatial.run(_trace([a, b]), _spec())
    assert result.passed
    assert result.payload["advisory"]
    assert not result.payload["blocking"]


def test_strict_advisory_makes_it_block() -> None:
    a = _box("Text#0", left=-1.0, right=1.0, bottom=-0.5, top=0.5)
    b = _box("Text#1", left=-0.9, right=1.1, bottom=-0.4, top=0.6)
    result = g3_spatial.run(_trace([a, b]), _spec(), strict_advisory=True)
    assert not result.passed


# -- dead air ---------------------------------------------------------------- #


def test_a_long_motionless_stretch_blocks() -> None:
    """The symptom of an animation that silently did nothing. Gate 2 sees a clean
    exit and a plausible duration, so this is the only check that catches it."""
    boundaries = [
        Boundary(
            index=0, t_start=0.0, t_end=1.0, animation_names=["Create"], boxes=[_box()]
        ),
    ]
    trace = _trace([_box()], duration=14.0, boundaries=boundaries)
    result = g3_spatial.run(trace, _spec())
    assert not result.passed
    assert any("nothing changes" in f for f in result.findings)


def test_declared_holds_are_exempt() -> None:
    """A 1.5s pause after a reveal IS the style contract, not a defect."""
    boundaries = [
        Boundary(
            index=0, t_start=0.0, t_end=1.0, animation_names=["Write"], boxes=[_box()]
        ),
    ]
    trace = _trace(
        [_box()], duration=5.0, boundaries=boundaries, declared_holds=[[1.0, 5.0]]
    )
    assert g3_spatial.run(trace, _spec()).passed


def test_a_scene_that_animates_nothing_blocks() -> None:
    trace = _trace([_box()], boundaries=[])
    result = g3_spatial.run(trace, _spec())
    assert not result.passed
    assert any("no animations at all" in f for f in result.findings)


# -- contrast ---------------------------------------------------------------- #


def test_contrast_is_measured_from_real_pixels() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    # ArcVisual's own palette: #E6EDF3 foreground on the #0E1116 canvas.
    frame = Image.new("RGB", (64, 64), (0x0E, 0x11, 0x16))
    for x in range(20, 44):
        for y in range(20, 44):
            frame.putpixel((x, y), (0xE6, 0xED, 0xF3))
    result = g3_spatial.run(_trace([_box()]), _spec(), frames=[frame])
    assert result.passed
    assert result.payload["contrast_ratio"] is not None
    assert result.payload["contrast_ratio"] > 4.5, "the style contract's own palette"


def test_low_contrast_is_reported_but_does_not_block() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    frame = Image.new("RGB", (64, 64), (0x0E, 0x11, 0x16))
    for x in range(20, 44):
        for y in range(20, 44):
            frame.putpixel((x, y), (0x20, 0x24, 0x2A))  # barely above the canvas
    result = g3_spatial.run(_trace([_box()]), _spec(), frames=[frame])
    assert result.passed, "a dim gridline is a design choice, not a broken scene"
    assert any("contrast" in f for f in result.findings)


def test_contrast_is_skipped_without_pixels() -> None:
    """A sandbox uploads and discards its video. The other four checks still run,
    and the payload says which ones actually happened."""
    result = g3_spatial.run(_trace([_box()]), _spec())
    assert "contrast" not in result.payload["checked"]
    assert set(result.payload["checked"]) == {
        "containment",
        "legibility",
        "overlap",
        "dead_air",
    }


# -- integration with the ladder --------------------------------------------- #


def test_gate3_findings_are_written_for_a_repair_prompt() -> None:
    """Findings are fed verbatim to the model, so they must say what to change."""
    result = g3_spatial.run(_trace([_box(px=9.0)]), _spec())
    finding = next(f for f in result.findings if "floor" in f)
    assert "remove elements" in finding
    assert len(finding) < 300, "a finding that long crowds out the repair context"


def test_contrast_is_measured_where_the_text_is() -> None:
    """Measured over the whole frame, contrast answers the wrong question.

    architecture_flow reported 3.6:1 while its labels were #E6EDF3 at roughly 12:1 —
    the median was dragged down by large 6%-opacity box fills the reader never has to
    read. Restricting the sample to the text region moved it to 5.7:1 and removed the
    false positive.
    """
    pytest.importorskip("PIL")
    from PIL import Image

    canvas = (0x0E, 0x11, 0x16)
    frame = Image.new("RGB", (200, 100), canvas)
    # Dim chrome over most of the frame — a muted box outline, not something to read.
    for x in range(0, 200):
        for y in range(0, 60):
            frame.putpixel((x, y), (0x2A, 0x2E, 0x34))
    # Bright text in a small region at the bottom.
    for x in range(20, 60):
        for y in range(70, 90):
            frame.putpixel((x, y), (0xE6, 0xED, 0xF3))

    # Frame is 14.22 x 8.0 units; the text sits low and left of centre.
    text_box = BBox(
        key="Text#0",
        kind="Text",
        left=-5.7,
        right=-2.8,
        bottom=-3.2,
        top=-1.6,
        is_text=True,
        px_height=40.0,
    )
    trace = _trace([text_box])
    result = g3_spatial.run(trace, _spec(), frames=[frame])
    assert result.payload["contrast_ratio"] is not None
    assert result.payload["contrast_ratio"] > 4.5, (
        "sampling the whole frame would let dim chrome mask legible text"
    )
    assert not any("contrast" in f for f in result.findings)


# --------------------------------------------------------------------------- #
# Environment failures are not scene failures
# --------------------------------------------------------------------------- #


def test_a_missing_toolchain_is_not_blamed_on_the_scene() -> None:
    """A machine with no LaTeX spent 21 scenes x 3 attempts = 63 renders retrying
    something no parameter change can fix, and recorded a 0% first-pass rate for
    transform_chain — a template that was never actually given a chance to run."""
    from arcvisual.gates.g2_runtime import environment_failure

    log = (
        "Traceback (most recent call last):\n"
        "RuntimeError: latex failed but did not produce a log file. "
        "Check your LaTeX installation.\n"
    )
    problem = environment_failure(log)
    assert problem is not None
    assert "no parameter change can fix this" in problem


def test_a_scene_error_is_still_the_scene_s_problem() -> None:
    """The distinction has to cut both ways: misclassifying a real scene bug as
    environmental would rob it of the repair attempts that would have fixed it."""
    from arcvisual.gates.g2_runtime import environment_failure

    assert environment_failure("TypeError: Mobject.scale() missing 1 argument") is None
    assert environment_failure("! Missing $ inserted") is None
    # A figure path the scene got wrong is not a missing binary.
    assert environment_failure("FileNotFoundError: 'figures/plot.png'") is None


def test_a_missing_binary_is_environmental() -> None:
    from arcvisual.gates.g2_runtime import environment_failure

    problem = environment_failure("FileNotFoundError: [Errno 2] not found: dvisvgm")
    assert problem is not None and "dvisvgm" in problem


def test_a_single_long_animation_is_not_dead_air() -> None:
    """The span of an animation is the animation, not a gap.

    An earlier version walked every boundary endpoint as one flat list, so a scene
    with one 8-second beat read as "nothing changes between 0.0s and 8.0s" — and
    scenes with few, long, deliberate beats were failed for being exactly what the
    template intended.
    """
    boundaries = [
        Boundary(
            index=0,
            t_start=0.0,
            t_end=8.0,
            animation_names=["Create"],
            boxes=[_box()],
        )
    ]
    trace = _trace([_box()], duration=8.0, boundaries=boundaries)
    result = g3_spatial.run(trace, _spec())
    assert result.passed, result.findings


def test_a_real_gap_between_animations_still_blocks() -> None:
    """The fix must not blind the check: silence BETWEEN beats is the real symptom."""
    boundaries = [
        Boundary(
            index=0, t_start=0.0, t_end=1.0, animation_names=["Create"], boxes=[_box()]
        ),
        Boundary(
            index=1, t_start=9.0, t_end=10.0, animation_names=["FadeIn"], boxes=[_box()]
        ),
    ]
    trace = _trace([_box()], duration=10.0, boundaries=boundaries)
    result = g3_spatial.run(trace, _spec())
    assert not result.passed
    assert any("nothing changes between 1.0s and 9.0s" in f for f in result.findings)


def test_trailing_silence_after_the_last_beat_blocks() -> None:
    boundaries = [
        Boundary(
            index=0, t_start=0.0, t_end=2.0, animation_names=["Create"], boxes=[_box()]
        )
    ]
    trace = _trace([_box()], duration=12.0, boundaries=boundaries)
    assert not g3_spatial.run(trace, _spec()).passed

"""Gate 3 — spatial coherence. Geometry and pixels, ~5s, no model call.

This is the gate that catches scenes which **render successfully and are still
broken**: an equation drifting off-frame, a label at 9px, two captions stacked on
each other, a scene that "plays" for twelve seconds while nothing moves. Gate 2
proves a render terminated; Gate 3 proves it produced something a person can read.

Its input is the bbox trace :class:`~arcvisual.templates.base.ArcSceneMixin` records
at every ``play()`` boundary — which is the whole reason templates render through
that mixin rather than as bare ``manim.Scene`` subclasses. Nothing here re-derives
geometry from pixels; it reads what the scene actually did.

Five checks, in the plan's order:

* **Frame containment** — no bbox outside the frame minus the safe margin.
* **Text legibility** — every text mobject at or above the 18px floor at 1080p.
* **Unintended overlap** — IoU between visible text pairs, unless the template
  declared the overlap deliberate (a label inside its own box, a split screen).
* **Contrast** — sampled from real pixels, foreground against background ≥ 4.5:1.
* **Dead air** — no interval beyond 2.5s with no visual change, excluding the
  holds a template declares on purpose.

**Blocking versus advisory.** Containment, legibility and dead air block: each makes
the scene unreadable or wrong. Overlap and contrast are advisory by default — both
have false positives (a deliberate annotation near a curve; a legitimately dim
gridline) and neither makes a scene useless. Advisory findings are recorded so the
per-archetype dashboard can show them without spending a repair attempt.
"""

from __future__ import annotations

import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcvisual.config import settings
from arcvisual.storyboard import GateResult, SceneSpec
from arcvisual.templates.base import SceneTrace

log = logging.getLogger(__name__)

#: IoU above which two *text* boxes are considered to be colliding. Text is the only
#: kind checked: shapes overlap constantly and legitimately (an arrow meeting a box),
#: whereas two overlapping labels are always a defect.
OVERLAP_IOU_THRESHOLD = 0.18

#: Contrast is sampled from this many evenly spaced frames. More is slower without
#: being more informative — a scene whose contrast fails does so throughout.
CONTRAST_SAMPLES = 5

#: Fraction of sampled pixels that must clear the contrast ratio.
CONTRAST_PASS_FRACTION = 0.85

#: How far above the canvas a pixel must sit, as a fraction of the frame's own
#: luminance range, to count as foreground. Relative to the peak because measured
#: frames put content on under 1% of pixels — a percentile rule reads the whole
#: frame as canvas, and a fixed delta misses dim content on a near-black background.
FOREGROUND_PEAK_FRACTION = 0.40

#: Below this luminance spread a frame is effectively one flat colour, so there is
#: no foreground to judge and contrast is not reported either way.
UNIFORM_FRAME_EPSILON = 0.002


@dataclass
class SpatialFindings:
    containment: list[str] = field(default_factory=list)
    legibility: list[str] = field(default_factory=list)
    dead_air: list[str] = field(default_factory=list)
    overlap: list[str] = field(default_factory=list)
    contrast: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[str]:
        """Findings that make a scene unreadable, so are worth a repair attempt."""
        return [*self.containment, *self.legibility, *self.dead_air]

    @property
    def advisory(self) -> list[str]:
        """Recorded and surfaced, but never spends an attempt on its own."""
        return [*self.overlap, *self.contrast]

    def all(self) -> list[str]:
        return [*self.blocking, *self.advisory]


def run(
    trace: SceneTrace | None,
    spec: SceneSpec,
    *,
    video_path: Path | None = None,
    frames: list[Any] | None = None,
    strict_advisory: bool = False,
) -> GateResult:
    """Assert spatial coherence over a recorded trace.

    ``video_path`` enables the pixel-based contrast check; without it (a sandbox that
    uploaded and discarded its file) the other four still run. A gate that silently
    does nothing is worse than one that says which half it could check, so the
    payload records what was actually examined.
    """
    t0 = time.perf_counter()

    if trace is None:
        return GateResult(
            gate=3,
            passed=False,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            findings=[
                "no bbox trace was produced, so spatial coherence could not be "
                "checked; the scene must render through ArcSceneMixin"
            ],
            payload={"checked": []},
        )

    cfg = settings().render
    findings = SpatialFindings()
    checked = ["containment", "legibility", "overlap", "dead_air"]

    _check_containment(trace, cfg.safe_margin, findings)
    _check_legibility(trace, cfg.min_text_px_at_1080p, findings)
    _check_overlap(trace, findings)
    _check_dead_air(trace, cfg.max_dead_air_s, findings)

    contrast_ratio = None
    if video_path is not None or frames is not None:
        checked.append("contrast")
        contrast_ratio = _check_contrast(
            video_path, frames, cfg.min_contrast_ratio, findings, trace=trace
        )

    blocking = findings.blocking + (findings.advisory if strict_advisory else [])
    return GateResult(
        gate=3,
        passed=not blocking,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        # Advisory findings ride along even when passing, so the dashboard sees them.
        findings=findings.all(),
        payload={
            "checked": checked,
            "blocking": findings.blocking,
            "advisory": findings.advisory,
            "boundaries": len(trace.boundaries),
            "min_text_px": _min_text_px(trace),
            "contrast_ratio": contrast_ratio,
            "declared_overlaps": len(trace.declared_overlaps),
        },
    )


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _check_containment(
    trace: SceneTrace, margin: float, findings: SpatialFindings
) -> None:
    """Nothing may sit outside the frame minus the safe margin.

    Off-frame drift is the failure a reader notices instantly and the renderer never
    reports: Manim happily animates a mobject past the camera edge and exits zero.
    """
    half_w = trace.frame_width / 2 - margin
    half_h = trace.frame_height / 2 - margin
    worst: dict[str, float] = {}

    for boundary in trace.boundaries:
        for box in boundary.boxes:
            over = max(
                -half_w - box.left,
                box.right - half_w,
                -half_h - box.bottom,
                box.top - half_h,
            )
            if over > 0.01 and over > worst.get(box.key, 0.0):
                worst[box.key] = over

    for key, over in sorted(worst.items(), key=lambda kv: -kv[1])[:4]:
        findings.containment.append(
            f"{key} extends {over:.2f} units past the safe frame; shrink it or "
            f"reduce what is on screen (the safe margin is {margin} units and is "
            "not overridable from parameters)"
        )


def _check_legibility(
    trace: SceneTrace, floor_px: float, findings: SpatialFindings
) -> None:
    """Every text mobject must clear the legibility floor at 1080p.

    Checked at 1080p regardless of the draft render's own resolution, because that
    is the size the reader eventually sees. Text below this is technically present
    and practically absent.
    """
    smallest: dict[str, float] = {}
    for boundary in trace.boundaries:
        for box in boundary.boxes:
            if not box.is_text or box.px_height <= 0:
                continue
            if box.px_height < smallest.get(box.key, math.inf):
                smallest[box.key] = box.px_height

    for key, px in sorted(smallest.items(), key=lambda kv: kv[1]):
        if px < floor_px:
            findings.legibility.append(
                f"{key} renders at {px:.1f}px at 1080p, below the {floor_px:.0f}px "
                "floor; this usually means too much is on screen and the template "
                "scaled everything down to fit — remove elements rather than "
                "shrinking them"
            )
        if len(findings.legibility) >= 4:
            break


def _check_overlap(trace: SceneTrace, findings: SpatialFindings) -> None:
    """Two text boxes visible together must not collide.

    Only text is checked, and only pairs the template did not declare. Shapes overlap
    legitimately all the time; two labels on top of each other never do.
    """
    declared = {frozenset(pair) for pair in trace.declared_overlaps if len(pair) >= 2}
    reported: set[frozenset[str]] = set()

    for boundary in trace.boundaries:
        texts = [b for b in boundary.boxes if b.is_text and b.width > 0 and b.height > 0]
        for i, a in enumerate(texts):
            for b in texts[i + 1 :]:
                pair = frozenset({a.key, b.key})
                if pair in declared or pair in reported:
                    continue
                # A declared pair may name a parent group rather than the leaves.
                if any(pair <= d for d in declared):
                    continue
                iou = a.iou(b)
                if iou > OVERLAP_IOU_THRESHOLD:
                    reported.add(pair)
                    findings.overlap.append(
                        f"{a.key} and {b.key} overlap (IoU {iou:.2f}) at "
                        f"{boundary.t_start:.1f}s; separate them, or declare the "
                        "overlap in the template if it is deliberate"
                    )
        if len(findings.overlap) >= 4:
            break


def _check_dead_air(trace: SceneTrace, max_gap: float, findings: SpatialFindings) -> None:
    """No long stretch where nothing changes.

    A scene that renders for twelve seconds and moves for two has usually lost most
    of its content to a silently failed animation — Gate 2 sees a clean exit and a
    plausible duration, so this is the only check that catches it. Holds the template
    declares are exempt: a deliberate 1.5s pause after a reveal is the style contract,
    not a defect.
    """
    if not trace.boundaries:
        findings.dead_air.append(
            "the scene played no animations at all; a template that animates nothing "
            "renders as a still frame"
        )
        return

    held = [(float(a), float(b)) for a, b in trace.declared_holds]

    def is_declared(start: float, end: float) -> bool:
        # A gap is exempt when a declared hold covers essentially all of it.
        covered = sum(max(0.0, min(end, hb) - max(start, ha)) for ha, hb in held)
        return covered >= (end - start) * 0.9

    # Dead air is the time BETWEEN animations, never the span of one. An earlier
    # version walked every boundary endpoint as a flat list, so a single 8-second
    # animation read as "nothing changes between 0.0s and 8.0s" — the interval that
    # was, in fact, the animation. Scenes with few long beats were failed for being
    # exactly what the template intended.
    ordered = sorted(trace.boundaries, key=lambda b: b.t_start)
    gaps: list[tuple[float, float]] = []
    if ordered[0].t_start > 0:
        gaps.append((0.0, ordered[0].t_start))
    for prev, nxt in itertools.pairwise(ordered):
        if nxt.t_start > prev.t_end:
            gaps.append((prev.t_end, nxt.t_start))
    if trace.total_duration > ordered[-1].t_end:
        gaps.append((ordered[-1].t_end, trace.total_duration))

    for start, end in gaps:
        gap = end - start
        if gap > max_gap and not is_declared(start, end):
            findings.dead_air.append(
                f"nothing changes between {start:.1f}s and {end:.1f}s ({gap:.1f}s); "
                "either add a beat there or shorten the scene"
            )
        if len(findings.dead_air) >= 3:
            break


def _min_text_px(trace: SceneTrace) -> float | None:
    values = [
        b.px_height
        for bd in trace.boundaries
        for b in bd.boxes
        if b.is_text and b.px_height > 0
    ]
    return round(min(values), 1) if values else None


# --------------------------------------------------------------------------- #
# Pixels
# --------------------------------------------------------------------------- #


def _text_region(
    trace: SceneTrace, width: int, height: int
) -> tuple[int, int, int, int] | None:
    """Pixel rect covering every text bbox in the trace, or None if there is none.

    Contrast is a question about LEGIBILITY, so it has to be asked where the text is.
    Measured over a whole frame it answers a different question and answers it badly:
    architecture_flow reported 3.6:1 while its labels were #E6EDF3 at roughly 12:1,
    because the median was dragged down by large 6%-opacity box fills. A dashboard
    number that indicts a scene for chrome the reader never squints at is worse than
    no number.
    """
    boxes = [b for bd in trace.boundaries for b in bd.boxes if b.is_text]
    if not boxes or trace.frame_width <= 0 or trace.frame_height <= 0:
        return None
    # Manim units are centre-origin with y up; images are top-left with y down.
    px_per_x = width / trace.frame_width
    px_per_y = height / trace.frame_height
    left = min(b.left for b in boxes) * px_per_x + width / 2
    right = max(b.right for b in boxes) * px_per_x + width / 2
    top = height / 2 - max(b.top for b in boxes) * px_per_y
    bottom = height / 2 - min(b.bottom for b in boxes) * px_per_y
    x0, x1 = max(0, int(left) - 2), min(width, int(right) + 2)
    y0, y1 = max(0, int(top) - 2), min(height, int(bottom) + 2)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return x0, y0, x1, y1


def _check_contrast(
    video_path: Path | None,
    frames: list[Any] | None,
    min_ratio: float,
    findings: SpatialFindings,
    *,
    trace: SceneTrace | None = None,
) -> float | None:
    """Sample real pixels and compare foreground against the canvas.

    Advisory rather than blocking: a dim gridline is a legitimate design choice, and
    failing a scene over one would spend a repair attempt on something a reader would
    not complain about. It is still measured, because a scene that is uniformly low
    contrast is genuinely hard to read and the dashboard should show it.
    """
    try:
        samples = frames if frames is not None else _sample_frames(video_path)
    except Exception as exc:
        log.info("contrast sampling unavailable: %s", exc)
        return None
    if not samples:
        return None

    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None

    ratios: list[float] = []
    for frame in samples:
        arr = np.asarray(frame.convert("RGB"), dtype=float) / 255.0
        lum_full = _relative_luminance(arr)
        # The canvas comes from the WHOLE frame (it is the background everywhere);
        # the ink is measured only where text is.
        region = (
            _text_region(trace, arr.shape[1], arr.shape[0]) if trace is not None else None
        )
        lum = (
            lum_full[region[1] : region[3], region[0] : region[2]] if region else lum_full
        )
        if lum.size == 0:
            continue
        # The canvas is the median of the FULL frame; it dominates on a dark theme.
        background = float(np.median(lum_full))

        # Foreground is thresholded relative to the frame's own PEAK, not by a fixed
        # delta and not by percentile. Measured on real frames, content covers under
        # 1% of pixels on this dark canvas: even the 99th percentile is still
        # background, so a percentile rule classifies the whole frame as canvas and
        # the check silently reports nothing. A fixed delta fails the other way,
        # missing dim content that sits close to the near-black canvas.
        peak = float(np.percentile(lum, 99.9))
        if peak - background < UNIFORM_FRAME_EPSILON:
            # A blank frame — the opening beat before anything is drawn. There is no
            # foreground to judge, and dead-air already covers a scene that stays this way.
            continue
        foreground = lum[
            lum >= background + FOREGROUND_PEAK_FRACTION * (peak - background)
        ]
        if foreground.size == 0:
            continue
        # The MEDIAN of the foreground is the representative ink colour. The dimmest
        # decile would be anti-aliased glyph edges, which are faint by construction
        # and would report every scene as low contrast.
        ink = float(np.median(foreground))
        ratios.append((max(ink, background) + 0.05) / (min(ink, background) + 0.05))

    if not ratios:
        return None

    ratio = round(sum(ratios) / len(ratios), 2)
    passing = sum(1 for r in ratios if r >= min_ratio) / len(ratios)
    if passing < CONTRAST_PASS_FRACTION:
        findings.contrast.append(
            f"foreground-to-background contrast averages {ratio:.1f}:1, below the "
            f"{min_ratio}:1 target on {(1 - passing):.0%} of sampled frames"
        )
    return ratio


def _sample_frames(video_path: Path | None) -> list[Any]:
    """Evenly spaced frames, via ffmpeg. Empty list when it cannot be done."""
    if video_path is None or not Path(video_path).exists():
        return []
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            return []

    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="arcvisual-contrast-") as tmp:
        out = Path(tmp) / "f-%02d.png"
        proc = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(video_path),
                "-vf",
                "thumbnail,fps=1/1,scale=640:-1",
                "-frames:v",
                str(CONTRAST_SAMPLES),
                "-y",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode != 0:
            log.info("ffmpeg frame sampling failed: %s", (proc.stderr or "")[-200:])
            return []
        # Load eagerly: the directory is removed on exit from this block.
        return [Image.open(p).copy() for p in sorted(Path(tmp).glob("f-*.png"))]


def _relative_luminance(rgb: Any) -> Any:
    """WCAG relative luminance, vectorised over an H x W x 3 array in 0..1."""
    import numpy as np

    linear = np.where(rgb <= 0.03928, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]

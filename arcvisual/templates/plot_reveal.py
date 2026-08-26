"""``plot_reveal`` — a results curve or table, revealed rather than presented.

Fires on a results figure worth interrogating. The reader sees the axes first,
then each series drawn in, then the annotation that says what to notice. A static
chart states a number; a revealed chart makes an argument about it.

**Series are explicit point lists, never expressions.** Letting the model supply
a formula to evaluate would mean executing model-authored arithmetic inside the
render — an unbounded-runtime and injection surface for a gain (smooth curves)
that resampled points already deliver. This is the single most important
parameter-design decision in the template library.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arcvisual.storyboard import Archetype
from arcvisual.templates.base import (
    ACCENT,
    FG,
    GOOD,
    MIN_BEAT_S,
    MUTED,
    REVEAL_HOLD_S,
    WARN,
    TemplateParams,
    clamp_text,
    manim_mod,
)

TEMPLATE_ID = "plot_reveal"
ARCHETYPE = Archetype.PLOT_REVEAL

_SERIES_COLORS = (ACCENT, WARN, GOOD, MUTED)


class Series(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=40)
    points: list[tuple[float, float]] = Field(min_length=2, max_length=200)

    _clamp_label = field_validator("label", mode="before")(clamp_text(40))
    dashed: bool = False
    #: Index into ``points`` to call out with a dot and the label.
    annotate_at: int | None = None

    @model_validator(mode="after")
    def _annotation_in_range(self) -> Series:
        if self.annotate_at is not None and not 0 <= self.annotate_at < len(self.points):
            raise ValueError(
                f"annotate_at={self.annotate_at} out of range for "
                f"{len(self.points)} points"
            )
        return self


class Params(TemplateParams):
    x_label: str = Field(max_length=40)
    y_label: str = Field(max_length=40)
    series: list[Series] = Field(min_length=1, max_length=4)
    x_range: tuple[float, float] | None = Field(
        default=None, description="Omit to fit the data with padding."
    )
    y_range: tuple[float, float] | None = None
    log_y: bool = False
    takeaway: str | None = Field(
        default=None,
        max_length=90,
        description="The one thing the reader should notice. Shown last.",
    )

    _clamp_x = field_validator("x_label", mode="before")(clamp_text(40))
    _clamp_y = field_validator("y_label", mode="before")(clamp_text(40))
    _clamp_takeaway = field_validator("takeaway", mode="before")(clamp_text(90))

    @field_validator("series", mode="before")
    @classmethod
    def _drop_undrawable_series(cls, value):
        """A series with fewer than two points cannot be plotted as a line.

        Dropping it keeps the rest of the chart, where rejecting threw away a scene
        that was three-quarters correct. Only an empty result still fails, because a
        plot with no series is not a plot.
        """
        if not isinstance(value, list):
            return value
        keep = [
            s for s in value if not isinstance(s, dict) or len(s.get("points") or []) >= 2
        ]
        return keep or value  # nothing survived: let the real error surface

    @model_validator(mode="after")
    def _ranges_ordered(self) -> Params:
        for name, rng in (("x_range", self.x_range), ("y_range", self.y_range)):
            if rng is not None and rng[1] <= rng[0]:
                raise ValueError(f"{name} must be increasing, got {rng}")
        if self.log_y:
            for s in self.series:
                if any(y <= 0 for _, y in s.points):
                    raise ValueError(
                        f"log_y set but series {s.label!r} has non-positive y values"
                    )
        return self

    def resolved_ranges(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """Data-fitted ranges with 8% padding, unless explicitly given."""
        xs = [x for s in self.series for x, _ in s.points]
        ys = [y for s in self.series for _, y in s.points]
        return (
            self.x_range or _padded(min(xs), max(xs)),
            self.y_range or _padded(min(ys), max(ys)),
        )


def _padded(lo: float, hi: float, frac: float = 0.08) -> tuple[float, float]:
    if hi == lo:
        return (lo - 1.0, hi + 1.0)
    pad = (hi - lo) * frac
    return (lo - pad, hi + pad)


def build(scene, params: Params) -> None:
    m = manim_mod()

    (x0, x1), (y0, y1) = params.resolved_ranges()
    axes = m.Axes(
        x_range=[x0, x1, _step(x0, x1)],
        y_range=[y0, y1, _step(y0, y1)],
        axis_config={"color": MUTED, "include_tip": False, "font_size": 22},
        tips=False,
    )
    labels = axes.get_axis_labels(
        m.Text(params.x_label, font_size=28, color=FG),
        m.Text(params.y_label, font_size=28, color=FG),
    )
    group = m.VGroup(axes, labels)
    scene.fit_inside(group, width_frac=0.92, height_frac=0.84)
    group.move_to(m.ORIGIN).shift(m.UP * 0.3)

    cap = None
    if params.captions:
        cap = scene.show_caption(None, params.captions[0])

    scene.play(m.Create(axes, lag_ratio=0.15), m.FadeIn(labels), run_time=1.2)
    scene.declare_overlap(axes, labels)

    legend_entries = []
    for i, s in enumerate(params.series):
        color = _SERIES_COLORS[i % len(_SERIES_COLORS)]
        curve = axes.plot_line_graph(
            x_values=[p[0] for p in s.points],
            y_values=[p[1] for p in s.points],
            line_color=color,
            add_vertex_dots=len(s.points) <= 24,
            vertex_dot_radius=0.045,
            stroke_width=3,
        )
        if s.dashed:
            curve["line_graph"] = m.DashedVMobject(curve["line_graph"], num_dashes=40)

        if params.captions and i + 1 < len(params.captions):
            cap = scene.show_caption(cap, params.captions[i + 1])

        # Progressive Create: the curve is drawn left to right, so the reader
        # reads the trend as it forms instead of decoding a finished picture.
        scene.play(m.Create(curve), run_time=1.6)

        tag = m.Text(s.label, font_size=26, color=color)
        legend_entries.append(tag)

        if s.annotate_at is not None:
            px, py = s.points[s.annotate_at]
            dot = m.Dot(axes.c2p(px, py), color=color, radius=0.07)
            note = m.Text(f"{s.label}: {_fmt(py)}", font_size=26, color=color)
            note.next_to(dot, m.UR, buff=0.15)
            scene.fit_inside(note, width_frac=0.4)
            # A point near the top-right corner puts its label past the edge. Gate 3
            # measured exactly this (0.21 units over) on a real render.
            scene.clamp_into_frame(note)
            scene.play(m.FadeIn(dot, scale=0.5), m.FadeIn(note), run_time=MIN_BEAT_S)
            scene.declare_overlap(dot, note)
            scene.hold(REVEAL_HOLD_S)

    if len(legend_entries) > 1:
        legend = m.VGroup(*legend_entries).arrange(m.DOWN, aligned_edge=m.LEFT, buff=0.16)
        half_w, half_h = scene.safe_frame()
        legend.move_to([half_w - legend.width / 2, half_h - legend.height / 2, 0])
        scene.clamp_into_frame(legend)
        scene.play(m.FadeIn(legend), run_time=MIN_BEAT_S)

    if params.takeaway:
        note = m.Text(params.takeaway, font_size=32, color=FG)
        scene.fit_inside(note, width_frac=0.8)
        note.move_to(m.ORIGIN).shift(m.DOWN * 2.6)
        scene.clamp_into_frame(note)
        scene.play(m.FadeIn(note, shift=m.UP * 0.2), run_time=1.0)
        scene.hold(REVEAL_HOLD_S)


def _step(lo: float, hi: float) -> float:
    """A tick step that yields roughly 5-8 ticks, rounded to something human."""
    span = hi - lo
    if span <= 0:
        return 1.0
    raw = span / 6.0
    mag = 10 ** _floor_log10(raw)
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mag * mult:
            return mag * mult
    return mag * 10


def _floor_log10(x: float) -> int:
    import math

    return math.floor(math.log10(abs(x))) if x else 0


def _fmt(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.3g}"


def estimate_duration(params: Params) -> float:
    """The runtime this template will actually play. Mirrors :func:`build`.

    See the note in ``transform_chain.estimate_duration``: the template owns
    timing, and ``test_estimate_matches_real_render`` keeps this in step with the
    body.
    """
    total = 0.0
    if params.captions:
        total += MIN_BEAT_S  # first caption
    total += 1.2  # Create(axes) + FadeIn(labels)
    for i, s in enumerate(params.series):
        if params.captions and i + 1 < len(params.captions):
            total += MIN_BEAT_S  # caption swap
        total += 1.6  # Create(curve)
        if s.annotate_at is not None:
            total += MIN_BEAT_S + REVEAL_HOLD_S  # dot + note, then hold
    if len(params.series) > 1:
        total += MIN_BEAT_S  # legend
    if params.takeaway:
        total += max(1.0, MIN_BEAT_S) + REVEAL_HOLD_S
    return round(total, 3)


def simplify(params: Params) -> Params | None:
    """Repair rung 2: the leading series only, no legend, no annotation.

    Overcrowding is the usual Gate 3 failure here, and it is almost always the
    third and fourth series plus their labels.
    """
    if len(params.series) <= 1 and not params.takeaway:
        return None
    lead = params.series[0].model_copy(update={"annotate_at": None})
    return Params(
        x_label=params.x_label,
        y_label=params.y_label,
        series=[lead],
        x_range=params.x_range,
        y_range=params.y_range,
        log_y=params.log_y,
        takeaway=params.takeaway,
        captions=params.captions[:2],
    )

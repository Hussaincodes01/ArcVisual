"""``architecture_flow`` — a model or system diagram with data moving through it.

Fires on the paper's architecture figure. The value over the paper's own static
diagram is *sequence*: blocks appear in dependency order and the data path
animates along the edges, so the reader learns the order of operations rather
than reconstructing it from arrow directions.

**Layout is computed, not supplied.** The model gives each node a column and a
row *index*; this file resolves those to coordinates. Letting a model position
things in Manim units is how you get off-frame drift and overlapping boxes — the
two failures Gate 3 exists to catch. Grid indices cannot express either.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arcvisual.storyboard import Archetype
from arcvisual.templates.base import (
    ACCENT,
    FG,
    MIN_BEAT_S,
    MUTED,
    REVEAL_HOLD_S,
    TemplateParams,
    clamp_text,
    manim_mod,
)

TEMPLATE_ID = "architecture_flow"
ARCHETYPE = Archetype.ARCHITECTURE_FLOW

#: Vertical strip left free for the caption line at the bottom of the frame.
CAPTION_RESERVE = 0.9
#: Fraction of a grid cell a node box occupies. The remainder is the gutter the
#: arrows run through, so this cannot go much above 0.8 without arrows touching boxes.
BOX_WIDTH_OF_CELL = 0.80
BOX_HEIGHT_OF_CELL = 0.72
#: Ceiling on height/width, so a single-row diagram does not stretch into a letterbox.
#: The grid is scaled up to fill the frame afterwards, so this shapes the boxes
#: rather than capping the diagram's final size.
MAX_BOX_ASPECT = 0.46
#: Height fraction the assembled grid is grown into, leaving the caption strip clear.
FRAME_FILL_HEIGHT = 0.80
#: Starting label size. Shrunk per box to fit, so this is an upper bound rather than
#: a fixed value — it is set well above the legibility floor on purpose.
NODE_FONT_SIZE = 30


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    label: str = Field(max_length=36)
    column: int = Field(ge=0, le=5, description="Left-to-right position index.")
    row: int = Field(default=0, ge=0, le=4, description="Top-to-bottom within column.")
    emphasis: bool = Field(
        default=False, description="Accent colour — the paper's contribution."
    )

    # The label is display text; the id is a reference other params resolve against,
    # so that one still rejects rather than being quietly trimmed into a new name.
    _clamp_label = field_validator("label", mode="before")(clamp_text(36))


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    label: str | None = Field(default=None, max_length=24)
    dashed: bool = False

    _clamp_label = field_validator("label", mode="before")(clamp_text(24))


class Params(TemplateParams):
    nodes: list[Node] = Field(min_length=2, max_length=12)
    edges: list[Edge] = Field(min_length=1, max_length=18)
    stages: list[list[str]] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Reveal groups: node ids appearing together, in order. Omit to "
            "reveal column by column."
        ),
    )
    trace_path: list[str] = Field(
        default_factory=list,
        max_length=12,
        description=(
            "Node ids forming the data path to animate at the end. This is what "
            "the static figure cannot show."
        ),
    )

    @model_validator(mode="after")
    def _referential_integrity(self) -> Params:
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("duplicate node ids")
        for e in self.edges:
            missing = {e.src, e.dst} - ids
            if missing:
                raise ValueError(f"edge {e.src}->{e.dst} references unknown {missing}")
            if e.src == e.dst:
                raise ValueError(f"self-loop on {e.src}: not renderable in this template")
        for group in self.stages:
            if unknown := set(group) - ids:
                raise ValueError(f"stage references unknown nodes {unknown}")
        if unknown := set(self.trace_path) - ids:
            raise ValueError(f"trace_path references unknown nodes {unknown}")
        occupied: set[tuple[int, int]] = set()
        for n in self.nodes:
            if (n.column, n.row) in occupied:
                raise ValueError(f"two nodes share grid cell ({n.column}, {n.row})")
            occupied.add((n.column, n.row))
        return self

    def reveal_groups(self) -> list[list[str]]:
        if self.stages:
            return self.stages
        by_col: dict[int, list[str]] = {}
        for n in sorted(self.nodes, key=lambda n: (n.column, n.row)):
            by_col.setdefault(n.column, []).append(n.id)
        return [by_col[c] for c in sorted(by_col)]


def build(scene, params: Params) -> None:
    m = manim_mod()

    half_w, half_h = scene.safe_frame()
    cols = sorted({n.column for n in params.nodes})
    rows = sorted({n.row for n in params.nodes})
    usable_h = 2 * half_h - CAPTION_RESERVE
    col_gap = (2 * half_w) / max(len(cols), 1)
    row_gap = usable_h / max(len(rows), 1)

    # Boxes are sized from the grid cell, with NO absolute ceiling. An earlier
    # version capped them at 2.9 x 1.1 units, which on a three-node diagram left the
    # content filling 84% of the frame's width but only 61% of its height — half the
    # frame unused, and node labels sitting on the 18px legibility floor. The grid
    # already bounds the size; a second hardcoded cap only shrank things further.
    # The aspect clamp keeps a wide single-row diagram from becoming a letterbox.
    box_w = col_gap * BOX_WIDTH_OF_CELL
    box_h = min(row_gap * BOX_HEIGHT_OF_CELL, box_w * MAX_BOX_ASPECT)

    boxes: dict[str, object] = {}
    for n in params.nodes:
        ci, ri = cols.index(n.column), rows.index(n.row)
        x = -half_w + col_gap * (ci + 0.5)
        y = (usable_h / 2) - row_gap * (ri + 0.5) + 0.6
        color = ACCENT if n.emphasis else MUTED
        rect = m.RoundedRectangle(
            corner_radius=0.12,
            width=box_w,
            height=box_h,
            stroke_color=color,
            stroke_width=2.5,
            fill_opacity=0.06,
            fill_color=color,
        )
        text = m.Text(n.label, font_size=NODE_FONT_SIZE, color=FG)
        # Fit to the box WITHOUT dropping below the legibility floor. Plain scaling
        # produced a 17.0px label on a real run — one pixel short — and Gate 3
        # correctly failed the scene after a full render had already been paid for.
        scene.fit_text_legibly(text, box_w * 0.86)
        node = m.VGroup(rect, text)
        text.move_to(rect.get_center())
        node.move_to([x, y, 0])
        # A label inside its own box is intentional, not an overlap defect.
        scene.declare_overlap(rect, text)
        boxes[n.id] = node

    # Scale the assembled node grid to fill the frame BEFORE the arrows are built,
    # so the arrows are computed from final positions. Grid arithmetic alone leaves
    # whatever space it did not claim empty — measured at 64% of frame height on a
    # three-node row. Arrows must come after, or they would point at pre-scale
    # coordinates.
    if boxes:
        grid = m.VGroup(*boxes.values())
        scene.fill_frame(grid, width_frac=0.98, height_frac=FRAME_FILL_HEIGHT)
        grid.move_to([0, CAPTION_RESERVE / 2, 0])

    # (mobject, dashed) — dashed edges must use Create, since GrowArrow needs a
    # real Arrow and a DashedVMobject wrapper is not one.
    arrows: dict[tuple[str, str], tuple[object, bool]] = {}
    edge_labels: dict[tuple[str, str], object] = {}
    for e in params.edges:
        a, b = boxes[e.src], boxes[e.dst]
        arrow = m.Arrow(
            start=a.get_center(),
            end=b.get_center(),
            buff=max(box_w, box_h) / 2 + 0.08,
            stroke_width=2.5,
            color=MUTED,
            max_tip_length_to_length_ratio=0.12,
        )
        drawable: object = m.DashedVMobject(arrow, num_dashes=14) if e.dashed else arrow
        arrows[(e.src, e.dst)] = (drawable, e.dashed)
        if e.label:
            lab = m.Text(e.label, font_size=22, color=MUTED)
            lab.move_to(arrow.get_center()).shift(m.UP * 0.22)
            scene.clamp_into_frame(lab)
            edge_labels[(e.src, e.dst)] = lab
            scene.declare_overlap(arrow, lab)

    cap = None
    revealed: set[str] = set()
    for gi, group in enumerate(params.reveal_groups()):
        if gi < len(params.captions):
            cap = scene.show_caption(cap, params.captions[gi])

        new_nodes = [boxes[nid] for nid in group if nid not in revealed]
        if new_nodes:
            scene.play(
                m.AnimationGroup(
                    *[m.FadeIn(nd, shift=m.RIGHT * 0.3) for nd in new_nodes],
                    lag_ratio=0.18,
                ),
                run_time=max(MIN_BEAT_S, 0.5 + 0.22 * len(new_nodes)),
            )
        revealed.update(group)

        # Draw only the edges whose endpoints are both on screen — an arrow to
        # nothing is the commonest way a staged diagram reads as broken.
        ready = [k for k in arrows if k[0] in revealed and k[1] in revealed]
        to_draw = [arrows.pop(k) for k in ready]
        labs = [edge_labels.pop(k) for k in ready if k in edge_labels]
        if to_draw:
            anims = [
                m.Create(mob) if dashed else m.GrowArrow(mob) for mob, dashed in to_draw
            ]
            scene.play(
                m.AnimationGroup(*anims, lag_ratio=0.12),
                *([m.FadeIn(m.VGroup(*labs))] if labs else []),
                run_time=max(MIN_BEAT_S, 0.4 + 0.15 * len(to_draw)),
            )
        scene.hold(0.9)

    if len(params.trace_path) >= 2:
        if params.captions and len(params.captions) > len(params.reveal_groups()):
            cap = scene.show_caption(cap, params.captions[-1])
        pulse = m.Dot(radius=0.11, color=ACCENT)
        pulse.move_to(boxes[params.trace_path[0]].get_center())
        scene.play(m.FadeIn(pulse, scale=0.4), run_time=MIN_BEAT_S)
        for nid in params.trace_path[1:]:
            scene.play(
                pulse.animate.move_to(boxes[nid].get_center()),
                rate_func=m.rate_functions.ease_in_out_sine,
                run_time=0.9,
            )
        scene.play(m.FadeOut(pulse, scale=1.6), run_time=MIN_BEAT_S)
        scene.hold(REVEAL_HOLD_S)


def estimate_duration(params: Params) -> float:
    """The runtime this template will actually play. Mirrors :func:`build`.

    Note the edge bookkeeping: an edge is drawn in the first group where *both*
    endpoints are on screen, so the timeline depends on the reveal order and
    cannot be derived from the edge count alone.
    """
    total = 0.0
    revealed: set[str] = set()
    remaining = {(e.src, e.dst) for e in params.edges}
    groups = params.reveal_groups()

    for gi, group in enumerate(groups):
        if gi < len(params.captions):
            total += MIN_BEAT_S  # caption swap
        new_nodes = [n for n in group if n not in revealed]
        if new_nodes:
            total += max(MIN_BEAT_S, 0.5 + 0.22 * len(new_nodes))
        revealed.update(group)
        ready = [k for k in remaining if k[0] in revealed and k[1] in revealed]
        if ready:
            total += max(MIN_BEAT_S, 0.4 + 0.15 * len(ready))
        remaining -= set(ready)
        total += 0.9  # hold(0.9) -- a wait(), so no play() floor applies

    if len(params.trace_path) >= 2:
        if params.captions and len(params.captions) > len(groups):
            total += MIN_BEAT_S
        total += MIN_BEAT_S  # FadeIn(pulse)
        total += 0.9 * (len(params.trace_path) - 1)  # one hop per step
        total += MIN_BEAT_S  # FadeOut(pulse)
        total += REVEAL_HOLD_S
    return round(total, 3)


def simplify(params: Params) -> Params | None:
    """Repair rung 2: drop edge labels and the pulse, reveal column by column.

    What usually fails here is edge-label crowding on a dense graph, and the
    diagram still carries its claim without them.
    """
    if not any(e.label for e in params.edges) and not params.trace_path:
        return None
    return Params(
        nodes=params.nodes,
        edges=[e.model_copy(update={"label": None}) for e in params.edges],
        stages=[],
        trace_path=[],
        captions=params.captions[: len({n.column for n in params.nodes})],
    )

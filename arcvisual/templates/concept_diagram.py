"""``concept_diagram`` — the model draws the idea, then animates it step by step.

The other client-renderable templates are fixed shapes: a chain of equations, a
plot, a row of boxes. Given only those, a model explaining attention reaches for the
one that can hold the formula and writes ``softmax(QKᵀ/√dₖ)V`` out a line at a time —
the formula, animated, which teaches nothing the static formula did not. A reader
needs to *see* the query compared against every key, the scores becoming weights,
the weights mixing the values.

So this template gives the model a drawing vocabulary instead of a shape:

* **Elements** — ``block`` (a component), ``op`` (an operator: x, +, softmax),
  ``tokens`` (a sequence of words or patches), ``stack`` (a set of vectors),
  ``grid`` (a matrix whose cells can light up: attention, a kernel, a confusion
  matrix), ``bars`` (a distribution: softmax weights, gate scores), ``text`` (an
  annotation or a short formula fragment) and ``container`` (a dashed region that
  groups others: "Encoder x N", "one expert").
* **Connections** — arrows between elements, optionally labelled or dashed.
* **Steps** — the script. Each step has one caption and says what appears, what is
  in focus, what flows along which path, which cells light up and which labels
  change. A step is one beat of the explanation.

**Layout is computed, not supplied**, as in ``architecture_flow``: the model gives
grid indices (column, row, spans) and this file and the browser renderer resolve
them to coordinates. Grid indices cannot express off-frame drift; two elements in
one cell is rejected outright.

**Structural constraints reject, cosmetic ones coerce.** A connection or step that
names an element that does not exist fails validation, because the model meant
something we cannot draw. An element no step reveals, a mark outside a grid, or
values on an unnormalised scale are fixed here — the intent is unambiguous and a
repair attempt would be wasted on it. Normalisation happens once, in Python, and the
stored params are the normalised ones, so the browser never re-derives it.
"""

from __future__ import annotations

import re
from typing import Literal

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
    clamp_each,
    clamp_text,
    manim_mod,
)

TEMPLATE_ID = "concept_diagram"
ARCHETYPE = Archetype.CONCEPT_DIAGRAM

Kind = Literal["block", "op", "tokens", "stack", "grid", "bars", "text", "container"]
Tone = Literal["neutral", "accent", "input", "output", "muted"]

#: Kinds made of indexed items, which `marks` can point into.
ITEM_KINDS = frozenset({"tokens", "stack", "grid", "bars"})
MAX_ITEMS = 8
MAX_COLUMNS = 8
MAX_ROWS = 6

# -- the timeline, shared with reader/lib/scenes/plans.ts ------------------- #
#: Seconds a step holds after its last animation, so the reader can look.
STEP_HOLD_S = 1.2
FOCUS_S = 1.0
MARK_S = 1.0
HOP_S = MIN_BEAT_S
#: Runtime ceiling, kept under Render.max_runtime_s (75s) so Gate 2 never trips on a
#: scene this file already shortened. A constant, not a setting: validation has to
#: give the same answer on the control plane and in the render worker.
MAX_RUNTIME_S = 70.0
#: Stops a flow is cut to when a script runs long.
SHORT_FLOW = 4


class Element(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    kind: Kind = Field(
        description=(
            "block: a component. op: an operator such as ×, +, ⊕, softmax, concat. "  # noqa: RUF001
            "tokens: a sequence of words/patches (put them in `cells`). stack: a set "
            "of vectors (one per cell, e.g. q₁ q₂ q₃). grid: a size x size matrix whose "
            "cells can light up (attention, kernel). bars: a distribution (heights "
            "in `values`). text: an annotation or a SHORT formula fragment. "
            "container: a dashed region spanning other elements."
        )
    )
    label: str = Field(
        max_length=40,
        description="Name shown on the element. Plain text with Unicode maths, no LaTeX.",
    )
    column: int = Field(
        ge=0, le=MAX_COLUMNS - 1, description="Grid column, left to right."
    )
    row: int = Field(
        default=0, ge=0, le=MAX_ROWS - 1, description="Grid row, top to bottom."
    )
    col_span: int = Field(default=1, ge=1, le=MAX_COLUMNS)
    row_span: int = Field(default=1, ge=1, le=MAX_ROWS)
    cells: list[str] = Field(
        default_factory=list,
        max_length=MAX_ITEMS,
        description="Item labels for tokens/stack/bars, or row/column labels for grid.",
    )
    size: int = Field(
        default=0,
        ge=0,
        le=MAX_ITEMS,
        description="Item count for tokens/stack/bars, or N for an N x N grid. 0 = len(cells).",
    )
    values: list[float] = Field(
        default_factory=list,
        max_length=MAX_ITEMS * MAX_ITEMS,
        description="bars: one height per bar. grid: N x N intensities, row-major.",
    )
    tone: Tone = Field(
        default="neutral",
        description="accent marks the paper's contribution; input/output mark the ends.",
    )
    note: str = Field(
        default="",
        max_length=32,
        description="Small secondary line: a shape like 'n x d', or a role.",
    )

    _clamp_label = field_validator("label", mode="before")(clamp_text(40))
    _clamp_note = field_validator("note", mode="before")(clamp_text(32))
    _clamp_cells = field_validator("cells", mode="after")(clamp_each(14))

    @field_validator("label", "note", mode="after")
    @classmethod
    def _plain(cls, value: str) -> str:
        return plain_label(value)

    def item_count(self) -> int:
        """How many items this element draws. 0 for kinds without items."""
        if self.kind not in ITEM_KINDS:
            return 0
        n = self.size or len(self.cells)
        if self.kind == "bars" and not n:
            n = len(self.values)
        return max(2 if self.kind == "grid" else 1, min(n or 3, MAX_ITEMS))


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    label: str | None = Field(default=None, max_length=24)
    dashed: bool = Field(
        default=False, description="Dashed for a skip/residual or optional path."
    )

    _clamp_label = field_validator("label", mode="before")(clamp_text(24))


class Mark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(description="id of a tokens, stack, bars or grid element.")
    item: int = Field(ge=0, description="Item index, or the ROW for a grid.")
    col: int = Field(default=0, ge=0, description="Grid column. Ignored for other kinds.")


class Relabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = Field(max_length=40)

    _clamp_label = field_validator("label", mode="before")(clamp_text(40))

    @field_validator("label", mode="after")
    @classmethod
    def _plain(cls, value: str) -> str:
        return plain_label(value)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: str = Field(max_length=90, description="One plain sentence for this beat.")
    show: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Element ids that appear in this step. Arrows appear once both ends are shown.",
    )
    focus: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Element ids to highlight while everything else dims.",
    )
    flow: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="A path of element ids: a pulse travels along it, in order.",
    )
    marks: list[Mark] = Field(
        default_factory=list,
        max_length=12,
        description="Items or grid cells to light up in this step.",
    )
    relabel: list[Relabel] = Field(
        default_factory=list,
        max_length=4,
        description="Change an element's label, e.g. 'scores' becomes 'weights'.",
    )

    _clamp_caption = field_validator("caption", mode="before")(clamp_text(90))


class Params(TemplateParams):
    title: str | None = Field(default=None, max_length=60)
    elements: list[Element] = Field(min_length=2, max_length=16)
    connections: list[Connection] = Field(default_factory=list, max_length=20)
    steps: list[Step] = Field(min_length=1, max_length=8)

    _clamp_title = field_validator("title", mode="before")(clamp_text(60))

    @model_validator(mode="after")
    def _normalise(self) -> Params:
        ids = [e.id for e in self.elements]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate element ids")
        known = set(ids)
        by_id = {e.id: e for e in self.elements}

        # -- references that mean something we cannot draw: reject ---------- #
        seen_edges: set[tuple[str, str]] = set()
        edges: list[Connection] = []
        for c in self.connections:
            if missing := {c.src, c.dst} - known:
                raise ValueError(
                    f"connection {c.src}->{c.dst} references unknown {missing}"
                )
            if c.src == c.dst:
                raise ValueError(f"self-loop on {c.src}: not drawable")
            if (c.src, c.dst) not in seen_edges:
                seen_edges.add((c.src, c.dst))
                edges.append(c)
        self.connections = edges
        for i, step in enumerate(self.steps):
            named = set(step.show) | set(step.focus) | set(step.flow)
            named |= {m.target for m in step.marks} | {r.id for r in step.relabel}
            if missing := named - known:
                raise ValueError(f"step {i + 1} references unknown elements {missing}")

        # -- two drawn things in one grid cell: reject ---------------------- #
        occupied: dict[tuple[int, int], str] = {}
        for e in self.elements:
            if e.kind == "container":
                continue
            for c in range(e.column, min(e.column + e.col_span, MAX_COLUMNS)):
                for r in range(e.row, min(e.row + e.row_span, MAX_ROWS)):
                    if (c, r) in occupied:
                        raise ValueError(
                            f"{e.id} and {occupied[(c, r)]} share grid cell ({c}, {r}); "
                            "give each its own column/row"
                        )
                    occupied[(c, r)] = e.id

        # -- cosmetic: coerce ----------------------------------------------- #
        for e in self.elements:
            e.col_span = min(e.col_span, MAX_COLUMNS - e.column)
            e.row_span = min(e.row_span, MAX_ROWS - e.row)
            if e.kind in ITEM_KINDS:
                e.size = e.item_count()
                e.values = _normalise_values(e)
            else:
                e.size, e.values, e.cells = 0, [], []

        for step in self.steps:
            step.marks = [m for m in step.marks if _mark_fits(by_id[m.target], m)]
            # A flow needs two stops; a one-element "path" is a focus.
            if len(step.flow) == 1:
                step.focus = list(dict.fromkeys([*step.focus, *step.flow]))
                step.flow = []

        self.steps = _reveal_everything(self.elements, self.steps)
        self.steps = _fit_runtime(self)
        if not self.captions:
            self.captions = [s.caption for s in self.steps]
        return self


def plain_label(text: str) -> str:
    """Strip TeX delimiters a model left in a label. Diagram text is drawn as text.

    ``$QK^T$`` becomes ``QK^T``; the browser additionally maps ``^T`` and friends to
    Unicode. A label is never shown as raw source.
    """
    text = text.replace("$", "")
    text = re.sub(r"\\(?:mathrm|text|mathbf|operatorname)\{([^}]*)\}", r"\1", text)
    return " ".join(text.split())


def _normalise_values(e: Element) -> list[float]:
    """Values on a 0-1 scale, the length the element draws. Empty means "unspecified"."""
    n = e.size
    want = n * n if e.kind == "grid" else n
    if e.kind not in ("bars", "grid") or not e.values:
        return []
    vals = [abs(float(v)) for v in e.values[:want]]
    vals += [0.0] * (want - len(vals))
    top = max(vals) or 1.0
    return [round(v / top, 4) for v in vals]


def _mark_fits(target: Element, mark: Mark) -> bool:
    if target.kind not in ITEM_KINDS:
        return False
    n = target.size or target.item_count()
    if mark.item >= n:
        return False
    return target.kind != "grid" or mark.col < n


def _within(container: Element, e: Element) -> bool:
    return (
        container.column <= e.column < container.column + container.col_span
        and container.row <= e.row < container.row + container.row_span
    )


def _reveal_everything(elements: list[Element], steps: list[Step]) -> list[Step]:
    """Every element appears in some step, no later than the first step that uses it.

    An element the script forgot to reveal would otherwise be drawn never, and one
    focused before it is shown would pulse on an empty canvas. A container appears
    with the first element inside it, so the region frames its contents as they
    arrive rather than after.
    """
    shown_at: dict[str, int] = {}
    for i, step in enumerate(steps):
        used = [
            *step.show,
            *step.focus,
            *step.flow,
            *(m.target for m in step.marks),
            *(r.id for r in step.relabel),
        ]
        for eid in used:
            shown_at.setdefault(eid, i)
    for e in elements:
        if e.kind == "container":
            inside = [
                shown_at[o.id]
                for o in elements
                if o.id in shown_at and _within(e, o) and o.kind != "container"
            ]
            if inside:
                shown_at[e.id] = min(shown_at.get(e.id, len(steps)), min(inside))
        shown_at.setdefault(e.id, 0)
    out = []
    order = {e.id: k for k, e in enumerate(elements)}
    for i, step in enumerate(steps):
        reveal = sorted((eid for eid, at in shown_at.items() if at == i), key=order.get)
        out.append(step.model_copy(update={"show": reveal}))
    return out


def _fit_runtime(params: Params) -> list[Step]:
    """Trim a script that would outrun the runtime ceiling.

    Gate 2 rejects an over-long video, but in client mode there is no Gate 2, and a
    scene the reader has to sit through for a minute and a half has lost them either
    way. Long flow paths go first (a pulse visiting eight stops says no more than one
    visiting four), then trailing steps — whose elements are revealed in the last step
    kept, so the finished frame still shows the whole drawing.
    """
    steps = list(params.steps)

    def total() -> float:
        return estimate_duration(params.model_copy(update={"steps": steps}))

    if total() <= MAX_RUNTIME_S:
        return steps
    steps = [
        s.model_copy(update={"flow": s.flow[:SHORT_FLOW]})
        if len(s.flow) > SHORT_FLOW
        else s
        for s in steps
    ]
    while len(steps) > 1 and total() > MAX_RUNTIME_S:
        dropped = steps.pop()
        last = steps[-1]
        steps[-1] = last.model_copy(update={"show": [*last.show, *dropped.show]})
    return steps


# --------------------------------------------------------------------------- #
# The timeline — mirrored exactly by planConceptDiagram in the reader
# --------------------------------------------------------------------------- #


def reveal_time(n: int) -> float:
    return max(MIN_BEAT_S, 0.5 + 0.2 * n)


def connect_time(n: int) -> float:
    return max(MIN_BEAT_S, 0.4 + 0.15 * n)


def step_segments(params: Params) -> list[dict[str, float]]:
    """Per step, the duration of each phase. 0 means the phase does not play."""
    shown: set[str] = set()
    remaining = [(c.src, c.dst) for c in params.connections]
    out = []
    for step in params.steps:
        fresh = [eid for eid in step.show if eid not in shown]
        shown.update(step.show)
        ready = [k for k in remaining if k[0] in shown and k[1] in shown]
        remaining = [k for k in remaining if k not in ready]
        out.append(
            {
                "caption": MIN_BEAT_S,
                "reveal": reveal_time(len(fresh)) if fresh else 0.0,
                "connect": connect_time(len(ready)) if ready else 0.0,
                "relabel": MIN_BEAT_S if step.relabel else 0.0,
                "focus": FOCUS_S if step.focus else 0.0,
                "marks": MARK_S if step.marks else 0.0,
                "flow": (2 + len(step.flow) - 1) * HOP_S if len(step.flow) >= 2 else 0.0,
                "hold": STEP_HOLD_S,
            }
        )
    return out


def estimate_duration(params: Params) -> float:
    """The runtime :func:`build` plays. Every phase is a ``play`` at or above the
    pacing floor, except the holds, which are waits."""
    total = sum(sum(seg.values()) for seg in step_segments(params))
    if params.title:
        total += MIN_BEAT_S  # the title fades in before the first step
    return round(total + REVEAL_HOLD_S, 3)


def captions_for(params: Params) -> list[str]:
    """One caption per step: the steps ARE the beats of this scene."""
    return [s.caption for s in params.steps]


def simplify(params: Params) -> Params | None:
    """Repair rung 2: drop labels on arrows, cell marks and values.

    What crowds a diagram is annotation, not structure; the drawing still carries its
    claim without them.
    """
    if not any(c.label for c in params.connections) and not any(
        s.marks for s in params.steps
    ):
        return None
    return params.model_copy(
        update={
            "connections": [
                c.model_copy(update={"label": None}) for c in params.connections
            ],
            "steps": [s.model_copy(update={"marks": []}) for s in params.steps],
        }
    )


# --------------------------------------------------------------------------- #
# Manim
# --------------------------------------------------------------------------- #

_TONE_STROKE = {
    "neutral": FG,
    "accent": ACCENT,
    "input": WARN,
    "output": GOOD,
    "muted": MUTED,
}
CAPTION_RESERVE = 0.9
LABEL_FONT = 28
SMALL_FONT = 22


def _layout(params: Params, half_w: float, half_h: float, top: float):
    """Element id -> (cx, cy, w, h) in Manim units, from grid indices."""
    cols = max(e.column + e.col_span for e in params.elements)
    rows = max(e.row + e.row_span for e in params.elements)
    c0 = min(e.column for e in params.elements)
    r0 = min(e.row for e in params.elements)
    cols, rows = cols - c0, rows - r0
    width = 2 * half_w
    height = top - (-half_h + CAPTION_RESERVE)
    cw, ch = width / cols, height / rows
    out = {}
    for e in params.elements:
        x = -half_w + cw * (e.column - c0)
        y = top - ch * (e.row - r0)
        out[e.id] = (
            x + cw * e.col_span / 2,
            y - ch * e.row_span / 2,
            cw * e.col_span,
            ch * e.row_span,
        )
    return out


def _label(scene, m, text: str, max_w: float, size: int = LABEL_FONT, color: str = FG):
    mob = m.Text(text or " ", font_size=size, color=color)
    scene.fit_text_legibly(mob, max_w)
    return mob


def _draw(scene, m, e: Element, box: tuple[float, float, float, float]):
    """One element as a VGroup, plus its item sub-mobjects (for marks)."""
    cx, cy, w, h = box
    gut = min(w, h) * 0.12
    w, h = w - 2 * gut, h - 2 * gut
    stroke = _TONE_STROKE[e.tone]
    items: list = []
    label = None

    if e.kind == "container":
        rect = m.RoundedRectangle(
            corner_radius=0.18,
            width=w + gut,
            height=h + gut,
            stroke_color=MUTED,
            stroke_width=2,
        )
        body = m.DashedVMobject(rect, num_dashes=40)
        label = _label(scene, m, e.label, w * 0.6, SMALL_FONT, MUTED)
        label.next_to(rect.get_corner(m.UL), m.DOWN + m.RIGHT, buff=0.08)
        group = m.VGroup(body, label)
    elif e.kind == "op":
        r = min(w, h) * 0.32
        shape = m.Circle(
            radius=r,
            stroke_color=stroke,
            stroke_width=3,
            fill_color=stroke,
            fill_opacity=0.12,
        )
        label = _label(scene, m, e.label, max(r * 1.7, 0.6))
        group = m.VGroup(shape, label)
    elif e.kind == "text":
        label = _label(scene, m, e.label, w)
        group = m.VGroup(label)
        if e.note:
            note = _label(scene, m, e.note, w, SMALL_FONT, MUTED).next_to(
                label, m.DOWN, 0.1
            )
            group.add(note)
    elif e.kind in ITEM_KINDS:
        n = e.size
        label = _label(scene, m, e.label, w, SMALL_FONT, MUTED)
        area_h = h - label.height - 0.15
        if e.kind == "grid":
            side = min(w, area_h) / n
            cells = m.VGroup(
                *[
                    m.Square(
                        side_length=side * 0.92,
                        stroke_color=stroke,
                        stroke_width=1.5,
                        fill_color=ACCENT,
                        fill_opacity=0.08 + 0.7 * (e.values[i] if e.values else 0),
                    )
                    for i in range(n * n)
                ]
            ).arrange_in_grid(rows=n, cols=n, buff=side * 0.08)
            items = list(cells)
            body = m.VGroup(cells)
            # `cells` label the grid's rows and columns (the tokens of an attention
            # matrix), as the browser renderer draws them: across the top, down the
            # left.
            for i, name in enumerate(e.cells[:n]):
                col = _label(scene, m, name, side * 1.1, SMALL_FONT, MUTED)
                col.next_to(cells[i], m.UP, buff=0.08)
                row = _label(scene, m, name, side * 1.1, SMALL_FONT, MUTED)
                row.next_to(cells[i * n], m.LEFT, buff=0.1)
                body.add(col, row)
        elif e.kind == "bars":
            bw = min(w / n * 0.7, 0.6)
            vals = e.values or [0.6] * n
            bars = m.VGroup(
                *[
                    m.Rectangle(
                        width=bw,
                        height=max(0.05, area_h * 0.7 * v),
                        stroke_color=stroke,
                        stroke_width=1.5,
                        fill_color=stroke,
                        fill_opacity=0.35,
                    )
                    for v in vals[:n]
                ]
            ).arrange(m.RIGHT, buff=bw * 0.4, aligned_edge=m.DOWN)
            items = list(bars)
            body = m.VGroup(bars)
            names = [
                _label(scene, m, e.cells[i], bw * 1.4, SMALL_FONT).next_to(
                    bar, m.DOWN, 0.08
                )
                for i, bar in enumerate(bars)
                if i < len(e.cells)
            ]
            if names:
                body.add(*names)
        else:  # tokens, stack
            iw = min(w / n * 0.82, 1.4)
            ih = min(area_h * (0.45 if e.kind == "tokens" else 0.8), 1.6)
            boxes = []
            for i in range(n):
                cell = m.RoundedRectangle(
                    corner_radius=0.08,
                    width=iw,
                    height=ih,
                    stroke_color=stroke,
                    stroke_width=2,
                    fill_color=stroke,
                    fill_opacity=0.08,
                )
                if i < len(e.cells):
                    t = _label(scene, m, e.cells[i], iw * 0.9, SMALL_FONT)
                    t.move_to(cell)
                    boxes.append(m.VGroup(cell, t))
                    scene.declare_overlap(cell, t)
                else:
                    boxes.append(m.VGroup(cell))
            body = m.VGroup(*boxes).arrange(m.RIGHT, buff=iw * 0.15)
            items = boxes
        scene.fit_inside(body)
        if body.width > w:
            body.scale(w / body.width)
        if body.height > area_h:
            body.scale(area_h / body.height)
        label.next_to(body, m.DOWN, buff=0.12)
        group = m.VGroup(body, label)
    else:  # block
        bh = min(h, max(0.8, w * 0.55))
        rect = m.RoundedRectangle(
            corner_radius=0.14,
            width=w,
            height=bh,
            stroke_color=stroke,
            stroke_width=2.5,
            fill_color=stroke,
            fill_opacity=0.08,
        )
        label = _label(scene, m, e.label, w * 0.86)
        group = m.VGroup(rect, label)
        if e.note:
            note = _label(scene, m, e.note, w * 0.86, SMALL_FONT, MUTED)
            m.VGroup(label, note).arrange(m.DOWN, buff=0.08).move_to(rect)
            group.add(note)
            scene.declare_overlap(rect, note)
        scene.declare_overlap(rect, label)

    group.move_to([cx, cy, 0])
    return group, items, label


def build(scene, params: Params) -> None:
    m = manim_mod()
    half_w, half_h = scene.safe_frame()
    top = half_h
    title = None
    if params.title:
        title = _label(scene, m, params.title, 2 * half_w * 0.9, 32, FG)
        title.move_to([0, half_h - title.height / 2, 0])
        top = half_h - title.height - 0.25
    boxes = _layout(params, half_w, half_h, top)

    drawn: dict[str, tuple] = {}
    for e in params.elements:
        drawn[e.id] = _draw(scene, m, e, boxes[e.id])

    # Grow the assembled diagram into the band between the title and the caption
    # BEFORE routing arrows, so arrows are computed from final positions. Grid cells
    # sized for the worst case leave a sparse diagram filling half the frame.
    whole = m.VGroup(*[group for group, _, _ in drawn.values()])
    band_bottom = -half_h + CAPTION_RESERVE
    band = top - band_bottom
    scene.fill_frame(whole, width_frac=0.98, height_frac=band / (2 * half_h) * 0.96)
    whole.move_to([0, band_bottom + band / 2, 0])

    arrows: dict[tuple[str, str], object] = {}
    for c in params.connections:
        a, b = drawn[c.src][0], drawn[c.dst][0]
        arrow = m.Arrow(
            a.get_center(),
            b.get_center(),
            buff=0,
            stroke_width=3,
            color=MUTED,
            max_tip_length_to_length_ratio=0.1,
        )
        # Trim to the boxes' edges so arrows meet shapes instead of crossing them.
        start, end = _edge(m, a, b), _edge(m, b, a)
        arrow.put_start_and_end_on(start, end)
        mob = m.DashedVMobject(arrow, num_dashes=12) if c.dashed else arrow
        if c.label:
            lab = m.Text(c.label, font_size=SMALL_FONT, color=MUTED)
            lab.next_to(arrow, m.UP, buff=0.12)
            scene.clamp_into_frame(lab)
            scene.declare_overlap(arrow, lab)
            mob = m.VGroup(mob, lab)
        arrows[(c.src, c.dst)] = mob

    if title is not None:
        scene.play(m.FadeIn(title, shift=m.DOWN * 0.15), run_time=MIN_BEAT_S)

    cap = None
    shown: set[str] = set()
    for step, seg in zip(params.steps, step_segments(params), strict=True):
        cap = scene.show_caption(cap, step.caption)

        fresh = [eid for eid in step.show if eid not in shown]
        shown.update(step.show)
        if fresh:
            scene.play(
                m.LaggedStart(
                    *[m.FadeIn(drawn[eid][0], scale=0.92) for eid in fresh],
                    lag_ratio=0.18,
                ),
                run_time=seg["reveal"],
            )
        ready = [k for k in list(arrows) if k[0] in shown and k[1] in shown]
        if ready:
            scene.play(
                m.LaggedStart(*[m.Create(arrows.pop(k)) for k in ready], lag_ratio=0.12),
                run_time=seg["connect"],
            )
        if step.relabel:
            swaps = []
            for r in step.relabel:
                _, _, old = drawn[r.id]
                if old is None:
                    continue
                new = m.Text(r.label, font_size=old.font_size, color=old.get_color())
                scene.fit_text_legibly(new, max(old.width * 1.4, 1.0))
                new.move_to(old)
                swaps.append(m.Transform(old, new))
            if swaps:
                scene.play(*swaps, run_time=seg["relabel"])
            else:
                scene.wait(seg["relabel"])
        if step.focus:
            scene.play(
                *[
                    m.Indicate(drawn[eid][0], color=ACCENT, scale_factor=1.06)
                    for eid in step.focus
                ],
                run_time=seg["focus"],
            )
        if step.marks:
            lit = []
            for mk in step.marks:
                target = next(e for e in params.elements if e.id == mk.target)
                items = drawn[mk.target][1]
                idx = mk.item * target.size + mk.col if target.kind == "grid" else mk.item
                if idx < len(items):
                    lit.append(m.Indicate(items[idx], color=WARN, scale_factor=1.15))
            if lit:
                scene.play(*lit, run_time=seg["marks"])
            else:
                scene.wait(seg["marks"])
        if len(step.flow) >= 2:
            dot = m.Dot(radius=0.1, color=WARN).move_to(
                drawn[step.flow[0]][0].get_center()
            )
            scene.play(m.FadeIn(dot, scale=0.4), run_time=HOP_S)
            for eid in step.flow[1:]:
                scene.play(
                    dot.animate.move_to(drawn[eid][0].get_center()),
                    rate_func=m.rate_functions.ease_in_out_sine,
                    run_time=HOP_S,
                )
            scene.play(m.FadeOut(dot, scale=1.6), run_time=HOP_S)
        scene.hold(STEP_HOLD_S)
    scene.hold(REVEAL_HOLD_S)


def _edge(m, a, b):
    """Where the line from a's centre towards b leaves a's bounding box."""
    import numpy as np

    ca, cb = a.get_center(), b.get_center()
    d = cb - ca
    hw, hh = a.width / 2 + 0.08, a.height / 2 + 0.08
    sx = hw / abs(d[0]) if abs(d[0]) > 1e-6 else float("inf")
    sy = hh / abs(d[1]) if abs(d[1]) > 1e-6 else float("inf")
    s = min(sx, sy, 1.0)
    return np.array(ca + d * s)

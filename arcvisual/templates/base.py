"""``ArcSceneMixin`` — the instrumentation every template renders through.

**This module exists before the templates on purpose.** Gate 3 asserts frame
containment, text legibility and overlap from a record of every mobject's
bounding box at each ``play()`` boundary. That record can only be produced by
intercepting ``Scene.play``, so no template may render as a bare
``manim.Scene``. Establishing that in Phase 1 avoids retrofitting fifteen
templates when Gate 3 lands in Phase 2.

The mixin also owns the style contract mechanically rather than by prompt: the
dark canvas, the pacing floor, and safe margins that params cannot override.

**Why templates are functions, not classes.** A template exports a Pydantic
``Params`` model and a ``build(scene, params)`` function. The real Manim
subclass is assembled at render time by :func:`make_scene`. This keeps the
registry, the schema and Gate 1 importable on a machine with no Manim
installed, and it keeps the generated module down to a handful of lines — which
is the whole point of templates over free-form codegen.
"""

from __future__ import annotations

import inspect
import json
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arcvisual.config import Render, settings

# --------------------------------------------------------------------------- #
# The style contract, as constants
# --------------------------------------------------------------------------- #

BG = "#0E1116"
FG = "#E6EDF3"
ACCENT = "#58A6FF"
MUTED = "#7D8590"
WARN = "#F0883E"
GOOD = "#3FB950"

#: Minimum seconds per beat, and the hold after a reveal. "Deliberate pacing."
MIN_BEAT_S = 0.8
REVEAL_HOLD_S = 1.5


def manim_mod():
    """Import Manim on demand, with an actionable error when it is absent."""
    try:
        import manim
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RenderEnvironmentMissing(
            "Manim CE is not installed here. Rendering runs inside the "
            "manimcommunity/manim image (see arcvisual/render/modal_app.py); "
            "for local work install the render extra: pip install -e .[render]"
        ) from exc
    return manim


class RenderEnvironmentMissing(RuntimeError):
    """Manim is unavailable — expected on control-plane and dev machines."""


# --------------------------------------------------------------------------- #
# Instrumentation records — Gate 3's input
# --------------------------------------------------------------------------- #


@dataclass
class BBox:
    """One mobject's extent at one play() boundary, in Manim units."""

    key: str  # stable identifier: "Text#3", "MathTex#0"
    kind: str
    left: float
    right: float
    bottom: float
    top: float
    is_text: bool
    px_height: float = 0.0  # rendered height at 1080p, for legibility

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def iou(self, other: BBox) -> float:
        ix = max(0.0, min(self.right, other.right) - max(self.left, other.left))
        iy = max(0.0, min(self.top, other.top) - max(self.bottom, other.bottom))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        union = self.width * self.height + other.width * other.height - inter
        return inter / union if union > 0 else 0.0


@dataclass
class Boundary:
    """The visual state immediately after one ``play()`` call."""

    index: int
    t_start: float
    t_end: float
    animation_names: list[str]
    boxes: list[BBox] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


@dataclass
class SceneTrace:
    """Everything Gate 3 needs, written beside the video as ``trace.json``."""

    scene_id: str
    archetype: str
    frame_width: float
    frame_height: float
    total_duration: float = 0.0
    boundaries: list[Boundary] = field(default_factory=list)
    #: Overlaps the template declares intentional (a label on a box, a split
    #: screen), so Gate 3 does not flag deliberate composition.
    declared_overlaps: list[list[str]] = field(default_factory=list)
    #: Intervals where nothing moves by design — an exempted held reveal.
    declared_holds: list[list[float]] = field(default_factory=list)
    crashed: bool = False
    crash_repr: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> SceneTrace:
        raw = json.loads(text)
        bounds = [
            Boundary(
                index=b["index"],
                t_start=b["t_start"],
                t_end=b["t_end"],
                animation_names=b["animation_names"],
                boxes=[BBox(**bb) for bb in b["boxes"]],
            )
            for b in raw.pop("boundaries", [])
        ]
        return cls(boundaries=bounds, **raw)


# --------------------------------------------------------------------------- #
# The mixin
# --------------------------------------------------------------------------- #


class ArcSceneMixin:
    """Mixed in *before* ``manim.Scene`` so ``play`` and ``setup`` intercept.

    Never inherit this alongside anything that also overrides ``play``; the MRO
    contract is ``(ArcSceneMixin, manim.Scene)`` and nothing else.
    """

    scene_id: str = "unnamed"
    archetype: str = "unknown"
    params: Any = None
    build_fn: Callable[[Any, Any], None] | None = None
    trace_path: str = "trace.json"

    render_config: Render = settings().render

    # -- lifecycle --------------------------------------------------------- #

    def setup(self) -> None:  # Manim hook
        m = manim_mod()
        self.camera.background_color = BG
        self._trace = SceneTrace(
            scene_id=self.scene_id,
            archetype=self.archetype,
            frame_width=float(m.config.frame_width),
            frame_height=float(m.config.frame_height),
        )
        self._play_index = 0
        self._key_counter: dict[str, int] = {}
        self._keys: dict[int, str] = {}

    def construct(self) -> None:
        """Always writes a trace, even on a crash — Gate 2 needs to know *where*
        a render died, not only that it did."""
        try:
            if self.build_fn is None:  # pragma: no cover - misassembled scene
                raise RuntimeError(f"scene {self.scene_id} has no build function")
            self.build_fn(self, self.params)
        except BaseException as exc:
            self._trace.crashed = True
            self._trace.crash_repr = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._trace.total_duration = float(getattr(self.renderer, "time", 0.0))
            self._write_trace()

    # -- instrumentation --------------------------------------------------- #

    def play(self, *animations: Any, **kwargs: Any) -> None:
        """Record state after every animation group, and enforce the pacing
        floor here rather than trusting the model to respect it."""
        rt = kwargs.get("run_time")
        if rt is not None:
            kwargs["run_time"] = max(float(rt), MIN_BEAT_S)
        t_start = float(self.renderer.time)
        super().play(*animations, **kwargs)  # type: ignore[misc]
        self._record_boundary(t_start, animations)

    def hold(self, seconds: float = REVEAL_HOLD_S) -> None:
        """A declared pause. Exempt from Gate 3's dead-air check."""
        t0 = float(self.renderer.time)
        self.wait(seconds)  # type: ignore[attr-defined]
        self._trace.declared_holds.append([t0, float(self.renderer.time)])

    def declare_overlap(self, *mobjects: Any) -> None:
        self._trace.declared_overlaps.append([self._key_for(m) for m in mobjects])

    def _key_for(self, mobject: Any) -> str:
        oid = id(mobject)
        if oid not in self._keys:
            kind = type(mobject).__name__
            n = self._key_counter.get(kind, 0)
            self._key_counter[kind] = n + 1
            self._keys[oid] = f"{kind}#{n}"
        return self._keys[oid]

    def _text_types(self) -> tuple[type, ...]:
        m = manim_mod()
        return tuple(
            t
            for t in (
                getattr(m, "Text", None),
                getattr(m, "MathTex", None),
                getattr(m, "Tex", None),
                getattr(m, "MarkupText", None),
                getattr(m, "SingleStringMathTex", None),
            )
            if isinstance(t, type)
        )

    def _record_boundary(self, t_start: float, animations: Sequence[Any]) -> None:
        m = manim_mod()
        text_types = self._text_types()
        px_per_unit = 1080.0 / float(m.config.frame_height)

        boxes: list[BBox] = []
        for mob in self.mobjects:  # type: ignore[attr-defined]
            # Walk into groups so text nested in a VGroup is still measured, but
            # do not record every Bezier submobject — only whole mobjects and
            # text leaves. Otherwise a single Axes floods the trace.
            candidates = [mob]
            candidates.extend(
                leaf
                for leaf in mob.get_family()
                if leaf is not mob and isinstance(leaf, text_types)
            )
            for leaf in candidates:
                box = _measure(leaf, text_types, px_per_unit, self._key_for(leaf))
                if box is not None:
                    boxes.append(box)

        self._trace.boundaries.append(
            Boundary(
                index=self._play_index,
                t_start=t_start,
                t_end=float(self.renderer.time),
                animation_names=[type(a).__name__ for a in animations],
                boxes=_dedupe(boxes),
            )
        )
        self._play_index += 1

    def _write_trace(self) -> None:
        try:
            out = Path(self.trace_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(self._trace.to_json(), encoding="utf-8")
        except OSError:  # pragma: no cover - never fail a render over the trace
            pass

    # -- layout helpers every template shares ------------------------------ #

    def safe_frame(self) -> tuple[float, float]:
        """Usable half-width / half-height after the mandatory safe margin."""
        m = manim_mod()
        margin = self.render_config.safe_margin
        return (
            float(m.config.frame_width) / 2 - margin,
            float(m.config.frame_height) / 2 - margin,
        )

    def fit_inside(
        self, mobject: Any, width_frac: float = 1.0, height_frac: float = 1.0
    ) -> Any:
        """Scale a mobject down until it sits inside the safe frame.

        Templates call this on everything model-parameterised, which is why
        frame containment is rarely the gate that fails: the margin is not
        reachable from params.
        """
        half_w, half_h = self.safe_frame()
        max_w, max_h = 2 * half_w * width_frac, 2 * half_h * height_frac
        if mobject.width > max_w:
            mobject.scale(max_w / mobject.width)
        if mobject.height > max_h:
            mobject.scale(max_h / mobject.height)
        return mobject

    def fill_frame(
        self,
        mobject: Any,
        width_frac: float = 0.98,
        height_frac: float = 0.9,
        max_scale: float = 3.0,
    ) -> Any:
        """Scale a mobject UP to fill the safe frame, then centre it.

        The complement to :meth:`fit_inside`, which only ever shrinks. Composing a
        diagram from grid arithmetic and leaving it at that wastes whatever space the
        arithmetic did not claim — a three-node row measured 89% of the frame's width
        but 64% of its height, with empty bands above and below. Sizing the parts,
        then scaling the whole, fills the frame at any node count.

        Scaling up enlarges nested text along with everything else, which is the
        point: it lifts labels off the legibility floor instead of pushing them onto it.
        """
        half_w, half_h = self.safe_frame()
        max_w, max_h = 2 * half_w * width_frac, 2 * half_h * height_frac
        if mobject.width <= 0 or mobject.height <= 0:
            return mobject
        factor = min(max_w / mobject.width, max_h / mobject.height, max_scale)
        if factor > 1.0:
            mobject.scale(factor)
        return mobject

    def clamp_into_frame(self, mobject: Any) -> Any:
        """Shift a mobject until its bounding box lies inside the safe frame.

        The companion to :meth:`fit_inside`, which resizes but never moves. Anything
        positioned relative to *data* — a label beside a plotted point, a note next to
        a node — can land anywhere the data does, including past the edge. Scaling it
        down does not help: a small label off-frame is still off-frame.

        Gate 3 catches this after the fact; this stops it happening, which is cheaper
        than a repair attempt.
        """
        half_w, half_h = self.safe_frame()
        dx = dy = 0.0
        if mobject.get_right()[0] > half_w:
            dx = half_w - mobject.get_right()[0]
        elif mobject.get_left()[0] < -half_w:
            dx = -half_w - mobject.get_left()[0]
        if mobject.get_top()[1] > half_h:
            dy = half_h - mobject.get_top()[1]
        elif mobject.get_bottom()[1] < -half_h:
            dy = -half_h - mobject.get_bottom()[1]
        if dx or dy:
            mobject.shift([dx, dy, 0])
        return mobject

    def fit_text_legibly(self, text_mobject: Any, max_width: float) -> Any:
        """Shrink text to a width, but never below the legibility floor.

        Templates scale a label down to fit its box, and that scaling can push it
        under 18px-at-1080p — Gate 3 then fails the scene at, say, 17.0px, one pixel
        short, after a full render. The fix is to stop shrinking at the floor and
        ELIDE the text instead: a truncated label a reader can read beats a complete
        one they cannot.
        """
        m = manim_mod()
        floor_units = self.render_config.min_text_px_at_1080p * (
            float(m.config.frame_height) / 1080.0
        )
        if text_mobject.width <= max_width:
            return text_mobject

        scale = max_width / text_mobject.width
        if text_mobject.height * scale >= floor_units:
            text_mobject.scale(scale)
            return text_mobject

        # Scaling to fit would go below the floor. Shrink only as far as the floor
        # allows, then drop characters until the remainder fits at that size.
        allowed = floor_units / text_mobject.height
        if allowed < 1.0:
            text_mobject.scale(allowed)
        raw = getattr(text_mobject, "text", "") or ""
        while len(raw) > 4 and text_mobject.width > max_width:
            raw = raw[:-2]
            replacement = m.Text(
                raw.rstrip() + "…",
                font_size=text_mobject.font_size,
                color=text_mobject.get_color(),
            )
            replacement.move_to(text_mobject.get_center())
            text_mobject.become(replacement)
        return text_mobject

    def caption(self, text: str) -> Any:
        """Bottom-anchored narration line, styled once for every template."""
        m = manim_mod()
        _, half_h = self.safe_frame()
        cap = m.Text(text, font_size=30, color=MUTED)
        self.fit_inside(cap, width_frac=0.9)
        cap.move_to([0, -half_h + cap.height / 2, 0])
        return cap

    def show_caption(self, current: Any, text: str) -> Any:
        """Swap the caption line, transforming rather than cutting."""
        m = manim_mod()
        nxt = self.caption(text)
        if current is None:
            self.play(m.FadeIn(nxt, shift=m.UP * 0.2), run_time=MIN_BEAT_S)
        else:
            self.play(m.ReplacementTransform(current, nxt), run_time=MIN_BEAT_S)
        return nxt


def _measure(
    leaf: Any, text_types: tuple[type, ...], px_per_unit: float, key: str
) -> BBox | None:
    try:
        if leaf.get_num_points() == 0 and not leaf.submobjects:
            return None
        left = float(leaf.get_left()[0])
        right = float(leaf.get_right()[0])
        bottom = float(leaf.get_bottom()[1])
        top = float(leaf.get_top()[1])
    except (AttributeError, IndexError, ValueError):  # pragma: no cover
        return None
    if not all(map(_finite, (left, right, bottom, top))):
        return None
    is_text = isinstance(leaf, text_types)
    return BBox(
        key=key,
        kind=type(leaf).__name__,
        left=left,
        right=right,
        bottom=bottom,
        top=top,
        is_text=is_text,
        px_height=(top - bottom) * px_per_unit if is_text else 0.0,
    )


def _finite(x: float) -> bool:
    return x == x and abs(x) != float("inf")


def _dedupe(boxes: list[BBox]) -> list[BBox]:
    seen: dict[str, BBox] = {}
    for b in boxes:
        seen[b.key] = b
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Template protocol and the lazy class factory
# --------------------------------------------------------------------------- #


def clamp_text(limit: int):
    """A ``before`` validator that TRIMS an over-long display string.

    The rule this encodes, learned the expensive way: **cosmetic constraints coerce,
    structural constraints reject.** A ``takeaway`` six characters over its cap is not
    a reason to discard a whole scene, but that is exactly what ``max_length`` does —
    it cost a real scene its entire repair budget on its third attempt, after the
    first two had already been spent on other rejections.

    Length limits here exist so text fits on screen. Trimming satisfies that intent;
    refusing does not. Anything load-bearing — a source span, a node id, a series with
    too few points to draw — still rejects, because coercing those would produce a
    confidently wrong animation rather than a slightly shorter caption.
    """

    def _clamp(value: Any) -> Any:
        if not isinstance(value, str) or len(value) <= limit:
            return value
        # shorten() trims on WORD boundaries, so a single long token — a URL, an
        # identifier, a run of no-space text — collapses to just the placeholder.
        # A 100-character title came back as "…". Fall back to a hard cut so the
        # text survives in a readable form.
        short = textwrap.shorten(value, width=limit, placeholder="…")
        if len(short) < limit // 2:
            short = value[: max(1, limit - 1)].rstrip() + "…"
        return short

    return _clamp


def coerce_caption_list(value: Any) -> Any:
    """Captions arrive as a list of strings, or near enough.

    A model that emits a number, or ``null`` for a skipped beat, has not made a
    meaningful mistake — dropping the nulls and stringifying the scalars is what it
    meant. Dicts and lists are left alone so they still fail loudly: those signal a
    genuinely different shape, not a near miss.
    """
    if not isinstance(value, list):
        return value
    out = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (int, float, bool)):
            out.append(str(item))
        else:
            out.append(item)  # let validation reject it
    return out


class TemplateParams(BaseModel):
    """Base for every template's parameter model.

    ``captions`` mirrors the beat captions from the ``SceneSpec``; codegen copies
    them across so a template body never needs the storyboard. ``extra="forbid"``
    is deliberate: a hallucinated parameter name must fail Gate 1 loudly rather
    than be silently ignored and produce a subtly wrong animation.
    """

    model_config = ConfigDict(extra="forbid")

    captions: list[str] = Field(default_factory=list, max_length=12)

    _coerce_captions = field_validator("captions", mode="before")(coerce_caption_list)


class BuildFn(Protocol):
    """A template's body. Receives the live scene and validated params."""

    def __call__(self, scene: Any, params: Any) -> None: ...


def make_scene(
    scene_id: str,
    archetype: str,
    build_fn: BuildFn,
    params: Any,
    trace_path: str = "trace.json",
    module: str | None = None,
) -> type:
    """Assemble the real Manim subclass. Called only inside a render container.

    Returns a fresh class so two scenes in one process cannot share state.

    ``module`` must be the *generated* module's ``__name__``. Manim's scene
    discovery keeps only classes whose ``__module__`` equals the module it loaded,
    and ``type()`` would otherwise stamp this class with ``templates.base`` — so
    the CLI would report "there are no scenes inside that module" and the render
    would exit 0 having done nothing. When omitted it is read from the calling
    frame, which is correct for a generated module and for hand-written use.
    """
    m = manim_mod()
    if module is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        module = caller.f_globals.get("__name__", "__main__") if caller else "__main__"
    return type(
        f"ArcScene_{_ident(scene_id)}",
        (ArcSceneMixin, m.Scene),
        {
            "__module__": module,
            "scene_id": scene_id,
            "archetype": archetype,
            "build_fn": staticmethod(build_fn),
            "params": params,
            "trace_path": trace_path,
        },
    )


def _ident(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text)
    return out if out and not out[0].isdigit() else f"s_{out}"

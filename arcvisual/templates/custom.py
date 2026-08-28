"""The escape hatch: a scene whose body is written by the model, not by us.

Every other template in this package is a fixed shape with model-supplied
parameters. That buys determinism and safety, and it costs the thing the product
is actually for. With three templates implemented, every paper in the world gets
drawn as a chain, a plot, or a box diagram — so a reader watching two different
papers sees the same three animations with different labels. That is not a
polish problem, it is the design refusing to say anything specific.

So this module inverts the split. The model writes the Manim body; we keep
everything that makes an animation *safe* rather than everything that makes it
look a certain way:

* ``ArcSceneMixin`` still owns the frame, the safe margin, the pacing floor and
  the bbox trace, because those are correctness, not style.
* Gate 1 still parses the body, runs ruff over it and walks it against the Manim
  API allowlist — with ``archetype=None`` it validates arbitrary source, which is
  exactly this case.
* Gate 2 still renders it in a subprocess, Gate 3 still measures containment,
  legibility, overlap and dead air on the real frames.
* The repair ladder still feeds each failure back as text the model can act on.

The generated code is a ``build(scene, params)`` function, the same contract the
hand-written templates satisfy — not a whole module. That is deliberate: imports
stay ours, the class assembly stays ours, and the model's surface is the drawing
itself. A free-form module would put ``import`` back in reach of generated code
and make the allowlist advisory.

``Params`` here carries only narration. Everything else a normal template would
receive as validated parameters is instead written directly into the body, which
is the whole point.
"""

from __future__ import annotations

from typing import Any

from pydantic import field_validator

from arcvisual.storyboard import Archetype
from arcvisual.templates.base import TemplateParams, clamp_each

#: Beat captions are read, not heard, so they stay short. Same limit the other
#: templates use, enforced the same way — coerced rather than rejected, because a
#: caption two characters over is not worth failing a scene that renders.
CAPTION_LIMIT = 90

TEMPLATE_ID = "custom_scene@1"
ARCHETYPE = Archetype.CUSTOM_SCENE


class Params(TemplateParams):
    """Narration only. The drawing lives in the generated ``build``.

    Inherits ``coerce_caption_list`` and ``extra="forbid"`` from the shared base, and
    adds the per-item length clamp the other templates do without — a custom scene's
    captions come straight from free-form generation, so they run long more often.
    """

    _clamp = field_validator("captions", mode="after")(clamp_each(CAPTION_LIMIT))


def build(scene: Any, params: Params) -> None:  # pragma: no cover - replaced
    """Placeholder so the module satisfies the template protocol.

    A real custom scene never calls this: ``render_custom_module`` writes its own
    ``build`` into the generated module. It exists so the registry can treat
    ``custom_scene`` like any other archetype, and so importing this module in a
    test does not require a model.
    """
    m = _manim()
    caption = params.captions[0] if params.captions else "No custom scene was generated."
    text = m.Text(caption, font_size=32)
    scene.fit_inside(text, width_frac=0.9)
    scene.play(m.FadeIn(text))
    scene.hold()


def _manim():
    from arcvisual.templates.base import manim_mod

    return manim_mod()


#: What the model is allowed to reach for, quoted into the prompt.
#:
#: Kept deliberately small. A larger surface does not produce better animations —
#: it produces more ways to fail Gate 1 and more attempts spent on repair. These
#: are the primitives the three hand-written templates actually use between them,
#: which is evidence they are sufficient rather than a guess.
MANIM_SURFACE = """\
Mobjects:   Text, MathTex, Tex, Rectangle, RoundedRectangle, Circle, Ellipse,
            Line, Arrow, DoubleArrow, Dot, Polygon, Square, Triangle, Axes,
            NumberPlane, NumberLine, VGroup, Group, SurroundingRectangle, Brace,
            BraceLabel, DashedLine, Arc, CurvedArrow, Table, Matrix
Animations: Create, Write, FadeIn, FadeOut, Transform, ReplacementTransform,
            TransformMatchingTex, GrowArrow, GrowFromCenter, DrawBorderThenFill,
            Indicate, Circumscribe, Flash, MoveAlongPath, Rotate, ApplyWave,
            LaggedStart, AnimationGroup, Succession
Layout:     .next_to, .shift, .move_to, .to_edge, .arrange, .arrange_in_grid,
            .scale, .set_color, .set_opacity, .set_fill, .set_stroke, .rotate,
            .get_center, .get_top, .get_bottom, .get_left, .get_right, .copy
Constants:  m.UP, m.DOWN, m.LEFT, m.RIGHT, m.ORIGIN, m.PI, m.TAU, m.DEGREES, and
            the colours m.BLUE, m.RED, m.GREEN, m.YELLOW, m.ORANGE, m.PURPLE,
            m.TEAL, m.WHITE, m.GREY

EVERY name above needs the `m.` prefix, constants included. Bare `ORIGIN` or
`UP` is an undefined name: Gate 1 rejects it and the scene is lost. This is the
single most common way a generated body fails, which is why the constants are
spelled out with their prefix here rather than left to inference.

Maths:      `math` is imported. Use `math.exp`, `math.sqrt`, `math.log`,
            `math.sin`, `math.cos`, `math.pi`. Manim is a drawing library and has
            NO maths functions -- `m.exp` does not exist and raises at render
            time, after the animations before it have already been drawn.
"""

#: The helpers the scene object exposes. These are not optional niceties — they
#: are how a generated scene stays inside the frame and above the legibility
#: floor, which is what Gate 3 measures.
SCENE_HELPERS = """\
scene.safe_frame()            -> (half_width, half_height) inside the safe margin
scene.fit_inside(mob, width_frac=1.0, height_frac=1.0)  shrink to fit; returns mob
scene.fill_frame(mob, width_frac=0.98, height_frac=0.9)  grow to fill; returns mob
scene.clamp_into_frame(mob)   move a stray mobject back inside
scene.fit_text_legibly(mob, max_width)  shrink text to a width, never below 18px
scene.caption(text)           -> a styled, bottom-anchored narration line
scene.show_caption(cur, text) -> swap the caption line; returns the new one
scene.play(*anims, **kw)      instrumented; ALWAYS use this, never self.play
scene.hold(seconds)           a still beat, so a reveal can be read
scene.declare_overlap(a, b)   declare an INTENDED overlap so Gate 3 allows it
"""


def latex_available() -> bool:
    """Whether this machine can typeset ``MathTex``.

    Manim shells out to ``latex``/``dvisvgm`` for every ``MathTex`` and ``Tex``, and
    without them the scene raises before its first animation — Gate 2 reports it as
    an environment problem precisely because no amount of repair can fix it.

    Worth checking *before* generation rather than after: a custom scene's whole
    value is showing the paper's own notation, so on a machine without LaTeX the
    model should be told to render maths as Unicode text instead of writing
    ``MathTex`` that is certain to fail. Asking a model to avoid something is far
    cheaper than spending three repair attempts discovering it cannot work.
    """
    import shutil

    return shutil.which("latex") is not None and shutil.which("dvisvgm") is not None


#: Appended to the prompt when the render environment has no LaTeX.
NO_LATEX_NOTE = """IMPORTANT — this render environment has NO LaTeX toolchain. `m.MathTex` and
`m.Tex` WILL FAIL and cost the scene. Write mathematics as `m.Text` using Unicode
instead: superscripts and subscripts (Q Kᵀ, d_k as dₖ), Greek letters (α, σ, Σ),
operators (×, ·, →, ≈, ≤). A formula set in clean Unicode text renders; a perfect
LaTeX formula does not render at all.
"""  # noqa: RUF001


def estimate_duration(params: Params) -> float:
    """Runtime, for the registry's protocol.

    A custom scene's duration comes from the generated body, not from its params —
    ``custom_codegen.estimate_duration`` reads it off the ``scene.play`` and
    ``scene.hold`` calls in the source. This exists so the registry can treat
    ``custom_scene`` like any other archetype, and answers with a caption-count
    approximation for the rare caller that has params but no body.
    """
    return max(4.0, len(params.captions) * 2.5)

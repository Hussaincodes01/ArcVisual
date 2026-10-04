"""``transform_chain`` — an equation derived one step at a time.

Fires when the paper walks a derivation. The model supplies the LaTeX for each
step and a caption per step; everything about how the transformation *looks* —
easing, hold, colour, position, scaling — belongs to this file, not to the model.

The Manim primitive is ``TransformMatchingTex``, which is the mechanical form of
the style contract's "transform over cut": matching sub-expressions glide to
their new positions so the reader can follow *what changed*, and unmatched parts
fade. A hard swap between two equations teaches nothing.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from arcvisual.storyboard import Archetype
from arcvisual.templates.base import (
    ACCENT,
    FG,
    MIN_BEAT_S,
    REVEAL_HOLD_S,
    TemplateParams,
    clamp_text,
    manim_mod,
)

#: Longest chain the template stages well. Beyond this the transforms blur together
#: and each step gets too little screen time to read.
MAX_STEPS = 6


def _cap_steps(value: object) -> object:
    """Trim an over-long chain, keeping the opening AND the result.

    Dropping the tail would discard the conclusion — the one step the reader most
    needs — so the middle is what gives way. A model that proposes eight steps has
    understood the derivation; it has only misjudged how many fit on screen.
    """
    if not isinstance(value, list) or len(value) <= MAX_STEPS:
        return value
    return [*value[: MAX_STEPS - 1], value[-1]]


_DELIMITERS = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))
_MATH_SIGNS = set("=^_\\+<>|/*{}()[]∑∫√≤≥≈∝·×")  # noqa: RUF001


def _strip_delimiters(value: object) -> object:
    """Drop math delimiters a model wraps around a step.

    `steps` are typeset in math mode already. A step that arrives as ``$x^2$`` puts
    a literal ``$`` inside MathTex — a LaTeX error at render time — and in the
    browser shows the dollar signs. Removing them is a coercion, not a judgement:
    the notation inside is untouched.
    """
    if not isinstance(value, list):
        return value
    out = []
    for step in value:
        if isinstance(step, str):
            s = step.strip()
            for left, right in _DELIMITERS:
                if (
                    s.startswith(left)
                    and s.endswith(right)
                    and len(s) > len(left) + len(right)
                ):
                    # Only a step that HAD delimiters is touched; anything else
                    # stays byte-for-byte, because notation is copied verbatim.
                    step = s[len(left) : -len(right)].strip()
                    break
        out.append(step)
    return out


def _looks_like_prose(step: str) -> bool:
    """A sentence where an equation belongs.

    Observed on a real run: every step of a "derivation" came back as an English
    sentence. Typeset in math mode that renders as one long run of italic letters —
    the scene passes every mechanical check and teaches nothing. No math sign at all
    plus several real words is the signature.
    """
    if any(ch in _MATH_SIGNS for ch in step):
        return False
    words = [w for w in step.split() if len(w) >= 3 and w.isalpha()]
    return len(words) >= 4


TEMPLATE_ID = "transform_chain"
ARCHETYPE = Archetype.TRANSFORM_CHAIN


class Params(TemplateParams):
    steps: list[str] = Field(
        min_length=2,
        # No max_length here: an over-long chain is trimmed by `_trim_steps` below
        # rather than rejected. Pydantic's max_length would fail the whole scene, and
        # a seven-step derivation is still a derivation.
        description=(
            "LaTeX for each state of the derivation, in order. Copy the paper's "
            "own notation verbatim; do not re-derive or re-typeset. Two to six "
            "steps — a longer chain should be split into two scenes."
        ),
    )
    highlight: list[str] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Optional. A LaTeX substring to accent for each step, aligned by index "
            "with `steps`: highlight[0] accents step 1, highlight[1] accents step 2, "
            "and so on. Use an empty string to skip a step. May be shorter than "
            "`steps`; trailing steps simply get no accent."
        ),
    )
    _trim_steps = field_validator("steps", mode="before")(
        lambda v: _cap_steps(_strip_delimiters(v))
    )

    @field_validator("steps")
    @classmethod
    def _steps_are_equations(cls, steps: list[str]) -> list[str]:
        # STRUCTURAL, so it rejects: a sentence cannot be coerced into the equation
        # it describes. The message is written for the repair prompt.
        for i, step in enumerate(steps):
            if _looks_like_prose(step):
                raise ValueError(
                    f"step {i + 1} is an English sentence, not an equation: "
                    f"{step[:60]!r}. Every step must be LaTeX math copied from the "
                    "paper, e.g. \\mathrm{softmax}(QK^T / \\sqrt{d_k})V. Put the "
                    "explanation in the captions instead."
                )
        return steps

    title: str | None = Field(
        default=None,
        max_length=80,
        description="Short label for what is being derived, e.g. 'Softmax attention'.",
    )

    # `title` is a caption; `steps` is the paper's own notation and is never trimmed,
    # because a truncated equation is a WRONG equation, not a shorter one.
    _clamp_title = field_validator("title", mode="before")(clamp_text(80))

    @model_validator(mode="after")
    def _shapes(self) -> Params:
        # COERCE, do not reject. Both of these fired on a real run and cost two
        # scenes outright: 6 captions for a 3-step chain, and a 7-step chain against
        # a 6-step cap. Neither makes the scene wrong — surplus captions are simply
        # never shown, and a chain one step too long still carries its argument. This
        # is the same lesson as `highlight`: a limit that exists for presentation
        # must trim, because rejecting throws away work the model got right.
        if len(self.captions) > len(self.steps):
            object.__setattr__(self, "captions", self.captions[: len(self.steps)])
        # These bounds are deliberately loose. An earlier version required
        # `len(highlight) <= len(steps) - 1` on the theory that only transitions can
        # be accented, and every model that met it supplied one highlight per STEP
        # instead — the obvious reading, and one with no rendering reason to forbid.
        # It cost two scenes their entire repair budget on a real paper before the
        # schema was recognised as the thing at fault. A constraint the caller keeps
        # violating identically is a constraint that is wrong, not a caller that is.
        if len(self.highlight) > len(self.steps):
            raise ValueError(
                f"highlight has {len(self.highlight)} entries but there are only "
                f"{len(self.steps)} steps; entries align with steps by index"
            )
        return self


def build(scene, params: Params) -> None:
    m = manim_mod()

    title = None
    if params.title:
        _, half_h = scene.safe_frame()
        title = m.Text(params.title, font_size=36, color=FG)
        scene.fit_inside(title, width_frac=0.8)
        title.move_to([0, half_h - title.height / 2, 0])
        scene.play(m.FadeIn(title, shift=m.DOWN * 0.2), run_time=MIN_BEAT_S)

    cap = None
    if params.captions:
        cap = scene.show_caption(None, params.captions[0])

    current = m.MathTex(params.steps[0], color=FG)
    scene.fit_inside(current, width_frac=0.92, height_frac=0.60)
    current.move_to(m.ORIGIN)
    # Write, not FadeIn: the reader watches the expression being built.
    scene.play(m.Write(current), run_time=max(1.2, MIN_BEAT_S))
    _accent(scene, m, current, _highlight_for(params, 0))
    scene.hold(REVEAL_HOLD_S)

    for i, latex in enumerate(params.steps[1:]):
        nxt = m.MathTex(latex, color=FG)
        scene.fit_inside(nxt, width_frac=0.92, height_frac=0.60)
        nxt.move_to(m.ORIGIN)

        if params.captions and i + 1 < len(params.captions):
            cap = scene.show_caption(cap, params.captions[i + 1])

        scene.play(
            m.TransformMatchingTex(
                current,
                nxt,
                transform_mismatches=True,
                key_map={},
            ),
            run_time=1.4,
        )
        current = nxt

        _accent(scene, m, current, _highlight_for(params, i + 1))
        scene.hold(REVEAL_HOLD_S)

    if title is not None:
        scene.declare_overlap(title, current)


def estimate_duration(params: Params) -> float:
    """The runtime this template will actually play, in seconds.

    Mirrors :func:`build` beat for beat. It exists because the *template* owns
    timing, not the model: asking a model to predict a template's internal
    timeline means asking it to guess at code it cannot see, and Gate 2's
    duration assertion would then fire on every scene instead of on real drift.

    Kept honest by ``test_estimate_matches_real_render``, which renders and
    compares — so a change to ``build`` that forgets this function fails CI.
    """
    total = 0.0
    if params.title:
        total += MIN_BEAT_S  # FadeIn(title)
    if params.captions:
        total += MIN_BEAT_S  # first caption
    total += max(1.2, MIN_BEAT_S)  # Write(first step)
    if _highlight_for(params, 0):
        total += 1.0  # Indicate on the opening step
    total += REVEAL_HOLD_S
    for i in range(len(params.steps) - 1):
        if params.captions and i + 1 < len(params.captions):
            total += MIN_BEAT_S  # caption swap
        total += 1.4  # TransformMatchingTex
        if _highlight_for(params, i + 1):
            total += 1.0  # Indicate
        total += REVEAL_HOLD_S
    return round(total, 3)


def _highlight_for(params: Params, step_index: int) -> str:
    """The accent for one step. Index-aligned with `steps`; missing entries mean
    no accent, which is why this is a lookup rather than an assertion."""
    if step_index < len(params.highlight):
        return (params.highlight[step_index] or "").strip()
    return ""


def _accent(scene, m, expression, accent: str) -> None:
    """Indicate a sub-expression, or the whole thing if the substring did not match.

    Falling back rather than skipping keeps the runtime independent of whether a
    substring happened to match, so `estimate_duration` stays exact and Gate 2's
    duration assertion keeps meaning something.
    """
    if not accent:
        return
    try:
        part = expression.get_part_by_tex(accent)
    except Exception:
        part = None
    scene.play(
        m.Indicate(
            part if part is not None else expression, color=ACCENT, scale_factor=1.15
        ),
        run_time=1.0,
    )


def simplify(params: Params) -> Params | None:
    """Repair rung 2: keep only the endpoints of the derivation.

    A chain that fails a gate usually fails because an intermediate step is too
    wide or the matching is ambiguous. First and last state, one transform, is
    the smallest thing that still makes the same claim.
    """
    if len(params.steps) <= 2:
        return None
    caps = params.captions
    return Params(
        steps=[params.steps[0], params.steps[-1]],
        highlight=params.highlight[-1:] if params.highlight else [],
        title=params.title,
        captions=[caps[0], caps[-1]] if len(caps) >= 2 else [],
    )

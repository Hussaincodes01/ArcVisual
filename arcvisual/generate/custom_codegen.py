"""Model-authored Manim, for concepts no fixed template can express.

The parameter-filling path asks "which of our three shapes is this concept least
badly served by?". For a lot of real papers the honest answer is "none of them",
and the result is a chain diagram of a thing that is not a chain. This module
asks the other question: *what would you draw for this?*

**What the model writes and what it does not.** It writes the body of one
function, ``build(scene, params)`` — the same contract the hand-written templates
satisfy. It does not write imports, the module docstring, the ``Params`` object
or the class assembly; those are emitted around it. That boundary is what keeps
Gate 1's Manim allowlist meaningful, because generated code has no way to import
its way past it.

**Why this is safe enough to ship.** Free-form codegen is only reckless without
somewhere for bad output to fail cheaply, and this pipeline already has four such
places: Gate 1 parses and lints the body and walks it against the API allowlist,
Gate 2 renders it in a subprocess under a timeout, Gate 3 measures containment,
legibility, overlap and dead air on the actual frames, and the repair ladder
feeds each failure back as text under a per-scene cost ceiling. A generated scene
that misbehaves does not reach a reader; it degrades to the paper's own figure
and then to prose.

**Grounding is unchanged.** The claim, the verbatim quote and the equations all
come from the storyboard, which got them from spans that resolve against the
paper's source. The model chooses how to draw, never what is true.
"""

from __future__ import annotations

import ast
import logging
import re
import textwrap
from typing import Any

from pydantic import BaseModel, Field, field_validator

from arcvisual.providers.base import TextBlock
from arcvisual.storyboard import Beat, SceneSpec
from arcvisual.templates.base import (
    MIN_BEAT_S,
    REVEAL_HOLD_S,
    clamp_each,
    coerce_caption_list,
)
from arcvisual.templates.custom import (
    CAPTION_LIMIT,
    MANIM_SURFACE,
    NO_LATEX_NOTE,
    SCENE_HELPERS,
    latex_available,
)
from arcvisual.templates.custom import Params as CustomParams

log = logging.getLogger(__name__)


class CustomSceneOut(BaseModel):
    """What the model returns for a custom scene."""

    plan: str = Field(
        default="",
        max_length=600,
        description=(
            "One or two sentences: what the viewer sees and how it moves. Written "
            "first, so the code has something to be faithful to."
        ),
    )
    body: str = Field(
        min_length=40,
        max_length=6000,
        description=(
            "The body of build(scene, params). Python statements at ONE level of "
            "indentation. No def line, no imports, no markdown fence."
        ),
    )
    captions: list[str] = Field(default_factory=list, max_length=8)

    _coerce = field_validator("captions", mode="before")(coerce_caption_list)
    _clamp = field_validator("captions", mode="after")(clamp_each(CAPTION_LIMIT))

    @field_validator("body", mode="before")
    @classmethod
    def _strip_fences(cls, value: Any) -> Any:
        """Remove a markdown fence and any ``def build`` wrapper the model added.

        Both are things models produce constantly despite instructions, and both
        are mechanical to undo. Rejecting them would spend a repair attempt on a
        formatting habit rather than on the animation — the project's rule is that
        structural constraints reject and cosmetic ones coerce, and a fence is
        about as cosmetic as it gets.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        fence = re.match(r"^```(?:python)?\s*\n(.*?)\n?```$", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        # A `def build(...)` wrapper: drop the line and keep what follows.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\s*def\s+build\s*\(", line):
                text = "\n".join(lines[i + 1 :])
                break
        return _normalise_indent(_drop_redundant_preamble(text)).strip("\n")


#: Modules the emitted module already imports. A body that imports one of these
#: is not doing anything wrong — it is restating something already true.
_PROVIDED_MODULES = frozenset({"math", "manim"})

#: The binding the emitted ``build`` already opens with.
_REBINDS_MANIM = re.compile(r"^m\s*=\s*manim_mod\s*\(\s*\)\s*$")


def _drop_redundant_preamble(text: str) -> str:
    """Remove lines that restate what the generated module already set up.

    Models reliably write the preamble they were told not to. Observed on real
    replies, twice in a row: ``import math``, which shadowed the module-level import
    and failed Gate 1 with F811 for the redefinition plus F401 for the now-unused
    original; then ``m = manim_mod()``, which shadowed the binding ``build`` opens
    with and failed F811 again. Neither says anything about the animation, and both
    would otherwise burn a repair attempt on a habit.

    **Only what we actually provide is stripped.** ``import os`` stays exactly where
    it is, so the allowlist still refuses it and the security finding still fires —
    the point is to forgive a redundant restatement, never to quietly launder a
    forbidden import into a passing scene. A test puts ``import os`` through this
    path and asserts Gate 1 still rejects it.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        imported = re.match(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", stripped)
        if imported and imported.group(1).split(".")[0] in _PROVIDED_MODULES:
            continue
        if _REBINDS_MANIM.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def _normalise_indent(text: str) -> str:
    """Strip a uniform leading indent, tolerating comments at column 0.

    ``textwrap.dedent`` computes the longest common prefix over every non-blank
    line, and a comment is a non-blank line. A real model returned this, having
    written the body as if it were already inside ``def build``:

        # Setup
            hw, hh = scene.safe_frame()

    The common prefix is "" because of the comment, so dedent removed nothing, the
    emitter added four more spaces to every line, and the module failed to parse
    with "unexpected indent at line 20" — a whole scene lost to whitespace.

    So the indent is measured over CODE lines only and then applied to every line,
    including the comments. The result is checked by actually parsing it: if the
    normalised text does not compile but the original does, the original wins,
    because a clever transformation that breaks working code is worse than none.
    """
    lines = text.splitlines()
    code_indents = [
        len(line) - len(line.lstrip())
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not code_indents:
        return textwrap.dedent(text)
    shift = min(code_indents)
    if shift:
        lines = [line[min(shift, len(line) - len(line.lstrip())) :] for line in lines]
    candidate = "\n".join(lines)

    if _parses(candidate):
        return candidate
    for fallback in (textwrap.dedent(text), text):
        if _parses(fallback):
            return fallback
    return candidate  # nothing parses; let Gate 1 report it properly


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


SYSTEM = """\
You write one Manim animation that explains one specific idea from one specific \
research paper.

You are not filling in a template. There is no fixed shape to conform to: choose \
whatever visual actually explains THIS idea. A matrix product wants matrices \
moving; a scaling law wants axes and a curve; a routing mechanism wants tokens \
travelling to experts; an attention pattern wants a grid lighting up. If the \
honest answer is a simple diagram, draw a simple diagram well.

WHAT YOU WRITE
The body of `build(scene, params)` — Python statements at one level of \
indentation. Nothing else. No `def` line, no imports, no markdown fence, no \
explanation outside the JSON.

`m` is already bound to the manim module and `math` to the standard library's \
maths module. Reach for drawing through `m.` (`m.Text`, `m.Create`, `m.UP`) and \
for arithmetic through `math.` (`math.exp`, `math.sqrt`). Nothing else is \
available and there is no way to import anything.

AVAILABLE MANIM SURFACE
{surface}

THE SCENE OBJECT
{helpers}

HARD RULES, each of which fails the scene if broken
- Use `scene.play(...)`, never `self.play(...)` and never bare `play(...)`.
- No imports, no file or network access, no `exec`, `eval`, `open`, `__import__`.
- No `while` loops. Bounded `for` loops over literal collections are fine.
- Every mobject you show must be inside the safe frame. Call \
`scene.fit_inside(...)` on anything you built from arithmetic, or \
`scene.fill_frame(...)` on your main group to use the space properly.
- Text below 18px is unreadable and fails the legibility check. Use \
`scene.fit_text_legibly(mob, width)` rather than scaling text down by hand.
- Between animations the frame must not sit still for more than 2 seconds. Use \
`scene.hold(...)` deliberately after a reveal, not as filler.
- If two mobjects are meant to overlap, say so with \
`scene.declare_overlap(a, b)`, or the overlap is reported as a defect.

MAKING IT SPECIFIC
- Copy notation VERBATIM from the paper's equations into `m.MathTex`. Do not \
restate, simplify or re-derive; a derivation the paper does not perform must not \
appear on screen.
- Use the paper's own names for things. "Query", "Key", "Value" — not "Input A".
- Show the mechanism, not a picture of the words. Three labelled boxes with \
arrows is what to produce when you have understood nothing; prefer showing the \
actual transformation the idea describes.
- Aim for {beats} distinct moments, each earning its time on screen.

Return JSON matching the schema: a short `plan`, then `body`, then `captions` \
(one short line per moment, under {caption_limit} characters, plain language, no \
LaTeX).
"""

USER = """\
PAPER: {title}

THE IDEA TO ANIMATE
{claim}

THE CONCEPT IT EXPLAINS
{concept}

VERBATIM FROM THE PAPER (this is the ground truth; quote its notation exactly)
\"\"\"
{quote}
\"\"\"
{equations}
TARGET RUNTIME: about {duration:.0f} seconds.
"""


def build_prompt(
    spec: SceneSpec,
    *,
    title: str,
    concept: str,
    quote: str,
    equations: list[str] | None = None,
    beats: int = 4,
    latex: bool | None = None,
) -> tuple[str, str]:
    """Return ``(system, user)`` for one custom scene.

    ``latex`` overrides toolchain detection, for tests and for a renderer whose
    environment differs from this process's (a container that ships TinyTeX while
    the host has none).
    """
    has_latex = latex_available() if latex is None else latex
    eq = ""
    if equations:
        # Verbatim LaTeX, one per line. This is the single most valuable thing in
        # the prompt: it is what lets the animation show the paper's own notation
        # instead of a paraphrase of it.
        body = "\n".join(f"  {e}" for e in equations[:6])
        target = (
            "copy exactly into m.MathTex"
            if has_latex
            else "transcribe faithfully into Unicode m.Text -- LaTeX is unavailable here"
        )
        eq = f"\nEQUATIONS FROM THIS SECTION ({target})\n{body}\n"
    return (
        SYSTEM.format(
            surface=MANIM_SURFACE,
            helpers=SCENE_HELPERS,
            beats=beats,
            caption_limit=CAPTION_LIMIT,
        )
        + ("" if has_latex else "\n" + NO_LATEX_NOTE),
        USER.format(
            title=title,
            claim=spec.claim,
            concept=concept or "(not named)",
            quote=quote.strip()[:1200],
            equations=eq,
            duration=spec.duration_s,
        ),
    )


_MODULE_TEMPLATE = '''\
"""Generated by ArcVisual. Do not edit -- regenerate from the storyboard.

archetype : custom_scene
claim     : {claim}
grounded  : {section_id}[{span_start}:{span_end}]
plan      : {plan}
"""

import math

from arcvisual.templates.base import make_scene, manim_mod
from arcvisual.templates.custom import Params

SCENE_ID = {scene_id!r}
ARCHETYPE = "custom_scene"
PARAMS = Params(captions={captions!r})


def build(scene, params):
    m = manim_mod()
{body}


# module=__name__ is required: Manim only discovers Scene subclasses whose
# __module__ matches the module it loaded.
{class_name} = make_scene(
    SCENE_ID, ARCHETYPE, build, PARAMS, trace_path={trace_path!r}, module=__name__
)
'''


def render_custom_module(
    spec: SceneSpec,
    out: CustomSceneOut,
    *,
    trace_path: str = "trace.json",
) -> str:
    """Wrap a model-written body into a loadable module.

    The body is indented into ``build``; everything around it is ours. If the body
    is empty after coercion the module still parses and Gate 1 reports it, which is
    a better failure than a ``SyntaxError`` the repair loop cannot read.
    """
    from arcvisual.generate.codegen import scene_class_name

    body = out.body.strip("\n") or "pass"
    indented = textwrap.indent(textwrap.dedent(body), "    ")
    return _MODULE_TEMPLATE.format(
        claim=spec.claim.replace("\n", " ")[:200],
        section_id=spec.span.section_id,
        span_start=spec.span.start,
        span_end=spec.span.end,
        plan=out.plan.replace("\n", " ")[:200] or "(none given)",
        scene_id=spec.id,
        captions=list(out.captions),
        body=indented,
        class_name=scene_class_name(spec.id),
        trace_path=trace_path,
    )


#: Manim's own default when ``run_time`` is not given.
_DEFAULT_RUN_TIME_S = 1.0


def estimate_duration(body: str) -> float:
    """How long a model-written body will actually play, from its own calls.

    A hand-written template answers this with ``estimate_duration(params)`` because
    its shape is fixed. Model-written code has no such function — but its runtime is
    still fully determined by the ``scene.play`` and ``scene.hold`` calls in the
    source, so it can be read off statically.

    This matters more than it looks. Gate 2 fails a scene whose measured runtime
    drifts more than 20% from its declared beats, and that check is worth having:
    it catches a scene that renders four seconds of content while claiming twenty.
    Without an estimate, every custom scene inherits the opportunity's *target*
    duration and trips the check on arrival — one measured at 10.2s against a
    declared 14.0s and failed for it, having rendered perfectly. Beats laid over a
    real estimate keep the assertion meaningful instead of making it noise.

    Deliberately static rather than a trial render: it costs nothing, and it runs
    before Gate 2 rather than after, which is where the beats are needed.
    """
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        # Gate 1 reports this properly; here just decline to guess.
        return 0.0

    total = 0.0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func
        if not (isinstance(target.value, ast.Name) and target.value.id == "scene"):
            continue

        if target.attr == "play":
            run_time = _kwarg_number(node, "run_time")
            # The mixin raises anything below the pacing floor, so mirror that here
            # or the estimate reads low for a body full of very short animations.
            total += max(
                run_time if run_time is not None else _DEFAULT_RUN_TIME_S, MIN_BEAT_S
            )
        elif target.attr == "hold":
            seconds = _positional_number(node, 0)
            if seconds is None:
                seconds = _kwarg_number(node, "seconds")
            total += seconds if seconds is not None else REVEAL_HOLD_S
        elif target.attr == "show_caption":
            total += MIN_BEAT_S  # it plays exactly one transform at the floor
    return round(total, 3)


def _kwarg_number(call: ast.Call, name: str) -> float | None:
    for kw in call.keywords:
        if (
            kw.arg == name
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, (int, float))
        ):
            return float(kw.value.value)
    return None


def _positional_number(call: ast.Call, index: int) -> float | None:
    if len(call.args) > index and isinstance(call.args[index], ast.Constant):
        value = call.args[index].value
        if isinstance(value, (int, float)):
            return float(value)
    return None


def beats_for(out: CustomSceneOut, total_s: float | None = None) -> list[Beat]:
    """Lay the captions out over the runtime, reusing the shared pacing rule.

    ``total_s`` defaults to the duration read off the body itself, which is what
    keeps Gate 2's drift assertion honest for a scene nobody wrote a template for.
    """
    from arcvisual.generate.codegen import fit_beats

    captions = list(out.captions) or ["(no narration)"]
    if total_s is None or total_s <= 0:
        total_s = estimate_duration(out.body) or float(len(captions)) * 2.0
    return fit_beats(captions, total_s)


def generate_custom(
    sb: Any,
    opportunity: Any,
    *,
    provider: Any,
    findings: list[str] | None = None,
    attempt: int = 1,
) -> Any:
    """Generate one model-authored scene. Mirrors ``codegen.generate_scene``.

    Returns a ``GenerateResult`` so the repair ladder, the gates and the cache all
    treat a custom scene exactly like a templated one — the only thing that differs
    is where the Manim comes from.
    """
    from arcvisual.generate.codegen import GenerateResult, _spec
    from arcvisual.providers.base import Task
    from arcvisual.storyboard import GateReport, Scene, SceneState

    concept = sb.concept(opportunity.concept_id)
    section = sb.section(opportunity.span.section_id)
    quote = opportunity.span.resolve(section)
    equations = [e.latex for e in sb.equations if e.span.section_id == section.id]

    spec_stub = _spec(opportunity, {}, [], False)
    system, user = build_prompt(
        spec_stub,
        title=sb.paper.title,
        concept=concept.name if concept else "",
        quote=quote,
        equations=equations,
    )
    if findings:
        # Repair. The gate text goes back verbatim: it names the frame, the mobject
        # and the measurement that failed, which is more actionable than any summary
        # we could write over the top of it.
        user += (
            "\n\nYOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Rewrite the body to fix "
            "them, changing as little else as possible:\n"
            + "\n".join(f"- {f}" for f in findings[:8])
        )

    result = provider.structured(
        system=[TextBlock(text=system)],
        user=user,
        output_model=CustomSceneOut,
        task=Task.CODEGEN,
    )
    out = result.value
    if out is None:
        scene = Scene(
            spec=spec_stub,
            state=SceneState.GENERATING,
            attempts=attempt,
            gate_report=GateReport(),
        )
        return GenerateResult(
            scene=scene,
            source="",
            params_obj=None,
            cost_usd=result.usage.cost_usd,
            cost_attributed=result.usage.attributed,
            provider=provider.name,
            notes=["the model returned no usable custom scene", *result.findings],
        )

    beats = beats_for(out)
    duration = beats[-1].t + beats[-1].dur if beats else estimate_duration(out.body)
    params = CustomParams(captions=list(out.captions))
    spec = _spec(opportunity, params.model_dump(mode="json"), beats, False)
    # The body is what makes this scene unlike every other one, so it belongs in the
    # content hash: two scenes with identical captions but different drawings must
    # not share a cached video.
    spec = spec.model_copy(
        update={
            "params": {**spec.params, "body_sha": _body_sha(out.body)},
            "duration_s": duration,
        }
    )
    source = render_custom_module(spec, out)
    scene = Scene(spec=spec, state=SceneState.VALIDATING, attempts=attempt)
    return GenerateResult(
        scene=scene,
        source=source,
        params_obj=params,
        cost_usd=result.usage.cost_usd,
        cost_attributed=result.usage.attributed,
        provider=provider.name,
        notes=[out.plan] if out.plan else [],
    )


def _body_sha(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

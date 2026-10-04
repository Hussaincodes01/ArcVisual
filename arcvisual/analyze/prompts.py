"""Analyze-stage prompts and the schema the model fills.

Two properties are enforced here rather than hoped for:

* **Every claim quotes the paper.** The model returns a verbatim ``quote`` for
  each concept and each visual opportunity. :mod:`arcvisual.analyze.single_pass`
  grounds that quote with ``span_for_text`` and **drops anything it cannot
  locate**. A model cannot assert something the paper does not say and have it
  survive, because the assertion has nowhere to attach.
* **Only renderable archetypes are offered.** The archetype enum handed to the
  model comes from the template registry, not from the full taxonomy. Offering
  an archetype with no template invites a proposal we would have to throw away.

The prompt is deliberately arranged so the paper body sits in a cached system
block and everything volatile comes after it — see ``shared/prompt-caching.md``:
caching is a prefix match, so the stable half must come first.
"""

from __future__ import annotations

import functools
import logging
import textwrap
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from arcvisual.providers.base import TextBlock
from arcvisual.storyboard import Storyboard
from arcvisual.templates import registry

log = logging.getLogger(__name__)

SYSTEM_ROLE = """\
You are the analysis stage of ArcVisual, a pipeline that turns research papers \
into explained articles carried by 3Blue1Brown-style animations.

Your job is judgment, not summary. You decide which ideas in this paper are hard \
enough and central enough to deserve an animation, and which are better served by \
prose.

Three rules govern everything you output:

1. GROUND EVERYTHING. Every concept and every animation proposal must include a \
`quote` copied verbatim from the paper text you were given — exact characters, \
long enough to be unique (roughly 8 to 40 words). A proposal whose quote cannot \
be found in the source is discarded. Do not paraphrase inside `quote`. Do not \
quote the abstract when the body says it better.

2. AN ANIMATION MUST CARRY AN ARGUMENT. Every proposal states a `claim`: the \
specific thing the visual convinces the reader of. "Shows the architecture" is \
not a claim. "Data flows through the encoder once, in parallel, rather than \
step by step" is a claim. If you cannot state the claim, the idea does not need \
an animation.

3. RESTRAINT IS THE JOB. Most sections deserve zero animations. A paper deserves \
between 3 and 12 total. Rank by how hard an idea is to grasp from prose alone, \
multiplied by how central it is to the paper's contribution. An analysis that \
wants to animate everything has understood nothing.\
"""

TASK = """\
Analyze the paper above and return:

- `concepts`: the atomic ideas a reader must understand, in any order. Give each \
a short `name`, a one-sentence `statement` in your own words, the ids of other \
concepts it depends on (by `name`), a verbatim `quote`, and a `centrality` from \
0 to 1 measuring how load-bearing it is for the paper's contribution.

- `section_difficulty`: for each section id listed below, how hard that section \
is for a competent reader outside the subfield, 1 (easy) to 5 (dense).

- `opportunities`: the animations worth making. For each, pick the `archetype` \
whose primitive actually fits, state the `claim`, name the concept, give a \
verbatim `quote`, and `justification` explaining why prose alone is insufficient \
here. Set `difficulty` (1-5) and `centrality` (0-1) honestly — they determine \
which proposals survive triage.

Available archetypes and what each is for:
{archetype_help}

Section ids you may reference:
{section_list}\
"""

_ARCHETYPE_HELP = {
    "transform_chain": (
        "an equation derived step by step; the reader needs to see what changes "
        "between one line and the next"
    ),
    "plot_reveal": (
        "a results curve or comparison worth interrogating; the reader needs the "
        "trend built up rather than presented finished"
    ),
    "architecture_flow": (
        "a model or system diagram where the ORDER of operations and the path "
        "data takes is the thing the static figure cannot show"
    ),
    "custom_scene": (
        "ANY idea the three shapes above would misrepresent — a mechanism, a "
        "counterexample, a geometric intuition, an attention pattern, a scaling "
        "argument, an algorithm walking through its own data. A bespoke animation "
        "is written for it. PREFER THIS over forcing an idea into a chain, a plot "
        "or a box diagram it does not really fit: a diagram of the wrong shape "
        "teaches the wrong thing, and three shapes cannot cover a whole field"
    ),
}


def archetype_help() -> str:
    """Only the archetypes this build can actually render."""
    lines = []
    for arch in registry.available_archetypes():
        lines.append(f"- {arch.value}: {_ARCHETYPE_HELP.get(arch.value, 'see template')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# What the model returns
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Coercion vs. rejection
# --------------------------------------------------------------------------- #
#
# The rule this section encodes, learned the hard way: **structural constraints
# reject, cosmetic constraints coerce.**
#
# A real run produced 18 well-grounded concepts and 8 argued opportunities, and the
# whole analysis was discarded because two claims ran 12 and 16 characters over a
# 200-character cap and a reading note ran 16 over 400. Losing an entire paper's
# analysis to a presentation limit is never the right trade — the limit exists so a
# caption fits on screen, not because a longer sentence is wrong.
#
# Grounding correctness is enforced by span lookup, not by string length, so
# trimming these is safe. Anything that would change MEANING (a section reference, a
# quote's ability to ground) still fails loudly.


def _clamp(limit: int):
    """Truncate an over-long string at a word boundary instead of rejecting it.

    Falls back to a hard slice when word-wrapping cannot help: ``textwrap.shorten``
    on a string with no spaces returns nothing but the placeholder, which then trips
    the *minimum* length and fails the very validation this exists to avoid.
    """

    def _coerce(value: object) -> object:
        if not isinstance(value, str) or len(value) <= limit:
            return value
        wrapped = textwrap.shorten(value, width=limit, placeholder="…")
        if len(wrapped) >= min(limit // 2, 24):
            return wrapped
        return value[: limit - 1] + "…"

    return _coerce


def _available_archetypes() -> list[str]:
    return [a.value for a in registry.available_archetypes()]


def _archetype_schema(schema: dict) -> None:
    """Put the allowed archetypes in the JSON SCHEMA, not only in the prose.

    A bare ``type: string`` told the model nothing, and it duly invented
    `mechanism`, `counterfactual`, `decomposition` and four more — every one of which
    was then dropped as unavailable. The taxonomy is a closed set; the schema should
    say so.
    """
    schema["enum"] = _available_archetypes()


class ConceptOut(BaseModel):
    name: str = Field(max_length=60)
    statement: str = Field(max_length=300)
    #: Accepts a section id ("s003") or the heading path the model actually tends to
    #: use ("Model Architecture > Attention"). Resolved in single_pass._resolve_section.
    section_id: str
    quote: str = Field(min_length=12, max_length=600)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    centrality: float = Field(ge=0.0, le=1.0)

    # One clamp per field, each matching that field's own max_length. A single
    # shared limit silently fails to protect the tighter fields.
    _trim_name = field_validator("name", mode="before")(_clamp(60))
    _trim_statement = field_validator("statement", mode="before")(_clamp(300))
    _trim_quote = field_validator("quote", mode="before")(_clamp(600))


class SectionDifficultyOut(BaseModel):
    section_id: str
    difficulty: int = Field(ge=1, le=5)


class OpportunityOut(BaseModel):
    archetype: str = Field(json_schema_extra=_archetype_schema)
    claim: str = Field(min_length=10, max_length=280)
    concept_name: str = Field(max_length=60)
    section_id: str
    quote: str = Field(min_length=12, max_length=600)
    justification: str = Field(min_length=10, max_length=300)
    difficulty: int = Field(ge=1, le=5)
    centrality: float = Field(ge=0.0, le=1.0)

    _trim_claim = field_validator("claim", mode="before")(_clamp(280))
    _trim_concept = field_validator("concept_name", mode="before")(_clamp(60))
    _trim_quote = field_validator("quote", mode="before")(_clamp(600))
    _trim_just = field_validator("justification", mode="before")(_clamp(300))


class AnalysisOut(BaseModel):
    """The Analyze stage's structured output contract."""

    concepts: list[ConceptOut] = Field(max_length=40)
    section_difficulty: list[SectionDifficultyOut] = Field(default_factory=list)
    opportunities: list[OpportunityOut] = Field(default_factory=list, max_length=20)
    reading_note: str = Field(
        default="",
        max_length=800,
        description="Anything about this paper's structure a reader should know.",
    )

    _trim_note = field_validator("reading_note", mode="before")(_clamp(800))


#: Below this many characters, a section's prose is too fragmentary to quote from —
#: the model falls back on what it remembers of the paper and the quote fails to
#: ground. Measured rather than guessed: at ~200 characters per section, grounding
#: rejected every concept returned for the Transformer paper.
_MIN_QUOTABLE_CHARS = 800

#: Sections whose prose is never worth the budget. They rarely carry an animatable
#: claim, and references in particular are long enough to crowd out real content.
_LOW_VALUE_HEADINGS = (
    "reference",
    "bibliography",
    "acknowledg",
    "appendix",
    "supplementary",
)


def _section_value(section: object) -> tuple[int, int]:
    """Sort key: high-value sections first, then longer ones.

    Longer is a decent proxy for substance once the boilerplate is out of the way —
    a methods section that develops an idea has more prose than a two-line note.
    """
    heading = " > ".join(getattr(section, "heading_path", [])).lower()
    low = any(tag in heading for tag in _LOW_VALUE_HEADINGS)
    return (1 if low else 0, -len(getattr(section, "raw", "")))


def _whole_sections(sections: list, char_budget: int) -> tuple[str, int]:
    """Fill the budget with COMPLETE sections, listing the ones left out.

    Every section still appears by heading, so nothing disappears from the model's
    view of the paper silently — it simply has no text to quote for the omitted ones,
    which is honest. Quoting requires text that was actually sent.
    """
    keep: dict[str, int] = {}
    spent = 0
    for section in sorted(sections, key=_section_value):
        overhead = len(" > ".join(section.heading_path)) + len(section.id) + 10
        room = char_budget - spent - overhead
        if room < _MIN_QUOTABLE_CHARS:
            continue
        # Take the whole section when it fits, otherwise as much of it as the budget
        # allows — but never so little that it stops being quotable. The first section
        # is not exempt: letting it in unconditionally is how a 5,000-char budget sent
        # 11,560 characters, which is the overshoot this guard exists to prevent.
        take = min(len(section.raw), room)
        keep[section.id] = take
        spent += take + overhead

    if not keep and sections:
        # A budget too small for even one quotable section would otherwise return
        # headings alone. That is not a degraded prompt, it is a broken one: with no
        # quotable text the model can only answer from memory, every quote fails to
        # ground, and the job reports zero concepts with nothing explaining why. Send
        # one section anyway and say so — an over-long prompt fails loudly at the
        # provider, which is far easier to diagnose than silent emptiness.
        best = min(sections, key=_section_value)
        keep[best.id] = max(char_budget, _MIN_QUOTABLE_CHARS)
        log.warning(
            "analyze budget of %d chars fits no complete section (minimum %d); "
            "sending %s alone. Raise the provider's prompt_char_budget.",
            char_budget,
            _MIN_QUOTABLE_CHARS,
            best.id,
        )

    parts: list[str] = []
    omitted: list[str] = []
    dropped = 0
    for section in sections:  # document order, so the argument still reads in order
        heading = " > ".join(section.heading_path)
        take = keep.get(section.id)
        if take:
            body = section.raw[:take]
            if take < len(section.raw):
                dropped += len(section.raw) - take
                body += "\n[... section truncated ...]"
            # The section id travels WITH its text. The model is asked to attribute
            # each quote to a section id, and making it map a heading back through a
            # separate list is how quotes end up attributed to the wrong section.
            parts.append(f"## [{section.id}] {heading}\n{body}")
        else:
            omitted.append(f"[{section.id}] {heading}")
            dropped += len(section.raw)
    if omitted:
        parts.append(
            "## Sections omitted from this excerpt (do NOT quote these)\n"
            + "\n".join(f"- {h}" for h in omitted)
        )
    return "\n\n".join(parts), dropped


def trim_body(sb: Storyboard, char_budget: int) -> tuple[str, int]:
    """The paper body, trimmed to fit ``char_budget``. Returns ``(text, dropped)``.

    Every section keeps its heading and the HEAD of its prose. Keeping the head
    rather than sampling throughout is deliberate: a section states its claim first
    and elaborates afterwards, so the opening is where the analysable substance is,
    and a truncated tail costs less than a shuffled middle.

    Sections share the budget proportionally to their length, with a floor so a short
    section is never squeezed to nothing. Truncation is MARKED in the text — an
    unmarked cut invites the model to quote across the seam, and a quote that spans a
    gap would not ground.

    This exists because laguna counts reasoning against ``max_tokens`` and caps at
    32768: a ~20k-token prompt can consume the whole budget thinking and return no
    answer at all. Trimming buys headroom that raising the budget cannot.

    **When the budget is tight, whole sections beat fragments of all of them.** That
    is not a refinement, it is the difference between an analysis and nothing.
    Spreading a 5,000-character budget across 25 sections leaves ~200 characters each,
    and a model given 200 characters of a section it has memorised quotes the rest
    from memory: on the Transformer paper every returned quote was a real sentence
    from the paper and *none* of them appeared in the text actually sent, so grounding
    dropped all of them and the run shipped zero scenes. One quote even carried an
    invented "..." bridging two remembered passages. Sending fewer sections in full
    means the model has real text in front of it, and a quote that grounds.
    """
    sections = [s for s in sb.sections if s.raw.strip()]
    if not sections:
        return "", 0
    full = sum(len(s.raw) for s in sections)
    if full <= char_budget:
        return sb.body_text, 0

    if char_budget < len(sections) * _MIN_QUOTABLE_CHARS:
        return _whole_sections(sections, char_budget)

    # The floor keeps a short section from being squeezed to nothing, but it must not
    # override the budget: with 25 sections a flat 400-char floor guarantees 10,000
    # characters however small the budget is. A 5,000-char budget was sending 11,212
    # — which on a metered key is not a rounding error, it is the prompt crowding out
    # the reply until the analysis truncates mid-object and the call fails outright.
    per_section_cap = max(120, char_budget // max(1, len(sections)))
    floor = min(400, per_section_cap)
    share = char_budget / full
    parts: list[str] = []
    dropped = 0
    for section in sections:
        keep = max(floor, int(len(section.raw) * share))
        # Never let one long section eat a budget the others still need.
        keep = min(keep, per_section_cap)
        body = section.raw[:keep]
        if keep < len(section.raw):
            dropped += len(section.raw) - keep
            body += "\n[... section truncated ...]"
        parts.append(f"## [{section.id}] {' > '.join(section.heading_path)}\n{body}")
    return "\n\n".join(parts), dropped


def build_prompt(
    sb: Storyboard,
    char_budget: int | None = None,
    item_budget: int | None = None,
) -> tuple[list[TextBlock], str]:
    """Return ``(system_blocks, user_text)``, provider-neutral.

    The paper body is the expensive, stable half, so it goes last in the system
    blocks with ``cache=True``. Providers with a prompt cache read it back on every
    later call in the job (codegen, each repair); providers without one ignore the
    hint and re-send it. Either way the ORDER is what matters — caching is a prefix
    match, so volatile content must come after the stable half or nothing caches.

    ``char_budget`` is the provider's ceiling on the body, taken from its
    capabilities; None means "send it all". Honouring it is not optional on a metered
    key. Groq counts the prompt and the requested output against a single per-minute
    allowance and rejects the whole request with 413 once they exceed it, so an
    untrimmed 19,486-token prompt fails before generating anything. Poolside fails
    more quietly: it spends the budget reasoning and returns ``content: null``.
    """
    section_list = "\n".join(
        f"- {s.id}: {' > '.join(s.heading_path)}"
        for s in sb.sections
        if len(s.prose_md) >= 200
    )
    body_text = sb.body_text
    if char_budget is not None and char_budget > 0:
        # Title and abstract are small and always worth sending, so the budget applies
        # to what is left after them rather than to the whole prompt.
        header_cost = len(sb.paper.title) + len(sb.paper.abstract) + 8
        body_text, dropped = trim_body(sb, max(1000, char_budget - header_cost))
        if dropped:
            log.info(
                "analyze prompt trimmed to the provider's budget: "
                "%d chars dropped, %d sent (budget %d)",
                dropped,
                len(body_text),
                char_budget,
            )
    # The abstract is context, NOT a quotable source. It is not one of `sb.sections`,
    # so `span_for_text` has nothing to resolve a quote against and every quote taken
    # from it is dropped as ungrounded — observed costing four of six concepts on the
    # Transformer paper, each one attributed to s000 because that is the first section
    # id the model saw. Labelling it is cheaper than removing it: the abstract is the
    # best single statement of what the paper claims, and it steers the analysis even
    # when it cannot be cited.
    body = (
        f"# {sb.paper.title}\n\n"
        f"ABSTRACT (context only — you may NOT quote from this; it is not a section "
        f"and any quote taken from it will be discarded):\n{sb.paper.abstract}\n\n"
        f"SECTIONS (quote ONLY from these, and attribute each quote to the bracketed "
        f"section id that precedes its text):\n\n{body_text}"
    )
    system_blocks = [
        TextBlock(text=SYSTEM_ROLE),
        TextBlock(text=body, cache=True),
    ]
    user = TASK.format(archetype_help=archetype_help(), section_list=section_list)
    if item_budget is not None and item_budget > 0:
        # Stated as a hard cap rather than a preference. On a metered key the reply
        # shares one allowance with the prompt, and an over-long analysis does not
        # arrive truncated — constrained decoding stops mid-object and the request
        # fails with "missing properties", losing the entire call. Fewer, better
        # grounded concepts beat an analysis that never returns.
        user += (
            f"\n\nBUDGET: return AT MOST {item_budget} concepts and AT LEAST "
            f"{item_budget} opportunities — opportunities are the point of this "
            f"analysis, since each one becomes an animation and a concept without one "
            f"produces nothing. Some will be discarded for quoting text that is not "
            f"in the sections above, so propose more than you think are needed. "
            f"The concept ceiling is a hard limit set by "
            f"the token budget, not a stylistic preference: a longer reply is cut off "
            f"mid-object and the whole analysis is lost. Choose the most central ideas.\n"
            f"Keep every field short: `statement` under 300 characters, "
            f"`justification` under 300, `claim` under 280, `quote` under 600, and "
            f"`reading_note` to two sentences. These lengths are stated here rather "
            f"than enforced by the schema on purpose — a strict validator rejects the "
            f"WHOLE analysis over one long field, so overruns are truncated on arrival "
            f"instead. Long prose still spends the budget you need to finish the "
            f"object, so brevity is what gets the analysis returned at all."
        )
    return system_blocks, user


# --------------------------------------------------------------------------- #
# Codegen (Stage 3) prompt
# --------------------------------------------------------------------------- #

CODEGEN_SYSTEM = """\
You fill in parameters for a tested Manim animation template. You do not write \
Manim code — the template owns the camera, layout, palette, easing, safe margins \
and runtime bound. Your only job is to supply parameters that make the template \
say the intended thing about this paper.

Rules:
- Copy mathematical notation VERBATIM from the paper. Do not restate, simplify, \
or re-derive it. A derivation the paper does not perform must not appear.
- Captions are read, not heard. One short line per beat, under 90 characters, \
plain language, no LaTeX.
- Fewer elements is better. The template enforces margins you cannot override, \
so an overcrowded parameter set produces a scaled-down, illegible scene rather \
than a clever one.
- Use only the parameters in the schema. An invented parameter name fails \
validation and wastes an attempt.\
"""

CODEGEN_TASK = """\
Fill the parameters for a `{archetype}` scene.

The claim this animation must carry:
  {claim}

The concept it explains:
  {concept}

The grounded source text it must stay faithful to:
  \"\"\"{source}\"\"\"

Available equations from this section, verbatim:
{equations}

Return parameters matching the schema, plus `beats`: one beat per caption, each \
with a `dur` of at least 0.8 seconds. Total runtime should land between 12 and \
{max_runtime} seconds.\
"""

REPAIR_TASK = """\
The `{archetype}` scene you parameterised failed validation on attempt {attempt}.

What failed:
{findings}

Previous parameters:
{previous}

Fix the specific problem. Do not restyle or elaborate — change the least that \
resolves the failure. If the failure is about crowding or legibility, remove \
elements rather than shrinking them; the template already scales to fit, so \
"too small to read" means "too much on screen".\
"""


class BeatOut(BaseModel):
    caption: str = Field(max_length=90)
    dur: float = Field(ge=0.8, le=8.0)


class ParamsOut(BaseModel):
    """Wrapper so the model returns params and beats in one structured object."""

    params: dict = Field(description="Parameters matching the template's schema.")
    beats: list[BeatOut] = Field(min_length=1, max_length=12)
    scrubbable: Literal[True, False] = Field(
        default=False,
        description=(
            "True only if this scene rewards frame-by-frame scrubbing (a "
            "continuous transformation), not for staged reveals."
        ),
    )


@functools.lru_cache(maxsize=32)
def params_out_for(params_model: type[BaseModel]) -> type[ParamsOut]:
    """``ParamsOut`` whose *wire schema* names the template's real parameters.

    Two halves, deliberately different:

    * **What the model is constrained to** is the template's own schema, nested
      under ``params``. A free-form ``params: {}`` told constrained decoding nothing,
      and strict implementations now refuse it outright — Groq's qwen3.8 answers 400
      "additionalProperties:false must be set on every object", which degraded every
      scene of a real run.
    * **What the reply is parsed into** stays a plain dict. A parameter that fails
      the template's validators must still come back as a Gate 1 finding the repair
      ladder can act on; parsing straight into the template model would turn the same
      mistake into a whole failed call.
    """
    from pydantic import create_model

    typed = create_model(
        f"ParamsOutTyped_{params_model.__module__.rsplit('.', 1)[-1]}",
        __base__=ParamsOut,
        params=(params_model, Field(description="Parameters for this template.")),
    )
    wire = typed.model_json_schema()

    class _Wire(ParamsOut):
        @classmethod
        def model_json_schema(cls, *args, **kwargs):  # type: ignore[override]
            import copy

            return copy.deepcopy(wire)

    _Wire.__name__ = _Wire.__qualname__ = "ParamsOut"
    return _Wire

"""Stage 2 — Analyze, single pass.

Phase 1 deliberately runs **one** call over the whole paper rather than a
LangGraph fan-out of section agents. A graph buys parallelism and retry
checkpointing, and neither matters until the analysis is good enough to be worth
parallelising. The output contract is identical either way, so Phase 2 replaces
the body of :func:`analyze` without touching anything downstream.

The stage's real work is not the model call — it is what happens to the model's
answer afterwards:

1. **Grounding.** Each quote is located in the section it claims to come from. A
   quote that cannot be found means the model asserted something it did not read,
   and the proposal is dropped. This is the one check that protects against the
   product's worst failure: a beautiful animation of a wrong intuition.
2. **Triage.** Proposals are ranked by ``difficulty x centrality`` and cut to the
   cap. More animations is worse.
3. **Dependency repair.** Concept dependencies are resolved by name and any edge
   that would create a cycle is dropped, because a cycle would let us animate an
   idea before its prerequisite.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from arcvisual.analyze.prompts import AnalysisOut, build_prompt
from arcvisual.config import settings
from arcvisual.ingest.latex import span_for_text
from arcvisual.providers.base import Provider, Task
from arcvisual.providers.registry import get_provider
from arcvisual.storyboard import (
    Archetype,
    Concept,
    Section,
    Storyboard,
    VisualOpportunity,
    assert_acyclic,
    quote_hash,
    topological_concept_order,
)
from arcvisual.templates import registry

log = logging.getLogger(__name__)

#: Sections shorter than this are structural parents (a bare `\section{Method}`
#: followed immediately by a subsection) and carry no analysable prose.
MIN_ANALYSABLE_PROSE = 200


@dataclass
class AnalyzeReport:
    """What the stage discarded and why. Surfaced in the job trace, because a
    silent drop rate is indistinguishable from a working grounding check."""

    concepts_kept: int = 0
    concepts_dropped_ungrounded: int = 0
    opportunities_proposed: int = 0
    opportunities_dropped_ungrounded: int = 0
    opportunities_dropped_unavailable: int = 0
    opportunities_dropped_unknown_concept: int = 0
    opportunities_dropped_triage: int = 0
    kept_opportunities: int = 0
    dependency_edges_dropped: int = 0
    cost_usd: float = 0.0
    #: False when the provider's pricing is unknown. A 0.0 that means "unknown"
    #: must never be summed as if it meant "free".
    cost_attributed: bool = True
    provider: str = "heuristic"
    model: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"concepts {self.concepts_kept} kept / "
            f"{self.concepts_dropped_ungrounded} ungrounded; "
            f"opportunities {self.opportunities_proposed} proposed -> "
            f"{self.kept_opportunities} kept "
            f"(ungrounded {self.opportunities_dropped_ungrounded}, "
            f"unavailable {self.opportunities_dropped_unavailable}, "
            f"triaged {self.opportunities_dropped_triage})"
        )


def analyze(
    sb: Storyboard,
    *,
    provider: Provider | None = None,
    client: object | None = None,
    deadline: float | None = None,
) -> tuple[Storyboard, AnalyzeReport]:
    """Annotate a Storyboard with concepts, difficulty and visual opportunities.

    ``provider`` overrides selection; ``client`` is a legacy shortcut that wraps a
    raw Anthropic client, kept so existing call sites and tests keep working.
    """
    if provider is None and client is not None:
        from arcvisual.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(client=client)
    if provider is None:
        provider = get_provider()

    if provider is None:
        log.info("no model provider configured: using the heuristic analyzer")
        raw = heuristic_analysis(sb)
        report = AnalyzeReport(
            notes=["heuristic analyzer — no model provider configured"],
            provider="heuristic",
        )
    else:
        caps = provider.capabilities()
        groups = plan_passes(sb, caps.prompt_char_budget, caps.analysis_passes)
        if len(groups) > 1:
            raw, report = _call_in_passes(sb, provider, groups, deadline=deadline)
        else:
            raw, report = _call_provider(sb, provider)

    return _apply(sb, raw, report)


# --------------------------------------------------------------------------- #
# Several passes over a long paper
# --------------------------------------------------------------------------- #

#: Headings that rarely carry a visualisable idea. Read last, not never.
_LOW_VALUE = re.compile(
    r"related|prior work|background|acknowledg|reference|appendix|conclusion|"
    r"discussion|future|limitation|broader|ethic|reproducib",
    re.I,
)
_HIGH_VALUE = re.compile(
    r"method|model|architecture|approach|algorithm|framework|experiment|result|"
    r"analysis|training|objective|loss|derivation|theorem|attention|network|bound",
    re.I,
)


def plan_passes(sb: Storyboard, char_budget: int | None, passes: int) -> list[list[str]]:
    """Group a paper's sections into at most `passes` ANALYZE calls.

    Only when the body overflows the budget; otherwise one call sees everything.
    Sections are ranked by how likely they are to hold an idea worth animating —
    equations and method-like headings first, related work and acknowledgements
    last — and packed whole into groups of roughly `char_budget` characters. Each
    group keeps document order, so the model reads it as the paper flows.
    """
    sections = [s for s in sb.sections if len(s.prose_md) >= MIN_ANALYSABLE_PROSE]
    if not char_budget or passes <= 1 or sum(len(s.raw) for s in sections) <= char_budget:
        return [[s.id for s in sb.sections]]

    def score(s: Section) -> float:
        heading = " ".join(s.heading_path)
        value = 2.0 * bool(s.equation_ids) + 1.0 * bool(s.figure_ids)
        value += 1.5 * bool(_HIGH_VALUE.search(heading))
        value -= 2.5 * bool(_LOW_VALUE.search(heading))
        return value

    order = {s.id: i for i, s in enumerate(sb.sections)}
    ranked = sorted(sections, key=lambda s: (-score(s), order[s.id]))
    groups: list[list[Section]] = []
    sizes: list[int] = []
    for s in ranked:
        size = len(s.raw)
        for i in range(len(groups)):
            if sizes[i] + size <= char_budget:
                groups[i].append(s)
                sizes[i] += size
                break
        else:
            if len(groups) < passes:
                groups.append([s])
                sizes.append(size)  # an oversized section is trimmed by build_prompt
    return [[s.id for s in sorted(g, key=lambda s: order[s.id])] for g in groups]


def _subset(sb: Storyboard, section_ids: list[str]) -> Storyboard:
    keep = set(section_ids)
    sections = [s.model_copy(deep=True) for s in sb.sections if s.id in keep]
    eq_ids = {e for s in sections for e in s.equation_ids}
    fig_ids = {f for s in sections for f in s.figure_ids}
    return Storyboard(
        paper=sb.paper,
        sections=sections,
        equations=[e for e in sb.equations if e.id in eq_ids],
        figures=[f for f in sb.figures if f.id in fig_ids],
    )


def _call_in_passes(
    sb: Storyboard,
    provider: Provider,
    groups: list[list[str]],
    *,
    deadline: float | None = None,
) -> tuple[AnalysisOut, AnalyzeReport]:
    """One ANALYZE call per section group, merged before grounding.

    Grounding runs once, over the merged answer and the WHOLE storyboard, so a
    quote is checked against exactly the text that was sent — section ids are the
    same in every subset. A pass that fails is skipped when another succeeded: a
    paper explained from two thirds of its sections beats no explanation.
    """
    outs: list[AnalysisOut] = []
    report = AnalyzeReport()
    errors: list[str] = []
    import time

    for k, group in enumerate(groups):
        if outs and deadline is not None and time.monotonic() >= deadline:
            # Out of time with something in hand: ship it rather than risk the
            # platform killing the call and losing every pass.
            report.notes.append(f"stopped after pass {k}: out of time")
            break
        p = provider.rotated(k) if hasattr(provider, "rotated") else provider
        try:
            out, rep = _call_provider(_subset(sb, group), p)
        except Exception as exc:
            log.warning("analyze pass %d/%d failed: %s", k + 1, len(groups), exc)
            errors.append(str(exc))
            continue
        outs.append(out)
        report.cost_usd = round(report.cost_usd + rep.cost_usd, 6)
        report.cost_attributed = report.cost_attributed and rep.cost_attributed
        report.provider, report.model = rep.provider, rep.model
    if not outs:
        raise RuntimeError("every analyze pass failed: " + " | ".join(errors[:3]))
    report.notes.append(
        f"analyzed in {len(outs)} of {len(groups)} passes over section groups"
    )
    merged = AnalysisOut.model_construct(
        concepts=[c for o in outs for c in o.concepts],
        opportunities=[op for o in outs for op in o.opportunities],
        section_difficulty=[d for o in outs for d in o.section_difficulty],
        reading_note=" ".join(o.reading_note for o in outs if o.reading_note)[:600],
    )
    return merged, report


# --------------------------------------------------------------------------- #
# The model call
# --------------------------------------------------------------------------- #


def _call_provider(
    sb: Storyboard, provider: Provider
) -> tuple[AnalysisOut, AnalyzeReport]:
    cfg = settings()
    caps = provider.capabilities()
    system_blocks, user = build_prompt(
        sb, caps.prompt_char_budget, caps.analysis_item_budget
    )

    result = provider.structured(
        system=system_blocks,
        user=user,
        output_model=AnalysisOut,
        task=Task.CLASSIFY,
        max_tokens=cfg.models.max_tokens,
        max_attempts=cfg.models.analyze_json_attempts,
    )

    report = AnalyzeReport(
        cost_usd=result.usage.cost_usd,
        cost_attributed=result.usage.attributed,
        provider=result.provider,
        model=result.model,
    )
    if not caps.prompt_cache:
        # Worth stating: without a cache the paper body is re-sent on every call, and
        # for a 12-scene paper that is the dominant cost.
        report.notes.append(
            f"{result.provider} has no prompt cache; the paper body is re-sent per call"
        )
    if result.usage.cache_read_tokens or result.usage.cache_write_tokens:
        report.notes.append(
            f"cache_read={result.usage.cache_read_tokens} "
            f"cache_write={result.usage.cache_write_tokens}"
        )
    if result.attempts > 1:
        report.notes.append(
            f"structured output needed {result.attempts} attempts "
            f"({result.provider} has no constrained decoding)"
        )

    if result.value is None:
        # Include a slice of what actually came back. "No parsable JSON object" names
        # the symptom; only the raw text says whether the model refused, wrote prose,
        # was truncated mid-object, or answered in a shape the schema did not expect —
        # and those need completely different fixes.
        head = (result.raw_text or "").strip()[:400].replace("\n", " ")
        raise RuntimeError(
            f"analyze got no parsable output from {result.provider} "
            f"after {result.attempts} attempts: "
            + "; ".join(result.findings or ["unknown reason"])
            + (f" | model said: {head!r}" if head else " | the model returned nothing")
        )
    return result.value, report


# --------------------------------------------------------------------------- #
# Grounding and triage
# --------------------------------------------------------------------------- #


def _apply(
    sb: Storyboard, raw: AnalysisOut, report: AnalyzeReport
) -> tuple[Storyboard, AnalyzeReport]:
    resolve = _section_resolver(sb)

    # -- concepts, grounded --------------------------------------------------
    concepts: list[Concept] = []
    name_to_id: dict[str, str] = {}
    #: Index into raw.concepts for each entry that survived grounding, so the two
    #: lists can be paired later without guessing.
    kept_raw_indices: list[int] = []
    for i, c in enumerate(raw.concepts):
        section = resolve(c.section_id)
        if section is None:
            report.concepts_dropped_ungrounded += 1
            continue
        span = span_for_text(section, c.quote)
        if span is None:
            report.concepts_dropped_ungrounded += 1
            # Log the quote, not just the name. "dropped ungrounded concept 'X'" says
            # a claim failed to ground but not why, and the two causes need opposite
            # fixes: a paraphrase means the prompt must demand verbatim text, while a
            # correct quote attributed to the wrong section means the section resolver
            # is at fault. The quote itself distinguishes them at a glance.
            log.info(
                "dropped ungrounded concept %r: quote not found in section %s: %r",
                c.name,
                section.id,
                c.quote[:120],
            )
            continue
        cid = f"c{i:03d}"
        name_to_id[_norm(c.name)] = cid
        kept_raw_indices.append(i)
        concepts.append(
            Concept(
                id=cid,
                name=c.name,
                statement=c.statement,
                span=span,
                depends_on=[],  # resolved below, once every name is known
                centrality=c.centrality,
            )
        )

    by_cid = {c.id: c for c in concepts}
    # Pair each created concept with the raw entry it came from by INDEX, recorded at
    # creation time. Re-deriving the pairing by filtering on name misaligns the two
    # lists whenever a dropped concept shares a name with a kept one — every
    # subsequent concept then inherits the wrong entry's dependencies, which is a
    # silently wrong DAG rather than an error.
    for raw_index, concept in zip(kept_raw_indices, concepts, strict=True):
        c_out = raw.concepts[raw_index]
        for dep_name in c_out.depends_on:
            dep_id = name_to_id.get(_norm(dep_name))
            if dep_id is None or dep_id == concept.id:
                report.dependency_edges_dropped += 1
                continue
            concept.depends_on.append(dep_id)

    _break_cycles(concepts, report)
    report.concepts_kept = len(concepts)

    # -- section difficulty --------------------------------------------------
    for entry in raw.section_difficulty:
        section = resolve(entry.section_id)
        if section is not None:
            section.difficulty = entry.difficulty
    for section in sb.sections:
        section.concept_ids = [c.id for c in concepts if c.span.section_id == section.id]
        if section.difficulty is None:
            section.difficulty = 3 if len(section.prose_md) >= MIN_ANALYSABLE_PROSE else 1

    # -- opportunities, grounded then triaged --------------------------------
    report.opportunities_proposed = len(raw.opportunities)
    candidates: list[VisualOpportunity] = []
    for i, o in enumerate(raw.opportunities):
        try:
            archetype = Archetype(o.archetype)
        except ValueError:
            report.opportunities_dropped_unavailable += 1
            continue
        if not registry.is_available(archetype):
            report.opportunities_dropped_unavailable += 1
            log.info("dropped %s: no template in this build", o.archetype)
            continue
        section = resolve(o.section_id)
        if section is None:
            report.opportunities_dropped_ungrounded += 1
            continue
        span = span_for_text(section, o.quote)
        if span is None:
            report.opportunities_dropped_ungrounded += 1
            log.info("dropped ungrounded opportunity %r", o.claim[:60])
            continue
        concept_id = name_to_id.get(_norm(o.concept_name))
        if concept_id is None:
            # Fall back to any concept grounded in the same section rather than
            # discarding a well-grounded proposal over a naming mismatch.
            same_section = [c.id for c in concepts if c.span.section_id == section.id]
            if not same_section:
                report.opportunities_dropped_unknown_concept += 1
                continue
            concept_id = same_section[0]
        candidates.append(
            VisualOpportunity(
                id=f"o{i:03d}",
                archetype=archetype,
                claim=o.claim,
                concept_id=concept_id,
                span=span,
                justification=o.justification,
                difficulty=o.difficulty,
                centrality=o.centrality,
            )
        )

    kept = triage(candidates, by_cid)
    report.opportunities_dropped_triage = len(candidates) - len(kept)
    report.kept_opportunities = len(kept)

    sb.concepts = concepts
    sb.opportunities = kept
    sb.reading_order = _reading_order(sb, concepts)
    if raw.reading_note:
        report.notes.append(raw.reading_note)

    # Revalidate: the model wrote into a typed document and must not have broken it.
    Storyboard.model_validate(sb.model_dump())
    return sb, report


def _section_resolver(sb: Storyboard):
    """Resolve a model's section reference to a real section.

    Accepts the id ("s003") and, because that is what models actually produce, the
    heading path too: "Model Architecture > Attention", or just "Attention". A real
    run returned eighteen well-grounded concepts every one of which cited a heading
    rather than an id — under a strict id lookup all eighteen were dropped as
    ungrounded, which reads as "the model understood nothing" when in fact it had
    understood the paper and labelled its answer differently.

    Ambiguity is refused rather than guessed: two sections sharing a trailing
    heading resolve only by full path, because attaching a claim to the wrong
    section is exactly the silent-wrongness the grounding check exists to prevent.
    """
    by_id = {s.id: s for s in sb.sections}
    by_path: dict[str, Section] = {}
    by_leaf: dict[str, list[Section]] = {}
    for section in sb.sections:
        by_path[_norm(" > ".join(section.heading_path))] = section
        by_leaf.setdefault(_norm(section.heading), []).append(section)

    def resolve(reference: str) -> Section | None:
        if not reference:
            return None
        if (section := by_id.get(reference)) is not None:
            return section
        key = _norm(reference)
        if (section := by_path.get(key)) is not None:
            return section
        matches = by_leaf.get(key) or []
        if len(matches) == 1:
            return matches[0]
        return None  # unknown, or ambiguous: refuse rather than guess

    return resolve


def triage(
    candidates: list[VisualOpportunity], concepts: dict[str, Concept]
) -> list[VisualOpportunity]:
    """Rank by ``difficulty x centrality`` and cut to the cap.

    One extra rule beyond the plan's: at most two animations per section. A
    section with four proposals is a section the model found interesting, not one
    the reader needs four videos for.
    """
    cfg = settings().budgets
    ranked = sorted(candidates, key=lambda o: (-o.rank, o.id))
    per_section: dict[str, int] = {}
    kept: list[VisualOpportunity] = []
    for o in ranked:
        sec = o.span.section_id
        if per_section.get(sec, 0) >= 2:
            continue
        kept.append(o)
        per_section[sec] = per_section.get(sec, 0) + 1
        if len(kept) >= cfg.max_scenes:
            break
    return kept


def _reading_order(sb: Storyboard, concepts: list[Concept]) -> list[str]:
    """Document order, which for a paper is already the intended reading order.

    The concept DAG governs *animation* prerequisites, not section sequence —
    reordering a paper's sections would make it harder to follow along with the
    original, which is the thing ArcVisual is a companion to.
    """
    if concepts:
        topological_concept_order(concepts)  # asserts the DAG is usable
    return [s.id for s in sb.sections]


def _break_cycles(concepts: list[Concept], report: AnalyzeReport) -> None:
    """Drop the minimum edges needed to make the dependency graph a DAG."""
    while True:
        try:
            assert_acyclic(concepts)
            return
        except ValueError:
            # Remove one edge from the highest-degree node in the cycle set and
            # retry. Crude, but it converges and it never fails the job.
            worst = max(concepts, key=lambda c: len(c.depends_on))
            if not worst.depends_on:
                for c in concepts:
                    c.depends_on = []
                return
            worst.depends_on.pop()
            report.dependency_edges_dropped += 1


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# --------------------------------------------------------------------------- #
# Offline analyzer
# --------------------------------------------------------------------------- #

_RESULT_WORDS = ("result", "experiment", "evaluation", "ablation", "benchmark")
_ARCH_WORDS = ("architect", "model", "framework", "overview", "system", "pipeline")


def heuristic_analysis(sb: Storyboard) -> AnalysisOut:
    """A deterministic stand-in for the model, used offline and in CI.

    It exists for the Phase 1 build order: templates are validated with
    hand-filled parameters *before* codegen is trusted, and the eval harness must
    run without a key so CI stays deterministic. It is not a fallback for
    production — the notes make its use visible in the trace.
    """
    from arcvisual.analyze.prompts import (
        ConceptOut,
        OpportunityOut,
        SectionDifficultyOut,
    )

    concepts: list[ConceptOut] = []
    opportunities: list[OpportunityOut] = []
    difficulties: list[SectionDifficultyOut] = []

    analysable = [s for s in sb.sections if len(s.prose_md) >= MIN_ANALYSABLE_PROSE]
    eq_by_section: dict[str, list[str]] = {}
    for eq in sb.equations:
        eq_by_section.setdefault(eq.span.section_id, []).append(eq.latex)

    for s in analysable:
        quote = _first_sentence(s)
        if quote is None:
            continue
        name = s.heading
        concepts.append(
            ConceptOut(
                name=name,
                statement=f"The paper's treatment of {name.lower()}.",
                section_id=s.id,
                quote=quote,
                depends_on=[],
                centrality=round(min(1.0, 0.4 + 0.1 * len(s.equation_ids)), 3),
            )
        )
        heading_l = " ".join(s.heading_path).lower()
        difficulty = 4 if s.equation_ids else 3
        difficulties.append(SectionDifficultyOut(section_id=s.id, difficulty=difficulty))

        archetype: str | None = None
        if len(eq_by_section.get(s.id, [])) >= 1:
            archetype = "transform_chain"
        elif any(w in heading_l for w in _RESULT_WORDS):
            archetype = "plot_reveal"
        elif s.figure_ids or any(w in heading_l for w in _ARCH_WORDS):
            archetype = "architecture_flow"
        if archetype is None or not registry.is_available(archetype):
            continue

        opportunities.append(
            OpportunityOut(
                archetype=archetype,
                claim=f"How {name.lower()} works, one step at a time.",
                concept_name=name,
                section_id=s.id,
                quote=quote,
                justification=(
                    "Heuristic selection: this section carries equations or a "
                    "figure that the prose alone does not unpack."
                ),
                difficulty=difficulty,
                centrality=round(min(1.0, 0.4 + 0.1 * len(s.equation_ids)), 3),
            )
        )

    return AnalysisOut(
        concepts=concepts,
        section_difficulty=difficulties,
        opportunities=opportunities,
        reading_note="Produced by the offline heuristic analyzer, not by a model.",
    )


def _first_sentence(section: Section, min_len: int = 40) -> str | None:
    """A quote guaranteed to ground, taken from the section's own raw source."""
    for match in re.finditer(rf"[A-Z][^.!?]{{{min_len},300}}[.!?]", section.raw):
        text = match.group(0).strip()
        if "\\" in text or "$" in text:
            continue  # avoid TeX, which strip_tex would have mangled
        if quote_hash(text) and section.raw.find(text) != -1:
            return text
    return None

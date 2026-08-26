"""The contract's invariants. These are the tests that must never be relaxed.

Each one guards a property the product's credibility rests on: grounding cannot be
optional, a scene cannot exist without an argument, a dependency graph cannot cycle,
and a stage cannot write another stage's fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.storyboard import (
    Archetype,
    Beat,
    Concept,
    Scene,
    SceneSpec,
    SceneState,
    SourceDriftError,
    SourceSpan,
    Stage,
    Storyboard,
    assert_acyclic,
    assert_monotonic,
    quote_hash,
    topological_concept_order,
)
from tests.fixtures import metadata, paper_tarball


@pytest.fixture(scope="module")
def sb() -> Storyboard:
    return build_storyboard(metadata(), paper_tarball())


def _span(section, text: str) -> SourceSpan:
    start = section.raw.index(text)
    return SourceSpan(
        section_id=section.id,
        start=start,
        end=start + len(text),
        quote_sha256=quote_hash(text),
    )


# -- grounding -------------------------------------------------------------- #


def test_span_resolves_against_its_section(sb: Storyboard) -> None:
    for eq in sb.equations:
        assert eq.span.resolve(sb.section(eq.span.section_id))


def test_span_detects_source_drift(sb: Storyboard) -> None:
    """The whole point of quote_sha256: a paper revision must not silently
    re-point an existing claim at different text."""
    section = sb.section(sb.equations[0].span.section_id)
    drifted = section.model_copy(update={"raw": section.raw.replace("attention", "XXX")})
    with pytest.raises(SourceDriftError):
        sb.equations[0].span.resolve(drifted)


def test_empty_span_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(section_id="s000", start=10, end=10, quote_sha256="x")
    with pytest.raises(ValidationError):
        SourceSpan(section_id="s000", start=10, end=4, quote_sha256="x")


def test_scene_must_cite_a_known_section(sb: Storyboard) -> None:
    section = sb.sections[0]
    spec = SceneSpec(
        id="x",
        archetype=Archetype.TRANSFORM_CHAIN,
        claim="a claim long enough to pass",
        concept_id="c000",
        span=SourceSpan(
            section_id="does-not-exist", start=0, end=5, quote_sha256=quote_hash("hello")
        ),
        params={},
        beats=(Beat(t=0, dur=1.0, caption="x"),),
    )
    with pytest.raises(ValidationError, match="unknown section"):
        sb.model_copy(update={"scenes": [Scene(spec=spec)]}).model_validate(
            sb.model_copy(update={"scenes": [Scene(spec=spec)]}).model_dump()
        )
    assert section.id  # sanity


# -- the argument requirement ---------------------------------------------- #


def test_scene_without_a_claim_is_rejected(sb: Storyboard) -> None:
    """Decorative animation is banned at the schema level, not by review."""
    section = sb.sections[0]
    with pytest.raises(ValidationError):
        SceneSpec(
            id="x",
            archetype=Archetype.PLOT_REVEAL,
            claim="",  # no claim, no scene
            concept_id="c000",
            span=_span(section, "Recurrent"),
            params={},
            beats=(Beat(t=0, dur=1.0, caption="x"),),
        )


def test_beat_below_the_pacing_floor_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Beat(t=0.0, dur=0.2, caption="too fast to read")


def test_overlapping_beats_are_rejected(sb: Storyboard) -> None:
    section = sb.sections[0]
    with pytest.raises(ValidationError, match="overlap"):
        SceneSpec(
            id="x",
            archetype=Archetype.PLOT_REVEAL,
            claim="a claim long enough to pass",
            concept_id="c000",
            span=_span(section, "Recurrent"),
            params={},
            beats=(
                Beat(t=0.0, dur=2.0, caption="a"),
                Beat(t=1.0, dur=2.0, caption="b"),  # starts before a ends
            ),
        )


def test_passed_scene_needs_an_artifact(sb: Storyboard) -> None:
    spec = SceneSpec(
        id="x",
        archetype=Archetype.PLOT_REVEAL,
        claim="a claim long enough to pass",
        concept_id="c000",
        span=_span(sb.sections[0], "Recurrent"),
        params={},
        beats=(Beat(t=0, dur=1.0, caption="x"),),
    )
    with pytest.raises(ValidationError, match="without an artifact"):
        Scene(spec=spec, state=SceneState.PASSED)


def test_degraded_scene_needs_a_reason(sb: Storyboard) -> None:
    spec = SceneSpec(
        id="x",
        archetype=Archetype.PLOT_REVEAL,
        claim="a claim long enough to pass",
        concept_id="c000",
        span=_span(sb.sections[0], "Recurrent"),
        params={},
        beats=(Beat(t=0, dur=1.0, caption="x"),),
    )
    with pytest.raises(ValidationError, match="without a reason"):
        Scene(spec=spec, state=SceneState.DEGRADED)


# -- the dependency DAG ---------------------------------------------------- #


def _concept(cid: str, deps: list[str], section_id: str, text: str, raw: str) -> Concept:
    start = raw.index(text)
    return Concept(
        id=cid,
        name=cid,
        statement="s",
        span=SourceSpan(
            section_id=section_id,
            start=start,
            end=start + len(text),
            quote_sha256=quote_hash(text),
        ),
        depends_on=deps,
        centrality=0.5,
    )


def test_cycle_is_rejected(sb: Storyboard) -> None:
    """A cycle would let us animate an idea before its prerequisite."""
    raw = sb.sections[0].raw
    sid = sb.sections[0].id
    concepts = [
        _concept("a", ["b"], sid, "Recurrent", raw),
        _concept("b", ["a"], sid, "neural", raw),
    ]
    with pytest.raises(ValueError, match="cycle"):
        assert_acyclic(concepts)


def test_topological_order_respects_dependencies(sb: Storyboard) -> None:
    raw = sb.sections[0].raw
    sid = sb.sections[0].id
    concepts = [
        _concept("c", ["b"], sid, "Recurrent", raw),
        _concept("b", ["a"], sid, "neural", raw),
        _concept("a", [], sid, "networks", raw),
    ]
    order = topological_concept_order(concepts)
    assert order.index("a") < order.index("b") < order.index("c")


# -- stage ownership -------------------------------------------------------- #


def test_analyze_may_not_write_ingest_fields(sb: Storyboard) -> None:
    after = sb.model_copy(deep=True)
    after.paper.title = "tampered"
    with pytest.raises(Exception, match="does not own"):
        assert_monotonic(sb, after, Stage.ANALYZE)


def test_analyze_may_write_its_own_fields(sb: Storyboard) -> None:
    after = sb.model_copy(deep=True)
    after.reading_order = [s.id for s in after.sections]
    after.sections[0].difficulty = 4
    assert_monotonic(sb, after, Stage.ANALYZE)  # must not raise


def test_generate_may_not_write_analyze_fields(sb: Storyboard) -> None:
    after = sb.model_copy(deep=True)
    after.reading_order = ["s000"]
    with pytest.raises(Exception, match="does not own"):
        assert_monotonic(sb, after, Stage.GENERATE)


# -- integrity -------------------------------------------------------------- #


def test_duplicate_section_ids_are_rejected(sb: Storyboard) -> None:
    doc = sb.model_dump()
    doc["sections"].append(doc["sections"][0])
    with pytest.raises(ValidationError, match="duplicate section ids"):
        Storyboard.model_validate(doc)


def test_progress_payload_is_small_and_complete(sb: Storyboard) -> None:
    progress = sb.progress()
    assert set(progress) == {
        "sections",
        "concepts",
        "scenes_total",
        "scenes_done",
        "scenes_by_state",
        "cost_usd",
    }

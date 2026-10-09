"""``concept_diagram``: the model draws the idea instead of re-typesetting it.

The failure this exists for was reported by a reader, not found by a test: every
paper came out as its equations written onto the screen one line at a time. The
tests here hold the three things that fix it in place:

* the template validates what is structural and repairs what is cosmetic, so a
  model's drawing reaches the reader instead of burning its repair budget;
* the analyzer is told to draw, and triage stops a paper from being explained as a
  run of formulas;
* the browser can draw it — it is client-renderable, so the serverless deployment
  offers it — and the stored params survive Gate 1's re-validation unchanged.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from arcvisual.storyboard import Archetype
from arcvisual.templates import concept_diagram as cd
from arcvisual.templates import registry

ATTENTION = {
    "title": "Scaled dot-product attention",
    "elements": [
        {
            "id": "q",
            "kind": "stack",
            "label": "Queries Q",
            "column": 0,
            "row": 0,
            "cells": ["q₁", "q₂", "q₃"],
            "tone": "input",
        },
        {
            "id": "k",
            "kind": "stack",
            "label": "Keys K",
            "column": 0,
            "row": 1,
            "cells": ["k₁", "k₂", "k₃"],
            "tone": "input",
        },
        {"id": "dot", "kind": "op", "label": "QKᵀ", "column": 1, "row": 0, "row_span": 2},
        {
            "id": "scores",
            "kind": "grid",
            "label": "scores",
            "column": 2,
            "row": 0,
            "row_span": 2,
            "size": 3,
            "values": [2.0, 0.5, 0.5, 0.5, 2.0, 1.0, 0.0, 1.0, 4.0],
        },
        {
            "id": "sm",
            "kind": "op",
            "label": "softmax",
            "column": 3,
            "row": 0,
            "row_span": 2,
            "tone": "accent",
        },
        {
            "id": "w",
            "kind": "bars",
            "label": "weights",
            "column": 4,
            "row": 0,
            "cells": ["v₁", "v₂", "v₃"],
            "values": [0.78, 0.13, 0.09],
        },
        {
            "id": "v",
            "kind": "stack",
            "label": "Values V",
            "column": 4,
            "row": 1,
            "cells": ["v₁", "v₂", "v₃"],
        },
        {
            "id": "out",
            "kind": "block",
            "label": "Output",
            "column": 5,
            "row": 0,
            "row_span": 2,
            "tone": "output",
        },
    ],
    "connections": [
        {"src": "q", "dst": "dot"},
        {"src": "k", "dst": "dot"},
        {"src": "dot", "dst": "scores", "label": "÷ √dₖ"},
        {"src": "scores", "dst": "sm"},
        {"src": "sm", "dst": "w"},
        {"src": "w", "dst": "out"},
        {"src": "v", "dst": "out"},
    ],
    "steps": [
        {"caption": "Every token brings a query and a key.", "show": ["q", "k"]},
        {
            "caption": "Each query meets every key.",
            "show": ["dot", "scores"],
            "flow": ["q", "dot", "scores"],
        },
        {
            "caption": "Row 1 is how well q₁ matches each key.",
            "marks": [{"target": "scores", "item": 0, "col": c} for c in range(3)],
        },
        {"caption": "Softmax turns the row into weights.", "show": ["sm", "w"]},
        {
            "caption": "The weights mix the values.",
            "show": ["v", "out"],
            "flow": ["w", "out"],
            "focus": ["out"],
        },
    ],
}


def _params(**changes):
    raw = copy.deepcopy(ATTENTION)
    raw.update(changes)
    return cd.Params.model_validate(raw)


# --------------------------------------------------------------------------- #
# The template
# --------------------------------------------------------------------------- #


def test_a_real_diagram_validates_and_narrates_its_steps() -> None:
    p = _params()
    assert p.captions == [s["caption"] for s in ATTENTION["steps"]]
    assert cd.captions_for(p) == p.captions


def test_stored_params_survive_gate1_revalidation_unchanged() -> None:
    """Gate 1 validates the DUMPED params again. Normalisation that is not
    idempotent would change the scene between codegen and the reader."""
    once = _params().model_dump(mode="json")
    twice = cd.Params.model_validate(once).model_dump(mode="json")
    assert once == twice


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"connections": [{"src": "q", "dst": "nowhere"}]}, "unknown"),
        ({"connections": [{"src": "q", "dst": "q"}]}, "self-loop"),
        ({"steps": [{"caption": "Focus on a ghost.", "focus": ["ghost"]}]}, "unknown"),
    ],
)
def test_references_to_nothing_are_rejected(change, match) -> None:
    """Structural: the model meant something we cannot draw."""
    with pytest.raises(ValidationError, match=match):
        _params(**change)


def test_two_elements_in_one_cell_are_rejected() -> None:
    raw = copy.deepcopy(ATTENTION)
    raw["elements"][1]["row"] = 0  # keys onto the queries' cell
    with pytest.raises(ValidationError, match="share grid cell"):
        cd.Params.model_validate(raw)


def test_a_container_may_span_the_elements_it_groups() -> None:
    raw = copy.deepcopy(ATTENTION)
    raw["elements"].append(
        {
            "id": "head",
            "kind": "container",
            "label": "One attention head",
            "column": 1,
            "row": 0,
            "col_span": 3,
            "row_span": 2,
        }
    )
    p = cd.Params.model_validate(raw)
    # It appears with the first element inside it, not at the end.
    first = next(i for i, s in enumerate(p.steps) if "head" in s.show)
    assert first == next(i for i, s in enumerate(p.steps) if "dot" in s.show)


def test_every_element_is_revealed_no_later_than_its_first_use() -> None:
    """Cosmetic: an element the script forgot would never be drawn, and one
    focused before it is shown would pulse on an empty canvas."""
    raw = copy.deepcopy(ATTENTION)
    raw["steps"] = [
        {"caption": "Look at the output first.", "focus": ["out"]},
        {"caption": "Then everything else.", "show": ["q"]},
    ]
    p = cd.Params.model_validate(raw)
    assert "out" in p.steps[0].show
    shown = [eid for s in p.steps for eid in s.show]
    assert sorted(shown) == sorted(e["id"] for e in ATTENTION["elements"])
    assert len(shown) == len(set(shown)), "an element revealed twice"


def test_values_are_normalised_and_bad_marks_dropped() -> None:
    raw = copy.deepcopy(ATTENTION)
    raw["steps"][2]["marks"].append({"target": "scores", "item": 7, "col": 0})
    raw["steps"][2]["marks"].append({"target": "out", "item": 0, "col": 0})
    p = cd.Params.model_validate(raw)
    grid = next(e for e in p.elements if e.id == "scores")
    assert max(grid.values) == 1.0 and len(grid.values) == 9
    assert all(m.target == "scores" and m.item == 0 for m in p.steps[2].marks)


def test_a_one_stop_flow_becomes_a_focus() -> None:
    raw = copy.deepcopy(ATTENTION)
    raw["steps"][0]["flow"] = ["q"]
    p = cd.Params.model_validate(raw)
    assert p.steps[0].flow == [] and "q" in p.steps[0].focus


def test_tex_left_in_a_label_is_stripped() -> None:
    raw = copy.deepcopy(ATTENTION)
    raw["elements"][2]["label"] = "$\\mathrm{softmax}$"
    p = cd.Params.model_validate(raw)
    assert p.elements[2].label == "softmax"


def test_estimate_duration_is_the_sum_of_its_phases() -> None:
    p = _params()
    phases = cd.step_segments(p)
    assert len(phases) == len(p.steps)
    expected = sum(sum(seg.values()) for seg in phases) + cd.MIN_BEAT_S + cd.REVEAL_HOLD_S
    assert cd.estimate_duration(p) == pytest.approx(expected)
    # Every phase that plays is at or above the pacing floor the mixin enforces.
    for seg in phases:
        for name, dur in seg.items():
            assert dur == 0 or dur >= cd.MIN_BEAT_S or name == "hold"


def test_an_ambitious_diagram_still_fits_the_runtime_ceiling() -> None:
    from arcvisual.config import settings

    elements = [
        {
            "id": f"e{i}",
            "kind": "block",
            "label": f"Part {i}",
            "column": i % 8,
            "row": i // 8,
        }
        for i in range(16)
    ]
    steps = [
        {
            "caption": f"Step {i}.",
            "show": [f"e{2 * i}", f"e{2 * i + 1}"],
            "focus": [f"e{2 * i}"],
            "flow": [f"e{j}" for j in range(8)],
            "marks": [],
            "relabel": [{"id": f"e{2 * i}", "label": "changed"}],
        }
        for i in range(8)
    ]
    p = cd.Params.model_validate({"elements": elements, "steps": steps})
    assert cd.estimate_duration(p) <= settings().render.max_runtime_s
    # Trimmed, not truncated: the finished frame still holds the whole drawing, and
    # the trimmed script is stable under Gate 1's re-validation.
    assert {eid for s in p.steps for eid in s.show} == {e["id"] for e in elements}
    dumped = p.model_dump(mode="json")
    assert cd.Params.model_validate(dumped).model_dump(mode="json") == dumped


def test_simplify_drops_annotation_not_structure() -> None:
    p = _params()
    simpler = cd.simplify(p)
    assert simpler is not None
    assert not any(c.label for c in simpler.connections)
    assert not any(s.marks for s in simpler.steps)
    assert len(simpler.elements) == len(p.elements)


# --------------------------------------------------------------------------- #
# Where it is offered
# --------------------------------------------------------------------------- #


def test_it_is_offered_in_client_mode(reset_settings) -> None:
    """The deployed default animates in the browser; withholding the one archetype
    that draws ideas there is how every paper became a formula chain."""
    reset_settings(ARCVISUAL_RENDER_MODE="client")
    assert Archetype.CONCEPT_DIAGRAM in registry.CLIENT_RENDERABLE
    assert registry.is_available(Archetype.CONCEPT_DIAGRAM)


def test_the_analyzer_is_told_to_draw_first() -> None:
    from arcvisual.analyze.prompts import SYSTEM_ROLE, archetype_help

    help_text = archetype_help()
    assert help_text.index("concept_diagram") < help_text.index("transform_chain")
    assert "DRAW THE MECHANISM" in SYSTEM_ROLE


def _opportunity(sb, archetype: Archetype, i: int, section_id: str):
    base = sb.opportunities[0]
    return base.model_copy(
        update={
            "id": f"opp{i}",
            "archetype": archetype,
            "span": base.span.model_copy(update={"section_id": section_id}),
        }
    )


def test_triage_recasts_formula_scenes_past_the_cap(reset_settings) -> None:
    from arcvisual.analyze.single_pass import analyze, triage
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.fixtures import metadata, paper_tarball

    reset_settings(ARCVISUAL_MAX_FORMULA_SCENES="2")
    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    sections = [s.id for s in sb.sections]
    chains = [
        _opportunity(sb, Archetype.TRANSFORM_CHAIN, i, sections[i % len(sections)])
        for i in range(5)
    ]
    kept = triage(chains, {})
    formulas = [o for o in kept if o.archetype is Archetype.TRANSFORM_CHAIN]
    drawn = [o for o in kept if o.archetype is Archetype.CONCEPT_DIAGRAM]
    assert len(formulas) == 2
    assert drawn, "claims past the cap should be drawn, not lost"
    assert {o.claim for o in drawn} <= {o.claim for o in chains}


# --------------------------------------------------------------------------- #
# Codegen
# --------------------------------------------------------------------------- #


class _DiagramProvider:
    """Answers codegen with the attention diagram, and keeps what it was sent."""

    name = "stub"

    def __init__(self, budget: int | None = None) -> None:
        self.budget = budget
        self.system: list = []
        self.user = ""

    def capabilities(self):
        from arcvisual.providers.base import Capabilities

        return Capabilities(
            name="stub",
            native_structured_output=True,
            prompt_cache=False,
            vision=False,
            cost_attributed=True,
            prompt_char_budget=self.budget,
        )

    def model_for(self, task):
        return "stub-model"

    def structured(self, *, system, user, output_model, task, **_):
        from arcvisual.providers.base import StructuredResult, Usage

        self.system, self.user = system, user
        value = output_model.model_validate(
            {
                "params": copy.deepcopy(ATTENTION),
                "beats": [{"caption": "a different line", "dur": 2.0}],
                "scrubbable": False,
            }
        )
        return StructuredResult(value=value, usage=Usage(), provider="stub", model="m")


@pytest.fixture(scope="module")
def storyboard():
    from arcvisual.analyze.single_pass import analyze
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.fixtures import metadata, paper_tarball

    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    return sb


def test_codegen_guides_the_drawing_and_narrates_from_the_steps(storyboard) -> None:
    from arcvisual.analyze.prompts import DIAGRAM_GUIDE
    from arcvisual.generate.codegen import generate_scene

    opp = storyboard.opportunities[0].model_copy(
        update={"archetype": Archetype.CONCEPT_DIAGRAM}
    )
    provider = _DiagramProvider(budget=300_000)
    result = generate_scene(storyboard, opp, provider=provider)

    assert result.params_obj is not None, result.notes
    assert any(b.text == DIAGRAM_GUIDE for b in provider.system)
    # The beats are the step captions, not the model's parallel list.
    assert [b.caption for b in result.scene.spec.beats] == [
        s["caption"] for s in ATTENTION["steps"]
    ]
    # A long-context provider is given the section, not only the quote.
    assert "The full section this comes from" in provider.user
    assert "concept_diagram" in result.source


def test_a_metered_provider_is_not_sent_the_section(storyboard) -> None:
    from arcvisual.generate.codegen import generate_scene

    opp = storyboard.opportunities[0].model_copy(
        update={"archetype": Archetype.CONCEPT_DIAGRAM}
    )
    provider = _DiagramProvider(budget=5000)
    generate_scene(storyboard, opp, provider=provider)
    assert "The full section this comes from" not in provider.user


def test_offline_diagram_params_pass_gate1(storyboard) -> None:
    from arcvisual.gates import g1_static
    from arcvisual.generate.codegen import generate_scene

    opp = storyboard.opportunities[0].model_copy(
        update={"archetype": Archetype.CONCEPT_DIAGRAM}
    )
    result = generate_scene(storyboard, opp)  # heuristic: no provider configured
    assert result.params_obj is not None, result.notes
    gate1, _ = g1_static.run(
        result.source, Archetype.CONCEPT_DIAGRAM, result.scene.spec.params, run_ruff=False
    )
    assert gate1.passed, gate1.findings

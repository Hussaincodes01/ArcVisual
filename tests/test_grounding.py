"""Grounding: a correct quote must ground, an invented one must not.

The failure this covers came from production. "Attention Is All You Need" shipped
with two concepts and no visuals. The free model quoted the paper with its LaTeX
cleaned up ("dimension d_k" for ``dimension $d_k$``), so exact matching dropped
most quotes. Every opportunity whose concept had been dropped went with it.

Tolerance must not become approximation. Each span still points at exact
characters of the paper, and a sentence the paper never wrote still fails.
"""

from __future__ import annotations

import pytest

from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.ingest.latex import span_for_text
from arcvisual.storyboard import Section
from tests.fixtures import metadata, paper_tarball


def _section(raw: str, sid: str = "s001") -> Section:
    return Section(
        id=sid,
        heading_path=["Method"],
        raw=raw,
        prose_md=raw,
        equation_ids=[],
        figure_ids=[],
    )


RAW = (
    "We call our particular attention mechanism ``scaled dot-product attention''.\n"
    "The input consists of queries and keys of dimension $d_k$, and values of\n"
    "dimension $d_v$~\\cite{vaswani2017}. We compute the dot products of the query\n"
    "with all keys, divide each by $\\sqrt{d_k}$, and apply a softmax function."
)


@pytest.mark.parametrize(
    "quote",
    [
        # TeX math delimiters dropped
        "The input consists of queries and keys of dimension d_k, and values of dimension d_v",
        # citation dropped, curly quotes, different case
        "We call our particular attention mechanism “scaled dot-product attention”",
        # \sqrt dropped
        "divide each by d_k, and apply a softmax function",
    ],
)
def test_a_quote_with_its_latex_cleaned_up_still_grounds(quote: str) -> None:
    span = span_for_text(_section(RAW), quote)
    assert span is not None, quote
    grounded = RAW[span.start : span.end]
    # The span is real text of the paper, not the model's rewrite of it.
    assert grounded in RAW
    assert grounded.split()[0].lower().strip("`") == quote.split()[0].lower().strip("“")


def test_a_partly_copied_quote_grounds_to_its_verbatim_run() -> None:
    quote = (
        "We compute the dot products of the query with all keys, divide each by "
        "the magic constant"
    )
    span = span_for_text(_section(RAW), quote)
    assert span is not None
    assert RAW[span.start : span.end].startswith("We compute the dot products")


def test_a_mostly_invented_quote_does_not_ground_on_its_real_part() -> None:
    """14 verbatim words out of 24 is under the 60% floor: the claim would rest
    mostly on words the paper never wrote."""
    quote = (
        "We compute the dot products of the query with all keys, divide each by "
        "the magic constant, and then do something the paper never said at all"
    )
    assert span_for_text(_section(RAW), quote) is None


@pytest.mark.parametrize(
    "invented",
    [
        "Attention lets every token look at every other token in a single step",
        "The softmax function",  # a few real words, too short to be evidence
        "queries keys values attention softmax dimension function input query keys",
    ],
)
def test_text_the_paper_never_wrote_does_not_ground(invented: str) -> None:
    assert span_for_text(_section(RAW), invented) is None


def test_the_exact_path_is_unchanged() -> None:
    quote = "We compute the dot products of the query"
    span = span_for_text(_section(RAW), quote)
    assert span is not None and RAW[span.start : span.end] == quote


# --------------------------------------------------------------------------- #
# Analyze: attribution and the concept cascade
# --------------------------------------------------------------------------- #


class _Stub:
    """Returns a fixed AnalysisOut."""

    name = "stub"

    def __init__(self, out) -> None:
        self.out = out

    def capabilities(self):
        from arcvisual.providers.base import Capabilities

        return Capabilities(
            name="stub",
            native_structured_output=True,
            prompt_cache=False,
            vision=False,
            cost_attributed=True,
        )

    def model_for(self, task):
        return "stub"

    def structured(self, **_):
        from arcvisual.providers.base import StructuredResult, Usage

        return StructuredResult(value=self.out, usage=Usage(), provider="stub", model="m")


def _analyse(concepts, opportunities):
    from arcvisual.analyze.prompts import AnalysisOut
    from arcvisual.analyze.single_pass import analyze

    sb = build_storyboard(metadata(), paper_tarball())
    out = AnalysisOut.model_validate(
        {"concepts": concepts, "opportunities": opportunities, "section_difficulty": []}
    )
    return analyze(sb, provider=_Stub(out))


QUOTE_S003 = (
    "We compute the dot products of the query with all keys, divide each by the "
    "square root of the key dimension"
)


def test_a_quote_attributed_to_the_wrong_section_is_found_in_the_right_one() -> None:
    sb, _ = _analyse(
        [
            {
                "name": "Scaled attention",
                "statement": "Dot products scaled by the key dimension.",
                "section_id": "s000",  # wrong: the text is in s003
                "quote": QUOTE_S003,
                "depends_on": [],
                "centrality": 0.9,
            }
        ],
        [],
    )
    assert [c.span.section_id for c in sb.concepts] == ["s003"]


def test_a_grounded_opportunity_survives_without_its_concept() -> None:
    """The cascade that emptied a real article: the opportunity's own quote
    grounds, its named concept did not, and no other concept shared its section."""
    sb, _ = _analyse(
        [],
        [
            {
                "archetype": "concept_diagram",
                "claim": "Each query is scored against every key, then scaled.",
                "concept_name": "Scaled dot-product attention",
                "section_id": "s003",
                "quote": QUOTE_S003,
                "justification": "The mechanism is a process the formula hides.",
                "difficulty": 4,
                "centrality": 0.9,
            }
        ],
    )
    assert len(sb.opportunities) == 1
    concept = sb.concept(sb.opportunities[0].concept_id)
    assert concept is not None and concept.name == "Scaled dot-product attention"
    assert concept.span.section_id == "s003"
    section = next(s for s in sb.sections if s.id == "s003")
    assert concept.id in section.concept_ids

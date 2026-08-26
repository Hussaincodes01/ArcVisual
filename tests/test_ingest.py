"""Ingest: offsets that round-trip, licences read correctly, rejections that fire.

The offset tests are the important ones. Everything downstream is grounded by
character offsets into ``Section.raw``, so an off-by-one here becomes a confident
animation of the wrong sentence — the failure mode the plan calls critical.
"""

from __future__ import annotations

import pytest

from arcvisual.ingest.arxiv import (
    build_storyboard,
    check_metadata_gate,
    extract_arxiv_id,
    parse_atom,
    slugify,
    unpack_source,
)
from arcvisual.ingest.errors import IngestRejection, RejectCode
from arcvisual.ingest.latex import (
    match_brace,
    parse,
    span_for_text,
    strip_tex,
)
from tests.fixtures import (
    ARXIV_DEFAULT,
    ATOM_RESPONSE,
    CC_BY,
    PAPER_TEX,
    metadata,
    paper_tarball,
)

# -- URL resolution --------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762v7", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762", "1706.03762"),
        ("http://arxiv.org/html/2401.12345", "2401.12345"),
        ("1706.03762", "1706.03762"),
        ("arXiv:1706.03762", "1706.03762"),
    ],
)
def test_extracts_arxiv_id(url: str, expected: str) -> None:
    assert extract_arxiv_id(url) == expected


def test_non_arxiv_url_is_rejected_with_a_reason() -> None:
    with pytest.raises(IngestRejection) as exc:
        extract_arxiv_id("https://example.com/some-paper.pdf")
    assert exc.value.code is RejectCode.UNRESOLVABLE_URL
    assert "could not work out" in exc.value.user_facing


def test_slug_is_disambiguated_by_id() -> None:
    """Two papers can share a title, and papers.slug is UNIQUE."""
    a = slugify("Attention Is All You Need", "1706.03762")
    b = slugify("Attention Is All You Need", "9999.00001")
    assert a != b
    assert a == "attention-is-all-you-need-1706.03762"


# -- LaTeX ------------------------------------------------------------------ #


def test_match_brace_handles_nesting_and_escapes() -> None:
    text = r"{a{b}c\}d}rest"
    assert text[: match_brace(text, 0)] == r"{a{b}c\}d}"


def test_match_brace_rejects_unbalanced() -> None:
    with pytest.raises(ValueError, match="unbalanced"):
        match_brace("{a{b}", 0)


def test_strip_tex_drops_non_prose_environments() -> None:
    out = strip_tex(r"Before \begin{tabular}{cc}1&2\end{tabular} After")
    assert "1&2" not in out
    assert "Before" in out and "After" in out


def test_sections_follow_the_documents_own_hierarchy() -> None:
    doc = parse(PAPER_TEX)
    paths = [" > ".join(s.heading_path) for s in doc.sections]
    assert "Method" in paths
    assert "Method > Scaled Dot-Product Attention" in paths
    assert "Method > Multi-Head Attention" in paths
    # Depth is real, not flattened.
    assert max(s.depth for s in doc.sections) == 2


def test_every_equation_span_round_trips() -> None:
    doc = parse(PAPER_TEX)
    by_id = {s.id: s for s in doc.sections}
    assert doc.equations
    for eq in doc.equations:
        resolved = eq.span.resolve(by_id[eq.span.section_id])
        assert "begin{equation}" in resolved


def test_equation_latex_is_verbatim_and_label_extracted() -> None:
    doc = parse(PAPER_TEX)
    attn = next(e for e in doc.equations if e.label == "eq:attn")
    assert r"\mathrm{softmax}" in attn.latex
    assert r"\sqrt{d_k}" in attn.latex
    # The label is stripped out of the LaTeX but kept as a field.
    assert r"\label" not in attn.latex


def test_figure_caption_and_path_are_extracted() -> None:
    doc = parse(PAPER_TEX)
    assert len(doc.figures) == 1
    fig = doc.figures[0]
    assert "encoder stack" in fig.caption
    assert fig.origin_url == "figures/architecture.pdf"
    assert fig.label == "fig:arch"


def test_span_for_text_grounds_exact_and_reflowed_quotes() -> None:
    doc = parse(PAPER_TEX)
    section = next(s for s in doc.sections if "Scaled" in " ".join(s.heading_path))
    exact = "pushing the softmax function into regions where it has extremely\nsmall gradients"
    reflowed = (
        "pushing the softmax function into regions where it has extremely small gradients"
    )
    for quote in (exact, reflowed):
        span = span_for_text(section, quote)
        assert span is not None, quote
        assert "softmax" in span.resolve(section)


def test_span_for_text_returns_none_rather_than_guessing() -> None:
    """An ungroundable claim must be dropped, never approximated."""
    doc = parse(PAPER_TEX)
    assert span_for_text(doc.sections[0], "the model uses a quantum kernel") is None


def test_document_without_sections_is_rejected() -> None:
    with pytest.raises(IngestRejection) as exc:
        parse(r"\begin{document}Just a wall of text.\end{document}")
    assert exc.value.code is RejectCode.TOO_FEW_SECTIONS


# -- source unpacking ------------------------------------------------------- #


def test_unpacks_a_tarball() -> None:
    files = unpack_source(paper_tarball())
    assert "ms.tex" in files
    assert r"\begin{document}" in files["ms.tex"]


def test_unpacks_a_bare_tex_file() -> None:
    """arXiv e-prints arrive in whatever shape the author uploaded."""
    files = unpack_source(PAPER_TEX.encode())
    assert list(files) == ["main.tex"]


def test_pdf_only_submission_is_rejected() -> None:
    with pytest.raises(IngestRejection) as exc:
        unpack_source(b"%PDF-1.7\n... not latex ...")
    assert exc.value.code is RejectCode.SOURCE_UNAVAILABLE


# -- licence posture ------------------------------------------------------- #


def test_cc_by_figures_are_redistributable() -> None:
    sb = build_storyboard(metadata(license=CC_BY), paper_tarball())
    assert all(f.redistributable for f in sb.figures)


def test_arxiv_default_licence_forbids_rehosting() -> None:
    """Perpetual-non-exclusive grants arXiv distribution rights, not ours.
    Getting this backwards is the licence risk in the plan's risk table."""
    sb = build_storyboard(metadata(license=ARXIV_DEFAULT), paper_tarball())
    assert not any(f.redistributable for f in sb.figures)
    assert all(f.r2_key is None for f in sb.figures)
    assert all(f.origin_url for f in sb.figures)  # a link-out always exists


def test_atom_parsing_defaults_to_the_restrictive_licence() -> None:
    meta = parse_atom(ATOM_RESPONSE, "1706.03762")
    assert meta.title == "Attention Is All You Need"
    assert meta.declared_pages == 15
    assert not meta.redistributable


# -- the rejection gate ---------------------------------------------------- #


def test_overlong_paper_is_rejected_before_download() -> None:
    with pytest.raises(IngestRejection) as exc:
        check_metadata_gate(metadata(comment="120 pages, 40 figures"))
    assert exc.value.code is RejectCode.TOO_LONG


def test_withdrawn_submission_is_rejected() -> None:
    with pytest.raises(IngestRejection) as exc:
        check_metadata_gate(metadata(comment="This paper has been withdrawn"))
    assert exc.value.code is RejectCode.SOURCE_UNAVAILABLE


def test_non_latin_script_is_rejected() -> None:
    meta = metadata()
    meta.title = "注意力就是你所需要的一切"
    meta.abstract = "我们提出了一种新的网络架构，完全基于注意力机制，不再使用循环和卷积。"
    with pytest.raises(IngestRejection) as exc:
        check_metadata_gate(meta)
    assert exc.value.code is RejectCode.UNSUPPORTED_LANGUAGE


def test_rejections_carry_reader_facing_text() -> None:
    """Reject and explain; never silently degrade."""
    for code in RejectCode:
        failure = IngestRejection(code, "detail").as_failure()
        assert failure["user_facing"]
        assert failure["code"] == code.value

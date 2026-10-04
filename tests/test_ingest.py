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


# -- prose the reader actually sees ----------------------------------------- #

_PROSE_TEX = r"""Intro text here.
\begin{figure}[t]\includegraphics[scale=0.6]{Figures/ModalNet-21}\caption{The model.}\end{figure}
More text $x_1$ and
\begin{equation*}a=b\label{e1}\end{equation*}
% \begin{equation} an old draft \end{equation}
tail $$c=d$$ end."""


def test_figure_arguments_never_leak_into_prose() -> None:
    """Observed on the Transformer paper: `[scale=0.6]{Figures/ModalNet-21}` sat at
    the top of the Model Architecture section, as text."""
    out = strip_tex(_PROSE_TEX, eq_markers=True)
    assert "Figures/" not in out and "scale=" not in out


def test_equation_markers_line_up_with_extracted_equations() -> None:
    from arcvisual.ingest.latex import EQ_MARKER, _find_equations

    out = strip_tex(_PROSE_TEX, eq_markers=True)
    found = _find_equations(_PROSE_TEX)
    assert out.count(EQ_MARKER) == len(found) == 2
    assert [e[0] for e in found] == ["a=b", "c=d"]


def test_a_commented_out_equation_is_not_part_of_the_paper() -> None:
    from arcvisual.ingest.latex import _find_equations

    assert all("old draft" not in e[0] for e in _find_equations(_PROSE_TEX))


def test_a_derivation_step_must_be_math_not_a_sentence() -> None:
    import pytest
    from pydantic import ValidationError

    from arcvisual.templates.transform_chain import Params

    with pytest.raises(ValidationError, match="English sentence"):
        Params(steps=["Multi-head attention computes several functions", "x = y"])
    # Delimiters a model adds are removed; notation inside is untouched.
    assert Params(steps=["$a = b$", r"\[c = d\]"]).steps == ["a = b", "c = d"]


def test_references_leave_a_readable_trace_and_braces_do_not_leak() -> None:
    out = strip_tex(
        r"\paragraph{Encoder:}It maps $z_{i}$, as in \cite{a} and \citep[p.~3]{b}."
        r" See Figure~\ref{fig:x} , here."
    )
    assert out.startswith("Encoder: It maps $z_{i}$")  # math braces survive
    assert "as in [cite] and [cite]." in out
    assert "Figure [ref], here." in out
    assert "{" not in out.replace("$z_{i}$", "")


def test_a_footnote_with_nested_math_braces_is_removed_whole() -> None:
    out = strip_tex(
        r"the softmax.\footnote{Then $q \cdot k = \sum_{i=1}^{d_k} q_ik_i$ has mean $0$.}"
        r" To counteract this, we scale by $\frac{1}{\sqrt{d_k}}$."
    )
    assert out == r"the softmax. To counteract this, we scale by $\frac{1}{\sqrt{d_k}}$."


def test_reader_facing_rejections_carry_no_internal_detail() -> None:
    exc = IngestRejection(
        RejectCode.SOURCE_UNAVAILABLE, "wrapper around an embedded PDF; Phase 1 rung"
    )
    assert "Phase 1" not in exc.user_facing and "rung" not in exc.user_facing
    assert exc.as_failure()["message"].startswith("wrapper"), (
        "the detail is kept for developers"
    )


def test_author_macros_are_extracted_for_the_typesetter() -> None:
    r"""Observed on the VAE paper: `\pT`, `\bxi`, `\LB` are private shorthand, and
    without their definitions most of the display math rendered as error text."""
    from arcvisual.ingest.latex import extract_macros

    macros = extract_macros(
        [
            r"\newcommand{\pT}{p_{\boldsymbol{\theta}}}"
            r"\newcommand\bx{\mathbf{x}}"
            r"\newcommand{\LB}[1][x]{\mathcal{L}(#1)\xspace}"
            r"\def\pair#1#2{\langle #1, #2 \rangle}"
            r"\DeclareMathOperator*{\argmax}{arg\,max}"
            r"\providecommand{\bx}{WRONG}"
            r"\newcommand{\R}{\ensuremath{\mathbb{R}}}"
            "\n% " + r"\newcommand{\commented}{nope}" + "\n"
        ]
    )
    assert macros == {
        r"\pT": r"p_{\boldsymbol{\theta}}",
        r"\bx": r"\mathbf{x}",
        r"\LB": r"\mathcal{L}(#1)",
        r"\pair": r"\langle #1, #2 \rangle",
        r"\argmax": r"\operatorname*{arg\,max}",
        r"\R": r"\mathbb{R}",
    }


def test_ingest_carries_the_papers_macros() -> None:
    tex = PAPER_TEX.replace(
        r"\begin{document}", r"\newcommand{\vx}{\mathbf{x}}" + "\n" + r"\begin{document}"
    )
    sb = build_storyboard(metadata(), paper_tarball(tex))
    assert sb.paper.tex_macros.get(r"\vx") == r"\mathbf{x}"


def test_style_file_layout_macros_are_not_handed_to_the_typesetter() -> None:
    from arcvisual.ingest.latex import extract_macros

    macros = extract_macros(
        [
            r"\def\AND{\end{tabular}\hfil\linebreak[0]\hfil\begin{tabular}[t]{c}}"
            r"\newcommand{\Huge}{\@setfontsize\Huge{25}{30}}"
            r"\newcommand{\E}{\mathbb{E}}"
        ]
    )
    assert macros == {r"\E": r"\mathbb{E}"}


def test_equation_numbering_macros_expand_to_nothing() -> None:
    from arcvisual.ingest.latex import extract_macros

    numbering = r"\newcommand{\eqnr}{\addtocounter{equation}{1}\tag{\theequation}}"
    assert extract_macros([numbering]) == {r"\eqnr": "{}"}

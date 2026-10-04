"""LaTeX source -> sections, equations, figures, with round-trippable offsets.

The load-bearing property here is that **every offset resolves**. Everything
downstream is grounded by ``SourceSpan``, and a span that does not resolve is a
silent grounding failure — the worst class of bug this product can have, because
it looks like a working animation. So this module keeps the *original* text in
``Section.raw`` and only ever computes offsets into it; nothing is normalised in
place, and :func:`parse` self-checks every span it emits before returning.

The parser is deliberately shallow. It does not expand macros, resolve
``\\input`` beyond one level, or try to evaluate TeX. A paper it cannot read is a
rejection, not a guess.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from arcvisual.ingest.errors import IngestRejection, RejectCode
from arcvisual.storyboard import Equation, Figure, Section, SourceSpan, quote_hash

_SECTION_CMDS = ("section", "subsection", "subsubsection")
_SECTION_RE = re.compile(
    r"^[ \t]*\\(?P<cmd>section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{",
    re.MULTILINE,
)
_EQ_ENVS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "displaymath",
)
_FIG_ENVS = ("figure", "figure*", "wrapfigure")
_LABEL_RE = re.compile(r"\\label\{([^}]{1,120})\}")
_CAPTION_RE = re.compile(r"\\caption\s*\{")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]{1,200})\}")
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]{1,200})\}")

#: Where a display equation sat in the prose. The reader typesets the section's
#: equations at these markers, in order, instead of piling them up after the text.
EQ_MARKER = "⟦eq⟧"

# Environments whose bodies are not prose and must not reach the reader. Figures and
# tables are extracted separately; left in, `\includegraphics[scale=0.6]{path}` lost
# only its command name and its arguments showed up in the article as text.
_DROP_ENVS = (
    "figure",
    "wrapfigure",
    "table",
    "thebibliography",
    "tabular",
    "tabularx",
    "lstlisting",
    "verbatim",
    "algorithm",
    "algorithmic",
    "tikzpicture",
)


@dataclass
class ParsedDoc:
    sections: list[Section]
    equations: list[Equation]
    figures: list[Figure]
    title: str | None = None
    abstract: str = ""
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Brace matching — the only part of TeX we actually need to understand
# --------------------------------------------------------------------------- #


def match_brace(text: str, open_idx: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_idx``.

    Respects backslash escaping and ``%`` comments. Raises ``ValueError`` on an
    unbalanced document rather than returning a plausible-looking wrong answer.
    """
    if open_idx >= len(text) or text[open_idx] != "{":
        raise ValueError(
            f"expected '{{' at {open_idx}, found {text[open_idx : open_idx + 1]!r}"
        )
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced braces starting at {open_idx}")


def _brace_arg(text: str, open_idx: int) -> tuple[str, int]:
    """Return ``(contents, index_past_close)`` for the group at ``open_idx``."""
    end = match_brace(text, open_idx)
    return text[open_idx + 1 : end - 1], end


def _drop_commands(text: str, names: tuple[str, ...]) -> str:
    r"""Remove `\name{...}` (and `\href{..}{..}`'s second group), braces matched."""
    pattern = re.compile(r"\\(?:" + "|".join(names) + r")\*?\s*(?:\[[^\]]*\])?\s*\{")
    out: list[str] = []
    pos = 0
    while (m := pattern.search(text, pos)) is not None:
        out.append(text[pos : m.start()])
        try:
            end = match_brace(text, m.end() - 1)
        except ValueError:
            return "".join(out) + text[m.start() :]  # unbalanced: leave the rest
        if m.group(0).startswith("\\href") and end < len(text) and text[end] == "{":
            with contextlib.suppress(ValueError):
                end = match_brace(text, end)  # the link text goes too
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _blank_comments(text: str) -> str:
    """Comments replaced by spaces of the same length, so offsets survive."""
    return _COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


_DISPLAY_MATH_RE = re.compile(
    r"\\begin\{(?P<env>" + "|".join(re.escape(e) for e in _EQ_ENVS) + r")\}"
    r"(?P<a>.*?)\\end\{(?P=env)\}"
    r"|\$\$(?P<b>.+?)\$\$",
    re.DOTALL,
)


def strip_tex(text: str, *, keep_math: bool = False, eq_markers: bool = False) -> str:
    """Flatten TeX to readable prose. Lossy by design; never used for offsets.

    ``eq_markers`` leaves :data:`EQ_MARKER` where each display equation was, in the
    same order :func:`_find_equations` reports them, so the reader can put each
    equation back where the author placed it.
    """
    out = _COMMENT_RE.sub("", text)
    if eq_markers:

        def mark(m: re.Match[str]) -> str:
            inner = m.group("a") if m.group("a") is not None else m.group("b")
            if not _LABEL_RE.sub("", inner or "").strip():
                return " "  # _find_equations skips empty blocks too
            return f"\n\n{EQ_MARKER}\n\n"

        out = _DISPLAY_MATH_RE.sub(mark, out)
    for env in _DROP_ENVS:
        out = re.sub(
            rf"\\begin\{{{re.escape(env)}\*?\}}.*?\\end\{{{re.escape(env)}\*?\}}",
            "",
            out,
            flags=re.DOTALL,
        )
    if not keep_math:
        out = re.sub(r"\$\$(.+?)\$\$", " ", out, flags=re.DOTALL)
    # Brace-matched, because a footnote routinely holds math with its own braces:
    # a flat `\{[^}]*\}` stopped inside `\sum_{i=1}^{d_k}` and left half the
    # footnote in the prose as garbled inline math.
    out = _drop_commands(out, ("footnote", "thanks", "url", "href"))
    # Inline math is set aside before any command is stripped, and restored at the
    # end. Stripping ran over it too, so `$\frac{1}{\sqrt{d_k}}$` reached readers as
    # `${1}{{d_k}}$` — notation that typesets, and is wrong.
    maths: list[str] = []

    def stash(m: re.Match[str]) -> str:
        maths.append(m.group(0))
        return f"\x00{len(maths) - 1}\x00"

    out = re.sub(r"(?<!\\)\$[^$\n]{1,600}?(?<!\\)\$|\\\(.{1,600}?\\\)", stash, out)
    out = re.sub(r"\\label\s*\{[^}]*\}", "", out)
    # Run-in headings: authors write `\paragraph{Encoder:}The encoder...`.
    out = re.sub(r"\\(?:sub)?paragraph\*?\s*\{([^}]*)\}\s*", r"\1 ", out)
    # Keep a visible trace of a reference. Deleting it outright left sentences like
    # "models such as and ." and "shown in Figure , respectively".
    out = re.sub(r"\\(?:eqref|[cC]?ref|autoref)\s*\{[^}]*\}", "[ref]", out)
    out = re.sub(r"\\(?:cite[a-zA-Z]*)\s*(?:\[[^\]]*\])?\s*\{[^}]*\}", "[cite]", out)
    for cmd in ("emph", "textbf", "textit", "texttt", "textsc", "mbox", "text"):
        out = re.sub(rf"\\{cmd}\s*\{{([^{{}}]*)\}}", r"\1", out)
    out = re.sub(r"\\begin\{[^}]*\}(?:\[[^\]]*\])?", "", out)
    out = re.sub(r"\\end\{[^}]*\}", "", out)
    out = re.sub(r"\\[a-zA-Z@]+\*?", "", out)
    out = out.replace("~", " ").replace("\\\\", "\n")
    out = out.replace("``", "“").replace("''", "”")  # TeX quotes
    # Braces left behind by commands just removed (`\paragraph{Encoder:}` became
    # `{Encoder:}`). Inline math is still set aside here, so its braces are safe.
    out = out.replace("{", "").replace("}", "")
    out = re.sub(r"[ \t]+([.,;:])", r"\1", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: maths[int(m.group(1))], out)
    return out.strip()


# --------------------------------------------------------------------------- #
# Source assembly
# --------------------------------------------------------------------------- #


def find_main_tex(files: dict[str, str]) -> str:
    """Pick the main file from an e-print tarball's contents.

    arXiv tarballs routinely hold a dozen ``.tex`` files with no manifest. The
    only reliable signal is ``\\begin{document}``; among several, the longest
    wins, which in practice is the paper and not a style file.
    """
    candidates = [
        (name, body)
        for name, body in files.items()
        if name.lower().endswith(".tex") and "\\begin{document}" in body
    ]
    if not candidates:
        tex = [n for n in files if n.lower().endswith(".tex")]
        raise IngestRejection(
            RejectCode.SOURCE_UNAVAILABLE,
            f"no .tex file contains \\begin{{document}} (saw {len(tex)} .tex files)",
        )
    main = max(candidates, key=lambda kv: len(kv[1]))[0]
    _reject_pdf_wrapper(main, files[main])
    return main


#: A submission whose only content is an embedded PDF. Common on arXiv, and the
#: reason must say so: blaming the section parser sends a developer hunting a bug
#: that is not there.
_PDF_WRAPPER_RE = re.compile(
    r"\\(?:includepdf|includegraphics)\s*(?:\[[^\]]*\])?\s*\{[^}]*\.pdf\}", re.I
)


def _reject_pdf_wrapper(name: str, tex: str) -> None:
    """Detect a PDF-only submission dressed as LaTeX.

    Some authors upload a compiled PDF wrapped in a three-line shim built on
    ``pdfpages``. There is no LaTeX body to parse and never will be, so this is a
    clean rejection rather than a parse failure — and the accurate reason points
    at the resolution ladder's PDF rung (Phase 3, Docling) instead of at us.
    """
    body = document_body(tex)
    if len(body.strip()) > 2_000 or not _PDF_WRAPPER_RE.search(body):
        return
    raise IngestRejection(
        RejectCode.SOURCE_UNAVAILABLE,
        f"{name} is a wrapper around an embedded PDF ({len(body.strip())} characters "
        "of LaTeX, no prose); this is a PDF-only submission and needs the PDF rung "
        "of the resolution ladder, which Phase 1 does not implement",
    )


def inline_inputs(main: str, files: dict[str, str], depth: int = 1) -> str:
    """Splice one level of ``\\input``/``\\include``. Deeper nesting is rare and
    each level multiplies the chance of a silently wrong offset."""
    if depth <= 0:
        return main

    def repl(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        for cand in (target, f"{target}.tex", target.lstrip("./")):
            for name, body in files.items():
                if name == cand or name.endswith(f"/{cand}"):
                    return "\n" + inline_inputs(body, files, depth - 1) + "\n"
        return ""  # a missing include is dropped, not guessed at

    return _INPUT_RE.sub(repl, main)


# --------------------------------------------------------------------------- #
# Author macros
# --------------------------------------------------------------------------- #

_MACRO_DEF = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|"
    r"DeclareMathOperator|def)(\*?)"
)
#: Commands a browser typesetter does not know and that carry no meaning in math.
_NOISE = re.compile(r"\\(?:xspace|relax|protect|nobreak|ignorespaces)\b\s*")
_NUMBERING = re.compile(
    r"\\(?:addtocounter|setcounter|stepcounter|refstepcounter|theequation|tag|"
    r"nonumber|notag|eqno|leqno)\b"
)
#: Bodies that are page layout or TeX internals, not notation. Conference style
#: files define dozens (`\AND`, `\Huge`, `\maketitle`); handed to a math
#: typesetter they can only break equations that happen to share a name.
_LAYOUT = re.compile(
    r"@|\\(?:setlength|addtolength|[vh]skip|[vh]space|par\b|begin|end|fontsize|"
    r"selectfont|linebreak|newline|[vh]fil|[vh]box|kern|penalty|pagestyle|"
    r"thispagestyle|footnote|section|caption|label|ref\b|cite|item\b|input|include|"
    r"usepackage|let\b|def\b|newcommand|renewcommand|global|expandafter|csname)"
)


def _strip_wrapper(body: str, command: str) -> str:
    r"""`\ensuremath{X}` -> `X`, braces matched."""
    token = f"\\{command}"
    while (at := body.find(token)) != -1:
        brace = body.find("{", at)
        if brace == -1 or body[at + len(token) : brace].strip():
            break
        try:
            end = match_brace(body, brace)
        except ValueError:
            break
        body = body[:at] + body[brace + 1 : end - 1] + body[end:]
    return body


def extract_macros(sources: list[str], *, limit: int = 400) -> dict[str, str]:
    r"""The paper's own macro definitions, as ``{"\\name": "expansion"}``.

    Papers lean on private shorthand — `\pT`, `\bx`, `\LB` — defined in the
    preamble or a local style file. Without the definitions a browser typesetter
    shows every equation that uses one as red error text; observed on the VAE
    paper, where most of the display math was unreadable. Optional-argument
    defaults are dropped (no browser typesetter supports them); a definition that
    is too long, or not a plain control word, is skipped rather than guessed at.
    """
    macros: dict[str, str] = {}
    for raw in sources:
        src = _blank_comments(raw)
        pos = 0
        while (m := _MACRO_DEF.search(src, pos)) is not None:
            kind, star = m.group(1), m.group(2)
            i = m.end()
            pos = i
            while i < len(src) and src[i] in " \t\n":
                i += 1
            # The macro's name: `{\name}` or a bare `\name`.
            if src.startswith("{", i):
                try:
                    end = match_brace(src, i)
                except ValueError:
                    continue
                name = src[i + 1 : end - 1].strip()
                i = end
            else:
                nm = re.match(r"\\[A-Za-z@]+", src[i:])
                if nm is None:
                    continue
                name = nm.group(0)
                i += len(name)
            if not re.fullmatch(r"\\[A-Za-z]+", name):
                continue
            if kind == "def":
                params = re.match(r"(?:#\d)*\s*", src[i:])
                i += params.end() if params else 0
            elif kind != "DeclareMathOperator":
                nargs = re.match(r"\s*\[\d\]", src[i:])
                i += nargs.end() if nargs else 0
                default = re.match(r"\s*\[[^\]]*\]", src[i:])
                i += default.end() if default else 0
            while i < len(src) and src[i] in " \t\n":
                i += 1
            if not src.startswith("{", i):
                continue
            try:
                end = match_brace(src, i)
            except ValueError:
                continue
            body = src[i + 1 : end - 1].strip()
            pos = end
            if kind == "DeclareMathOperator":
                body = f"\\operatorname{star}{{{body}}}"
            body = _NOISE.sub("", _strip_wrapper(body, "ensuremath")).strip()
            if _NUMBERING.search(body):
                # Equation-numbering shorthand (the VAE paper's `\eqnr`). Kept, but
                # expanding to nothing: dropped, every equation using it would end
                # in a red "undefined" error; expanded, it typesets counter code.
                body = "{}"
            if not body or len(body) > 600 or _LAYOUT.search(body):
                continue
            if kind == "providecommand" and name in macros:
                continue
            macros[name] = body
            if len(macros) >= limit:
                return macros
    return macros


def macro_sources(tex: str, files: dict[str, str]) -> list[str]:
    """Where authors define macros: the preamble, then any local style files."""
    start = tex.find("\\begin{document}")
    preamble = tex[:start] if start != -1 else tex[:20000]
    styles = [body for name, body in files.items() if name.lower().endswith(".sty")]
    return [*styles, preamble]


def document_body(tex: str) -> str:
    """Everything between ``\\begin{document}`` and ``\\end{document}``."""
    start = tex.find("\\begin{document}")
    if start == -1:
        return tex
    start += len("\\begin{document}")
    end = tex.find("\\end{document}", start)
    return tex[start:end] if end != -1 else tex[start:]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse(tex: str, *, paper_slug: str = "paper") -> ParsedDoc:
    """LaTeX source -> a :class:`ParsedDoc` whose every span resolves.

    Uses the document's own ``\\section`` hierarchy. Never splits on length or
    on blank lines — a naive split destroys the offsets that ground every
    downstream claim.
    """
    title = _extract_braced(tex, r"\\title\s*\{")
    abstract = _extract_env(tex, "abstract")

    body = document_body(tex)
    boundaries = _section_boundaries(body)
    if not boundaries:
        raise IngestRejection(
            RejectCode.TOO_FEW_SECTIONS,
            "no \\section commands found in the document body",
        )

    sections: list[Section] = []
    equations: list[Equation] = []
    figures: list[Figure] = []
    warnings: list[str] = []
    heading_stack: list[str] = []
    fig_counter = 0

    for idx, (cmd, heading, content_start, content_end) in enumerate(boundaries):
        depth = _SECTION_CMDS.index(cmd) + 1
        heading_stack = heading_stack[: depth - 1]
        heading_stack.append(heading)

        raw = body[content_start:content_end]
        sec_id = f"s{idx:03d}"
        section = Section(
            id=sec_id,
            heading_path=list(heading_stack),
            raw=raw,
            prose_md=strip_tex(raw, eq_markers=True),
            char_offset=content_start,
        )

        for eq_idx, (eq_latex, eq_label, display, lo, hi) in enumerate(
            _find_equations(raw)
        ):
            eq = Equation(
                id=f"{sec_id}-eq{eq_idx:02d}",
                latex=eq_latex,
                label=eq_label,
                display=display,
                span=_span(sec_id, raw, lo, hi),
            )
            equations.append(eq)
            section.equation_ids.append(eq.id)

        for cap, label, path in _find_figures(raw):
            fig = Figure(
                id=f"fig{fig_counter:02d}",
                caption=cap,
                label=label,
                r2_key=None,  # set later, only if the licence allows rehosting
                origin_url=path,
                redistributable=False,  # ingest decides; default is the safe one
            )
            figures.append(fig)
            section.figure_ids.append(fig.id)
            fig_counter += 1

        sections.append(section)

    # Self-check: every emitted span must resolve against the section it cites.
    by_id = {s.id: s for s in sections}
    for eq in equations:
        eq.span.resolve(by_id[eq.span.section_id])

    if len(sections) < 2:
        raise IngestRejection(
            RejectCode.TOO_FEW_SECTIONS, f"parsed only {len(sections)} section(s)"
        )
    if not any(len(s.prose_md) > 200 for s in sections):
        warnings.append("no section has substantial prose; parse may be shallow")

    return ParsedDoc(
        sections=sections,
        equations=equations,
        figures=figures,
        title=title,
        abstract=strip_tex(abstract) if abstract else "",
        warnings=warnings,
    )


def _span(section_id: str, raw: str, lo: int, hi: int) -> SourceSpan:
    return SourceSpan(
        section_id=section_id,
        start=lo,
        end=hi,
        quote_sha256=quote_hash(raw[lo:hi]),
    )


def span_for_text(section: Section, needle: str) -> SourceSpan | None:
    """Ground an arbitrary quote back into a section, or return None.

    Analyze uses this to convert a model-quoted sentence into a real span. It
    returns None rather than approximating: an unground-able claim is dropped,
    which is the rule that keeps a beautiful animation from illustrating
    something the paper never said.
    """
    needle = needle.strip()
    if not needle:
        return None
    idx = section.raw.find(needle)
    if idx == -1:
        # One retry with whitespace collapsed, since models reflow quotes.
        flat = re.sub(r"\s+", " ", needle)
        collapsed = re.sub(r"\s+", " ", section.raw)
        j = collapsed.find(flat)
        if j == -1:
            return None
        idx = _map_collapsed_offset(section.raw, j)
        if idx is None:
            return None
        end = idx + _consume_len(section.raw, idx, len(flat))
        return _span(section.id, section.raw, idx, min(end, len(section.raw)))
    return _span(section.id, section.raw, idx, idx + len(needle))


def _map_collapsed_offset(raw: str, collapsed_idx: int) -> int | None:
    """Translate an offset in whitespace-collapsed text back into ``raw``."""
    seen = 0
    prev_ws = False
    for i, ch in enumerate(raw):
        is_ws = ch.isspace()
        if is_ws and prev_ws:
            continue
        if seen == collapsed_idx:
            return i
        seen += 1
        prev_ws = is_ws
    return None


def _consume_len(raw: str, start: int, collapsed_len: int) -> int:
    """How many raw chars correspond to ``collapsed_len`` collapsed chars."""
    seen = 0
    prev_ws = False
    i = start
    while i < len(raw) and seen < collapsed_len:
        ch = raw[i]
        is_ws = ch.isspace()
        if not (is_ws and prev_ws):
            seen += 1
        prev_ws = is_ws
        i += 1
    return i - start


def _section_boundaries(body: str) -> list[tuple[str, str, int, int]]:
    """``[(cmd, heading, content_start, content_end)]`` in document order."""
    found: list[tuple[str, str, int, int]] = []
    marks: list[tuple[str, str, int]] = []
    for m in _SECTION_RE.finditer(body):
        brace = body.index("{", m.end() - 1)
        try:
            heading_raw, after = _brace_arg(body, brace)
        except ValueError:
            continue  # malformed heading: skip rather than mis-split the paper
        heading = strip_tex(heading_raw).strip() or "Untitled"
        marks.append((m.group("cmd"), heading, after))
    for i, (cmd, heading, start) in enumerate(marks):
        end = len(body)
        if i + 1 < len(marks):
            # Content ends where the next heading's command begins.
            nxt_cmd_start = body.rfind(f"\\{marks[i + 1][0]}", start, marks[i + 1][2])
            end = nxt_cmd_start if nxt_cmd_start != -1 else marks[i + 1][2]
        found.append((cmd, heading, start, end))
    return found


def _find_equations(raw: str) -> list[tuple[str, str | None, bool, int, int]]:
    """Display-math blocks as ``(latex, label, display, lo, hi)`` offsets in raw."""
    out: list[tuple[str, str | None, bool, int, int]] = []
    # Search a copy with comments blanked to the same length: offsets stay valid in
    # `raw`, but a commented-out equation — authors keep old versions around — is no
    # longer extracted and shown to readers as if it were part of the paper.
    raw = _blank_comments(raw)
    for env in _EQ_ENVS:
        pattern = re.compile(
            rf"\\begin\{{{re.escape(env)}\}}(.*?)\\end\{{{re.escape(env)}\}}",
            re.DOTALL,
        )
        for m in pattern.finditer(raw):
            inner = m.group(1)
            label_m = _LABEL_RE.search(inner)
            latex = _LABEL_RE.sub("", inner).strip()
            if not latex:
                continue
            out.append(
                (latex, label_m.group(1) if label_m else None, True, m.start(), m.end())
            )
    for m in re.finditer(r"\$\$(.+?)\$\$", raw, re.DOTALL):
        latex = m.group(1).strip()
        if latex:
            out.append((latex, None, True, m.start(), m.end()))
    out.sort(key=lambda t: t[3])
    return out


def _find_figures(raw: str) -> list[tuple[str, str | None, str]]:
    """``(caption, label, graphics_path)`` for each figure environment."""
    out: list[tuple[str, str | None, str]] = []
    for env in _FIG_ENVS:
        pattern = re.compile(
            rf"\\begin\{{{re.escape(env)}\}}(.*?)\\end\{{{re.escape(env)}\}}",
            re.DOTALL,
        )
        for m in pattern.finditer(raw):
            inner = m.group(1)
            cap_m = _CAPTION_RE.search(inner)
            caption = ""
            if cap_m:
                try:
                    caption = strip_tex(
                        _brace_arg(inner, inner.index("{", cap_m.end() - 1))[0]
                    )
                except ValueError:
                    caption = ""
            label_m = _LABEL_RE.search(inner)
            gfx = _GRAPHICS_RE.search(inner)
            out.append(
                (
                    caption,
                    label_m.group(1) if label_m else None,
                    gfx.group(1) if gfx else "",
                )
            )
    return out


def _extract_braced(tex: str, cmd_pattern: str) -> str | None:
    m = re.search(cmd_pattern, tex)
    if not m:
        return None
    try:
        brace = tex.index("{", m.end() - 1)
        return strip_tex(_brace_arg(tex, brace)[0]).strip() or None
    except (ValueError, IndexError):
        return None


def _extract_env(tex: str, env: str) -> str:
    m = re.search(
        rf"\\begin\{{{re.escape(env)}\}}(.*?)\\end\{{{re.escape(env)}\}}",
        tex,
        re.DOTALL,
    )
    return m.group(1) if m else ""

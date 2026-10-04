"""Stage 1 — Ingest. arXiv URL -> a validated Storyboard skeleton.

The resolution ladder, stopping at the first success:

1. arXiv API metadata + the **LaTeX e-print tarball**. Real ``\\section``
   commands, real equations, a ``\\label``/``\\ref`` graph. This is the only rung
   Phase 1 implements, and it is the reason Phase 1 is arXiv-only: everything
   downstream is grounded by character offsets, and LaTeX is the only source
   where those offsets mean something exact.
2. arXiv native HTML / ar5iv (Phase 3).
3. Any PDF via Docling (Phase 3).
4. DOI or title via OpenAlex -> Unpaywall (Phase 3).

The rejection gate runs *before* the tarball download wherever the metadata is
enough to decide, so a paper we will not process costs one HTTP round trip.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from arcvisual.config import settings
from arcvisual.ingest.errors import IngestRejection, RejectCode
from arcvisual.ingest.latex import (
    ParsedDoc,
    document_body,
    find_main_tex,
    inline_inputs,
    parse,
)
from arcvisual.storyboard import PaperMeta, Storyboard

_ARXIV_API = "https://export.arxiv.org/api/query"
_EPRINT = "https://arxiv.org/e-print/{arxiv_id}"
_ABS = "https://arxiv.org/abs/{arxiv_id}"
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

_ID_PATTERNS = (
    re.compile(r"arxiv\.org/(?:abs|pdf|html)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.I),
    re.compile(r"arxiv\.org/abs/(?P<id>[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I),
    re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?:v\d+)?$"),
    re.compile(r"^arxiv:(?P<id>\d{4}\.\d{4,5})(?:v\d+)?$", re.I),
)
_PAGES_RE = re.compile(r"(\d{1,3})\s*pages", re.I)


@dataclass
class ArxivMetadata:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    license: str
    published: str
    doi: str | None = None
    comment: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def declared_pages(self) -> int | None:
        """arXiv has no page-count field; the comment usually says "N pages"."""
        m = _PAGES_RE.search(self.comment or "")
        return int(m.group(1)) if m else None

    @property
    def redistributable(self) -> bool:
        """Whether the paper's *figures* may be rehosted.

        Perpetual-non-exclusive (arXiv's default) grants arXiv distribution
        rights, not ours. Those papers get deep-linked figures instead. Getting
        this backwards is the licence risk in the plan's risk table.
        """
        return self.license.rstrip("/") in tuple(
            lic.rstrip("/") for lic in settings().ingest.redistributable_licenses
        )


# --------------------------------------------------------------------------- #
# URL resolution
# --------------------------------------------------------------------------- #


def extract_arxiv_id(url: str) -> str:
    text = url.strip()
    for pattern in _ID_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group("id")
    raise IngestRejection(
        RejectCode.UNRESOLVABLE_URL,
        f"{url!r} is not an arXiv URL or id; Phase 1 is arXiv-only",
    )


def slugify(title: str, arxiv_id: str) -> str:
    """Readable permalink slug, disambiguated by the arXiv id.

    The id suffix is not decoration: two papers can share a title, and
    ``papers.slug`` is UNIQUE.
    """
    norm = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", norm.lower())
    stem = "-".join(words[:8]) or "paper"
    return f"{stem}-{arxiv_id.replace('/', '-')}"


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


def fetch_metadata(arxiv_id: str, *, client: Any = None) -> ArxivMetadata:
    import httpx

    owns = client is None
    client = client or httpx.Client(timeout=20.0, follow_redirects=True)
    try:
        resp = client.get(_ARXIV_API, params={"id_list": arxiv_id, "max_results": 1})
        resp.raise_for_status()
        return parse_atom(resp.text, arxiv_id)
    except Exception as exc:
        if isinstance(exc, IngestRejection):
            raise
        raise IngestRejection(
            RejectCode.SOURCE_UNAVAILABLE, f"arXiv API error: {type(exc).__name__}"
        ) from exc
    finally:
        if owns:
            client.close()


def parse_atom(xml_text: str, arxiv_id: str) -> ArxivMetadata:
    """Parse one arXiv API Atom entry. Kept separate so it is testable offline."""
    root = ET.fromstring(xml_text)
    entry = root.find("a:entry", _ATOM)
    if entry is None or entry.findtext("a:title", "", _ATOM).strip() == "Error":
        raise IngestRejection(
            RejectCode.UNRESOLVABLE_URL, f"arXiv has no record for {arxiv_id}"
        )

    def text(path: str, default: str = "") -> str:
        return (entry.findtext(path, default, _ATOM) or default).strip()

    licence = ""
    for child in entry:
        if child.tag.endswith("license") and child.text:
            licence = child.text.strip()
    if not licence:
        licence = "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"

    return ArxivMetadata(
        arxiv_id=arxiv_id,
        title=re.sub(r"\s+", " ", text("a:title", "Untitled")),
        authors=[
            (a.findtext("a:name", "", _ATOM) or "").strip()
            for a in entry.findall("a:author", _ATOM)
        ],
        abstract=re.sub(r"\s+", " ", text("a:summary")),
        license=licence,
        published=text("a:published"),
        doi=text("arxiv:doi") or None,
        comment=text("arxiv:comment"),
        categories=[c.attrib.get("term", "") for c in entry.findall("a:category", _ATOM)],
    )


def fetch_source(arxiv_id: str, *, client: Any = None) -> bytes:
    import httpx

    owns = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        resp = client.get(
            _EPRINT.format(arxiv_id=arxiv_id),
            headers={"User-Agent": "ArcVisual/0.1 (+https://arcvisual.app)"},
        )
        resp.raise_for_status()
        blob = resp.content
    except Exception as exc:
        raise IngestRejection(
            RejectCode.SOURCE_UNAVAILABLE,
            f"e-print fetch failed: {type(exc).__name__}",
        ) from exc
    finally:
        if owns:
            client.close()

    limit = settings().ingest.max_source_bytes
    if len(blob) > limit:
        raise IngestRejection(
            RejectCode.TOO_LONG, f"source is {len(blob) // 1_000_000}MB"
        )
    return blob


def unpack_source(blob: bytes) -> dict[str, str]:
    """Tarball / gzip / bare-tex bytes -> ``{filename: text}``.

    arXiv e-prints arrive in whatever shape the author uploaded, so all three
    cases are real and all three appear in the eval set.
    """
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.size > 8_000_000:
                    continue
                if not member.name.lower().endswith((".tex", ".bbl", ".sty", ".cls")):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                files[member.name] = fh.read().decode("utf-8", errors="replace")
        if files:
            return files
    except tarfile.TarError:
        pass

    for decoder in (lambda b: gzip.decompress(b), lambda b: b):
        try:
            text = decoder(blob).decode("utf-8", errors="replace")
        except (OSError, gzip.BadGzipFile, UnicodeDecodeError):
            continue
        if "\\documentclass" in text or "\\begin{document}" in text:
            return {"main.tex": text}

    raise IngestRejection(
        RejectCode.SOURCE_UNAVAILABLE,
        "e-print is neither a tarball nor LaTeX (likely a PDF-only submission)",
    )


# --------------------------------------------------------------------------- #
# The rejection gate
# --------------------------------------------------------------------------- #


def check_metadata_gate(meta: ArxivMetadata) -> None:
    """Everything decidable before spending a download. Runs in ~1s."""
    cfg = settings().ingest

    pages = meta.declared_pages
    if pages is not None and pages > cfg.max_pages:
        raise IngestRejection(RejectCode.TOO_LONG, f"author declares {pages} pages")

    if "withdrawn" in (meta.comment or "").lower():
        raise IngestRejection(
            RejectCode.SOURCE_UNAVAILABLE, "this submission has been withdrawn"
        )

    if not _looks_english(f"{meta.title} {meta.abstract}"):
        raise IngestRejection(
            RejectCode.UNSUPPORTED_LANGUAGE, "title and abstract are not English"
        )


def check_source_gate(parsed: ParsedDoc, tex: str) -> None:
    """Structural checks that need the parse. Still well inside the 90s budget."""
    body = document_body(tex)
    if len(body) < 4_000:
        raise IngestRejection(
            RejectCode.NO_MACHINE_TEXT,
            f"document body is only {len(body)} characters",
        )
    prose = sum(len(s.prose_md) for s in parsed.sections)
    if prose < 2_000:
        raise IngestRejection(
            RejectCode.PARSE_FAILED,
            f"extracted only {prose} characters of prose from "
            f"{len(parsed.sections)} sections",
        )


def _looks_english(text: str) -> bool:
    """Cheap script check. Real language ID is a Phase 3 concern; this catches
    the actual v1 case, which is a paper written in a non-Latin script."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 40:
        return True  # not enough signal to reject on
    latin = sum(1 for c in letters if ord(c) < 0x250)
    return latin / len(letters) > 0.85


# --------------------------------------------------------------------------- #
# The stage
# --------------------------------------------------------------------------- #


def ingest_url(url: str, *, client: Any = None) -> Storyboard:
    """The Ingest stage. Raises :class:`IngestRejection` with a reader-facing
    reason rather than degrading silently."""
    arxiv_id = extract_arxiv_id(url)
    meta = fetch_metadata(arxiv_id, client=client)
    check_metadata_gate(meta)
    blob = fetch_source(arxiv_id, client=client)
    return build_storyboard(meta, blob)


def build_storyboard(meta: ArxivMetadata, source_blob: bytes) -> Storyboard:
    """Offline half of ingest: bytes + metadata -> Storyboard. Directly testable."""
    files = unpack_source(source_blob)
    main = find_main_tex(files)
    tex = inline_inputs(files[main], files)

    try:
        parsed = parse(tex, paper_slug=slugify(meta.title, meta.arxiv_id))
    except IngestRejection:
        raise
    except Exception as exc:
        raise IngestRejection(
            RejectCode.PARSE_FAILED, f"{type(exc).__name__}: {exc}"
        ) from exc

    check_source_gate(parsed, tex)

    # The licence decision, applied once, here. Non-permissive papers get their
    # figures deep-linked instead of rehosted.
    redistributable = meta.redistributable
    for fig in parsed.figures:
        fig.redistributable = redistributable
        fig.origin_url = fig.origin_url or _ABS.format(arxiv_id=meta.arxiv_id)

    return Storyboard(
        paper=PaperMeta(
            arxiv_id=meta.arxiv_id,
            doi=meta.doi,
            slug=slugify(meta.title, meta.arxiv_id),
            title=meta.title,
            authors=meta.authors,
            abstract=meta.abstract or parsed.abstract,
            license=meta.license,
            source_sha256=hashlib.sha256(source_blob).hexdigest(),
            published=meta.published,
            origin_url=_ABS.format(arxiv_id=meta.arxiv_id),
            categories=[c for c in meta.categories if c][:6],
        ),
        sections=parsed.sections,
        equations=parsed.equations,
        figures=parsed.figures,
    )

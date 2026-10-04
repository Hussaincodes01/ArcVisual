"""Ingest rejections — the "fail loud at second 5, not minute 12" contract.

Every rejection carries text meant for a reader, not a stack trace. The point of
this module is that ArcVisual never silently degrades an unreadable paper into a
thin article; it says what it could not do and links out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RejectCode(str, Enum):
    NOT_OPEN_ACCESS = "not_open_access"
    NO_MACHINE_TEXT = "no_machine_text"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    TOO_LONG = "too_long"
    LICENSE_FORBIDS = "license_forbids"
    UNRESOLVABLE_URL = "unresolvable_url"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PARSE_FAILED = "parse_failed"
    TOO_FEW_SECTIONS = "too_few_sections"


_MESSAGES: dict[RejectCode, str] = {
    RejectCode.NOT_OPEN_ACCESS: (
        "This paper is behind a paywall. ArcVisual only reads open-access papers."
    ),
    RejectCode.NO_MACHINE_TEXT: (
        "This PDF looks scanned — there is no machine-readable text to work from."
    ),
    RejectCode.UNSUPPORTED_LANGUAGE: (
        "ArcVisual currently only handles English-language papers."
    ),
    RejectCode.TOO_LONG: (
        "This paper is longer than ArcVisual handles well. Try a paper under "
        "60 pages, or a specific chapter."
    ),
    RejectCode.LICENSE_FORBIDS: (
        "This paper's licence does not permit us to build a derivative article "
        "from it. You can still read it at the original link."
    ),
    RejectCode.UNRESOLVABLE_URL: (
        "We could not work out which paper that URL points to."
    ),
    RejectCode.SOURCE_UNAVAILABLE: (
        "We could not get this paper's LaTeX source from arXiv. Some papers are "
        "posted only as a PDF, which ArcVisual cannot read yet; if this one does "
        "have source, arXiv may be busy, so try again in a minute."
    ),
    RejectCode.PARSE_FAILED: (
        "We could not read this paper's structure well enough to explain it."
    ),
    RejectCode.TOO_FEW_SECTIONS: (
        "We could only find one section in this paper, which usually means the "
        "structure did not parse. Rather than guess, we stopped."
    ),
}


@dataclass
class IngestRejection(Exception):
    """A paper ArcVisual will not process, with a reason a human can act on."""

    code: RejectCode
    detail: str = ""

    @property
    def user_facing(self) -> str:
        """Plain language only. `detail` is for developers and goes in `message`:
        appended here it showed readers lines like "needs the PDF rung of the
        resolution ladder, which Phase 1 does not implement"."""
        return _MESSAGES[self.code]

    def as_failure(self) -> dict[str, str]:
        """Shape stored in ``jobs.failure`` and returned by the API."""
        return {
            "code": self.code.value,
            "message": self.detail or self.code.value,
            "user_facing": self.user_facing,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code.value}: {self.detail or self.user_facing}"

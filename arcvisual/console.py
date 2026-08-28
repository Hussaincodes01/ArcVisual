"""Console output that survives a Windows terminal.

Python picks the console's own codepage for ``sys.stdout``, which on a default
Windows install is **cp1252** — an encoding with no box-drawing characters, no
em-dash and no arrows. Printing one raises ``UnicodeEncodeError`` rather than
degrading, so a decorative banner does not merely look wrong, it takes the process
down:

    print("\\u2500" * 70)
    UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-69

That is exactly how ``python -m arcvisual.render.serve`` failed on Windows: the
server never bound its port, because it died drawing a horizontal rule two lines
before ``uvicorn.run``. The eval harness hit the softer version of the same thing,
printing ``ArcVisual eval ? pipeline`` where an em-dash belonged.

The fix belongs at the entry points rather than at each ``print``: switch the stream
to UTF-8 once, and fall back to replacing unencodable characters if even that is
refused. A banner is never worth a crash.
"""

from __future__ import annotations

import contextlib
import sys
from typing import IO, Any


def enable_unicode_output() -> bool:
    """Make ``stdout``/``stderr`` accept non-ASCII. Returns True if reconfigured.

    Call once, early, from anything that prints for a human. Safe to call repeatedly
    and safe when the streams are redirected to a file or pipe.
    """
    changed = False
    for stream in (sys.stdout, sys.stderr):
        changed |= _make_utf8(stream)
    return changed


def _make_utf8(stream: IO[Any] | None) -> bool:
    if stream is None:
        return False
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # a StringIO or similar under test: nothing to do
        return False
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if encoding in ("utf8", "utf8mb4"):
        # Already fine, but still ask for replacement rather than an exception: a
        # surrogate from a filename or a model's reply should not kill the process.
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            return False
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
        return True
    except (OSError, ValueError, LookupError):  # pragma: no cover
        # Some consoles refuse. Degrade to mangled characters, never to a crash.
        with contextlib.suppress(OSError, ValueError):
            reconfigure(errors="replace")
        return False

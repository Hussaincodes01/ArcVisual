"""The control plane as a single Vercel Python function.

Same :func:`arcvisual.render.api.build_api` as the Modal and local deployments,
configured for a platform with no long-lived workers and no render containers:

* **Stepped execution.** No worker is spawned. Jobs advance through bounded
  ``POST /api/jobs/{id}/advance`` calls (see :mod:`arcvisual.render.stepper`).
* **Client rendering.** Scenes ship as validated template parameters that the
  reader animates in the browser. No Manim, no ffmpeg, no object storage — which is
  what makes the whole product fit on serverless functions and a free Postgres.

Both are defaults, not hard-wiring: the environment can still override them.

Exposed to Vercel by ``api/index.py``; ``vercel.json`` rewrites every path there.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("ARCVISUAL_EXECUTION", "stepped")
os.environ.setdefault("ARCVISUAL_RENDER_MODE", "client")
# A stray .env must never shadow the platform's configured secrets in production.
os.environ.setdefault("ARCVISUAL_NO_DOTENV", "1")

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", force=True
)
log = logging.getLogger("arcvisual.vercel")

from arcvisual.render.api import build_api  # noqa: E402


def _prepare_database() -> None:
    """Migrate on the first cold start after a deploy. Never fatal at import: a
    database blip should surface as a 503 on the request, not a crashed function."""
    from arcvisual.db import repo
    from arcvisual.db.migrate import ensure_schema

    try:
        revision = ensure_schema(repo.engine())
        log.info("database schema at %s", revision)
    except Exception as exc:
        log.error("database schema check failed: %s: %s", type(exc).__name__, exc)


if os.environ.get("DATABASE_URL"):
    _prepare_database()
else:
    log.error("DATABASE_URL is not set; every database-backed route will fail")

app = build_api(None)

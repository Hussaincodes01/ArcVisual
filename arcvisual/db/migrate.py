"""Bring the database schema to head on deploy, without a separate migration step.

A serverless deployment has no release phase in which to run ``alembic upgrade``,
and a function that serves requests against a schema one migration behind fails in
ways that look like application bugs. So the first cold start after a deploy checks
the recorded revision — one indexed read — and upgrades only when it is behind.

Concurrent cold starts are expected (a deploy plus traffic), so the upgrade runs
under a Postgres advisory lock: one instance migrates, the others wait on the lock
and then find nothing left to do.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: The newest migration in ``migrations/versions``. Kept as a constant so the cold-
#: start check is a single query with no script-directory scan;
#: ``test_schema_head_matches_migrations`` keeps it honest.
HEAD = "0002"

#: Arbitrary, stable key for pg_advisory_xact_lock.
_LOCK_KEY = 72_000_402


def current_revision(engine: Engine) -> str | None:
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None  # no alembic_version table yet: a fresh database


def ensure_schema(engine: Engine) -> str:
    """Upgrade to :data:`HEAD` if needed. Returns the revision now in place."""
    if current_revision(engine) == HEAD:
        return HEAD

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
    )
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        cfg.attributes["connection"] = conn
        log.info("migrating database schema to %s", HEAD)
        command.upgrade(cfg, "head")
    return HEAD

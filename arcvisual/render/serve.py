"""``python -m arcvisual.render.serve`` — the whole control plane, on a laptop.

The API is a Modal ASGI app in production, which left no way to exercise the submit
flow locally: the reader posts to ``localhost:8000``, nothing listens, and the
browser reports "Failed to fetch". This module is the missing half — the same
:func:`arcvisual.render.api.build_api` served by uvicorn, with jobs run in threads
instead of Modal containers.

Two defaults make it work with no configuration at all:

* **SQLite when ``DATABASE_URL`` is unset.** The ORM already carries ``GUID`` and
  ``JSON`` variants for exactly this, so the same models and the same queries run
  against a file. Postgres is still what production uses and what Alembic targets.
* **The local renderer.** Not a sandbox — it cannot deny network access — so it is
  refused outright unless this is plainly a development run. Gate 2 can also be
  skipped entirely with ``--no-gate2`` when Manim is not installed.

Usage::

    python -m arcvisual.render.serve                 # :8000, SQLite, renders on
    python -m arcvisual.render.serve --no-gate2      # skip rendering, fastest loop
    python -m arcvisual.render.serve --port 9000
"""

from __future__ import annotations

import argparse
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_DB = "sqlite+pysqlite:///./arcvisual-local.db"


def build_local_app(
    *,
    database_url: str | None = None,
    run_gate2: bool = True,
    max_workers: int = 2,
):
    """The FastAPI app, wired to a local database and a thread orchestrator."""
    from sqlalchemy.orm import sessionmaker

    if database_url is None:
        database_url = os.environ.get("DATABASE_URL") or DEFAULT_DB
    os.environ["DATABASE_URL"] = database_url

    from arcvisual.config import settings

    settings.cache_clear()

    from arcvisual.db import repo
    from arcvisual.db.models import Base

    connect_args = {}
    if database_url.startswith("sqlite"):
        # Jobs run in worker threads and the request thread reads the same rows, so
        # SQLite's default single-thread guard has to come off.
        connect_args["check_same_thread"] = False

    from sqlalchemy import create_engine

    engine = create_engine(database_url, future=True, connect_args=connect_args)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # The API resolves sessions through repo.session_factory(); point it at ours so
    # request handlers and background jobs share one engine.
    repo._engine = engine

    renderer = None
    if run_gate2:
        from arcvisual.gates.g2_runtime import LocalRenderer

        renderer = LocalRenderer()

    from arcvisual.render.api import build_api
    from arcvisual.render.orchestrator import ThreadOrchestrator

    orchestrator = ThreadOrchestrator(
        max_workers=max_workers,
        session_factory=Session,
        renderer=renderer,
        run_gate2=run_gate2,
    )
    app = build_api(orchestrator)
    app.state.orchestrator = orchestrator
    app.state.session_factory = Session
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"defaults to $DATABASE_URL, else {DEFAULT_DB}",
    )
    parser.add_argument(
        "--no-gate2",
        action="store_true",
        help="skip the draft render (no Manim needed); Gate 1 still runs",
    )
    parser.add_argument("--workers", type=int, default=2, help="concurrent jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the api extra:\n"
            "  pip install 'fastapi[standard]' uvicorn"
        )
        return 1

    # The local renderer is a subprocess, not a sandbox: it cannot deny network
    # access. Fine on a laptop, never in production, so say so rather than let it
    # pass unnoticed.
    if not args.no_gate2:
        os.environ.setdefault("ARCVISUAL_RENDERER", "local")
        from arcvisual.gates.g2_runtime import ensure_ffmpeg

        ensure_ffmpeg()

    app = build_local_app(
        database_url=args.database_url,
        run_gate2=not args.no_gate2,
        max_workers=args.workers,
    )

    from arcvisual.config import settings
    from arcvisual.providers.registry import describe, get_provider

    info = describe(get_provider())
    db = os.environ.get("DATABASE_URL", DEFAULT_DB)
    print("─" * 70)
    print(f"ArcVisual control plane   http://{args.host}:{args.port}")
    print(
        f"  provider   {info['provider']}  ({'exact cost' if info['cost_attributed'] else 'cost unattributed'})"
    )
    print(f"  database   {db}")
    print(
        f"  gate 2     {'local renderer (NOT a sandbox)' if not args.no_gate2 else 'skipped'}"
    )
    print(f"  rate limit {settings().rate_limit_per_hour}/hour per client")
    print(
        f"  reader     set NEXT_PUBLIC_ARCVISUAL_API_BASE=http://{args.host}:{args.port}"
    )
    print("─" * 70)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

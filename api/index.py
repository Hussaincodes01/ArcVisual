"""Vercel entrypoint: every request is rewritten here (see vercel.json).

The app itself lives in arcvisual/render/vercel_app.py so it can be imported and
tested like any other module; this file only exposes it where Vercel looks.
"""

from arcvisual.render.vercel_app import app

__all__ = ["app"]

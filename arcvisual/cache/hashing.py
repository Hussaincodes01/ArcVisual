"""Content addressing — the L2 cache, and the difference between a $4 paper
and a $40 one.

The hash input deliberately includes the *source bytes* of the template and of
the ``ArcScene`` base class, not just ``template_id``. A name is not content:
without those two inputs, fixing a layout bug inside ``transform_chain.py``
leaves every previously rendered ``transform_chain`` scene serving the buggy
video forever, because its hash never moved.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from arcvisual.config import MANIM_VERSION
from arcvisual.storyboard import SCHEMA_VERSION, SceneSpec

_FLOAT_DP = 6


def canonical(value: Any) -> Any:
    """Normalise a value so equivalent params hash identically.

    ``{"n": 2, "color": "#FFF"}`` and ``{"color": "#FFF", "n": 2.0}`` must
    produce the same digest, or the cache silently loses much of its value.
    """
    if isinstance(value, bool):  # before int: bool is an int subclass
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        r = round(value, _FLOAT_DP)
        # 2.0 and 2 must agree, so integral floats collapse to int.
        return int(r) if r == int(r) else r
    if isinstance(value, dict):
        return {str(k): canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"))


def sha256_of(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(canonical_json(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator: prevents field-boundary collisions
    return h.hexdigest()


#: Serialises source reads. ``inspect.getsource`` walks the global ``linecache`` and
#: parses with ``ast``; two threads inside it at once corrupt the shared parser state.
_SOURCE_LOCK = threading.Lock()
_SOURCE_CACHE: dict[int, str] = {}


def source_sha(obj: Any) -> str:
    """Digest of a class or module's own source text.

    **This must hold a lock, and memoisation alone is not enough.** Called from
    several scene lanes at once, ``inspect.getsource`` raises

        SystemError: AST constructor recursion depth mismatch (before=30, after=26)

    intermittently — roughly one run in six, always passing in isolation, which is
    what made it look like flakiness rather than a bug.

    ``functools.lru_cache`` does *not* fix this. It makes the cache dict thread-safe
    but does not serialise the wrapped call: on a cold cache every thread misses and
    every thread enters ``getsource`` together, which is precisely the race. It only
    narrows the window to the first call, so a suite whose cache happens to be warm
    passes and reports the bug fixed. Hence the explicit lock.

    The cache is also a real saving: a module's source cannot change inside a running
    process, so re-reading and re-hashing it per scene was pure waste on the hot path.
    """
    key = id(obj)
    cached = _SOURCE_CACHE.get(key)  # fast path: dict reads need no lock
    if cached is not None:
        return cached
    with _SOURCE_LOCK:
        cached = _SOURCE_CACHE.get(key)  # re-check: another thread may have won
        if cached is not None:
            return cached
        try:
            src = inspect.getsource(obj)
        except (OSError, TypeError):  # pragma: no cover - interactive definitions
            src = repr(obj)
        digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
        # Keyed on id(), so keep a reference alive: a garbage-collected object could
        # otherwise have its address reused and serve another object's digest.
        _SOURCE_CACHE[key] = digest
        _SOURCE_KEEPALIVE.append(obj)
        return digest


#: Holds references for objects whose ``id()`` keys the cache above. Bounded in
#: practice: the callers are module and class objects, which live for the process.
_SOURCE_KEEPALIVE: list[Any] = []


def _source_sha_cache_clear() -> None:
    """Reset the memo. For tests that need a cold cache."""
    with _SOURCE_LOCK:
        _SOURCE_CACHE.clear()
        _SOURCE_KEEPALIVE.clear()


source_sha.cache_clear = _source_sha_cache_clear  # type: ignore[attr-defined]


@lru_cache(maxsize=64)
def _plugin_lock_sha() -> str:
    """Digest of the resolved versions of every Manim plugin we import.

    Falls back to a marker when the plugins are absent (Phase 1 dev machines),
    so hashes stay stable locally but never collide with a real render env.
    """
    from importlib.metadata import PackageNotFoundError, version

    pinned = []
    for pkg in ("manim", "manim-ml", "manim-physics", "manim-chemistry", "manim-dsa"):
        try:
            pinned.append(f"{pkg}=={version(pkg)}")
        except PackageNotFoundError:
            pinned.append(f"{pkg}==absent")
    return sha256_of(pinned)[:16]


@dataclass(frozen=True)
class RenderEnv:
    """Everything about the renderer that can change a pixel."""

    manim_version: str
    plugin_lock_sha: str
    template_source_sha: str
    arcscene_base_sha: str
    quality: str

    @classmethod
    def for_template(cls, template: Any, quality: str = "final") -> RenderEnv:
        """``template`` is the template *module*, whose source bytes are hashed."""
        from arcvisual.templates.base import ArcSceneMixin

        return cls(
            manim_version=MANIM_VERSION,
            plugin_lock_sha=_plugin_lock_sha(),
            template_source_sha=source_sha(template),
            # The mixin owns instrumentation, pacing and margins, so editing it
            # changes every rendered scene — it must be in the key.
            arcscene_base_sha=source_sha(ArcSceneMixin),
            quality=quality,
        )


def content_hash(spec: SceneSpec, env: RenderEnv) -> str:
    """The L2 cache key. Any input changing must invalidate the artifact."""
    return sha256_of(
        spec.archetype.value,
        canonical(spec.params),
        [b.model_dump() for b in spec.beats],
        env.manim_version,
        env.plugin_lock_sha,
        env.template_source_sha,  # <-- the input the plan omitted
        env.arcscene_base_sha,  # <-- and this one
        env.quality,
        SCHEMA_VERSION,
    )


def r2_keys(content_hash_: str) -> dict[str, str]:
    """Content-addressed object keys. Sharded two levels to keep listings sane."""
    a, b = content_hash_[:2], content_hash_[2:4]
    base = f"scenes/{a}/{b}/{content_hash_}"
    return {
        "mp4_key": f"{base}/scene.mp4",
        "webm_key": f"{base}/scene.webm",
        "poster_key": f"{base}/poster.png",
        "framestrip_key": f"{base}/framestrip.png",
    }

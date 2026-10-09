"""Provider selection. One env var, three backends, one honest fallback.

``ARCVISUAL_PROVIDER`` picks the backend; ``auto`` (the default) takes the first one
that is actually configured. When none is, the pipeline runs its deterministic
heuristic analyzer — which is why ``pytest`` and the eval harness work on a laptop with
no credentials at all.

The fallback is *reported*, never silent. A heuristic baseline being mistaken for a
model baseline would corrupt the one number Phase 2 is measured against.
"""

from __future__ import annotations

import logging
from typing import Any

from arcvisual.config import LONG_CONTEXT_NAMES, settings
from arcvisual.providers.base import (
    Capabilities,
    Provider,
    ProviderNotConfigured,
    Task,
)

log = logging.getLogger(__name__)

#: Every provider that can be constructed by name.
PROVIDERS = ("anthropic", "groq", "poolside", "opencode", *LONG_CONTEXT_NAMES)

#: What ``auto`` will actually pick, most capable first. Anthropic leads because it is
#: the only backend with constrained output, a prompt cache and real cost attribution.
#:
#: **opencode is deliberately excluded from auto-selection.** It is an agent CLI whose
#: billing happens against its own credentials, so ArcVisual cannot attribute cost, and
#: merely having the binary on PATH should not silently route a whole pipeline through
#: it. Choosing it is a decision, so it requires ``ARCVISUAL_PROVIDER=opencode``.
#: The long-context hosts sit second: they read the whole paper in one request,
#: which is the difference between an analysis of the paper and an analysis of
#: whichever 5,000 characters fit Groq's free-tier minute. Groq follows: it has
#: constrained decoding and is the fastest way to get a reliable typed object, but
#: its per-minute meter starves ANALYZE of context. Poolside follows because it
#: needs the prompted-JSON ladder and is far slower per call.
AUTO_PREFERENCE = ("anthropic", "longcontext", "groq", "poolside")


def _build(name: str) -> Provider:
    if name == "anthropic":
        from arcvisual.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "groq":
        from arcvisual.providers.groq_provider import GroqProvider

        return GroqProvider()
    if name == "poolside":
        from arcvisual.providers.poolside_provider import PoolsideProvider

        return PoolsideProvider()
    if name == "opencode":
        from arcvisual.providers.opencode_provider import OpencodeProvider

        return OpencodeProvider()
    if name in LONG_CONTEXT_NAMES:
        from arcvisual.providers.longcontext_provider import LongContextProvider

        cfg = settings().long_context
        if name != "longcontext" and name != cfg.preset:
            from arcvisual.config import long_context_from_env

            cfg = long_context_from_env(name)
        return LongContextProvider(cfg=cfg)
    raise ValueError(
        f"unknown provider {name!r}; available: {', '.join(PROVIDERS)} (or 'auto')"
    )


def available() -> list[str]:
    """Providers that could actually be constructed here."""
    found = []
    for name in ("anthropic", "groq", "poolside", "opencode", "longcontext"):
        try:
            _build(name)
        except (ProviderNotConfigured, ImportError):
            continue
        except Exception as exc:
            log.debug("provider %s unavailable: %s", name, exc)
            continue
        found.append(name)
    return found


def get_provider(name: str | None = None) -> Provider | None:
    """The provider to use, or None when the pipeline should run heuristically.

    Raises only when a provider was named *explicitly* and cannot be built — asking
    for Poolside and silently getting Claude would be a worse outcome than an error.
    """
    requested = (name or settings().provider or "auto").strip().lower()

    if requested in ("", "none", "off", "heuristic"):
        return None

    if requested != "auto":
        try:
            return _build(requested)
        except ProviderNotConfigured as exc:
            raise ProviderNotConfigured(
                f"provider {requested!r} was requested explicitly but is not "
                f"configured: {exc}"
            ) from exc

    for candidate in AUTO_PREFERENCE:
        try:
            provider = _build(candidate)
        except (ProviderNotConfigured, ImportError):
            continue
        if candidate != AUTO_PREFERENCE[0]:
            log.info("provider auto-selected %s", candidate)
        return provider

    log.info(
        "no provider configured; the pipeline will use its heuristic analyzer and "
        "report itself as such"
    )
    return None


def describe(provider: Provider | None) -> dict[str, Any]:
    """Provider metadata for the job report and ``GET /api/health``.

    Deliberately includes the caveats, not just the name: a reader of a cost anomaly
    needs to know whether the number is exact, and whether the paper body was cached
    or re-sent on every call.
    """
    if provider is None:
        return {
            "provider": "heuristic",
            "native_structured_output": False,
            "prompt_cache": False,
            "cost_attributed": False,
            "prompt_char_budget": None,
            "note": (
                "no model provider configured; concepts and parameters come from the "
                "deterministic heuristic analyzer. Not a model baseline."
            ),
            "models": {},
        }
    caps: Capabilities = provider.capabilities()
    return {
        "provider": caps.name,
        "native_structured_output": caps.native_structured_output,
        "prompt_cache": caps.prompt_cache,
        "cost_attributed": caps.cost_attributed,
        #: None means the whole paper is sent. A number means this backend reasons
        #: against its own output budget and would otherwise run out before
        #: answering, so Analyze trims the body to fit.
        "prompt_char_budget": caps.prompt_char_budget,
        "note": caps.note,
        "models": {task.value: provider.model_for(task) for task in Task},
    }

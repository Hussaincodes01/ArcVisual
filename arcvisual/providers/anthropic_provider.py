"""Claude, via the official Anthropic SDK. The reference provider.

The only backend with all three properties the pipeline actually wants: constrained
structured output (``messages.parse``), a prompt cache that makes re-sending the paper
body across ~20 analyze calls nearly free, and exact per-token cost attribution.

Everything else in :mod:`arcvisual.providers` is a step down from this on at least one
axis, which is worth knowing before switching a production deployment.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from arcvisual.config import settings
from arcvisual.providers.base import (
    Capabilities,
    ProviderNotConfigured,
    StructuredResult,
    Task,
    TextBlock,
    Usage,
)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

#: Per-million-token rates, input/output. Kept here rather than in config because
#: they are facts about the provider, not deployment choices.
RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}
_DEFAULT_RATE = (3.00, 15.00)

#: Cache-write multiplier over base input price. 1.25x for the 5-minute default TTL,
#: 2x for 1h. Must stay in step with the `ttl` requested in `_to_block`.
_CACHE_WRITE_TTL = "1h"
_CACHE_WRITE_MULTIPLIER = 2.0 if _CACHE_WRITE_TTL == "1h" else 1.25


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any = None) -> None:
        cfg = settings()
        if client is None and not cfg.anthropic_api_key:
            raise ProviderNotConfigured("ANTHROPIC_API_KEY is not set")
        self._client = client
        self._models = cfg.models
        #: Set to route through an Anthropic-compatible gateway (Dedalus Labs, a
        #: proxy, a self-hosted relay) instead of api.anthropic.com. The SDK, the
        #: request shape and the model ids are unchanged — only the host moves —
        #: which is why this is a base-URL setting rather than a fourth provider.
        self._base_url = cfg.anthropic_base_url
        #: Sent as a default header when set. An identity-linked key — one issued to
        #: a person rather than to a workspace — is refused outright without it.
        self._workspace_id = cfg.anthropic_workspace_id

    # -- plumbing ---------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            native_structured_output=True,
            prompt_cache=True,
            vision=True,
            cost_attributed=True,
            note=(
                "constrained structured output, 1h prompt cache, exact per-token cost"
                + (f"; routed via {self._base_url}" if self._base_url else "")
                + (f"; workspace {self._workspace_id}" if self._workspace_id else "")
            ),
        )

    def model_for(self, task: Task) -> str:
        return {
            Task.CLASSIFY: self._models.classify,
            Task.CODEGEN: self._models.codegen,
            Task.VISION: self._models.vision,
        }[task]

    def client(self) -> Any:
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._workspace_id:
                # A default header rather than a per-call one: every request needs
                # it, and threading it through each call site is how one gets missed.
                kwargs["default_headers"] = {
                    "anthropic-workspace-id": self._workspace_id
                }
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def healthcheck(self, timeout_s: float = 30.0):
        """Will this backend actually answer right now? See providers.base.probe."""
        from arcvisual.providers.base import probe

        return probe(self, timeout_s)

    # -- the call ---------------------------------------------------------- #

    def structured(
        self,
        *,
        system: list[TextBlock],
        user: str,
        output_model: type[T],
        task: Task,
        max_tokens: int = 8192,
        max_attempts: int = 2,  # accepted for protocol parity; constrained
        # decoding either returns a valid object or a refusal, so retries buy nothing
    ) -> StructuredResult:
        model = self.model_for(task)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [_to_block(b) for b in system],
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "output_format": output_model,
        }
        # NOTE: no `betas`/`fallbacks` here. Server-side refusal fallback is a beta
        # feature and belongs on `client.beta.messages.*`; passing it to the non-beta
        # `messages.parse()` makes the SDK reject every call, which would degrade
        # every scene rather than saving the occasional refused one. A refusal is
        # instead detected below and handed to repair.py as a finding, which already
        # owns a budgeted retry ladder. Re-add fallbacks only alongside a verified
        # beta parse entry point.
        response = self.client().messages.parse(**kwargs)
        usage = self._usage(response, model)
        value = getattr(response, "parsed_output", None)
        findings: list[str] = []
        if value is None:
            stop = getattr(response, "stop_reason", "?")
            # stop_details is populated only on a refusal, and is None for every other
            # stop_reason — so it must be guarded before reading.
            details = getattr(response, "stop_details", None)
            if stop == "refusal":
                category = getattr(details, "category", None)
                findings.append(
                    f"the model declined this request (category={category}). Rephrase "
                    "the claim in neutral terms, or let this scene degrade to prose."
                )
            elif stop == "max_tokens":
                findings.append(
                    "the response hit max_tokens before the object closed; ask for "
                    "fewer elements so the parameters fit"
                )
            else:
                findings.append(f"no parsed output (stop_reason={stop})")
        return StructuredResult(
            value=value,
            usage=usage,
            provider=self.name,
            model=model,
            attempts=1,
            findings=findings,
        )

    def _usage(self, response: Any, model: str) -> Usage:
        raw = getattr(response, "usage", None)
        if raw is None:
            return Usage(attributed=False)
        rate_in, rate_out = RATES.get(model, _DEFAULT_RATE)
        fresh = getattr(raw, "input_tokens", 0) or 0
        cache_read = getattr(raw, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(raw, "cache_creation_input_tokens", 0) or 0
        out = getattr(raw, "output_tokens", 0) or 0
        cost = (
            fresh * rate_in
            # A cache read is ~0.1x base. A cache WRITE depends on TTL: 1.25x for the
            # 5-minute default, 2x for 1h. `_to_block` requests ttl="1h" (one paper is
            # revisited across analyze, codegen and every repair), so the write
            # multiplier must match — billing at 1.25x understated every paper that
            # cached its body, which is all of them.
            + cache_read * rate_in * 0.10
            + cache_write * rate_in * _CACHE_WRITE_MULTIPLIER
            + out * rate_out
        ) / 1_000_000
        return Usage(
            input_tokens=fresh,
            output_tokens=out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=round(cost, 6),
            attributed=True,
        )


def _to_block(block: TextBlock) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "text", "text": block.text}
    if block.cache:
        # 1h TTL: one paper is revisited across analyze, codegen and every repair in
        # a single job, and again on a re-run after a prompt tweak. The write costs 2x
        # base rather than 1.25x — see _CACHE_WRITE_MULTIPLIER, which reads this
        # constant so the two cannot drift apart.
        out["cache_control"] = {"type": "ephemeral", "ttl": _CACHE_WRITE_TTL}
    return out

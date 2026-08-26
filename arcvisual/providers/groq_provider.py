"""Groq — the fast, free, *constrained* option.

Verified against https://console.groq.com/docs/structured-outputs :

* OpenAI-compatible at ``https://api.groq.com/openai/v1``
* ``response_format: {"type": "json_schema", "json_schema": {..., "strict": true}}``
  uses **constrained decoding** — the reply is guaranteed to match the schema
* free tier, no card; LPU hardware runs large models at hundreds of tokens/second

That combination is why this provider exists. The two problems that have cost this
pipeline the most are reliability and latency, and Groq addresses both at once:

* **Reliability.** Poolside and opencode have no constrained decoding, so ArcVisual
  prompts for JSON and validates client-side, with a retry ladder for the times that
  fails. On Groq the ladder becomes a safety net rather than the mechanism.
* **Latency.** A Poolside Analyze call on a 25-section paper measured **478 seconds**.
  Groq's throughput is roughly two orders of magnitude higher per token.

**Cost is reported as unattributed by default.** The free tier is genuinely free, but
a paid key is billed at rates that change; rather than bake in a number that quietly
goes stale, set ``GROQ_RATE_IN``/``GROQ_RATE_OUT`` from your own account to turn
attribution on. A plausible-looking wrong cost is worse than an honest gap.

Structured Outputs on Groq do not support streaming or tool use — neither of which
this pipeline needs, since every call is one request for one typed object.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from arcvisual.config import settings
from arcvisual.providers.base import (
    Capabilities,
    ProviderNotConfigured,
    Task,
    Usage,
)
from arcvisual.providers.openai_compat import OpenAICompatProvider
from arcvisual.providers.ratelimit import TokenBudget

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

#: One meter per key, shared by every provider instance in this process.
#:
#: The budget belongs to the *key*, not to an object: scene lanes each build their
#: own provider, and per-instance meters would let four lanes each believe they had
#: the whole 8,000 TPM and collectively ask for 32,000.
_METERS: dict[tuple[str, int], TokenBudget] = {}
_METERS_LOCK = threading.Lock()


def _meter_for(api_key: str, tpm: int) -> TokenBudget | None:
    if tpm <= 0:
        return None
    fingerprint = (hashlib.sha256(api_key.encode()).hexdigest()[:16], tpm)
    with _METERS_LOCK:
        meter = _METERS.get(fingerprint)
        if meter is None:
            meter = _METERS[fingerprint] = TokenBudget(tpm)
        return meter


class GroqProvider(OpenAICompatProvider):
    name = "groq"

    def __init__(self, client: object | None = None) -> None:
        cfg = settings().groq
        if client is None and not cfg.api_key:
            raise ProviderNotConfigured(
                "GROQ_API_KEY is not set (free key, no card, at console.groq.com/keys)"
            )
        self._cfg = cfg
        self._http = client

    # -- config ------------------------------------------------------------- #

    def _base_url(self) -> str:
        return self._cfg.base_url

    def _api_key(self) -> str:
        return self._cfg.api_key

    def _timeout_s(self) -> float:
        return self._cfg.timeout_s

    def _temperature(self) -> float:
        return self._cfg.temperature

    def _budget_field(self) -> str:
        """Groq accepts both spellings; ``max_completion_tokens`` is the current one
        and the only one that is unambiguous for reasoning models, where thinking is
        billed as completion."""
        return "max_completion_tokens"

    def _reasoning_effort(self, model: str = "") -> str:
        return self._cfg.reasoning_effort if self._cfg.sends_reasoning_effort(model) else ""

    def _budget(self, requested: int, model: str = "", task: str = "") -> int:
        """Clamp to this model's per-request ceiling for this task.

        Groq charges the *requested* budget against the per-minute allowance on
        arrival, so an ask above the ceiling is rejected outright with 413 however
        short the real answer would have been. Verified: 8,192 against an 8,000 TPM
        model is a 413; 6,000 succeeds and returns 589 tokens.
        """
        return max(1, min(requested, self._cfg.max_tokens_for(model, task)))

    def _token_budget(self, model: str = "") -> TokenBudget | None:
        return _meter_for(self._cfg.api_key, self._cfg.tpm_for(model))

    def supports_json_schema(self, model: str = "") -> bool:
        """Whether *this model* enforces the schema.

        Per-model because the answer differs on one key: `qwen/qwen3.6-27b` enforces
        it, `groq/compound` returns 400 "This model does not support response format
        `json_schema`". An empty model means "does the provider do this at all",
        which capabilities() asks.
        """
        if not model:
            return self._cfg.json_schema
        return self._cfg.enforces_schema(model)

    def model_for(self, task: Task) -> str:
        return self._cfg.model_for(task.value)

    def capabilities(self) -> Capabilities:
        attributed = self._cfg.rate_in is not None and self._cfg.rate_out is not None
        analyze_model = self._cfg.classify
        constrained = self.supports_json_schema(analyze_model)
        return Capabilities(
            name=self.name,
            native_structured_output=constrained,
            prompt_cache=False,
            vision=False,
            cost_attributed=attributed,
            # Sized to the ANALYZE model's own per-minute allowance, which is what
            # the paper body has to fit inside: Groq counts prompt and requested
            # output against one budget, so a prompt beyond it is a 413 rather than a
            # slow answer. ~4 chars/token, and half the allowance left for the reply.
            prompt_char_budget=self._cfg.prompt_budget_for(analyze_model),
            analysis_item_budget=self._cfg.analysis_item_budget,
            note=(
                (
                    f"{analyze_model} for analyze / {self._cfg.codegen} for codegen; "
                )
                + (
                    "analyze uses constrained decoding (strict json_schema), "
                    if constrained
                    else "analyze uses prompted JSON validated client-side "
                    "(its model has the larger token allowance but no schema "
                    "enforcement), "
                )
                + "no prompt cache so the paper body is re-sent per call. "
                + (
                    "Cost attributed from your configured rates."
                    if attributed
                    else "Cost unattributed: free tier, or set GROQ_RATE_IN/OUT."
                )
            ),
        )

    def usage_from(self, body: dict) -> Usage:
        raw = body.get("usage") or {}
        prompt_tokens = int(raw.get("prompt_tokens") or 0)
        completion_tokens = int(raw.get("completion_tokens") or 0)
        rate_in, rate_out = self._cfg.rate_in, self._cfg.rate_out
        if rate_in is None or rate_out is None:
            return Usage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cost_usd=0.0,
                attributed=False,
            )
        cost = (prompt_tokens * rate_in + completion_tokens * rate_out) / 1_000_000
        return Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cost_usd=round(cost, 6),
            attributed=True,
        )

    def list_models(self) -> list[str]:
        """``GET /models`` — useful for checking what this key can actually reach."""
        response = self._client().get("/models")
        if response.status_code >= 400:
            return []
        return [m.get("id", "") for m in (response.json().get("data") or [])]

"""Poolside, via its OpenAI-compatible API.

Verified against https://docs.poolside.ai/api/overview :

* base URL ``https://inference.poolside.ai/v1`` (self-managed deployments and
  OpenRouter are configurable overrides, which is why the URL is not hardcoded)
* ``POST /v1/chat/completions``, ``GET /v1/models``
* ``Authorization: Bearer <key>``
* model id in the ``poolside/<name>`` form, e.g. ``poolside/laguna-s-2.1``

**Structured output is prompted, not constrained.** Poolside's documentation does not
list ``response_format`` or JSON-schema support, and guessing that a parameter exists is
how you get a 400 in production — or worse, a silently ignored field and unvalidated
output. So this provider goes through :func:`prompted_structured`: schema in the system
prompt, tolerant extraction, one reprompt on a validation failure. If Poolside adds
``response_format`` later, :meth:`_supports_response_format` is the one place to change.

**Cost is reported as unattributed.** Poolside publishes no per-token rates I could
verify, so this provider returns token counts with ``attributed=False`` rather than
inventing a rate. The job report shows the gap instead of a plausible-looking wrong
number, because the plan's unit-economics argument depends on that figure being real.
Set ``POOLSIDE_RATE_IN``/``POOLSIDE_RATE_OUT`` (USD per million tokens) from your own
contract to turn attribution on.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from arcvisual.config import settings
from arcvisual.providers.base import (
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
    RetryableProviderError,
    StructuredResult,
    Task,
    TextBlock,
    Usage,
    prompted_structured,
)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "https://inference.poolside.ai/v1"
DEFAULT_MODEL = "poolside/laguna-s-2.1"


class PoolsideProvider:
    name = "poolside"

    def __init__(self, client: Any = None) -> None:
        cfg = settings().poolside
        if client is None and not cfg.api_key:
            raise ProviderNotConfigured(
                "POOLSIDE_API_KEY is not set (create one in the Poolside Platform "
                "API Keys tab, or point POOLSIDE_BASE_URL at a self-managed endpoint)"
            )
        self._cfg = cfg
        self._client = client

    # -- plumbing ---------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        attributed = self._cfg.rate_in is not None and self._cfg.rate_out is not None
        return Capabilities(
            name=self.name,
            native_structured_output=False,
            prompt_cache=False,
            vision=False,
            cost_attributed=attributed,
            prompt_char_budget=self._cfg.prompt_char_budget,
            note=(
                "OpenAI-compatible; JSON is prompted and validated client-side. "
                "No prompt cache, so the paper body is re-sent on every call — the "
                "dominant cost for a multi-scene paper."
                + (
                    ""
                    if attributed
                    else " Cost unattributed: set POOLSIDE_RATE_IN/OUT from your "
                    "contract to enable it."
                )
            ),
        )

    def model_for(self, task: Task) -> str:
        return self._cfg.model_for(task.value)

    def _supports_response_format(self) -> bool:
        """Single switch to flip if Poolside documents JSON-schema output.

        Left False deliberately: sending an undocumented parameter risks a 400, and a
        silently-ignored one is worse — we would stop validating output while
        believing the model was constrained.
        """
        return False

    def _client_or_new(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self._cfg.base_url,
                timeout=self._cfg.timeout_s,
                headers={
                    "Authorization": f"Bearer {self._cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )
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
        max_attempts: int = 2,
    ) -> StructuredResult:
        model = self.model_for(task)
        # The caller's max_tokens is calibrated for a provider that budgets thinking
        # separately. Poolside counts reasoning against this number, so honouring a
        # caller's 8192 means the model reasons for 8192 tokens and returns nothing.
        budget = max(max_tokens, self._cfg.max_tokens)

        def send(system_text: str, user_text: str) -> tuple[str, Usage]:
            return self._chat(model, system_text, user_text, budget)

        return prompted_structured(
            send=send,
            system=system,
            user=user,
            output_model=output_model,
            provider=self.name,
            model=model,
            max_attempts=max_attempts,
        )

    def _chat(
        self, model: str, system_text: str, user_text: str, max_tokens: int
    ) -> tuple[str, Usage]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": self._cfg.temperature,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        }
        if self._supports_response_format():  # pragma: no cover - see the docstring
            payload["response_format"] = {"type": "json_object"}

        client = self._client_or_new()
        try:
            response = client.post("/chat/completions", json=payload)
        except Exception as exc:
            # A timeout or connection reset is transient by nature.
            raise RetryableProviderError(
                f"poolside request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"poolside returned {response.status_code}: {response.text[:400]}"
            )
        body = response.json()
        return _first_message(body), self._usage(body)

    def _usage(self, body: dict[str, Any]) -> Usage:
        raw = body.get("usage") or {}
        prompt_tokens = int(raw.get("prompt_tokens") or 0)
        completion_tokens = int(raw.get("completion_tokens") or 0)
        rate_in, rate_out = self._cfg.rate_in, self._cfg.rate_out
        if rate_in is None or rate_out is None:
            # Tokens are real, the price is not knowable here. Say so.
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
        """``GET /v1/models``. Useful for checking a self-managed endpoint's inventory."""
        response = self._client_or_new().get("/models")
        if response.status_code >= 400:
            raise ProviderError(f"poolside /models returned {response.status_code}")
        return [m.get("id", "") for m in (response.json().get("data") or [])]


def _first_message(body: dict[str, Any]) -> str:
    """The assistant's text, or an error that says what actually went wrong.

    ``content: null`` is a real and confusing outcome on a reasoning model: the
    request succeeds with HTTP 200 and every field present, but the answer is absent
    because the token budget went to reasoning. A bare "unreadable content of type
    NoneType" sent an operator hunting through the wrong layer, so the budget numbers
    are named here instead.
    """
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError("poolside returned no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")

    if content is None:
        finish = choice.get("finish_reason")
        usage = body.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens", 0
        )
        completion = usage.get("completion_tokens", 0)
        if finish == "length":
            # Reasoning depth varies run to run on the same prompt, so this is a dice
            # roll rather than a dead end — the next attempt often answers fine.
            raise RetryableProviderError(
                f"poolside returned no content: the {completion}-token budget was "
                f"consumed by reasoning ({reasoning} reasoning tokens) before an "
                "answer was written. Raise POOLSIDE_MAX_TOKENS, or shorten the prompt."
            )
        # A reasoning-only reply is still an answer of sorts; hand it downstream and
        # let the tolerant JSON extractor decide, rather than discarding the call.
        fallback = message.get("reasoning_content")
        if isinstance(fallback, str) and fallback.strip():
            log.info("poolside: content was null, falling back to reasoning_content")
            return fallback
        raise ProviderError(
            f"poolside returned no content (finish_reason={finish!r}, "
            f"completion_tokens={completion})"
        )
    if isinstance(content, str):
        return content
    # Some OpenAI-compatible servers return a content-part list.
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    raise ProviderError(f"poolside returned unreadable content of type {type(content)}")

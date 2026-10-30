"""Long-context hosts: one request carries the whole paper.

Groq's free tier is fast and constrained, but it meters 8,000 tokens per minute with
the prompt and the reply sharing that allowance, so ANALYZE is sent ~5,000 characters
of a 78,000-character paper — a model with a 131K window reading one section at a
time. Everything downstream inherits that blindness: the analysis cannot see the
architecture section while it reads the equations, so it proposes what it can see,
which is equations.

This provider targets hosts whose models accept 100K-260K tokens *per request* on the
plan the operator holds (OpenRouter, Gemini, Mistral, DeepSeek, Cerebras, or any
OpenAI-compatible endpoint). The prompt budget is derived from the context window
rather than from a per-minute meter, so a whole paper goes in one ANALYZE pass and
codegen can be given a section's full text instead of a 1,500-character quote.

Two things are deliberately conservative:

* **Strict schemas degrade to prompted JSON, they never fail the call.** Each host
  implements ``json_schema`` strict mode against a different subset of JSON Schema
  (Gemini rejects some optional fields; OpenRouter may route to a host that ignores
  the parameter). A 400 on the constrained path, or a constrained reply that does not
  validate, falls back to the schema-in-prompt ladder, which every model can follow.
* **Cost is unattributed unless rates are configured** (``LLM_RATE_IN``/``OUT``),
  for the same reason as every other provider here: a stale rate is worse than none.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from pydantic import BaseModel

from arcvisual.config import LONG_CONTEXT_PRESETS, LongContext, settings
from arcvisual.providers.base import (
    Capabilities,
    ModelUnavailable,
    ProviderError,
    ProviderNotConfigured,
    RetryableProviderError,
    StructuredResult,
    Task,
    TextBlock,
    Usage,
    prompted_structured,
)
from arcvisual.providers.openai_compat import (
    _SCHEMA_REJECTED,
    OpenAICompatProvider,
    _schema_digest,
)

log = logging.getLogger(__name__)

#: Model ids the server reported as gone, per process — shared across scene lanes.
_UNAVAILABLE: set[str] = set()

#: Below this many seconds left there is no point starting a call: it cannot finish.
_MIN_CALL_S = 10.0


class LongContextProvider(OpenAICompatProvider):
    name = "longcontext"

    def __init__(
        self, client: object | None = None, cfg: LongContext | None = None
    ) -> None:
        cfg = cfg or settings().long_context
        if client is None:
            preset = LONG_CONTEXT_PRESETS.get(cfg.preset)
            key_env = preset.key_env if preset else "LLM_API_KEY"
            if not cfg.api_key:
                raise ProviderNotConfigured(
                    f"{key_env} (or LLM_API_KEY) is not set for the {cfg.preset!r} "
                    "long-context preset"
                )
            if not cfg.base_url:
                raise ProviderNotConfigured(
                    "LLM_BASE_URL is not set (required for the 'custom' preset)"
                )
            if not cfg.models_for("classify"):
                raise ProviderNotConfigured(
                    f"no model configured for the {cfg.preset!r} preset; set "
                    "LLM_MODEL_CLASSIFY"
                )
        self._cfg = cfg
        self._http = client
        #: time.monotonic() instant no call may run past; None = only call_timeout_s.
        self._deadline: float | None = None
        #: The task of the call in flight, per thread (scene lanes share a provider).
        self._local = threading.local()
        # Reports read "openrouter" or "gemini", not a generic label: a cost anomaly
        # is investigated per host.
        self.name = cfg.preset if cfg.preset in LONG_CONTEXT_PRESETS else "longcontext"

    # -- config ------------------------------------------------------------- #

    def _base_url(self) -> str:
        return self._cfg.base_url

    def _api_key(self) -> str:
        return self._cfg.api_key

    def _timeout_s(self) -> float:
        return self._cfg.timeout_s

    def _temperature(self) -> float:
        return self._cfg.temperature

    def _budget(self, requested: int, model: str = "", task: str = "") -> int:
        """Never below the configured ceiling.

        Callers ask for ``Models.max_tokens`` (8,192), sized for a provider that
        budgets thinking separately. Reasoning models on these hosts bill thinking as
        output, and a model that thinks for 8k tokens returns ``content: null``.
        """
        return max(requested, self._cfg.max_tokens)

    def _reasoning_effort(self, model: str = "") -> str:
        return self._cfg.reasoning_effort

    def _extra_payload(self, response_format: dict | None) -> dict[str, Any]:
        if self._cfg.preset != "openrouter":
            return {}
        extra: dict[str, Any] = {}
        if self._reasons():
            # Every call here is a single turn, so there is no `reasoning_details` to
            # pass back: the model reasons, answers, and the next call starts fresh.
            extra["reasoning"] = {"enabled": True}
        if response_format is not None:
            # Route only to upstream hosts that honour `response_format`. Without it
            # OpenRouter may pick one that silently ignores the schema.
            extra["provider"] = {"require_parameters": True}
        return extra

    def _reasons(self) -> bool:
        """Reason on this call? Diagrams yes; analysis only when asked for."""
        if not self._cfg.reasoning:
            return False
        task = getattr(self._local, "task", None)
        return task is not Task.CLASSIFY or self._cfg.reasoning_analyze

    def set_deadline(self, deadline: float | None) -> None:
        """No model call may run past this ``time.monotonic()`` instant.

        The stepped deployment calls this with the end of its step: a call still
        running when the platform kills the function never reaches an error handler,
        so the job re-runs the same doomed call on every step, forever.
        """
        self._deadline = deadline

    def _call_budget(self) -> float:
        budget = self._cfg.call_timeout_s
        if self._deadline is not None:
            budget = min(budget, self._deadline - time.monotonic())
        return budget

    def _post(self, payload: dict[str, Any]) -> Any:
        """Stream the reply and stop at a wall-clock budget.

        Non-streaming is unboundable here: OpenRouter answers 200 at once and keeps
        the connection alive with periodic bytes until the model finishes, so the
        HTTP read timeout never fires. Streaming yields a line at a time, and the
        clock is checked on each one.
        """
        model = payload.get("model", "")
        budget = self._call_budget()
        if budget < _MIN_CALL_S:
            # Not retryable within this step: a retry would start with even less.
            raise ProviderError(
                f"{self.name}: only {max(budget, 0):.0f}s left in this step, too "
                "little for another model call; the next step continues"
            )
        client = self._client()
        if not hasattr(client, "stream"):
            return client.post("/chat/completions", json=payload)
        body: dict[str, Any] = {**payload, "stream": True}
        if self._cfg.preset == "openrouter":
            # Usage on a stream is opt-in there, and without it a call is billed as
            # zero tokens: the job's dollar ceiling would never trip. Other hosts
            # (Mistral) reject unknown request fields, so this is not sent to them.
            body["stream_options"] = {"include_usage": True}
        with client.stream("POST", "/chat/completions", json=body) as response:
            if response.status_code != 200:
                response.read()
                return response
            # The elapsed check below runs between lines, so it cannot end a stream
            # that has gone silent: one blocked read would wait out the HTTP read
            # timeout (300s) — past the function's own limit. The watchdog closes
            # the stream at the budget, which unblocks that read.
            expired = threading.Event()

            def _expire() -> None:
                expired.set()
                response.close()

            watchdog = threading.Timer(budget, _expire)
            watchdog.daemon = True
            watchdog.start()
            try:
                return self._read_stream(response, model, budget, expired)
            finally:
                watchdog.cancel()

    def _read_stream(
        self, response: Any, model: str, budget: float, expired: threading.Event
    ) -> _Reply:
        content: list[str] = []
        finish = None
        usage: dict[str, Any] | None = None
        try:
            for line in response.iter_lines():
                if expired.is_set():
                    self._too_slow(model, budget)
                if not line.startswith("data:"):
                    continue  # SSE comments are OpenRouter's keep-alives
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    error = chunk["error"] or {}
                    code = error.get("code")
                    return _Reply(code if isinstance(code, int) else 502, chunk)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    finish = choice.get("finish_reason") or finish
        except ProviderError:
            raise
        except Exception:
            if expired.is_set():
                # The watchdog closed the stream mid-read.
                self._too_slow(model, budget)
            raise
        if expired.is_set():
            self._too_slow(model, budget)
        message = {"content": "".join(content) if content else None}
        return _Reply(
            200,
            {"choices": [{"message": message, "finish_reason": finish}], "usage": usage},
        )

    def _too_slow(self, model: str, budget: float) -> None:
        if model.endswith(":free"):
            # A free endpoint this slow now is no faster on the next attempt; the
            # paid models behind it in the chain are the way to finish in time.
            raise ModelUnavailable(model, f"no complete answer within {budget:.0f}s")
        raise RetryableProviderError(
            f"{self.name}: {model} gave no complete answer within {budget:.0f}s"
        )

    def _model_exhausted(self, model: str, body: str) -> None:
        # OpenRouter caps ':free' models per day ("Rate limit exceeded:
        # free-models-per-day", ~50 requests without credits). Retrying cannot clear
        # that until tomorrow, and failing the call would never reach the paid models
        # behind it in the chain — so the free model is reported spent instead.
        if model.endswith(":free") and re.search(r"per[- ]day", body, re.IGNORECASE):
            raise ModelUnavailable(model, "free-model daily quota exhausted")

    def supports_json_schema(self, model: str = "") -> bool:
        return self._cfg.json_schema

    def candidates_for(self, task: Task) -> list[str]:
        chain = self._cfg.models_for(task.value) or self._cfg.models_for("classify")
        return [m for m in chain if m not in _UNAVAILABLE] or chain

    def mark_unavailable(self, model: str) -> None:
        _UNAVAILABLE.add(model)

    def model_for(self, task: Task) -> str:
        candidates = self.candidates_for(task)
        return candidates[0] if candidates else ""

    # -- the call ----------------------------------------------------------- #

    def _structured_with(
        self,
        model: str,
        system: list[TextBlock],
        user: str,
        output_model: type[BaseModel],
        task: Task,
        max_tokens: int,
        max_attempts: int,
    ) -> StructuredResult:
        self._local.task = task
        budget = self._budget(max_tokens, model, task.value)
        schema_key = (model, output_model.__name__, _schema_digest(output_model))
        first: StructuredResult | None = None
        if self.supports_json_schema(model) and schema_key not in _SCHEMA_REJECTED:
            try:
                first = self._constrained(
                    model, system, user, output_model, budget, max_attempts
                )
            except ProviderError as exc:
                if not _is_schema_refusal(exc):
                    raise
                log.warning(
                    "%s/%s refused the %s schema for strict decoding; using prompted "
                    "JSON: %s",
                    self.name,
                    model,
                    output_model.__name__,
                    str(exc)[:200],
                )
                _SCHEMA_REJECTED.add(schema_key)
            if first is not None and first.ok:
                return first

        def send(system_text: str, user_text: str) -> tuple[str, Usage]:
            return self._chat(model, system_text, user_text, budget)

        try:
            result = prompted_structured(
                send=send,
                system=system,
                user=user,
                output_model=output_model,
                provider=self.name,
                model=model,
                max_attempts=max_attempts,
            )
        except ProviderError as exc:
            # Without `response_format` there is no parameter to route around, so
            # OpenRouter's "No endpoints found" means the model itself has no live
            # host — a withdrawn ':free' endpoint. That is the one failure where the
            # next model in the chain is the right answer.
            if "returned 404" in str(exc) and "No endpoints found" in str(exc):
                raise ModelUnavailable(model, str(exc)[:200]) from exc
            raise
        if first is not None:
            # The failed constrained attempt was still billed.
            result.usage = first.usage.merge(result.usage)
            result.attempts += first.attempts
            result.findings = [*first.findings, *result.findings]
        return result

    # -- reporting ---------------------------------------------------------- #

    def capabilities(self) -> Capabilities:
        attributed = self._cfg.rate_in is not None and self._cfg.rate_out is not None
        preset = LONG_CONTEXT_PRESETS.get(self._cfg.preset)
        return Capabilities(
            name=self.name,
            native_structured_output=self._cfg.json_schema,
            prompt_cache=False,
            vision=False,
            cost_attributed=attributed,
            prompt_char_budget=self._cfg.prompt_char_budget,
            analysis_item_budget=self._cfg.analysis_item_budget or None,
            analysis_passes=1,
            note=(
                f"{self.model_for(Task.CLASSIFY)} for analyze / "
                f"{self.model_for(Task.CODEGEN)} for codegen at {self._cfg.base_url}; "
                f"{self._cfg.context_tokens:,}-token context, so the whole paper is "
                "read in one pass. "
                + (
                    "strict json_schema with a prompted fallback. "
                    if self._cfg.json_schema
                    else "JSON prompted and validated client-side. "
                )
                + (
                    "Reasoning enabled for diagrams"
                    + (" and analysis. " if self._cfg.reasoning_analyze else ". ")
                    if self._cfg.reasoning
                    else ""
                )
                + (preset.note + " " if preset and preset.note else "")
                + (
                    "Cost attributed from LLM_RATE_IN/OUT."
                    if attributed
                    else "Cost unattributed: set LLM_RATE_IN/OUT to enable it."
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
                attributed=False,
            )
        cost = (prompt_tokens * rate_in + completion_tokens * rate_out) / 1_000_000
        return Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cost_usd=round(cost, 6),
            attributed=True,
        )


def _is_schema_refusal(exc: ProviderError) -> bool:
    """A 400/422 on the constrained path is about the schema's shape, not the paper.

    Hosts word it differently ("invalid JSON schema", Gemini's INVALID_ARGUMENT,
    "response_format is not supported"), but a bad request on the *constrained* path
    with everything else identical to a working prompted call means the same thing.
    """
    if isinstance(exc, ModelUnavailable):
        return False  # the id is gone; the caller moves to the next model
    text = str(exc)
    # OpenRouter with `require_parameters` answers 404 "No endpoints found that can
    # handle the requested parameters" when no host serving this model supports
    # `response_format` — common on free endpoints. Prompted JSON still works there.
    if "returned 404" in text and "No endpoints found" in text:
        return True
    return any(f"returned {code}" in text for code in (400, 422)) or (
        "invalid JSON schema" in text
    )


class _Reply:
    """A response assembled from a stream, shaped like the httpx one `_chat` reads."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._body

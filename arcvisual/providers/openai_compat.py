"""Shared base for OpenAI-compatible chat providers.

Poolside and Groq speak the same wire protocol — ``POST /chat/completions`` with a
Bearer token — and differ in exactly the ways that matter to this pipeline:

* **Constrained decoding.** Groq implements ``response_format: {"type":
  "json_schema", ..., "strict": true}``, which *guarantees* the reply matches the
  schema. Poolside documents no such parameter. That single difference decides
  whether the prompted-JSON retry ladder is load-bearing or dead code.
* **Reasoning accounting.** Poolside counts reasoning tokens against ``max_tokens``
  and will spend the whole budget thinking; that is why it needs a prompt budget and
  a big ceiling. Groq does not behave this way.

Everything else — auth, usage parsing, error shapes — is shared, so it lives here.
Subclasses declare their differences rather than reimplementing an HTTP client.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TypeVar

from pydantic import BaseModel

from arcvisual.providers.base import (
    ModelUnavailable,
    ProviderError,
    RetryableProviderError,
    StructuredResult,
    Task,
    TextBlock,
    Usage,
    prompted_structured,
)
from arcvisual.providers.ratelimit import (
    TokenBudget,
    daily_limit_detail,
    is_daily_limit,
    limit_resets_in,
    retry_after_seconds,
)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

#: Longest we will wait out a per-DAY quota before calling it a day. Beyond this
#: the job should fail with something the operator can act on rather than sleep.
_DAILY_WAIT_CEILING_S = 45.0

#: HTTP statuses worth another attempt. 429 and 5xx are load, not a bad request.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: (model, schema name, schema digest) combinations a server refused for strict
#: decoding in this process. Retrying the identical schema fails identically.
_SCHEMA_REJECTED: set[tuple[str, str, str]] = set()


def _schema_digest(output_model: type[BaseModel]) -> str:
    import hashlib

    raw = json.dumps(output_model.model_json_schema(), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class OpenAICompatProvider:
    """Chat-completions plumbing. Subclasses supply config and capabilities."""

    name = "openai-compat"

    # -- subclass hooks ----------------------------------------------------- #

    def _base_url(self) -> str:
        raise NotImplementedError

    def _api_key(self) -> str:
        raise NotImplementedError

    def _timeout_s(self) -> float:
        return 300.0

    def _temperature(self) -> float:
        return 0.2

    def _budget(self, requested: int, model: str = "", task: str = "") -> int:
        """Output budget to request. ``task`` matters where prompt sizes differ
        sharply between tasks and the two share one per-minute allowance."""
        return requested

    def _reasoning_effort(self, model: str = "") -> str:
        """Reasoning depth to request, or "" to omit the parameter entirely.

        Omission matters, and is per-model: ``qwen/qwen3.6-27b`` accepts only "none"
        or "default" and rejects "low"/"medium"/"high" with 400, while other models
        reject the parameter outright.
        """
        return ""

    def _budget_field(self) -> str:
        """Name of the output-budget parameter.

        ``max_tokens`` is deprecated in OpenAI's own schema in favour of
        ``max_completion_tokens``; both are accepted by Groq today, so subclasses
        can pick whichever their backend documents.
        """
        return "max_tokens"

    def _token_budget(self, model: str = "") -> TokenBudget | None:
        """A per-minute token meter shared across this provider's callers, or None.

        Keyed by model because the allowance is: on one key ``qwen/qwen3.6-27b``
        allows 8,000 tokens/minute and ``groq/compound`` allows 70,000.
        """
        return None

    def supports_json_schema(self, model: str = "") -> bool:
        """Whether this *model* enforces a JSON Schema rather than merely reading it.

        Per-model, not per-provider: on one Groq key ``qwen/qwen3.6-27b`` enforces
        strict schemas while ``groq/compound`` answers 400 "This model does not
        support response format `json_schema`". Asking the provider as a whole would
        be wrong for one of them whichever answer it gave.

        True means the retry ladder becomes a safety net instead of the primary
        mechanism. It must only be True where the provider documents it — a silently
        ignored parameter is worse than an absent one, because we would stop
        validating while believing the model was constrained.
        """
        return False

    # -- plumbing ----------------------------------------------------------- #

    def _client(self) -> Any:
        if getattr(self, "_http", None) is None:
            import httpx

            self._http = httpx.Client(
                base_url=self._base_url(),
                timeout=self._timeout_s(),
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
            )
        return self._http

    def healthcheck(self, timeout_s: float = 30.0):
        from arcvisual.providers.base import probe

        return probe(self, timeout_s)

    # -- the call ----------------------------------------------------------- #

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
        """One typed object, walking the configured models if one has been retired.

        Only :class:`ModelUnavailable` moves on to the next model. Every other failure
        is about the request or the moment, not the model id, and switching models
        would only hide it.
        """
        candidates = self.candidates_for(task) or [self.model_for(task)]
        last: ModelUnavailable | None = None
        for model in candidates:
            try:
                return self._structured_with(
                    model, system, user, output_model, task, max_tokens, max_attempts
                )
            except ModelUnavailable as exc:
                log.warning("%s: %s; trying the next configured model", self.name, exc)
                self.mark_unavailable(model)
                last = exc
        raise (
            last if last else ProviderError(f"{self.name} has no model for {task.value}")
        )

    def candidates_for(self, task: Task) -> list[str]:
        """Models to try for `task`, best first. Single-model providers return one."""
        return [self.model_for(task)]

    def mark_unavailable(self, model: str) -> None:
        """Remember that the server rejected `model` as unknown. No-op by default."""

    def _structured_with(
        self,
        model: str,
        system: list[TextBlock],
        user: str,
        output_model: type[T],
        task: Task,
        max_tokens: int,
        max_attempts: int,
    ) -> StructuredResult:
        budget = self._budget(max_tokens, model, task.value)

        schema_key = (model, output_model.__name__, _schema_digest(output_model))
        if self.supports_json_schema(model) and schema_key not in _SCHEMA_REJECTED:
            try:
                return self._constrained(
                    model, system, user, output_model, budget, max_attempts
                )
            except ProviderError as exc:
                if "invalid JSON schema" not in str(exc):
                    raise
                # The model refuses this SHAPE of schema (each implementation of
                # strict mode supports a different subset). That is a fact about the
                # schema, not the request, so remember it and use the prompted path
                # — validated client-side — rather than failing every call.
                log.warning(
                    "%s rejected the %s schema for strict decoding; using prompted "
                    "JSON instead: %s",
                    model,
                    output_model.__name__,
                    str(exc)[:200],
                )
                _SCHEMA_REJECTED.add(schema_key)

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

    def _constrained(
        self,
        model: str,
        system: list[TextBlock],
        user: str,
        output_model: type[T],
        budget: int,
        max_attempts: int = 2,
    ) -> StructuredResult:
        """Schema-enforced generation, retried on a truncated reply.

        A guaranteed schema removes the *parsing* failure mode, not every failure
        mode. Constrained decoding can still run past the output budget and stop
        mid-object, and the server then rejects its own generation with
        ``json_validate_failed``. Reply length varies run to run for one prompt, so
        the same request often completes next time — which is why this retries rather
        than failing outright. Analyze has no ladder above it: giving up here loses
        the whole paper, not one scene.
        """
        system_text = "\n\n".join(b.text for b in system if b.text.strip())
        schema = output_model.model_json_schema()
        _harden_schema(schema)

        last: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                return self._constrained_once(
                    model, system_text, user, output_model, schema, budget, attempt
                )
            except RetryableProviderError as exc:
                last = exc
                log.info(
                    "%s constrained attempt %d/%d failed: %s",
                    self.name,
                    attempt,
                    max_attempts,
                    exc,
                )
        raise last if last else ProviderError(f"{self.name} returned no result")

    def _constrained_once(
        self,
        model: str,
        system_text: str,
        user: str,
        output_model: type[T],
        schema: dict,
        budget: int,
        attempt: int,
    ) -> StructuredResult:
        from arcvisual.providers.base import parse_into

        raw, usage = self._chat(
            model,
            system_text,
            user,
            budget,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": output_model.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        value, findings = parse_into(output_model, raw)
        return StructuredResult(
            value=value,
            usage=usage,
            provider=self.name,
            model=model,
            raw_text=raw,
            attempts=attempt,
            findings=findings,
        )

    def _chat(
        self,
        model: str,
        system_text: str,
        user_text: str,
        max_tokens: int,
        response_format: dict | None = None,
    ) -> tuple[str, Usage]:
        meter = self._token_budget(model)
        # Charged against the same per-minute budget as the reply, so the prompt is
        # measured before deciding what to ask for. ~4 chars/token is rough, but it
        # only needs to be right enough to stay under a ceiling.
        prompt_tokens = (len(system_text) + len(user_text)) // 4
        if response_format is not None:
            # The schema travels with the request and is billed with it, but the
            # caller estimating from message length cannot see it. Measured at ~665
            # tokens for AnalysisOut — enough to push a request over the ceiling.
            prompt_tokens += len(json.dumps(response_format)) // 4
        if meter is not None:
            max_tokens = meter.clamp(max_tokens, prompt_tokens)

        payload: dict[str, Any] = {
            "model": model,
            self._budget_field(): max_tokens,
            "temperature": self._temperature(),
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        }
        effort = self._reasoning_effort(model)
        if effort:
            payload["reasoning_effort"] = effort
        if response_format is not None:
            payload["response_format"] = response_format

        reserved = 0
        if meter is not None:
            reserved = meter.acquire(max_tokens + prompt_tokens)
        try:
            response = self._client().post("/chat/completions", json=payload)
        except Exception as exc:
            if meter is not None:
                meter.settle(reserved, 0)  # nothing was spent; give it all back
            # A timeout or reset is transient by nature; let the ladder retry it.
            raise RetryableProviderError(
                f"{self.name} request failed: {type(exc).__name__}"
            ) from exc

        if meter is not None:
            meter.observe(response.headers)

        if response.status_code == 429 and is_daily_limit(response.text):
            if meter is not None:
                meter.penalise(response.headers)
            resets_in = limit_resets_in(response.text)
            # The per-day window is rolling, so the wait scales with the size of the
            # ask. A short wait is worth taking; a long one is not, because every
            # retry against it spends a request for nothing and reports
            # "rate-limited" when the real answer is "this key is done for now".
            # Groq exposes none of this in the headers — they still advertise a full
            # per-minute allowance while the daily budget is spent.
            if resets_in is not None and resets_in <= _DAILY_WAIT_CEILING_S:
                log.info(
                    "%s daily quota nearly clear; waiting %.0fs", self.name, resets_in
                )
                time.sleep(resets_in)
                raise RetryableProviderError(
                    f"{self.name} daily quota clears in {resets_in:.0f}s; retrying"
                )
            raise ProviderError(
                f"{self.name} daily token quota exhausted "
                f"({daily_limit_detail(response.text)}). Wait for the reset, use a "
                f"different key, or switch provider with ARCVISUAL_PROVIDER."
            )
        if response.status_code == 429:
            if meter is not None:
                # Drain rather than refund: refunding would let every lane waiting
                # behind this one wake immediately and fire into the same limit.
                meter.penalise(response.headers)
            # Never zero: a 429 whose reset header reads "0s" or a few milliseconds
            # otherwise spends every remaining attempt in the same instant.
            delay = max(2.0, retry_after_seconds(response.headers))
            log.info(
                "%s rate-limited; sleeping %.1fs per its own headers", self.name, delay
            )
            time.sleep(delay)
            raise RetryableProviderError(
                f"{self.name} rate-limited; retrying after {delay:.0f}s"
            )
        if response.status_code == 413:
            if meter is not None:
                meter.settle(reserved, 0)
            # Not retryable: the *request* is too big, so an identical retry fails
            # identically. Say what to change, since the raw body does not.
            raise ProviderError(
                f"{self.name} rejected the request as too large (413). The requested "
                f"output budget ({max_tokens}) plus prompt (~{prompt_tokens} tokens) "
                f"exceeds this key's per-minute ceiling. Lower the budget "
                f"(e.g. GROQ_MAX_TOKENS) or shorten the prompt. Body: "
                f"{response.text[:200]}"
            )
        if response.status_code in _RETRYABLE_STATUS:
            if meter is not None:
                meter.settle(reserved, 0)
            raise RetryableProviderError(
                f"{self.name} returned {response.status_code}; retrying"
            )
        if response.status_code == 400 and "json_validate_failed" in response.text:
            if meter is not None:
                # Charge the FULL reservation, do not refund. The model generated
                # until it ran out of budget — that is what "did not complete within
                # the output budget" means — so those tokens were spent even though
                # the server discarded the result. This body carries no `usage` block
                # to read them from, and refunding on that absence let the meter
                # believe capacity existed that the server had already consumed:
                # every retry then fired straight into a 429.
                meter.settle(reserved, reserved)
            # Constrained decoding ran past the output budget and stopped mid-object,
            # so the server rejected its own generation. Retryable rather than fatal:
            # reply length varies run to run for the same prompt, and the identical
            # request often completes on the next attempt. That is precisely the
            # "dice roll" distinction RetryableProviderError exists to draw.
            raise RetryableProviderError(
                f"{self.name} could not complete a schema-valid reply within the "
                f"output budget ({max_tokens} tokens); retrying"
            )
        if response.status_code >= 400:
            if meter is not None:
                meter.settle(reserved, 0)
            if _is_model_gone(response.status_code, response.text):
                raise ModelUnavailable(model, response.text[:200])
            raise ProviderError(
                f"{self.name} returned {response.status_code}: {response.text[:400]}"
            )
        body = response.json()
        usage = self.usage_from(body)
        if meter is not None:
            meter.settle(reserved, usage.input_tokens + usage.output_tokens)
        return first_message(body), usage

    def usage_from(self, body: dict[str, Any]) -> Usage:
        raw = body.get("usage") or {}
        return Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            cost_usd=0.0,
            attributed=False,
        )


def _harden_schema(schema: dict) -> None:
    """Make a Pydantic schema acceptable to strict constrained decoding.

    Two opposite edits, for two different failure modes.

    **Tighten what strict mode requires.** It wants ``additionalProperties: false`` on
    every object and every property listed in ``required``. Pydantic emits neither for
    optional fields, and Groq's implementation is stricter than OpenAI's, so a schema
    that works elsewhere is rejected here unless it is walked and tightened first.

    **Loosen what it would reject pointlessly.** ``maxLength`` on a prose field is a
    *cosmetic* constraint: every such field on the analysis models carries a
    ``_clamp`` validator that truncates it client-side. Sent to a strict validator it
    stops being cosmetic — one over-long ``justification`` makes Groq reject the
    entire response with 400, discarding a complete and otherwise valid analysis.
    Observed exactly that: a well-formed object with four concepts thrown away over
    ``/opportunities/0/justification``.

    So the rule this project already applies to its own schemas applies to the wire
    format too — **structural constraints reject, cosmetic constraints coerce.**
    ``minLength`` stays: a too-short quote is a grounding failure that no client-side
    truncation can repair, so it should genuinely constrain generation. Enums, types,
    numeric ranges and ``maxItems`` stay for the same reason — they carry meaning, and
    the model satisfies them readily.
    """
    if not isinstance(schema, dict):
        return
    # Cosmetic: the client clamps these, so enforcing them here only loses answers.
    schema.pop("maxLength", None)
    schema.pop("pattern", None)
    for defs_key in ("$defs", "definitions"):
        for sub in (schema.get(defs_key) or {}).values():
            _harden_schema(sub)
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        if props:
            schema["additionalProperties"] = False
            schema["required"] = list(props.keys())
        # An object with NO declared properties is a free-form map, and
        # `additionalProperties: false` locks it to accept nothing at all. That is not
        # a stricter version of the same schema, it is the opposite of what the field
        # means. `ParamsOut.params` is exactly this: an untyped dict holding whatever
        # the chosen template's parameters are. Sealed empty, constrained decoding
        # cannot emit a single parameter key, and the call burns its whole output
        # budget failing to produce a valid object.
        for sub in props.values():
            _harden_schema(sub)
    # Tuples arrive as `prefixItems`, which strict implementations support
    # unevenly. A homogeneous tuple (a point, a range) says the same thing as
    # `items` plus its min/max length, which every implementation accepts; the
    # client-side model still validates the exact arity.
    prefix = schema.get("prefixItems")
    if isinstance(prefix, list) and prefix and all(p == prefix[0] for p in prefix):
        schema["items"] = schema.pop("prefixItems")[0]
        schema.setdefault("minItems", len(prefix))
        schema.setdefault("maxItems", len(prefix))
    for key in ("items", "prefixItems"):
        node = schema.get(key)
        if isinstance(node, dict):
            _harden_schema(node)
        elif isinstance(node, list):
            for sub in node:
                _harden_schema(sub)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            _harden_schema(sub)


def _is_model_gone(status: int, text: str) -> bool:
    """Whether an error body says the model id itself is the problem.

    Groq answers a retired id with 404 ``model_not_found`` and a deprecated one with
    400 ``model_decommissioned``. Matching the codes rather than the prose keeps this
    from firing on an ordinary 404 from a mistyped base URL.
    """
    if status not in (400, 404):
        return False
    return "model_not_found" in text or "model_decommissioned" in text


def first_message(body: dict[str, Any]) -> str:
    """The assistant's text from a chat-completions body."""
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError("no choices in the response")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if content is None:
        finish = choices[0].get("finish_reason")
        raise RetryableProviderError(f"no content returned (finish_reason={finish!r})")
    raise ProviderError(f"unreadable content of type {type(content)}")

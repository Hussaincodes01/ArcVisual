"""The provider abstraction — one interface, three very different backends.

ArcVisual needs exactly one thing from a model: **a typed object matching a Pydantic
schema.** Analyze returns an ``AnalysisOut``; Generate returns a ``ParamsOut``. Nothing
downstream cares how that object was produced, so that is the whole interface.

The interesting part is that the three backends differ in a way that *matters*, and
pretending otherwise would quietly break the product:

============  ====================  =============  ===================================
Provider      Structured output     Prompt cache   Cost attribution
============  ====================  =============  ===================================
Anthropic     native (constrained)  yes, 1h TTL    exact, per-token
Poolside      prompted JSON only    no             unattributed (no published rates)
opencode      prompted JSON only    no             billed by ITS own configured provider
============  ====================  =============  ===================================

Two consequences are designed for rather than hidden:

* **Prompted JSON needs a repair loop.** A provider without constrained decoding will
  occasionally wrap JSON in prose, fence it, or emit a trailing comma. So
  :func:`extract_json` is deliberately tolerant, and :func:`prompted_structured`
  reprompts once with the validation error before giving up. This is not optional
  politeness — without it the non-Anthropic providers would fail Analyze regularly.
* **An unattributed cost is reported as unattributed, never as zero.** The plan's
  whole unit-economics argument rests on per-paper cost. Silently summing zeros for a
  provider we cannot price would make the ``$/paper`` metric a lie, so
  :class:`Usage` carries ``attributed`` and the job report surfaces the gap.

The paper body is the expensive half of every prompt. Anthropic caches it across the
~20 analyze calls plus every codegen and repair call; the others re-send it each time.
That is a real cost difference, exposed as ``Capabilities.prompt_cache`` so callers can
report it rather than discover it on a bill.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Task(str, Enum):
    """What the call is for. Providers map this to a model.

    The plan asks for a cheaper model to classify and a stronger one to write code;
    that split is per-task, not per-provider, so it belongs here.
    """

    CLASSIFY = "classify"  # Analyze: concepts, triage
    CODEGEN = "codegen"  # Generate: parameter filling, repair
    VISION = "vision"  # Gate 4 (Phase 2)


@dataclass(frozen=True)
class TextBlock:
    """One piece of system context.

    ``cache`` requests a cache breakpoint *after* this block. Providers without a
    cache ignore it, which is why it is a hint and not a guarantee.
    """

    text: str
    cache: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    #: False when the provider's pricing is unknown to us. A zero that means
    #: "we don't know" must never be summed as if it meant "free".
    attributed: bool = True

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            attributed=self.attributed and other.attributed,
        )


@dataclass
class StructuredResult:
    """What every provider returns. ``value`` is None when parsing never succeeded."""

    value: Any | None
    usage: Usage
    provider: str
    model: str
    raw_text: str = ""
    attempts: int = 1
    findings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Capabilities:
    name: str
    native_structured_output: bool
    prompt_cache: bool
    vision: bool
    cost_attributed: bool
    #: Characters of paper body this backend can be given before its own reasoning
    #: crowds out the answer. None means "send the whole thing" — the normal case
    #: for a provider that budgets thinking separately from output.
    prompt_char_budget: int | None = None
    #: Ceiling on how many concepts to ask ANALYZE for, or None for the schema's own
    #: limit. Prompt and reply share one allowance on a metered key, so a large
    #: analysis is not merely slow — constrained decoding runs out of budget partway
    #: and the request fails with "missing properties", losing the whole call. Asking
    #: for less is what makes the reply fit.
    analysis_item_budget: int | None = None
    #: How many ANALYZE calls a paper may be split across when its body exceeds
    #: `prompt_char_budget`. One call over a trimmed body sees a couple of sections;
    #: several calls over different sections see most of the paper. 1 = single pass.
    analysis_passes: int = 1
    #: Free-text note surfaced in the job report — how this provider is billed, what
    #: it cannot do. Read by humans looking at a cost anomaly.
    note: str = ""


class ProviderError(RuntimeError):
    """The provider could not be reached or refused the request outright."""


class ProviderNotConfigured(ProviderError):
    """Credentials or a binary are missing. Distinct from a call failing."""


class ModelUnavailable(ProviderError):
    """The server says this model id does not exist (any more) for this key.

    Not retryable against the same model — it fails identically forever — but it is
    the one failure where switching to the *next configured model* is the right
    response. Hosted providers retire ids without notice, and a deployment that
    cannot route around that is one deprecation away from being down.
    """

    def __init__(self, model: str, detail: str = "") -> None:
        super().__init__(f"model {model!r} is unavailable: {detail}".rstrip(": "))
        self.model = model


class RetryableProviderError(ProviderError):
    """A failure that varies run to run, so another attempt may well succeed.

    The distinction is load-bearing. A 401, a missing binary or an unknown model
    fails identically forever and retrying only wastes time and money. But a
    reasoning model that spent its whole token budget thinking, or a read timeout,
    is a *dice roll* — the same request often succeeds on the next call.

    Raised by providers; caught by :func:`prompted_structured`, which otherwise lets
    call failures escape the retry loop entirely. That gap meant the analyze retry
    budget only ever covered PARSE failures, while the failure actually killing
    papers was a call failure and bypassed it completely.
    """


class HealthResult(BaseModel):
    """Whether a provider will actually answer right now."""

    ok: bool
    detail: str = ""
    latency_ms: int = 0


class Provider(Protocol):
    """Anything that can turn a prompt into a validated Pydantic object."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def healthcheck(self, timeout_s: float = 30.0) -> HealthResult: ...

    def model_for(self, task: Task) -> str: ...

    def structured(
        self,
        *,
        system: list[TextBlock],
        user: str,
        output_model: type[T],
        task: Task,
        max_tokens: int = 8192,
        max_attempts: int = 2,
    ) -> StructuredResult: ...


# --------------------------------------------------------------------------- #
# Tolerant JSON extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json|jsonc)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before a closing brace or bracket.

    String-aware, and that is the whole point. A bare ``re.sub(r",(\\s*[}\\]])", ...)``
    also rewrites content *inside* string values — a caption reading
    ``"first, }"`` would silently lose its comma, and the result still parses, so
    nothing downstream ever notices the text was corrupted. Scanning tracks
    string state and escapes so only structural commas are touched.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ",":
            # Look ahead past whitespace: a comma followed by a closer is trailing.
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in "}]":
                continue  # drop it
        out.append(ch)
    return "".join(out)


def extract_json(text: str) -> str | None:
    """Pull the most plausible JSON object out of free-form model output.

    Ordered by confidence, most reliable first: a fenced block, then a balanced
    brace scan. Deliberately does **not** fall back to a regex over the whole text —
    a "nearly JSON" match would parse into a wrong-but-valid object, and a wrong
    object silently produces a wrong animation. None is the honest answer.
    """
    if not text:
        return None

    candidates: list[str] = []
    for match in _FENCE_RE.finditer(text):
        candidates.append(match.group(1).strip())
    if scanned := _scan_balanced(text):
        candidates.append(scanned)

    for candidate in candidates:
        cleaned = strip_trailing_commas(candidate.strip())
        try:
            json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        return cleaned
    return None


def _scan_balanced(text: str) -> str | None:
    """The first balanced ``{...}`` region, respecting strings and escapes."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_into(output_model: type[T], text: str) -> tuple[T | None, list[str]]:
    """Extract and validate. Findings are phrased for a reprompt, not for a log."""
    payload = extract_json(text)
    if payload is None:
        return None, [
            "the response contained no parsable JSON object; reply with JSON only, "
            "no prose before or after it"
        ]
    try:
        return output_model.model_validate_json(payload), []
    except ValidationError as exc:
        findings = []
        for err in exc.errors()[:12]:
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            findings.append(f"field {loc}: {err['msg']}")
        return None, findings


# --------------------------------------------------------------------------- #
# The prompted-JSON path, shared by every provider without constrained decoding
# --------------------------------------------------------------------------- #

_JSON_INSTRUCTION = """\
Reply with a single JSON object and nothing else. No prose, no explanation, no \
markdown fence. It must validate against this JSON Schema:

{schema}
"""

_REPAIR_INSTRUCTION = """\
Your previous reply did not validate:

{findings}

Reply again with the corrected JSON object only. Do not explain the correction.\
"""


def schema_instruction(output_model: type[BaseModel]) -> str:
    return _JSON_INSTRUCTION.format(
        schema=json.dumps(output_model.model_json_schema(), indent=2)
    )


def prompted_structured(
    *,
    send: Any,
    system: list[TextBlock],
    user: str,
    output_model: type[T],
    provider: str,
    model: str,
    max_attempts: int = 2,
) -> StructuredResult:
    """Drive a plain text-in/text-out backend to a validated object.

    ``send(system_text, user_text) -> (raw_text, Usage)``.

    ``max_attempts`` is a *caller* decision because the cost of giving up differs by
    stage. A codegen failure degrades one scene and the repair ladder above absorbs
    it; an ANALYZE failure kills the entire paper, since there is no ladder above it.
    Giving both the same two attempts treated a single point of failure as if it were
    recoverable — measured on laguna, whose valid-JSON rate is high but not 1.0, so
    the occasional bad reply took a whole paper with it.

    Still bounded, and never unbounded: a model that cannot follow an explicit schema
    after several tries will not manage it on the tenth, and repair.py's per-scene
    cost ceiling has to stay enforceable.
    """
    system_text = "\n\n".join(b.text for b in system if b.text.strip())
    system_text = f"{system_text}\n\n{schema_instruction(output_model)}".strip()

    total = Usage(attributed=True)
    findings: list[str] = []
    raw = ""
    prompt = user

    for attempt in range(1, max_attempts + 1):
        try:
            raw, usage = send(system_text, prompt)
        except RetryableProviderError as exc:
            # A transient call failure counts as a spent attempt, not a dead end.
            findings = [str(exc)]
            log.info(
                "%s: retryable call failure on attempt %s: %s", provider, attempt, exc
            )
            if attempt >= max_attempts:
                break
            continue
        total = total.merge(usage)
        value, findings = parse_into(output_model, raw)
        if value is not None:
            return StructuredResult(
                value=value,
                usage=total,
                provider=provider,
                model=model,
                raw_text=raw,
                attempts=attempt,
                findings=[],
            )
        log.info(
            "%s: structured parse failed on attempt %s: %s", provider, attempt, findings
        )
        prompt = f"{user}\n\n" + _REPAIR_INSTRUCTION.format(
            findings="\n".join(f"  - {f}" for f in findings)
        )

    return StructuredResult(
        value=None,
        usage=total,
        provider=provider,
        model=model,
        raw_text=raw,
        attempts=max_attempts,
        findings=findings,
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class _Ping(BaseModel):
    """The smallest possible structured answer, for a liveness probe."""

    ok: bool


def probe(provider: Any, timeout_s: float = 30.0) -> HealthResult:
    """Ask a provider for a trivial object, and time it.

    Exists because a provider can fail by *going quiet*. Measured on opencode's free
    OpenCode Zen route: calls that had worked minutes earlier began returning no
    output, no error and no exit — just silence until a timeout. Without a probe, a
    twelve-scene paper discovers that twelve times over, once per scene.
    """
    import time as _time

    started = _time.perf_counter()
    try:
        result = provider.structured(
            system=[TextBlock(text="You reply with JSON and nothing else.")],
            user='Reply with exactly {"ok": true}',
            output_model=_Ping,
            task=Task.CLASSIFY,
            max_tokens=256,
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            detail=f"{type(exc).__name__}: {exc}"[:300],
            latency_ms=int((_time.perf_counter() - started) * 1000),
        )
    elapsed = int((_time.perf_counter() - started) * 1000)
    if result.value is None:
        return HealthResult(
            ok=False,
            detail="; ".join(result.findings)[:300] or "no parsable output",
            latency_ms=elapsed,
        )
    return HealthResult(
        ok=True, detail=f"{result.provider}/{result.model}", latency_ms=elapsed
    )

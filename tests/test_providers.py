"""The provider abstraction: selection, structured output, and honest cost.

The tests that matter most here are the ones about *not lying*:

* an unattributed cost must not be reported as zero,
* ``settings().offline`` must agree with what the registry will actually do,
* an explicitly requested provider must never be silently swapped for another,
* ``opencode`` must not be auto-selected just because the binary exists.

Each of those, if broken, produces a system that looks like it is working.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from arcvisual.config import settings
from arcvisual.providers import registry
from arcvisual.providers.base import (
    ProviderNotConfigured,
    Task,
    TextBlock,
    Usage,
    extract_json,
    parse_into,
    prompted_structured,
)
from arcvisual.providers.opencode_provider import extract_assistant_text


class Answer(BaseModel):
    name: str
    count: int = Field(ge=0)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_heuristic_when_nothing_configured(reset_settings) -> None:
    reset_settings(ARCVISUAL_PROVIDER="heuristic")
    assert registry.get_provider() is None
    assert settings().offline is True


def test_opencode_is_never_auto_selected(reset_settings) -> None:
    """Having the binary on PATH must not silently route a pipeline through an
    agent whose cost cannot be attributed. Choosing it has to be a decision."""
    assert "opencode" not in registry.AUTO_PREFERENCE
    reset_settings(
        ARCVISUAL_PROVIDER="auto", ANTHROPIC_API_KEY=None, POOLSIDE_API_KEY=None
    )
    assert registry.get_provider() is None


def test_explicit_provider_is_never_silently_swapped(reset_settings) -> None:
    """Asking for Poolside and quietly getting Claude would be worse than an error."""
    reset_settings(ARCVISUAL_PROVIDER="poolside", POOLSIDE_API_KEY=None)
    with pytest.raises(ProviderNotConfigured, match="requested explicitly"):
        registry.get_provider()


def test_unknown_provider_names_the_alternatives(reset_settings) -> None:
    reset_settings(ARCVISUAL_PROVIDER="gpt-9")
    with pytest.raises(ValueError, match="unknown provider"):
        registry.get_provider()


@pytest.mark.parametrize(
    "provider,env",
    [
        ("anthropic", {"ANTHROPIC_API_KEY": "sk-test"}),
        ("poolside", {"POOLSIDE_API_KEY": "ps-test"}),
    ],
)
def test_offline_agrees_with_registry(reset_settings, provider: str, env: dict) -> None:
    """Two answers to "is a model available" that can disagree is a bug waiting."""
    reset_settings(ARCVISUAL_PROVIDER=provider, **env)
    assert settings().offline is False
    assert registry.get_provider() is not None


def test_describe_flags_the_heuristic_path() -> None:
    described = registry.describe(None)
    assert described["provider"] == "heuristic"
    assert described["cost_attributed"] is False
    assert "not a model baseline" in described["note"].lower()


# --------------------------------------------------------------------------- #
# Cost honesty
# --------------------------------------------------------------------------- #


def test_unattributed_usage_does_not_claim_to_be_free() -> None:
    """A 0.0 that means "we don't know the rate" must be distinguishable from one
    that means "this was free", or the per-paper cost metric becomes fiction."""
    unknown = Usage(input_tokens=1000, output_tokens=500, attributed=False)
    assert unknown.cost_usd == 0.0
    assert unknown.attributed is False


def test_merging_one_unattributed_usage_taints_the_total() -> None:
    known = Usage(input_tokens=10, cost_usd=0.01, attributed=True)
    merged = known.merge(Usage(input_tokens=10, attributed=False))
    assert merged.attributed is False, "a partially-known total is not a known total"
    assert merged.input_tokens == 20


def test_poolside_reports_unattributed_without_configured_rates(reset_settings) -> None:
    reset_settings(
        ARCVISUAL_PROVIDER="poolside",
        POOLSIDE_API_KEY="ps-test",
        POOLSIDE_RATE_IN=None,
        POOLSIDE_RATE_OUT=None,
    )
    from arcvisual.providers.poolside_provider import PoolsideProvider

    caps = PoolsideProvider().capabilities()
    assert caps.cost_attributed is False
    assert "unattributed" in caps.note.lower()


def test_poolside_attributes_cost_once_rates_are_given(reset_settings) -> None:
    reset_settings(
        ARCVISUAL_PROVIDER="poolside",
        POOLSIDE_API_KEY="ps-test",
        POOLSIDE_RATE_IN="1.0",
        POOLSIDE_RATE_OUT="2.0",
    )
    from arcvisual.providers.poolside_provider import PoolsideProvider

    provider = PoolsideProvider()
    assert provider.capabilities().cost_attributed is True
    usage = provider._usage(
        {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}
    )
    assert usage.attributed is True
    assert usage.cost_usd == pytest.approx(1.0 + 1.0)


def test_capabilities_are_not_oversold(reset_settings) -> None:
    """Only Anthropic gets to claim constrained output and a prompt cache."""
    reset_settings(POOLSIDE_API_KEY="ps-test", ANTHROPIC_API_KEY="sk-test")
    from arcvisual.providers.anthropic_provider import AnthropicProvider
    from arcvisual.providers.poolside_provider import PoolsideProvider

    anthropic_caps = AnthropicProvider(client=object()).capabilities()
    assert anthropic_caps.native_structured_output and anthropic_caps.prompt_cache

    poolside_caps = PoolsideProvider(client=object()).capabilities()
    assert not poolside_caps.native_structured_output
    assert not poolside_caps.prompt_cache


# --------------------------------------------------------------------------- #
# Tolerant JSON extraction — the prompted-JSON path's load-bearing part
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        '{"name": "a", "count": 1}',
        'Here you go:\n```json\n{"name": "a", "count": 1}\n```\nHope that helps!',
        '```\n{"name": "a", "count": 1}\n```',
        'Sure.\n{"name": "a", "count": 1}\nLet me know if you need more.',
        '{"name": "a", "count": 1,}',  # trailing comma
    ],
)
def test_extracts_json_from_messy_output(raw: str) -> None:
    payload = extract_json(raw)
    assert payload is not None, raw
    assert json.loads(payload)["name"] == "a"


def test_brace_scan_respects_string_contents() -> None:
    """A naive brace counter would stop at the ``}`` inside the string value and
    return truncated, unparsable JSON."""
    raw = 'Result:\n{"name": "a {not a brace}", "count": 1}\ndone'
    payload = extract_json(raw)
    assert payload is not None
    assert json.loads(payload) == {"name": "a {not a brace}", "count": 1}


def test_brace_scan_respects_escaped_quotes() -> None:
    raw = r'{"name": "quote \" then }", "count": 2}'
    payload = extract_json(raw)
    assert payload is not None
    assert json.loads(payload)["count"] == 2


@pytest.mark.parametrize("raw", ["", "no json at all", "{unclosed: ", "[1, 2, 3]"])
def test_returns_none_rather_than_guessing(raw: str) -> None:
    """A "nearly JSON" match would parse into a wrong-but-valid object, and a wrong
    object silently produces a wrong animation. None is the honest answer."""
    assert extract_json(raw) is None


def test_validation_findings_are_phrased_for_a_reprompt() -> None:
    value, findings = parse_into(Answer, '{"name": "a", "count": -5}')
    assert value is None
    assert any("count" in f for f in findings)


def test_missing_json_finding_tells_the_model_what_to_do() -> None:
    value, findings = parse_into(Answer, "I would rather explain it in prose.")
    assert value is None
    assert any("JSON only" in f or "no parsable JSON" in f for f in findings)


# --------------------------------------------------------------------------- #
# The prompted-JSON repair loop
# --------------------------------------------------------------------------- #


def test_prompted_json_succeeds_first_try() -> None:
    calls = []

    def send(system_text: str, user_text: str):
        calls.append(user_text)
        return '{"name": "ok", "count": 2}', Usage(attributed=True)

    result = prompted_structured(
        send=send,
        system=[TextBlock(text="role")],
        user="do it",
        output_model=Answer,
        provider="fake",
        model="fake-1",
    )
    assert result.ok and result.value.name == "ok"
    assert result.attempts == 1 and len(calls) == 1


def test_prompted_json_reprompts_once_with_the_error() -> None:
    """Without this, providers lacking constrained decoding would fail Analyze
    regularly on a stray fence or a trailing comma."""
    replies = ["I think the answer is 2.", '{"name": "ok", "count": 2}']
    seen: list[str] = []

    def send(system_text: str, user_text: str):
        seen.append(user_text)
        return replies.pop(0), Usage(input_tokens=10, attributed=True)

    result = prompted_structured(
        send=send,
        system=[TextBlock(text="role")],
        user="do it",
        output_model=Answer,
        provider="fake",
        model="fake-1",
    )
    assert result.ok and result.attempts == 2
    # The second prompt must carry the validation failure, or the retry is blind.
    assert "did not validate" in seen[1]
    assert result.usage.input_tokens == 20  # both calls billed


def test_prompted_json_gives_up_rather_than_looping() -> None:
    """repair.py owns the budgeted ladder above this; a second unbounded loop in
    here would make the per-scene cost ceiling unenforceable."""
    calls = 0

    def send(system_text: str, user_text: str):
        nonlocal calls
        calls += 1
        return "still prose", Usage(attributed=True)

    result = prompted_structured(
        send=send,
        system=[TextBlock(text="role")],
        user="do it",
        output_model=Answer,
        provider="fake",
        model="fake-1",
    )
    assert not result.ok
    assert calls == 2 and result.attempts == 2
    assert result.findings


def test_schema_is_included_in_the_system_prompt() -> None:
    captured: dict[str, str] = {}

    def send(system_text: str, user_text: str):
        captured["system"] = system_text
        return '{"name": "ok", "count": 0}', Usage(attributed=True)

    prompted_structured(
        send=send,
        system=[TextBlock(text="role text")],
        user="u",
        output_model=Answer,
        provider="fake",
        model="m",
    )
    assert "role text" in captured["system"]
    assert "JSON Schema" in captured["system"]
    assert '"count"' in captured["system"]


# --------------------------------------------------------------------------- #
# opencode event-stream parsing — pinned to the REAL v1.18.15 format
# --------------------------------------------------------------------------- #

#: One line per event; the answer lives at `part.text`, not at the top level.
#: Captured from an actual `opencode run --format json` invocation.
REAL_STREAM = "\n".join(
    [
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "text", "part": {"type": "text", "text": '{"name": "a", '}}),
        json.dumps({"type": "text", "part": {"type": "text", "text": '"count": 1}'}}),
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {"total": 28804, "input": 28780, "output": 12},
                    "cost": 0,
                },
            }
        ),
    ]
)


def test_reads_the_real_event_schema() -> None:
    """An earlier parser looked for `text` on the top-level event and found nothing,
    because opencode nests it under `part`. It silently returned "" on every call."""

    value, _ = parse_into(Answer, extract_assistant_text(REAL_STREAM))
    assert value is not None and value.name == "a" and value.count == 1


def test_usage_comes_from_step_finish() -> None:
    """opencode DOES report tokens and cost, so spend is attributable — the earlier
    claim that it was not came from never having looked at the stream."""
    from arcvisual.providers.opencode_provider import stream_usage

    usage = stream_usage(REAL_STREAM)
    assert usage.input_tokens == 28780
    assert usage.output_tokens == 12
    assert usage.attributed is True, "a reported cost of 0 is a real 0, not unknown"
    assert usage.cost_usd == 0.0


def test_api_errors_are_detected_despite_exit_code_zero() -> None:
    """An insufficient-balance 401 arrives as a normal event with exit status 0.
    Checking only the return code would treat it as a successful empty answer."""
    from arcvisual.providers.opencode_provider import stream_error

    stream = json.dumps(
        {
            "type": "error",
            "error": {
                "name": "APIError",
                "data": {"message": "Insufficient balance.", "statusCode": 401},
            },
        }
    )
    failure = stream_error(stream)
    assert failure is not None and "Insufficient balance" in failure
    assert stream_error(REAL_STREAM) is None


def test_tool_and_reasoning_parts_are_skipped() -> None:
    """A tool's output must never be mistaken for the assistant's answer."""

    stream = "\n".join(
        [
            json.dumps(
                {"type": "tool", "part": {"type": "tool", "text": '{"name":"WRONG"}'}}
            ),
            json.dumps(
                {"type": "text", "part": {"type": "reasoning", "text": "thinking"}}
            ),
            json.dumps(
                {"type": "text", "part": {"type": "text", "text": '{"name":"right"}'}}
            ),
        ]
    )
    assert extract_assistant_text(stream) == '{"name":"right"}'


def test_unrecognised_shape_still_yields_its_text() -> None:
    """The stream format carries no stability promise, so a renamed field must not
    cost a call that was already paid for."""

    stream = json.dumps({"weird": {"nested": {"text": '{"name": "a", "count": 1}'}}})
    assert extract_json(extract_assistant_text(stream)) is not None


def test_empty_stdout_is_empty_not_an_exception() -> None:

    assert extract_assistant_text("") == ""
    assert extract_assistant_text("   \n ") == ""


def test_binary_is_resolved_to_a_full_path(reset_settings) -> None:
    """On Windows the entry point is a `.CMD` shim and CreateProcess does not apply
    PATHEXT, so a bare name passes `shutil.which` and then dies with WinError 2."""
    import shutil

    from arcvisual.providers.opencode_provider import OpencodeProvider

    reset_settings(ARCVISUAL_PROVIDER="opencode")
    if shutil.which("opencode") is None:
        pytest.skip("opencode is not installed here")
    provider = OpencodeProvider()
    assert provider._binary == shutil.which("opencode")
    assert provider._binary != "opencode" or "/" in provider._binary


# --------------------------------------------------------------------------- #
# Task-to-model mapping
# --------------------------------------------------------------------------- #


def test_anthropic_maps_tasks_to_the_plans_model_split(reset_settings) -> None:
    reset_settings(ANTHROPIC_API_KEY="sk-test")
    from arcvisual.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(client=object())
    # Cheap to classify, strong to write code — the plan's own split.
    assert "sonnet" in provider.model_for(Task.CLASSIFY)
    assert "opus" in provider.model_for(Task.CODEGEN)


def test_opencode_model_may_be_empty(reset_settings) -> None:
    """Selecting opencode delegates the model choice to opencode's own config
    rather than making it here."""
    reset_settings(ARCVISUAL_PROVIDER="opencode", OPENCODE_MODEL_CLASSIFY=None)
    from arcvisual.providers.opencode_provider import OpencodeProvider

    provider = OpencodeProvider(runner=object())
    assert provider.model_for(Task.CLASSIFY) == ""


# --------------------------------------------------------------------------- #
# Reasoning-model token budget (found by running the real pipeline)
# --------------------------------------------------------------------------- #


def test_poolside_budget_is_not_inherited_from_the_anthropic_number(
    reset_settings,
) -> None:
    """Poolside counts REASONING tokens against `max_tokens`.

    Measured on the real Transformer paper: a caller-supplied 8192 was spent
    entirely on reasoning (`reasoning_tokens: 8192`), returning `content: null` with
    `finish_reason: "length"` — a total failure that looks like a successful HTTP
    200. The provider must therefore floor the budget at its own configured value
    rather than honouring a number calibrated for a provider that budgets thinking
    separately.
    """
    reset_settings(ARCVISUAL_PROVIDER="poolside", POOLSIDE_API_KEY="ps-test")
    from arcvisual.config import settings as _settings

    assert _settings().poolside.max_tokens >= 32000

    seen: dict[str, int] = {}

    class _Client:
        def post(self, path, json):
            seen["max_tokens"] = json["max_tokens"]

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "choices": [{"message": {"content": '{"name":"a","count":1}'}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    }

            return _R()

    from arcvisual.providers.poolside_provider import PoolsideProvider

    PoolsideProvider(client=_Client()).structured(
        system=[TextBlock(text="s")],
        user="u",
        output_model=Answer,
        task=Task.CLASSIFY,
        max_tokens=8192,  # the Anthropic-tuned number
    )
    assert seen["max_tokens"] >= 32000, "an 8192 budget returns no answer at all"


def test_null_content_from_a_length_stop_names_the_real_cause() -> None:
    """`content: null` on an HTTP 200 sent an operator hunting the wrong layer.
    The error must name the budget, not the Python type."""
    from arcvisual.providers.base import ProviderError
    from arcvisual.providers.poolside_provider import _first_message

    body = {
        "choices": [{"message": {"content": None}, "finish_reason": "length"}],
        "usage": {
            "completion_tokens": 8192,
            "completion_tokens_details": {"reasoning_tokens": 8192},
        },
    }
    with pytest.raises(ProviderError, match="consumed by reasoning"):
        _first_message(body)


def test_reasoning_content_is_used_when_content_is_empty() -> None:
    """A reasoning-only reply still often contains the JSON; hand it to the tolerant
    extractor rather than discarding a call that was paid for."""
    from arcvisual.providers.poolside_provider import _first_message

    body = {
        "choices": [
            {
                "message": {"content": None, "reasoning_content": '{"name":"a"}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 12},
    }
    assert _first_message(body) == '{"name":"a"}'


# --------------------------------------------------------------------------- #
# Retry budget is a caller decision, not a constant
# --------------------------------------------------------------------------- #


def test_analyze_gets_more_attempts_than_codegen() -> None:
    """The cost of giving up differs by stage.

    A codegen failure degrades one scene and repair.py's ladder absorbs it. An
    analyze failure kills the whole paper — nothing sits above it. Giving both the
    same two attempts treated a single point of failure as recoverable, and on
    laguna (high but imperfect valid-JSON rate) one bad reply took a paper with it.
    """
    from arcvisual.config import Models

    assert Models().analyze_json_attempts > 2


def test_max_attempts_reaches_the_prompted_loop(reset_settings) -> None:
    reset_settings(ARCVISUAL_PROVIDER="poolside", POOLSIDE_API_KEY="ps-test")
    from arcvisual.providers.poolside_provider import PoolsideProvider

    calls = {"n": 0}

    class _Client:
        def post(self, path, json):
            calls["n"] += 1

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "choices": [{"message": {"content": "not json at all"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }

            return _R()

    result = PoolsideProvider(client=_Client()).structured(
        system=[TextBlock(text="s")],
        user="u",
        output_model=Answer,
        task=Task.CLASSIFY,
        max_attempts=4,
    )
    assert not result.ok
    assert calls["n"] == 4, "the caller's attempt budget must reach the retry loop"
    assert result.attempts == 4


def test_the_retry_budget_is_still_bounded(reset_settings) -> None:
    """Bounded, never unbounded — repair.py's per-scene cost ceiling depends on it."""
    reset_settings(ARCVISUAL_PROVIDER="poolside", POOLSIDE_API_KEY="ps-test")
    from arcvisual.providers.poolside_provider import PoolsideProvider

    calls = {"n": 0}

    class _Client:
        def post(self, path, json):
            calls["n"] += 1

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": "nope"}}], "usage": {}}

            return _R()

    PoolsideProvider(client=_Client()).structured(
        system=[TextBlock(text="s")],
        user="u",
        output_model=Answer,
        task=Task.CLASSIFY,
    )
    assert calls["n"] == 2, "default stays at two"


# --------------------------------------------------------------------------- #
# Retryable call failures — the gap the attempt budget did not cover
# --------------------------------------------------------------------------- #


def test_a_transient_call_failure_is_retried_not_raised() -> None:
    """The attempt budget only covered PARSE failures.

    `send()` raising escaped the loop entirely, so the failure that was actually
    killing papers — laguna spending its whole token budget on reasoning, which
    varies run to run — bypassed all four attempts and failed the job on the first
    dice roll.
    """
    from arcvisual.providers.base import RetryableProviderError

    calls = {"n": 0}

    def send(system_text: str, user_text: str):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableProviderError("budget consumed by reasoning")
        return '{"name": "ok", "count": 1}', Usage(attributed=True)

    result = prompted_structured(
        send=send,
        system=[TextBlock(text="s")],
        user="u",
        output_model=Answer,
        provider="fake",
        model="m",
        max_attempts=4,
    )
    assert result.ok, result.findings
    assert calls["n"] == 3


def test_a_permanent_failure_still_raises() -> None:
    """A 401 or a missing binary fails identically forever. Retrying only wastes
    time and money, so it must NOT be swallowed by the loop."""
    from arcvisual.providers.base import ProviderError

    def send(system_text: str, user_text: str):
        raise ProviderError("401 unauthorized")

    with pytest.raises(ProviderError, match="401"):
        prompted_structured(
            send=send,
            system=[TextBlock(text="s")],
            user="u",
            output_model=Answer,
            provider="fake",
            model="m",
            max_attempts=4,
        )


def test_exhausting_retries_on_a_transient_failure_reports_it() -> None:
    from arcvisual.providers.base import RetryableProviderError

    def send(system_text: str, user_text: str):
        raise RetryableProviderError("still no content")

    result = prompted_structured(
        send=send,
        system=[TextBlock(text="s")],
        user="u",
        output_model=Answer,
        provider="fake",
        model="m",
        max_attempts=3,
    )
    assert not result.ok
    assert any("still no content" in f for f in result.findings)

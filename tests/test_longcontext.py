"""The long-context provider: a whole paper in one request.

What these hold in place:

* the prompt budget comes from the CONTEXT WINDOW, so ANALYZE reads the paper in one
  pass instead of the ~5,000 characters a per-minute meter allows;
* dropping a preset's key into the environment is enough for ``auto`` to use it, and
  ``settings().offline`` agrees with the registry about that;
* a host that refuses or ignores the strict schema degrades to prompted JSON instead
  of failing the call — hosts implement strict mode against different subsets.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from arcvisual.config import LONG_CONTEXT_PRESETS, settings
from arcvisual.providers import registry
from arcvisual.providers.base import ProviderNotConfigured, Task, TextBlock


class Out(BaseModel):
    answer: str


class _Resp:
    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers: dict = {}

    def json(self):
        return self._body


def _ok(content: str) -> _Resp:
    return _Resp(
        200,
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


class _Http:
    """Replies in order and keeps every payload it was sent."""

    def __init__(self, *replies: _Resp):
        self.replies = list(replies)
        self.sent: list[dict] = []

    def post(self, _path: str, json: dict) -> _Resp:
        self.sent.append(json)
        return self.replies.pop(0)


@pytest.fixture(autouse=True)
def _fresh_schema_memory():
    from arcvisual.providers import longcontext_provider, openai_compat

    openai_compat._SCHEMA_REJECTED.clear()
    longcontext_provider._UNAVAILABLE.clear()
    yield
    openai_compat._SCHEMA_REJECTED.clear()
    longcontext_provider._UNAVAILABLE.clear()


def _provider(reset_settings, http: _Http, **env: str):
    from arcvisual.providers.longcontext_provider import LongContextProvider

    reset_settings(ARCVISUAL_PROVIDER="openrouter", OPENROUTER_API_KEY="k", **env)
    return LongContextProvider(client=http)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_auto_uses_a_preset_whose_key_is_present(reset_settings) -> None:
    reset_settings(ARCVISUAL_PROVIDER="auto", GEMINI_API_KEY="g")
    provider = registry.get_provider()
    assert provider is not None and provider.name == "gemini"
    assert settings().offline is False


def test_long_context_outranks_groq_in_auto(reset_settings) -> None:
    """Groq's model has a 131K window, but its free-tier meter sends 5,000 chars."""
    assert registry.AUTO_PREFERENCE.index("longcontext") < registry.AUTO_PREFERENCE.index(
        "groq"
    )
    reset_settings(ARCVISUAL_PROVIDER="auto", GROQ_API_KEY="q", MISTRAL_API_KEY="m")
    assert registry.get_provider().name == "mistral"


@pytest.mark.parametrize("preset", [p for p in LONG_CONTEXT_PRESETS if p != "custom"])
def test_offline_agrees_with_registry_for_every_preset(reset_settings, preset) -> None:
    reset_settings(
        ARCVISUAL_PROVIDER=preset, **{LONG_CONTEXT_PRESETS[preset].key_env: "k"}
    )
    assert settings().offline is False
    provider = registry.get_provider()
    assert provider is not None and provider.name == preset


def test_an_explicit_preset_without_its_key_is_an_error(reset_settings) -> None:
    reset_settings(ARCVISUAL_PROVIDER="deepseek", DEEPSEEK_API_KEY=None)
    assert settings().offline is True
    with pytest.raises(ProviderNotConfigured, match="DEEPSEEK_API_KEY"):
        registry.get_provider()


def test_a_custom_endpoint_needs_only_url_key_and_model(reset_settings) -> None:
    reset_settings(
        ARCVISUAL_PROVIDER="longcontext",
        LLM_BASE_URL="https://llm.example/v1/",
        LLM_API_KEY="k",
        LLM_MODEL_CLASSIFY="my-model",
        LLM_CONTEXT_TOKENS="262144",
    )
    provider = registry.get_provider()
    assert provider.name == "custom"
    assert provider.model_for(Task.CODEGEN) == "my-model"
    assert settings().long_context.base_url == "https://llm.example/v1"


# --------------------------------------------------------------------------- #
# Budget: the point of the provider
# --------------------------------------------------------------------------- #


def test_the_whole_paper_fits_in_one_analyze_pass(reset_settings) -> None:
    """The real Transformer paper is ~78,000 characters; Groq's free tier sends 5,000."""
    provider = _provider(reset_settings, _Http())
    caps = provider.capabilities()
    assert caps.prompt_char_budget >= 200_000
    assert caps.analysis_passes == 1
    # Bounded, so the reply (and with it the call) fits inside one serverless step.
    assert caps.analysis_item_budget == 12
    assert caps.prompt_char_budget > settings().groq.prompt_char_budget * 20


def test_the_budget_scales_with_the_context_window(reset_settings) -> None:
    reset_settings(
        ARCVISUAL_PROVIDER="openrouter",
        OPENROUTER_API_KEY="k",
        LLM_CONTEXT_TOKENS="262144",
    )
    big = settings().long_context.prompt_char_budget
    reset_settings(LLM_CONTEXT_TOKENS="131072")
    small = settings().long_context.prompt_char_budget
    assert big > small * 1.9


def test_reasoning_models_get_room_to_answer(reset_settings) -> None:
    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    provider.structured(
        system=[TextBlock("s")],
        user="u",
        output_model=Out,
        task=Task.CLASSIFY,
        max_tokens=8192,
    )
    assert http.sent[0]["max_tokens"] >= 16384


# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #


def test_openrouter_routes_only_to_hosts_that_honour_the_schema(reset_settings) -> None:
    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok
    sent = http.sent[0]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["provider"] == {"require_parameters": True}


def test_a_refused_schema_falls_back_to_prompted_json(reset_settings) -> None:
    """Gemini answers 400 INVALID_ARGUMENT for schema shapes it does not support."""
    http = _Http(
        _Resp(400, '{"error":{"status":"INVALID_ARGUMENT","message":"bad schema"}}'),
        _ok('Sure: {"answer": "ok"}'),
    )
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok and result.value.answer == "ok"
    assert "response_format" not in http.sent[1]


def test_an_ignored_schema_falls_back_to_prompted_json(reset_settings) -> None:
    """A host that silently ignores response_format returns prose; that must cost a
    reprompt, not the whole paper's analysis."""
    http = _Http(_ok("I think the answer is ok."), _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok
    assert result.usage.input_tokens == 200, "the failed constrained call was billed too"


def test_deepseek_is_prompted_rather_than_constrained(reset_settings) -> None:
    from arcvisual.providers.longcontext_provider import LongContextProvider

    reset_settings(ARCVISUAL_PROVIDER="deepseek", DEEPSEEK_API_KEY="k")
    http = _Http(_ok('{"answer": "ok"}'))
    result = LongContextProvider(client=http).structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok
    assert "response_format" not in http.sent[0]


def test_a_retired_model_moves_to_the_next_in_the_chain(reset_settings) -> None:
    gone = _Resp(404, '{"error":{"code":"model_not_found","message":"no such model"}}')
    http = _Http(gone, _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http, LLM_MODEL_CLASSIFY="old/model,new/model")
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok and result.model == "new/model"


def test_cost_is_unattributed_until_rates_are_set(reset_settings) -> None:
    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.usage.attributed is False and result.usage.cost_usd == 0.0

    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http, LLM_RATE_IN="1.0", LLM_RATE_OUT="2.0")
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert result.usage.attributed is True
    assert result.usage.cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)


# --------------------------------------------------------------------------- #
# Nemotron on OpenRouter's free endpoint
# --------------------------------------------------------------------------- #


def test_openrouter_leads_with_nemotron_and_asks_it_to_reason(reset_settings) -> None:
    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    assert provider.model_for(Task.CODEGEN) == "nvidia/nemotron-3.5-lightning:free"
    assert provider.model_for(Task.CLASSIFY) == "nvidia/nemotron-3.5-lightning:free"
    provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert http.sent[0]["model"] == "nvidia/nemotron-3.5-lightning:free"
    assert http.sent[0]["reasoning"] == {"enabled": True}


def test_analysis_does_not_reason_unless_asked(reset_settings) -> None:
    """Measured on the free endpoint: 112s with reasoning, 30s without, for the same
    analysis. With reasoning the real one outran the 300s function limit."""
    http = _Http(_ok('{"answer": "ok"}'), _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert "reasoning" not in http.sent[0]

    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http, LLM_REASONING_ANALYZE="1")
    provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CLASSIFY
    )
    assert http.sent[0]["reasoning"] == {"enabled": True}


def test_reasoning_can_be_turned_off(reset_settings) -> None:
    http = _Http(_ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http, LLM_REASONING="0")
    provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert "reasoning" not in http.sent[0]


def test_reasoning_is_openrouter_only(reset_settings) -> None:
    from arcvisual.providers.longcontext_provider import LongContextProvider

    reset_settings(ARCVISUAL_PROVIDER="mistral", MISTRAL_API_KEY="k", LLM_REASONING="1")
    http = _Http(_ok('{"answer": "ok"}'))
    LongContextProvider(client=http).structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert "reasoning" not in http.sent[0]


NO_SCHEMA_HOST = _Resp(
    404,
    '{"error":{"message":"No endpoints found that can handle the requested '
    'parameters.","code":404}}',
)
NO_HOST_AT_ALL = _Resp(
    404,
    '{"error":{"message":"No endpoints found for nvidia/nemotron-3.5-lightning:free.",'
    '"code":404}}',
)


def test_a_free_endpoint_without_schema_support_is_prompted(reset_settings) -> None:
    """require_parameters + a free host that lacks response_format = a 404, not an
    answer. The schema goes in the prompt instead, on the same model."""
    http = _Http(NO_SCHEMA_HOST, _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert result.ok and result.model == "nvidia/nemotron-3.5-lightning:free"
    assert "response_format" not in http.sent[1]
    assert "provider" not in http.sent[1]


def test_a_withdrawn_free_endpoint_falls_through_to_the_next_model(
    reset_settings,
) -> None:
    http = _Http(NO_SCHEMA_HOST, NO_HOST_AT_ALL, _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert result.ok and result.model == "qwen/qwen3-235b-a22b-2507"


FREE_DAILY_QUOTA = _Resp(
    429,
    '{"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits '
    'to unlock 1000 free model requests per day","code":429}}',
)


def test_a_spent_free_quota_falls_through_to_the_paid_model(reset_settings) -> None:
    """Retrying a daily cap cannot succeed until tomorrow; the paid models behind the
    free one are the only way the paper still gets explained today."""
    http = _Http(FREE_DAILY_QUOTA, _ok('{"answer": "ok"}'))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert result.ok and result.model == "qwen/qwen3-235b-a22b-2507"
    # Remembered for the rest of the process: the next call skips the spent model.
    assert provider.model_for(Task.CLASSIFY) == "qwen/qwen3-235b-a22b-2507"


def test_a_paid_models_daily_limit_is_not_mistaken_for_a_free_quota(
    reset_settings,
) -> None:
    from arcvisual.providers.base import ProviderError

    http = _Http(_Resp(429, '{"error":{"message":"tokens per day (TPD): Limit 1"}}'))
    provider = _provider(reset_settings, http, LLM_MODEL_CODEGEN="paid/model")
    with pytest.raises(ProviderError, match="daily token quota"):
        provider.structured(
            system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
        )


# --------------------------------------------------------------------------- #
# A call must end before the function does
# --------------------------------------------------------------------------- #


class _Stream:
    """An httpx streaming response: SSE lines, optionally slow, optionally stalled.

    ``stall_s`` blocks before the first line the way a silent host does; like a
    real socket, the block ends early when the stream is closed from elsewhere.
    """

    def __init__(
        self,
        lines: list[str],
        status: int = 200,
        delay_s: float = 0.0,
        stall_s: float = 0.0,
    ):
        import threading

        self.status_code = status
        self.headers: dict = {}
        self._lines = lines
        self._delay = delay_s
        self._stall = stall_s
        self._closed = threading.Event()
        self.text = "\n".join(lines)

    def close(self):
        self._closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.text.encode()

    def json(self):
        return json.loads(self.text)

    def iter_lines(self):
        import time

        if self._stall and self._closed.wait(self._stall):
            raise OSError("stream closed while reading")
        for line in self._lines:
            if self._delay:
                time.sleep(self._delay)
            yield line


class _StreamingHttp:
    def __init__(self, *streams: _Stream):
        self.streams = list(streams)
        self.sent: list[dict] = []

    def stream(self, method: str, path: str, json: dict):
        self.sent.append(json)
        return self.streams.pop(0)


def _sse(content: str) -> list[str]:
    chunks = [content[i : i + 5] for i in range(0, len(content), 5)]
    lines = [": OPENROUTER PROCESSING"]
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append(
        "data: "
        + json.dumps(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        )
    )
    lines.append("data: [DONE]")
    return lines


def test_a_streamed_reply_is_assembled_and_billed(reset_settings) -> None:
    http = _StreamingHttp(_Stream(_sse('{"answer": "ok"}')))
    provider = _provider(reset_settings, http)
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert result.ok and result.value.answer == "ok"
    assert http.sent[0]["stream"] is True
    assert result.usage.input_tokens == 7 and result.usage.output_tokens == 3


def test_a_too_slow_free_model_hands_over_to_the_next(reset_settings) -> None:
    """The failure that stalled a real job for 83 minutes: a slow call ran past the
    function limit. Bounded by wall clock, it now ends in time, and a free model that
    slow is passed over for the paid one behind it."""
    slow = _Stream(_sse('{"answer": "too late"}'), delay_s=0.05)
    fast = _Stream(_sse('{"answer": "ok"}'))
    http = _StreamingHttp(slow, fast)
    provider = _provider(
        reset_settings, http, LLM_CALL_TIMEOUT_S="0.1", LLM_JSON_SCHEMA="0"
    )
    import arcvisual.providers.longcontext_provider as lc

    old, lc._MIN_CALL_S = lc._MIN_CALL_S, 0.0
    try:
        result = provider.structured(
            system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
        )
    finally:
        lc._MIN_CALL_S = old
    assert result.ok and result.model == "qwen/qwen3-235b-a22b-2507"


def test_no_call_starts_when_the_step_is_out_of_time(reset_settings) -> None:
    import time

    from arcvisual.providers.base import ProviderError, RetryableProviderError

    http = _StreamingHttp(_Stream(_sse('{"answer": "ok"}')))
    provider = _provider(reset_settings, http)
    provider.set_deadline(time.monotonic() + 1.0)
    with pytest.raises(ProviderError, match="too little for another model call") as info:
        provider.structured(
            system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
        )
    # Not retryable: a retry would start with even less time.
    assert not isinstance(info.value, RetryableProviderError)
    assert http.sent == []


def test_an_error_inside_the_stream_is_reported_with_its_status(reset_settings) -> None:
    quota = _Stream(
        [
            "data: "
            + json.dumps(
                {
                    "error": {
                        "code": 429,
                        "message": "Rate limit exceeded: free-models-per-day",
                    }
                }
            )
        ]
    )
    http = _StreamingHttp(quota, _Stream(_sse('{"answer": "ok"}')))
    provider = _provider(reset_settings, http, LLM_JSON_SCHEMA="0")
    result = provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert result.ok and result.model == "qwen/qwen3-235b-a22b-2507"


def test_a_silent_stream_is_cut_off_at_the_budget(reset_settings) -> None:
    """A host that accepts the request and then sends nothing blocks the read
    itself; the elapsed check between lines never runs. The watchdog closes the
    stream at the budget instead of waiting out the 300s read timeout."""
    import time

    import arcvisual.providers.longcontext_provider as lc

    stalled = _Stream(_sse('{"answer": "never"}'), stall_s=30)
    http = _StreamingHttp(stalled, _Stream(_sse('{"answer": "ok"}')))
    provider = _provider(
        reset_settings, http, LLM_CALL_TIMEOUT_S="0.3", LLM_JSON_SCHEMA="0"
    )
    old, lc._MIN_CALL_S = lc._MIN_CALL_S, 0.0
    started = time.monotonic()
    try:
        result = provider.structured(
            system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
        )
    finally:
        lc._MIN_CALL_S = old
    assert time.monotonic() - started < 5, "the stalled read was not cut off"
    assert result.ok and result.model == "qwen/qwen3-235b-a22b-2507"


def test_openrouter_streams_ask_for_usage(reset_settings) -> None:
    """Usage on an OpenRouter stream is opt-in; without it a call bills as zero."""
    http = _StreamingHttp(_Stream(_sse('{"answer": "ok"}')))
    provider = _provider(reset_settings, http)
    provider.structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert http.sent[0]["stream_options"] == {"include_usage": True}


def test_other_hosts_are_not_sent_openrouter_stream_options(reset_settings) -> None:
    from arcvisual.providers.longcontext_provider import LongContextProvider

    reset_settings(ARCVISUAL_PROVIDER="mistral", MISTRAL_API_KEY="k")
    http = _StreamingHttp(_Stream(_sse('{"answer": "ok"}')))
    LongContextProvider(client=http).structured(
        system=[TextBlock("s")], user="u", output_model=Out, task=Task.CODEGEN
    )
    assert "stream_options" not in http.sent[0]

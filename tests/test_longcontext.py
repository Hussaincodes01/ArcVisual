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
    assert caps.analysis_item_budget is None
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

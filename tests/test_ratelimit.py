"""The per-minute token budget, and the 413/429 handling built on it.

These exist because the failure they guard is invisible in a single-threaded test:
lanes each got their own meter, so four lanes each believed they owned the whole
8,000 TPM and collectively asked for 32,000.
"""

from __future__ import annotations

import threading
import time

import pytest

from arcvisual.providers.base import ProviderError, RetryableProviderError
from arcvisual.providers.openai_compat import OpenAICompatProvider
from arcvisual.providers.ratelimit import (
    TokenBudget,
    _parse_duration,
    retry_after_seconds,
)


class _Resp:
    def __init__(
        self, status: int, headers: dict | None = None, body: dict | None = None
    ):
        self.status_code = status
        self.headers = headers or {}
        self._body = body or {}
        self.text = str(self._body)

    def json(self) -> dict:
        return self._body


class _FakeClient:
    """Records payloads and replays canned responses."""

    def __init__(self, responses: list[_Resp]):
        self.responses = responses
        self.payloads: list[dict] = []

    def post(self, _path: str, json: dict) -> _Resp:
        self.payloads.append(json)
        return self.responses.pop(0)


class _Prov(OpenAICompatProvider):
    name = "fake"

    def __init__(self, client, meter=None, effort="", budget_field="max_tokens"):
        self._http = client
        self._meter = meter
        self._effort = effort
        self._field = budget_field

    def _base_url(self):
        return "http://x"

    def _api_key(self):
        return "k"

    def _client(self):
        return self._http

    # Model-aware, matching the base class: the allowance, the schema guarantee and
    # the reasoning parameter all differ per model on a single Groq key.
    def _token_budget(self, model: str = ""):
        return self._meter

    def _reasoning_effort(self, model: str = ""):
        return self._effort

    def _budget_field(self):
        return self._field


# -- the meter ------------------------------------------------------------- #


def test_clamp_keeps_the_ask_under_the_ceiling() -> None:
    """The bug this prevents: Groq charges the *requested* budget on arrival, so
    asking 8192 of an 8000 TPM key is a certain 413, not a long answer."""
    budget = TokenBudget(tpm=8000)
    # 700 estimated -> 805 padded + 256 headroom = 1061 held back, against a
    # spendable 7,200 (90% of 8,000), leaving 6,140. The safety margin lives here, in
    # the size of the ask, rather than in the bucket's capacity — a bucket smaller
    # than the server's allowance under-counts requests sized near that allowance.
    assert budget.clamp(8192, prompt_tokens=700) == 6140
    assert budget.clamp(1000, prompt_tokens=700) == 1000  # already fits: untouched
    assert budget.capacity == 8000, "the bucket must hold the server's full allowance"


def test_clamp_pads_an_optimistic_prompt_estimate() -> None:
    """Callers estimate at ~4 chars/token, which runs low against a real tokeniser: a
    5,385-token estimate was counted as ~6,217 and the request was refused for
    exceeding an 8,000 ceiling. Underestimating costs a 413; overestimating costs a
    slightly shorter reply."""
    budget = TokenBudget(tpm=8000)
    granted = budget.clamp(4000, prompt_tokens=5385)
    assert granted + int(5385 * 1.10) < 8000, "must survive a 10% tokeniser undercount"


def test_clamp_is_a_noop_when_metering_is_disabled() -> None:
    assert TokenBudget(tpm=0).clamp(99999) == 99999


def test_settle_refunds_the_unspent_reservation() -> None:
    """Reservations are made against max_tokens, an upper bound the model rarely
    reaches. Without the refund the meter throttles several times harder than the
    provider actually does."""
    budget = TokenBudget(tpm=8000)
    start = budget._tokens
    reserved = budget.acquire(6000)
    assert budget._tokens < start
    budget.settle(reserved, 1284)
    # Only what was really used stays spent.
    assert budget._tokens == pytest.approx(start - 1284, abs=50)


def test_one_meter_is_shared_across_lanes() -> None:
    """Four lanes must not each spend the whole budget."""
    budget = TokenBudget(tpm=4000)
    got: list[int] = []
    barrier = threading.Barrier(4)

    def lane() -> None:
        barrier.wait()
        got.append(budget.acquire(1000, timeout_s=5))

    threads = [threading.Thread(target=lane) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(got) == 4000
    # The bucket, not four independent copies of it, absorbed all four reservations.
    assert budget._tokens < budget.capacity


def test_acquire_waits_rather_than_overspending() -> None:
    """A drained bucket makes the next caller wait for refill, not overspend."""
    budget = TokenBudget(tpm=1000, window_s=1.0)
    budget.acquire(budget.capacity)  # drain it
    started = time.monotonic()
    budget.acquire(400, timeout_s=5)
    waited = time.monotonic() - started
    # 400 tokens at 1000/second-window = ~0.4s of refill.
    assert waited >= 0.25, f"returned in {waited:.2f}s without waiting for refill"


def test_server_headers_override_local_accounting() -> None:
    """The server also counts usage this process cannot see — another session on the
    same key — so its number wins."""
    budget = TokenBudget(tpm=8000)
    budget.observe({"x-ratelimit-remaining-tokens": "100"})
    # Projected forward from the reading, so slightly above 100 but nowhere near the
    # local belief of a full bucket.
    assert budget._available(time.monotonic()) < 200


@pytest.mark.parametrize(
    "text,expected",
    [("12", 12.0), ("39.937s", 39.937), ("7m12s", 432.0), ("1h2m3s", 3723.0)],
)
def test_parse_duration_handles_groqs_formats(text: str, expected: float) -> None:
    assert _parse_duration(text) == pytest.approx(expected)


def test_retry_after_prefers_the_servers_guidance_and_caps_it() -> None:
    assert retry_after_seconds({"retry-after": "5"}) == 5.0
    assert retry_after_seconds({"x-ratelimit-reset-tokens": "217ms"}) == pytest.approx(
        0.217
    )
    # 7m12s is real, but a TPM allowance refills continuously; sleeping that long
    # is never the right answer and once turned a minutes-long job into two hours.
    assert retry_after_seconds({"x-ratelimit-reset-tokens": "7m12s"}) == 20.0
    assert retry_after_seconds({}, default=2.0) == 2.0


def test_retry_after_ignores_the_daily_request_quota() -> None:
    """`x-ratelimit-reset-requests` reports the DAILY request budget — observed at
    "43m12s" with 970 of 1000 left. Treating it as a token-limit backoff would idle
    every lane for the full 60s cap over a limit that was not the one we hit."""
    assert (
        retry_after_seconds({"x-ratelimit-reset-requests": "43m12s"}, default=3.0) == 3.0
    )


# -- the provider ----------------------------------------------------------- #


def _ok_body(text: str = "{}") -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


def test_413_explains_the_fix_instead_of_echoing_the_body() -> None:
    """413 is not retryable: an identical retry fails identically, so the message
    has to say what to change."""
    client = _FakeClient([_Resp(413, body={"error": "Request too large"})])
    with pytest.raises(ProviderError) as excinfo:
        _Prov(client)._chat("m", "sys", "user", 8192)
    message = str(excinfo.value)
    assert "too large" in message
    assert "GROQ_MAX_TOKENS" in message, "must name the knob that fixes it"


def test_429_drains_the_bucket_rather_than_refunding_it() -> None:
    """Refunding would be worse than useless here: every lane waiting behind the
    refused request would wake, see capacity, and fire into the same limit."""
    meter = TokenBudget(tpm=8000)
    client = _FakeClient([_Resp(429, headers={"retry-after": "0"})])
    with pytest.raises(RetryableProviderError):
        _Prov(client, meter=meter)._chat("m", "s", "u", 1000)
    assert meter._tokens == 0.0, "a 429 must leave the bucket empty, not restored"


def test_transport_failure_refunds_too() -> None:
    class Boom:
        def post(self, *_a, **_k):
            raise TimeoutError("reset")

    meter = TokenBudget(tpm=8000)
    before = meter._tokens
    with pytest.raises(RetryableProviderError):
        _Prov(Boom(), meter=meter)._chat("m", "s", "u", 1000)
    # Nothing reached the server, so nothing was spent.
    assert meter._tokens == pytest.approx(before, abs=50)


def test_reasoning_effort_is_omitted_when_empty() -> None:
    """A model that does not accept the parameter rejects the whole request with
    400, so absence has to mean absence."""
    client = _FakeClient([_Resp(200, body=_ok_body())])
    _Prov(client, effort="")._chat("m", "s", "u", 100)
    assert "reasoning_effort" not in client.payloads[0]

    client = _FakeClient([_Resp(200, body=_ok_body())])
    _Prov(client, effort="none")._chat("m", "s", "u", 100)
    assert client.payloads[0]["reasoning_effort"] == "none"


def test_budget_field_is_configurable() -> None:
    client = _FakeClient([_Resp(200, body=_ok_body())])
    _Prov(client, budget_field="max_completion_tokens")._chat("m", "s", "u", 100)
    assert client.payloads[0]["max_completion_tokens"] == 100
    assert "max_tokens" not in client.payloads[0]


def test_success_settles_against_real_usage() -> None:
    meter = TokenBudget(tpm=8000)
    before = meter._tokens
    client = _FakeClient([_Resp(200, body=_ok_body())])
    _Prov(client, meter=meter)._chat("m", "s", "u", 4000)
    spent = before - meter._tokens
    assert spent == pytest.approx(300, abs=50), (
        "100 prompt + 200 completion should be charged, not the 4000 reserved"
    )


def test_source_sha_is_safe_under_concurrency() -> None:
    """`inspect.getsource` reads the global linecache and parses with `ast`; called
    from several scene lanes at once it raises

        SystemError: AST constructor recursion depth mismatch (before=30, after=26)

    intermittently - about one run in six, always passing in isolation, which made it
    look like flakiness rather than a bug.

    Note what this test had to do to be worth having. `functools.lru_cache` alone
    looks like a fix and is not: it makes the cache dict thread-safe but does not
    serialise the wrapped call, so on a cold cache every thread misses and every
    thread enters `getsource` together. A weaker version of this test passed against
    that non-fix, because one warm entry hides the race. So: many distinct objects,
    repeated rounds, and a cold cache at the start of each - enough concurrent misses
    that an unsynchronised implementation fails reliably rather than occasionally.
    """
    import contextlib
    import importlib

    from arcvisual.cache import hashing
    from arcvisual.cache.hashing import source_sha

    # Distinct objects => distinct cache keys => every thread takes the slow path.
    targets: list[object] = [hashing, source_sha.__class__]
    for name in ("arcvisual.templates.base", "arcvisual.storyboard", "arcvisual.config"):
        with contextlib.suppress(ImportError):  # pragma: no cover
            targets.append(importlib.import_module(name))
    from arcvisual.templates.base import ArcSceneMixin

    targets.append(ArcSceneMixin)

    errors: list[BaseException] = []
    digests: dict[int, set[str]] = {}
    lock = threading.Lock()

    for _ in range(4):  # several cold-start rounds
        source_sha.cache_clear()
        barrier = threading.Barrier(8)

        def worker(barrier: threading.Barrier = barrier) -> None:
            try:
                barrier.wait()
                for target in targets:
                    value = source_sha(target)
                    with lock:
                        digests.setdefault(id(target), set()).add(value)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            break

    assert not errors, f"concurrent source_sha raised: {errors[:3]}"
    disagreeing = {k: v for k, v in digests.items() if len(v) != 1}
    assert not disagreeing, (
        f"same object hashed differently across threads: {disagreeing}"
    )


def test_build_prompt_actually_applies_the_char_budget() -> None:
    """`trim_body` existed, was documented, was unit-tested, and was never called.

    `build_prompt` accepted `char_budget` and ignored it, so every provider received
    the whole paper however small its ceiling. On Groq that is a hard 413 before a
    token is generated; on Poolside it is the intermittent `content: null` that was
    read as provider flakiness for weeks. The unit test on `trim_body` passed
    throughout, because it tested the function rather than its use.
    """
    from arcvisual.analyze.prompts import build_prompt
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.test_review_fixes import metadata, paper_tarball

    sb = build_storyboard(metadata(), paper_tarball())
    full_blocks, _ = build_prompt(sb, None)
    full = sum(len(b.text) for b in full_blocks)

    # Below the fixture paper's own size, so the ceiling genuinely binds. The real
    # ratio is harsher still: a 78,000-char paper against a 14,000-char budget.
    budget = 3000
    trimmed_blocks, _ = build_prompt(sb, budget)
    trimmed = sum(len(b.text) for b in trimmed_blocks)

    assert trimmed < full, "the budget must actually shrink the prompt"
    # Generous headroom over the budget for the system role, title and abstract, but
    # far below the untrimmed size — the point is that the ceiling binds at all.
    assert trimmed <= budget * 3, f"prompt {trimmed} chars ignores a {budget} budget"

    # And the no-op direction: a budget larger than the paper must change nothing,
    # or every small paper would be needlessly truncated.
    roomy, _ = build_prompt(sb, 10_000_000)
    assert sum(len(b.text) for b in roomy) == full


def test_harden_schema_drops_cosmetic_constraints_but_keeps_meaningful_ones() -> None:
    """A strict validator turns `maxLength` into a whole-response rejection.

    Observed on a real call: a complete, valid analysis with four concepts was
    discarded with 400 because `/opportunities/0/justification` ran past 300
    characters — a limit every one of those fields already enforces client-side with a
    `_clamp` validator. `minLength` is different in kind: a too-short quote is a
    grounding failure that truncation cannot repair, so it must still bind.
    """
    import json

    from arcvisual.analyze.prompts import AnalysisOut
    from arcvisual.providers.openai_compat import _harden_schema

    schema = AnalysisOut.model_json_schema()
    assert "maxLength" in json.dumps(schema), "fixture assumption: Pydantic emits it"

    _harden_schema(schema)
    text = json.dumps(schema)

    assert "maxLength" not in text, (
        "cosmetic: the client clamps it, so it must not reject"
    )
    assert "pattern" not in text
    assert "minLength" in text, "structural: grounding needs a real quote"
    assert "enum" in text, "structural: the archetype taxonomy is a closed set"
    assert "maxItems" in text, "structural: bounds cost"

    # And the tightening half still happened.
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_clamp_still_enforces_the_length_client_side() -> None:
    """Dropping `maxLength` from the wire schema is only safe because Pydantic
    truncates on the way in. If that stopped being true, over-long prose would reach
    the storyboard instead of being rejected — worse than the 400 it replaced."""
    from arcvisual.analyze.prompts import OpportunityOut

    opportunity = OpportunityOut(
        archetype="transform_chain",
        claim="c" * 500,
        concept_name="n" * 200,
        section_id="s001",
        quote="q" * 900,
        justification="j" * 900,
        difficulty=3,
        centrality=0.5,
    )
    assert len(opportunity.justification) == 300
    assert len(opportunity.claim) == 280
    assert len(opportunity.quote) == 600
    assert len(opportunity.concept_name) == 60


def test_trim_body_respects_the_budget_it_is_given() -> None:
    """The floor that protects short sections must not override the budget.

    With 25 sections a flat 400-character floor guarantees 10,000 characters however
    small the budget: a 5,000-char budget was sending 11,212. On a metered key that
    is not a rounding error — prompt and reply share one allowance, so an oversized
    prompt starves the reply until constrained decoding stops mid-object and the whole
    analysis is lost.
    """
    from arcvisual.analyze.prompts import trim_body
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.test_review_fixes import metadata, paper_tarball

    sb = build_storyboard(metadata(), paper_tarball())
    analysable = [s for s in sb.sections if s.raw.strip()]

    for budget in (2000, 5000):
        text, _ = trim_body(sb, budget)
        assert len(text) <= budget * 2.5, (
            f"budget {budget} produced {len(text)} chars; the floor is overriding it"
        )
        # Every section must still be represented, or ideas vanish silently.
        for section in analysable:
            assert section.heading in text, f"{section.id} vanished at budget {budget}"


def test_harden_schema_leaves_free_form_maps_open() -> None:
    """`additionalProperties: false` on an object with no declared properties seals it
    against everything — the opposite of what the field means.

    `ParamsOut.params` is exactly that: an untyped dict holding whatever the chosen
    template's parameters happen to be. Sealed empty, constrained decoding cannot emit
    a single parameter key and the call burns its entire output budget failing to
    build a valid object — which surfaced as codegen "truncating" at 3,000 tokens and
    again at 4,500, as though it merely needed more room.
    """
    from arcvisual.analyze.prompts import AnalysisOut, ParamsOut
    from arcvisual.providers.openai_compat import _harden_schema

    schema = ParamsOut.model_json_schema()
    _harden_schema(schema)
    params = schema["properties"]["params"]
    assert params.get("additionalProperties") is not False, (
        "a free-form parameter map must not be sealed against all keys"
    )

    # The tightening must still apply where there ARE properties to tighten.
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"params", "beats", "scrubbable"}

    analysis = AnalysisOut.model_json_schema()
    _harden_schema(analysis)
    assert analysis["additionalProperties"] is False
    assert set(analysis["required"]) == set(analysis["properties"])


def test_daily_quota_is_not_retried_as_if_it_were_per_minute() -> None:
    """A per-day quota needs the opposite response to a per-minute one.

    Groq reports it ONLY in the error body — the headers keep advertising a healthy
    `x-ratelimit-remaining-tokens: 8000`, which is the minute allowance. Retrying
    against it spends the little that remains and reports "rate-limited" when the
    real answer is "this key is finished for today", with a reset measured in minutes
    to hours ("Please try again in 15m35.28s").
    """
    from arcvisual.providers.ratelimit import daily_limit_detail, is_daily_limit

    daily = (
        '{"error":{"message":"Rate limit reached for model `qwen/qwen3.6-27b` in '
        "organization `org_x` service tier `on_demand` on tokens per day (TPD): "
        'Limit 200000, Used 198188, Requested 2276. Please try again in 3m20.448s."}}'
    )
    minute = (
        '{"error":{"message":"Rate limit reached ... on tokens per minute (TPM): '
        'Limit 8000, Requested 8832"}}'
    )
    assert is_daily_limit(daily)
    assert not is_daily_limit(minute)
    detail = daily_limit_detail(daily)
    assert "198,188" in detail and "200,000" in detail

    # A LONG per-day wait must surface as a permanent error, not a retryable one, or
    # the ladder above it keeps trying against a wall.
    meter = TokenBudget(tpm=8000)
    client = _FakeClient([_Resp(429, headers={"retry-after": "1"}, body={"raw": daily})])
    client.responses[0].text = daily
    with pytest.raises(ProviderError) as excinfo:
        _Prov(client, meter=meter)._chat("m", "s", "u", 1000)
    assert not isinstance(excinfo.value, RetryableProviderError), (
        "a daily quota must not be retried"
    )
    assert "daily token quota" in str(excinfo.value)
    assert "ARCVISUAL_PROVIDER" in str(excinfo.value), "must say what the operator can do"


def test_per_minute_limit_stays_retryable() -> None:
    minute = (
        '{"error":{"message":"Rate limit reached ... on tokens per minute (TPM): '
        'Limit 8000, Requested 8832"}}'
    )
    client = _FakeClient([_Resp(429, headers={"retry-after": "0"})])
    client.responses[0].text = minute
    with pytest.raises(RetryableProviderError):
        _Prov(client, meter=TokenBudget(tpm=8000))._chat("m", "s", "u", 1000)


def test_a_nearly_clear_daily_window_is_waited_out_not_abandoned() -> None:
    """Groq's per-day window is ROLLING, so the wait scales with the size of the ask.

    At 197,770 of 200,000 used, a 2,272-token request was told to wait 18 seconds
    while a 5,266-token one was told 22 minutes. Treating every per-day rejection as
    fatal would abandon a job that only needed a moment's patience.
    """
    from arcvisual.providers.ratelimit import limit_resets_in

    assert limit_resets_in("try again in 18.144s.") == pytest.approx(18.144)
    assert limit_resets_in("try again in 22m1.488s.") == pytest.approx(1321.488)
    assert limit_resets_in("no guidance here") is None

    short = (
        '{"error":{"message":"... on tokens per day (TPD): Limit 200000, Used 197770, '
        'Requested 2272. Please try again in 0.01s."}}'
    )
    client = _FakeClient([_Resp(429, headers={})])
    client.responses[0].text = short
    with pytest.raises(RetryableProviderError):
        _Prov(client, meter=TokenBudget(tpm=8000))._chat("m", "s", "u", 1000)


def test_a_truncated_generation_is_charged_not_refunded() -> None:
    """`json_validate_failed` means the model generated until its budget ran out.

    Those tokens were spent even though the server discarded the result, and the
    error body carries no `usage` block to read them from. Refunding on that absence
    left the meter believing it had capacity the server had already consumed, so
    every retry fired straight into a 429.
    """
    body = '{"error":{"code":"json_validate_failed","failed_generation":"{...."}}'
    meter = TokenBudget(tpm=8000)
    before = meter._tokens
    client = _FakeClient([_Resp(400)])
    client.responses[0].text = body

    with pytest.raises(RetryableProviderError):
        _Prov(client, meter=meter)._chat("m", "s", "u", 2000)

    spent = before - meter._tokens
    assert spent > 0, "a truncated generation must cost the meter something"
    assert spent >= 2000, (
        f"the full reservation should be charged, not refunded; only {spent} was"
    )


def test_reservations_are_never_silently_shrunk() -> None:
    """`acquire` used to clamp to a capacity below the server's allowance, so a
    request sized near that allowance reserved less than it went on to ask for."""
    budget = TokenBudget(tpm=8000)
    assert budget.acquire(7900, timeout_s=1) == 7900


def test_a_tiny_budget_still_sends_quotable_text() -> None:
    """Headings alone are not a degraded prompt, they are a broken one.

    With no quotable text the model can only answer from memory, every quote fails to
    ground, and the job reports zero concepts with nothing in the logs explaining
    why — the exact silent failure that cost a full debugging cycle on the real
    Transformer paper.
    """
    from arcvisual.analyze.prompts import trim_body
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.test_review_fixes import metadata, paper_tarball

    sb = build_storyboard(metadata(), paper_tarball())
    analysable = [s for s in sb.sections if s.raw.strip()]

    for budget in (0, 1, 100, 500):
        text, _ = trim_body(sb, budget)
        # Some real prose from some section must survive, not just its heading.
        assert any(
            s.raw[:200].strip() and s.raw[:200].strip() in text for s in analysable
        ), f"budget {budget} sent no quotable text at all"


def test_dotenv_parsing_and_precedence(tmp_path, monkeypatch) -> None:
    """`.env` loading is how every credential reaches the app, and it was untested.

    The precedence rule is the part worth pinning: a real environment variable must
    beat the file, or `ARCVISUAL_PROVIDER=groq python -m ...` would be silently
    ignored and overriding anything for one run would mean editing a file and
    remembering to edit it back.
    """
    import os

    from arcvisual.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "A_PLAIN=hello world\n"
        'B_DOUBLE="quoted value"\n'
        "C_SINGLE='single quoted'\n"
        "export D_EXPORTED=exported\n"
        "E_EMPTY=\n"
        "F_EQUALS=key=with=equals\n"
        "G_ALREADY_SET=from_file\n",
        encoding="utf-8",
    )
    for key in ("A_PLAIN", "B_DOUBLE", "C_SINGLE", "D_EXPORTED", "E_EMPTY", "F_EQUALS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ARCVISUAL_NO_DOTENV", raising=False)
    monkeypatch.setenv("G_ALREADY_SET", "from_environment")

    load_dotenv(env_file)

    assert os.environ["A_PLAIN"] == "hello world"
    assert os.environ["B_DOUBLE"] == "quoted value", "matched quotes are stripped"
    assert os.environ["C_SINGLE"] == "single quoted"
    assert os.environ["D_EXPORTED"] == "exported", "an `export ` prefix is tolerated"
    assert os.environ["E_EMPTY"] == ""
    assert os.environ["F_EQUALS"] == "key=with=equals", "only the FIRST = splits"
    assert os.environ["G_ALREADY_SET"] == "from_environment", (
        "a real environment variable must win over the file"
    )


def test_dotenv_can_be_disabled(tmp_path, monkeypatch) -> None:
    """The suite sets this so a developer's real keys and provider choice cannot leak
    into a test run — a suite whose cost and outcome depend on the machine it runs on
    is not a suite."""
    import os

    from arcvisual.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("SHOULD_NOT_APPEAR=1\n", encoding="utf-8")
    monkeypatch.delenv("SHOULD_NOT_APPEAR", raising=False)
    monkeypatch.setenv("ARCVISUAL_NO_DOTENV", "1")

    assert load_dotenv(env_file) == 0
    assert "SHOULD_NOT_APPEAR" not in os.environ


def test_dotenv_missing_file_is_not_an_error(tmp_path, monkeypatch) -> None:
    from arcvisual.config import load_dotenv

    monkeypatch.delenv("ARCVISUAL_NO_DOTENV", raising=False)
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_the_polled_job_id_survives_a_resubmission(tmp_path) -> None:
    """Re-submitting a paper must not delete the id the client is polling.

    `(paper_id, pipeline_version)` is unique, and the queued placeholder starts with
    `paper_id=None` — so the collision only appears once `persist_job` resolves the
    paper. Resolving it by folding into the OLDER row deleted the placeholder
    mid-run: the reader's waiting room polled itself into `404 unknown_job` on every
    re-submission of a paper already seen, while the work completed perfectly. The
    placeholder's id is the one already handed out, so it is the one that must live.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from arcvisual.db import repo
    from arcvisual.db.models import Base, Job
    from arcvisual.ingest.arxiv import build_storyboard
    from arcvisual.render.pipeline import run_job
    from tests.test_review_fixes import metadata, paper_tarball

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    sb = build_storyboard(metadata(), paper_tarball())
    result = run_job(storyboard=sb, run_gate2=False)
    url = "https://arxiv.org/abs/1706.03762"

    # First submission, exactly as the API does it.
    with Session() as session:
        first = repo.create_queued_job(
            session, url=url, arxiv_id="1706.03762", submitted_by=None
        )
        repo.persist_job(result, session=session, job_id=first.id)
        session.commit()
        first_id = first.id

    # Second submission of the same paper at the same pipeline version.
    with Session() as session:
        second = repo.create_queued_job(
            session, url=url, arxiv_id="1706.03762", submitted_by=None
        )
        second_id = second.id
        repo.persist_job(result, session=session, job_id=second_id)
        session.commit()

    assert first_id != second_id
    with Session() as session:
        survivor = session.get(Job, second_id)
        assert survivor is not None, (
            "the id handed to the client was deleted; its waiting room now 404s"
        )
        assert survivor.state == "complete"
        # And the superseded row is gone rather than accumulating duplicates.
        assert session.get(Job, first_id) is None

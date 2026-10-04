"""Runtime configuration and the budgets that keep unit economics honest.

The ceilings here are not advice — they are checked in code before each
expensive action (see :mod:`arcvisual.generate.repair`). Attempt counts alone do
not bound spend, so the per-scene *dollar* ceiling is the real guard.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from functools import lru_cache

#: Bumped whenever pipeline behaviour changes in a way that should produce a
#: different article for the same paper. Part of the L3 cache key and of the
#: (paper_id, pipeline_version) idempotency key on `jobs`.
PIPELINE_VERSION = "0.1.0-phase1"

#: Pinned render environment. Any change here invalidates every L2 artifact.
MANIM_VERSION = "0.18.1"


@dataclass(frozen=True)
class Budgets:
    # --- per scene ---
    max_attempts: int = 3
    scene_cost_ceiling_usd: float = 0.60
    draft_render_timeout_s: int = 120
    draft_render_memory_mb: int = 2048
    # --- per job ---
    job_deadline_s: int = 25 * 60  # the plan's own p95 criterion
    job_cost_ceiling_usd: float = 8.00  # 2x target; a tripwire, not a target
    max_scenes: int = 12  # the triage cap
    min_scenes: int = 3
    # --- concurrency ---
    analyze_concurrency: int = 8  # Anthropic-bound, not CPU-bound
    render_max_containers: int = 12
    #: Scenes built in parallel in ONE process. Modest on purpose: each lane can start
    #: a Manim render, and a dozen at once thrashes a laptop. Production fans out
    #: across Modal containers instead, bounded by `render_max_containers`.
    scene_concurrency: int = 4


@dataclass(frozen=True)
class Ingest:
    max_pages: int = 60
    max_source_bytes: int = 60 * 1024 * 1024
    allowed_languages: tuple[str, ...] = ("en",)
    #: Licences that permit embedding the paper's own figures with attribution.
    redistributable_licenses: tuple[str, ...] = (
        "http://creativecommons.org/licenses/by/4.0/",
        "http://creativecommons.org/licenses/by-sa/4.0/",
        "http://creativecommons.org/licenses/by/3.0/",
        "http://creativecommons.org/publicdomain/zero/1.0/",
    )


@dataclass(frozen=True)
class Models:
    """Anthropic model choices, per the plan's cheap-to-classify / strong-to-codegen
    split."""

    # Current generation: Opus 5.5 is both newer and cheaper than Opus 5
    # ($4/$20 vs $5/$25 per MTok), and Sonnet 5.5 matches Sonnet 5's price.
    classify: str = "claude-sonnet-5-5"  # section analysis, triage
    codegen: str = "claude-opus-5-5"  # parameter filling, repair
    vision: str = "claude-sonnet-5-5"  # Gate 4
    max_tokens: int = 8192
    #: Structured-output attempts for ANALYZE, on providers without constrained
    #: decoding. Higher than codegen's because the cost of giving up is different:
    #: a codegen failure degrades one scene and the repair ladder absorbs it, while
    #: an analyze failure kills the entire paper — there is no ladder above it.
    analyze_json_attempts: int = 4


@dataclass(frozen=True)
class Poolside:
    """Poolside's OpenAI-compatible API. See docs.poolside.ai/api/overview.

    ``base_url`` is configurable because there are three ways in: Poolside-hosted
    inference, a self-managed endpoint, and OpenRouter.
    """

    api_key: str = ""
    base_url: str = "https://inference.poolside.ai/v1"
    classify: str = "poolside/laguna-s-2.1"
    codegen: str = "poolside/laguna-s-2.1"
    vision: str = ""  # no documented vision model; Gate 4 must use another provider
    temperature: float = 0.2
    timeout_s: float = 600.0
    #: Poolside counts REASONING tokens against max_tokens. laguna-s-2.1 will happily
    #: spend an 8192 budget entirely on reasoning and return `content: null` with
    #: `finish_reason: "length"` — a silent, total failure. Measured on the real
    #: Transformer paper: 8192 gave 8192 reasoning tokens and no answer; 32000 gave
    #: ~9.8k reasoning plus a complete one. This must NOT inherit Models.max_tokens,
    #: which is sized for a provider that budgets thinking separately.
    #: laguna's hard ceiling, verified: 40000 returns
    #: "max_tokens (40000): Input should be less than or equal to 32768".
    #: There is no room above this, so an over-long prompt cannot be solved by
    #: raising the budget — see `prompt_char_budget`.
    max_tokens: int = 32768
    #: Characters of paper body sent to Analyze. laguna counts reasoning against
    #: `max_tokens`, so a ~20k-token prompt can consume the entire 32768 budget
    #: thinking and return `content: null` — observed on the real Transformer paper,
    #: intermittently, because reasoning depth varies run to run. Trimming the body
    #: leaves headroom for an answer. ~40k chars is roughly 10k tokens.
    prompt_char_budget: int = 40000
    #: USD per million tokens, from your own contract. Left None on purpose: Poolside
    #: publishes no rates we could verify, and an invented rate would make the
    #: per-paper cost metric a fiction. None => cost reported as unattributed.
    rate_in: float | None = None
    rate_out: float | None = None
    def model_for(self, task: str) -> str:
        return {
            "classify": self.classify,
            "codegen": self.codegen,
            "vision": self.vision,
        }.get(task, self.classify)


#: Ceiling on a single request's prompt, in characters, independent of the
#: per-minute budget. Groq rejects larger requests with 413 "Request Entity Too
#: Large" even when the minute's allowance is mostly unspent — measured at 16,000
#: chars on `groq/compound`, which passed at 8,000. Kept conservative because the
#: cost of guessing high is a hard failure mid-job.
_MAX_PROMPT_CHARS_PER_REQUEST = 16000


@dataclass(frozen=True)
class Groq:
    """Groq. OpenAI-compatible, free tier, and — the reason it is here —
    constrained decoding via `response_format: json_schema` with `strict: true`.

    See console.groq.com/docs/structured-outputs.
    """

    api_key: str = ""
    base_url: str = "https://api.groq.com/openai/v1"
    #: Measured on a real free-tier key, because none of this is in the docs together:
    #:
    #:   model                 TPM     max prompt/req   strict json_schema
    #:   qwen/qwen3.6-27b      8,000   >= 16,000 chars  yes
    #:   openai/gpt-oss-20b    8,000   >= 16,000 chars  yes
    #:   groq/compound        70,000    < 16,000 chars  no (400)
    #:
    #: `compound`'s 70,000 TPM looks like the answer for ANALYZE, which sends the
    #: whole paper, and is not: it caps a *single request* far below its per-minute
    #: allowance, returning 413 "Request Entity Too Large" with 51,581 tokens still
    #: remaining in the window. It also rejects json_schema outright. So both tasks
    #: use the constrained model, and the paper body is trimmed to fit —
    #: see `prompt_char_budget` on the capabilities.
    #: Comma-separated, in preference order. Groq retires models without notice —
    #: `qwen/qwen3.6-27b` started answering 404 "model_not_found" and took every job
    #: down with it, because a single hardcoded id had nothing to fall back to. The
    #: provider now walks this list and skips ids the server has declared gone.
    classify: str = "qwen/qwen3.8-27b,openai/gpt-oss-120b"
    codegen: str = "qwen/qwen3.8-27b,openai/gpt-oss-120b"
    vision: str = ""
    temperature: float = 0.2
    timeout_s: float = 180.0
    #: Master switch for `response_format: json_schema`. Set 0 to force the prompted
    #: ladder everywhere, e.g. to compare the two paths.
    json_schema: bool = True
    #: Models that genuinely enforce a strict schema. Per-MODEL, not per-provider:
    #: `groq/compound` answers 400 rather than ignoring the parameter, and a model
    #: that ignored it silently would be worse still — we would stop validating while
    #: believing the output was constrained. Substring match, so a family covers its
    #: versioned members.
    json_schema_models: tuple[str, ...] = ("qwen", "gpt-oss", "llama-3.3", "llama-3.1")
    #: **There is also a per-DAY quota, and it is the one you will actually hit.**
    #: Measured on a free key: 200,000 tokens/day for qwen/qwen3.6-27b, against 8,000
    #: per minute. Nothing advertises it — `x-ratelimit-limit-tokens` reports the
    #: MINUTE allowance and keeps reading a healthy 8,000 while the day's budget is
    #: spent; only the 429 body names it. The window is rolling rather than a midnight
    #: reset, so the wait scales with the size of the ask: at 197,770 used, a
    #: 2,272-token request was told to wait 18 seconds and a 5,266-token one 22
    #: minutes. Budget roughly 30-40 full pipeline runs per day per key.
    #:
    #: Per-model tokens-per-minute allowances, read from `x-ratelimit-limit-tokens`.
    #: The meter is keyed by model because the allowance is.
    tpm_by_model: tuple[tuple[str, int], ...] = (("compound", 70000),)
    #: Characters of paper body sent to ANALYZE. The binding constraint on a free key
    #: is the per-minute token budget, which the prompt and the requested output
    #: share: every character sent is a token the reply cannot use.
    #:
    #: That trade-off is sharper than it looks. At 12,000 chars the analysis was
    #: truncated mid-object — constrained decoding ran out of budget after `concepts`
    #: and the request failed with "missing properties: 'opportunities',
    #: 'reading_note'". A full AnalysisOut needs roughly 4-5k output tokens, so the
    #: prompt has to stay near 2k. 8,000 chars is ~2,000 tokens, leaving ~5,200.
    #:
    #: The real Transformer paper is ~78,000 chars, so this drops most of it.
    #: Grounding still holds — every concept must quote text that was actually sent —
    #: but coverage is reduced, and that is a property of the free tier rather than
    #: of the design. Raise it on a paid key, where the ceiling is far higher.
    prompt_char_budget: int = 5000
    #: Concepts to ask ANALYZE for. The schema permits 40 and a real Anthropic run
    #: produced 18, which does not fit here: prompt and reply share one 8,000-token
    #: allowance, and an over-long reply is not truncated politely — constrained
    #: decoding stops mid-object and the request fails outright with "missing
    #: properties: 'reading_note'". Eight concepts and the opportunities they carry
    #: fit inside the remaining ~5,200 tokens.
    analysis_item_budget: int = 6
    #: USD per million tokens. None on the free tier, and left None rather than
    #: guessed on paid — a plausible wrong cost is worse than an honest gap.
    rate_in: float | None = None
    rate_out: float | None = None
    #: Reasoning budget. ``qwen/qwen3.6-27b`` accepts only "none" or "default"; other
    #: models accept neither. Measured on one Analyze call, same 4 valid concepts:
    #: "none" 1.4s / 1,284 tokens, "default" 9.2s / 5,224. On a token-per-minute
    #: budget that is the difference between six calls a minute and one, so it is
    #: the single largest efficiency lever this provider has.
    reasoning_effort: str = "none"
    #: Tokens-per-minute ceiling for this key. Groq charges the *requested*
    #: ``max_completion_tokens`` against this budget before generating anything, so a
    #: budget above the ceiling is a guaranteed 413 rather than a long answer. 0
    #: disables local pacing and leaves it to the server's 429s.
    tpm: int = 8000
    #: Ceiling on any single request's output budget.
    #:
    #: Deliberately far below ``tpm``. Groq admits a request by counting prompt PLUS
    #: requested output, so asking for 6,000 against an 8,000 allowance reserves
    #: nearly the whole minute for one call — every other call then waits ~54s for the
    #: bucket to refill, and a job needing analyze plus two codegen calls plus repairs
    #: spends its life asleep. Measured need: a full AnalysisOut completes in ~1,400
    #: output tokens and a codegen params object in far fewer, so 3,000 leaves real
    #: headroom while letting two calls share a minute instead of one owning it.
    max_tokens: int = 3000
    #: Output ceiling for CODEGEN specifically. Higher than analyze's because the two
    #: sit at opposite ends of the same trade: codegen's prompt is small (it gets the
    #: claim, the concept, one section's text and a schema — not the paper), so it can
    #: afford a longer reply within the same per-minute allowance. It also needs one:
    #: a params object plus its beats truncated at 3,000 tokens, and constrained
    #: decoding does not truncate politely — it fails the whole call.
    max_tokens_codegen: int = 4500

    def models_for(self, task: str) -> list[str]:
        """Every configured model for `task`, in preference order."""
        raw = {
            "classify": self.classify,
            "codegen": self.codegen,
            "vision": self.vision,
        }.get(task, self.classify)
        return [m.strip() for m in raw.split(",") if m.strip()]

    def model_for(self, task: str) -> str:
        models = self.models_for(task)
        return models[0] if models else ""

    def sends_reasoning_effort(self, model: str = "") -> bool:
        """Whether to send the parameter at all, for this model.

        Models that do not accept it reject the request outright (400), so an empty
        value means "omit" rather than "send the default". Only the models that
        document the parameter get it; `compound` is an agentic system and rejects it.
        """
        if not self.reasoning_effort:
            return False
        return any(tag in model for tag in ("qwen", "gpt-oss"))

    def effort_for(self, model: str) -> str:
        """The reasoning effort value this *model* accepts, or "" to omit it.

        The two families take disjoint vocabularies, verified on a live key:
        qwen accepts "none" / "default" (and "low"), while gpt-oss accepts only
        "low" / "medium" / "high" and rejects "none" with 400. Sending the qwen
        setting to a gpt-oss fallback would fail every call it was meant to rescue.
        """
        if not self.sends_reasoning_effort(model):
            return ""
        if "gpt-oss" in model and self.reasoning_effort in ("none", "default"):
            return "low"
        return self.reasoning_effort

    def enforces_schema(self, model: str) -> bool:
        """Whether `model` actually constrains decoding to the schema."""
        return self.json_schema and any(tag in model for tag in self.json_schema_models)

    def tpm_for(self, model: str) -> int:
        """The per-minute token allowance for `model`, falling back to the default."""
        for tag, limit in self.tpm_by_model:
            if tag in model:
                return limit
        return self.tpm

    def prompt_budget_for(self, model: str) -> int:
        """Characters of paper body this model can be sent.

        Scales with the model's own allowance, but never above the measured
        per-request ceiling — the two limits are independent, and `compound` fails the
        second while passing the first.
        """
        scaled = self.prompt_char_budget * max(1, self.tpm_for(model) // max(self.tpm, 1))
        return min(scaled, _MAX_PROMPT_CHARS_PER_REQUEST)

    def max_tokens_for(self, model: str, task: str = "") -> int:
        """Output ceiling for `model` on `task`, kept below its own TPM allowance.

        Groq charges the *requested* budget against the allowance on arrival, so a
        model with a bigger allowance can also be asked for a longer answer — and a
        task with a smaller prompt can afford a longer reply within the same one.
        """
        base = self.max_tokens_codegen if task == "codegen" else self.max_tokens
        allowance = self.tpm_for(model)
        return base if allowance <= self.tpm else min(allowance // 2, 16384)


@dataclass(frozen=True)
class Opencode:
    """The opencode agent CLI. See opencode.ai/docs/cli.

    ``model`` is in opencode's own ``provider/model`` form and may be empty, in which
    case opencode uses whatever it is configured with — selecting this backend
    delegates the model choice rather than making it.
    """

    binary: str = "opencode"
    classify: str = ""
    codegen: str = ""
    vision: str = ""
    agent: str = ""
    variant: str = ""  # provider-specific reasoning effort
    #: URL of a running `opencode serve`. Without it every call pays backend and MCP
    #: cold-start, which dominates wall clock across a dozen scenes.
    attach: str = ""
    #: 180s, not 600s. opencode HANGS when its route is unresponsive — no error, no
    #: output, just silence — so this timeout is the only thing that ends the call.
    #: At 600s a twelve-scene paper spends two hours discovering the provider is down.
    timeout_s: float = 420.0

    def model_for(self, task: str) -> str:
        return {
            "classify": self.classify,
            "codegen": self.codegen,
            "vision": self.vision,
        }.get(task, self.classify)


@dataclass(frozen=True)
class Render:
    draft_flag: str = "-ql"  # 480p15, validation only
    #: 720p30 by default. 1080p60 is the largest single contributor to the
    #: render budget; reserve it for scenes that actually need frame-accurate
    #: seeking. See ArcVisual-Architecture.md §12.
    final_flag: str = "-qm"
    scrubbable_final_flag: str = "-qh"
    background_color: str = "#0E1116"  # the style contract's dark canvas
    safe_margin: float = 0.3  # Manim units, Gate 3
    min_text_px_at_1080p: float = 18.0
    min_contrast_ratio: float = 4.5
    max_dead_air_s: float = 2.5
    max_runtime_s: float = 75.0  # a scene longer than this has lost the plot


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    #: "auto" | "anthropic" | "poolside" | "opencode" | "heuristic".
    #: "auto" takes the first configured provider in preference order.
    provider: str = "auto"
    anthropic_api_key: str = ""
    #: Optional. Any Anthropic-compatible gateway (e.g. Dedalus Labs, a proxy).
    #: Same SDK, same request shape, same model ids — only the host changes.
    anthropic_base_url: str = ""
    #: Required when the key is *identity-linked* (issued to a person rather than to
    #: a workspace). Such a key is rejected with
    #: "anthropic-workspace-id is required when authenticating with an
    #: identity-linked API key" until the header names the workspace to act in.
    #: Empty for an ordinary workspace key, where sending it would be wrong.
    anthropic_workspace_id: str = ""
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "arcvisual"
    r2_public_base: str = ""  # CDN domain; R2 itself stays private
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    sentry_dsn: str = ""
    ip_hash_salt: str = "dev-salt-change-me"
    rate_limit_per_hour: int = 3
    #: How many proxies sit in front of the API. 0 means trust nothing from
    #: X-Forwarded-For — that header is client-controlled, and trusting it blindly
    #: hands every caller an unlimited supply of fresh rate-limit buckets. Set to 1
    #: behind a single load balancer, 2 behind a CDN plus a load balancer.
    trusted_proxy_hops: int = 0
    strict_ownership: bool = True  # assert_monotonic between stages
    #: "manim" renders each scene to video (Modal, or the local renderer).
    #: "client" ships the validated parameters and lets the reader animate them in
    #: the browser — no Manim, no ffmpeg, no object storage, so the whole product
    #: runs on serverless functions. Only archetypes with a browser renderer are
    #: offered to the analyzer in this mode.
    render_mode: str = "manim"
    #: Wall-clock seconds one `/advance` call may spend before handing back. Kept
    #: well under the platform's function limit (300s on Vercel Hobby): a scene
    #: started near the end must still finish inside the same invocation.
    step_budget_s: int = 200

    @property
    def client_render(self) -> bool:
        return self.render_mode.strip().lower() == "client"

    budgets: Budgets = field(default_factory=Budgets)
    ingest: Ingest = field(default_factory=Ingest)
    models: Models = field(default_factory=Models)
    poolside: Poolside = field(default_factory=Poolside)
    groq: Groq = field(default_factory=Groq)
    opencode: Opencode = field(default_factory=Opencode)
    render: Render = field(default_factory=Render)

    def provider_configured(self, name: str) -> bool:
        """Whether one named provider has what it needs to be constructed.

        opencode is a PATH question rather than a key question — it holds its own
        credentials — which is why this is not simply "is a key set".
        """
        if name == "anthropic":
            return bool(self.anthropic_api_key)
        if name == "poolside":
            return bool(self.poolside.api_key)
        if name == "groq":
            return bool(self.groq.api_key)
        if name == "opencode":
            import shutil

            return shutil.which(self.opencode.binary) is not None
        return False

    @property
    def offline(self) -> bool:
        """True when no model provider is available, so the pipeline runs its
        deterministic heuristic analyzer. Tests and the eval harness rely on this.

        Provider-aware on purpose: with ``ARCVISUAL_PROVIDER=poolside`` and no
        Anthropic key the pipeline is emphatically *not* offline, and treating it as
        such would silently skip the model the operator asked for. Kept in step with
        :func:`arcvisual.providers.registry.get_provider` by
        ``test_offline_agrees_with_registry`` — two answers to "is a model available"
        that can disagree is a bug waiting to happen.
        """
        requested = (self.provider or "auto").strip().lower()
        if requested in ("", "none", "off", "heuristic"):
            return True
        if requested == "auto":
            # opencode is excluded here to match registry.AUTO_PREFERENCE: having the
            # binary on PATH must not silently route the pipeline through an agent
            # whose cost we cannot attribute.
            return not any(self.provider_configured(n) for n in ("anthropic", "poolside"))
        return not self.provider_configured(requested)


def load_dotenv(path: str | os.PathLike[str] | None = None) -> int:
    """Read ``.env`` into the process environment. Returns the number of keys set.

    Hand-rolled rather than taking a dependency: the format we need is a few lines
    of ``KEY=value``, and the only subtlety worth having is the precedence rule.

    **A real environment variable always wins.** ``ARCVISUAL_PROVIDER=groq python -m
    ...`` must override the file, or overriding anything for a single run would mean
    editing a file and remembering to edit it back.
    """
    # The suite must not inherit the developer's real keys and provider choice: a
    # test run whose outcome and cost depend on the machine it runs on is not a test
    # run. tests/conftest.py sets this before importing anything.
    if os.environ.get("ARCVISUAL_NO_DOTENV") == "1":
        return 0
    root = pathlib.Path(path) if path else pathlib.Path(__file__).resolve().parent.parent / ".env"
    try:
        text = root.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        # Strip matched quotes, so KEY="a b" and KEY='a b' both mean `a b`.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


@lru_cache(maxsize=1)
def settings() -> Settings:
    # Before the first read, never after: settings() is cached, so a later .env edit
    # would not be picked up anyway, and pretending otherwise would mislead.
    load_dotenv()
    env = os.environ.get
    return Settings(
        database_url=env("DATABASE_URL", ""),
        provider=env("ARCVISUAL_PROVIDER", "auto"),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        anthropic_base_url=env("ANTHROPIC_BASE_URL", ""),
        anthropic_workspace_id=env("ANTHROPIC_WORKSPACE_ID", ""),
        r2_account_id=env("R2_ACCOUNT_ID", ""),
        r2_access_key_id=env("R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=env("R2_SECRET_ACCESS_KEY", ""),
        r2_bucket=env("R2_BUCKET", "arcvisual"),
        r2_public_base=env("R2_PUBLIC_BASE", ""),
        langfuse_public_key=env("LANGFUSE_PUBLIC_KEY", ""),
        langfuse_secret_key=env("LANGFUSE_SECRET_KEY", ""),
        sentry_dsn=env("SENTRY_DSN", ""),
        ip_hash_salt=env("IP_HASH_SALT", "dev-salt-change-me"),
        rate_limit_per_hour=int(env("RATE_LIMIT_PER_HOUR", "3")),
        trusted_proxy_hops=int(env("ARCVISUAL_TRUSTED_PROXY_HOPS", "0")),
        strict_ownership=env("STRICT_OWNERSHIP", "1") != "0",
        render_mode=env("ARCVISUAL_RENDER_MODE", "manim"),
        step_budget_s=int(env("ARCVISUAL_STEP_BUDGET_S", "200")),
        budgets=Budgets(
            scene_concurrency=int(env("ARCVISUAL_SCENE_CONCURRENCY", "4")),
        ),
        models=Models(
            classify=env("ANTHROPIC_MODEL_CLASSIFY", Models.classify),
            codegen=env("ANTHROPIC_MODEL_CODEGEN", Models.codegen),
        ),
        groq=Groq(
            api_key=env("GROQ_API_KEY", ""),
            base_url=env("GROQ_BASE_URL", Groq.base_url),
            classify=env("GROQ_MODEL_CLASSIFY", Groq.classify),
            codegen=env("GROQ_MODEL_CODEGEN", Groq.codegen),
            temperature=float(env("GROQ_TEMPERATURE", "0.2")),
            timeout_s=float(env("GROQ_TIMEOUT_S", "180")),
            json_schema=env("GROQ_JSON_SCHEMA", "1") != "0",
            rate_in=_opt_float(env("GROQ_RATE_IN")),
            rate_out=_opt_float(env("GROQ_RATE_OUT")),
            reasoning_effort=env("GROQ_REASONING_EFFORT", Groq.reasoning_effort),
            tpm=int(env("GROQ_TPM", str(Groq.tpm))),
            vision=env("GROQ_MODEL_VISION", Groq.vision),
            prompt_char_budget=int(
                env("GROQ_PROMPT_CHAR_BUDGET", str(Groq.prompt_char_budget))
            ),
            analysis_item_budget=int(
                env("GROQ_ANALYSIS_ITEMS", str(Groq.analysis_item_budget))
            ),
            max_tokens=int(env("GROQ_MAX_TOKENS", str(Groq.max_tokens))),
            max_tokens_codegen=int(
                env("GROQ_MAX_TOKENS_CODEGEN", str(Groq.max_tokens_codegen))
            ),
        ),
        poolside=Poolside(
            api_key=env("POOLSIDE_API_KEY", ""),
            base_url=env("POOLSIDE_BASE_URL", Poolside.base_url),
            classify=env("POOLSIDE_MODEL_CLASSIFY", Poolside.classify),
            codegen=env("POOLSIDE_MODEL_CODEGEN", Poolside.codegen),
            temperature=float(env("POOLSIDE_TEMPERATURE", "0.2")),
            timeout_s=float(env("POOLSIDE_TIMEOUT_S", "600")),
            max_tokens=int(env("POOLSIDE_MAX_TOKENS", "32768")),
            prompt_char_budget=int(env("POOLSIDE_PROMPT_CHAR_BUDGET", "40000")),
            rate_in=_opt_float(env("POOLSIDE_RATE_IN")),
            rate_out=_opt_float(env("POOLSIDE_RATE_OUT")),
        ),
        opencode=Opencode(
            binary=env("OPENCODE_BIN", "opencode"),
            classify=env("OPENCODE_MODEL_CLASSIFY", ""),
            codegen=env("OPENCODE_MODEL_CODEGEN", ""),
            agent=env("OPENCODE_AGENT", ""),
            variant=env("OPENCODE_VARIANT", ""),
            attach=env("OPENCODE_ATTACH", ""),
            timeout_s=float(env("OPENCODE_TIMEOUT_S", "420")),
        ),
    )


def _opt_float(value: str | None) -> float | None:
    """None stays None. A rate we do not know must not become 0.0, or an
    unattributed cost would be reported as free."""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None

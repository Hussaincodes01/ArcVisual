"""The serverless deployment: stepped execution, client rendering, model fallback.

These drive the real HTTP routes against a real (SQLite) database, because the
properties that matter are end-to-end ones: a job submitted through the API reaches
`complete` through repeated `/advance` calls alone, concurrent calls never double up,
and a finished article is served from the cache rather than rebuilt.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from arcvisual.db import repo
from arcvisual.db.models import Base, Job
from arcvisual.ingest import arxiv as arxiv_mod
from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.ingest.errors import IngestRejection, RejectCode
from arcvisual.storyboard import Archetype, Storyboard
from tests.fixtures import metadata, paper_tarball

URL = "https://arxiv.org/abs/1706.03762"  # the fixture paper


@pytest.fixture
def client_mode(reset_settings):
    reset_settings(ARCVISUAL_RENDER_MODE="client", ARCVISUAL_STEP_BUDGET_S="120")
    yield


@pytest.fixture
def api(tmp_path, client_mode, monkeypatch):
    """The stepped API on a file-backed SQLite database.

    File-backed rather than :memory: because the stepper builds scenes on worker
    threads, and each SQLite connection to :memory: is a separate empty database.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'av.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(repo, "_engine", engine)

    calls = {"ingest": 0}

    def fake_ingest(url: str) -> Storyboard:
        calls["ingest"] += 1
        return build_storyboard(metadata(), paper_tarball())

    monkeypatch.setattr(arxiv_mod, "ingest_url", fake_ingest)

    from arcvisual.render.api import build_api

    app = build_api(None, stepped=True)
    with TestClient(app) as tc:
        tc.calls = calls  # type: ignore[attr-defined]
        tc.engine = engine  # type: ignore[attr-defined]
        yield tc


def _drive(tc: TestClient, job_id: str, limit: int = 10) -> dict:
    body: dict = {}
    for _ in range(limit):
        res = tc.post(f"/api/jobs/{job_id}/advance")
        assert res.status_code == 200, res.text
        body = res.json()
        if body["state"] in ("complete", "failed"):
            return body
    return body


# -- stepped execution ------------------------------------------------------ #


def test_a_submitted_job_completes_through_advance_calls_alone(api) -> None:
    res = api.post("/api/jobs", json={"url": URL})
    assert res.status_code == 202, res.text
    submitted = res.json()
    assert submitted["stepped"] is True and submitted["state"] == "queued"

    final = _drive(api, submitted["job_id"])
    assert final["state"] == "complete", final
    assert final["stage_progress"]["slug"]

    # Effects, not status fields: the article is actually servable, and every
    # passed scene carries parameters the browser can animate.
    paper = api.get(f"/api/papers/{final['stage_progress']['slug']}")
    assert paper.status_code == 200
    body = paper.json()
    assert body["render_mode"] == "client"
    scenes = body["storyboard"]["scenes"]
    assert scenes, "the fixture paper should yield at least one scene"
    passed = [s for s in scenes if s["state"] == "passed"]
    assert passed
    for s in passed:
        assert s["artifact"]["quality"] == "client"
        assert s["spec"]["params"], "a client scene with no params cannot be drawn"
        assert s["spec"]["archetype"] in {a.value for a in _client_archetypes()}


def test_the_article_is_readable_before_the_scenes_finish(api, reset_settings) -> None:
    # A zero budget makes each advance call take exactly one stage and stop.
    reset_settings(ARCVISUAL_STEP_BUDGET_S="0")
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]

    from arcvisual.render import stepper

    sf = repo.session_factory()
    stepper.advance(job_id, session_factory=sf, budget_s=1)  # ingest (+ maybe more)
    with sf() as s:
        state = s.get(Job, job_id).state
    while state == "analyzing" or state == "ingesting":
        stepper.advance(job_id, session_factory=sf, budget_s=0.01)
        with sf() as s:
            state = s.get(Job, job_id).state
    if state == "rendering":
        slug = api.get(f"/api/jobs/{job_id}").json()["stage_progress"]["slug"]
        doc = api.get(f"/api/papers/{slug}").json()
        assert doc["state"] == "rendering"
        assert doc["storyboard"]["sections"], "prose must be readable already"
        assert all(s["state"] == "pending" for s in doc["storyboard"]["scenes"])


def test_a_held_lease_turns_a_second_caller_away(api) -> None:
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    with repo.session_factory()() as s:
        token = repo.acquire_lease(s, job_id, ttl_s=60)
        assert token
        assert repo.acquire_lease(s, job_id, ttl_s=60) is None

    res = api.post(f"/api/jobs/{job_id}/advance").json()
    assert res["busy"] is True
    assert res["state"] == "queued", "a busy caller must not have done any work"
    assert api.calls["ingest"] == 0

    with repo.session_factory()() as s:
        repo.release_lease(s, job_id, token)
    assert _drive(api, job_id)["state"] == "complete"


def test_an_expired_lease_is_reclaimed(api) -> None:
    """A step killed by the platform never releases its lease. It must lapse."""
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    with repo.session_factory()() as s:
        assert repo.acquire_lease(s, job_id, ttl_s=-1)  # already in the past
    assert _drive(api, job_id)["state"] == "complete"


def test_resubmitting_a_finished_paper_is_free(api) -> None:
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    assert _drive(api, job_id)["state"] == "complete"
    assert api.calls["ingest"] == 1

    again = api.post("/api/jobs", json={"url": URL}).json()
    # Client artifacts have no bytes to go missing, so the cache must hit. Before
    # the fix it never did, and every submission rebuilt the article from scratch.
    assert again["cached"] is True
    assert again["job_id"] == job_id
    assert api.calls["ingest"] == 1


def test_resubmitting_a_running_paper_shares_the_run(api) -> None:
    first = api.post("/api/jobs", json={"url": URL}).json()
    second = api.post("/api/jobs", json={"url": URL}).json()
    assert second["job_id"] == first["job_id"]
    assert second.get("resumed") is True


def test_free_answers_do_not_spend_the_rate_limit(api, reset_settings) -> None:
    from arcvisual.render.api import build_api

    reset_settings(RATE_LIMIT_PER_HOUR="1")
    api = TestClient(build_api(None, stepped=True))  # settings are read at build time
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    _drive(api, job_id)
    for _ in range(3):  # already explained: open instantly, never a 429
        assert api.post("/api/jobs", json={"url": URL}).status_code == 202
    other = api.post("/api/jobs", json={"url": "https://arxiv.org/abs/1512.03385"})
    assert other.status_code == 429


def test_an_ingest_rejection_fails_the_job_with_reader_text(api, monkeypatch) -> None:
    def reject(url: str):
        raise IngestRejection(RejectCode.NO_MACHINE_TEXT, "no LaTeX source")

    monkeypatch.setattr(arxiv_mod, "ingest_url", reject)
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    final = _drive(api, job_id)
    assert final["state"] == "failed"
    assert final["failure"]["user_facing"]


def test_the_gallery_lists_finished_articles(api) -> None:
    assert api.get("/api/papers").json()["papers"] == []
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    _drive(api, job_id)
    papers = api.get("/api/papers").json()["papers"]
    assert len(papers) == 1
    assert papers[0]["slug"] and papers[0]["title"]
    assert papers[0]["visuals"] >= 1


def test_the_cron_sweep_is_closed_without_its_secret(api, reset_settings) -> None:
    assert api.get("/api/cron/sweep").status_code == 403
    reset_settings(CRON_SECRET="s3cret")
    assert (
        api.get("/api/cron/sweep", headers={"authorization": "Bearer no"}).status_code
        == 403
    )
    job_id = api.post("/api/jobs", json={"url": URL}).json()["job_id"]
    swept = api.get("/api/cron/sweep", headers={"authorization": "Bearer s3cret"})
    assert swept.status_code == 200
    assert swept.json()["advanced"][0]["job_id"] == job_id


# -- client render mode ----------------------------------------------------- #


def _client_archetypes():
    from arcvisual.templates.registry import CLIENT_RENDERABLE

    return CLIENT_RENDERABLE


def test_client_mode_never_offers_an_archetype_the_browser_cannot_draw(
    client_mode,
) -> None:
    from arcvisual.analyze.prompts import archetype_help
    from arcvisual.templates import registry

    offered = set(registry.available_archetypes())
    assert offered == set(_client_archetypes())
    assert Archetype.CUSTOM_SCENE not in offered
    assert "custom_scene" not in archetype_help()
    assert not registry.is_available(Archetype.CUSTOM_SCENE)


def test_video_mode_still_offers_custom_scenes() -> None:
    from arcvisual.templates import registry

    assert Archetype.CUSTOM_SCENE in registry.available_archetypes()


# -- deployment plumbing ---------------------------------------------------- #


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        (
            "postgresql://u:p@h/db?sslmode=require",
            "postgresql+psycopg://u:p@h/db?sslmode=require",
        ),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite+pysqlite:///x.db", "sqlite+pysqlite:///x.db"),
    ],
)
def test_hosted_postgres_urls_use_the_installed_driver(given: str, expected: str) -> None:
    assert repo.normalize_dsn(given) == expected


def test_schema_head_matches_migrations() -> None:
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import arcvisual.db.migrate as migrate

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(migrate.__file__).parent / "migrations")
    )
    assert ScriptDirectory.from_config(cfg).get_current_head() == migrate.HEAD


def test_job_model_carries_the_lease_columns_the_migration_adds() -> None:
    cols = set(Job.__table__.columns.keys())
    assert {"lease_until", "lease_owner"} <= cols


# -- provider resilience ---------------------------------------------------- #


class _Resp:
    def __init__(self, status: int, body: dict | str):
        import json as _json

        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else _json.dumps(body)
        self.headers: dict = {}

    def json(self):
        return self._body


class _ScriptedHttp:
    """Answers by model id, and records which ids were asked for."""

    def __init__(self, by_model: dict[str, _Resp]):
        self.by_model = by_model
        self.asked: list[str] = []

    def post(self, _path: str, json: dict) -> _Resp:
        self.asked.append(json["model"])
        return self.by_model[json["model"]]


def _ok(content: str) -> _Resp:
    return _Resp(
        200,
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


GONE = _Resp(
    404,
    '{"error":{"message":"The model `old/model` does not exist","code":"model_not_found"}}',
)


def test_a_retired_groq_model_falls_through_to_the_next(reset_settings) -> None:
    """The outage this prevents: one hardcoded model id was retired upstream, every
    call answered 404, and every job failed until someone redeployed."""
    from pydantic import BaseModel

    from arcvisual.providers import groq_provider
    from arcvisual.providers.base import Task, TextBlock

    reset_settings(
        GROQ_API_KEY="k",
        GROQ_MODEL_CLASSIFY="old/model,qwen/qwen3.8-27b",
        GROQ_TPM="0",
    )
    groq_provider._UNAVAILABLE.clear()

    class Out(BaseModel):
        answer: str

    http = _ScriptedHttp({"old/model": GONE, "qwen/qwen3.8-27b": _ok('{"answer":"ok"}')})
    provider = groq_provider.GroqProvider(client=http)
    result = provider.structured(
        system=[TextBlock("sys")], user="q", output_model=Out, task=Task.CLASSIFY
    )
    assert result.ok and result.value.answer == "ok"
    assert result.model == "qwen/qwen3.8-27b"
    # Remembered: the next call goes straight to the live model.
    provider.structured(
        system=[TextBlock("sys")], user="q", output_model=Out, task=Task.CLASSIFY
    )
    assert http.asked == ["old/model", "qwen/qwen3.8-27b", "qwen/qwen3.8-27b"]
    groq_provider._UNAVAILABLE.clear()


def test_an_ordinary_404_is_not_mistaken_for_a_retired_model(reset_settings) -> None:
    from pydantic import BaseModel

    from arcvisual.providers import groq_provider
    from arcvisual.providers.base import ModelUnavailable, ProviderError, Task, TextBlock

    reset_settings(GROQ_API_KEY="k", GROQ_MODEL_CLASSIFY="a,b", GROQ_TPM="0")
    groq_provider._UNAVAILABLE.clear()

    class Out(BaseModel):
        answer: str

    http = _ScriptedHttp({"a": _Resp(404, "Not Found"), "b": _ok('{"answer":"x"}')})
    provider = groq_provider.GroqProvider(client=http)
    with pytest.raises(ProviderError) as info:
        provider.structured(
            system=[TextBlock("s")], user="q", output_model=Out, task=Task.CLASSIFY
        )
    assert not isinstance(info.value, ModelUnavailable)
    assert http.asked == ["a"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("qwen/qwen3.8-27b", "none"),
        # gpt-oss rejects "none" with 400; it must get a value it accepts.
        ("openai/gpt-oss-120b", "low"),
        ("openai/gpt-oss-20b", "low"),
        ("groq/compound", ""),
    ],
)
def test_reasoning_effort_matches_what_each_model_accepts(
    model: str, expected: str
) -> None:
    from arcvisual.config import Groq

    assert Groq(reasoning_effort="none").effort_for(model) == expected


def test_the_default_groq_models_are_a_fallback_chain() -> None:
    from arcvisual.config import Groq

    models = Groq().models_for("classify")
    assert len(models) >= 2, "a single model id is one retirement away from an outage"
    assert "qwen/qwen3.6-27b" not in models, "retired upstream"


# -- multi-pass analysis ---------------------------------------------------- #


def test_a_long_paper_is_analysed_in_prioritised_passes() -> None:
    from arcvisual.analyze.single_pass import plan_passes

    sb = build_storyboard(metadata(), paper_tarball())
    single = plan_passes(sb, None, 3)
    assert len(single) == 1, "no budget means one pass over everything"
    groups = plan_passes(sb, 900, 3)
    assert 1 < len(groups) <= 3
    flat = [sid for g in groups for sid in g]
    assert len(flat) == len(set(flat)), "a section is analysed once"
    order = [s.id for s in sb.sections]
    for g in groups:
        assert g == sorted(g, key=order.index), "each pass reads in document order"


def test_one_failed_pass_does_not_lose_the_others() -> None:
    from arcvisual.analyze import single_pass
    from arcvisual.analyze.prompts import AnalysisOut, ConceptOut
    from arcvisual.providers.base import Capabilities, RetryableProviderError

    sb = build_storyboard(metadata(), paper_tarball())
    groups = single_pass.plan_passes(sb, 900, 3)
    assert len(groups) >= 2
    calls = {"n": 0}

    class Stub:
        name = "stub"

        def capabilities(self):
            return Capabilities(
                name="stub",
                native_structured_output=True,
                prompt_cache=False,
                vision=False,
                cost_attributed=True,
                prompt_char_budget=900,
                analysis_passes=3,
            )

    def fake_call(sub, provider):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableProviderError("rate limited")
        section = next(s for s in sub.sections if len(s.raw) > 80)
        quote = section.raw[:60]
        out = AnalysisOut(
            concepts=[
                ConceptOut(
                    name=f"Idea {calls['n']}",
                    statement="A grounded idea from this pass.",
                    section_id=section.id,
                    quote=quote,
                    centrality=0.8,
                )
            ]
        )
        return out, single_pass.AnalyzeReport(provider="stub")

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(single_pass, "_call_provider", fake_call)
        out, report = single_pass._call_in_passes(sb, Stub(), groups)
    assert calls["n"] == len(groups)
    assert len(out.concepts) == len(groups) - 1
    assert any("passes" in n for n in report.notes)

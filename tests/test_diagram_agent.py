"""The Python side of the TypeScript diagram agent.

What matters here is the seam: the agent is used when it is configured and has time,
its parameters go through exactly the validation one-shot codegen's do, and any
failure falls back to the one-shot call. It must never cost a scene.
"""

from __future__ import annotations

import copy

import pytest

from arcvisual.storyboard import Archetype
from tests.test_concept_diagram import ATTENTION, _DiagramProvider


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _Http:
    def __init__(self, resp: _Resp | Exception):
        self.resp = resp
        self.sent: list[dict] = []

    def post(self, url, json, headers):
        self.sent.append({"url": url, "json": json, "headers": headers})
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.fixture(scope="module")
def storyboard():
    from arcvisual.analyze.single_pass import analyze
    from arcvisual.ingest.arxiv import build_storyboard
    from tests.fixtures import metadata, paper_tarball

    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    return sb


def _opp(sb):
    return sb.opportunities[0].model_copy(update={"archetype": Archetype.CONCEPT_DIAGRAM})


AGENT_OK = {
    "ok": True,
    "params": copy.deepcopy(ATTENTION),
    "captions": [s["caption"] for s in ATTENTION["steps"]],
    "turns": 2,
    "model": "nvidia/nemotron-3.5-lightning:free",
    "notes": [],
}


@pytest.fixture
def agent_on(reset_settings):
    reset_settings(
        ARCVISUAL_DIAGRAM_AGENT_URL="https://reader.example/api/diagram",
        ARCVISUAL_INTERNAL_SECRET="s3cret",
    )


def test_the_agent_is_asked_with_the_secret_and_the_section(agent_on, storyboard) -> None:
    from arcvisual.generate import diagram_agent

    http = _Http(_Resp(200, AGENT_OK))
    drawn = diagram_agent.draw(storyboard, _opp(storyboard), budget_s=150, client=http)
    assert drawn is not None and drawn.turns == 2
    sent = http.sent[0]
    assert sent["headers"] == {"x-arcvisual-key": "s3cret"}
    assert sent["json"]["budget_s"] == 150
    assert sent["json"]["claim"] and sent["json"]["section"]


@pytest.mark.parametrize(
    "resp",
    [
        _Resp(200, {"ok": False, "params": None, "notes": ["no accepted diagram"]}),
        _Resp(401, {"error": "unauthorised"}),
        ConnectionError("down"),
    ],
)
def test_any_agent_failure_is_a_fallback_not_an_error(agent_on, storyboard, resp) -> None:
    from arcvisual.generate import diagram_agent

    assert (
        diagram_agent.draw(storyboard, _opp(storyboard), budget_s=150, client=_Http(resp))
        is None
    )


def test_no_call_without_time_or_configuration(reset_settings, storyboard) -> None:
    from arcvisual.generate import diagram_agent

    http = _Http(_Resp(200, AGENT_OK))
    reset_settings(ARCVISUAL_DIAGRAM_AGENT_URL=None, ARCVISUAL_INTERNAL_SECRET=None)
    assert (
        diagram_agent.draw(storyboard, _opp(storyboard), budget_s=150, client=http)
        is None
    )
    reset_settings(
        ARCVISUAL_DIAGRAM_AGENT_URL="https://reader.example/api/diagram",
        ARCVISUAL_INTERNAL_SECRET="s3cret",
    )
    assert (
        diagram_agent.draw(storyboard, _opp(storyboard), budget_s=5, client=http) is None
    )
    assert http.sent == []


def test_codegen_uses_the_agent_and_validates_its_diagram(
    agent_on, storyboard, monkeypatch
) -> None:
    from arcvisual.generate import codegen, diagram_agent

    real = diagram_agent.draw
    http = _Http(_Resp(200, AGENT_OK))
    monkeypatch.setattr(
        diagram_agent,
        "draw",
        lambda sb, opp, budget_s: real(sb, opp, budget_s=budget_s, client=http),
    )
    provider = _DiagramProvider(budget=300_000)
    result = codegen.generate_scene(storyboard, _opp(storyboard), provider=provider)
    assert result.params_obj is not None, result.notes
    assert any("diagram agent" in n for n in result.notes)
    assert provider.user == "", "the one-shot call should not have been made"
    assert [b.caption for b in result.scene.spec.beats] == AGENT_OK["captions"]


def test_codegen_falls_back_to_one_shot_when_the_agent_fails(
    agent_on, storyboard, monkeypatch
) -> None:
    from arcvisual.generate import codegen, diagram_agent

    monkeypatch.setattr(diagram_agent, "draw", lambda sb, opp, budget_s: None)
    provider = _DiagramProvider(budget=300_000)
    result = codegen.generate_scene(storyboard, _opp(storyboard), provider=provider)
    assert result.params_obj is not None
    assert provider.user, "the one-shot call should have been made"


def test_repairs_do_not_go_to_the_agent(agent_on, storyboard, monkeypatch) -> None:
    from arcvisual.generate import codegen, diagram_agent

    called = []
    monkeypatch.setattr(diagram_agent, "draw", lambda *a, **k: called.append(1))
    codegen.generate_scene(
        storyboard,
        _opp(storyboard),
        provider=_DiagramProvider(budget=300_000),
        findings=["x"],
        attempt=2,
    )
    assert called == []

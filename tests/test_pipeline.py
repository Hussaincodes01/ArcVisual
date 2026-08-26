"""End-to-end behaviour: triage restraint, the repair ladder, budgets, persistence.

The tests that matter most here are the *negative* ones — that a scene failure never
fails a job, that the cost ceiling actually stops spending, and that triage refuses
to animate everything. Those are the properties that make the unit economics and
the credibility claims true rather than aspirational.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from arcvisual.analyze.single_pass import analyze, triage
from arcvisual.config import Budgets, settings
from arcvisual.db import repo
from arcvisual.gates.g2_runtime import RenderOutcome, RenderUnavailable
from arcvisual.generate.codegen import generate_scene, render_module
from arcvisual.generate.repair import build_scene
from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.render.pipeline import run_job
from arcvisual.storyboard import (
    Archetype,
    JobState,
    SceneState,
    Stage,
    Storyboard,
    VisualOpportunity,
    assert_monotonic,
)
from tests.fixtures import metadata, paper_tarball


@pytest.fixture
def sb() -> Storyboard:
    return build_storyboard(metadata(), paper_tarball())


# -- analyze ---------------------------------------------------------------- #


def test_analyze_grounds_every_concept_and_opportunity(sb: Storyboard) -> None:
    out, report = analyze(sb)
    assert out.concepts
    for concept in out.concepts:
        assert concept.span.resolve(out.section(concept.span.section_id))
    for opp in out.opportunities:
        assert opp.span.resolve(out.section(opp.span.section_id))
    assert report.concepts_dropped_ungrounded == 0


def test_analyze_respects_stage_ownership(sb: Storyboard) -> None:
    before = sb.model_copy(deep=True)
    after, _ = analyze(sb)
    assert_monotonic(before, after, Stage.ANALYZE)


def test_analyze_only_proposes_renderable_archetypes(sb: Storyboard) -> None:
    from arcvisual.templates import registry

    out, _ = analyze(sb)
    available = set(registry.available_archetypes())
    assert all(o.archetype in available for o in out.opportunities)


# -- triage ----------------------------------------------------------------- #


def _opp(oid: str, section_id: str, difficulty: int, centrality: float, sb: Storyboard):
    section = sb.section(section_id)
    text = section.raw.strip()[:60]
    from arcvisual.ingest.latex import span_for_text

    span = span_for_text(section, text)
    assert span is not None
    return VisualOpportunity(
        id=oid,
        archetype=Archetype.TRANSFORM_CHAIN,
        claim="a claim long enough to pass validation",
        concept_id="c000",
        span=span,
        justification="because prose alone is insufficient here",
        difficulty=difficulty,
        centrality=centrality,
    )


def test_triage_caps_total_animations(sb: Storyboard) -> None:
    """More is worse. An agent that wants to animate everything has understood
    nothing."""
    sections = [s.id for s in sb.sections if len(s.prose_md) > 200]
    candidates = [
        _opp(f"o{i:03d}", sections[i % len(sections)], 5, 1.0, sb) for i in range(40)
    ]
    kept = triage(candidates, {})
    assert len(kept) <= settings().budgets.max_scenes


def test_triage_ranks_by_difficulty_times_centrality(sb: Storyboard) -> None:
    sections = [s.id for s in sb.sections if len(s.prose_md) > 200][:3]
    weak = _opp("weak", sections[0], 1, 0.1, sb)
    strong = _opp("strong", sections[1], 5, 1.0, sb)
    kept = triage([weak, strong], {})
    assert kept[0].id == "strong"


def test_triage_limits_animations_per_section(sb: Storyboard) -> None:
    """A section with four proposals is a section the model found interesting,
    not one the reader needs four videos for."""
    section_id = next(s.id for s in sb.sections if len(s.prose_md) > 200)
    candidates = [_opp(f"o{i}", section_id, 5, 1.0, sb) for i in range(5)]
    kept = triage(candidates, {})
    assert len(kept) <= 2


# -- codegen ---------------------------------------------------------------- #


def test_generated_module_is_valid_python(sb: Storyboard) -> None:
    import ast

    out, _ = analyze(sb)
    for opp in out.opportunities:
        gen = generate_scene(out, opp)
        ast.parse(gen.source)  # must not raise


def test_generated_module_is_deterministic(sb: Storyboard) -> None:
    """Same spec, same bytes — otherwise the L2 cache never warms."""
    out, _ = analyze(sb)
    gen = generate_scene(out, out.opportunities[0])
    again = render_module(gen.scene.spec, gen.params_obj)
    assert gen.source == again


def test_generated_module_stays_thin(sb: Storyboard) -> None:
    """Every line the model does not write is a line that cannot hallucinate."""
    out, _ = analyze(sb)
    for opp in out.opportunities:
        gen = generate_scene(out, opp)
        code_lines = [
            ln
            for ln in gen.source.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", '"""'))
        ]
        assert len(code_lines) < 45, opp.id


def test_transform_chain_never_repeats_a_step(sb: Storyboard) -> None:
    """Repeating one equation renders a no-op transform: dead air that teaches
    nothing."""
    out, _ = analyze(sb)
    for opp in out.opportunities:
        if opp.archetype is not Archetype.TRANSFORM_CHAIN:
            continue
        steps = generate_scene(out, opp).params_obj.steps
        assert len(set(steps)) == len(steps), steps


# -- the repair ladder ------------------------------------------------------ #


class AlwaysFailsRenderer:
    """A backend whose renders always crash, to exercise the full ladder."""

    def __init__(self) -> None:
        self.calls = 0

    def render(self, source, spec, *, quality, timeout_s) -> RenderOutcome:
        self.calls += 1
        return RenderOutcome(
            ok=False,
            exit_code=1,
            stdout="",
            stderr="Traceback (most recent call last):\nValueError: boom",
            duration_s=0.0,
            wall_ms=10,
        )


class UnavailableRenderer:
    def render(self, source, spec, *, quality, timeout_s) -> RenderOutcome:
        raise RenderUnavailable("no manim here")


def test_scene_failure_degrades_and_never_raises(sb: Storyboard) -> None:
    out, _ = analyze(sb)
    renderer = AlwaysFailsRenderer()
    outcome = build_scene(out, out.opportunities[0], renderer=renderer)
    assert outcome.scene.state in (SceneState.DEGRADED, SceneState.FAILED)
    assert outcome.scene.degraded_reason
    assert len(outcome.attempts) == settings().budgets.max_attempts


def test_every_attempt_is_recorded_including_failures(sb: Storyboard) -> None:
    """That record IS the per-archetype first-pass metric."""
    out, _ = analyze(sb)
    outcome = build_scene(out, out.opportunities[0], renderer=AlwaysFailsRenderer())
    assert all(a.failed_gate == 2 for a in outcome.attempts)
    assert not outcome.first_pass


def test_ladder_simplifies_on_later_attempts(sb: Storyboard) -> None:
    out, _ = analyze(sb)
    outcome = build_scene(out, out.opportunities[0], renderer=AlwaysFailsRenderer())
    assert any(a.simplified for a in outcome.attempts[1:])


def test_unavailable_renderer_is_inconclusive_not_a_failure(sb: Storyboard) -> None:
    """CI has no Manim. Refusing to ship would make CI impossible; pretending the
    gate passed would be a lie. Inconclusive is recorded as such."""
    out, _ = analyze(sb)
    outcome = build_scene(out, out.opportunities[0], renderer=UnavailableRenderer())
    assert outcome.scene.state is SceneState.PASSED
    findings = outcome.attempts[0].gate_report.gate2.findings
    assert any("inconclusive" in f for f in findings)


def test_cost_ceiling_stops_spending(sb: Storyboard, monkeypatch) -> None:
    """Attempt count alone does not bound spend; the dollar ceiling does."""
    from arcvisual import config

    out, _ = analyze(sb)
    tight = Budgets(scene_cost_ceiling_usd=0.0001, max_attempts=3)
    monkeypatch.setattr(
        config, "settings", lambda: config.Settings(budgets=tight), raising=True
    )
    import arcvisual.generate.repair as repair_mod

    monkeypatch.setattr(repair_mod, "settings", lambda: config.Settings(budgets=tight))

    def expensive(*args, **kwargs):
        from arcvisual.generate.codegen import generate_scene as real

        gen = real(*args, **kwargs)
        gen.cost_usd = 0.50  # blow the ceiling on the first attempt
        return gen

    monkeypatch.setattr(repair_mod, "generate_scene", expensive)
    outcome = build_scene(out, out.opportunities[0], renderer=AlwaysFailsRenderer())
    assert len(outcome.attempts) == 1
    assert any("ceiling" in n for n in outcome.notes)


# -- the job ---------------------------------------------------------------- #


def test_job_completes_with_gate1_only(sb: Storyboard) -> None:
    result = run_job(storyboard=sb, run_gate2=False)
    assert result.state is JobState.COMPLETE
    assert result.shipped == len(result.scenes)
    assert result.first_pass_rate == 1.0


def test_scene_failures_do_not_fail_the_job(sb: Storyboard) -> None:
    """A section with good prose and no animation is a fine outcome."""
    result = run_job(storyboard=sb, renderer=AlwaysFailsRenderer())
    assert result.state is JobState.COMPLETE
    assert result.shipped == 0
    assert all(s.state is not SceneState.PASSED for s in result.scenes)


def test_ingest_rejection_fails_the_job_with_reader_facing_text() -> None:
    result = run_job("https://example.com/not-a-paper")
    assert result.state is JobState.FAILED
    assert result.failure and result.failure["user_facing"]


def test_storyboard_stays_valid_after_the_full_run(sb: Storyboard) -> None:
    result = run_job(storyboard=sb, run_gate2=False)
    Storyboard.model_validate(result.storyboard.model_dump())


# -- persistence ------------------------------------------------------------ #


@pytest.fixture
def session():
    engine = repo.create_all("sqlite+pysqlite:///:memory:")
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        yield s


def test_persist_records_scenes_and_attempts(sb: Storyboard, session) -> None:
    result = run_job(storyboard=sb, run_gate2=False)
    job = repo.persist_job(
        result, session=session, submitted_by=repo.hash_submitter("1.2.3.4")
    )
    assert job.state == "complete"
    rows = repo.scene_states(session, job.id)
    assert len(rows) == len(result.scenes)
    assert all(r["ready"] for r in rows)


def test_rerunning_the_same_paper_is_idempotent(sb: Storyboard, session) -> None:
    from arcvisual.db.models import Job, SceneRow

    result = run_job(storyboard=sb, run_gate2=False)
    repo.persist_job(result, session=session)
    repo.persist_job(result, session=session)
    assert session.query(Job).count() == 1
    assert session.query(SceneRow).count() == len(result.scenes)


def test_artifacts_are_shared_across_runs(sb: Storyboard, session) -> None:
    """The L2 cache: an identical scene is one object, referenced twice."""
    result = run_job(storyboard=sb, run_gate2=False)
    repo.persist_job(result, session=session)
    repo.persist_job(result, session=session)
    row = repo.cached_artifact(session, result.scenes[0].artifact.content_hash)
    assert row is not None and row.ref_count == 2


def test_raw_ip_is_never_stored(sb: Storyboard, session) -> None:
    result = run_job(storyboard=sb, run_gate2=False)
    job = repo.persist_job(
        result, session=session, submitted_by=repo.hash_submitter("203.0.113.9")
    )
    assert "203.0.113.9" not in (job.submitted_by or "")
    assert len(job.submitted_by) == 32


def test_archetype_health_reports_first_pass_rate(sb: Storyboard, session) -> None:
    result = run_job(storyboard=sb, run_gate2=False)
    repo.persist_job(result, session=session)
    rows = repo.archetype_health(session)
    assert rows
    assert all(r["first_pass_rate"] == 1.0 for r in rows)

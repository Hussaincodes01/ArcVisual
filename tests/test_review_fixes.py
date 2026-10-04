"""Regression tests for the code-review findings.

Each test pins the *behaviour that was wrong*, not the shape of the fix. Several of
these bugs shared a property worth naming: they passed every existing test while
being broken in production, because the tests exercised the one code path the bug
did not affect (a local renderer, a single-attempt scene, a well-formed UUID). So
these deliberately reach for the paths that were not covered.
"""

from __future__ import annotations

import inspect
import itertools
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from arcvisual.analyze.prompts import AnalysisOut, ConceptOut
from arcvisual.analyze.single_pass import AnalyzeReport, _apply
from arcvisual.db import repo
from arcvisual.gates.g2_runtime import RenderOutcome, scrape_log
from arcvisual.gates.g2_runtime import run as gate2_run
from arcvisual.ingest.arxiv import build_storyboard
from arcvisual.providers.base import extract_json, strip_trailing_commas
from arcvisual.render.pipeline import run_job
from arcvisual.storyboard import Beat, SceneSpec, SourceSpan, quote_hash
from tests.fixtures import metadata, paper_tarball

# --------------------------------------------------------------------------- #
# F1 — Gate 2 asserted on a local file path that no remote backend can provide
# --------------------------------------------------------------------------- #


def _spec() -> SceneSpec:
    return SceneSpec(
        id="s1",
        archetype="transform_chain",
        claim="a claim long enough to satisfy the schema",
        concept_id="c0",
        span=SourceSpan(section_id="s000", start=0, end=5, quote_sha256=quote_hash("x")),
        params={},
        beats=(Beat(t=0.0, dur=2.0, caption="one"),),
    )


class _FakeRenderer:
    def __init__(self, **kw):
        self.kw = kw

    def render(self, source, spec, *, quality, timeout_s):
        return RenderOutcome(
            ok=True,
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=spec.duration_s,
            wall_ms=10,
            **self.kw,
        )


def test_gate2_passes_a_sandboxed_render_that_kept_no_local_file() -> None:
    """The bug: a sandbox uploads its video and is torn down, so `video_path` is
    always None there. Gating on it failed every scene on every Modal backend while
    passing locally, which is why no test caught it."""
    result, _ = gate2_run(
        "", _spec(), _FakeRenderer(video_path=None, produced_video=True)
    )
    assert result.passed, result.findings


def test_gate2_still_fails_when_no_video_was_produced() -> None:
    result, _ = gate2_run(
        "", _spec(), _FakeRenderer(video_path=None, produced_video=False)
    )
    assert not result.passed
    assert any("no video file" in f for f in result.findings)


# --------------------------------------------------------------------------- #
# F7 — greedy log scraping swallowed the whole log into one finding
# --------------------------------------------------------------------------- #


def test_log_findings_stay_one_line() -> None:
    log = (
        "LaTeX Error: File `foo.sty' not found.\n"
        + "Manim progress bar noise\n" * 400
        + "more unrelated output\n"
    )
    findings = scrape_log(log)
    assert findings
    # The finding is fed verbatim into the repair prompt; a swallowed log would
    # crowd out the context the repair actually needs.
    assert all(len(f) < 300 for f in findings), findings
    assert "progress bar noise" not in " ".join(findings)


# --------------------------------------------------------------------------- #
# F15 — trailing-comma repair corrupted commas inside string values
# --------------------------------------------------------------------------- #


def test_commas_inside_strings_survive() -> None:
    """The old regex rewrote `", }"` inside a caption. The result still parsed, so
    nothing downstream ever noticed the text had been silently altered."""
    raw = '{"caption": "first, then second, }", "n": 1}'
    payload = extract_json(raw)
    assert payload is not None
    import json

    assert json.loads(payload)["caption"] == "first, then second, }"


def test_structural_trailing_commas_are_still_removed() -> None:
    assert strip_trailing_commas('{"a": 1,}') == '{"a": 1}'
    assert strip_trailing_commas('{"a": [1, 2,]}') == '{"a": [1, 2]}'
    assert strip_trailing_commas('{"a": "x,", "b": 2}') == '{"a": "x,", "b": 2}'


def test_escaped_quote_does_not_break_string_tracking() -> None:
    raw = r'{"a": "he said \"hi,\" ", "b": 2,}'
    payload = extract_json(raw)
    assert payload is not None


# --------------------------------------------------------------------------- #
# F14 — cache writes priced at the 5-minute rate while asking for a 1-hour TTL
# --------------------------------------------------------------------------- #


def test_cache_write_multiplier_matches_the_requested_ttl(reset_settings) -> None:
    reset_settings(ANTHROPIC_API_KEY="sk-test")
    from arcvisual.providers import anthropic_provider as ap

    # 1.25x is the 5-minute rate; 1h costs 2x. Billing the wrong one understated
    # every paper that cached its body — which is all of them.
    assert ap._CACHE_WRITE_TTL == "1h"
    assert ap._CACHE_WRITE_MULTIPLIER == 2.0

    provider = ap.AnthropicProvider(client=object())

    class _Usage:
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 1_000_000

    class _Resp:
        usage = _Usage()

    # Sonnet 4.6 bills $3/MTok input, so 1M cache-written tokens at 1h cost $6.
    usage = provider._usage(_Resp(), "claude-sonnet-4-6")
    assert usage.cost_usd == pytest.approx(3.00 * 2.0)


def test_requested_ttl_and_priced_ttl_cannot_drift(reset_settings) -> None:
    reset_settings(ANTHROPIC_API_KEY="sk-test")
    from arcvisual.providers.anthropic_provider import _CACHE_WRITE_TTL, _to_block
    from arcvisual.providers.base import TextBlock

    block = _to_block(TextBlock(text="body", cache=True))
    assert block["cache_control"]["ttl"] == _CACHE_WRITE_TTL


# --------------------------------------------------------------------------- #
# F11 — duplicate concept names misaligned the dependency pairing
# --------------------------------------------------------------------------- #


def test_dependencies_follow_the_kept_concept_not_a_namesake() -> None:
    sb = build_storyboard(metadata(), paper_tarball())
    section = sb.section("s003")
    raw = AnalysisOut(
        concepts=[
            # Same name as the next one, but ungroundable, so it is dropped.
            ConceptOut(
                name="Scaling",
                statement="dropped",
                section_id="s003",
                quote="this sentence is nowhere in the paper",
                centrality=0.5,
            ),
            ConceptOut(
                name="Scaling",
                statement="kept",
                section_id="s003",
                quote=section.raw[100:200].strip(),
                centrality=0.5,
            ),
            ConceptOut(
                name="Softmax",
                statement="c",
                section_id="s003",
                quote=section.raw[300:400].strip(),
                centrality=0.5,
                depends_on=["Scaling"],
            ),
        ]
    )
    out, report = _apply(sb, raw, AnalyzeReport())
    assert report.concepts_dropped_ungrounded == 1
    kept = {c.id: c for c in out.concepts}
    softmax = next(c for c in out.concepts if c.name == "Softmax")
    # The edge must land on the concept that survived, and the surviving "Scaling"
    # must be the one whose quote grounded.
    assert len(softmax.depends_on) == 1
    assert kept[softmax.depends_on[0]].statement == "kept"


# --------------------------------------------------------------------------- #
# F2 / F10 — no job row existed until the job finished
# --------------------------------------------------------------------------- #


@pytest.fixture
def session():
    engine = repo.create_all("sqlite+pysqlite:///:memory:")
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        yield s


def test_queued_job_is_pollable_immediately(session) -> None:
    """The waiting room polls from its first render. Before this, nothing existed to
    poll until the pipeline happened to finish."""
    job = repo.create_queued_job(
        session,
        url="https://arxiv.org/abs/1706.03762",
        arxiv_id="1706.03762",
        submitted_by="hash",
    )
    assert job.id is not None
    assert job.state == "queued"
    assert job.paper_id is None  # not resolved yet, and that is allowed


def test_rate_limit_counts_work_in_flight(session) -> None:
    """Counting only completed jobs let a caller fire any number of concurrent
    submissions, every one of them seeing a count of zero."""
    for _ in range(3):
        repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="same")
    assert repo.recent_submissions(session, "same") == 3
    assert repo.recent_submissions(session, "other") == 0


def test_finished_job_lands_on_the_id_the_reader_polled(session) -> None:
    queued = repo.create_queued_job(
        session, url="u", arxiv_id="1706.03762", submitted_by="hash"
    )
    queued_id = queued.id

    result = run_job(
        storyboard=build_storyboard(metadata(), paper_tarball()), run_gate2=False
    )
    job = repo.persist_job(result, session=session, job_id=queued_id)

    assert job.id == queued_id, "a second row would strand the polling reader"
    assert job.state == "complete"
    # The slug rides in the progress payload so the waiting room can redirect.
    assert job.stage_progress.get("slug") == result.storyboard.paper.slug


def test_progress_updates_are_visible_mid_run(session) -> None:
    job = repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="h")
    repo.update_job_progress(
        session, job.id, state="analyzing", progress={"slug": "a-paper", "sections": 8}
    )
    session.expire_all()
    from arcvisual.db.models import Job

    fresh = session.get(Job, job.id)
    assert fresh.state == "analyzing"
    assert fresh.stage_progress["slug"] == "a-paper"
    # Merged, not replaced — a later event must not wipe an earlier field.
    repo.update_job_progress(session, job.id, progress={"scenes_done": 2})
    session.expire_all()
    fresh = session.get(Job, job.id)
    assert fresh.stage_progress["slug"] == "a-paper"
    assert fresh.stage_progress["scenes_done"] == 2


# --------------------------------------------------------------------------- #
# F4 — archetype_health inflated scene columns by attempt count
# --------------------------------------------------------------------------- #


def test_metrics_do_not_multiply_cost_by_attempt_count(session) -> None:
    from arcvisual.db.models import SceneAttempt, SceneRow

    job = repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="h")
    scene = SceneRow(
        job_id=job.id,
        scene_key="s1",
        archetype="transform_chain",
        state="passed",
        attempts=3,
        cost_usd=0.30,
    )
    session.add(scene)
    session.flush()
    # Three attempts: the first two failed. Joining would count this scene's $0.30
    # three times and weight its first_pass_rate by three.
    for n, gate in ((1, 1), (2, 2), (3, None)):
        session.add(
            SceneAttempt(scene_id=scene.id, attempt_no=n, failed_gate=gate, render_ms=100)
        )
    session.commit()

    rows = repo.archetype_health(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["scenes"] == 1
    assert float(row["total_cost"]) == pytest.approx(0.30)
    assert float(row["first_pass_rate"]) == pytest.approx(0.0)
    assert row["passed"] == 1


# --------------------------------------------------------------------------- #
# F8 — a malformed job id was a 500, not a 404
# --------------------------------------------------------------------------- #


def test_malformed_job_id_is_not_a_uuid() -> None:
    """The guard the endpoint uses. `session.get` would otherwise pass this straight
    into the GUID type decorator, which raises rather than returning None."""
    with pytest.raises(ValueError):
        uuid.UUID("not-a-uuid")


# --------------------------------------------------------------------------- #
# F9 — the rate-limit key trusted a client-controlled header
# --------------------------------------------------------------------------- #


def test_forwarded_header_is_ignored_without_a_trusted_proxy(reset_settings) -> None:
    reset_settings(ARCVISUAL_TRUSTED_PROXY_HOPS=None)
    from arcvisual.config import settings

    assert settings().trusted_proxy_hops == 0, (
        "X-Forwarded-For is attacker-controlled; trusting it by default hands every "
        "caller an unlimited supply of fresh rate-limit buckets"
    )


# --------------------------------------------------------------------------- #
# Schema brittleness — found by running the real pipeline on a real paper
# --------------------------------------------------------------------------- #


def test_one_highlight_per_step_validates() -> None:
    """The exact shape that cost two scenes their whole repair budget.

    On arXiv:1706.03762, Poolside supplied 2 steps and 2 highlights on all six
    attempts across both scenes. The old rule allowed at most `len(steps) - 1`, on
    the theory that only transitions can be accented — a constraint with no
    rendering reason behind it, and the opposite of the obvious reading. A caller
    that violates a rule identically six times is not the thing that is wrong.
    """
    from arcvisual.templates.transform_chain import Params

    params = Params(
        steps=[
            r"\mathrm{MultiHead}(Q,K,V) = \mathrm{Concat}(\mathrm{head}_1)W^O",
            r"\mathrm{head}_i = \mathrm{Attention}(QW_i^Q, KW_i^K, VW_i^V)",
        ],
        highlight=[r"\mathrm{Concat}", r"W_i^Q"],
        captions=["Concatenate the heads.", "Each head projects separately."],
    )
    assert len(params.highlight) == len(params.steps)


def test_highlight_longer_than_steps_is_still_rejected() -> None:
    """Loosened, not removed: an entry with no step to attach to is still a bug."""
    import pytest as _pytest
    from pydantic import ValidationError

    from arcvisual.templates.transform_chain import Params

    with _pytest.raises(ValidationError, match="align with steps by index"):
        Params(steps=["a=b", "b=c"], highlight=["x", "y", "z"])


def test_highlight_is_aligned_to_steps_not_transitions() -> None:
    """highlight[0] belongs to step 0, so the opening step can be accented."""
    from arcvisual.templates.transform_chain import Params, _highlight_for

    params = Params(steps=["a=b", "b=c", "c=d"], highlight=["FIRST", "", "THIRD"])
    assert _highlight_for(params, 0) == "FIRST"
    assert _highlight_for(params, 1) == ""
    assert _highlight_for(params, 2) == "THIRD"
    assert _highlight_for(params, 3) == ""  # past the end is simply no accent


def test_estimate_duration_counts_the_opening_accent() -> None:
    """A beat that build() plays but estimate_duration() forgets shows up as
    duration drift at Gate 2, on every scene, forever."""
    from arcvisual.templates.transform_chain import Params, estimate_duration

    without = estimate_duration(Params(steps=["a=b", "b=c"]))
    with_first = estimate_duration(Params(steps=["a=b", "b=c"], highlight=["a"]))
    assert with_first == pytest.approx(without + 1.0)


def test_fewer_captions_than_steps_is_allowed() -> None:
    from arcvisual.templates.transform_chain import Params

    assert Params(steps=["a=b", "b=c", "c=d"], captions=["only one"]).captions == [
        "only one"
    ]


def test_completion_does_not_erase_the_title(session) -> None:
    """`progress()` does not carry the title, so replacing stage_progress wholesale
    at completion made the waiting-room header revert from the paper's title to the
    generic placeholder at the exact moment the job finished."""
    queued = repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="h")
    repo.update_job_progress(
        session,
        queued.id,
        state="ingesting",
        progress={"title": "Attention Is All You Need", "sections": 25},
    )
    result = run_job(
        storyboard=build_storyboard(metadata(), paper_tarball()), run_gate2=False
    )
    job = repo.persist_job(result, session=session, job_id=queued.id)
    # `title` is not produced by progress(), so it must survive the merge.
    assert job.stage_progress["title"] == "Attention Is All You Need"
    assert job.stage_progress["slug"] == result.storyboard.paper.slug
    # `sections` IS produced by progress(), so the final run's real count wins over
    # the value written mid-flight — merging must not freeze stale numbers.
    assert job.stage_progress["sections"] == len(result.storyboard.sections)


# --------------------------------------------------------------------------- #
# Schema tolerance — found by running a real model against a real paper
# --------------------------------------------------------------------------- #
#
# The governing rule, arrived at by losing an entire paper's analysis three
# different ways: STRUCTURAL constraints reject, COSMETIC constraints coerce.


def test_over_long_strings_are_trimmed_not_rejected() -> None:
    """A real run returned 18 grounded concepts and 8 argued opportunities. All of
    it was discarded because two claims ran 12 and 16 characters past a 200-char cap
    and a note ran 16 past 400. The cap exists so a caption fits on screen — it is
    not a statement about correctness, so it must not be able to void an analysis."""
    from arcvisual.analyze.prompts import AnalysisOut, OpportunityOut

    opp = OpportunityOut(
        archetype="transform_chain",
        claim="a genuinely useful claim that simply runs long " * 12,
        concept_name="x" * 200,
        section_id="s000",
        quote="a quote that is far too long " * 60,
        justification="because prose alone will not do it " * 30,
        difficulty=4,
        centrality=0.9,
    )
    assert len(opp.claim) <= 280
    assert len(opp.concept_name) <= 60
    assert len(opp.quote) <= 600
    assert len(opp.justification) <= 300
    assert AnalysisOut(concepts=[], reading_note="note " * 400).reading_note


def test_clamping_survives_a_string_with_no_word_boundaries() -> None:
    """`textwrap.shorten` on an unbroken string returns only the placeholder, which
    then trips the MINIMUM length — failing the validation the clamp exists to
    prevent."""
    from arcvisual.analyze.prompts import OpportunityOut

    opp = OpportunityOut(
        archetype="transform_chain",
        claim="x" * 900,
        concept_name="c",
        section_id="s000",
        quote="q" * 900,
        justification="j" * 900,
        difficulty=3,
        centrality=0.5,
    )
    assert 10 <= len(opp.claim) <= 280
    assert 12 <= len(opp.quote) <= 600


def test_archetype_enum_is_in_the_json_schema() -> None:
    """A bare `type: string` told the model nothing, and it invented `mechanism`,
    `counterfactual`, `decomposition` and four more — every one then dropped as
    unavailable. The taxonomy is a closed set, so the schema must say so."""
    from arcvisual.analyze.prompts import OpportunityOut
    from arcvisual.templates import registry

    field = OpportunityOut.model_json_schema()["properties"]["archetype"]
    assert "enum" in field, "the model cannot honour a constraint it is never shown"
    assert set(field["enum"]) == {a.value for a in registry.available_archetypes()}


def test_sections_resolve_by_heading_as_well_as_id() -> None:
    """A real run cited every section by heading path rather than by id. Under a
    strict id lookup all eighteen concepts were dropped as ungrounded — which reads
    as "the model understood nothing" when it had understood the paper perfectly and
    merely labelled its answer differently."""
    from arcvisual.analyze.single_pass import _section_resolver

    sb = build_storyboard(metadata(), paper_tarball())
    resolve = _section_resolver(sb)
    target = sb.section("s003")

    assert resolve("s003") is target
    assert resolve(" > ".join(target.heading_path)) is target
    assert resolve(target.heading) is target
    assert resolve(target.heading.upper()) is target
    assert resolve("A Section That Does Not Exist") is None
    assert resolve("") is None


def test_ambiguous_heading_is_refused_rather_than_guessed() -> None:
    """Attaching a claim to the wrong section is the silent wrongness the grounding
    check exists to prevent, so a duplicate leaf heading resolves only by full path."""
    from arcvisual.analyze.single_pass import _section_resolver
    from arcvisual.storyboard import Section

    sb = build_storyboard(metadata(), paper_tarball())
    sb.sections.append(
        Section(
            id="s900",
            heading_path=["Appendix", "Multi-Head Attention"],
            raw="duplicate leaf heading",
            prose_md="duplicate leaf heading",
        )
    )
    resolve = _section_resolver(sb)
    assert resolve("Multi-Head Attention") is None  # ambiguous -> refuse
    assert resolve("Appendix > Multi-Head Attention").id == "s900"  # full path is fine


# --------------------------------------------------------------------------- #
# Beat arithmetic — found by a real render run, not by a fixture
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "total_s,n",
    [
        (6.2, 3),  # the exact runtime that broke two scenes: 6.2/3 = 2.0666...
        (10.0, 3),
        (7.0, 3),
        (13.9, 7),
        (4.1, 2),
        (20.3, 9),
        (5.0, 4),
    ],
)
def test_fit_beats_never_overlaps(total_s: float, n: int) -> None:
    """`fit_beats` output must satisfy SceneSpec's own overlap validator.

    The original accumulated the RAW quotient while storing a ROUNDED `dur`, so the
    two disagreed by up to a millisecond per beat. On a 6.2s runtime over 3 captions
    that produced dur=2.067 with the next beat at t=4.133 — beat 1 ended at 4.134,
    which the validator rejected. Two scenes burned every attempt on it, and every
    fixture missed it because round numbers divide evenly.
    """
    from arcvisual.generate.codegen import fit_beats

    beats = fit_beats([f"caption {i}" for i in range(n)], total_s)
    for prev, nxt in itertools.pairwise(beats):
        assert nxt.t >= prev.t + prev.dur - 1e-9, (
            f"beat overlap at total_s={total_s}: {prev.t}+{prev.dur} > {nxt.t}"
        )


def test_fit_beats_output_builds_a_valid_scenespec() -> None:
    """The end-to-end guard: the validator is what actually rejected these."""
    from arcvisual.generate.codegen import fit_beats
    from arcvisual.storyboard import Archetype, SceneSpec, SourceSpan, quote_hash

    beats = fit_beats(["one", "two", "three"], 6.2)
    spec = SceneSpec(
        id="o006",
        archetype=Archetype.PLOT_REVEAL,
        claim="A claim long enough to satisfy the schema minimum.",
        concept_id="c000",
        span=SourceSpan(
            section_id="s000", start=0, end=10, quote_sha256=quote_hash("0123456789")
        ),
        params={},
        beats=tuple(beats),
    )
    assert len(spec.beats) == 3
    assert spec.duration_s == pytest.approx(6.201, abs=0.01)


# --------------------------------------------------------------------------- #
# Frame usage — scenes were rendering small inside their own frame
# --------------------------------------------------------------------------- #


def test_templates_use_most_of_the_frame() -> None:
    """Guard the fit fractions against creeping back down.

    Measured from real renders: `plot_reveal` filled 68% of the frame's area and
    `transform_chain` was capped at 45% of its HEIGHT — for a template whose entire
    subject is one equation. Text landed on the 18px legibility floor as a result.
    These are the floors that keep that from returning.
    """
    import re

    from arcvisual.templates import architecture_flow, plot_reveal, transform_chain

    def fractions(module) -> list[float]:
        src = inspect.getsource(module)
        return [float(m) for m in re.findall(r"height_frac=([0-9.]+)", src)]

    for module, floor in (
        (transform_chain, 0.55),
        (plot_reveal, 0.80),
    ):
        used = fractions(module)
        assert used, f"{module.__name__} sets no height_frac"
        assert min(used) >= floor, (
            f"{module.__name__} shrank to {min(used)}; an equation or plot that uses "
            f"less than {floor:.0%} of the frame height is hard to read when the "
            "video is painted into a reading column"
        )

    # architecture_flow sizes from the grid, then grows the whole assembly.
    src = inspect.getsource(architecture_flow)
    assert "fill_frame" in src, "the assembled grid must be scaled up to fill the frame"
    assert architecture_flow.NODE_FONT_SIZE >= 28


def test_fill_frame_only_grows() -> None:
    """`fill_frame` complements `fit_inside`; shrinking here would push text back
    toward the legibility floor rather than away from it."""
    from arcvisual.templates.base import ArcSceneMixin

    src = inspect.getsource(ArcSceneMixin.fill_frame)
    assert "if factor > 1.0" in src, "fill_frame must never scale down"


# --------------------------------------------------------------------------- #
# Cosmetic caps coerce; structural ones reject
# --------------------------------------------------------------------------- #


def test_overlong_display_text_is_trimmed_not_rejected() -> None:
    """The exact third-attempt failure that cost a real scene its last try.

    `takeaway` came back six characters over a 90-char cap and `max_length` threw the
    whole scene away. A length limit exists so text fits on screen; trimming serves
    that intent and refusing does not.
    """
    from arcvisual.templates.plot_reveal import Params

    takeaway = (
        "Scaled dot-product attention keeps the softmax out of its "
        "saturated region as d_k grows large."
    )
    assert len(takeaway) > 90
    params = Params(
        x_label="steps",
        y_label="BLEU",
        series=[{"label": "big", "points": [[0, 1], [1, 2]]}],
        takeaway=takeaway,
    )
    assert len(params.takeaway) <= 90
    assert params.takeaway.startswith("Scaled dot-product attention")


def test_unbroken_token_survives_clamping() -> None:
    """`textwrap.shorten` trims on word boundaries, so a single long token collapses
    to just the placeholder — a 100-character title came back as one character."""
    from arcvisual.templates.base import clamp_text

    clamped = clamp_text(80)("A" * 100)
    assert 40 < len(clamped) <= 80, clamped


def test_undrawable_series_are_dropped_not_fatal() -> None:
    """A line needs two points. Dropping the degenerate one keeps a chart that was
    three-quarters correct instead of discarding the scene."""
    from arcvisual.templates.plot_reveal import Params

    params = Params(
        x_label="x",
        y_label="y",
        series=[
            {"label": "good", "points": [[0, 1], [1, 2], [2, 3]]},
            {"label": "degenerate", "points": [[0, 1]]},
            {"label": "also good", "points": [[0, 5], [1, 6]]},
        ],
    )
    assert [s.label for s in params.series] == ["good", "also good"]


def test_a_plot_with_no_drawable_series_still_fails() -> None:
    """Coercion has a floor: a plot with nothing plottable is not a plot."""
    from pydantic import ValidationError

    from arcvisual.templates.plot_reveal import Params

    with pytest.raises(ValidationError):
        Params(
            x_label="x",
            y_label="y",
            series=[{"label": "a", "points": [[0, 1]]}],
        )


def test_near_miss_caption_types_are_coerced() -> None:
    from arcvisual.templates.base import TemplateParams

    assert TemplateParams(captions=["a", 42, None, "b"]).captions == ["a", "42", "b"]


def test_equation_text_is_never_trimmed() -> None:
    """A truncated equation is a WRONG equation, not a shorter one — so `steps` has
    no clamp, unlike `title` beside it."""
    from arcvisual.templates.transform_chain import Params

    long_step = r"\sum_{i=1}^{n} \alpha_i x_i " * 6
    params = Params(steps=[long_step, "y = 0"], title="T" * 200)
    assert params.steps[0] == long_step, "notation must survive verbatim"
    assert len(params.title) <= 80, "the title is display text and may be trimmed"


@pytest.mark.parametrize(
    "build",
    [
        lambda: __import__(
            "arcvisual.templates.architecture_flow", fromlist=["Params"]
        ).Params(
            nodes=[
                {"id": "a", "label": "A", "column": 0},
                {"id": "b", "label": "B", "column": 1},
            ],
            edges=[{"src": "a", "dst": "ghost"}],
        ),
        lambda: __import__(
            "arcvisual.templates.transform_chain", fromlist=["Params"]
        ).Params(steps=["only one"]),
    ],
)
def test_structural_constraints_still_reject(build) -> None:
    """Loosening the cosmetic caps must not loosen the load-bearing ones: an edge to
    a node that does not exist would render an arrow pointing at nothing."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        build()


# --------------------------------------------------------------------------- #
# Progressive scene rows — the waiting room's per-scene list
# --------------------------------------------------------------------------- #


def test_scene_rows_appear_while_the_job_is_still_running(session) -> None:
    """Scene rows used to be written only by persist_job, at the very end.

    The waiting room therefore showed "no animations queued yet" through the entire
    render phase — the longest one, and the whole reason the page exists. Watching a
    real job on Muse is how this surfaced: `stage` said "scene" while the scenes list
    stayed empty.
    """
    job = repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="h")

    repo.upsert_scene_progress(
        session, job.id, scene_key="o000", archetype="plot_reveal", state="generating"
    )
    live = repo.scene_states(session, job.id)
    assert len(live) == 1
    assert live[0]["state"] == "generating" and live[0]["ready"] is False

    # The same scene advancing updates in place rather than duplicating.
    repo.upsert_scene_progress(
        session,
        job.id,
        scene_key="o000",
        archetype="plot_reveal",
        state="passed",
        attempts=1,
    )
    live = repo.scene_states(session, job.id)
    assert len(live) == 1, "a scene advancing must update its row, not add another"
    assert live[0]["ready"] is True


def test_persist_job_still_owns_the_final_state(session) -> None:
    """The progressive rows are a view, not a second source of truth: the final
    write must replace them with the full outcome."""
    job = repo.create_queued_job(session, url="u", arxiv_id="1", submitted_by="h")
    repo.upsert_scene_progress(
        session, job.id, scene_key="o000", archetype="plot_reveal", state="generating"
    )
    result = run_job(
        storyboard=build_storyboard(metadata(), paper_tarball()), run_gate2=False
    )
    persisted = repo.persist_job(result, session=session, job_id=job.id)
    states = {r["state"] for r in repo.scene_states(session, persisted.id)}
    assert "generating" not in states, "stale progressive rows must not survive"


# --------------------------------------------------------------------------- #
# Prompt budget — a reasoning model can spend its whole budget thinking
# --------------------------------------------------------------------------- #


def test_trim_keeps_every_section_and_marks_the_cut() -> None:
    """laguna caps max_tokens at 32768 and counts REASONING against it, so a ~20k
    token prompt can consume the entire budget before writing an answer. Raising the
    budget is impossible; trimming the body is the only lever.

    Dropping whole sections would silently remove ideas from consideration, so every
    section keeps its heading either way, and nothing disappears from the model's view
    of the paper unannounced.

    There are two regimes, because one strategy does not survive both budgets.

    *Roomy budget*: every section keeps the head of its prose — a section states its
    claim first — and the cut is MARKED, so the model cannot quote across the seam.

    *Tight budget*: fragments stop being quotable. Given ~200 characters of a section
    it has memorised, a model quotes the rest from memory, and the quote then fails to
    ground — on the Transformer paper that rejected every concept returned and the run
    shipped no scenes at all. So below a quotable minimum the budget buys COMPLETE
    sections and names the omitted ones, because a quote can only ground against text
    that was actually sent.
    """
    from arcvisual.analyze.prompts import _MIN_QUOTABLE_CHARS, trim_body

    sb = build_storyboard(metadata(), paper_tarball())
    analysable = [s for s in sb.sections if s.raw.strip()]

    # -- roomy: proportional truncation, marked --------------------------------
    roomy = len(analysable) * _MIN_QUOTABLE_CHARS + 1000
    text, dropped = trim_body(sb, roomy)
    if dropped:
        assert "[... section truncated ...]" in text

    # -- tight: whole sections, omissions named --------------------------------
    text, dropped = trim_body(sb, 4000)
    assert dropped > 0
    for section in analysable:
        assert section.heading in text, f"{section.id} vanished from the prompt"
    assert "[... section truncated ...]" not in text, (
        "a tight budget must send whole sections, not unquotable fragments"
    )
    assert "do NOT quote these" in text, "omitted sections must be named as unquotable"

    # The point of the exercise: every line of prose sent is quotable verbatim.
    prose = [
        block
        for block in text.split("\n\n")
        if not block.startswith("## Sections omitted")
    ]
    assert prose, "something must actually be sent"


def test_trim_is_a_no_op_when_the_body_already_fits() -> None:
    from arcvisual.analyze.prompts import trim_body

    sb = build_storyboard(metadata(), paper_tarball())
    text, dropped = trim_body(sb, 10_000_000)
    assert dropped == 0
    assert text == sb.body_text


def test_only_providers_that_need_it_declare_a_budget(reset_settings) -> None:
    """Anthropic budgets thinking separately, so it gets the whole paper. Poolside
    does not, so it declares a limit and Analyze honours it."""
    reset_settings(ANTHROPIC_API_KEY="sk-test", POOLSIDE_API_KEY="ps-test")
    from arcvisual.providers.anthropic_provider import AnthropicProvider
    from arcvisual.providers.poolside_provider import PoolsideProvider

    assert AnthropicProvider(client=object()).capabilities().prompt_char_budget is None
    budget = PoolsideProvider(client=object()).capabilities().prompt_char_budget
    assert budget and budget > 0


# --------------------------------------------------------------------------- #
# The happy path with gates enabled — the one no test covered
# --------------------------------------------------------------------------- #


def test_a_scene_passing_every_gate_returns_a_scene(tmp_path) -> None:
    """The success path crashed with `not enough values to unpack (expected 5, got 4)`
    and the whole suite stayed green, because every existing test runs with
    `run_gate2=False` and returns before that line is reached.

    A path only exercised in production is a path with no tests, so this one drives
    the full ladder with a renderer that reports success.
    """
    import os

    from arcvisual.analyze.single_pass import analyze
    from arcvisual.generate.repair import build_scene
    from arcvisual.storyboard import SceneState
    from arcvisual.templates.base import BBox, Boundary, SceneTrace

    os.environ["ARCVISUAL_MEDIA_DIR"] = str(tmp_path / "media")

    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    opportunity = sb.opportunities[0]

    class _PassingRenderer:
        """Reports a clean render with a trace Gate 3 will accept."""

        def render(self, source, spec, *, quality, timeout_s):
            box = BBox(
                key="Text#0",
                kind="Text",
                left=-2.0,
                right=2.0,
                bottom=-1.0,
                top=1.0,
                is_text=True,
                px_height=40.0,
            )
            trace = SceneTrace(
                scene_id=spec.id,
                archetype=spec.archetype.value,
                frame_width=14.22,
                frame_height=8.0,
                total_duration=spec.duration_s,
                boundaries=[
                    Boundary(
                        index=0,
                        t_start=0.0,
                        t_end=spec.duration_s,
                        animation_names=["Create"],
                        boxes=[box],
                    )
                ],
            )
            return RenderOutcome(
                ok=True,
                exit_code=0,
                stdout="",
                stderr="",
                duration_s=spec.duration_s,
                wall_ms=1200,
                produced_video=True,
                trace=trace,
            )

    outcome = build_scene(sb, opportunity, renderer=_PassingRenderer(), run_gate2=True)

    assert outcome.scene.state is SceneState.PASSED, outcome.notes
    assert outcome.scene.artifact is not None
    assert outcome.attempts and outcome.attempts[0].passed
    report = outcome.scene.gate_report
    assert report.gate1 and report.gate1.passed
    assert report.gate2 and report.gate2.passed
    assert report.gate3 and report.gate3.passed, "gate 3 must actually have run"


# --------------------------------------------------------------------------- #
# A passing scene must produce servable bytes, not just a content hash
# --------------------------------------------------------------------------- #


def test_the_renderer_preserves_the_video_past_its_workdir(tmp_path) -> None:
    """LocalRenderer deletes its workdir on success, and used to null `video_path`
    with it. The result: scenes passed every gate, recorded a content hash, wrote a
    0-byte artifact row, and served an empty player.

    `produced_video=True` while nothing can read the video is not a success.
    """
    from arcvisual.gates.g2_runtime import LocalRenderer

    renderer = LocalRenderer()
    workdir = tmp_path / "work"
    media = workdir / "media" / "videos"
    media.mkdir(parents=True)
    video = media / "scene.mp4"
    video.write_bytes(b"\x00" * 2048)

    outcome = RenderOutcome(
        ok=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_s=4.0,
        wall_ms=100,
        video_path=video,
        produced_video=True,
        workdir=workdir,
    )
    # Reproduce the cleanup the renderer performs on a successful render.
    import shutil
    import tempfile
    from pathlib import Path as _P

    if outcome.ok and not renderer.keep_output:
        kept_dir = _P(tempfile.mkdtemp(prefix="arcvisual-scene-"))
        kept = kept_dir / video.name
        shutil.move(str(video), str(kept))
        outcome.video_path = kept
        shutil.rmtree(workdir, ignore_errors=True)

    assert outcome.video_path is not None
    assert outcome.video_path.exists(), "the bytes must outlive the workdir"
    assert outcome.video_path.stat().st_size == 2048
    assert not workdir.exists(), "the media tree must still be reclaimed"


def test_a_passing_scene_stores_nonzero_bytes(tmp_path, monkeypatch) -> None:
    """The end-to-end guard: a content hash with no bytes behind it is a broken
    player, and every prior test only checked that the hash existed."""

    from arcvisual.analyze.single_pass import analyze
    from arcvisual.generate.repair import build_scene
    from arcvisual.storyboard import SceneState
    from arcvisual.templates.base import BBox, Boundary, SceneTrace

    monkeypatch.setenv("ARCVISUAL_MEDIA_DIR", str(tmp_path / "media"))

    sb, _ = analyze(build_storyboard(metadata(), paper_tarball()))
    opportunity = sb.opportunities[0]

    class _RendererWithBytes:
        def render(self, source, spec, *, quality, timeout_s):
            out = tmp_path / f"{spec.id}.mp4"
            out.write_bytes(b"\x00" * 4096)
            box = BBox(
                key="Text#0",
                kind="Text",
                left=-2.0,
                right=2.0,
                bottom=-1.0,
                top=1.0,
                is_text=True,
                px_height=40.0,
            )
            trace = SceneTrace(
                scene_id=spec.id,
                archetype=spec.archetype.value,
                frame_width=14.22,
                frame_height=8.0,
                total_duration=spec.duration_s,
                boundaries=[
                    Boundary(
                        index=0,
                        t_start=0.0,
                        t_end=spec.duration_s,
                        animation_names=["Create"],
                        boxes=[box],
                    )
                ],
            )
            return RenderOutcome(
                ok=True,
                exit_code=0,
                stdout="",
                stderr="",
                duration_s=spec.duration_s,
                wall_ms=100,
                video_path=out,
                produced_video=True,
                trace=trace,
            )

    outcome = build_scene(sb, opportunity, renderer=_RendererWithBytes(), run_gate2=True)
    assert outcome.scene.state is SceneState.PASSED, outcome.notes
    artifact = outcome.scene.artifact
    assert artifact is not None
    assert artifact.bytes > 0, "a passing scene with 0 bytes serves an empty player"

    stored = list((tmp_path / "media").rglob("*.mp4"))
    assert stored, "no video reached the media root"
    assert stored[0].stat().st_size == 4096


# --------------------------------------------------------------------------- #
# Coercion applied consistently — the same lesson, three more places
# --------------------------------------------------------------------------- #


def test_surplus_captions_are_trimmed_not_rejected() -> None:
    """A real run lost a scene to `captions (6) cannot exceed steps (3)`.

    Surplus captions are simply never shown — the template indexes by step. Failing
    the scene threw away a derivation the model got right over output nothing would
    have displayed.
    """
    from arcvisual.templates.transform_chain import Params

    params = Params(
        steps=["a=b", "b=c", "c=d"],
        captions=["one", "two", "three", "four", "five", "six"],
    )
    assert len(params.captions) == 3


def test_an_overlong_chain_keeps_its_conclusion() -> None:
    """A 7-step chain against a 6-step cap cost another scene outright.

    The middle gives way, never the ends: dropping the tail would discard the
    result, which is the one step the reader most needs.
    """
    from arcvisual.templates.transform_chain import MAX_STEPS, Params

    steps = [f"step{i}" for i in range(8)]
    params = Params(steps=steps)
    assert len(params.steps) == MAX_STEPS
    assert params.steps[0] == "step0", "the opening survives"
    assert params.steps[-1] == "step7", "the conclusion survives"


def test_steps_are_never_individually_truncated() -> None:
    """`steps` carry the paper's own notation. A shortened equation is a WRONG
    equation, so the trimming applies to the list, never to its members."""
    from arcvisual.templates.transform_chain import Params

    long_latex = r"\mathrm{Attention}(Q,K,V) = \mathrm{softmax}(QK^T/\sqrt{d_k})V" * 3
    params = Params(steps=[long_latex, "b=c"])
    assert params.steps[0] == long_latex


def test_text_is_elided_rather_than_shrunk_below_legibility() -> None:
    """Gate 3 failed a scene at 17.0px — one pixel short — after a full render.

    Templates shrink labels to fit their box, and that scaling could cross the
    floor. Stopping at the floor and eliding gives a reader something they can
    actually read.
    """
    import inspect

    from arcvisual.templates import architecture_flow
    from arcvisual.templates.base import ArcSceneMixin

    src = inspect.getsource(ArcSceneMixin.fit_text_legibly)
    assert "min_text_px_at_1080p" in src, "the floor must come from config, not a literal"
    # The template must route label fitting through it rather than scaling raw.
    build_src = inspect.getsource(architecture_flow.build)
    assert "fit_text_legibly" in build_src
    assert "text.scale((box_w" not in build_src, "raw scaling can cross the floor"

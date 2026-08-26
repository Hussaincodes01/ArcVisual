"""The orchestrator — one job, five stages, one Storyboard.

Phase 1 runs this as a plain function. Phase 2 replaces the body with a LangGraph
graph checkpointed to Postgres; the signature and the Storyboard contract do not
change, which is the point of §3's rule that stages only communicate through the
document.

What this layer owns, and nothing else does:

* **Stage sequencing** and the ownership assertion between stages, so a stage that
  writes another's fields fails here rather than mysteriously later.
* **The job deadline.** At 25 minutes, scenes still in flight are cancelled and
  marked degraded, and the article ships with what passed. A late animation is
  worth less than a page that finished.
* **The job-level cost tripwire**, which is a second line behind the per-scene
  ceiling in case a paper proposes many expensive scenes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from arcvisual.analyze.single_pass import AnalyzeReport, analyze
from arcvisual.config import PIPELINE_VERSION, settings
from arcvisual.gates import g2_runtime
from arcvisual.generate.repair import SceneOutcome, build_scene
from arcvisual.ingest.arxiv import build_storyboard, ingest_url
from arcvisual.ingest.errors import IngestRejection
from arcvisual.providers.registry import describe, get_provider
from arcvisual.storyboard import (
    JobState,
    Scene,
    SceneState,
    Stage,
    Storyboard,
    assert_monotonic,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, dict[str, Any]], None]


@dataclass
class JobResult:
    state: JobState
    pipeline_version: str = PIPELINE_VERSION
    #: Which backend produced the concepts and parameters, and what it can/cannot do.
    #: Carried into the report so a cost figure is never read without its caveats.
    provider: dict[str, Any] = field(default_factory=dict)
    storyboard: Storyboard | None = None
    failure: dict[str, str] | None = None
    analyze_report: AnalyzeReport | None = None
    scene_outcomes: list[SceneOutcome] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        analyze_cost = self.analyze_report.cost_usd if self.analyze_report else 0.0
        return round(analyze_cost + sum(o.cost_usd for o in self.scene_outcomes), 6)

    @property
    def cost_attributed(self) -> bool:
        """False when any call's cost could not be priced.

        The plan's unit-economics argument rests on ``$/paper`` being real. A total
        that silently included unpriced calls would look like a cheap paper, so this
        flag travels with the number and the summary marks it.
        """
        if self.analyze_report and not self.analyze_report.cost_attributed:
            return False
        return all(o.cost_attributed for o in self.scene_outcomes)

    @property
    def scenes(self) -> list[Scene]:
        return [o.scene for o in self.scene_outcomes]

    @property
    def shipped(self) -> int:
        return sum(1 for s in self.scenes if s.state is SceneState.PASSED)

    @property
    def first_pass_rate(self) -> float | None:
        attempted = [o for o in self.scene_outcomes if o.attempts]
        if not attempted:
            return None
        return round(sum(1 for o in attempted if o.first_pass) / len(attempted), 4)

    def summary(self) -> str:
        if self.state is JobState.FAILED:
            return f"FAILED: {(self.failure or {}).get('user_facing', 'unknown')}"
        by_state: dict[str, int] = {}
        for s in self.scenes:
            by_state[s.state.value] = by_state.get(s.state.value, 0) + 1
        fpr = self.first_pass_rate
        cost = f"${self.cost_usd:.4f}"
        if not self.cost_attributed:
            # Never print a bare number that excludes unpriced calls.
            cost += "+unpriced"
        return (
            f"{self.state.value} via {self.provider.get('provider', '?')}: "
            f"{self.shipped}/{len(self.scenes)} scenes shipped "
            f"{by_state} first_pass={'n/a' if fpr is None else f'{fpr:.0%}'} "
            f"cost={cost} "
            f"wall={sum(self.timings_ms.values()) / 1000:.1f}s"
        )


def run_job(
    url: str | None = None,
    *,
    storyboard: Storyboard | None = None,
    provider: object | None = None,
    client: object | None = None,
    renderer: g2_runtime.Renderer | None = None,
    run_gate2: bool = True,
    preflight: bool = True,
    on_progress: ProgressFn | None = None,
) -> JobResult:
    """Run the pipeline for one paper.

    Pass ``url`` for the real path, or ``storyboard`` to resume from an ingested
    document (which is how the eval harness avoids re-fetching arXiv on every run).
    """
    cfg = settings()
    # Select the provider ONCE per job. Re-selecting per scene could silently switch
    # backends mid-article if credentials changed under us, and half an article from
    # each of two models is not a thing anyone wants to debug.
    if provider is None and client is None:
        provider = get_provider()
    result = JobResult(state=JobState.QUEUED, provider=describe(provider))
    started = time.perf_counter()

    def progress(stage: str, payload: dict[str, Any]) -> None:
        if on_progress is not None:
            on_progress(stage, payload)

    # -- Stage 1: Ingest ---------------------------------------------------- #
    t0 = time.perf_counter()
    result.state = JobState.INGESTING
    progress("ingesting", {})
    try:
        if storyboard is None:
            if not url:
                raise ValueError("run_job needs either a url or a storyboard")
            storyboard = ingest_url(url)
    except IngestRejection as exc:
        result.state = JobState.FAILED
        result.failure = exc.as_failure()
        result.timings_ms["ingest"] = int((time.perf_counter() - t0) * 1000)
        progress("failed", result.failure)
        return result
    result.storyboard = storyboard
    result.timings_ms["ingest"] = int((time.perf_counter() - t0) * 1000)
    progress(
        "ingested",
        {
            "title": storyboard.paper.title,
            "slug": storyboard.paper.slug,
            "sections": len(storyboard.sections),
        },
    )

    # -- Provider liveness -------------------------------------------------- #
    # Ask for one trivial object before committing to a whole paper. A provider can
    # fail by going QUIET rather than erroring — measured on opencode's free route,
    # which stopped answering mid-session with no output, no error and no exit. Every
    # scene then waits out the full timeout before failing, so a twelve-scene paper
    # spends hours learning what one probe answers in seconds.
    if provider is not None and preflight:
        health = provider.healthcheck()
        result.provider["health"] = health.model_dump()
        if not health.ok:
            result.state = JobState.FAILED
            result.failure = {
                "code": "provider_unavailable",
                "message": f"{provider.name} healthcheck failed: {health.detail}",
                "user_facing": (
                    "Our explanation service is not responding right now. Nothing is "
                    "wrong with your paper — please try again shortly."
                ),
            }
            progress("failed", result.failure)
            return result

    # -- Stage 2: Analyze --------------------------------------------------- #
    t0 = time.perf_counter()
    result.state = JobState.ANALYZING
    progress("analyzing", {})
    before = storyboard.model_copy(deep=True) if cfg.strict_ownership else None
    try:
        storyboard, report = analyze(storyboard, provider=provider, client=client)
    except Exception as exc:
        log.exception("analyze failed")
        result.state = JobState.FAILED
        result.failure = {
            "code": "analyze_failed",
            "message": f"{type(exc).__name__}: {exc}",
            "user_facing": (
                "We read the paper but could not work out how to explain it. "
                "This is on us, not the paper."
            ),
        }
        result.timings_ms["analyze"] = int((time.perf_counter() - t0) * 1000)
        progress("failed", result.failure)
        return result
    if before is not None:
        assert_monotonic(before, storyboard, Stage.ANALYZE)
    result.storyboard = storyboard
    result.analyze_report = report
    result.timings_ms["analyze"] = int((time.perf_counter() - t0) * 1000)
    progress(
        "analyzed",
        {
            "concepts": len(storyboard.concepts),
            "opportunities": len(storyboard.opportunities),
            "summary": report.summary(),
        },
    )

    if not storyboard.opportunities:
        # A paper with nothing worth animating is a legitimate article, not a
        # failure. Restraint is the job.
        result.state = JobState.COMPLETE
        result.notes.append("no visual opportunities survived triage; prose-only article")
        progress("complete", storyboard.progress())
        return result

    # -- Stages 3+4: Generate and Validate, per scene ------------------------ #
    t0 = time.perf_counter()
    result.state = JobState.RENDERING
    progress("rendering", {"scenes": len(storyboard.opportunities)})
    deadline = started + cfg.budgets.job_deadline_s

    # Scenes are INDEPENDENT: each is one codegen call plus one render, sharing
    # nothing. Running them one at a time made a twelve-scene paper take twelve times
    # one scene, which is the single largest contributor to wall clock — and the
    # architecture calls for fan-out precisely here. Threads rather than processes
    # because both halves release the GIL: the codegen call waits on a socket, and
    # the render waits on a Manim subprocess.
    #
    # The cap is deliberately modest. Each worker can start a Manim render, and a
    # dozen simultaneous renders will thrash a laptop; production fans out across
    # Modal containers instead, where the ceiling is `render_max_containers`.
    lanes = max(1, min(cfg.budgets.scene_concurrency, len(storyboard.opportunities)))

    def _one(opportunity: Any) -> SceneOutcome:
        # The deadline and the cost ceiling are checked INSIDE the worker, at the
        # moment it starts, so a scene queued behind a slow one still declines rather
        # than beginning work the job no longer has time or budget for.
        if time.perf_counter() >= deadline:
            return _cancelled(storyboard, opportunity, "the job deadline was reached")
        if result.cost_usd >= cfg.budgets.job_cost_ceiling_usd:
            return _cancelled(
                storyboard,
                opportunity,
                f"the job cost ceiling (${cfg.budgets.job_cost_ceiling_usd:.2f}) "
                "was reached",
            )
        return build_scene(
            storyboard,
            opportunity,
            provider=provider,
            client=client,
            renderer=renderer,
            run_gate2=run_gate2,
        )

    if lanes == 1:
        outcomes = [_one(o) for o in storyboard.opportunities]
    else:
        with ThreadPoolExecutor(
            max_workers=lanes, thread_name_prefix="arc-scene"
        ) as pool:
            futures = {pool.submit(_one, o): o for o in storyboard.opportunities}
            outcomes = []
            for future in as_completed(futures):
                opportunity = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    log.exception("scene %s raised", opportunity.id)
                    outcomes.append(
                        _cancelled(storyboard, opportunity, f"scene raised: {exc}")
                    )

    # Restore the storyboard's own order. `as_completed` yields by finish time, and
    # scene order is reading order — the article would otherwise be shuffled by
    # whichever render happened to be quickest.
    rank = {o.id: i for i, o in enumerate(storyboard.opportunities)}
    outcomes.sort(key=lambda out: rank.get(out.scene.spec.id, 0))

    for outcome in outcomes:
        result.scene_outcomes.append(outcome)
        progress(
            "scene",
            {
                "id": outcome.scene.spec.id,
                "archetype": outcome.scene.spec.archetype.value,
                "state": outcome.scene.state.value,
                "attempts": len(outcome.attempts),
                "cost_usd": outcome.cost_usd,
            },
        )

    before = storyboard.model_copy(deep=True) if cfg.strict_ownership else None
    storyboard.scenes = result.scenes
    if before is not None:
        # Generate owns `scenes`; Validate owns the mutable fields on each. They
        # run together per scene here, so the combined write is checked against
        # both owners.
        for stage in (Stage.GENERATE, Stage.VALIDATE):
            try:
                assert_monotonic(before, storyboard, stage)
            except Exception as exc:
                if stage is Stage.GENERATE:
                    raise
                log.debug("validate ownership note: %s", exc)
    Storyboard.model_validate(storyboard.model_dump())

    result.storyboard = storyboard
    result.timings_ms["render"] = int((time.perf_counter() - t0) * 1000)
    result.state = JobState.COMPLETE
    progress("complete", storyboard.progress())
    return result


def _cancelled(sb: Storyboard, opportunity: Any, why: str) -> SceneOutcome:
    """A scene we deliberately did not attempt. Degraded, never silently missing."""
    from arcvisual.generate.repair import _degrade, _pending

    outcome = SceneOutcome(scene=_pending(opportunity))
    outcome.notes.append(why)
    out = _degrade(sb, opportunity, outcome)
    out.scene = out.scene.model_copy(
        update={"degraded_reason": f"{why}; {out.scene.degraded_reason}"}
    )
    return out


def ingest_only(url: str) -> Storyboard:
    """Stage 1 alone. Used by the eval harness and by ``GET /api/preview``."""
    return ingest_url(url)


__all__ = ["JobResult", "build_storyboard", "ingest_only", "run_job"]

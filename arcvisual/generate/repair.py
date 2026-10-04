"""The repair ladder — one scene, from proposal to a terminal outcome.

    attempt 1  fail -> feed the failing gate output back    -> attempt 2
    attempt 2  fail -> simplify the parameters              -> attempt 3
    attempt 3  fail -> degrade to the paper's own figure
               fail -> prose only

Three properties matter more than the ladder itself:

* **Every attempt is recorded**, pass or fail, into ``scene_attempts``. That table
  is the per-archetype first-pass-rate metric, which is the evidence for the
  template library being worth its cost. Discarding failures would discard the
  only proof.
* **The budget is checked in dollars before each attempt**, not counted in
  attempts afterwards. Three attempts does not bound spend when one of them can
  be a large codegen call; a ceiling does.
* **A scene failure never fails the job.** The function always returns a terminal
  scene. A section with good prose and no animation is a fine outcome; a section
  with a garbled animation is a product failure.

**Phase 1 scope note.** The plan's rung 2 is "drop to a simpler template for the
same concept". With three templates there is nowhere sensible to fall back *to* —
a failing derivation does not become a bar chart. So rung 2 simplifies the
parameters within the same template (each template implements ``simplify``), and
cross-template fallback arrives with the Phase 3 library. Rung 3 degrades to the
paper's own figure, which is the honest Phase 1 substitute for the annotated-
figure template that does not exist yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from arcvisual.cache.hashing import RenderEnv, content_hash
from arcvisual.config import settings
from arcvisual.gates import g1_static, g2_runtime, g3_spatial
from arcvisual.generate.codegen import (
    GenerateResult,
    fit_beats,
    generate_scene,
    render_module,
)
from arcvisual.storyboard import (
    Artifact,
    GateReport,
    Scene,
    SceneState,
    Storyboard,
    VisualOpportunity,
)
from arcvisual.templates import registry

log = logging.getLogger(__name__)


@dataclass
class Attempt:
    """One row of ``scene_attempts``."""

    attempt_no: int
    params: dict[str, Any]
    generated_src: str
    gate_report: GateReport
    failed_gate: int | None
    render_ms: int
    cost_usd: float
    simplified: bool = False

    @property
    def passed(self) -> bool:
        return self.failed_gate is None


@dataclass
class SceneOutcome:
    scene: Scene
    attempts: list[Attempt] = field(default_factory=list)
    source: str = ""
    #: False when any attempt's cost could not be attributed to a rate.
    cost_attributed: bool = True
    provider: str = "heuristic"
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self.attempts), 6)

    @property
    def first_pass(self) -> bool:
        return bool(self.attempts) and self.attempts[0].passed


def build_scene(
    sb: Storyboard,
    opportunity: VisualOpportunity,
    *,
    provider: object | None = None,
    client: object | None = None,
    renderer: g2_runtime.Renderer | None = None,
    run_gate2: bool = True,
) -> SceneOutcome:
    """Take one opportunity to a terminal state. Never raises for scene reasons."""
    cfg = settings()
    budgets = cfg.budgets
    outcome = SceneOutcome(scene=_pending(opportunity))
    findings: list[str] = []
    previous_params: dict[str, Any] | None = None
    spent = 0.0
    simplify_next = False

    for attempt_no in range(1, budgets.max_attempts + 1):
        # The ceiling is checked *before* spending, which is the only place it
        # can actually prevent a cost overrun.
        if spent >= budgets.scene_cost_ceiling_usd:
            outcome.notes.append(
                f"stopped after attempt {attempt_no - 1}: spent ${spent:.3f} of the "
                f"${budgets.scene_cost_ceiling_usd:.2f} per-scene ceiling"
            )
            break

        try:
            gen = generate_scene(
                sb,
                opportunity,
                provider=provider,
                client=client,
                previous=previous_params,
                findings=findings or None,
                attempt=attempt_no,
            )
        except Exception as exc:
            log.warning("codegen raised for %s: %s", opportunity.id, exc)
            outcome.notes.append(f"codegen error on attempt {attempt_no}: {exc}")
            break

        spent += gen.cost_usd
        # Captured BEFORE any simplification: _regenerate_with rebuilds the module
        # locally and reports 0.0, so reading gen.cost_usd afterwards would drop the
        # codegen call that actually happened out of the scene and job totals — and
        # out of the ceilings that are supposed to bound them.
        attempt_cost = gen.cost_usd
        outcome.provider = gen.provider
        outcome.cost_attributed = outcome.cost_attributed and gen.cost_attributed
        if gen.params_obj is None:
            # Parameters did not validate: Gate 1's parameter half, without a
            # module to lint. Feed the validation messages straight back.
            report = GateReport(
                gate1=g1_static.GateResult(
                    gate=1, passed=False, findings=gen.notes, payload={"stage": "params"}
                )
            )
            outcome.attempts.append(
                Attempt(attempt_no, {}, "", report, 1, 0, gen.cost_usd)
            )
            findings = gen.notes
            previous_params = None
            # Invalid parameters are worth one plain retry with the validation
            # messages before falling back to a simpler shape.
            simplify_next = attempt_no >= 2
            continue

        params = gen.params_obj
        if simplify_next:
            simpler = _simplify(opportunity, params)
            if simpler is not None:
                params = simpler
                gen = _regenerate_with(gen, opportunity, params)
                outcome.notes.append(f"attempt {attempt_no}: simplified parameters")

        # L2 CACHE. The content hash covers the params, the beats, the template's
        # own source and the pinned Manim version — so an identical hash means an
        # identical video, already rendered. Computing it and never checking it (the
        # previous behaviour) meant re-running a paper re-rendered every scene from
        # scratch, which is precisely the difference the plan calls out between a
        # $4 paper and a $40 one.
        cached = _cached_artifact(gen)
        if cached is not None:
            log.info("cache hit for %s (%s)", opportunity.id, cached.content_hash[:12])
            outcome.scene = gen.scene.model_copy(
                update={
                    "state": SceneState.PASSED,
                    "attempts": attempt_no,
                    "cost_usd": outcome.cost_usd,
                    "gate_report": GateReport(),
                    "artifact": cached,
                }
            )
            outcome.source = gen.source
            outcome.notes.append("reused a cached render (identical content hash)")
            return outcome

        report, failed_gate, render_ms, environment_problem, render_outcome = _run_gates(
            gen, renderer=renderer, run_gate2=run_gate2
        )
        outcome.attempts.append(
            Attempt(
                attempt_no=attempt_no,
                params=params.model_dump(mode="json"),
                generated_src=gen.source,
                gate_report=report,
                failed_gate=failed_gate,
                render_ms=render_ms,
                cost_usd=attempt_cost,
                simplified=simplify_next,
            )
        )

        if failed_gate is None:
            outcome.scene = gen.scene.model_copy(
                update={
                    "state": SceneState.PASSED,
                    "attempts": attempt_no,
                    "cost_usd": outcome.cost_usd,
                    "gate_report": report,
                    "artifact": _artifact_for(gen, report, render_outcome),
                }
            )
            outcome.source = gen.source
            return outcome

        if environment_problem:
            # No parameter change installs a missing toolchain. Retrying spends real
            # renders — and real money on a paid provider — on something the model
            # cannot fix, and records a first-pass rate that indicts the template for
            # the deployment's missing dependency. Stop and degrade.
            outcome.notes.append(
                "stopped after one attempt: the failure is in the render "
                "environment, not the scene"
            )
            break

        findings = report.findings()
        previous_params = params.model_dump(mode="json")
        # The ladder, and the reason for `>= 2` rather than `>= 1`:
        #   attempt 1 fails -> attempt 2 retries the SAME template with the gate
        #                      findings fed back (rung 1)
        #   attempt 2 fails -> attempt 3 simplifies the parameters (rung 2)
        # With `>= 1` this was always true, so rung 1 never ran and every scene
        # jumped straight to a reduced version of itself on its second try.
        simplify_next = attempt_no >= 2
        log.info(
            "scene %s failed gate %s on attempt %s",
            opportunity.id,
            failed_gate,
            attempt_no,
        )

    return _degrade(sb, opportunity, outcome)


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def _run_gates(
    gen: GenerateResult,
    *,
    renderer: g2_runtime.Renderer | None,
    run_gate2: bool,
) -> tuple[GateReport, int | None, int, bool, object | None]:
    report = GateReport()
    # In client render mode the generated module is never executed — the browser
    # animates the parameters — so linting it would spend a subprocess per scene on
    # code that cannot run. Parameter validation, the part that still matters, stays.
    g1, _ = g1_static.run(
        gen.source,
        gen.scene.spec.archetype,
        gen.params_obj.model_dump(mode="json"),
        run_ruff=not settings().client_render,
    )
    report.gate1 = g1
    if not g1.passed:
        return report, 1, 0, False, None

    if not run_gate2:
        return report, None, 0, False, None

    g2, out = g2_runtime.run(gen.source, gen.scene.spec, renderer)
    report.gate2 = g2
    render_ms = out.wall_ms if out is not None else 0
    if g2.payload.get("skipped"):
        # No renderer here. Gate 2 is inconclusive, not failed — refusing to ship
        # would make CI impossible, and pretending it passed would be a lie. The
        # inconclusive result is recorded so it is visible in the trace.
        g2.passed = True
        g2.findings.append("gate 2 inconclusive: no render backend available")
        # No render means no bbox trace, so Gate 3 has nothing to read either.
        # Recording that is better than silently reporting a scene as fully gated.
        report.gate3 = g3_spatial.GateResult(
            gate=3,
            passed=True,
            findings=["gate 3 skipped: no render produced a trace to check"],
            payload={"checked": [], "skipped": True},
        )
        return report, None, render_ms, False, out
    if not g2.passed:
        return report, 2, render_ms, bool(g2.payload.get("environment_failure")), out

    # Gate 3 reads the bbox trace Gate 2's render just produced. It is what catches a
    # scene that rendered cleanly and is still unreadable — text off-frame, a label at
    # 9px, a scene that plays for twelve seconds while nothing moves. Gate 2 sees a
    # zero exit and a plausible duration for all of those.
    g3 = g3_spatial.run(
        out.trace if out is not None else None,
        gen.scene.spec,
        video_path=out.video_path if out is not None else None,
    )
    report.gate3 = g3
    if not g3.passed:
        return report, 3, render_ms, False, out
    # The success path. `out` carries the rendered file, and _artifact_for needs it to
    # store the bytes — returning without it crashed every scene that passed all
    # gates, while every test kept passing because they run with run_gate2=False and
    # return before this line.
    return report, None, render_ms, False, out


def _cached_artifact(gen: GenerateResult) -> Artifact | None:
    """An already-rendered artifact for this exact scene, or None.

    Checks the database row AND that the bytes are still on disk. A row alone is not
    evidence — an earlier bug recorded artifacts with zero bytes, and trusting the
    row would have served empty players from the cache forever.
    """
    if settings().client_render:
        # Nothing is rendered, so there is nothing to reuse — and a lookup here would
        # be a database round trip per scene for a guaranteed miss.
        return None
    try:
        from arcvisual.db import repo
        from arcvisual.render import storage
    except ImportError:  # pragma: no cover
        return None

    try:
        env = RenderEnv.for_template(
            registry.get(gen.scene.spec.archetype).module, quality="draft"
        )
        ch = content_hash(gen.scene.spec, env)
        with repo.session_factory()() as session:
            row = repo.cached_artifact(session, ch)
            if row is None or not row.bytes:
                return None
            if not storage.has_scene_local(row.mp4_key):
                return None
            return Artifact(
                content_hash=row.content_hash,
                mp4_key=row.mp4_key,
                webm_key=row.webm_key,
                poster_key=row.poster_key,
                framestrip_key=row.framestrip_key,
                duration_s=row.duration_s,
                quality=row.quality,
                bytes=row.bytes,
            )
    except Exception as exc:
        log.debug("cache lookup unavailable: %s", exc)
        return None


def _artifact_for(
    gen: GenerateResult, report: GateReport, outcome: object | None = None
) -> Artifact | None:
    """The draft artifact recorded once the gates pass.

    Phase 1 ships the draft; the final-quality re-render is a Stage-3 concern that
    the pipeline schedules separately, so gates are never paid for at full quality.
    """
    env = RenderEnv.for_template(
        registry.get(gen.scene.spec.archetype).module, quality="draft"
    )
    ch = content_hash(gen.scene.spec, env)
    from arcvisual.cache.hashing import r2_keys

    keys = r2_keys(ch)
    payload = (report.gate2.payload if report.gate2 else {}) or {}

    if settings().client_render and getattr(outcome, "video_path", None) is None:
        # The reader animates the validated parameters itself. Say so explicitly:
        # a draft artifact with zero bytes would make the reader request a video
        # that was never produced and show a broken player.
        return Artifact(
            content_hash=ch,
            duration_s=float(gen.scene.spec.duration_s),
            quality="client",
            bytes=0,
            **keys,
        )

    # Put the bytes somewhere the reader can fetch them. Without this the pipeline
    # renders a video, records its content hash, and serves an empty player — which
    # is exactly what a run with no R2 credentials did.
    video = getattr(outcome, "video_path", None)
    stored_bytes = 0
    written: dict[str, int] = {}
    if video is not None:
        try:
            from arcvisual.render import storage

            # Derive the poster and WebM before storing. Without this the local path
            # writes scene.mp4 alone while the artifact record still advertises
            # poster_key and webm_key, so the reader renders a <video> whose poster
            # 404s on every scene. `derive_assets` was only ever called on the Modal
            # path, so the whole local experience shipped with broken posters.
            assets = None
            try:
                assets = storage.derive_assets(video, video.parent / "derived")
            except Exception as exc:  # ffmpeg absent or a codec missing
                log.info("could not derive poster/webm for %s: %s", gen.scene.spec.id, exc)

            # save_scene_local returns {key: bytes_written}, not paths.
            written = storage.save_scene_local(video, keys, assets=assets)
            stored_bytes = sum(written.values())
            log.info(
                "stored %s assets (%s bytes) for %s",
                len(written),
                stored_bytes,
                gen.scene.spec.id,
            )
        except Exception as exc:
            log.warning("could not store assets for %s: %s", gen.scene.spec.id, exc)
        finally:
            # The renderer moved this out of its workdir so we could copy it; the
            # copy is now the durable one, so release the interim file rather than
            # leaving a temp directory per scene behind.
            try:
                video.unlink(missing_ok=True)
                video.parent.rmdir()
            except OSError:
                pass
    # Advertise ONLY what was actually written. A key for a file that does not exist
    # is worse than a missing key: the reader has no way to tell them apart, so it
    # emits a <source> and a poster that 404 rather than falling back gracefully.
    # mp4_key and poster_key are required by the schema, so they keep their nominal
    # value; the optional webm_key is dropped unless its file landed.
    advertised = dict(keys)
    if written and keys.get("webm_key") not in written:
        advertised["webm_key"] = None
    return Artifact(
        content_hash=ch,
        duration_s=float(payload.get("rendered_duration_s") or gen.scene.spec.duration_s),
        quality="draft",
        bytes=stored_bytes,
        **advertised,
    )


def _simplify(opportunity: VisualOpportunity, params: Any) -> Any | None:
    template = registry.get(opportunity.archetype)
    if template.simplify is None:
        return None
    try:
        return template.simplify(params)
    except Exception as exc:
        log.warning("simplify failed for %s: %s", opportunity.archetype.value, exc)
        return None


def _regenerate_with(
    gen: GenerateResult, opportunity: VisualOpportunity, params: Any
) -> GenerateResult:
    """Rebuild the spec and module around simplified parameters.

    Beats are **refit**, not truncated. Simplifying drops elements, so the template
    now plays for less time; slicing the old beat list keeps the original durations
    and leaves the spec claiming a runtime the scene no longer has. Gate 2's ±20%
    duration assertion would then fail on drift the simplification itself introduced —
    turning the repair rung into a guaranteed second failure.
    """
    template = registry.get(opportunity.archetype)
    captions = list(getattr(params, "captions", []) or [])
    if not captions:
        captions = [b.caption for b in gen.scene.spec.beats]
    beats = tuple(fit_beats(captions, template.estimate_duration(params)))
    spec = gen.scene.spec.model_copy(
        update={"params": params.model_dump(mode="json"), "beats": beats}
    )
    return GenerateResult(
        scene=gen.scene.model_copy(update={"spec": spec}),
        source=render_module(spec, params),
        params_obj=params,
        cost_usd=0.0,  # a simplification costs nothing; it is local
        notes=gen.notes,
    )


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def _pending(opportunity: VisualOpportunity) -> Scene:
    """A placeholder scene, so a caller always has something to record."""
    from arcvisual.storyboard import Beat, SceneSpec

    return Scene(
        spec=SceneSpec(
            id=opportunity.id,
            archetype=opportunity.archetype,
            claim=opportunity.claim,
            concept_id=opportunity.concept_id,
            span=opportunity.span,
            params={},
            beats=(Beat(t=0.0, dur=0.8, caption=""),),
        ),
        state=SceneState.PENDING,
    )


def _degrade(
    sb: Storyboard, opportunity: VisualOpportunity, outcome: SceneOutcome
) -> SceneOutcome:
    """Rung 3. Never ship a broken slot; ship a smaller true thing or nothing."""
    last = outcome.attempts[-1] if outcome.attempts else None
    why = (
        f"failed gate {last.failed_gate} after {len(outcome.attempts)} attempts"
        if last
        else "no attempt completed"
    )
    section = sb.section(opportunity.span.section_id)
    figures = [f for f in sb.figures if f.id in section.figure_ids]

    if figures:
        reason = (
            f"{why}; showing the paper's own figure ({figures[0].id}) with the "
            "explanation in prose instead"
        )
    else:
        reason = f"{why}; this section ships as prose only"

    outcome.scene = outcome.scene.model_copy(
        update={
            "state": SceneState.DEGRADED if figures else SceneState.FAILED,
            "attempts": len(outcome.attempts),
            "cost_usd": outcome.cost_usd,
            "gate_report": last.gate_report if last else None,
            "degraded_reason": reason,
            "artifact": None,
        }
    )
    outcome.notes.append(reason)
    return outcome

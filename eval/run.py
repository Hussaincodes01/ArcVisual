"""The Phase 1 exit test, mechanised.

    python -m eval.run --assert-no-code-changes

Exit criterion from the plan: **five papers end-to-end without touching the code
between runs.** That phrase is only meaningful if something checks it, so
``--assert-no-code-changes`` hashes every source file before the first paper and
after the last and fails if anything moved. A harness that lets you tweak a
template mid-run is measuring the operator, not the pipeline.

The run also records the **per-archetype first-pass rate** as a baseline. Phase 2's
target is >= 60%; without a Phase 1 number written down, that criterion is
unfalsifiable.

Papers are cached under ``eval-out/cache`` so re-runs do not hammer arXiv and so
CI is deterministic. ``--offline`` runs entirely from cache with the heuristic
analyzer, which is what makes this usable as a pre-merge check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from arcvisual.config import PIPELINE_VERSION, settings
from arcvisual.console import enable_unicode_output
from arcvisual.ingest.arxiv import (
    build_storyboard,
    extract_arxiv_id,
    fetch_metadata,
    fetch_source,
)
from arcvisual.ingest.errors import IngestRejection
from arcvisual.render.pipeline import JobResult, run_job
from arcvisual.storyboard import JobState, SceneState, Storyboard

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = Path(__file__).with_name("papers.yaml")
OUTDIR = ROOT / "eval-out"

log = logging.getLogger("eval")


# --------------------------------------------------------------------------- #
# Source fingerprint
# --------------------------------------------------------------------------- #


def source_fingerprint() -> str:
    """Hash every tracked Python source file, so a mid-run edit is detectable."""
    h = hashlib.sha256()
    for path in sorted((ROOT / "arcvisual").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(path.relative_to(ROOT).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class PaperRun:
    id: str
    url: str
    state: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    sections: int = 0
    concepts: int = 0
    scenes_total: int = 0
    scenes_passed: int = 0
    scenes_degraded: int = 0
    scenes_failed: int = 0
    first_pass_rate: float | None = None
    cost_usd: float = 0.0
    wall_s: float = 0.0
    archetypes: dict[str, int] = field(default_factory=dict)
    interventions: int = 0


@dataclass
class EvalReport:
    pipeline_version: str
    fingerprint_before: str
    fingerprint_after: str = ""
    offline: bool = False
    runs: list[PaperRun] = field(default_factory=list)
    archetype_baseline: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def code_unchanged(self) -> bool:
        return self.fingerprint_before == self.fingerprint_after

    @property
    def total_cost(self) -> float:
        return round(sum(r.cost_usd for r in self.runs), 4)

    def exit_test_passed(self, require_unchanged: bool) -> bool:
        if require_unchanged and not self.code_unchanged:
            return False
        return self.runs and self.completed == len(self.runs)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def cached_storyboard(paper: dict, *, offline: bool, refresh: bool) -> Storyboard:
    """Ingest once, reuse thereafter. Keeps CI off the network and deterministic."""
    cache_dir = OUTDIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    arxiv_id = extract_arxiv_id(paper["url"])
    blob_path = cache_dir / f"{arxiv_id.replace('/', '_')}.tar.gz"
    meta_path = cache_dir / f"{arxiv_id.replace('/', '_')}.meta.json"

    if refresh or not (blob_path.exists() and meta_path.exists()):
        if offline:
            raise IngestRejection(
                _reject_code(),
                f"{arxiv_id} is not cached and --offline forbids fetching it",
            )
        meta = fetch_metadata(arxiv_id)
        blob = fetch_source(arxiv_id)
        blob_path.write_bytes(blob)
        meta_path.write_text(
            json.dumps(
                {
                    k: v
                    for k, v in vars(meta).items()
                    if isinstance(v, (str, int, float, list, type(None)))
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    from arcvisual.ingest.arxiv import ArxivMetadata

    meta = ArxivMetadata(**json.loads(meta_path.read_text(encoding="utf-8")))
    return build_storyboard(meta, blob_path.read_bytes())


def _reject_code():
    from arcvisual.ingest.errors import RejectCode

    return RejectCode.SOURCE_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #


def check_expectations(result: JobResult, expect: dict) -> list[str]:
    """Floors, not predictions. A run below any of these fails the exit test."""
    reasons: list[str] = []
    sb = result.storyboard
    if expect.get("rejected"):
        return [
            f"expected this paper to be rejected as {expect['rejected']!r}, but the "
            "pipeline processed it; the eval set needs updating"
        ]
    if result.state is JobState.FAILED:
        return [f"job failed: {(result.failure or {}).get('code', 'unknown')}"]
    if sb is None:
        return ["no storyboard produced"]

    if len(sb.sections) < expect.get("min_sections", 0):
        reasons.append(
            f"only {len(sb.sections)} sections parsed, expected "
            f">= {expect['min_sections']}"
        )
    if len(sb.concepts) < expect.get("min_concepts", 0):
        reasons.append(
            f"only {len(sb.concepts)} concepts, expected >= {expect['min_concepts']}"
        )

    shipped = [s for s in sb.scenes if s.state is SceneState.PASSED]
    if len(shipped) < expect.get("min_scenes", 0):
        reasons.append(
            f"only {len(shipped)} scenes shipped, expected >= {expect['min_scenes']}"
        )

    present = {s.spec.archetype.value for s in sb.scenes}
    for required in expect.get("archetypes_present", []) or []:
        if required not in present:
            reasons.append(
                f"expected a {required} scene, got {sorted(present) or 'none'}"
            )

    # Structural invariants that must hold for every paper, expectations or not.
    for scene in sb.scenes:
        if scene.state is SceneState.PASSED and scene.artifact is None:
            reasons.append(f"scene {scene.spec.id} passed without an artifact")
        try:
            scene.spec.span.resolve(sb.section(scene.spec.span.section_id))
        except Exception as exc:
            reasons.append(f"scene {scene.spec.id} is not grounded: {exc}")
    return reasons


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def run_eval(
    papers_file: Path,
    *,
    only: list[str] | None,
    offline: bool,
    refresh: bool,
    run_gate2: bool,
    require_unchanged: bool,
) -> EvalReport:
    spec = yaml.safe_load(papers_file.read_text(encoding="utf-8"))
    papers = spec.get("papers", [])
    if only:
        papers = [p for p in papers if p["id"] in only]

    report = EvalReport(
        pipeline_version=PIPELINE_VERSION,
        fingerprint_before=source_fingerprint(),
        offline=offline or settings().offline,
    )

    for paper in papers:
        t0 = time.perf_counter()
        run = PaperRun(id=paper["id"], url=paper["url"], state="?", passed=False)
        try:
            sb = cached_storyboard(paper, offline=offline, refresh=refresh)
        except IngestRejection as exc:
            # A paper the set expects us to refuse is a PASS: proving we reject
            # cleanly, with an accurate reason, is exactly as valuable as proving
            # we process a good paper. The alternative — a silently degraded
            # article — is the outcome the plan forbids.
            expected = (paper.get("expect", {}) or {}).get("rejected")
            run.state = f"rejected:{exc.code.value}"
            if expected and exc.code.value == expected:
                run.passed = True
                run.reasons = []
            else:
                run.reasons = [
                    f"unexpected rejection ({exc.code.value}"
                    + (f", expected {expected}" if expected else "")
                    + f"): {exc.detail or exc.user_facing}"
                ]
            run.wall_s = round(time.perf_counter() - t0, 2)
            report.runs.append(run)
            _print_run(run)
            continue

        result = run_job(storyboard=sb, run_gate2=run_gate2)
        run.state = result.state.value
        run.sections = len(result.storyboard.sections) if result.storyboard else 0
        run.concepts = len(result.storyboard.concepts) if result.storyboard else 0
        run.scenes_total = len(result.scenes)
        run.scenes_passed = sum(1 for s in result.scenes if s.state is SceneState.PASSED)
        run.scenes_degraded = sum(
            1 for s in result.scenes if s.state is SceneState.DEGRADED
        )
        run.scenes_failed = sum(1 for s in result.scenes if s.state is SceneState.FAILED)
        run.first_pass_rate = result.first_pass_rate
        run.cost_usd = result.cost_usd
        for scene in result.scenes:
            key = scene.spec.archetype.value
            run.archetypes[key] = run.archetypes.get(key, 0) + 1
        run.reasons = check_expectations(result, paper.get("expect", {}) or {})
        run.passed = not run.reasons
        run.wall_s = round(time.perf_counter() - t0, 2)

        _accumulate_baseline(report, result)
        report.runs.append(run)
        _print_run(run)

    report.fingerprint_after = source_fingerprint()
    return report


def _accumulate_baseline(report: EvalReport, result: JobResult) -> None:
    """Per-archetype first-pass rate — the number Phase 2 is measured against."""
    for outcome in result.scene_outcomes:
        if not outcome.attempts:
            continue
        key = outcome.scene.spec.archetype.value
        row = report.archetype_baseline.setdefault(
            key,
            {
                "scenes": 0,
                "first_pass": 0,
                "attempts": 0,
                "cost_usd": 0.0,
                "failed_gates": {},
            },
        )
        row["scenes"] += 1
        row["first_pass"] += int(outcome.first_pass)
        row["attempts"] += len(outcome.attempts)
        row["cost_usd"] = round(row["cost_usd"] + outcome.cost_usd, 6)
        for attempt in outcome.attempts:
            if attempt.failed_gate:
                g = f"gate{attempt.failed_gate}"
                row["failed_gates"][g] = row["failed_gates"].get(g, 0) + 1


def _print_run(run: PaperRun) -> None:
    mark = "PASS" if run.passed else "FAIL"
    fpr = "n/a" if run.first_pass_rate is None else f"{run.first_pass_rate:.0%}"
    print(
        f"  [{mark}] {run.id:10s} {run.state:9s} "
        f"sections={run.sections:2d} concepts={run.concepts:2d} "
        f"scenes={run.scenes_passed}/{run.scenes_total} "
        f"first_pass={fpr:>4s} ${run.cost_usd:.4f} {run.wall_s:.1f}s"
    )
    for reason in run.reasons:
        print(f"         - {reason}")


def print_report(report: EvalReport, require_unchanged: bool) -> None:
    print("\n" + "=" * 78)
    print(
        f"ArcVisual eval — pipeline {report.pipeline_version}"
        # Distinguish clearly: this flag is about the ANALYZER, not the source
        # cache. A heuristic baseline is not a model baseline, and reading the
        # two as the same number would badly mislead the Phase 2 comparison.
        f"{'  [heuristic analyzer — no ANTHROPIC_API_KEY]' if report.offline else ''}"
    )
    print("=" * 78)
    print(f"papers completed        : {report.completed}/{len(report.runs)}")
    print("human interventions     : 0 (the harness makes none)")
    print(
        f"code changed mid-run    : {'NO' if report.code_unchanged else 'YES'}"
        f"  ({report.fingerprint_before} -> {report.fingerprint_after})"
    )
    print(f"total cost              : ${report.total_cost:.4f}")

    if report.archetype_baseline:
        print(
            "\nper-archetype baseline (the number Phase 2's >=60% is measured against):"
        )
        print(
            f"  {'archetype':20s} {'scenes':>6s} {'first-pass':>11s} "
            f"{'mean attempts':>14s} {'cost':>9s}  failing gates"
        )
        for name, row in sorted(report.archetype_baseline.items()):
            fp = row["first_pass"] / row["scenes"] if row["scenes"] else 0.0
            mean = row["attempts"] / row["scenes"] if row["scenes"] else 0.0
            print(
                f"  {name:20s} {row['scenes']:6d} {fp:10.0%} "
                f"{mean:14.2f} ${row['cost_usd']:8.4f}  "
                f"{row['failed_gates'] or '-'}"
            )

    ok = report.exit_test_passed(require_unchanged)
    print("\n" + ("EXIT TEST PASSED" if ok else "EXIT TEST FAILED"))
    if not ok and require_unchanged and not report.code_unchanged:
        print(
            "  source files changed between the first and last paper; the run "
            "measured the operator, not the pipeline"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--papers", type=Path, default=DEFAULT_SET)
    ap.add_argument("--only", nargs="*", help="run a subset by id")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="use only cached papers and the heuristic analyzer",
    )
    ap.add_argument("--refresh", action="store_true", help="re-fetch cached sources")
    ap.add_argument(
        "--no-gate2",
        action="store_true",
        help="skip the draft render (no Manim installed)",
    )
    ap.add_argument(
        "--assert-no-code-changes",
        action="store_true",
        help="fail if any source file changes during the run",
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # cp1252 consoles cannot encode the em-dash in the report banner.
    enable_unicode_output()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print(f"running {args.papers.name} ...")
    report = run_eval(
        args.papers,
        only=args.only,
        offline=args.offline,
        refresh=args.refresh,
        run_gate2=not args.no_gate2,
        require_unchanged=args.assert_no_code_changes,
    )
    print_report(report, args.assert_no_code_changes)

    out = args.json or (OUTDIR / "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    print(f"\nreport written to {out.relative_to(ROOT)}")

    return 0 if report.exit_test_passed(args.assert_no_code_changes) else 1


if __name__ == "__main__":
    sys.exit(main())

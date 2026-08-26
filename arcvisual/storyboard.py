"""The Storyboard contract — ArcVisual's spine.

Every pipeline stage is a function ``(Storyboard) -> Storyboard`` that may only
*add* fields it owns.  See ``STAGE_OWNERSHIP`` and :func:`assert_monotonic`,
which turn that rule from a convention into a test.

Two invariants are structural rather than advisory:

* **Grounding.** ``SourceSpan`` is non-optional on every concept, opportunity
  and scene, so there is no code path that yields an ungrounded claim.
* **Argument.** ``claim`` has ``min_length=10``. Decorative animation is
  rejected by the schema, not by a reviewer.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Archetype(str, Enum):
    """The visual-opportunity taxonomy. Phase 1 implements the first three."""

    TRANSFORM_CHAIN = "transform_chain"
    PLOT_REVEAL = "plot_reveal"
    ARCHITECTURE_FLOW = "architecture_flow"
    # Declared but unimplemented in Phase 1 — the registry gates availability.
    VECTOR_FIELD = "vector_field"
    GEOMETRIC_INTUITION = "geometric_intuition"
    ATTENTION_MATRIX = "attention_matrix"
    ALGORITHM_TRACE = "algorithm_trace"
    COUNTEREXAMPLE = "counterexample"
    SCALE_COMPARISON = "scale_comparison"
    STATE_MACHINE = "state_machine"
    CUSTOM_SCENE = "custom_scene"


class SceneState(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    VALIDATING = "validating"
    PASSED = "passed"
    DEGRADED = "degraded"
    FAILED = "failed"


TERMINAL_SCENE_STATES = frozenset(
    {SceneState.PASSED, SceneState.DEGRADED, SceneState.FAILED}
)


class JobState(str, Enum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    ANALYZING = "analyzing"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"


class Stage(str, Enum):
    INGEST = "ingest"
    ANALYZE = "analyze"
    GENERATE = "generate"
    VALIDATE = "validate"


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #


def quote_hash(text: str) -> str:
    """Hash of a quoted span, used to detect source drift across re-ingests."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class SourceSpan(BaseModel):
    """Grounding anchor: a character range in a section's raw source."""

    model_config = ConfigDict(frozen=True)

    section_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote_sha256: str

    @model_validator(mode="after")
    def _ordered(self) -> SourceSpan:
        if self.end <= self.start:
            raise ValueError(f"empty or inverted span: [{self.start}, {self.end})")
        return self

    def resolve(self, section: Section) -> str:
        """Return the spanned text, raising if the source has drifted."""
        if section.id != self.section_id:
            raise ValueError(f"span belongs to {self.section_id}, not {section.id}")
        text = section.raw[self.start : self.end]
        if quote_hash(text) != self.quote_sha256:
            raise SourceDriftError(
                f"span {self.section_id}[{self.start}:{self.end}] no longer matches "
                "its recorded hash; the paper source changed under this storyboard"
            )
        return text


class SourceDriftError(Exception):
    """Raised when a grounded span no longer matches the source it cites."""


# --------------------------------------------------------------------------- #
# Ingest-owned
# --------------------------------------------------------------------------- #


class PaperMeta(BaseModel):
    arxiv_id: str | None = None
    doi: str | None = None
    slug: str
    title: str
    authors: list[str] = []
    abstract: str = ""
    license: str
    source_sha256: str
    published: str | None = None
    origin_url: str


class Equation(BaseModel):
    id: str
    latex: str  # verbatim from source; never re-typeset
    label: str | None = None  # \label{...}, for cross-references
    display: bool = True  # display math vs. inline
    span: SourceSpan


class Figure(BaseModel):
    id: str
    caption: str
    label: str | None = None
    r2_key: str | None = None  # None => license forbids rehosting
    origin_url: str  # always present: the link-out
    redistributable: bool


class Section(BaseModel):
    id: str
    heading_path: list[str]  # ["3 Method", "3.2 Attention"]
    raw: str  # source of truth for span offsets
    prose_md: str
    equation_ids: list[str] = []
    figure_ids: list[str] = []
    char_offset: int = 0  # start of `raw` within the whole document
    # --- Analyze owns below ---
    difficulty: int | None = Field(default=None, ge=1, le=5)
    concept_ids: list[str] = []

    @property
    def heading(self) -> str:
        return self.heading_path[-1] if self.heading_path else self.id

    @property
    def depth(self) -> int:
        return len(self.heading_path)


# --------------------------------------------------------------------------- #
# Analyze-owned
# --------------------------------------------------------------------------- #


class Concept(BaseModel):
    id: str
    name: str
    statement: str  # one sentence, paraphrased — never a long quote
    span: SourceSpan
    depends_on: list[str] = []  # concept ids; must form a DAG
    centrality: float = Field(ge=0.0, le=1.0)


class VisualOpportunity(BaseModel):
    """An argued case that one concept deserves an animation.

    Analyze produces these; Generate turns the surviving ones into
    :class:`SceneSpec` by filling in a template's parameters.
    """

    id: str
    archetype: Archetype
    claim: str = Field(min_length=10)  # the argument the visual makes
    concept_id: str
    span: SourceSpan
    justification: str = Field(min_length=10)
    difficulty: int = Field(ge=1, le=5)
    centrality: float = Field(ge=0.0, le=1.0)

    @property
    def rank(self) -> float:
        """Triage score. The plan's `difficulty x centrality`."""
        return self.difficulty * self.centrality


# --------------------------------------------------------------------------- #
# Generate / Validate-owned
# --------------------------------------------------------------------------- #


class Beat(BaseModel):
    """One narration step. The style contract's pacing floor lives here."""

    t: float = Field(ge=0.0)  # start, seconds
    dur: float = Field(ge=0.8)  # min 0.8s per beat — style contract
    caption: str  # on-screen text (v1: no audio)


class SceneSpec(BaseModel):
    """What the model produces. Immutable once hashed."""

    model_config = ConfigDict(frozen=True)

    id: str
    archetype: Archetype
    claim: str = Field(min_length=10)
    concept_id: str
    span: SourceSpan
    params: dict[str, Any]  # validated against the template's own model
    beats: tuple[Beat, ...] = Field(min_length=1)
    scrubbable: bool = False
    priority: int = 0  # placement order after triage

    @model_validator(mode="after")
    def _beats_ordered(self) -> SceneSpec:
        for prev, nxt in zip(self.beats, self.beats[1:], strict=False):
            if nxt.t + 1e-6 < prev.t + prev.dur:
                raise ValueError(f"beats overlap: {prev.t}+{prev.dur} > {nxt.t}")
        return self

    @property
    def duration_s(self) -> float:
        last = self.beats[-1]
        return last.t + last.dur


class GateResult(BaseModel):
    gate: int
    passed: bool
    duration_ms: int = 0
    findings: list[str] = []  # human-readable, fed back to the repair prompt
    payload: dict[str, Any] = {}  # structured detail (tracebacks, bboxes, ...)


class Gate4Result(GateResult):
    clarity_score: int | None = Field(default=None, ge=1, le=5)
    blocking_failures: list[str] = []


class GateReport(BaseModel):
    gate1: GateResult | None = None
    gate2: GateResult | None = None
    gate3: GateResult | None = None
    gate4: Gate4Result | None = None

    @property
    def first_failure(self) -> int | None:
        for n in (1, 2, 3, 4):
            r = getattr(self, f"gate{n}")
            if r is not None and not r.passed:
                return n
        return None

    @property
    def all_passed(self) -> bool:
        return self.first_failure is None and self.gate1 is not None

    def findings(self) -> list[str]:
        out: list[str] = []
        for n in (1, 2, 3, 4):
            r = getattr(self, f"gate{n}")
            if r is not None and not r.passed:
                out.extend(f"[gate{n}] {f}" for f in r.findings)
        return out


class Artifact(BaseModel):
    content_hash: str
    mp4_key: str
    webm_key: str | None = None
    poster_key: str
    framestrip_key: str
    duration_s: float
    quality: Literal["draft", "final"]
    bytes: int = 0


class Scene(BaseModel):
    spec: SceneSpec
    state: SceneState = SceneState.PENDING
    attempts: int = 0
    cost_usd: float = 0.0
    artifact: Artifact | None = None
    gate_report: GateReport | None = None
    degraded_reason: str | None = None

    @model_validator(mode="after")
    def _terminal_states_are_explained(self) -> Scene:
        if self.state is SceneState.PASSED and self.artifact is None:
            raise ValueError(f"scene {self.spec.id} passed without an artifact")
        if self.state is SceneState.DEGRADED and not self.degraded_reason:
            raise ValueError(f"scene {self.spec.id} degraded without a reason")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_SCENE_STATES

    @property
    def shippable(self) -> bool:
        """Whether the reader should render a slot for this scene at all."""
        return self.state in (SceneState.PASSED, SceneState.DEGRADED)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


class Storyboard(BaseModel):
    schema_version: str = SCHEMA_VERSION
    paper: PaperMeta
    sections: list[Section]
    equations: list[Equation] = []
    figures: list[Figure] = []
    # --- Analyze owns below ---
    concepts: list[Concept] = []
    opportunities: list[VisualOpportunity] = []
    reading_order: list[str] = []  # section ids, topologically sorted
    # --- Generate/Validate own below ---
    scenes: list[Scene] = []

    # -- lookups ----------------------------------------------------------- #

    @property
    def section_ids(self) -> set[str]:
        return {s.id for s in self.sections}

    def section(self, section_id: str) -> Section:
        for s in self.sections:
            if s.id == section_id:
                return s
        raise KeyError(section_id)

    def concept(self, concept_id: str) -> Concept:
        for c in self.concepts:
            if c.id == concept_id:
                return c
        raise KeyError(concept_id)

    def scene(self, scene_id: str) -> Scene:
        for sc in self.scenes:
            if sc.spec.id == scene_id:
                return sc
        raise KeyError(scene_id)

    def ordered_sections(self) -> list[Section]:
        if not self.reading_order:
            return list(self.sections)
        by_id = {s.id: s for s in self.sections}
        return [by_id[sid] for sid in self.reading_order if sid in by_id]

    def scenes_for(self, section_id: str) -> list[Scene]:
        found = [s for s in self.scenes if s.spec.span.section_id == section_id]
        return sorted(found, key=lambda s: s.spec.priority)

    @property
    def body_text(self) -> str:
        """The whole paper, as the analyze prompt sees it. Cache key for L1."""
        return "\n\n".join(
            f"## {' > '.join(s.heading_path)}\n{s.raw}" for s in self.sections
        )

    # -- integrity --------------------------------------------------------- #

    @model_validator(mode="after")
    def _integrity(self) -> Storyboard:
        ids = self.section_ids
        if len(ids) != len(self.sections):
            raise ValueError("duplicate section ids")

        concept_ids = {c.id for c in self.concepts}
        if len(concept_ids) != len(self.concepts):
            raise ValueError("duplicate concept ids")

        for c in self.concepts:
            if c.span.section_id not in ids:
                raise ValueError(f"concept {c.id} cites unknown section")
            for dep in c.depends_on:
                if dep not in concept_ids:
                    raise ValueError(f"concept {c.id} depends on unknown {dep!r}")
        assert_acyclic(self.concepts)

        for o in self.opportunities:
            if o.span.section_id not in ids:
                raise ValueError(f"opportunity {o.id} cites unknown section")
            if self.concepts and o.concept_id not in concept_ids:
                raise ValueError(f"opportunity {o.id} cites unknown concept")

        seen: set[str] = set()
        for sc in self.scenes:
            if sc.spec.id in seen:
                raise ValueError(f"duplicate scene id {sc.spec.id}")
            seen.add(sc.spec.id)
            if sc.spec.span.section_id not in ids:
                raise ValueError(f"scene {sc.spec.id} cites unknown section")

        if self.reading_order:
            unknown = set(self.reading_order) - ids
            if unknown:
                raise ValueError(f"reading_order names unknown sections: {unknown}")

        eq_ids = {e.id for e in self.equations}
        fig_ids = {f.id for f in self.figures}
        for s in self.sections:
            if missing := set(s.equation_ids) - eq_ids:
                raise ValueError(f"section {s.id} cites unknown equations {missing}")
            if missing := set(s.figure_ids) - fig_ids:
                raise ValueError(f"section {s.id} cites unknown figures {missing}")
        return self

    # -- progress ---------------------------------------------------------- #

    def progress(self) -> dict[str, Any]:
        """Payload for ``GET /api/jobs/:id``. Cheap enough to compute per poll."""
        by_state: dict[str, int] = {}
        for sc in self.scenes:
            by_state[sc.state.value] = by_state.get(sc.state.value, 0) + 1
        done = sum(1 for sc in self.scenes if sc.is_terminal)
        return {
            "sections": len(self.sections),
            "concepts": len(self.concepts),
            "scenes_total": len(self.scenes),
            "scenes_done": done,
            "scenes_by_state": by_state,
            "cost_usd": round(sum(sc.cost_usd for sc in self.scenes), 4),
        }


# --------------------------------------------------------------------------- #
# Structural rules
# --------------------------------------------------------------------------- #


def assert_acyclic(concepts: list[Concept]) -> None:
    """Kahn's algorithm. A cycle means we could animate an idea before its
    prerequisite, which is the whole thing ``depends_on`` exists to prevent."""
    indeg = {c.id: 0 for c in concepts}
    edges: dict[str, list[str]] = {c.id: [] for c in concepts}
    for c in concepts:
        for dep in c.depends_on:
            edges[dep].append(c.id)
            indeg[c.id] += 1

    queue = [cid for cid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in edges[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(concepts):
        stuck = sorted(cid for cid, d in indeg.items() if d > 0)
        raise ValueError(f"concept dependency cycle among {stuck}")


def topological_concept_order(concepts: list[Concept]) -> list[str]:
    """Concept ids in dependency order, ties broken by descending centrality."""
    assert_acyclic(concepts)
    by_id = {c.id: c for c in concepts}
    remaining = dict[str, set[str]]()
    for c in concepts:
        remaining[c.id] = set(c.depends_on)

    out: list[str] = []
    while remaining:
        ready = [cid for cid, deps in remaining.items() if not deps]
        ready.sort(key=lambda cid: (-by_id[cid].centrality, cid))
        for cid in ready:
            out.append(cid)
            del remaining[cid]
        for deps in remaining.values():
            deps -= set(ready)
    return out


#: Which stage may write which top-level (or nested) field.
STAGE_OWNERSHIP: dict[Stage, frozenset[str]] = {
    Stage.INGEST: frozenset(
        {
            "schema_version",
            "paper",
            "sections",
            "equations",
            "figures",
        }
    ),
    Stage.ANALYZE: frozenset(
        {
            "concepts",
            "opportunities",
            "reading_order",
            "sections[].difficulty",
            "sections[].concept_ids",
        }
    ),
    Stage.GENERATE: frozenset({"scenes"}),
    Stage.VALIDATE: frozenset(
        {
            "scenes[].state",
            "scenes[].attempts",
            "scenes[].cost_usd",
            "scenes[].artifact",
            "scenes[].gate_report",
            "scenes[].degraded_reason",
        }
    ),
}


def _flatten(sb: Storyboard) -> dict[str, Any]:
    """Storyboard -> {ownership-key: comparable value}."""
    d = sb.model_dump(mode="json")
    flat: dict[str, Any] = {
        "schema_version": d["schema_version"],
        "paper": d["paper"],
        "equations": d["equations"],
        "figures": d["figures"],
        "concepts": d["concepts"],
        "opportunities": d["opportunities"],
        "reading_order": d["reading_order"],
    }
    # Sections: split the Analyze-owned fields out of the Ingest-owned body.
    flat["sections"] = [
        {k: v for k, v in s.items() if k not in ("difficulty", "concept_ids")}
        for s in d["sections"]
    ]
    flat["sections[].difficulty"] = [s["difficulty"] for s in d["sections"]]
    flat["sections[].concept_ids"] = [s["concept_ids"] for s in d["sections"]]
    # Scenes: the spec belongs to Generate, everything mutable to Validate.
    flat["scenes"] = [s["spec"] for s in d["scenes"]]
    for field in (
        "state",
        "attempts",
        "cost_usd",
        "artifact",
        "gate_report",
        "degraded_reason",
    ):
        flat[f"scenes[].{field}"] = [s[field] for s in d["scenes"]]
    return flat


def assert_monotonic(before: Storyboard, after: Storyboard, stage: Stage) -> None:
    """Fail if `stage` mutated a field it does not own.

    This is the mechanism behind "stages never talk to each other": a stage can
    only be trusted as a pure ``(Storyboard) -> Storyboard`` if it provably
    keeps its hands off everyone else's fields. Called in tests and in the
    orchestrator under ``--strict``, so a violation surfaces in CI rather than
    as a mystery in production.
    """
    owned = STAGE_OWNERSHIP[stage]
    b, a = _flatten(before), _flatten(after)
    violations = []
    for key in b:
        if key in owned:
            continue
        # Generate appends scenes, so the Validate-owned per-scene lists grow
        # with them; only compare the prefix that existed before.
        if key.startswith("scenes[].") and len(a[key]) != len(b[key]):
            if a[key][: len(b[key])] != b[key]:
                violations.append(key)
            continue
        if a[key] != b[key]:
            violations.append(key)
    if violations:
        raise OwnershipViolation(
            f"stage {stage.value!r} wrote fields it does not own: {sorted(violations)}"
        )


class OwnershipViolation(Exception):
    """A stage mutated part of the Storyboard belonging to another stage."""

/**
 * Mirrors `arcvisual/storyboard.py`. The Storyboard is the contract between the
 * pipeline and the reader, so these types are generated-by-hand copies of the
 * Pydantic models and must be updated together with them.
 *
 * `SCHEMA_VERSION` is checked at render time: a document written by a newer
 * pipeline than this reader understands is shown as a plain article rather than
 * silently mis-rendered.
 */

export const SUPPORTED_SCHEMA_MAJOR = 1;

export type SceneState =
  | "pending"
  | "generating"
  | "validating"
  | "passed"
  | "degraded"
  | "failed";

export interface SourceSpan {
  section_id: string;
  start: number;
  end: number;
  quote_sha256: string;
}

export interface PaperMeta {
  arxiv_id: string | null;
  doi: string | null;
  slug: string;
  title: string;
  authors: string[];
  abstract: string;
  license: string;
  source_sha256: string;
  published: string | null;
  origin_url: string;
}

export interface Equation {
  id: string;
  latex: string;
  label: string | null;
  display: boolean;
  span: SourceSpan;
}

export interface Figure {
  id: string;
  caption: string;
  label: string | null;
  /** Null when the licence forbids rehosting — deep-link to `origin_url` instead. */
  r2_key: string | null;
  origin_url: string;
  redistributable: boolean;
}

export interface Section {
  id: string;
  heading_path: string[];
  raw: string;
  prose_md: string;
  equation_ids: string[];
  figure_ids: string[];
  char_offset: number;
  difficulty: number | null;
  concept_ids: string[];
}

export interface Concept {
  id: string;
  name: string;
  statement: string;
  span: SourceSpan;
  depends_on: string[];
  centrality: number;
}

export interface Beat {
  t: number;
  dur: number;
  caption: string;
}

export interface SceneSpec {
  id: string;
  archetype: string;
  /** The argument this visual makes. Required by the schema — no claim, no scene. */
  claim: string;
  concept_id: string;
  span: SourceSpan;
  params: Record<string, unknown>;
  beats: Beat[];
  scrubbable: boolean;
  priority: number;
}

export interface GateResult {
  gate: number;
  passed: boolean;
  duration_ms: number;
  findings: string[];
  payload: Record<string, unknown>;
}

export interface Gate4Result extends GateResult {
  clarity_score: number | null;
  blocking_failures: string[];
}

export interface GateReport {
  gate1: GateResult | null;
  gate2: GateResult | null;
  gate3: GateResult | null;
  gate4: Gate4Result | null;
}

export interface Artifact {
  content_hash: string;
  mp4_key: string;
  webm_key: string | null;
  poster_key: string;
  framestrip_key: string;
  duration_s: number;
  quality: "draft" | "final";
  bytes: number;
}

export interface Scene {
  spec: SceneSpec;
  state: SceneState;
  attempts: number;
  cost_usd: number;
  artifact: Artifact | null;
  gate_report: GateReport | null;
  degraded_reason: string | null;
}

export interface Storyboard {
  schema_version: string;
  paper: PaperMeta;
  sections: Section[];
  equations: Equation[];
  figures: Figure[];
  concepts: Concept[];
  opportunities: unknown[];
  reading_order: string[];
  scenes: Scene[];
}

export interface PaperResponse {
  pipeline_version: string;
  storyboard: Storyboard;
  media_base: string;
}

export interface JobStatus {
  job_id: string;
  state: string;
  pipeline_version: string;
  stage_progress: Record<string, unknown>;
  scenes: { key: string; archetype: string; state: string; ready: boolean }[];
  failure: { code: string; message: string; user_facing: string } | null;
  cost_usd: number;
}

/** A scene the reader should render a slot for at all. */
export function isShippable(scene: Scene): boolean {
  return scene.state === "passed" || scene.state === "degraded";
}

/**
 * Gate 4 passed but only marginally. Readers deserve to know which visuals the
 * machine was less sure about, so these carry a quieter provenance chip.
 */
export function isMarginal(scene: Scene): boolean {
  const score = scene.gate_report?.gate4?.clarity_score;
  return typeof score === "number" && score <= 3;
}

export function schemaSupported(storyboard: Storyboard): boolean {
  const major = Number.parseInt(storyboard.schema_version.split(".")[0] ?? "0", 10);
  return major === SUPPORTED_SCHEMA_MAJOR;
}

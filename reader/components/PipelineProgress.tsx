"use client";

/**
 * What a reader sees between pasting a link and reading the explainer.
 *
 * The wait is owned honestly rather than hidden: the stages are named, each visual
 * is listed as it lands, and the article opens for reading as soon as the paper has
 * been analysed — the prose does not need to wait for the visuals.
 */

import { formatElapsed, STAGES, type JobView } from "../hooks/useJobStatus";
import type { JobStatus } from "../lib/types";
import ArcLoader, { ArcDots } from "./ArcLoader";
import { plainText } from "../lib/tex";

interface Props extends JobView {
  title?: string;
  slug?: string | null;
}

const SCENE_LABEL: Record<string, string> = {
  pending: "Queued",
  generating: "Drawing",
  validating: "Checking",
  passed: "Ready",
  degraded: "In the text",
  failed: "In the text",
};

const KIND: Record<string, string> = {
  transform_chain: "Derivation",
  plot_reveal: "Result",
  architecture_flow: "System",
};

export default function PipelineProgress({
  status,
  progress,
  stageIndex,
  isTerminal,
  failed,
  scenesDone,
  scenesTotal,
  error,
  elapsedMs,
  refresh,
  title,
  slug,
}: Props) {
  if (failed) return <Failure status={status} />;
  const readable = Boolean(slug) && (status?.state === "rendering" || status?.state === "complete");

  return (
    <div className="mx-auto max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <div className="rounded-[2rem] border-[1.5px] border-ink bg-paper p-6 sm:p-10">
        <div className="flex items-start justify-between gap-6">
          <div className="min-w-0">
            <span className="pill bg-mustard text-xs">{isTerminal ? "Done" : "Working on it"}</span>
            <h1 className="mt-4 text-[clamp(1.6rem,3.6vw,2.4rem)] font-semibold leading-tight tracking-tight">
              {title ? plainText(title) : "Reading your paper"}
            </h1>
          </div>
          <ArcLoader size={56} progress={isTerminal ? 1 : progress} />
        </div>

        <div
          className="mt-8 h-3 w-full overflow-hidden rounded-full border-[1.5px] border-ink bg-wash"
          role="progressbar"
          aria-label="Progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress * 100)}
        >
          <div className="h-full rounded-full bg-coral transition-[width] duration-700 ease-out" style={{ width: `${Math.max(3, progress * 100)}%` }} />
        </div>
        <p className="mt-3 flex flex-wrap items-center gap-x-3 text-sm text-ink-soft">
          <span className="tabular-nums">{formatElapsed(elapsedMs)} so far</span>
          <span>{scenesTotal > 0 ? `${scenesDone} of ${scenesTotal} visuals done` : "No visuals planned yet"}</span>
          {!isTerminal ? <ArcDots /> : null}
        </p>

        <ol className="mt-8 space-y-2">
          {STAGES.map((stage, i) => {
            const done = isTerminal || i < stageIndex;
            const active = !isTerminal && i === stageIndex;
            return (
              <li
                key={stage.key}
                className={`flex items-center gap-4 rounded-2xl border-[1.5px] px-4 py-3 ${
                  active ? "border-ink bg-lavender-soft" : "border-transparent"
                }`}
              >
                <span
                  className={`flex size-8 shrink-0 items-center justify-center rounded-full border-[1.5px] border-ink text-sm font-semibold ${
                    done ? "bg-ink text-paper" : active ? "bg-mustard" : "bg-paper text-muted"
                  }`}
                  aria-hidden="true"
                >
                  {done ? "✓" : i + 1}
                </span>
                <span className="min-w-0">
                  <span className={`block ${active ? "font-semibold" : done ? "" : "text-muted"}`}>{stage.label}</span>
                  {active ? <span className="block text-sm text-ink-soft">{stage.detail}</span> : null}
                </span>
              </li>
            );
          })}
        </ol>

        {readable ? (
          <div className="mt-8 flex flex-wrap items-center gap-4 rounded-2xl border-[1.5px] border-ink bg-mustard-soft p-4">
            <p className="flex-1 text-sm">
              {isTerminal ? "Your explainer is ready." : "The text is ready. Visuals keep filling in while you read."}
            </p>
            <a href={`/p/${slug}`} className="btn btn-ink">
              {isTerminal ? "Read the explainer" : "Start reading now"}
            </a>
          </div>
        ) : null}

        {error ? (
          <div className="mt-6 rounded-2xl border-[1.5px] border-ink bg-wash p-4 text-sm">
            <p>{error}</p>
            <button type="button" onClick={refresh} className="mt-2 font-medium underline decoration-coral decoration-2 underline-offset-4">
              Try again now
            </button>
          </div>
        ) : null}
      </div>

      {scenesTotal > 0 ? <SceneList scenes={status?.scenes ?? []} /> : <Expectations />}

      <p className="mt-8 text-center text-sm text-ink-soft">
        You can leave this page. The explainer keeps its link{slug ? <> at <code className="rounded bg-wash px-1.5 py-0.5 text-ink">/p/{slug}</code></> : null}, and
        opening it again picks up where it left off.
      </p>
    </div>
  );
}

function SceneList({ scenes }: { scenes: JobStatus["scenes"] }) {
  return (
    <div className="mt-6 rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-6">
      <h2 className="text-lg font-semibold">Visuals</h2>
      <ul className="mt-4 divide-y-[1.5px] divide-dashed divide-line">
        {scenes.map((scene) => {
          const ready = scene.state === "passed";
          const degraded = scene.state === "degraded" || scene.state === "failed";
          return (
            <li key={scene.key} className="flex items-center gap-3 py-3 text-sm">
              <span
                className={`size-2.5 shrink-0 rounded-full border border-ink ${ready ? "bg-good" : degraded ? "bg-wash" : "arc-pulse bg-coral"}`}
                aria-hidden="true"
              />
              <span className="text-ink">{KIND[scene.archetype] ?? scene.archetype.replace(/_/g, " ")}</span>
              <span className={`ml-auto pill text-xs ${ready ? "bg-lavender-soft" : degraded ? "bg-wash" : "bg-paper"}`}>
                {SCENE_LABEL[scene.state] ?? scene.state}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="mt-4 text-xs text-muted">
        A visual that does not pass its checks is left out and explained in the text instead. A confusing animation is worse
        than none.
      </p>
    </div>
  );
}

function Expectations() {
  const rows = [
    { at: "Seconds", text: "The paper is fetched and split into sections" },
    { at: "Under a minute", text: "The hard ideas are found and the text is readable" },
    { at: "A few minutes", text: "Visuals are drawn and checked, one by one" },
  ];
  return (
    <div className="mt-6 grid gap-3 sm:grid-cols-3">
      {rows.map((row, i) => (
        <div key={row.at} className={`rounded-[var(--radius-tile)] border-[1.5px] border-ink p-4 ${["bg-paper", "bg-lavender-soft", "bg-mustard-soft"][i]}`}>
          <p className="font-semibold">{row.at}</p>
          <p className="mt-1 text-sm text-ink-soft">{row.text}</p>
        </div>
      ))}
    </div>
  );
}

function Failure({ status }: { status: JobStatus | null }) {
  const message = status?.failure?.user_facing ?? "We could not build an explainer from that paper.";
  return (
    <div className="mx-auto max-w-2xl px-4 pb-24 pt-6 sm:px-6">
      <div className="rounded-[2rem] border-[1.5px] border-ink bg-paper p-8 sm:p-10">
        <span className="pill bg-wash text-xs">Stopped</span>
        <h1 className="mt-4 text-3xl font-semibold tracking-tight">This paper could not be explained</h1>
        <p className="mt-4 leading-relaxed text-ink-soft">{message}</p>
        <div className="mt-8 flex flex-wrap gap-3">
          <a href="/#start" className="btn btn-coral">
            Try another paper
          </a>
        </div>
        {status?.failure?.code ? <p className="mt-8 text-xs text-muted">Reference: {status.failure.code}</p> : null}
      </div>
    </div>
  );
}

"use client";

/**
 * The waiting room. What a reader sees between pasting a URL and having an article.
 *
 * The plan is blunt about this: ArcVisual is asynchronous by design, rendering a dozen
 * Manim scenes takes minutes, and "pretending otherwise produces a bad product". So
 * this view owns the wait honestly rather than hiding it:
 *
 * - the five stages are named, so the reader knows *what* is happening, not just that
 *   something is;
 * - per-scene state is shown as it lands, because twelve things finishing one at a time
 *   is far more reassuring than one bar creeping;
 * - the expected timings from the plan (10s / 90s / 2–12m) are stated up front, so a
 *   four-minute wait is a met expectation instead of a worry;
 * - a failure names the reason in the reader's terms and links to the paper.
 *
 * It is a client component because it polls. The article itself is server-rendered, so
 * the prose never waits on JavaScript.
 */

import type { JobStatus } from "../lib/types";
import ArcLoader, { ArcDots } from "./ArcLoader";
import { formatElapsed, STAGES, type JobView } from "../hooks/useJobStatus";

interface Props extends JobView {
  title?: string;
  slug?: string | null;
  originUrl?: string;
}

const SCENE_STATE_LABEL: Record<string, string> = {
  pending: "queued",
  generating: "writing parameters",
  validating: "rendering",
  passed: "ready",
  degraded: "prose instead",
  failed: "prose only",
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
  originUrl,
}: Props) {
  if (failed) {
    return <Failure status={status} originUrl={originUrl} />;
  }

  return (
    <div className="mx-auto max-w-2xl px-6 py-16">
      <div className="flex items-start justify-between gap-6">
        <div>
          <p className="text-xs uppercase tracking-widest text-muted">Working on it</p>
          <h1 className="mt-2 text-2xl font-semibold text-fg">
            {title ?? "Reading your paper"}
          </h1>
        </div>
        <ArcLoader size={52} progress={isTerminal ? 1 : progress} />
      </div>

      {/* A single determinate bar, weighted so rendering owns most of it. */}
      <div
        className="mt-8 h-1 w-full overflow-hidden rounded-full bg-surface"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress * 100)}
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-700 ease-out"
          style={{ width: `${Math.max(2, progress * 100)}%` }}
        />
      </div>

      <p className="mt-3 flex items-center gap-2 text-sm text-muted">
        <span className="tabular-nums">{formatElapsed(elapsedMs)}</span>
        <span aria-hidden="true">·</span>
        <span>
          {scenesTotal > 0
            ? `${scenesDone} of ${scenesTotal} animations done`
            : "no animations queued yet"}
        </span>
        {!isTerminal ? <ArcDots className="ml-1" /> : null}
      </p>

      <StageList stageIndex={stageIndex} isTerminal={isTerminal} />

      {scenesTotal > 0 ? (
        <SceneList scenes={status?.scenes ?? []} />
      ) : (
        <Expectations />
      )}

      {error ? (
        <div className="mt-8 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
          <p>{error}</p>
          <button
            type="button"
            onClick={refresh}
            className="mt-2 underline decoration-dotted"
          >
            Try again now
          </button>
        </div>
      ) : null}

      {isTerminal && slug ? (
        <a
          href={`/p/${slug}`}
          className="mt-10 inline-block rounded-md border border-accent px-4 py-2 text-accent"
        >
          Read the article →
        </a>
      ) : null}

      <p className="mt-12 border-t border-border pt-6 text-sm text-muted">
        You can close this tab. The article is permalinked and will be waiting
        {slug ? (
          <>
            {" at "}
            <code className="text-fg">/p/{slug}</code>
          </>
        ) : null}
        .
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //

function StageList({
  stageIndex,
  isTerminal,
}: {
  stageIndex: number;
  isTerminal: boolean;
}) {
  return (
    <ol className="mt-10 space-y-1">
      {STAGES.map((stage, i) => {
        const done = isTerminal || i < stageIndex;
        const active = !isTerminal && i === stageIndex;
        return (
          <li
            key={stage.key}
            className={`flex items-start gap-3 rounded-md px-3 py-2 transition-colors ${
              active ? "bg-surface" : ""
            }`}
          >
            <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center">
              {done ? (
                <svg viewBox="0 0 20 20" className="size-4 text-good" aria-hidden="true">
                  <path
                    d="M4 10.5l4 4 8-9"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              ) : active ? (
                <span className="arc-pulse size-2 rounded-full bg-accent" />
              ) : (
                <span className="size-2 rounded-full border border-border" />
              )}
            </span>
            <span className="min-w-0">
              <span
                className={`block text-sm ${
                  active ? "text-fg" : done ? "text-muted" : "text-muted/60"
                }`}
              >
                {stage.label}
              </span>
              {active ? (
                <span className="block text-xs text-muted">{stage.detail}</span>
              ) : null}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function SceneList({ scenes }: { scenes: JobStatus["scenes"] }) {
  return (
    <div className="mt-10">
      <h2 className="text-xs uppercase tracking-widest text-muted">Animations</h2>
      <ul className="mt-3 divide-y divide-border rounded-md border border-border">
        {scenes.map((scene) => {
          const ready = scene.state === "passed";
          const degraded = scene.state === "degraded" || scene.state === "failed";
          return (
            <li
              key={scene.key}
              className="flex items-center gap-3 px-3 py-2 text-sm"
            >
              <span
                className={`size-1.5 shrink-0 rounded-full ${
                  ready
                    ? "bg-good"
                    : degraded
                      ? "bg-warn"
                      : "arc-pulse bg-accent"
                }`}
                aria-hidden="true"
              />
              <span className="truncate text-muted">
                {scene.archetype.replace(/_/g, " ")}
              </span>
              <span
                className={`ml-auto shrink-0 text-xs ${
                  ready ? "text-good" : degraded ? "text-warn" : "text-muted"
                }`}
              >
                {SCENE_STATE_LABEL[scene.state] ?? scene.state}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="mt-3 text-xs text-muted">
        A section with good prose and no animation is a fine outcome — we would rather
        drop a visual than ship a confusing one.
      </p>
    </div>
  );
}

/** The plan's own timing arc, stated before the reader starts wondering. */
function Expectations() {
  const rows = [
    { at: "~10s", text: "Paper identified, sections extracted" },
    { at: "~90s", text: "Article text readable" },
    { at: "2–12m", text: "Animations stream in" },
  ];
  return (
    <div className="mt-10 rounded-md border border-border p-4">
      <h2 className="text-xs uppercase tracking-widest text-muted">What to expect</h2>
      <ul className="mt-3 space-y-2">
        {rows.map((row) => (
          <li key={row.at} className="flex gap-4 text-sm">
            <span className="w-16 shrink-0 tabular-nums text-accent">{row.at}</span>
            <span className="text-muted">{row.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Failure({
  status,
  originUrl,
}: {
  status: JobStatus | null;
  originUrl?: string;
}) {
  // Reader-facing text comes from the pipeline's own rejection taxonomy, which is
  // written for humans. Never show a stack trace or an error code here.
  const message =
    status?.failure?.user_facing ??
    "We could not build an article from that paper.";
  return (
    <div className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-2xl font-semibold text-fg">We stopped early</h1>
      <p className="mt-4 text-muted">{message}</p>
      <div className="mt-8 flex flex-wrap gap-4 text-sm">
        <a href="/" className="text-accent underline decoration-dotted">
          Try another paper
        </a>
        {originUrl ? (
          <a
            href={originUrl}
            target="_blank"
            rel="noreferrer"
            className="text-accent underline decoration-dotted"
          >
            Read the original paper →
          </a>
        ) : null}
      </div>
      {status?.failure?.code ? (
        <p className="mt-10 text-xs text-muted/70">
          reference: <code>{status.failure.code}</code>
        </p>
      ) : null}
    </div>
  );
}

"use client";

/**
 * The one interactive element on the landing page.
 *
 * Two behaviours worth naming:
 *
 * - **A cache hit goes straight to the article.** Re-submitting a paper someone else
 *   already ran should feel instant, because it is — the pipeline is idempotent on
 *   (paper, pipeline_version), so there is nothing to wait for.
 * - **A new job goes to `/watch/:id`, never to a spinner here.** The wait is minutes,
 *   and the plan is explicit that hiding that produces a bad product. The waiting room
 *   names the stages and streams per-scene state.
 *
 * Client-side validation is a courtesy, not a gate — the server re-validates and owns
 * the rejection taxonomy. What it buys is a fast, specific "that is not an arXiv link"
 * instead of a round trip.
 */

import { useId, useState } from "react";
import { submitPaper } from "../lib/api";

const ARXIV_HINT =
  /(arxiv\.org\/(abs|pdf|html)\/|^\s*\d{4}\.\d{4,5}(v\d+)?\s*$|^\s*arxiv:)/i;

const EXAMPLES = [
  { id: "1706.03762", label: "Attention Is All You Need" },
  { id: "1512.03385", label: "Deep Residual Learning" },
  { id: "1312.6114", label: "Auto-Encoding Variational Bayes" },
];

export default function SubmitForm({ compact = false }: { compact?: boolean }) {
  // The landing page mounts this form twice (hero and closing). A hardcoded id
  // would duplicate in the DOM, and the second form's <label for> would then point
  // at the FIRST form's input — so clicking that label focuses the wrong field and
  // screen readers announce the wrong control.
  const inputId = useId();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const value = url.trim();
    setError(null);

    if (!ARXIV_HINT.test(value)) {
      setError(
        "That does not look like an arXiv link. Paste something like arxiv.org/abs/1706.03762 — other sources are coming."
      );
      return;
    }

    setBusy(true);
    try {
      const res = await submitPaper(value);
      if (res.cached && res.slug) {
        window.location.href = `/p/${res.slug}`;
        return;
      }
      // `job_id` only — never `call_id`. A Modal call id is not a job row, so
      // `/api/jobs/<call_id>` would 404 on every poll and the waiting room would
      // sit there forever. The API creates the job row before spawning precisely
      // so this id exists from the first response.
      if (res.job_id) {
        const slug = res.slug ? `?slug=${encodeURIComponent(res.slug)}` : "";
        window.location.href = `/watch/${res.job_id}${slug}`;
        return;
      }
      setError(
        "The pipeline accepted the paper but did not give us a job to follow. Try again in a moment."
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={compact ? "" : "w-full"}>
      <form onSubmit={onSubmit}>
        <label htmlFor={inputId} className="sr-only">
          arXiv URL or identifier
        </label>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            id={inputId}
            name="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="arxiv.org/abs/1706.03762"
            autoComplete="off"
            spellCheck={false}
            className="flex-1 rounded-md border border-border bg-surface px-4 py-3 font-mono text-sm text-fg outline-none transition-colors placeholder:text-muted/60 focus:border-accent"
          />
          <button
            type="submit"
            disabled={busy || url.trim().length < 6}
            className="rounded-md bg-accent px-6 py-3 font-medium text-canvas transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy ? "Sending…" : "Explain it"}
          </button>
        </div>
      </form>

      {error ? (
        <p
          role="alert"
          className="mt-3 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm text-warn"
        >
          {error}
        </p>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted">
        <span>Try:</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex.id}
            type="button"
            onClick={() => setUrl(`https://arxiv.org/abs/${ex.id}`)}
            className="rounded border border-border px-2 py-1 transition-colors hover:border-accent hover:text-accent"
          >
            {ex.label}
          </button>
        ))}
      </div>
    </div>
  );
}

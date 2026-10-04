"use client";

/**
 * The paper submission form.
 *
 * - **A paper someone already explained opens instantly.** The pipeline is
 *   idempotent on (paper, pipeline version), so there is nothing to wait for.
 * - **A new paper goes to the waiting room**, which names the stages and shows each
 *   visual as it lands, rather than a spinner here.
 *
 * Client-side validation is a courtesy, not a gate: the server re-validates and owns
 * the rejection reasons. What it buys is a fast, specific message instead of a
 * round trip.
 */

import { useId, useState } from "react";
import { submitPaper } from "../lib/api";

const ARXIV_HINT = /(arxiv\.org\/(abs|pdf|html)\/|^\s*\d{4}\.\d{4,5}(v\d+)?\s*$|^\s*arxiv:)/i;

export const EXAMPLES = [
  { id: "1706.03762", label: "Attention Is All You Need" },
  { id: "1512.03385", label: "Deep Residual Learning" },
  { id: "1312.6114", label: "Auto-Encoding Variational Bayes" },
];

export default function SubmitForm({
  examples = true,
  tone = "light",
}: {
  examples?: boolean;
  tone?: "light" | "onColor";
}) {
  // Mounted more than once on the landing page; a fixed id would make the second
  // label point at the first input.
  const inputId = useId();
  const hintId = useId();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go(value: string) {
    setError(null);
    if (!value) {
      setError("Paste an arXiv link first, or pick one of the papers below.");
      return;
    }
    if (!ARXIV_HINT.test(value)) {
      setError("That isn't an arXiv link. Paste one like arxiv.org/abs/1706.03762, or just the number.");
      return;
    }
    setBusy(true);
    try {
      const res = await submitPaper(value);
      if (res.cached && res.slug) {
        window.location.href = `/p/${res.slug}`;
        return;
      }
      if (res.job_id) {
        const slug = res.slug ? `?slug=${encodeURIComponent(res.slug)}` : "";
        window.location.href = `/watch/${res.job_id}${slug}`;
        return;
      }
      setError("The paper was accepted but no job came back to follow. Try again in a moment.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="w-full">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void go(url.trim());
        }}
      >
        <label htmlFor={inputId} className="sr-only">
          arXiv link or paper number
        </label>
        <div className="flex flex-col gap-2 rounded-[1.15rem] border-[1.5px] border-ink bg-paper p-1.5 sm:flex-row">
          <input
            id={inputId}
            name="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste an arXiv link, e.g. arxiv.org/abs/1706.03762"
            autoComplete="off"
            spellCheck={false}
            inputMode="url"
            aria-describedby={error ? hintId : undefined}
            className="min-w-0 flex-1 rounded-xl bg-transparent px-4 py-3 text-[0.98rem] text-ink outline-none placeholder:text-muted"
          />
          <button type="submit" disabled={busy} className="btn btn-coral shrink-0">
            {busy ? "Opening…" : "Explain it"}
          </button>
        </div>
      </form>

      {error ? (
        <p
          id={hintId}
          role="alert"
          className={`mt-3 rounded-xl border-[1.5px] border-ink px-4 py-2.5 text-sm ${
            tone === "onColor" ? "bg-paper" : "bg-mustard-soft"
          }`}
        >
          {error}
        </p>
      ) : null}

      {examples ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink-soft">Try one:</span>
          {EXAMPLES.map((ex) => (
            <button
              key={ex.id}
              type="button"
              disabled={busy}
              onClick={() => {
                const value = `https://arxiv.org/abs/${ex.id}`;
                setUrl(value);
                void go(value);
              }}
              className="pill bg-paper transition-colors hover:bg-lavender-soft"
            >
              {ex.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

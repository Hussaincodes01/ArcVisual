"use client";

import { useMemo, useRef, useState } from "react";
import { submitPaper } from "../../lib/api";
import type { RecentPaper } from "../../lib/types";
import { EXAMPLES } from "../SubmitForm";

const KINDS = [
  { key: "all", label: "All" },
  { key: "transform_chain", label: "Derivations" },
  { key: "plot_reveal", label: "Results" },
  { key: "architecture_flow", label: "Systems" },
] as const;

const HEADS = [
  { bg: "bg-lavender", pill: "bg-mustard" },
  { bg: "bg-mustard", pill: "bg-ink text-paper" },
  { bg: "bg-wash", pill: "bg-lavender" },
  { bg: "bg-ink", pill: "bg-mustard" },
];

function Glyph({ kind, dark }: { kind: string; dark: boolean }) {
  const ink = dark ? "#f7f7f5" : "#151313";
  if (kind === "plot_reveal") {
    return (
      <svg viewBox="0 0 200 120" className="h-full w-full" aria-hidden="true">
        <path d="M30 100 H180 M30 100 V20" stroke={ink} strokeWidth="5" strokeLinecap="round" fill="none" />
        <path d="M38 88 Q80 84 104 60 T172 26" stroke="#ff5734" strokeWidth="6" fill="none" strokeLinecap="round" />
        <circle cx="172" cy="26" r="9" fill="#fff" stroke={ink} strokeWidth="4" />
      </svg>
    );
  }
  if (kind === "architecture_flow") {
    return (
      <svg viewBox="0 0 200 120" className="h-full w-full" aria-hidden="true">
        <g stroke={ink} strokeWidth="5" strokeLinejoin="round">
          <rect x="14" y="40" width="48" height="40" rx="10" fill="#fff" />
          <rect x="78" y="40" width="48" height="40" rx="10" fill="#ff5734" />
          <rect x="142" y="40" width="48" height="40" rx="10" fill="#fff" />
          <path d="M62 60 H76 M126 60 H140" fill="none" strokeLinecap="round" />
        </g>
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 200 120" className="h-full w-full" aria-hidden="true">
      <g fill="none" stroke={ink} strokeWidth="5" strokeLinecap="round">
        <path d="M28 46 H86 M28 74 H70" />
        <path d="M100 60 H124 M116 52 L124 60 L116 68" />
        <path d="M140 46 H178 M140 74 H164" stroke="#ff5734" />
      </g>
    </svg>
  );
}

function authorsLine(authors: string[]): string {
  if (!authors.length) return "";
  if (authors.length <= 2) return authors.join(" and ");
  return `${authors[0]} and others`;
}

export default function Gallery({ papers }: { papers: RecentPaper[] }) {
  const [kind, setKind] = useState<(typeof KINDS)[number]["key"]>("all");
  const [page, setPage] = useState(0);
  const rail = useRef<HTMLDivElement | null>(null);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: papers.length };
    for (const p of papers) for (const a of p.archetypes) c[a] = (c[a] ?? 0) + 1;
    return c;
  }, [papers]);
  const shown = kind === "all" ? papers : papers.filter((p) => p.archetypes.includes(kind));
  const pages = Math.max(1, Math.ceil(shown.length / 3));

  const scrollTo = (next: number) => {
    const el = rail.current;
    if (!el) return;
    const clamped = Math.max(0, Math.min(pages - 1, next));
    el.scrollTo({ left: clamped * el.clientWidth, behavior: "smooth" });
    setPage(clamped);
  };

  if (!papers.length) return <EmptyGallery />;

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-wrap gap-2" role="tablist" aria-label="Filter explainers by kind of visual">
          {KINDS.filter((k) => k.key === "all" || counts[k.key]).map((k) => (
            <button
              key={k.key}
              role="tab"
              aria-selected={kind === k.key}
              onClick={() => {
                setKind(k.key);
                setPage(0);
                rail.current?.scrollTo({ left: 0 });
              }}
              className={`rounded-[0.9rem] border-[1.5px] border-ink px-4 py-2 text-[0.95rem] transition-colors ${
                kind === k.key ? "bg-ink text-paper" : "bg-paper hover:bg-wash"
              }`}
            >
              {k.label} ({counts[k.key] ?? 0})
            </button>
          ))}
        </div>
        {pages > 1 ? (
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => scrollTo(page - 1)}
              disabled={page === 0}
              aria-label="Previous"
              className="flex size-11 items-center justify-center rounded-xl border-[1.5px] border-ink bg-paper disabled:opacity-35"
            >
              <svg viewBox="0 0 20 20" className="size-5" aria-hidden="true">
                <path d="M16 10H4m5-5-5 5 5 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
            <span className="text-lg tabular-nums text-ink-soft">
              {page + 1}/{pages}
            </span>
            <button
              type="button"
              onClick={() => scrollTo(page + 1)}
              disabled={page >= pages - 1}
              aria-label="Next"
              className="flex size-11 items-center justify-center rounded-xl border-[1.5px] border-ink bg-paper disabled:opacity-35"
            >
              <svg viewBox="0 0 20 20" className="size-5" aria-hidden="true">
                <path d="M4 10h12m-5-5 5 5-5 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        ) : null}
      </div>

      <div
        ref={rail}
        onScroll={(e) => {
          const el = e.currentTarget;
          setPage(Math.round(el.scrollLeft / Math.max(1, el.clientWidth)));
        }}
        className="mt-6 grid snap-x snap-mandatory auto-cols-[85%] grid-flow-col gap-4 overflow-x-auto pb-2 [scrollbar-width:none] sm:auto-cols-[calc((100%-1rem)/2)] lg:auto-cols-[calc((100%-2rem)/3)]"
      >
        {shown.map((p, i) => {
          const head = HEADS[i % HEADS.length];
          const dark = head.bg === "bg-ink";
          const field = p.categories[0];
          const primary = p.archetypes[0] ?? "transform_chain";
          return (
            <article key={p.slug} className="flex snap-start flex-col rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-3">
              <div className={`relative h-44 overflow-hidden rounded-[var(--radius-tile)] ${head.bg}`}>
                {field ? <span className={`pill absolute left-3 top-3 z-10 text-xs ${head.pill}`}>{field}</span> : null}
                <div className="absolute inset-x-6 bottom-3 top-10">
                  <Glyph kind={primary} dark={dark} />
                </div>
              </div>
              <h3 className="mt-4 line-clamp-2 min-h-[3.1rem] text-[1.15rem] font-semibold leading-snug tracking-tight">{p.title}</h3>
              <p className="mt-1 line-clamp-1 text-sm text-muted">{authorsLine(p.authors)}</p>
              <p className="mt-3 text-sm text-ink-soft">
                {p.visuals} {p.visuals === 1 ? "visual" : "visuals"}, {p.sections} sections
              </p>
              <a href={`/p/${p.slug}`} className="btn btn-coral mt-4 w-full">
                Read the explainer
              </a>
            </article>
          );
        })}
        {kind === "all" && shown.length < 3 ? (
          <a
            href="#start"
            className="flex snap-start flex-col items-start justify-end rounded-[var(--radius-card)] border-[1.5px] border-dashed border-ink bg-canvas p-6 transition-colors hover:bg-mustard-soft"
          >
            <span className="flex size-12 items-center justify-center rounded-full border-[1.5px] border-ink bg-mustard text-2xl" aria-hidden="true">
              +
            </span>
            <span className="mt-5 text-[1.15rem] font-semibold leading-snug tracking-tight">Your paper could be next</span>
            <span className="mt-1 text-sm text-ink-soft">Paste any arXiv link at the top of the page. Once explained, it opens here for everyone.</span>
          </a>
        ) : null}
      </div>
    </div>
  );
}

function EmptyGallery() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const start = async (id: string) => {
    setBusy(id);
    setError(null);
    try {
      const res = await submitPaper(`https://arxiv.org/abs/${id}`);
      window.location.href = res.cached && res.slug ? `/p/${res.slug}` : `/watch/${res.job_id}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start that one. Try again.");
      setBusy(null);
    }
  };
  return (
    <div>
      <p className="max-w-xl text-ink-soft">
        Nothing has been explained here yet. Start with a classic: each one takes about a minute the first time,
        and opens instantly for everyone after.
      </p>
      <div className="mt-6 grid gap-4 md:grid-cols-3">
        {EXAMPLES.map((ex, i) => (
          <article key={ex.id} className="flex flex-col rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-3">
            <div className={`h-32 rounded-[var(--radius-tile)] ${HEADS[i % HEADS.length].bg}`} />
            <h3 className="mt-4 text-lg font-semibold leading-snug">{ex.label}</h3>
            <p className="mt-1 text-sm text-muted">arXiv {ex.id}</p>
            <button type="button" onClick={() => void start(ex.id)} disabled={busy !== null} className="btn btn-coral mt-4">
              {busy === ex.id ? "Starting…" : "Explain this paper"}
            </button>
          </article>
        ))}
      </div>
      {error ? <p role="alert" className="mt-4 text-sm text-warn">{error}</p> : null}
    </div>
  );
}

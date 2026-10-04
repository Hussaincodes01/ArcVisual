"use client";

/**
 * The reading experience: prose on the left, a sticky stage of visuals on the
 * right, one column on small screens.
 *
 * Scrollama decides which section's visuals are on the stage. It does not drive
 * playback — each scene autoplays from its own IntersectionObserver.
 *
 * While the job behind this article is still running, the page drives it (a
 * serverless job only moves while someone asks it to) and refetches the document
 * whenever another visual lands, so scenes fill in place without a reload.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import katex from "katex";
import { fetchPaperFresh } from "../lib/api";
import { isTerminalScene, type PaperResponse, type Scene, type Section, type Storyboard } from "../lib/types";
import { useJobStatus } from "../hooks/useJobStatus";
import { ArcDots } from "./ArcLoader";
import SceneSlot from "./SceneSlot";

const NO_MACROS: Record<string, string> = {};

/** Mirrors arcvisual/ingest/latex.py EQ_MARKER: where a display equation sat. */
const EQ_MARKER = "⟦eq⟧";

interface Props {
  initial: PaperResponse;
}

export default function Article({ initial }: Props) {
  const [doc, setDoc] = useState(initial);
  const storyboard = doc.storyboard;
  const building = doc.state !== undefined && doc.state !== "complete" && doc.state !== "failed";
  const job = useJobStatus(building ? doc.job_id : null);

  // Refetch the document whenever the job reports progress, and once at the end.
  const progressKey = `${job.status?.state ?? ""}:${job.status?.stage_progress?.scenes_done ?? ""}`;
  useEffect(() => {
    if (!building || !job.status) return;
    let cancelled = false;
    fetchPaperFresh(storyboard.paper.slug)
      .then((fresh) => {
        if (!cancelled && fresh) setDoc(fresh);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [progressKey]);

  const sections = useMemo(() => orderedSections(storyboard), [storyboard]);
  const scenesBySection = useMemo(() => groupScenes(storyboard), [storyboard]);
  const conceptNames = useMemo(() => new Map(storyboard.concepts.map((c) => [c.id, c.name])), [storyboard]);
  const macros = storyboard.paper.tex_macros ?? NO_MACROS;
  const pending = storyboard.scenes.filter((s) => !isTerminalScene(s)).length;
  const ready = storyboard.scenes.filter((s) => s.state === "passed").length;

  const [active, setActive] = useState<string | null>(null);
  useEffect(() => {
    let destroy: (() => void) | undefined;
    let disposed = false;
    void (async () => {
      try {
        const mod = await import("scrollama");
        if (disposed) return;
        const instance = mod.default();
        instance
          .setup({ step: "[data-step]", offset: 0.4 })
          .onStepEnter(({ element }: { element: HTMLElement }) => setActive(element.dataset.step ?? null));
        const onResize = () => instance.resize();
        window.addEventListener("resize", onResize);
        destroy = () => {
          window.removeEventListener("resize", onResize);
          instance.destroy();
        };
      } catch {
        // An enhancement: without it the stage shows the first section's visuals.
      }
    })();
    return () => {
      disposed = true;
      destroy?.();
    };
  }, [sections.length]);

  const firstWithScenes = sections.find((s) => scenesBySection.has(s.id))?.id;
  const stageId = active && scenesBySection.has(active) ? active : (lastWithScenesBefore(sections, active, scenesBySection) ?? firstWithScenes);
  const stageScenes = (stageId && scenesBySection.get(stageId)) || [];
  const stageSection = sections.find((s) => s.id === stageId);

  return (
    <div className="mx-auto max-w-7xl px-4 pb-32 sm:px-6 lg:px-10">
      <Header storyboard={storyboard} pending={pending} ready={ready} building={building} failed={doc.state === "failed"} />

      {/* grid-cols-1 pins the single mobile column to the viewport. Without it the
          implicit track grows to fit its widest child — a long equation — and the
          whole page scrolled sideways on phones. */}
      <div className="mt-12 grid grid-cols-1 gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.92fr)]">
        <div className="min-w-0">
          {sections.map((section) => {
            const scenes = scenesBySection.get(section.id) ?? [];
            return (
              <section key={section.id} id={section.id} data-step={section.id} className="mb-16 scroll-mt-24">
                <SectionHeading section={section} concepts={section.concept_ids.map((id) => conceptNames.get(id)).filter(Boolean) as string[]} originUrl={storyboard.paper.origin_url} />
                <Prose section={section} storyboard={storyboard} macros={macros} />
                {scenes.length ? (
                  <div className="mt-8 space-y-6 lg:hidden">
                    {scenes.map((scene) => (
                      <SceneSlot key={scene.spec.id} scene={scene} sectionHeading={section.heading_path.at(-1) ?? section.id} mediaBase={doc.media_base} originUrl={storyboard.paper.origin_url} macros={macros} />
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>

        <aside className="stage hidden lg:block" aria-label="Visuals for the section you are reading">
          {stageScenes.length ? (
            <div className="space-y-6">
              {stageScenes.map((scene) => (
                <SceneSlot key={scene.spec.id} scene={scene} sectionHeading={stageSection?.heading_path.at(-1) ?? scene.spec.span.section_id} mediaBase={doc.media_base} originUrl={storyboard.paper.origin_url} macros={macros} />
              ))}
            </div>
          ) : (
            <div className="rounded-[var(--radius-card)] border-[1.5px] border-dashed border-ink bg-paper p-8 text-ink-soft">
              {storyboard.scenes.length === 0 && !building
                ? "This paper reads best as prose, so it has no visuals. Not every idea needs an animation."
                : "Visuals appear here beside the sections they explain."}
            </div>
          )}
        </aside>
      </div>

      <Outline sections={sections} active={active} withScenes={scenesBySection} />
    </div>
  );
}

// --------------------------------------------------------------------------- //

function Header({
  storyboard,
  pending,
  ready,
  building,
  failed,
}: {
  storyboard: Storyboard;
  pending: number;
  ready: number;
  building: boolean;
  failed: boolean;
}) {
  const { paper } = storyboard;
  const [open, setOpen] = useState(false);
  const pdf = paper.arxiv_id ? `https://arxiv.org/pdf/${paper.arxiv_id}` : null;
  const categories = paper.categories ?? [];
  return (
    <header className="rounded-[2rem] border-[1.5px] border-ink bg-paper p-6 sm:p-10">
      <div className="flex flex-wrap items-center gap-2">
        {categories.slice(0, 3).map((c, i) => (
          <span key={c} className={`pill text-xs ${["bg-lavender", "bg-mustard", "bg-paper"][i % 3]}`}>
            {c}
          </span>
        ))}
        {paper.arxiv_id ? <span className="pill bg-paper text-xs">arXiv {paper.arxiv_id}</span> : null}
      </div>
      <h1 className="mt-5 max-w-4xl text-[clamp(1.9rem,4.2vw,3.25rem)] font-semibold leading-[1.06] tracking-[-0.03em]">{paper.title}</h1>
      <p className="mt-4 max-w-3xl text-ink-soft">
        {paper.authors.slice(0, 8).join(", ")}
        {paper.authors.length > 8 ? `, and ${paper.authors.length - 8} more` : ""}
      </p>

      {paper.abstract ? (
        <div className="mt-6 max-w-3xl">
          <p className={`leading-relaxed text-ink-soft ${open ? "" : "line-clamp-3"}`}>{paper.abstract}</p>
          <button type="button" onClick={() => setOpen((o) => !o)} className="mt-2 text-sm font-medium underline decoration-coral decoration-2 underline-offset-4">
            {open ? "Show less" : "Read the full abstract"}
          </button>
        </div>
      ) : null}

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <a href={paper.origin_url} target="_blank" rel="noreferrer" className="btn btn-coral">
          Open on arXiv
        </a>
        {pdf ? (
          <a href={pdf} target="_blank" rel="noreferrer" className="btn btn-ghost">
            PDF
          </a>
        ) : null}
        <span className="text-sm text-muted">
          {paper.license.includes("creativecommons") ? "CC-licensed paper" : "Figures stay with the paper; we link to them"}
        </span>
      </div>

      {building || pending > 0 ? (
        <p className="mt-6 inline-flex items-center gap-3 rounded-full border-[1.5px] border-ink bg-mustard-soft px-4 py-2 text-sm" role="status">
          {storyboard.scenes.length === 0
            ? "Working out which ideas deserve a visual"
            : `Building visuals: ${ready} of ${storyboard.scenes.length} ready`}
          <ArcDots />
        </p>
      ) : null}
      {failed ? (
        <p className="mt-6 rounded-xl border-[1.5px] border-ink bg-wash px-4 py-3 text-sm">
          This explainer stopped before it finished. The text below is complete; some visuals may be missing.
        </p>
      ) : null}
    </header>
  );
}

function SectionHeading({ section, concepts, originUrl }: { section: Section; concepts: string[]; originUrl: string }) {
  const depth = section.heading_path.length;
  const text = section.heading_path.at(-1) ?? section.id;
  const Tag = depth <= 1 ? "h2" : "h3";
  return (
    <div className="mb-5">
      <div className="flex items-start justify-between gap-4">
        <Tag className={depth <= 1 ? "text-[1.75rem] font-semibold leading-tight tracking-tight" : "text-xl font-semibold leading-snug"}>{text}</Tag>
        <a href={originUrl} target="_blank" rel="noreferrer" className="mt-1 shrink-0 text-xs text-muted underline decoration-dotted underline-offset-4 hover:text-coral">
          In the paper
        </a>
      </div>
      {section.difficulty || concepts.length ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {section.difficulty ? <Difficulty level={section.difficulty} /> : null}
          {concepts.slice(0, 4).map((c) => (
            <span key={c} className="pill bg-lavender-soft text-xs">
              {c}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Difficulty({ level }: { level: number }) {
  return (
    <span className="pill bg-paper text-xs" title={`Difficulty ${level} of 5`}>
      <span className="flex gap-0.5" aria-hidden="true">
        {[1, 2, 3, 4, 5].map((i) => (
          <span key={i} className={`block h-2.5 w-1.5 rounded-sm border border-ink ${i <= level ? "bg-coral" : "bg-paper"}`} />
        ))}
      </span>
      <span className="sr-only">Difficulty {level} of 5</span>
      <span aria-hidden="true">{["", "Gentle", "Moderate", "Involved", "Hard", "Very hard"][level] ?? ""}</span>
    </span>
  );
}

function Prose({ section, storyboard, macros }: { section: Section; storyboard: Storyboard; macros: Record<string, string> }) {
  const equations = useMemo(
    () => section.equation_ids.map((id) => storyboard.equations.find((e) => e.id === id)).filter((e): e is NonNullable<typeof e> => Boolean(e)),
    [section.equation_ids, storyboard.equations],
  );
  const blocks = useMemo(() => {
    const out: ({ kind: "p"; text: string } | { kind: "eq"; latex: string })[] = [];
    let next = 0;
    for (const raw of section.prose_md.split(/\n{2,}/)) {
      const text = raw.trim();
      if (!text) continue;
      if (text === EQ_MARKER) {
        if (next < equations.length) out.push({ kind: "eq", latex: equations[next++].latex });
        continue;
      }
      // A marker glued to text (older documents): split it out.
      const parts = text.split(EQ_MARKER);
      parts.forEach((part, i) => {
        if (part.trim()) out.push({ kind: "p", text: part.trim() });
        if (i < parts.length - 1 && next < equations.length) out.push({ kind: "eq", latex: equations[next++].latex });
      });
    }
    // Documents ingested before markers existed: equations after the text.
    for (; next < equations.length; next++) out.push({ kind: "eq", latex: equations[next].latex });
    return out;
  }, [section.prose_md, equations]);

  return (
    <div className="prose-arc">
      {blocks.map((b, i) => (b.kind === "p" ? <p key={i}>{inlineMath(b.text, macros)}</p> : <DisplayMath key={i} latex={b.latex} macros={macros} />))}
    </div>
  );
}

const INLINE_MATH = /(\$\$[^$]+\$\$|\$[^$\n]{1,400}\$|\\\([^)]{1,400}\\\))/g;

function inlineMath(text: string, macros: Record<string, string>): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_MATH)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const raw = m[0];
    const latex = raw.startsWith("$$") ? raw.slice(2, -2) : raw.startsWith("$") ? raw.slice(1, -1) : raw.slice(2, -2);
    let html = "";
    try {
      // Macros copied per call: KaTeX writes \gdef definitions into the object.
      html = katex.renderToString(latex, { displayMode: false, throwOnError: true, strict: "ignore", macros: { ...macros } });
    } catch {
      html = "";
    }
    out.push(
      html ? (
        <span key={at} dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        // KaTeX could not parse it: show readable math, never the source.
        <span key={at} className="math-fallback">
          {toUnicode(latex, macros)}
        </span>
      ),
    );
    last = at + raw.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/**
 * Ingest stores the inside of an `align`/`eqnarray`, so alignment points (`&`) and
 * line breaks (`\\`) arrive bare, which KaTeX rejects outside an environment. Put
 * them back in `aligned`, and drop numbering commands that mean nothing here.
 */
function prepareDisplay(latex: string): string {
  const body = latex.replace(/\\(?:nonumber|notag)\b/g, "").trim();
  const aligned = /(^|[^\\])&/.test(body) || /\\\\/.test(body);
  if (aligned && !/\\begin\{/.test(body)) return `\\begin{aligned}${body}\\end{aligned}`;
  return body;
}

/**
 * A displayed equation, typeset — and, for readers who do not read the notation,
 * readable aloud in plain English on request ("S equals the fraction Q K transpose
 * over the square root of d sub k").
 *
 * KaTeX renders synchronously, so equations never reflow the page mid-scroll. When
 * it cannot parse something, the fallback is Unicode math (√dₖ), never TeX source.
 */
function DisplayMath({ latex, macros }: { latex: string; macros: Record<string, string> }) {
  const [inWords, setInWords] = useState(false);
  const html = useMemo(() => {
    try {
      return katex.renderToString(prepareDisplay(latex), { displayMode: true, throwOnError: true, strict: "ignore", macros: { ...macros } });
    } catch {
      return "";
    }
  }, [latex, macros]);
  const reading = useMemo(() => (inWords ? toWords(latex, macros) : ""), [inWords, latex, macros]);
  return (
    <figure className="my-6 rounded-[var(--radius-tile)] border-[1.5px] border-line bg-paper">
      {html ? (
        <div className="scroll-x px-4 py-3" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <p className="math-fallback scroll-x px-4 py-3 text-center text-lg text-ink">{toUnicode(latex, macros)}</p>
      )}
      <figcaption className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-t border-dashed border-line px-4 py-2 text-sm">
        <button
          type="button"
          onClick={() => setInWords((v) => !v)}
          aria-expanded={inWords}
          className="shrink-0 font-medium text-ink-soft underline decoration-coral decoration-dotted underline-offset-4 hover:text-ink"
        >
          {inWords ? "Hide words" : "Read it in words"}
        </button>
        {inWords && reading ? <span className="text-ink-soft">{reading}</span> : null}
      </figcaption>
    </figure>
  );
}

function Outline({ sections, active, withScenes }: { sections: Section[]; active: string | null; withScenes: Map<string, Scene[]> }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const index = active ? sections.findIndex((s) => s.id === active) : 0;
  const pct = sections.length ? ((index + 1) / sections.length) * 100 : 0;
  useEffect(() => {
    ref.current?.querySelector(`[data-o="${active}"]`)?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [active]);
  return (
    <nav aria-label="Sections" className="fixed inset-x-3 bottom-3 z-30 mx-auto max-w-5xl rounded-2xl border-[1.5px] border-ink bg-paper/95 backdrop-blur">
      <div className="h-1 overflow-hidden rounded-t-2xl bg-wash">
        <div className="h-full bg-coral transition-[width] duration-300" style={{ width: `${pct}%` }} />
      </div>
      <div ref={ref} className="scroll-x flex gap-1.5 px-2 py-2 text-xs [scrollbar-width:none]">
        {sections.map((s) => (
          <a
            key={s.id}
            data-o={s.id}
            href={`#${s.id}`}
            className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 transition-colors ${s.id === active ? "bg-ink text-paper" : "text-ink-soft hover:bg-wash"}`}
          >
            {withScenes.has(s.id) ? <span className="size-1.5 rounded-full bg-coral" aria-label="has visuals" /> : null}
            {s.heading_path.at(-1)}
          </a>
        ))}
      </div>
    </nav>
  );
}

// --------------------------------------------------------------------------- //

function orderedSections(sb: Storyboard): Section[] {
  const byId = new Map(sb.sections.map((s) => [s.id, s]));
  const ordered = sb.reading_order.length ? sb.reading_order.map((id) => byId.get(id)).filter((s): s is Section => Boolean(s)) : sb.sections;
  // Structural parents (a bare \section followed by a subsection) carry no prose.
  return ordered.filter((s) => s.prose_md.trim().length > 0);
}

function groupScenes(sb: Storyboard): Map<string, Scene[]> {
  const map = new Map<string, Scene[]>();
  for (const scene of sb.scenes) {
    if (scene.state === "failed") continue; // carried by the prose; no slot at all
    const key = scene.spec.span.section_id;
    map.set(key, [...(map.get(key) ?? []), scene]);
  }
  for (const list of map.values()) list.sort((a, b) => a.spec.priority - b.spec.priority);
  return map;
}

/** Keep the most recent section's visuals on stage while reading prose-only sections. */
function lastWithScenesBefore(sections: Section[], active: string | null, map: Map<string, Scene[]>): string | undefined {
  if (!active) return undefined;
  const at = sections.findIndex((s) => s.id === active);
  for (let i = at; i >= 0; i--) if (map.has(sections[i].id)) return sections[i].id;
  return undefined;
}

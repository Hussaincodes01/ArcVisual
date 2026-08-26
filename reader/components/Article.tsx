"use client";

/**
 * The reading experience: prose left, sticky visual stage right, single column on
 * mobile.
 *
 * Scrollama drives *which* scene is on the stage — it is the de facto standard for
 * exactly this and costs about 2KB. It does not drive playback; each SceneSlot owns
 * its own IntersectionObserver, so a scene plays when it is visible whether or not
 * Scrollama has settled.
 *
 * Progressive fill polls `GET /api/jobs/{id}` when a `jobId` is supplied. Polling
 * rather than SSE is deliberate (see arcvisual/render/api.py): one JSONB read,
 * survives every proxy, nothing to operate.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";
import { fetchJob } from "../lib/api";
import { isShippable, type Scene, type Section, type Storyboard } from "../lib/types";
import SceneSlot from "./SceneSlot";

interface Props {
  storyboard: Storyboard;
  mediaBase: string;
  /** Present while the pipeline is still filling slots. */
  jobId?: string;
}

export default function Article({ storyboard, mediaBase, jobId }: Props) {
  const [activeSection, setActiveSection] = useState<string | null>(null);
  const [readyKeys, setReadyKeys] = useState<Set<string>>(
    () => new Set(storyboard.scenes.filter(isShippable).map((s) => s.spec.id))
  );
  const [jobState, setJobState] = useState<string | null>(null);

  const sections = useMemo(() => orderedSections(storyboard), [storyboard]);
  const scenesBySection = useMemo(() => groupScenes(storyboard), [storyboard]);
  const pendingCount = storyboard.scenes.filter((s) => !isShippable(s)).length;

  // -- progressive fill --------------------------------------------------- //
  useEffect(() => {
    if (!jobId || pendingCount === 0) return;
    let cancelled = false;
    let delay = 3000; // back off to 10s after the first couple of minutes
    let elapsed = 0;

    const tick = async () => {
      if (cancelled) return;
      try {
        const status = await fetchJob(jobId);
        if (cancelled) return;
        setJobState(status.state);
        setReadyKeys((prev) => {
          const next = new Set(prev);
          for (const s of status.scenes) if (s.ready) next.add(s.key);
          return next;
        });
        if (status.state === "complete" || status.state === "failed") {
          // The document itself changed; a reload picks up the new artifacts.
          if (status.state === "complete") window.location.reload();
          return;
        }
      } catch {
        // A failed poll is not worth surfacing; the next one will tell us.
      }
      elapsed += delay;
      if (elapsed > 120_000) delay = 10_000;
      window.setTimeout(tick, delay);
    };

    const handle = window.setTimeout(tick, delay);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [jobId, pendingCount]);

  // -- scrollama ---------------------------------------------------------- //
  const stepsRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    let scroller: { destroy?: () => void } | null = null;
    let disposed = false;

    void (async () => {
      try {
        const mod = await import("scrollama");
        if (disposed) return;
        const instance = mod.default();
        instance
          .setup({ step: "[data-step]", offset: 0.5, progress: false })
          .onStepEnter(({ element }: { element: HTMLElement }) => {
            setActiveSection(element.dataset.step ?? null);
          });
        window.addEventListener("resize", instance.resize);
        scroller = {
          destroy: () => {
            window.removeEventListener("resize", instance.resize);
            instance.destroy();
          },
        };
      } catch {
        // Scrollama is an enhancement. Without it the article still reads; the
        // stage simply shows the first section's scene.
      }
    })();

    return () => {
      disposed = true;
      scroller?.destroy?.();
    };
  }, []);

  const stageScenes =
    (activeSection && scenesBySection.get(activeSection)) ||
    scenesBySection.get(sections[0]?.id ?? "") ||
    [];

  return (
    <div className="mx-auto max-w-[96rem] px-4 pb-32 lg:px-8">
      <Header storyboard={storyboard} pendingCount={pendingCount} jobState={jobState} />

      <div className="grid gap-10 lg:grid-cols-[minmax(0,58ch)_minmax(0,1fr)]">
        {/* Prose column */}
        <div ref={stepsRef}>
          {sections.map((section) => {
            const scenes = scenesBySection.get(section.id) ?? [];
            return (
              <section
                key={section.id}
                id={section.id}
                data-step={section.id}
                className="mb-16 scroll-mt-24"
              >
                <SectionHeading section={section} originUrl={storyboard.paper.origin_url} />
                <Prose section={section} storyboard={storyboard} />

                {/* On mobile the stage is inline, at full width. */}
                <div className="mt-6 space-y-6 lg:hidden">
                  {scenes.map((scene) => (
                    <SceneSlot
                      key={scene.spec.id}
                      scene={scene}
                      mediaBase={mediaBase}
                      sectionHeading={section.heading_path.at(-1) ?? section.id}
                      originUrl={storyboard.paper.origin_url}
                      pending={!readyKeys.has(scene.spec.id) && !isShippable(scene)}
                    />
                  ))}
                </div>
              </section>
            );
          })}
        </div>

        {/* Sticky visual stage (desktop only) */}
        <div className="stage hidden lg:block">
          <div className="space-y-6">
            {stageScenes.length > 0 ? (
              stageScenes.map((scene) => (
                <SceneSlot
                  key={scene.spec.id}
                  scene={scene}
                  mediaBase={mediaBase}
                  sectionHeading={
                    sections.find((s) => s.id === scene.spec.span.section_id)
                      ?.heading_path.at(-1) ?? scene.spec.span.section_id
                  }
                  originUrl={storyboard.paper.origin_url}
                  pending={!readyKeys.has(scene.spec.id) && !isShippable(scene)}
                />
              ))
            ) : (
              <p className="rounded-lg border border-dashed border-border p-6 text-sm text-muted">
                This section is carried by prose. Not everything needs an animation.
              </p>
            )}
          </div>
        </div>
      </div>

      <Outline sections={sections} active={activeSection} />
    </div>
  );
}

// --------------------------------------------------------------------------- //

function Header({
  storyboard,
  pendingCount,
  jobState,
}: {
  storyboard: Storyboard;
  pendingCount: number;
  jobState: string | null;
}) {
  const { paper } = storyboard;
  return (
    <header className="mb-14 border-b border-border pb-8 pt-10">
      <p className="mb-3 text-xs uppercase tracking-widest text-muted">
        ArcVisual · a companion to the paper, not a replacement
      </p>
      <h1 className="max-w-3xl text-3xl font-semibold leading-tight text-fg sm:text-4xl">
        {paper.title}
      </h1>
      <p className="mt-3 max-w-3xl text-sm text-muted">
        {paper.authors.slice(0, 6).join(", ")}
        {paper.authors.length > 6 ? " et al." : ""}
      </p>
      <p className="prose-arc mt-6 text-muted">{paper.abstract}</p>
      <div className="mt-6 flex flex-wrap items-center gap-4 text-sm">
        <a
          className="text-accent underline decoration-dotted"
          href={paper.origin_url}
          target="_blank"
          rel="noreferrer"
        >
          Read the original paper →
        </a>
        <span className="text-muted">{paper.license.includes("creativecommons") ? "CC-licensed" : "arXiv licence"}</span>
        {pendingCount > 0 ? (
          <span className="rounded bg-surface px-2 py-1 text-xs text-muted">
            {pendingCount} animation{pendingCount === 1 ? "" : "s"} still rendering
            {jobState ? ` · ${jobState}` : ""}
          </span>
        ) : null}
      </div>
    </header>
  );
}

function SectionHeading({
  section,
  originUrl,
}: {
  section: Section;
  originUrl: string;
}) {
  const depth = section.heading_path.length;
  const text = section.heading_path.at(-1) ?? section.id;
  return (
    <div className="mb-4 flex items-baseline justify-between gap-4">
      {depth <= 1 ? (
        <h2 className="text-2xl font-semibold text-fg">{text}</h2>
      ) : (
        <h3 className="text-xl font-semibold text-fg">{text}</h3>
      )}
      {/* Every section links out. ArcVisual is a companion and should say so. */}
      <a
        className="shrink-0 text-xs text-muted underline decoration-dotted hover:text-accent"
        href={originUrl}
        target="_blank"
        rel="noreferrer"
      >
        view in original
      </a>
    </div>
  );
}

function Prose({
  section,
  storyboard,
}: {
  section: Section;
  storyboard: Storyboard;
}) {
  const equations = storyboard.equations.filter((e) =>
    section.equation_ids.includes(e.id)
  );
  const paragraphs = section.prose_md
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

  return (
    <div className="prose-arc">
      {paragraphs.map((text, i) => (
        <p key={i}>{text}</p>
      ))}
      {equations.map((eq) => (
        <Math key={eq.id} latex={eq.latex} />
      ))}
    </div>
  );
}

/**
 * KaTeX, rendered synchronously. That is the whole reason KaTeX over MathJax: an
 * async typesetter reflows the page mid-scroll, and reflow during a scroll-driven
 * reading experience is the one jank a reader always notices.
 */
function Math({ latex }: { latex: string }) {
  const html = useMemo(() => {
    try {
      return katex.renderToString(latex, {
        displayMode: true,
        throwOnError: false,
        strict: "ignore",
      });
    } catch {
      return "";
    }
  }, [latex]);

  if (!html) {
    return (
      <pre className="scroll-x rounded border border-border bg-surface/60 p-3 text-xs text-muted">
        {latex}
      </pre>
    );
  }
  return (
    <div
      className="scroll-x my-5"
      // KaTeX output only; the LaTeX itself came verbatim from the paper source.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function Outline({
  sections,
  active,
}: {
  sections: Section[];
  active: string | null;
}) {
  const index = active ? sections.findIndex((s) => s.id === active) : 0;
  const progress = sections.length ? ((index + 1) / sections.length) * 100 : 0;
  return (
    <nav className="fixed inset-x-0 bottom-0 border-t border-border bg-canvas/95 backdrop-blur">
      <div
        className="h-0.5 bg-accent transition-[width] duration-300"
        style={{ width: `${progress}%` }}
      />
      <div className="scroll-x mx-auto flex max-w-7xl gap-4 px-4 py-2 text-xs lg:px-8">
        {sections.map((s) => (
          <a
            key={s.id}
            href={`#${s.id}`}
            className={
              s.id === active
                ? "whitespace-nowrap text-accent"
                : "whitespace-nowrap text-muted hover:text-fg"
            }
          >
            {s.heading_path.at(-1)}
          </a>
        ))}
      </div>
    </nav>
  );
}

// --------------------------------------------------------------------------- //

function orderedSections(storyboard: Storyboard): Section[] {
  const byId = new Map(storyboard.sections.map((s) => [s.id, s]));
  const ordered = storyboard.reading_order.length
    ? storyboard.reading_order.map((id) => byId.get(id)).filter(Boolean as unknown as (s: Section | undefined) => s is Section)
    : storyboard.sections;
  // Structural parents (a bare \section followed straight by a subsection) carry
  // no prose and would render as an empty step.
  return ordered.filter((s) => s.prose_md.trim().length > 0);
}

function groupScenes(storyboard: Storyboard): Map<string, Scene[]> {
  const map = new Map<string, Scene[]>();
  for (const scene of storyboard.scenes) {
    if (scene.state === "failed") continue; // prose-only; no slot at all
    const key = scene.spec.span.section_id;
    const list = map.get(key) ?? [];
    list.push(scene);
    map.set(key, list);
  }
  for (const list of map.values()) list.sort((a, b) => a.spec.priority - b.spec.priority);
  return map;
}

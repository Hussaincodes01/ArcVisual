/**
 * The landing page. A server component: everything is in the initial HTML except
 * the live scenes, the form and the gallery filter, which are islands.
 *
 * The hero shows the product rather than describing it: a real scene, drawn live by
 * the same renderer the articles use, from parameters a model would produce.
 */

import type { Metadata } from "next";
import Gallery from "../components/landing/Gallery";
import SceneCard from "../components/SceneCard";
import SiteHeader, { Logo } from "../components/SiteHeader";
import SubmitForm from "../components/SubmitForm";
import { Blocks, Cloud, PaperSheet, Pencil, Sparkle } from "../components/art";
import { fetchRecentPapers } from "../lib/api";
import { ATTENTION_DEMO, ATTENTION_DIAGRAM_DEMO, RESNET_DEMO, TRANSFORMER_DEMO } from "../lib/demos";

export const revalidate = 120;

export const metadata: Metadata = {
  title: { absolute: "ArcVisual — see the idea behind any arXiv paper" },
};

export default async function Landing() {
  const papers = await fetchRecentPapers(12);
  return (
    <div className="overflow-x-clip">
      <SiteHeader />
      <main>
        <Hero />
        <Explainers papers={papers} />
        <HowItWorks />
        <Visuals />
        <Audience />
        <GoodToKnow />
        <Closing />
      </main>
      <Footer />
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Hero() {
  return (
    <section id="start" className="relative mx-auto max-w-7xl scroll-mt-6 px-4 pb-16 pt-6 sm:px-6 lg:px-10 lg:pb-24 lg:pt-10">
      {/* The faint swoosh from the reference, behind everything. */}
      <svg viewBox="0 0 1200 600" className="pointer-events-none absolute inset-x-0 top-24 -z-10 hidden w-full lg:block" aria-hidden="true">
        <path d="M-40 470 C 260 520, 420 360, 600 330 S 980 170, 1240 260" fill="none" stroke="#ebe9e2" strokeWidth="26" strokeLinecap="round" />
      </svg>

      <div className="grid items-start gap-12 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)] lg:gap-14">
        <div className="hero-rise">
          <h1 className="text-[clamp(2.9rem,7.4vw,5.6rem)] font-semibold leading-[0.98] tracking-[-0.035em]">
            See the <span className="text-coral">idea</span>
            <br />
            behind any paper
          </h1>
          <p className="mt-7 max-w-[34rem] text-lg leading-relaxed text-ink-soft sm:text-xl">
            Paste an arXiv link. ArcVisual reads the paper&rsquo;s source, finds the parts that are genuinely hard,
            and turns them into animations you can play, pause and scrub. Every claim points back to the paper&rsquo;s
            own words.
          </p>
          <div className="mt-9 max-w-[38rem]">
            <SubmitForm />
          </div>

          <dl className="mt-12 grid max-w-[38rem] grid-cols-1 gap-3 sm:grid-cols-3">
            <Stat tone="bg-paper" tag="Input" tagTone="bg-lavender" small="paste a link from" big="arXiv" />
            <Stat tone="bg-lavender" tag="Visuals" tagTone="bg-mustard" small="kinds of animation" big="3" />
            <Stat
              tone="bg-mustard"
              tag={
                <>
                  <Stars /> Grounded
                </>
              }
              tagTone="bg-paper"
              small="of claims cite the paper"
              big="100%"
            />
          </dl>
        </div>

        <div className="relative lg:pt-4">
          <FieldBadges />
          <div className="relative mt-6">
            <Sparkle className="absolute -left-6 -top-8 z-10 size-16 sm:-left-10 sm:size-20" />
            <Sparkle className="absolute -left-4 bottom-20 z-10 size-9" fill="#be94f5" />
            <Cloud className="absolute -right-12 bottom-24 z-0 hidden w-32 sm:block" />
            <div className="relative z-[5] -rotate-1">
              <SceneCard
                archetype={ATTENTION_DIAGRAM_DEMO.archetype}
                params={ATTENTION_DIAGRAM_DEMO.params}
                beats={ATTENTION_DIAGRAM_DEMO.beats}
                claim={ATTENTION_DIAGRAM_DEMO.claim}
                source={`Drawn live in your browser, from ${ATTENTION_DIAGRAM_DEMO.source}`}
                autoplay="always"
                loop
              />
            </div>
            <Pencil className="absolute -bottom-12 -right-6 z-10 w-56 rotate-[-18deg] sm:w-72" />
          </div>
        </div>
      </div>
    </section>
  );
}

function Stars() {
  return (
    <span className="tracking-[-0.12em] text-coral" aria-hidden="true">
      ★★★
    </span>
  );
}

function Stat({
  tone,
  tag,
  tagTone,
  small,
  big,
}: {
  tone: string;
  tag: React.ReactNode;
  tagTone: string;
  small: string;
  big: string;
}) {
  return (
    <div className={`rounded-[var(--radius-tile)] border-[1.5px] border-ink p-4 ${tone}`}>
      <span className={`pill text-xs ${tagTone}`}>{tag}</span>
      <dt className="mt-3 text-sm text-ink-soft">{small}</dt>
      <dd className="text-[2.5rem] font-semibold leading-none tracking-[-0.03em]">{big}</dd>
    </div>
  );
}

function FieldBadges() {
  const fields = [
    { label: "cs", tone: "bg-mustard" },
    { label: "math", tone: "bg-lavender" },
    { label: "phys", tone: "bg-coral text-paper" },
    { label: "stat", tone: "bg-paper" },
    { label: "bio", tone: "bg-lavender-soft" },
  ];
  return (
    <div className="flex items-center justify-end gap-4">
      <p className="text-right text-sm leading-tight text-ink-soft">
        Any arXiv field
        <br />
        with LaTeX source
      </p>
      <div className="flex">
        {fields.map((f, i) => (
          <span
            key={f.label}
            className={`-ml-2 flex size-[3.25rem] items-center justify-center rounded-full border-[1.5px] border-ink text-[0.72rem] font-semibold first:ml-0 ${f.tone}`}
            style={{ zIndex: fields.length - i }}
          >
            {f.label}
          </span>
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function SectionHead({
  title,
  aside,
  tag,
}: {
  title: React.ReactNode;
  aside?: React.ReactNode;
  tag?: string;
}) {
  return (
    <div className="grid items-end gap-6 md:grid-cols-[minmax(0,1.3fr)_minmax(0,0.7fr)]">
      <h2 className="text-[clamp(2rem,4.4vw,3.4rem)] font-semibold leading-[1.04] tracking-[-0.03em]">{title}</h2>
      {aside ? (
        <div className="md:pb-2">
          {tag ? <span className="pill mb-4 bg-mustard text-xs">{tag}</span> : null}
          <p className="text-ink-soft">{aside}</p>
        </div>
      ) : null}
    </div>
  );
}

function Explainers({ papers }: { papers: Awaited<ReturnType<typeof fetchRecentPapers>> }) {
  return (
    <section id="explainers" className="scroll-mt-6 border-y-[1.5px] border-ink bg-paper">
      <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-10 lg:py-24">
        <SectionHead
          title="Papers already explained"
          tag="Open to everyone"
          aside="Every explainer is public and keeps its link. If someone has explained a paper before, it opens instantly for you."
        />
        <div className="mt-10">
          <Gallery papers={papers} />
        </div>
      </div>
    </section>
  );
}

function HowItWorks() {
  const steps = [
    {
      title: "Paste a link",
      body: "Any arXiv paper with LaTeX source. No account, no upload.",
      tone: "bg-paper",
    },
    {
      title: "We read the source",
      body: "Sections, equations and figures come from the LaTeX itself, so nothing is lost to PDF extraction.",
      tone: "bg-lavender-soft",
    },
    {
      title: "We pick what is hard",
      body: "Ideas are ranked by difficulty and how central they are. Most sections stay as prose, on purpose.",
      tone: "bg-mustard-soft",
    },
    {
      title: "You read and replay",
      body: "The hard parts become animations beside the text. Scrub back to the exact step you missed.",
      tone: "bg-paper",
    },
  ];
  return (
    <section id="how" className="mx-auto max-w-7xl scroll-mt-6 px-4 py-20 sm:px-6 lg:px-10 lg:py-28">
      <SectionHead
        title="From link to explainer in a few minutes"
        aside="The article becomes readable as soon as the paper is analysed. Visuals fill in beside the text as each one is checked."
      />
      <ol className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {steps.map((s, i) => (
          <li key={s.title} className={`relative rounded-[var(--radius-card)] border-[1.5px] border-ink p-6 ${s.tone}`}>
            <span className="flex size-10 items-center justify-center rounded-full border-[1.5px] border-ink bg-paper text-lg font-semibold">
              {i + 1}
            </span>
            <h3 className="mt-6 text-xl font-semibold tracking-tight">{s.title}</h3>
            <p className="mt-2 leading-relaxed text-ink-soft">{s.body}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Visuals() {
  const items = [
    { demo: ATTENTION_DIAGRAM_DEMO, name: "Mechanisms", when: "When an idea is a process, it is drawn as one: the parts appear, data flows between them, and the cells that matter light up." },
    { demo: ATTENTION_DEMO, name: "Derivations", when: "When a paper walks through an equation, each step is written out and transformed into the next." },
    { demo: RESNET_DEMO, name: "Results", when: "When a figure carries the argument, the chart is built up so you see what changed and by how much." },
    { demo: TRANSFORMER_DEMO, name: "Systems", when: "When a model is a pipeline of parts, the blocks appear in order and data is traced through them." },
  ];
  return (
    <section id="visuals" className="scroll-mt-6 border-y-[1.5px] border-ink bg-lavender-soft">
      <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-10 lg:py-28">
        <SectionHead
          title="Four ways an idea can move"
          aside="These run live in your browser, from the same kind of parameters the pipeline writes for a paper. Press play, drag the bar, or slow them down."
        />
        <div className="mt-12 grid gap-6 md:grid-cols-2">
          {items.map(({ demo, name, when }) => (
            <div key={name} className="flex flex-col">
              <h3 className="text-2xl font-semibold tracking-tight">{name}</h3>
              <p className="mb-5 mt-2 leading-relaxed text-ink-soft lg:min-h-[5.25rem]">{when}</p>
              <SceneCard
                archetype={demo.archetype}
                params={demo.params}
                beats={demo.beats}
                claim={demo.claim}
                source={demo.source}
                className="flex-1"
              />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Audience() {
  const people = [
    {
      who: "Researchers",
      what: "Get the core of a paper outside your field before committing an afternoon to the PDF.",
      tone: "bg-paper",
    },
    {
      who: "Students",
      what: "Watch a derivation unfold one step at a time instead of decoding it from a dense page.",
      tone: "bg-mustard",
    },
    {
      who: "Professors",
      what: "Share a readable companion that cites the exact passages it explains, so nothing is paraphrased away.",
      tone: "bg-lavender",
    },
    {
      who: "Educators",
      what: "Pause on the step a textbook skips, scrub back while you talk it through, and enlarge it for the room.",
      tone: "bg-ink text-paper",
    },
  ];
  return (
    <section id="audience" className="mx-auto max-w-7xl scroll-mt-6 px-4 py-20 sm:px-6 lg:px-10 lg:py-28">
      <SectionHead title="Made for people who read papers" />
      <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {people.map((p) => (
          <article key={p.who} className={`rounded-[var(--radius-card)] border-[1.5px] border-ink p-6 ${p.tone}`}>
            <h3 className="text-2xl font-semibold tracking-tight">{p.who}</h3>
            <p className={`mt-3 leading-relaxed ${p.tone.includes("bg-ink") ? "text-paper/85" : "text-ink-soft"}`}>{p.what}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function GoodToKnow() {
  const items = [
    ["arXiv papers with LaTeX source", "PDF-only submissions are turned away with a reason, never half-explained."],
    ["English, under 60 pages", "Longer or non-English papers are declined in the first few seconds."],
    ["A companion, not a replacement", "Every section links back to the original, and figures stay with their authors."],
    ["Free while it lasts", "Explanations run on a free model budget. If today's is spent, try again in a few hours."],
  ];
  return (
    <section className="mx-auto max-w-7xl px-4 pb-20 sm:px-6 lg:px-10 lg:pb-28">
      <div className="grid gap-10 rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-6 sm:p-10 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        <div className="flex items-start gap-5">
          <PaperSheet className="hidden w-28 shrink-0 sm:block" />
          <div>
            <h2 className="text-3xl font-semibold tracking-tight">Good to know</h2>
            <p className="mt-3 text-ink-soft">This is a tool for people who read critically, so here are its limits up front.</p>
          </div>
        </div>
        <dl className="grid gap-6 sm:grid-cols-2">
          {items.map(([h, p]) => (
            <div key={h} className="border-l-[3px] border-mustard pl-4">
              <dt className="font-semibold">{h}</dt>
              <dd className="mt-1 text-sm leading-relaxed text-ink-soft">{p}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

function Closing() {
  return (
    <section className="mx-auto max-w-7xl px-4 pb-20 sm:px-6 lg:px-10 lg:pb-28">
      <div className="relative overflow-hidden rounded-[2rem] border-[1.5px] border-ink bg-mustard px-6 py-12 sm:px-12 sm:py-16">
        <div className="relative z-10 max-w-xl">
          <h2 className="text-[clamp(2rem,4.4vw,3.2rem)] font-semibold leading-[1.04] tracking-[-0.03em]">
            Start with a paper you already know
          </h2>
          <p className="mt-4 text-lg text-ink-soft">
            It is the fastest way to judge whether the explanation is any good, and whether anything was gotten wrong.
          </p>
          <div className="mt-8">
            <SubmitForm examples={false} tone="onColor" />
          </div>
        </div>
        <Blocks className="pointer-events-none absolute -bottom-6 right-4 hidden w-72 md:block lg:right-12 lg:w-80" />
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t-[1.5px] border-ink bg-paper">
      <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-10 sm:px-6 md:flex-row md:items-center md:justify-between lg:px-10">
        <div>
          <Logo />
          <p className="mt-3 max-w-md text-sm text-ink-soft">
            A companion to the papers it reads. Thanks to the authors who share their LaTeX source on arXiv.
          </p>
        </div>
        <nav aria-label="Footer" className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          <a href="/#explainers" className="hover:text-coral">Explainers</a>
          <a href="/#how" className="hover:text-coral">How it works</a>
          <a href="https://arxiv.org" target="_blank" rel="noreferrer" className="hover:text-coral">arXiv</a>
        </nav>
      </div>
    </footer>
  );
}

/**
 * The landing page.
 *
 * A server component: everything except the demo, the diagram and the form is static,
 * so it is in the initial HTML. The three interactive pieces are islands.
 *
 * The design brief is academic rather than SaaS — a serif measure, numbered sections,
 * a running footnote rail, no gradient hero and no testimonial carousel. The argument
 * a research tool has to make is that it is *careful*, and a page that looks like a
 * growth funnel argues the opposite. The one flourish is the worked derivation in the
 * hero, which earns its place by demonstrating the product rather than describing it.
 */

import type { Metadata } from "next";
import PipelineDiagram from "../components/landing/PipelineDiagram";
import Reveal from "../components/landing/Reveal";
import TransformDemo from "../components/landing/TransformDemo";
import SubmitForm from "../components/SubmitForm";

export const metadata: Metadata = {
  title: "ArcVisual — paper in, explained paper out",
  description:
    "Paste an arXiv paper. Get a scrollytelling article where the hard parts are carried by 3Blue1Brown-style animations, every claim grounded in the paper's own text.",
};

export default function Landing() {
  return (
    <main className="mx-auto max-w-6xl px-6 lg:px-10">
      <Nav />
      <Hero />
      <Provenance />
      <Pipeline />
      <Restraint />
      <Archetypes />
      <Honesty />
      <Closing />
      <Footer />
    </main>
  );
}

/* -------------------------------------------------------------------------- */

function Nav() {
  return (
    <nav className="flex items-center justify-between border-b border-border py-5">
      <span className="flex items-baseline gap-2">
        <span className="text-lg font-semibold tracking-tight text-fg">ArcVisual</span>
        <span className="hidden text-xs text-muted sm:inline">
          paper in, explained paper out
        </span>
      </span>
      <span className="flex items-center gap-5 text-sm text-muted">
        <a className="transition-colors hover:text-fg" href="#pipeline">
          How it works
        </a>
        <a className="transition-colors hover:text-fg" href="#grounding">
          Grounding
        </a>
        <a
          className="transition-colors hover:text-fg"
          href="https://arxiv.org"
          target="_blank"
          rel="noreferrer"
        >
          arXiv ↗
        </a>
      </span>
    </nav>
  );
}

function Hero() {
  return (
    <header className="grid items-center gap-12 py-16 lg:grid-cols-[minmax(0,1fr)_minmax(0,30rem)] lg:py-24">
      <div>
        <p className="text-xs uppercase tracking-[0.2em] text-muted">
          Open-access papers · arXiv
        </p>
        <h1 className="mt-5 text-4xl font-semibold leading-[1.1] tracking-tight text-fg sm:text-5xl">
          The hard part of a paper
          <br />
          is usually one idea.
          <span className="block text-muted">We animate that one.</span>
        </h1>

        <p className="prose-arc mt-7 text-lg text-muted">
          Paste an arXiv URL. ArcVisual reads the LaTeX source, works out which ideas are
          genuinely hard, and builds a scrollytelling article where those — and only
          those — are carried by animation. Everything else stays prose, because most of
          a paper does not need a video.
        </p>

        <div className="mt-9 max-w-xl">
          <SubmitForm />
        </div>

        <dl className="mt-12 grid max-w-xl grid-cols-3 gap-6 border-t border-border pt-6">
          <Stat value="8–12" label="animations per paper, capped" />
          <Stat value="~90s" label="until the article is readable" />
          <Stat value="0" label="claims without a source span" />
        </dl>
      </div>

      <Reveal className="lg:pt-6">
        <TransformDemo />
      </Reveal>
    </header>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div>
      <dt className="font-mono text-2xl text-accent">{value}</dt>
      <dd className="mt-1 text-xs leading-snug text-muted">{label}</dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Section({
  n,
  title,
  lead,
  id,
  children,
}: {
  n: string;
  title: string;
  lead?: string;
  id?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-20 border-t border-border py-20">
      <Reveal>
        <p className="font-mono text-xs text-accent">§{n}</p>
        <h2 className="mt-3 text-2xl font-semibold tracking-tight text-fg sm:text-3xl">
          {title}
        </h2>
        {lead ? <p className="prose-arc mt-4 text-muted">{lead}</p> : null}
      </Reveal>
      <div className="mt-12">{children}</div>
    </section>
  );
}

/**
 * Section 1 leads with grounding rather than with features, because it is the
 * difference that actually matters: an explainer that confidently animates a wrong
 * intuition is worse than no explainer.
 */
function Provenance() {
  return (
    <Section
      id="grounding"
      n="1"
      title="Every claim points at a line in the paper"
      lead="The failure mode that would sink a tool like this is a beautiful animation of something the paper never said. So grounding is structural here, not a review step."
    >
      <div className="grid gap-6 md:grid-cols-3">
        {[
          {
            h: "Verbatim spans",
            p: "Each concept and each animation carries a character range into the paper's own source, plus a hash of the quoted text. A proposal we cannot locate is dropped — never paraphrased into place.",
          },
          {
            h: "Drift detection",
            p: "The quote hash means a revised paper cannot silently re-point an existing claim at different text. The article fails loudly instead of quietly becoming wrong.",
          },
          {
            h: "Provenance on the page",
            p: "Every animation shows which section it came from, and flags when our own review scored it marginal. You get to know what was machine-inferred.",
          },
        ].map((card, i) => (
          <Reveal key={card.h} delay={i * 80} className="h-full">
            <article className="h-full rounded-lg border border-border bg-surface/30 p-6">
              <h3 className="font-medium text-fg">{card.h}</h3>
              <p className="mt-3 text-sm leading-relaxed text-muted">{card.p}</p>
            </article>
          </Reveal>
        ))}
      </div>

      <Reveal delay={120}>
        <figure className="mt-8 overflow-hidden rounded-lg border border-border">
          <figcaption className="border-b border-border bg-surface/50 px-4 py-2 font-mono text-xs text-muted">
            storyboard.json — the contract every stage reads and writes
          </figcaption>
          <pre className="scroll-x bg-canvas p-5 font-mono text-xs leading-relaxed text-muted">
            <code>{`"span": {
  "section_id":   "s003",
  "start":        625,
  "end":          705,
  "quote_sha256": "9f2c…"     `}<span className="text-accent">{`// re-ingest mismatch → SourceDriftError`}</span>{`
},
"claim": "The scaling factor keeps the softmax out of its saturated region."
                              `}<span className="text-accent">{`// no claim, no scene — enforced by the schema`}</span></code>
          </pre>
        </figure>
      </Reveal>
    </Section>
  );
}

function Pipeline() {
  return (
    <Section
      id="pipeline"
      n="2"
      title="Five stages, and what each one refuses to do"
      lead="Every stage reads and writes one document. None of them can write another's fields — that rule is a test, not a convention."
    >
      <PipelineDiagram />
    </Section>
  );
}

/**
 * The triage argument. Worth its own section because it is counter-intuitive: the
 * product's quality claim rests on doing *less*.
 */
function Restraint() {
  return (
    <Section
      n="3"
      title="Most sections get no animation at all"
      lead="Ranked by difficulty × centrality, capped at twelve, and never more than two per section. An analysis that wants to animate everything has understood nothing."
    >
      <div className="grid gap-10 lg:grid-cols-2">
        <Reveal>
          <div className="rounded-lg border border-border p-6">
            <p className="text-xs uppercase tracking-widest text-muted">
              A typical 20-page ML paper
            </p>
            <ul className="mt-5 space-y-3 font-mono text-sm">
              {[
                { label: "sections parsed", value: "25", tone: "text-fg" },
                { label: "concepts grounded", value: "22", tone: "text-fg" },
                { label: "animations proposed", value: "18", tone: "text-muted" },
                { label: "survive triage", value: "12", tone: "text-accent" },
              ].map((row) => (
                <li key={row.label} className="flex items-baseline gap-4">
                  <span className={`w-10 text-right tabular-nums ${row.tone}`}>
                    {row.value}
                  </span>
                  <span className="h-px flex-1 bg-border" aria-hidden="true" />
                  <span className="text-xs text-muted">{row.label}</span>
                </li>
              ))}
            </ul>
          </div>
        </Reveal>

        <Reveal delay={100}>
          <blockquote className="prose-arc border-l-2 border-accent/40 pl-6 text-muted">
            <p>
              A section with good prose and no animation is a fine outcome. A section
              with a garbled animation is a failure of the product.
            </p>
            <p className="mt-4">
              So when a scene cannot clear its gates, it degrades — first to a simpler
              version of itself, then to the paper&rsquo;s own figure, then to prose. The
              slot is never left broken, and the article always ships.
            </p>
          </blockquote>
        </Reveal>
      </div>
    </Section>
  );
}

function Archetypes() {
  const rows = [
    {
      name: "transform_chain",
      fires: "An equation derived step by step",
      shows: "What changed between one line and the next",
      live: true,
    },
    {
      name: "plot_reveal",
      fires: "A results curve worth interrogating",
      shows: "The trend building, rather than a finished chart",
      live: true,
    },
    {
      name: "architecture_flow",
      fires: "A model diagram with data moving through it",
      shows: "The order of operations a static figure cannot",
      live: true,
    },
    {
      name: "vector_field",
      fires: "Gradients, flows, dynamics",
      shows: "—",
      live: false,
    },
    {
      name: "attention_matrix",
      fires: "Attention, adjacency, any heatmap over time",
      shows: "—",
      live: false,
    },
    {
      name: "algorithm_trace",
      fires: "A pseudocode block",
      shows: "—",
      live: false,
    },
  ];

  return (
    <Section
      n="4"
      title="A taxonomy, not a prompt"
      lead="“3Blue1Brown-style” is a property of tested templates, not an instruction to a model. Each archetype owns its camera, palette, easing, margins and runtime bound; the model only fills a typed schema."
    >
      <Reveal>
        <div className="scroll-x rounded-lg border border-border">
          <table className="w-full min-w-[38rem] text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-widest text-muted">
                <th className="px-5 py-3 font-normal">Archetype</th>
                <th className="px-5 py-3 font-normal">Fires when</th>
                <th className="px-5 py-3 font-normal">What it shows</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.name}
                  className={`border-b border-border/60 last:border-0 ${
                    row.live ? "" : "opacity-45"
                  }`}
                >
                  <td className="px-5 py-3">
                    <span className="font-mono text-xs text-accent">{row.name}</span>
                    {!row.live ? (
                      <span className="ml-2 rounded border border-border px-1.5 py-0.5 text-[0.65rem] text-muted">
                        planned
                      </span>
                    ) : null}
                  </td>
                  <td className="px-5 py-3 text-muted">{row.fires}</td>
                  <td className="px-5 py-3 text-muted">{row.shows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Reveal>
      <Reveal delay={80}>
        <p className="mt-4 text-xs text-muted">
          Three templates ship today. The library compounds: a custom scene that succeeds
          twice becomes a candidate for promotion into it.
        </p>
      </Reveal>
    </Section>
  );
}

/**
 * Stating the limits on the landing page rather than burying them. For a research
 * audience this is a feature — the fastest way to lose them is an overclaim they can
 * check in thirty seconds.
 */
function Honesty() {
  return (
    <Section
      n="5"
      title="What it does not do yet"
      lead="This is a tool for people who read papers critically. It would be strange to hide the caveats from them."
    >
      <div className="grid gap-x-10 gap-y-6 md:grid-cols-2">
        {[
          ["arXiv only", "Non-arXiv PDFs need the Docling path, which is not built. A PDF-only submission is rejected with a reason, never silently degraded."],
          ["No narration", "Text-first. Captions are read, not heard; audio is a later lever."],
          ["Pre-rendered video", "No live WebGL sandboxes. Animations are MP4s, with scroll-scrubbing only where seeking is genuinely cheap."],
          ["English, under 60 pages", "Rejected at second five with a reason, rather than at minute twelve with a bad article."],
          ["Figures may be linked, not embedded", "arXiv's default licence grants arXiv distribution rights, not ours. Non-permissive papers get a deep link."],
          ["A companion, not a replacement", "Every section links back to the original, and there is a takedown path for authors."],
        ].map(([h, p], i) => (
          <Reveal key={h} delay={i * 60}>
            <div className="border-l border-border pl-5">
              <h3 className="text-sm font-medium text-fg">{h}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{p}</p>
            </div>
          </Reveal>
        ))}
      </div>
    </Section>
  );
}

function Closing() {
  return (
    <section className="border-t border-border py-20">
      <Reveal>
        <div className="rounded-lg border border-border bg-surface/30 p-8 sm:p-12">
          <h2 className="text-2xl font-semibold tracking-tight text-fg">
            Start with a paper you already understand.
          </h2>
          <p className="prose-arc mt-3 text-muted">
            It is the fastest way to judge whether the explanation is any good — and
            whether we got anything wrong.
          </p>
          <div className="mt-8 max-w-xl">
            <SubmitForm compact />
          </div>
        </div>
      </Reveal>
    </section>
  );
}

function Footer() {
  return (
    <footer className="flex flex-col gap-3 border-t border-border py-10 text-xs text-muted sm:flex-row sm:items-center sm:justify-between">
      <p>
        ArcVisual is a companion to the papers it reads. Thanks to the authors who post
        their source.
      </p>
      <p className="font-mono">
        open access only · <a className="hover:text-fg" href="#grounding">grounding</a>
      </p>
    </footer>
  );
}

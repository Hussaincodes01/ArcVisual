# ArcVisual — Master Plan

**Paper in. Explained paper out.**
Paste an arXiv (or any) research paper URL; get back an interactive scrollytelling article where the hard parts are carried by 3Blue1Brown-style Manim animations.

**Version:** 0.1 (planning) · **Status:** pre-build · **Guiding constraint:** assemble from existing tools; write glue, taxonomy, and templates — nothing else.

---

## 1. Scope

### What ArcVisual is
A **pipeline product**, not a model. Five stages — Ingest, Analyze, Generate, Validate, Experience — wired from off-the-shelf parsers, agent frameworks, a mature animation engine, and a standard scrollytelling stack. The proprietary surface is deliberately thin and sits in three places:

1. **The visual-opportunity taxonomy** — which concepts deserve animation, and which archetype fits.
2. **The parameterized Manim scene library** — a constrained DSL the model fills in rather than free-forming code.
3. **The four validation gates** — what "good enough to ship" means, mechanically.

Everything else is procurement.

### Non-goals (v1)
- No PDF/LaTeX authoring, no paper summarization-as-a-service, no chat-with-paper.
- No live/interactive simulations (WebGL sandboxes) — pre-rendered video only.
- No narration audio in v1. Text-first reading experience; TTS is a Phase 4 lever.
- No paywalled-paper retrieval. Open access only.

### Success criteria for v1
| Metric | Target |
|---|---|
| Papers that complete end-to-end without human intervention | ≥ 85% |
| Animations passing all four gates on first generation | ≥ 60% |
| Animations passing after ≤ 3 repair attempts | ≥ 90% |
| Wall-clock, submit → readable article | ≤ 12 min p50, ≤ 25 min p95 |
| Marginal cost per paper | ≤ $4 |
| Reader rating "this made the paper clearer" | ≥ 70% |

---

## 2. Product shape

ArcVisual is **asynchronous by design**. Rendering a dozen Manim scenes takes minutes; pretending otherwise produces a bad product. The UX must own this:

```
Paste URL
   ↓  (< 10s)  Paper identified, sections extracted, outline shown to reader
   ↓  (< 90s)  Full text article readable — figures, math, prose. Animation slots marked "rendering".
   ↓  (2-12m)  Animations stream in and fill their slots progressively
   ↓           Article is permalinked, cached, shareable
```

The reader gets something useful at 90 seconds and something great at 10 minutes. Never a spinner over an empty page.

---

## 3. Architecture at a glance

```
 ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐
 │ INGEST  │──▶│ ANALYZE  │──▶│ GENERATE │──▶│ VALIDATE │──▶│ EXPERIENCE │
 └─────────┘   └──────────┘   └──────────┘   └────┬─────┘   └────────────┘
  resolve URL   section agents  template fill   ▲    │ fail      Next.js +
  LaTeX/HTML/   concept graph   Manim codegen   │    ▼           Scrollama
  PDF → DocTree visual triage   render on Modal └─repair loop    on Vercel
       │             │               │           (max 3, then degrade)
       └─────────────┴───────────────┴──────────────┬──────────────┘
                                                    ▼
                    Postgres (job + artifact state) · R2 (video/frames) · content-hash cache
```

**Core data contract.** Every stage reads and writes one document: the **Storyboard JSON**. Ingest produces its skeleton, Analyze annotates it, Generate/Validate attach media, Experience renders it. Stages never talk to each other directly. This is the single most important structural decision in the plan — it makes each stage independently testable, cacheable, and replaceable.

---

## 4. Build vs. buy ledger

The constraint is "use what exists." Here is the explicit accounting.

| Concern | Use | Why this one | Rejected |
|---|---|---|---|
| arXiv metadata | arXiv API + `arxiv` pypi pkg | Canonical, free, includes license field | Scraping |
| arXiv full text | Native arXiv HTML → **ar5iv** → LaTeX source tarball | Semantic sections + real equations, no OCR guessing | PDF-first parsing |
| General PDF → structure | **Docling** (IBM), **Marker** as second opinion | Layout-aware, formula + table aware, actively maintained | Nougat (stale), raw PyMuPDF |
| Scholarly metadata / OA resolution | **OpenAlex**, **Unpaywall** | Free, no key, covers non-arXiv | Semantic Scholar (rate limits) |
| Figure extraction | Docling figure blocks; **PDFFigures 2.0** fallback | Already solved | Custom CV |
| Agent orchestration | **LangGraph** (graph + checkpointing) | Retry loops and resumable state are native; the pipeline *is* a graph | Rolling our own state machine |
| Reasoning / codegen models | **Claude** (Sonnet for classify, Opus for codegen) w/ prompt caching | Paper body cached once, reused across every section agent | — |
| Structured output | Pydantic + native tool-use schemas | Storyboard JSON must be typed | Regex on prose |
| Animation engine | **Manim Community Edition** | Docs, plugins, headless render, stable API | manimgl (3b1b's own — unstable API) |
| Domain animation | `manim-ml`, `manim-physics`, `manim-chemistry`, `manim-dsa` | Neural nets, vectors, molecules already implemented | Hand-built mobjects |
| Render environment | `manimcommunity/manim` Docker image + TinyTeX | Reproducible; LaTeX preinstalled | Bespoke image |
| Compute / sandbox / queue | **Modal** | Python-native, `.spawn()` for burst fan-out, volumes, sandboxes — collapses four services into one | Celery+Redis+K8s+E2B stack |
| Job state | **Postgres** (Neon or Supabase) | Boring, correct | — |
| Object storage | **Cloudflare R2** + CDN | Zero egress fees; video-heavy workload | S3 (egress cost) |
| Static analysis | `ruff`, Python `ast`, custom API allowlist | Catches ~40% of codegen failures before spending a render | — |
| Video processing | `ffmpeg` | Keyframe sampling, contact sheets, transcode | — |
| Scrollytelling | **Scrollama** (IntersectionObserver) | The de facto standard; ~2KB | GSAP ScrollTrigger (heavier, licensing) |
| Frontend | **Next.js** + MDX + Tailwind, deployed on **Vercel** | Streaming/partial rendering fits progressive fill | — |
| Math typesetting | **KaTeX** | Fast, synchronous, no layout thrash while scrolling | MathJax |
| Observability | **Langfuse** or LangSmith + Sentry | Per-stage traces, token cost attribution | — |

**Total net-new code:** the Storyboard schema, the taxonomy, ~15 scene templates, the four gates, and the reader UI. Everything else is configuration.

---

## 5. Stage 1 — Ingest

**Goal:** any paper URL → a typed `DocTree` with clean sections, real LaTeX equations, extracted figures, and license metadata.

**Resolution ladder** (stop at first success):

1. `arxiv.org/abs/XXXX` → arXiv API for metadata + **LaTeX e-print source**. Best case: real `\section`, `\begin{equation}`, `\label`/`\ref` graph, TikZ sources.
2. arXiv native HTML (`/html/`) or **ar5iv** → LaTeXML output with MathML. Nearly as good.
3. Any PDF → **Docling** → structured doc. Cross-check section boundaries against **Marker** when confidence is low.
4. DOI / title only → **OpenAlex** → Unpaywall → OA PDF → step 3.

**Decomposition.** Use the document's own hierarchy. Never naive text-splitting. Each `Section` carries: id, heading path, prose, equation blocks (LaTeX verbatim), figure refs, and **character offsets back into the source** — the grounding anchor everything downstream depends on.

**Gate at ingest.** Reject and explain, don't silently degrade: paywalled, scanned-without-OCR, non-English (v1), > 60 pages, or a license that forbids derivative display. Fail loud at second 5, not minute 12.

**Legal note.** Read the arXiv `license` field. Perpetual-non-exclusive papers do **not** grant redistribution of figures — for those, deep-link to the original figure rather than rehosting. CC-BY/CC-BY-SA papers may be embedded with attribution. Quote paper text sparingly; the article should paraphrase and explain, not reproduce.

---

## 6. Stage 2 — Analyze

A LangGraph fan-out: one **Section Agent** per section, running in parallel over a prompt-cached paper body.

Each agent emits, per section:
- **Concepts** — atomic ideas, each grounded to a source span.
- **Dependencies** — a DAG across sections. Drives reading order and prevents animating an idea before its prerequisite.
- **Difficulty score** — 1–5. Drives how much explanation budget the section gets.
- **Visual opportunities** — zero or more, each tagged with an archetype and an argued justification.

**The visual-opportunity taxonomy.** This is the product's judgment, encoded. A first cut of archetypes:

| Archetype | Fires when | Manim primitive |
|---|---|---|
| `transform_chain` | Equation derived step by step | `TransformMatchingTex` |
| `vector_field` | Gradients, flows, dynamics | `ArrowVectorField`, `StreamLines` |
| `geometric_intuition` | Claim about distance, projection, manifolds | `Axes`, `Surface`, `DashedLine` |
| `architecture_flow` | Model/system diagram with data movement | `manim-ml`, `Arrow`, staged `FadeIn` |
| `attention_matrix` | Attention, adjacency, any heatmap-over-time | `Matrix` + color interpolation |
| `algorithm_trace` | Pseudocode block | `manim-dsa`, indexed highlighting |
| `plot_reveal` | Results table or curve worth interrogating | `Axes` + progressive `Create` |
| `counterexample` | "Naive approach fails because…" | side-by-side split screen |
| `scale_comparison` | Orders of magnitude | animated bar/area morph |
| `state_machine` | Protocol, training loop, sampling procedure | `Graph` + traversal highlight |

**Triage discipline.** Cap at **8–12 animations per paper**. More is worse. Rank opportunities by `difficulty × centrality`, keep the top slice, and let the rest be prose. An agent that wants to animate everything has understood nothing.

**Hard rule: no ungrounded claims.** Every concept and every animation spec must reference a source span. If a model wants to assert something the paper doesn't say, that assertion is dropped. The single largest reputational risk in this product is a beautiful animation of a wrong intuition.

**Output:** the annotated Storyboard JSON.

---

## 7. Stage 3 — Generate

**The central decision: templates, not free-form codegen.**

Asking a model to write arbitrary Manim produces a punishing failure rate — hallucinated methods, silent off-frame drift, unbounded runtimes. Instead ship a library of ~15 **parameterized scene templates**, one per archetype. Each is real, tested, hand-written Manim CE with a typed Pydantic parameter schema. The model's job shrinks from "write an animation" to "fill in this schema."

```
Model chooses:  archetype + parameters + narration beats + timing
Template owns:  camera, layout grid, palette, easing, safe margins, runtime bound
```

Consequences: first-pass validation rates rise dramatically, style stays consistent across every paper, and repair becomes parameter adjustment rather than code rewriting.

**Escape hatch.** Allow a `custom_scene` path for the 10–15% of ideas no template covers — but route it through the same four gates with a stricter threshold and a tighter retry budget. Every custom scene that succeeds twice is a candidate for promotion into the template library. The library compounds.

**Rendering.** Modal function, one scene per invocation, fanned out with `.spawn()`. Container = `manimcommunity/manim` + plugins + TinyTeX. Draft pass at `-ql` (480p15) for validation; final at `-qh` (1080p60) only after gates pass. Never spend full-quality render time on code that hasn't been proven.

**Outputs per scene:** MP4 (H.264, `-g 1` all-keyframe for scrub-ability), WebM/VP9 fallback, poster frame, a sampled frame strip for Gate 4, and the render log.

**Caching.** Content-address every artifact on `sha256(template_id, params, manim_version, plugin_versions)`. Re-running a paper after a prompt tweak should re-render only what actually changed. This is the difference between a $4 paper and a $40 paper.

---

## 8. Stage 4 — Validate

Four gates, ordered cheapest-first. A scene must clear all four.

### Gate 1 — Syntactic & contract (static, ~50ms)
- `ast.parse` — does it parse.
- `ruff` — lint, unused imports, undefined names.
- **API allowlist check** — walk the AST and confirm every Manim attribute accessed exists in the pinned version's surface. This is the highest-leverage check in the whole system; hallucinated methods are the #1 codegen failure and they cost nothing to catch here.
- Parameter schema validation against the template's Pydantic model.
- Forbidden constructs: network calls, filesystem writes outside tmp, `while True`, unbounded recursion.

### Gate 2 — Runtime & resource stability (sandboxed draft render, ~20–60s)
- Render at `-ql` inside a Modal sandbox with hard caps: 120s wall clock, 2GB memory, no network.
- Non-zero exit, timeout, or OOM → fail with the traceback attached for repair.
- Scrape logs for LaTeX compilation errors (missing packages, bad math mode) and Manim deprecation warnings.
- Assert produced duration is within ±20% of the specified beat timing.

### Gate 3 — Spatial coherence (geometry + pixels, ~5s)
Instrument the render with a `Scene` subclass that records every mobject's bounding box at each `play()` boundary. Then assert:
- **Frame containment** — no mobject bbox exceeds `frame_width`/`frame_height` minus a 0.3-unit safe margin.
- **Text legibility** — every `Text`/`MathTex` renders at ≥ 18px equivalent at 1080p after all scaling.
- **Unintended overlap** — IoU between any two text mobjects visible in the same frame stays below threshold, unless the template explicitly declares the overlap intentional.
- **Contrast** — sample rendered pixels; foreground/background contrast ≥ 4.5:1.
- **Dead air** — no interval > 2.5s with zero visual change (a common symptom of a broken animation that "succeeded").

### Gate 4 — Semantic fidelity (VLM review, ~10s)
- `ffmpeg` extracts 6–9 keyframes into a contact sheet.
- Send the sheet + the concept description + the grounded source span to a vision model against a fixed rubric:
  1. Does the visual depict the stated concept? (blocking)
  2. Is anything shown that the paper does not claim? (blocking)
  3. Is all text legible and correctly typeset? (blocking)
  4. Does it read as clear, uncluttered, deliberate? (score 1–5, threshold 3)
- Any blocking failure → repair. Score below threshold → repair once, then accept with a lower placement priority.

### Repair loop and graceful degradation
```
attempt 1 → fail → feed gate output + traceback back to generator → attempt 2
attempt 2 → fail → drop to a simpler template for the same concept  → attempt 3
attempt 3 → fail → degrade: render the paper's own figure with an animated
                   annotation overlay; if that fails, ship prose only.
```
**Never ship a broken slot.** A section with good prose and no animation is a fine outcome. A section with a garbled animation is a failure of the product.

---

## 9. Stage 5 — Experience

**Stack:** Next.js (App Router) + MDX + Tailwind + KaTeX + Scrollama, on Vercel. Media from R2 via CDN.

**Layout.** Two-column on desktop: prose in a readable measure (~65ch) on the left, a sticky visual stage on the right. Single-column stacked on mobile, with animations inline at full width.

**Playback strategy — pick the robust default.**
- **Default: autoplay-on-enter.** IntersectionObserver triggers `play()` when a scene enters the stage; `muted playsinline loop`. Reliable everywhere, no jank.
- **Enhancement: scroll-scrubbing** for a subset of scenes flagged `scrubbable` in the storyboard. Requires the all-keyframe encode from Stage 3 to make `currentTime` seeking cheap. Feature-detect and fall back to autoplay on Safari/mobile where scrubbing is unreliable.
- Every scene gets manual controls: replay, pause, speed. Respect `prefers-reduced-motion` by showing the poster frame with a play affordance.

**Reading affordances.** Sticky section outline with progress. Hover cards for citations (from the reference graph). Equations from the paper rendered inline via KaTeX with a "jump to the animation of this" link. A visible **"View in original paper"** anchor on every section — ArcVisual is a companion, not a replacement, and should say so.

**Progressive fill.** The page is served as soon as prose is ready. Animation slots render as poster placeholders and hydrate over WebSocket/SSE as renders land.

**Trust surface.** Every animation carries a small provenance chip: which section it came from, and a badge if it passed Gate 4 with a marginal score. Readers deserve to know what was machine-inferred.

---

## 10. Cross-cutting concerns

**Cost & latency budget (20-page ML paper, ~10 animations):**

| Line item | Est. |
|---|---|
| Ingest (Docling on CPU) | ~$0.01 · 30s |
| Analyze (cached body, ~20 section agents) | ~$1.20 · 60s parallel |
| Generate (codegen for 10 scenes) | ~$1.00 · 90s parallel |
| Render (draft + final, Modal CPU) | ~$0.15 · 4–8 min with fan-out |
| Validate (Gate 4 VLM ×10, plus repairs) | ~$0.40 · 60s |
| Storage + CDN | ~$0.02 |
| **Total** | **~$2.80 · 8–11 min** |

Repairs are the variance driver. Every point of first-pass gate success is real money — which is the economic argument for the template library.

**Observability.** Langfuse traces per stage with token attribution. Sentry for render crashes. A dashboard tracking, per archetype: first-pass gate rate, mean repairs, mean render seconds. That table tells you which templates to fix next.

**Evaluation set.** Curate 30 papers spanning ML, physics, biology, theory, and systems. Freeze it. Every pipeline change runs against it before merge. Without this, "improvements" are vibes.

**Idempotency.** Same URL + same pipeline version = same article, served from cache. Bump the version, invalidate selectively by content hash.

---

## 11. The style contract

"3Blue1Brown-style" is not a prompt instruction — it's a property of the templates. Encode it once, in code:

- **Dark canvas** (`#0E1116`), high-contrast foreground. Manim's default palette already descends from 3b1b's; keep it.
- **Progressive disclosure.** One idea enters at a time. Nothing appears fully formed.
- **Transform over cut.** `TransformMatchingTex` between equation states, never a hard swap.
- **Deliberate pacing.** Minimum 0.8s per beat; hold 1.5s after a reveal before moving on.
- **Concrete before abstract.** Show the n=2 case, then generalize.
- **The visual carries an argument.** Every scene must have a claim it is making, stated in the storyboard. Decorative animation is banned at the schema level — no `claim`, no scene.
- **Restraint.** Empty space is a design element. Templates enforce margins that cannot be overridden.

---

## 12. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Confidently wrong animation** | Critical | Grounding requirement, Gate 4 blocking checks, provenance chips, conservative triage. This is the risk that kills the product's credibility. |
| Codegen failure rate makes unit economics fail | High | Template library; measure first-pass rate per archetype as a core metric |
| Figure/text redistribution beyond license | High | Parse license at ingest; deep-link rather than rehost for non-permissive; paraphrase, don't quote at length |
| Manim CE version drift breaking templates | Medium | Pin exact version + plugin versions in the image; templates are covered by a render-snapshot test suite |
| Long-tail parse failures on non-arXiv PDFs | Medium | Resolution ladder + explicit "we couldn't read this" failure, never a degraded silent output |
| Render cost spike on a viral paper | Medium | Per-user rate limits, aggressive content-hash cache, queue depth caps |
| Scroll-scrubbing jank tanks the reading experience | Medium | Autoplay-on-enter is the default; scrubbing is opt-in and feature-detected |
| Author objection to a misrepresenting article | Medium | Prominent "companion, not replacement" framing, link-out on every section, a takedown path |

---

## 13. Roadmap

### Phase 0 — Spike (1 week)
One hardcoded paper (pick *Attention Is All You Need* — well-understood, visually rich). Hand-written storyboard JSON, three hand-written Manim scenes, a static Next.js page with Scrollama.
**Exit:** the reading experience feels good enough to justify building the machine behind it. If it doesn't, stop here.

### Phase 1 — Vertical slice (3 weeks)
arXiv-only ingest via LaTeX source. Single-pass analysis, no agent graph. Three templates (`transform_chain`, `plot_reveal`, `architecture_flow`). Gates 1 and 2 only. Manual trigger.
**Exit:** 5 papers end-to-end without touching the code between runs.

### Phase 2 — The machine (4 weeks)
LangGraph agent graph with per-section fan-out and prompt caching. Full taxonomy and triage. Gates 3 and 4 plus the repair loop. Modal fan-out rendering. Postgres job state. Progressive fill in the reader.
**Exit:** ≥ 60% first-pass gate rate on the 30-paper eval set; p50 under 12 minutes.

### Phase 3 — Breadth (4 weeks)
Template library to 15. Non-arXiv ingest via Docling + OpenAlex. Content-hash caching. Custom-scene escape hatch. Observability dashboard. Permalinks and sharing.
**Exit:** eval set expands to 60 papers across five fields with ≥ 85% completion.

### Phase 4 — Polish (ongoing)
`manim-voiceover` narration. Scroll-scrubbing for flagged scenes. Reader accounts and libraries. Author feedback loop. Template promotion pipeline from successful custom scenes.

---

## 14. Decisions to make before Phase 1

1. **Compute plane** — Modal (recommended, collapses queue + sandbox + render) vs. Temporal + K8s. Affects everything downstream; decide first.
2. **Custom scenes in v1?** Recommendation: no. Templates only until the gate metrics are trustworthy.
3. **Public gallery or private links?** Changes the licensing posture materially.
4. **Animation cap** — is 8–12 right? Test against reader feedback in Phase 0.
5. **Who owns the taxonomy?** It needs a human editor with real domain taste. This is not a role to backfill later.

# ArcVisual

**Paper in. Explained paper out.**

Paste an arXiv URL; get an interactive scrollytelling article where the hard parts
are carried by animations, and every claim points back to the paper's own text.

**Live:** https://arcvisual.vercel.app · API: https://arcvisual-api.vercel.app/api/health

Two render modes share one pipeline:

- **client** (the deployed default) — the pipeline ships each scene's validated
  template parameters and the reader animates them in the browser. No Manim, no
  ffmpeg, no object storage, so the whole product runs on serverless functions and a
  free Postgres. Scenes are scrubbable, crisp at any size, and cost nothing to render.
- **manim** — the original path: each scene rendered to MP4 by Manim (locally, or in
  a Modal sandbox) and served from R2.

Design docs: [ArcVisual-Master-Plan.md](ArcVisual-Master-Plan.md) (what and why) ·
[ArcVisual-Architecture.md](ArcVisual-Architecture.md) (how it is wired).
This README is what is actually built.

---

## What Phase 1 does

Scope is the plan's vertical slice: **arXiv-only ingest via LaTeX source, single-pass
analysis, three templates, Gates 1 and 2, manual trigger.**

```
arXiv URL ──▶ INGEST ──▶ ANALYZE ──▶ GENERATE ──▶ VALIDATE ──▶ Storyboard JSON
              LaTeX      concepts    template     Gate 1        │
              sections   + triage    params       Gate 2        ▼
              equations                                    Next.js reader
```

Every stage reads and writes one document — the **Storyboard** — and may only add
fields it owns. That rule is enforced by `assert_monotonic`, not by convention.

### Current state

| Piece | Status |
|---|---|
| Ingest (arXiv → LaTeX → sections, equations, figures, licence) | working, 6-paper eval set |
| Analyze (concepts, difficulty, visual opportunities, triage) | working; single-pass, heuristic fallback |
| Providers (Anthropic · Poolside · opencode · heuristic) | working; selection, capabilities, cost attribution |
| Templates (`transform_chain`, `plot_reveal`, `architecture_flow`) | 2 of 3 render and pass Gate 2; `transform_chain` needs LaTeX |
| Generate (schema filling → thin module) | working |
| Gate 1 (parse, allowlist, security, params, lint) | working, ~100ms |
| Gate 2 (sandboxed draft render) | working locally against real Manim 0.18.1 |
| Gate 3 (spatial coherence) | **working**; containment, legibility, overlap, dead air, contrast |
| Gate 4 (VLM semantic review) | not built (Phase 2) |
| Repair ladder + budgets | working |
| Persistence + metrics view | working; Postgres (Neon) in production |
| Serverless deploy (Vercel API + Neon + Groq, stepped jobs) | **live** |
| Browser scene renderer (all three templates) | **live**; mirrors each template's timeline |
| Reader — landing, waiting room, article | **live**, redesigned; renders real articles |
| Modal deploy, R2 upload | written, undeployed (the optional video path) |

**What has been verified.** Real papers run end to end in production: the
Transformer paper produced 10 visuals and ResNet 7, every one passing Gate 1 on its
first or second attempt, in about four minutes each on Groq's free tier. The reader
has been exercised in a real browser at desktop and phone widths. In the Manim path,
`architecture_flow` and `plot_reveal` render real MP4s through Gate 2 on Manim CE
0.18.1. Nothing has run inside a Modal sandbox.

---

## Running it

```bash
pip install -e ".[dev]"

pytest                                  # 322 tests, no network, no key, no Manim
python -m eval.run --no-gate2           # the 6-paper eval set against real arXiv
ruff check arcvisual eval tests
```

Nothing above needs an API key or a database. With `ANTHROPIC_API_KEY` unset the
pipeline uses a deterministic heuristic analyzer; the eval report labels this
explicitly, because **a heuristic baseline is not a model baseline.**

### Rendering for real

```bash
pip install -e ".[render]"               # Manim CE 0.18.1 + plugins
pytest tests/test_render.py -m slow      # renders real video; ~90s per template
python -m eval.run --only attention      # now with Gate 2
```

`transform_chain` additionally needs a LaTeX toolchain (`texlive texlive-latex-extra
dvisvgm`) and skips without one. The render tests fall back to the `imageio-ffmpeg`
static binary when the system has no ffmpeg.

Or on Modal, which is the production path and the only one that renders generated
code inside a real sandbox:

```bash
pip install -e ".[compute]"
modal deploy arcvisual/render/modal_app.py
```

### The control plane, locally

```bash
python -m arcvisual.render.serve            # :8000, SQLite, renders on
python -m arcvisual.render.serve --no-gate2 # skip rendering, fastest loop
python -m arcvisual.render.serve --stepped  # mirror production: advance-driven jobs,
                                            # scenes animated in the browser
```

In production the API is a Modal ASGI app; this serves the same `build_api()` under
uvicorn with jobs run in threads instead of Modal containers. It defaults to SQLite
when `DATABASE_URL` is unset — the ORM already carries `GUID` and `JSON` variants for
exactly that — so it needs no configuration at all. Without it the reader posts to
`localhost:8000`, nothing answers, and the browser reports "Failed to fetch".

### The reader

```bash
cd reader && npm install
NEXT_PUBLIC_ARCVISUAL_API_BASE=http://localhost:8000 npm run dev
```

| Route | What it is | Rendering |
|---|---|---|
| `/` | Landing: a live scene in the hero, the gallery of explained papers, the three visual kinds running live. | static, revalidated every 2 min |
| `/watch/[jobId]` | The waiting room. Drives the job, names the stages, lists visuals as they land, opens the article as soon as its text is ready. | client |
| `/p/[slug]` | The article. Prose in the initial HTML (inline math typeset), sticky stage of live scenes, fills in place while a job is still running. | server + ISR |

The scene engine lives in `reader/lib/scenes` (plans: parameters → timeline, no DOM)
and `reader/components/scenes` (renderers: a pure function of time). Each plan mirrors
its Python template's `estimate_duration`, which is what keeps the captions — fitted
by the pipeline to the template's runtime — in step with the picture. Scenes autoplay
when scrolled into view, can be scrubbed, slowed or enlarged, and under
`prefers-reduced-motion` show their final frame with an explicit play button.

---

## Deployment

Two Vercel projects from this repository, plus a free Neon Postgres:

```
browser ──▶ arcvisual.vercel.app        (reader/, Next.js; holds no credentials)
   │
   └──────▶ arcvisual-api.vercel.app    (repo root, api/index.py → FastAPI)
                 │            │
                 ▼            ▼
             Neon Postgres   Groq (free tier: qwen3.8 analyze, gpt-oss-120b codegen)
```

**Stepped execution.** A serverless function cannot run a multi-minute job in one
go, so `arcvisual/render/stepper.py` advances a job in bounded steps:
`POST /api/jobs/{id}/advance` takes a database lease on the job, runs ingest, analyze
or a batch of scenes for up to `ARCVISUAL_STEP_BUDGET_S`, saves everything to the
job's storyboard, and returns. The waiting room and the article page call it back to
back; any number of tabs can, because the lease turns all but one away. Finished work
is never repeated, a step killed by the platform simply lets its lease lapse, and a
daily cron (`/api/cron/sweep`) moves along any job whose readers left.

**Redeploy** after a change:

```bash
vercel deploy --prod --scope jiyad2332            # API, from the repo root
cd reader && vercel deploy --prod --scope jiyad2332   # reader
```

The API migrates its own schema on the first cold start after a deploy
(`arcvisual/db/migrate.py`, under a Postgres advisory lock), so there is no separate
migration step. Bump `PIPELINE_VERSION` in `arcvisual/config.py` when pipeline output
changes: cached articles are keyed on it and will be rebuilt.

**API environment** (Vercel → arcvisual-api → Settings → Environment Variables):

| Variable | Value |
|---|---|
| `DATABASE_URL` | set by the Neon integration |
| `GROQ_API_KEY` | free key from console.groq.com |
| `ARCVISUAL_PROVIDER` | `groq` |
| `IP_HASH_SALT`, `CRON_SECRET` | long random strings |
| `ARCVISUAL_ALLOWED_ORIGINS` | the reader's URLs, comma-separated |
| `ARCVISUAL_ALLOWED_ORIGIN_REGEX` | the reader's preview URLs |
| `ARCVISUAL_TRUSTED_PROXY_HOPS` | `1` (Vercel sets `x-forwarded-for`) |
| `ARCVISUAL_STEP_BUDGET_S` | `170` (the function limit is 300 s on Hobby) |
| `ARCVISUAL_SCENE_CONCURRENCY` | `2` |
| `RATE_LIMIT_PER_HOUR` | new papers per visitor per hour; re-opening explained papers is free |

**Reader environment:** `NEXT_PUBLIC_ARCVISUAL_API_BASE` and `ARCVISUAL_API_BASE`
(the API URL), `NEXT_PUBLIC_SITE_URL`.

**Cost.** Vercel Hobby, Neon free and Groq's free tier: $0 per month. The binding
limit is Groq's daily token allowance, roughly 15–25 new papers a day per key;
explained papers are cached forever and cost nothing to re-open. Switching
`ARCVISUAL_PROVIDER=anthropic` (with credit on the key) buys deeper analysis — the whole
paper in one pass, with prompt caching.

### Database

```bash
export DATABASE_URL=postgresql+psycopg://...
alembic upgrade head
```

---

## Gate 3 — what "rendered successfully" does not prove

Gate 2 proves a render terminated. Gate 3 proves it produced something a person can
read, from the bbox trace `ArcSceneMixin` records at every `play()` boundary.

| Check | Blocks? | Catches |
|---|---|---|
| Frame containment | yes | Manim animates a mobject past the camera edge and exits zero |
| Text legibility | yes | a label at 9px — present, and practically absent |
| Dead air | yes | a scene that plays 14s and moves for 1 — a silently failed animation |
| Overlap (text IoU) | advisory | two stacked captions, unless the template declared it |
| Contrast | advisory | uniformly dim output, measured from real pixels |

**Blocking versus advisory** is a cost decision, not a confidence one. The three
blocking checks make a scene unreadable, so they are worth a repair attempt. Overlap
and contrast have real false positives — a deliberate annotation beside a curve, a
legitimately dim gridline — and spending an attempt on either would buy nothing. They
are recorded and surfaced so the per-archetype dashboard sees them.

Two measurement details that were wrong before they were measured:

- **Contrast is sampled inside text regions, not over the whole frame.**
  `architecture_flow` scored 3.6:1 whole-frame while its labels were `#E6EDF3` at
  roughly 12:1 — large 6%-opacity box fills dragged the median down. Restricted to
  where text actually is, it reads 5.7:1 and the false positive is gone.
- **Foreground is thresholded against the frame's own peak.** Content covers under 1%
  of pixels on this canvas, so a percentile rule reads the entire frame as background
  and reports nothing at all.

Verified against real renders, including a deliberately broken one whose labels were
shrunk to 6px: Gate 2 exits zero for it, Gate 3 fails it.

---

## Known failure modes, and what they taught

Every entry here was found by running the pipeline, not by reading it. Each passed the
test suite at the time.

| Symptom | Root cause | Rule it produced |
|---|---|---|
| Scenes pass, players are empty | the renderer deleted the video with its workdir | assert on **effects** (`bytes > 0`), never on status fields |
| Analyze dies on a whole paper | `send()` raised outside the retry loop | a retry budget must cover the failure it exists for |
| Every passing scene crashed the job | a 5-tuple return with one arm returning 4 | tests must exercise the *happy* path, not just the guarded ones |
| Correct scenes failed Gate 3 | animation spans counted as dead air | measure the gap between beats, not the beat |
| A whole analysis discarded | claims 12 chars over a cosmetic cap | **structural constraints reject, cosmetic ones coerce** |
| Model invented 7 archetypes | the taxonomy was prose, not schema | a closed set belongs in the JSON Schema `enum` |
| 63 renders burned on a missing toolchain | environment failure treated as scene failure | no parameter change installs LaTeX — fail fast, degrade |
| laguna returns `content: null` | reasoning counts against `max_tokens` (cap 32768) | trim the prompt; the budget cannot be raised |

The through-line: a constraint the caller violates *identically every time* is a
constraint that is wrong, and a status field is not evidence.

---

## Model providers

One env var picks the backend. They are **not** equivalent, and the differences are
surfaced rather than smoothed over — a cost figure is meaningless without them.

| Provider | Structured output | Prompt cache | Cost attribution | Keys |
|---|---|---|---|---|
| `anthropic` | native, constrained | yes, 1h TTL | exact, per-token | 1 |
| `poolside` | prompted + validated | no | needs your contract rates | 1 |
| `opencode` | prompted + validated | no | per-step, from its stream | 0 |
| `heuristic` | n/a | n/a | genuinely free | 0 |

```bash
ARCVISUAL_PROVIDER=anthropic   ANTHROPIC_API_KEY=sk-...
ARCVISUAL_PROVIDER=poolside    POOLSIDE_API_KEY=...
ARCVISUAL_PROVIDER=opencode    # opencode auth login — it holds its own credentials
ARCVISUAL_PROVIDER=heuristic   # no model at all; the CI default
```

Three decisions worth knowing:

- **`opencode` is never auto-selected.** It is an agent CLI, not an inference API, so
  choosing it delegates the model choice to your opencode config and its billing goes
  against *its* credentials. Merely having the binary on PATH must not silently route a
  pipeline through it, so it requires an explicit `ARCVISUAL_PROVIDER=opencode`.
- **An unpriced call is reported as unpriced, never as $0.00.** Poolside publishes no
  rates we could verify, so its cost comes back `attributed=false` and the job summary
  prints `$0.0000+unpriced`. Set `POOLSIDE_RATE_IN`/`OUT` from your own contract to turn
  attribution on. The plan's unit-economics argument depends on that number being real.
  opencode is the opposite case: it reports `tokens` and `cost` per step in its event
  stream, so its spend *is* attributed — and a reported `cost: 0` on a free model is a
  real zero, not an unknown.
- **Only Anthropic gets constrained decoding.** The others go through a schema-in-prompt
  path with tolerant JSON extraction and exactly one reprompt on a validation failure —
  `repair.py` already owns a budgeted ladder above it, and a second unbounded loop inside
  would make the per-scene cost ceiling unenforceable.

`ANTHROPIC_BASE_URL` routes the Anthropic provider through any compatible gateway
(Dedalus Labs, a proxy). Same SDK, same model ids — a setting, not a fourth provider.

### Operational notes, measured rather than assumed

| Provider | Finding |
|---|---|
| Poolside | `laguna-s-2.1` counts **reasoning** tokens against `max_tokens`. At 8192 it spent the whole budget thinking and returned `content: null` with `finish_reason: "length"` — an HTTP 200 with no answer. `POOLSIDE_MAX_TOKENS` defaults to 32000 for this reason. One Analyze call on a 25-section paper took **478s**. |
| opencode | Each call carries ~28k input tokens of opencode's own agent prompt and tool definitions before your content. API failures (e.g. a 401) arrive as an `error` **event with exit code 0**, so the stream must be inspected — the return code alone reports success. On Windows the entry point is a `.CMD` shim, so the binary is resolved with `shutil.which` before spawning. |
| opencode models | `opencode models` lists what your auth actually reaches. On this account `opencode/deepseek-v4-flash` returns *Insufficient balance*; `opencode/deepseek-v4-flash-free` works. |

`GET /api/providers` reports what this deployment selected and what it gives up.

---

## Layout

```
arcvisual/
  storyboard.py          the contract; read this first
  config.py              budgets and ceilings, enforced in code
  ingest/                arXiv → LaTeX → sections with round-trippable offsets
  analyze/               concepts, opportunities, triage, prompts
  templates/             base.py (the keystone) + three templates + registry
  generate/              codegen.py (schema filling) + repair.py (the ladder)
  gates/                 allowlist.py, g1_static.py, g2_runtime.py
  cache/hashing.py       content addressing
  db/                    models, repo, Alembic migrations
  render/                pipeline.py, modal_app.py, api.py, storage.py
eval/                    the frozen paper set and the exit test
reader/                  Next.js article renderer
```

---

## Six decisions worth knowing before you change anything

**1. Templates are functions, not classes; Manim is imported lazily.**
A template exports a Pydantic `Params` model and `build(scene, params)`. The real
Manim subclass is assembled at render time by `make_scene`. That keeps the schema,
the registry and Gate 1 importable with no renderer installed — which is why the
control plane runs in a small container and CI runs at all.

**2. Templates must render through `ArcSceneMixin`, never bare `manim.Scene`.**
Gate 3 asserts frame containment, legibility, overlap, dead air and contrast from a record of every
mobject's bounding box at each `play()` boundary, and only the mixin produces that
record. It exists in Phase 1 *before* Gate 3 does, deliberately: retrofitting
fifteen templates later would be the expensive version of this decision.

**3. The content hash includes the template's source bytes.**
The plan's tuple was `(template_id, params, manim_version, plugin_versions)`.
`template_id` is a name, not content — fix a layout bug in `transform_chain.py`
under that scheme and every previously rendered scene of that archetype serves the
buggy video forever. `test_template_edit_invalidates_the_hash` is what keeps that
from coming back.

**4. Grounding is structural. Ungroundable claims are dropped, not approximated.**
Every concept and scene carries a `SourceSpan` with a hash of the quoted text. The
model returns verbatim quotes; anything that cannot be located in the cited section
is discarded. A `SourceDriftError` on re-ingest means the paper changed under an
existing storyboard. The failure this prevents — a beautiful animation of a wrong
intuition — is the one the plan calls critical.

**5. The template reports its own runtime; the model only supplies captions.**
`Beat` durations are fitted to `estimate_duration(params)`, not taken from the model.
Gate 2 asserts rendered duration within ±20% of the beats — but the template owns
pacing, so the model cannot predict it, and in practice missed by 60–90%, making the
assertion fire on every scene. `estimate_duration` unavoidably duplicates the body's
timeline (the control plane has no Manim to probe), so
`test_estimate_matches_real_render` holds them together. If it fails, fix the
estimator; do not loosen the tolerance.

**6. A scene failure never fails a job.**
The repair ladder ends in degradation: the paper's own figure, or prose only. A
section with good prose and no animation is a fine outcome; a section with a
garbled animation is a product failure. Budgets are checked in **dollars before
each attempt**, because three attempts does not bound spend.

---

## Known gaps

- **Nobody has looked at the output.** Two templates render and pass every
  mechanical check, but "passes Gate 2" is not "looks good". The plan's Phase 0 exit
  criterion is a human deciding the reading experience justifies building the
  machine, and that judgement has not been made.
- **`transform_chain` is unrendered** — needs LaTeX locally (`texlive` + `dvisvgm`).
  It is the archetype most likely to have layout problems, since equation width is
  the thing `fit_inside` has to fight hardest.
- **The reader has never been fed real data.** It typechecks and `next build`
  succeeds, but no article has been rendered in a browser, so the two-column layout,
  the Scrollama stage and the autoplay behaviour are unobserved.
- **Nothing has run on Modal.** The sandbox renderer, R2 upload and FastAPI app are
  written against the documented APIs and unexecuted.
- **Rung 2 of the repair ladder simplifies parameters** rather than switching
  template. With three templates there is nowhere sensible to fall back to; a
  failing derivation does not become a bar chart. Cross-template fallback wants the
  Phase 3 library.
- **`eval/papers.yaml` has 6 papers, not 30.** Phase 2's number.
- **Gate 4's contact sheet is generated but unused** — `storage.derive_assets`
  produces the framestrip; no VLM reads it yet.
- **The public-vs-private-links decision is unmade** and the schema already assumes
  public (`papers.slug` is unique and unauthenticated). See
  ArcVisual-Architecture.md §12.3 — cheap now, expensive after the first hundred
  articles are indexed.

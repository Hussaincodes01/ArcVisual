/**
 * The hero's centrepiece: a live `transform_chain`, rendered in the browser.
 *
 * This is the landing page's one real argument. Rather than *describing* what
 * ArcVisual does to a derivation, it performs one — the same scaled dot-product
 * attention chain a real `transform_chain` scene would animate, with the same palette,
 * the same 1.5s hold after each reveal, and the same rule that matching sub-expressions
 * persist while only the changed part is accented.
 *
 * **This is a server component, and that is the point.** KaTeX is ~275KB and these
 * three equations never change, so typesetting them at build time keeps the whole
 * library out of the landing bundle — the client island receives finished HTML strings
 * and only owns the timer. Shipping a maths typesetter to every visitor to render three
 * constant strings would be exactly the kind of thoughtlessness this page argues
 * against.
 *
 * It is also not video: a hero that shipped an MP4 would cost a megabyte before a
 * visitor had decided to stay.
 */

import katex from "katex";
import TransformDemoClient, { type RenderedStep } from "./TransformDemoClient";

/** Mirrors MIN_BEAT_S / REVEAL_HOLD_S in arcvisual/templates/base.py. */
export const BEAT_MS = 800;
export const HOLD_MS = 1500;

interface Step {
  latex: string;
  caption: string;
  /** Substring to accent as `Indicate` would. */
  accent?: string;
}

const STEPS: Step[] = [
  {
    latex: String.raw`\mathrm{Attention}(Q, K, V) = \;?`,
    caption: "Start with the shape of the claim.",
  },
  {
    latex: String.raw`\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(QK^{T}\right)V`,
    caption: "Score every query against every key, then weight the values.",
  },
  {
    latex: String.raw`\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^{T}}{\sqrt{d_k}}\right)V`,
    caption: "Scale by √dₖ — without it the softmax saturates and gradients vanish.",
    accent: String.raw`\sqrt{d_k}`,
  },
];

function typeset(step: Step): RenderedStep {
  let html = "";
  try {
    html = katex.renderToString(step.latex, {
      displayMode: true,
      throwOnError: false,
      strict: "ignore",
    });
    if (step.accent) {
      // Accent the changed part, the way `Indicate` does in a real scene. KaTeX emits
      // its own markup, so this matches against the rendered fragment rather than the
      // source — imperfect by nature, and harmless when it does not match.
      const fragment = katex
        .renderToString(step.accent, { throwOnError: false, strict: "ignore" })
        .replace(/^<span class="katex">/, "")
        .replace(/<\/span>$/, "");
      if (fragment && html.includes(fragment)) {
        html = html.replace(fragment, `<span class="accent-glow">${fragment}</span>`);
      }
    }
  } catch {
    html = "";
  }
  return { html, latex: step.latex, caption: step.caption };
}

export default function TransformDemo() {
  return (
    <TransformDemoClient
      steps={STEPS.map(typeset)}
      beatMs={BEAT_MS}
      holdMs={HOLD_MS}
    />
  );
}

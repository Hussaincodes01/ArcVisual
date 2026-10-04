"use client";

/**
 * `transform_chain` in the browser: an equation derived one step at a time.
 *
 * Mirrors arcvisual/templates/transform_chain.py — same parameters, same timeline.
 * Manim's `TransformMatchingTex` glides matching sub-expressions; here the outgoing
 * line lifts and fades while the incoming one is written in left to right, which
 * keeps the eye on *what changed* without a hard cut between equations.
 */

import { useMemo } from "react";
import katex from "katex";
import FitBox from "./FitBox";
import {
  PALETTE,
  ease,
  progress,
  type SceneProps,
} from "../../lib/scenes/core";
import type { TransformChainData } from "../../lib/scenes/plans";
import { plainText, prettifyPlainMath, splitMath, toUnicode } from "../../lib/tex";
import { renderInlineTex } from "../MathText";

const MATH_SIGNS = /[=^_\\+<>|/*{}()[\]∑∫√≤≥≈∝·×]/;

/** An English sentence where an equation belongs (older documents allowed it). */
function looksLikeProse(step: string): boolean {
  if (MATH_SIGNS.test(step)) return false;
  return step.split(/\s+/).filter((w) => /^[A-Za-z‑-]{3,}$/.test(w)).length >= 4;
}

const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

interface Rendered {
  html: string;
  /** A sentence (words with math in it) wraps like text; an equation stays on one line. */
  wrap: boolean;
}

/**
 * One derivation step as markup — always readable, never TeX source.
 *
 * Models sometimes write a sentence with math in it ("for any offset $k$, $PE_{pos+k}$
 * is linear in…") where an equation belongs. Typeset whole, that is a parse error;
 * the old fallback stripped its symbols and showed the remains, which read as
 * nonsense. Now words stay words and each `$…$` is typeset in place.
 */
function render(raw: string, macros: Record<string, string>): Rendered {
  // The non-breaking hyphen models like to emit has no KaTeX glyph metrics.
  const cleaned = raw.replace(/‑/g, "-");
  if (cleaned.includes("$")) {
    const html = splitMath(cleaned)
      .map((p) => {
        if (!p.math) return escapeHtml(prettifyPlainMath(p.text));
        return renderInlineTex(p.tex, macros) ?? `<span class="math-fallback">${escapeHtml(toUnicode(p.tex, macros))}</span>`;
      })
      .join("");
    return { html, wrap: true };
  }
  if (looksLikeProse(cleaned)) return { html: escapeHtml(prettifyPlainMath(cleaned)), wrap: true };
  try {
    const html = katex.renderToString(cleaned, {
      displayMode: true,
      throwOnError: true,
      strict: "ignore",
      macros: { ...macros }, // copied: KaTeX writes \gdef definitions into it
    });
    return { html, wrap: false };
  } catch {
    // Notation KaTeX cannot parse still reads as math: √dₖ, not \sqrt{d_k}.
    return { html: `<span class="math-fallback">${escapeHtml(toUnicode(cleaned, macros))}</span>`, wrap: false };
  }
}

/** The step with its accent substring coloured, or null if it cannot be found. */
function renderAccented(latex: string, accent: string, macros: Record<string, string>): string | null {
  if (!accent) return null;
  const at = latex.indexOf(accent);
  if (at < 0) return null;
  const marked = `${latex.slice(0, at)}\\textcolor{${PALETTE.coral}}{${accent}}${latex.slice(at + accent.length)}`;
  try {
    return katex.renderToString(marked, { displayMode: true, throwOnError: true, strict: "ignore", macros: { ...macros } });
  } catch {
    return null; // the substring split a group; fall back to accenting the whole step
  }
}

const NO_MACROS: Record<string, string> = {};

export default function TransformChainScene({ plan, t, macros = NO_MACROS }: SceneProps<TransformChainData>) {
  const { steps, highlights, appear, indicate, title, titleSeg } = plan.data;
  const html = useMemo(() => steps.map((s) => render(s, macros)), [steps, macros]);
  const accented = useMemo(
    () => steps.map((s, i) => renderAccented(s, highlights[i], macros)),
    [steps, highlights, macros],
  );

  // The step currently on screen: the last one whose entrance has begun.
  let k = 0;
  for (let i = 0; i < appear.length; i++) if (t >= appear[i].start) k = i;
  const enter = ease.inOutCubic(progress(t, appear[k]));

  const layers: { i: number; opacity: number; dy: number; reveal: number; scale: number }[] = [];
  if (k > 0 && enter < 1) {
    layers.push({ i: k - 1, opacity: 1 - enter, dy: -0.06 * enter, reveal: 1, scale: 1 - 0.03 * enter });
  }
  const ind = indicate[k];
  const q = progress(t, ind);
  const pulse = ind && q > 0 && q < 1 ? ease.pulse(q) : 0;
  layers.push({
    i: k,
    opacity: k === 0 ? 1 : enter,
    dy: k === 0 ? 0 : 0.05 * (1 - enter),
    reveal: k === 0 ? enter : Math.min(1, enter * 1.25),
    scale: 1 + 0.07 * pulse,
  });
  // Accent from the moment it is indicated until the next step arrives.
  const accentOn = Boolean(ind) && t >= (ind?.start ?? Infinity);

  return (
    <div className="absolute inset-0">
      {title ? (
        <p
          className="absolute inset-x-0 top-[6%] text-center font-semibold tracking-tight"
          style={{
            fontSize: "3.4cqw",
            color: PALETTE.ink,
            opacity: titleSeg ? progress(t, titleSeg) : 1,
            transform: `translateY(${(1 - progress(t, titleSeg)) * -6}px)`,
          }}
        >
          {plainText(title, macros)}
        </p>
      ) : null}

      {layers.map(({ i, opacity, dy, reveal, scale }) => {
        const useAccent = i === k && accentOn;
        const markup = useAccent && accented[i] ? accented[i]! : html[i].html;
        const wholeAccent = useAccent && !accented[i];
        const motion = {
          opacity,
          transform: `translateY(${dy * 100}cqh) scale(${scale})`,
          clipPath: `inset(-20% ${(1 - reveal) * 100}% -20% 0)`,
        };
        if (html[i].wrap) {
          // A sentence with math in it: wrap like text instead of shrinking one
          // long line until it is unreadable.
          return (
            <div key={`${i}-${k}`} className="absolute inset-0 flex items-center justify-center" style={motion}>
              <div
                className="max-w-[84%] text-center"
                style={{ fontSize: "3.5cqw", lineHeight: 1.5, color: wholeAccent ? PALETTE.coral : PALETTE.ink }}
                dangerouslySetInnerHTML={{ __html: markup }}
              />
            </div>
          );
        }
        return (
          <FitBox key={`${i}-${k}`} style={motion}>
            <div
              style={{ fontSize: "5cqw", color: wholeAccent ? PALETTE.coral : PALETTE.ink }}
              // KaTeX output of LaTeX copied verbatim from the paper.
              dangerouslySetInnerHTML={{ __html: markup }}
            />
          </FitBox>
        );
      })}

      {steps.length > 1 ? (
        <div
          className="absolute bottom-[7%] left-[5%] flex items-center gap-[0.9cqw]"
          aria-label={`Step ${k + 1} of ${steps.length}`}
        >
          {steps.map((_, i) => (
            <span
              key={i}
              className="block rounded-full border-[1.5px]"
              style={{
                width: i === k ? "3.6cqw" : "1.4cqw",
                height: "1.4cqw",
                borderColor: PALETTE.ink,
                background: i < k ? PALETTE.ink : i === k ? PALETTE.mustard : "transparent",
                transition: "width 240ms ease",
              }}
            />
          ))}
          <span className="ml-[0.6cqw] tabular-nums" style={{ fontSize: "1.9cqw", color: PALETTE.muted }}>
            step {k + 1} of {steps.length}
          </span>
        </div>
      ) : null}
    </div>
  );
}

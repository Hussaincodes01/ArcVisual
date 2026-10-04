"use client";

/**
 * Text that may contain math — a claim, a caption, a section heading — rendered
 * so the math looks like math.
 *
 * `$…$` segments are typeset with KaTeX using the paper's own macros; if KaTeX
 * cannot parse one, it falls back to readable Unicode (`√dₖ`), never to source.
 * Words around them get the same treatment for pseudo-math models type without
 * dollar signs (`sqrt(d_k)` → `√(dₖ)`).
 */

import { memo, useMemo } from "react";
import katex from "katex";
import { prettifyPlainMath, splitMath, toUnicode } from "../lib/tex";

interface Props {
  text: string;
  macros?: Record<string, string>;
  className?: string;
}

export function renderInlineTex(tex: string, macros?: Record<string, string>): string | null {
  try {
    return katex.renderToString(tex, {
      displayMode: false,
      throwOnError: true,
      strict: "ignore",
      macros: { ...(macros ?? {}) }, // copied: KaTeX writes \gdef definitions into it
    });
  } catch {
    return null;
  }
}

function MathText({ text, macros, className }: Props) {
  const pieces = useMemo(
    () =>
      splitMath(text).map((p) => {
        if (!p.math) return { html: null, text: prettifyPlainMath(p.text) };
        const html = renderInlineTex(p.tex, macros);
        return html ? { html, text: "" } : { html: null, text: toUnicode(p.tex, macros) };
      }),
    [text, macros],
  );
  return (
    <span className={className}>
      {pieces.map((p, i) =>
        p.html ? (
          // KaTeX output of model- or paper-supplied notation.
          <span key={i} dangerouslySetInnerHTML={{ __html: p.html }} />
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </span>
  );
}

export default memo(MathText);

/**
 * Hand-filled scenes for the landing page.
 *
 * These are exactly the parameter objects the pipeline's codegen produces for each
 * template, filled by hand — the way the architecture doc says templates should be
 * validated before any model is trusted to fill them. The numbers are real:
 * the ResNet figures are top-1 error (%, 10-crop) on ImageNet validation from
 * He et al. 2015, Table 2.
 */

import type { Beat } from "./types";
import { MIN_BEAT_S } from "./scenes/core";
import { planArchitectureFlow, planPlotReveal, planTransformChain } from "./scenes/plans";

const PLANS: Record<string, (p: Record<string, unknown>) => { duration: number } | null> = {
  transform_chain: planTransformChain,
  plot_reveal: planPlotReveal,
  architecture_flow: planArchitectureFlow,
};

export interface DemoScene {
  archetype: string;
  claim: string;
  source: string;
  params: Record<string, unknown>;
  beats: Beat[];
}

/** Lay captions end to end across the runtime the template dictates (codegen.fit_beats). */
function fitBeats(captions: string[], total: number): Beat[] {
  const seatable = Math.max(1, Math.floor(total / MIN_BEAT_S));
  const kept = captions.filter((c) => c.trim()).slice(0, seatable);
  const each = Math.round(Math.max(MIN_BEAT_S, total / Math.max(1, kept.length)) * 1000) / 1000;
  return kept.map((caption, i) => ({ t: Math.round(i * each * 1000) / 1000, dur: each, caption }));
}

function scene(
  archetype: string,
  claim: string,
  source: string,
  params: Record<string, unknown>,
  captions: string[],
): DemoScene {
  const plan = PLANS[archetype]?.({ ...params, captions });
  return {
    archetype,
    claim,
    source,
    params: { ...params, captions },
    beats: fitBeats(captions, plan?.duration ?? 6),
  };
}

export const ATTENTION_DEMO = scene(
  "transform_chain",
  "Dividing by √dₖ keeps dot products from growing with dimension, so the softmax never saturates.",
  "Attention Is All You Need · §3.2.1",
  {
    title: "Scaled dot-product attention",
    steps: [
      "\\mathrm{score}(q, k) = q \\cdot k",
      "\\mathrm{score}(q, k) = \\frac{q \\cdot k}{\\sqrt{d_k}}",
      "\\mathrm{Attention}(Q, K, V) = \\mathrm{softmax}\\!\\left(\\frac{QK^{\\top}}{\\sqrt{d_k}}\\right) V",
    ],
    highlight: ["", "\\sqrt{d_k}", "\\mathrm{softmax}"],
  },
  [
    "Start with how well a query matches a key: their dot product.",
    "For large dₖ that product grows large, so divide by √dₖ.",
    "Softmax turns the scaled scores into weights over the values.",
  ],
);

export const RESNET_DEMO = scene(
  "plot_reveal",
  "Making a plain network deeper made it worse; making a residual network deeper made it better.",
  "Deep Residual Learning · Table 2",
  {
    x_label: "Layers",
    y_label: "Top-1 error (%)",
    series: [
      { label: "Plain", points: [[18, 27.94], [34, 28.54]], dashed: true, annotate_at: 1 },
      { label: "ResNet", points: [[18, 27.88], [34, 25.03]], dashed: false, annotate_at: 1 },
    ],
    x_range: [14, 38],
    y_range: [24, 29.5],
    takeaway: "Shortcut connections let depth help instead of hurt",
  },
  [
    "Two networks, 18 and 34 layers deep, on ImageNet.",
    "The plain network gets worse as it gets deeper.",
    "The residual network gets better with the same extra depth.",
  ],
);

export const TRANSFORMER_DEMO = scene(
  "architecture_flow",
  "An encoder layer is two sublayers, each wrapped in a residual connection and normalisation.",
  "Attention Is All You Need · §3.1",
  {
    nodes: [
      { id: "emb", label: "Input embedding", column: 0, row: 0, emphasis: false },
      { id: "attn", label: "Multi-head attention", column: 1, row: 0, emphasis: true },
      { id: "norm1", label: "Add & norm (residual)", column: 2, row: 0, emphasis: false },
      { id: "ffn", label: "Feed-forward", column: 2, row: 1, emphasis: false },
      { id: "norm2", label: "Add & norm (residual)", column: 1, row: 1, emphasis: false },
      { id: "out", label: "To next layer", column: 0, row: 1, emphasis: false },
    ],
    edges: [
      { src: "emb", dst: "attn", label: null, dashed: false },
      { src: "attn", dst: "norm1", label: null, dashed: false },
      { src: "norm1", dst: "ffn", label: null, dashed: false },
      { src: "ffn", dst: "norm2", label: null, dashed: false },
      { src: "norm2", dst: "out", label: null, dashed: false },
    ],
    stages: [["emb"], ["attn", "norm1"], ["ffn", "norm2"], ["out"]],
    trace_path: ["emb", "attn", "norm1", "ffn", "norm2", "out"],
  },
  [
    "Each token starts as an embedding.",
    "Attention mixes in context, then the input is added back and normalised.",
    "A feed-forward block refines each position on its own.",
    "The result flows into the next identical layer.",
    "Follow one token through the whole layer.",
  ],
);

export const DEMOS = [ATTENTION_DEMO, RESNET_DEMO, TRANSFORMER_DEMO];

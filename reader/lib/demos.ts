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
import { planArchitectureFlow, planConceptDiagram, planPlotReveal, planTransformChain } from "./scenes/plans";

const PLANS: Record<string, (p: Record<string, unknown>) => { duration: number } | null> = {
  transform_chain: planTransformChain,
  plot_reveal: planPlotReveal,
  architecture_flow: planArchitectureFlow,
  concept_diagram: planConceptDiagram,
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

/**
 * The same idea as ATTENTION_DEMO, drawn as the mechanism instead of the formula:
 * queries meet keys, the score grid lights up, softmax turns a row into weights,
 * and the weights mix the values. This is what `concept_diagram` is for.
 */
export const ATTENTION_DIAGRAM_DEMO = scene(
  "concept_diagram",
  "Each query is scored against every key; softmax turns the scores into weights that mix the values.",
  "Attention Is All You Need · §3.2.1",
  {
    "title": "Scaled dot-product attention",
    "elements": [
      {
        "id": "q",
        "kind": "stack",
        "label": "Queries Q",
        "column": 0,
        "row": 0,
        "cells": [
          "q₁",
          "q₂",
          "q₃"
        ],
        "tone": "input"
      },
      {
        "id": "k",
        "kind": "stack",
        "label": "Keys K",
        "column": 0,
        "row": 1,
        "cells": [
          "k₁",
          "k₂",
          "k₃"
        ],
        "tone": "input"
      },
      {
        "id": "dot",
        "kind": "op",
        "label": "QKᵀ",
        "column": 1,
        "row": 0,
        "row_span": 2
      },
      {
        "id": "scores",
        "kind": "grid",
        "label": "scores",
        "column": 2,
        "row": 0,
        "row_span": 2,
        "size": 3,
        "values": [
          2.1,
          0.3,
          0.2,
          0.4,
          1.8,
          0.9,
          0.1,
          0.6,
          2.4
        ],
        "note": "n × n"
      },
      {
        "id": "sm",
        "kind": "op",
        "label": "softmax",
        "column": 3,
        "row": 0,
        "row_span": 2,
        "tone": "accent"
      },
      {
        "id": "w",
        "kind": "bars",
        "label": "weights for q₁",
        "column": 4,
        "row": 0,
        "cells": [
          "v₁",
          "v₂",
          "v₃"
        ],
        "values": [
          0.78,
          0.13,
          0.09
        ],
        "tone": "accent"
      },
      {
        "id": "v",
        "kind": "stack",
        "label": "Values V",
        "column": 4,
        "row": 1,
        "cells": [
          "v₁",
          "v₂",
          "v₃"
        ],
        "tone": "input"
      },
      {
        "id": "out",
        "kind": "block",
        "label": "Output",
        "note": "weighted sum of V",
        "column": 5,
        "row": 0,
        "row_span": 2,
        "tone": "output"
      }
    ],
    "connections": [
      {
        "src": "q",
        "dst": "dot"
      },
      {
        "src": "k",
        "dst": "dot"
      },
      {
        "src": "dot",
        "dst": "scores",
        "label": "÷ √dₖ"
      },
      {
        "src": "scores",
        "dst": "sm"
      },
      {
        "src": "sm",
        "dst": "w"
      },
      {
        "src": "w",
        "dst": "out"
      },
      {
        "src": "v",
        "dst": "out"
      }
    ],
    "steps": [
      {
        "caption": "Every token brings a query and a key.",
        "show": [
          "q",
          "k"
        ]
      },
      {
        "caption": "Each query is compared with every key by a dot product.",
        "show": [
          "dot",
          "scores"
        ],
        "flow": [
          "q",
          "dot",
          "scores"
        ]
      },
      {
        "caption": "Row 1 holds how well q₁ matches each key.",
        "marks": [
          {
            "target": "scores",
            "item": 0,
            "col": 0
          },
          {
            "target": "scores",
            "item": 0,
            "col": 1
          },
          {
            "target": "scores",
            "item": 0,
            "col": 2
          }
        ]
      },
      {
        "caption": "Softmax turns that row into weights that sum to one.",
        "show": [
          "sm",
          "w"
        ],
        "flow": [
          "scores",
          "sm",
          "w"
        ],
        "marks": [
          {
            "target": "w",
            "item": 0,
            "col": 0
          }
        ]
      },
      {
        "caption": "The output is the values mixed by those weights.",
        "show": [
          "v",
          "out"
        ],
        "flow": [
          "w",
          "out"
        ],
        "focus": [
          "out"
        ]
      }
    ]
  },
  ["Every token brings a query and a key.", "Each query is compared with every key by a dot product.", "Row 1 holds how well q₁ matches each key.", "Softmax turns that row into weights that sum to one.", "The output is the values mixed by those weights."],
);

export const DEMOS = [ATTENTION_DIAGRAM_DEMO, ATTENTION_DEMO, RESNET_DEMO, TRANSFORMER_DEMO];

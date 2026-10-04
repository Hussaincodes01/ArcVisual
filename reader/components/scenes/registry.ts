/**
 * Archetype → browser renderer. The client-side mirror of
 * arcvisual/templates/registry.py, restricted to what can be drawn from parameters
 * alone (`CLIENT_RENDERABLE` there). Adding an archetype means adding it on both
 * sides; the pipeline will not propose one this map does not know.
 */

import type { ComponentType } from "react";
import type { ScenePlan, SceneProps } from "../../lib/scenes/core";
import { planArchitectureFlow, planPlotReveal, planTransformChain } from "../../lib/scenes/plans";
import ArchitectureFlowScene from "./ArchitectureFlowScene";
import PlotRevealScene from "./PlotRevealScene";
import TransformChainScene from "./TransformChainScene";

export interface SceneRenderer {
  plan: (params: Record<string, unknown>) => ScenePlan<unknown> | null;
  Component: ComponentType<SceneProps<unknown>>;
  /** What the scene does, for screen readers and loading states. */
  verb: string;
}

export const RENDERERS: Record<string, SceneRenderer> = {
  transform_chain: {
    plan: planTransformChain as SceneRenderer["plan"],
    Component: TransformChainScene as SceneRenderer["Component"],
    verb: "A derivation, one step at a time",
  },
  plot_reveal: {
    plan: planPlotReveal as SceneRenderer["plan"],
    Component: PlotRevealScene as SceneRenderer["Component"],
    verb: "A result, drawn as it builds",
  },
  architecture_flow: {
    plan: planArchitectureFlow as SceneRenderer["plan"],
    Component: ArchitectureFlowScene as SceneRenderer["Component"],
    verb: "A system, assembled in order",
  },
};

export function canRender(archetype: string): boolean {
  return archetype in RENDERERS;
}

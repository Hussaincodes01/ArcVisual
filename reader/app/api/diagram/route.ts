/**
 * POST /api/diagram — the TypeScript diagram agent, called by the Python pipeline.
 *
 * Server-side only: the OpenRouter key lives in this function's environment and never
 * reaches a browser. Callers authenticate with `ARCVISUAL_INTERNAL_SECRET`; without
 * that secret configured the route refuses everything, so a deployment that has not
 * opted in exposes no model on the public internet.
 */

import { timingSafeEqual } from "node:crypto";
import { drawDiagram, type DiagramRequest } from "../../../lib/harness/diagramAgent";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300;

const DEFAULT_MODELS =
  "nvidia/nemotron-3.5-lightning:free,qwen/qwen3-235b-a22b-2507,openai/gpt-oss-120b,deepseek/deepseek-v3.2";
/** Never let the agent run into the platform's 300s limit. */
const MAX_BUDGET_S = 250;

function authorised(req: Request): boolean {
  const secret = process.env.ARCVISUAL_INTERNAL_SECRET ?? "";
  const given = req.headers.get("x-arcvisual-key") ?? "";
  if (!secret || given.length !== secret.length) return false;
  return timingSafeEqual(Buffer.from(given), Buffer.from(secret));
}

function str(v: unknown, max: number): string {
  return typeof v === "string" ? v.slice(0, max) : "";
}

export async function POST(req: Request): Promise<Response> {
  if (!authorised(req)) return Response.json({ error: "unauthorised" }, { status: 401 });
  const apiKey = process.env.OPENROUTER_API_KEY ?? "";
  if (!apiKey) return Response.json({ error: "OPENROUTER_API_KEY is not set" }, { status: 503 });

  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return Response.json({ error: "body must be JSON" }, { status: 400 });
  }
  const request: DiagramRequest = {
    title: str(body.title, 300),
    claim: str(body.claim, 600),
    concept: str(body.concept, 600),
    quote: str(body.quote, 3000),
    section: str(body.section, 12000),
    equations: Array.isArray(body.equations) ? body.equations.filter((e): e is string => typeof e === "string").slice(0, 6) : [],
  };
  if (!request.claim) return Response.json({ error: "claim is required" }, { status: 400 });

  const budgetS = Math.min(MAX_BUDGET_S, Math.max(10, Number(body.budget_s) || 120));
  const result = await drawDiagram(request, {
    apiKey,
    baseUrl: process.env.OPENROUTER_BASE_URL || undefined,
    models: (process.env.DIAGRAM_MODELS || process.env.LLM_MODEL_CODEGEN || DEFAULT_MODELS)
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean),
    // `||`, not `??`: an empty variable must not become 0, which means "no cap".
    reasoningMaxTokens: Number(process.env.LLM_REASONING_MAX_TOKENS || 800),
    deadline: Date.now() + budgetS * 1000,
  });
  return Response.json(result);
}

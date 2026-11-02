/**
 * The diagram agent: a model draws one `concept_diagram` by calling tools.
 *
 * Built for a slow free model, so turns are the unit of cost: one tool takes the
 * whole diagram and accepts it the moment it is sound, so the good case is one turn.
 * Problems come back in the same conversation, so a fix costs one more turn instead
 * of a whole regeneration. The loop stops at a turn limit and at the
 * caller's deadline, whichever comes first.
 */

import { Chat, OutOfTime, NoModelLeft, addUsage, type ClientOptions, type Message, type Usage } from "./openrouter";
import { TOOLS, emptyState, runTool, toParams } from "./diagramTools";

export interface DiagramRequest {
  title: string;
  claim: string;
  concept: string;
  quote: string;
  /** The section the idea comes from; names, structure and numbers to draw from. */
  section: string;
  equations: string[];
}

export interface DiagramResult {
  ok: boolean;
  params: Record<string, unknown> | null;
  captions: string[];
  turns: number;
  model: string | null;
  usage: Usage;
  notes: string[];
}

export const SYSTEM = `You draw one diagram that explains one idea from a research paper, and script how it animates.

You have one tool, submit_diagram. Call it with the WHOLE diagram — elements, arrows and steps — in your first reply. It is accepted at once if nothing is wrong; otherwise it lists every problem. Fix them all and call it again with the whole corrected diagram. Write no prose outside the tool call.

DRAW THE OBJECTS THE IDEA IS ABOUT
- Use the paper's own things and names: "Queries Q", "Keys K", "Expert 3". Never "Input A".
- Pick the kind that matches what the thing IS: a sequence of words or patches is tokens; a set of vectors is a stack; a pairwise matrix (attention, similarity) is a grid; a probability or weighting is bars; a transformation is an op (×, +, ⊕, softmax, concat); a learned component is a block; a repeated or grouped region is a container spanning its members.
- Labels in Unicode: QKᵀ, √dₖ, x₁…xₙ. No LaTeX, no dollar signs. At most one text element, as an annotation.
- Use numbers the paper gives in values; otherwise leave values empty.

LAYOUT: data flows left to right over columns 0-7; parallel streams use rows 0-5. One element per cell; tall things get row_span, wide things col_span. 5-12 elements.

SCRIPT 3-7 STEPS, ONE IDEA EACH: inputs first, then each operation, then the result. show brings parts in; flow sends a pulse along a path of ids; marks light up cells or items; relabel changes a label when a thing changes meaning; focus points at the contribution. Each caption is one plain sentence about what is on screen now.`;

export function userMessage(req: DiagramRequest): string {
  const eqs = req.equations.length ? `\nEQUATIONS IN THIS SECTION (verbatim)\n${req.equations.slice(0, 6).join("\n")}\n` : "";
  return `PAPER: ${req.title}

THE IDEA TO DRAW
${req.claim}

THE CONCEPT
${req.concept}

VERBATIM FROM THE PAPER (ground truth)
"""
${req.quote.slice(0, 1500)}
"""
${eqs}
THE SECTION IT COMES FROM
"""
${req.section.slice(0, 12000)}
"""`;
}

export interface AgentOptions extends ClientOptions {
  /** Absolute deadline, in the clock `now` reports (ms). */
  deadline: number;
  maxTurns?: number;
}

export async function drawDiagram(req: DiagramRequest, opts: AgentOptions): Promise<DiagramResult> {
  const chat = new Chat(opts);
  const state = emptyState();
  const messages: Message[] = [
    { role: "system", content: SYSTEM },
    { role: "user", content: userMessage(req) },
  ];
  let usage: Usage = { prompt: 0, completion: 0, reasoning: 0 };
  let model: string | null = null;
  const notes: string[] = [];
  const maxTurns = opts.maxTurns ?? 4;
  let nudged = false;

  for (let turn = 1; turn <= maxTurns; turn++) {
    let reply;
    try {
      reply = await chat.send(messages, TOOLS, opts.deadline);
    } catch (err) {
      if (err instanceof OutOfTime || err instanceof NoModelLeft) {
        notes.push(err.message);
        return { ok: false, params: null, captions: [], turns: turn - 1, model, usage, notes };
      }
      throw err;
    }
    usage = addUsage(usage, reply.usage);
    model = reply.model;
    const msg = reply.message;
    // Passed back unmodified: the model continues its reasoning on the next turn.
    messages.push({
      role: "assistant",
      content: msg.content ?? "",
      tool_calls: msg.tool_calls,
      ...(msg.reasoning_details !== undefined ? { reasoning_details: msg.reasoning_details } : {}),
    });

    const calls = msg.tool_calls ?? [];
    if (!calls.length) {
      if (nudged) {
        notes.push("the model answered without calling a tool, twice");
        break;
      }
      nudged = true;
      messages.push({ role: "user", content: "Call submit_diagram now with the whole diagram." });
      continue;
    }
    for (const call of calls) {
      const { reply: out, done } = runTool(state, call.function.name, call.function.arguments);
      messages.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(out) });
      if (done) {
        return {
          ok: true,
          params: toParams(state),
          captions: state.steps.map((s) => s.caption),
          turns: turn,
          model,
          usage,
          notes,
        };
      }
    }
  }
  notes.push(`no accepted diagram within ${maxTurns} turns`);
  return { ok: false, params: null, captions: [], turns: maxTurns, model, usage, notes };
}

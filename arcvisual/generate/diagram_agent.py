"""Client for the TypeScript diagram agent (``reader/app/api/diagram``).

The agent draws a ``concept_diagram`` by calling tools that validate each step as it
is taken, so a mistake costs one more turn of the same conversation rather than a
whole regeneration. This module only asks it for parameters; everything after that
(the template's validation, beats, Gate 1) is the same path one-shot codegen takes.

It never fails a scene. Not configured, out of time, unreachable, or no accepted
diagram: it returns None and codegen falls back to the one-shot call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from arcvisual.config import settings
from arcvisual.storyboard import Storyboard, VisualOpportunity

log = logging.getLogger(__name__)

#: Below this, the agent cannot finish a turn on the free endpoint (~15s each).
MIN_BUDGET_S = 30.0
#: Never ask for more: the route caps itself at 250s inside a 300s function.
MAX_BUDGET_S = 240.0


@dataclass
class AgentDiagram:
    params: dict[str, Any]
    captions: list[str]
    turns: int
    model: str
    notes: list[str] = field(default_factory=list)


def configured() -> bool:
    cfg = settings()
    return bool(cfg.diagram_agent_url and cfg.internal_secret)


def draw(
    sb: Storyboard,
    opportunity: VisualOpportunity,
    *,
    budget_s: float,
    client: Any = None,
) -> AgentDiagram | None:
    """Ask the agent for one diagram's parameters, or None to fall back."""
    cfg = settings()
    if not configured():
        return None
    budget_s = min(budget_s, MAX_BUDGET_S)
    if budget_s < MIN_BUDGET_S:
        log.info("diagram agent skipped: only %.0fs left", budget_s)
        return None

    section = sb.section(opportunity.span.section_id)
    concept = sb.concept(opportunity.concept_id)
    payload = {
        "title": sb.paper.title,
        "claim": opportunity.claim,
        "concept": f"{concept.name} — {concept.statement}" if concept else "",
        "quote": opportunity.span.resolve(section),
        "section": section.raw[:12000],
        "equations": [e.latex for e in sb.equations if e.span.section_id == section.id][
            :6
        ],
        "budget_s": round(budget_s),
    }
    headers = {"x-arcvisual-key": cfg.internal_secret}
    try:
        if client is None:
            import httpx

            # The agent bounds itself by budget_s; this only catches a hung network.
            with httpx.Client(timeout=budget_s + 15) as http:
                response = http.post(cfg.diagram_agent_url, json=payload, headers=headers)
        else:
            response = client.post(cfg.diagram_agent_url, json=payload, headers=headers)
    except Exception as exc:  # network trouble is a fallback, never a failed scene
        log.warning("diagram agent unreachable: %s", exc)
        return None
    if response.status_code != 200:
        log.warning(
            "diagram agent answered %s: %s", response.status_code, response.text[:200]
        )
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not body.get("ok") or not isinstance(body.get("params"), dict):
        log.info("diagram agent gave no diagram: %s", "; ".join(body.get("notes") or []))
        return None
    return AgentDiagram(
        params=body["params"],
        captions=[c for c in body.get("captions") or [] if isinstance(c, str)],
        turns=int(body.get("turns") or 0),
        model=str(body.get("model") or ""),
        notes=[n for n in body.get("notes") or [] if isinstance(n, str)],
    )

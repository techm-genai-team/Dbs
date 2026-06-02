"""
graph/graph_runner.py
──────────────────────
WHAT THIS FILE DOES:
  Runs one conversation turn.
  Detects new vs resume session.
  Returns dict compatible with terminal output.

WHAT CHANGED FROM STRANDS:
  DELETED  → invoke_agent() — called LLM every turn
  DELETED  → invoke_rdr_agent() — called LLM every turn
  ADDED    → run_ro_graph_turn() — LLM called Turn 1 only
  ADDED    → run_rdr_graph_turn() — ZERO LLM calls

HOW WAIT/RESUME WORKS:
  Turn 1 (new session):
    graph.invoke(initial_state, config)
    → graph runs until interrupt() fires
    → SQLite saves checkpoint
    → returns state to terminal

  Turn 2+ (resume):
    graph.invoke(Command(resume=user_message), config)
    → SQLite loads checkpoint
    → interrupt() receives user_message
    → graph continues from exact pause point
    → NO LLM call
"""
from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from langgraph.types import Command

from graph.ro_graph  import ROGraphState,  _init_ro_state,  get_ro_graph
from graph.rdr_graph import RDRGraphState, _init_rdr_state, get_rdr_graph

load_dotenv()
logger = logging.getLogger(__name__)


def _config(session_id: str) -> dict:
    """Each session has isolated checkpoint via thread_id."""
    return {"configurable": {"thread_id": session_id}}


def _is_new_session(graph, session_id: str) -> bool:
    """True if no checkpoint exists for this session."""
    try:
        snap = graph.get_state(_config(session_id))
        return snap is None or not snap.values
    except Exception:
        return True


def _get_latest_state(graph, session_id: str, invoke_result) -> dict:
    """
    graph.invoke() returns state dict when complete,
    or the graph state snapshot when interrupted.
    Either way we want the latest state.
    """
    if invoke_result is not None:
        return invoke_result
    snap = graph.get_state(_config(session_id))
    return snap.values if snap else {}


# ─────────────────────────────────────────────────────────────────────────────
# RO Graph runner
# ─────────────────────────────────────────────────────────────────────────────

def run_ro_graph_turn(
    session_id:   str,
    user_message: str,
    context:      dict[str, Any],
) -> dict[str, Any]:
    """
    One turn of RO investigation.

    Turn 1  → graph.invoke(init_state)  → Bedrock classify_intent fires
    Turn 2+ → graph.invoke(Command(resume=msg)) → ZERO LLM, resume from checkpoint
    """
    graph  = get_ro_graph()
    config = _config(session_id)

    try:
        if _is_new_session(graph, session_id):
            logger.info("RO | NEW session=%s", session_id)
            init = _init_ro_state(
                session_id    = session_id,
                dealer_number = context.get("dealer_number"),
                region        = context.get("region"),
                ro_number     = context.get("ro_number"),
                vin_last_8    = context.get("vin_last_8"),
                dms_closed    = context.get("dms_closed"),
            )
            init["last_user_input"] = user_message
            raw = graph.invoke(init, config)
        else:
            logger.info("RO | RESUME session=%s msg=%r", session_id, user_message[:60])
            raw = graph.invoke(Command(resume=user_message), config)

        state = _get_latest_state(graph, session_id, raw)

    except Exception as exc:
        logger.exception("RO graph error session=%s", session_id)
        return {
            "reply":                 "Internal error. Please try again.",
            "tool_calls_made":       [],
            "needs_clarification":   False,
            "clarification_type":    None,
            "escalate_to_helpdesk":  False,
            "conversation_complete": False,
            "extracted_context":     {},
            "metadata":              {"error": str(exc)},
        }

    return {
        "reply":                 state.get("reply") or "",
        "tool_calls_made":       state.get("tool_calls_made") or [],
        "needs_clarification":   state.get("needs_clarification", False),
        "clarification_type":    state.get("clarification_type"),
        "escalate_to_helpdesk":  state.get("escalate_to_helpdesk", False),
        "conversation_complete": state.get("conversation_complete", False),
        "extracted_context": {
            "dealer_number":     state.get("dealer_number"),
            "dealer_normalised": state.get("dealer_normalised"),
            "region":            state.get("region"),
            "ro_number":         state.get("ro_number"),
            "vin_last_8":        state.get("vin_last_8"),
        },
        "metadata": {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# RDR Graph runner
# ─────────────────────────────────────────────────────────────────────────────

def run_rdr_graph_turn(
    session_id:   str,
    user_message: str,
    context:      dict[str, Any],
) -> dict[str, Any]:
    """
    One turn of RDR investigation.
    ZERO Bedrock calls — entire flow is pure Python.
    """
    graph  = get_rdr_graph()
    config = _config(session_id)

    try:
        if _is_new_session(graph, session_id):
            logger.info("RDR | NEW session=%s", session_id)
            init = _init_rdr_state(
                session_id               = session_id,
                dealer_number            = context.get("dealer_number"),
                sales_consultant_user_id = context.get("sales_consultant_user_id"),
            )
            init["last_user_input"] = user_message
            raw = graph.invoke(init, config)
        else:
            logger.info("RDR | RESUME session=%s msg=%r", session_id, user_message[:60])
            raw = graph.invoke(Command(resume=user_message), config)

        state = _get_latest_state(graph, session_id, raw)

    except Exception as exc:
        logger.exception("RDR graph error session=%s", session_id)
        return {
            "reply":                 "Internal error. Please try again.",
            "tool_calls_made":       [],
            "needs_clarification":   False,
            "conversation_complete": False,
            "extracted_context":     {},
        }

    return {
        "reply":                 state.get("reply") or "",
        "tool_calls_made":       state.get("tool_calls_made") or [],
        "needs_clarification":   state.get("needs_clarification", False),
        "conversation_complete": state.get("conversation_complete", False),
        "extracted_context": {
            "dealer_number":            state.get("dealer_number"),
            "sales_consultant_user_id": state.get("sales_consultant_user_id"),
        },
    }

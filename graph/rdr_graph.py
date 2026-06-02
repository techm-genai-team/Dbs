"""
graph/rdr_graph.py
───────────────────
WHAT THIS FILE DOES:
  Complete RDR LMS ID Investigation LangGraph state machine.
  ZERO Bedrock calls — pure Python nodes.

WHAT CHANGED FROM STRANDS:
  DELETED  → rdr_agent.py (Haiku Bedrock model, 3 LLM calls per session)
  DELETED  → update_rdr_context.py (@tool with global variable race condition)
  ADDED    → StateGraph with 4 nodes
  ADDED    → Python split() replaces LLM parsing "NNA3565 XD389786"
  ADDED    → interrupt() for mid-flow user input
  ADDED    → SQLite checkpoint for state persistence

FLOW:
  START
  → collect_context   ← Pure Python (split() parses dealer + user_id)
       ↕ [missing] → human_input (WAIT) → back to collect_context
       ↕ [non-NNA] → END
  → check_user_status ← Direct DB call (query_dlr_usr_prfl)
       ├─ not found → END
       └─ inactive  → END
       └─ active    → check_lms
  → check_lms         ← Direct DB call (query_dlr_usr)
       ├─ not in LMS → END
       └─ in LMS     → END (ticket raised)
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from checkpointer import get_checkpointer
from tools.rdr_tools import query_dlr_usr, query_dlr_usr_prfl

load_dotenv()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

class RDRGraphState(TypedDict):
    session_id:               str
    dealer_number:            Optional[str]
    sales_consultant_user_id: Optional[str]
    messages: Annotated[list[dict], lambda a, b: a + b]
    current_node:    str
    awaiting:        Optional[str]
    last_user_input: Optional[str]
    user_prfl_result: Optional[dict]
    lms_result:       Optional[dict]
    reply:                 Optional[str]
    needs_clarification:   bool
    conversation_complete: bool
    tool_calls_made:       list[str]


def _init_rdr_state(
    session_id: str,
    dealer_number: str | None = None,
    sales_consultant_user_id: str | None = None,
) -> RDRGraphState:
    return RDRGraphState(
        session_id=session_id,
        dealer_number=dealer_number,
        sales_consultant_user_id=sales_consultant_user_id,
        messages=[], current_node="collect_context",
        awaiting=None, last_user_input=None,
        user_prfl_result=None, lms_result=None,
        reply=None, needs_clarification=False,
        conversation_complete=False, tool_calls_made=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# NODE 1 — collect_context
# Replaces: update_rdr_context @tool + LLM parsing + global _rdr_session_id
# ─────────────────────────────────────────────────────────────────────────────

def collect_context(state: RDRGraphState) -> dict:
    """
    Collects dealer_number and sales_consultant_user_id.
    Python split() replaces LLM. Zero Bedrock.
    interrupt() for mid-flow pause if info missing.
    """
    awaiting   = state.get("awaiting")
    user_input = state.get("last_user_input", "")
    updates: dict = {
        "current_node":        "collect_context",
        "awaiting":            None,
        "needs_clarification": False,
    }

    # Process previous answer
    if awaiting == "both" and user_input:
        parts = user_input.strip().split()
        if len(parts) >= 2:
            updates["dealer_number"]            = parts[0].upper()
            updates["sales_consultant_user_id"] = parts[1].upper()
        elif len(parts) == 1:
            updates["dealer_number"] = parts[0].upper()
    elif awaiting == "dealer" and user_input:
        updates["dealer_number"] = user_input.strip().upper()
    elif awaiting == "user_id" and user_input:
        updates["sales_consultant_user_id"] = user_input.strip().upper()

    dealer  = updates.get("dealer_number")            or state.get("dealer_number")
    user_id = updates.get("sales_consultant_user_id") or state.get("sales_consultant_user_id")

    # Non-NNA dealer check
    if dealer and not dealer.upper().startswith("NNA"):
        return {**updates,
            "dealer_number":         dealer,
            "reply":                 "This flow applies only to NNA (U.S.) dealerships. "
                                     "Please contact your regional support.",
            "conversation_complete": True}

    # Missing info — interrupt
    if not dealer and not user_id:
        q   = "Please provide your Dealer Number and Sales Consultant User ID (e.g. NNA3565 XD389786)."
        ans = interrupt({"question": q, "awaiting": "both"})
        return {**updates, "awaiting": "both", "last_user_input": ans,
                "reply": q, "needs_clarification": True}

    if not dealer:
        q   = "Please provide your Dealer Number (e.g. NNA3565)."
        ans = interrupt({"question": q, "awaiting": "dealer"})
        return {**updates, "awaiting": "dealer", "last_user_input": ans,
                "reply": q, "needs_clarification": True}

    if not user_id:
        q   = "Please provide the Sales Consultant User ID (e.g. XD389786)."
        ans = interrupt({"question": q, "awaiting": "user_id"})
        return {**updates, "awaiting": "user_id", "last_user_input": ans,
                "reply": q, "needs_clarification": True}

    updates.update({
        "dealer_number":            dealer,
        "sales_consultant_user_id": user_id,
        "awaiting":                 None,
        "needs_clarification":      False,
    })
    logger.info("rdr collect_context done | dealer=%s user=%s", dealer, user_id)
    return updates


# ─────────────────────────────────────────────────────────────────────────────
# NODE 2 — human_input (WAIT STATE)
# ─────────────────────────────────────────────────────────────────────────────

def human_input(state: RDRGraphState) -> dict:
    """Graph suspends here. Resumes when user replies. NO LLM."""
    question  = state.get("reply") or "Please provide the requested information."
    user_text = interrupt({"question": question, "awaiting": state.get("awaiting")})
    return {"last_user_input": user_text}


# ─────────────────────────────────────────────────────────────────────────────
# NODE 3 — check_user_status
# Replaces: LLM calling query_dlr_usr_prfl @tool
# ─────────────────────────────────────────────────────────────────────────────

def check_user_status(state: RDRGraphState) -> dict:
    """Direct DB call. Node interprets result. No LLM."""
    dealer  = state["dealer_number"]
    user_id = state["sales_consultant_user_id"]

    logger.info("check_user_status | dealer=%s user=%s", dealer, user_id)
    result = query_dlr_usr_prfl(dealer_number=dealer, usr_id=user_id)

    base = {
        "user_prfl_result": result,
        "current_node":     "check_user_status",
        "tool_calls_made":  state.get("tool_calls_made", []) + ["query_dlr_usr_prfl"],
    }

    if result.get("error"):
        return {**base, "reply": "Database error. Please try again.",
                "conversation_complete": True}

    if not result.get("found"):
        return {**base,
            "reply":                f"User {user_id} not found in DBS for {dealer}. "
                                    "Please verify Dealer Number and User ID, or check NNAnet.",
            "conversation_complete": True}

    if not result.get("is_active"):
        return {**base,
            "reply":                f"Sales Consultant {user_id} is Inactive in DBS for {dealer}. "
                                    "Please update the status in NNAnet.",
            "conversation_complete": True}

    return base  # active → proceed to LMS check


# ─────────────────────────────────────────────────────────────────────────────
# NODE 4 — check_lms
# Replaces: LLM calling query_dlr_usr @tool
# ─────────────────────────────────────────────────────────────────────────────

def check_lms(state: RDRGraphState) -> dict:
    """Direct DB call. Node interprets result. No LLM."""
    user_id = state["sales_consultant_user_id"]

    logger.info("check_lms | user=%s", user_id)
    result = query_dlr_usr(usr_id=user_id)

    base = {
        "lms_result":            result,
        "current_node":          "check_lms",
        "tool_calls_made":       state.get("tool_calls_made", []) + ["query_dlr_usr"],
        "conversation_complete": True,
    }

    if result.get("error"):
        return {**base, "reply": "Database error checking LMS. Please try again."}

    if not result.get("registered_in_lms"):
        return {**base,
            "reply": f"User ID {user_id} is not registered with Virtual Academy (LMS). "
                     "Please register first — DBS requires LMS registration."}

    return {**base,
        "reply": f"Consultant {user_id} is valid in both DBS and LMS but missing from "
                 "the RDR Sales Consultant list. An investigation ticket has been raised — "
                 "you will be updated shortly."}


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING
# ─────────────────────────────────────────────────────────────────────────────

def route_after_collect_context(state: RDRGraphState) -> str:
    if state.get("conversation_complete"):
        return "__end__"
    if state.get("awaiting"):
        return "human_input"
    return "check_user_status"


def route_after_human_input(state: RDRGraphState) -> str:
    return "collect_context"


def route_after_check_user_status(state: RDRGraphState) -> str:
    if state.get("conversation_complete"):
        return "__end__"
    return "check_lms"


# ─────────────────────────────────────────────────────────────────────────────
# BUILD GRAPH
# ─────────────────────────────────────────────────────────────────────────────

def build_rdr_graph():
    b = StateGraph(RDRGraphState)

    b.add_node("collect_context",   collect_context)
    b.add_node("human_input",       human_input)
    b.add_node("check_user_status", check_user_status)
    b.add_node("check_lms",         check_lms)

    b.add_edge(START, "collect_context")
    b.add_conditional_edges("collect_context",   route_after_collect_context)
    b.add_conditional_edges("human_input",       route_after_human_input)
    b.add_conditional_edges("check_user_status", route_after_check_user_status)
    b.add_edge("check_lms", END)

    cp = get_checkpointer()
    return b.compile(checkpointer=cp, interrupt_before=["human_input"])


_instance = None


def get_rdr_graph():
    global _instance
    if _instance is None:
        _instance = build_rdr_graph()
        logger.info("RDR graph compiled")
    return _instance


__all__ = ["get_rdr_graph", "_init_rdr_state", "RDRGraphState"]

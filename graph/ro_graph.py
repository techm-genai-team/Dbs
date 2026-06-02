"""
graph/ro_graph.py
──────────────────
WHAT THIS FILE DOES:
  Complete RO Investigation LangGraph state machine.

WHAT CHANGED FROM STRANDS:
  DELETED  → ro_agent.py (build_agent, invoke_agent, StreamingCallbackHandler)
  DELETED  → update_context.py (@tool — LLM saved state via tool call)
  DELETED  → finish_conversation.py (@tool — LLM signalled completion)
  ADDED    → StateGraph with 8 nodes
  ADDED    → classify_intent node (Bedrock — Turn 1 ONLY)
  ADDED    → interrupt() for mid-flow user input (WAIT state)
  ADDED    → SQLite checkpoint for state persistence

MID-FLOW USER INPUT — HOW IT WORKS:
  Node hits interrupt() → graph FREEZES → state saved to SQLite
  User replies → Command(resume=user_text) → graph continues from exact point
  LLM NOT called on resume — controller reads state and routes

FLOW:
  START
  → classify_intent   ← BEDROCK (Turn 1 only)
  → collect_context   ← Pure Python
       ↕ [missing dealer/ro] → human_input (WAIT) → back to collect_context
  → query_ro          ← Direct DB call
       ├─ New/Updated → END
       ├─ Closed → human_input (WAIT: "warranty?") → query_warranty
       ├─ found_multiple → human_input (WAIT: "give VIN") → query_ro retry
       └─ not_found → human_input (WAIT: "give date") → query_fuse
  → query_fuse        ← Direct DB call
       ├─ success/error → END
       ├─ not_found → auto retry (extend=True) → query_fuse
       └─ still nothing → query_dynatrace → END
  → query_warranty    ← Direct DB call
       ├─ multiple_claims → human_input (WAIT: "which line?") → handle_line → query_warranty
       ├─ archvd_in=Y → Express Warranty + END
       ├─ archvd_in=N → Manage Warranty + END
       └─ not_in_wrnty_clm → show line details + END
"""
import os, json, logging
from datetime import date
from typing import Annotated, Any, Optional

BEDROCK_MOCK = os.environ.get("BEDROCK_MOCK", "false").lower() == "true"

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from checkpointer import get_checkpointer
from tools.query_dynatrace    import query_dynatrace    as _qdynatrace
from tools.query_fuse_payload  import query_fuse_payload as _qfuse
from tools.query_rpr_ordr     import query_rpr_ordr     as _qro
from tools.query_warranty     import query_warranty     as _qwarranty

load_dotenv()
logger = logging.getLogger(__name__)


# ── STATE ─────────────────────────────────────────────────────────────────────
class ROGraphState(TypedDict):
    session_id:        str
    dealer_number:     Optional[str]
    dealer_normalised: Optional[str]
    dealer_numeric:    Optional[str]
    region:            Optional[str]
    ro_number:         Optional[str]
    vin_last_8:        Optional[str]
    dms_closed:        Optional[bool]
    ro_open_date:      Optional[str]
    messages:          Annotated[list[dict], lambda a, b: a + b]
    current_node:      str
    awaiting:          Optional[str]
    last_user_input:   Optional[str]
    intent:            Optional[str]
    llm_inputs:        Optional[dict]
    ro_result:         Optional[dict]
    fuse_result:       Optional[dict]
    warranty_result:   Optional[dict]
    fuse_extended:     bool
    warranty_line:     Optional[str]
    reply:                 Optional[str]
    needs_clarification:   bool
    clarification_type:    Optional[str]
    escalate_to_helpdesk:  bool
    conversation_complete: bool
    tool_calls_made:       list[str]


def _init_ro_state(session_id, dealer_number=None, region=None,
                   ro_number=None, vin_last_8=None, dms_closed=None):
    return ROGraphState(
        session_id=session_id, dealer_number=dealer_number,
        dealer_normalised=None, dealer_numeric=None, region=region,
        ro_number=ro_number, vin_last_8=vin_last_8, dms_closed=dms_closed,
        ro_open_date=None, messages=[], current_node="classify_intent",
        awaiting=None, last_user_input=None, intent=None, llm_inputs=None,
        ro_result=None, fuse_result=None, warranty_result=None,
        fuse_extended=False, warranty_line=None, reply=None,
        needs_clarification=False, clarification_type=None,
        escalate_to_helpdesk=False, conversation_complete=False,
        tool_calls_made=[],
    )


def _normalise(dealer, region):
    upper = dealer.strip().upper()
    if upper.startswith(("NNA", "NCI")):
        return upper, upper[3:]
    prefix = "NNA" if region.upper() == "US" else "NCI"
    return f"{prefix}{upper}", upper


# ── MOCK ─────────────────────────────────────────────────────────────────────
def _mock_classify(user_msg):
    import re
    msg    = user_msg.upper()
    inputs = {"dealer_number": None, "ro_number": None, "region": "US"}
    m = re.search(r'\b(NNA\d+|NCI\d+)\b', msg)
    if m: inputs["dealer_number"] = m.group(1)
    m = re.search(r'\bRO\d+\b', msg)
    if m: inputs["ro_number"] = m.group(0)
    print(f"  [MOCK] dealer={inputs['dealer_number']} ro={inputs['ro_number']}")
    return {"intent": "RO_INVESTIGATE", "inputs": inputs}


_INTENT_SYSTEM = """You are a DBS support chatbot intent classifier.
Reply ONLY with valid JSON:
{"intent":"RO_INVESTIGATE"|"UNKNOWN","inputs":{"dealer_number":"<or null>","ro_number":"<or null>","region":"<US or CA or null>"},"confidence":0.0-1.0}"""


# ── NODE 0: classify_intent ──────────────────────────────────────────────────
def classify_intent(state: ROGraphState) -> dict:
    user_msg = state.get("last_user_input", "") or ""

    if state.get("dealer_number") and state.get("ro_number"):
        return {"intent": "RO_INVESTIGATE", "current_node": "classify_intent", "llm_inputs": {}}

    if not user_msg:
        return {"intent": "UNKNOWN", "llm_inputs": {}, "current_node": "classify_intent",
                "reply": "Please describe your issue.", "needs_clarification": True}

    if BEDROCK_MOCK:
        parsed = _mock_classify(user_msg)
    else:
        try:
            from services.bedrock_client import invoke_claude
            raw    = invoke_claude(system_prompt=_INTENT_SYSTEM, user_message=user_msg,
                                   max_tokens=256, temperature=0.0)
            raw    = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(raw)
        except Exception as exc:
            logger.warning("Bedrock failed: %s", exc)
            parsed = _mock_classify(user_msg)

    intent = parsed.get("intent", "RO_INVESTIGATE")
    inputs = parsed.get("inputs", {})
    updates: dict = {"intent": intent, "llm_inputs": inputs, "current_node": "classify_intent"}
    for field in ("dealer_number", "ro_number", "region"):
        val = inputs.get(field)
        if val and not state.get(field):
            updates[field] = val
    if intent == "UNKNOWN":
        updates["reply"] = "Please describe the RO issue."
        updates["needs_clarification"] = True
    return updates


# ── NODE 1: collect_context ──────────────────────────────────────────────────
def collect_context(state: ROGraphState) -> dict:
    awaiting   = state.get("awaiting")
    user_input = state.get("last_user_input", "")
    updates: dict = {"current_node": "collect_context", "awaiting": None, "needs_clarification": False}

    if awaiting == "dealer_and_ro" and user_input:
        parts = user_input.strip().split()
        if len(parts) >= 2: updates["dealer_number"] = parts[0]; updates["ro_number"] = parts[-1]
        elif len(parts) == 1: updates["ro_number"] = parts[0]
    elif awaiting == "dealer_number" and user_input:
        updates["dealer_number"] = user_input.strip()
    elif awaiting == "ro_number" and user_input:
        updates["ro_number"] = user_input.strip()
    elif awaiting == "region" and user_input:
        txt = user_input.strip().upper()
        updates["region"] = "CA" if ("CA" in txt or "CANADA" in txt) else "US"

    dealer = updates.get("dealer_number") or state.get("dealer_number")
    region = updates.get("region")        or state.get("region")
    ro     = updates.get("ro_number")     or state.get("ro_number")

    if not dealer and not ro:
        q   = "Please provide Dealer Number and RO Number (e.g. NNA3565 RO002)."
        ans = interrupt({"question": q, "awaiting": "dealer_and_ro"})
        return {**updates, "awaiting": "dealer_and_ro", "last_user_input": ans, "reply": q,
                "needs_clarification": True, "clarification_type": "general"}
    if not dealer:
        q   = "Please provide your Dealer Number (e.g. NNA3565)."
        ans = interrupt({"question": q, "awaiting": "dealer_number"})
        return {**updates, "awaiting": "dealer_number", "last_user_input": ans, "reply": q,
                "needs_clarification": True, "clarification_type": "general"}
    if not ro:
        q   = "Please provide the RO Number (e.g. RO002)."
        ans = interrupt({"question": q, "awaiting": "ro_number"})
        return {**updates, "awaiting": "ro_number", "last_user_input": ans, "reply": q,
                "needs_clarification": True, "clarification_type": "general"}

    upper = dealer.strip().upper()
    if not upper.startswith(("NNA", "NCI")) and not region:
        q   = "US or Canadian dealer? (US or CA)"
        ans = interrupt({"question": q, "awaiting": "region"})
        return {**updates, "awaiting": "region", "last_user_input": ans, "reply": q,
                "needs_clarification": True, "clarification_type": "region"}

    eff_region = region or ("US" if upper.startswith("NNA") else "CA")
    normalised, numeric = _normalise(dealer, eff_region)
    updates.update({"dealer_number": dealer, "dealer_normalised": normalised,
                    "dealer_numeric": numeric, "region": eff_region,
                    "ro_number": ro, "awaiting": None, "needs_clarification": False})
    return updates


# ── NODE 2: human_input ──────────────────────────────────────────────────────
def human_input(state: ROGraphState) -> dict:
    """
    WAIT STATE.
    interrupt() delivers user text.
    Saves specific field. Keeps awaiting for route_after_human_input.
    """
    question  = state.get("reply") or "Please provide the requested information."
    awaiting  = state.get("awaiting")
    user_text = interrupt({"question": question, "awaiting": awaiting})

    # Keep awaiting — route_after_human_input needs it
    result = {"last_user_input": user_text}

    if awaiting == "vin":
        v = user_text.strip().upper()
        bad = {"YES","NO","Y","N","HAAN","NAHI"}
        if v not in bad and not v.startswith("202"):
            result["vin_last_8"] = v

    elif awaiting == "warranty_confirm":
        # No special field — last_user_input is enough
        pass

    elif awaiting == "line_number":
        result["warranty_line"] = user_text.strip()

    elif awaiting == "ro_date":
        result["ro_open_date"] = user_text.strip()

    return result


# ── NODE 3: query_ro ─────────────────────────────────────────────────────────
def query_ro(state: ROGraphState) -> dict:
    dealer = state["dealer_normalised"]
    ro     = state["ro_number"]
    vin    = state.get("vin_last_8")

    # Validate VIN — reject yes/no/dates
    if vin:
        bad = ["yes","no","y","n","haan","nahi"]
        if vin.lower() in bad or vin.upper().startswith("202"):
            vin = None

    result = _qro(dealer_input=state["dealer_number"], ro_number=ro,
                  region=state.get("region"), dealer_normalised=dealer, vin_last_8=vin)

    base = {
        "ro_result":       result,
        "vin_last_8":      vin,
        "current_node":    "query_ro",
        "awaiting":        None,          # ← query_ro always clears awaiting
        "needs_clarification": False,
        "tool_calls_made": state.get("tool_calls_made", []) + ["query_rpr_ordr"],
    }

    s = result.get("status")

    if s == "found_single":
        rs = result.get("ro_status", "Unknown")
        if result.get("is_closed"):
            return {**base,
                "reply":               f"RO {ro} found — '{rs}' for {dealer}. Warranty check karein? (yes/no)",
                "awaiting":            "warranty_confirm",
                "needs_clarification": True,
                "clarification_type":  "general"}
        return {**base,
            "reply":                f"RO {ro} is '{rs}' — in progress. No action needed.",
            "conversation_complete": True}

    if s == "found_multiple":
        return {**base,
            "reply":               f"Multiple ROs found for {ro}. VIN last 6-8 digits do.",
            "awaiting":            "vin",
            "needs_clarification": True,
            "clarification_type":  "vin"}

    if s == "not_found":
        return {**base,
            "reply":               f"RO {ro} not found for {dealer}. Date do (YYYY-MM-DD).",
            "awaiting":            "ro_date",
            "needs_clarification": True,
            "clarification_type":  "general"}

    return {**base, "reply": "Error querying RO.",
            "escalate_to_helpdesk": True, "conversation_complete": True}


# ── NODE 4: query_fuse ────────────────────────────────────────────────────────
def query_fuse(state: ROGraphState) -> dict:
    ro_date = state.get("ro_open_date")
    extend  = state.get("fuse_extended", False)
    dn      = state.get("dealer_numeric") or (state.get("dealer_normalised") or "")[3:]

    result = _qfuse(dealer_numeric=dn, ro_number=state["ro_number"],
                    start_time=ro_date or date.today().isoformat(),
                    end_time  =ro_date or date.today().isoformat(),
                    extend_window=extend)
    base = {"fuse_result": result, "ro_open_date": ro_date, "current_node": "query_fuse",
            "awaiting": None,
            "tool_calls_made": state.get("tool_calls_made", []) + ["query_fuse_payload"]}

    s = result.get("status")
    if s == "dms_error":
        return {**base, "reply": f"DMS Error: {result.get('error_text','Unknown')}.",
                "conversation_complete": True}
    if s == "success":
        return {**base, "reply": f"FUSE shows RO status: {result.get('status_text','Unknown')}.",
                "conversation_complete": True}
    if s == "app_error":
        return {**base, "reply": "App error. Escalating.",
                "escalate_to_helpdesk": True, "conversation_complete": True}
    if s == "not_found" and not extend:
        return {**base, "fuse_extended": True}
    return {**base, "current_node": "query_dynatrace"}


# ── NODE 5: query_dynatrace ───────────────────────────────────────────────────
def query_dynatrace_node(state: ROGraphState) -> dict:
    result = _qdynatrace()
    return {"current_node": "query_dynatrace", "conversation_complete": True,
            "tool_calls_made": state.get("tool_calls_made", []) + ["query_dynatrace"],
            "reply": result.get("message", "DMS did not send this RO.")}


# ── NODE 6: query_warranty ────────────────────────────────────────────────────
def query_warranty_node(state: ROGraphState) -> dict:
    if state.get("awaiting") == "warranty_confirm":
        ui = (state.get("last_user_input") or "").lower()
        if not ("yes" in ui or "haan" in ui or ui.strip() == "y"):
            return {"reply": "No warranty check performed.",
                    "conversation_complete": True, "awaiting": None}
        # Clear vin from state so warranty query doesn't confuse it
        # (vin_last_8 was set for query_ro, not query_warranty)

    wl     = state.get("warranty_line")
    result = _qwarranty(dealer_input=state["dealer_normalised"],
                        ro_number=state["ro_number"],
                        vin=state.get("vin_last_8"), line_number=wl,
                        multiple_claims_confirmed=wl is not None)
    base = {"warranty_result": result, "current_node": "query_warranty",
            "awaiting": None,
            "tool_calls_made": state.get("tool_calls_made", []) + ["query_warranty"]}

    ws = result.get("status")
    if ws == "multiple_claims":
        lines = ", ".join(result.get("available_lines", []))
        return {**base, "reply": f"Multiple claims (lines: {lines}). Konsa line?",
                "awaiting": "line_number", "needs_clarification": True,
                "clarification_type": "line_number"}
    if ws == "archive_checked":
        a = result.get("archvd_in", "")
        if a == "Y": return {**base, "reply": "Use Express Warranty Search.", "conversation_complete": True}
        if a == "N": return {**base, "reply": "Use Manage Warranty Screen.",  "conversation_complete": True}
        return {**base, "reply": f"Archive '{a}' unexpected. Create incident.",
                "escalate_to_helpdesk": True, "conversation_complete": True}
    if ws == "not_in_wrnty_clm":
        lr = result.get("line_rows", [])
        d  = "; ".join(f"Line {r.get('rpr_ordr_ln_nb')} Pay:{r.get('pay_typ_cd')}" for r in lr)
        return {**base, "reply": f"Not in warranty table. Lines: {d}.", "conversation_complete": True}
    return {**base, "reply": "Unexpected warranty result.",
            "escalate_to_helpdesk": True, "conversation_complete": True}


# ── NODE 7: handle_warranty_line_input ────────────────────────────────────────
def handle_warranty_line_input(state: ROGraphState) -> dict:
    return {"warranty_line": (state.get("last_user_input") or "").strip(),
            "awaiting": None, "current_node": "query_warranty"}


# ── ROUTING ───────────────────────────────────────────────────────────────────
def route_after_classify_intent(state):
    return "human_input" if state.get("intent") == "UNKNOWN" else "collect_context"

def route_after_collect_context(state):
    return "human_input" if state.get("awaiting") else "query_ro"

def route_after_human_input(state):
    a = state.get("awaiting")
    if a in ("dealer_and_ro", "dealer_number", "ro_number", "region"):
        return "collect_context"
    if a == "vin":              return "query_ro"
    if a == "ro_date":          return "query_fuse"
    if a == "warranty_confirm": return "query_warranty"
    if a == "line_number":      return "handle_warranty_line_input"
    return "collect_context"

def route_after_query_ro(state):
    if state.get("conversation_complete"): return "__end__"
    if state.get("awaiting"):              return "human_input"
    return "__end__"

def route_after_query_fuse(state):
    if state.get("conversation_complete"):                return "__end__"
    if state.get("current_node") == "query_dynatrace":    return "query_dynatrace"
    if state.get("fuse_extended"):                        return "query_dynatrace"
    return "query_fuse"

def route_after_query_warranty(state):
    if state.get("conversation_complete"): return "__end__"
    if state.get("awaiting"):              return "human_input"
    return "__end__"


# ── BUILD ─────────────────────────────────────────────────────────────────────
def build_ro_graph():
    b = StateGraph(ROGraphState)
    b.add_node("classify_intent",            classify_intent)
    b.add_node("collect_context",            collect_context)
    b.add_node("human_input",                human_input)
    b.add_node("query_ro",                   query_ro)
    b.add_node("query_fuse",                 query_fuse)
    b.add_node("query_dynatrace",            query_dynatrace_node)
    b.add_node("query_warranty",             query_warranty_node)
    b.add_node("handle_warranty_line_input", handle_warranty_line_input)
    b.add_edge(START, "classify_intent")
    b.add_conditional_edges("classify_intent",            route_after_classify_intent)
    b.add_conditional_edges("collect_context",            route_after_collect_context)
    b.add_conditional_edges("human_input",                route_after_human_input)
    b.add_conditional_edges("query_ro",                   route_after_query_ro)
    b.add_conditional_edges("query_fuse",                 route_after_query_fuse)
    b.add_conditional_edges("query_warranty",             route_after_query_warranty)
    b.add_edge("handle_warranty_line_input",              "query_warranty")
    b.add_edge("query_dynatrace",                         END)
    return b.compile(checkpointer=get_checkpointer())

_instance = None
def get_ro_graph():
    global _instance
    if _instance is None:
        _instance = build_ro_graph()
    return _instance

__all__ = ["get_ro_graph", "_init_ro_state", "ROGraphState", "BEDROCK_MOCK"]
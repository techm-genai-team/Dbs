"""
tools/query_dynatrace.py
─────────────────────────
WHAT THIS FILE DOES:
  Called after FUSE log also returns not_found.
  Checks Dynatrace pipeline for backlogged processes.
  Returns mock when no DYNATRACE_URL set (local testing).

BEFORE (Strands): @tool — LLM called this
AFTER (LangGraph): query_dynatrace NODE calls directly
"""
import logging
import os

logger = logging.getLogger(__name__)


def query_dynatrace() -> dict:
    url = os.environ.get("DYNATRACE_URL", "")

    if not url:
        # Local mock — simulate pipeline empty
        logger.info("query_dynatrace | MOCK mode")
        return {
            "status":  0,
            "message": "DMS did not send this RO. Please check the RO in your DMS and resubmit.",
        }

    try:
        import requests
        resp   = requests.get(url, timeout=5)
        resp.raise_for_status()
        result = str(resp.json().get("result", ""))
    except Exception as exc:
        logger.warning("query_dynatrace call failed: %s", exc)
        return {"status": 0, "message": "DMS did not send this RO. Please check the RO in your DMS."}

    if result == "0":
        return {"status": 0,   "message": "DMS did not send this RO. Check RO in your DMS."}
    if result == "500":
        return {"status": 500, "message": "Records being processed in pipeline. Try again later."}
    return {"status": -1, "message": "Unexpected Dynatrace response."}

"""
tools/query_rpr_ordr.py
────────────────────────
WHAT THIS FILE DOES:
  Queries rpr_ordr table for a Repair Order.
  Returns: found_single / found_multiple / not_found

BEFORE (Strands):
  Had @tool decorator — LLM decided when to call this
  LLM passed params, LLM read results

AFTER (LangGraph):
  @tool removed — query_ro NODE calls this directly
  Node reads result and decides next step (no LLM)
  SQLite compatible: LIKE instead of ILIKE
"""
import logging
from typing import Optional
from sqlalchemy import text
from db import get_db_session, DB_SCHEMA

logger = logging.getLogger(__name__)
_STATUS_MAP = {"N": "New", "U": "Updated", "C": "Closed"}


def _normalize_dealer(dealer: str, region: Optional[str]):
    dealer = dealer.strip()
    if dealer.upper().startswith(("NNA", "NCI")):
        return dealer.upper(), dealer[3:]
    prefix = "NCI" if (region or "US").upper() == "CA" else "NNA"
    return f"{prefix}{dealer}", dealer


def query_rpr_ordr(
    dealer_input: str,
    ro_number: str,
    region: Optional[str] = None,
    dealer_normalised: Optional[str] = None,
    vin_last_8: Optional[str] = None,
) -> dict:
    if dealer_normalised:
        prefixed = dealer_normalised.upper().strip()
        numeric  = prefixed[3:] if prefixed[:3] in ("NNA", "NCI") else prefixed
    else:
        prefixed, numeric = _normalize_dealer(dealer_input, region)

    logger.info("query_rpr_ordr | dealer=%s ro=%s vin=%s", prefixed, ro_number, vin_last_8)

    # SQLite: no schema prefix, LIKE instead of ILIKE
    tbl        = f"{DB_SCHEMA}.rpr_ordr" if DB_SCHEMA else "rpr_ordr"
    vin_clause = "AND TRIM(vin_id) LIKE :vin_filter" if (vin_last_8 and vin_last_8.strip()) else ""

    try:
        with get_db_session() as session:
            params: dict = {"dealer": prefixed, "ro": ro_number}
            if vin_last_8 and vin_last_8.strip():
                params["vin_filter"] = f"%{vin_last_8.strip()}"

            rows = [dict(r) for r in session.execute(text(f"""
                SELECT rfrnc_dlr_nb, rpr_ordr_nb, vin_id, rpr_ordr_sts_cd
                FROM {tbl}
                WHERE rfrnc_dlr_nb = :dealer
                  AND rpr_ordr_nb  = :ro
                  {vin_clause}
            """), params).mappings().all()]

    except Exception as exc:
        logger.exception("query_rpr_ordr DB error")
        return {"error": str(exc), "status": "error"}

    if not rows:
        return {
            "status":         "not_found",
            "dealer":         prefixed,
            "dealer_numeric": numeric,
            "ro_number":      ro_number,
            "rows":           [],
        }

    if len(rows) == 1:
        sts_cd    = rows[0].get("rpr_ordr_sts_cd", "")
        ro_status = _STATUS_MAP.get(sts_cd, f"Unknown({sts_cd})")
        return {
            "status":    "found_single",
            "dealer":    prefixed,
            "ro_number": ro_number,
            "ro_status": ro_status,
            "is_closed": sts_cd == "C",
            "rows":      rows,
        }

    return {
        "status":    "found_multiple",
        "dealer":    prefixed,
        "ro_number": ro_number,
        "row_count": len(rows),
        "rows":      rows,
    }

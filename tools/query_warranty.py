"""
tools/query_warranty.py
────────────────────────
WHAT THIS FILE DOES:
  Queries wrnty_clm for warranty claim on a Closed RO.
  If not found, queries rpr_ordr_ln for line details.
  Returns: archive_checked / multiple_claims / not_in_wrnty_clm

BEFORE (Strands): @tool — LLM called this, LLM read result
AFTER (LangGraph): query_warranty NODE calls directly
SQLite changes: removed ::text casts, LIKE instead of ILIKE
"""
import logging
from typing import Optional
from sqlalchemy import text
from db import get_db_session, DB_SCHEMA

logger = logging.getLogger(__name__)


def query_warranty(
    dealer_input: str,
    ro_number: str,
    vin: Optional[str] = None,
    line_number: Optional[str] = None,
    multiple_claims_confirmed: bool = False,
) -> dict:
    logger.info("query_warranty | dealer=%s ro=%s line=%s", dealer_input, ro_number, line_number)

    try:
        wrnty_rows = _query_wrnty_clm(dealer_input, ro_number, line_number)
    except Exception as exc:
        logger.exception("wrnty_clm error")
        return {"error": str(exc), "status": "error"}

    # Not in warranty table → check RO line details
    if not wrnty_rows:
        try:
            line_rows = _query_rpr_ordr_ln(dealer_input, ro_number, vin)
        except Exception as exc:
            logger.exception("rpr_ordr_ln error")
            return {"error": str(exc), "status": "error"}
        return {"status": "not_in_wrnty_clm", "line_rows": line_rows}

    # Multiple claims — need line number
    if len(wrnty_rows) > 1 and not multiple_claims_confirmed:
        lines = [str(r.get("rpr_ordr_ln_nb", "?")) for r in wrnty_rows]
        return {"status": "multiple_claims", "available_lines": lines, "rows": wrnty_rows}

    # Single claim — check archvd_in
    row       = wrnty_rows[0]
    archvd_in = (row.get("archvd_in") or "").strip().upper()
    return {"status": "archive_checked", "archvd_in": archvd_in, "row": row}


def _query_wrnty_clm(dealer: str, ro: str, line: Optional[str]) -> list[dict]:
    tbl  = f"{DB_SCHEMA}.wrnty_clm" if DB_SCHEMA else "wrnty_clm"
    lc   = "AND rpr_ordr_ln_nb = :line" if line else ""
    with get_db_session() as s:
        p = {"dealer": dealer, "ro": ro}
        if line:
            p["line"] = line
        return [dict(r) for r in s.execute(text(f"""
            SELECT archvd_in, rpr_ordr_ln_nb
            FROM {tbl}
            WHERE rfrnc_dlr_nb = :dealer AND rpr_ordr_nb = :ro {lc}
        """), p).mappings().all()]


def _query_rpr_ordr_ln(dealer: str, ro: str, vin: Optional[str]) -> list[dict]:
    ro_tbl  = f"{DB_SCHEMA}.rpr_ordr"    if DB_SCHEMA else "rpr_ordr"
    rol_tbl = f"{DB_SCHEMA}.rpr_ordr_ln" if DB_SCHEMA else "rpr_ordr_ln"
    vc = "AND TRIM(ro.vin_id) LIKE :vin" if vin else ""
    with get_db_session() as s:
        p = {"dealer": dealer, "ro": ro}
        if vin:
            p["vin"] = f"%{vin}"
        return [dict(r) for r in s.execute(text(f"""
            SELECT ro.rfrnc_dlr_nb, ro.rpr_ordr_nb, rol.rpr_ordr_ln_nb,
                   ro.vin_id, ro.rpr_ordr_sts_cd,
                   ro.rpr_ordr_opn_dt, ro.rpr_ordr_clos_dt, rol.pay_typ_cd
            FROM {ro_tbl} ro
            JOIN {rol_tbl} rol ON ro.rpr_ordr_ky = rol.rpr_ordr_ky
            WHERE ro.rpr_ordr_nb = :ro AND ro.rfrnc_dlr_nb = :dealer
              AND ro.rpr_ordr_typ_cd = 'R' {vc}
        """), p).mappings().all()]

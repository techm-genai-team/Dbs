"""
tools/query_fuse_payload.py
────────────────────────────
WHAT THIS FILE DOES:
  Queries fuse_pyld_log after RO not found in rpr_ordr.
  Returns: success / dms_error / app_error / not_found / dms_did_not_send

BEFORE (Strands): @tool — LLM called this, LLM read result
AFTER (LangGraph): query_fuse NODE calls directly, node decides routing
SQLite changes: LIKE instead of ~, no ::text cast, no ILIKE
"""
import logging
import re
from datetime import datetime, timedelta
from sqlalchemy import text
from db import get_db_session, DB_SCHEMA

logger    = logging.getLogger(__name__)
_ST_RE    = re.compile(r"<(?:[\w]+:)?StatusText>([^<]+)</(?:[\w]+:)?StatusText>", re.I)
_ERR_RE   = re.compile(r"<(?:[\w]+:)?Description>([^<]+)</(?:[\w]+:)?Description>", re.I)


def _parse_dt(s: str) -> datetime:
    s = s.strip()
    if "T" not in s and " " not in s:
        s += "T00:00:00"
    return datetime.fromisoformat(s)


def query_fuse_payload(
    dealer_numeric: str,
    ro_number: str,
    start_time: str,
    end_time: str,
    extend_window: bool = False,
) -> dict:
    try:
        s_dt = _parse_dt(start_time)
        e_dt = _parse_dt(end_time)
    except ValueError as exc:
        return {"error": str(exc), "status": "error"}

    if extend_window:
        s_dt -= timedelta(days=5)
        e_dt += timedelta(days=5)

    logger.info("query_fuse_payload | dealer=%s ro=%s extend=%s", dealer_numeric, ro_number, extend_window)

    tbl = f"{DB_SCHEMA}.fuse_pyld_log" if DB_SCHEMA else "fuse_pyld_log"

    try:
        with get_db_session() as session:
            # SQLite: LIKE instead of ~ (postgres regex)
            rows = [dict(r) for r in session.execute(text(f"""
                SELECT rfrnc_dlr_nb, rqst_tm, rqst_msg_tx, err_msg_tx, sts_cd
                FROM {tbl}
                WHERE dlr_nb    = :dlr
                  AND trnscn_nm = 'ROIntegrationService'
                  AND rqst_tm   BETWEEN :s AND :e
                  AND rqst_msg_tx LIKE :pat
                ORDER BY rqst_tm DESC
            """), {
                "dlr": dealer_numeric,
                "s":   s_dt.isoformat(),
                "e":   e_dt.isoformat(),
                "pat": f"%DocumentID>{ro_number}<%",
            }).mappings().all()]

    except Exception as exc:
        logger.exception("query_fuse_payload DB error")
        return {"error": str(exc), "status": "error"}

    if not rows:
        if not extend_window:
            return {"status": "not_found"}
        return {"status": "dms_did_not_send"}

    latest = rows[0]
    sts_cd = int(latest.get("sts_cd", -1))

    if sts_cd == 0:
        m = _ERR_RE.search(latest.get("err_msg_tx", ""))
        return {"status": "dms_error", "sts_cd": 0,
                "error_text": m.group(1).strip() if m else "Unknown DMS error"}
    if sts_cd == 1:
        m = _ST_RE.search(latest.get("rqst_msg_tx", ""))
        return {"status": "success", "sts_cd": 1,
                "status_text": m.group(1).strip() if m else "Unknown"}
    if sts_cd == 3:
        return {"status": "app_error", "sts_cd": 3}
    return {"status": "unknown", "sts_cd": sts_cd}

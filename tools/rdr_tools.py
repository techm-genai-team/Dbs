"""
tools/rdr_tools.py
───────────────────
WHAT THIS FILE DOES:
  query_dlr_usr_prfl → checks DBS user active/inactive
  query_dlr_usr      → checks LMS registration

BEFORE (Strands): @tool — LLM called these
AFTER (LangGraph): check_user_status and check_lms NODES call directly
"""
import logging
from sqlalchemy import text
from db import get_db_session, DB_SCHEMA

logger = logging.getLogger(__name__)


def query_dlr_usr_prfl(dealer_number: str, usr_id: str) -> dict:
    tbl = f"{DB_SCHEMA}.dlr_usr_prfl" if DB_SCHEMA else "dlr_usr_prfl"
    try:
        with get_db_session() as session:
            result = session.execute(text(f"""
                SELECT usr_id, sts_in FROM {tbl}
                WHERE usr_id = :usr_id AND rfrnc_dlr_nb = :dealer
            """), {"usr_id": usr_id, "dealer": dealer_number}).fetchone()

            if not result:
                return {"found": False}
            return {
                "found":       True,
                "status_code": result[1],
                "is_active":   result[1] == "A",
            }
    except Exception as exc:
        logger.error("query_dlr_usr_prfl error: %s", exc)
        return {"error": "Database error occurred."}


def query_dlr_usr(usr_id: str) -> dict:
    tbl = f"{DB_SCHEMA}.dlr_usr" if DB_SCHEMA else "dlr_usr"
    try:
        with get_db_session() as session:
            result = session.execute(
                text(f"SELECT 1 FROM {tbl} WHERE usr_id = :usr_id"),
                {"usr_id": usr_id}
            ).fetchone()
            return {"registered_in_lms": result is not None}
    except Exception as exc:
        logger.error("query_dlr_usr error: %s", exc)
        return {"error": "Database error occurred."}

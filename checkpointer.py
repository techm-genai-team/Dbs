"""
checkpointer.py
────────────────
SQLite checkpointer — file based.
State survives restarts.
checkpoints.db auto-created in project root.
"""
import logging
import os

logger = logging.getLogger(__name__)

_checkpointer_instance = None


def get_checkpointer():
    global _checkpointer_instance

    if _checkpointer_instance is not None:
        return _checkpointer_instance

    path = os.environ.get("SQLITE_CHECKPOINT_PATH", "./checkpoints.db")

    # ── SqliteSaver — file based, survives restart ────────────────────────
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3
        conn = sqlite3.connect(path, check_same_thread=False)
        _checkpointer_instance = SqliteSaver(conn)
        print(f"  ✓ Checkpoint → SQLite ({path})")
        return _checkpointer_instance
    except Exception as exc:
        logger.warning("SqliteSaver failed: %s — trying MemorySaver", exc)

    # ── MemorySaver fallback ──────────────────────────────────────────────
    try:
        from langgraph.checkpoint.memory import MemorySaver
        _checkpointer_instance = MemorySaver()
        print(f"  ✓ Checkpoint → MemorySaver (in-memory)")
        return _checkpointer_instance
    except Exception as exc:
        raise RuntimeError(f"No checkpointer available: {exc}")
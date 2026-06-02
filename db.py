"""
db.py
──────
WHAT THIS FILE DOES:
  - Creates local SQLite database (local_test.db)
  - Creates all tables needed by the tools
  - Inserts seed data for testing all scenarios
  - Provides get_db_session() used by every tool

BEFORE (Strands):
  Connected to PostgreSQL — needed real DB credentials
  No local testing possible without DB access

AFTER (LangGraph):
  SQLite — no credentials, no server, file auto-created
  Same get_db_session() API — all tools unchanged
"""
"""
db.py - Local SQLite DB
Complete demo scenarios with VIN + date + warranty
"""
import logging
import os
from contextlib import contextmanager
from typing import Generator

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger      = logging.getLogger(__name__)
SQLITE_PATH = os.environ.get("SQLITE_DB_PATH", "./local_test.db")
DB_SCHEMA   = ""

engine = create_engine(
    f"sqlite:///{SQLITE_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_is_healthy() -> bool:
    try:
        with get_db_session() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("DB health: %s", exc)
        return False


def setup_local_db() -> None:
    ddl = [
        """CREATE TABLE IF NOT EXISTS rpr_ordr (
            rpr_ordr_ky      INTEGER PRIMARY KEY AUTOINCREMENT,
            rfrnc_dlr_nb     TEXT NOT NULL,
            rpr_ordr_nb      TEXT NOT NULL,
            vin_id           TEXT,
            rpr_ordr_sts_cd  TEXT DEFAULT 'N',
            rpr_ordr_typ_cd  TEXT DEFAULT 'R',
            rpr_ordr_opn_dt  TEXT,
            rpr_ordr_clos_dt TEXT)""",

        """CREATE TABLE IF NOT EXISTS rpr_ordr_ln (
            rpr_ordr_ln_ky INTEGER PRIMARY KEY AUTOINCREMENT,
            rpr_ordr_ky    INTEGER,
            rpr_ordr_ln_nb TEXT,
            pay_typ_cd     TEXT)""",

        """CREATE TABLE IF NOT EXISTS fuse_pyld_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            dlr_nb       TEXT,
            rfrnc_dlr_nb TEXT,
            trnscn_nm    TEXT,
            rqst_tm      TEXT,
            rqst_msg_tx  TEXT,
            err_msg_tx   TEXT,
            sts_cd       INTEGER DEFAULT 1)""",

        """CREATE TABLE IF NOT EXISTS wrnty_clm (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            rfrnc_dlr_nb   TEXT,
            rpr_ordr_nb    TEXT,
            rpr_ordr_ln_nb TEXT,
            archvd_in      TEXT DEFAULT 'N')""",

        """CREATE TABLE IF NOT EXISTS dlr_usr_prfl (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rfrnc_dlr_nb TEXT,
            usr_id       TEXT,
            sts_in       TEXT DEFAULT 'A')""",

        """CREATE TABLE IF NOT EXISTS dlr_usr (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            usr_id TEXT UNIQUE)""",
    ]

    seed = [
        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 1: RO001 → New (1 turn complete)
        # Flow: query_ro → found New → END
        # Test: Pre-fill NNA3565 / RO001
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd,rpr_ordr_opn_dt) VALUES('NNA3565','RO001','VIN001','N','R','2026-04-01')",

        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 2: RO002 → Closed → Warranty → archvd_in=N
        # Flow: query_ro → Closed → ask "warranty?" → yes
        #       → query_warranty → single claim → archvd_in=N
        #       → "Use Manage Warranty Screen"
        # Test: Pre-fill NNA3565 / RO002
        # Turns: Turn1=found Closed, Turn2=yes
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd,rpr_ordr_opn_dt,rpr_ordr_clos_dt) VALUES('NNA3565','RO002','VIN002','C','R','2026-03-01','2026-03-20')",
        "INSERT OR IGNORE INTO rpr_ordr_ln(rpr_ordr_ky,rpr_ordr_ln_nb,pay_typ_cd) VALUES(2,'1','W')",
        "INSERT OR IGNORE INTO wrnty_clm(rfrnc_dlr_nb,rpr_ordr_nb,rpr_ordr_ln_nb,archvd_in) VALUES('NNA3565','RO002','1','N')",

        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 3: RO003 → Multiple VINs → user gives VIN → found
        # Flow: query_ro → found_multiple → ask VIN
        #       → handle_vin_input → query_ro again with VIN
        #       → found single New → END
        # Test: Pre-fill NNA3565 / RO003
        # Turns: Turn1=multiple found, Turn2=AAAAA
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd) VALUES('NNA3565','RO003','AAAAA','N','R')",
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd) VALUES('NNA3565','RO003','BBBBB','N','R')",

        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 4: RO004 → Multiple VINs → VIN → Closed → Warranty
        # Flow: query_ro → found_multiple → ask VIN
        #       → handle_vin_input → query_ro again with VIN
        #       → found single Closed → ask warranty
        #       → yes → query_warranty → archvd_in=Y
        #       → "Use Express Warranty Search"
        # Test: Pre-fill NNA3565 / RO004
        # Turns: Turn1=multiple, Turn2=VIN004A, Turn3=yes
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd,rpr_ordr_clos_dt) VALUES('NNA3565','RO004','VIN004A','C','R','2026-04-10')",
        "INSERT OR IGNORE INTO rpr_ordr(rfrnc_dlr_nb,rpr_ordr_nb,vin_id,rpr_ordr_sts_cd,rpr_ordr_typ_cd) VALUES('NNA3565','RO004','VIN004B','N','R')",
        "INSERT OR IGNORE INTO wrnty_clm(rfrnc_dlr_nb,rpr_ordr_nb,rpr_ordr_ln_nb,archvd_in) VALUES('NNA3565','RO004','1','Y')",

        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 5: RO999 → Not found → FUSE date → Dynatrace
        # Flow: query_ro → not_found → ask date
        #       → query_fuse(date) → not_found → extend → not_found
        #       → query_dynatrace → "DMS did not send"
        # Test: Pre-fill NNA3565 / RO999
        # Turns: Turn1=not found, Turn2=2026-04-10
        # ══════════════════════════════════════════════════════════════════
        # (no rpr_ordr row for RO999 — intentionally missing)

        # ══════════════════════════════════════════════════════════════════
        # SCENARIO 6: RO005 → Not found → FUSE → sts_cd=1 SUCCESS
        # Flow: query_ro → not_found → ask date
        #       → query_fuse → found sts_cd=1 → "FUSE shows Closed"
        # Test: Pre-fill NNA3565 / RO005
        # Turns: Turn1=not found, Turn2=2026-04-10
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO fuse_pyld_log(dlr_nb,rfrnc_dlr_nb,trnscn_nm,rqst_tm,rqst_msg_tx,sts_cd) VALUES('3565','NNA3565','ROIntegrationService','2026-04-10T10:00:00','<DocumentID>RO005</DocumentID><star:StatusText>Closed</star:StatusText>',1)",

        # ══════════════════════════════════════════════════════════════════
        # RDR SCENARIOS
        # ══════════════════════════════════════════════════════════════════
        "INSERT OR IGNORE INTO dlr_usr_prfl(rfrnc_dlr_nb,usr_id,sts_in) VALUES('NNA3565','XD389786','A')",  # Active + in LMS
        "INSERT OR IGNORE INTO dlr_usr_prfl(rfrnc_dlr_nb,usr_id,sts_in) VALUES('NNA3565','XD000000','I')",  # Inactive
        "INSERT OR IGNORE INTO dlr_usr_prfl(rfrnc_dlr_nb,usr_id,sts_in) VALUES('NNA3565','XD000001','A')",  # Active, no LMS
        "INSERT OR IGNORE INTO dlr_usr(usr_id) VALUES('XD389786')",
    ]

    with engine.begin() as conn:
        for s in ddl:
            conn.execute(text(s))
        for s in seed:
            conn.execute(text(s))

    print(f"  ✓ DB ready → {SQLITE_PATH}")
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  RO SCENARIOS                                           │")
    print("  ├──────┬──────────────────────────────────────────────────┤")
    print("  │ RO001│ New → 1 turn done                                │")
    print("  │ RO002│ Closed → yes → Manage Warranty Screen            │")
    print("  │ RO003│ Multiple → VIN: AAAAA or BBBBB → New            │")
    print("  │ RO004│ Multiple → VIN: VIN004A → Closed → Express Wrnty│")
    print("  │ RO005│ Not found → date → FUSE success (sts_cd=1)      │")
    print("  │ RO999│ Not found → date → FUSE miss → Dynatrace        │")
    print("  ├──────┴──────────────────────────────────────────────────┤")
    print("  │  RDR SCENARIOS                                          │")
    print("  ├──────┬──────────────────────────────────────────────────┤")
    print("  │ XD389786│ Active + LMS → ticket raised                 │")
    print("  │ XD000000│ Inactive → update NNAnet                     │")
    print("  │ XD000001│ Active, no LMS → register first              │")
    print("  └──────┴──────────────────────────────────────────────────┘")
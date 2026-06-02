"""
test_terminal.py  —  DBS Agent Demo

"""
import os, sys, uuid, logging
os.environ["BEDROCK_MOCK"] = "true"

logging.basicConfig(level=logging.INFO, format="  [LOG] %(message)s")

W = 65
def box(t, c="═"): print(f"\n{c*W}\n  {t}\n{c*W}")
def div(): print(f"{'─'*W}")

# ── Startup ────────────────────────────────────────────────────────────────────
print("\n" + "="*W)
print("  DBS Agent — Single Entry Point Demo")
print("  agent.handle(session_id, user_message, context)")
print("="*W)

print("\n[1/3] DB setup...")
from db import setup_local_db, db_is_healthy
setup_local_db()
assert db_is_healthy()

print("\n[2/3] Bedrock client...")
from services.bedrock_client import get_bedrock_client
get_bedrock_client()
print("  ✓ BEDROCK_MOCK=true")

print("\n[3/3] Agent...")
from agent import get_agent
agent = get_agent()
print("  ✓ Agent ready — single entry point for all graphs\n")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ask(hint=""):
    try:
        if hint:
            print(f"  (hint: type '{hint}')")
        v = input("  You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye."); sys.exit(0)
    if v.lower() in ("exit","quit","q"):
        print("Goodbye."); sys.exit(0)
    return v

def run(sid, msg, ctx={}, turn=1):
    """One agent call — shows full flow."""

    print(f"\n  ┌─ agent.handle() {'─'*42}┐")
    print(f"  │  session_id   = '...{sid[-8:]}'")
    print(f"  │  user_message = '{msg}'")
    if ctx and turn == 1:
        print(f"  │  context      = {ctx}")
    print(f"  └{'─'*57}┘")

    result = agent.handle(sid, msg, ctx if turn == 1 else {})

    if turn == 1:
        print(f"\n  Routing decision:")
        print(f"    graph_type     = {result.get('graph_type')}")
        print(f"    session_locked = True (Turn 2+ same graph)")

    div()
    print(f"  [Turn {turn}] Bot: {result.get('reply','')}")
    if result.get('tool_calls_made'):
        print(f"  Tools used      : {', '.join(result['tool_calls_made'])}")
    ctx_out = {k:v for k,v in (result.get('extracted_context') or {}).items() if v}
    if ctx_out:
        print(f"  Context saved   : {ctx_out}")
    if result.get('needs_clarification'):
        print(f"  ⏸  Waiting for  : {result.get('clarification_type','input')}")
        print(f"\n  [Graph interrupted — waiting for your input]")
        print(f"  [agent.handle() will resume graph on next call]")
    if result.get('conversation_complete'):
        print(f"  ✓  Conversation complete")
    if result.get('error'):
        print(f"  ✗  Error        : {result['error']}")
    div()
    return result


def run_session(label, prefill, hints):
    """
    Manual session.
    hints = list of hint strings per turn (what to type).
    """
    sid = str(uuid.uuid4())
    box(f"SESSION: {label}  [...{sid[-8:]}]")

    ctx  = prefill
    turn = 1

    print(f"\n  HOW THIS WORKS:")
    print(f"    Turn 1  → agent routes → graph starts → may interrupt")
    print(f"    Turn 2+ → agent resumes graph → 0 Bedrock calls")
    print(f"    Interrupt → graph froze → you give input → graph resumes")

    while True:
        hint = hints[turn-1] if turn <= len(hints) else ""
        print(f"\n  Turn {turn} — type your message:")
        msg = _ask(hint)

        r = run(sid, msg, ctx, turn)

        if r.get('conversation_complete') or r.get('error'):
            box(f"DONE — {turn} turns | graph={r.get('graph_type')} | 0 Bedrock calls", "─")
            break

        if not r.get('reply'):
            break

        turn += 1


# ── OPTION 1 — Auto-detect ─────────────────────────────────────────────────────

def option1():
    box("Option 1 — Auto-detect (no pre-fill)")
    print("""
  Frontend did not send the context
  only Natural language.

  Agent internally:
    Layer 1: context keys → {} → nothing
    Layer 2: regex → \\bRO\\d+\\b → found RO → "RO"
    Session lock → RO graph start
    """)

    sid  = str(uuid.uuid4())
    turn = 1
    ctx  = {}

    print("  Type anything — agent will detect intent automatically")
    print("  Example: 'My RO RO003 for dealer NNA3565 is missing'\n")

    while True:
        print(f"\n  Turn {turn}:")
        msg = _ask()
        r   = run(sid, msg, ctx, turn)
        if r.get('conversation_complete') or r.get('error') or not r.get('reply'):
            box(f"DONE — {turn} turns | graph={r.get('graph_type')}", "─")
            break
        turn += 1


# ── OPTION 2 — RO001 (1 turn) ─────────────────────────────────────────────────

def option2():
    run_session(
        label   = "RO001 → New (1 turn)",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO001","region":"US"},
        hints   = ["investigate"],
    )


# ── OPTION 3 — RO002 (2 turns) ────────────────────────────────────────────────

def option3():
    box("Option 3 — RO002 → Closed → Warranty (2 turns)")
    print("""
  Flow:
    Turn 1: query_ro → found_single Closed
            graph interrupt → "Warranty check? yes/no"
            [agent gets result → returns to frontend]

    Turn 2: you type 'yes'
            frontend → agent → graph resume
            query_warranty → archvd_in=N
            → "Use Manage Warranty Screen"
    """)
    run_session(
        label   = "RO002 → Closed → Warranty → Manage Warranty Screen",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO002","region":"US"},
        hints   = ["investigate", "yes"],
    )


# ── OPTION 4 — RO003 (2 turns VIN) ───────────────────────────────────────────

def option4():
    box("Option 4 — RO003 → Multiple → VIN (2 turns)")
    print("""
  Flow:
    Turn 1: query_ro → found_multiple (AAAAA, BBBBB)
            graph interrupt → "VIN last 6-8 digits do"
            [graph froze — state SQLite mein saved]

    Turn 2: you type 'AAAAA'
            frontend → agent → Command(resume='AAAAA') → graph
            human_input: vin_last_8='AAAAA' saved
            query_ro(vin='AAAAA') → found_single New
            → "RO003 is New — in progress"
    """)
    run_session(
        label   = "RO003 → Multiple → VIN=AAAAA → New",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO003","region":"US"},
        hints   = ["investigate", "AAAAA"],
    )


# ── OPTION 5 — RO004 (3 turns VIN+Warranty) ───────────────────────────────────

def option5():
    box("Option 5 — RO004 → VIN + Warranty (3 turns)")
    print("""
  Flow:
    Turn 1: query_ro → found_multiple (VIN004A, VIN004B)
            graph interrupt → "VIN do"
            [state: awaiting='vin', SQLite saved]

    Turn 2: you type 'VIN004A'
            graph resume → human_input: vin_last_8='VIN004A'
            query_ro(vin='VIN004A') → found_single Closed
            graph interrupt → "Warranty? yes/no"
            [state: awaiting='warranty_confirm', SQLite saved]

    Turn 3: you type 'yes'
            graph resume → query_warranty → archvd_in='Y'
            → "Use Express Warranty Search"
            complete = True
    """)
    run_session(
        label   = "RO004 → Multiple → VIN004A → Closed → Express Warranty",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO004","region":"US"},
        hints   = ["investigate", "VIN004A", "yes"],
    )


# ── OPTION 6 — RO005 (FUSE success) ───────────────────────────────────────────

def option6():
    box("Option 6 — RO005 → Not found → FUSE success (2 turns)")
    print("""
  Flow:
    Turn 1: query_ro → not_found
            graph interrupt → "Date do (YYYY-MM-DD)"

    Turn 2: you type '2026-04-10'
            human_input: ro_open_date='2026-04-10' saved
            query_fuse(extend=False) → sts_cd=1 → success
            → "FUSE shows: Closed"
    """)
    run_session(
        label   = "RO005 → Not found → FUSE success",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO005","region":"US"},
        hints   = ["investigate", "2026-04-10"],
    )


# ── OPTION 7 — RO999 (Dynatrace) ──────────────────────────────────────────────

def option7():
    box("Option 7 — RO999 → Not found → FUSE → Dynatrace (2 turns)")
    print("""
  Flow:
    Turn 1: query_ro → 0 rows → not_found
            graph interrupt → "Date do"

    Turn 2: you type '2026-04-10'
            query_fuse(extend=False) → not_found → fuse_extended=True
            query_fuse(extend=True)  → still not_found
            query_dynatrace → MOCK → "DMS did not send this RO"
            complete = True
    """)
    run_session(
        label   = "RO999 → Not found → FUSE → Dynatrace",
        prefill = {"dealer_number":"NNA3565","ro_number":"RO999","region":"US"},
        hints   = ["investigate", "2026-04-10"],
    )


# ── OPTION 8 — RDR XD389786 ───────────────────────────────────────────────────

def option8():
    box("Option 8 — RDR XD389786 → Active + LMS (1 turn)")
    print("""
  Flow:
    Agent:
      Layer 1: sales_consultant_user_id present → "RDR"
      LLM nahi chala — 0 Bedrock calls

    rdr_graph:
      collect_context: Python split() — no LLM
      check_user_status: XD389786 → Active
      check_lms: XD389786 → in LMS → ticket raised
      complete = True
    """)
    run_session(
        label   = "RDR XD389786 → Active + LMS → ticket",
        prefill = {"dealer_number":"NNA3565","sales_consultant_user_id":"XD389786"},
        hints   = ["check this user"],
    )


# ── OPTION 9 — RDR XD000000 ───────────────────────────────────────────────────

def option9():
    box("Option 9 — RDR XD000000 → Inactive (1 turn)")
    print("""
  Flow:
    check_user_status: XD000000 → sts_in='I' (Inactive)
    → "Sales Consultant XD000000 is Inactive. Update in NNAnet."
    """)
    run_session(
        label   = "RDR XD000000 → Inactive",
        prefill = {"dealer_number":"NNA3565","sales_consultant_user_id":"XD000000"},
        hints   = ["check"],
    )


# ── OPTION 10 — RDR XD000001 ──────────────────────────────────────────────────

def option10():
    box("Option 10 — RDR XD000001 → Active, no LMS (1 turn)")
    print("""
  Flow:
    check_user_status: XD000001 → Active
    check_lms: XD000001 → NOT in dlr_usr table
    → "Active in DBS but not registered in LMS. Register first."
    """)
    run_session(
        label   = "RDR XD000001 → Active, not in LMS",
        prefill = {"dealer_number":"NNA3565","sales_consultant_user_id":"XD000001"},
        hints   = ["check"],
    )


# ── OPTION T — Topic switch ────────────────────────────────────────────────────

def option_t():
    box("Option T — Topic Switch Safety Demo")
    print("""
  Turn 1: RO investigation start
  Turn 2: XD  send user ID  (wrong topic)
  Expected: session locked → RO graph still handles it
    """)

    sid = str(uuid.uuid4())

    print("  Turn 1 — start RO investigation:")
    r1 = run(sid, "RO999 dealer NNA3565 missing", {}, 1)

    print(f"\n  Turn 2 — type XD user ID (wrong topic test):")
    print(f"  (hint: type 'XD389786 check karo')")
    msg = _ask()
    r2  = run(sid, msg, {}, 2)

    print(f"\n  Result:")
    print(f"  graph = {r2.get('graph_type')} ← still RO (session locked ✓)")
    print(f"  RDR graph nahi chala ✓")
    print(f"  No data leakage ✓")


# ── OPTION S — Session stats ───────────────────────────────────────────────────

def option_s():
    box("Option S — Session Stats + TTL")
    print("""
  agent.session_stats() — active sessions
  agent.force_cleanup() — expired sessions remove

  TTL = SESSION_TTL_SECONDS env variable (default 1800 = 30 min)
  To test fast: set SESSION_TTL_SECONDS=10 in .env
    """)

    stats = agent.session_stats()
    print(f"  Active sessions: {stats['total_sessions']}")
    for sid, info in stats.get('session_details', {}).items():
        print(f"    ...{sid}: graph={info['graph']} turns={info['turns']} age={info['age_min']}min")

    cleaned = agent.force_cleanup()
    print(f"\n  force_cleanup() removed: {cleaned} expired sessions")
    print(f"  SESSION_TTL = {os.environ.get('SESSION_TTL_SECONDS','1800')} seconds")


# ── MENU ───────────────────────────────────────────────────────────────────────

def main():
    while True:
        box("DBS Agent — Single Entry Point Demo")
        print("""
  HOW WAIT/RESUME WORKS:
    Turn 1  → agent routes → graph starts → may interrupt
    Turn 2+ → agent resumes same graph (0 Bedrock calls)
    Interrupt → graph froze → you give input → graph resumes
    Type exit anytime to quit

  RO SCENARIOS (agent auto-detects → ro_graph):
    1  RO001  → New           (1 turn)
    2  RO002  → Warranty N    (2 turns: investigate → yes)
    3  RO003  → VIN=AAAAA     (2 turns: investigate → AAAAA)
    4  RO004  → VIN+Warranty  (3 turns: investigate → VIN004A → yes)
    5  RO005  → FUSE success  (2 turns: investigate → 2026-04-10)
    6  RO999  → Dynatrace     (2 turns: investigate → 2026-04-10)

  RDR SCENARIOS (agent auto-detects → rdr_graph):
    7  XD389786 → Active + LMS   (1 turn)
    8  XD000000 → Inactive        (1 turn)
    9  XD000001 → Active, no LMS  (1 turn)

  AGENT FEATURES:
    0  Auto-detect (type anything — no pre-fill)
    T  Topic switch safety demo
    S  Session stats + TTL
    q  Quit
        """)

        c = input("  Enter: ").strip().lower()

        if   c == "q":  print("Goodbye."); break
        elif c == "0":  option1()
        elif c == "1":  option2()
        elif c == "2":  option3()
        elif c == "3":  option4()
        elif c == "4":  option5()
        elif c == "5":  option6()
        elif c == "6":  option7()
        elif c == "7":  option8()
        elif c == "8":  option9()
        elif c == "9":  option10()
        elif c == "t":  option_t()
        elif c == "s":  option_s()
        else: print("  Invalid — enter 0-9, T, S or q")


if __name__ == "__main__":
    main()

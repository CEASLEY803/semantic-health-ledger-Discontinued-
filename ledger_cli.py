#!/usr/bin/env python3
"""
ledger_cli.py: Developer CLI for the Semantic Health Ledger.

Calls Python functions directly (no HTTP server required) so the full
pipeline can be tested and debugged without going through the UI.

Usage
-----
# Full pipeline: extraction + Mem0 context + conversational reply
python ledger_cli.py chat "500mg vitamin D and 400mg magnesium glycinate with breakfast"

# Extraction only: parses text and prints structured result without writing to DB
python ledger_cli.py extract "58 bpm resting heart rate"

# Query SQLite tables directly
python ledger_cli.py db biometrics
python ledger_cli.py db supplements --limit 3
python ledger_cli.py db chat         # chat history rows

# Delete an entry by type + UUID
python ledger_cli.py delete biometric <uuid>
python ledger_cli.py delete chat <uuid>

# Test Mem0 semantic retrieval (runs in-process and may segfault on Windows)
python ledger_cli.py mem0 "vitamin D levels"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── UTF-8 output (Windows cp1252 can't handle box-drawing chars) ──────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Bootstrap ─────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from init_storage import DATABASE_PATH, WAL_PATH, initialize_storage  # noqa: E402


# ── Formatting helpers ────────────────────────────────────────────────────────

def _hr(char: str = "─", width: int = 64) -> None:
    print(char * width)

def _section(label: str) -> None:
    pad = max(0, 55 - len(label))
    print(f"\n  ┌─ {label} {'─' * pad}")

def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")

def _err(msg: str) -> None:
    print(f"  ✗  {msg}", file=sys.stderr)

def _pjson(obj: object) -> None:
    print(json.dumps(obj, indent=4, default=str))


# ── DB helpers (bypass api.py to avoid FastAPI import cost) ──────────────────

def _get_conn() -> sqlite3.Connection:
    initialize_storage(DATABASE_PATH, WAL_PATH)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _db_history(limit: int = 20) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        conn.close()

def _db_insert_chat(role: str, content: str) -> None:
    import uuid
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO chat_history (id, role, content) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), role, content),
        )
        conn.commit()
    finally:
        conn.close()


# ── Mem0 subprocess helper ────────────────────────────────────────────────────
# Qdrant's fastembed native extension segfaults in-process on Windows.
# We isolate it in a child process exactly like the API server does.

_MEM0_HELPER = """\
import sys, os
sys.path.insert(0, {root!r})
os.chdir({root!r})
from dotenv import load_dotenv
load_dotenv({env!r})
from memory_layer import retrieve_context
result = retrieve_context(query={query!r}, top_k={top_k})
print(result or '', end='')
"""

def _retrieve_context_safe(query: str, top_k: int = 5) -> str:
    """Run retrieve_context in a subprocess to avoid Qdrant segfault on Windows."""
    script = _MEM0_HELPER.format(
        root=str(ROOT),
        env=str(ROOT / ".env"),
        query=query,
        top_k=top_k,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return ""
    except Exception:
        return ""


# ── cmd: extract ──────────────────────────────────────────────────────────────

def cmd_extract(args: argparse.Namespace) -> None:
    """Parse text through Flash extraction without DB writes or conversation."""
    from llm_service import extract

    text = " ".join(args.text)
    print(f"\nInput: {text!r}")
    _hr()

    t0 = time.time()
    try:
        result = extract(text)
    except Exception as exc:
        _err(f"Extraction failed: {exc}")
        sys.exit(1)

    elapsed = time.time() - t0
    _ok(f"Extracted in {elapsed:.2f}s  →  log_type = {result.log_type}")

    if result.supplements:
        _section("supplements")
        for c in result.supplements:
            print(f"    {c.supplement_name}  {c.dose_value} {c.dose_unit}  via {c.route}"
                  + (f"  [{c.site}]" if c.site else "")
                  + (f"  // {c.notes}" if c.notes else ""))

    if result.biometrics:
        _section("biometrics")
        for b in result.biometrics:
            print(f"    {b.metric_name} = {b.value} {b.unit}"
                  + (f"  // {b.notes}" if b.notes else ""))

    if result.labs:
        _section("labs")
        for lab in result.labs:
            val = f"{lab.value_numeric} {lab.unit or ''}".strip() if lab.value_numeric else lab.value_text
            ref = (f"  [ref {lab.reference_low}–{lab.reference_high}]"
                   if lab.reference_low and lab.reference_high else "")
            print(f"    {lab.marker_name} = {val}{ref}")

    if result.journals:
        _section("journals")
        for j in result.journals:
            print(f"    mood={j.mood}  energy={j.energy_score}  sleep={j.sleep_hours}")
            print(f"    {j.notes[:120]}")

    if not any([result.supplements, result.biometrics, result.labs, result.journals]):
        print("\n  (no health data detected)")

    print()


# ── cmd: chat ─────────────────────────────────────────────────────────────────

def cmd_chat(args: argparse.Namespace) -> None:
    """Full pipeline: extract → Mem0 → conversation reply → save to history."""
    from llm_service import chat_respond, extract, CONVERSATION_MODEL

    # Avoid circular import by pulling only the required DB helpers
    from api import commit_logs, append_to_wal  # noqa: PLC0415

    text = " ".join(args.text)
    print(f"\nInput: {text!r}")
    _hr()

    t_total = time.time()

    # ── Chat history ──────────────────────────────────────────────────────────
    chat_history = _db_history(20)
    print(f"  history   {len(chat_history)} prior turns loaded")

    # ── Step A: extraction ────────────────────────────────────────────────────
    extracted = None
    committed = None
    t = time.time()
    try:
        extracted = extract(text)
        _ok(f"extract   {time.time()-t:.1f}s  log_type={extracted.log_type}")
        for c in extracted.supplements:
            print(f"           supplement: {c.supplement_name} {c.dose_value}{c.dose_unit} {c.route}")
        for b in extracted.biometrics:
            print(f"           biometric: {b.metric_name}={b.value}{b.unit}")
        for lab in extracted.labs:
            print(f"           lab: {lab.marker_name}={lab.value_numeric}{lab.unit or ''}")
        for j in extracted.journals:
            print(f"           journal: mood={j.mood}")
    except Exception as exc:
        _err(f"extract   {exc} (non-fatal, continuing)")

    # ── Commit if data ────────────────────────────────────────────────────────
    if extracted and any([extracted.supplements, extracted.biometrics,
                          extracted.labs, extracted.journals]):
        try:
            append_to_wal(text)
            committed = commit_logs(
                supplements=extracted.supplements,
                biometrics=extracted.biometrics,
                labs=extracted.labs,
                journals=extracted.journals,
            )
            _ok(f"commit    {committed}")
        except Exception as exc:
            _err(f"commit    {exc}")

    # ── Step B: Mem0 context (subprocess-isolated to avoid Qdrant segfault) ─────
    mem0_context = ""
    t = time.time()
    mem0_context = _retrieve_context_safe(text, top_k=5)
    snippet = mem0_context[:80].replace("\n", " ") + ("…" if len(mem0_context) > 80 else "")
    _ok(f"mem0      {time.time()-t:.1f}s  {len(mem0_context)} chars  {snippet!r}")

    # ── Step C: conversational reply ──────────────────────────────────────────
    print(f"  respond   calling {CONVERSATION_MODEL}…")
    t = time.time()
    try:
        reply = chat_respond(text, extracted, mem0_context, chat_history)
        _ok(f"respond   {time.time()-t:.1f}s")
    except Exception as exc:
        reply = f"[error: {exc}]"
        _err(f"respond   {exc}")

    # ── Output ────────────────────────────────────────────────────────────────
    _hr()
    print(f"\nLEDGER:\n{reply}\n")
    _hr()
    total = time.time() - t_total
    print(f"  total {total:.1f}s  |  committed: {committed}\n")

    # ── Persist to chat history ───────────────────────────────────────────────
    if not args.no_save:
        try:
            _db_insert_chat("user", text)
            _db_insert_chat("model", reply)
            print("  history   saved")
        except Exception as exc:
            _err(f"history save: {exc}")


# ── cmd: simulate ───────────────────────────────────────────────────────────

_SIM_PRICING = {
    # per 1M tokens, <=200k context tier. thinking bills at the OUTPUT rate.
    "gemini-2.5-pro":         {"in": 1.25, "out": 10.0},
    "gemini-3.1-pro-preview": {"in": 2.00, "out": 12.0},
    "gemini-2.5-flash":       {"in": 0.30, "out": 2.50},
}


def cmd_simulate(args: argparse.Namespace) -> None:
    """Dry-run ONE conversation turn as an AI-friendly test harness.

    Unlike `chat`, this NEVER commits extracted data and NEVER writes chat_history.
    It mirrors chat_respond's tool loop while capturing token usage per round, then
    splits out the <think> block (which the app hides) and prints behavior + cost in
    one shot. Use it to reproduce a scenario and see exactly how the model reasons.

    Examples:
      simulate "is my ferritin actually the problem?"          # vs live history (read-only)
      simulate "..." --no-history                              # fresh, no prior turns
      simulate "..." --history-json scenario.json              # scripted prior turns
      simulate "..." --model gemini-2.5-pro --thinking-budget -1   # compare a config
    """
    import os
    import re
    # Apply overrides BEFORE importing llm_service because its model and budget constants are
    # read at import time.
    if args.model:
        os.environ["CONVERSATION_MODEL"] = args.model
    if args.thinking_budget is not None:
        os.environ["CONVERSATION_THINKING_BUDGET"] = str(args.thinking_budget)

    from google.genai import types
    from llm_service import (
        CONVERSATION_MODEL, _get_conversation_client, _build_conversation_prompt,
        _to_gemini_history, _make_ledger_tools, _execute_ledger_tool, _get_fn_calls,
        _safe_response_text, _load_graph_context, _generate_clinical_narrative,
        _clinical_safety_settings, _conversation_thinking_config, _compact_tool_log,
    )

    scenario = {}
    if args.scenario:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))

    text = " ".join(args.text) if args.text else scenario.get("message", "")
    if not text:
        _err('No message: pass text, or use --scenario with a "message" field.')
        sys.exit(1)

    # History precedence: --history-json > scenario.history > --no-history > live DB
    if args.history_json:
        history, hist_src = json.loads(Path(args.history_json).read_text(encoding="utf-8")), args.history_json
    elif "history" in scenario:
        history, hist_src = scenario["history"], args.scenario
    elif args.no_history:
        history, hist_src = [], "fresh"
    else:
        history, hist_src = _db_history(args.turns), "live DB (read-only)"

    print(f"\nInput:   {text!r}")
    print(f"Model:   {CONVERSATION_MODEL}   thinking={_conversation_thinking_config()}")
    print(f"History: {len(history)} prior turn(s)  [{hist_src}]")
    if scenario.get("expect"):
        print(f"Expect:  {scenario['expect']}")
    _hr()

    client    = _get_conversation_client()
    graph     = _load_graph_context()
    narrative = _generate_clinical_narrative(None, "", graph, "")
    prompt    = f"{narrative}\n\n[User message]\n{text}" if narrative else text

    chat = client.chats.create(
        model=CONVERSATION_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=_build_conversation_prompt(),
            temperature=0.7,
            tools=_make_ledger_tools(),
            safety_settings=_clinical_safety_settings(),
            thinking_config=_conversation_thinking_config(),
        ),
        history=_to_gemini_history(history),
    )

    tin = tout = tthink = tcached = rounds = 0
    t0 = time.time()
    response = chat.send_message(prompt)
    for _ in range(5):
        rounds += 1
        um = getattr(response, "usage_metadata", None)
        if um:
            tin     += getattr(um, "prompt_token_count", 0) or 0
            tout    += getattr(um, "candidates_token_count", 0) or 0
            tthink  += getattr(um, "thoughts_token_count", 0) or 0
            tcached += getattr(um, "cached_content_token_count", 0) or 0
        fn_calls = _get_fn_calls(response)
        if not fn_calls:
            break
        parts = []
        for fc in fn_calls:
            fc_args = dict(fc.args or {})
            result  = _execute_ledger_tool(fc.name, fc_args)
            print(f"  tool  {_compact_tool_log(fc.name, fc_args, result)}")
            parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
        response = chat.send_message(parts)
    elapsed = time.time() - t0

    raw     = _safe_response_text(response).strip()
    think   = "\n".join(re.findall(r"<think>(.*?)</think>", raw, flags=re.DOTALL)).strip()
    visible = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    _section("REASONING  <think>")
    print(think or "  (no <think> block emitted this turn)")
    _section("VISIBLE REPLY")
    print(visible or raw or "  (empty)")

    _section("USAGE")
    print(f"  rounds={rounds}  input={tin}  output={tout}  thinking={tthink}  cached={tcached}  ({elapsed:.1f}s)")
    price = _SIM_PRICING.get(CONVERSATION_MODEL)
    if price:
        cost = (tin / 1e6) * price["in"] + ((tout + tthink) / 1e6) * price["out"]
        per5 = int(5 / cost) if cost else 0
        print(f"  est ~${cost:.4f}/turn (cached billed at full rate = upper bound; implicit cache ~25%)  →  ~{per5} turns/$5")
    else:
        print(f"  (no pricing on file for {CONVERSATION_MODEL})")
    print()


# ── cmd: db ───────────────────────────────────────────────────────────────────

_TABLE_MAP = {
    "biometrics": ("biometric_logs",   "recorded_at"),
    "supplements":  ("supplement_logs",    "recorded_at"),
    "labs":       ("lab_results",      "collected_at"),
    "journals":   ("daily_journals",   "journal_date"),
    "chat":       ("chat_history",     "created_at"),
}

_TRIM_COLS = {"raw_text", "notes", "content"}

def cmd_db(args: argparse.Namespace) -> None:
    """Query a SQLite table and pretty-print rows."""
    table_key = getattr(args, "table", "chat")
    if table_key not in _TABLE_MAP:
        _err(f"Unknown table '{table_key}'. Choose: {', '.join(_TABLE_MAP)}")
        sys.exit(1)

    table, order_col = _TABLE_MAP[table_key]
    limit = getattr(args, "limit", 10)

    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,)  # noqa: S608
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"\n  (no rows in {table})\n")
        return

    print(f"\n{len(rows)} rows from \033[1m{table}\033[0m (newest first):\n")
    _hr()
    for row in rows:
        d = dict(row)
        for k in _TRIM_COLS:
            if k in d and d[k] and len(str(d[k])) > 100:
                d[k] = str(d[k])[:100] + "…"
        _pjson(d)
        print()


# ── cmd: history ──────────────────────────────────────────────────────────────

def cmd_history(args: argparse.Namespace) -> None:
    """Show persisted chat history rows."""
    args.table = "chat"
    args.limit = getattr(args, "limit", 20)
    cmd_db(args)


# ── cmd: delete ───────────────────────────────────────────────────────────────

def cmd_delete(args: argparse.Namespace) -> None:
    """Hard-delete a row by entry type and UUID."""
    type_to_table = {
        "biometric": "biometric_logs",
        "supplement":  "supplement_logs",
        "lab":       "lab_results",
        "journal":   "daily_journals",
        "chat":      "chat_history",
    }
    if args.entry_type not in type_to_table:
        _err(f"Unknown type. Choose: {', '.join(type_to_table)}")
        sys.exit(1)

    table = type_to_table[args.entry_type]
    conn = _get_conn()
    try:
        cursor = conn.execute(f"DELETE FROM {table} WHERE id = ?", (args.id,))  # noqa: S608
        conn.commit()
    finally:
        conn.close()

    if cursor.rowcount == 0:
        _err(f"No row found  id={args.id!r}  table={table}")
        sys.exit(1)
    _ok(f"Deleted {args.entry_type}  {args.id}  from {table}")


# ── cmd: mem0 ────────────────────────────────────────────────────────────────

def cmd_mem0(args: argparse.Namespace) -> None:
    """Run a Mem0 semantic retrieval query in-process.

    Warning: Qdrant's native extension may segfault on Windows on first call.
    This is expected, which is why the server isolates Mem0 in a subprocess.
    """
    from memory_layer import retrieve_context

    query = " ".join(args.query)
    print(f"\nQuery: {query!r}")
    _hr()
    t0 = time.time()
    try:
        result = retrieve_context(query=query, top_k=5)
        elapsed = time.time() - t0
        _ok(f"Retrieved in {elapsed:.2f}s\n")
        print(result or "  (empty: no matching memories)")
    except Exception as exc:
        _err(f"{type(exc).__name__}: {exc}")
        print("\n  Note: Qdrant segfaults in-process on Windows. This is expected.\n"
              "  The server uses ProcessPoolExecutor to isolate this crash.")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ledger",
        description="Semantic Health Ledger developer CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # chat
    p = sub.add_parser("chat", help="Full pipeline: extract + Mem0 + conversation + save")
    p.add_argument("text", nargs="+", help="Message to process")
    p.add_argument("--no-save", action="store_true",
                   help="Skip saving this turn to chat_history")
    p.set_defaults(func=cmd_chat)

    # simulate
    p = sub.add_parser("simulate", aliases=["sim"],
                       help="Dry-run a turn: raw <think> + token cost, no DB writes")
    p.add_argument("text", nargs="*", help="The user message (optional if --scenario supplies one)")
    p.add_argument("--scenario", help="Path to a scenario JSON {message, history, expect} (see scenarios/)")
    p.add_argument("--model", help="Override CONVERSATION_MODEL for this run")
    p.add_argument("--thinking-budget", type=int, dest="thinking_budget",
                   help="Override CONVERSATION_THINKING_BUDGET (-1 = uncapped)")
    p.add_argument("--history-json", help="Path to a JSON array of {role, content} prior turns")
    p.add_argument("--no-history", action="store_true", help="Start with empty history")
    p.add_argument("--turns", type=int, default=20,
                   help="Live DB turns to load when no --history-json/--no-history (default 20)")
    p.set_defaults(func=cmd_simulate)

    # extract
    p = sub.add_parser("extract", help="Extraction only, with no DB write or conversation")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_extract)

    # db
    p = sub.add_parser("db", help="Query a SQLite table")
    p.add_argument("table", choices=list(_TABLE_MAP), metavar="TABLE",
                   help=f"{{{', '.join(_TABLE_MAP)}}}")
    p.add_argument("--limit", type=int, default=10, metavar="N")
    p.set_defaults(func=cmd_db)

    # history
    p = sub.add_parser("history", help="Show chat history (alias: db chat)")
    p.add_argument("--limit", type=int, default=20, metavar="N")
    p.set_defaults(func=cmd_history)

    # delete
    p = sub.add_parser("delete", help="Hard-delete an entry")
    p.add_argument("entry_type",
                   choices=["biometric", "supplement", "lab", "journal", "chat"],
                   metavar="TYPE")
    p.add_argument("id", metavar="UUID")
    p.set_defaults(func=cmd_delete)

    # mem0
    p = sub.add_parser("mem0", help="Test Mem0 semantic retrieval in-process")
    p.add_argument("query", nargs="+")
    p.set_defaults(func=cmd_mem0)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

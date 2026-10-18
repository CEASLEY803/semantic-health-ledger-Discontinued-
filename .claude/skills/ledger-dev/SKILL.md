---
name: ledger-dev
description: >
  Dev/test workflow for the Semantic Health Ledger (this project). Use when running,
  testing, debugging, or modifying the FastAPI backend (api.py, llm_service.py) or the
  conversation/extraction pipeline, when reproducing how the model reasons on a given
  message, when measuring per-turn token cost, or when launching the app to see the UI.
  Covers the non-obvious gotchas (.env overrides code defaults, streaming-vs-CLI response
  paths, venv requirement, Mem0 segfault) so you don't rediscover them.
---

# Ledger: Developer & Testing Guide

Tauri v2 desktop app: Next.js 15 frontend + FastAPI backend (port 8787), SQLite (`ledger.db` /
`data/`). Conversation model is Gemini (see `.env`); extraction uses Flash. Read `CLAUDE.md`
for the full architecture (knowledge graph, cognition loop, Garmin sync, tool calling).

## Always use the venv Python

```
.venv\Scripts\python.exe <script>
```
Never use bare `python`, since dependencies live in the venv.

## Fastest way to test conversation behavior: `simulate`

`ledger_cli.py simulate` is the AI-friendly harness. It dry-runs ONE conversation turn,
**writes nothing to the DB**, splits out the `<think>` block (the app hides it), and prints
token usage + cost. This is the first thing to reach for when changing the prompt or model.

```
.venv\Scripts\python.exe ledger_cli.py simulate "is my ferritin actually the problem?"
.venv\Scripts\python.exe ledger_cli.py simulate "..." --no-history          # fresh, no priors
.venv\Scripts\python.exe ledger_cli.py simulate "..." --history-json scn.json  # scripted priors
.venv\Scripts\python.exe ledger_cli.py simulate --scenario scenarios/iron_ferritin_contradiction.json
.venv\Scripts\python.exe ledger_cli.py simulate "..." --model gemini-2.5-pro --thinking-budget -1
```
- `--scenario PATH` runs a committed regression fixture (message + history + an `expect`
  rubric); see `scenarios/README.md`. Use these to check that behavior held after a change.
- `--history-json` takes a JSON array of `{"role": "user"|"model", "content": "..."}`. Use it
  to reproduce a multi-turn scenario (e.g. model states a theory, user contradicts it).
- `--model` / `--thinking-budget` override `.env` for that run only (great for A/B cost/quality).
- Default (no flags) loads live chat history read-only.

Other CLI commands (full table in `CLAUDE.md`): `extract`, `chat` (full pipeline, writes DB;
use `--no-save` to skip history), `db <table>`, `history`, `delete <type> <uuid>`.

## Gotchas that will waste your time if you don't know them

1. **`.env` overrides code defaults.** `CONVERSATION_MODEL`, `CONVERSATION_THINKING_BUDGET`,
   etc. are read from `.env` (gitignored) and the env var WINS over the default in
   `llm_service.py`. Changing the code default alone does nothing if `.env` pins a value.
   Edit `.env` to actually change runtime behavior. (`.env.example` is the tracked template.)
2. **Two response paths.** The CLI `chat`/`simulate` use `chat_respond` (non-streaming); the
   **running app uses `chat_respond_stream`**. If you change conversation behavior, apply it to
   BOTH and verify the streaming path, as a CLI test alone doesn't prove the app changed.
3. **Mem0 / Qdrant segfaults in-process on Windows.** Expected. The server isolates it in a
   subprocess; the CLI shells out. The `mem0` subcommand segfaulting is documented/normal.
4. **The conversation system prompt** is built in `_build_conversation_prompt()` in
   `llm_service.py` (large; `[FIRST PRINCIPLES]`, `[DATA INTERPRETATION PROTOCOL]`, the
   `<think>` structure rules). The `<think>` tags are literal text the model emits, not
   Gemini's native thinking (which is stripped).

## Cost / token accounting

- `simulate`'s USAGE block prints input/output/thinking/cached tokens + a per-turn estimate.
- Thinking tokens bill at the OUTPUT rate; the dominant real cost is **re-sent input**
  (system prompt + graph context + history, re-sent every tool round), which grows as a
  conversation lengthens. Google implicit caching is active (see the `cached` count).

## Seeing the actual UI

The app is a Next.js frontend in a Tauri shell. To view/drive the UI yourself, run the web
dev server (same UI as the desktop window) and use the Chrome MCP:

1. **Backend:** `.venv\Scripts\python.exe api.py`  → FastAPI on http://127.0.0.1:8787
2. **Frontend:** `cd frontend; npm run dev`  → Next.js on http://localhost:3000
3. Open `http://localhost:3000` with the **Chrome MCP** (`mcp__claude-in-chrome__*`) to
   screenshot, read the DOM, and interact. The frontend calls the backend on 8787, so both
   must be running.

For the full desktop build instead: `cd frontend; npx tauri dev` (Tauri auto-spawns the
backend). Requires the Rust toolchain (see `CLAUDE.md`). Prefer the web dev server for
inspection because it is lighter and screenshot-friendly.

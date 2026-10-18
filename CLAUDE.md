# Semantic Health Ledger: Developer Notes

## Project Overview

Tauri v2 desktop app (Windows). Next.js 15 frontend + FastAPI backend.

- **Backend**: `api.py` (FastAPI, port 8787), `llm_service.py`, `memory_layer.py`
- **Frontend**: `frontend/src/` (Next.js 15, Turbopack, Tailwind)
- **DB**: SQLite at `ledger.db` (WAL mode)
- **Models**: `gemini-2.5-flash` for extraction (temp 0, forced JSON), `gemini-2.5-pro` for conversation (temp 0.7)

## Running the App

```bash
cd frontend && npx tauri dev
```

Backend is spawned automatically by Tauri. In dev, Turbopack serves the frontend.

## Developer CLI (for me to use directly)

**Always use the venv python:**
```
.venv\Scripts\python.exe ledger_cli.py <command>
```

| Command | What it does |
|---|---|
| `extract "text"` | Runs Flash extraction only, with no DB write and no conversation. Fast. |
| `chat "text"` | Full pipeline: history → extract → commit → mem0 → chat_respond → save |
| `chat "text" --no-save` | Full pipeline but don't write to chat_history (use for testing) |
| `simulate "text"` | Dry-run ONE turn: no DB writes, prints the `<think>` block + token cost. `--no-history`, `--history-json PATH`, `--model`, `--thinking-budget` to control the run. Best harness for testing prompt/model changes. |
| `simulate --scenario PATH` | Run a committed regression fixture (message + history + `expect` rubric) from `scenarios/`. See `scenarios/README.md`. |
| `db biometrics` | Query `biometric_logs` table, newest first |
| `db supplements` | Query `supplement_logs` table |
| `db labs` | Query `lab_results` table |
| `db journals` | Query `daily_journals` table |
| `db chat` | Query `chat_history` table |
| `db <table> --limit N` | Limit rows returned (default 10) |
| `history` | Alias for `db chat --limit 20` |
| `delete biometric <uuid>` | Hard-delete a biometric_logs row |
| `delete supplement <uuid>` | Hard-delete a supplement_logs row |
| `delete lab <uuid>` | Hard-delete a lab_results row |
| `delete journal <uuid>` | Hard-delete a daily_journals row |
| `delete chat <uuid>` | Hard-delete a chat_history row |
| `mem0 "query"` | Test Mem0 retrieval in-process (**will segfault on Windows**, expected) |

**Notes:**
- `chat` runs mem0 via subprocess to avoid the Qdrant segfault
- `mem0` subcommand runs in-process intentionally; the segfault is documented and expected
- Use `--no-save` during extraction/pipeline testing to keep chat_history clean

## Known Issues

### Mem0 / Qdrant segfault (Windows)
`fastembed`'s BM25 native extension segfaults in-process on Windows. The API server
isolates Mem0 in a `ProcessPoolExecutor` subprocess. The CLI uses `subprocess.run()`.
The pool auto-recovers (`_mem0_pool = None` reset on `BrokenProcess*` exception), but
semantic memory context is always empty until this is fixed.

**Long-term fix**: upgrade `qdrant-client` / `mem0ai`, or switch Mem0 to a different
vector store backend (e.g., Chroma, FAISS).

### Garmin token saving
- Tokens saved to `data/garmin_tokens/garmin_tokens.json` (format: `{di_token, di_refresh_token, di_client_id}`)
- `garmin_auth.py` passes `tokenstore=str(TOKEN_DIR)` to `Garmin.login()`, and the library auto-saves on success
- On subsequent runs, tokens are loaded from disk; SSO is only hit if tokens are missing or expired
- **Rate limiting**: Garmin IP-blocks after repeated failed logins (~24–48 hrs, all 5 strategies hit same block)
  - Fix: authenticate once from a different IP (iPhone Personal Hotspot or VPN)
  - Once tokens are saved, re-auth is rarely needed (DI refresh tokens last ~1 year)

### Garmin metrics collected per day (`_fetch_garmin_metrics` in `api.py`)
All stored as `biometric_logs` with `context = 'garmin_sync'`:

| metric_name | unit | source |
|---|---|---|
| `sleep_start` | hour_of_day | `dailySleepDTO.sleepStartTimestampLocal`; decimal hours, midnight-crossing times shifted +24 (e.g. 1 AM = 25.0); `notes` = "HH:MM" |
| `sleep_end` | hour_of_day | `dailySleepDTO.sleepEndTimestampLocal`; decimal hours (no shift); `notes` = "HH:MM" |
| `sleep_duration` | hours | `dailySleepDTO.sleepTimeSeconds` |
| `sleep_deep` | hours | `dailySleepDTO.deepSleepSeconds` |
| `sleep_light` | hours | `dailySleepDTO.lightSleepSeconds` |
| `sleep_rem` | hours | `dailySleepDTO.remSleepSeconds` |
| `sleep_awake` | hours | `dailySleepDTO.awakeSleepSeconds` |
| `sleep_spo2_avg` | % | `dailySleepDTO.averageSpO2Value` |
| `sleep_spo2_low` | % | `dailySleepDTO.lowestSpO2Value` |
| `sleep_respiration` | breaths/min | `dailySleepDTO.averageRespirationValue` |
| `sleep_stress` | score | `dailySleepDTO.avgSleepStress` |
| `hrv_last_night` | ms | `get_hrv_data → hrvSummary.lastNight` |
| `hrv_weekly_avg` | ms | `get_hrv_data → hrvSummary.weeklyAvg` |
| `resting_heart_rate` | bpm | `get_stats → restingHeartRate` |
| `body_battery` | % | `get_body_battery → end-of-day value` |
| `workout_session` | bpm (avg HR) | `get_activities_by_date → averageHR`; one row per activity; `notes` = "Name · Xmin · max HR Y · Zcal · Wkm" |
| `workout_duration` | min | companion row to `workout_session`; same `recorded_at` so the UI can join them by timestamp; omitted when Garmin reports no duration |

Idempotent: sleep/HRV/daily metrics dedup on `(metric_name, date)`; `workout_session` rows dedup on exact `recorded_at` timestamp so multiple workouts on the same day each get a row.
Fields are optional, so devices without HRV or SpO2 sensors simply omit those rows. Activities without HR data are skipped.

**Sync trigger flow:**
1. `GarminSyncButton` auto-fires on app mount (2 s delay) in silent mode, showing no toast if nothing is new
2. Regular click → gap-fill: queries `_garmin_missing_dates(30)` and only syncs absent dates
3. **Shift+click** → force mode: re-syncs all 30 days; per-metric dedup adds new metrics to old dates
   without creating duplicate rows. Use this after adding new metrics (e.g. HRV) to backfill history.

**API params:** `POST /api/v1/sync/garmin?force=true&days=30`
- `force` (bool, default false): skip date-level gap check and re-touch all dates
- `days`  (int,  default 30): lookback window, capped at 365

## Architecture Notes

### Timeout layers (conversation model)
1. SDK HTTP timeout: `90_000 ms` (90s) via `HttpOptions(timeout=90_000)`
2. SDK retries disabled: `HttpRetryOptions(attempts=1)` (no exponential backoff)
3. asyncio ceiling: `asyncio.wait_for(..., timeout=65.0)` in `ingest_chat`

### `HttpOptions.timeout` is in MILLISECONDS
The google-genai SDK divides by 1000 internally (`timeout_in_seconds = timeout / 1000.0`).
`timeout=30.0` = 30ms. Always pass `timeout=30_000` for 30 seconds.

### Chat history / working memory
- Stored in `chat_history` SQLite table (`id`, `role`, `content`, `created_at`)
- `role` is `'user'` or `'model'` (Gemini convention, not `'assistant'`)
- `_to_gemini_history()` in `llm_service.py` enforces strict user/model alternation
  before passing to `chats.create(history=...)`, as Gemini returns 400 on malformed history
- Frontend loads history on startup via `GET /api/v1/chat/history` (triggered by
  `online` state from `useTelemetry`, not component mount)

### LEDGER tool calling (function calling)
`chat_respond` passes three Gemini function declarations and runs a tool loop (max 5 rounds):
- **`query_entries(entry_type, metric_name?, limit?)`**: SELECT from any table with optional
  partial-match name filter. Returns `{entries: [...], count: N}`.
- **`update_entry(entry_type, entry_id, field_name, new_value)`**: UPDATE a single field by UUID.
  Dates must be ISO 8601. Returns `{success, updated_id, field, new_value}`.
- **`delete_entry(entry_type, entry_id)`**: hard DELETE by UUID. Returns `{success, deleted_id}`.

LEDGER always calls `query_entries` first to find the UUID, then `update_entry` or `delete_entry`.
Tool execution runs in `_execute_ledger_tool()` in `llm_service.py` via direct sqlite3 calls.

Dashboard auto-refreshes after every chat ingest via `refreshKey` in `page.tsx`, so
updates and deletions via chat are reflected immediately without any extra plumbing.

`entry_type` values: `biometric`, `supplement`, `lab`, `journal` (NOT `labs`/`journals`).

Updatable fields per type (see `_UPDATABLE_FIELDS` in both `llm_service.py` and `api.py`):
- lab: `collected_at`, `panel_name`, `marker_name`, `value_numeric`, `unit`, `reference_low`,
  `reference_high`, `notes`, `flagged`
- biometric: `recorded_at`, `metric_name`, `value`, `unit`, `notes`
- supplement: `recorded_at`, `supplement_name`, `dose_value`, `dose_unit`, `route`, `site`, `notes`
- journal: `journal_date`, `mood`, `energy_score`, `sleep_hours`, `notes`

### Entry deletion / update (UI)
- `DELETE /api/v1/entry/{entry_type}/{entry_id}`: hard delete, no soft delete
- `PATCH  /api/v1/entry/{entry_type}/{entry_id}`: update single field `{field_name, new_value}`
- UI: hover-reveal `×` button on every row/card across all four dashboard panels

### Module-level cached Gemini clients
`_extractor_client` and `_conversation_client` in `llm_service.py` are lazy-init
module-level singletons. Avoids TLS handshake overhead on every request.

### Cognition loop (the "mind" heartbeat): `cognition_worker.py`
Continuous proactive cognition, distinct from the once-a-day morning synthesis.
`_cognition_heartbeat()` (async task started in the `lifespan`, after a startup delay)
calls `run_cognition_tick()` via `run_in_threadpool()` every `COGNITION_INTERVAL_SECONDS`.

**Two bars by design:**
- **THINK bar** (low): every tick writes one row to `cognition_log` (the visible inner
  monologue). Idle ticks are free: pure-Python tripwires, no LLM call.
- **SPEAK bar** (high): only a salient, un-deduped, in-budget observation becomes an
  unsolicited chat message. Phrasing is a single Gemini Flash call (`EXTRACTOR_MODEL`,
  thinking disabled via `ThinkingConfig(thinking_budget=0)` so the short message isn't
  truncated). On phrasing failure it falls back to a plain factual sentence.

**Tripwires (Python, deterministic):** (1) a lab newly committed AND flagged; (2) a watched
biometric (`hrv_last_night`, `resting_heart_rate`, `sleep_duration`, `body_battery`)
deviating ≥ `COGNITION_ZSCORE` σ from its 14-day baseline (≥4 prior points required);
(3) a temporal `clinical_nodes` reaching full clearance (today within ±1d of `expires_at`),
deduped on the node's own `last_surfaced_date` (shared with morning synthesis).
New-vs-old is judged by a half-open `created_at` watermark window stored in
`system_state.cognition_watermark`; the first run ever just calibrates and surfaces nothing.

**Continuity (resolution):** when a watched biometric logs a NEW reading back within
baseline, its open thread is marked `resolved` and a brief follow-up may be spoken
("…that dip has resolved"). Resolution closes the thread whether or not it's announced.

**Phrasing model by salience:** salience ≥ `COGNITION_ESCALATE_BAR` (0.85) escalates to the
Pro model (`CONVERSATION_MODEL`, thinking ON) with an instruction to check the new data
against `confirmed` graph nodes and name contradictions/mechanism; below that, the cheap
Flash path is used. Pro failure falls back to Flash.

**Governor (blocks a speak):** below the *effective* speak bar (`COGNITION_SPEAK_BAR` +
learned `cognition_bar_adjust`) · snooze active · quiet hours (`COGNITION_QUIET_START/END`,
local) · daily budget (`COGNITION_DAILY_BUDGET`, counted in `system_state`) · cooldown
(`COGNITION_COOLDOWN_MINUTES`) · dedup (`COGNITION_DEDUP_HOURS`, via `cognition_threads.last_surfaced_at`).
`force=true` on the manual tick bypasses all of these.

**Feedback / attunement (`record_feedback`):** 👍 lowers the bar adjust (−0.03, floor −0.15),
👎 raises it (+0.05, cap +0.30) and dismisses the thread, snooze sets `cognition_snooze_until`
= now + `COGNITION_SNOOZE_HOURS`. Stored in `system_state`; the governor reads them each tick.

**Delivery:** `_run_cognition_tick_and_dispatch()` (api.py) always emits `COGNITION_TICK`
(Mind panel updates live) and, when speaking, persists the message to `chat_history`
(`role='model'`, attached to `_latest_session_id()`) and emits `LEDGER_THOUGHT`. Frontend
`ChatPanel` appends it as a `proactive` bubble (violet "UNPROMPTED" variant) deduped by DB
id; if `userIsAway()` it fires a native toast via `tauri-plugin-notification`. The webview
stays alive when the window is hidden to tray, so proactive thoughts + toasts work even
when "closed."

- `GET  /api/v1/cognition/log?limit=N`: recent monologue + open threads (the **MIND** dashboard tab)
- `POST /api/v1/cognition/tick?force=bool`: fire one tick manually (testing)
- `POST /api/v1/cognition/feedback` `{rating: up|down|snooze, signature?}`: attune the loop
  (👍/👎 buttons live on every proactive chat bubble)

All env tunables live at the top of `cognition_worker.py` with sensible defaults
(interval 300s, startup delay 120s, budget 3/day, quiet 22→8, cooldown 90m, dedup 20h,
z-score 1.5, speak bar 0.6, escalate bar 0.85, snooze 8h).

## SQLite Tables

| Table | Key columns |
|---|---|
| `biometric_logs` | `id`, `metric_name`, `value`, `unit`, `recorded_at` |
| `supplement_logs` | `id`, `supplement_name`, `dose_value`, `dose_unit`, `route`, `site`, `recorded_at` |
| `lab_results` | `id`, `marker_name`, `panel_name`, `value_numeric`, `unit`, `reference_low`, `reference_high`, `collected_at` |
| `daily_journals` | `id`, `mood`, `energy_score`, `sleep_hours`, `notes`, `journal_date` |
| `chat_history` | `id`, `role`, `content`, `created_at` |
| `cognition_threads` | `id`, `signature` (unique key), `title`, `status`, `salience`, `last_surfaced_at` |
| `cognition_log` | `id`, `kind` (idle/observation/spoke), `salience`, `summary`, `detail`, `thread_id`, `created_at` |

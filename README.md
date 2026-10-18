# Semantic Health Ledger

Log supplements, biometrics, lab results, and journal entries in plain text. Gemini extracts and routes structured data into a local SQLite database. Everything stays on your machine, and nothing is stored in the cloud except the Gemini API call itself.

---

## Before you start

You need four things installed:

| Tool | Minimum | Get it |
|---|---|---|
| Python | 3.9+ (3.11 recommended) | [python.org](https://www.python.org/downloads/) |
| Node.js | 18+ | [nodejs.org](https://nodejs.org/) |
| Rust | latest stable | [rustup.rs](https://rustup.rs) (also installs `cargo`) |
| Gemini API key | Required | [aistudio.google.com](https://aistudio.google.com) (free tier works) |

> **Windows note:** Rust requires the MSVC build tools. The `rustup` installer will prompt you to install them if they are missing, so allow it when prompted. The first `tauri dev` run will also download WebView2 if it isn't already present. Expect the first build to take 5–10 minutes while Cargo compiles dependencies.

---

## Option A: Run from source (recommended for dev)

**Step 1.** Double-click **`setup.bat`** in the project root.
- Creates the Python virtual environment
- Installs all Python and Node dependencies
- Creates `.env` and prompts you for your Gemini API key
- Initialises the SQLite database

**Step 2.** Double-click **`LaunchLedger.vbs`** any time you want to open the app.
- Kills any stale processes on the backend ports
- Opens a terminal and starts the app in dev mode (hot-reload)

That's it. You don't need to touch anything else for day-to-day use.

---

## Option B: Install as a Windows app

This builds a proper `.exe` installer so the app appears in your Start menu like any other Windows program. **You must complete Option A setup first**, because the installed app still reads your `.env` and uses the Python backend from this project folder.

**Step 1.** Complete Option A (setup.bat + confirm the app runs in dev mode).

**Step 2.** Open a terminal in the project root and run:
```
.\ledger.ps1 build
```
This compiles the frontend, packages it as a native app, and copies the installer to the project root when it finishes.

**Step 3.** Double-click the **`Health Ledger_x.x.x_x64-setup.exe`** that appears in the project root.

The installer registers the app with Windows. You can now launch it from the Start menu.

> **Note:** If you move or rename the project folder after installing, re-run `setup.bat` to re-register the new path, then re-install.

---

## Developer commands

All dev operations go through `ledger.ps1`. Open a terminal in the project root:

```powershell
.\ledger.ps1 setup    # first-time install
.\ledger.ps1 dev      # kill ports + start dev mode (same as LaunchLedger.vbs)
.\ledger.ps1 build    # build production installer
.\ledger.ps1 kill     # free ports 8787 / 3000 / 3001 without starting anything
.\ledger.ps1 help     # print this list
```

The CLI (for querying the database directly, testing extraction, etc.):
```powershell
.venv\Scripts\python.exe ledger_cli.py help
```

---

## Configuration

All config lives in `.env` (created automatically by `setup.bat`). The only required value is your API key:

```env
GEMINI_API_KEY=your_key_here
```

Everything else has sensible defaults. Optional overrides:

```env
# Personalise LEDGER's system prompts (your name is never committed to git)
USER_NAME=Your Name

# Model routing (defaults shown)
GEMINI_MODEL=gemini-2.5-flash
EXTRACTOR_MODEL=gemini-2.5-flash
CONVERSATION_MODEL=gemini-2.5-pro

# Storage paths
DATABASE_PATH=data/ledger.sqlite3
MEM0_USER_ID=primary_user
MEM0_QDRANT_PATH=data/memory/qdrant
MEM0_COLLECTION_NAME=health_ledger_semantic
```

### Garmin sync (optional)

Garmin sync is entirely optional, and the app works without it. If you want automatic sleep, HRV, and heart rate data pulled from your Garmin device, add your credentials to `.env`:

```env
GARMIN_EMAIL=your.garmin@email.com
GARMIN_PASSWORD=your_garmin_password
```

Then click the sync button in the app. Garmin rate-limits aggressive login attempts (~24–48 hr block if you hit it repeatedly), so authenticate once and let the token refresh automatically.

---

## Architecture

```
Tauri desktop window
  └─ Next.js 15 frontend (port 3000, Turbopack)
       ├─ Chat Panel   →  POST /api/v1/chat/ingest   (SSE stream)
       └─ Dashboard    ←  GET  /api/v1/get/history?entry_type=...

FastAPI backend (port 8787, spawned automatically by Tauri)
  ├─ api.py           →  routing, SSE streaming, Garmin sync
  ├─ llm_service.py   →  Gemini 2.5 Flash (extraction) + Pro (conversation)
  ├─ memory_layer.py  →  Mem0 + local Qdrant (semantic recall)
  └─ init_storage.py  →  SQLite WAL + 4-table schema
```

All data is local. The only outbound traffic is Gemini API calls.

---

## Troubleshooting

**`GEMINI_API_KEY is not set`**
Open `.env` and confirm `GEMINI_API_KEY=your_key_here` is filled in. Then restart the app.

**App won't start / "virtual environment not found"**
Run `setup.bat` again. It's safe to re-run and won't overwrite your data or `.env`.

**Dashboard shows "Failed to fetch"**
The backend is still starting. Wait 5–10 seconds and refresh. If it persists, check `logs\backend.log` for errors.

**Installed app stopped working after moving the project folder**
Re-run `setup.bat` from the new location to re-register the path, then reinstall using `.\ledger.ps1 build`.

**Ports already in use**
Run `.\ledger.ps1 kill` or double-click `LaunchLedger.vbs` (it kills stale ports automatically before launching).

**Mem0 / semantic memory is slow on first run**
This is normal: Qdrant initialises an on-disk collection and loads the embedding model on the first call. Subsequent calls are fast.

---

## Data location

All data is local and excluded from version control:

```
data/
├── ledger.db              # structured ledger (supplements, biometrics, labs, journals)
└── memory/
    ├── qdrant/            # local vector store
    └── mem0_state/        # Mem0 state directory
logs/
└── backend.log            # backend stdout/stderr
```

To reset everything (PowerShell):
```powershell
Remove-Item -Recurse -Force data\
.venv\Scripts\python.exe init_storage.py
```

---

## API reference

Interactive docs at `http://localhost:8787/docs` while the backend is running.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/health` | Liveness check |
| `POST` | `/api/v1/chat/ingest` | Natural language → extraction → ledger (SSE stream) |
| `GET` | `/api/v1/chat/history` | Chat history |
| `DELETE` | `/api/v1/chat/history` | Clear chat history |
| `GET` | `/api/v1/get/history` | Paginated history for any entry type |
| `DELETE` | `/api/v1/entry/{type}/{id}` | Hard-delete a single entry |
| `PATCH` | `/api/v1/entry/{type}/{id}` | Update a single field |
| `POST` | `/api/v1/sync/garmin` | Sync Garmin biometrics (last 30 days) |

from __future__ import annotations

import asyncio
import concurrent.futures
import faulthandler
import json
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
import warnings
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Tuple
from uuid import UUID, uuid4

# ── Logging configuration ──────────────────────────────────────────────────────
# Must run before any other module-level code so early errors are captured.

def _configure_logging() -> None:
    _BASE = Path(__file__).resolve().parent
    log_dir = _BASE / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ledger.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file (5 MB per file, 3 backups → max ~20 MB on disk)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    # Console (stderr so Tauri's sidecar capture still works)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Suppress noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()

# Suppress Python 3.14 deprecation warning from google-genai's use of _UnionGenericAlias.
# SDK-internal issue; message-based matching is more reliable than module regex on Windows.
warnings.filterwarnings("ignore", message=r".*_UnionGenericAlias.*", category=DeprecationWarning)

# ── Crash handler ──────────────────────────────────────────────────────────────
# Print a C-level stack trace on segfault instead of dying silently.
faulthandler.enable()

import psutil

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from fastapi.responses import JSONResponse, StreamingResponse

from init_storage import DATABASE_PATH, WAL_PATH, initialize_storage, migrate_chat_session_id, migrate_clinical_nodes_temporal, migrate_transcript_tables, migrate_chat_memory_table, migrate_cognition_tables, migrate_compound_to_supplement
from llm_service import chat_respond, chat_respond_stream, check_in_stream, extract, extract_from_file, get_all_protocols, get_regimen_context, import_interpret_stream, parse_csv_direct
from memory_layer import add_semantic_memory, retrieve_context
from models import AIExtractionPayload, BiometricLog, ClinicalNode, SupplementLog, DailyJournal, LabResult, UPDATABLE_FIELDS
from reflection_worker import run_morning_synthesis
from red_team_worker import run_weekly_audit
from cognition_worker import (
    run_cognition_tick,
    get_cognition_log,
    record_feedback as record_cognition_feedback,
    HEARTBEAT_SECONDS as COGNITION_HEARTBEAT_SECONDS,
    STARTUP_DELAY_SECONDS as COGNITION_STARTUP_DELAY,
)
import transcript_memory
import chat_memory
from event_bus import bus, register_main_loop


logger = logging.getLogger(__name__)

# ── Mem0 process isolation ─────────────────────────────────────────────────────
# Qdrant's native extensions can segfault on Windows, which kills the entire
# uvicorn process. Running Mem0 in a dedicated subprocess means a native crash
# only kills that worker; uvicorn and the rest of the app keep running.

_mem0_pool: Optional[concurrent.futures.ProcessPoolExecutor] = None
# fastembed's BM25 native extension segfaults on Windows with Python 3.14; every
# subprocess call crashes before returning, producing a noisy C-level stack dump.
# Disable by default until the upstream issue is resolved (qdrant-client / fastembed
# upgrade, or switching the vector store backend to Chroma/FAISS).
# Set MEM0_ENABLED=true in .env to re-enable when fixed.
_mem0_disabled = os.getenv("MEM0_ENABLED", "false").lower() not in ("1", "true", "yes")


def _mem0_worker_init() -> None:
    import warnings as _w
    _w.filterwarnings("ignore", category=DeprecationWarning)


def _get_mem0_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _mem0_pool
    if _mem0_pool is None:
        _mem0_pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=1, initializer=_mem0_worker_init
        )
    return _mem0_pool


async def _mem0_retrieve_safe(query: str) -> str:
    """Semantic recall of prior conversation snippets.

    Despite the legacy name, this now routes through the embedding-backed
    ``chat_memory`` store rather than Mem0/Qdrant (dead on Windows; see CLAUDE.md).
    Bounded top-K cosine retrieval; never fatal.
    """
    try:
        return await run_in_threadpool(chat_memory.retrieve_chat_memory, query)
    except Exception as exc:
        logger.warning("[chat_memory] retrieval failed (non-fatal): %s", exc)
        return ""


async def _transcript_retrieve_safe(query: str) -> str:
    """Retrieve verbatim transcript snippets relevant to the query. Never fatal."""
    try:
        return await run_in_threadpool(transcript_memory.retrieve_transcript_context, query)
    except Exception as exc:
        logger.warning("[transcript] retrieve failed (non-fatal): %s", exc)
        return ""


async def _mem0_add_safe(text: str) -> None:
    """Persist a conversation snippet for future semantic recall.

    Despite the legacy name, this now writes to the embedding-backed
    ``chat_memory`` store rather than Mem0/Qdrant (dead on Windows; see CLAUDE.md).
    Best-effort; never fatal.
    """
    try:
        await run_in_threadpool(chat_memory.add_chat_memory, text)
    except Exception as exc:
        logger.warning("[chat_memory] add failed (non-fatal): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    # ── Startup ────────────────────────────────────────────────────────────────
    _run_startup_migrations()
    register_main_loop(asyncio.get_running_loop())
    await _boot_morning_sequence()
    asyncio.create_task(_cognition_heartbeat())
    yield
    # ── Shutdown: nothing needed ───────────────────────────────────────────────


app = FastAPI(title="Semantic Health Ledger API", version="1.0.0", lifespan=lifespan)


def _migrate_garmin_noon_utc() -> None:
    """Shift garmin_sync midnight-UTC timestamps to noon-UTC (idempotent).

    Garmin records were stored at 00:00:00+00:00 which renders as the previous
    calendar day in US timezones (EDT=UTC-4, PDT=UTC-7, etc.).  Noon UTC is
    always the correct local date across all US timezones.
    """
    conn = get_db_conn()
    try:
        result = conn.execute(
            "UPDATE biometric_logs "
            "SET recorded_at = substr(recorded_at, 1, 10) || 'T12:00:00+00:00' "
            "WHERE context = 'garmin_sync' AND recorded_at LIKE '%T00:00:00+00:00'"
        )
        if result.rowcount:
            logger.info(f"[migration] fixed {result.rowcount} garmin_sync timestamp(s) → noon UTC")
        conn.commit()
    except Exception as exc:
        logger.warning(f"[migration] garmin noon-UTC migration failed (non-fatal): {exc}")
    finally:
        conn.close()


def _migrate_lab_reference_ranges() -> None:
    """Backfill null reference_low / reference_high on existing lab_results rows.

    Uses the same _STANDARD_LAB_RANGES lookup used at commit time.  Idempotent:
    only touches rows where at least one bound is still null.
    """
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, marker_name, reference_low, reference_high "
            "FROM lab_results WHERE reference_low IS NULL OR reference_high IS NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            ref = _STANDARD_LAB_RANGES.get(_norm(row["marker_name"]))
            if not ref:
                continue
            low, high = ref
            changes: Dict[str, float] = {}
            if row["reference_low"]  is None and low  is not None:
                changes["reference_low"]  = low
            if row["reference_high"] is None and high is not None:
                changes["reference_high"] = high
            if changes:
                set_clause = ", ".join(f"{col} = ?" for col in changes)
                conn.execute(
                    f"UPDATE lab_results SET {set_clause} WHERE id = ?",  # noqa: S608
                    [*changes.values(), row["id"]],
                )
                updated += 1
        if updated:
            conn.commit()
            logger.info(f"[startup] backfilled reference ranges on {updated} lab rows")
    finally:
        conn.close()


def _migrate_derived_labs() -> None:
    """Compute derived lab values (ratios, differences) for all existing collection dates."""
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT DATE(collected_at) FROM lab_results"
        ).fetchall()]
        cursor = conn.cursor()
        for date_str in dates:
            _compute_derived_labs(cursor, date_str)
        conn.commit()
        if dates:
            logger.info(f"[startup] computed derived labs for {len(dates)} collection date(s)")
    finally:
        conn.close()


def _migrate_journal_unique_index() -> None:
    """Add a UNIQUE index on daily_journals(journal_date) if it doesn't exist.

    Prevents duplicate rows for the same calendar day; concurrent upsert_daily_journal
    calls for the same date will now fail cleanly on the second INSERT (IntegrityError)
    rather than silently writing a duplicate row.
    """
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uidx_daily_journals_date "
            "ON daily_journals(journal_date)"
        )
        conn.commit()
    finally:
        conn.close()


def _run_startup_migrations() -> None:
    """Run all idempotent schema migrations. Errors are logged, never re-raised."""
    for name, fn in [
        # Must run before anything opens a connection / creates the schema, so the
        # real compound_logs data is renamed in rather than orphaned by an empty table.
        ("compound_to_supplement",  migrate_compound_to_supplement),
        ("chat_session_id",         migrate_chat_session_id),
        ("garmin_noon_utc",         _migrate_garmin_noon_utc),
        ("lab_reference_ranges",    _migrate_lab_reference_ranges),
        ("derived_labs",            _migrate_derived_labs),
        ("clinical_nodes_temporal", migrate_clinical_nodes_temporal),
        ("journal_unique_index",    _migrate_journal_unique_index),
        ("transcript_tables",       migrate_transcript_tables),
        ("chat_memory_table",       migrate_chat_memory_table),
        ("cognition_tables",        migrate_cognition_tables),
    ]:
        try:
            fn()
        except Exception as exc:
            logger.error(f"[startup] migration '{name}' failed: {exc}")


# Minimum seconds between manual synthesis runs as a server-side guard against
# accidental double-clicks or rapid resubmissions.
SYNTHESIS_COOLDOWN_SECONDS = 60  # 1 minute


def _get_system_state(key: str) -> Optional[str]:
    conn = get_db_conn()
    try:
        row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_system_state(key: str, value: str) -> None:
    conn = get_db_conn()
    try:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


async def _run_synthesis_and_record(today: str) -> None:
    try:
        logger.info("[boot] morning synthesis starting...")
        # run_morning_synthesis is sync (all blocking I/O) and must run in thread pool
        result = await run_in_threadpool(run_morning_synthesis)
        _set_system_state("last_synthesis_date", today)
        logger.info(f"[boot] synthesis complete: {result}")
    except Exception as exc:
        logger.error(f"[boot] morning synthesis failed: {exc}")


async def _boot_morning_sequence() -> None:
    today = date.today().isoformat()
    last = _get_system_state("last_synthesis_date")
    if last == today:
        logger.info("[boot] synthesis already ran today, skipping")
        return
    logger.info("[boot] first boot of the day: triggering morning sequence")

    # Garmin sync: pull any missing data from the last 2 days (last night's sleep)
    email    = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    if email and password:
        try:
            missing = await run_in_threadpool(_garmin_missing_dates, 2)
            if missing:
                logger.info(f"[boot] Garmin gap-fill for {len(missing)} date(s): {missing}")
                result = await run_in_threadpool(_do_garmin_sync, email, password, missing)
                mem0_summary = result.pop("_mem0_summary", "")
                if mem0_summary:
                    await _mem0_add_safe(mem0_summary)
        except Exception as exc:
            logger.warning(f"[boot] Garmin pre-synthesis sync failed (non-fatal): {exc}")
    else:
        logger.debug("[boot] GARMIN_EMAIL/PASSWORD not set, skipping pre-synthesis sync")

    # Synthesis runs as a background task so the API is immediately available
    asyncio.create_task(_run_synthesis_and_record(today))


# ── Cognition loop (the "mind" heartbeat) ───────────────────────────────────────

def _latest_session_id() -> str:
    """Session id of the most recent chat message, so proactive thoughts land in the
    conversation the user is actually looking at. Falls back to 'default'."""
    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT session_id FROM chat_history ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row and row[0] else "default"
    except Exception:  # noqa: BLE001
        return "default"
    finally:
        conn.close()


async def _run_cognition_tick_and_dispatch(force_speak: bool = False) -> Dict[str, Any]:
    """Run one cognition tick (in a thread) and push its results onto the event bus.

    Every tick emits COGNITION_TICK so the Mind panel updates live. When the tick
    decides to speak, the message is persisted to chat_history (role='model') and
    emitted as LEDGER_THOUGHT so it streams into the open chat as an unsolicited turn.
    """
    result = await run_in_threadpool(run_cognition_tick, force_speak)

    try:
        await bus.emit("COGNITION_TICK", {
            "kind": "spoke" if result.get("spoke") else ("observation" if result.get("salience") else "idle"),
            "summary": result.get("summary", ""),
            "salience": result.get("salience", 0.0),
            "open_threads": result.get("open_threads", 0),
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cognition] COGNITION_TICK emit failed (non-fatal): %s", exc)

    if result.get("spoke") and result.get("content"):
        content = result["content"]
        session_id = await run_in_threadpool(_latest_session_id)
        try:
            msg_id = await run_in_threadpool(_insert_chat_message, "model", content, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("[cognition] failed to persist spoken thought: %s", exc)
            msg_id = None
        try:
            await bus.emit("LEDGER_THOUGHT", {
                "id": msg_id,
                "content": content,
                "title": result.get("title"),
                "signature": result.get("signature"),
                "session_id": session_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cognition] LEDGER_THOUGHT emit failed (non-fatal): %s", exc)

    return result


async def _cognition_heartbeat() -> None:
    """Background loop: think every COGNITION_HEARTBEAT_SECONDS while the app runs."""
    await asyncio.sleep(COGNITION_STARTUP_DELAY)
    logger.info("[cognition] heartbeat started (every %ss)", COGNITION_HEARTBEAT_SECONDS)
    while True:
        try:
            await _run_cognition_tick_and_dispatch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[cognition] heartbeat tick error: %s", exc)
        await asyncio.sleep(COGNITION_HEARTBEAT_SECONDS)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_PROCESS = psutil.Process(os.getpid())
_START_TIME = time.time()


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    logging_enabled: bool = True
    session_id: str = "default"


class RewindPayload(BaseModel):
    """Payload for POST /api/v1/chat/rewind.

    Edits an existing user message in-place, deletes every subsequent row in the
    session, then re-runs the full ingest pipeline on the new content so LEDGER
    regenerates its response from the rewound point forward.
    """
    session_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=256)
    new_content: str = Field(min_length=1, max_length=4000)


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def append_to_wal(raw_text: str, source: str = "user") -> None:
    WAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    wal_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "raw_text": raw_text,
    }
    with WAL_PATH.open("a", encoding="utf-8") as wal_file:
        wal_file.write(json.dumps(wal_entry, default=json_default) + "\n")


def get_db_conn() -> sqlite3.Connection:
    initialize_storage(DATABASE_PATH, WAL_PATH)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def model_as_wal_text(model: Any) -> str:
    raw_text = getattr(model, "raw_text", None)
    if raw_text:
        return raw_text
    return model.model_dump_json()


def insert_supplement(cursor: sqlite3.Cursor, supplement: SupplementLog) -> None:
    cursor.execute(
        """
        INSERT INTO supplement_logs (
            id, recorded_at, supplement_name, dose_value, dose_unit, route,
            site, protocol_phase, notes, raw_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(supplement.id),
            supplement.recorded_at.isoformat(),
            supplement.supplement_name,
            str(supplement.dose_value),
            supplement.dose_unit,
            supplement.route,
            supplement.site,
            supplement.protocol_phase,
            supplement.notes,
            supplement.raw_text,
        ),
    )


def insert_biometric(cursor: sqlite3.Cursor, biometric: BiometricLog) -> None:
    cursor.execute(
        """
        INSERT INTO biometric_logs (
            id, recorded_at, metric_name, value, unit, context, notes, raw_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(biometric.id),
            biometric.recorded_at.isoformat(),
            biometric.metric_name,
            str(biometric.value),
            biometric.unit,
            biometric.context,
            biometric.notes,
            biometric.raw_text,
        ),
    )


# ── Standard reference ranges lookup ─────────────────────────────────────────
# Applied at commit time to fill null reference_low / reference_high fields.
# Extraction-provided ranges always take priority; this is a fallback only.
# Keys are lowercase-normalised marker names (punctuation stripped, extra spaces
# collapsed).  Tuple is (low, high); None means the bound does not apply.
# Ranges are general adult defaults unless otherwise noted.

_STANDARD_LAB_RANGES: Dict[str, tuple] = {
    # CBC
    "red blood cell count": (4.5, 5.9),
    "rbc": (4.5, 5.9),
    "hemoglobin": (13.2, 17.1),
    "hgb": (13.2, 17.1),
    "hematocrit": (38.5, 50.0),
    "hct": (38.5, 50.0),
    "mcv": (80.0, 100.0),
    "mch": (27.0, 33.0),
    "mchc": (32.0, 36.0),
    "rdw": (None, 14.5),
    "rdw-cv": (None, 14.5),
    "rdw rdw-cv": (None, 14.5),
    "platelet count": (150.0, 400.0),
    "platelets": (150.0, 400.0),
    "plt": (150.0, 400.0),
    "mpv": (7.5, 12.5),
    "wbc": (4.0, 11.0),
    "white blood cell count": (4.0, 11.0),
    "white blood cells": (4.0, 11.0),
    # Differential: percentages
    "neutrophils": (40.0, 74.0),
    "neutrophils %": (40.0, 74.0),
    "neutrophil %": (40.0, 74.0),
    "lymphocytes": (20.0, 45.0),
    "lymphocytes %": (20.0, 45.0),
    "monocytes": (2.0, 10.0),
    "monocytes %": (2.0, 10.0),
    "eosinophils": (1.0, 6.0),
    "eosinophils %": (1.0, 6.0),
    "basophils": (0.0, 2.0),
    "basophils %": (0.0, 2.0),
    # Differential: absolute counts (cells/uL)
    "absolute neutrophils": (1800.0, 7700.0),
    "absolute neutrophil count": (1800.0, 7700.0),
    "absolute lymphocytes": (1000.0, 4800.0),
    "absolute lymphocyte count": (1000.0, 4800.0),
    "absolute monocytes": (200.0, 1000.0),
    "absolute monocyte count": (200.0, 1000.0),
    "absolute eosinophils": (15.0, 500.0),
    "absolute eosinophil count": (15.0, 500.0),
    "absolute basophils": (0.0, 200.0),
    "absolute basophil count": (0.0, 200.0),
    # CMP / BMP
    "glucose": (70.0, 99.0),
    "glucose fasting": (70.0, 99.0),
    "fasting glucose": (70.0, 99.0),
    "bun": (7.0, 25.0),
    "blood urea nitrogen": (7.0, 25.0),
    "creatinine": (0.74, 1.35),
    "egfr": (60.0, None),
    "egfr creatinine": (60.0, None),
    "bun/creatinine ratio": (10.0, 20.0),
    "bun creatinine ratio": (10.0, 20.0),
    "sodium": (136.0, 145.0),
    "potassium": (3.5, 5.1),
    "chloride": (98.0, 107.0),
    "carbon dioxide": (22.0, 29.0),
    "co2": (22.0, 29.0),
    "bicarbonate": (22.0, 29.0),
    "anion gap": (3.0, 11.0),
    "calcium": (8.5, 10.5),
    "calcium total": (8.5, 10.5),
    "total protein": (6.0, 8.5),
    "albumin": (3.5, 5.0),
    "globulin": (2.0, 3.5),
    "albumin/globulin ratio": (1.2, 2.2),
    "albumin globulin ratio": (1.2, 2.2),
    "a/g ratio": (1.2, 2.2),
    "ag ratio": (1.2, 2.2),
    # Liver
    "alt": (7.0, 56.0),
    "alanine aminotransferase": (7.0, 56.0),
    "sgpt": (7.0, 56.0),
    "ast": (10.0, 40.0),
    "aspartate aminotransferase": (10.0, 40.0),
    "sgot": (10.0, 40.0),
    "alp": (44.0, 147.0),
    "alkaline phosphatase": (44.0, 147.0),
    "total bilirubin": (0.2, 1.2),
    "direct bilirubin": (0.0, 0.3),
    "indirect bilirubin": (0.2, 1.2),
    "ggt": (8.0, 61.0),
    "gamma-glutamyl transferase": (8.0, 61.0),
    # Lipids
    "total cholesterol": (None, 200.0),
    "cholesterol": (None, 200.0),
    "hdl-cholesterol": (40.0, None),
    "hdl cholesterol": (40.0, None),
    "hdl": (40.0, None),
    "ldl-cholesterol": (None, 100.0),
    "ldl cholesterol": (None, 100.0),
    "ldl": (None, 100.0),
    "triglycerides": (None, 150.0),
    "tg": (None, 150.0),
    "non-hdl cholesterol": (None, 130.0),
    "non hdl cholesterol": (None, 130.0),
    "total cholesterol/hdl ratio": (None, 5.0),
    "total cholesterol hdl ratio": (None, 5.0),
    "ldl/hdl ratio": (None, 3.0),
    "ldl hdl ratio": (None, 3.0),
    "triglyceride/hdl ratio": (None, 3.0),
    "triglyceride hdl ratio": (None, 3.0),
    # Iron panel
    "serum iron": (60.0, 170.0),
    "iron": (60.0, 170.0),
    "tibc": (240.0, 450.0),
    "total iron binding capacity": (240.0, 450.0),
    "uibc": (100.0, 370.0),
    "unsaturated iron binding capacity": (100.0, 370.0),
    "transferrin saturation": (20.0, 50.0),
    "transferrin saturation tsat": (20.0, 50.0),
    "tsat": (20.0, 50.0),
    "ferritin": (30.0, 400.0),
    # Vitamins / Minerals
    "vitamin d": (30.0, 100.0),
    "vitamin d 25-oh": (30.0, 100.0),
    "vitamin d 25 oh": (30.0, 100.0),
    "25-oh vitamin d": (30.0, 100.0),
    "25 oh vitamin d": (30.0, 100.0),
    "vitamin b12": (200.0, 900.0),
    "b12": (200.0, 900.0),
    "cobalamin": (200.0, 900.0),
    "folate": (5.4, 20.0),
    "folate serum": (5.4, 20.0),
    "serum folate": (5.4, 20.0),
    "folic acid": (5.4, 20.0),
    "magnesium": (1.7, 2.2),
    "zinc": (60.0, 130.0),
    "phosphorus": (2.5, 4.5),
    "phosphate": (2.5, 4.5),
    "uric acid": (3.5, 7.2),
    # Metabolic / inflammation
    "hba1c": (None, 5.7),
    "hemoglobin a1c": (None, 5.7),
    "a1c": (None, 5.7),
    "insulin": (2.6, 24.9),
    "fasting insulin": (2.6, 24.9),
    "crp": (None, 10.0),
    "c-reactive protein": (None, 10.0),
    "hs-crp": (None, 1.0),
    "high sensitivity crp": (None, 1.0),
    "high-sensitivity c-reactive protein": (None, 1.0),
    "homocysteine": (None, 15.0),
    # Apolipoproteins / advanced lipids
    "apolipoprotein b": (None, 100.0),
    "apob": (None, 100.0),
    "apo b": (None, 100.0),
    "apolipoprotein a-1": (110.0, 190.0),
    "apolipoprotein a1": (110.0, 190.0),
    "apoa1": (110.0, 190.0),
    "apo a-1": (110.0, 190.0),
    "lipoprotein a": (None, 30.0),
    "lp(a)": (None, 30.0),
    "lpa": (None, 30.0),
    "vldl cholesterol": (None, 30.0),
    "vldl": (None, 30.0),
    # Kidney / urinary
    "cystatin c": (0.62, 1.11),
    "microalbumin urine": (None, 30.0),
    "urine albumin creatinine ratio": (None, 30.0),
    "uacr": (None, 30.0),
    "albumin creatinine ratio": (None, 30.0),
    # Cardiac / muscle enzymes
    "creatine kinase": (55.0, 170.0),
    "ck": (55.0, 170.0),
    "cpk": (55.0, 170.0),
    "ldh": (140.0, 280.0),
    "lactate dehydrogenase": (140.0, 280.0),
    "troponin i": (None, 0.04),
    "troponin t": (None, 0.01),
    "hs troponin": (None, 0.019),
    # Coagulation
    "fibrinogen": (200.0, 400.0),
    "prothrombin time": (11.0, 13.5),
    "pt": (11.0, 13.5),
    "inr": (0.8, 1.1),
    "aptt": (25.0, 35.0),
    # Hematology extras
    "reticulocyte count": (0.5, 1.5),
    "reticulocytes": (0.5, 1.5),
    "immature reticulocyte fraction": (None, 0.14),
    "nrbc": (None, 0.5),
    "absolute reticulocyte count": (25000.0, 75000.0),
    # Inflammation extras
    "esr": (None, 20.0),
    "erythrocyte sedimentation rate": (None, 20.0),
    "sed rate": (None, 20.0),
    "il-6": (None, 7.0),
    "interleukin-6": (None, 7.0),
    "interleukin 6": (None, 7.0),
    "fibrin degradation products": (None, 5.0),
    "d-dimer": (None, 0.5),
}

import re as _re

def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, strip common punctuation for table lookup."""
    s = s.lower()
    s = _re.sub(r"[()%\-/]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s

# Rebuild the lookup table with normalised keys so lookups work regardless of
# whether the source string contains hyphens, slashes, or parentheses.
_STANDARD_LAB_RANGES = {_norm(k): v for k, v in _STANDARD_LAB_RANGES.items()}


def _apply_standard_ranges(lab: LabResult) -> LabResult:
    """Fill null reference_low / reference_high from the standard lookup table.

    Extraction-provided values always win.  This is a no-op for markers that
    already have both bounds, or whose normalised name is not in the table.
    """
    if lab.reference_low is not None and lab.reference_high is not None:
        return lab

    row = _STANDARD_LAB_RANGES.get(_norm(lab.marker_name))
    if row is None:
        return lab

    low, high = row
    if lab.reference_low is None and low is not None:
        lab.reference_low = low
    if lab.reference_high is None and high is not None:
        lab.reference_high = high
    return lab


# ── Derived lab value calculation ────────────────────────────────────────────
# Ratios and differences computed from same-date source markers.
# Only fills rows that already exist with null value_numeric.

_DERIVED_RATIOS: list = [
    # (result_marker, numerator_marker, denominator_marker)
    ("BUN/Creatinine Ratio",       "BUN",             "Creatinine"),
    ("Total Cholesterol/HDL Ratio","Total Cholesterol","HDL-Cholesterol"),
    ("LDL/HDL Ratio",              "LDL-Cholesterol",  "HDL-Cholesterol"),
    ("Triglyceride/HDL Ratio",     "Triglycerides",    "HDL-Cholesterol"),
]

_DERIVED_DIFFERENCES: list = [
    # (result_marker, minuend_marker, subtrahend_marker)
    ("Non-HDL Cholesterol", "Total Cholesterol", "HDL-Cholesterol"),
]


def _compute_derived_labs(cursor: sqlite3.Cursor, date_str: str) -> None:
    """Compute derived lab values from same-date source markers and write them back.

    Operates only on rows that already exist in lab_results with a null value_numeric.
    Safe to call multiple times because the WHERE clause prevents overwriting real values.
    """
    def get_val(marker: str) -> Optional[float]:
        row = cursor.execute(
            "SELECT value_numeric FROM lab_results "
            "WHERE marker_name = ? AND DATE(collected_at) = DATE(?) LIMIT 1",
            (marker, date_str),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def write_derived(result_marker: str, computed: float) -> None:
        ref = _STANDARD_LAB_RANGES.get(_norm(result_marker))
        low  = ref[0] if ref else None
        high = ref[1] if ref else None
        flagged = 1 if (
            (high is not None and computed > high) or
            (low  is not None and computed < low)
        ) else 0
        cursor.execute(
            "UPDATE lab_results SET value_numeric = ?, flagged = ? "
            "WHERE marker_name = ? AND DATE(collected_at) = DATE(?) AND value_numeric IS NULL",
            (str(round(computed, 2)), flagged, result_marker, date_str),
        )

    for result, num_name, den_name in _DERIVED_RATIOS:
        num = get_val(num_name)
        den = get_val(den_name)
        if num is not None and den is not None and den != 0:
            write_derived(result, num / den)

    for result, minuend_name, subtrahend_name in _DERIVED_DIFFERENCES:
        minuend    = get_val(minuend_name)
        subtrahend = get_val(subtrahend_name)
        if minuend is not None and subtrahend is not None:
            write_derived(result, minuend - subtrahend)


def insert_lab_result(cursor: sqlite3.Cursor, lab: LabResult) -> None:
    cursor.execute(
        """
        INSERT INTO lab_results (
            id, collected_at, resulted_at, panel_name, marker_name, value_type,
            value_numeric, value_text, unit, reference_low, reference_high,
            lab_name, flagged, notes, raw_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(lab.id),
            lab.collected_at.isoformat(),
            lab.resulted_at.isoformat() if lab.resulted_at else None,
            lab.panel_name,
            lab.marker_name,
            lab.value_type,
            str(lab.value_numeric) if lab.value_numeric is not None else None,
            lab.value_text,
            lab.unit,
            str(lab.reference_low) if lab.reference_low is not None else None,
            str(lab.reference_high) if lab.reference_high is not None else None,
            lab.lab_name,
            int(lab.flagged),
            lab.notes,
            lab.raw_text,
        ),
    )


def insert_daily_journal(cursor: sqlite3.Cursor, journal: DailyJournal) -> None:
    """Raw INSERT. Only call this when no row exists for journal_date.
    For the public write path use upsert_daily_journal instead."""
    cursor.execute(
        """
        INSERT INTO daily_journals (
            id, journal_date, mood, energy_score, sleep_hours, symptoms,
            training, nutrition, notes, raw_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(journal.id),
            journal.journal_date.isoformat(),
            journal.mood,
            journal.energy_score,
            str(journal.sleep_hours) if journal.sleep_hours is not None else None,
            json.dumps(journal.symptoms),
            journal.training,
            journal.nutrition,
            journal.notes,
            journal.raw_text,
        ),
    )


_JOURNAL_DIVIDER = "\n\n---\n\n"


def upsert_daily_journal(
    cursor: sqlite3.Cursor,
    journal: DailyJournal,
) -> str:
    """Rolling daily journal upsert with strict string deduplication.

    Returns one of three strings describing what happened:
      "inserted": no prior row existed; standard INSERT was performed.
      "updated":  row existed; new, genuinely-novel text was appended.
      "skipped":  row existed but incoming notes were already a substring
                   of the stored notes; nothing was written.

    Dedup logic
    -----------
    * notes is the only field that can grow across the day. Numerical fields
      (mood, energy_score, sleep_hours) take the new value when supplied.
    * Appending is gated on: incoming notes stripped is non-empty AND is NOT
      already present anywhere in the existing notes string. This catches both
      exact re-submissions ("pasted again") and partial repeats (a subset of
      text that was already stored).
    * If incoming notes are empty, numerical fields are still updated, but
      no divider or empty string is appended to notes.
    * symptoms is union-merged (preserving order, removing duplicates).
    * training / nutrition prefer the new value when non-None.
    """
    existing = cursor.execute(
        """
        SELECT id, notes, mood, energy_score, sleep_hours,
               symptoms, training, nutrition
        FROM daily_journals
        WHERE journal_date = ?
        LIMIT 1
        """,
        (journal.journal_date.isoformat(),),
    ).fetchone()

    if existing is None:
        # No entry for this date; standard INSERT.
        insert_daily_journal(cursor, journal)
        return "inserted"

    # ── Entry exists: check for duplicate notes ───────────────────────────────
    existing_notes: str = existing["notes"] or ""
    new_notes: str = (journal.notes or "").strip()

    # Dedup: skip only when new_notes exactly matches the whole existing note
    # OR exactly matches one of the already-stored segments (split by the
    # divider).  A plain substring check (`new_notes in existing_notes`) would
    # silently drop "tired" if the stored notes contain "felt tired all day",
    # which is a false positive that causes permanent data loss.
    if new_notes:
        existing_segments = [s.strip() for s in existing_notes.split(_JOURNAL_DIVIDER)]
        if new_notes in existing_segments:
            return "skipped"

    # ── Merge notes ───────────────────────────────────────────────────────────
    if new_notes:
        merged_notes = (
            existing_notes + _JOURNAL_DIVIDER + new_notes
            if existing_notes
            else new_notes
        )
    else:
        # No new text; preserve existing notes unchanged.
        merged_notes = existing_notes

    # ── Merge numerical fields (prefer incoming if provided) ──────────────────
    mood         = journal.mood         if journal.mood         is not None else existing["mood"]
    energy_score = journal.energy_score if journal.energy_score is not None else existing["energy_score"]
    sleep_hours  = journal.sleep_hours  if journal.sleep_hours  is not None else existing["sleep_hours"]

    # ── Merge symptoms (union, order-preserving dedup) ────────────────────────
    existing_symptoms: list = json.loads(existing["symptoms"] or "[]")
    new_symptoms: list = journal.symptoms or []
    seen: set = set()
    merged_symptoms: list = []
    for s in existing_symptoms + new_symptoms:
        if s not in seen:
            seen.add(s)
            merged_symptoms.append(s)

    # ── Merge free-text fields (prefer new non-None values) ──────────────────
    training  = journal.training  if journal.training  is not None else existing["training"]
    nutrition = journal.nutrition if journal.nutrition is not None else existing["nutrition"]

    # ── Check if anything actually changed; skip the UPDATE if not ────────────
    # Without this guard, submissions with no new notes and no changed numerical
    # fields return "updated" and increment the written counter even though
    # nothing was written to the database.
    nothing_changed = (
        merged_notes == existing_notes
        and mood         == existing["mood"]
        and energy_score == existing["energy_score"]
        and (str(sleep_hours) if sleep_hours is not None else None)
            == existing["sleep_hours"]
        and merged_symptoms == json.loads(existing["symptoms"] or "[]")
        and training  == existing["training"]
        and nutrition == existing["nutrition"]
    )
    if nothing_changed:
        return "skipped"

    cursor.execute(
        """
        UPDATE daily_journals
        SET notes        = ?,
            mood         = ?,
            energy_score = ?,
            sleep_hours  = ?,
            symptoms     = ?,
            training     = ?,
            nutrition    = ?
        WHERE id = ?
        """,
        (
            merged_notes,
            mood,
            energy_score,
            str(sleep_hours) if sleep_hours is not None else None,
            json.dumps(merged_symptoms),
            training,
            nutrition,
            existing["id"],
        ),
    )
    return "updated"


def commit_logs(
    supplements: Optional[List[SupplementLog]] = None,
    biometrics: Optional[List[BiometricLog]] = None,
    labs: Optional[List[LabResult]] = None,
    journals: Optional[List[DailyJournal]] = None,
) -> Dict[str, Any]:
    supplements = supplements or []
    biometrics = biometrics or []
    labs = labs or []
    journals = journals or []

    ins = {"supplement_logs": 0, "biometric_logs": 0, "lab_results": 0, "daily_journals": 0}
    skp = {"supplement_logs": 0, "biometric_logs": 0, "lab_results": 0, "daily_journals": 0}

    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        # Supplements: no dedup; the same supplement can be dosed multiple times legitimately.
        for supplement in supplements:
            insert_supplement(cursor, supplement)
            ins["supplement_logs"] += 1

        # Biometrics: skip if exact (metric_name, recorded_at) already exists.
        # Same metric at the same timestamp = re-submission of the same reading.
        # Logging the same metric later in the day is fine because the timestamp will differ.
        for biometric in biometrics:
            dup = cursor.execute(
                "SELECT 1 FROM biometric_logs WHERE metric_name = ? AND recorded_at = ? LIMIT 1",
                (biometric.metric_name, biometric.recorded_at.isoformat()),
            ).fetchone()
            if dup:
                skp["biometric_logs"] += 1
            else:
                insert_biometric(cursor, biometric)
                ins["biometric_logs"] += 1

        # Labs: deduplicate on (marker_name, DATE(collected_at)).
        # If the row already exists but is missing reference ranges or panel name,
        # fill those fields in rather than skipping to handle re-pasting a panel
        # with reference ranges after an initial paste that lacked them.
        for lab in [_apply_standard_ranges(l) for l in labs]:
            existing = cursor.execute(
                "SELECT id, reference_low, reference_high, panel_name "
                "FROM lab_results WHERE marker_name = ? AND DATE(collected_at) = DATE(?) LIMIT 1",
                (lab.marker_name, lab.collected_at.isoformat()),
            ).fetchone()
            if existing:
                to_update: Dict[str, str] = {}
                if existing["reference_low"]  is None and lab.reference_low  is not None:
                    to_update["reference_low"]  = str(lab.reference_low)
                if existing["reference_high"] is None and lab.reference_high is not None:
                    to_update["reference_high"] = str(lab.reference_high)
                if existing["panel_name"]     is None and lab.panel_name:
                    to_update["panel_name"]     = lab.panel_name
                if to_update:
                    set_clause = ", ".join(f"{col} = ?" for col in to_update)
                    cursor.execute(
                        f"UPDATE lab_results SET {set_clause} WHERE id = ?",  # noqa: S608
                        [*to_update.values(), existing["id"]],
                    )
                    ins["lab_results"] += 1   # count as updated
                else:
                    skp["lab_results"] += 1
            else:
                insert_lab_result(cursor, lab)
                ins["lab_results"] += 1

        # Compute derived ratios / differences for every unique collection date
        # that had labs in this batch (fills null value_numeric on existing rows).
        lab_dates = {l.collected_at.isoformat() for l in labs}
        for date_str in lab_dates:
            _compute_derived_labs(cursor, date_str)

        # Journals: rolling daily append with strict string deduplication.
        # upsert_daily_journal handles insert vs. append vs. skip internally.
        for journal in journals:
            outcome = upsert_daily_journal(cursor, journal)
            if outcome == "skipped":
                skp["daily_journals"] += 1
            else:
                ins["daily_journals"] += 1   # "inserted" or "updated" both count as written

        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database write transaction aborted: {exc}",
        ) from exc
    finally:
        conn.close()

    total_skipped = sum(skp.values())
    result: Dict[str, Any] = {**ins}
    if total_skipped:
        result["skipped"] = skp
    return result


# ── Chat history helpers ───────────────────────────────────────────────────────

# Fallback strings that llm_service yields when the model produces no usable output.
# These must NOT be saved as model turns because doing so poisons the conversation history
# and causes every subsequent Gemini request to see the failure as a prior turn,
# triggering a chain of empty responses.
_LLM_FALLBACK_RESPONSES: frozenset[str] = frozenset({
    "I wasn't able to generate a response. Please try again.",
    "Gemini flagged that response. Try rephrasing.",
    "I wasn't able to retrieve that data. Please try asking again.",
})


def _is_fallback_response(text: str) -> bool:
    """Return True when *text* is a known LLM fallback string (no real model output)."""
    stripped = text.strip()
    return stripped in _LLM_FALLBACK_RESPONSES or stripped.startswith("[stream error:")


def _insert_chat_message(role: str, content: str, session_id: str = "default", message_id: Optional[str] = None) -> str:
    """Persist a single chat turn to the chat_history table. Returns the saved message ID."""
    mid = message_id or str(uuid4())
    conn = get_db_conn()
    try:
        conn.execute(
            "INSERT INTO chat_history (id, role, content, session_id) VALUES (?, ?, ?, ?)",
            (mid, role, content, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return mid


def _get_session_history(session_id: str) -> List[Dict[str, str]]:
    """Return ALL messages for the given session in chronological order (oldest first).

    No LIMIT: the entire session is passed to Gemini so long conversations never
    lose context. Gemini 2.5 Pro's context window is large enough to handle this.
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM chat_history "
            "WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )
        return [{"role": r["role"], "content": r["content"]} for r in cursor.fetchall()]
    finally:
        conn.close()


def _get_history_before_rowid(session_id: str, before_rowid: int) -> List[Dict[str, str]]:
    """Return session messages with rowid < before_rowid, in chronological order.

    Used by rewind_chat to slice the context history the model receives when
    regenerating a response: everything before (and not including) the edited
    user message, so that message becomes the current turn rather than history.

    rowid is used (not created_at) because multiple messages can share the same
    created_at second; rowid is strictly insertion-ordered and gives the correct
    deterministic tiebreak.  VACUUM would renumber implicit rowids, but WAL mode
    with the default auto_vacuum=NONE means VACUUM never runs automatically.
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM chat_history "
            "WHERE session_id = ? AND rowid < ? ORDER BY created_at ASC, rowid ASC",
            (session_id, before_rowid),
        )
        return [{"role": r["role"], "content": r["content"]} for r in cursor.fetchall()]
    finally:
        conn.close()


def _get_recent_chat_history(limit: int = 20) -> List[Dict[str, str]]:
    """Return the last *limit* messages in chronological order. Used by check-in and file upload."""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM chat_history ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        conn.close()


async def _iter_sync_gen(
    gen_fn: Any, *args: Any, **kwargs: Any
) -> AsyncGenerator[Tuple[str, str], None]:
    """Run a synchronous generator in a daemon thread and bridge its output to
    async via asyncio.Queue so the SSE endpoint can await individual items."""
    q: asyncio.Queue[Optional[Tuple[str, str]]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _producer() -> None:
        try:
            for item in gen_fn(*args, **kwargs):
                loop.call_soon_threadsafe(q.put_nowait, item)
        except Exception as exc:
            loop.call_soon_threadsafe(q.put_nowait, ("error", str(exc)))
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)  # sentinel

    threading.Thread(target=_producer, daemon=True).start()
    while True:
        item = await q.get()
        if item is None:
            break
        yield item


_PROTOCOL_KEYWORDS = frozenset([
    "mg", "mcg", "iu", "dose", "protocol", "schedule", "half-life",
    "mechanism", "receptor", "pathway", "alternate", "timing",
    "absorption", "bioavailability", "chelated", "enzyme", "titrate",
    "substrate", "taper", "pulsing", "circadian", "half life",
])


def _is_protocol_response(text: str) -> bool:
    """Heuristic: true when the reply looks like clinical protocol advice."""
    if len(text) < 200:
        return False
    lower = text.lower()
    return sum(1 for kw in _PROTOCOL_KEYWORDS if kw in lower) >= 2


@app.get("/api/v1/health", status_code=status.HTTP_200_OK)
async def health_check() -> Dict[str, str]:
    return {"status": "ok"}


def _wal_calls_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        count = 0
        with WAL_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    if json.loads(line).get("timestamp", "")[:10] == today:
                        count += 1
                except json.JSONDecodeError:
                    continue
        return count
    except FileNotFoundError:
        return 0


@app.get("/api/v1/telemetry", status_code=status.HTTP_200_OK)
async def get_telemetry() -> Dict[str, Any]:
    cpu_pct = _PROCESS.cpu_percent(interval=None)
    mem_mb = round(_PROCESS.memory_info().rss / 1_048_576, 1)
    db_mb = round(DATABASE_PATH.stat().st_size / 1_048_576, 2) if DATABASE_PATH.exists() else 0.0
    uptime_s = int(time.time() - _START_TIME)

    records_today = 0
    try:
        conn = get_db_conn()
        try:
            cursor = conn.cursor()
            for table in ("supplement_logs", "biometric_logs", "lab_results", "daily_journals"):
                cursor.execute(  # noqa: B608
                    f"SELECT COUNT(*) FROM {table} WHERE DATE(created_at) = DATE('now')"
                )
                records_today += cursor.fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass

    api_calls_today = await run_in_threadpool(_wal_calls_today)

    return {
        "cpu_pct": cpu_pct,
        "mem_mb": mem_mb,
        "db_mb": db_mb,
        "uptime_s": uptime_s,
        "records_today": records_today,
        "api_calls_today": api_calls_today,
    }


@app.get("/api/v1/chat/history", status_code=status.HTTP_200_OK)
async def get_chat_history(
    limit: int = Query(default=50, ge=1, le=200),
    session_id: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    """Return chat messages for UI reconstruction.

    When session_id is supplied, returns ALL messages for that session (no limit) in
    chronological order, used by the frontend to restore the full session on mount.
    When omitted, returns the last *limit* messages (legacy behaviour).
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        if session_id:
            cursor.execute(
                "SELECT id, role, content, created_at FROM chat_history "
                "WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT id, role, content, created_at FROM chat_history "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


@app.delete("/api/v1/chat/history", status_code=status.HTTP_204_NO_CONTENT)
async def clear_chat_history() -> None:
    """Delete all chat history rows so the model starts a fresh session."""
    conn = get_db_conn()
    try:
        conn.execute("DELETE FROM chat_history")
        conn.commit()
    finally:
        conn.close()


class CheckInRequest(BaseModel):
    days: int = 14
    focus: Optional[str] = None


@app.post("/api/v1/check-in", status_code=status.HTTP_200_OK)
async def run_check_in(payload: CheckInRequest) -> StreamingResponse:
    """Fetch all ledger data for `days` days and stream a clinical analysis.

    Bypasses tool calling entirely because data is pre-fetched from SQLite and handed
    directly to the model as context. More reliable than the chat ingest path
    for pure data-review flows.
    """
    async def _event_gen() -> AsyncGenerator[str, None]:
        full_text = ""
        try:
            chat_history = await run_in_threadpool(_get_recent_chat_history, 20)

            async for (ev_type, ev_data) in _iter_sync_gen(
                check_in_stream, payload.days, payload.focus, chat_history
            ):
                if ev_type == "status":
                    safe_msg = json.dumps(ev_data)
                    yield f'data: {{"type":"status","msg":{safe_msg}}}\n\n'
                elif ev_type == "chunk":
                    safe_text = json.dumps(ev_data)
                    full_text += ev_data
                    yield f'data: {{"type":"chunk","text":{safe_text}}}\n\n'
                elif ev_type == "error":
                    safe_err = json.dumps(ev_data)
                    yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'
                    break

        except Exception as exc:
            safe_err = json.dumps(str(exc))
            yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'

        # Persist to chat history as a synthetic check-in turn
        if full_text:
            label = f"[Check-in: past {payload.days} days"
            if payload.focus:
                label += f": {payload.focus}"
            label += "]"
            try:
                _insert_chat_message("user", label)
                _insert_chat_message("model", full_text)
            except Exception:
                pass

            if _is_protocol_response(full_text):
                await _mem0_add_safe(f"[Protocol] {full_text}")

        yield f'data: {{"type":"done","committed":null}}\n\n'

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pypdf. Raises ValueError if nothing found."""
    import io
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    if len(reader.pages) == 0:
        raise ValueError("PDF has no pages")

    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())

    if not pages:
        raise ValueError(
            "No extractable text found. This may be a scanned PDF; "
            "text layer required for extraction."
        )
    return "\n\n".join(pages)


def _extract_csv_text(file_bytes: bytes) -> str:
    """Convert a CSV file to a readable text block for LLM extraction."""
    import csv
    import io

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Could not decode CSV file: unknown character encoding.")

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]

    if not rows:
        raise ValueError("CSV file is empty or contains no data rows.")

    # Cap rows so large wearable exports don't overwhelm Flash or hit the timeout.
    # Lab panels have <80 markers; biometric exports can have thousands of rows.
    MAX_ROWS = 300
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]

    header = " | ".join(cell.strip() for cell in rows[0])
    data_lines = [" | ".join(cell.strip() for cell in row) for row in rows[1:]]

    lines = [
        "Health Data (CSV Import):",
        f"Columns: {header}",
        f"Rows ({len(data_lines)} data rows{', first 299 shown' if truncated else ''}):",
    ] + data_lines

    return "\n".join(lines)


_SUPPORTED_EXTENSIONS = {".pdf", ".csv"}


def _extract_file_text(file_bytes: bytes, filename: str) -> str:
    """Dispatch to the right extractor based on file extension."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext == ".pdf":
        return _extract_pdf_text(file_bytes)
    if ext == ".csv":
        return _extract_csv_text(file_bytes)
    raise ValueError(
        f"Unsupported file type '{ext or filename}'. Drop a PDF or CSV."
    )


class FilePathRequest(BaseModel):
    path: str


@app.post("/api/v1/upload/pdf-path", status_code=status.HTTP_200_OK)
async def upload_pdf_path(payload: FilePathRequest) -> StreamingResponse:
    """Accept a local file-system path to a PDF or CSV (from Tauri drag-drop), read it, and stream SSE."""
    from pathlib import Path as _Path

    p = _Path(payload.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {payload.path}")
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{p.suffix}'. Drop a PDF or CSV."
        )

    try:
        file_bytes = p.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _stream_file_bytes(file_bytes, p.name)


@app.post("/api/v1/upload/pdf", status_code=status.HTTP_200_OK)
async def upload_pdf(file: UploadFile = File(...)) -> StreamingResponse:
    """Accept a multipart file upload (PDF or CSV) and stream SSE."""
    filename = file.filename or "upload"
    file_bytes = await file.read()
    return _stream_file_bytes(file_bytes, filename)


def _stream_file_bytes(file_bytes: bytes, filename: str) -> StreamingResponse:
    """Shared SSE pipeline for file bytes regardless of how they arrived (path or upload)."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    file_label = "CSV" if ext == ".csv" else "PDF"

    async def _event_gen() -> AsyncGenerator[str, None]:
        full_text = ""
        committed: Optional[Dict[str, Any]] = None

        try:
            # ── Stage 1: text extraction ──────────────────────────────────────
            yield f'data: {{"type":"status","msg":"Reading {file_label}..."}}\n\n'
            try:
                raw_text = await run_in_threadpool(_extract_file_text, file_bytes, filename)
            except ValueError as exc:
                safe_err = json.dumps(str(exc))
                yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'
                return

            # ── Stage 2: extraction ───────────────────────────────────────────
            yield f'data: {{"type":"status","msg":"Extracting health data..."}}\n\n'
            try:
                if ext == ".csv":
                    # Direct Python parse without LLM, runs in <10 ms.
                    # Falls back to LLM only if the column layout is unrecognised.
                    extracted = await run_in_threadpool(parse_csv_direct, file_bytes, filename)
                    if extracted is None:
                        logger.info("[upload_file] direct CSV parse found no recognised columns, trying LLM")
                        extracted = await run_in_threadpool(extract_from_file, raw_text)
                else:
                    extracted = await run_in_threadpool(extract_from_file, raw_text)

                bio_count = len(extracted.biometrics) if extracted else 0
                lab_count = len(extracted.labs) if extracted else 0
                logger.info(f"[upload_file] extraction result: {bio_count} biometrics, {lab_count} labs")
            except Exception as exc:
                logger.warning(f"[upload_file] extraction error (non-fatal): {exc}")
                extracted = None

            # ── Stage 3: commit to ledger ─────────────────────────────────────
            if extracted and any([
                extracted.supplements, extracted.biometrics,
                extracted.labs, extracted.journals,
            ]):
                yield f'data: {{"type":"status","msg":"Committing to ledger..."}}\n\n'
                try:
                    committed = commit_logs(
                        supplements=extracted.supplements,
                        biometrics=extracted.biometrics,
                        labs=extracted.labs,
                        journals=extracted.journals,
                    )
                    try:
                        await bus.emit("DATA_LOGGED", {"source": "file_upload"})
                    except Exception as _bus_exc:
                        logger.debug("[upload_file] bus.emit error (non-fatal): %s", _bus_exc)
                    await _mem0_add_safe(raw_text)
                except Exception as exc:
                    logger.warning(f"[upload_file] commit error (non-fatal): {exc}")

            # ── Stage 4: stream interpretation (no tool calls) ───────────────
            has_extracted = extracted and any([
                extracted.supplements, extracted.biometrics,
                extracted.labs, extracted.journals,
            ])

            if not has_extracted:
                logger.info(f"[upload_file] extraction returned empty for {filename}")
                no_data_msg = (
                    f"No health data could be extracted from **{filename}**. "
                    "The direct CSV parser looks for columns named Biomarker/Marker/Test/Analyte + Value + Unit + Date. "
                    "If your file uses different headers, paste a few rows as a chat message and I'll log them manually."
                )
                full_text = no_data_msg
                yield f'data: {{"type":"chunk","text":{json.dumps(no_data_msg)}}}\n\n'
            else:
                # Interpret the imported data without chat history; passing history
                # causes the model to mix newly imported values with old lab discussions
                # from previous sessions, producing hallucinated or stale context.
                async for (ev_type, ev_data) in _iter_sync_gen(
                    import_interpret_stream, filename, extracted, committed
                ):
                    if ev_type == "status":
                        safe_msg = json.dumps(ev_data)
                        yield f'data: {{"type":"status","msg":{safe_msg}}}\n\n'
                    elif ev_type == "chunk":
                        safe_text = json.dumps(ev_data)
                        full_text += ev_data
                        yield f'data: {{"type":"chunk","text":{safe_text}}}\n\n'
                    elif ev_type == "error":
                        safe_err = json.dumps(ev_data)
                        yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'
                        break

        except Exception as exc:
            safe_err = json.dumps(str(exc))
            logger.error(f"[upload_file] unhandled error: {exc}", exc_info=True)
            yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'

        # ── Stage 5: save to chat history ─────────────────────────────────────
        if full_text:
            try:
                _insert_chat_message("user", f"[{file_label} import: {filename}]")
                _insert_chat_message("model", full_text)
            except Exception as exc:
                logger.warning(f"[upload_file] history save error (non-fatal): {exc}")

            if _is_protocol_response(full_text):
                await _mem0_add_safe(f"[Protocol] {full_text}")

        committed_json = json.dumps(committed, default=json_default)
        yield f'data: {{"type":"done","committed":{committed_json}}}\n\n'

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Transcript memory ─────────────────────────────────────────────────────────
# Ingest long PDF/text transcripts of conversations into a retrieval-backed
# semantic store + the knowledge graph, WITHOUT loading the full document into the
# conversation context. Upload returns immediately; chunk/embed/extract runs in a
# background task and reports progress over the event bus (TRANSCRIPT_PROGRESS).

_TRANSCRIPT_EXTENSIONS = {".pdf", ".txt", ".md"}


def _extract_transcript_text(file_bytes: bytes, filename: str) -> str:
    """Extract plain text from a transcript file (PDF or UTF-8/Latin-1 text)."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext == ".pdf":
        return _extract_pdf_text(file_bytes)
    if ext in (".txt", ".md"):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Could not decode text file: unknown character encoding.")
        if not text.strip():
            raise ValueError("Text file is empty.")
        return text
    raise ValueError(f"Unsupported transcript type '{ext or filename}'. Drop a PDF or .txt file.")


async def _process_transcript_bg(doc_id: str, file_bytes: bytes, filename: str) -> None:
    """Background worker: extract text, then chunk/embed/extract in a thread pool."""
    try:
        raw_text = await run_in_threadpool(_extract_transcript_text, file_bytes, filename)
    except Exception as exc:
        logger.warning("[transcript] extraction failed for %s: %s", filename, exc)
        await run_in_threadpool(transcript_memory.mark_failed, doc_id, str(exc))
        try:
            await bus.emit("TRANSCRIPT_PROGRESS",
                           {"document_id": doc_id, "stage": "failed", "error": str(exc)[:200]})
        except Exception:
            pass
        return
    try:
        await run_in_threadpool(transcript_memory.process_document, doc_id, raw_text)
    except Exception as exc:
        logger.error("[transcript] process_document crashed: %s", exc, exc_info=True)


class TranscriptPathRequest(BaseModel):
    path: str
    source_label: Optional[str] = None


@app.post("/api/v1/transcript/upload", status_code=status.HTTP_202_ACCEPTED)
async def transcript_upload(
    file: UploadFile = File(...),
    source_label: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """Accept a multipart transcript upload; process in the background."""
    filename = file.filename or "transcript"
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in _TRANSCRIPT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported type '{ext}'. Use PDF or .txt.")
    file_bytes = await file.read()
    doc_id = await run_in_threadpool(transcript_memory.create_document, filename, source_label)
    asyncio.create_task(_process_transcript_bg(doc_id, file_bytes, filename))
    return {"document_id": doc_id, "filename": filename, "status": "pending"}


@app.post("/api/v1/transcript/upload-path", status_code=status.HTTP_202_ACCEPTED)
async def transcript_upload_path(payload: TranscriptPathRequest) -> Dict[str, Any]:
    """Accept a local file-system path (Tauri drag-drop); process in the background."""
    from pathlib import Path as _Path

    p = _Path(payload.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {payload.path}")
    if p.suffix.lower() not in _TRANSCRIPT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported type '{p.suffix}'. Use PDF or .txt.")
    try:
        file_bytes = p.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    doc_id = await run_in_threadpool(transcript_memory.create_document, p.name, payload.source_label)
    asyncio.create_task(_process_transcript_bg(doc_id, file_bytes, p.name))
    return {"document_id": doc_id, "filename": p.name, "status": "pending"}


@app.get("/api/v1/transcript/documents", status_code=status.HTTP_200_OK)
async def transcript_documents() -> Dict[str, Any]:
    """List ingested transcript documents and their processing status."""
    docs = await run_in_threadpool(transcript_memory.list_documents)
    return {"documents": docs}


@app.delete("/api/v1/transcript/{document_id}", status_code=status.HTTP_200_OK)
async def transcript_delete(document_id: str) -> Dict[str, Any]:
    """Delete a transcript document and its chunks (cascade)."""
    removed = await run_in_threadpool(transcript_memory.delete_document, document_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": document_id}


@app.post("/api/v1/chat/ingest", status_code=status.HTTP_200_OK)
async def ingest_chat(payload: ChatMessage) -> StreamingResponse:
    raw_text = payload.text
    logging_enabled = payload.logging_enabled
    session_id = payload.session_id

    async def _event_gen() -> AsyncGenerator[str, None]:
        committed: Optional[Dict[str, Any]] = None
        extracted = None
        full_text: str = ""
        _bus_emitted = False  # track whether DATA_LOGGED has been sent this turn

        try:
            # ── Stage 1: fetch history + extraction + mem0 ───────────────────
            chat_history = await run_in_threadpool(_get_session_history, session_id)

            if logging_enabled:
                _preview = raw_text[:80].replace("\n", " ")
                logger.info('[ingest] message: "%s%s"', _preview, "…" if len(raw_text) > 80 else "")
                yield 'data: {"type":"status","msg":"Extracting data..."}\n\n'
                protocols_context = get_all_protocols()
                extraction_result, mem0_context, transcript_context = await asyncio.gather(
                    run_in_threadpool(extract, raw_text, protocols_context),
                    _mem0_retrieve_safe(raw_text),
                    _transcript_retrieve_safe(raw_text),
                    return_exceptions=True,
                )

                if isinstance(extraction_result, Exception):
                    logger.warning(f"[ingest] extraction error (non-fatal): {extraction_result}")
                else:
                    extracted = extraction_result
                    has_data = any([
                        extracted.supplements,
                        extracted.biometrics,
                        extracted.labs,
                        extracted.journals,
                    ])
                    if has_data:
                        logger.info(
                            "[ingest] extracted: types=%s  supplements=%d  biometrics=%d  labs=%d  journals=%d",
                            extracted.log_type,
                            len(extracted.supplements),
                            len(extracted.biometrics),
                            len(extracted.labs),
                            len(extracted.journals),
                        )
                        try:
                            yield 'data: {"type":"status","msg":"Committing to ledger..."}\n\n'
                            append_to_wal(raw_text)
                            committed = commit_logs(
                                supplements=extracted.supplements,
                                biometrics=extracted.biometrics,
                                labs=extracted.labs,
                                journals=extracted.journals,
                            )
                            # commit_logs returns inserted counts as top-level keys
                            # and an optional "skipped" sub-dict.
                            _table_keys = ("supplement_logs", "biometric_logs", "lab_results", "daily_journals")
                            _ins_summary = {k: committed[k] for k in _table_keys if committed.get(k)}
                            _skp_summary = {k: v for k, v in committed.get("skipped", {}).items() if v}
                            logger.info(
                                "[ingest] committed: %s%s",
                                _ins_summary,
                                f"  skipped={_skp_summary}" if _skp_summary else "",
                            )
                            try:
                                await bus.emit("DATA_LOGGED", {
                                    "source": "chat_extraction",
                                    "types": extracted.log_type,
                                })
                                _bus_emitted = True
                            except Exception as _bus_exc:
                                logger.debug("[ingest] bus.emit error (non-fatal): %s", _bus_exc)
                            await _mem0_add_safe(raw_text)
                        except Exception as exc:
                            logger.warning(f"[ingest] commit error (non-fatal): {exc}")
                    else:
                        logger.info("[ingest] extraction found no loggable data")

                if isinstance(mem0_context, Exception):
                    mem0_context = ""
                if isinstance(transcript_context, Exception):
                    transcript_context = ""
            else:
                # Brainstorm mode: skip extraction and DB writes, retrieve memory only.
                yield 'data: {"type":"status","msg":"Querying memory..."}\n\n'
                mem0_context, transcript_context = await asyncio.gather(
                    _mem0_retrieve_safe(raw_text),
                    _transcript_retrieve_safe(raw_text),
                    return_exceptions=True,
                )
                if isinstance(mem0_context, Exception):
                    mem0_context = ""
                if isinstance(transcript_context, Exception):
                    transcript_context = ""

            # ── Stage 2: stream the Pro model response ────────────────────────
            # Build working memory payload only when data was actually extracted
            # and committed this turn.  Passed as the system-prompt sensory block
            # so the model is mandated to acknowledge and analyze the new data.
            # exclude raw_input_text (redundant with [User message]) and use
            # mode='json' so Pydantic Enums / datetimes serialize cleanly.
            _wm_payload = (
                extracted.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={"raw_input_text"},
                )
                if extracted and any([
                    extracted.supplements,
                    extracted.biometrics,
                    extracted.labs,
                    extracted.journals,
                ])
                else None
            )
            yield 'data: {"type":"status","msg":"Generating response..."}\n\n'
            # ── [SILENT_EXECUTE] intercept ─────────────────────────────────────
            # Buffer chunks until we know the response isn't the silent-execute
            # token (17 chars).  Once the accumulated text exceeds that length we
            # flush the buffer and stream normally.  If the whole response is
            # exactly [SILENT_EXECUTE], emit a silent_receipt event instead.
            _SILENT_TOKEN = "[SILENT_EXECUTE]"
            _chunk_buf: list[str] = []
            _buf_flushed = False

            async for (ev_type, ev_data) in _iter_sync_gen(
                chat_respond_stream, raw_text, extracted, mem0_context, chat_history,
                working_memory_payload=_wm_payload, transcript_context=transcript_context
            ):
                if ev_type == "status":
                    safe_msg = json.dumps(ev_data)
                    yield f'data: {{"type":"status","msg":{safe_msg}}}\n\n'
                elif ev_type == "chunk":
                    full_text += ev_data
                    if not _buf_flushed:
                        _chunk_buf.append(ev_data)
                        if len(full_text) > len(_SILENT_TOKEN):
                            _buf_flushed = True
                            for _bc in _chunk_buf:
                                yield f'data: {{"type":"chunk","text":{json.dumps(_bc)}}}\n\n'
                            _chunk_buf.clear()
                    else:
                        yield f'data: {{"type":"chunk","text":{json.dumps(ev_data)}}}\n\n'
                elif ev_type == "error":
                    safe_err = json.dumps(ev_data)
                    yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'
                    _buf_flushed = True  # prevent post-loop chunk emission after error
                    break

            # Resolve any remaining buffered output
            if not _buf_flushed:
                if full_text.strip() == _SILENT_TOKEN:
                    yield 'data: {"type":"silent_receipt","msg":"System updated."}\n\n'
                else:
                    for _bc in _chunk_buf:
                        yield f'data: {{"type":"chunk","text":{json.dumps(_bc)}}}\n\n'

        except Exception as exc:
            error_detail = str(exc)
            logger.error(f"[ingest] SSE generator error: {error_detail}", exc_info=True)
            safe_err = json.dumps(error_detail)
            yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'

        # ── Stage 3: post-stream work (non-blocking, after SSE is done) ───────
        # Emit DATA_LOGGED if extraction didn't already do it; this covers LEDGER
        # tool calls (update_entry / delete_entry) that mutate the DB without
        # producing extractable data, so the dashboard always refreshes after
        # any turn that resulted in a real model response.
        if full_text and not _bus_emitted:
            try:
                await bus.emit("DATA_LOGGED", {"source": "chat"})
            except Exception as _bus_exc:
                logger.debug("[ingest] bus.emit (fallback) error (non-fatal): %s", _bus_exc)

        reply = full_text or "I wasn't able to generate a response. Please try again."

        if full_text and _is_protocol_response(full_text):
            await _mem0_add_safe(f"[Protocol] {full_text}")

        user_msg_id: Optional[str] = None
        if not _is_fallback_response(reply):
            # Only persist real model responses. Saving a fallback string as a model
            # turn poisons the conversation history and causes Gemini to see its own
            # failure message as prior context, triggering further empty responses.
            try:
                user_msg_id = await run_in_threadpool(_insert_chat_message, "user", raw_text, session_id)
                await run_in_threadpool(_insert_chat_message, "model", reply, session_id)
            except Exception as exc:
                logger.warning(f"[ingest] chat history save error (non-fatal): {exc}")
        else:
            logger.warning("[ingest] skipping history save: model returned fallback response")

        committed_json = json.dumps(committed, default=json_default)
        user_msg_id_json = json.dumps(user_msg_id)
        yield f'data: {{"type":"done","committed":{committed_json},"user_message_id":{user_msg_id_json}}}\n\n'

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/chat/rewind", status_code=status.HTTP_200_OK)
async def rewind_chat(payload: RewindPayload) -> StreamingResponse:
    """Edit a past user message and regenerate the conversation from that point.

    Stage 0: Update the target message in-place; hard-delete every row that
             follows it in the session (by rowid, which avoids timestamp ties).
    Stage 1: Run extraction + mem0 retrieval on the new content, exactly as
             ingest_chat does, so any newly extractable data is committed.
    Stage 2: Stream the regenerated Pro model response.
    Stage 3: Save the new model reply; emit DATA_LOGGED so the UI re-fetches.
    """
    raw_text  = payload.new_content
    session_id = payload.session_id
    message_id = payload.message_id

    async def _event_gen() -> AsyncGenerator[str, None]:
        committed: Optional[Dict[str, Any]] = None
        extracted = None
        full_text: str = ""
        target_rowid: Optional[int] = None
        _errored = False  # set when the outer except fires; suppresses the done event

        try:
            # ── Stage 0: rewind the DB ────────────────────────────────────────
            try:
                _rw_conn = None  # guard: close() is safe even if get_db_conn() raises
                _rw_conn = get_db_conn()
                _rw_cursor = _rw_conn.cursor()
                row = _rw_cursor.execute(
                    "SELECT rowid, created_at FROM chat_history WHERE id = ? AND session_id = ?",
                    (message_id, session_id),
                ).fetchone()
                if row is None:
                    yield 'data: {"type":"error","msg":"Message not found in this session"}\n\n'
                    return
                target_rowid = row["rowid"]
                _rw_cursor.execute(
                    "UPDATE chat_history SET content = ? WHERE id = ?",
                    (raw_text, message_id),
                )
                # rowid is strictly insertion-ordered and correctly handles the
                # common case of user+model rows sharing the same created_at second.
                deleted = _rw_cursor.execute(
                    "DELETE FROM chat_history WHERE session_id = ? AND rowid > ?",
                    (session_id, target_rowid),
                ).rowcount
                _rw_conn.commit()
                logger.info(
                    "[rewind] updated msg id=%s, pruned %d subsequent row(s)",
                    message_id[:8], deleted,
                )
            except sqlite3.Error as exc:
                logger.error("[rewind] DB error during rewind: %s", exc)
                yield 'data: {"type":"error","msg":"Database error during rewind"}\n\n'
                return
            finally:
                if _rw_conn is not None:
                    _rw_conn.close()

            # ── Stage 1: fetch prior history + extraction + mem0 ─────────────
            # History = everything BEFORE the edited message (rowid < target_rowid)
            # so the edited message is the current turn, not part of context.
            chat_history = await run_in_threadpool(
                _get_history_before_rowid, session_id, target_rowid
            )

            _preview = raw_text[:80].replace("\n", " ")
            logger.info('[rewind] new content: "%s%s"', _preview, "…" if len(raw_text) > 80 else "")
            yield 'data: {"type":"status","msg":"Extracting data..."}\n\n'
            protocols_context = get_all_protocols()
            extraction_result, mem0_context, transcript_context = await asyncio.gather(
                run_in_threadpool(extract, raw_text, protocols_context),
                _mem0_retrieve_safe(raw_text),
                _transcript_retrieve_safe(raw_text),
                return_exceptions=True,
            )

            if isinstance(extraction_result, Exception):
                logger.warning("[rewind] extraction error (non-fatal): %s", extraction_result)
            else:
                extracted = extraction_result
                has_data = any([
                    extracted.supplements,
                    extracted.biometrics,
                    extracted.labs,
                    extracted.journals,
                ])
                if has_data:
                    logger.info(
                        "[rewind] extracted: types=%s  supplements=%d  biometrics=%d  labs=%d  journals=%d",
                        extracted.log_type,
                        len(extracted.supplements),
                        len(extracted.biometrics),
                        len(extracted.labs),
                        len(extracted.journals),
                    )
                    try:
                        yield 'data: {"type":"status","msg":"Committing to ledger..."}\n\n'
                        committed = commit_logs(
                            supplements=extracted.supplements,
                            biometrics=extracted.biometrics,
                            labs=extracted.labs,
                            journals=extracted.journals,
                        )
                        _table_keys = ("supplement_logs", "biometric_logs", "lab_results", "daily_journals")
                        _ins_summary = {k: committed[k] for k in _table_keys if committed.get(k)}
                        logger.info("[rewind] committed: %s", _ins_summary)
                    except Exception as exc:
                        logger.warning("[rewind] commit error (non-fatal): %s", exc)
                else:
                    logger.info("[rewind] extraction found no loggable data")

            if isinstance(mem0_context, Exception):
                mem0_context = ""
            if isinstance(transcript_context, Exception):
                transcript_context = ""

            # ── Stage 2: stream the regenerated Pro model response ────────────
            _wm_payload = (
                extracted.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={"raw_input_text"},
                )
                if extracted and any([
                    extracted.supplements,
                    extracted.biometrics,
                    extracted.labs,
                    extracted.journals,
                ])
                else None
            )
            yield 'data: {"type":"status","msg":"Generating response..."}\n\n'
            # ── [SILENT_EXECUTE] intercept (same logic as ingest_chat) ─────────
            _SILENT_TOKEN_RW = "[SILENT_EXECUTE]"
            _chunk_buf_rw: list[str] = []
            _buf_flushed_rw = False

            async for (ev_type, ev_data) in _iter_sync_gen(
                chat_respond_stream, raw_text, extracted, mem0_context, chat_history,
                working_memory_payload=_wm_payload, transcript_context=transcript_context,
            ):
                if ev_type == "status":
                    safe_msg = json.dumps(ev_data)
                    yield f'data: {{"type":"status","msg":{safe_msg}}}\n\n'
                elif ev_type == "chunk":
                    full_text += ev_data
                    if not _buf_flushed_rw:
                        _chunk_buf_rw.append(ev_data)
                        if len(full_text) > len(_SILENT_TOKEN_RW):
                            _buf_flushed_rw = True
                            for _bc in _chunk_buf_rw:
                                yield f'data: {{"type":"chunk","text":{json.dumps(_bc)}}}\n\n'
                            _chunk_buf_rw.clear()
                    else:
                        yield f'data: {{"type":"chunk","text":{json.dumps(ev_data)}}}\n\n'
                elif ev_type == "error":
                    safe_err = json.dumps(ev_data)
                    yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'
                    _buf_flushed_rw = True  # prevent post-loop chunk emission after error
                    break

            # Resolve any remaining buffered output
            if not _buf_flushed_rw:
                if full_text.strip() == _SILENT_TOKEN_RW:
                    yield 'data: {"type":"silent_receipt","msg":"System updated."}\n\n'
                else:
                    for _bc in _chunk_buf_rw:
                        yield f'data: {{"type":"chunk","text":{json.dumps(_bc)}}}\n\n'

        except Exception as exc:
            _errored = True
            error_detail = str(exc)
            logger.error("[rewind] SSE generator error: %s", error_detail, exc_info=True)
            safe_err = json.dumps(error_detail)
            yield f'data: {{"type":"error","msg":{safe_err}}}\n\n'

        # ── Stage 3: save model reply + emit refresh event ────────────────────
        # Skip entirely when the outer except fired; the client already received
        # an error event and emitting done on the same stream would prematurely
        # reset the pending/error UI state.
        if _errored:
            return

        # Only persist and notify when we actually received a real model response.
        # Skip the DB write for empty responses AND known fallback strings so no
        # phantom or poisoned turn ends up in chat history.
        if full_text and not _is_fallback_response(full_text):
            try:
                # The edited user message is already in DB (updated in Stage 0).
                # Only the new model reply needs to be inserted here.
                await run_in_threadpool(_insert_chat_message, "model", full_text, session_id)
            except Exception as exc:
                logger.warning("[rewind] chat history save error (non-fatal): %s", exc)
        elif full_text:
            logger.warning("[rewind] skipping history save: model returned fallback response")

            # Only emit DATA_LOGGED when something was actually committed; this avoids
            # a spurious dashboard re-fetch when commit_logs raised and committed is None.
            if committed is not None:
                try:
                    await bus.emit("DATA_LOGGED", {"source": "chat_rewind"})
                except Exception as exc:
                    logger.debug("[rewind] bus.emit error (non-fatal): %s", exc)

        committed_json = json.dumps(committed, default=json_default)
        yield f'data: {{"type":"done","committed":{committed_json}}}\n\n'

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/log/raw", status_code=status.HTTP_201_CREATED)
async def log_raw_entry(payload: AIExtractionPayload) -> Dict[str, Any]:
    append_to_wal(payload.raw_input_text)
    committed = commit_logs(
        supplements=payload.supplements,
        biometrics=payload.biometrics,
        labs=payload.labs,
        journals=payload.journals,
    )
    await bus.emit("DATA_LOGGED", {"source": "raw", "log_type": payload.log_type})
    await _mem0_add_safe(payload.raw_input_text)
    return {"status": "success", "committed": committed, "log_type": payload.log_type}


@app.post("/api/v1/log/supplement", status_code=status.HTTP_201_CREATED)
async def log_supplement(payload: SupplementLog) -> Dict[str, Any]:
    raw_text = model_as_wal_text(payload)
    append_to_wal(raw_text, source="supplement")
    committed = commit_logs(supplements=[payload])
    await bus.emit("DATA_LOGGED", {"source": "supplement"})
    await _mem0_add_safe(raw_text)
    return {"status": "success", "committed": committed}


@app.post("/api/v1/log/biometric", status_code=status.HTTP_201_CREATED)
async def log_biometric(payload: BiometricLog) -> Dict[str, Any]:
    raw_text = model_as_wal_text(payload)
    append_to_wal(raw_text, source="biometric")
    committed = commit_logs(biometrics=[payload])
    await bus.emit("DATA_LOGGED", {"source": "biometric"})
    await _mem0_add_safe(raw_text)
    return {"status": "success", "committed": committed}


@app.post("/api/v1/log/lab_result", status_code=status.HTTP_201_CREATED)
async def log_lab_result(payload: LabResult) -> Dict[str, Any]:
    raw_text = model_as_wal_text(payload)
    append_to_wal(raw_text, source="lab_result")
    committed = commit_logs(labs=[payload])
    await bus.emit("DATA_LOGGED", {"source": "lab"})
    await _mem0_add_safe(raw_text)
    return {"status": "success", "committed": committed}


@app.post("/api/v1/log/journal", status_code=status.HTTP_201_CREATED)
async def log_journal(payload: DailyJournal) -> Dict[str, Any]:
    raw_text = model_as_wal_text(payload)
    append_to_wal(raw_text, source="daily_journal")
    committed = commit_logs(journals=[payload])
    await bus.emit("DATA_LOGGED", {"source": "journal"})
    await _mem0_add_safe(raw_text)
    return {"status": "success", "committed": committed}


class UpdateEntryPayload(BaseModel):
    field_name: str
    new_value: str


# UPDATABLE_FIELDS imported from models.py as single source of truth

_TABLE_MAP: Dict[str, str] = {
    "biometric": "biometric_logs",
    "supplement":  "supplement_logs",
    "lab":       "lab_results",
    "journal":   "daily_journals",
}


@app.patch("/api/v1/entry/{entry_type}/{entry_id}", status_code=status.HTTP_200_OK)
async def patch_entry(
    entry_type: Literal["biometric", "supplement", "lab", "journal"],
    entry_id: str,
    payload: UpdateEntryPayload,
) -> Dict[str, Any]:
    """Update a single field on an existing entry."""
    allowed = UPDATABLE_FIELDS.get(entry_type, set())
    if payload.field_name not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Field '{payload.field_name}' is not updatable for {entry_type}. "
                   f"Allowed: {sorted(allowed)}",
        )
    table = _TABLE_MAP[entry_type]
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE {table} SET {payload.field_name} = ? WHERE id = ?",  # noqa: S608
            (payload.new_value, entry_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
        conn.commit()
    finally:
        conn.close()
    return {"updated": True, "id": entry_id, "field": payload.field_name, "new_value": payload.new_value}


@app.delete("/api/v1/entry/{entry_type}/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_type: Literal["biometric", "supplement", "lab", "journal"],
    entry_id: str,
) -> None:
    """Hard-delete a single log entry by type and UUID."""
    table = _TABLE_MAP[entry_type]
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))  # noqa: S608
        if cursor.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
        conn.commit()
    finally:
        conn.close()


@app.get("/api/v1/history/supplements", status_code=status.HTTP_200_OK)
async def get_supplement_history(limit: int = Query(default=50, ge=1, le=500)) -> List[Dict[str, Any]]:
    return get_history("supplement", limit)


@app.get("/api/v1/get/history", status_code=status.HTTP_200_OK)
async def get_history_endpoint(
    entry_type: Literal["supplement", "biometric", "lab_result", "daily_journal"] = "supplement",
    limit: int = Query(default=50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    return get_history(entry_type, limit)


# ── Semantic Event Bus: SSE stream ───────────────────────────────────────────

@app.get("/api/v1/events/stream")
async def events_stream() -> StreamingResponse:
    """Server-Sent Events stream backed by the module-level EventBus.

    The frontend connects once and receives push notifications whenever data is
    logged (DATA_LOGGED) or the knowledge graph is updated (SYNTHESIS_COMPLETE).
    A keep-alive comment is emitted every ~25 s to prevent proxy timeouts.

    Each browser tab / Tauri window gets its own asyncio.Queue via bus.subscribe()
    so all active connections receive every event independently.
    """
    async def _gen() -> AsyncGenerator[str, None]:
        # Immediately confirm the connection so the frontend knows the pipe is live.
        yield 'data: {"type":"connected"}\n\n'
        async for event in bus.subscribe(heartbeat_interval=25.0):
            if event is None:
                # SSE comment: invisible to JS onmessage, but resets idle timers.
                yield ": heartbeat\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx / reverse-proxy buffering
            "Connection": "keep-alive",
        },
    )


@app.get("/api/v1/cognition/log", status_code=status.HTTP_200_OK)
async def cognition_log(limit: int = 50) -> Dict[str, Any]:
    """Return LEDGER's inner monologue stream + currently open threads (the Mind panel)."""
    limit = max(1, min(limit, 200))
    return await run_in_threadpool(get_cognition_log, limit)


@app.post("/api/v1/cognition/tick", status_code=status.HTTP_200_OK)
async def cognition_tick(force: bool = False) -> Dict[str, Any]:
    """Manually fire one cognition tick. `force=true` bypasses quiet hours / budget /
    cooldown / dedup so a salient observation speaks immediately, which is useful for testing."""
    return await _run_cognition_tick_and_dispatch(force_speak=force)


class CognitionFeedback(BaseModel):
    rating: str = Field(description="'up' | 'down' | 'snooze'")
    signature: Optional[str] = None


@app.post("/api/v1/cognition/feedback", status_code=status.HTTP_200_OK)
async def cognition_feedback(payload: CognitionFeedback) -> Dict[str, Any]:
    """Attune the cognition loop: 👍 lowers the speak bar, 👎 raises it (and dismisses the
    thread), 'snooze' silences proactive speech for a while."""
    return await run_in_threadpool(record_cognition_feedback, payload.signature, payload.rating)


@app.get("/api/v1/insights/synthesis-status", status_code=status.HTTP_200_OK)
async def synthesis_status() -> Dict[str, Any]:
    """Return cooldown state and last synthesis date so the UI can render the button correctly."""
    last_time_str = _get_system_state("last_synthesis_time")
    remaining = 0
    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            remaining = max(0, int(SYNTHESIS_COOLDOWN_SECONDS - elapsed))
        except Exception:
            pass
    return {
        "cooldown_remaining_seconds": remaining,
        "cooldown_total_seconds": SYNTHESIS_COOLDOWN_SECONDS,
        "last_synthesis_date": _get_system_state("last_synthesis_date"),
    }


@app.post("/api/v1/insights/force-synthesis", status_code=status.HTTP_200_OK)
async def force_synthesis() -> Any:
    """Manually trigger morning synthesis, bypassing the daily date gate.

    Returns 429 with cooldown_remaining_seconds if called within SYNTHESIS_COOLDOWN_SECONDS
    of the previous run, providing the primary guard against accidental rapid resubmission.
    Updates last_synthesis_date so the automatic boot doesn't double-fire.
    """
    # ── Cooldown check ────────────────────────────────────────────────────────
    last_time_str = _get_system_state("last_synthesis_time")
    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            remaining = int(SYNTHESIS_COOLDOWN_SECONDS - elapsed)
            if remaining > 0:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Synthesis cooldown active; wait before re-running.",
                        "cooldown_remaining_seconds": remaining,
                        "cooldown_total_seconds": SYNTHESIS_COOLDOWN_SECONDS,
                    },
                )
        except Exception:
            pass  # malformed timestamp: allow the run

    # ── Run ───────────────────────────────────────────────────────────────────
    today = date.today().isoformat()
    try:
        result = await run_in_threadpool(run_morning_synthesis)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Synthesis failed: {exc}",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    _set_system_state("last_synthesis_date", today)
    _set_system_state("last_synthesis_time", now_iso)

    await bus.emit("SYNTHESIS_COMPLETE", {
        "synthesis_date": today,
        "nodes_created": result.get("nodes_created", 0),
        "nodes_updated": result.get("nodes_updated", 0),
        "edges_created": result.get("edges_created", 0),
    })

    return {
        "success": True,
        "synthesis_date": today,
        "nodes_created": result.get("nodes_created", 0),
        "nodes_updated": result.get("nodes_updated", 0),
        "edges_created": result.get("edges_created", 0),
        "cooldown_seconds": SYNTHESIS_COOLDOWN_SECONDS,
    }


@app.get("/api/v1/insights/morning-briefing", status_code=status.HTTP_200_OK)
async def morning_briefing() -> Dict[str, Any]:
    """Return Knowledge Graph nodes/edges updated during today's morning synthesis."""
    synthesis_date = _get_system_state("last_synthesis_date")
    if not synthesis_date:
        return {"synthesis_date": None, "nodes": [], "edges": [], "ready": False}

    conn = get_db_conn()
    try:
        nodes = [dict(r) for r in conn.execute(
            "SELECT concept_name, category, summary_text, confidence_level, last_updated "
            "FROM clinical_nodes WHERE date(last_updated) = ? ORDER BY last_updated DESC",
            (synthesis_date,),
        ).fetchall()]

        edges = [dict(r) for r in conn.execute(
            """SELECT src.concept_name AS source, tgt.concept_name AS target,
                      e.relationship_type, e.evidence_summary
               FROM clinical_edges e
               JOIN clinical_nodes src ON src.id = e.source_node_id
               JOIN clinical_nodes tgt ON tgt.id = e.target_node_id
               WHERE date(e.created_at) = ?
               ORDER BY e.created_at DESC""",
            (synthesis_date,),
        ).fetchall()]
    finally:
        conn.close()

    return {
        "synthesis_date": synthesis_date,
        "nodes": nodes,
        "edges": edges,
        "ready": True,
    }


@app.get("/api/v1/insights/full-graph", status_code=status.HTTP_200_OK)
async def full_graph() -> Dict[str, Any]:
    """Return the complete active Knowledge Graph (all non-archived nodes + all edges).

    Used by the NetworkGraph visualisation; always returns the full state, not
    just today's synthesis delta.
    """
    conn = get_db_conn()
    try:
        nodes = [dict(r) for r in conn.execute(
            "SELECT concept_name, category, summary_text, confidence_level, "
            "last_updated, expires_at, last_surfaced_date "
            "FROM clinical_nodes WHERE is_archived = 0 OR is_archived IS NULL "
            "ORDER BY last_updated DESC",
        ).fetchall()]

        edges = [dict(r) for r in conn.execute(
            """SELECT src.concept_name AS source, tgt.concept_name AS target,
                      e.relationship_type, e.evidence_summary
               FROM clinical_edges e
               JOIN clinical_nodes src ON src.id = e.source_node_id
               JOIN clinical_nodes tgt ON tgt.id = e.target_node_id
               WHERE (src.is_archived = 0 OR src.is_archived IS NULL)
                 AND (tgt.is_archived = 0 OR tgt.is_archived IS NULL)
               ORDER BY e.created_at DESC""",
        ).fetchall()]
    finally:
        conn.close()

    return {"nodes": nodes, "edges": edges}


@app.get("/api/v1/insights/active-tracking", status_code=status.HTTP_200_OK)
async def active_tracking() -> List[ClinicalNode]:
    """Return all active pharmacokinetics Temporal Tracking Nodes (not yet archived)."""
    conn = get_db_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, concept_name, category, summary_text, confidence_level, "
            "last_updated, expires_at, last_surfaced_date, is_archived "
            "FROM clinical_nodes "
            "WHERE category = 'pharmacokinetics' AND is_archived = 0 "
            "ORDER BY expires_at ASC",
        ).fetchall()]
    finally:
        conn.close()
    return [ClinicalNode(**r) for r in rows]


@app.get("/api/v1/insights/inquiries", status_code=status.HTTP_200_OK)
async def get_todays_inquiries() -> Dict[str, Any]:
    """Return today's targeted clinical inquiries generated by morning synthesis.

    Returns {"questions": [...], "generated_at": "<iso datetime or null>"}.
    questions is an empty list if synthesis hasn't run yet today.
    """
    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key = 'todays_inquiries'",
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"questions": [], "generated_at": None}

    try:
        questions = json.loads(row["value"])
        if not isinstance(questions, list):
            questions = []
    except (json.JSONDecodeError, TypeError):
        questions = []

    return {"questions": questions, "generated_at": row["updated_at"]}


# ── Red Team Audit endpoints ───────────────────────────────────────────────────

# Minimum gap between manual audit runs because audits are expensive (Pro model, large context).
AUDIT_COOLDOWN_SECONDS = 3600  # 1 hour


@app.get("/api/v1/insights/audit-report", status_code=status.HTTP_200_OK)
async def get_audit_report() -> Dict[str, Any]:
    """Return the latest Red Team audit report stored in system_state.

    Returns {"report": <AuditReport dict>, "generated_at": "<iso>"}
    or      {"report": null, "generated_at": null} if no audit has been run yet.
    """
    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key = 'latest_audit_report'",
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"report": None, "generated_at": None}

    try:
        report_dict = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return {"report": None, "generated_at": None}

    return {"report": report_dict, "generated_at": row["updated_at"]}


@app.post("/api/v1/insights/run-audit", status_code=status.HTTP_200_OK)
async def run_audit() -> Any:
    """Manually trigger a Red Team audit of the current Knowledge Graph.

    Rate-limited to once per AUDIT_COOLDOWN_SECONDS (1 hour) to prevent
    runaway spending on the Pro model. Returns 429 with remaining seconds
    if called too soon after the last run.
    """
    # ── Cooldown check ─────────────────────────────────────────────────────────
    last_time_str = _get_system_state("last_audit_time")
    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str)
            elapsed   = (datetime.now(timezone.utc) - last_time).total_seconds()
            remaining = int(AUDIT_COOLDOWN_SECONDS - elapsed)
            if remaining > 0:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Audit cooldown active; wait before re-running.",
                        "cooldown_remaining_seconds": remaining,
                        "cooldown_total_seconds": AUDIT_COOLDOWN_SECONDS,
                        "success": False,
                    },
                )
        except Exception:
            pass  # malformed timestamp: allow the run

    # ── Run (threadpool: blocking I/O + Gemini SDK) ────────────────────────────
    try:
        result = await run_in_threadpool(run_weekly_audit)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Audit failed: {exc}",
        )

    # Stamp the run time regardless of partial success
    _set_system_state("last_audit_time", datetime.now(timezone.utc).isoformat())

    return {
        "success":                result.get("success", False),
        "challenges_found":       result.get("challenges_found", 0),
        "nodes_audited":          result.get("nodes_audited", 0),
        "summary":                result.get("summary"),
        "cooldown_seconds":       AUDIT_COOLDOWN_SECONDS,
        "error":                  result.get("error"),
    }


class ApplyAuditActionPayload(BaseModel):
    concept_name: str = Field(..., min_length=1)
    action:       str = Field(..., description="archive_node | demote_to_testing | request_new_lab")


_VALID_AUDIT_ACTIONS = frozenset({"archive_node", "demote_to_testing", "request_new_lab"})


@app.post("/api/v1/insights/apply-audit-action", status_code=status.HTTP_200_OK)
async def apply_audit_action(payload: ApplyAuditActionPayload) -> Dict[str, Any]:
    """Apply a Red Team audit recommendation to the Knowledge Graph.

    archive_node       → sets is_archived = 1 for the matched node.
    demote_to_testing  → sets confidence_level = 'testing' for the matched node.
    request_new_lab    → informational only, no DB mutation.

    Emits SYNTHESIS_COMPLETE after any DB mutation so the NetworkGraph redraws.
    """
    if payload.action not in _VALID_AUDIT_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid action '{payload.action}'. "
                f"Must be one of: {', '.join(sorted(_VALID_AUDIT_ACTIONS))}"
            ),
        )

    rows_affected = 0
    graph_updated = False

    conn = get_db_conn()
    try:
        if payload.action == "archive_node":
            cur = conn.execute(
                "UPDATE clinical_nodes "
                "SET is_archived = 1, last_updated = datetime('now') "
                "WHERE concept_name = ? AND is_archived = 0",
                (payload.concept_name,),
            )
            conn.commit()
            rows_affected = cur.rowcount
            graph_updated = rows_affected > 0

        elif payload.action == "demote_to_testing":
            cur = conn.execute(
                "UPDATE clinical_nodes "
                "SET confidence_level = 'testing', last_updated = datetime('now') "
                "WHERE concept_name = ? AND confidence_level != 'testing'",
                (payload.concept_name,),
            )
            conn.commit()
            rows_affected = cur.rowcount
            graph_updated = rows_affected > 0

        # request_new_lab → no DB mutation; graph_updated stays False
    finally:
        conn.close()

    if graph_updated:
        await bus.emit("SYNTHESIS_COMPLETE", {
            "synthesis_date": date.today().isoformat(),
            "source": "audit_action",
            "concept_name": payload.concept_name,
            "action": payload.action,
        })

    return {
        "success":       True,
        "concept_name":  payload.concept_name,
        "action":        payload.action,
        "rows_affected": rows_affected,
        "graph_updated": graph_updated,
    }


_REGIMEN_TIME_ORDER = {
    "morning": 1, "midday": 2, "afternoon": 3,
    "evening": 4, "night": 5, "as_needed": 6,
}
_REGIMEN_FREQ_ORDER = {
    "daily": 1, "twice_daily": 2, "weekly": 3,
    "biweekly": 4, "monthly": 5, "as_needed": 6,
}


@app.get("/api/v1/regimen", status_code=status.HTTP_200_OK)
async def get_regimen() -> List[Dict[str, Any]]:
    """Return all current regimen items sorted by time of day, then frequency, then name."""
    conn = get_db_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, supplement_name, dose_value, dose_unit, route, site,
                      frequency, time_of_day, days_of_week, notes
               FROM user_regimen"""
        ).fetchall()]
    finally:
        conn.close()

    rows.sort(key=lambda r: (
        _REGIMEN_TIME_ORDER.get(r["time_of_day"], 99),
        _REGIMEN_FREQ_ORDER.get(r["frequency"], 99),
        r["supplement_name"].lower(),
    ))
    return rows


@app.delete("/api/v1/regimen/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_regimen_item(item_id: str) -> None:
    """Hard-delete a regimen entry by ID."""
    conn = get_db_conn()
    try:
        cursor = conn.execute("DELETE FROM user_regimen WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Regimen item {item_id!r} not found")


class _RegimenBatchBody(BaseModel):
    items: List[Dict[str, Any]]


@app.post("/api/v1/regimen/batch", status_code=status.HTTP_200_OK)
async def save_regimen_batch(body: _RegimenBatchBody) -> Dict[str, Any]:
    """Upsert multiple regimen items in one call.

    Used by the HITL ProposalCard when the user approves an AI-drafted
    regimen change.  Same upsert logic as _tool_save_regimen_batch in
    llm_service.py: conflicts on (supplement_name, time_of_day) update in place.
    """
    conn = get_db_conn()
    saved: List[str] = []
    errors: List[str] = []
    try:
        for item in body.items:
            if not isinstance(item, dict):
                errors.append(f"Invalid item: {item!r}")
                continue
            name = str(item.get("supplement_name", "")).strip()
            if not name:
                errors.append("supplement_name is required")
                continue
            try:
                conn.execute(
                    """INSERT INTO user_regimen
                           (supplement_name, dose_value, dose_unit, route, site,
                            frequency, time_of_day, days_of_week, notes, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(supplement_name, time_of_day) DO UPDATE SET
                           dose_value   = excluded.dose_value,
                           dose_unit    = excluded.dose_unit,
                           route        = excluded.route,
                           site         = excluded.site,
                           frequency    = excluded.frequency,
                           days_of_week = excluded.days_of_week,
                           notes        = excluded.notes,
                           updated_at   = excluded.updated_at""",
                    (
                        name,
                        str(item.get("dose_value", "")).strip(),
                        str(item.get("dose_unit", "")).strip(),
                        str(item.get("route", "oral")).strip(),
                        str(item.get("site", "") or "").strip() or None,
                        str(item.get("frequency", "daily")).strip(),
                        str(item.get("time_of_day", "morning")).strip(),
                        str(item.get("days_of_week", "") or "").strip() or None,
                        str(item.get("notes", "") or "").strip() or None,
                    ),
                )
                saved.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        # Atomic: rollback the whole batch if any item failed so callers never get
        # partial state.  saved is cleared to reflect that nothing was actually written.
        if errors:
            conn.rollback()
            saved.clear()
        else:
            conn.commit()
    finally:
        conn.close()
    return {"success": len(errors) == 0, "saved": saved, "errors": errors, "count": len(saved)}


class _RegimenByNameBody(BaseModel):
    supplement_name: str
    time_of_day: Optional[str] = None


@app.delete("/api/v1/regimen/by-name", status_code=status.HTTP_200_OK)
async def delete_regimen_by_name(body: _RegimenByNameBody) -> Dict[str, Any]:
    """Delete regimen item(s) by supplement name, optionally scoped to a time_of_day.

    Used by the HITL ProposalCard when the user approves a removal proposal.
    The LLM uses supplement_name (not the UUID) when it drafts removals.
    """
    conn = get_db_conn()
    # Capture rowcount inside the try so cursor is always defined when we use it.
    rows_deleted = 0
    try:
        if body.time_of_day and body.time_of_day.strip():
            rows_deleted = conn.execute(
                "DELETE FROM user_regimen WHERE supplement_name = ? AND time_of_day = ?",
                (body.supplement_name.strip(), body.time_of_day.strip()),
            ).rowcount
        else:
            rows_deleted = conn.execute(
                "DELETE FROM user_regimen WHERE supplement_name = ?",
                (body.supplement_name.strip(),),
            ).rowcount
        conn.commit()
    finally:
        conn.close()
    if rows_deleted == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No regimen entry found for '{body.supplement_name.strip()}'",
        )
    return {"success": True, "supplement_name": body.supplement_name.strip(), "removed": rows_deleted}


@app.get("/api/v1/memory/search", status_code=status.HTTP_200_OK)
async def search_semantic_memory(
    query: str = Query(min_length=1),
    top_k: int = Query(default=5, ge=1, le=20),
) -> Dict[str, str]:
    return {"context": retrieve_context(query=query, top_k=top_k)}


def get_history(entry_type: str, limit: int) -> List[Dict[str, Any]]:
    table_by_type = {
        "supplement": ("supplement_logs", "recorded_at"),
        "biometric": ("biometric_logs", "recorded_at"),
        "lab_result": ("lab_results", "collected_at"),
        "daily_journal": ("daily_journals", "journal_date"),
    }
    table_name, order_column = table_by_type[entry_type]
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT * FROM {table_name} ORDER BY {order_column} DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# ── Garmin sync ───────────────────────────────────────────────────────────────

def _sec_to_hours(seconds: Any) -> Optional[float]:
    """Convert seconds to hours, returning None if the value is missing or invalid."""
    try:
        s = float(seconds)
        return round(s / 3600, 2) if s >= 0 else None
    except (TypeError, ValueError):
        return None


def _fetch_garmin_metrics(client: Any, date_str: str, errors: list) -> list:
    """Pull sleep timing, sleep stages, HRV, SpO2, RHR, and body battery for date_str."""
    metrics: list = []

    # ── Sleep (detailed stages + quality) ────────────────────────────────────
    try:
        sleep_data = client.get_sleep_data(date_str) or {}
        dto = sleep_data.get("dailySleepDTO") or {}

        # Total sleep
        total = _sec_to_hours(dto.get("sleepTimeSeconds"))
        if total and total > 0:
            metrics.append({"metric_name": "sleep_duration",  "value": total,         "unit": "hours"})

        # Sleep timing (bedtime / wake time)
        # Stored as decimal hours using a "shifted" scale so bedtimes after midnight
        # sort correctly against late-evening times:
        #   22:30 -> 22.50   (no shift needed, before midnight)
        #   00:30 -> 24.50   (+24 shift, so it reads "later" than 22:30)
        #   01:45 -> 25.75   (+24 shift)
        # Wake times (sleep_end) are always morning hours and need no shift.
        _start_ts = dto.get("sleepStartTimestampLocal")
        _end_ts   = dto.get("sleepEndTimestampLocal")

        if _start_ts:
            try:
                # sleepStartTimestampLocal is a Unix-ms timestamp where Garmin
                # has already baked in the local offset (i.e. the epoch value)
                # represents the local clock time expressed as if it were UTC.
                # We must therefore interpret it with tz=UTC to get the correct
                # local hour; using the system timezone would double-convert.
                _dt = datetime.fromtimestamp(int(_start_ts) / 1000, tz=timezone.utc)
                _h  = _dt.hour + _dt.minute / 60
                if _h < 12:   # past midnight: shift so it sorts after evening hours
                    _h += 24
                metrics.append({
                    "metric_name": "sleep_start",
                    "value": round(_h, 2),
                    "unit": "hour_of_day",
                    "notes": _dt.strftime("%H:%M"),
                })
            except Exception:
                pass

        if _end_ts:
            try:
                _dt = datetime.fromtimestamp(int(_end_ts) / 1000, tz=timezone.utc)
                _h  = _dt.hour + _dt.minute / 60
                metrics.append({
                    "metric_name": "sleep_end",
                    "value": round(_h, 2),
                    "unit": "hour_of_day",
                    "notes": _dt.strftime("%H:%M"),
                })
            except Exception:
                pass

        # Sleep stages
        deep = _sec_to_hours(dto.get("deepSleepSeconds"))
        if deep is not None:
            metrics.append({"metric_name": "sleep_deep",      "value": deep,          "unit": "hours"})

        light = _sec_to_hours(dto.get("lightSleepSeconds"))
        if light is not None:
            metrics.append({"metric_name": "sleep_light",     "value": light,         "unit": "hours"})

        rem = _sec_to_hours(dto.get("remSleepSeconds"))
        if rem is not None:
            metrics.append({"metric_name": "sleep_rem",       "value": rem,           "unit": "hours"})

        awake = _sec_to_hours(dto.get("awakeSleepSeconds"))
        if awake is not None:
            metrics.append({"metric_name": "sleep_awake",     "value": awake,         "unit": "hours"})

        # SpO2 (blood oxygen during sleep)
        spo2_avg = dto.get("averageSpO2Value")
        if spo2_avg and float(spo2_avg) > 50:
            metrics.append({"metric_name": "sleep_spo2_avg",  "value": round(float(spo2_avg), 1),  "unit": "%"})

        spo2_low = dto.get("lowestSpO2Value")
        if spo2_low and float(spo2_low) > 50:
            metrics.append({"metric_name": "sleep_spo2_low",  "value": round(float(spo2_low), 1),  "unit": "%"})

        # Respiration rate
        resp = dto.get("averageRespirationValue")
        if resp and float(resp) > 0:
            metrics.append({"metric_name": "sleep_respiration", "value": round(float(resp), 1), "unit": "breaths/min"})

        # Sleep stress (Garmin 0–100 score; lower = more restful)
        stress = dto.get("avgSleepStress") or dto.get("averageStressLevel")
        if stress and float(stress) > 0:
            metrics.append({"metric_name": "sleep_stress",    "value": round(float(stress), 1), "unit": "score"})

    except Exception as exc:
        errors.append(f"sleep: {exc}")

    # ── HRV ──────────────────────────────────────────────────────────────────
    try:
        hrv_data = client.get_hrv_data(date_str) or {}
        summary  = hrv_data.get("hrvSummary") or {}
        readings = hrv_data.get("hrvReadings") or []

        # Prefer Garmin's pre-computed lastNight average; fall back to computing
        # the mean from raw 5-minute RMSSD readings if lastNight is null/zero.
        # Some devices/firmware versions omit lastNight even when readings exist.
        last_night = summary.get("lastNight")
        if not last_night or float(last_night) <= 0:
            vals = [
                float(r["hrvValue"])
                for r in readings
                if r.get("hrvValue") and float(r.get("hrvValue", 0)) > 0
            ]
            if vals:
                last_night = round(sum(vals) / len(vals))

        if last_night and float(last_night) > 0:
            metrics.append({"metric_name": "hrv_last_night", "value": int(float(last_night)), "unit": "ms"})

        weekly_avg = summary.get("weeklyAvg")
        if weekly_avg and float(weekly_avg) > 0:
            metrics.append({"metric_name": "hrv_weekly_avg", "value": int(float(weekly_avg)), "unit": "ms"})

    except Exception as exc:
        errors.append(f"hrv: {exc}")

    # ── Resting heart rate ────────────────────────────────────────────────────
    rhr = None
    try:
        stats = client.get_stats(date_str) or {}
        rhr = stats.get("restingHeartRate")
    except Exception:
        pass
    if rhr is None:
        try:
            rhr_data = client.get_rhr_day(date_str) or {}
            rhr_vals = (
                rhr_data.get("allMetrics", {})
                .get("metricsMap", {})
                .get("WELLNESS_RESTING_HEART_RATE", [])
            )
            if rhr_vals:
                rhr = rhr_vals[0].get("value")
        except Exception as exc:
            errors.append(f"rhr: {exc}")
    if rhr is not None:
        metrics.append({"metric_name": "resting_heart_rate", "value": int(rhr), "unit": "bpm"})

    # ── Body Battery ──────────────────────────────────────────────────────────
    try:
        bb_list = client.get_body_battery(date_str, date_str) or []
        if bb_list and isinstance(bb_list, list):
            day      = bb_list[0] if isinstance(bb_list[0], dict) else {}
            bb_vals  = day.get("bodyBatteryValuesArray", [])
            bb_level = bb_vals[-1][1] if bb_vals and len(bb_vals[-1]) > 1 else None
            if bb_level is None:
                bb_level = day.get("charged") or day.get("endBatteryLevel")
            if bb_level is not None:
                metrics.append({"metric_name": "body_battery", "value": int(bb_level), "unit": "%"})
    except Exception as exc:
        errors.append(f"body_battery: {exc}")

    # ── Workout activities ────────────────────────────────────────────────────
    # One row per activity; dedup uses exact start timestamp so multiple
    # workouts on the same day are each stored independently.
    try:
        activities = client.get_activities_by_date(date_str, date_str) or []
        for act in activities:
            avg_hr = act.get("averageHR")
            if not avg_hr or float(avg_hr) <= 0:
                continue  # skip activities without HR data

            act_type_key = (act.get("activityType") or {}).get("typeKey", "")
            act_name     = act.get("activityName") or act_type_key or "Workout"
            max_hr       = act.get("maxHR")
            duration_sec = act.get("duration")
            duration_min = round(float(duration_sec) / 60) if duration_sec else None
            calories     = act.get("calories")
            distance_m   = act.get("distance")

            note_parts: list = [act_name]
            if duration_min:
                note_parts.append(f"{duration_min}min")
            if max_hr and float(max_hr) > 0:
                note_parts.append(f"max HR {int(float(max_hr))}")
            if calories and float(calories) > 0:
                note_parts.append(f"{int(float(calories))}cal")
            if distance_m and float(distance_m) > 0:
                note_parts.append(f"{round(float(distance_m) / 1000, 2)}km")

            start_local = act.get("startTimeLocal") or act.get("startTimeGMT") or ""
            recorded_at_override: Optional[datetime] = None
            if start_local:
                try:
                    recorded_at_override = datetime.fromisoformat(
                        start_local.replace(" ", "T")
                    ).replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            metrics.append({
                "metric_name": "workout_session",
                "value": round(float(avg_hr)),
                "unit": "bpm",
                "notes": " · ".join(note_parts),
                "_recorded_at": recorded_at_override,  # exact ts: triggers per-activity dedup
            })
            # Duration companion row with same timestamp so the UI can join them.
            # Omitted when duration is unknown (no data from Garmin).
            if duration_min:
                metrics.append({
                    "metric_name": "workout_duration",
                    "value": duration_min,
                    "unit": "min",
                    "notes": " · ".join(note_parts),
                    "_recorded_at": recorded_at_override,
                })
    except Exception as exc:
        errors.append(f"activities: {exc}")

    return metrics


def _garmin_auth_client(email: str, password: str) -> Any:
    """Authenticate via saved DI tokens. Raises HTTPException on failure."""
    try:
        from garminconnect import Garmin  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="garminconnect not installed. Run: pip install garminconnect",
        ) from exc

    token_file = Path(__file__).parent / "data" / "garmin_tokens" / "garmin_tokens.json"
    if not token_file.exists():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Garmin not set up yet. Run: .venv\\Scripts\\python.exe garmin_auth.py",
        )
    try:
        client = Garmin(email=email, password=password)
        client.login(tokenstore=str(token_file.parent))
        return client
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Garmin session expired. Re-run garmin_auth.py: {exc}",
        ) from exc


def _do_garmin_sync(email: str, password: str, dates: List[str]) -> Dict[str, Any]:
    """Sync one or more dates. Idempotent: skips existing records."""
    import time as _time

    client = _garmin_auth_client(email, password)

    all_synced: list = []
    all_skipped: list = []
    all_errors: list = []

    for date_str in dates:
        errors: list = []
        raw_metrics = _fetch_garmin_metrics(client, date_str, errors)
        all_errors.extend(errors)

        year, month, day_int = (int(x) for x in date_str.split("-"))
        # Noon UTC keeps the correct calendar date in any US timezone (midnight UTC
        # would render as the previous day for EDT/CDT/PDT/PST users).
        recorded_at = datetime(year, month, day_int, 12, 0, 0, tzinfo=timezone.utc)

        conn = get_db_conn()
        try:
            cursor = conn.cursor()
            for metric in raw_metrics:
                metric_name = metric["metric_name"]
                # Activity metrics carry an exact start timestamp; all others use noon-UTC.
                metric_recorded_at = metric.get("_recorded_at") or recorded_at

                if metric.get("_recorded_at") is not None:
                    # Dedup by minute-truncated timestamp to tolerate the ≤1-second
                    # rounding drift that Garmin's API exhibits between repeated fetches
                    # of the same activity.  Two workouts must start in different minutes
                    # to be stored as separate rows (a safe assumption for real workouts).
                    _minute_prefix = metric_recorded_at.strftime("%Y-%m-%dT%H:%M")
                    cursor.execute(
                        "SELECT 1 FROM biometric_logs "
                        "WHERE metric_name = ? AND context = 'garmin_sync' "
                        "AND recorded_at LIKE ?",  # noqa: B608
                        (metric_name, f"{_minute_prefix}%"),
                    )
                else:
                    cursor.execute(
                        "SELECT 1 FROM biometric_logs WHERE metric_name = ? AND context = 'garmin_sync' AND recorded_at LIKE ?",  # noqa: B608
                        (metric_name, f"{date_str}%"),
                    )
                if cursor.fetchone():
                    all_skipped.append(metric_name)
                    continue
                bio = BiometricLog(
                    metric_name=metric_name,
                    value=float(metric["value"]),
                    unit=metric["unit"],
                    context="garmin_sync",
                    recorded_at=metric_recorded_at,
                    notes=metric.get("notes"),
                    raw_text=f"Garmin sync {date_str}: {metric_name} = {metric['value']} {metric['unit']}",
                )
                insert_biometric(cursor, bio)
                # Strip internal _keys before appending to synced list
                all_synced.append({
                    **{k: v for k, v in metric.items() if not k.startswith("_")},
                    "date": date_str,
                })
            conn.commit()
        except Exception as exc:
            conn.rollback()
            all_errors.append(f"db {date_str}: {exc}")
        finally:
            conn.close()

        if len(dates) > 1:
            _time.sleep(0.4)  # brief pause between days to respect Garmin rate limits

    # Build Mem0 summary (last 7 days only to keep it concise)
    mem0_summary = ""
    recent = [m for m in all_synced if m.get("date", "") >= (date.today() - timedelta(days=7)).isoformat()]
    if recent:
        parts = [f"Garmin historical sync: {len(all_synced)} data points across {len(dates)} day(s)."]
        for m in recent[-20:]:  # cap at 20 entries (more metrics per day now)
            label = m["metric_name"].replace("_", " ")
            parts.append(f"{m['date']}: {label} = {m['value']} {m['unit']}.")
        mem0_summary = " ".join(parts)

    # dates_synced = number of dates that actually produced new rows
    dates_with_new_data = len({m["date"] for m in all_synced})
    return {
        "dates_synced": dates_with_new_data,
        "synced": all_synced,
        "skipped": all_skipped,
        "errors": all_errors,
        "_mem0_summary": mem0_summary,
    }


def _garmin_missing_dates(lookback_days: int = 30) -> List[str]:
    """Return dates (YYYY-MM-DD) in the last *lookback_days* with no garmin_sync data.

    Includes today: Garmin attributes last night's sleep/HRV to the wake-up date
    (today), so i=0 must be in scope to capture the most recent night.
    Results are sorted newest-first.
    """
    cutoff = (date.today() - timedelta(days=lookback_days + 1)).isoformat()
    conn = get_db_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(recorded_at, 1, 10) FROM biometric_logs "
            "WHERE context = 'garmin_sync' AND recorded_at >= ?",
            (cutoff,),
        ).fetchall()
        existing = {r[0] for r in rows}
    finally:
        conn.close()

    target = {
        (date.today() - timedelta(days=i)).isoformat()
        for i in range(0, lookback_days)  # i=0 = today, captures last night's sleep/HRV
    }
    return sorted(target - existing, reverse=True)


@app.post("/api/v1/sync/garmin", status_code=status.HTTP_200_OK)
async def sync_garmin(
    force: bool = Query(
        default=False,
        description=(
            "Re-sync all dates in the lookback window even if data already exists. "
            "Adds any metrics that were absent (e.g. after schema expansions); "
            "per-metric dedup still prevents true duplicates."
        ),
    ),
    days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="How many days back to check / backfill. Default 30.",
    ),
) -> Dict[str, Any]:
    email    = os.getenv("GARMIN_EMAIL",    "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Set GARMIN_EMAIL and GARMIN_PASSWORD in .env to enable Garmin sync",
        )

    if force:
        # Re-touch every date in the window.  Per-metric dedup inside _do_garmin_sync
        # means existing individual readings are never duplicated.
        dates = [
            (date.today() - timedelta(days=i)).isoformat()
            for i in range(0, days)  # i=0 = today (captures last night's sleep/HRV)
        ]
    else:
        # Only sync dates that have zero garmin_sync records to fill gaps automatically.
        dates = await run_in_threadpool(_garmin_missing_dates, days)

    if not dates:
        return {"dates_synced": 0, "synced": [], "skipped": [], "errors": []}

    result = await run_in_threadpool(_do_garmin_sync, email, password, dates)

    mem0_summary = result.pop("_mem0_summary", "")
    if mem0_summary:
        await _mem0_add_safe(mem0_summary)

    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8787"))
    uvicorn.run("api:app", host="127.0.0.1", port=port, reload=True)

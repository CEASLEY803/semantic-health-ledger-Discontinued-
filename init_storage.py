from __future__ import annotations

import os
import sqlite3
from pathlib import Path


_BASE = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(_BASE / "data" / "osassistant.sqlite3")))
WAL_PATH = Path(os.getenv("WAL_PATH", str(_BASE / "data" / "wal.jsonl")))


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS supplement_logs (
    id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    supplement_name TEXT NOT NULL,
    dose_value TEXT NOT NULL,
    dose_unit TEXT NOT NULL,
    route TEXT NOT NULL,
    site TEXT,
    protocol_phase TEXT,
    notes TEXT,
    raw_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS biometric_logs (
    id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    context TEXT,
    notes TEXT,
    raw_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_results (
    id TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    resulted_at TEXT,
    panel_name TEXT,
    marker_name TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value_numeric TEXT,
    value_text TEXT,
    unit TEXT,
    reference_low TEXT,
    reference_high TEXT,
    lab_name TEXT,
    flagged INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    notes TEXT,
    raw_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_journals (
    id TEXT PRIMARY KEY,
    journal_date TEXT NOT NULL,
    mood TEXT,
    energy_score INTEGER CHECK (energy_score BETWEEN 1 AND 10),
    sleep_hours TEXT,
    symptoms TEXT NOT NULL DEFAULT '[]',
    training TEXT,
    nutrition TEXT,
    notes TEXT NOT NULL,
    raw_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_history (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('user', 'model')),
    content TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'legacy',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clinical_nodes (
    id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    concept_name     TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL,
    summary_text     TEXT NOT NULL,
    confidence_level TEXT NOT NULL CHECK (confidence_level IN ('hypothesis', 'testing', 'confirmed')),
    last_updated     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clinical_edges (
    id                TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    source_node_id    TEXT NOT NULL REFERENCES clinical_nodes(id) ON DELETE CASCADE,
    target_node_id    TEXT NOT NULL REFERENCES clinical_nodes(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    evidence_summary  TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_node_id, target_node_id, relationship_type)
);

CREATE TABLE IF NOT EXISTS user_protocols (
    id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    protocol_name    TEXT NOT NULL UNIQUE,
    protocol_content TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_regimen (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    supplement_name TEXT NOT NULL,
    dose_value    TEXT NOT NULL,
    dose_unit     TEXT NOT NULL,
    route         TEXT NOT NULL DEFAULT 'oral',
    site          TEXT,
    frequency     TEXT NOT NULL DEFAULT 'daily',
    time_of_day   TEXT NOT NULL DEFAULT 'morning',
    days_of_week  TEXT,
    notes         TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (supplement_name, time_of_day)
);

CREATE TABLE IF NOT EXISTS transcript_documents (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    source_label TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'processing', 'complete', 'failed')),
    total_chunks INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcript_chunks (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES transcript_documents(id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    speaker        TEXT,
    content        TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    embedding      BLOB,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transcript_chunks_doc
    ON transcript_chunks(document_id);

CREATE TABLE IF NOT EXISTS chat_memory (
    id         TEXT PRIMARY KEY,
    role       TEXT,
    content    TEXT NOT NULL,
    embedding  BLOB,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Cognition loop (the "mind") ───────────────────────────────────────────────
-- cognition_threads: things LEDGER is actively preoccupied with as part of its inner life.
-- Each row is an open question keyed by a stable `signature` so the heartbeat can
-- upsert the same concern across ticks instead of spawning duplicates.
CREATE TABLE IF NOT EXISTS cognition_threads (
    id                TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    signature         TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'watching'
                      CHECK (status IN ('watching', 'resolved', 'dismissed')),
    salience          REAL NOT NULL DEFAULT 0.0,
    detail            TEXT,
    opened_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_considered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_surfaced_at  TEXT
);

-- cognition_log: append-only stream of what LEDGER thought each tick, even when
-- it stays silent. This is the visible inner monologue rendered by the Mind panel.
CREATE TABLE IF NOT EXISTS cognition_log (
    id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    kind       TEXT NOT NULL DEFAULT 'idle'
               CHECK (kind IN ('idle', 'observation', 'spoke')),
    salience   REAL NOT NULL DEFAULT 0.0,
    summary    TEXT NOT NULL,
    detail     TEXT,
    thread_id  TEXT REFERENCES cognition_threads(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cognition_log_created
    ON cognition_log(created_at DESC);
"""


def migrate_chat_session_id(database_path: Path = DATABASE_PATH) -> None:
    """Add session_id column to chat_history for existing databases. Idempotent."""
    with sqlite3.connect(database_path) as conn:
        try:
            conn.execute(
                "ALTER TABLE chat_history ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


def migrate_clinical_nodes_temporal(database_path: Path = DATABASE_PATH) -> None:
    """Add temporal tracking columns to clinical_nodes. Idempotent."""
    with sqlite3.connect(database_path) as conn:
        for ddl in [
            "ALTER TABLE clinical_nodes ADD COLUMN expires_at TEXT",
            "ALTER TABLE clinical_nodes ADD COLUMN last_surfaced_date TEXT",
            "ALTER TABLE clinical_nodes ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(ddl)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists


def migrate_transcript_tables(database_path: Path = DATABASE_PATH) -> None:
    """Create transcript_documents / transcript_chunks for existing databases. Idempotent."""
    with sqlite3.connect(database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript_documents (
                id           TEXT PRIMARY KEY,
                filename     TEXT NOT NULL,
                source_label TEXT,
                status       TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'processing', 'complete', 'failed')),
                total_chunks INTEGER NOT NULL DEFAULT 0,
                error        TEXT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS transcript_chunks (
                id             TEXT PRIMARY KEY,
                document_id    TEXT NOT NULL REFERENCES transcript_documents(id) ON DELETE CASCADE,
                chunk_index    INTEGER NOT NULL,
                speaker        TEXT,
                content        TEXT NOT NULL,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                embedding      BLOB,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_transcript_chunks_doc
                ON transcript_chunks(document_id);
            """
        )
        conn.commit()


def migrate_chat_memory_table(database_path: Path = DATABASE_PATH) -> None:
    """Create chat_memory (embedding-backed conversation recall) for existing DBs. Idempotent."""
    with sqlite3.connect(database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_memory (
                id         TEXT PRIMARY KEY,
                role       TEXT,
                content    TEXT NOT NULL,
                embedding  BLOB,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()


def migrate_cognition_tables(database_path: Path = DATABASE_PATH) -> None:
    """Create cognition_threads / cognition_log for existing databases. Idempotent."""
    with sqlite3.connect(database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cognition_threads (
                id                TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                signature         TEXT NOT NULL UNIQUE,
                title             TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'watching'
                                  CHECK (status IN ('watching', 'resolved', 'dismissed')),
                salience          REAL NOT NULL DEFAULT 0.0,
                detail            TEXT,
                opened_at         TEXT NOT NULL DEFAULT (datetime('now')),
                last_considered_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_surfaced_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS cognition_log (
                id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                kind       TEXT NOT NULL DEFAULT 'idle'
                           CHECK (kind IN ('idle', 'observation', 'spoke')),
                salience   REAL NOT NULL DEFAULT 0.0,
                summary    TEXT NOT NULL,
                detail     TEXT,
                thread_id  TEXT REFERENCES cognition_threads(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_cognition_log_created
                ON cognition_log(created_at DESC);
            """
        )
        conn.commit()


def migrate_compound_to_supplement(database_path: Path = DATABASE_PATH) -> None:
    """Rename compound_logs->supplement_logs and compound_name->supplement_name. Idempotent.

    Preserves existing data. Robust to call order: if the schema already auto-created an
    EMPTY supplement_logs, it is dropped first so the real compound_logs can be renamed in.
    """
    with sqlite3.connect(database_path) as conn:
        cur = conn.cursor()

        def tables() -> set:
            return {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        def cols(t: str) -> set:
            return {r[1] for r in cur.execute(f"PRAGMA table_info('{t}')").fetchall()}

        t = tables()
        if "compound_logs" in t:
            # Drop an empty, schema-auto-created supplement_logs so the rename can carry data.
            if "supplement_logs" in t:
                n = cur.execute("SELECT COUNT(*) FROM supplement_logs").fetchone()[0]
                if n == 0:
                    cur.execute("DROP TABLE supplement_logs")
                    t = tables()
            if "supplement_logs" not in t:
                cur.execute("ALTER TABLE compound_logs RENAME TO supplement_logs")
                t = tables()

        # Column renames (SQLite >= 3.25 supports RENAME COLUMN).
        for tbl in ("supplement_logs", "user_regimen"):
            if tbl in t:
                c = cols(tbl)
                if "compound_name" in c and "supplement_name" not in c:
                    try:
                        cur.execute(
                            f"ALTER TABLE {tbl} RENAME COLUMN compound_name TO supplement_name"
                        )
                    except sqlite3.OperationalError:
                        pass  # already renamed or unsupported
        conn.commit()


def initialize_storage(database_path: Path = DATABASE_PATH, wal_path: Path = WAL_PATH) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    wal_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)

    wal_path.touch(exist_ok=True)


if __name__ == "__main__":
    initialize_storage()
    print(f"Initialized SQLite database at {DATABASE_PATH}")
    print(f"Initialized raw event WAL at {WAL_PATH}")

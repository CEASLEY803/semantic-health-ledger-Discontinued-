"""Cognition Loop: LEDGER's continuous inner life.

Unlike the morning Reflection Engine (reflection_worker.py), which runs once a day
in a big batch, the cognition loop runs on a *heartbeat*: every few minutes it wakes,
looks at what changed since it last thought, and decides whether anything is worth
saying. Most ticks it stays quiet and just writes a line to its inner monologue
(cognition_log). Occasionally a tripwire fires and, if the governor allows, it
speaks an unsolicited message into the chat.

Design (mirrors reflection_worker.py): synchronous, all blocking I/O. The async
heartbeat in api.py calls run_cognition_tick() via run_in_threadpool(); persisting
the spoken message to chat_history and emitting it on the event bus is the caller's
job (those need the asyncio loop).

Two bars, by design:
  * THINK bar (low): Every tick writes to cognition_log. This is the inner life.
  * SPEAK bar (high): Only a salient, un-deduped, in-budget thought reaches the user.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from init_storage import DATABASE_PATH
from llm_service import (
    EXTRACTOR_MODEL,
    CONVERSATION_MODEL,
    _get_extractor_client,
    _get_conversation_client,
)

logger = logging.getLogger(__name__)


# ── Tunables (env-overridable) ─────────────────────────────────────────────────

def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default

def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


HEARTBEAT_SECONDS   = _envi("COGNITION_INTERVAL_SECONDS", 300)   # used by api.py heartbeat
STARTUP_DELAY_SECONDS = _envi("COGNITION_STARTUP_DELAY_SECONDS", 120)
DAILY_BUDGET        = _envi("COGNITION_DAILY_BUDGET", 3)         # max unsolicited msgs/day
QUIET_START_HOUR    = _envi("COGNITION_QUIET_START", 22)         # local hour, inclusive
QUIET_END_HOUR      = _envi("COGNITION_QUIET_END", 8)            # local hour, exclusive
COOLDOWN_MINUTES    = _envi("COGNITION_COOLDOWN_MINUTES", 90)    # min gap between spoken msgs
DEDUP_HOURS         = _envi("COGNITION_DEDUP_HOURS", 20)         # don't re-surface same signature
ZSCORE_THRESHOLD    = _envf("COGNITION_ZSCORE", 1.5)            # biometric anomaly bar
SPEAK_BAR           = _envf("COGNITION_SPEAK_BAR", 0.6)         # salience needed to speak
ESCALATE_BAR        = _envf("COGNITION_ESCALATE_BAR", 0.85)     # salience needed for the Pro model
SNOOZE_HOURS        = _envf("COGNITION_SNOOZE_HOURS", 8)         # how long "not now" silences the loop

# Biometric metrics worth reacting to, with the direction that signals concern.
# value: (human label, unit, "low" if a drop is the concern else "high")
WATCHED_METRICS: Dict[str, Tuple[str, str, str]] = {
    "hrv_last_night":     ("HRV (last night)",   "ms",  "low"),
    "resting_heart_rate": ("resting heart rate", "bpm", "high"),
    "sleep_duration":     ("sleep duration",     "h",   "low"),
    "body_battery":       ("Body Battery",       "%",   "low"),
}

# Idle-tick monologue phrasings, rotated so the inner voice doesn't read like a cron job.
_IDLE_LINES = [
    "Swept the last {n} new reading(s). Nothing crosses the line, so holding steady.",
    "Quiet pass. {n} new data point(s), all within the expected band.",
    "Looked things over. No anomalies in {n} new reading(s); {t} thread(s) still open.",
    "Checked in on the data. Everything is steady, with nothing worth interrupting you for.",
    "Reviewed {n} new reading(s). Baselines hold; staying out of your way.",
]


# ── State helpers (system_state) ───────────────────────────────────────────────

def _get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _to_float(v: Any) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── Candidate gathering (the tripwires) ────────────────────────────────────────

def _gather_flagged_labs(conn: sqlite3.Connection, watermark: str, tick_now: str) -> List[Dict]:
    """Tripwire 1: a lab result newly committed (since last tick) and flagged out of range."""
    rows = conn.execute(
        "SELECT marker_name, panel_name, value_numeric, unit, reference_low, reference_high, collected_at "
        "FROM lab_results "
        "WHERE flagged = 1 AND created_at > ? AND created_at <= ? "
        "ORDER BY created_at DESC",
        (watermark, tick_now),
    ).fetchall()

    out: List[Dict] = []
    for r in rows:
        marker = r["marker_name"]
        val = r["value_numeric"]
        unit = (r["unit"] or "").strip()
        lo, hi = r["reference_low"], r["reference_high"]
        ref = ""
        if lo is not None or hi is not None:
            ref = f" (ref {lo or 'n/a'}–{hi or 'n/a'})"
        out.append({
            "signature": f"lab:{_norm(marker)}",
            "title": f"{marker} flagged out of range",
            "salience": 0.9,
            "detail": f"{marker} = {val} {unit}{ref}, collected {str(r['collected_at'])[:10]}.",
            "fact": f"A new lab result came in flagged: {marker} = {val} {unit}{ref} "
                    f"(panel: {r['panel_name'] or 'n/a'}, collected {str(r['collected_at'])[:10]}).",
        })
    return out


def _gather_biometric_anomalies(conn: sqlite3.Connection, watermark: str, tick_now: str) -> List[Dict]:
    """Tripwire 2: a watched biometric newly logged that deviates from its 14-day baseline."""
    placeholders = ",".join("?" for _ in WATCHED_METRICS)
    new_rows = conn.execute(
        f"SELECT metric_name, value, unit, recorded_at, created_at "
        f"FROM biometric_logs "
        f"WHERE created_at > ? AND created_at <= ? AND metric_name IN ({placeholders}) "
        f"ORDER BY recorded_at DESC",
        (watermark, tick_now, *WATCHED_METRICS.keys()),
    ).fetchall()

    # Keep only the newest reading per metric this tick.
    newest: Dict[str, sqlite3.Row] = {}
    for r in new_rows:
        newest.setdefault(r["metric_name"], r)

    out: List[Dict] = []
    fourteen_days_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    for metric, r in newest.items():
        val = _to_float(r["value"])
        if val is None:
            continue
        label, unit, concern_dir = WATCHED_METRICS[metric]

        # Baseline: prior 14 days of this metric, excluding the reading we're judging.
        hist = conn.execute(
            "SELECT value FROM biometric_logs "
            "WHERE metric_name = ? AND recorded_at >= ? AND recorded_at < ? "
            "ORDER BY recorded_at DESC",
            (metric, fourteen_days_ago, r["recorded_at"]),
        ).fetchall()
        vals = [v for v in (_to_float(x["value"]) for x in hist) if v is not None]
        if len(vals) < 4:
            continue  # not enough history to call anything anomalous yet

        mean = statistics.fmean(vals)
        stdev = statistics.pstdev(vals)
        if stdev <= 0:
            continue
        z = (val - mean) / stdev
        if abs(z) < ZSCORE_THRESHOLD:
            continue

        direction = "below" if z < 0 else "above"
        # Salience: only the concerning direction earns high salience; the other
        # direction is still noted but quieter (a good HRV night isn't urgent).
        concerning = (concern_dir == "low" and z < 0) or (concern_dir == "high" and z > 0)
        salience = min(0.95, 0.45 + 0.18 * abs(z)) if concerning else min(0.55, 0.25 + 0.1 * abs(z))

        out.append({
            "signature": f"biom:{metric}",
            "title": f"{label} {direction} baseline ({z:+.1f}σ)",
            "salience": round(salience, 3),
            "detail": f"{label} = {val:g} {unit} vs 14-day mean {mean:.1f} "
                      f"(σ={stdev:.1f}, z={z:+.2f}).",
            "fact": f"{label} just logged at {val:g} {unit}, which is {abs(z):.1f} standard "
                    f"deviations {direction} the 14-day average of {mean:.1f} {unit}.",
        })
    return out


def _gather_resolutions(conn: sqlite3.Connection, watermark: str, tick_now: str) -> List[Dict]:
    """Continuity: an open biometric thread whose metric just logged a NEW reading that
    is back within baseline. The concern has resolved on its own, so close the loop.

    Returns follow-through candidates carrying ``resolves_signature`` (the original
    thread's signature). These are NOT upserted as new threads; the caller marks the
    referenced thread 'resolved'.
    """
    try:
        threads = conn.execute(
            "SELECT signature, title FROM cognition_threads "
            "WHERE status = 'watching' AND last_surfaced_at IS NOT NULL "
            "AND signature LIKE 'biom:%'",
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not threads:
        return []

    out: List[Dict] = []
    fourteen_days_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    for t in threads:
        metric = t["signature"].split(":", 1)[1]
        if metric not in WATCHED_METRICS:
            continue
        r = conn.execute(
            "SELECT value, unit, recorded_at FROM biometric_logs "
            "WHERE metric_name = ? AND created_at > ? AND created_at <= ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (metric, watermark, tick_now),
        ).fetchone()
        if not r:
            continue  # no fresh reading this tick, so resolution cannot be judged yet
        val = _to_float(r["value"])
        if val is None:
            continue
        hist = conn.execute(
            "SELECT value FROM biometric_logs "
            "WHERE metric_name = ? AND recorded_at >= ? AND recorded_at < ? "
            "ORDER BY recorded_at DESC",
            (metric, fourteen_days_ago, r["recorded_at"]),
        ).fetchall()
        vals = [v for v in (_to_float(x["value"]) for x in hist) if v is not None]
        if len(vals) < 4:
            continue
        mean = statistics.fmean(vals)
        stdev = statistics.pstdev(vals)
        if stdev <= 0:
            continue
        z = (val - mean) / stdev
        if abs(z) >= ZSCORE_THRESHOLD:
            continue  # still out of band, not resolved

        label, unit, _ = WATCHED_METRICS[metric]
        out.append({
            "signature": f"resolved:{metric}",
            "resolves_signature": t["signature"],
            "title": f"{label} back to baseline",
            "salience": round(min(0.7, SPEAK_BAR + 0.02), 3),  # speakable, but low priority
            "detail": f"{label} = {val:g} {unit} (z={z:+.2f}), within range again.",
            "fact": f"{label} is back to {val:g} {unit} and within the normal range now. The "
                    f"deviation I flagged earlier has resolved.",
        })
    return out


def _gather_temporal_milestones(conn: sqlite3.Connection) -> List[Dict]:
    """Tripwire 3: a tracked supplement reaching its estimated full systemic clearance.

    State-based (not watermark-based): fires when today is within ±1 day of a temporal
    node's expires_at (≈ 5 half-lives from discontinuation). Deduped against the node's
    own last_surfaced_date so it doesn't collide with the morning synthesis inquiries.
    """
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    try:
        rows = conn.execute(
            "SELECT concept_name, summary_text, expires_at, last_surfaced_date "
            "FROM clinical_nodes "
            "WHERE COALESCE(is_archived, 0) = 0 AND expires_at IS NOT NULL",
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # pre-migration schema without expires_at/last_surfaced_date

    out: List[Dict] = []
    for r in rows:
        exp_raw = str(r["expires_at"])[:10]
        try:
            exp = datetime.fromisoformat(exp_raw).date()
        except (ValueError, TypeError):
            continue
        if abs((exp - today).days) > 1:
            continue  # not at the milestone window
        if r["last_surfaced_date"] and str(r["last_surfaced_date"])[:10] == today_iso:
            continue  # already surfaced today (by synthesis or a prior tick)
        name = r["concept_name"]
        out.append({
            "signature": f"temporal:{_norm(name)}",
            "title": f"{name}: full-clearance window",
            "salience": 0.7,
            "detail": f"Estimated full clearance ~{exp_raw} ({(exp - today).days:+d}d).",
            "fact": f"A supplement you discontinued (tracked as '{name}') is reaching its "
                    f"estimated full systemic clearance around {exp_raw}. "
                    f"Context: {(r['summary_text'] or '')[:300]}",
            "node_name": name,
        })
    return out


# ── Thread persistence ─────────────────────────────────────────────────────────

def _upsert_thread(conn: sqlite3.Connection, cand: Dict) -> str:
    """Upsert an open thread keyed by signature; returns its id."""
    conn.execute(
        "INSERT INTO cognition_threads (signature, title, salience, detail, status, "
        "                               opened_at, last_considered_at) "
        "VALUES (?, ?, ?, ?, 'watching', datetime('now'), datetime('now')) "
        "ON CONFLICT(signature) DO UPDATE SET "
        "    title = excluded.title, "
        "    salience = excluded.salience, "
        "    detail = excluded.detail, "
        "    status = 'watching', "
        "    last_considered_at = datetime('now')",
        (cand["signature"], cand["title"], cand["salience"], cand["detail"]),
    )
    row = conn.execute(
        "SELECT id, last_surfaced_at FROM cognition_threads WHERE signature = ?",
        (cand["signature"],),
    ).fetchone()
    return row["id"]


def _open_thread_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM cognition_threads WHERE status = 'watching'"
    ).fetchone()
    return int(row["n"]) if row else 0


def _write_log(conn: sqlite3.Connection, kind: str, salience: float,
               summary: str, detail: Optional[str] = None,
               thread_id: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO cognition_log (kind, salience, summary, detail, thread_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, salience, summary, detail, thread_id),
    )


# ── Governor ───────────────────────────────────────────────────────────────────

def _in_quiet_hours() -> bool:
    """True if the local hour falls inside the quiet window (default 22:00–08:00)."""
    h = datetime.now().hour
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return False
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= h < QUIET_END_HOUR
    # window wraps midnight
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def _spoken_today(conn: sqlite3.Connection) -> int:
    today = datetime.now().date().isoformat()
    key = f"cognition_spoken_{today}"
    raw = _get_state(conn, key)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _record_spoke(conn: sqlite3.Connection) -> None:
    today = datetime.now().date().isoformat()
    key = f"cognition_spoken_{today}"
    _set_state(conn, key, str(_spoken_today(conn) + 1))
    _set_state(conn, "cognition_last_spoke_at", datetime.now(timezone.utc).isoformat())


def _cooldown_active(conn: sqlite3.Connection) -> bool:
    raw = _get_state(conn, "cognition_last_spoke_at")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(minutes=COOLDOWN_MINUTES)


def _get_bar_adjust(conn: sqlite3.Connection) -> float:
    """Learned offset to the speak bar, raised by 👎 and lowered by 👍 (see record_feedback)."""
    raw = _get_state(conn, "cognition_bar_adjust")
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _snoozed(conn: sqlite3.Connection) -> bool:
    """True while a 'not now' snooze window is still active."""
    raw = _get_state(conn, "cognition_snooze_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < until


def _recently_surfaced(conn: sqlite3.Connection, signature: str) -> bool:
    row = conn.execute(
        "SELECT last_surfaced_at FROM cognition_threads WHERE signature = ?",
        (signature,),
    ).fetchone()
    if not row or not row["last_surfaced_at"]:
        return False
    raw = row["last_surfaced_at"]
    try:
        # last_surfaced_at is written via datetime('now') → "YYYY-MM-DD HH:MM:SS" (UTC)
        last = datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return False
    return datetime.now(timezone.utc) - last < timedelta(hours=DEDUP_HOURS)


# ── Phrasing (the only LLM call, and only when we decide to speak) ──────────────

_SPEAK_SYSTEM_PROMPT = """
You are LEDGER, the user's clinical co-pilot. You have been quietly watching their
data and something just caught your attention. Speak to them UNPROMPTED; they did
not ask you anything; you are reaching out on your own.

Write 1–2 sentences, warm but precise. Lead with the specific observation (name the
marker and number). If useful, end with one short, concrete question or offer. No
greeting, no "I noticed that as your assistant" preamble, no markdown headers. Sound
like a sharp friend who happens to know the medicine, rather than a notification bot.
Do NOT invent data beyond what you are given.
""".strip()


_ESCALATE_SYSTEM_PROMPT = """
You are LEDGER, the user's clinical co-pilot, reaching out UNPROMPTED about something
important you just noticed in their data. This one matters, so think before you speak.

Before writing, silently check the observation against WHAT YOU ALREADY BELIEVE:
  * Does this new data CONTRADICT an established (confirmed) theory? If so, say so plainly;
    a confirmed belief meeting disconfirming evidence is exactly what the user needs to hear.
  * If it fits a known mechanism, name the mechanism (receptor, axis, enzyme, PK pathway)
    in plain language.

Then write 2–3 sentences, warm but precise. Lead with the specific observation (name the
marker and number). If a belief is now in tension, name the tension. End with one concrete
question or next step. No greeting, no preamble, no markdown headers, no bullet lists.
Sound like a sharp friend who knows the medicine. Do NOT invent data beyond what you are given.
""".strip()


def _build_phrasing_prompt(fact: str, graph_context: str) -> str:
    return (
        f"OBSERVATION (the reason you are reaching out):\n{fact}\n\n"
        f"WHAT YOU ALREADY BELIEVE (active theories; stay consistent and flag contradictions):\n"
        f"{graph_context or '  (no established theories yet)'}\n\n"
        "Write your short unprompted message now."
    )


def _compose_message_flash(fact: str, graph_context: str) -> Optional[str]:
    """Cheap path: Flash phrases one short message for a routine observation."""
    try:
        client = _get_extractor_client()
        from google.genai import types  # noqa: PLC0415

        # Disable thinking: this is a 1–2 sentence phrasing job, and on Gemini 2.5
        # Flash the thinking pass would otherwise eat the output-token budget and
        # truncate the message mid-sentence.
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": _SPEAK_SYSTEM_PROMPT,
            "temperature": 0.7,
            "max_output_tokens": 400,
        }
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:  # noqa: BLE001 - older SDKs without thinking control
            pass

        resp = client.models.generate_content(
            model=EXTRACTOR_MODEL,
            contents=_build_phrasing_prompt(fact, graph_context),
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cognition] Flash phrasing call failed: %s", exc)
        return None


def _compose_message_pro(fact: str, graph_context: str) -> Optional[str]:
    """Escalation path: the Pro model reasons about contradictions/mechanism for a
    high-salience thought. Thinking is left ON because that is the point of escalating."""
    try:
        client = _get_conversation_client()
        from google.genai import types  # noqa: PLC0415

        resp = client.models.generate_content(
            model=CONVERSATION_MODEL,
            contents=_build_phrasing_prompt(fact, graph_context),
            config=types.GenerateContentConfig(
                system_instruction=_ESCALATE_SYSTEM_PROMPT,
                temperature=0.7,
            ),
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cognition] Pro phrasing call failed: %s", exc)
        return None


def _compose_message(fact: str, graph_context: str, salience: float) -> Optional[str]:
    """Pick the model by salience: Pro (with contradiction reasoning) for the heavy
    ones, Flash for routine observations. Falls back to Flash if Pro fails."""
    if salience >= ESCALATE_BAR:
        text = _compose_message_pro(fact, graph_context)
        if text:
            return text
        logger.info("[cognition] Pro escalation failed, falling back to Flash")
    return _compose_message_flash(fact, graph_context)


def _graph_context(conn: sqlite3.Connection, limit: int = 6) -> str:
    """Brief active-graph summary so phrasing stays consistent with known theories.

    Non-essential: any failure (e.g. pre-migration schema) returns "" rather than
    blocking a spoken thought.
    """
    try:
        rows = conn.execute(
            "SELECT concept_name, confidence_level, summary_text FROM clinical_nodes "
            "WHERE COALESCE(is_archived, 0) = 0 ORDER BY last_updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    return "\n".join(
        f"  [{(r['confidence_level'] or 'hypothesis').upper()}] {r['concept_name']}: "
        f"{(r['summary_text'] or '')[:160]}"
        for r in rows
    )


# ── Main tick ──────────────────────────────────────────────────────────────────

def run_cognition_tick(force_speak: bool = False) -> Dict[str, Any]:
    """One heartbeat of the cognition loop.

    Always writes one cognition_log row (the inner monologue). Returns a dict; when
    it decides to speak, ``content`` holds the message text for the async caller to
    persist to chat_history and emit on the event bus.

    force_speak=True bypasses quiet hours / budget / cooldown / dedup (manual testing).
    """
    result: Dict[str, Any] = {
        "spoke": False, "content": None, "signature": None,
        "title": None, "summary": "", "salience": 0.0, "open_threads": 0,
    }

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        tick_now = conn.execute("SELECT datetime('now')").fetchone()[0]
        watermark = _get_state(conn, "cognition_watermark")

        # First run ever: calibrate the watermark to "now" so we don't dump the
        # entire backlog of historical flagged labs in one burst.
        if watermark is None:
            _set_state(conn, "cognition_watermark", tick_now)
            _write_log(conn, "idle", 0.0,
                       "First cognition tick - calibrating. I'll watch from here on.")
            conn.commit()
            result["summary"] = "calibrating"
            return result

        # ── Gather candidates (cheap, deterministic, no LLM) ───────────────────
        candidates = (
            _gather_flagged_labs(conn, watermark, tick_now)
            + _gather_biometric_anomalies(conn, watermark, tick_now)
            + _gather_temporal_milestones(conn)
        )
        resolutions = _gather_resolutions(conn, watermark, tick_now)

        # Count of new readings this tick (for the idle monologue line).
        new_count = conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM biometric_logs WHERE created_at > ? AND created_at <= ?) + "
            "  (SELECT COUNT(*) FROM lab_results    WHERE created_at > ? AND created_at <= ?) + "
            "  (SELECT COUNT(*) FROM supplement_logs  WHERE created_at > ? AND created_at <= ?) + "
            "  (SELECT COUNT(*) FROM daily_journals WHERE created_at > ? AND created_at <= ?) AS n",
            (watermark, tick_now) * 4,
        ).fetchone()["n"]

        # Advance the watermark; a half-open window ensures no skips or double-counts.
        _set_state(conn, "cognition_watermark", tick_now)

        # Upsert threads for every live concern (inner life: it's now preoccupied).
        for cand in candidates:
            cand["thread_id"] = _upsert_thread(conn, cand)

        # Resolutions reference an existing thread and CLOSE it because the concern is over
        # whether or not we end up announcing it. Mark resolved now; link for logging.
        for res in resolutions:
            row = conn.execute(
                "SELECT id FROM cognition_threads WHERE signature = ?",
                (res["resolves_signature"],),
            ).fetchone()
            res["thread_id"] = row["id"] if row else None
            conn.execute(
                "UPDATE cognition_threads SET status = 'resolved', "
                "last_considered_at = datetime('now') WHERE signature = ?",
                (res["resolves_signature"],),
            )
            _write_log(conn, "observation", res["salience"],
                       f"Resolved: {res['title']}.", detail=res["detail"],
                       thread_id=res.get("thread_id"))

        open_threads = _open_thread_count(conn)
        result["open_threads"] = open_threads

        # Speakable pool = live concerns + resolution follow-throughs.
        speakable = candidates + resolutions
        if not speakable:
            line = random.choice(_IDLE_LINES).format(n=new_count, t=open_threads)
            _write_log(conn, "idle", 0.0, line)
            conn.commit()
            result["summary"] = line
            return result

        # Record the observation in the monologue when a candidate is found.
        top = max(speakable, key=lambda c: c["salience"])
        obs_summary = (
            f"Flagged {len(speakable)} signal(s). Top: {top['title']} "
            f"(salience {top['salience']:.2f})."
        )
        _write_log(conn, "observation", top["salience"], obs_summary,
                   detail="; ".join(c["detail"] for c in speakable),
                   thread_id=top.get("thread_id"))
        result["summary"] = obs_summary
        result["salience"] = top["salience"]
        result["signature"] = top["signature"]
        result["title"] = top["title"]

        # ── Governor: should this observation become a spoken message? ─────────
        effective_bar = SPEAK_BAR + _get_bar_adjust(conn)  # learned from 👍/👎 feedback
        block: Optional[str] = None
        if top["salience"] < effective_bar:
            block = f"below speak bar ({effective_bar:.2f})"
        elif not force_speak and _snoozed(conn):
            block = "snoozed"
        elif not force_speak and _in_quiet_hours():
            block = "quiet hours"
        elif not force_speak and _spoken_today(conn) >= DAILY_BUDGET:
            block = "daily budget reached"
        elif not force_speak and _cooldown_active(conn):
            block = "cooldown active"
        elif not force_speak and _recently_surfaced(conn, top["signature"]):
            block = "recently surfaced (dedup)"

        if block:
            _write_log(conn, "observation", top["salience"],
                       f"Held back: {block}.", thread_id=top.get("thread_id"))
            conn.commit()
            return result

        # ── Speak ──────────────────────────────────────────────────────────────
        content = _compose_message(top["fact"], _graph_context(conn), top["salience"])
        if not content:
            # Fall back to a plain sentence if phrasing fails so the
            # observation isn't silently swallowed.
            content = top["fact"]

        conn.execute(
            "UPDATE cognition_threads SET last_surfaced_at = datetime('now') WHERE signature = ?",
            (top["signature"],),
        )
        # Temporal milestones dedup on the node's own last_surfaced_date (shared with
        # morning synthesis) so the milestone isn't raised twice in one day.
        if top["signature"].startswith("temporal:") and top.get("node_name"):
            try:
                conn.execute(
                    "UPDATE clinical_nodes SET last_surfaced_date = date('now') WHERE concept_name = ?",
                    (top["node_name"],),
                )
            except sqlite3.OperationalError:
                pass
        _write_log(conn, "spoke", top["salience"],
                   f"Spoke up: {top['title']}.", detail=content,
                   thread_id=top.get("thread_id"))
        _record_spoke(conn)
        conn.commit()

        result["spoke"] = True
        result["content"] = content
        logger.info("[cognition] spoke (sig=%s salience=%.2f): %s",
                    top["signature"], top["salience"], content[:120])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("[cognition] tick failed: %s", exc, exc_info=True)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        result["summary"] = f"tick error: {exc}"
        return result
    finally:
        conn.close()


# ── Read API for the Mind panel ────────────────────────────────────────────────

def get_cognition_log(limit: int = 50) -> Dict[str, Any]:
    """Return the recent inner-monologue stream + currently open threads."""
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        log = [dict(r) for r in conn.execute(
            "SELECT id, kind, salience, summary, detail, thread_id, created_at "
            "FROM cognition_log ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()]
        threads = [dict(r) for r in conn.execute(
            "SELECT id, signature, title, status, salience, detail, "
            "       opened_at, last_considered_at, last_surfaced_at "
            "FROM cognition_threads WHERE status = 'watching' "
            "ORDER BY salience DESC, last_considered_at DESC",
        ).fetchall()]
        return {"log": log, "threads": threads}
    finally:
        conn.close()


def record_feedback(signature: Optional[str], rating: str) -> Dict[str, Any]:
    """Attune the loop from user feedback on a proactive message.

    rating:
      * 'up'     : that was useful; lower the speak bar slightly (more forthcoming).
      * 'down'   : not useful; raise the speak bar (more selective) and dismiss the
                   thread so the same concern stops resurfacing.
      * 'snooze' : not now; silence all proactive speech for SNOOZE_HOURS.
    """
    rating = (rating or "").strip().lower()
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        adj = _get_bar_adjust(conn)
        if rating == "down":
            adj = min(0.30, adj + 0.05)
            _set_state(conn, "cognition_bar_adjust", str(round(adj, 3)))
            if signature:
                conn.execute(
                    "UPDATE cognition_threads SET status = 'dismissed' WHERE signature = ?",
                    (signature,),
                )
        elif rating == "up":
            adj = max(-0.15, adj - 0.03)
            _set_state(conn, "cognition_bar_adjust", str(round(adj, 3)))
        elif rating == "snooze":
            until = (datetime.now(timezone.utc) + timedelta(hours=SNOOZE_HOURS)).isoformat()
            _set_state(conn, "cognition_snooze_until", until)
        else:
            return {"ok": False, "error": f"unknown rating: {rating!r}"}
        conn.commit()
        return {
            "ok": True,
            "rating": rating,
            "bar_adjust": round(adj, 3),
            "effective_speak_bar": round(SPEAK_BAR + adj, 3),
        }
    finally:
        conn.close()

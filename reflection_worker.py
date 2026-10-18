"""Morning Reflection Engine: LEDGER's overnight reasoning step.

run_morning_synthesis() is called once per calendar day (from the FastAPI lifespan
boot sequence). It reads the last 24h of telemetry + recent chats + the current
Knowledge Graph, feeds them to Gemini, and writes back structured graph updates.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from init_storage import DATABASE_PATH
from llm_service import CONVERSATION_MODEL, _get_conversation_client

logger = logging.getLogger(__name__)


def _save_system_state(key: str, value: str) -> None:
    """Upsert a key-value pair in the system_state table."""
    db_path = str(DATABASE_PATH)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )


# ── Synthesis system prompt ────────────────────────────────────────────────────

_SYNTHESIS_SYSTEM_PROMPT = """
You are LEDGER, running your morning analysis.

Your job: review yesterday's data, re-examine your existing theories, and output ONLY
a JSON array of graph update commands. No prose. No markdown. Just the array.

[GRAPH MUTATION RULES]

You are MUTATING an EXISTING knowledge graph rather than generating a new one from scratch.
The user prompt contains [CURRENT GRAPH STATE] showing every active node and edge.

PRESERVE:
- Every node in the existing graph that is still relevant.
- Do NOT issue UPDATE_NODE with is_archived=1 simply because a node was not mentioned
  in today's telemetry. Absence from recent logs ≠ staleness.
- An established protocol, confirmed supplement, or long-term hypothesis remains valid
  until you have SPECIFIC, EXPLICIT CONTRARY EVIDENCE in today's data.
- Do NOT re-create nodes with slightly different names. If the node exists, UPDATE it.

ADD or UPDATE:
- Only output commands for nodes and edges that genuinely need to change based on
  today's data. Nodes not mentioned today need no command and are preserved as-is.

CONFIDENCE LEVEL PROTECTION:
- Via CREATE_NODE: confidence_level can only move forward (hypothesis → testing → confirmed).
  The backend will never downgrade a 'confirmed' or 'testing' node to 'hypothesis' via
  CREATE_NODE; use UPDATE_NODE if you have specific evidence warranting a downgrade.

[STRICT ENTITY RESOLUTION]

concept_name is the UNIQUE KEY. It is case-sensitive and whitespace-sensitive.
Copy-paste concept_name values exactly from the [CURRENT GRAPH STATE]. Do NOT rephrase,
pluralize, abbreviate, or capitalize differently.

For CREATE_EDGE: source_concept and target_concept MUST be EXACT character-for-character
copies of the concept_name field shown in the NODES list. Any mismatch, even a single
character difference, will cause the edge to be silently dropped, corrupting the graph.

Before writing a CREATE_EDGE command, verify both names appear verbatim in the NODES list.
If you are unsure of the exact name, use the NODES list as your reference and copy literally.

Supported commands:

{"command": "CREATE_NODE", "concept_name": "string", "category": "string", "summary_text": "string", "confidence_level": "hypothesis|testing|confirmed", "half_life_days": float (TEMPORAL NODES ONLY), "discontinued_at": "YYYY-MM-DD" (TEMPORAL NODES ONLY: the date the supplement was last taken/discontinued; Python computes expires_at = discontinued_at + 5 × half_life_days; if omitted, falls back to today; do NOT pass expires_at directly)}

{"command": "UPDATE_NODE", "concept_name": "string (must match existing)", "summary_text": "string (optional)", "confidence_level": "hypothesis|testing|confirmed (optional)", "last_surfaced_date": "ISO date string (optional)", "is_archived": 0 or 1 (optional)}

{"command": "CREATE_EDGE", "source_concept": "string (must match existing node)", "target_concept": "string (must match existing node)", "relationship_type": "CAUSES|MITIGATES|EXACERBATES|CORRELATES_WITH|REQUIRES|PRECEDES", "evidence_summary": "string"}

{"command": "SET_DAILY_INQUIRIES", "questions": ["question 1", "question 2", "question 3"]}

Rules:
- CREATE_NODE uses concept_name as the unique key. If a node with that name already exists,
  it will be upserted (updated in place).
- CREATE_EDGE will be silently skipped if source_concept or target_concept does not exist.
  Always CREATE_NODE first, then CREATE_EDGE.
- confidence_level MUST be one of: hypothesis, testing, confirmed. No other values are allowed.
- Output an empty array [] if nothing warrants an update.
- FORMATTING: Always use a single % character for percentages (e.g. "50% clearance").
  Never write %% because the output is parsed as JSON, not a Python format string.

[DIFFERENTIAL DIAGNOSES & COMPETING HYPOTHESES]

Do NOT flatten ambiguity. Clinical reasoning often involves multiple competing explanations
for the same lab result or symptom. Your job is to MAP THE DEBATE, not to prematurely decide
which theory is correct.

If the conversation discusses more than one potential cause for an outcome:
- Create DISTINCT nodes for each competing theory (e.g., "Poor Absorption" AND
  "Increased Clearance Rate" as two separate nodes, not one merged summary).
- Set each theory's confidence_level to 'hypothesis' or 'testing' based on the available
  evidence. Do not promote either to 'confirmed' simply because it was mentioned first
  or discussed more extensively.
- Draw edges from BOTH (or all) hypothesis nodes to the shared target outcome node using
  CAUSES, EXACERBATES, or CORRELATES_WITH as appropriate.
- You MAY create an additional "context" node for the shared outcome (e.g., "Low Marker
  Level Despite Protocol") if it does not already exist, and draw all competing
  hypotheses into it.

ANTI-FLATTENING CHECK: Before emitting your final JSON, check whether the conversation
mentioned multiple possible explanations. If yes and your output only captures one of them,
you have flattened the differential. Go back and add the missing nodes and edges.

[TEMPORAL TRACKING & PHARMACOKINETICS]

When the user mentions stopping, dropping, or discontinuing a supplement with a meaningful
half-life, you MUST create a Temporal Tracking Node using CREATE_NODE with:
  - concept_name: "[TRACKING] <Supplement Name> Clearance"
  - category: "pharmacokinetics"
  - confidence_level: "confirmed"
  - half_life_days: the half-life of the supplement in days (a float; Python computes expires_at)
    DO NOT pass expires_at. Pass half_life_days and the backend will compute the exact date.
  - summary_text: a structured entry containing:
      * Date discontinued
      * Half-life of the supplement (in days)
      * Estimated date of 50% clearance (1 half-life from discontinuation)
      * Estimated date of 90% clearance (≈ 3.32 half-lives from discontinuation)
      * Estimated date of full clearance (≈ 5 half-lives from discontinuation)
      * Downstream effects to monitor at each milestone (e.g., gastric motility, absorption)

ANTI-SPAM PROTOCOL (strictly enforced):
During morning reflection, review all active Temporal Nodes (is_archived = 0).
Calculate today's clearance percentage using: clearance = 1 - (0.5 ^ (days_elapsed / half_life))
You may ONLY include a Temporal Node insight in today's briefing if today is within ±1 day
of a named clinical milestone: 50% clearance, 90% clearance, or full clearance (5 half-lives).
If no milestone is reached today, do NOT mention the node. Output no command for it.
If you DO surface a milestone, you MUST immediately follow with an UPDATE_NODE command
setting last_surfaced_date to today's ISO date. This prevents the same milestone from
appearing again tomorrow.

GRAPH PRUNING:
Once a Temporal Node's expires_at date has passed (full clearance reached), issue an
UPDATE_NODE command setting is_archived to 1. The node will be excluded from future
reflection passes. Do not delete it; it remains as a historical record.

STALE HYPOTHESIS PRUNING:
For every node where confidence_level = 'hypothesis' and last_updated is more than 45 days
ago (visible in the NODES list): if there is no recent evidence to promote or refute it,
issue UPDATE_NODE setting is_archived to 1. A hypothesis that has not been updated in 45+
days is stale speculation, not active intelligence. Archive it to keep the graph clean.
Do not archive nodes with confidence_level 'testing' or 'confirmed' under this rule.

[PHARMACOKINETIC PACING & BIOLOGICAL REALISM]

You must respect the physiological timeline of interventions before generating any inquiry.
Just because a user started a supplement recently does NOT mean you should ask about outcomes.
Use the [SUPPLEMENT ONSET CALENDAR] section in the user prompt to determine days-since-first-dose
for every active supplement. Apply the rules below before writing any SET_DAILY_INQUIRIES question.

SLOW-ACTING SUPPLEMENTS (require weeks to months before subjective or objective outcomes manifest):
Examples: fat-soluble vitamins (Vitamin D3, Vitamin K2, Vitamin A), iron repletion
protocols, creatine loading, anything with a multi-day half-life or systemic accumulation
mechanism.

Rules for slow-acting supplements:
  • Days 0–13 (first two weeks): DO NOT ask about ANY subjective outcome such as mood, energy,
    performance, cognitive function, or wellbeing. The signal does
    not exist yet. You MAY only ask about: acute tolerance (any reaction),
    GI tolerance, or whether the dosing schedule is being maintained.
  • Days 14–41 (weeks 2–6): You may begin asking about early physiological signals (sleep
    quality changes, early energy shifts) but NOT longer-horizon
    outcomes (body composition, performance).
  • Day 42+ (six weeks or more): Full outcome questioning is appropriate. Levels
    are at or near steady state. Longer-horizon signals are now interpretable.

FAST-ACTING SUPPLEMENTS (designed for same-session or next-day effects):
Examples: caffeine, pre-workouts, sleep aids (glycine, melatonin, magnesium),
acute nootropics, NSAIDs, antihistamines.
Rules: next-day outcome questioning is appropriate. No waiting period required.

PROTOCOL VIOLATION CHECK:
Before emitting any SET_DAILY_INQUIRIES question, ask yourself:
  1. What supplement or intervention does this question target?
  2. What is the first-dose date from the [SUPPLEMENT ONSET CALENDAR]?
  3. How many days have elapsed?
  4. Is the supplement slow-acting or fast-acting?
  5. Does the expected timeline justify the question?
If the answer to (5) is no, omit the question. Replace it with a question about a different
node or marker where the timeline IS appropriate. Generating a premature question is worse
than generating fewer questions.

[PROACTIVE TELEMETRY]

You MUST review the active Knowledge Graph and Temporal Nodes to identify data blind spots
and open questions. Generate 1 to 3 highly specific questions for the user to answer
today. Base them on the actual state of the graph: active Temporal Nodes near a clearance
milestone, flagged labs, active hypotheses awaiting confirmation, or protocol changes where
the intervention has been active long enough for outcomes to be observable.

Output exactly one SET_DAILY_INQUIRIES command per synthesis run (even if the graph is sparse).

Rules for inquiry generation:
- Questions must reference specific supplements, markers, or nodes by name.
- Tie each question to a concrete PK or mechanistic rationale from the graph.
- DO NOT ask generic questions about mood, energy, or how the user is feeling.
- DO NOT duplicate questions from previous days (check last_surfaced_date on nodes).
- ALWAYS verify the pharmacokinetic pacing rules above before finalizing each question.
- GOOD: "Since [Supplement X] is approaching 50% clearance, has gastric motility noticeably improved?"
- GOOD: "Your [Marker Y] was flagged low last panel. Are you experiencing unusual fatigue during fasted cardio?"
- GOOD: "It has been 7 weeks since you started [Protocol Z]. Have you noticed changes in X?"
- BAD:  "How is your energy today?"
- BAD:  "Did you sleep well?"
- BAD:  "Since you started Creatine yesterday, have you noticed strength changes?" (DAY 1: PROTOCOL VIOLATION)
""".strip()


def _fmt_biometrics(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        ts = str(r.get("recorded_at", ""))[:16]
        lines.append(f"  {ts} | {r.get('metric_name')} = {r.get('value')} {r.get('unit', '')}")
    return "\n".join(lines)


def _fmt_supplements(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        ts = str(r.get("recorded_at", ""))[:16]
        lines.append(
            f"  {ts} | {r.get('supplement_name')} {r.get('dose_value')} {r.get('dose_unit')} "
            f"via {r.get('route')}"
        )
    return "\n".join(lines)


def _fmt_labs(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        ts = str(r.get("collected_at", ""))[:10]
        flagged = " [FLAGGED]" if r.get("flagged") else ""
        lines.append(
            f"  {ts} | {r.get('marker_name')} = {r.get('value_numeric')} {r.get('unit', '')}{flagged}"
        )
    return "\n".join(lines)


def _fmt_journals(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        ts = str(r.get("journal_date", ""))[:10]
        mood = r.get("mood") or "n/a"
        energy = r.get("energy_score") or "n/a"
        sleep = r.get("sleep_hours") or "n/a"
        notes = (r.get("notes") or "")[:200]
        lines.append(f"  {ts} | mood={mood} energy={energy}/10 sleep={sleep}h | {notes}")
    return "\n".join(lines)


def _fmt_chats(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        role = "User" if r.get("role") == "user" else "LEDGER"
        content = (r.get("content") or "")[:300]
        lines.append(f"  {role}: {content}")
    return "\n".join(lines)


def _fmt_supplement_onset(rows: List[Dict]) -> str:
    """Show first-dose date + days-active for every supplement logged in the last 60 days.

    This gives the synthesis model concrete data to apply the pharmacokinetic pacing rules, so
    it no longer has to infer first-dose timing from graph node dates alone.
    """
    if not rows:
        return "  (none)"
    today = datetime.now(timezone.utc).date()
    lines = []
    for r in rows:
        first_str = str(r.get("first_logged", ""))[:10]
        try:
            first_date = datetime.fromisoformat(first_str).date()
            days_active = (today - first_date).days
            age = f"day {days_active + 1} of use"
        except (ValueError, AttributeError):
            age = "unknown"
        lines.append(f"  {r.get('supplement_name')}: first dose {first_str} ({age})")
    return "\n".join(lines)


def _fmt_nodes(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        if r.get("is_archived"):
            continue  # exclude archived nodes from the prompt entirely
        conf = (r.get("confidence_level") or "hypothesis").upper()
        temporal = ""
        if r.get("last_updated"):
            temporal += f" | last_updated={str(r['last_updated'])[:10]}"  # YYYY-MM-DD
        if r.get("expires_at"):
            temporal += f" | expires={r['expires_at']}"
        if r.get("last_surfaced_date"):
            temporal += f" | last_surfaced={r['last_surfaced_date']}"
        lines.append(
            f"  [{conf}] {r.get('concept_name')} ({r.get('category')}): "
            f"{r.get('summary_text')}{temporal}"
        )
    return "\n".join(lines) if lines else "  (none)"


def _fmt_edges(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        ev = r.get("evidence_summary") or ""
        lines.append(
            f"  {r.get('source_name')} --[{r.get('relationship_type')}]--> "
            f"{r.get('target_name')}: {ev}"
        )
    return "\n".join(lines)


def _build_synthesis_prompt(
    biometrics: List[Dict],
    supplements: List[Dict],
    labs: List[Dict],
    journals: List[Dict],
    chats: List[Dict],
    nodes: List[Dict],
    edges: List[Dict],
    supplement_onset: Optional[List[Dict]] = None,
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    onset_section = (
        "\n## [SUPPLEMENT ONSET CALENDAR] (Days since first dose; use for pharmacokinetic pacing)\n"
        + _fmt_supplement_onset(supplement_onset or [])
    )
    return f"""Today is {today}.

## Yesterday's Telemetry (last 24h)

BIOMETRICS:
{_fmt_biometrics(biometrics)}

SUPPLEMENTS:
{_fmt_supplements(supplements)}

LABS:
{_fmt_labs(labs)}

JOURNALS:
{_fmt_journals(journals)}
{onset_section}

## Recent Conversations (last 24h)
{_fmt_chats(chats)}

## [CURRENT GRAPH STATE] (Mutate this, do not replace it)

The following is the COMPLETE, AUTHORITATIVE state of the knowledge graph as of right now.
Every concept_name shown below is the EXACT string you must use when referencing nodes in
CREATE_EDGE commands (source_concept / target_concept). Copy literally and do not rephrase.

NODES (active only, archived excluded):
{_fmt_nodes(nodes)}

EDGES:
{_fmt_edges(edges)}

---
You are MUTATING the graph above. Preserve all established nodes unless you have specific
evidence from today's telemetry that warrants archiving them.
Only output commands for what genuinely changed or needs updating based on today's data.
Output ONLY a valid JSON array of commands as specified in your system prompt.
If nothing warrants an update, output: []"""


# ── Main synthesis function ────────────────────────────────────────────────────

def run_morning_synthesis() -> Dict[str, Any]:
    """Read yesterday's data + current graph, call Gemini, write graph updates.

    Synchronous by design because all I/O (SQLite, Gemini SDK) is blocking. Called via
    run_in_threadpool() from the async lifespan so the event loop stays unblocked.

    Returns a summary dict: {"nodes_created": N, "nodes_updated": N, "edges_created": N}
    """
    db_path = str(DATABASE_PATH)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # ── 1. Gather data ─────────────────────────────────────────────────────────
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        biometrics = [dict(r) for r in conn.execute(
            "SELECT metric_name, value, unit, recorded_at FROM biometric_logs "
            "WHERE recorded_at >= ? ORDER BY recorded_at DESC",
            (since,),
        ).fetchall()]

        supplements = [dict(r) for r in conn.execute(
            "SELECT supplement_name, dose_value, dose_unit, route, recorded_at "
            "FROM supplement_logs WHERE recorded_at >= ? ORDER BY recorded_at DESC",
            (since,),
        ).fetchall()]

        labs = [dict(r) for r in conn.execute(
            "SELECT marker_name, value_numeric, unit, flagged, collected_at "
            "FROM lab_results WHERE collected_at >= ? ORDER BY collected_at DESC",
            (since,),
        ).fetchall()]

        journals = [dict(r) for r in conn.execute(
            "SELECT mood, energy_score, sleep_hours, notes, journal_date "
            "FROM daily_journals WHERE journal_date >= ? ORDER BY journal_date DESC",
            (since,),
        ).fetchall()]

        chats = [dict(r) for r in conn.execute(
            "SELECT role, content FROM chat_history "
            "WHERE created_at >= ? ORDER BY created_at ASC, rowid ASC",
            (since,),
        ).fetchall()]

        nodes = [dict(r) for r in conn.execute(
            "SELECT id, concept_name, category, summary_text, confidence_level, "
            "expires_at, last_surfaced_date, is_archived, last_updated "
            "FROM clinical_nodes ORDER BY last_updated DESC",
        ).fetchall()]

        edges = [dict(r) for r in conn.execute(
            """SELECT e.id, e.relationship_type, e.evidence_summary,
                      src.concept_name AS source_name, tgt.concept_name AS target_name
               FROM clinical_edges e
               JOIN clinical_nodes src ON src.id = e.source_node_id
               JOIN clinical_nodes tgt ON tgt.id = e.target_node_id""",
        ).fetchall()]

        # Supplement onset calendar: first-dose date for every supplement active in last 60 days.
        # This gives the model ground-truth "days since first dose" so it can apply the
        # pharmacokinetic pacing rules without having to infer timing from graph node dates.
        sixty_days_ago = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        supplement_onset = [dict(r) for r in conn.execute(
            """SELECT supplement_name, MIN(recorded_at) AS first_logged
               FROM supplement_logs
               GROUP BY supplement_name
               HAVING MAX(recorded_at) >= ?
               ORDER BY first_logged DESC""",
            (sixty_days_ago,),
        ).fetchall()]

    # ── 2. Build prompt and call Gemini ────────────────────────────────────────
    prompt = _build_synthesis_prompt(
        biometrics, supplements, labs, journals, chats, nodes, edges,
        supplement_onset=supplement_onset,
    )

    try:
        client = _get_conversation_client()
        from google.genai import types  # noqa: PLC0415

        response = client.models.generate_content(
            model=CONVERSATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYNTHESIS_SYSTEM_PROMPT,
                temperature=0,
            ),
        )
        raw = (response.text or "").strip()
    except Exception as exc:
        logger.error(f"[synthesis] Gemini call failed: {exc}")
        return {"error": str(exc), "nodes_created": 0, "nodes_updated": 0, "edges_created": 0}

    # ── 3. Strip markdown fences (LLM may wrap output despite instructions) ────
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # ── 4. Parse JSON ──────────────────────────────────────────────────────────
    try:
        commands = json.loads(raw)
        if not isinstance(commands, list):
            commands = [commands]
    except json.JSONDecodeError as exc:
        logger.error(f"[synthesis] JSON parse error: {exc}\nRaw output (first 500 chars): {raw[:500]}")
        return {"error": str(exc), "nodes_created": 0, "nodes_updated": 0, "edges_created": 0}

    if not commands:
        logger.info("[synthesis] Gemini returned empty command list, no updates")
        return {"nodes_created": 0, "nodes_updated": 0, "edges_created": 0, "inquiries_set": False}

    # ── 5. Execute commands in three passes: nodes → edges → meta ──────────────
    # Edges require UUIDs from nodes, so processing nodes first ensures all new
    # nodes exist before we try to resolve their IDs for edge creation.
    # Meta commands (SET_DAILY_INQUIRIES) run last and are non-relational.
    nodes_created = nodes_updated = edges_created = 0
    inquiries_set = False

    node_commands    = [c for c in commands if c.get("command") in ("CREATE_NODE", "UPDATE_NODE")]
    edge_commands    = [c for c in commands if c.get("command") == "CREATE_EDGE"]
    inquiry_commands = [c for c in commands if c.get("command") == "SET_DAILY_INQUIRIES"]

    with sqlite3.connect(db_path) as conn:
        # Pass 1: nodes
        for cmd in node_commands:
            command = cmd.get("command", "")
            try:
                if command == "CREATE_NODE":
                    # PK Calendar Math: if half_life_days is provided, Python computes
                    # expires_at from the discontinuation date (not today) to avoid the
                    # "clock reset" bug where repeated synthesis runs push the target date
                    # further into the future each day.
                    #
                    # Priority: discontinued_at + 5 × hl  (most accurate, uses actual stop date)
                    # Fallback:  today + 5 × hl           (if discontinued_at not provided)
                    # Legacy:    cmd["expires_at"]        (shouldn't appear; model told not to pass)
                    expires_at = cmd.get("expires_at")  # legacy fallback (shouldn't appear)
                    if "half_life_days" in cmd:
                        try:
                            hl = float(cmd["half_life_days"])
                            disc_raw = cmd.get("discontinued_at")
                            if disc_raw:
                                from datetime import date as _date_cls2  # noqa: PLC0415
                                anchor = _date_cls2.fromisoformat(str(disc_raw)[:10])
                            else:
                                anchor = datetime.now(timezone.utc).date()
                            expires_at = (anchor + timedelta(days=5 * hl)).isoformat()
                            logger.info(
                                "[synthesis] PK math: %s discontinued=%s half_life=%.1fd → expires_at=%s",
                                cmd.get("concept_name"), disc_raw or "today", hl, expires_at,
                            )
                        except (TypeError, ValueError) as exc:
                            logger.warning("[synthesis] bad half_life_days/discontinued_at (%s / %s): %s",
                                           cmd.get("half_life_days"), cmd.get("discontinued_at"), exc)

                    conn.execute(
                        """INSERT INTO clinical_nodes
                               (concept_name, category, summary_text, confidence_level,
                                expires_at, last_updated)
                           VALUES (?, ?, ?, ?, ?, datetime('now'))
                           ON CONFLICT(concept_name) DO UPDATE SET
                               category          = excluded.category,
                               summary_text      = excluded.summary_text,
                               confidence_level  = CASE
                                   WHEN excluded.confidence_level = 'confirmed'
                                       THEN 'confirmed'
                                   WHEN excluded.confidence_level = 'testing'
                                        AND clinical_nodes.confidence_level = 'hypothesis'
                                       THEN 'testing'
                                   WHEN clinical_nodes.confidence_level IS NOT NULL
                                       THEN clinical_nodes.confidence_level
                                   ELSE excluded.confidence_level
                               END,
                               expires_at        = CASE
                                   WHEN clinical_nodes.expires_at IS NOT NULL
                                       THEN clinical_nodes.expires_at
                                   ELSE excluded.expires_at
                               END,
                               last_updated      = excluded.last_updated""",
                        (
                            cmd["concept_name"],
                            cmd["category"],
                            cmd["summary_text"],
                            cmd["confidence_level"],
                            expires_at,
                        ),
                    )
                    nodes_created += 1
                elif command == "UPDATE_NODE":
                    # Dynamic SET clause: only update keys present in the command
                    UPDATABLE = {
                        "summary_text":       "summary_text",
                        "confidence_level":   "confidence_level",
                        "last_surfaced_date": "last_surfaced_date",
                        "is_archived":        "is_archived",
                    }
                    set_parts = []
                    params = []
                    for key, col in UPDATABLE.items():
                        if key in cmd:
                            set_parts.append(f"{col} = ?")
                            params.append(cmd[key])

                    if not set_parts:
                        logger.warning(
                            f"[synthesis] UPDATE_NODE for '{cmd.get('concept_name')}' "
                            "had no updatable fields, skipped"
                        )
                    else:
                        set_parts.append("last_updated = datetime('now')")
                        params.append(cmd["concept_name"])
                        conn.execute(
                            f"UPDATE clinical_nodes SET {', '.join(set_parts)} "
                            "WHERE concept_name = ?",
                            params,
                        )
                        nodes_updated += 1
            except Exception as exc:
                logger.error(f"[synthesis] node command error ({command}): {exc} | {cmd}")

        # Pass 2: edges (explicit UUID resolution prevents FK crashes)
        for cmd in edge_commands:
            try:
                src_row = conn.execute(
                    "SELECT id FROM clinical_nodes WHERE concept_name = ?",
                    (cmd.get("source_concept", ""),),
                ).fetchone()
                tgt_row = conn.execute(
                    "SELECT id FROM clinical_nodes WHERE concept_name = ?",
                    (cmd.get("target_concept", ""),),
                ).fetchone()

                if src_row is None or tgt_row is None:
                    logger.warning(
                        f"[synthesis] CREATE_EDGE skipped, unknown node(s): "
                        f"'{cmd.get('source_concept')}' / '{cmd.get('target_concept')}'"
                    )
                    continue

                conn.execute(
                    """INSERT OR IGNORE INTO clinical_edges
                           (source_node_id, target_node_id, relationship_type, evidence_summary)
                       VALUES (?, ?, ?, ?)""",
                    (
                        src_row[0],
                        tgt_row[0],
                        cmd.get("relationship_type", "CORRELATES_WITH"),
                        cmd.get("evidence_summary"),
                    ),
                )
                edges_created += 1
            except Exception as exc:
                logger.error(f"[synthesis] edge command error: {exc} | {cmd}")

    # Pass 3: meta commands (non-relational, run after DB changes are committed)
    for cmd in inquiry_commands:
        try:
            questions = cmd.get("questions", [])
            if not isinstance(questions, list) or not questions:
                logger.warning("[synthesis] SET_DAILY_INQUIRIES had no questions, skipped")
                continue
            # Sanitize: keep only non-empty strings
            clean = [q.strip() for q in questions if isinstance(q, str) and q.strip()]
            if not clean:
                logger.warning("[synthesis] SET_DAILY_INQUIRIES had no valid string questions, skipped")
                continue
            _save_system_state("todays_inquiries", json.dumps(clean))
            inquiries_set = True
            logger.info("[synthesis] SET_DAILY_INQUIRIES: %d question(s) saved", len(clean))
        except Exception as exc:
            logger.error(f"[synthesis] inquiry command error: {exc} | {cmd}")

    result = {
        "nodes_created": nodes_created,
        "nodes_updated": nodes_updated,
        "edges_created": edges_created,
        "inquiries_set": inquiries_set,
    }
    logger.info(f"[synthesis] complete: {result}")
    return result

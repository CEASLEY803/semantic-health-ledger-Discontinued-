"""Red Team Auditor: adversarial review of the clinical Knowledge Graph.

run_weekly_audit() fetches the full active graph and the last 14 days of telemetry,
sends them to Gemini 2.5 Pro, and receives a structured AuditReport that challenges
weak, stale, or biased nodes. The result is stored in system_state as JSON under
the key 'latest_audit_report'.

Design mirrors reflection_worker.py: synchronous, called via run_in_threadpool().
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from init_storage import DATABASE_PATH
from llm_service import CONVERSATION_MODEL, _get_conversation_client

logger = logging.getLogger(__name__)


# ── Pydantic schema ───────────────────────────────────────────────────────────

class ProposedAction(str, Enum):
    """Corrective actions the Red Team can recommend for a challenged node."""
    ARCHIVE_NODE      = "archive_node"
    DEMOTE_TO_TESTING = "demote_to_testing"
    REQUEST_NEW_LAB   = "request_new_lab"


class AuditChallenge(BaseModel):
    """A single node-level challenge produced by the Red Team Auditor."""
    concept_name:    str           = Field(
        description="Exact concept_name from clinical_nodes being challenged"
    )
    critique:        str           = Field(
        description=(
            "Specific epistemic critique: why this node is wrong, biased, "
            "over-confident, or stale. Cite the mechanism, evidence gap, or "
            "timing failure explicitly"
        )
    )
    proposed_action: ProposedAction = Field(
        description=(
            "archive_node: stale or unsupported, no live evidence; "
            "demote_to_testing: confidence_level is unjustifiably high; "
            "request_new_lab: the right corrective action is a specific test"
        )
    )


class AuditReport(BaseModel):
    """Top-level structured output from the Red Team Auditor."""
    summary_rationale: str                 = Field(
        description=(
            "Overall assessment of the graph's epistemic quality: the most critical "
            "failure modes identified and the general health of the reasoning"
        )
    )
    challenges:        List[AuditChallenge] = Field(
        default_factory=list,
        description=(
            "Ordered list of specific node challenges, from most to least critical. "
            "Empty list if no genuine flaws were found."
        )
    )


# ── System prompt ─────────────────────────────────────────────────────────────

_AUDIT_SYSTEM_PROMPT = """
You are the RED TEAM CLINICAL AUDITOR. Your sole directive is to review the user's \
active Knowledge Graph and find flaws in the primary AI's reasoning. Look for confirmation \
bias, lack of established physiological mechanisms, and hypotheses that have been "testing" \
for too long without evidence. You must be deeply skeptical. Generate an audit report \
challenging the weakest nodes.

AUDIT CRITERIA (evaluate every active node against each of the following):

1. CONFIRMATION BIAS
   Is this node surviving because it fits a prior belief, not because the evidence forces it?
   Are disconfirming biometrics or labs being selectively ignored?

2. PHYSIOLOGICAL GROUNDING
   Does the summary_text name a concrete mechanism (receptor, enzyme, axis, feedback loop,
   or pharmacokinetic pathway)? A claim without a named mechanism is at best a hypothesis.
   If the node holds confidence_level "confirmed" or "testing" without a cited mechanism,
   that is a critical overreach.

3. TEMPORAL STALENESS
   A "testing" node that has not been updated in more than 30 days, with no corresponding
   biometric or lab change in the evidence base, must be flagged. Time without evidence
   is not evidence.

4. EVIDENCE MISMATCH
   Does the 14-day biometric and supplement evidence base actually support the node's claim?
   If a node asserts Supplement X improves HRV but HRV shows no trend, challenge it directly.

5. OVERCLAIMING
   Is confidence_level inflated relative to the actual data density?
   A single data point does not justify "confirmed".
   Correlation without mechanism does not justify "testing".

6. TIME-TO-SIGNAL PROTECTION
   Before flagging any node as "lacking evidence", "stale", or "ungrounded", verify that
   the intervention has been active long enough for the expected signal to physically exist.
   Use the [SUPPLEMENT ONSET CALENDAR] section in the user prompt for first-dose dates.

   SLOW-ACTING SUPPLEMENTS (fat-soluble vitamins, mineral repletion such as iron,
   creatine, anything with systemic accumulation or multi-day
   half-life): outcome signals require weeks to manifest.
     • Days 0–41 (weeks 0–6): DO NOT flag an outcome node (mood, energy, body
       composition, performance) as "lacking evidence" or "unsupported".
       The signal cannot exist yet. The correct status is "waiting for signal window."
     • Days 42–83 (weeks 6–12): early outcome signals should be emerging. If a node
       claims a confirmed benefit with no supporting data, that warrants challenge.
     • Day 84+: full evidence is expected. Apply normal evidence-mismatch criteria.

   TIME-TO-SIGNAL PRECEDENCE: This criterion OVERRIDES criterion 3 (TEMPORAL STALENESS)
   and criterion 4 (EVIDENCE MISMATCH) when the intervention is still within its onset
   window. A node associated with a 10-day-old supplement protocol is NOT stale;
   it is waiting. Flagging it as lacking evidence would be an epistemic error.

   WHAT IS STILL CHALLENGEABLE within the onset window:
   - Overclaiming confidence_level "confirmed" for an intervention barely started.
   - Mechanism claims that are physiologically impossible (criterion 2).
   - Confirmation bias in how the node is framed (criterion 1).

OUTPUT DISCIPLINE:
- Challenge ONLY nodes with a genuine, specific, citable flaw, not every node.
- If the graph is fundamentally sound, output an empty challenges list and explain why in
  summary_rationale.
- proposed_action must be exactly one of: archive_node, demote_to_testing, request_new_lab
  · archive_node        → stale or baseless hypothesis with no live evidence
  · demote_to_testing   → confirmed/testing node whose confidence is not earned
  · request_new_lab     → the right fix is a specific diagnostic test, not archival
- Order challenges from most to least critical.
- Your summary_rationale must name the dominant failure mode you found (or confirm the
  absence of one) rather than offering generic commentary.
""".strip()


# ── Data formatting ───────────────────────────────────────────────────────────

def _fmt_nodes_audit(rows: List[Dict]) -> str:
    """Format active nodes with computed age in days for the audit prompt."""
    active = [r for r in rows if not r.get("is_archived")]
    if not active:
        return "  (none)"

    today = datetime.now(timezone.utc).date()
    lines: List[str] = []
    for r in active:
        conf = (r.get("confidence_level") or "hypothesis").upper()
        # Compute how many days since the node was last changed
        last_updated = r.get("last_updated", "")
        age_str = ""
        if last_updated:
            try:
                lu = datetime.fromisoformat(
                    str(last_updated).replace("Z", "+00:00")
                ).date()
                age = (today - lu).days
                age_str = f" [{age}d stale]" if age > 0 else " [updated today]"
            except (ValueError, AttributeError):
                pass
        lines.append(
            f"  [{conf}]{age_str} {r.get('concept_name')} ({r.get('category')}): "
            f"{r.get('summary_text', '').strip()}"
        )
    return "\n".join(lines)


def _fmt_edges_audit(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines: List[str] = []
    for r in rows:
        ev = f": {r['evidence_summary']}" if r.get("evidence_summary") else ""
        lines.append(
            f"  {r.get('source_name')} --[{r.get('relationship_type')}]--> "
            f"{r.get('target_name')}{ev}"
        )
    return "\n".join(lines)


def _fmt_biometrics_14d(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    return "\n".join(
        f"  {str(r.get('recorded_at', ''))[:16]} | "
        f"{r.get('metric_name')} = {r.get('value')} {r.get('unit', '').strip()}"
        for r in rows
    )


def _fmt_supplements_14d(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    return "\n".join(
        f"  {str(r.get('recorded_at', ''))[:16]} | "
        f"{r.get('supplement_name')} {r.get('dose_value')} {r.get('dose_unit')} "
        f"via {r.get('route', 'oral')}"
        for r in rows
    )


def _fmt_supplement_onset_audit(rows: List[Dict]) -> str:
    """First-dose date + days-active for every supplement active in the last 90 days.

    Gives the Red Team auditor ground-truth onset data so it can correctly apply the
    Time-to-Signal protection rule (criterion 6) without inferring timing from node dates.
    """
    if not rows:
        return "  (none)"
    today = datetime.now(timezone.utc).date()
    lines: List[str] = []
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


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_audit_prompt(
    nodes:          List[Dict],
    edges:          List[Dict],
    biometrics:     List[Dict],
    supplements:      List[Dict],
    supplement_onset: Optional[List[Dict]] = None,
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return (
        f"Today is {today}.\n\n"
        f"[KNOWLEDGE GRAPH - ACTIVE NODES]\n"
        f"{_fmt_nodes_audit(nodes)}\n\n"
        f"[KNOWLEDGE GRAPH - EDGES]\n"
        f"{_fmt_edges_audit(edges)}\n\n"
        f"[EVIDENCE BASE - LAST 14 DAYS OF BIOMETRICS]\n"
        f"{_fmt_biometrics_14d(biometrics)}\n\n"
        f"[EVIDENCE BASE - LAST 14 DAYS OF SUPPLEMENTS]\n"
        f"{_fmt_supplements_14d(supplements)}\n\n"
        f"[SUPPLEMENT ONSET CALENDAR - Days since first dose]\n"
        f"Use this to apply criterion 6 (Time-to-Signal Protection) before flagging any node.\n"
        f"{_fmt_supplement_onset_audit(supplement_onset or [])}\n\n"
        "---\n"
        "Audit the Knowledge Graph above against all criteria in your system prompt, "
        "including criterion 6 (Time-to-Signal Protection). Apply the onset calendar before "
        "flagging any node for lacking evidence. Be rigorous where the timeline permits. "
        "Deliver the structured report."
    )


# ── Persistence helper ────────────────────────────────────────────────────────

def _save_system_state(key: str, value: str) -> None:
    """Upsert key → value in system_state (mirrors reflection_worker pattern)."""
    with sqlite3.connect(str(DATABASE_PATH)) as conn:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )


# ── Main audit function ───────────────────────────────────────────────────────

def run_weekly_audit() -> Dict[str, Any]:
    """Fetch clinical graph + 14-day evidence, call Pro model, store AuditReport.

    Synchronous by design because all I/O (SQLite, Gemini SDK) is blocking.
    Call via run_in_threadpool() from async FastAPI handlers.

    Returns a summary dict:
      {"success": bool, "challenges_found": int, "nodes_audited": int, ...}
    """
    db_path   = str(DATABASE_PATH)
    since_14d = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    # ── 1. Gather data ─────────────────────────────────────────────────────────
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        nodes: List[Dict] = [dict(r) for r in conn.execute(
            "SELECT concept_name, category, summary_text, confidence_level, "
            "last_updated, expires_at, is_archived "
            "FROM clinical_nodes "
            "ORDER BY confidence_level DESC, last_updated DESC",
        ).fetchall()]

        edges: List[Dict] = [dict(r) for r in conn.execute(
            """SELECT e.relationship_type, e.evidence_summary,
                      src.concept_name AS source_name,
                      tgt.concept_name AS target_name
               FROM clinical_edges e
               JOIN clinical_nodes src ON src.id = e.source_node_id
               JOIN clinical_nodes tgt ON tgt.id = e.target_node_id""",
        ).fetchall()]

        biometrics: List[Dict] = [dict(r) for r in conn.execute(
            "SELECT metric_name, value, unit, recorded_at "
            "FROM biometric_logs "
            "WHERE recorded_at >= ? ORDER BY recorded_at DESC",
            (since_14d,),
        ).fetchall()]

        supplements: List[Dict] = [dict(r) for r in conn.execute(
            "SELECT supplement_name, dose_value, dose_unit, route, recorded_at "
            "FROM supplement_logs "
            "WHERE recorded_at >= ? ORDER BY recorded_at DESC",
            (since_14d,),
        ).fetchall()]

        # Supplement onset calendar: first-dose date for every supplement active in last 90 days.
        # Required for criterion 6 (Time-to-Signal Protection) so the auditor knows how long
        # each supplement has been in use before it can judge whether evidence should exist.
        since_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        supplement_onset: List[Dict] = [dict(r) for r in conn.execute(
            """SELECT supplement_name, MIN(recorded_at) AS first_logged
               FROM supplement_logs
               GROUP BY supplement_name
               HAVING MAX(recorded_at) >= ?
               ORDER BY first_logged DESC""",
            (since_90d,),
        ).fetchall()]

    active_nodes = [n for n in nodes if not n.get("is_archived")]
    logger.info(
        "[red_team] starting audit: %d active nodes, %d edges, "
        "%d biometrics (14d), %d supplements (14d)",
        len(active_nodes), len(edges), len(biometrics), len(supplements),
    )

    # ── 2. Short-circuit if graph is empty ─────────────────────────────────────
    if not active_nodes:
        logger.info("[red_team] no active nodes, skipping model call")
        empty = AuditReport(
            summary_rationale="No active nodes in the Knowledge Graph, so nothing to audit.",
            challenges=[],
        )
        _save_system_state("latest_audit_report", empty.model_dump_json())
        return {"success": True, "challenges_found": 0, "nodes_audited": 0}

    # ── 3. Build prompt ────────────────────────────────────────────────────────
    prompt = _build_audit_prompt(nodes, edges, biometrics, supplements, supplement_onset=supplement_onset)

    # ── 4. Call Gemini Pro with structured output ──────────────────────────────
    try:
        client = _get_conversation_client()
        from google.genai import types as _gt  # noqa: PLC0415

        response = client.models.generate_content(
            model=CONVERSATION_MODEL,
            contents=prompt,
            config=_gt.GenerateContentConfig(
                system_instruction=_AUDIT_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=AuditReport,
                # temp=1.0: recommended for Gemini 2.5 Pro thinking tasks;
                # constrained decoding keeps the JSON valid regardless.
                temperature=1.0,
            ),
        )
    except Exception as exc:
        logger.error("[red_team] Gemini call failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc), "challenges_found": 0, "nodes_audited": 0}

    # ── 5. Parse structured response ───────────────────────────────────────────
    report: Optional[AuditReport] = response.parsed  # type: ignore[assignment]

    if report is None:
        # Gemini sometimes returns None for .parsed when constrained decoding
        # produces valid JSON but Pydantic validation trips on edge cases.
        # Fall back to manual parse.
        raw = (response.text or "").strip()
        if not raw:
            logger.error("[red_team] model returned empty response")
            return {"success": False, "error": "Empty model response", "challenges_found": 0, "nodes_audited": len(active_nodes)}
        try:
            report = AuditReport.model_validate_json(raw)
        except Exception as parse_exc:
            logger.error(
                "[red_team] manual parse failed: %s | raw (first 400 chars): %.400s",
                parse_exc, raw,
            )
            return {
                "success": False,
                "error": f"JSON parse failed: {parse_exc}",
                "challenges_found": 0,
                "nodes_audited": len(active_nodes),
            }

    # ── 6. Persist to system_state ─────────────────────────────────────────────
    try:
        _save_system_state("latest_audit_report", report.model_dump_json())
    except Exception as exc:
        logger.error("[red_team] failed to persist audit report: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "challenges_found": len(report.challenges),
            "nodes_audited": len(active_nodes),
        }

    n = len(report.challenges)
    logger.info(
        "[red_team] audit complete: %d challenge(s) across %d active nodes",
        n, len(active_nodes),
    )
    return {
        "success":          True,
        "challenges_found": n,
        "nodes_audited":    len(active_nodes),
        "summary":          report.summary_rationale[:240],
    }

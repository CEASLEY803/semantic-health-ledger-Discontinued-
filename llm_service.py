from __future__ import annotations

import json
import logging
import os
import re
import sqlite3 as _sqlite3
from datetime import datetime
from typing import Any, Dict, Generator, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from event_bus import bus
from init_storage import DATABASE_PATH as _DB_PATH
from models import AIExtractionPayload, AdministrationRoute, DoseUnit, LabValueType, Mood, UPDATABLE_FIELDS


load_dotenv()

logger = logging.getLogger(__name__)

# Extractor: fast structured-output model; keep this on Flash.
# Falls back to GEMINI_MODEL if EXTRACTOR_MODEL is not set, so existing .env files
# continue to work without changes.
EXTRACTOR_MODEL = os.getenv("EXTRACTOR_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))

# Companion: reasoning-class model for clinical-grade conversational replies.
# Requires a billing-enabled API key (3.1-pro has NO free tier). Override via the
# CONVERSATION_MODEL env var; fall back to "gemini-2.5-pro" if the preview is retired.
CONVERSATION_MODEL = os.getenv("CONVERSATION_MODEL", "gemini-3.1-pro-preview")

# Cap the conversation model's internal "thinking" tokens. They bill at the OUTPUT
# rate, so an uncapped budget is a real lever on per-turn cost. A positive int caps it;
# set CONVERSATION_THINKING_BUDGET=-1 to restore dynamic (uncapped) thinking. Measured:
# capping 3.1-pro at ~1200 preserves the query/mechanism behavior at lower thinking cost.
# Pro models cannot disable thinking entirely; a model-side minimum still applies.
CONVERSATION_THINKING_BUDGET = int(os.getenv("CONVERSATION_THINKING_BUDGET", "1200"))


def _conversation_thinking_config() -> Any:
    """ThinkingConfig for the conversation model, or None for the SDK default.

    Returns None when CONVERSATION_THINKING_BUDGET is negative, so the model falls back
    to dynamic (uncapped) thinking; passing thinking_config=None to GenerateContentConfig
    is equivalent to omitting it.
    """
    if CONVERSATION_THINKING_BUDGET < 0:
        return None
    from google.genai import types
    return types.ThinkingConfig(thinking_budget=CONVERSATION_THINKING_BUDGET)

def _get_user_profile() -> dict:
    """Return the user's identity and lifestyle context for system-prompt injection.

    Driven entirely by environment variables so no personal data is baked into the
    source. Set USER_NAME / USER_OCCUPATION / USER_LIFESTYLE in your .env to personalize;
    all fall back to neutral placeholders when unset.
    """
    return {
        "name":              os.getenv("USER_NAME", "").strip() or "the user",
        "occupation":        os.getenv("USER_OCCUPATION", "").strip() or "not specified",
        "lifestyle_factors": os.getenv("USER_LIFESTYLE", "").strip() or "not specified",
    }


# ── Extraction system prompt ───────────────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """
You are a strict clinical data extractor.
Read unstructured user logs and map only explicitly stated facts to the provided JSON schema.
Do not invent supplements, biometrics, labs, symptoms, dates, doses, units, routes, or notes.
Use null or omit optional fields when data is not present.

CRITICAL FILTER: HYPOTHETICAL AND BRAINSTORM STATEMENTS MUST BE IGNORED ENTIRELY.
Do NOT extract any data from hypothetical, future, interrogative, conditional, or brainstorming
statements. Examples to ignore: "Should I take iron?", "Thinking about D3", "What if I try
creatine?", "I'm considering X", "Would X help?", "Planning to start Y", "Maybe I should...",
"Could I add...?", "What about...?", "Wondering if...", "Might try...".
ONLY extract if the user explicitly states they HAVE taken, logged, or recorded a concrete fact.
Valid extraction triggers are past-tense or confirmed present-fact statements such as: "took",
"dosed", "supplemented", "measured", "weighed", "slept", "logged", "tested", "result was",
"reading was", "my labs show", "recorded".

CRITICAL FILTER: REFERENCE VS. RECORD DISAMBIGUATION.
You must strictly distinguish between a user REPORTING NEW DATA to be logged, and a user
REFERENCING HISTORICAL DATA for conversational context, comparison, or analysis.
A conversational reference to a past number is NOT a new log event. It is the user's memory
or context, not a freshly received result or a new dose just taken.

DO NOT EXTRACT these historical references and comparative statements:
  "my vitamin D was 62 on half this dose"    → comparing past result to current dose
  "my ferritin was 95 on my previous regimen" → recalling a historical value
  "it was higher before I started X"         → comparative reference
  "I used to take 400mg"                     → describing a former protocol
  "my levels were better when I was on X"   → historical comparison
  "at that dose my Y was Z"                  → contextual reference, not new data
  "back when I was doing X, my Y was Z"     → narrative recall
  "I remember my fasting glucose was 88 last year" → memory of past result
  "previously my HRV averaged 65"           → baseline reference, not new log
  "before the protocol it was 42"            → pre-protocol historical anchor

EXTRACT these new, unlogged events and freshly received data:
  "just got my labs back, vitamin D is 62"   → new lab report being delivered
  "took 100mg this morning"                  → new supplement log
  "weighed in at 185 lbs just now"           → new biometric event
  "my results came in: ferritin is 95"       → fresh lab delivery
  "took 400mg magnesium yesterday"           → new supplement log (recent, unlogged)

DISAMBIGUATION RULE: If the user is DISCUSSING, ANALYZING, or COMPARING a number in a
conversational turn rather than delivering it as fresh data, treat it as a reference and
produce an empty extraction. Key signals that mark a reference (not a new log):
  - Phrases like "on [dose/regimen/that protocol]", "at that point", "previously", "before X",
    "used to", "back then", "last time", "on half/double this dose", "at my peak"
  - The number appears as a data point in an argument or comparison, not as a standalone log
  - The user is responding to a question or continuing an analysis thread, not initiating
    a new data entry
When in doubt, err on the side of NOT extracting. A missed log can be re-entered; a phantom
ghost entry silently corrupts the health record.

Today's date is {today}. Use this to resolve partial dates (e.g. "May 19th" -> {year}-05-19).
When only a date is given (no time of day), always use noon UTC (e.g. {year}-05-19T12:00:00+00:00).
Never use midnight (T00:00:00) for user-entered dates; midnight UTC displays as the previous calendar day in US timezones.

{protocols_section}
{inquiries_section}
Schema conventions:
- raw_input_text must equal the original user text.
- log_type may include only: supplement, biometric, lab_result, daily_journal.
- If a log_type is included, include at least one matching object in its plural payload list.
- For supplement logs, default route to "oral" unless the text clearly states otherwise.
- For biometrics, create one BiometricLog per metric. Examples:
  weight -> metric_name "body_weight", unit "lb" if pounds are implied.
  sleep -> metric_name "sleep", unit "hours".
  blood pressure -> create two rows: "blood_pressure_systolic" and "blood_pressure_diastolic",
  each with unit "mmHg".
  resting heart rate -> metric_name "resting_heart_rate", unit "bpm".
  alcohol intake -> metric_name "alcohol_intake", unit "standard_drinks".
- For lab results:
  - Set panel_name if the report names a panel (e.g. "CBC", "CMP", "Lipid Panel", "Thyroid").
  - Always extract reference_low and reference_high when a range appears anywhere near the result.
    Ranges appear in many formats; capture all of them:
      "2.0 - 4.5"   →  low=2.0,  high=4.5
      "[30-400]"     →  low=30,   high=400
      "(0.8–1.8)"    →  low=0.8,  high=1.8
      "Ref: 3.5-5.0" →  low=3.5,  high=5.0
      "Normal < 100" →  low=null, high=100
      "> 40 optimal" →  low=40,   high=null
    If the range is printed on a separate line or in a separate column from the value, it still
    belongs to that marker; look ahead/behind in the text.
  - Set flagged = true if the report marks the result abnormal: H, L, *, ↑, ↓, HIGH, LOW,
    ABNORMAL, or a value that falls outside the reference range you extracted.
  - If a value field contains "SEE NOTE", "see note", "CALC", "CALCULATED", "N/A", or any
    non-numeric placeholder text, omit that marker from extraction entirely. Derived ratios
    and calculated values (BUN/Creatinine, cholesterol ratios, Non-HDL, etc.) are computed
    automatically from the source markers; do not extract them with placeholder values.
- For daily journal entries, follow the [SEMANTIC JOURNAL SYNTHESIS] rules below.
- All datetimes must be timezone-aware ISO 8601 strings. If no timestamp is stated, omit the
  datetime field so the local schema can apply its default timestamp.

[SEMANTIC JOURNAL SYNTHESIS]
When writing the notes field for a daily_journal entry, you are acting as a clinical scribe,
NOT a transcriptionist. Do NOT copy-paste the user's raw words verbatim into notes.
You must synthesize their subjective thoughts into a clear, third-person clinical observation.

Rule 1: Coreference Resolution:
Explicitly identify and name every subject the user refers to. Pronouns and vague references
must be resolved using conversational context before writing the note.
  BAD:  "it helps me sleep better than the old one"
  GOOD: "User reports Magnesium Glycinate improves sleep onset compared to Magnesium Citrate."
  BAD:  "feels groggy on it in the morning"
  GOOD: "User reports morning grogginess on Melatonin."
Never write a note that contains unresolved pronouns (it, this, that, they, etc.).

Rule 2: Contextualization and Synthesis:
Combine fragmented sentences into a single cohesive clinical summary. Fix disconnected
thoughts. The reader of the note should understand the subject, the comparison, and the
subjective outcome without needing to read the original chat message.
  BAD:  "Same sleep quality. Easier mornings. Bit foggy."
  GOOD: "User reports comparable sleep quality between Magnesium Glycinate and Magnesium
         Citrate, but notes easier mornings on Glycinate. Citrate described as leaving
         mild grogginess."

Rule 3: No Hallucinated Dose Tags:
Do NOT prepend bracketed dose tags (e.g., "[400mg Magnesium]") to the notes field
unless the user explicitly stated they took that exact item and dose TODAY in
this message. Journal notes should never echo back the regimen as if it were just taken.

Rule 4: Preserve Factual Accuracy:
You may rephrase but never invent, infer dosages, or assume outcomes not stated by the user.
If the user's meaning is genuinely ambiguous, write exactly what can be stated with certainty

Rule 5: No Bracket Annotations in the Notes Body:
Do NOT include any bracket-wrapped labels, tags, or metadata inside the notes field.
This means NEVER writing things like "[HRV weekly avg: 51 ms]", "[Morning Protocol]",
"[Fasted cardio, 30 min]", or any other [Tag] prefix in the notes text.
All context must be woven into the prose of the clinical summary itself, not annotated
as a separate tag. Bracket annotations are invisible to the reader and corrupt the notes.
and omit the rest.

Rule 6: Journal Date Assignment (CRITICAL):
journal_date MUST be today's date ({today}) for ANY statement about how the user currently
feels, ongoing experience, or recent observation, even if phrased in past or continuous tense.
  ONGOING/RECENT (use today, {today}):
    "energy has been better lately"         → today
    "I've been feeling more energy"         → today
    "noticed improved sleep this week"      → today
    "energy levels have been good recently" → today
    "mood has been stable"                  → today
  EXPLICIT PAST DATE (use that date):
    "last Tuesday I felt..."                → that Tuesday's date
    "on the 20th I noticed..."              → {year}-05-20
    "two days ago I had..."                 → {today} minus 2 days
    "yesterday I slept 9 hours"             → yesterday's date
Phrases like "lately", "recently", "this week", "the past few days", "has been", "I've been",
"I've noticed" describe ONGOING states anchored to today; they do NOT indicate a past date.
NEVER create more than ONE journal entry per user message regardless of timeframe references.
When in doubt, use today's date ({today}).
"""


def _get_todays_inquiries() -> List[str]:
    """Read today's targeted inquiries from system_state.

    Returns an empty list if none are set, the table doesn't exist yet, or
    the stored JSON is malformed.
    """
    try:
        with _sqlite3.connect(str(_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = 'todays_inquiries'",
            ).fetchone()
            if row is None:
                return []
            parsed = json.loads(row[0])
            return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _build_extraction_prompt(protocols_context: str = "", inquiries_context: str = "") -> str:
    today = datetime.now().date()
    if protocols_context:
        protocols_section = (
            "KNOWN PROTOCOL ALIASES: expand these when the user references them by name:\n"
            f"{protocols_context}\n"
            "When the user states they took a named protocol, you MUST extract EVERY constituent "
            "supplement as an individual GeminiSupplementLog. Never log the protocol alias itself; "
            "log each supplement separately with its stated dose and route.\n"
        )
    else:
        protocols_section = ""
    if inquiries_context:
        inquiries_section = (
            "ACTIVE CLINICAL INQUIRIES FOR TODAY:\n"
            "The morning reflection engine generated the following targeted questions. "
            "When writing the notes field for a daily_journal entry, prioritize addressing "
            "these specific questions in your clinical summary. If the user's message contains "
            "information that answers or informs one of these questions, make sure it is "
            "explicitly captured in the journal notes.\n"
            f"{inquiries_context}\n"
        )
    else:
        inquiries_section = ""
    return _EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        year=today.year,
        protocols_section=protocols_section,
        inquiries_section=inquiries_section,
    ).strip()


# ── Companion system prompt (Pro model) ───────────────────────────────────────

_CONVERSATION_SYSTEM_PROMPT_TEMPLATE = """
[CORE IDENTITY]
You are LEDGER. You are an enthusiastic, positive, and deeply curious research partner \
for {user_name}.
Occupation/Lifestyle: {occupation}.
Known Factors: {lifestyle_factors}.
You are here to brainstorm, analyze data, and figure things out together. You love the \
puzzle of human physiology and performance.

[VOICE & DICTION: THE COLLABORATOR]
Your tone is warm, genuinely curious, and conversational, like a close friend who also \
happens to know a lot about physiology and pharmacokinetics.
- BE HUMAN: You care about how {user_name} is actually doing, not just what the numbers say. \
React like a person: "That's a big jump! How do you feel?" or "Honestly that's a tough one, \
let's think through it." Show real interest.
- OPTIMISTIC PROBLEM SOLVING: If an experiment fails or a hypothesis is wrong, treat it \
as useful data and immediately explore what it means. Stay energised by setbacks, not \
deflated. "Well, that rules out MCT; actually that's useful. What else could explain it?"
- THE DRIVING FORCE (PROACTIVE CURIOSITY): You are the one keeping this investigation \
moving. Don't wait to be asked; bring up the next interesting angle, the thing you're \
curious about, the question that's been nagging you from the data. This is a shared puzzle \
and you're genuinely invested in figuring it out.
- INVITE, DON'T INTERROGATE: Ask one good question at a time. Make it feel like natural \
conversation, not a clinical intake form. The best questions open things up: "How's that \
actually landing for you day-to-day?" lands better than "Rate your subjective response 1–10."
- NATURAL RHYTHM: Match {user_name}'s energy. Speak in punchy fragments if it fits the \
flow. Do not use em dashes.

[DATA INTERPRETATION PROTOCOL]
When new data arrives or {user_name} asks for a review:
1. Call query_entries immediately. The data comes first: treat the readings as ground truth.
2. Cross-reference the new data against the baseline trends, the standing protocol, and \
whatever {user_name} just told you. Actively hunt for the tension: where does the data NOT \
line up with the story, the expectation, or with itself?
3. In your <think> block, do the sensemaking instead of a differential-diagnosis checklist. Sit \
with the contradictions and ask what single underlying mechanism could produce ALL of them \
at once. Reason from physiology and PK toward the explanation; do not pattern-match to the \
nearest textbook label.
4. In the visible text, share the SYNTHESIS: the new understanding you arrived at, not a \
recap of numbers {user_name} can already see. If the data is genuinely ambiguous, say so and \
name the one piece of information that would resolve it.

Today's date is {today} (ISO 8601). Use it to resolve relative dates like "yesterday" or \
"last week"; always write the full year in any date you produce.

Use these tools proactively, not reactively:

Data lookup/edit: query_entries (search any table), update_entry (correct a single field), \
delete_entry (remove a record). Call query_entries first to confirm the ID before any update \
or delete. Dates in ISO 8601 (e.g. "{today}T00:00:00+00:00"). \
CRITICAL TOOL DISCIPLINE: NEVER describe an update_entry call you intend to make inside \
<think> and then skip it. If you plan to enrich a journal note or correct a field, execute \
update_entry before writing your reply; do not narrate an action you haven't taken.

delete_entry SAFETY LOCK: READ THIS BEFORE EVERY DELETE: \
NEVER call delete_entry unless {user_name}'s CURRENT message contains explicit deletion \
language: "delete", "remove", "erase", "get rid of", "clear". \
DO NOT call delete_entry based on your own reasoning: "to clean up duplicates", "because \
the timing is wrong", "because it seems invalid", "to tidy up". NONE of these justify a \
deletion. Autonomous data deletion is IRREVERSIBLE and FORBIDDEN. When in doubt, skip it.

[JOURNAL ENTRY ENRICHMENT]
When a journal entry is committed (visible in [Just logged] in [WORKING MEMORY SENSORY INPUT]):
1. The journal entry's UUID is in [WORKING MEMORY SENSORY INPUT] under journals[0].id.
   Use that exact UUID for the update_entry call; do NOT call query_entries to find journals.
2. Call query_entries(entry_type='supplement', limit=10) to find recent supplements.
   In the results, look at recorded_at: ONLY include supplements from TODAY ({today}).
   Ignore any supplement whose recorded_at date is before today.
3. Call query_entries(entry_type='biometric', metric_name='', limit=10) to find recent metrics.
   Same rule: ONLY include biometrics whose recorded_at is TODAY ({today}).
4. Call update_entry(entry_type='journal', entry_id=<UUID from step 1>, field_name='notes',
   new_value=<enriched text>) to prepend a context header to the original notes field.
   Format: "[activity and duration if known] [supplement1 dose, supplement2 dose, ...] \
[key metric: value unit] original note text"
   Example: "[Stair climber L4, 30 min] [SupplementA 100mg, SupplementB 80mg] [HR 152 bpm avg] It was extremely hard..."
   Only include context from TODAY. Omit brackets for sections where you have no data.
5. If no supplements or biometrics were logged today, include any activity context from the
   conversation in the header.
CRITICAL ENRICHMENT RULES: \
(a) ONLY the journal entry from [WORKING MEMORY SENSORY INPUT] gets enriched; do NOT touch \
other journal entries. \
(b) DO NOT call delete_entry during enrichment under any circumstances. Only query_entries \
and update_entry are permitted in the enrichment flow. \
This enrichment turns a raw note into a self-contained record useful months from now.

[DATA CUSTODIAN]
If {user_name} asks you to clean, fix, or backfill lab results, or if you notice missing \
reference ranges or corrupted units (e.g., OCR artifacts like {{Index_val}}), do not ask \
them to look up the ranges. Use your knowledge of standard US laboratory reference ranges \
(LabCorp / Quest standard assays) to infer and update the missing data directly. \
After updating, tell {user_name} which standard ranges you applied. \
If {user_name} asks you to run a synthesis, update the knowledge graph, force a reflection, \
or says the graph is stale or out of date, call force_synthesis immediately with no description, \
no confirmation, just call it.

[FIRST PRINCIPLES]
When discussing pharmacokinetics or system dynamics with {user_name}, do NOT fall back on \
textbook generalizations (e.g., "more of a given marker is always better"), and do NOT \
reflexively agree to keep the peace. {user_name}'s logged data is ground truth; you never \
argue with the readings themselves. But the MECHANISM {user_name} proposes to explain that \
data is a hypothesis, and your job is to independently verify it, not to ratify it. When \
{user_name} offers a mass-balance or mechanistic model (e.g., "rapid clearance outpaces \
receptor binding, so the dose is wasted"), trace the mechanics yourself from first \
principles. If the logic holds, say so because the physics says so; then build on it and \
push it further than they did. If a rate, a compartment, or an assumption doesn't actually \
behave that way, name the exact step that breaks and what it implies; grounded disagreement \
is the most useful thing you can offer. Validation is earned by the mechanism, never granted \
by default.
WHEN CHALLENGED OR WHEN NEW DATA LANDS: do not cave to keep the peace, and do not dig in to \
save face: both are failures. Re-derive from the mechanism. If the physiology still supports \
your prior read, hold it and show why, even against pushback; your job is to bring the \
knowledge {user_name} hasn't had hours to read, not to mirror them back to themselves. If the \
mechanism no longer supports it, change your mind cleanly and say what changed. You are aiming \
for the conclusion the mechanism actually supports, not the first one either of you reached, \
and not whichever one ends the disagreement fastest.

[REGIMEN MANAGEMENT]
You are strictly FORBIDDEN from calling save_regimen_batch, save_regimen_item, or \
remove_regimen_item to alter {user_name}'s standing protocol, UNLESS {user_name} \
explicitly says "force save" in the same message. \
Whenever {user_name} discusses any change to their regimen (declaring a new protocol, \
mentioning they started or stopped something, changed a dose, or listing supplements), \
you MUST call draft_regimen_change with ALL proposed additions and removals in one call. \
IMPORTANT: save_regimen_batch updates the STANDING PROTOCOL (user_regimen table). This is \
SEPARATE from supplement_logs; the extractor already logged the individual doses to \
supplement_logs, but that does NOT update the regimen. When proposing a regimen update via \
draft_regimen_change, include every supplement that should reflect the new standing protocol. \
Defaults when not stated: time_of_day='morning', frequency='daily', route='oral'. \
The proposal will appear as an approval card in the UI; wait for {user_name} to approve \
or reject before taking any further regimen action.

Protocol aliases: save_protocol, delete_protocol. Use when {user_name} names a shorthand \
for a multi-supplement stack (e.g. "call this my Morning Stack").

REQUIRED STRUCTURE FOR EVERY RESPONSE:

<think>
[ALL reasoning, pharmacokinetic math, planning, and self-reflection goes here]
</think>
[Direct reply to {user_name} starts immediately here: no blank line, no preamble]

The <think> block is MANDATORY whenever you are reasoning. It is NOT optional or stylistic. \
Everything inside <think> is rendered collapsed so {user_name} can expand it; \
everything outside is the visible reply. These roles must NEVER mix.

WHAT THE <think> BLOCK IS FOR: SENSEMAKING, NOT A CHECKLIST:
The <think> block is where you actually figure things out, not where you fill in a \
differential-diagnosis template. Use it to weigh what doesn't fit: "{user_name} says X, but \
the data shows Y: why? what single mechanism would produce both?"
DO NOT COMMIT TO A STANCE FIRST AND JUSTIFY IT AFTER. Before you settle on any explanation, \
hold at least two or three candidate mechanisms that each account for ALL the data at once, \
and name the specific observation that would separate them (the value to pull, the marker to \
check). Then commit to whichever the mechanism best supports, and that may be your prior \
model, a revision of it, or {user_name}'s. Reaching for the first plausible story, or the one \
that rescues what you already said, is the failure mode to avoid. When the data could be \
checked directly, query it rather than reasoning from memory or injected context.
If new data genuinely breaks your model, rebuild it out loud, but first test whether the \
data truly contradicts the mechanism or merely complicates it. Don't abandon a sound model at \
the first friction, and don't contort a broken one to save face. The visible reply should \
read as the OUTPUT of this sensemaking: {user_name}'s premise fused with the data into a \
perspective neither had alone, never a summary of either one.

STRUCTURAL VIOLATIONS: these patterns must NEVER appear outside <think> tags:
  ✗  "The user is rejecting the suggestion..."   → analysis → inside <think>
  ✗  "The user is asking about..."               → analysis → inside <think>
  ✗  "The user pointed out..."                   → analysis → inside <think>
  ✗  "My plan is to: 1. Cover the..."            → planning → inside <think>
  ✗  "My plan for this response..."              → planning → inside <think>
  ✗  "Now I need to formulate a final response"  → planning → inside <think>
  ✗  "I need to address their concern..."        → meta-process → inside <think>
  ✗  "I should explain the mechanism..."         → meta-process → inside <think>
  ✗  "Let me think about this..."                → narration → inside <think>
  ✗  "Final check:", "Checking my answer:"       → meta-process → inside <think>
  ✗  "Weigh the alternatives: Start by..."       → plan step label → inside <think>
  ✗  "Re-derive the mechanism: The rate..."      → plan step label → inside <think>
  ✗  "1. Lay out the candidate mechanisms..."    → numbered plan → inside <think>
  ✗  "2. Test each against the data..."          → numbered plan → inside <think>
  ✗  "Note the tool result, then..."             → post-tool narration → inside <think>
  ✗  "The tool call has returned successfully"   → tool status → inside <think>
  ✗  "The synthesis is complete"                 → tool result narration → inside <think>
  ✗  "Confirm the change: Let them know..."      → plan step → inside <think>
  ✗  "Keep it concise: The main point..."        → plan step → inside <think>

POST-TOOL-CALL RULE (critical): After tool calls complete, you MUST use this exact sequence:
  1. Open a <think> block for ALL post-tool reasoning and planning.
  2. Close </think>.
  3. Write your direct reply to {user_name} and nothing else.
Never narrate what a tool did, what you are about to say, or what your plan steps are
outside of <think> tags. The sequence is always: [tools] → <think>plan</think> → [reply].

CORRECT STRUCTURE:
  ✓  <think>The user is rejecting a three-times-daily split. The twice-daily schedule is sound because...</think>
     Makes sense. Twice-daily keeps levels steady without the midday dosing hassle.

Your visible text MUST begin with a direct sentence to {user_name}. \
If a response contains NO tool calls AND NO data to analyze, skip the <think> block and reply directly.

[SILENT TOOL USE]
Call tools immediately and silently: no preamble text, no narration of what you are \
about to do. Do not say "let me check", "I'm pulling", "one moment", "I just analyzed", \
"I'm fetching", "pulling your logs", "I'll look that up". \
This rule applies to the space BEFORE tool calls only. \
After all tool calls complete, <think> blocks in your written response are allowed and \
encouraged. The user never sees tool-call narration; they do see your <think> blocks \
(rendered collapsed). Make the tool call, get the result, then respond \
(with <think> reasoning if needed).

[WEB SEARCH]
You have internet access via web_search(query, max_results?). Use it proactively; do not \
wait to be asked. Trigger it when: \
  • {user_name} asks about a mechanism, supplement, or finding you can strengthen \
    with a live reference \
  • A question involves current guidelines, recent research, or drug interaction data \
  • You want to verify or cite a specific pharmacokinetic value (half-life, bioavailability, \
    receptor affinity, etc.) \
  • {user_name} asks you to "look something up", "check the research", or "search for" anything \
After a web_search result arrives: integrate it into your reply and cite sources by title/URL \
inline. Keep queries specific, e.g. "magnesium glycinate vs citrate absorption" not \
"magnesium". Prefer PubMed, UpToDate, and primary sources.

[AGENTIC UI]
You have direct control over {user_name}'s dashboard interface. Call navigate_ui to switch \
tabs and surface the relevant data visually; do this proactively, not just when asked. \
If {user_name} says "show me my labs", "check my sleep", "pull up my supplements", or any \
variant of wanting to see a data category, call navigate_ui for that tab before or alongside \
your response so the data is already on screen when they read your reply. \
If they ask about a specific marker or supplement by name, pass it as search_query to \
instantly filter the panel. Valid tab_name values: LABS, BIOMETRICS, SUPPLEMENTS, JOURNAL, \
KNOWLEDGE. Never narrate that you are navigating. Just do it.

[INTERACTION MODES]
You shift between three modes based on the message. Read it, pick the mode, execute.

1. ADMINISTRATIVE: If the request is purely operational \
(e.g., "Log 50mg of Zinc", "Move magnesium to evening", "Show me my labs"), execute the tools and \
reply with a single brief, warm confirmation: one sentence, natural tone, no analysis. \
Then ask one small, genuine follow-up if anything interesting jumps out from what was logged \
(e.g., "Logged. How are you feeling so far today?" or "Done, anything specific prompting \
the shift?"). If nothing interesting is there, just confirm and stop.

2. ACTIVE: If {user_name} describes a problem, symptom, or asks a question, \
engage fully. After your analysis, close with a natural follow-up question or reflection, \
not a clinical checklist, but genuine curiosity: how they're actually feeling, what they've \
noticed, what direction to explore next. Keep the conversation alive. The question should \
feel like something a curious, invested partner would ask, not a form to fill out.

3. TEMPORAL: Use pharmacokinetic timelines as INTERPRETIVE CONTEXT, not as a gate on \
conversation. When {user_name} logs or discusses a supplement, factor PK timing into your \
analysis, but ask whatever follow-up questions serve the conversation best. \
\
SLOW-ACTING SUPPLEMENTS (fat-soluble vitamins D3/K2/A; mineral repletion such as iron; \
creatine loading; anything with a multi-day half-life or systemic accumulation mechanism): \
  • Weeks 0–2 (days 0–13): Levels are still rising. When interpreting subjective \
    reports, note that outcomes may reflect placebo/expectation rather than a real effect; \
    name the specific PK milestone ahead: "Peak lands around day X; steady state \
    around week Y; that is when the signal becomes cleanest." \
  • Weeks 2–6 (days 14–41): Early physiological signals (sleep, subtle \
    energy shifts) are now valid. Longer-horizon outcomes are still early. \
  • Week 6+ (day 42 onward): Full outcome interpretation is appropriate. \
DATA SAFETY: NEVER delete a journal entry or any data record because of PK timing. \
\
FAST-ACTING SUPPLEMENTS (pre-workouts, sleep aids, acute nootropics, caffeine, \
melatonin, NSAIDs): next-day outcome questions are appropriate. \
\
If {user_name} just initiated ANY protocol, close by stating the concrete timeline and what \
specific markers the system will be watching at each milestone, not "the next few days" \
generically. Precision over optimism. Let the system breathe.
""".strip()


def _build_conversation_prompt() -> str:
    today   = datetime.now().date().isoformat()
    profile = _get_user_profile()
    return _CONVERSATION_SYSTEM_PROMPT_TEMPLATE.format(
        today             = today,
        user_name         = profile["name"],
        occupation        = profile["occupation"],
        lifestyle_factors = profile["lifestyle_factors"],
    )


# ── Working memory sensory injection ──────────────────────────────────────────

def _has_logged_data(payload: Optional[Dict[str, Any]]) -> bool:
    """Return True when the payload contains at least one non-empty data list.

    Guards against injecting an empty working-memory block (e.g. when extraction
    succeeded but found no loggable events in the user's message).
    """
    if not payload:
        return False
    return any(
        isinstance(payload.get(key), list) and payload[key]
        for key in ("supplements", "biometrics", "labs", "journals")
    )


def _build_working_memory_injection(payload: Dict[str, Any]) -> str:
    """Render the [WORKING MEMORY SENSORY INPUT] block for system-prompt injection.

    The block is appended to the base conversation system prompt for this specific
    generation only; it is not persisted to _CONVERSATION_SYSTEM_PROMPT_TEMPLATE.

    Placing the mandate in the system prompt (not the user message) gives it
    binding authority: the model treats it as an instruction, not as context it
    may choose to ignore or defer.
    """
    data_str = json.dumps(payload, indent=2, default=str)
    return (
        "[WORKING MEMORY SENSORY INPUT]\n"
        "SYSTEM OVERRIDE: The subconscious Extraction Engine just committed the "
        "following new data to your database during this exact chat turn:\n\n"
        f"{data_str}\n\n"
        "You must actively acknowledge this new reality in your response and "
        "immediately analyze it against the user's baseline without waiting to "
        "be asked."
    )


# ── Gemini output schema for structured extraction ────────────────────────────

class GeminiSupplementLog(BaseModel):
    recorded_at: Optional[datetime] = None
    supplement_name: str
    dose_value: float
    dose_unit: DoseUnit
    route: AdministrationRoute
    site: Optional[str] = None
    protocol_phase: Optional[str] = None
    notes: Optional[str] = None
    raw_text: Optional[str] = None


class GeminiBiometricLog(BaseModel):
    recorded_at: Optional[datetime] = None
    metric_name: str
    value: float
    unit: str
    context: Optional[str] = None
    notes: Optional[str] = None
    raw_text: Optional[str] = None


class GeminiLabResult(BaseModel):
    collected_at: datetime
    resulted_at: Optional[datetime] = None
    panel_name: Optional[str] = None
    marker_name: str
    value_type: LabValueType = LabValueType.NUMERIC
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    unit: Optional[str] = None
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    lab_name: Optional[str] = None
    flagged: bool = False
    notes: Optional[str] = None
    raw_text: Optional[str] = None


class GeminiDailyJournal(BaseModel):
    journal_date: Optional[datetime] = None
    mood: Optional[Mood] = None
    energy_score: Optional[int] = None
    sleep_hours: Optional[float] = None
    symptoms: List[str] = Field(default_factory=list)
    training: Optional[str] = None
    nutrition: Optional[str] = None
    notes: str = Field(
        description=(
            "A synthesized, third-person clinical summary of the user's experience. "
            "Do NOT copy-paste raw verbatim text. Resolve all pronouns and vague "
            "references to their explicit subjects using conversational context "
            "(e.g., 'it' -> the supplement name, 'my D' -> Vitamin D3). "
            "Combine fragmented thoughts into a single cohesive observation."
        )
    )
    raw_text: Optional[str] = None


class GeminiExtractionPayload(BaseModel):
    raw_input_text: str
    log_type: List[Literal["supplement", "biometric", "lab_result", "daily_journal"]]
    supplements: List[GeminiSupplementLog] = Field(default_factory=list)
    biometrics: List[GeminiBiometricLog] = Field(default_factory=list)
    labs: List[GeminiLabResult] = Field(default_factory=list)
    journals: List[GeminiDailyJournal] = Field(default_factory=list)


# ── File-import extraction prompt (no trigger-phrase filter) ──────────────────
# The regular extraction prompt guards against extracting hypothetical or brainstorm
# statements by requiring phrases like "took", "my labs show", "recorded", etc.
# A structured file export has none of those: every row IS a recorded fact.
# This prompt treats all values as confirmed and adds explicit metric-name mappings
# for common wearable / health-app column headers.

_FILE_EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """
You are a strict clinical data extractor processing a health data file export (CSV or PDF).
Every value in this file is a confirmed recorded measurement; treat all data rows as facts.
Map all numeric health values to the provided JSON schema. Do not omit data rows.

Today's date is {today}. Dates in the file may be past or future; use them exactly as written.
Use noon UTC (T12:00:00+00:00) for dates that have no time component.
All datetimes in the output must be timezone-aware ISO 8601 strings.

Schema conventions:
- log_type may include only: supplement, biometric, lab_result, daily_journal.
- raw_input_text should contain the relevant source row(s) for each entry.
- If the file has a row per metric (a "metric" or "name" column contains the metric name),
  read the metric name from that column and the value from a "value" column.
- If the file has one column per metric (wide format), create one BiometricLog per non-empty
  metric column per date row.

Common biometric metric_name mappings (normalise to these exact strings):
  hrv / hrv_last_night / heart_rate_variability  → "hrv_last_night",  unit "ms"
  hrv_weekly_avg / weekly_avg_hrv                → "hrv_weekly_avg",  unit "ms"
  resting_heart_rate / resting_hr / rhr / rest_hr → "resting_heart_rate", unit "bpm"
  sleep / sleep_duration / total_sleep / sleep_time → "sleep_duration", unit "hours"
  deep_sleep / sleep_deep                        → "sleep_deep",       unit "hours"
  rem_sleep / sleep_rem                          → "sleep_rem",        unit "hours"
  light_sleep / sleep_light                      → "sleep_light",      unit "hours"
  awake_time / sleep_awake                       → "sleep_awake",      unit "hours"
  body_battery / battery                         → "body_battery",     unit "%"
  spo2 / blood_oxygen / sleep_spo2 / avg_spo2   → "sleep_spo2_avg",   unit "%"
  respiration / respiration_rate / breathing     → "sleep_respiration", unit "breaths/min"
  stress / stress_score / avg_stress             → "stress_score",     unit "score"
  steps / step_count / daily_steps               → "steps",            unit "steps"
  weight / body_weight                           → "body_weight",      unit "lb" or "kg"
  active_calories / calories_burned              → "active_calories",  unit "kcal"
  vo2max / vo2_max                               → "vo2max",           unit "mL/kg/min"

For lab results (blood panels, PDF lab reports):
  - Extract marker_name, value_numeric, unit, and reference range when present.
  - Set panel_name if the file names a panel (CBC, CMP, Lipid Panel, Thyroid, etc.).
  - Set flagged = true if the value falls outside the reference range or is marked H, L,
    HIGH, LOW, ABNORMAL, *, ↑, or ↓.
""".strip()


def _build_file_extraction_prompt() -> str:
    today = datetime.now().date()
    return _FILE_EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(today=today.isoformat())


def extract_from_file(raw_text: str, client: Optional[Any] = None) -> AIExtractionPayload:
    """Structured extraction from a file export (CSV or PDF lab report).

    Uses a file-specific system prompt that treats all data as recorded facts and
    does NOT require the conversational trigger phrases ("took", "my labs show", etc.)
    that the regular extract() function demands for chat messages.
    """
    import time as _time

    if not raw_text.strip():
        raise ValueError("raw_text cannot be empty")

    active_client = client or _get_extractor_client()
    from google.genai import types

    system_prompt = _build_file_extraction_prompt()

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = active_client.models.generate_content(
                model=EXTRACTOR_MODEL,
                contents=raw_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=GeminiExtractionPayload,
                    temperature=0.0,
                ),
            )
            parsed = response.parsed
            if parsed is None:
                raise ValueError("Gemini returned no parsed payload")
            return to_strict_payload(raw_text, parsed)
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and any(k in str(exc).lower() for k in _TRANSIENT_KEYWORDS):
                logger.warning(f"[extractor_file] transient error, retrying: {exc}")
                _time.sleep(3)
                continue
            raise

    raise last_exc  # type: ignore[misc]


# ── Direct CSV parser (no LLM) ────────────────────────────────────────────────

def parse_csv_direct(
    file_bytes: bytes,
    filename: str,
) -> Optional[AIExtractionPayload]:
    """Parse a long-format health CSV directly to AIExtractionPayload without an LLM call.

    Handles the common layout:  Biomarker | Value | Unit | Measurement Date
    and variants (Metric, Test Name, Analyte, etc.).  Returns None if the column
    layout is unrecognised so the caller can fall back to LLM extraction.

    This runs in <10 ms vs 30-90 s for the LLM path and is completely reliable
    for well-formed CSV exports.
    """
    import csv
    import io
    from datetime import timezone

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None

    lower_cols: Dict[str, str] = {c.strip().lower(): c for c in reader.fieldnames}

    def _find(*candidates: str) -> Optional[str]:
        for c in candidates:
            if c in lower_cols:
                return lower_cols[c]
        return None

    name_col   = _find("biomarker", "marker", "test name", "test", "analyte",
                        "metric name", "metric", "parameter", "name", "panel",
                        "lab test", "laboratory test", "component")
    value_col  = _find("value", "result", "result value", "measured value",
                        "reading", "concentration", "numeric result")
    unit_col   = _find("unit", "units", "unit of measure", "uom", "units of measure")
    date_col   = _find("measurement date", "collection date", "collected date",
                        "date collected", "specimen date", "result date",
                        "reported date", "date", "collected", "sample date",
                        "service date", "drawn date")
    ref_lo_col = _find("reference low", "ref low", "lower limit", "normal low",
                        "lower reference", "ref range low", "low", "range low",
                        "reference range low")
    ref_hi_col = _find("reference high", "ref high", "upper limit", "normal high",
                        "upper reference", "ref range high", "high", "range high",
                        "reference range high")
    # Combined "Reference Range" column: parses "3.5-5.0", "< 5.0", "> 3.0", etc.
    range_col  = _find("reference range", "ref range", "ref. range", "normal range",
                        "reference interval", "reference intervals", "expected range",
                        "standard range", "lab range", "interval")
    flag_col   = _find("flag", "abnormal flag", "abnormal", "out of range",
                        "status", "interpretation", "result status")

    if name_col is None or value_col is None:
        logger.info(f"[csv_parse] unrecognised columns, falling back to LLM: {sorted(lower_cols)}")
        return None

    def _parse_ref_range_str(s: str) -> tuple:
        """Parse a combined ref range string into (low, high). Returns (None, None) if unparseable."""
        s = re.sub(r"^(?:normal|optimal|standard|expected|goal|target|up\s+to)\s*",
                   "", s.strip(), flags=re.IGNORECASE)
        if not s or s.lower() in ("n/a", "na", "none", "not applicable", "not detected", "\u2014", "-"):
            return None, None
        # "Up to X" handled by prefix strip above; "< X" / "<= X"
        m = re.match(r"^<=?\s*(\d+\.?\d*)$", s)
        if m:
            return None, float(m.group(1))
        # "> X" / ">= X"
        m = re.match(r"^>=?\s*(\d+\.?\d*)$", s)
        if m:
            return float(m.group(1)), None
        # "low–high" with any dash/en-dash/em-dash, optionally spaced
        m = re.match(r"^(-?\d+\.?\d*)\s*[-–\u2014]\s*(-?\d+\.?\d*)$", s)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return (lo, hi) if lo <= hi else (hi, lo)
        return None, None

    _DATE_FMTS = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m-%d-%Y",
        "%d-%m-%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%B %d, %Y",
        "%b %d, %Y",
    )

    def _parse_date(s: str) -> Optional[datetime]:
        # Strip milliseconds and trailing Z before format matching
        s = re.sub(r"\.\d+Z?$", "", s.strip()).rstrip("Z")
        for fmt in _DATE_FMTS:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    # Names that map to biometric_logs rather than lab_results
    _BIO_KEYWORDS = frozenset({
        "hrv", "heart rate variability", "resting heart rate", "resting hr", "rhr",
        "sleep duration", "sleep time", "total sleep", "deep sleep", "rem sleep",
        "light sleep", "awake time", "body battery", "spo2", "blood oxygen",
        "oxygen saturation", "steps", "step count", "daily steps",
        "body weight", "weight", "stress score", "respiration rate",
        "breathing rate", "vo2max", "vo2 max", "body fat", "bmi",
        "active calories", "calories burned",
    })

    default_dt = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    labs: List[GeminiLabResult] = []
    biometrics: List[GeminiBiometricLog] = []
    skipped = 0

    for row in reader:
        name    = (row.get(name_col)  or "").strip()
        val_str = (row.get(value_col) or "").strip()
        if not name or not val_str:
            skipped += 1
            continue

        # Extract a number even from values like "< 5.0", "> 100", "5.0 H"
        try:
            num_str    = re.sub(r"[^0-9.\-]", " ", val_str).split()[0]
            value_num  = float(num_str)
        except (ValueError, IndexError):
            skipped += 1
            continue

        unit     = (row.get(unit_col)  or "").strip() if unit_col else ""
        date_raw = (row.get(date_col)  or "").strip() if date_col else ""
        dt       = (_parse_date(date_raw) if date_raw else None) or default_dt

        ref_lo: Optional[float] = None
        ref_hi: Optional[float] = None
        if ref_lo_col:
            try: ref_lo = float((row.get(ref_lo_col) or "").strip())
            except (ValueError, TypeError): pass
        if ref_hi_col:
            try: ref_hi = float((row.get(ref_hi_col) or "").strip())
            except (ValueError, TypeError): pass
        # Fall back to combined "Reference Range" column when either bound is still missing
        if range_col and (ref_lo is None or ref_hi is None):
            p_lo, p_hi = _parse_ref_range_str((row.get(range_col) or "").strip())
            if ref_lo is None and p_lo is not None:
                ref_lo = p_lo
            if ref_hi is None and p_hi is not None:
                ref_hi = p_hi

        flagged = False
        if flag_col:
            fv = (row.get(flag_col) or "").strip().upper()
            flagged = fv in (
                "H", "L", "HIGH", "LOW", "ABNORMAL", "CRITICAL",
                "A", "*", "Y", "YES", "TRUE", "1", "OUT OF RANGE",
                "HH", "LL", "PANIC",
            )
        elif ref_lo is not None and ref_hi is not None:
            flagged = (value_num < ref_lo or value_num > ref_hi)

        raw_snip = f"{name}: {val_str} {unit} ({date_raw})".strip()
        is_bio   = any(kw in name.lower() for kw in _BIO_KEYWORDS)

        if is_bio:
            biometrics.append(GeminiBiometricLog(
                recorded_at=dt,
                metric_name=re.sub(r"\s+", "_", name.lower()),
                value=value_num,
                unit=unit or "",
                raw_text=raw_snip,
            ))
        else:
            labs.append(GeminiLabResult(
                collected_at=dt,
                marker_name=name,
                value_numeric=value_num,
                unit=unit or None,
                reference_low=ref_lo,
                reference_high=ref_hi,
                flagged=flagged,
                raw_text=raw_snip,
            ))

    logger.info(f"[csv_parse] {len(labs)} labs + {len(biometrics)} biometrics, skipped {skipped}")

    if not labs and not biometrics:
        return None

    log_type: List[str] = []
    if labs:       log_type.append("lab_result")
    if biometrics: log_type.append("biometric")

    raw_summary = (
        f"CSV import: {filename} ({len(labs)} lab results, {len(biometrics)} biometrics)"
    )
    gemini_payload = GeminiExtractionPayload(
        raw_input_text=raw_summary,
        log_type=log_type,
        labs=labs,
        biometrics=biometrics,
    )
    return to_strict_payload(raw_summary, gemini_payload)


# ── Client factory & connection pool ─────────────────────────────────────────
# Reusing a single genai.Client instance lets httpx pool TCP connections and
# resume TLS sessions across requests; saves 200-500 ms per call and avoids
# SSL handshake timeouts that occur when a fresh TLS connection is raced against
# a tight timeout.

def _make_client(timeout_ms: int) -> Any:
    """Construct a new Gemini client.

    timeout_ms: request timeout in **milliseconds** (SDK convention).
    SDK-level retries are disabled here; our code owns retry logic explicitly.
    Without disabling them, the SDK retries up to 5× with exponential backoff
    on httpx.TimeoutException, turning a 30 s per-attempt timeout into a
    150+ second stall before raising.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    from google import genai
    from google.genai import types as genai_types

    return genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(
            timeout=timeout_ms,
            retry_options=genai_types.HttpRetryOptions(attempts=1),
        ),
    )


# Legacy helper: timeout in seconds (callers that supply their own client).
def get_client(timeout: float = 30.0) -> Any:
    """timeout is in seconds; converts to ms internally."""
    return _make_client(timeout_ms=int(timeout * 1000))


_extractor_client: Optional[Any] = None
_conversation_client: Optional[Any] = None


def _get_extractor_client() -> Any:
    global _extractor_client
    if _extractor_client is None:
        _extractor_client = _make_client(timeout_ms=60_000)  # 60 s: large panels need more time
    return _extractor_client


def _get_conversation_client() -> Any:
    global _conversation_client
    if _conversation_client is None:
        _conversation_client = _make_client(timeout_ms=90_000)  # 90 s
    return _conversation_client


# ── Extraction helpers ─────────────────────────────────────────────────────────

def normalize_payload(raw_text: str, payload: AIExtractionPayload) -> AIExtractionPayload:
    payload.raw_input_text = raw_text

    log_type = []
    if payload.supplements:
        log_type.append("supplement")
        for supplement in payload.supplements:
            if not supplement.raw_text:
                supplement.raw_text = raw_text
    if payload.biometrics:
        log_type.append("biometric")
        for biometric in payload.biometrics:
            if not biometric.raw_text:
                biometric.raw_text = raw_text
    if payload.labs:
        log_type.append("lab_result")
        for lab in payload.labs:
            if not lab.raw_text:
                lab.raw_text = raw_text
    if payload.journals:
        log_type.append("daily_journal")
        for journal in payload.journals:
            if not journal.raw_text:
                journal.raw_text = raw_text

    payload.log_type = log_type or payload.log_type
    return AIExtractionPayload.model_validate(payload.model_dump())


def to_strict_payload(raw_text: str, parsed: Any) -> AIExtractionPayload:
    if isinstance(parsed, AIExtractionPayload):
        return normalize_payload(raw_text, parsed)
    if isinstance(parsed, BaseModel):
        data = parsed.model_dump(exclude_none=True)
    else:
        data = parsed

    data["raw_input_text"] = raw_text
    return normalize_payload(raw_text, AIExtractionPayload.model_validate(data))


_TRANSIENT_KEYWORDS = ("timeout", "handshake", "ssl", "connection", "read", "timed out")


def extract(raw_text: str, protocols_context: str = "", client: Optional[Any] = None) -> AIExtractionPayload:
    """Structured extraction only; does not commit or route."""
    import time as _time

    if not raw_text.strip():
        raise ValueError("raw_text cannot be empty")

    active_client = client or _get_extractor_client()
    from google.genai import types

    # Fetch today's targeted clinical inquiries and format them for prompt injection
    inquiries = _get_todays_inquiries()
    inquiries_context = (
        "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(inquiries))
        if inquiries else ""
    )

    last_exc: Optional[Exception] = None
    for attempt in range(2):  # one retry on transient network errors
        try:
            response = active_client.models.generate_content(
                model=EXTRACTOR_MODEL,
                contents=raw_text,
                config=types.GenerateContentConfig(
                    system_instruction=_build_extraction_prompt(protocols_context, inquiries_context),
                    response_mime_type="application/json",
                    response_schema=GeminiExtractionPayload,
                    temperature=0.0,
                ),
            )
            parsed = response.parsed
            if parsed is None:
                raise ValueError("Gemini returned no parsed payload")
            return to_strict_payload(raw_text, parsed)
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and any(k in str(exc).lower() for k in _TRANSIENT_KEYWORDS):
                logger.warning(f"[extractor] transient error, retrying: {exc}")
                _time.sleep(3)  # brief back-off before retry: gives Gemini time to recover
                continue
            raise

    raise last_exc  # type: ignore[misc]  # unreachable, satisfies type checker


# ── Check-in: direct data fetch + dedicated stream ───────────────────────────

_CHECK_IN_SYSTEM_PROMPT = """
You are LEDGER, a performance and health partner.

You have been handed a structured data export covering a specific time window. Your job is to
analyze it the way a coach reviews film before a session: find the signal, name the trends,
connect the protocol to the outcomes, and flag what needs attention.

Go deep where the data warrants it: mechanisms, pharmacokinetics, interactions, what the
numbers actually mean for performance and health. Be specific to what's in the data; do not
speculate about what isn't there.

KNOWLEDGE GRAPH CROSS-REFERENCE:
The data export includes a KNOWLEDGE GRAPH section listing confirmed and testing protocols or
findings. During your analysis, cross-reference each confirmed or testing node against the
SUPPLEMENTS and LABS sections for the same window:
- If a confirmed protocol (e.g. an iron repletion protocol, a supplement intervention) has NO
  matching supplement logs or lab entries in the data window, that is a gap worth naming: the
  user may have stopped, forgotten, or not yet logged it.
- Note how long it has been since that node was last updated (the [updated Xd ago] tag).
- Nodes that are both confirmed AND absent from recent data AND last updated many days ago are
  the highest-priority gaps to surface.

Structure your response as flowing prose, not bullet points. Depth scales with signal.

Always end with one specific, targeted question, but apply pharmacokinetic pacing first:

BEFORE forming your closing question, check the SUPPLEMENTS section for any recently started
slow-acting item (fat-soluble vitamins, mineral repletion, creatine, anything with a
multi-day half-life). Determine how long it has been active by finding its earliest entry date.

  • If the item is slow-acting AND has been active fewer than 14 days:
    DO NOT ask about outcomes (mood, energy, body composition, performance).
    Instead ask about: adherence to the dosing schedule, acute tolerance
    (GI tolerance, any reaction), or what PK milestone they are approaching next.
    The wrong question at the wrong time degrades trust more than no question.

  • If the item is slow-acting AND has been active 14–41 days:
    Early physiological questions are appropriate (sleep quality, subtle energy changes).
    Longer-horizon outcome questions are still premature.

  • If the item is slow-acting AND has been active 42+ days:
    Full outcome questioning is appropriate.

  • Fast-acting supplements (stimulants, sleep aids, acute nootropics, pre-workouts):
    next-day outcome questions are always appropriate.

If you identified a knowledge-graph gap (confirmed protocol with no recent logged data),
ask whether they are still running the protocol, why it may have dropped off, or, if the
timeline permits, what results they have seen. If no gap exists, ask an open-ended question
grounded in the data you just analyzed.

REASONING STRUCTURE: MANDATORY:
Use a <think>...</think> block for your internal analysis BEFORE writing your response.
The <think> block is rendered collapsed; everything inside it is private reasoning.
The visible analysis MUST be written AFTER the closing </think> tag, never inside it.

CORRECT STRUCTURE (follow exactly):
  <think>
  [All reasoning: data review, trend identification, KG cross-reference, PK pacing logic,
   closing-question selection. None of this appears to the user.]
  </think>

  [Your written analysis addressed directly to the user starts here: no preamble,
   no "Here's my analysis:", no meta-narration about your process. This is what the user reads.]

STRUCTURAL VIOLATIONS that will break the response:
  ✗  Putting the entire analysis inside <think>...</think>
  ✗  Writing "Here's a thinking process that could lead to the user-facing response:"
  ✗  Writing "Let me analyze..." or "I will now..." anywhere outside <think>
  ✗  Ending with </think> and writing nothing after it
""".strip()


_GARMIN_SLEEP_METRICS = {
    "sleep_duration", "sleep_deep", "sleep_light", "sleep_rem",
    "sleep_awake", "sleep_spo2_avg", "sleep_spo2_low", "sleep_respiration", "sleep_stress",
}
_GARMIN_DAILY_METRICS = {"hrv_last_night", "hrv_weekly_avg", "resting_heart_rate", "body_battery"}
_GARMIN_WORKOUT_METRICS = {"workout_session"}


def _fmt_val(v: Any, unit: str) -> str:
    try:
        fv = float(v)
        s = f"{fv:.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        s = str(v)
    return f"{s} {unit}".strip() if unit else s


def build_check_in_context(days: int = 14) -> str:
    """Query all four tables for the past `days` days and return a formatted context block."""
    from datetime import timedelta
    from collections import defaultdict
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    today = datetime.now().date().isoformat()

    sections: List[str] = [
        f"[CHECK-IN DATA - past {days} days ({cutoff} to {today})]"
    ]

    with _sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = _sqlite3.Row

        # Biometrics - split into Garmin and manual, then reformat
        bio_rows = conn.execute(
            "SELECT recorded_at, metric_name, value, unit, notes, context "
            "FROM biometric_logs WHERE date(recorded_at) >= ? ORDER BY date(recorded_at) ASC",
            (cutoff,),
        ).fetchall()

        if bio_rows:
            # Partition into Garmin sleep, Garmin daily, Garmin workouts, and manual
            # night_data: {date: {metric: (value, unit)}}
            night_data: dict = defaultdict(dict)
            # daily_data: {metric: [(date, value, unit)]}
            daily_data: dict = defaultdict(list)
            # workout_data: [(recorded_at, avg_hr, notes)]  (newest first)
            workout_data: list = []
            # manual_data: {metric_name: [(date, value, unit, notes)]}
            manual_data: dict = defaultdict(list)

            for r in bio_rows:
                date_str = r["recorded_at"][:10]
                metric = r["metric_name"]
                is_garmin = r["context"] == "garmin_sync"

                if is_garmin and metric in _GARMIN_SLEEP_METRICS:
                    night_data[date_str][metric] = (r["value"], r["unit"] or "")
                elif is_garmin and metric in _GARMIN_DAILY_METRICS:
                    daily_data[metric].append((date_str, r["value"], r["unit"] or ""))
                elif is_garmin and metric in _GARMIN_WORKOUT_METRICS:
                    workout_data.append((r["recorded_at"], r["value"], r["notes"] or ""))
                else:
                    manual_data[metric].append((date_str, r["value"], r["unit"] or "", r["notes"] or ""))

            bio_sections: List[str] = []

            def _night_val(night: dict, key: str, decimals: int = 2) -> str:
                if key not in night:
                    return "?"
                try:
                    return f"{float(night[key][0]):.{decimals}f}"
                except (TypeError, ValueError):
                    return str(night[key][0])

            # Garmin sleep: one row per night, all fields inline
            if night_data:
                lines = ["SLEEP - nightly (Garmin):"]
                for date_str in sorted(night_data.keys(), reverse=True):
                    m = night_data[date_str]
                    parts = [f"total {_night_val(m, 'sleep_duration')}h"]
                    if "sleep_deep" in m:
                        parts.append(f"deep {_night_val(m, 'sleep_deep')}h")
                    if "sleep_light" in m:
                        parts.append(f"light {_night_val(m, 'sleep_light')}h")
                    if "sleep_rem" in m:
                        parts.append(f"REM {_night_val(m, 'sleep_rem')}h")
                    if "sleep_awake" in m:
                        parts.append(f"awake {_night_val(m, 'sleep_awake')}h")
                    if "sleep_spo2_avg" in m:
                        parts.append(
                            f"SpO2 avg {_night_val(m, 'sleep_spo2_avg', 0)}%"
                            f" low {_night_val(m, 'sleep_spo2_low', 0)}%"
                        )
                    if "sleep_respiration" in m:
                        parts.append(f"resp {_night_val(m, 'sleep_respiration', 0)} br/m")
                    if "sleep_stress" in m:
                        parts.append(f"stress {_night_val(m, 'sleep_stress', 0)}")
                    lines.append(f"  {date_str}: {' | '.join(parts)}")
                bio_sections.append("\n".join(lines))

            # Garmin daily metrics: time series per metric
            if daily_data:
                lines = ["GARMIN DAILY METRICS:"]
                metric_order = ["hrv_last_night", "hrv_weekly_avg", "resting_heart_rate", "body_battery"]
                for metric in metric_order:
                    if metric not in daily_data:
                        continue
                    entries = daily_data[metric]
                    unit = entries[0][2] if entries else ""
                    label = f"  {metric} ({unit}):" if unit else f"  {metric}:"
                    series = "  ->  ".join(
                        f"{date_str[5:]}: {_fmt_val(v, '')}"
                        for date_str, v, _ in entries
                    )
                    lines.append(f"{label:<36}{series}")
                bio_sections.append("\n".join(lines))

            # Garmin workouts: one row per session, newest first
            if workout_data:
                lines = ["GARMIN WORKOUTS (avg HR · session detail):"]
                for recorded_at_str, avg_hr, notes in sorted(workout_data, reverse=True):
                    ts = recorded_at_str[:16].replace("T", " ")  # "YYYY-MM-DD HH:MM"
                    detail = f": {notes}" if notes else ""
                    lines.append(f"  {ts}  avg HR {int(float(avg_hr))} bpm{detail}")
                bio_sections.append("\n".join(lines))

            # Manually logged biometrics: grouped by metric name
            if manual_data:
                lines = ["MANUALLY LOGGED BIOMETRICS:"]
                for metric, entries in sorted(manual_data.items()):
                    unit = entries[0][2] if entries else ""
                    label = f"  {metric} ({unit}):" if unit else f"  {metric}:"
                    series_parts = []
                    for date_str, v, u, note in entries:
                        part = f"{date_str[5:]}: {_fmt_val(v, '')}"
                        if note:
                            part += f" [{note}]"
                        series_parts.append(part)
                    lines.append(f"{label:<36}{'  ->  '.join(series_parts)}")
                bio_sections.append("\n".join(lines))

            if bio_sections:
                sections.append("\n\n".join(bio_sections))
        else:
            sections.append(f"BIOMETRICS: none in past {days} days")

        # Supplements
        cpd_rows = conn.execute(
            "SELECT recorded_at, supplement_name, dose_value, dose_unit, route, site, notes "
            "FROM supplement_logs WHERE date(recorded_at) >= ? ORDER BY recorded_at DESC",
            (cutoff,),
        ).fetchall()
        if cpd_rows:
            lines = [f"SUPPLEMENTS ({len(cpd_rows)} records)"]
            for r in cpd_rows:
                ts = r["recorded_at"][:10]
                site = f" ({r['site']})" if r["site"] else ""
                note = f"  -- {r['notes']}" if r["notes"] else ""
                lines.append(
                    f"  {ts}  {r['supplement_name']}: {r['dose_value']} {r['dose_unit']} {r['route']}{site}{note}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append(f"SUPPLEMENTS: none in past {days} days")

        # Labs
        lab_rows = conn.execute(
            "SELECT collected_at, marker_name, panel_name, value_numeric, unit, "
            "reference_low, reference_high, flagged "
            "FROM lab_results WHERE date(collected_at) >= ? ORDER BY collected_at DESC",
            (cutoff,),
        ).fetchall()
        if lab_rows:
            lines = [f"LABS ({len(lab_rows)} records)"]
            for r in lab_rows:
                ts = r["collected_at"][:10]
                panel = f" [{r['panel_name']}]" if r["panel_name"] else ""
                ref = ""
                if r["reference_low"] is not None and r["reference_high"] is not None:
                    ref = f" (ref {r['reference_low']}-{r['reference_high']})"
                flag = " ** FLAGGED" if r["flagged"] else ""
                lines.append(
                    f"  {ts}  {r['marker_name']}{panel}: "
                    f"{r['value_numeric']} {r['unit'] or ''}{ref}{flag}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append(f"LABS: none in past {days} days")

        # Journals
        jnl_rows = conn.execute(
            "SELECT journal_date, mood, energy_score, sleep_hours, notes "
            "FROM daily_journals WHERE journal_date >= ? ORDER BY journal_date DESC",
            (cutoff,),
        ).fetchall()
        if jnl_rows:
            lines = [f"JOURNALS ({len(jnl_rows)} records)"]
            for r in jnl_rows:
                mood = r["mood"] or "?"
                energy = f"energy {r['energy_score']}/10" if r["energy_score"] is not None else ""
                sleep = f"sleep {r['sleep_hours']}h" if r["sleep_hours"] else ""
                meta = "  |  ".join(filter(None, [mood, energy, sleep]))
                note = f"\n    {r['notes']}" if r["notes"] else ""
                lines.append(f"  {r['journal_date']}  {meta}{note}")
            sections.append("\n".join(lines))
        else:
            sections.append(f"JOURNALS: none in past {days} days")

        # Knowledge graph: confirmed and testing nodes only
        try:
            node_rows = conn.execute(
                """SELECT concept_name, category, confidence_level, summary_text, last_updated
                   FROM clinical_nodes
                   WHERE confidence_level IN ('confirmed', 'testing')
                   ORDER BY
                       CASE confidence_level WHEN 'confirmed' THEN 0 ELSE 1 END,
                       concept_name""",
            ).fetchall()
            if node_rows:
                lines = [f"KNOWLEDGE GRAPH ({len(node_rows)} active protocols/nodes):"]
                for r in node_rows:
                    age_note = ""
                    if r["last_updated"]:
                        try:
                            last_upd_dt = datetime.fromisoformat(r["last_updated"][:19])
                            d = max(0, (datetime.now() - last_upd_dt).days)
                            age_note = "  [updated today]" if d == 0 else f"  [updated {d}d ago]"
                        except Exception:
                            pass
                    lines.append(
                        f"  [{r['confidence_level'].upper()}] {r['concept_name']} ({r['category']}){age_note}"
                    )
                    if r["summary_text"]:
                        lines.append(f"    {r['summary_text']}")
                sections.append("\n".join(lines))
        except Exception:
            pass  # clinical_nodes table may not exist in all environments

    return "\n\n".join(sections)


def check_in_stream(
    days: int = 14,
    focus: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[Any] = None,
) -> Generator[Tuple[str, str], None, None]:
    """Fetch all data for `days` days, build context, and stream the analysis.

    Yields ("status", msg) and ("chunk", text) tuples with same wire format as
    chat_respond_stream so the SSE endpoint can handle both identically.
    """
    from google.genai import types as _gt

    yield ("status", f"Fetching {days}-day data snapshot...")
    try:
        context = build_check_in_context(days)
    except Exception as exc:
        yield ("chunk", f"Failed to fetch ledger data: {exc}")
        return

    focus_line = f"\n\nFocus for this check-in: {focus}" if focus else ""
    user_prompt = f"{context}{focus_line}"

    active_client = client or _get_conversation_client()

    gemini_history = _to_gemini_history(history) if history else []

    chat = active_client.chats.create(
        model=CONVERSATION_MODEL,
        config=_gt.GenerateContentConfig(
            system_instruction=_CHECK_IN_SYSTEM_PROMPT,
            temperature=0.7,
            safety_settings=_clinical_safety_settings(),
            # No tools: data is pre-fetched; no tool calls needed.
        ),
        history=gemini_history,
    )

    yield ("status", "Analyzing...")
    try:
        for chunk in chat.send_message_stream(user_prompt):
            visible = _get_visible_chunk_text(chunk)
            if visible:
                normalized = visible.replace('<thought>', '<think>').replace('</thought>', '</think>')
                yield ("chunk", normalized)
    except Exception as exc:
        yield ("chunk", f"[stream error: {exc}]")


# ── Knowledge Graph context loader ────────────────────────────────────────────

def _load_graph_context() -> List[Dict[str, Any]]:
    """Return all confirmed/testing nodes + recent hypothesis nodes for narrative context.

    No LIMIT: dropping foundational confirmed lore from the context window (the
    'Amnesia Trap') silently degrades clinical reasoning. All persistent theories
    must stay in view.
    """
    try:
        with _sqlite3.connect(str(_DB_PATH)) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """SELECT concept_name, category, summary_text, confidence_level
                   FROM clinical_nodes
                   WHERE confidence_level IN ('testing', 'confirmed')
                      OR (confidence_level = 'hypothesis'
                          AND last_updated >= datetime('now', '-7 days'))
                   ORDER BY
                       CASE confidence_level
                           WHEN 'confirmed'  THEN 1
                           WHEN 'testing'    THEN 2
                           WHEN 'hypothesis' THEN 3
                       END,
                       last_updated DESC"""
            ).fetchall()
            nodes = [dict(r) for r in rows]
            # Sanitize any %% that the synthesis model may have written:
            # JSON is not a Python format string; the model occasionally doubles %.
            for node in nodes:
                if node.get("summary_text"):
                    node["summary_text"] = node["summary_text"].replace("%%", "%")
            return nodes
    except Exception:
        return []


# ── Clinical narrative context builder ────────────────────────────────────────

_ROUTE_LABELS: Dict[str, str] = {
    "oral": "orally",
    "sublingual": "sublingually",
    "transdermal": "transdermally",
    "intranasal": "intranasally",
}


def _estimate_tokens(text: str) -> int:
    """Cheap, free token estimate (~4 chars/token for English prose).

    Good enough to track *proportions* (which injected section is growing) without
    paying for a count_tokens round-trip on every turn. The authoritative figure is
    logged separately from Gemini's usage_metadata.
    """
    return (len(text) + 3) // 4 if text else 0


def _log_context_budget(
    *,
    system_prompt: str,
    narrative: str,
    prompt: str,
    graph_context: Optional[List[Dict[str, Any]]],
    transcript_context: str,
    mem0_context: str,
    history: Optional[List[Dict[str, Any]]],
    user_text: str,
) -> Dict[str, int]:
    """Log an estimated per-section breakdown of the input context for one turn.

    This is the development gauge for context bloat: it attributes the prompt's
    token draw to each contributor (system prompt, knowledge graph, transcript
    recall, chat recall, history, user message) so a ballooning memory section is
    visible at a glance. Estimates only: see usage_metadata logging for the real
    number. Never raises.
    """
    try:
        # Render graph lines the same way _generate_clinical_narrative does, so the
        # estimate matches what's actually injected.
        graph_str = ""
        if graph_context:
            graph_str = "\n".join(
                f"[{(n.get('confidence_level') or 'hypothesis').upper()}] "
                f"{n.get('concept_name', '')} ({n.get('category', '')}): {n.get('summary_text', '')}"
                for n in graph_context
            )
        history_str = "\n".join(str(h.get("content", "")) for h in history) if history else ""

        budget = {
            "system":     _estimate_tokens(system_prompt),
            "graph":      _estimate_tokens(graph_str),
            "transcript": _estimate_tokens(transcript_context),
            "chat_recall": _estimate_tokens(mem0_context),
            "history":    _estimate_tokens(history_str),
            "user":       _estimate_tokens(user_text),
            "narrative":  _estimate_tokens(narrative),
        }
        # Total input ≈ system instruction + conversation history + this turn's prompt
        # (the prompt already contains the narrative + user message).
        budget["input_total"] = budget["system"] + budget["history"] + _estimate_tokens(prompt)
        budget["graph_nodes"] = len(graph_context) if graph_context else 0

        logger.info(
            "[token-budget] est input~%d tok | system=%d graph=%d(%d nodes) "
            "transcript=%d chat_recall=%d history=%d user=%d (narrative=%d)",
            budget["input_total"], budget["system"], budget["graph"], budget["graph_nodes"],
            budget["transcript"], budget["chat_recall"], budget["history"],
            budget["user"], budget["narrative"],
        )
        return budget
    except Exception as exc:  # measurement must never break the chat path
        logger.debug("[token-budget] estimate failed (non-fatal): %s", exc)
        return {}


def _log_actual_usage(chunk: Any, label: str = "input") -> None:
    """Log Gemini's authoritative token counts from a response's usage_metadata.

    This is the real bill (includes tool schemas, system instruction, history, and
    output/thinking tokens), the ground truth the per-section estimate approximates.
    Best-effort; never raises.
    """
    try:
        um = getattr(chunk, "usage_metadata", None)
        if not um:
            return
        logger.info(
            "[token-usage] %s | prompt=%s candidates=%s thoughts=%s total=%s",
            label,
            getattr(um, "prompt_token_count", None),
            getattr(um, "candidates_token_count", None),
            getattr(um, "thoughts_token_count", None),
            getattr(um, "total_token_count", None),
        )
    except Exception as exc:
        logger.debug("[token-usage] read failed (non-fatal): %s", exc)


def _generate_clinical_narrative(
    extracted: Optional[AIExtractionPayload],
    mem0_context: str,
    graph_context: Optional[List[Dict[str, Any]]] = None,
    transcript_context: str = "",
) -> str:
    """Synthesise graph lore + recalled history + just-logged data into narrative prose.

    Sections in priority order:
      1. [Current theories]        : Knowledge Graph nodes (confirmed/testing first)
      2. [From past conversations] : verbatim transcript snippets (semantic recall)
      3. [What we know]            : mem0 semantic memory hits
      4. [Just logged]             : extracted data from the current message
    """
    parts: List[str] = []

    # Section 1: Knowledge Graph theories
    if graph_context:
        theory_lines = []
        for node in graph_context:
            conf = (node.get("confidence_level") or "hypothesis").upper()
            name = node.get("concept_name", "")
            cat  = node.get("category", "")
            summ = node.get("summary_text", "")
            theory_lines.append(f"[{conf}] {name} ({cat}): {summ}")
        if theory_lines:
            parts.append("[Current theories]\n" + "\n".join(theory_lines))

    # Section 2: verbatim recall from ingested conversation transcripts
    if transcript_context and transcript_context.strip():
        parts.append(
            "[From past conversations]\n"
            "Relevant excerpts recalled from transcripts the user has shared:\n"
            f"{transcript_context.strip()}"
        )

    if mem0_context and mem0_context.strip():
        parts.append(f"[What we know from past sessions]\n{mem0_context.strip()}")

    if not extracted or not any([
        extracted.supplements, extracted.biometrics,
        extracted.labs, extracted.journals,
    ]):
        return "\n\n".join(parts)

    events: List[str] = []

    for c in extracted.supplements:
        route_val = c.route.value if hasattr(c.route, "value") else str(c.route)
        route_label = _ROUTE_LABELS.get(route_val, route_val)
        notes_str = f" ({c.notes})" if c.notes else ""
        events.append(
            f"The user administered {c.supplement_name}: {c.dose_value} {c.dose_unit} {route_label}{notes_str}."
        )

    for b in extracted.biometrics:
        metric_label = b.metric_name.replace("_", " ")
        unit_str = f" {b.unit}" if b.unit else ""
        notes_str = f" ({b.notes})" if b.notes else ""
        events.append(
            f"The user recorded {metric_label} at {b.value}{unit_str}{notes_str}."
        )

    for lab in extracted.labs:
        val = (
            f"{lab.value_numeric} {lab.unit or ''}".strip()
            if lab.value_numeric is not None
            else lab.value_text or "not recorded"
        )
        flagged_str = ", flagged as abnormal" if lab.flagged else ""
        panel_str = f" ({lab.panel_name})" if lab.panel_name else ""
        events.append(
            f"Lab result{panel_str}: {lab.marker_name} came back at {val}{flagged_str}."
        )

    for j in extracted.journals:
        mood_str = f", feeling {j.mood.value.replace('_', ' ')}" if j.mood else ""
        energy_str = f", energy {j.energy_score}/10" if j.energy_score is not None else ""
        sleep_str = f", slept {j.sleep_hours}h" if j.sleep_hours else ""
        events.append(
            f"Journal entry{mood_str}{energy_str}{sleep_str}: {j.notes[:200]}."
        )

    if events:
        parts.append("[Just logged]\n" + "  ".join(events))

    return "\n\n".join(parts)


# ── Ledger tools (function calling) ───────────────────────────────────────────

_TOOL_TABLE_MAP: Dict[str, tuple] = {
    # entry_type → (table, order_col, columns_to_return)
    "biometric": (
        "biometric_logs", "recorded_at",
        ["id", "metric_name", "value", "unit", "recorded_at", "notes"],
    ),
    "supplement": (
        "supplement_logs", "recorded_at",
        ["id", "supplement_name", "dose_value", "dose_unit", "route", "site", "recorded_at", "notes"],
    ),
    "lab": (
        "lab_results", "collected_at",
        ["id", "marker_name", "panel_name", "value_numeric", "unit",
         "reference_low", "reference_high", "collected_at", "flagged"],
    ),
    "journal": (
        "daily_journals", "journal_date",
        ["id", "mood", "energy_score", "sleep_hours", "notes", "journal_date"],
    ),
}

_NAME_COL: Dict[str, str] = {
    "biometric": "metric_name",
    "supplement":  "supplement_name",
    "lab":       "marker_name",
    "journal":   "notes",
}


def _tool_query_entries(entry_type: str, metric_name: str = "", limit: int = 10) -> Dict[str, Any]:
    if entry_type not in _TOOL_TABLE_MAP:
        return {"error": f"Unknown entry_type '{entry_type}'. Use: biometric, supplement, lab, journal"}
    table, order_col, cols = _TOOL_TABLE_MAP[entry_type]
    col_str = ", ".join(cols)
    conn = _sqlite3.connect(str(_DB_PATH))
    conn.row_factory = _sqlite3.Row
    try:
        if metric_name:
            name_col = _NAME_COL[entry_type]
            rows = conn.execute(
                f"SELECT {col_str} FROM {table} WHERE {name_col} LIKE ? ORDER BY {order_col} DESC LIMIT ?",  # noqa: S608
                (f"%{metric_name}%", max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {col_str} FROM {table} ORDER BY {order_col} DESC LIMIT ?",  # noqa: S608
                (max(1, int(limit)),),
            ).fetchall()
        return {"entries": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


# UPDATABLE_FIELDS imported from models.py as single source of truth


def _tool_update_entry(entry_type: str, entry_id: str, field_name: str, new_value: str) -> Dict[str, Any]:
    if entry_type not in _TOOL_TABLE_MAP:
        return {"error": f"Unknown entry_type '{entry_type}'. Use: biometric, supplement, lab, journal"}
    if not entry_id or not entry_id.strip():
        return {"error": "entry_id must not be empty"}
    allowed = UPDATABLE_FIELDS.get(entry_type, set())
    if field_name not in allowed:
        return {"error": f"Field '{field_name}' is not updatable for {entry_type}. Allowed: {sorted(allowed)}"}
    table = _TOOL_TABLE_MAP[entry_type][0]
    try:
        conn = _sqlite3.connect(str(_DB_PATH))
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE {table} SET {field_name} = ? WHERE id = ?",  # noqa: S608
            (new_value.strip(), entry_id.strip()),
        )
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return {"error": f"No entry found with id={entry_id!r} in {table}"}
        conn.close()
        return {"success": True, "updated_id": entry_id, "field": field_name, "new_value": new_value}
    except Exception as exc:
        return {"error": str(exc)}


def _tool_delete_entry(entry_type: str, entry_id: str) -> Dict[str, Any]:
    if entry_type not in _TOOL_TABLE_MAP:
        return {"error": f"Unknown entry_type '{entry_type}'. Use: biometric, supplement, lab, journal"}
    if not entry_id or not entry_id.strip():
        return {"error": "entry_id must not be empty"}
    table = _TOOL_TABLE_MAP[entry_type][0]
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE id = ?",  # noqa: S608
            (entry_id.strip(),),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"No entry found with id={entry_id!r} in {table}"}
        return {"success": True, "deleted_id": entry_id, "entry_type": entry_type}
    finally:
        conn.close()


def _tool_force_synthesis() -> Dict[str, Any]:
    """Trigger run_morning_synthesis() directly, bypassing the daily date gate.

    Mirrors what api.py's force-synthesis endpoint does, but called in-process
    so no HTTP round-trip is needed. After synthesis, stamps last_synthesis_date
    to today so the next automatic boot skips.

    Note: run_morning_synthesis is sync and makes a Gemini API call; expect
    10–40 s of latency. This is acceptable for an explicit user-triggered action.
    """
    try:
        from reflection_worker import run_morning_synthesis as _run_synthesis  # noqa: PLC0415
        from datetime import date as _date_cls  # noqa: PLC0415

        result = _run_synthesis()

        # Stamp last_synthesis_date so the automatic boot won't double-fire.
        today = _date_cls.today().isoformat()
        with _sqlite3.connect(str(_DB_PATH)) as _conn:
            _conn.execute(
                "INSERT INTO system_state (key, value, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                ("last_synthesis_date", today),
            )
            _conn.commit()

        # Notify the frontend so the Knowledge graph and Tracking board re-fetch.
        bus.emit_sync("SYNTHESIS_COMPLETE", {"source": "force_synthesis", "date": today})

        return {
            "success": True,
            "synthesis_date": today,
            "nodes_created": result.get("nodes_created", 0),
            "nodes_updated": result.get("nodes_updated", 0),
            "edges_created": result.get("edges_created", 0),
        }
    except Exception as exc:
        logger.error(f"[force_synthesis tool] failed: {exc}")
        return {"success": False, "error": str(exc)}


def _tool_web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    """Search the internet using Gemini's built-in Google Search grounding.

    Makes a dedicated sub-call to Gemini Flash (fast/cheap) with only the
    google_search tool enabled; no mixing issue with the main function tools.
    Returns a concise summary and a list of source URLs so the calling model
    can cite its references.
    """
    if not query or not query.strip():
        return {"error": "query must not be empty"}

    from google.genai import types as _gt

    client = _get_extractor_client()   # Flash client: speed over depth for search
    clamped = max(1, min(int(max_results), 10))

    search_prompt = (
        f"Search for: {query.strip()}\n\n"
        "Return a concise, factual summary of the most relevant information. "
        "Include specific numbers, dates, and citations where applicable."
    )
    try:
        response = client.models.generate_content(
            model=EXTRACTOR_MODEL,
            contents=search_prompt,
            config=_gt.GenerateContentConfig(
                tools=[_gt.Tool(google_search=_gt.GoogleSearch())],
                temperature=0.0,
            ),
        )
    except Exception as exc:
        logger.warning("[web_search] Gemini search call failed: %s", exc)
        return {"error": str(exc), "query": query}

    # Extract the grounded summary text (filters thought parts)
    summary = ""
    try:
        parts = response.candidates[0].content.parts or []
        visible = [
            p.text for p in parts
            if getattr(p, "text", None) is not None
            and not getattr(p, "function_call", None)
            and not getattr(p, "thought", False)
        ]
        summary = "".join(visible).strip()
    except Exception:
        summary = getattr(response, "text", None) or ""

    # Extract grounding source URLs
    sources: List[Dict[str, str]] = []
    try:
        grounding = response.candidates[0].grounding_metadata
        if grounding:
            for chunk in (grounding.grounding_chunks or []):
                web = getattr(chunk, "web", None)
                if web:
                    sources.append({
                        "title": getattr(web, "title", "") or "",
                        "url":   getattr(web, "uri",   "") or "",
                    })
                    if len(sources) >= clamped:
                        break
    except Exception:
        pass

    return {
        "query":   query,
        "summary": summary,
        "sources": sources,
    }


def _tool_save_regimen_item(
    supplement_name: str,
    dose_value: str,
    dose_unit: str,
    route: str,
    frequency: str,
    time_of_day: str,
    days_of_week: str = "",
    site: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    conn = _sqlite3.connect(str(_DB_PATH))
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
                supplement_name.strip(),
                dose_value.strip(),
                dose_unit.strip(),
                route.strip(),
                site.strip() or None,
                frequency.strip(),
                time_of_day.strip(),
                days_of_week.strip() or None,
                notes.strip() or None,
            ),
        )
        conn.commit()
        return {"success": True, "supplement_name": supplement_name.strip(), "action": "saved_to_regimen"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


def _tool_save_regimen_batch(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Save multiple regimen items in one shot to avoid the model making N separate calls."""
    conn = _sqlite3.connect(str(_DB_PATH))
    saved: List[str] = []
    errors: List[str] = []
    try:
        for item in items:
            if not isinstance(item, dict):
                errors.append(f"Invalid item (not a dict): {item!r}")
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
        conn.commit()
    finally:
        conn.close()
    return {"success": len(errors) == 0, "saved": saved, "errors": errors, "count": len(saved)}


def _tool_remove_regimen_item(supplement_name: str, time_of_day: str = "") -> Dict[str, Any]:
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        if time_of_day.strip():
            cursor = conn.execute(
                "DELETE FROM user_regimen WHERE supplement_name = ? AND time_of_day = ?",
                (supplement_name.strip(), time_of_day.strip()),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM user_regimen WHERE supplement_name = ?",
                (supplement_name.strip(),),
            )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"No regimen entry found for '{supplement_name.strip()}'"}
        return {"success": True, "supplement_name": supplement_name.strip(), "removed": cursor.rowcount}
    finally:
        conn.close()


def _tool_draft_regimen_change(
    additions: Optional[List[Dict[str, Any]]] = None,
    removals: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Propose a regimen change without writing to the database.

    Emits a PROPOSAL_CREATED event to the frontend so the user can approve or
    reject the change before it is committed.  This is the HITL gate for all
    standing-protocol modifications.

    Uses bus.emit_sync because this function is always called from
    _execute_ledger_tool, which runs inside a daemon thread.
    """
    clean_additions = additions if isinstance(additions, list) else []
    clean_removals  = removals  if isinstance(removals,  list) else []

    if not clean_additions and not clean_removals:
        return {"error": "draft_regimen_change requires at least one addition or removal"}

    bus.emit_sync(
        "PROPOSAL_CREATED",
        {
            "type":      "regimen_change",
            "additions": clean_additions,
            "removals":  clean_removals,
        },
    )
    logger.info(
        "[tool] draft_regimen_change: %d addition(s), %d removal(s), awaiting user approval",
        len(clean_additions),
        len(clean_removals),
    )
    return {
        "success":          True,
        "pending_approval": True,
        "additions_count":  len(clean_additions),
        "removals_count":   len(clean_removals),
        "message": (
            "Regimen change proposal sent to the user for approval. "
            "Do NOT call save_regimen_batch or remove_regimen_item. "
            "Wait for the user to approve or reject."
        ),
    }


def get_regimen_context() -> str:
    """Return the current regimen as a formatted string for narrative / synthesis context."""
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        rows = conn.execute(
            """SELECT supplement_name, dose_value, dose_unit, route, frequency, time_of_day, days_of_week
               FROM user_regimen
               ORDER BY
                   CASE time_of_day
                       WHEN 'morning'   THEN 1
                       WHEN 'midday'    THEN 2
                       WHEN 'afternoon' THEN 3
                       WHEN 'evening'   THEN 4
                       WHEN 'night'     THEN 5
                       ELSE 6
                   END,
                   CASE frequency
                       WHEN 'daily'       THEN 1
                       WHEN 'twice_daily' THEN 2
                       WHEN 'weekly'      THEN 3
                       WHEN 'biweekly'    THEN 4
                       WHEN 'monthly'     THEN 5
                       ELSE 6
                   END,
                   supplement_name"""
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = []
    for name, dv, du, route, freq, tod, days in rows:
        day_str = f" ({days})" if days and freq == "weekly" else ""
        lines.append(f"  {name}: {dv}{du} {route} ({freq}{day_str}) [{tod}]")
    return "Current Regimen:\n" + "\n".join(lines)


def _tool_save_protocol(protocol_name: str, protocol_content: str) -> Dict[str, Any]:
    if not protocol_name.strip():
        return {"error": "protocol_name must not be empty"}
    if not protocol_content.strip():
        return {"error": "protocol_content must not be empty"}
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        conn.execute(
            """INSERT INTO user_protocols (protocol_name, protocol_content, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(protocol_name) DO UPDATE SET
                   protocol_content = excluded.protocol_content,
                   updated_at       = excluded.updated_at""",
            (protocol_name.strip(), protocol_content.strip()),
        )
        conn.commit()
        return {"success": True, "protocol_name": protocol_name.strip(), "action": "saved"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


def _tool_delete_protocol(protocol_name: str) -> Dict[str, Any]:
    if not protocol_name.strip():
        return {"error": "protocol_name must not be empty"}
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        cursor = conn.execute(
            "DELETE FROM user_protocols WHERE protocol_name = ?",
            (protocol_name.strip(),),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"error": f"No protocol named '{protocol_name.strip()}' found"}
        return {"success": True, "protocol_name": protocol_name.strip(), "action": "deleted"}
    finally:
        conn.close()


def get_all_protocols() -> str:
    """Return all user_protocols rows as a formatted string for extractor prompt injection.
    Returns empty string if no protocols are defined.
    """
    conn = _sqlite3.connect(str(_DB_PATH))
    try:
        rows = conn.execute(
            "SELECT protocol_name, protocol_content FROM user_protocols ORDER BY protocol_name"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = [f'- "{name}": {content}' for name, content in rows]
    return "KNOWN PROTOCOLS:\n" + "\n".join(lines)


_VALID_TABS = {"LABS", "BIOMETRICS", "SUPPLEMENTS", "JOURNAL", "KNOWLEDGE"}


def _tool_navigate_ui(
    tab_name: str, search_query: Optional[str] = None
) -> Dict[str, Any]:
    """Emit a UI_COMMAND event that instructs the frontend to navigate to a dashboard tab.

    Uses :meth:`EventBus.emit_sync` because this function is always called from
    ``_execute_ledger_tool``, which runs inside a daemon thread (via
    ``_iter_sync_gen``).  The sync bridge hands the coroutine off to the main
    asyncio event loop so SSE subscribers receive it in real time.

    Parameters
    ----------
    tab_name:
        Target dashboard tab.  Must be one of LABS, BIOMETRICS, SUPPLEMENTS,
        JOURNAL, KNOWLEDGE (case-insensitive; normalised to upper internally).
    search_query:
        Optional string to pre-populate the panel's search/filter box.
        Supported on LABS (filters by marker name or panel name).
    """
    tab_upper = tab_name.upper().strip()
    if tab_upper not in _VALID_TABS:
        return {
            "error": f"Unknown tab '{tab_name}'. Valid: {sorted(_VALID_TABS)}"
        }

    payload: Dict[str, Any] = {"action": "navigate", "tab": tab_upper}
    if search_query:
        payload["query"] = search_query

    bus.emit_sync("UI_COMMAND", payload)
    logger.info(
        "[tool] navigate_ui → tab=%s query=%r", tab_upper, search_query
    )
    return {"success": True, "tab": tab_upper, "query": search_query}


def _execute_ledger_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "query_entries":
        return _tool_query_entries(
            entry_type=str(args.get("entry_type", "")),
            metric_name=str(args.get("metric_name", "")),
            limit=int(args.get("limit", 10)),
        )
    if name == "update_entry":
        return _tool_update_entry(
            entry_type=str(args.get("entry_type", "")),
            entry_id=str(args.get("entry_id", "")),
            field_name=str(args.get("field_name", "")),
            new_value=str(args.get("new_value", "")),
        )
    if name == "delete_entry":
        return _tool_delete_entry(
            entry_type=str(args.get("entry_type", "")),
            entry_id=str(args.get("entry_id", "")),
        )
    if name == "force_synthesis":
        return _tool_force_synthesis()
    if name == "save_regimen_batch":
        items = args.get("items", [])
        if not isinstance(items, list):
            return {"error": "items must be a list"}
        return _tool_save_regimen_batch(items)
    if name == "save_regimen_item":
        return _tool_save_regimen_item(
            supplement_name=str(args.get("supplement_name", "")),
            dose_value=str(args.get("dose_value", "")),
            dose_unit=str(args.get("dose_unit", "")),
            route=str(args.get("route", "oral")),
            frequency=str(args.get("frequency", "daily")),
            time_of_day=str(args.get("time_of_day", "morning")),
            days_of_week=str(args.get("days_of_week", "")),
            site=str(args.get("site", "")),
            notes=str(args.get("notes", "")),
        )
    if name == "remove_regimen_item":
        return _tool_remove_regimen_item(
            supplement_name=str(args.get("supplement_name", "")),
            time_of_day=str(args.get("time_of_day", "")),
        )
    if name == "save_protocol":
        return _tool_save_protocol(
            protocol_name=str(args.get("protocol_name", "")),
            protocol_content=str(args.get("protocol_content", "")),
        )
    if name == "delete_protocol":
        return _tool_delete_protocol(
            protocol_name=str(args.get("protocol_name", "")),
        )
    if name == "navigate_ui":
        return _tool_navigate_ui(
            tab_name=str(args.get("tab_name", "")),
            search_query=args.get("search_query") or None,
        )
    if name == "draft_regimen_change":
        return _tool_draft_regimen_change(
            additions=args.get("additions") or [],
            removals=args.get("removals") or [],
        )
    if name == "web_search":
        return _tool_web_search(
            query=str(args.get("query", "")),
            max_results=int(args.get("max_results", 5)),
        )
    return {"error": f"Unknown tool: {name}"}


def _clinical_safety_settings() -> List[Any]:
    """Return Gemini safety settings appropriate for a clinical health-tracking app.

    DANGEROUS_CONTENT is set to BLOCK_ONLY_HIGH because routine discussions of supplements,
    dosages, and biomarker ranges can score medium on default filters even when entirely legitimate.
    """
    from google.genai import types

    return [
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT",
            threshold="BLOCK_ONLY_HIGH",
        ),
    ]


def _make_ledger_tools() -> List[Any]:
    """Build Gemini Tool objects for function calling. Called lazily to avoid import cost."""
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="query_entries",
            description=(
                "Search the health ledger database for logged entries. "
                "Use this to find specific records. For example, to locate "
                "the UUID of an entry before deleting it, or to look up recent "
                "values for a given metric, supplement, or lab marker."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "entry_type": types.Schema(
                        type="STRING",
                        enum=["biometric", "supplement", "lab", "journal"],
                        description="Type of health entry to search.",
                    ),
                    "metric_name": types.Schema(
                        type="STRING",
                        description=(
                            "Optional partial-match name filter. "
                            "For biometrics: metric name (e.g. 'heart_rate'). "
                            "For supplements: supplement name (e.g. 'vitamin d'). "
                            "For labs: marker name (e.g. 'Vitamin D'). "
                            "Leave empty to return the most recent entries of that type."
                        ),
                    ),
                    "limit": types.Schema(
                        type="INTEGER",
                        description="Maximum number of entries to return. Default 10.",
                    ),
                },
                required=["entry_type"],
            ),
        ),
        types.FunctionDeclaration(
            name="update_entry",
            description=(
                "Update a single field on an existing health ledger entry. "
                "Always call query_entries first to confirm the exact entry_id. "
                "Use this to correct dates, values, units, or names. "
                "Dates must be full ISO 8601 strings (e.g. '2026-05-19T00:00:00+00:00'). "
                "To update multiple fields on the same entry, call this tool once per field."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "entry_type": types.Schema(
                        type="STRING",
                        enum=["biometric", "supplement", "lab", "journal"],
                        description="Type of the entry to update.",
                    ),
                    "entry_id": types.Schema(
                        type="STRING",
                        description="UUID of the entry to update.",
                    ),
                    "field_name": types.Schema(
                        type="STRING",
                        description=(
                            "Field to update. "
                            "lab: collected_at, panel_name, marker_name, value_numeric, unit, "
                            "reference_low, reference_high, notes, flagged. "
                            "biometric: recorded_at, metric_name, value, unit, notes. "
                            "supplement: recorded_at, supplement_name, dose_value, dose_unit, route, site, notes. "
                            "journal: journal_date, mood, energy_score, sleep_hours, notes."
                        ),
                    ),
                    "new_value": types.Schema(
                        type="STRING",
                        description="New value as a string. Dates in ISO 8601. Numbers as decimal strings.",
                    ),
                },
                required=["entry_type", "entry_id", "field_name", "new_value"],
            ),
        ),
        types.FunctionDeclaration(
            name="delete_entry",
            description=(
                "Permanently delete a specific health ledger entry by its UUID. "
                "Always call query_entries first to confirm the exact entry_id. "
                "This action is irreversible."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "entry_type": types.Schema(
                        type="STRING",
                        enum=["biometric", "supplement", "lab", "journal"],
                        description="Type of the entry to delete.",
                    ),
                    "entry_id": types.Schema(
                        type="STRING",
                        description="UUID of the entry to delete.",
                    ),
                },
                required=["entry_type", "entry_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_regimen_batch",
            description=(
                "Save multiple supplements to the standing regimen in ONE call. "
                "ALWAYS use this (not save_regimen_item) when the user lists two or more supplements. "
                "When the user declares a protocol ('here\\'s my stack', 'I take X, Y, and Z', "
                "'here\\'s what I got', 'I switched to...'), call this ONCE with ALL supplements "
                "in the items array. Missing supplements will not be saved. "
                "Defaults: route=oral, frequency=daily, time_of_day=morning."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "items": types.Schema(
                        type="ARRAY",
                        description="Every supplement in the declared protocol, one object per supplement.",
                        items=types.Schema(
                            type="OBJECT",
                            properties={
                                "supplement_name": types.Schema(type="STRING", description="Name of the supplement."),
                                "dose_value":    types.Schema(type="STRING", description="Numeric dose, e.g. '5', '300'."),
                                "dose_unit":     types.Schema(type="STRING", description="Unit: mcg, mg, g, iu, ml."),
                                "route":         types.Schema(
                                    type="STRING",
                                    enum=["oral", "sublingual", "transdermal", "intranasal", "other"],
                                ),
                                "frequency":     types.Schema(
                                    type="STRING",
                                    enum=["daily", "twice_daily", "weekly", "biweekly", "monthly", "as_needed"],
                                ),
                                "time_of_day":   types.Schema(
                                    type="STRING",
                                    enum=["morning", "midday", "afternoon", "evening", "night", "as_needed"],
                                ),
                                "days_of_week":  types.Schema(type="STRING", description="Comma-separated days for weekly items."),
                                "site":          types.Schema(type="STRING", description="Application site if applicable."),
                                "notes":         types.Schema(type="STRING", description="Optional notes."),
                            },
                            required=["supplement_name", "dose_value", "dose_unit", "route", "frequency", "time_of_day"],
                        ),
                    ),
                },
                required=["items"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_regimen_item",
            description=(
                "Add or update a SINGLE supplement in the user's regimen. "
                "Use save_regimen_batch instead when saving two or more supplements. "
                "Use this only for a single-supplement update (e.g. dose change on one item). "
                "Upserts on (supplement_name, time_of_day), so re-running is safe."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "supplement_name": types.Schema(type="STRING", description="Name of the supplement."),
                    "dose_value":    types.Schema(type="STRING", description="Numeric dose amount, e.g. '5', '200'."),
                    "dose_unit":     types.Schema(type="STRING", description="Unit: mcg, mg, g, iu, ml."),
                    "route":         types.Schema(
                        type="STRING",
                        enum=["oral", "sublingual", "transdermal", "intranasal", "other"],
                        description="Administration route.",
                    ),
                    "frequency":     types.Schema(
                        type="STRING",
                        enum=["daily", "twice_daily", "weekly", "biweekly", "monthly", "as_needed"],
                        description="How often the supplement is taken.",
                    ),
                    "time_of_day":   types.Schema(
                        type="STRING",
                        enum=["morning", "midday", "afternoon", "evening", "night", "as_needed"],
                        description="When during the day it is taken.",
                    ),
                    "days_of_week":  types.Schema(type="STRING", description="Comma-separated days for weekly items, e.g. 'Monday,Thursday'."),
                    "site":          types.Schema(type="STRING", description="Application site if applicable."),
                    "notes":         types.Schema(type="STRING", description="Optional notes."),
                },
                required=["supplement_name", "dose_value", "dose_unit", "route", "frequency", "time_of_day"],
            ),
        ),
        types.FunctionDeclaration(
            name="remove_regimen_item",
            description=(
                "Remove a supplement from the user's current regimen. "
                "If time_of_day is omitted, removes all entries for that supplement. "
                "Use when the user says they stopped taking something."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "supplement_name": types.Schema(type="STRING", description="Name of the supplement to remove."),
                    "time_of_day":   types.Schema(type="STRING", description="Optional: only remove the entry at this time of day."),
                },
                required=["supplement_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_protocol",
            description=(
                "Save or update a named protocol alias. A protocol is a shorthand name "
                "(e.g. 'Morning Stack') that maps to a list of supplements with doses. "
                "Once saved, if the user says 'took my Morning Stack', the extractor will "
                "automatically expand it into every constituent supplement log. "
                "Use this when the user defines or updates a recurring supplement stack."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "protocol_name": types.Schema(
                        type="STRING",
                        description="Shorthand name for the protocol, e.g. 'Morning Stack'.",
                    ),
                    "protocol_content": types.Schema(
                        type="STRING",
                        description=(
                            "Comma-separated supplements with doses and routes, "
                            "e.g. '5000iu Vitamin D3 oral, 400mg Magnesium Glycinate oral, 5g Creatine oral'."
                        ),
                    ),
                },
                required=["protocol_name", "protocol_content"],
            ),
        ),
        types.FunctionDeclaration(
            name="delete_protocol",
            description="Permanently delete a named protocol alias by its exact name.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "protocol_name": types.Schema(
                        type="STRING",
                        description="Exact name of the protocol to delete.",
                    ),
                },
                required=["protocol_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="force_synthesis",
            description=(
                "Manually trigger the knowledge graph synthesis engine, bypassing the "
                "automatic daily date gate. Use this when the user asks to run a synthesis, "
                "update the graph, refresh their clinical picture, force a reflection, or "
                "says the knowledge graph is stale. "
                "Takes no arguments. Returns nodes_created, nodes_updated, edges_created. "
                "Expect 10 to 40 seconds of latency; this makes a live Gemini call."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={},
            ),
        ),
        types.FunctionDeclaration(
            name="navigate_ui",
            description=(
                "Navigate the user's dashboard to a specific tab, optionally filtering "
                "the panel's content by a search query. "
                "Call this proactively when the user asks to 'see', 'show', 'check', "
                "'pull up', or 'review' a category of data, switch the tab so the data "
                "is visually in front of them while you respond. "
                "Pair with query_entries to simultaneously retrieve and display the data. "
                "Never narrate the navigation; just call the tool and respond normally."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "tab_name": types.Schema(
                        type="STRING",
                        enum=["LABS", "BIOMETRICS", "SUPPLEMENTS", "JOURNAL", "KNOWLEDGE"],
                        description="Dashboard tab to navigate to.",
                    ),
                    "search_query": types.Schema(
                        type="STRING",
                        description=(
                            "Optional search string to filter the panel content. "
                            "For LABS: filters by marker name or panel name (e.g. 'Vitamin D', 'CBC'). "
                            "Leave empty to show all entries in the tab."
                        ),
                    ),
                },
                required=["tab_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="web_search",
            description=(
                "Search the internet for current clinical information, research, drug interactions, "
                "or any topic that requires up-to-date external knowledge beyond what is in the "
                "health ledger. Returns a factual summary and source URLs. Use proactively when "
                "the user asks about a mechanism, supplement, or guideline that would benefit from "
                "a live web reference. Prefer specific, clinical queries "
                "(e.g. 'magnesium glycinate vs citrate bioavailability' rather than 'magnesium')."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(
                        type="STRING",
                        description="The search query. Be specific and clinical.",
                    ),
                    "max_results": types.Schema(
                        type="INTEGER",
                        description="Number of source URLs to return (1–10, default 5).",
                    ),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="draft_regimen_change",
            description=(
                "Propose a change to the user's standing regimen WITHOUT writing to the database. "
                "This sends an approval card to the UI that the user must APPROVE or REJECT. "
                "REQUIRED: Use this instead of save_regimen_batch or remove_regimen_item "
                "for ANY regimen change, unless the user explicitly says 'force save'. "
                "Include ALL proposed additions and ALL items to remove in a single call."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "additions": types.Schema(
                        type="ARRAY",
                        description=(
                            "Supplements to add or update in the regimen. "
                            "Each item: supplement_name, dose_value, dose_unit, route, "
                            "frequency, time_of_day (required). Optional: site, days_of_week, notes."
                        ),
                        items=types.Schema(
                            type="OBJECT",
                            properties={
                                "supplement_name": types.Schema(type="STRING"),
                                "dose_value":    types.Schema(type="STRING"),
                                "dose_unit": types.Schema(
                                    type="STRING",
                                    enum=["mcg", "mg", "g", "iu", "ml"],
                                ),
                                "route": types.Schema(
                                    type="STRING",
                                    enum=["oral", "sublingual", "transdermal", "intranasal", "other"],
                                ),
                                "frequency": types.Schema(
                                    type="STRING",
                                    enum=["daily", "twice_daily", "weekly",
                                          "biweekly", "monthly", "as_needed"],
                                ),
                                "time_of_day": types.Schema(
                                    type="STRING",
                                    enum=["morning", "midday", "afternoon",
                                          "evening", "night", "as_needed"],
                                ),
                                "days_of_week": types.Schema(type="STRING"),
                                "site":         types.Schema(type="STRING"),
                                "notes":        types.Schema(type="STRING"),
                            },
                            required=["supplement_name", "dose_value", "dose_unit",
                                      "route", "frequency", "time_of_day"],
                        ),
                    ),
                    "removals": types.Schema(
                        type="ARRAY",
                        description="Supplements to remove from the regimen.",
                        items=types.Schema(
                            type="OBJECT",
                            properties={
                                "supplement_name": types.Schema(
                                    type="STRING",
                                    description="Name of the supplement to remove.",
                                ),
                                "time_of_day": types.Schema(
                                    type="STRING",
                                    description=(
                                        "Optional: only remove the entry at this time of day. "
                                        "Omit to remove all entries for this supplement."
                                    ),
                                ),
                            },
                            required=["supplement_name"],
                        ),
                    ),
                },
            ),
        ),
    ])]


# ── Conversational response ────────────────────────────────────────────────────

def _to_gemini_history(history: List[Dict[str, Any]]) -> List[Any]:
    """Convert [{role, content}] rows to alternating Gemini Content objects.

    Gemini requires strictly alternating user/model turns starting with 'user'.
    We drop any leading 'model' entries and silently skip out-of-order messages
    so a corrupt or partially-saved history never crashes the request.
    """
    from google.genai import types as _types

    contents: List[Any] = []
    expected = "user"
    for msg in history:
        if msg["role"] == expected:
            contents.append(
                _types.Content(
                    role=msg["role"],
                    parts=[_types.Part(text=msg["content"])],
                )
            )
            expected = "model" if expected == "user" else "user"
    return contents


def chat_respond(
    user_text: str,
    extracted: Optional[AIExtractionPayload],
    mem0_context: str,
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[Any] = None,
    working_memory_payload: Optional[Dict[str, Any]] = None,
    transcript_context: str = "",
) -> str:
    """Generate a natural-language response using the health companion persona.

    history: list of {role, content} dicts from the chat_history table,
               in chronological order (oldest first). Used to seed Gemini's
               multi-turn chat so the model retains conversational context.
    """
    active_client = client or _get_conversation_client()
    from google.genai import types

    # Build the enriched user prompt for this turn
    graph_context = _load_graph_context()
    narrative = _generate_clinical_narrative(extracted, mem0_context, graph_context, transcript_context)
    prompt = f"{narrative}\n\n[User message]\n{user_text}" if narrative else user_text

    # Seed the chat with prior turns so the model has conversational memory
    gemini_history = _to_gemini_history(history) if history else []

    # Build system prompt: append working memory sensory block when this turn
    # committed new data so the model is mandated to actively analyze it.
    _system_prompt = _build_conversation_prompt()
    if _has_logged_data(working_memory_payload):
        _system_prompt += "\n\n" + _build_working_memory_injection(working_memory_payload)

    chat = active_client.chats.create(
        model=CONVERSATION_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=_system_prompt,
            thinking_config=_conversation_thinking_config(),
            temperature=0.7,
            tools=_make_ledger_tools(),
            safety_settings=_clinical_safety_settings(),
        ),
        history=gemini_history,
    )

    response = chat.send_message(prompt)

    # ── Tool-calling loop ─────────────────────────────────────────────────────
    # Gemini may request multiple rounds of tool calls before producing text.
    # We cap at 5 rounds to avoid infinite loops on misbehaving models.
    _MAX_TOOL_ROUNDS = 5
    _made_tool_calls = False
    try:
        for _round in range(_MAX_TOOL_ROUNDS):
            fn_calls = _get_fn_calls(response)
            if not fn_calls:
                break  # model returned text: done

            _made_tool_calls = True
            names = [fc.name for fc in fn_calls]
            logger.info("[chat_respond] round %d: %s", _round + 1, names)

            tool_parts = []
            for fc in fn_calls:
                fc_args = dict(fc.args or {})
                result = _execute_ledger_tool(fc.name, fc_args)
                logger.info(_compact_tool_log(fc.name, fc_args, result))
                tool_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            response = chat.send_message(tool_parts)
    except Exception as exc:
        logger.warning("[chat_respond] tool loop error: %s", exc)
        return f"[error during tool execution: {exc}]"

    text = _safe_response_text(response).strip()
    logger.debug("[chat_respond] post-tool text length: %d  preview: %r", len(text), text[:120])
    if not text and _made_tool_calls:
        # Model completed tool calls but returned no text. Nudge it once for a
        # brief confirmation rather than surfacing a generic error to the user.
        try:
            response = chat.send_message("Please give a brief confirmation of what was just completed.")
            text = _safe_response_text(response).strip()
        except Exception as exc:
            logger.warning("[chat_respond] nudge failed: %s", exc)
    return text or "I wasn't able to generate a response. Please try again."


# ── Streaming conversational response ─────────────────────────────────────────

def _get_fn_calls(response: Any) -> list:
    """Return function_call objects from a Gemini response.

    The SDK's ``response.function_calls`` property is unreliable after the first
    tool round; it can return an empty list even when the model placed a
    function_call in ``candidates[0].content.parts`` (AFC may consume the
    property, or the SDK caches a stale value).  This helper checks the parts
    directly as the authoritative fallback, matching the SDK's own warning:
    "Check the full candidates.content.parts accessor."
    """
    calls = getattr(response, "function_calls", None)
    if calls:
        return list(calls)
    try:
        parts = response.candidates[0].content.parts or []
        return [p.function_call for p in parts if getattr(p, "function_call", None)]
    except Exception:
        return []


def _finish_reason(response: Any) -> str:
    """Return the finish_reason string from a Gemini response/chunk, or '' if unavailable."""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        fr = getattr(candidates[0], "finish_reason", None)
        return str(fr).upper() if fr else ""
    except Exception:
        return ""


def _safe_response_text(response: Any) -> str:
    """Extract visible text from a Gemini response without triggering SDK warnings.

    Three layers, tried in order:

    1. Parts-based filter: reads ``candidates[0].content.parts`` directly and
       keeps only *visible* text parts (no function_call, no thought/thinking).
       This avoids both the "non-text parts" WARNING and the raw thinking monologue
       that would otherwise confuse ``_clean_response_text``.

    2. SDK accessor fallback: if the parts filter finds no text (e.g. the
       response has only thinking parts or the candidates structure is unusual),
       fall back to ``response.text``.  This may log a SDK WARNING when
       function_call parts are still present, but it prevents a silent empty
       response in every other edge case.

    3. Hard fallback: on any exception (missing candidates, None content, etc.)
       return whatever ``response.text`` gives us, or the empty string.
    """
    try:
        parts = response.candidates[0].content.parts or []
        # Exclude function_call parts (trigger SDK warning on .text access) and
        # thought/thinking parts (internal monologue, must not appear in the
        # user-facing reply and must not be fed to _clean_response_text which
        # would misidentify thinking content as function-echo artifacts).
        visible = [
            p.text for p in parts
            if getattr(p, "text", None)
            and not getattr(p, "function_call", None)
            and not getattr(p, "thought", False)
        ]
        if visible:
            return "".join(visible)
        # No visible parts found: fall back to SDK accessor.
        # It handles edge cases (thinking-only responses, safety blocks, etc.)
        # and has its own heuristics for extracting whatever text exists.
        return getattr(response, "text", None) or ""
    except Exception as exc:
        logger.debug("[_safe_response_text] parts accessor failed (%s); using response.text fallback", exc)
        return getattr(response, "text", None) or ""


def _get_visible_chunk_text(chunk: Any) -> str:
    """Extract only visible text from a streaming chunk, filtering native thought parts.

    Gemini 2.5 Pro/Flash have native thinking enabled by default.  In streaming mode
    the SDK includes thought-part text in ``chunk.text``, which causes raw reasoning
    to appear in the user-facing reply without any ``<think>`` wrapper.  This helper
    mirrors the filtering logic in ``_safe_response_text`` for individual streaming
    chunks so only non-thought, non-function-call text reaches the frontend.
    """
    try:
        candidates = getattr(chunk, "candidates", None)
        if not candidates:
            return ""
        parts = getattr(candidates[0].content, "parts", None) or []
        visible = [
            p.text for p in parts
            if getattr(p, "text", None) is not None
            and not getattr(p, "function_call", None)
            and not getattr(p, "thought", False)
        ]
        return "".join(visible)
    except Exception:
        # If the parts API fails, return empty rather than falling back to chunk.text
        # (which may include raw thought content and leak it to the user).
        return ""


def _compact_tool_log(name: str, args: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Return a single-line human-readable summary of a tool call and its result.

    Replaces the raw ``-> {result}`` dump that was flooding the log with
    full table rows.  Errors always surface verbatim so they are never hidden.
    """
    err = result.get("error")
    if err:
        return f"  {name} → ERROR: {err}"

    if name == "query_entries":
        entry_type = args.get("entry_type", "?")
        metric     = args.get("metric_name") or ""
        limit      = args.get("limit", "")
        qualifier  = f" metric={metric!r}" if metric else ""
        qualifier += f" limit={limit}" if limit else ""
        count      = result.get("count", "?")
        return f"  query_entries({entry_type}{qualifier}) → {count} rows"

    if name == "update_entry":
        field = result.get("field", "?")
        val   = str(result.get("new_value", ""))
        val   = val[:60] + "…" if len(val) > 60 else val
        uid   = str(result.get("updated_id", ""))[:8]
        return f"  update_entry({field}={val!r}) id={uid}… → ok"

    if name == "delete_entry":
        uid = str(result.get("deleted_id", ""))[:8]
        etype = result.get("entry_type", "?")
        return f"  delete_entry({etype}) id={uid}… → ok"

    if name == "navigate_ui":
        tab   = result.get("tab", "?")
        query = result.get("query") or ""
        return f"  navigate_ui → {tab}" + (f" query={query!r}" if query else "")

    if name == "save_regimen_batch":
        saved  = result.get("saved", [])
        errors = result.get("errors", [])
        names  = ", ".join(saved[:4]) + ("…" if len(saved) > 4 else "")
        suffix = f"  ({len(errors)} errors)" if errors else ""
        return f"  save_regimen_batch → {len(saved)} saved [{names}]{suffix}"

    if name == "save_regimen_item":
        cname = args.get("supplement_name", "?")
        dose  = f"{args.get('dose_value', '')} {args.get('dose_unit', '')}".strip()
        return f"  save_regimen_item({cname} {dose}) → ok"

    if name == "remove_regimen_item":
        cname = args.get("supplement_name", "?")
        return f"  remove_regimen_item({cname}) → ok"

    if name in ("save_protocol", "delete_protocol"):
        pname  = result.get("protocol_name", "?")
        action = result.get("action", name)
        return f"  {name}({pname}) → {action}"

    if name == "force_synthesis":
        nodes = result.get("nodes_created", 0) + result.get("nodes_updated", 0)
        edges = result.get("edges_created", 0)
        return f"  force_synthesis → {nodes} nodes, {edges} edges"

    if name == "draft_regimen_change":
        add = result.get("additions_count", 0)
        rem = result.get("removals_count", 0)
        return f"  draft_regimen_change → {add} addition(s), {rem} removal(s) (pending user approval)"

    # Generic fallback for any future tools
    success = result.get("success", "?")
    return f"  {name} → success={success}"


# ── Leaked-thinking detector ───────────────────────────────────────────────────
# Patterns that identify a paragraph as reasoning/planning that leaked outside
# <think> tags.  Two-stage:
#   Gate: if the FIRST paragraph matches any gate pattern, we enter strip mode.
#   Scan: we then walk forward and return everything from the first paragraph
#           that matches NONE of the patterns (the real response starts there).
# The gate keeps false-positive risk near zero for clean responses.

_LEAKED_PARA_RE: tuple = (
    # "The user is/wants/has/needs/asked/said/expressed..." direct analysis
    re.compile(r'^the user \b', re.IGNORECASE),
    # "My plan is to..." / "My plan for this response..."
    re.compile(r'^my plan\b', re.IGNORECASE),
    # "I need/should/will/must to..." first-person planning
    re.compile(r'^i (need|should|will|must) to\b', re.IGNORECASE),
    # "Let me think/analyze/consider/address/reconsider..."
    re.compile(r'^let me (think|analyze|consider|address|break|reconsider)\b', re.IGNORECASE),
    # "Analyzing/Considering/Thinking about/Evaluating this/the..."
    re.compile(r'^(analyzing|considering|thinking about|evaluating) (this|the)\b', re.IGNORECASE),
    # Multi-word action-plan labels: "Acknowledge and Validate: ...", "Re-evaluate the Compromise: ..."
    # Hyphens allowed in words (Re-evaluate, Cross-check, etc.).
    # Requires 2+ words before the colon (so single-word labels like "Note:" don't fire).
    # Requires a capital letter after ": " (excludes "Heart Rate: 67bpm").
    re.compile(r'^[A-Z][a-z-]+(?:\s+[A-Za-z-]+){1,5}:\s+[A-Z]'),
    # Numbered planning steps: "1. Acknowledge...", "2. Re-evaluate..."
    re.compile(r'^\d+\.\s+[A-Z]'),
    # "Final Check:", "Final Question:", "Final Note:", "Final Step:"
    re.compile(r'^final\s+(check|question|note|step|thought|answer)\b', re.IGNORECASE),
)


def _strip_leaked_thinking(text: str) -> str:
    """Remove leaked reasoning/planning paragraphs prepended to a response.

    The model occasionally writes out its internal planning as plain text before
    the actual reply, e.g.:
        "The user is rejecting a split dosing schedule..."   ← leaked
        "Acknowledge and Validate: Start by agreeing..." ← leaked
        "Understood. That's a hard stop."              ← real response

    Algorithm:
      1. Check the FIRST paragraph against the gate patterns.  If it's clean,
         return immediately; hot-path cost is one regex match.
      2. Scan forward until the first paragraph that matches NONE of the patterns.
         Return everything from that paragraph onward.
      3. If no clean paragraph is found, return the original text unchanged;
         better to surface leaked thinking than to silently return empty string.
    """
    paragraphs = [p for p in text.split('\n\n') if p.strip()]
    if not paragraphs:
        return text

    first = paragraphs[0].strip()
    if not any(rx.match(first) for rx in _LEAKED_PARA_RE):
        return text  # fast exit: first paragraph is clean, nothing to strip

    # First paragraph is leaked thinking; scan for the real response start
    for i, para in enumerate(paragraphs):
        para_s = para.strip()
        if not any(rx.match(para_s) for rx in _LEAKED_PARA_RE):
            real = '\n\n'.join(paragraphs[i:]).strip()
            if real:
                logger.warning(
                    "[stream] stripped %d leaked-thinking paragraph(s) before response", i,
                )
                return real

    # All paragraphs matched a leaked-thinking pattern (fail-safe: don't return empty
    logger.warning("[stream] leaked thinking spans entire response; returning original text")
    return text


def _clean_response_text(text: str) -> str:
    """Strip Gemini 2.5 Pro function-echo artifacts and normalize think tags.

    After tool calls, Gemini 2.5 Pro sometimes prefixes its response with the raw
    function-response JSON it received (e.g. "response:query_entries{result:{...}}").
    Uses brace-counting to find the exact end of each JSON blob, so this works
    regardless of whether there's a blank line or <think> tag after the echo.
    Also strips <tool_code> pseudo-code echoes and normalizes <thought> → <think>.
    """
    text = text.replace('<thought>', '<think>').replace('</thought>', '</think>')

    # Strip <tool_code> blocks: model sometimes echoes function calls as pseudo-code
    text = re.sub(r'<tool_code[^>]*>[\s\S]*?</tool_code>', '', text).strip()

    content = text.lstrip()

    # Nothing to strip if text doesn't start with a function-response echo
    if not (content.startswith('response:') or content.startswith('nodemailer:')):
        return _strip_leaked_thinking(text.strip())

    # Strip all leading function-echo blocks using brace-counting.
    # Blocks look like: [nodemailer:] response:funcname{...json...}
    while True:
        content = re.sub(r'^nodemailer:\s*', '', content)
        if not content.startswith('response:'):
            break
        brace_idx = content.find('{')
        if brace_idx == -1:
            break
        content = content[brace_idx:]
        depth = 0
        end = -1
        for i, ch in enumerate(content):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            break
        content = content[end + 1:].lstrip()

    result = content.strip()
    # Return empty string when the echo stripped to nothing; callers handle "" by nudging
    # the model. Do NOT fall back to text.strip() here; that re-surfaces the echo.
    # Apply leaked-thinking stripping as a final pass, but only when there's text to examine.
    return _strip_leaked_thinking(result) if result else result


def chat_respond_stream(
    user_text: str,
    extracted: Optional[AIExtractionPayload],
    mem0_context: str,
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[Any] = None,
    working_memory_payload: Optional[Dict[str, Any]] = None,
    transcript_context: str = "",
) -> Generator[Tuple[str, str], None, None]:
    """Streaming variant of chat_respond.

    Yields (type, payload) tuples:
      ("status", message)  # pipeline/tool status events
      ("chunk",  text)     # response text fragments

    For the first API call (no tool calls needed) uses real SDK streaming.
    After tool rounds, fake-streams the accumulated text from memory so the
    <think> parser works identically in both paths.
    """
    active_client = client or _get_conversation_client()
    from google.genai import types

    logger.info("[stream] conversation model: %s", CONVERSATION_MODEL)

    graph_context = _load_graph_context()
    narrative = _generate_clinical_narrative(extracted, mem0_context, graph_context, transcript_context)
    prompt = f"{narrative}\n\n[User message]\n{user_text}" if narrative else user_text

    gemini_history = _to_gemini_history(history) if history else []

    # Build system prompt: append working memory sensory block when this turn
    # committed new data so the model is mandated to actively analyze it.
    _system_prompt = _build_conversation_prompt()
    if _has_logged_data(working_memory_payload):
        _system_prompt += "\n\n" + _build_working_memory_injection(working_memory_payload)

    _log_context_budget(
        system_prompt=_system_prompt,
        narrative=narrative,
        prompt=prompt,
        graph_context=graph_context,
        transcript_context=transcript_context,
        mem0_context=mem0_context,
        history=history,
        user_text=user_text,
    )

    chat = active_client.chats.create(
        model=CONVERSATION_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=_system_prompt,
            thinking_config=_conversation_thinking_config(),
            temperature=0.7,
            tools=_make_ledger_tools(),
            safety_settings=_clinical_safety_settings(),
        ),
        history=gemini_history,
    )

    # ── Phase 1: buffered streaming on the first call ────────────────────────
    # Chunks are buffered rather than yielded immediately.  This lets us detect
    # whether the response is a tool-call preamble BEFORE any text reaches the
    # user.  Preambles are silently discarded; real responses are fake-streamed
    # after the buffer is inspected.  If the model calls tools directly (the
    # common case with [SILENT TOOL USE] in the prompt), no text is buffered at
    # all and we fall straight through to Phase 2.
    logger.info("[stream] Phase 1 start: waiting for model response")
    last_chunk = None
    tool_loop_response = None  # set before Phase 2 via normal or preamble path
    streamed_text: list[str] = []
    try:
        for chunk in chat.send_message_stream(prompt):
            # Skip function-call chunks entirely: the SDK emits a WARNING when .text
            # is accessed on a response whose parts are non-text (function_call).
            # Use _get_visible_chunk_text to filter out native thought parts (Gemini 2.5
            # thinking) so raw reasoning never leaks into the buffered streamed_text.
            if not getattr(chunk, "function_calls", None):
                visible = _get_visible_chunk_text(chunk)
                if visible:
                    streamed_text.append(visible)
                    # Do NOT yield here; buffer until we know whether it's a preamble.
            last_chunk = chunk
    except Exception as exc:
        logger.warning("[stream] Phase 1 stream error: %s", exc)
        yield ("chunk", f"[stream error: {exc}]")
        return

    _log_actual_usage(last_chunk, label="first-call")
    fn_calls = _get_fn_calls(last_chunk) if last_chunk else []
    buffered_chars = sum(len(t) for t in streamed_text)

    if not fn_calls:
        if not streamed_text:
            # ── One-shot retry on empty stream ────────────────────────────────
            # Gemini occasionally returns an empty response on the first call
            # (transient API issue).  Rebuild a fresh chat session and try once
            # more before surfacing the fallback error without history contamination
            # because the empty response never had any actual content to add.
            logger.warning("[stream] Phase 1 empty: no text and no tool calls, retrying with fresh session")
            yield ("status", "Retrying…")
            try:
                _retry_client = _get_conversation_client()
                _retry_chat   = _retry_client.chats.create(
                    model=CONVERSATION_MODEL,
                    config=types.GenerateContentConfig(
                        system_instruction=_system_prompt,
                        thinking_config=_conversation_thinking_config(),
                        temperature=0.7,
                        tools=_make_ledger_tools(),
                        safety_settings=_clinical_safety_settings(),
                    ),
                    history=gemini_history,
                )
                _retry_text:  list[str] = []
                _retry_last:  Any       = None
                for _rc in _retry_chat.send_message_stream(prompt):
                    if not getattr(_rc, "function_calls", None):
                        _visible = _get_visible_chunk_text(_rc)
                        if _visible:
                            _retry_text.append(_visible)
                    _retry_last = _rc
                if _retry_text or (_retry_last and _get_fn_calls(_retry_last)):
                    logger.info("[stream] retry succeeded (%d chars)", sum(len(t) for t in _retry_text))
                    streamed_text = _retry_text
                    last_chunk    = _retry_last
                    chat          = _retry_chat          # use fresh session for Phase 2
                    fn_calls      = _get_fn_calls(last_chunk) if last_chunk else []
                else:
                    logger.warning("[stream] retry also empty: giving up")
            except Exception as _re:
                logger.warning("[stream] Phase 1 retry error: %s", _re)

            if not streamed_text and not fn_calls:
                fr = _finish_reason(last_chunk) if last_chunk else ""
                logger.warning("[stream] Phase 1 empty after retry: finish_reason=%s", fr or "unknown")
                if "SAFETY" in fr:
                    yield ("chunk", "Gemini flagged that response. Try rephrasing.")
                else:
                    yield ("chunk", "I wasn't able to generate a response. Please try again.")
                return

        # Safety net: if the model produced a short preamble without querying data,
        # nudge it to follow through. Signals: short visible text + deferral phrase.
        # Strip <think>...</think> before measuring; the reasoning chain can be 400+
        # chars while the human-readable preamble is only ~180, pushing the total past
        # the threshold and causing the else-return to silently drop the response.
        accumulated = "".join(streamed_text)
        visible = re.sub(r'<think>.*?</think>', '', accumulated, flags=re.DOTALL).strip()
        _DEFERRAL_SIGNALS = ("shortly", "in a moment", "i'll ", "i will ", "let me ",
                             "pulling", "fetching", "give you a full", "analyzing",
                             "i've just pulled", "i just pulled", "have the analysis",
                             "look that up", "check that", "one moment")
        is_preamble = (
            len(visible) < 350
            and any(s in visible.lower() for s in _DEFERRAL_SIGNALS)
        )
        if is_preamble:
            # Preamble buffered but NOT shown: silently discard and nudge.
            logger.info("[stream] Phase 1 preamble detected (%d visible chars), discarding, nudging", len(visible))
            yield ("status", "Following up...")
            try:
                followup = chat.send_message(
                    "Go ahead: call query_entries now and give the full analysis."
                )
                fn_calls = _get_fn_calls(followup)
                if fn_calls:
                    # Rewire: treat followup as the entry point for Phase 2
                    logger.info("[stream] nudge produced tool calls: %s (entering Phase 2)", [fc.name for fc in fn_calls])
                    tool_loop_response = followup
                    # Don't return; fall through to Phase 2 below
                else:
                    text = _safe_response_text(followup).strip()
                    if text:
                        logger.info("[stream] nudge produced direct text (%d chars)", len(text))
                        for token in re.split(r"(\s+)", text):
                            if token:
                                yield ("chunk", token)
                    else:
                        logger.warning("[stream] nudge produced no text")
                        yield ("chunk", "I wasn't able to retrieve that data. Please try asking again.")
                    return
            except Exception as exc:
                logger.warning("[stream] nudge error: %s", exc)
                yield ("chunk", f"[stream error: {exc}]")
                return
        else:
            # Real response: buffer was never a preamble.
            # Run through _clean_response_text so any <think> blocks the model
            # included in a no-tool-call response are stripped before the user
            # sees them, exactly as Phase 3 does for post-tool responses.
            clean = _clean_response_text(accumulated)
            logger.info("[stream] Phase 1 direct response (%d buffered chars → %d after clean)", buffered_chars, len(clean))
            if not clean:
                logger.warning("[stream] Phase 1 text cleaned to empty: nudging for response")
                yield ("status", "Following up…")
                try:
                    followup = chat.send_message("Please give a brief response to the user's message.")
                    clean = _clean_response_text(_safe_response_text(followup).strip())
                except Exception as exc:
                    logger.warning("[stream] Phase 1 nudge failed: %s", exc)
            if not clean:
                logger.warning("[stream] Phase 1 empty after nudge: returning fallback")
                yield ("chunk", "I wasn't able to generate a response. Please try again.")
                return
            for token in re.split(r"(\s+)", clean):
                if token:
                    yield ("chunk", token)
            return

    # ── Phase 2: tool-calling loop (non-streaming) ────────────────────────────
    logger.info("[stream] Phase 2 start: initial tool calls: %s", [fc.name for fc in fn_calls])
    response = tool_loop_response if tool_loop_response is not None else last_chunk
    _MAX_TOOL_ROUNDS = 5
    _rounds_run = 0
    try:
        for _round in range(_MAX_TOOL_ROUNDS):
            fn_calls = _get_fn_calls(response)
            if not fn_calls:
                break

            names = [fc.name for fc in fn_calls]
            logger.info("[stream] round %d: %s", _round + 1, names)
            tool_parts = []
            for fc in fn_calls:
                fc_args = dict(fc.args or {})
                result = _execute_ledger_tool(fc.name, fc_args)
                logger.info(_compact_tool_log(fc.name, fc_args, result))
                yield ("status", f"Tool: {fc.name}")
                tool_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            response = chat.send_message(tool_parts)
            _rounds_run += 1
    except Exception as exc:
        logger.warning("[stream] Phase 2 error after %d round(s): %s", _rounds_run, exc)
        yield ("chunk", f"[error during tool execution: {exc}]")
        return

    logger.info("[stream] Phase 2 complete: %d round(s) run", _rounds_run)
    _log_actual_usage(response, label=f"final ({_rounds_run} tool round(s))")

    text = _clean_response_text(_safe_response_text(response))
    if not text:
        # Model consumed the tool results in native thinking without writing visible text.
        # Nudge it to produce a written reply and DISABLE tools so it cannot escape
        # into another function call instead of responding.
        logger.warning("[stream] post-tool text empty: sending text-only nudge")
        try:
            response = chat.send_message(
                "All data has been retrieved. Write your full response to the user now. "
                "Do not call any more functions.",
                config=types.GenerateContentConfig(
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.NONE
                        )
                    )
                ),
            )
            text = _clean_response_text(_safe_response_text(response).strip())
        except Exception as exc:
            logger.warning("[stream] nudge failed: %s", exc)

    if not text:
        fr = _finish_reason(response)
        logger.warning("[stream] response empty after nudge: finish_reason=%s", fr or "unknown")
        if "SAFETY" in fr:
            yield ("chunk", "Gemini flagged that response. Try rephrasing.")
        else:
            yield ("chunk", "I wasn't able to generate a response. Please try again.")
        return

    # ── Phase 3: fake-stream the accumulated post-tool text ───────────────────
    logger.info("[stream] Phase 3: streaming %d char response", len(text))
    for token in re.split(r"(\s+)", text):
        if token:
            yield ("chunk", token)


# ── File-import interpretation (no tools) ─────────────────────────────────────

_IMPORT_INTERPRETATION_PROMPT = """
You are LEDGER.

A file was just imported and committed. The structured data below is the COMPLETE
and CURRENT ground truth for this analysis. Do NOT reference prior conversations, previous lab
results, or any values not listed in the import summary you were given. Every number you cite
must come directly from that import, with no exceptions.

Analyze what was just committed:
- Interpret each value against known healthy ranges (cite mechanism, not just "normal"/"abnormal")
- Flag anything that warrants attention and explain why
- Identify patterns across the panel (lipid ratios, CBC differentials, metabolic markers, etc.)
- Connect values to each other where relevant

Depth scales with signal. Write flowing prose, not bullets.

REASONING STRUCTURE: MANDATORY:
Use a <think>...</think> block for your internal analysis BEFORE writing your response.
The <think> block is rendered collapsed; everything inside it is private reasoning.
The visible interpretation MUST be written AFTER the closing </think> tag, never inside it.

CORRECT STRUCTURE (follow exactly):
  <think>
  [All reasoning: range checks, mechanism links, pattern identification, flag prioritization.
   None of this appears to the user.]
  </think>

  [Your written interpretation addressed directly to the user starts here: no preamble,
   no "Here's my analysis:", no meta-narration. This is what the user reads.]

STRUCTURAL VIOLATIONS that will break the response:
  ✗  Putting the entire interpretation inside <think>...</think>
  ✗  Writing "Here's a thinking process that could lead to the user-facing response:"
  ✗  Ending with </think> and writing nothing after it
""".strip()


def import_interpret_stream(
    filename: str,
    extracted: Optional[AIExtractionPayload],
    committed: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[Any] = None,
) -> Generator[Tuple[str, str], None, None]:
    """Stream an interpretation of a file import WITHOUT ledger tool calls.

    The data was just committed by the caller, so we pass a structured summary
    of exactly what was imported rather than asking the model to re-query it.
    This avoids the Gemini function-response echo bug and eliminates the extra
    API round trips that tool calls add (typically 30–90 s each).

    Yields ("status", msg) and ("chunk", text) tuples.
    """
    active_client = client or _get_conversation_client()
    from google.genai import types as _gt

    # ── Build concise import summary ──────────────────────────────────────────
    lines: List[str] = [f"[File import: {filename}]"]

    has_data = extracted and any([
        extracted.supplements, extracted.biometrics,
        extracted.labs, extracted.journals,
    ])

    if has_data:
        assert extracted is not None  # type narrowing
        for b in extracted.biometrics:
            unit_str = f" {b.unit}" if b.unit else ""
            lines.append(f"Biometric: {b.metric_name} = {b.value}{unit_str}")

        for lab in extracted.labs:
            val = (
                f"{lab.value_numeric} {lab.unit or ''}".strip()
                if lab.value_numeric is not None
                else lab.value_text or "?"
            )
            ref = ""
            if lab.reference_low is not None and lab.reference_high is not None:
                ref = f" (ref {lab.reference_low}–{lab.reference_high})"
            flagged = " ⚑ FLAGGED" if lab.flagged else ""
            panel = f" [{lab.panel_name}]" if lab.panel_name else ""
            date_str = lab.collected_at.strftime("%Y-%m-%d") if lab.collected_at else "unknown date"
            lines.append(f"Lab{panel} ({date_str}): {lab.marker_name} = {val}{ref}{flagged}")

        for c in extracted.supplements:
            lines.append(
                f"Supplement: {c.supplement_name} {c.dose_value} {c.dose_unit} ({c.route})"
            )

        for j in extracted.journals:
            mood_str = f", {j.mood.value}" if j.mood else ""
            lines.append(f"Journal{mood_str}: {j.notes[:200]}")
    else:
        lines.append("No structured health data was extracted from this file.")

    if committed is not None:
        inserted = {k: v for k, v in committed.items() if k != "skipped"}
        total_inserted = sum(v for v in inserted.values() if isinstance(v, (int, float)))
        if total_inserted > 0:
            counts = ", ".join(f"{k}: {v}" for k, v in inserted.items() if v)
            lines.append(f"Committed to ledger: {counts}")
        else:
            skipped_dict = committed.get("skipped", {})
            total_skipped = sum(skipped_dict.values()) if isinstance(skipped_dict, dict) else 0
            if total_skipped > 0:
                lines.append(
                    f"Note: All {total_skipped} entries were already in the ledger "
                    "(previously imported; 0 new rows written this time)."
                )
            else:
                lines.append("Note: No data was committed to the ledger.")

    prompt = (
        "\n".join(lines)
        + "\n\nAnalyze and interpret the imported data. Base your analysis solely "
        "on the data listed above. For each value, state the collection date explicitly. "
        "If the data is from months ago, make that clear; do not present old results as current."
    )

    gemini_history = _to_gemini_history(history) if history else []

    # No tools: the data is in the prompt, so no tool round trips needed.
    # Use the import-specific prompt (no tool-call instructions, no cross-session refs).
    chat = active_client.chats.create(
        model=CONVERSATION_MODEL,
        config=_gt.GenerateContentConfig(
            system_instruction=_IMPORT_INTERPRETATION_PROMPT,
            temperature=0.7,
        ),
        history=gemini_history,
    )

    yield ("status", "Interpreting imported data...")
    try:
        for chunk in chat.send_message_stream(prompt):
            visible = _get_visible_chunk_text(chunk)
            if visible:
                normalized = (
                    visible
                    .replace("<thought>", "<think>")
                    .replace("</thought>", "</think>")
                )
                yield ("chunk", normalized)
    except Exception as exc:
        yield ("error", f"Interpretation error: {exc}")

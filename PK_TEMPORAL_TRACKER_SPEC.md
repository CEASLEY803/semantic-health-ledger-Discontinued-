# LEDGER: Pharmacokinetic Temporal Tracker Spec
## Morning Reflection Engine Upgrade

---

## IMPORTANT: Differences From the Original Spec

Before you read the implementation steps, here is why this spec differs from an earlier version
you may have seen. Four bugs were caught in review that would have caused silent failures or
hard crashes at runtime. Each difference is called out below with a short reason.

---

### Change 1: `is_archived` column instead of `confidence_level = 'archived'`

**Original spec said:** "Issue an `UPDATE_NODE` to change the `confidence_level` to `'archived'`
once a Temporal Node hits its final milestone."

**Why that breaks:** The `clinical_nodes` table has a SQLite CHECK constraint locking
`confidence_level` to exactly three values: `'hypothesis'`, `'testing'`, `'confirmed'`. SQLite
enforces this at the row level, so writing `'archived'` throws an `IntegrityError` and the update
is rejected. This would silently fail (or crash, depending on error handling).

**What to do instead:** Add a dedicated `is_archived INTEGER NOT NULL DEFAULT 0` column.
The AI sets `is_archived = 1` via `UPDATE_NODE` when a node is fully cleared. This avoids
touching the CHECK constraint entirely.

---

### Change 2: `UPDATE_NODE` executor must be extended

**Original spec said:** Add `last_surfaced_date` to the schema and instruct the AI to issue
an `UPDATE_NODE` to write it after surfacing a milestone.

**Why that breaks:** The `UPDATE_NODE` executor in `reflection_worker.py` is hardcoded:

```python
conn.execute(
    """UPDATE clinical_nodes
       SET summary_text = ?, confidence_level = ?, last_updated = datetime('now')
       WHERE concept_name = ?""",
    (cmd["summary_text"], cmd["confidence_level"], cmd["concept_name"]),
)
```

Even after `last_surfaced_date` exists in the schema, the AI issuing `UPDATE_NODE` with that
field would have it silently ignored because the column is never referenced in the SQL. The anti-spam
lock would never actually write, so the AI would resurface the same milestone every morning.

**What to do instead:** Refactor the `UPDATE_NODE` executor to build its SET clause
dynamically from whichever fields are present in the command dict (see step 2 below).

---

### Change 3: `CREATE_NODE` executor must accept `expires_at`

**Original spec said:** The AI creates a Temporal Node when a supplement is dropped.

**Why that breaks:** The `CREATE_NODE` executor only writes four fields:
`concept_name`, `category`, `summary_text`, `confidence_level`. There is no mechanism for
the AI to encode the `expires_at` date into the row at creation time because the INSERT statement
doesn't reference it.

**What to do instead:** Extend the `CREATE_NODE` command schema and INSERT to accept an
optional `expires_at` field (TEXT, ISO 8601 date string).

---

### Change 4: `ClinicalNode` Pydantic model must reflect new columns

**Original spec said:** Only modify `init_storage.py` and `reflection_worker.py`.

**Why that's incomplete:** `ClinicalNode` in `models.py` is the Pydantic model that serializes
node rows for API responses (the Knowledge Graph panel in the UI reads from this). If the DB
has columns that aren't on the model, those fields are silently dropped on every API response.
The UI would never see `expires_at` or `last_surfaced_date`.

**What to do instead:** Add the three new fields to `ClinicalNode` in `models.py`.

---
---

## Full Implementation Spec

---

### 1. Database Upgrade: `init_storage.py` and `models.py`

#### `init_storage.py`

Add a new migration function following the exact pattern of `migrate_chat_session_id()`.
Add it directly below that function:

```python
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
```

Then call `migrate_clinical_nodes_temporal()` from wherever `migrate_chat_session_id()` is
called (the FastAPI lifespan in `api.py`).

#### `models.py`

Add three new optional fields to `ClinicalNode`:

```python
class ClinicalNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[str] = None
    concept_name: str
    category: str
    summary_text: str
    confidence_level: str
    last_updated: Optional[str] = None
    expires_at: Optional[str] = None            # ← new
    last_surfaced_date: Optional[str] = None    # ← new
    is_archived: int = 0                        # ← new
```

---

### 2. The PK Engine: `reflection_worker.py`

#### 2a. Extend the system prompt

Add a new section to `_SYNTHESIS_SYSTEM_PROMPT` after the existing Rules block:

```
[TEMPORAL TRACKING & PHARMACOKINETICS]

When the user mentions stopping, dropping, or discontinuing a supplement with a meaningful
half-life, you MUST create a Temporal Tracking Node using CREATE_NODE with:
  - concept_name: "[TRACKING] <Supplement Name> Clearance"
  - category: "pharmacokinetics"
  - confidence_level: "confirmed"
  - expires_at: the ISO date of estimated full clearance (5 half-lives from discontinuation)
  - summary_text: a structured entry containing:
      * Date discontinued
      * Half-life of the supplement (in days)
      * Estimated date of 50% clearance
      * Estimated date of 90% clearance (≈ 3.32 half-lives)
      * Estimated date of full clearance (≈ 5 half-lives)
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
```

#### 2b. Extend the `_fmt_nodes` formatter

Update `_fmt_nodes()` to show temporal fields so the AI can see them in the prompt:

```python
def _fmt_nodes(rows: List[Dict]) -> str:
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        if r.get("is_archived"):
            continue  # exclude archived nodes from the prompt entirely
        conf = (r.get("confidence_level") or "hypothesis").upper()
        temporal = ""
        if r.get("expires_at"):
            temporal = f" | expires={r['expires_at']}"
            if r.get("last_surfaced_date"):
                temporal += f" last_surfaced={r['last_surfaced_date']}"
        lines.append(
            f"  [{conf}] {r.get('concept_name')} ({r.get('category')}): "
            f"{r.get('summary_text')}{temporal}"
        )
    return "\n".join(lines)
```

Also update the node SELECT query in `run_morning_synthesis()` to include the new columns:

```python
nodes = [dict(r) for r in conn.execute(
    "SELECT id, concept_name, category, summary_text, confidence_level, "
    "expires_at, last_surfaced_date, is_archived "
    "FROM clinical_nodes ORDER BY last_updated DESC",
).fetchall()]
```

#### 2c. Extend the `CREATE_NODE` executor

Update the `CREATE_NODE` branch to optionally write `expires_at`:

```python
if command == "CREATE_NODE":
    conn.execute(
        """INSERT INTO clinical_nodes
               (concept_name, category, summary_text, confidence_level,
                expires_at, last_updated)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(concept_name) DO UPDATE SET
               category          = excluded.category,
               summary_text      = excluded.summary_text,
               confidence_level  = excluded.confidence_level,
               expires_at        = excluded.expires_at,
               last_updated      = excluded.last_updated""",
        (
            cmd["concept_name"],
            cmd["category"],
            cmd["summary_text"],
            cmd["confidence_level"],
            cmd.get("expires_at"),  # optional, None if not a temporal node
        ),
    )
    nodes_created += 1
```

#### 2d. Refactor the `UPDATE_NODE` executor to be dynamic

Replace the hardcoded UPDATE with a dynamic SET clause builder:

```python
elif command == "UPDATE_NODE":
    # Map allowed command keys to their column names
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
        logger.warning(f"[synthesis] UPDATE_NODE for '{cmd.get('concept_name')}' had no updatable fields, skipped")
    else:
        set_parts.append("last_updated = datetime('now')")
        params.append(cmd["concept_name"])
        conn.execute(
            f"UPDATE clinical_nodes SET {', '.join(set_parts)} WHERE concept_name = ?",
            params,
        )
        nodes_updated += 1
```

---

### 3. Expected Runtime Behavior

Once implemented, a typical flow looks like this:

1. User says: *"I dropped Supplement X today."*
2. Flash extraction logs a supplement entry. During that same chat turn, or the next morning
   synthesis, Gemini detects the discontinuation and issues a `CREATE_NODE` for
   `[TRACKING] Supplement X Clearance` with `expires_at` set ~42 days out (7 × 6-day half-life).
3. Every morning, the Reflection Engine loads the node, sees it in the prompt with its
   `expires_at` and `last_surfaced_date`, calculates the current clearance percentage,
   and stays silent unless today is a milestone day.
4. On day ~6 (50% clearance): surfaces a briefing insight, immediately issues `UPDATE_NODE`
   with `last_surfaced_date = today`. Silent the next morning.
5. On day ~20 (90% clearance): surfaces the milestone once, stamps the date, and goes quiet.
6. On day ~42 (full clearance / `expires_at` reached): surfaces final insight, then issues
   `UPDATE_NODE` with `is_archived = 1`. Node disappears from all future reflection passes.

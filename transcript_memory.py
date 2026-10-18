"""Transcript memory: ingest long PDF transcripts of conversations into a
retrieval-backed semantic store, without ever loading the full document into the
conversation context window.

Design
------
A transcript is chunked into ~600-token windows, each embedded with Gemini's
embedding model and stored as a float32 BLOB in ``transcript_chunks``. At chat
time we embed the user's message, brute-force cosine-rank the stored chunks, and
inject only the top-K verbatim snippets. The full document lives in SQLite and is
never sent to the model wholesale. Bounded top-K injection is the core
mechanism that keeps a hundred-page transcript from eating the context window.

This module deliberately does **not** use Mem0/Qdrant (which segfaults on Windows
as noted in CLAUDE.md). Embeddings are generated directly via the google-genai client
and similarity search is plain numpy. Single-user scale (tens of thousands of
chunks at most) makes brute-force search trivially fast.

Two halves of the "hybrid" memory the user asked for:
  1. Verbatim recall      → transcript_chunks + retrieve_transcript_context()
  2. Durable profile facts → extract_profile_facts_to_graph() writes 'life_context'
     nodes into the knowledge graph, which surface in every conversation via
     _load_graph_context().
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from init_storage import DATABASE_PATH

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = os.getenv("TRANSCRIPT_EMBEDDING_MODEL", "models/gemini-embedding-001")
EMBEDDING_DIMS = int(os.getenv("TRANSCRIPT_EMBEDDING_DIMS", "768"))
CHARS_PER_TOKEN = 4                      # rough heuristic for English prose
CHUNK_TOKEN_BUDGET = 600                 # target tokens per chunk
CHUNK_CHAR_BUDGET = CHUNK_TOKEN_BUDGET * CHARS_PER_TOKEN  # ≈ 2400 chars
CHUNK_OVERLAP_CHARS = 240                # ~1 sentence carried into the next chunk
EMBED_BATCH = 64                         # contents per embed_content call
RETRIEVE_TOP_K = 6                       # snippets injected into a conversation
MAP_WINDOW_CHARS = 24_000                # fact-extraction map window (~6k tokens)
MAP_WINDOW_CAP = 50                      # safety cap on map passes for huge docs
# The knowledge graph is injected IN FULL into every conversation turn, so each
# transcript must contribute only a handful of the most durable identity-level
# facts so large or numerous transcripts do not bloat every prompt. Hard-capped both in
# the reduce prompt and in code below.
LIFE_CONTEXT_MAX_NODES = 8

# Speaker labels commonly seen at the start of a transcript turn.
_SPEAKER_RE = re.compile(
    r"^\s*(user|assistant|me|you|human|ai|system|chatgpt|gpt|claude|gemini|bot|therapist|coach|q|a)\s*[:\-\u2014]\s*",
    re.IGNORECASE,
)
_SPEAKER_CANON = {
    "user": "user", "me": "user", "human": "user", "you": "user", "q": "user",
    "assistant": "assistant", "ai": "assistant", "system": "assistant",
    "chatgpt": "assistant", "gpt": "assistant", "claude": "assistant",
    "gemini": "assistant", "bot": "assistant", "therapist": "assistant",
    "coach": "assistant", "a": "assistant",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_document(filename: str, source_label: Optional[str] = None) -> str:
    """Insert a pending transcript_documents row and return its id."""
    doc_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO transcript_documents (id, filename, source_label, status) "
            "VALUES (?, ?, ?, 'pending')",
            (doc_id, filename, source_label),
        )
        conn.commit()
    return doc_id


def _set_status(doc_id: str, status: str, error: Optional[str] = None,
                total_chunks: Optional[int] = None) -> None:
    sets = ["status = ?"]
    params: List[Any] = [status]
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if total_chunks is not None:
        sets.append("total_chunks = ?")
        params.append(total_chunks)
    params.append(doc_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE transcript_documents SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()


def mark_failed(doc_id: str, error: str) -> None:
    """Flip a document to failed with an error message (e.g. text extraction failed)."""
    _set_status(doc_id, "failed", error=error[:500])


def list_documents() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, filename, source_label, status, total_chunks, error, created_at "
            "FROM transcript_documents ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_document(doc_id: str) -> bool:
    """Delete a document; chunks cascade. Returns True if a row was removed."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM transcript_documents WHERE id = ?", (doc_id,))
        conn.commit()
        return cur.rowcount > 0


# ── Vector (de)serialisation ──────────────────────────────────────────────────

def _vec_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


# ── Chunking ──────────────────────────────────────────────────────────────────

def _split_turns(text: str) -> List[Tuple[Optional[str], str]]:
    """Split text into (speaker, content) segments.

    Prefers conversation-turn boundaries (lines beginning with a speaker label);
    falls back to blank-line paragraph splits when no labels are present.
    """
    lines = text.splitlines()
    segments: List[Tuple[Optional[str], List[str]]] = []
    saw_speaker = False

    for line in lines:
        m = _SPEAKER_RE.match(line)
        if m:
            saw_speaker = True
            speaker = _SPEAKER_CANON.get(m.group(1).lower())
            body = line[m.end():]
            segments.append((speaker, [body] if body.strip() else []))
        elif segments:
            segments[-1][1].append(line)
        else:
            segments.append((None, [line]))

    if not saw_speaker:
        # Fall back to paragraph (blank-line) segmentation when no speaker labels are present.
        paragraphs = re.split(r"\n\s*\n", text)
        return [(None, p.strip()) for p in paragraphs if p.strip()]

    out: List[Tuple[Optional[str], str]] = []
    for speaker, body_lines in segments:
        body = "\n".join(body_lines).strip()
        if body:
            out.append((speaker, body))
    return out


def chunk_transcript(text: str) -> List[Dict[str, Any]]:
    """Chunk a transcript into ~CHUNK_TOKEN_BUDGET windows with small overlap.

    Returns a list of {chunk_index, speaker, content, token_estimate}. ``speaker``
    is the segment speaker when a chunk is homogeneous, else None (mixed turns).
    Oversized single segments are hard-split on the char budget.
    """
    segments = _split_turns(text)
    chunks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_len = 0
    buf_speakers: set = set()

    def flush() -> None:
        nonlocal buf, buf_len, buf_speakers
        if not buf:
            return
        content = "\n".join(buf).strip()
        if content:
            speaker = next(iter(buf_speakers)) if len(buf_speakers) == 1 else None
            chunks.append({
                "chunk_index": len(chunks),
                "speaker": speaker,
                "content": content,
                "token_estimate": max(1, len(content) // CHARS_PER_TOKEN),
            })
        buf, buf_len, buf_speakers = [], 0, set()

    for speaker, body in segments:
        # Hard-split any segment that alone blows the budget.
        pieces = [body]
        if len(body) > CHUNK_CHAR_BUDGET:
            pieces = [body[i:i + CHUNK_CHAR_BUDGET] for i in range(0, len(body), CHUNK_CHAR_BUDGET)]

        for piece in pieces:
            if buf_len + len(piece) > CHUNK_CHAR_BUDGET and buf:
                tail = "\n".join(buf)[-CHUNK_OVERLAP_CHARS:]
                flush()
                if tail.strip():
                    buf.append(tail.strip())
                    buf_len += len(tail)
            label = f"{speaker}: " if speaker else ""
            buf.append(f"{label}{piece}".strip())
            buf_len += len(piece)
            if speaker:
                buf_speakers.add(speaker)

    flush()
    # Re-index in case overlap logic produced gaps.
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks


# ── Embeddings ────────────────────────────────────────────────────────────────

def _embed_call(contents: List[str], task_type: str) -> List[np.ndarray]:
    """Single embed_content call returning L2-normalised float32 vectors."""
    from llm_service import _get_extractor_client  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    client = _get_extractor_client()
    resp = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=contents,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBEDDING_DIMS,
        ),
    )
    out: List[np.ndarray] = []
    for emb in resp.embeddings:
        out.append(_l2_normalize(np.asarray(emb.values, dtype=np.float32)))
    return out


def embed_texts(texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[np.ndarray]:
    """Embed a list of texts in batches, with per-item fallback on batch failure."""
    vectors: List[np.ndarray] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        try:
            vectors.extend(_embed_call(batch, task_type))
        except Exception as exc:
            logger.warning("[transcript] batch embed failed (%s), falling back to per-item", exc)
            for t in batch:
                try:
                    vectors.extend(_embed_call([t], task_type))
                except Exception as exc2:
                    logger.error("[transcript] per-item embed failed: %s", exc2)
                    vectors.append(np.zeros(EMBEDDING_DIMS, dtype=np.float32))
    return vectors


# ── Ingestion worker ──────────────────────────────────────────────────────────

def process_document(doc_id: str, raw_text: str) -> Dict[str, Any]:
    """Chunk → embed → store → extract profile facts. Synchronous (blocking I/O).

    Run via run_in_threadpool from the async layer. Emits progress over the event
    bus and flips the document status to 'complete' or 'failed'.
    """
    from event_bus import bus  # noqa: PLC0415

    def _progress(stage: str, **extra: Any) -> None:
        try:
            bus.emit_sync("TRANSCRIPT_PROGRESS", {"document_id": doc_id, "stage": stage, **extra})
        except Exception:
            pass

    try:
        _set_status(doc_id, "processing")
        _progress("chunking")

        chunks = chunk_transcript(raw_text)
        if not chunks:
            _set_status(doc_id, "failed", error="No text to chunk")
            _progress("failed", error="No text to chunk")
            return {"status": "failed", "reason": "empty"}

        _progress("embedding", total=len(chunks))
        vectors = embed_texts([c["content"] for c in chunks], task_type="RETRIEVAL_DOCUMENT")

        with _connect() as conn:
            conn.execute("DELETE FROM transcript_chunks WHERE document_id = ?", (doc_id,))
            conn.executemany(
                "INSERT INTO transcript_chunks "
                "(id, document_id, chunk_index, speaker, content, token_estimate, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(uuid.uuid4()), doc_id, c["chunk_index"], c["speaker"],
                        c["content"], c["token_estimate"], _vec_to_blob(vectors[i]),
                    )
                    for i, c in enumerate(chunks)
                ],
            )
            conn.commit()

        _set_status(doc_id, "processing", total_chunks=len(chunks))

        _progress("extracting_facts")
        facts = 0
        try:
            facts = extract_profile_facts_to_graph(doc_id, raw_text)
        except Exception as exc:
            logger.warning("[transcript] profile-fact extraction failed (non-fatal): %s", exc)

        _set_status(doc_id, "complete")
        result = {"status": "complete", "chunks": len(chunks), "facts": facts}
        _progress("complete", **result)
        logger.info("[transcript] document %s complete: %s", doc_id, result)
        return result
    except Exception as exc:
        logger.error("[transcript] process_document failed: %s", exc, exc_info=True)
        _set_status(doc_id, "failed", error=str(exc)[:500])
        _progress("failed", error=str(exc)[:200])
        return {"status": "failed", "reason": str(exc)}


# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve_transcript_context(query: str, top_k: int = RETRIEVE_TOP_K) -> str:
    """Embed the query, cosine-rank stored chunks, return top-K verbatim snippets.

    Output shape mirrors memory_layer.retrieve_context: a newline-joined bullet
    list, so it drops straight into the conversation narrative. Returns "" when
    nothing is stored or on any error (recall is best-effort, never fatal).
    """
    if not query.strip():
        return ""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT content, embedding FROM transcript_chunks "
                "WHERE embedding IS NOT NULL"
            ).fetchall()
        if not rows:
            return ""

        matrix = np.vstack([_blob_to_vec(r["embedding"]) for r in rows])
        q = _l2_normalize(_embed_call([query], task_type="RETRIEVAL_QUERY")[0])
        scores = matrix @ q
        k = min(top_k, len(rows))
        top_idx = np.argsort(scores)[::-1][:k]

        snippets: List[str] = []
        for i in top_idx:
            row = rows[int(i)]
            content = re.sub(r"\s+", " ", row["content"]).strip()
            if len(content) > 600:
                content = content[:600].rstrip() + "…"
            snippets.append(f"- {content}")
        return "\n".join(snippets)
    except Exception as exc:
        logger.warning("[transcript] retrieve failed (non-fatal): %s", exc)
        return ""


# ── Profile-fact extraction → knowledge graph ─────────────────────────────────

_FACT_SYSTEM_PROMPT = (
    "You extract DURABLE, PERSISTENT facts about a person from a transcript of "
    "their personal conversations or therapy/notes sessions. Focus on who they "
    "are: relationships, family, history, recurring struggles, values, goals, "
    "identity, important life events. Ignore transient small-talk and anything "
    "that will not still be true and relevant months from now.\n\n"
    "Return ONLY a JSON array of commands, no prose, no markdown fences. Each item:\n"
    '{"command":"CREATE_NODE","concept_name":"<short unique label>",'
    '"category":"life_context","summary_text":"<1-2 sentence durable fact>",'
    '"confidence_level":"confirmed"}\n'
    "Keep concept_name short and specific (e.g. \"Relationship with father\", "
    "\"Career goal: nursing\", \"History of anxiety\").\n"
    "CONSOLIDATE aggressively: merge related observations into a single broad fact "
    "rather than emitting many narrow ones (one \"Relationship with father\" node, "
    "not separate nodes for each detail). These facts are injected into EVERY future "
    "conversation, so output ONLY the few most defining, identity-level facts, "
    "AT MOST 8. If nothing durable is present, return []."
)


def _map_extract_facts(window: str) -> List[str]:
    """Map step: pull short durable-fact bullets from one window via Flash."""
    from llm_service import _get_extractor_client, EXTRACTOR_MODEL  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    client = _get_extractor_client()
    resp = client.models.generate_content(
        model=EXTRACTOR_MODEL,
        contents=(
            "Extract durable personal facts (relationships, history, goals, "
            "identity, recurring themes) as a short bullet list. One fact per line, "
            "no preamble. Transcript excerpt:\n\n" + window
        ),
        config=types.GenerateContentConfig(temperature=0),
    )
    text = (resp.text or "").strip()
    bullets = [re.sub(r"^[\-\*•\d\.\)\s]+", "", ln).strip() for ln in text.splitlines()]
    return [b for b in bullets if b]


def _reduce_facts_to_commands(bullets: List[str]) -> List[Dict[str, Any]]:
    """Reduce step: dedupe/consolidate bullets into CREATE_NODE commands."""
    from llm_service import _get_extractor_client, EXTRACTOR_MODEL  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    client = _get_extractor_client()
    resp = client.models.generate_content(
        model=EXTRACTOR_MODEL,
        contents="Candidate facts:\n" + "\n".join(f"- {b}" for b in bullets),
        config=types.GenerateContentConfig(
            system_instruction=_FACT_SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    raw = (resp.text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE).strip()
    try:
        cmds = json.loads(raw)
        return cmds if isinstance(cmds, list) else [cmds]
    except json.JSONDecodeError as exc:
        logger.warning("[transcript] fact JSON parse failed: %s | raw: %s", exc, raw[:300])
        return []


def _upsert_life_context_node(conn: sqlite3.Connection, concept_name: str,
                              summary_text: str, confidence_level: str) -> None:
    """Upsert a 'life_context' clinical node (mirrors the synthesis upsert)."""
    conn.execute(
        """INSERT INTO clinical_nodes
               (concept_name, category, summary_text, confidence_level, last_updated)
           VALUES (?, 'life_context', ?, ?, datetime('now'))
           ON CONFLICT(concept_name) DO UPDATE SET
               category         = 'life_context',
               summary_text     = excluded.summary_text,
               confidence_level = CASE
                   WHEN excluded.confidence_level = 'confirmed' THEN 'confirmed'
                   ELSE clinical_nodes.confidence_level
               END,
               last_updated     = excluded.last_updated""",
        (concept_name, summary_text, confidence_level),
    )


def extract_profile_facts_to_graph(doc_id: str, full_text: str) -> int:
    """Map-reduce durable profile facts out of a transcript into the graph.

    Writes 'life_context' nodes which surface in every conversation via
    _load_graph_context(). Returns the number of nodes written.
    """
    windows = [full_text[i:i + MAP_WINDOW_CHARS] for i in range(0, len(full_text), MAP_WINDOW_CHARS)]
    windows = windows[:MAP_WINDOW_CAP]

    bullets: List[str] = []
    for w in windows:
        try:
            bullets.extend(_map_extract_facts(w))
        except Exception as exc:
            logger.warning("[transcript] map window failed (non-fatal): %s", exc)
    if not bullets:
        return 0

    commands = _reduce_facts_to_commands(bullets)
    written = 0
    with _connect() as conn:
        for cmd in commands:
            if written >= LIFE_CONTEXT_MAX_NODES:
                logger.info("[transcript] life_context cap (%d) reached, dropping extras",
                            LIFE_CONTEXT_MAX_NODES)
                break
            try:
                if cmd.get("command") != "CREATE_NODE":
                    continue
                name = (cmd.get("concept_name") or "").strip()
                summary = (cmd.get("summary_text") or "").strip()
                if not name or not summary:
                    continue
                conf = cmd.get("confidence_level", "confirmed")
                if conf not in ("hypothesis", "testing", "confirmed"):
                    conf = "confirmed"
                _upsert_life_context_node(conn, name, summary, conf)
                written += 1
            except Exception as exc:
                logger.error("[transcript] life_context upsert failed: %s | %s", exc, cmd)
        conn.commit()
    logger.info("[transcript] wrote %d life_context node(s) from %s", written, doc_id)
    return written

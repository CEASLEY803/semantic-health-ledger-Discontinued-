"""Bounded, embedding-backed semantic recall for conversations.

This is the working replacement for Mem0/Qdrant, which segfaults on Windows (see
CLAUDE.md). Mem0 stays importable but is effectively dead; ``_mem0_add_safe`` /
``_mem0_retrieve_safe`` in api.py now route through this module instead.

Each stored snippet (a user message, summary, or protocol note) is embedded with
Gemini's embedding model and saved as a float32 BLOB in ``chat_memory``. At chat
time we embed the incoming message, brute-force cosine-rank stored snippets, and
inject only the top-K verbatim ones into the conversation narrative, using the same
bounded-injection trick that keeps transcript recall from eating the context
window. Single-user scale makes brute-force search trivially fast.

The embedding + vector machinery is shared with :mod:`transcript_memory`; this
module reuses those primitives rather than duplicating them.
"""

from __future__ import annotations

import logging
import uuid
from typing import List

import numpy as np

from transcript_memory import (
    _connect,
    _embed_call,
    _l2_normalize,
    _blob_to_vec,
    _vec_to_blob,
)

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
RETRIEVE_TOP_K = 5           # snippets injected into a conversation
MIN_STORE_CHARS = 8          # ignore trivially short fragments
SNIPPET_CHAR_CAP = 600       # per-snippet ceiling in the injected context


def add_chat_memory(text: str, role: str | None = None) -> None:
    """Embed and store one conversation snippet for later semantic recall.

    Best-effort: embedding/DB failures are logged, never raised (memory writes
    must not break the chat path). Exact-duplicate content is skipped so repeated
    phrasings don't crowd out top-K retrieval.
    """
    snippet = (text or "").strip()
    if len(snippet) < MIN_STORE_CHARS:
        return
    try:
        with _connect() as conn:
            dup = conn.execute(
                "SELECT 1 FROM chat_memory WHERE content = ? LIMIT 1", (snippet,)
            ).fetchone()
            if dup:
                return
            vec = _l2_normalize(_embed_call([snippet], task_type="RETRIEVAL_DOCUMENT")[0])
            conn.execute(
                "INSERT INTO chat_memory (id, role, content, embedding) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), role, snippet, _vec_to_blob(vec)),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[chat_memory] add failed (non-fatal): %s", exc)


def retrieve_chat_memory(query: str, top_k: int = RETRIEVE_TOP_K) -> str:
    """Embed the query, cosine-rank stored snippets, return top-K as a bullet list.

    Output mirrors ``transcript_memory.retrieve_transcript_context`` and the old
    Mem0 context shape, so it drops straight into the conversation narrative under
    "[What we know from past sessions]". Returns "" when nothing is stored or on
    any error (recall is best-effort, never fatal).
    """
    q = (query or "").strip()
    if not q:
        return ""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT content, embedding FROM chat_memory WHERE embedding IS NOT NULL"
            ).fetchall()
        if not rows:
            return ""

        matrix = np.vstack([_blob_to_vec(r["embedding"]) for r in rows])
        qv = _l2_normalize(_embed_call([q], task_type="RETRIEVAL_QUERY")[0])
        scores = matrix @ qv
        k = min(top_k, len(rows))
        top_idx = np.argsort(scores)[::-1][:k]

        snippets: List[str] = []
        for i in top_idx:
            content = " ".join(rows[int(i)]["content"].split())
            if len(content) > SNIPPET_CHAR_CAP:
                content = content[:SNIPPET_CHAR_CAP].rstrip() + "…"
            snippets.append(f"- {content}")
        return "\n".join(snippets)
    except Exception as exc:
        logger.warning("[chat_memory] retrieve failed (non-fatal): %s", exc)
        return ""

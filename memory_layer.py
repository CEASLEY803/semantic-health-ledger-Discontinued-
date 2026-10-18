from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


load_dotenv()

_BASE = Path(__file__).resolve().parent

MEM0_USER_ID = os.getenv("MEM0_USER_ID", "primary_user")
MEM0_STATE_DIR = Path(os.getenv("MEM0_STATE_DIR", str(_BASE / "data" / "memory" / "mem0_state")))
MEM0_QDRANT_PATH = Path(os.getenv("MEM0_QDRANT_PATH", str(_BASE / "data" / "memory" / "qdrant")))
MEM0_COLLECTION_NAME = os.getenv("MEM0_COLLECTION_NAME", "health_ledger_semantic")
MEM0_LLM_MODEL = os.getenv("MEM0_LLM_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
MEM0_EMBEDDING_MODEL = os.getenv("MEM0_EMBEDDING_MODEL", "models/gemini-embedding-001")
MEM0_EMBEDDING_DIMS = int(os.getenv("MEM0_EMBEDDING_DIMS", "768"))

_memory: Optional[Any] = None


def build_mem0_config() -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    return {
        "llm": {
            "provider": "gemini",
            "config": {
                "model": MEM0_LLM_MODEL,
                "api_key": api_key,
            },
        },
        "embedder": {
            "provider": "gemini",
            "config": {
                "model": MEM0_EMBEDDING_MODEL,
                "api_key": api_key,
                "embedding_dims": MEM0_EMBEDDING_DIMS,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(MEM0_QDRANT_PATH),
                "collection_name": MEM0_COLLECTION_NAME,
                "embedding_model_dims": MEM0_EMBEDDING_DIMS,
                "on_disk": True,
            },
        },
    }


def get_memory() -> Any:
    global _memory
    if _memory is not None:
        return _memory

    MEM0_STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEM0_QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MEM0_DIR", str(MEM0_STATE_DIR))

    config = build_mem0_config()
    from mem0 import Memory

    _memory = Memory.from_config(config)
    return _memory


def add_semantic_memory(raw_text: str, user_id: str = MEM0_USER_ID) -> Dict[str, Any]:
    if not raw_text.strip():
        return {"status": "skipped", "reason": "empty raw_text"}

    try:
        memory = get_memory()
        result = memory.add(messages=raw_text, user_id=user_id)
        return {"status": "success", "result": result}
    except Exception as exc:
        logger.error(f"[mem0] add error: {exc}", exc_info=True)
        return {"status": "error", "message": str(exc)}


def retrieve_context(query: str, top_k: int = 5, user_id: str = MEM0_USER_ID) -> str:
    if not query.strip():
        return ""

    try:
        memory = get_memory()
        results = memory.search(
            query=query,
            filters={"user_id": user_id},
            top_k=top_k,
        )
    except Exception as exc:
        logger.error(f"[mem0] search error: {exc}", exc_info=True)
        return ""

    memories = normalize_search_results(results)
    return "\n".join(f"- {memory}" for memory in memories)


def normalize_search_results(results: Any) -> List[str]:
    if isinstance(results, dict):
        results = results.get("results", [])

    memories = []
    for result in results or []:
        if isinstance(result, dict):
            memory = result.get("memory") or result.get("text")
            if memory:
                memories.append(str(memory))
        elif isinstance(result, str):
            memories.append(result)
    return memories

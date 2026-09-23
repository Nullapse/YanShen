"""Optional LlamaIndex vector retrieval for the existing knowledge-card RAG."""

import hashlib
import json
import threading

from .embeddings import FEATURE_HASH_MODEL, embed_text
from .indexing import _active_embedding, _load_dense_model

_CACHE_LOCK = threading.RLock()
_CACHE_KEY = None
_CACHE_INDEX = None


def _vector(values):
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [float(value) for value in values]


def _make_embedding(model_name, model):
    try:
        from llama_index.core.bridge.pydantic import PrivateAttr
        from llama_index.core.embeddings import BaseEmbedding
    except ImportError as exc:
        raise RuntimeError(
            "LlamaIndex retriever is optional; install it with `pip install -r requirements-eval.txt`."
        ) from exc

    class ProjectEmbedding(BaseEmbedding):
        _model = PrivateAttr(default=None)

        def __init__(self):
            super().__init__(model_name=model_name)
            self._model = model

        def _get_query_embedding(self, query):
            if self._model is None:
                return embed_text(query)[0]
            return _vector(next(iter(self._model.query_embed([query]))))

        def _get_text_embedding(self, text):
            if self._model is None:
                return embed_text(text)[0]
            return _vector(next(iter(self._model.passage_embed([text]))))

        def _get_text_embeddings(self, texts):
            if self._model is None:
                return [embed_text(text)[0] for text in texts]
            return [_vector(value) for value in self._model.passage_embed(texts)]

        async def _aget_query_embedding(self, query):
            return self._get_query_embedding(query)

        async def _aget_text_embedding(self, text):
            return self._get_text_embedding(text)

    return ProjectEmbedding()


def _knowledge_rows(conn):
    rows = conn.execute(
        """
        SELECT id, title, body, metadata_json, content_hash
          FROM agent_context_chunks
         WHERE source_type = 'knowledge'
      ORDER BY id
        """
    ).fetchall()
    valid_rows = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        module = metadata.get("module")
        knowledge_id = metadata.get("knowledge_id")
        if not module or not knowledge_id:
            continue
        item = dict(row)
        item["module"] = str(module)
        item["knowledge_id"] = str(knowledge_id)
        valid_rows.append(item)
    return valid_rows


def _build_index(rows, embedding_model):
    try:
        from llama_index.core import VectorStoreIndex
        from llama_index.core.schema import TextNode
    except ImportError as exc:
        raise RuntimeError(
            "LlamaIndex retriever is optional; install it with `pip install -r requirements-eval.txt`."
        ) from exc

    if embedding_model == FEATURE_HASH_MODEL:
        model = None
    else:
        model = _load_dense_model(embedding_model, allow_download=False)
        if model is None:
            raise RuntimeError(f"The local embedding model {embedding_model} is not available in the model cache.")

    nodes = [
        TextNode(
            id_=str(row["id"]),
            text=f"{row['title']}\n{row['body']}",
            metadata={
                "chunk_id": int(row["id"]),
                "knowledge_id": row["knowledge_id"],
                "module": row["module"],
            },
        )
        for row in rows
    ]
    embed_model = _make_embedding(embedding_model, model)
    return VectorStoreIndex(nodes, embed_model=embed_model, show_progress=False)


def retrieve_knowledge_chunks(conn, query, module_id, limit=120):
    """Return knowledge chunks ranked by LlamaIndex's local vector retriever."""
    try:
        from llama_index.core.vector_stores import MetadataFilter, MetadataFilters
    except ImportError as exc:
        raise RuntimeError(
            "LlamaIndex retriever is optional; install it with `pip install -r requirements-eval.txt`."
        ) from exc

    rows = _knowledge_rows(conn)
    if not rows:
        return []

    model_name, dimensions = _active_embedding(conn)
    fingerprint = hashlib.sha256(
        "\n".join(
            f"{row['id']}:{row['content_hash']}:{row['title']}:{row['body']}:{row['module']}:{row['knowledge_id']}"
            for row in rows
        ).encode("utf-8")
    ).hexdigest()
    cache_key = (model_name, dimensions, fingerprint)

    global _CACHE_KEY, _CACHE_INDEX
    with _CACHE_LOCK:
        if _CACHE_KEY != cache_key or _CACHE_INDEX is None:
            _CACHE_INDEX = _build_index(rows, model_name)
            _CACHE_KEY = cache_key
        index = _CACHE_INDEX

    filters = MetadataFilters(filters=[MetadataFilter(key="module", value=module_id)])
    retriever = index.as_retriever(
        similarity_top_k=max(1, int(limit)),
        filters=filters,
    )
    matches = retriever.retrieve(query)
    chunk_ids = []
    ranked = []
    for rank, match in enumerate(matches, start=1):
        metadata = match.node.metadata or {}
        try:
            chunk_id = int(metadata["chunk_id"])
        except (KeyError, TypeError, ValueError):
            continue
        chunk_ids.append(chunk_id)
        ranked.append((chunk_id, rank, float(match.score or 0.0)))
    if not chunk_ids:
        return []

    placeholders = ", ".join("?" for _ in chunk_ids)
    chunk_rows = conn.execute(
        f"SELECT * FROM agent_context_chunks WHERE id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    by_id = {int(row["id"]): dict(row) for row in chunk_rows}
    vector_backend = f"llamaindex:{model_name}"
    results = []
    for chunk_id, rank, score in ranked:
        item = by_id.get(chunk_id)
        if item is None:
            continue
        item["_vector_score"] = score
        item["_vector_rank"] = rank
        item["_vector_backend"] = vector_backend
        results.append(item)
    return results

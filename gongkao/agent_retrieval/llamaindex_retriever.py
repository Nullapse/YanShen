"""LlamaIndex pipeline for the static knowledge-card collection.

User history remains in the SQLite/FTS5/sqlite-vec retrieval path. This module
owns static-card nodes, their persistent dense index, BM25 retrieval, and fusion.
"""

import hashlib
import json
import logging
import threading

from ..paths import user_data_dir
from .embeddings import FEATURE_HASH_MODEL, embed_text, tokenize
from .indexing import _active_embedding, _load_dense_model

_CACHE_LOCK = threading.RLock()
_CACHE_STORE_KEY = None
_CACHE_FINGERPRINT = None
_CACHE_INDEX = None
_WARNED_DUPLICATES = False
_INDEX_SCHEMA_VERSION = 2
_EMBEDDING_METADATA_EXCLUSIONS = (
    "knowledge_id",
    "source_file",
    "source_version",
    "source",
    "source_name",
    "source_section",
    "source_license",
    "visibility",
    "review_status",
)


def _vector(values):
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [float(value) for value in values]


def _missing_dependency():
    return RuntimeError(
        "LlamaIndex knowledge retrieval is optional; install it with "
        "`pip install -r requirements-eval.txt`."
    )


def _make_embedding(model_name, model):
    try:
        from llama_index.core.bridge.pydantic import PrivateAttr
        from llama_index.core.embeddings import BaseEmbedding
    except ImportError as exc:
        raise _missing_dependency() from exc

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
        SELECT id, source_id, title, body, metadata_json, content_hash
          FROM agent_context_chunks
         WHERE source_type = 'knowledge'
      ORDER BY id
        """
    ).fetchall()
    valid_rows = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        module = metadata.get("module")
        knowledge_id = metadata.get("knowledge_id")
        if not module or not knowledge_id:
            continue
        knowledge_id = str(knowledge_id)
        if knowledge_id in valid_rows:
            # Older local indexes can contain duplicate rows after a card
            # source migration. The latest row wins while the stable Evidence
            # ID and LlamaIndex node ID remain the card's canonical ID.
            global _WARNED_DUPLICATES
            if not _WARNED_DUPLICATES:
                with _CACHE_LOCK:
                    if not _WARNED_DUPLICATES:
                        logging.warning("Duplicate static knowledge rows found; using the latest row per stable card ID")
                        _WARNED_DUPLICATES = True
        item = dict(row)
        item["source_type"] = "knowledge"
        item["module"] = str(module)
        item["knowledge_id"] = knowledge_id
        item["metadata"] = metadata
        valid_rows[knowledge_id] = item
    return sorted(valid_rows.values(), key=lambda item: int(item["id"]))


def _node_from_row(row):
    try:
        from llama_index.core.schema import TextNode
    except ImportError as exc:
        raise _missing_dependency() from exc

    metadata = dict(row["metadata"])
    # The node id is the stable knowledge-card id. The transient SQLite row id
    # is intentionally absent from both node text and node metadata.
    metadata["knowledge_id"] = row["knowledge_id"]
    metadata["module"] = row["module"]
    return TextNode(
        id_=row["knowledge_id"],
        text=f"{row['title']}\n{row['body']}",
        metadata=metadata,
        excluded_embed_metadata_keys=list(_EMBEDDING_METADATA_EXCLUSIONS),
    )


def _node_fingerprint(node):
    from llama_index.core.schema import MetadataMode

    embed_text = node.get_content(metadata_mode=MetadataMode.EMBED)
    serialized = json.dumps(
        {"text": node.text, "metadata": node.metadata},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        hashlib.sha256(embed_text.encode("utf-8")).hexdigest(),
        hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def _rows_fingerprint(nodes):
    values = []
    for node in sorted(nodes, key=lambda item: item.node_id):
        embed_hash, node_hash = _node_fingerprint(node)
        values.append(f"{node.node_id}:{embed_hash}:{node_hash}")
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _storage_dir(model_name, dimensions):
    identity = json.dumps(
        {"schema": _INDEX_SCHEMA_VERSION, "model": model_name, "dimensions": dimensions},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return user_data_dir() / "llamaindex" / "knowledge" / f"v{_INDEX_SCHEMA_VERSION}-{digest}"


def _load_index(storage_dir, embed_model):
    try:
        from llama_index.core import load_index_from_storage
        from llama_index.core.storage import StorageContext

        context = StorageContext.from_defaults(persist_dir=str(storage_dir))
        return load_index_from_storage(context, embed_model=embed_model)
    except Exception as exc:
        logging.warning("Unable to load persisted LlamaIndex knowledge index at %s: %s", storage_dir, exc)
        return None


def _persist(index, storage_dir):
    storage_dir.mkdir(parents=True, exist_ok=True)
    index.storage_context.persist(persist_dir=str(storage_dir))


def _existing_embedding(index, node_id):
    vector_store = getattr(index, "_vector_store", None)
    data = getattr(vector_store, "data", None)
    embeddings = getattr(data, "embedding_dict", {})
    value = embeddings.get(node_id)
    return list(value) if value else None


def _reconcile_index(index, rows, storage_dir):
    nodes = [_node_from_row(row) for row in rows]
    wanted = {node.node_id: node for node in nodes}
    existing_docs = dict(getattr(index.docstore, "docs", {}) or {})
    changed = False

    obsolete_ids = set(existing_docs) - set(wanted)
    if obsolete_ids:
        index.delete_nodes(sorted(obsolete_ids), delete_from_docstore=True)
        changed = True

    inserts = []
    for node_id, node in wanted.items():
        previous = existing_docs.get(node_id)
        if previous is None:
            inserts.append(node)
            continue
        previous_embed_hash, previous_node_hash = _node_fingerprint(previous)
        embed_hash, node_hash = _node_fingerprint(node)
        if node_hash == previous_node_hash:
            continue
        if embed_hash == previous_embed_hash:
            # Provenance-only edits update the persisted node without spending
            # an embedding call. The SimpleVectorStore keeps vectors by node id.
            node.embedding = _existing_embedding(index, node_id)
        index.delete_nodes([node_id], delete_from_docstore=True)
        inserts.append(node)

    if inserts:
        index.insert_nodes(inserts)
        changed = True
    if changed or not storage_dir.exists():
        _persist(index, storage_dir)
    return nodes


def _get_index(rows, model_name, dimensions):
    try:
        from llama_index.core import VectorStoreIndex
    except ImportError as exc:
        raise _missing_dependency() from exc

    model = None
    if model_name != FEATURE_HASH_MODEL:
        model = _load_dense_model(model_name, allow_download=False)
        if model is None:
            raise RuntimeError(f"The local embedding model {model_name} is not available in the model cache.")
    embed_model = _make_embedding(model_name, model)
    storage_dir = _storage_dir(model_name, dimensions)
    store_key = (str(storage_dir), model_name, dimensions)
    target_nodes = [_node_from_row(row) for row in rows]
    fingerprint = _rows_fingerprint(target_nodes)

    global _CACHE_STORE_KEY, _CACHE_FINGERPRINT, _CACHE_INDEX
    with _CACHE_LOCK:
        if _CACHE_STORE_KEY == store_key and _CACHE_INDEX is not None:
            index = _CACHE_INDEX
            if _CACHE_FINGERPRINT == fingerprint:
                return index
        elif storage_dir.exists():
            index = _load_index(storage_dir, embed_model)
            if index is None:
                index = VectorStoreIndex([], embed_model=embed_model, show_progress=False)
        else:
            index = VectorStoreIndex([], embed_model=embed_model, show_progress=False)

        _reconcile_index(index, rows, storage_dir)
        _CACHE_INDEX = index
        _CACHE_STORE_KEY = store_key
        _CACHE_FINGERPRINT = fingerprint
        return index


def _tokenized_node(node):
    from llama_index.core.schema import TextNode

    return TextNode(
        id_=node.node_id,
        text=" ".join(tokenize(node.text or "")),
        metadata=dict(node.metadata or {}),
        excluded_embed_metadata_keys=list(_EMBEDDING_METADATA_EXCLUSIONS),
    )


def _build_fusion_retriever(index, module_id, limit):
    try:
        from llama_index.core.llms.mock import MockLLM
        from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
        from llama_index.core.schema import NodeWithScore, QueryBundle
        from llama_index.core.vector_stores import MetadataFilter, MetadataFilters
        from llama_index.retrievers.bm25 import BM25Retriever
    except ImportError as exc:
        raise _missing_dependency() from exc

    top_k = max(20, min(120, int(limit) * 4))
    metadata_filters = MetadataFilters(
        filters=[MetadataFilter(key="module", value=module_id)]
    )
    dense = index.as_retriever(similarity_top_k=top_k, filters=metadata_filters)
    nodes = [
        node
        for node in (getattr(index.docstore, "docs", {}) or {}).values()
        if str((node.metadata or {}).get("module") or "") == module_id
    ]
    if not nodes:
        return None
    sparse = BM25Retriever.from_defaults(
        nodes=[_tokenized_node(node) for node in nodes],
        similarity_top_k=min(top_k, len(nodes)),
        skip_stemming=True,
        token_pattern=r"(?u)\b\w+\b",
    )
    dense_by_id = {node.node_id: node for node in nodes}

    class TokenizingRetriever(BaseRetriever):
        def __init__(self, wrapped):
            super().__init__()
            self._wrapped = wrapped

        def _retrieve(self, query_bundle):
            tokenized = " ".join(tokenize(query_bundle.query_str))
            matches = self._wrapped.retrieve(QueryBundle(query_str=tokenized))
            # QueryFusionRetriever fuses by node content hash. Return the
            # canonical dense-index node so both retrievers share that hash.
            return [
                NodeWithScore(node=dense_by_id[item.node.node_id], score=item.score)
                for item in matches
                if item.node.node_id in dense_by_id
            ]

    return QueryFusionRetriever(
        [dense, TokenizingRetriever(sparse)],
        similarity_top_k=max(1, int(limit)),
        num_queries=1,
        mode="reciprocal_rerank",
        use_async=False,
        # Query expansion is disabled (num_queries=1); MockLLM prevents the
        # constructor from resolving an unrelated default provider.
        llm=MockLLM(),
    )


def retrieve_knowledge_chunks(conn, query, module_id, limit=120):
    """Return static knowledge cards ranked by LlamaIndex dense+BM25 fusion."""
    rows = _knowledge_rows(conn)
    if not rows:
        return []

    model_name, dimensions = _active_embedding(conn)
    index = _get_index(rows, model_name, dimensions)
    retriever = _build_fusion_retriever(index, str(module_id), limit)
    if retriever is None:
        return []
    matches = retriever.retrieve(str(query or ""))

    by_knowledge_id = {row["knowledge_id"]: row for row in rows}
    results = []
    for rank, match in enumerate(matches, start=1):
        knowledge_id = str((match.node.metadata or {}).get("knowledge_id") or match.node.node_id)
        row = by_knowledge_id.get(knowledge_id)
        if row is None:
            continue
        item = dict(row)
        item["_metadata"] = dict(row["metadata"])
        item["_fusion_score"] = float(match.score or 0.0)
        item["_vector_score"] = 0.0
        item["_vector_rank"] = rank
        item["_vector_backend"] = f"llamaindex:query-fusion:{model_name}"
        normalized_score = min(1.0, max(0.0, float(match.score or 0.0) / (2.0 / 60.0)))
        item["_rrf_score"] = round(normalized_score, 4)
        item["_rerank_score"] = round(normalized_score, 4)
        item["_llamaindex_rank"] = rank
        results.append(item)
    return results

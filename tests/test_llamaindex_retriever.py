import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

try:
    import llama_index.core  # noqa: F401
    import llama_index.retrievers.bm25  # noqa: F401
except ImportError:  # Optional evaluation dependency.
    HAS_LLAMA_INDEX = False
else:
    HAS_LLAMA_INDEX = True


@unittest.skipUnless(HAS_LLAMA_INDEX, "LlamaIndex evaluation dependencies are optional")
class LlamaIndexKnowledgeRetrieverTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self._environment_patch = patch.dict(os.environ, {"GONGKAO_DATA_DIR": self.temp_dir.name})
        self._environment_patch.start()
        self.addCleanup(self._environment_patch.stop)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute(
            """
            CREATE TABLE agent_context_chunks (
                id INTEGER PRIMARY KEY,
                source_type TEXT,
                source_id INTEGER,
                title TEXT,
                body TEXT,
                metadata_json TEXT,
                content_hash TEXT
            )
            """
        )
        self._insert(
            41,
            "knowledge:summary:stable-card",
            "概括题提炼材料要点",
            "围绕主体、处境、做法和结果归纳材料，使用简洁准确的表达。",
            {"module": "summary", "kind": "method", "tags": ["概括", "材料"], "source_section": "概括方法"},
        )
        self._insert(
            97,
            "knowledge:analysis:other-card",
            "综合分析解释含义",
            "联系材料解释观点，说明原因、影响和落实路径。",
            {"module": "analysis", "kind": "method", "tags": ["分析"]},
        )
        self.module = self._import_module()
        self.addCleanup(self._clear_cache)

    def _insert(self, row_id, knowledge_id, title, body, extra_metadata):
        metadata = {"knowledge_id": knowledge_id, **extra_metadata}
        self.db.execute(
            "INSERT INTO agent_context_chunks VALUES (?, 'knowledge', ?, ?, ?, ?, ?)",
            (row_id, row_id * 10, title, body, json.dumps(metadata, ensure_ascii=False), f"hash-{row_id}"),
        )

    def _import_module(self):
        from gongkao.agent_retrieval import llamaindex_retriever

        return llamaindex_retriever

    def _clear_cache(self):
        self.module._CACHE_STORE_KEY = None
        self.module._CACHE_FINGERPRINT = None
        self.module._CACHE_INDEX = None

    def test_nodes_use_stable_knowledge_ids_and_metadata_filtering(self):
        rows = self.module._knowledge_rows(self.db)
        node = self.module._node_from_row(rows[0])
        self.assertEqual(node.node_id, "knowledge:summary:stable-card")
        self.assertNotIn("410", node.text)
        self.assertNotIn("chunk_id", node.metadata)
        self.assertEqual(node.metadata["module"], "summary")
        self.assertEqual(node.metadata["source_section"], "概括方法")
        self.assertNotIn("knowledge", self.module._tokenized_node(node).text)

        results = self.module.retrieve_knowledge_chunks(
            self.db, "概括题 提炼 材料 要点", "summary", limit=5
        )
        self.assertTrue(results)
        self.assertTrue(all(item["_metadata"]["module"] == "summary" for item in results))
        self.assertEqual(results[0]["_metadata"]["knowledge_id"], "knowledge:summary:stable-card")

        from gongkao.agent_modules import retrieve_knowledge_evidence
        from gongkao.agent_rag import cards_from_module_context

        evidence = retrieve_knowledge_evidence(
            self.db,
            "summary",
            "概括题 提炼 材料 要点",
            limit=4,
            ensure_index=False,
            retriever_backend="llamaindex",
        )
        self.assertEqual(evidence[0]["evidence_ref"], "knowledge:summary:stable-card")
        self.assertEqual(evidence[0]["retrieval"]["fusion_backend"], "llamaindex.query_fusion")
        cards = cards_from_module_context({"module": "summary", "evidence_chunks": evidence})
        self.assertEqual(cards[0]["evidence_id"], "knowledge:summary:stable-card")

    def test_persistence_and_card_level_incremental_embedding(self):
        embed_text = self.module.embed_text
        with patch.dict(os.environ, {"GONGKAO_DATA_DIR": self.temp_dir.name}):
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                self.module.retrieve_knowledge_chunks(self.db, "概括材料要点", "summary", limit=4)
                self.assertEqual(embed.call_count, 3)  # Two nodes plus the query.

            self._clear_cache()
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                self.module.retrieve_knowledge_chunks(self.db, "概括材料要点", "summary", limit=4)
                self.assertEqual(embed.call_count, 1)  # Persisted vectors load without re-embedding.

            metadata = json.loads(
                self.db.execute("SELECT metadata_json FROM agent_context_chunks WHERE id = 41").fetchone()[0]
            )
            metadata["source_section"] = "概括方法（修订）"
            self.db.execute(
                "UPDATE agent_context_chunks SET metadata_json = ? WHERE id = 41",
                (json.dumps(metadata, ensure_ascii=False),),
            )
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                changed_metadata = self.module.retrieve_knowledge_chunks(
                    self.db, "概括材料要点", "summary", limit=4
                )
                self.assertEqual(embed.call_count, 1)  # Query only; source metadata is excluded from embeddings.
            self.assertEqual(changed_metadata[0]["_metadata"]["source_section"], "概括方法（修订）")

            self.db.execute(
                "UPDATE agent_context_chunks SET body = ? WHERE id = 41",
                ("按主体、处境、举措和实际结果提炼材料要点。",),
            )
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                self.module.retrieve_knowledge_chunks(self.db, "概括材料要点", "summary", limit=4)
                self.assertEqual(embed.call_count, 2)  # One changed node plus the query.

            self._insert(
                111,
                "knowledge:summary:new-card",
                "新增概括检查卡",
                "提交前对照题干核查主体、对象、范围和任务要求。",
                {"module": "summary", "kind": "checklist", "tags": ["核查", "题干"]},
            )
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                added = self.module.retrieve_knowledge_chunks(self.db, "核查题干范围", "summary", limit=4)
                self.assertEqual(embed.call_count, 2)  # One new node plus the query.
            self.assertIn("knowledge:summary:new-card", [item["knowledge_id"] for item in added])

            self.db.execute("DELETE FROM agent_context_chunks WHERE id = 111")
            with patch.object(self.module, "embed_text", wraps=embed_text) as embed:
                removed = self.module.retrieve_knowledge_chunks(self.db, "核查题干范围", "summary", limit=4)
                self.assertEqual(embed.call_count, 1)  # Deleting a node does not embed the remaining cards.
            self.assertNotIn("knowledge:summary:new-card", [item["knowledge_id"] for item in removed])

    def test_duplicate_legacy_rows_keep_the_latest_stable_card(self):
        self._insert(
            101,
            "knowledge:summary:stable-card",
            "更新后的范围限定方法",
            "新版卡片说明如何对照题干范围筛选材料信息。",
            {"module": "summary", "kind": "method", "tags": ["范围", "更新"]},
        )
        rows = self.module._knowledge_rows(self.db)
        stable_rows = [row for row in rows if row["knowledge_id"] == "knowledge:summary:stable-card"]
        self.assertEqual(len(stable_rows), 1)
        self.assertEqual(stable_rows[0]["id"], 101)
        results = self.module.retrieve_knowledge_chunks(
            self.db, "范围限定筛选材料", "summary", limit=4
        )
        result = next(item for item in results if item["_metadata"]["knowledge_id"] == "knowledge:summary:stable-card")
        self.assertEqual(result["id"], 101)

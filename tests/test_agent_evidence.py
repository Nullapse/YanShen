import json
import sqlite3
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from gongkao.agent_evidence import compact_search, effective_filters, read_source
from gongkao.agent_rag import cards_from_module_context, normalize_query_plan
from gongkao.agent_react import observation, validate_call
from tests import test_agent_react


class EvidenceTests(unittest.TestCase):
    def test_selected_review_with_guidance_words_keeps_entity_scope(self):
        plan = normalize_query_plan(None, "复盘这道概括题，找出遗漏的要点", "review", [17], "summary")
        self.assertEqual(plan["scope"], "current_attempt")
        plan = normalize_query_plan(None, "短评如何写，有哪些结构", "review", [17], "document")
        self.assertEqual(plan["action"], "guide")

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
          CREATE TABLE questions(id INTEGER, question_type TEXT,region TEXT,year INTEGER,
                                 title TEXT,prompt TEXT,requirements TEXT,paper_id INTEGER);
          CREATE TABLE attempts(id INTEGER,question_id INTEGER,answer_text TEXT,personal_note TEXT);
          CREATE TABLE grading_reports(id INTEGER,attempt_id INTEGER,report_text TEXT,created_at TEXT);
          INSERT INTO questions VALUES(42,'归纳概括','浙江',2024,'合成题','概括材料一','准确',1);
          INSERT INTO questions VALUES(43,'综合分析','江苏',2023,'其他题','分析','准确',2);
          INSERT INTO attempts VALUES(17,42,'原始完整作答','笔记');
          INSERT INTO attempts VALUES(18,43,'其他作答','其他笔记');
          INSERT INTO grading_reports VALUES(91,17,'','2026-01-01');
          INSERT INTO grading_reports VALUES(92,17,'另一份报告','2026-01-02');
        """)
        self.text = "报告正文与材料遗漏。" * 500 + "尾部关键信息"
        self.conn.execute("UPDATE grading_reports SET report_text=? WHERE id=91", (self.text,))
        self.card = {
            "evidence_id": "grading_report:91",
            "source_type": "grading_report",
            "question_id": 42,
            "attempt_id": 17,
            "title": "报告",
            "content": self.text,
            "metadata": {"source_id": 91},
        }
        self.state = {"module": "summary", "evidence_catalog": {"grading_report:91": self.card}}

    def test_short_discovery_keeps_entity_locator_without_sending_full_state(self):
        context = {
            "evidence_cards": [self.card],
            "query_plan": {"sources": ["grading_report"]},
            "module_context": {"evidence_chunks": [{"body": self.text}]},
        }
        change = compact_search(self.conn, {}, context, {"top_k": 1}, {"year": 2024})
        observed = json.loads(observation(change))["data"]
        self.assertEqual(observed["evidence_cards"][0]["evidence_id"], "grading_report:91")
        self.assertEqual(len(observed["evidence_cards"][0]["content"]), 240)
        self.assertNotIn("module_context", observed)
        self.assertNotIn("尾部关键信息", json.dumps(observed, ensure_ascii=False))
        self.assertIn("grading_report:91", change["evidence_catalog"])

    def test_paging_recovers_exact_report_to_the_end(self):
        args = {"evidence_id": "grading_report:91", "length": 1000}
        pages = []
        while True:
            result = read_source(self.conn, self.state, args)["source_detail"]
            pages.append(result["text"])
            if result["next_offset"] is None:
                break
            args.update(offset=result["next_offset"], revision=result["revision"], section=result["section"])
        self.assertEqual("".join(pages), "[grading_report:91]\n" + self.text)
        self.assertNotIn("另一份报告", "".join(pages))

    def test_revision_change_rejects_mixed_pages(self):
        first = read_source(self.conn, self.state, {"evidence_id": "grading_report:91"})["source_detail"]
        self.conn.execute("UPDATE grading_reports SET report_text='changed' WHERE id=91")
        with self.assertRaisesRegex(ValueError, "变化"):
            read_source(
                self.conn,
                self.state,
                {"evidence_id": "grading_report:91", "offset": 1600, "revision": first["revision"]},
            )

    def test_unknown_ids_and_cross_attempt_scope_are_rejected(self):
        for args in ({"evidence_id": "grading_report:92"}, {"attempt_id": 18}):
            with self.assertRaises(ValueError):
                read_source(self.conn, self.state, args)
        state = {**self.state, "subject_ids": [18], "context_plan": {"rag_query_plan": {"scope": "current_attempt"}}}
        with self.assertRaises(ValueError):
            read_source(self.conn, state, {"evidence_id": "grading_report:91"})

    def test_selected_attempt_can_load_question_but_notes_scope_cannot_load_answer(self):
        result = read_source(self.conn, {"subject_ids": [17]}, {"attempt_id": 17, "section": "question"})
        self.assertIn("概括材料一", result["source_detail"]["text"])
        state = {"subject_ids": [17], "context_plan": {"rag_query_plan": {"scope": "notes_only"}}}
        with self.assertRaises(ValueError):
            read_source(self.conn, state, {"attempt_id": 17, "section": "answer"})

    def test_filters_and_parameter_validation(self):
        self.assertEqual(effective_filters({"module": "summary", "filters": {"question_type": ""}}, {})["question_type"], "归纳概括")
        for args in ({"region": "江苏"}, {"question_type": "综合分析"}, {"year": 2023}):
            with self.assertRaises(ValueError):
                effective_filters({"module": "summary", "filters": {"region": "浙江", "year": 2024}}, args)
        for args in (
            {"query": "概括", "top_k": True},
            {"query": "概括", "sources": ["secret"]},
            {"query": "概括", "year": 0},
        ):
            with self.assertRaises(ValueError):
                validate_call({}, "search_evidence", args)
        for args in ({}, {"attempt_id": 17, "evidence_id": "x"}, {"attempt_id": 17, "offset": -1}):
            with self.assertRaises(ValueError):
                validate_call({}, "read_source", args)

    def test_different_sources_under_same_attempt_have_distinct_ids(self):
        cards = cards_from_module_context(
            {
                "evidence_chunks": [
                    {"source_type": "grading_report", "source_id": 91, "attempt_id": 17},
                    {"source_type": "personal_note", "source_id": 17, "attempt_id": 17},
                ]
            }
        )
        self.assertEqual([c["evidence_id"] for c in cards], ["grading_report:91", "personal_note:17"])

    def test_filtered_discovery_never_registers_excluded_entity(self):
        context = {"evidence_cards": [self.card], "query_plan": {"sources": ["grading_report"]}}
        change = compact_search(self.conn, {}, context, {}, {"year": 2023})
        self.assertEqual(change["evidence_catalog"], {})

    def test_graph_discovery_then_read_receives_catalog_and_full_page(self):
        def execute(state, name, args):
            if name == "search_evidence":
                return compact_search(
                    self.conn,
                    state,
                    {"evidence_cards": [self.card], "query_plan": {"sources": ["grading_report"]}},
                    args,
                    {},
                )
            return read_source(self.conn, state, args)

        responses = [
            AIMessage(content="", tool_calls=[test_agent_react.call("search_evidence", {"query": "概括"}, "c1")]),
            AIMessage(
                content="",
                tool_calls=[test_agent_react.call("read_source", {"evidence_id": "grading_report:91"}, "c2")],
            ),
            AIMessage(content="根据报告分析"),
        ]
        result, client, _, _ = test_agent_react.ReactGraphTests().run_graph(responses, execute=execute)
        self.assertIn("grading_report:91", result["evidence_catalog"])
        detail = [m for m in client.invoke.call_args.args[0] if isinstance(m, ToolMessage)][-1]
        self.assertTrue(json.loads(detail.content)["data"]["source_detail"]["available"])

    def test_deleted_entity_returns_unavailable(self):
        self.conn.execute("DELETE FROM grading_reports WHERE id=91")
        self.assertFalse(
            read_source(self.conn, self.state, {"evidence_id": "grading_report:91"})["source_detail"]["available"]
        )

    def test_material_and_reference_pages_read_original_entity(self):
        self.conn.executescript("""
            CREATE TABLE paper_materials(id INTEGER,paper_id INTEGER,material_number INTEGER,title TEXT,content TEXT);
            CREATE TABLE reference_answers(id INTEGER,question_id INTEGER,answer_text TEXT,scoring_points TEXT);
            INSERT INTO paper_materials VALUES(201,1,1,'材料一','材料原文');
            INSERT INTO paper_materials VALUES(202,2,1,'其他卷材料','不得读取');
            INSERT INTO reference_answers VALUES(301,42,'参考全文','采分点');
        """)
        for kind, source_id, expected in (("material", 201, "材料原文"), ("reference_answer", 301, "参考全文")):
            identifier = f"{kind}:{source_id}"
            card = {**self.card, "source_type": kind, "evidence_id": identifier, "metadata": {"source_id": source_id}}
            state = {**self.state, "evidence_catalog": {identifier: card}}
            detail = read_source(self.conn, state, {"evidence_id": identifier})["source_detail"]
            self.assertTrue(detail["available"])
            self.assertIn(expected, detail["text"])
            self.assertNotIn("不得读取", detail["text"])

    def test_knowledge_and_note_scope_can_read_their_own_source(self):
        knowledge = {"evidence_id": "knowledge:demo", "source_type": "knowledge"}
        note = {"evidence_id": "personal_note:17", "source_type": "personal_note", "attempt_id": 17, "question_id": 42}
        state = {
            "context_plan": {"rag_query_plan": {"scope": "notes_only"}},
            "evidence_catalog": {"knowledge:demo": knowledge, "personal_note:17": note},
        }
        with patch(
            "gongkao.agent_modules.load_knowledge_items", return_value=[{"id": "knowledge:demo", "body": "知识全文"}]
        ):
            detail = read_source(self.conn, state, {"evidence_id": "knowledge:demo"})["source_detail"]
            self.assertIn("知识全文", detail["text"])
        detail = read_source(self.conn, state, {"evidence_id": "personal_note:17"})["source_detail"]
        self.assertIn("笔记", detail["text"])

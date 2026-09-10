import json
import sqlite3
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from gongkao.agent_selected import MAX_SELECTED_BYTES, prepare_selected_evidence
from gongkao.db import SCHEMA
from tests import test_agent_evidence, test_agent_react


class SelectedEvidenceTests(unittest.TestCase):
    def test_real_schema_selected_review_without_mocked_data_loading(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("""INSERT INTO questions(id,question_code,exam_type,year,region,
                     question_type,title,prompt,materials,requirements)
                     VALUES(42,'synthetic','省考',2024,'浙江','归纳概括','题目','概括措施','','准确')""")
        conn.execute("INSERT INTO attempts(id,question_id,answer_text) VALUES(17,42,'原文作答')")
        conn.execute("INSERT INTO grading_reports(id,attempt_id,report_text) VALUES(91,17,'原文报告')")
        payload, catalog = prepare_selected_evidence(conn, {
            "subject_ids": [17], "module": "summary",
            "context_plan": {"rag_query_plan": {"scope": "current_attempt", "action": "review"}},
        })
        self.assertIn("grading_report:91", catalog)
        self.assertIn("原文报告", json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["partial"])

    def prepare(self, review):
        fixture = test_agent_evidence.EvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch("gongkao.agent_selected.get_attempts_review_context", return_value=review):
            return prepare_selected_evidence(fixture.conn, {
                "subject_ids": [17], "module": "summary",
                "context_plan": {"rag_query_plan": {"scope": "current_attempt", "action": "review"}},
            })

    def test_selected_sources_are_raw_and_can_answer_without_tools(self):
        prepared = self.prepare({
            "question": {"id": 42, "prompt": "clipped placeholder"},
            "attempt": {"id": 17, "question_id": 42, "answer_text": "clipped answer"},
        })
        payload, catalog = prepared
        self.assertIn("原始完整作答", payload["sources"][1]["text"])
        self.assertNotIn("clipped answer", json.dumps(payload))
        result, client, tool, _ = test_agent_react.ReactGraphTests().run_graph(
            [AIMessage(content="已根据题干和本次作答完成复盘。")],
            task_type="review", subject_ids=[17], user_goal="复盘本题", prepared=prepared,
        )
        tool.assert_not_called()
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["evidence_catalog"], catalog)
        self.assertIn("原始完整作答", client.invoke.call_args.args[0][-1].content)

    def test_long_sources_fit_budget_and_expose_unread_pages(self):
        payload, _ = self.prepare({
            "attempt": {"id": 17, "question_id": 42},
            "reports": [{"id": 91, "report_text": "summary"}],
        })
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), MAX_SELECTED_BYTES)
        self.assertTrue(payload["partial"])
        report = next(p for p in payload["sources"] if p["evidence_id"] == "grading_report:91")
        self.assertEqual(report["next_offset"], len(report["text"]))
        self.assertEqual(len(report["revision"]), 24)

    def test_unknown_history_does_not_load_selected_sources(self):
        with patch("gongkao.agent_selected.get_attempts_review_context") as load:
            self.assertEqual(prepare_selected_evidence(None, {
                "subject_ids": [], "context_plan": {"rag_query_plan": {"scope": "all"}},
            }), ({}, {}))
        load.assert_not_called()

    def test_missing_entity_is_explicit(self):
        payload, catalog = self.prepare({})
        self.assertEqual(payload["sources"], [])
        self.assertTrue(payload["partial"])
        self.assertEqual(catalog, {})

    def test_evidence_is_not_reinjected_as_a_tool_observation(self):
        prepared = self.prepare({"attempt": {"id": 17, "question_id": 42}})
        _, client, _, _ = test_agent_react.ReactGraphTests().run_graph([
            AIMessage(content="", tool_calls=[test_agent_react.call("read_source", {"attempt_id": 17, "section": "materials"})]),
            AIMessage(content="补充材料后完成。"),
        ], task_type="review", subject_ids=[17], user_goal="复盘本题", prepared=prepared,
            execute=lambda *args: {"source_detail": {"text": "补充材料"}})
        for request in client.invoke.call_args_list:
            body = "\n".join(m.content for m in request.args[0])
            self.assertEqual(body.count("原始完整作答"), 1)

    def test_many_long_pages_keep_a_strict_total_budget(self):
        review = {"attempt": {"id": 17, "question_id": 42},
                  "reports": [{"id": i, "report_text": "摘要"} for i in range(30)]}
        with patch("gongkao.agent_selected.read_source", side_effect=lambda conn, state, args: {
            "source_detail": {"evidence_id": args["evidence_id"], "available": True,
                              "text": "中文长原文" * 400, "offset": 0, "next_offset": 2000,
                              "revision": "a" * 24, "total_chars": 9000}
        }):
            payload, _ = self.prepare(review)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), MAX_SELECTED_BYTES)
        self.assertGreater(payload["omitted_sources"], 0)
        self.assertTrue(payload["partial"])
        for page in payload["sources"]:
            self.assertEqual(page["next_offset"], len(page["text"]))

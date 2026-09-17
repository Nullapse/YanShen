import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, AIMessageChunk

from gongkao.agent_context import MAX_INPUT_UNITS, input_units, stable_prefix
from gongkao.agent_policy import response_policy
from gongkao.agent_progress import clear_progress, read_progress, update_progress
from gongkao.agent_prompts import build_agent_messages, with_conversation_history, with_long_term_memories
from gongkao.agent_selected import prepare_selected_evidence
from gongkao.db import SCHEMA
from tests import test_agent_react
from tests.test_agent_react import call


class BalancedCoachTests(unittest.TestCase):
    def test_long_task_does_not_consume_separate_history_allowance(self):
        history = [("system", "规则" * 2000), ("system", "用户每天只有15分钟"),
                   ("human", "只练闭环"), ("assistant", "先补需求台账"), ("human", "本题原文" * 1000)]
        prefix, removed = stable_prefix(history, [])
        self.assertEqual(removed, 0)
        self.assertIn("用户每天只有15分钟", [m.content for m in prefix])
        self.assertLessEqual(input_units(prefix, []), MAX_INPUT_UNITS - 4000)

    def test_old_turn_removed_as_pair_and_preferences_survive(self):
        prefix, removed = stable_prefix([
            ("system", "规则"), ("system", "每天15分钟"),
            ("human", "旧问题" * 1500), ("assistant", "旧回答"),
            ("human", "新问题"), ("assistant", "新建议"), ("human", "追问"),
        ], [], history_limit=1500)
        self.assertEqual(removed, 2)
        self.assertEqual([m.content for m in prefix], ["规则", "每天15分钟", "新问题", "新建议", "追问"])

    def test_followup_keeps_recommendation_after_first_350_characters(self):
        messages = with_conversation_history(
            [("system", "规则"), ("human", "第二道怎么练")],
            [{"role": "assistant", "content": "第一道说明" * 100 + "第二道题是question_id 42"}],
        )
        self.assertIn("question_id 42", messages[-2][1])

    def test_plain_answer_contract_has_no_conflicting_json_requirement(self):
        policy = response_policy({"user_goal": "只给一个动作"}, {"scope": "current_attempt"})
        messages = build_agent_messages("review", "只给一个动作", {}, [], {}, policy=policy)
        messages = with_long_term_memories(messages, [{"memory_key": "response_style", "content": "简洁"}])
        prompt = "\n".join(m[1] for m in messages)
        self.assertIn("不附 JSON", prompt)
        self.assertNotIn("JSON 仍需完整", prompt)
        self.assertEqual(policy["model_calls"], 2)
        self.assertLess(len(messages[0][1].encode()), 4500)

    def test_plans_offer_cards_but_no_recommendation_is_respected(self):
        self.assertTrue(response_policy({"user_goal": "推荐题"}, {"action": "recommend"})["structured"])
        self.assertFalse(response_policy({"user_goal": "不要推荐题"}, {"action": "recommend"})["structured"])
        policy = response_policy({"user_goal": "全面复盘", "subject_ids": [1, 2]}, {"scope": "current_attempt"})
        self.assertEqual(policy["tier"], "analysis")

    def test_selected_followup_stays_local_but_explicit_guidance_can_leave(self):
        from gongkao.agent_rag import normalize_query_plan

        for goal in ("那具体怎么练？只给今天的安排。", "按你说的如何修改？"):
            self.assertEqual(normalize_query_plan(None, goal, "review", [1])["scope"], "current_attempt")
        self.assertEqual(normalize_query_plan(None, "不看作答，只讲方法：短评如何写？", "review", [1])["action"], "guide")

    def test_discovered_history_ids_can_be_read_but_unseen_ids_cannot(self):
        from contextlib import nullcontext

        from gongkao.agent_evidence import read_source
        from gongkao.agent_react import execute_tool
        from tests.test_agent_evidence import EvidenceTests

        fixture = EvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch("gongkao.agent_react.connect", return_value=nullcontext(fixture.conn)), patch(
            "gongkao.agent_react.load_user_context", return_value={"recent_attempts": [
                {"id": 17, "question_id": 42, "question_type": "归纳概括", "region": "浙江", "year": 2024},
                {"id": 18, "question_id": 43, "question_type": "综合分析", "region": "江苏", "year": 2023},
            ]},
        ):
            change = execute_tool({"db_path": "unused", "filters": {"region": "浙江"}}, "load_user_context", {})
        state = {**change, "filters": {"region": "浙江"}}
        self.assertIn("原始完整作答", read_source(fixture.conn, state, {"attempt_id": 17})["source_detail"]["text"])
        with self.assertRaises(ValueError):
            read_source(fixture.conn, state, {"attempt_id": 18})

    def test_brief_task_stops_after_two_calls_with_complete_evidence(self):
        result, client, tool, _ = test_agent_react.ReactGraphTests().run_graph([
            AIMessage(content="", tool_calls=[call()]), AIMessage(content="今天先补回访机制。"),
        ], user_goal="只给一个动作")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["budget_escalations"], 0)
        self.assertEqual(client.bind_tools.call_args.kwargs["tool_choice"], "none")

    def test_partial_source_allows_one_more_round_then_stops(self):
        result, client, _, _ = test_agent_react.ReactGraphTests().run_graph([
            AIMessage(content="", tool_calls=[call(identifier="1")]),
            AIMessage(content="", tool_calls=[call(identifier="2")]),
            AIMessage(content="当前证据有限，先修改已有依据支持的部分。"),
        ], user_goal="只给一个动作", execute=lambda *args: {
            "source_detail": {"available": True, "text": "部分原文", "next_offset": 1200},
        })
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["budget_escalations"], 1)
        self.assertEqual(client.bind_tools.call_args.kwargs["tool_choice"], "none")

    def test_streamed_answer_accumulates_and_keeps_usage(self):
        progress = Mock()
        result, client, _, complete = test_agent_react.ReactGraphTests().run_graph([], on_progress=progress, chunks=[iter([
            AIMessageChunk(content="先补"), AIMessageChunk(content="回访机制。",
                usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}),
        ])])
        self.assertEqual(result["final_text"], "先补回访机制。")
        self.assertEqual(result["react_messages"][-1].usage_metadata["input_tokens"], 100)
        progress.assert_any_call("answering", "先补回访机制。")
        client.invoke.assert_not_called()
        complete.assert_called_once()

    def test_streamed_tool_fragments_are_joined_and_not_shown_as_answer(self):
        progress = Mock()
        result, _, tool, _ = test_agent_react.ReactGraphTests().run_graph([], on_progress=progress, chunks=[iter([
            AIMessageChunk(content="", tool_call_chunks=[{
                "id": "c1", "name": "load_user_context", "args": "{", "index": 0}]),
            AIMessageChunk(content="", tool_call_chunks=[{
                "id": None, "name": None, "args": "}", "index": 0}]),
        ]), iter([AIMessageChunk(content="资料显示样本有限。")])])
        tool.assert_called_once()
        self.assertEqual(result["model_calls"], 2)
        self.assertTrue(all("load_user_context" not in str(c) for c in progress.call_args_list))

    def test_progress_is_isolated_expires_and_hides_json(self):
        self.addCleanup(clear_progress, "test-db", 1)
        update_progress("test-db", 1, "answering", '正文\n```json\n{"hidden":true}')
        self.assertEqual(read_progress("test-db", 1)["text"], "正文\n")
        self.assertEqual(read_progress("other-db", 1), {})
        with patch("gongkao.agent_progress.time.monotonic", return_value=10**15):
            self.assertEqual(read_progress("test-db", 1), {})

    def test_card_renderer_accepts_minimal_fields_and_plain_answers(self):
        from gongkao.web.runtime import agent_response_html, split_agent_structured_json

        data = {"next_actions": [{"action": "补齐回访", "target": "原题", "timebox": "10分钟"}],
                "recommended_questions": [{"question_id": 1, "title": "养老服务", "reason": "补练遗漏"}]}
        raw = "先练原题。\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"
        body, parsed = split_agent_structured_json(raw)
        self.assertEqual(body, "先练原题。")
        self.assertEqual(parsed, data)
        html = agent_response_html(raw)
        self.assertIn("补齐回访", html)
        self.assertIn("养老服务", html)
        self.assertNotIn("```", html)
        self.assertNotIn("agent-structured-output", agent_response_html("直接补上每月回访。"))

    def test_status_endpoint_returns_only_pending_runs_progress(self):
        from contextlib import nullcontext

        from gongkao.web.controllers.agent import AgentController

        conn = Mock()
        latest = {"id": 9, "run_id": 3, "role": "assistant", "message_type": "pending"}
        handler = Mock(db_path="status-db")
        self.addCleanup(clear_progress, "status-db", 3)
        update_progress("status-db", 3, "answering", "正在补齐回访机制。")
        with patch("gongkao.web.controllers.agent.connect", return_value=nullcontext(conn)), patch(
            "gongkao.web.controllers.agent._cleanup_orphaned_pending_messages"
        ), patch("gongkao.web.controllers.agent.get_run_steps", return_value=[]), patch(
            "gongkao.web.controllers.agent.render_agent_message_row", return_value="完成"
        ):
            conn.execute.return_value.fetchone.side_effect = [{"id": 7}, latest]
            AgentController.handle_agent_conversation_status(handler, "/agent/conversations/7/status")
            payload = handler.send_json.call_args.args[0]
            self.assertEqual(payload["progress"]["text"], "正在补齐回访机制。")
            self.assertEqual(payload["message_html"], "")
            conn.execute.return_value.fetchone.side_effect = [{"id": 7}, {**latest, "message_type": "suggestion"}]
            AgentController.handle_agent_conversation_status(handler, "/agent/conversations/7/status")
            self.assertEqual(handler.send_json.call_args.args[0]["progress"], {})

    def test_loss_review_prioritizes_report_before_long_materials(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO papers(id,paper_code,paper_name,exam_type,year,region) VALUES(1,'s','s','省考',2025,'浙江')")
        conn.execute("""INSERT INTO questions(id,question_code,paper_id,exam_type,year,region,
                     question_type,title,prompt,materials,requirements)
                     VALUES(1,'s',1,'省考',2025,'浙江','归纳概括','题目','概括措施','','准确')""")
        for i in range(6):
            conn.execute("INSERT INTO paper_materials(paper_id,material_number,content) VALUES(1,?,?)",
                         (i + 1, "很长的背景材料。" * 500))
        conn.execute("INSERT INTO attempts(id,question_id,answer_text) VALUES(1,1,'作答原文')")
        conn.execute("INSERT INTO grading_reports(id,attempt_id,report_text) VALUES(1,1,'主要失分是遗漏需求回访。')")
        payload, _ = prepare_selected_evidence(conn, {
            "subject_ids": [1], "user_goal": "根据批改报告找出失分原因", "response_policy": {"tier": "brief"},
            "context_plan": {"rag_query_plan": {"scope": "current_attempt", "action": "review"}},
        })
        self.assertIn("主要失分是遗漏需求回访", json.dumps(payload, ensure_ascii=False))
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), 7000)
        self.assertEqual(payload["sources"][2]["evidence_id"], "grading_report:1")

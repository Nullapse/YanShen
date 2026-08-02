import json
import shutil
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from gongkao.agent_chat import start_or_continue_chat, start_or_continue_chat_async
from gongkao.agent_coach import filters_from_text, infer_intent
from gongkao.agent_eval import (
    EVAL_DATASET_PATH,
    MULTITURN_DATASET_PATH,
    compare_eval_reports,
    evaluate_deterministic_suite,
    load_eval_cases,
    load_multiturn_cases,
    retrieval_ranking_metrics,
    run_eval_suite,
    run_multiturn_eval_suite,
    write_eval_report,
)
from gongkao.agent_graph import AgentRunError, _compact_concise_response, _rerank_evidence_with_llm, run_agent
from gongkao.agent_indexer import AgentIndexWorker
from gongkao.agent_modules import (
    ensure_agent_context_index,
    load_knowledge_items,
    rebuild_agent_context_index,
    retrieve_knowledge_evidence,
    retrieve_module_evidence,
)
from gongkao.agent_prompts import build_agent_messages, with_conversation_history, with_long_term_memories
from gongkao.agent_rag import (
    build_rag_context,
    fallback_query_plan,
    normalize_query_plan,
    route_rag,
    summarize_evidence_cards,
)
from gongkao.agent_retrieval.indexing import (
    coalesce_agent_context_pending,
    finalize_agent_context_index,
    process_agent_context_pending_batch,
)
from gongkao.agent_store import (
    active_memories,
    add_message,
    add_step,
    clear_memories,
    complete_run,
    conversation_context,
    create_conversation,
    delete_conversation,
    delete_memory,
    extract_explicit_memories,
)
from gongkao.agent_tools import (
    get_attempt_review_context,
    get_attempts_review_context,
    input_summary,
    load_user_context,
    retrieve_candidates,
)
from gongkao.db import connect, init_db, sync_seed_content
from gongkao.skill_graph import rebuild_skill_graph
from tests.asset_bundle import (
    read_server_application,
    read_static_scripts,
    read_static_styles,
)

REGION_ZHEJIANG = "\u6d59\u6c5f"
QUESTION_TYPE_ANALYSIS = "\u7efc\u5408\u5206\u6790"


class FakeGraph:
    def invoke(self, state, config=None):
        with connect(state["db_path"]) as conn:
            user_context = load_user_context(conn)
            candidates = retrieve_candidates(conn, state.get("filters") or {}, limit=8)
            review_context = {}
            if state.get("task_type") == "review":
                subject_ids = state.get("subject_ids") or []
                if subject_ids:
                    review_context = get_attempts_review_context(conn, subject_ids)
                elif state.get("subject_id"):
                    review_context = get_attempt_review_context(conn, state.get("subject_id"))
            add_step(conn, state["run_id"], "tool", "load_user_context", {}, user_context)
            add_step(
                conn,
                state["run_id"],
                "tool",
                "retrieve_candidates",
                state.get("filters") or {},
                {
                    "candidate_count": len(candidates),
                    "review_context": bool(review_context),
                    "review_count": len(review_context.get("attempt_reviews") or ([review_context] if review_context else [])),
                },
            )
            rag_context = build_rag_context(
                conn,
                state.get("task_type"),
                state.get("user_goal", ""),
                subject_ids=state.get("subject_ids") or [],
                module=state.get("module") or "overview",
                filters=state.get("filters") or {},
                user_context=user_context,
                candidates=candidates,
                review_context=review_context,
                query_plan=fallback_query_plan(
                    state.get("user_goal", ""),
                    state.get("task_type"),
                    state.get("subject_ids") or [],
                    state.get("module") or "overview",
                ),
            )
            add_step(
                conn,
                state["run_id"],
                "tool",
                "build_rag_context",
                {"task_type": state.get("task_type")},
                {
                    "rag_route": rag_context["rag_route"],
                    "query_plan": rag_context.get("query_plan") or {},
                    "retrieval_policy": rag_context["retrieval_policy"],
                    "evidence_sufficiency": rag_context.get("evidence_sufficiency") or {},
                    "evidence_card_count": len(rag_context["evidence_cards"]),
                    "allowed_evidence_ids": (rag_context.get("grounding_contract") or {}).get("allowed_evidence_ids", [])[:12],
                    "current_attempt_only": (rag_context.get("grounding_contract") or {}).get("current_attempt_only", False),
                    "evidence_cards": summarize_evidence_cards(rag_context.get("evidence_cards", []), limit=12),
                },
            )
            final_text = (
                "## 推荐顺序\n- 基层治理复盘\n\n## 使用方式\n- 限时作答后复盘。"
                if state.get("task_type") == "recommend"
                else "## 训练判断\n推荐题目：基层治理复盘\n\n## 下一步\n- 完成本题。"
            )
            add_step(
                conn,
                state["run_id"],
                "llm",
                "ChatOpenAI",
                {"task_type": state.get("task_type")},
                {"output_preview": final_text},
            )
            complete_run(
                conn,
                state["run_id"],
                final_text,
                input_summary(state.get("task_type"), user_context, candidates, review_context),
            )
        return {}


class RecordingGraph(FakeGraph):
    def __init__(self):
        self.invocations = []

    def invoke(self, state, config=None):
        self.invocations.append({"state": dict(state), "config": config or {}})
        return super().invoke(state, config=config)


class AgentSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(".test_tmp")
        self.tmpdir.mkdir(exist_ok=True)
        self.db_file = self.tmpdir / f"{uuid4().hex}.sqlite3"
        init_db(self.db_file)
        with connect(self.db_file) as conn:
            conn.execute("UPDATE ai_settings SET mode = 'api', api_key = 'test-key', api_key_env = '' WHERE id = 1")
            conn.execute(
                """
                INSERT INTO questions (
                    question_code, exam_type, year, region, question_type,
                    title, prompt, materials, requirements, zhejiang_relevance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "AGENT-Q1",
                    "\u6d59\u6c5f\u7701\u8003",
                    2026,
                    REGION_ZHEJIANG,
                    QUESTION_TYPE_ANALYSIS,
                    "\u57fa\u5c42\u6cbb\u7406\u590d\u76d8",
                    "\u8bf7\u5206\u6790\u57fa\u5c42\u6cbb\u7406\u4e2d\u7684\u95ee\u9898\u3002",
                    "\u6750\u6599\u4e00\u3002",
                    "\u5168\u9762\u51c6\u786e\u3002",
                    5,
                ),
            )

    def tearDown(self):
        if self.db_file.exists():
            self.db_file.unlink()
        if self.tmpdir.exists() and not any(self.tmpdir.iterdir()):
            self.tmpdir.rmdir()

    def test_agent_tables_exist_and_api_run_persists_trace(self):
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            run_id = run_agent(
                self.db_file,
                "diagnosis",
                user_goal="\u5e2e\u6211\u5b89\u6392\u8bad\u7ec3",
                filters={"question_type": QUESTION_TYPE_ANALYSIS},
                auto_approve=True,
            )
        with connect(self.db_file) as conn:
            run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            self.assertEqual(run["status"], "completed")
            self.assertIn("\u8bad\u7ec3\u5224\u65ad", run["final_text"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_steps WHERE run_id = ?", (run_id,)).fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_plan_items WHERE run_id = ?", (run_id,)).fetchone()[0], 0)

    def test_skill_graph_enriches_weaknesses_and_question_candidates(self):
        with connect(self.db_file) as conn:
            graph = rebuild_skill_graph(conn, load_knowledge_items())
            self.assertGreaterEqual(graph["skills"], 60)
            self.assertGreater(graph["edges"], graph["skills"])
            conn.execute(
                """
                INSERT INTO agent_weakness_profile (
                    module, question_type, problem_type, frequency, severity
                ) VALUES ('analysis', ?, '结构表达', 3, 0.8)
                """,
                (QUESTION_TYPE_ANALYSIS,),
            )
            context = load_user_context(conn)
            candidates = retrieve_candidates(conn, {"question_type": QUESTION_TYPE_ANALYSIS})
        self.assertTrue(context["skill_gaps"])
        self.assertTrue(all(item["problem_type"] == "结构表达" for item in context["skill_gaps"]))
        self.assertTrue(candidates)
        self.assertTrue(candidates[0]["skill_targets"])

    def test_api_run_without_auto_approve_does_not_create_plan(self):
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            run_id = run_agent(
                self.db_file,
                "recommend",
                filters={"question_type": QUESTION_TYPE_ANALYSIS},
                auto_approve=False,
            )
        with connect(self.db_file) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM training_plan_items WHERE run_id = ?", (run_id,)).fetchone()[0],
                0,
            )

    def test_agent_run_does_not_modify_another_runs_plan_items(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            other_run_id = conn.execute(
                """
                INSERT INTO agent_runs (task_type, status, user_goal, final_text)
                VALUES (?, ?, ?, ?)
                """,
                ("recommend", "completed", "other run", "other result"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO training_plan_items (run_id, question_id, title, reason, target_date, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (other_run_id, question_id, "其他计划", "其他运行的数据", "2026-07-03", "todo"),
            )
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            run_id = run_agent(
                self.db_file,
                "recommend",
                filters={"question_type": QUESTION_TYPE_ANALYSIS},
                auto_approve=False,
            )
        with connect(self.db_file) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_plan_items WHERE run_id = ?", (run_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_plan_items WHERE run_id = ?", (other_run_id,)).fetchone()[0], 1)

    def test_chat_thread_saves_messages_and_associated_run(self):
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            conversation_id, run_id = start_or_continue_chat(
                self.db_file,
                user_text="\u6211\u4eca\u5929\u7ec3\u4ec0\u4e48\uff0c\u6362\u6210\u6d59\u6c5f\u7efc\u5408\u5206\u6790",
                auto_approve=True,
            )
        with connect(self.db_file) as conn:
            conversation = conn.execute(
                "SELECT * FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            self.assertEqual(conversation["status"], "active")
            messages = conn.execute(
                "SELECT * FROM agent_messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
            self.assertEqual(messages[-1]["run_id"], run_id)
            metadata = json.loads(messages[-1]["metadata_json"])
            self.assertEqual(metadata["rag"]["rag_route"], "recommend_questions")
            self.assertIn("evidence_sufficiency", metadata["rag"])
            self.assertGreater(metadata["rag"]["evidence_card_count"], 0)
            self.assertTrue(metadata["rag"]["evidence_cards"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_plan_items WHERE run_id = ?", (run_id,)).fetchone()[0], 0)

    def test_async_pending_message_is_bound_to_run_for_real_progress(self):
        with patch("gongkao.agent_chat.threading.Thread") as thread_class:
            conversation_id, pending_id = start_or_continue_chat_async(
                self.db_file,
                user_text="帮我分析一下最近问题",
            )
            thread_class.return_value.start.assert_called_once()
            self.assertTrue(thread_class.call_args.kwargs["daemon"])
        with connect(self.db_file) as conn:
            message = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ? AND conversation_id = ?",
                (pending_id, conversation_id),
            ).fetchone()
            self.assertEqual(message["message_type"], "pending")
            self.assertIsNotNone(message["run_id"])
            self.assertIsNotNone(conn.execute("SELECT id FROM agent_runs WHERE id = ?", (message["run_id"],)).fetchone())

    def test_index_rebuild_releases_write_lock_before_dense_inference(self):
        def probe_concurrent_write(_conn):
            with connect(self.db_file) as other:
                other.execute("PRAGMA busy_timeout = 100")
                other.execute(
                    "UPDATE ai_settings SET temperature = temperature WHERE id = 1"
                )
            return {"available": False, "updated": 0, "remaining": 0}

        with connect(self.db_file) as conn:
            with patch(
                "gongkao.agent_retrieval.indexing.sync_dense_embeddings",
                side_effect=probe_concurrent_write,
            ):
                rebuild_agent_context_index(conn)

    def test_chat_followup_inherits_filters_and_adds_constraint(self):
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            conversation_id, _ = start_or_continue_chat(
                self.db_file,
                user_text="\u63a8\u8350\u6d59\u6c5f\u7efc\u5408\u5206\u6790\u9898",
            )
            start_or_continue_chat(
                self.db_file,
                conversation_id=conversation_id,
                user_text="\u6362\u6210\u672a\u6279\u6539",
            )
        with connect(self.db_file) as conn:
            message = conn.execute(
                """
                SELECT metadata_json
                  FROM agent_messages
                 WHERE conversation_id = ? AND role = 'assistant'
              ORDER BY id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            filters = json.loads(message["metadata_json"])["filters"]
            self.assertEqual(filters["region"], REGION_ZHEJIANG)
            self.assertEqual(filters["question_type"], QUESTION_TYPE_ANALYSIS)
            self.assertEqual(filters["work_status"], "ungraded")

    def test_chat_followup_passes_same_thread_history_to_graph(self):
        graph = RecordingGraph()
        with patch("gongkao.agent_graph._graph_for", return_value=graph):
            conversation_id, _ = start_or_continue_chat(
                self.db_file,
                user_text="我主要备考浙江，回答尽量简洁",
            )
            start_or_continue_chat(
                self.db_file,
                conversation_id=conversation_id,
                user_text="那我下一题练什么",
            )
        self.assertEqual(len(graph.invocations), 1)
        second = graph.invocations[0]
        expected_thread = f"agent-conversation-{conversation_id}"
        self.assertEqual(second["config"]["configurable"]["thread_id"], expected_thread)
        self.assertEqual(second["state"]["conversation_id"], conversation_id)
        history = second["state"]["conversation_messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant", "user"])
        self.assertIn("浙江", history[0]["content"])
        self.assertIn("下一题", history[-1]["content"])

    def test_conversation_history_is_isolated_between_threads(self):
        graph = RecordingGraph()
        with patch("gongkao.agent_graph._graph_for", return_value=graph):
            first_conversation, _ = start_or_continue_chat(
                self.db_file,
                user_text="线程甲的目标是只练归纳概括",
            )
            second_conversation, _ = start_or_continue_chat(
                self.db_file,
                user_text="线程乙的目标是只练综合分析",
            )
            start_or_continue_chat(
                self.db_file,
                conversation_id=second_conversation,
                user_text="继续按刚才的目标推荐",
            )
        self.assertNotEqual(first_conversation, second_conversation)
        second_followup = graph.invocations[-1]["state"]
        rendered = "\n".join(message["content"] for message in second_followup["conversation_messages"])
        self.assertIn("线程乙", rendered)
        self.assertNotIn("线程甲", rendered)

    def test_long_conversation_builds_bounded_rolling_summary(self):
        with connect(self.db_file) as conn:
            conversation_id = create_conversation(conn, "长线程")
            for index in range(20):
                role = "user" if index % 2 == 0 else "assistant"
                content = "最早确认的目标是浙江省考" if index == 0 else f"第 {index} 条消息"
                add_message(conn, conversation_id, role, content)
            context = conversation_context(conn, conversation_id, recent_limit=6)
        self.assertEqual(context["message_count"], 20)
        self.assertEqual(context["summarized_count"], 14)
        self.assertEqual(len(context["messages"]), 6)
        self.assertIn("最早确认的目标是浙江省考", context["summary"])

    def test_prompt_history_excludes_duplicate_current_turn_and_keeps_summary(self):
        messages = with_conversation_history(
            [("system", "system"), ("human", "current task")],
            [
                {"role": "user", "content": "我备考浙江"},
                {"role": "assistant", "content": "已记录目标"},
                {"role": "user", "content": "那下一题呢"},
            ],
            "用户早期确认每天训练 30 分钟",
            "那下一题呢",
        )
        self.assertEqual(messages[0], ("system", "system"))
        self.assertEqual(messages[-1], ("human", "current task"))
        rendered = "\n".join(content for _, content in messages)
        self.assertIn("每天训练 30 分钟", rendered)
        self.assertIn("我备考浙江", rendered)
        self.assertEqual(rendered.count("那下一题呢"), 0)

    def test_long_term_memory_only_extracts_explicit_user_facts(self):
        memories = extract_explicit_memories("我主要备考浙江省考，回答尽量简洁，每天训练 30 分钟")
        by_key = {item["memory_key"]: item for item in memories}
        self.assertEqual(by_key["target_exam"]["content"], "浙江省考")
        self.assertEqual(by_key["response_style"]["memory_type"], "procedural")
        self.assertIn("每天训练 30 分钟", by_key["training_rhythm"]["content"])
        self.assertEqual(extract_explicit_memories("那下一题练什么"), [])
        corrected = extract_explicit_memories("以后还是简洁一点")
        self.assertEqual(corrected[0]["memory_key"], "response_style")
        prepared = extract_explicit_memories("我准备江苏省考")
        self.assertEqual(prepared[0]["content"], "江苏省考")

    def test_followup_filters_apply_corrections_and_exclusions(self):
        corrected = filters_from_text(
            "先不看归纳概括，改看提出对策",
            {"question_type": "归纳概括", "region": "浙江"},
        )
        self.assertEqual(corrected["question_type"], "提出对策")
        national = filters_from_text("改成国考的", corrected)
        self.assertEqual(national["region"], "全国")
        excluded = filters_from_text("不要综合写作", {})
        self.assertEqual(excluded["exclude_question_type"], "综合写作")

    def test_followup_intent_keeps_thread_action_without_treating_reference_as_review(self):
        self.assertEqual(infer_intent("按刚才目标推荐", "today", True), "next_question")
        self.assertEqual(infer_intent("为什么是这道", "next_question", True), "next_question")
        self.assertEqual(infer_intent("刚才那条证据说明什么", "today", True), "today")
        plan = normalize_query_plan(
            {"action": "diagnose", "scope": "module_history", "sources": ["attempt"]},
            "为什么是这道",
            "recommend",
            [],
            "overview",
        )
        self.assertEqual(plan["action"], "recommend")
        self.assertEqual(plan["scope"], "candidate_questions")
        expanded = normalize_query_plan(
            {"action": "diagnose", "scope": "current_attempt", "sources": ["attempt"]},
            "再结合我的全部历史看",
            "review",
            [122],
            "overview",
        )
        self.assertEqual(expanded["scope"], "overall_history")
        self.assertIn("grading_report", expanded["sources"])
        missing_attempt = normalize_query_plan(
            {"action": "explain", "scope": "current_attempt", "sources": ["grading_report"]},
            "刚才那条证据说明什么",
            "diagnosis",
            [],
            "summary",
        )
        self.assertEqual(missing_attempt["scope"], "module_history")
        self.assertEqual(missing_attempt["module"], "summary")

    def test_concise_response_compactor_is_bounded_and_keeps_schema(self):
        structured = {
            "summary": "最大问题是对策缺乏材料依据" * 20,
            "weaknesses": [{"name": "对策针对性", "severity": "high", "evidence_refs": ["report:1", "report:2", "report:3"], "reason": "对策经常脱离材料且不够具体" * 10}],
            "next_actions": [{"action": "逐条回到材料标注依据" * 10, "target": "提出对策", "timebox": "10分钟"}],
            "recommended_questions": [],
        }
        compacted = _compact_concise_response("原回答", structured)
        body = compacted.split("```json", 1)[0].strip()
        self.assertLessEqual(len(body), 260)
        payload = json.loads(compacted.split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertEqual(set(payload), {"summary", "weaknesses", "next_actions", "recommended_questions"})
        self.assertEqual(len(payload["weaknesses"][0]["evidence_refs"]), 2)

    def test_llm_reranker_only_accepts_existing_evidence_ids(self):
        class Response:
            content = '{"ordered_evidence_ids":["evidence:3","invented","evidence:1"],"reason":"更相关"}'
            usage_metadata = {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}

        class Llm:
            def invoke(self, messages):
                return Response()

        cards = [
            {"evidence_id": f"evidence:{index}", "source_type": "knowledge", "title": str(index), "content": "内容"}
            for index in range(1, 5)
        ]
        reranked, metadata = _rerank_evidence_with_llm(
            Llm(),
            "问题",
            {"evidence_cards": cards, "grounding_contract": {"allowed_evidence_ids": [card["evidence_id"] for card in cards]}},
        )
        self.assertEqual([card["evidence_id"] for card in reranked["evidence_cards"][:2]], ["evidence:1", "evidence:2"])
        self.assertEqual(metadata["status"], "ranked_by_score")

    def test_multiturn_runner_scores_continuity_without_live_judge(self):
        case = {
            "id": "local_multiturn",
            "title": "本地多轮",
            "category": "reference",
            "turns": [{"user": "推荐下一题"}, {"user": "那具体怎么练"}],
            "expected_context_terms": ["推荐下一题"],
            "final_expected_keywords": ["限时", "复盘"],
        }
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            results = run_multiturn_eval_suite(
                self.db_file,
                cases=[case],
                run_judge=False,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["metrics"]["thread_continuity"], 1.0)
        self.assertEqual(results[0]["metrics"]["final_keyword_accuracy"], 1.0)
        self.assertEqual(results[0]["metrics"]["turn_count"], 2)

    def test_context_index_rebuilds_only_after_source_data_changes(self):
        with connect(self.db_file) as conn:
            self.assertFalse(ensure_agent_context_index(conn))
            rebuild_agent_context_index(conn)
            self.assertFalse(ensure_agent_context_index(conn))
            conn.execute("UPDATE questions SET title = title || '（更新）' WHERE question_code = 'AGENT-Q1'")
            dirty = conn.execute("SELECT dirty FROM agent_context_index_state WHERE id = 1").fetchone()[0]
            self.assertEqual(dirty, 1)
            self.assertFalse(ensure_agent_context_index(conn))
        self.assertEqual(process_agent_context_pending_batch(self.db_file), 1)
        self.assertTrue(finalize_agent_context_index(self.db_file))

    def test_static_queue_coalesces_material_and_covered_reference_tasks(self):
        with connect(self.db_file) as conn:
            paper_id = conn.execute(
                "INSERT INTO papers (paper_code, paper_name, exam_type, year, region) "
                "VALUES ('AGENT-P1', '测试卷', '省考', 2026, '浙江')"
            ).lastrowid
            question_id = conn.execute(
                "SELECT id FROM questions WHERE question_code = 'AGENT-Q1'"
            ).fetchone()[0]
            conn.execute("UPDATE questions SET paper_id = ? WHERE id = ?", (paper_id, question_id))
            material_id = conn.execute(
                "INSERT INTO paper_materials (paper_id, material_number, content) VALUES (?, 1, '材料')",
                (paper_id,),
            ).lastrowid
            reference_id = conn.execute(
                "INSERT INTO reference_answers (question_id, organization, answer_text) "
                "VALUES (?, '测试机构', '答案')",
                (question_id,),
            ).lastrowid
            conn.execute("DELETE FROM agent_context_pending")
            conn.execute(
                "INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('material', ?, 'upsert')",
                (material_id,),
            )
            conn.execute(
                "INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('reference_answer', ?, 'upsert')",
                (reference_id,),
            )

        self.assertEqual(coalesce_agent_context_pending(self.db_file), "coalesced")
        with connect(self.db_file) as conn:
            queued = conn.execute(
                "SELECT source_type, source_id FROM agent_context_pending ORDER BY source_type"
            ).fetchall()
        self.assertEqual([(row["source_type"], row["source_id"]) for row in queued], [("question", question_id)])

    def test_large_static_queue_promotes_to_single_full_rebuild(self):
        with connect(self.db_file) as conn:
            rebuild_agent_context_index(conn)
            conn.execute("DELETE FROM agent_context_pending")
            conn.executemany(
                "INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('question', ?, 'upsert')",
                [(101,), (102,), (103,)],
            )
        self.assertEqual(coalesce_agent_context_pending(self.db_file, rebuild_threshold=3), "rebuild")
        with connect(self.db_file) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT full_rebuild FROM agent_context_index_state").fetchone()[0], 1)
            worker = conn.execute("SELECT status, total_count FROM agent_context_worker_state").fetchone()
            self.assertEqual((worker["status"], worker["total_count"]), ("queued_rebuild", 3))

    def test_failed_task_retries_then_moves_aside_without_blocking_queue(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute(
                "SELECT id FROM questions WHERE question_code = 'AGENT-Q1'"
            ).fetchone()[0]
            conn.execute("DELETE FROM agent_context_pending")
            conn.execute(
                "INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('attempt', 999999, 'upsert')"
            )
            conn.execute(
                "INSERT INTO agent_context_pending (source_type, source_id, operation) VALUES ('question', ?, 'upsert')",
                (question_id,),
            )

        with patch("gongkao.agent_retrieval.indexing._refresh_attempt_chunks", side_effect=ValueError("坏任务")):
            self.assertEqual(process_agent_context_pending_batch(self.db_file, batch_size=2), 1)
            for _ in range(2):
                with connect(self.db_file) as conn:
                    conn.execute(
                        "UPDATE agent_context_pending SET next_retry_at = NULL "
                        "WHERE source_type = 'attempt' AND source_id = 999999"
                    )
                self.assertEqual(process_agent_context_pending_batch(self.db_file, batch_size=1), 0)

        with connect(self.db_file) as conn:
            failed = conn.execute(
                "SELECT status, retry_count, last_error FROM agent_context_pending "
                "WHERE source_type = 'attempt' AND source_id = 999999"
            ).fetchone()
            question_pending = conn.execute(
                "SELECT COUNT(*) FROM agent_context_pending WHERE source_type = 'question'"
            ).fetchone()[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["retry_count"], 3)
        self.assertIn("坏任务", failed["last_error"])
        self.assertEqual(question_pending, 0)

    def test_background_worker_yields_after_each_successful_batch(self):
        worker = AgentIndexWorker(self.db_file, batch_pause_seconds=0.17)
        worker._stop_event = MagicMock()
        worker._stop_event.is_set.side_effect = [False, True]
        with (
            patch("gongkao.agent_indexer.coalesce_agent_context_pending", return_value="none"),
            patch("gongkao.agent_indexer.rebuild_agent_context_index_if_needed", return_value=False),
            patch("gongkao.agent_indexer.process_agent_context_pending_batch", return_value=1),
        ):
            worker._run()
        worker._stop_event.wait.assert_called_once_with(0.17)

    def test_attempt_update_refreshes_only_pending_chunks(self):
        with connect(self.db_file) as conn:
            rebuild_agent_context_index(conn)
            question_id = conn.execute("SELECT id FROM questions WHERE question_code = 'AGENT-Q1'").fetchone()[0]
            question_chunk_id = conn.execute(
                "SELECT id FROM agent_context_chunks WHERE source_type = 'question' AND source_id = ?",
                (question_id,),
            ).fetchone()[0]
            attempt_id = conn.execute(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '旧作答', 3)",
                (question_id,),
            ).lastrowid
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0], 1)
        self.assertEqual(process_agent_context_pending_batch(self.db_file), 1)
        with connect(self.db_file) as conn:
            first = conn.execute(
                "SELECT id, content_hash FROM agent_context_chunks WHERE source_type = 'attempt' AND source_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("UPDATE attempts SET answer_text = '新作答内容', word_count = 5 WHERE id = ?", (attempt_id,))
            conn.commit()
            self.assertEqual(process_agent_context_pending_batch(self.db_file), 1)
            second = conn.execute(
                "SELECT id, body, content_hash FROM agent_context_chunks WHERE source_type = 'attempt' AND source_id = ?",
                (attempt_id,),
            ).fetchone()
            self.assertNotEqual(first["id"], second["id"])
            self.assertNotEqual(first["content_hash"], second["content_hash"])
            self.assertEqual(second["body"], "新作答内容")
            self.assertEqual(
                conn.execute("SELECT id FROM agent_context_chunks WHERE source_type = 'question' AND source_id = ?", (question_id,)).fetchone()[0],
                question_chunk_id,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0], 0)

    def test_background_indexer_drains_queue_without_foreground_retrieval(self):
        with connect(self.db_file) as conn:
            rebuild_agent_context_index(conn)
            question_id = conn.execute(
                "SELECT id FROM questions WHERE question_code = 'AGENT-Q1'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '后台索引', 4)",
                (question_id,),
            )
        worker = AgentIndexWorker(self.db_file, batch_size=1, idle_seconds=0.05)
        worker.start()
        self.assertTrue(worker._thread.daemon)
        pending = 1
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with connect(self.db_file) as conn:
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM agent_context_pending"
                    ).fetchone()[0]
                if pending == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(pending, 0)
        finally:
            worker.stop()

    def test_index_batch_respects_limit_and_commits_each_item(self):
        with connect(self.db_file) as conn:
            rebuild_agent_context_index(conn)
            question_id = conn.execute(
                "SELECT id FROM questions WHERE question_code = 'AGENT-Q1'"
            ).fetchone()[0]
            conn.executemany(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, ?, 1)",
                [(question_id, f"batch-{index}") for index in range(3)],
            )
        self.assertEqual(
            process_agent_context_pending_batch(self.db_file, batch_size=1),
            1,
        )
        with connect(self.db_file) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0],
                2,
            )
            conn.execute(
                "UPDATE ai_settings SET temperature = temperature WHERE id = 1"
            )

    def test_seed_sync_enqueues_only_changed_index_sources(self):
        seed_file = self.tmpdir / f"{uuid4().hex}-seed.sqlite3"
        target_file = self.tmpdir / f"{uuid4().hex}-target.sqlite3"
        try:
            shutil.copy2(self.db_file, seed_file)
            shutil.copy2(self.db_file, target_file)
            with connect(target_file) as conn:
                rebuild_agent_context_index(conn)
            sync_seed_content(target_file, seed_file)
            with connect(target_file) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0],
                    0,
                )
            with connect(seed_file) as conn:
                conn.execute(
                    "UPDATE questions SET title = title || ' changed' "
                    "WHERE question_code = 'AGENT-Q1'"
                )
            sync_seed_content(target_file, seed_file)
            with connect(target_file) as conn:
                rows = conn.execute(
                    "SELECT source_type, source_id FROM agent_context_pending"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["source_type"], "question")
        finally:
            for path in (seed_file, target_file):
                if path.exists():
                    path.unlink()

    def test_explicit_memory_is_shared_but_thread_messages_stay_isolated(self):
        graph = RecordingGraph()
        with patch("gongkao.agent_graph._graph_for", return_value=graph):
            first_conversation, _ = start_or_continue_chat(
                self.db_file,
                user_text="我主要备考浙江省考，回答尽量简洁",
            )
            second_conversation, _ = start_or_continue_chat(
                self.db_file,
                user_text="给我一个训练建议",
            )
        second_state = graph.invocations[-1]["state"]
        memory_text = "\n".join(item["content"] for item in second_state["long_term_memories"])
        history_text = "\n".join(item["content"] for item in second_state["conversation_messages"])
        self.assertIn("浙江省考", memory_text)
        self.assertIn("偏好简洁回答", memory_text)
        self.assertNotIn("我主要备考浙江省考", history_text)
        self.assertNotEqual(first_conversation, second_conversation)

        with connect(self.db_file) as conn:
            rows = active_memories(conn)
            self.assertEqual(len(rows), 2)
            self.assertTrue(delete_memory(conn, rows[0]["id"]))
            self.assertEqual(len(active_memories(conn)), 1)
            clear_memories(conn)
            self.assertEqual(len(active_memories(conn)), 0)

    def test_long_term_memory_prompt_is_bounded_and_evidence_safe(self):
        messages = with_long_term_memories(
            [("system", "base"), ("human", "task")],
            [{"memory_type": "semantic", "memory_key": "target_exam", "content": "浙江省考", "confidence": 0.9}],
        )
        self.assertEqual(messages[0], ("system", "base"))
        self.assertIn("浙江省考", messages[1][1])
        self.assertIn("题目事实、分数和能力判断仍必须使用本轮检索证据", messages[1][1])

    def test_chat_review_multiple_recent_attempts(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            for index in range(3):
                conn.execute(
                    """
                    INSERT INTO attempts (question_id, answer_text, word_count, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        question_id,
                        f"\u7b2c{index + 1}\u9053\u4f5c\u7b54",
                        6,
                        f"2026-07-03 10:0{index}:00",
                    ),
                )
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            _, run_id = start_or_continue_chat(
                self.db_file,
                user_text="\u590d\u76d8\u6700\u8fd1\u505a\u7684\u4e09\u9053\u9898",
            )
        with connect(self.db_file) as conn:
            steps = conn.execute(
                """
                SELECT output_json
                  FROM agent_steps
                 WHERE run_id = ? AND tool_name = 'retrieve_candidates'
                """,
                (run_id,),
            ).fetchall()
            output = json.loads(steps[-1]["output_json"])
            self.assertTrue(output["review_context"])
            self.assertEqual(output["review_count"], 3)

    def test_review_thread_followup_keeps_selected_attempt_scope(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            selected_attempt_id = conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, "选定作答", 4, "2026-07-03 15:06:00"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, "更新作答", 4, "2026-07-03 16:06:00"),
            )
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            conversation_id, first_run_id = start_or_continue_chat(
                self.db_file,
                user_text="复盘本题",
                review_attempt_id=selected_attempt_id,
            )
            _, followup_run_id = start_or_continue_chat(
                self.db_file,
                conversation_id=conversation_id,
                user_text="可以按照过去现在这样回答吗",
            )
        with connect(self.db_file) as conn:
            first_run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (first_run_id,)).fetchone()
            followup_run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (followup_run_id,)).fetchone()
            self.assertEqual(first_run["task_type"], "review")
            self.assertEqual(first_run["subject_id"], selected_attempt_id)
            self.assertEqual(followup_run["task_type"], "review")
            self.assertEqual(followup_run["subject_id"], selected_attempt_id)
            message = conn.execute(
                """
                SELECT metadata_json
                  FROM agent_messages
                 WHERE conversation_id = ? AND role = 'assistant'
              ORDER BY id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            metadata = json.loads(message["metadata_json"])
            self.assertEqual(metadata["entrypoint"], "recent_review")
            self.assertEqual(metadata["subject_ids"], [selected_attempt_id])

    def test_rag_router_and_evidence_cards_use_current_attempt(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            attempt_id = conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, personal_note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (question_id, "过去是粗放治理，现在是精细治理。", 16, "结构想用过去现在", "2026-07-03 15:06:00"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO grading_reports (attempt_id, provider, model, report_text)
                VALUES (?, 'test', 'fake', '结构判断需要紧扣题干变化，不能泛泛写过去现在。')
                """,
                (attempt_id,),
            )
            review_context = get_attempt_review_context(conn, attempt_id)
            rag_context = build_rag_context(
                conn,
                "review",
                "可以按照过去现在这样回答吗",
                subject_ids=[attempt_id],
                module="overview",
                review_context=review_context,
            )
        self.assertEqual(route_rag("可以按照过去现在这样回答吗", "review", [attempt_id], "overview"), "structure_judgement")
        self.assertEqual(rag_context["rag_route"], "structure_judgement")
        self.assertTrue(rag_context["grounding_contract"]["current_attempt_only"])
        evidence_ids = {card["evidence_id"] for card in rag_context["evidence_cards"]}
        self.assertIn(f"attempt:{attempt_id}", evidence_ids)
        self.assertIn(f"question:{question_id}", evidence_ids)
        self.assertTrue(any(card["source_type"] == "grading_report" for card in rag_context["evidence_cards"]))

    def test_module_rag_context_uses_aggregate_and_evidence_cards(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            attempt_id = conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, personal_note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (question_id, "综合分析作答，材料遗漏。", 12, "复盘：结构表达偏散。", "2026-07-03 10:00:00"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO grading_reports (attempt_id, provider, model, report_text)
                VALUES (?, 'test', 'fake', '最大问题是材料遗漏和结构表达。')
                """,
                (attempt_id,),
            )
            rebuild_agent_context_index(conn)
            rag_context = build_rag_context(
                conn,
                "diagnosis",
                "分析我综合分析的问题",
                module="analysis",
                filters={"question_type": QUESTION_TYPE_ANALYSIS},
                user_context=load_user_context(conn),
            )
        self.assertEqual(rag_context["rag_route"], "module_diagnosis")
        self.assertFalse(rag_context["grounding_contract"]["current_attempt_only"])
        self.assertTrue(rag_context["module_context"]["coverage"]["attempt_count"])
        self.assertTrue(any(card["source_type"] == "aggregate" for card in rag_context["evidence_cards"]))

    def test_note_organization_route_uses_only_personal_notes(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            for index, note in enumerate(["先审题再分层，避免对策脱离材料。", "结尾前检查是否遗漏核心词和采分点。"], start=1):
                conn.execute(
                    """
                    INSERT INTO attempts (question_id, answer_text, word_count, personal_note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (question_id, f"作答 {index}", 4, note, f"2026-07-03 1{index}:00:00"),
                )
            rag_context = build_rag_context(
                conn,
                "diagnosis",
                "帮我整理所有我的笔记成一个注意事项",
                module="overview",
            )
        self.assertEqual(route_rag("帮我整理所有我的笔记成一个注意事项", "diagnosis", [], "overview"), "note_organization")
        self.assertEqual(route_rag("编者按是什么应该怎么写，有几种写法", "diagnosis", [], "overview"), "writing_guidance")

        self.assertEqual(route_rag("\u77ed\u8bc4\u5982\u4f55\u5199\uff0c\u6709\u54ea\u4e9b\u7ed3\u6784", "diagnosis", [], "overview"), "writing_guidance")
        self.assertEqual(route_rag("\u5ba3\u4f20\u7a3f\u683c\u5f0f\u548c\u6a21\u677f\u662f\u4ec0\u4e48", "diagnosis", [], "overview"), "writing_guidance")
        self.assertEqual(route_rag("\u77ed\u8bc4\u5982\u4f55\u5199\uff0c\u6709\u54ea\u4e9b\u7ed3\u6784", "review", [1], "overview"), "writing_guidance")
        self.assertEqual(rag_context["rag_route"], "note_organization")
        source_types = {card["source_type"] for card in rag_context["evidence_cards"]}
        self.assertIn("personal_note", source_types)
        self.assertIn("aggregate", source_types)
        self.assertNotIn("grading_report", source_types)
        self.assertNotIn("attempt", source_types)

    def test_writing_guidance_uses_direct_explanation_prompt(self):
        messages = build_agent_messages(
            "diagnosis",
            "编者按是什么应该怎么写，有几种写法",
            {},
            [],
            {},
            {"rag_route": "writing_guidance", "evidence_cards": [{"evidence_id": "knowledge:writing_guidance"}]},
        )
        prompt = messages[-1][1]
        self.assertIn("写法讲解", prompt)
        self.assertIn("不要套训练诊断报告结构", prompt)
        self.assertIn("不得给题目编号编造网址", build_agent_messages("diagnosis", "推荐一道题", {}, [], {}, {})[0][1])

    def test_knowledge_library_is_indexed_and_used_for_writing_guidance(self):
        items = load_knowledge_items()
        self.assertGreaterEqual(len(items), 300)
        self.assertTrue(all(item.get("content_hash") for item in items))
        self.assertTrue(all(item.get("source") for item in items))
        self.assertTrue(any(item["id"] == "knowledge:document:course:signature" for item in items))
        saduck_items = [item for item in items if item.get("source", {}).get("name") == "SaDuck 公考知识库"]
        self.assertEqual(len(saduck_items), 59)
        self.assertEqual(
            {item["source"]["section"] for item in saduck_items},
            {"什么是申论", "材料阅读技巧", "归纳概括", "提出对策", "综合分析", "贯彻执行（公文写作）", "文章写作"},
        )
        self.assertEqual(
            Counter(item["source"]["section"] for item in saduck_items),
            {
                "什么是申论": 4,
                "材料阅读技巧": 6,
                "归纳概括": 8,
                "提出对策": 11,
                "综合分析": 5,
                "贯彻执行（公文写作）": 11,
                "文章写作": 14,
            },
        )
        self.assertTrue(
            {
                "knowledge:overview:saduck:answer-sheet-format",
                "knowledge:overview:saduck:paragraph-logic",
                "knowledge:summary:saduck:effects",
                "knowledge:countermeasure:saduck:law-supervision",
                "knowledge:analysis:saduck:interpretation",
                "knowledge:document:saduck:editor-note",
                "knowledge:essay:saduck:argument-methods",
            }
            <= {item["id"] for item in saduck_items}
        )
        self.assertFalse(
            {"作文写作模板", "申论规范词", "名言积累", "必备名言", "人物素材", "范文欣赏"}
            & {item["source"]["section"] for item in saduck_items}
        )
        with connect(self.db_file) as conn:
            rebuild_agent_context_index(conn)
            self.assertGreaterEqual(
                conn.execute("SELECT COUNT(*) FROM agent_context_chunks WHERE source_type = 'knowledge'").fetchone()[0],
                18,
            )
            knowledge = retrieve_knowledge_evidence(conn, "document", "编者按写法 导读 推荐语", limit=12)
            self.assertTrue(knowledge)
            self.assertTrue(any(item["evidence_ref"] == "knowledge:document:editor_note" for item in knowledge))
            course_knowledge = retrieve_knowledge_evidence(conn, "document", "公文什么时候写落款", limit=5)
            self.assertTrue(any(item["evidence_ref"] == "knowledge:document:course:signature" for item in course_knowledge))
            reading_knowledge = retrieve_knowledge_evidence(conn, "overview", "材料阅读 转折词 段落逻辑 粗读精读", limit=8)
            self.assertTrue(any(item["evidence_ref"] == "knowledge:overview:saduck:reading-method" for item in reading_knowledge))
            countermeasure_knowledge = retrieve_knowledge_evidence(conn, "countermeasure", "提出对策 针对性 可行性 身份越权", limit=8)
            self.assertTrue(any(item["evidence_ref"] == "knowledge:countermeasure:saduck:question-guide" for item in countermeasure_knowledge))
            retrieval_checks = [
                ("overview", "答题卡 标点占格 破折号 字数限制", "knowledge:overview:saduck:answer-sheet-format"),
                ("summary", "归纳概括 影响 行动 结果 积极影响 消极影响", "knowledge:summary:saduck:effects"),
                ("analysis", "解释分析 多要素语句 含义解释", "knowledge:analysis:saduck:interpretation"),
                ("document", "摘要 客观压缩 不加入评论", "knowledge:document:saduck:abstract"),
                ("essay", "论证方法 举例 引用 对比 类比 因果 假设 归纳", "knowledge:essay:saduck:argument-methods"),
            ]
            for module, query, expected_id in retrieval_checks:
                evidence = retrieve_knowledge_evidence(conn, module, query, limit=12)
                self.assertTrue(any(item["evidence_ref"] == expected_id for item in evidence), (module, expected_id))
            rag_context = build_rag_context(
                conn,
                "diagnosis",
                "编者按是什么应该怎么写",
                module="overview",
            )
        self.assertEqual(rag_context["rag_route"], "writing_guidance")
        self.assertIn("knowledge", {card["source_type"] for card in rag_context["evidence_cards"]})
        self.assertNotIn("personal_note", {card["source_type"] for card in rag_context["evidence_cards"]})

    def test_knowledge_can_mix_with_notes_when_user_requests_it(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, personal_note)
                VALUES (?, '测试作答', 4, '编者按要先点明材料价值，再提醒阅读重点。')
                """,
                (question_id,),
            )
            rebuild_agent_context_index(conn)
            rag_context = build_rag_context(
                conn,
                "diagnosis",
                "结合我的笔记整理编者按注意事项",
                module="overview",
            )
        source_types = {card["source_type"] for card in rag_context["evidence_cards"]}
        self.assertEqual(rag_context["rag_route"], "note_organization")
        self.assertIn("knowledge", source_types)
        self.assertIn("personal_note", source_types)

    def test_query_plan_can_select_sources_without_new_route_keywords(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            conn.execute(
                """
                INSERT INTO attempts (question_id, answer_text, word_count, personal_note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (question_id, "作答文本", 4, "开写前先圈主体、问题、要求。", "2026-07-03 12:00:00"),
            )
            rag_context = build_rag_context(
                conn,
                "review",
                "给我做一个考前检查单",
                subject_ids=[1],
                module="overview",
                query_plan={
                    "action": "organize",
                    "scope": "notes_only",
                    "sources": ["attempt", "question", "personal_note", "aggregate"],
                    "module": "overview",
                    "reason": "用户要从个人记录生成检查单",
                },
            )
        self.assertEqual(rag_context["rag_route"], "note_organization")
        self.assertEqual(rag_context["query_plan"]["scope"], "notes_only")
        self.assertEqual(rag_context["query_plan"]["sources"], ["personal_note", "aggregate"])
        self.assertEqual({card["source_type"] for card in rag_context["evidence_cards"]}, {"aggregate", "personal_note"})

    def test_module_rag_indexes_all_matching_history(self):
        with connect(self.db_file) as conn:
            question_id = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
            for index in range(4):
                attempt_id = conn.execute(
                    """
                    INSERT INTO attempts (question_id, answer_text, word_count, personal_note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        question_id,
                        f"综合分析作答 {index}，材料提取不完整，结构表达偏散。",
                        18,
                        f"复盘笔记 {index}：采分点遗漏。",
                        f"2026-07-03 10:0{index}:00",
                    ),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO grading_reports (attempt_id, provider, model, report_text)
                    VALUES (?, 'test', 'fake', ?)
                    """,
                    (attempt_id, "最大问题是材料遗漏和结构表达，采分点不全。"),
                )
            rebuild_agent_context_index(conn)
            chunk_count = conn.execute("SELECT COUNT(*) FROM agent_context_chunks").fetchone()[0]
            self.assertGreaterEqual(chunk_count, 12)
            evidence = retrieve_module_evidence(
                conn,
                "analysis",
                "分析我综合分析的缺点",
                {},
            )
            self.assertGreaterEqual(evidence["coverage"]["attempt_count"], 4)
            self.assertGreaterEqual(evidence["coverage"]["report_count"], 4)
            self.assertTrue(evidence["weakness_profile"])
            self.assertIn("evidence_ref", evidence["evidence_chunks"][0])
            self.assertIn("retrieval", evidence["evidence_chunks"][0])
            categories = {item["name"] for item in evidence["problem_categories"]}
            self.assertTrue({"材料遗漏", "采分点遗漏", "结构表达"} & categories)

    def test_eval_suite_writes_local_regression_results(self):
        ragas_metrics = {
            "ragas_available": True,
            "ragas_status": "ok",
            "ragas_scores": {
                "faithfulness": 0.91,
                "context_precision": 0.82,
                "context_recall": 0.73,
                "factual_correctness": 0.88,
            },
            "ragas_average": 0.835,
        }
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()), patch(
            "gongkao.agent_eval.compute_ragas_metrics",
            return_value=ragas_metrics,
        ):
            results = run_eval_suite(self.db_file)
            self.assertEqual(len(results), 5)
        with connect(self.db_file) as conn:
            rows = conn.execute("SELECT * FROM agent_eval_results ORDER BY id").fetchall()
            self.assertEqual(len(rows), 5)
            metrics = json.loads(rows[0]["metrics_json"])
            self.assertIn("tool_call_accuracy", metrics)
            self.assertIn("ragas_available", metrics)
            self.assertEqual(metrics["ragas_status"], "ok")
            self.assertIn("ragas_average", metrics)
            self.assertIn("context_precision", metrics["ragas_scores"])
            self.assertEqual(metrics["runtime"]["dataset_version"], "agent-v2-regression-v1")
            self.assertIn("prompt_version", metrics["runtime"])
            self.assertEqual(set(metrics["layers"]), {"planner", "retriever", "tool", "answer"})

    def test_agent_v2_eval_dataset_is_versioned_and_reviewable(self):
        cases = load_eval_cases(EVAL_DATASET_PATH)
        smoke = load_eval_cases(EVAL_DATASET_PATH, tags={"smoke"})
        self.assertEqual(len(cases), 60)
        self.assertEqual(len(smoke), 5)
        self.assertEqual(len({case["id"] for case in cases}), 60)
        self.assertTrue(all(case.get("reference") for case in cases))
        self.assertTrue(all(case.get("expected_keywords") for case in cases))

    def test_m1_multiturn_dataset_covers_30_reviewable_groups(self):
        cases = load_multiturn_cases(MULTITURN_DATASET_PATH)
        self.assertEqual(len(cases), 30)
        self.assertEqual(len({case["id"] for case in cases}), 30)
        categories = {case["category"] for case in cases}
        self.assertEqual(categories, {"reference", "constraint", "correction", "summary", "isolation"})
        self.assertTrue(all(case["expected_context_terms"] for case in cases))

    def test_retrieval_ranking_metrics_are_deterministic(self):
        metrics = retrieval_ranking_metrics(
            ["noise:1", "gold:b", "noise:2", "gold:a"],
            ["gold:a", "gold:b", "gold:c"],
        )
        self.assertEqual(metrics["recall_at_5"], 0.6667)
        self.assertEqual(metrics["recall_at_10"], 0.6667)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertGreater(metrics["ndcg_at_10"], 0)
        self.assertLess(metrics["ndcg_at_10"], 1)

    def test_deterministic_eval_covers_layers_tokens_and_report_comparison(self):
        results = evaluate_deterministic_suite()
        self.assertEqual(len(results), 12)
        exact = next(item for item in results if item["case"]["id"] == "retrieval_exact_top")
        self.assertEqual(exact["metrics"]["layers"]["retriever"]["recall_at_10"], 1.0)
        token_case = next(item for item in results if item["case"]["id"] == "token_usage_aggregation")
        self.assertEqual(token_case["metrics"]["runtime"]["token_usage"]["total_tokens"], 500)

        baseline_path = self.tmpdir / "baseline.json"
        candidate_path = self.tmpdir / "candidate.json"
        write_eval_report(results, baseline_path, dataset_path=EVAL_DATASET_PATH, mode="deterministic")
        candidate_results = json.loads(json.dumps(results, ensure_ascii=False))
        candidate_results[0]["metrics"]["score"] += 5
        write_eval_report(candidate_results, candidate_path, dataset_path=EVAL_DATASET_PATH, mode="deterministic")
        comparison = compare_eval_reports(baseline_path, candidate_path)
        self.assertEqual(comparison["shared_case_count"], 12)
        self.assertEqual(comparison["improved"], 1)
        self.assertGreater(comparison["average_delta"], 0)

    def test_eval_results_can_be_deleted_and_cleared(self):
        with connect(self.db_file) as conn:
            first_id = conn.execute(
                """
                INSERT INTO agent_eval_results (suite_name, case_id, case_title, task_type, score, metrics_json, notes)
                VALUES ('suite', 'one', '用例一', 'diagnosis', 88, '{}', '')
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO agent_eval_results (suite_name, case_id, case_title, task_type, score, metrics_json, notes)
                VALUES ('suite', 'two', '用例二', 'review', 77, '{}', '')
                """
            )
        handler = object.__new__(__import__("gongkao.web.application", fromlist=["Handler"]).Handler)
        handler.redirect = lambda path: setattr(handler, "redirected_to", path)
        with patch("gongkao.web.application.DB_PATH", self.db_file):
            handler.handle_agent_eval_delete(f"/agent/evals/{first_id}/delete")
        with connect(self.db_file) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM agent_eval_results WHERE id = ?", (first_id,)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_eval_results").fetchone()[0], 1)
        with patch("gongkao.web.application.DB_PATH", self.db_file):
            handler.handle_agent_eval_clear()
        with connect(self.db_file) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_eval_results").fetchone()[0], 0)

    def test_agent_requires_api_configuration(self):
        with connect(self.db_file) as conn:
            conn.execute("UPDATE ai_settings SET api_key = '', api_key_env = '' WHERE id = 1")
        with self.assertRaises(AgentRunError):
            run_agent(self.db_file, "diagnosis")
        with connect(self.db_file) as conn:
            run = conn.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(run["status"], "failed")

    def test_delete_conversation_removes_thread_messages(self):
        with patch("gongkao.agent_graph._graph_for", return_value=FakeGraph()):
            conversation_id, _ = start_or_continue_chat(
                self.db_file,
                user_text="\u6211\u4eca\u5929\u7ec3\u4ec0\u4e48",
            )
        with connect(self.db_file) as conn:
            delete_conversation(conn, conversation_id)
            self.assertIsNone(conn.execute("SELECT id FROM agent_conversations WHERE id = ?", (conversation_id,)).fetchone())
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0],
                0,
            )

    def test_agent_ui_uses_thread_workspace_layout(self):
        root = Path(__file__).resolve().parent.parent
        server_source = read_server_application(root)
        agent_page_source = server_source[
            server_source.index("    def page_agent(self, query, flashes=None):"):
            server_source.index("    def page_agent_conversation(self, path):")
        ]
        graph_source = (root / "gongkao" / "agent_graph.py").read_text(encoding="utf-8")
        coach_source = (root / "gongkao" / "agent_coach.py").read_text(encoding="utf-8")
        tools_source = (root / "gongkao" / "agent_tools.py").read_text(encoding="utf-8")
        prompts_source = (root / "gongkao" / "agent_prompts.py").read_text(encoding="utf-8")
        js_source = read_static_scripts(root)
        css = read_static_styles(root)
        self.assertIn("agent-workspace", server_source)
        self.assertIn("agent-thread-rail", server_source)
        self.assertIn("agent-message-stream", server_source)
        self.assertIn("agent-composer", server_source)
        self.assertIn("<details class=\"agent-structured-output\">", server_source)
        self.assertIn("结构化提取", server_source)
        self.assertNotIn("<section class=\"agent-structured-output\">", server_source)
        self.assertNotIn("agent-context-panel", server_source)
        self.assertIn("agent-module-workbench", server_source)
        self.assertIn("agent-module-tabs", server_source)
        self.assertIn('visible_modules = ("overview", "summary", "analysis", "countermeasure", "document", "essay")', server_source)
        self.assertIn("复盘本题", server_source)
        self.assertIn("综合写作", (root / "gongkao" / "agent_modules.py").read_text(encoding="utf-8"))
        self.assertIn("详细筛选", server_source)
        self.assertIn("agent-scope-settings", server_source)
        self.assertIn("agent_summary_for_display", server_source)
        self.assertNotIn("运行全量分析", server_source)
        self.assertNotIn("agent-module-controls", server_source)
        self.assertNotIn("默认分析全部相关历史。最近 N 题只在你明确要求时启用。", server_source)
        self.assertNotIn("均分约", server_source + coach_source)
        self.assertIn("/agent/evals", server_source)
        self.assertIn("/agent/evals/clear", server_source)
        self.assertIn("/delete", server_source)
        self.assertIn("ragas_average", server_source)
        self.assertIn("Context", server_source)
        self.assertIn("Recall@10", server_source)
        self.assertIn("查看 Trace", server_source)
        self.assertIn("prompt_version", server_source)
        self.assertIn("scripts/run_agent_eval.py", server_source)
        self.assertIn("/agent/memories", server_source)
        self.assertIn("长期记忆", server_source)
        self.assertIn("delete_memory", server_source)
        self.assertNotIn("/agent/showcase", server_source)
        self.assertNotIn("page_agent_showcase", server_source)
        self.assertIn('href="/statistics"', server_source)
        self.assertIn("训练统计", server_source)
        self.assertNotIn("API 是唯一运行模式", server_source)
        self.assertNotIn("API 智能模式", server_source)
        self.assertNotIn("当前只支持 API", server_source)
        self.assertNotIn("手动包", graph_source + tools_source + prompts_source)
        self.assertNotIn("手动 Agent", graph_source + tools_source + prompts_source)
        self.assertNotIn("等待用户确认后继续", graph_source)
        self.assertNotIn("awaiting_confirmation", graph_source)
        self.assertIn('name="agent_connection_mode"', server_source)
        self.assertIn("沿用批改 API", server_source)
        self.assertIn("agent_ai_settings", server_source)
        self.assertIn("agent-thread-delete-button", server_source)
        self.assertIn('aria-label="删除线程">×</button>', server_source)
        self.assertIn("agent-thread-time", server_source)
        self.assertNotIn("recent_conversations(conn, 6)", server_source)
        self.assertEqual(server_source.count("recent_conversations(conn, 50)"), 2)
        self.assertIn("data-updated", server_source)
        self.assertIn("agent-rag-panel", server_source)
        self.assertIn("agent-rag-debug", server_source)
        self.assertIn("evidence_sufficiency", prompts_source)
        self.assertIn("note_organization", prompts_source)
        self.assertIn("data-agent-composer", server_source)
        self.assertIn("/delete", server_source)
        self.assertNotIn("training-todos", server_source)
        self.assertNotIn("加入训练待办", server_source)
        self.assertNotIn("训练待办", server_source)
        self.assertNotIn("contextmenu", js_source)
        self.assertIn("agent-message assistant is-pending", js_source)
        self.assertNotIn("Pending Plan", server_source)
        self.assertNotIn("approve-plan", server_source)
        self.assertNotIn("render_agent_plan_row", server_source)
        self.assertNotIn("agent-plan-actions", server_source)
        self.assertNotIn("/agent/plan-items/", server_source)
        self.assertNotIn("训练计划", agent_page_source)
        self.assertNotIn("plan_next_actions", graph_source)
        self.assertIn("grid-template-columns: 260px minmax(0, 1fr)", css)
        self.assertIn("grid-template-columns: repeat(6, minmax(96px, 1fr))", css)
        self.assertIn(".agent-thread-row", css)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn(".agent-context-metrics", css)
        self.assertIn(".agent-module-tab", css)
        self.assertIn(".agent-scope-grid", css)
        self.assertIn(".agent-run-row.active", css)
        self.assertIn(".agent-thread-row::after", css)
        self.assertIn(".agent-thread-row:hover", css)
        self.assertIn(".agent-thread-row > a:active", css)
        self.assertIn(".agent-module-tab:not(.active):hover", css)
        self.assertIn(".agent-scope-settings > summary:hover", css)
        self.assertIn(".agent-status-steps span.done", css)
        self.assertIn(".agent-status-steps span.current", css)
        self.assertIn("agent-status-note", server_source + js_source + css)
        self.assertIn("/status", server_source + js_source)
        self.assertIn("message_html", server_source + js_source)
        self.assertIn("insertAdjacentHTML(\"afterend\", payload.message_html)", js_source)
        spec_source = (root / "研申.spec").read_text(encoding="utf-8")
        self.assertIn('"knowledge/manifest.json", "knowledge"', spec_source)
        self.assertIn('"knowledge/knowledge_cards_v2.jsonl", "knowledge"', spec_source)
        self.assertIn('"knowledge/saduck_methodology.jsonl", "knowledge"', spec_source)
        self.assertNotIn('"document_theory_course.jsonl", "knowledge"', spec_source)
        self.assertNotIn("window.location.reload();\n      }\n    }, 3500", js_source)
        self.assertNotIn("@keyframes agent-status-pulse", css)
        self.assertIn(".agent-rag-card", css)
        self.assertIn(".agent-rag-sufficiency.limited", css)
        self.assertNotIn(".agent-plan-row", css)
        self.assertNotIn(".agent-plan-actions", css)
        self.assertIn(".agent-eval-row", css)


if __name__ == "__main__":
    unittest.main()

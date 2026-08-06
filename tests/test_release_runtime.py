import json
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import launcher as desktop_launcher
from gongkao.ai_config import load_effective_agent_settings
from gongkao.db import CURRENT_SCHEMA_VERSION, connect, prepare_user_database
from gongkao.grading import build_grading_package
from gongkao.organizations import canonicalize_organization
from gongkao.services.personal_records import (
    export_personal_data,
    import_personal_data,
    save_text_annotations,
)
from gongkao.statistics import (
    build_module_score_statistics,
    build_training_statistics,
    calculate_streak,
    parse_report_score,
)
from gongkao.taxonomy import classify_question_type
from gongkao.timeutils import BEIJING_TIMEZONE, format_beijing_time
from gongkao.web import dispatch_get, dispatch_post
from gongkao.web.application import create_server
from gongkao.web.runtime import (
    _agent_rag_card_href,
    activity_level,
    attempt_grading_references,
    evidence_return_path,
    grading_references_from_form,
    grading_report_return_path,
    hide_internal_score_calibration,
    inline_markdown,
    is_report_citation_return,
    knowledge_evidence_href,
    layout,
    markdownish,
    next_question_path,
    pagination_html,
    question_code_href,
    recommended_timed_paper,
    report_answer_snapshot,
    requested_page,
    requested_page_size,
    safe_return_path,
    safe_static_path,
    save_attempt_grading_references,
    sort_exam_types,
    sort_regions,
    tabbed_materials,
    tabbed_references,
    workflow_header,
)
from scripts.audit_release import audit_database
from tests.asset_bundle import (
    read_server_application,
    read_static_scripts,
    read_static_styles,
)

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "gongkao_seed.sqlite3"


class ReleaseRuntimeTest(unittest.TestCase):
    def test_internal_score_calibration_note_is_hidden_from_old_reports(self):
        report = (
            "## 采分点证据与整体校准\n"
            "- 整体调整理由：采分点覆盖校准。 已按真实考场高分稀缺度校准总分：94→84.4。\n"
            "- 参考答案使用说明：按材料核验。"
        )
        cleaned = hide_internal_score_calibration(report)
        self.assertNotIn("真实考场高分稀缺度", cleaned)
        self.assertIn("整体调整理由：采分点覆盖校准。", cleaned)
        self.assertIn("参考答案使用说明", cleaned)

    def test_timed_paper_recommendation_skips_completed_papers_and_essay_status(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY,
                paper_name TEXT,
                year INTEGER,
                region TEXT,
                zhejiang_relevance INTEGER,
                paper_category TEXT
            );
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                paper_id INTEGER,
                question_type TEXT,
                question_number INTEGER
            );
            CREATE TABLE attempts (id INTEGER PRIMARY KEY, question_id INTEGER);
            INSERT INTO papers VALUES (1, '最新卷', 2026, '浙江', 1, '省考');
            INSERT INTO papers VALUES (2, '最近作答卷', 2025, '浙江', 1, '省考');
            INSERT INTO papers VALUES (3, '相邻未完成卷', 2024, '浙江', 1, '省考');
            INSERT INTO questions VALUES (11, 1, '归纳概括', 1);
            INSERT INTO questions VALUES (21, 2, '提出对策', 1);
            INSERT INTO questions VALUES (22, 2, '综合写作', 2);
            INSERT INTO questions VALUES (31, 3, '贯彻执行', 1);
            INSERT INTO attempts VALUES (1, 11);
            INSERT INTO attempts VALUES (2, 21);
            """
        )
        recommendation = recommended_timed_paper(conn, latest_paper_id=2)
        self.assertEqual(recommendation["id"], 3)
        self.assertEqual(recommendation["next_question_id"], 31)
        self.assertEqual(recommendation["unattempted_questions"], 1)
        conn.close()

    def test_agent_evidence_refs_support_generated_formats(self):
        return_to = "/agent/conversations/27"
        attempt_html = inline_markdown("依据 [evidence: attempt_id=140]", return_to)
        self.assertIn('href="/attempts/140?return_to=%2Fagent%2Fconversations%2F27"', attempt_html)
        self.assertIn("作答ID:140", attempt_html)
        self.assertNotIn("[evidence:", attempt_html)

        coded_attempt_html = inline_markdown("`attempt:73`", return_to)
        self.assertEqual(coded_attempt_html.count('class="agent-evidence-link"'), 1)
        self.assertNotIn("<code>", coded_attempt_html)

        knowledge_id = "knowledge:overview:transfer:counterexample"
        knowledge_html = inline_markdown(f"依据 [evidence: {knowledge_id}]", return_to)
        expected_href = knowledge_evidence_href(knowledge_id, return_to)
        self.assertIn(f'href="{expected_href}"', knowledge_html)
        self.assertNotIn("[evidence:", knowledge_html)
        self.assertNotIn(f">{knowledge_id}<", knowledge_html)
        self.assertIn("知识卡", knowledge_html)
        self.assertEqual(
            _agent_rag_card_href({"evidence_id": knowledge_id, "source_type": "knowledge"}, return_to),
            expected_href,
        )

        question_html = inline_markdown("题目 [GKS-324-Q2](https://www.baidu.com)", return_to)
        self.assertIn(f'href="{question_code_href("GKS-324-Q2", return_to)}"', question_html)
        self.assertNotIn("baidu.com", question_html)

    def test_grading_report_evidence_is_clickable_with_or_without_return_path(self):
        linked = inline_markdown("（依据：grading_report:144）", "/agent/conversations/27")
        self.assertIn(
            'href="/grading-reports/144?return_to=%2Fagent%2Fconversations%2F27"',
            linked,
        )
        self.assertIn(">grading_report:144</a>", linked)

        standalone = inline_markdown("依据：grading_report:160")
        self.assertIn('href="/grading-reports/160"', standalone)

    def test_cross_report_citation_returns_to_exact_origin_report(self):
        origin = grading_report_return_path(
            156,
            168,
            "/attempts?question_type=归纳概括",
        )
        self.assertEqual(
            origin,
            "/attempts/156?return_to=%2Fattempts%3Fquestion_type%3D%E5%BD%92%E7%BA%B3%E6%A6%82%E6%8B%AC#report-168",
        )
        self.assertTrue(is_report_citation_return(origin))
        linked = inline_markdown("依据：grading_report:144", origin)
        self.assertIn(
            "/grading-reports/144?return_to=%2Fattempts%2F156%3Freturn_to%3D%252Fattempts%253Fquestion_type%253D%25E5%25BD%2592%25E7%25BA%25B3%25E6%25A6%2582%25E6%258B%25AC%23report-168",
            linked,
        )

    def test_cross_report_http_flow_returns_to_origin_and_is_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "citation-return.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question_ids = [
                    row["id"]
                    for row in conn.execute("SELECT id FROM questions ORDER BY id LIMIT 2")
                ]
                origin_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '原作答', 3)",
                    (question_ids[0],),
                ).lastrowid
                target_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '被引用作答', 5)",
                    (question_ids[1],),
                ).lastrowid
                target_report_id = conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text) VALUES (?, '被引用报告')",
                    (target_attempt_id,),
                ).lastrowid
                origin_report_id = conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text) VALUES (?, ?)",
                    (origin_attempt_id, f"重复问题（依据：grading_report:{target_report_id}）"),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                parent = "/attempts?question_type=归纳概括"
                origin = grading_report_return_path(origin_attempt_id, origin_report_id, parent)
                with urlopen(f"{base}{origin}", timeout=10) as response:
                    origin_html = response.read().decode("utf-8")
                citation_path = f"/grading-reports/{target_report_id}?return_to={quote(origin, safe='')}"
                self.assertIn(citation_path, origin_html)

                with urlopen(f"{base}{citation_path}", timeout=10) as response:
                    target_url = response.geturl()
                    target_html = response.read().decode("utf-8")
                self.assertIn(f"/attempts/{target_attempt_id}?return_to=", target_url)
                self.assertIn(f"#report-{target_report_id}", target_url)
                self.assertIn("关闭引用并返回原位置", target_html)
                self.assertIn('data-active-section="papers"', target_html)
                self.assertIn('data-transient-route="1"', target_html)
                self.assertIn(origin, target_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_public_seed_is_complete_and_sanitized(self):
        audit_database(SEED)
        with connect(SEED) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION)

    def test_release_audit_rejects_private_agent_data(self):
        with tempfile.TemporaryDirectory() as directory:
            test_db = Path(directory) / "seed.sqlite3"
            shutil.copy2(SEED, test_db)
            with connect(test_db) as conn:
                conn.execute(
                    "INSERT INTO agent_runs (task_type, status, user_goal) VALUES ('diagnosis', 'completed', 'private')"
                )
            with self.assertRaisesRegex(RuntimeError, "agent runs"):
                audit_database(test_db)

    def test_public_seed_uses_curated_taxonomy(self):
        allowed = {"归纳概括", "综合分析", "提出对策", "公文写作", "综合写作"}
        with connect(SEED) as conn:
            actual = {row[0] for row in conn.execute("SELECT DISTINCT question_type FROM questions")}
            self.assertTrue(actual)
            self.assertTrue(actual <= allowed)
            self.assertFalse({"申论题", "贯彻执行", "文章写作"} & actual)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers WHERE region = '新疆兵团'").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE paper_name LIKE '%广东省公考%' AND paper_name LIKE '%公安%' AND (region <> '广东' OR exam_type <> '广东省考')"
                ).fetchone()[0],
                0,
            )

    def test_question_taxonomy_uses_task_intent_and_document_genres(self):
        cases = {
            "请写一篇宣传稿，介绍家庭签约医生制度。": "公文写作",
            "请撰写一份年度工作报告的报告提纲。": "公文写作",
            "请结合材料，自拟题目，写一篇议论文。": "综合写作",
            "请梳理存在的问题，并提出改进建议。": "提出对策",
            "请谈谈该地推进基层治理的经验做法。": "归纳概括",
            "请深入分析理解点评专家的这一观点。": "综合分析",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_question_type(prompt)[0], expected)

    def test_seed_sync_preserves_personal_data_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "gongkao.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()["id"]
                conn.execute("UPDATE ai_settings SET api_key = 'user-owned-key' WHERE id = 1")
                cursor = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, personal_note) VALUES (?, '我的作答', 4, '我的复盘笔记')",
                    (question_id,),
                )
                attempt_id = cursor.lastrowid
                paper_id = conn.execute("SELECT paper_id FROM questions WHERE id = ?", (question_id,)).fetchone()["paper_id"]
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, provider, model, report_text) VALUES (?, 'Codex', 'manual', '我的批改报告')",
                    (attempt_id,),
                )
                conn.execute("INSERT INTO question_favorites (question_id) VALUES (?)", (question_id,))
                conn.execute("INSERT INTO paper_favorites (paper_id) VALUES (?)", (paper_id,))
                for question_code in (
                    "GKS-OBSOLETE-Q1",
                    "CUSTOM-QUESTION-1",
                ):
                    conn.execute(
                        """
                        INSERT INTO questions (
                            question_code, exam_type, year, region, question_type,
                            title, prompt, materials, requirements
                        ) VALUES (?, '测试', 2026, '测试', '综合分析', '同步测试', '同步测试题干', '同步测试材料', '同步测试要求')
                        """,
                        (question_code,),
                    )

            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM grading_reports").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT api_key FROM ai_settings WHERE id = 1").fetchone()[0], "user-owned-key")
                self.assertEqual(conn.execute("SELECT mode FROM ai_settings WHERE id = 1").fetchone()[0], "api")
                self.assertEqual(conn.execute("SELECT model FROM ai_settings WHERE id = 1").fetchone()[0], "deepseek-v4-pro")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 1751)
                self.assertFalse(conn.execute("SELECT 1 FROM questions WHERE question_code = 'GKS-OBSOLETE-Q1'").fetchone())
                self.assertTrue(conn.execute("SELECT 1 FROM questions WHERE question_code = 'CUSTOM-QUESTION-1'").fetchone())
                self.assertEqual(conn.execute("SELECT personal_note FROM attempts WHERE id = ?", (attempt_id,)).fetchone()[0], "我的复盘笔记")
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
                self.assertTrue(
                    {
                        "duration_seconds",
                        "paper_elapsed_seconds",
                        "paper_time_excluded",
                        "answer_format_json",
                    }
                    <= columns
                )

    def test_empty_unversioned_database_is_initialized_from_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "gongkao.sqlite3"
            sqlite3.connect(user_db).close()
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION)
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 0)

    def test_unversioned_application_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "gongkao.sqlite3"
            shutil.copy2(SEED, user_db)
            with connect(user_db) as conn:
                conn.execute("PRAGMA user_version = 0")
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema 0"):
                prepare_user_database(user_db, SEED)

    def test_unrelated_unversioned_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "gongkao.sqlite3"
            with connect(user_db) as conn:
                conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema 0"):
                prepare_user_database(user_db, SEED)

    def test_favorites_are_unique_and_follow_target_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "favorites.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute("SELECT id, paper_id FROM questions ORDER BY id LIMIT 1").fetchone()
                conn.execute("INSERT OR IGNORE INTO question_favorites (question_id) VALUES (?)", (question["id"],))
                conn.execute("INSERT OR IGNORE INTO question_favorites (question_id) VALUES (?)", (question["id"],))
                conn.execute("INSERT OR IGNORE INTO paper_favorites (paper_id) VALUES (?)", (question["paper_id"],))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0], 1)
                conn.execute("DELETE FROM questions WHERE id = ?", (question["id"],))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0], 1)
                conn.execute("DELETE FROM papers WHERE id = ?", (question["paper_id"],))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0], 0)

    def test_personal_backup_exports_and_imports_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source_db = Path(directory) / "source.sqlite3"
            target_db = Path(directory) / "target.sqlite3"
            prepare_user_database(source_db, SEED)
            prepare_user_database(target_db, SEED)
            with connect(source_db) as conn:
                question = conn.execute("SELECT id, paper_id, question_code FROM questions WHERE paper_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()
                reference = conn.execute(
                    "SELECT id FROM reference_answers WHERE question_id = ? ORDER BY id LIMIT 1",
                    (question["id"],),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE ai_settings
                       SET provider_name = 'LocalAI', api_key = 'secret-key',
                           model = 'custom-model', prompt_template = 'custom prompt'
                     WHERE id = 1
                    """
                )
                conn.execute(
                    """
                    UPDATE agent_ai_settings
                       SET use_grading_api = 0, provider_name = 'CoachAI',
                           api_key = 'coach-secret', model = 'coach-model'
                     WHERE id = 1
                    """
                )
                attempt_id = conn.execute(
                    """
                    INSERT INTO attempts (
                        question_id, answer_text, word_count, personal_note,
                        duration_seconds, paper_elapsed_seconds, paper_time_excluded, created_at
                    )
                    VALUES (?, '备份作答', 4, '备份复盘笔记', 720, 3600, 1, '2026-06-24 04:00:00')
                    """,
                    (question["id"],),
                ).lastrowid
                conn.execute(
                    """
                    UPDATE attempts
                       SET grading_references_configured = 1,
                           grading_reference_ids = ?,
                           custom_reference_answer = '备份自定义参考'
                     WHERE id = ?
                    """,
                    (f"[{reference['id']}]", attempt_id),
                )
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, provider, model, report_text, created_at) VALUES (?, 'Codex', 'manual', '备份报告', '2026-06-24 04:10:00')",
                    (attempt_id,),
                )
                conn.execute("INSERT INTO question_favorites (question_id) VALUES (?)", (question["id"],))
                conn.execute("INSERT INTO paper_favorites (paper_id) VALUES (?)", (question["paper_id"],))
                material_number = conn.execute(
                    "SELECT material_number FROM paper_materials WHERE paper_id = ? ORDER BY material_number LIMIT 1",
                    (question["paper_id"],),
                ).fetchone()[0]
                save_text_annotations(
                    conn,
                    "material",
                    [{"start": 1, "end": 4, "color": "yellow"}],
                    "material-hash",
                    question_id=question["id"],
                    material_number=material_number,
                )
                save_text_annotations(
                    conn,
                    "answer",
                    [{"start": 0, "end": 2, "style": "strike"}],
                    "answer-hash",
                    attempt_id=attempt_id,
                )
                save_text_annotations(
                    conn,
                    "note",
                    [{"start": 0, "end": 2, "color": "blue"}],
                    "note-hash",
                    attempt_id=attempt_id,
                )
                conn.execute(
                    """
                    INSERT INTO agent_memories (memory_type, memory_key, content, confidence)
                    VALUES ('semantic', 'target_exam', '浙江省考', 0.9)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO training_plan_items (question_id, title, reason, target_date, status)
                    VALUES (?, '归纳概括练习', '巩固总结能力', '2026-06-30', 'todo')
                    """,
                    (question["id"],)
                )
                run_id = conn.execute(
                    """
                    INSERT INTO agent_runs (task_type, subject_type, subject_id, status)
                    VALUES ('diagnosis', 'attempt', ?, 'completed')
                    """,
                    (attempt_id,)
                ).lastrowid
                conv_id = conn.execute(
                    """
                    INSERT INTO agent_conversations (title, entrypoint, status)
                    VALUES ('会话备份', 'chat', 'active')
                    """
                ).lastrowid
                msg_id = conn.execute(
                    """
                    INSERT INTO agent_messages (conversation_id, run_id, role, content)
                    VALUES (?, ?, 'user', '备份消息')
                    """,
                    (conv_id, run_id)
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO agent_feedback (run_id, rating, note)
                    VALUES (?, 1, '点赞备份')
                    """,
                    (run_id,)
                )
                payload = export_personal_data(conn)
                payload_with_key = export_personal_data(conn, include_api_key=True)
            self.assertNotIn("api_key", payload["ai_settings"])
            self.assertNotIn("api_key", payload["agent_ai_settings"])
            self.assertEqual(payload_with_key["ai_settings"]["api_key"], "secret-key")
            self.assertEqual(payload_with_key["agent_ai_settings"]["api_key"], "coach-secret")
            self.assertEqual(payload["version"], 3)
            self.assertEqual(payload["attempts"][0]["question"]["question_code"], question["question_code"])
            self.assertEqual(payload["attempts"][0]["personal_note"], "备份复盘笔记")
            self.assertEqual(payload["attempts"][0]["duration_seconds"], 720)
            self.assertEqual(payload["attempts"][0]["paper_elapsed_seconds"], 3600)
            self.assertEqual(payload["attempts"][0]["paper_time_excluded"], 1)
            self.assertEqual(payload["question_favorites"][0]["question"]["question_code"], question["question_code"])
            self.assertTrue(payload["paper_favorites"][0]["paper"]["paper_code"])
            self.assertEqual(len(payload["text_annotations"]), 3)
            self.assertEqual(payload["agent_memories"][0]["content"], "浙江省考")
            self.assertEqual(payload["training_plan_items"][0]["title"], "归纳概括练习")
            self.assertEqual(payload["agent_runs"][0]["task_type"], "diagnosis")
            self.assertEqual(payload["agent_conversations"][0]["title"], "会话备份")
            self.assertEqual(payload["agent_messages"][0]["content"], "备份消息")
            self.assertEqual(payload["agent_feedback"][0]["note"], "点赞备份")
            material_annotation = next(
                item for item in payload["text_annotations"] if item["target_type"] == "material"
            )
            self.assertEqual(material_annotation["question"]["question_code"], question["question_code"])

            with connect(target_db) as conn:
                payload_with_key["attempts"][0]["question_id"] = 999999
                payload_with_key["attempts"][0]["question"]["id"] = 999999
                payload_with_key["question_favorites"][0]["question_id"] = 999999
                payload_with_key["question_favorites"][0]["question"]["id"] = 999999
                payload_with_key["paper_favorites"][0]["paper_id"] = 999999
                payload_with_key["paper_favorites"][0]["paper"]["id"] = 999999
                material_annotation = next(
                    item for item in payload_with_key["text_annotations"] if item["target_type"] == "material"
                )
                material_annotation["question_id"] = 999999
                material_annotation["question"]["id"] = 999999
                counts = import_personal_data(conn, payload_with_key)
                self.assertEqual(counts["attempts"], 1)
                self.assertEqual(counts["reports"], 1)
                self.assertEqual(counts["question_favorites"], 1)
                self.assertEqual(counts["paper_favorites"], 1)
                self.assertEqual(counts["annotations"], 3)
                self.assertEqual(counts["memories"], 1)
                self.assertEqual(counts["runs"], 1)
                self.assertEqual(counts["conversations"], 1)
                self.assertEqual(counts["messages"], 1)
                self.assertEqual(counts["feedback"], 1)
                self.assertEqual(counts["training_plan_items"], 1)
                self.assertEqual(counts["skipped_attempts"], 0)
                self.assertEqual(conn.execute("SELECT answer_text FROM attempts").fetchone()[0], "备份作答")
                restored_attempt = conn.execute("SELECT * FROM attempts").fetchone()
                self.assertEqual(restored_attempt["question_id"], question["id"])
                self.assertEqual(restored_attempt["personal_note"], "备份复盘笔记")
                self.assertEqual(restored_attempt["duration_seconds"], 720)
                self.assertEqual(restored_attempt["paper_elapsed_seconds"], 3600)
                self.assertEqual(restored_attempt["paper_time_excluded"], 1)
                self.assertEqual(restored_attempt["grading_references_configured"], 1)
                self.assertEqual(restored_attempt["grading_reference_ids"], f"[{reference['id']}]")
                self.assertEqual(restored_attempt["custom_reference_answer"], "备份自定义参考")
                self.assertEqual(conn.execute("SELECT report_text FROM grading_reports").fetchone()[0], "备份报告")
                self.assertEqual(conn.execute("SELECT provider_name FROM ai_settings WHERE id = 1").fetchone()[0], "LocalAI")
                self.assertEqual(conn.execute("SELECT api_key FROM ai_settings WHERE id = 1").fetchone()[0], "secret-key")
                self.assertEqual(conn.execute("SELECT prompt_template FROM ai_settings WHERE id = 1").fetchone()[0], "custom prompt")
                agent_settings = conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone()
                self.assertEqual(agent_settings["use_grading_api"], 0)
                self.assertEqual(agent_settings["provider_name"], "CoachAI")
                self.assertEqual(agent_settings["model"], "coach-model")
                self.assertEqual(conn.execute("SELECT content FROM agent_memories").fetchone()[0], "浙江省考")
                restored_annotations = conn.execute(
                    "SELECT target_type, question_id, attempt_id, annotations_json FROM text_annotations ORDER BY target_type"
                ).fetchall()
                self.assertEqual({row["target_type"] for row in restored_annotations}, {"material", "answer", "note"})
                self.assertEqual(
                    next(row for row in restored_annotations if row["target_type"] == "material")["question_id"],
                    question["id"],
                )
                self.assertEqual(
                    next(row for row in restored_annotations if row["target_type"] == "answer")["attempt_id"],
                    restored_attempt["id"],
                )

                duplicate_counts = import_personal_data(conn, payload_with_key)
                self.assertEqual(duplicate_counts["attempts"], 0)
                self.assertEqual(duplicate_counts["reports"], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM grading_reports").fetchone()[0], 1)
                conn.execute(
                    """
                    UPDATE attempts
                       SET grading_references_configured = 0,
                           grading_reference_ids = '[]',
                           custom_reference_answer = ''
                    """
                )
                import_personal_data(conn, payload_with_key)
                restored_attempt = conn.execute("SELECT * FROM attempts").fetchone()
                self.assertEqual(restored_attempt["grading_references_configured"], 1)
                self.assertEqual(restored_attempt["grading_reference_ids"], f"[{reference['id']}]")
                self.assertEqual(restored_attempt["custom_reference_answer"], "备份自定义参考")

    def test_noncurrent_personal_backup_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target_db = Path(directory) / "target.sqlite3"
            prepare_user_database(target_db, SEED)
            with connect(target_db) as conn:
                payload = {
                    "format": "gongkao-personal-backup",
                    "version": 1,
                }
                with self.assertRaisesRegex(ValueError, "仅支持版本 3"):
                    import_personal_data(conn, payload)

    def test_grading_reference_selection_defaults_and_rejects_cross_question_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "references.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute("UPDATE ai_settings SET grading_mode = 'enhanced' WHERE id = 1")
                question = conn.execute(
                    """
                    SELECT q.id
                      FROM questions q
                     WHERE (SELECT COUNT(*) FROM reference_answers r WHERE r.question_id = q.id) >= 2
                     ORDER BY q.id LIMIT 1
                    """
                ).fetchone()
                references = conn.execute(
                    "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id",
                    (question["id"],),
                ).fetchall()
                foreign_reference = conn.execute(
                    "SELECT id FROM reference_answers WHERE question_id <> ? ORDER BY id LIMIT 1",
                    (question["id"],),
                ).fetchone()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '测试作答', 4)",
                    (question["id"],),
                ).lastrowid
                attempt = conn.execute(
                    "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()

                selected, selected_ids, custom = attempt_grading_references(
                    attempt, references
                )
                self.assertEqual(len(selected), len(references))
                self.assertEqual(selected_ids, {row["id"] for row in references})
                self.assertEqual(custom, "")

                data = (
                    f"reference_id={references[0]['id']}"
                    f"&reference_id={foreign_reference['id']}"
                    "&reference_id=not-a-number"
                    "&custom_reference_answer=%E8%A1%A5%E5%85%85%E7%AD%94%E6%A1%88"
                )
                selected, selected_ids, custom = grading_references_from_form(
                    data, references
                )
                self.assertEqual(selected_ids, [references[0]["id"]])
                self.assertEqual(custom, "补充答案")
                save_attempt_grading_references(
                    conn, attempt_id, selected_ids, custom
                )
                saved = conn.execute(
                    "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                selected, selected_set, custom = attempt_grading_references(
                    saved, references
                )
                self.assertEqual([row["id"] for row in selected], selected_ids)
                self.assertEqual(selected_set, set(selected_ids))
                self.assertEqual(custom, "补充答案")

    def test_api_grade_saves_current_reference_selection_in_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "api-reference.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute("UPDATE ai_settings SET grading_mode = 'basic' WHERE id = 1")
                question = conn.execute(
                    """
                    SELECT q.id
                      FROM questions q
                     WHERE (SELECT COUNT(*) FROM reference_answers r WHERE r.question_id = q.id) >= 2
                     ORDER BY q.id LIMIT 1
                    """
                ).fetchone()
                references = conn.execute(
                    "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id LIMIT 2",
                    (question["id"],),
                ).fetchall()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '接口测试作答', 6)",
                    (question["id"],),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlencode(
                    [
                        ("reference_id", references[0]["id"]),
                        ("custom_reference_answer", "接口补充参考"),
                    ]
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/attempts/{attempt_id}/grade",
                    data=body,
                    method="POST",
                )
                with patch(
                    "gongkao.web.controllers.grading.chat_completion",
                    return_value=("接口批改报告", '{"ok":true}'),
                ) as completion:
                    with urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                self.assertEqual(completion.call_args.args[2], {"thinking": "disabled"})

                with connect(user_db) as conn:
                    attempt = conn.execute(
                        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                    ).fetchone()
                    report = conn.execute(
                        "SELECT * FROM grading_reports WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchone()
                    job_count = conn.execute(
                        "SELECT COUNT(*) FROM grading_jobs WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchone()[0]
                self.assertEqual(attempt["grading_references_configured"], 1)
                self.assertEqual(attempt["grading_reference_ids"], f"[{references[0]['id']}]")
                self.assertEqual(attempt["custom_reference_answer"], "接口补充参考")
                self.assertIn(references[0]["answer_text"], report["prompt_text"])
                self.assertNotIn(references[1]["answer_text"], report["prompt_text"])
                self.assertIn("接口补充参考", report["prompt_text"])
                self.assertEqual(job_count, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_grading_controls_follow_current_mode_and_default_to_deep_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "grading-control-defaults.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute("UPDATE ai_settings SET grading_mode = 'basic' WHERE id = 1")
                question_id = conn.execute(
                    "SELECT id FROM questions ORDER BY id LIMIT 1"
                ).fetchone()[0]
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '设置回显测试', 6)",
                    (question_id,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO grading_jobs (
                        attempt_id, input_hash, status, progress, options_json
                    ) VALUES (?, 'old-job', 'completed', 100, '{"deep_thinking": false}')
                    """,
                    (attempt_id,),
                )

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/attempts/{attempt_id}",
                    timeout=10,
                ) as response:
                    html = response.read().decode("utf-8")
                smart_input = re.search(
                    r'<input[^>]+name="use_smart_grading"[^>]*>',
                    html,
                ).group(0)
                deep_input = re.search(
                    r'<input[^>]+name="use_deep_thinking"[^>]*>',
                    html,
                ).group(0)
                self.assertNotIn(" checked", smart_input)
                self.assertIn(" checked", deep_input)
                self.assertIn("data-deep-thinking-preference", deep_input)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_api_grade_smart_checkbox_overrides_basic_default(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "api-smart-toggle.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute("UPDATE ai_settings SET grading_mode = 'basic' WHERE id = 1")
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()[0]
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '智能开关测试', 6)",
                    (question_id,),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                body = urlencode(
                    {
                        "answer_text": "点击批改时的最新答案\n落款",
                        "answer_format_json": '["center","right"]',
                        "use_smart_grading": "1",
                        "use_deep_thinking": "1",
                        "use_analogies": "1",
                        "use_knowledge": "1",
                        "use_history": "1",
                    }
                ).encode("utf-8")
                with patch("gongkao.web.controllers.grading.start_grading_job") as starter:
                    with urlopen(Request(f"{base_url}/attempts/{attempt_id}/grade", data=body, method="POST"), timeout=10):
                        pass
                self.assertEqual(starter.call_count, 1)
                with connect(user_db) as conn:
                    job = conn.execute(
                        "SELECT * FROM grading_jobs WHERE attempt_id = ? ORDER BY id DESC LIMIT 1",
                        (attempt_id,),
                    ).fetchone()
                options = json.loads(job["options_json"])
                self.assertTrue(options["deep_thinking"])
                self.assertTrue(options["analogies"])
                with connect(user_db) as conn:
                    saved_attempt = conn.execute(
                        "SELECT answer_text, answer_format_json FROM attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                self.assertEqual(saved_attempt["answer_text"], "点击批改时的最新答案\n落款")
                self.assertEqual(saved_attempt["answer_format_json"], '["center","right"]')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_api_grade_repairs_only_hard_overflow_once_and_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "api-word-budget.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute("UPDATE ai_settings SET grading_mode = 'basic' WHERE id = 1")
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()[0]
                conn.execute("UPDATE questions SET word_limit = '250字以内' WHERE id = ?", (question_id,))
                short_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '短答案', 3)",
                    (question_id,),
                ).lastrowid
                overflow_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '超限答案', 4)",
                    (question_id,),
                ).lastrowid
                failed_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '返修失败', 4)",
                    (question_id,),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                short_report = (
                    "## 总体评分\n- 核心判断：短答案也保留\n\n"
                    f"## 修改版答案\n\n{'甲' * 208}\n\n"
                    "## 优化建议\n1. 原建议"
                )
                with patch(
                    "gongkao.web.controllers.grading.chat_completion",
                    return_value=(short_report, '{"round":1}'),
                ) as completion:
                    with urlopen(Request(f"{base_url}/attempts/{short_attempt_id}/grade", data=b"", method="POST"), timeout=10):
                        pass
                    self.assertEqual(completion.call_count, 1)

                overflow_report = (
                    "## 总体评分\n- 核心判断：必须逐字保留\n\n"
                    f"## 修改版答案\n\n{'甲' * 250}\n\n"
                    "## 优化建议\n1. 也必须逐字保留"
                )
                repaired_body = "乙" * 235
                with patch(
                    "gongkao.web.controllers.grading.chat_completion",
                    side_effect=[
                        (overflow_report, '{"round":1}'),
                        (f"<revised_answer>{repaired_body}</revised_answer>", '{"round":2}'),
                    ],
                ) as completion:
                    with urlopen(Request(f"{base_url}/attempts/{overflow_attempt_id}/grade", data=b"", method="POST"), timeout=10):
                        pass
                    self.assertEqual(completion.call_count, 2)

                with patch(
                    "gongkao.web.controllers.grading.chat_completion",
                    side_effect=[
                        (overflow_report, '{"round":1}'),
                        (f"<revised_answer>{'丙' * 250}</revised_answer>", '{"round":2}'),
                    ],
                ) as completion:
                    with urlopen(Request(f"{base_url}/attempts/{failed_attempt_id}/grade", data=b"", method="POST"), timeout=10) as response:
                        failed_html = response.read().decode("utf-8")
                    self.assertEqual(completion.call_count, 2)

                with connect(user_db) as conn:
                    short_saved = conn.execute(
                        "SELECT * FROM grading_reports WHERE attempt_id = ?", (short_attempt_id,)
                    ).fetchone()
                    repaired_saved = conn.execute(
                        "SELECT * FROM grading_reports WHERE attempt_id = ?", (overflow_attempt_id,)
                    ).fetchone()
                    failed_saved = conn.execute(
                        "SELECT * FROM grading_reports WHERE attempt_id = ?", (failed_attempt_id,)
                    ).fetchone()
                self.assertIsNotNone(short_saved)
                self.assertIn("状态：符合字数要求，篇幅偏短", short_saved["report_text"])
                self.assertIsNotNone(repaired_saved)
                self.assertIn("核心判断：必须逐字保留", repaired_saved["report_text"])
                self.assertIn("1. 也必须逐字保留", repaired_saved["report_text"])
                self.assertIn(repaired_body, repaired_saved["report_text"])
                self.assertNotIn("甲" * 250, repaired_saved["report_text"])
                self.assertIn("localized revised-answer repair", repaired_saved["raw_response"])
                self.assertIsNotNone(failed_saved)
                self.assertEqual(failed_saved["status"], "ok")
                self.assertIn("丙" * 250, failed_saved["report_text"])
                self.assertIn("批改报告", failed_html)
                self.assertIn("修改版答案超出字数限制", failed_html)
                self.assertIn("丙" * 250, failed_html)
                self.assertNotIn("临时报告", failed_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_manual_report_uses_strict_count_without_calling_api(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "manual-word-budget.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()[0]
                conn.execute("UPDATE questions SET word_limit = '250字以内' WHERE id = ?", (question_id,))
                accepted_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '手工报告', 4)",
                    (question_id,),
                ).lastrowid
                rejected_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '手工超限', 4)",
                    (question_id,),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with patch("gongkao.web.controllers.grading.chat_completion") as completion:
                    accepted_body = urlencode(
                        {"provider": "Codex", "report_text": f"## 修改版答案\n\n{'甲' * 208}"}
                    ).encode("utf-8")
                    with urlopen(Request(f"{base_url}/attempts/{accepted_id}/reports", data=accepted_body, method="POST"), timeout=10):
                        pass

                    rejected_body = urlencode(
                        {"provider": "Codex", "report_text": f"## 修改版答案\n\n{'乙' * 250}"}
                    ).encode("utf-8")
                    with urlopen(Request(f"{base_url}/attempts/{rejected_id}/reports", data=rejected_body, method="POST"), timeout=10):
                        pass
                    completion.assert_not_called()

                with connect(user_db) as conn:
                    accepted_count = conn.execute(
                        "SELECT COUNT(*) FROM grading_reports WHERE attempt_id = ?", (accepted_id,)
                    ).fetchone()[0]
                    rejected_count = conn.execute(
                        "SELECT COUNT(*) FROM grading_reports WHERE attempt_id = ?", (rejected_id,)
                    ).fetchone()[0]
                self.assertEqual(accepted_count, 1)
                self.assertEqual(rejected_count, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_attempt_note_autosaves_and_stays_out_of_grading_package(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "attempt-note.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute("SELECT * FROM questions ORDER BY id LIMIT 1").fetchone()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, 'note answer', 2)",
                    (question["id"],),
                ).lastrowid
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                package = build_grading_package(question, [], attempt)
                self.assertNotIn("复盘笔记", package)
                self.assertNotIn("接口复盘笔记", package)

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlencode({"personal_note": "接口复盘笔记"}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/attempts/{attempt_id}/note",
                    data=body,
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    self.assertEqual(
                        conn.execute("SELECT personal_note FROM attempts WHERE id = ?", (attempt_id,)).fetchone()[0],
                        "接口复盘笔记",
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_text_annotation_api_persists_and_clears_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "annotations.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                target = conn.execute(
                    """
                    SELECT q.id AS question_id, m.material_number
                      FROM questions q
                      JOIN paper_materials m ON m.paper_id = q.paper_id
                     ORDER BY q.id, m.material_number
                     LIMIT 1
                    """
                ).fetchone()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '测试作答', 4)",
                    (target["question_id"],),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/annotations"
                payloads = [
                    {
                        "target_type": "material",
                        "question_id": target["question_id"],
                        "material_number": target["material_number"],
                        "text_hash": "10:abc",
                        "annotations": [{"start": 0, "end": 3, "color": "yellow", "style": ""}],
                    },
                    {
                        "target_type": "answer",
                        "attempt_id": attempt_id,
                        "text_hash": "4:def",
                        "annotations": [{"start": 1, "end": 3, "color": "", "style": "strike"}],
                    },
                ]
                for payload in payloads:
                    request = Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(json.loads(response.read())["ok"])
                with connect(user_db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM text_annotations").fetchone()[0], 2)
                with urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/attempts/{attempt_id}",
                    timeout=10,
                ) as response:
                    attempt_page = response.read().decode("utf-8")
                self.assertIn('data-saved-text-hash="4:def"', attempt_page)
                self.assertIn('data-annotation-target="answer"', attempt_page)

                payloads[0]["annotations"] = []
                request = Request(
                    url,
                    data=json.dumps(payloads[0]).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    rows = conn.execute("SELECT target_type FROM text_annotations").fetchall()
                    self.assertEqual([row["target_type"] for row in rows], ["answer"])
                    conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM text_annotations").fetchone()[0], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_answer_and_note_save_text_with_anchored_annotations_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "annotation-save.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()[0]
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, personal_note) VALUES (?, '甲乙丙丁', 4, '旧笔记')",
                    (question_id,),
                ).lastrowid
            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                answer_annotations = [{
                    "start": 0,
                    "end": 2,
                    "color": "orange",
                    "style": "",
                    "quote": "甲乙",
                    "prefix": "",
                    "suffix": "新增丙丁",
                    "anchor_version": 1,
                }]
                answer_body = urlencode({
                    "answer_text": "甲乙新增丙丁",
                    "annotations_json": json.dumps(answer_annotations, ensure_ascii=False),
                    "annotations_text_hash": "6:answer",
                }).encode("utf-8")
                with urlopen(Request(f"{base}/attempts/{attempt_id}/update", data=answer_body, method="POST"), timeout=10) as response:
                    self.assertEqual(response.status, 200)

                note_annotations = [{
                    "start": 2,
                    "end": 4,
                    "color": "purple",
                    "style": "",
                    "quote": "笔记",
                    "prefix": "复盘",
                    "suffix": "新增",
                    "anchor_version": 1,
                }]
                note_body = urlencode({
                    "personal_note": "复盘笔记新增",
                    "annotations_json": json.dumps(note_annotations, ensure_ascii=False),
                    "annotations_text_hash": "6:note",
                }).encode("utf-8")
                with urlopen(Request(f"{base}/attempts/{attempt_id}/note", data=note_body, method="POST"), timeout=10) as response:
                    self.assertEqual(response.status, 200)

                with connect(user_db) as conn:
                    attempt = conn.execute("SELECT answer_text, personal_note FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                    self.assertEqual(attempt["answer_text"], "甲乙新增丙丁")
                    self.assertEqual(attempt["personal_note"], "复盘笔记新增")
                    rows = conn.execute(
                        "SELECT target_type, annotations_json FROM text_annotations WHERE attempt_id = ? ORDER BY target_type",
                        (attempt_id,),
                    ).fetchall()
                    self.assertEqual([row["target_type"] for row in rows], ["answer", "note"])
                    saved = {row["target_type"]: json.loads(row["annotations_json"])[0] for row in rows}
                    self.assertEqual(saved["answer"]["quote"], "甲乙")
                    self.assertEqual(saved["answer"]["color"], "orange")
                    self.assertEqual(saved["note"]["quote"], "笔记")
                    self.assertEqual(saved["note"]["color"], "purple")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_attempt_submission_records_timer_values_and_update_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "attempt-timer.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute("SELECT id FROM questions WHERE paper_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlencode(
                    {
                        "answer_text": "接口计时作答\n第二段",
                        "answer_format_json": '["center","right"]',
                        "duration_seconds": "735",
                        "paper_elapsed_seconds": "1800",
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/questions/{question['id']}/attempts",
                    data=body,
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    attempt = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(attempt["duration_seconds"], 735)
                self.assertEqual(attempt["paper_elapsed_seconds"], 735)
                self.assertEqual(attempt["paper_time_excluded"], 0)
                self.assertEqual(attempt["answer_format_json"], '["center","right"]')

                excluded_body = urlencode(
                    {
                        "answer_text": "不计入套卷的二刷",
                        "duration_seconds": "30",
                        "paper_elapsed_seconds": "9999",
                        "paper_time_excluded": "1",
                    }
                ).encode("utf-8")
                excluded_request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/questions/{question['id']}/attempts",
                    data=excluded_body,
                    method="POST",
                )
                with urlopen(excluded_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    excluded = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(excluded["duration_seconds"], 30)
                self.assertEqual(excluded["paper_elapsed_seconds"], 735)
                self.assertEqual(excluded["paper_time_excluded"], 1)

                shorter_body = urlencode(
                    {
                        "answer_text": "计入套卷但更短的二刷",
                        "duration_seconds": "30",
                        "paper_elapsed_seconds": "9999",
                    }
                ).encode("utf-8")
                shorter_request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/questions/{question['id']}/attempts",
                    data=shorter_body,
                    method="POST",
                )
                with urlopen(shorter_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    shorter = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(shorter["paper_elapsed_seconds"], 735)

                longer_body = urlencode(
                    {
                        "answer_text": "计入套卷且更长的三刷",
                        "duration_seconds": "900",
                        "paper_elapsed_seconds": "9999",
                    }
                ).encode("utf-8")
                longer_request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/questions/{question['id']}/attempts",
                    data=longer_body,
                    method="POST",
                )
                with urlopen(longer_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    longer = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(longer["paper_elapsed_seconds"], 900)

                update_body = urlencode(
                    {
                        "answer_text": "修改后的作答\n落款",
                        "answer_format_json": '["left","right"]',
                    }
                ).encode("utf-8")
                update_request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/attempts/{attempt['id']}/update",
                    data=update_body,
                    method="POST",
                )
                with urlopen(update_request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                with connect(user_db) as conn:
                    updated = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt["id"],)).fetchone()
                self.assertEqual(updated["answer_text"], "修改后的作答\n落款")
                self.assertEqual(updated["answer_format_json"], '["left","right"]')
                self.assertEqual(updated["duration_seconds"], 735)
                self.assertEqual(updated["paper_elapsed_seconds"], 735)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_favorite_return_path_rejects_external_destinations(self):
        self.assertEqual(safe_return_path("/papers?region=广东", "/papers"), "/papers?region=广东")
        self.assertEqual(safe_return_path("//example.com/path", "/papers"), "/papers")
        self.assertEqual(safe_return_path("https://example.com", "/papers"), "/papers")

    def test_static_asset_path_supports_modules_and_rejects_traversal(self):
        static_root = ROOT / "static"
        self.assertEqual(
            safe_static_path(static_root, "js/core.js"),
            static_root / "js" / "core.js",
        )
        self.assertIsNone(safe_static_path(static_root, "../gongkao/db.py"))

    def test_organization_aliases_keep_unknown_names(self):
        self.assertEqual(canonicalize_organization("粉笔单淑玲"), "粉笔")
        self.assertEqual(canonicalize_organization("袁东2026"), "袁东")
        self.assertEqual(canonicalize_organization("独立作者甲"), "独立作者甲")

    def test_server_can_bind_an_available_port(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "gongkao.sqlite3"
            server = create_server(port=0, db_path=user_db)
            try:
                self.assertGreater(server.server_address[1], 0)
                self.assertEqual(server.server_address[0], "127.0.0.1")
            finally:
                server.server_close()

    def test_layout_template_escapes_metadata_and_preserves_page_markup(self):
        rendered = layout(
            "<unsafe>",
            '<section id="content">正文</section>',
            "papers",
            [("notice", "<message>")],
            transient_route=True,
        )
        self.assertIn("<title>&lt;unsafe&gt;</title>", rendered)
        self.assertIn('<section id="content">正文</section>', rendered)
        self.assertIn("&lt;message&gt;", rendered)
        self.assertNotIn("<message>", rendered)
        self.assertIn('data-active-section="papers"', rendered)
        self.assertIn('data-transient-route="1"', rendered)

    def test_multiple_servers_keep_database_and_autosave_state_isolated(self):
        import gongkao.web.application as server_module

        original_db_path = server_module.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.sqlite3"
            second_path = Path(directory) / "second.sqlite3"
            first = create_server(port=0, db_path=first_path)
            second = create_server(port=0, db_path=second_path)
            try:
                self.assertEqual(first.app_context.db_path, first_path)
                self.assertEqual(second.app_context.db_path, second_path)
                self.assertIsNot(first.app_context.autosave, second.app_context.autosave)
                self.assertEqual(server_module.DB_PATH, original_db_path)
            finally:
                first.server_close()
                second.server_close()

    def test_report_markdown_renders_tables_and_bold(self):
        html = markdownish("## 踩点对比\n| 采分点 | 命中情况 |\n| --- | --- |\n| 设计方案巧 | **命中** |\n")
        self.assertIn("<table", html)
        self.assertIn("report-score-table", html)
        self.assertIn("<th>采分点</th>", html)
        self.assertIn("<strong>命中</strong>", html)
        self.assertNotIn("**命中**", html)

    def test_report_markdown_renders_visual_grading_annotations(self):
        html = markdownish(
            "## 原文可视化批注\n[好|概括准确|命中题干核心]\n[删|空泛表述|没有材料依据]",
            source_text="先概括准确，再删掉空泛表述。",
            annotation_scope="test-report",
        )
        self.assertIn("grading-annotation-map", html)
        self.assertIn("grading-annotation-source-text", html)
        self.assertIn('id="test-report-mark-1"', html)
        self.assertIn('href="#test-report-mark-2"', html)
        self.assertIn("亮点", html)
        self.assertIn("<del>空泛表述</del>", html)

    def test_report_annotations_support_severity_and_insertion_anchors(self):
        html = markdownish(
            "## 原文可视化批注\n"
            "[亮点|主体明确|命中任务对象||positive|]\n"
            "[润色|表达一般|措辞不够准确||low|建议改写]\n"
            "[补充|补上实施效果|要点缺失|主体明确|high|主体明确后补充实施效果]\n"
            "[关键|方向错误|偏离题干任务||critical|改为围绕题干作答]",
            source_text="主体明确，但表达一般，后文方向错误。",
            annotation_scope="severity-report",
        )
        self.assertIn("grading-review-connectors", html)
        self.assertIn("grading-insert-anchor", html)
        self.assertIn("severity-low", html)
        self.assertIn("severity-critical", html)
        self.assertIn("主体明确后补充实施效果", html)
        self.assertIn('data-annotation-id="3"', html)

    def test_report_annotations_are_numbered_in_source_order(self):
        html = markdownish(
            "## 原文可视化批注\n"
            "[修改|开头|先改标题||medium|改写开头]\n"
            "[补充|补充结论|需要落到文章结尾|文章结尾|high|补充完整结论]\n"
            "[润色|中间段落|调整措辞||low|优化表达]",
            source_text="开头。中间段落。文章结尾。",
            annotation_scope="ordered-report",
        )
        self.assertIn(
            'class="grading-source-mark polish severity-low" '
            'id="ordered-report-mark-2"',
            html,
        )
        self.assertIn(
            'class="grading-insert-anchor severity-high" '
            'id="ordered-report-mark-3"',
            html,
        )
        self.assertIn(
            'class="grading-annotation-note add severity-high"\n'
            '                id="ordered-report-note-3"',
            html,
        )

    def test_report_answer_snapshot_prefers_saved_result_and_recovers_old_prompts(self):
        self.assertEqual(
            report_answer_snapshot(
                '本次作答：\n{"id": 1, "answer_text": "结构化旧答案"}\n\n下一段',
                {"answer_snapshot": "持久化快照"},
                "当前答案",
            ),
            "持久化快照",
        )
        self.assertEqual(
            report_answer_snapshot(
                '本次作答：\n{"id": 1, "answer_text": "结构化旧答案"}\n\n下一段',
                {},
                "当前答案",
            ),
            "结构化旧答案",
        )
        self.assertEqual(
            report_answer_snapshot(
                "## 我的答案\n- 作答时间：今天\n- 实际占格数：6\n"
                "原文命中识别规则：按原文判断。\n\n批改时旧答案\n\n## 请按以下标准批改",
                {},
                "当前答案",
            ),
            "批改时旧答案",
        )

    def test_report_markdown_hides_repeated_dimension_score_section(self):
        html = markdownish(
            "## 总体评分\n- 总分：15/20\n\n"
            "## 维度评分\n| 维度 | 得分 | 满分 |\n| --- | ---: | ---: |\n| 内容 | 7 | 10 |\n\n"
            "## 踩点对比\n- 命中核心点"
        )
        self.assertIn("总体评分", html)
        self.assertNotIn("维度评分", html)
        self.assertNotIn("<th>维度</th>", html)
        self.assertIn("踩点对比", html)

    def test_regular_report_table_does_not_get_score_table_layout(self):
        html = markdownish("| 项目 | 说明 |\n| --- | --- |\n| 总分 | 20分 |\n")
        self.assertIn("report-table", html)
        self.assertNotIn("report-score-table", html)

    def test_filter_order_helpers(self):
        self.assertEqual(sort_regions(["浙江", "全国", "安徽", "新疆"]), ["全国", "安徽", "新疆", "浙江"])
        self.assertEqual(
            sort_exam_types(["广东省考", "国考", "公安院校联考", "北京选调", "安徽省考"]),
            ["国考", "安徽省考", "广东省考", "北京选调", "公安院校联考"],
        )

    def test_beijing_time_display_converts_sqlite_utc(self):
        self.assertEqual(format_beijing_time("2026-06-24 05:30:00"), "2026-06-24 13:30")

    def test_report_score_parser_is_strict_and_normalized(self):
        self.assertEqual(parse_report_score("## 总体评分\n- 总分：13/20分"), 65.0)
        self.assertEqual(parse_report_score("得分: 17.5/25"), 70.0)
        self.assertIsNone(parse_report_score("总分：优秀"))
        self.assertIsNone(parse_report_score("总分：21/20"))
        self.assertIsNone(parse_report_score("总分：10/0"))

    def test_streak_allows_today_or_yesterday_as_latest_day(self):
        today = date(2026, 6, 24)
        self.assertEqual(calculate_streak([date(2026, 6, 24), date(2026, 6, 23)], today), 2)
        self.assertEqual(calculate_streak([date(2026, 6, 23), date(2026, 6, 22)], today), 2)
        self.assertEqual(calculate_streak([date(2026, 6, 20)], today), 0)

    def test_training_statistics_use_latest_successful_report(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "stats.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute("SELECT id, paper_id FROM questions ORDER BY id LIMIT 1").fetchone()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, duration_seconds, created_at) VALUES (?, '答案', 123, 900, '2026-06-24 04:00:00')",
                    (question["id"],),
                ).lastrowid
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text, status) VALUES (?, '总分：10/20', 'ok')",
                    (attempt_id,),
                )
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text, status) VALUES (?, '总分：16/20', 'ok')",
                    (attempt_id,),
                )
                conn.execute("INSERT INTO question_favorites (question_id) VALUES (?)", (question["id"],))
                conn.execute("INSERT INTO paper_favorites (paper_id) VALUES (?)", (question["paper_id"],))
                stats = build_training_statistics(conn, datetime(2026, 6, 24, 18, 0, tzinfo=BEIJING_TIMEZONE))
            self.assertEqual(stats["attempt_count"], 1)
            self.assertEqual(stats["word_count"], 123)
            self.assertEqual(stats["total_duration_seconds"], 900)
            self.assertEqual(stats["timed_attempt_count"], 1)
            self.assertEqual(stats["average_score"], 80.0)
            self.assertEqual(stats["recognized_scores"], 1)
            self.assertEqual(stats["favorite_questions"], 1)
            self.assertEqual(stats["favorite_papers"], 1)
            self.assertEqual(len(stats["daily"]), 365)
            self.assertEqual(stats["daily"][-1][0], date(2026, 6, 24))

    def test_module_score_statistics_support_first_and_best_attempt_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "module-score.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute(
                    "SELECT id, question_type FROM questions WHERE question_type <> '' ORDER BY id LIMIT 1"
                ).fetchone()
                first_attempt = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, duration_seconds, created_at) VALUES (?, '第一次', 3, 600, '2026-06-20 04:00:00')",
                    (question["id"],),
                ).lastrowid
                second_attempt = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, duration_seconds, created_at) VALUES (?, '第二次', 3, 900, '2026-06-21 04:00:00')",
                    (question["id"],),
                ).lastrowid
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text, status) VALUES (?, '总分：10/20', 'ok')",
                    (first_attempt,),
                )
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text, status) VALUES (?, '总分：18/20', 'ok')",
                    (second_attempt,),
                )
                first_stats = build_module_score_statistics(conn, "first")
                best_stats = build_module_score_statistics(conn, "best")
            first_module = next(item for item in first_stats["modules"] if item["name"] == question["question_type"])
            best_module = next(item for item in best_stats["modules"] if item["name"] == question["question_type"])
            self.assertLess(first_module["average_score"], best_module["average_score"])
            self.assertEqual(first_module["questions"][0]["attempt_id"], first_attempt)
            self.assertEqual(best_module["questions"][0]["attempt_id"], second_attempt)
            self.assertEqual(first_module["questions"][0]["duration_seconds"], 600)
            self.assertEqual(best_module["questions"][0]["duration_seconds"], 900)
            self.assertTrue(first_module["trend"])

    def test_activity_heatmap_uses_absolute_levels(self):
        self.assertEqual([activity_level(value) for value in [0, 1, 2, 3, 4, 5, 6, 7, 9]], [0, 1, 1, 2, 2, 3, 3, 4, 4])

    def test_activity_heatmap_counts_distinct_questions_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "activity.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                questions = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 2").fetchall()
                conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, created_at) VALUES (?, 'a', 1, '2026-06-24 02:00:00')",
                    (questions[0]["id"],),
                )
                conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, created_at) VALUES (?, 'b', 1, '2026-06-24 03:00:00')",
                    (questions[0]["id"],),
                )
                conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count, created_at) VALUES (?, 'c', 1, '2026-06-24 04:00:00')",
                    (questions[1]["id"],),
                )
                stats = build_training_statistics(conn, datetime(2026, 6, 24, 18, 0, tzinfo=BEIJING_TIMEZONE))
        self.assertEqual(stats["daily"][-1], (date(2026, 6, 24), 2))

    def test_attempt_ui_supports_live_count_editing_and_card_management(self):
        server_source = read_server_application(ROOT)
        script_source = read_static_scripts(ROOT)
        style_source = read_static_styles(ROOT)
        self.assertIn('data-answer-form', server_source)
        self.assertIn('data-dirty-submit', server_source)
        self.assertIn('/update"', server_source)
        self.assertIn('class="record-main"', server_source)
        self.assertIn('class="record-delete"', server_source)
        self.assertNotIn('>打开</a></div>', server_source)
        self.assertIn("countAnswerCharacters", script_source)
        self.assertIn("answerLineCount", script_source)
        self.assertIn("answerGridMetrics", script_source)
        self.assertIn("answerGridCellsForLine", script_source)
        self.assertIn('compact.includes("左右") && upper > 500', script_source)
        self.assertIn("current >= limit", script_source)
        self.assertIn("必须低于 ${limit} 字", script_source)
        self.assertIn("Math.ceil(Math.max(0, Number(characterCount) || 0) / 25)", script_source)
        self.assertIn("data-line-status", server_source)
        self.assertIn("data-current-line-status", server_source)
        self.assertIn("${currentLines}/${limitLines}", script_source)
        self.assertIn("caretLineCells", script_source)
        self.assertIn("本行：${caretLineCells(value, caretOffset)}/25格", script_source)
        self.assertNotIn("（每行25格）", script_source)
        self.assertIn("gongkao.answerDraft", script_source)
        self.assertIn("bindChineseAnswerPunctuation(input)", script_source)
        self.assertIn('addEventListener("beforeinput"', script_source)
        self.assertIn('event.data === "\\\\"', script_source)
        self.assertIn('event.data === ","', script_source)
        self.assertIn('replacement = "、"', script_source)
        self.assertIn('replacement = "，"', script_source)
        self.assertNotIn('event.code === "Comma"', script_source)
        self.assertIn("setRangeText", script_source)
        self.assertIn("data-autosave-status", server_source)
        self.assertIn("data-attempt-note-input", server_source)
        self.assertIn("data-practice-timer", server_source)
        self.assertIn('data-timer-autostart="1"', server_source)
        self.assertIn("data-editor-toolbar", server_source)
        self.assertIn("data-editor-align=\"center\"", server_source)
        self.assertNotIn("data-editor-indent", server_source)
        self.assertIn("contenteditable=\"true\"", server_source)
        self.assertIn("data-answer-hidden", server_source)
        self.assertIn("__paragraphAlignments", script_source)
        self.assertIn("lineSpan", script_source)
        self.assertIn("data-editor-line", script_source)
        self.assertNotIn("collectLineUnits", script_source)
        self.assertNotIn("intersectsNode", script_source)
        self.assertIn("initializeFavoriteToggles", script_source)
        self.assertIn('name="duration_seconds"', server_source)
        self.assertIn('name="paper_elapsed_seconds"', server_source)
        self.assertIn("gongkao.practiceTimer", script_source)
        self.assertIn("bindPracticeTimer", script_source)
        self.assertIn("data-paper-summary-timer", server_source)
        self.assertIn("data-paper-base-seconds", server_source)
        self.assertIn("data-question-base-seconds", server_source)
        self.assertIn("data-paper-derived-display", server_source)
        self.assertIn("data-paper-time-excluded", server_source)
        self.assertIn("exclude_question_id", server_source)
        self.assertIn("data-timer-toggle>开始", server_source)
        self.assertIn('data-timer-autostart="1"', server_source)
        # Only explicit timed entry points start the timer. The workflow's
        # direct "02 写答案" route has no timer query parameter.
        self.assertNotIn('if query.get("practice", [""])[0] == "paper"', server_source)
        self.assertIn('if query.get("timer", [""])[0] == "auto"', server_source)
        self.assertIn("bindPaperSummaryTimer", script_source)
        self.assertIn("Math.max(questionBaseSeconds", script_source)
        self.assertIn("timerAutostart", script_source)
        self.assertIn("searchParams.delete(\"timer\")", script_source)
        self.assertIn("history.replaceState", script_source)
        self.assertIn('\\u5f00\\u59cb', script_source)
        self.assertIn("paperTimeExcluded", script_source)
        self.assertIn("persistCurrent(true)", script_source)
        self.assertNotIn("data-linked-timer-key", server_source)
        self.assertNotIn("data-linked-timer-display", server_source)
        self.assertNotIn("practiceTimerElapsedMs(linkedState", script_source)
        self.assertIn(".attempt-form .practice-timer .button", style_source)
        self.assertNotIn('data-timer-kind="paper" data-timer-key="paper-{question[\'paper_id\']}" data-timer-autostart="1"', server_source)
        self.assertIn("/note", server_source)
        self.assertIn("note-badge", server_source)
        self.assertIn("data-attempt-note-status", script_source)
        self.assertIn("navigator.sendBeacon", script_source)
        self.assertIn(".attempt-note-panel", style_source)
        self.assertIn("build_revised_answer_retry_prompt", server_source)
        self.assertIn("report_answer_snapshot(", server_source)
        self.assertIn(
            "markdownish(display_report_text, report_return_to, report_source_text",
            server_source,
        )
        self.assertIn("parse_revised_answer_repair", server_source)
        self.assertIn("replace_revised_answer_body", server_source)
        self.assertIn("revised_answer_word_count_status", server_source)
        self.assertIn("report-word-limit-warning", server_source)
        self.assertNotIn("AI 局部压缩后仍超出字数限制", server_source)
        self.assertNotIn("for _ in range(2)", server_source)
        self.assertIn("paper-attempt-history", server_source)
        self.assertIn("answer-input-field", server_source)
        self.assertIn("answer-input-field", style_source)
        self.assertIn("timer-exclude-toggle span", style_source)
        self.assertIn(".note-badge", style_source)
        self.assertIn('href="/notes"', server_source)
        self.assertIn("def page_notes", server_source)
        self.assertIn("pagination_html(\"/notes\"", server_source)
        self.assertIn("已恢复本地草稿", script_source)
        self.assertIn("草稿已自动保存", script_source)
        self.assertIn("removeDraft(storageKey)", script_source)
        self.assertIn("submitButton.hidden", script_source)
        self.assertIn("已超出", script_source)
        self.assertIn("[data-autosave-status]", style_source)
        self.assertIn("[hidden]", style_source)
        self.assertIn("display: none !important", style_source)

    def test_grading_reference_picker_updates_manual_and_api_inputs(self):
        server_source = read_server_application(ROOT)
        script_source = read_static_scripts(ROOT)
        style_source = read_static_styles(ROOT)
        self.assertIn('data-grading-references', server_source)
        self.assertIn('/grading-references"', server_source)
        self.assertIn('data-grade-submit', server_source)
        self.assertIn('data-package-copy', server_source)
        self.assertIn("grading_references_from_form", server_source)
        self.assertIn("initialSignature", script_source)
        self.assertIn("data-reference-select-all", script_source)
        self.assertIn("aria-disabled", script_source)
        self.assertIn("grading-context-disclosure", server_source)
        self.assertIn("查看题目、材料与参考答案", server_source)
        self.assertIn("data-resize-storage-key=\"gongkao.gradingPaneWidth.v2\"", server_source)
        self.assertIn("data-min-main-width", server_source)
        self.assertIn("data-min-side-width", server_source)
        self.assertIn(".grading-material-body", style_source)
        self.assertIn(".content-tabs-row [data-clear-active-material-highlights]", style_source)
        self.assertIn("width: auto;", style_source)
        self.assertIn("rect.width - minQuestionWidth - 24", script_source)
        self.assertNotIn("const maxWidth = 620", script_source)

    def test_question_resources_render_as_tabs(self):
        materials = [{"id": 11, "material_number": 1, "title": "材料1", "content": "材料正文"}]
        references = [{
            "organization": "粉笔",
            "canonical_organization": "粉笔",
            "answer_text": "答案正文",
            "scoring_points": "",
            "is_reviewed": 1,
        }]
        material_html = tabbed_materials(materials, "test")
        reference_html = tabbed_references(references, "test")
        self.assertIn('role="tab"', material_html)
        self.assertIn('data-tab-target="test-material-panel-0"', material_html)
        self.assertNotIn("data-material-highlight", material_html)
        highlighted_material_html = tabbed_materials(
            materials,
            "test",
            highlight_scope="question-1",
            question_id=1,
            saved_annotations={
                1: {
                    "annotations_json": '[{"start":0,"end":2,"color":"yellow","style":""}]',
                    "text_hash": "4:abcd",
                }
            },
        )
        self.assertIn("data-material-highlight", highlighted_material_html)
        self.assertIn('data-highlight-scope="question-1"', highlighted_material_html)
        self.assertIn('data-material-id="11"', highlighted_material_html)
        self.assertIn('data-annotation-target="material"', highlighted_material_html)
        self.assertIn('data-question-id="1"', highlighted_material_html)
        self.assertIn('data-annotation-save-url="/annotations"', highlighted_material_html)
        self.assertIn("data-saved-annotations=", highlighted_material_html)
        self.assertIn("content-tabs-row", highlighted_material_html)
        self.assertIn("data-clear-active-material-highlights", highlighted_material_html)
        self.assertIn('role="tab"', reference_html)
        self.assertIn("答案正文", reference_html)
        self.assertNotIn("采分点", reference_html)

    def test_material_highlights_persist_for_question_and_attempt_pages(self):
        server_source = read_server_application(ROOT)
        records_source = (ROOT / "gongkao" / "services" / "personal_records.py").read_text(
            encoding="utf-8"
        )
        script = read_static_scripts(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn('highlight_scope=f"question-{question_id}"', server_source)
        self.assertIn('highlight_scope=f"question-{question[\'id\']}"', server_source)
        self.assertIn("gongkao.materialHighlights:v1", script)
        self.assertIn('data-annotation-target="material"', server_source)
        self.assertIn("saved_annotations=material_annotations", server_source)
        self.assertIn("persistTextAnnotations", script)
        self.assertIn("data-saved-annotations", records_source)
        self.assertIn('["yellow", "orange", "pink", "purple", "blue", "green"]', script)
        self.assertIn("removeHighlightOverlap", script)
        self.assertIn("normalizeMaterialHighlights", script)
        self.assertIn("showToolbarForHighlight", script)
        self.assertIn("!selection.isCollapsed", script)
        self.assertIn("if (!toolbar.hidden && !toolbar.contains(event.target))", script)
        self.assertIn("dataset.highlightStart", script)
        self.assertIn("is-selected-highlight", script)
        self.assertIn("activeRangeFromHighlight", script)
        self.assertIn("data-highlight-toolbar", script)
        self.assertIn(".material-highlight-toolbar", css)
        self.assertIn(".content-tabs-row", css)
        self.assertIn('.material-highlight[data-highlight-color="yellow"]', css)

    def test_attempt_answer_and_note_support_persistent_text_annotations(self):
        server_source = read_server_application(ROOT)
        records_source = (ROOT / "gongkao" / "services" / "personal_records.py").read_text(
            encoding="utf-8"
        )
        script = read_static_scripts(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn("data-text-annotation", server_source)
        self.assertIn('data-annotation-type="answer"', server_source)
        self.assertIn('data-annotation-type="note"', server_source)
        self.assertIn('data-annotation-target="answer"', server_source)
        self.assertIn('data-annotation-target="note"', server_source)
        self.assertIn("data-attempt-id", server_source)
        self.assertIn('contenteditable="true"', server_source)
        self.assertIn("data-answer-hidden", server_source)
        self.assertIn("data-clear-text-annotations", server_source)
        self.assertIn("gongkao.textAnnotations:v1", script)
        self.assertIn('data-highlight-style="strike"', script)
        self.assertIn("划掉", script)
        self.assertIn("annotation-note-icon", script)
        self.assertIn("highlight-color-group", script)
        self.assertIn("highlight-action-group", script)
        self.assertIn("text-decoration-color: #c84f43", css)
        self.assertIn("color: #202725", css)
        self.assertIn("background: #edf5f7", css)
        self.assertIn("editableValue", script)
        self.assertIn("editablePlainText", script)
        self.assertIn("blockTags.has(tagName)", script)
        self.assertIn("pushLineBreak(false)", script)
        self.assertIn("syncTextAnnotationsForEdit", script)
        self.assertIn("textEditRange", script)
        self.assertIn("anchoredTextRange", script)
        self.assertIn("anchorTextAnnotations", script)
        self.assertIn("annotationStates", script)
        self.assertIn("annotationTextSnapshots", script)
        self.assertIn("annotationSavePayload", script)
        self.assertIn("pendingLocalAnnotations", script)
        self.assertIn('method: "POST"', script)
        self.assertIn("data-saved-text-hash", records_source)
        self.assertIn("CREATE TABLE IF NOT EXISTS text_annotations", (ROOT / "gongkao" / "db.py").read_text(encoding="utf-8"))
        self.assertNotIn("innerText", script)
        self.assertIn("bindPlainTextPaste", script)
        self.assertIn("renderTextAnnotations", script)
        self.assertIn("data-annotation-style=\"strike\"", css)
        self.assertIn("line-through", css)
        self.assertIn('.text-annotation-highlight[data-highlight-color="orange"]', css)
        self.assertIn('.text-annotation-highlight[data-highlight-color="purple"]', css)
        self.assertIn(".direct-text-editor", css)
        self.assertIn("缺的要点来自材料哪里？为什么找出来？下次遇到怎么识别？", server_source)
        self.assertNotIn("答案标注视图", server_source)
        self.assertNotIn("笔记标注视图", server_source)

    def test_question_page_does_not_offer_source_link(self):
        server_source = read_server_application(ROOT)
        self.assertNotIn("打开来源链接", server_source)

    def test_question_page_hides_references_by_default(self):
        server_source = read_server_application(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn('<details class="reference-disclosure">', server_source)
        self.assertIn("作答后再看", server_source)
        self.assertNotIn('<details class="reference-disclosure" open>', server_source)
        self.assertIn(".reference-disclosure summary::after", css)

    def test_answer_sidebar_is_resizable(self):
        server_source = read_server_application(ROOT)
        css = read_static_styles(ROOT)
        script = read_static_scripts(ROOT)
        self.assertIn("data-resizable-attempt-pane", server_source)
        self.assertIn("data-pane-resizer", server_source)
        self.assertIn("--attempt-pane-width", css)
        self.assertIn(".pane-resizer", css)
        self.assertIn("gongkao.attemptPaneWidth", script)
        self.assertIn('data-default-side-width="500"', server_source)
        self.assertIn('data-default-side-width="480"', server_source)
        self.assertIn('data-default-side-ratio="0.42"', server_source)
        self.assertIn("document.cookie", script)
        self.assertIn("Max-Age=0", script)
        self.assertIn("viewState.persistentSet(storageKey, value)", script)
        self.assertIn("readPaneWidth(storageKey)", script)
        self.assertIn("writePaneWidth(storageKey, nextWidth)", script)
        self.assertIn("--attempt-pane-width: 500px", css)
        self.assertIn("--attempt-pane-width: clamp(380px, 38vw, 680px)", css)
        self.assertIn("setPointerCapture", script)
        self.assertIn('addEventListener("mousedown"', script)

    def test_sidebar_pages_are_canonical_while_filters_remain_persistent(self):
        server_source = read_server_application(ROOT)
        shell_html = layout("测试", "<p>内容</p>", "papers")
        script = read_static_scripts(ROOT)
        self.assertNotIn('data-session-nav="papers"', server_source)
        self.assertNotIn('data-session-nav="settings"', server_source)
        self.assertIn('<a href="/papers" class="active">', shell_html)
        self.assertIn('data-session-scroll="grading-tools"', server_source)
        self.assertIn('f"{question[\'title\']} - 研申",', server_source)
        self.assertIn("transient_route=citation_target", server_source)
        self.assertIn("class ViewStateManager", script)
        self.assertIn('VIEW_STATE_NAMESPACE = "gongkao.viewState.v2"', script)
        self.assertIn('data-active-section="papers"', shell_html)
        self.assertNotIn("initializeHistoryEntry()", script)
        self.assertNotIn("navigation:pending", script)
        self.assertNotIn("history.back()", script)
        self.assertIn("initializeDisclosureState()", script)
        self.assertIn('["return_to", "grading_job", "smart", "deep"]', script)
        self.assertIn("initializeSessionNavigation()", script)
        self.assertIn("restoreFilterState(form)", script)
        self.assertIn("rememberFilterState(form)", script)
        self.assertIn("viewState.persistentGet(storageKey)", script)
        self.assertIn("data-filter-reset", server_source)
        self.assertIn("viewState.clearFilterState(form)", script)
        self.assertIn("class AutosaveCoordinator", script)
        self.assertIn('data-autosave-url="/attempts/{attempt_id}/update"', server_source)
        self.assertIn('window.addEventListener("pagehide"', script)

    def test_pagination_preserves_filters_and_bounds_page(self):
        self.assertEqual(requested_page({"page": ["bad"]}), 1)
        self.assertEqual(requested_page({"page": ["-4"]}), 1)
        html = pagination_html(
            "/papers",
            {"region": "广东", "year_from": "2020", "q": ""},
            page=2,
            total_items=40,
            page_size=18,
        )
        self.assertIn("第 19-36 项，共 40 项", html)
        self.assertIn("region=%E5%B9%BF%E4%B8%9C", html)
        self.assertIn("year_from=2020", html)
        self.assertIn("page=3", html)
        self.assertIn('class="page-jump"', html)
        self.assertIn('name="region" value="广东"', html)
        self.assertIn('name="page" type="number"', html)
        self.assertIn('max="3"', html)

    def test_adaptive_page_size_is_bounded(self):
        self.assertEqual(requested_page_size({}), 12)
        self.assertEqual(requested_page_size({"per_page": ["12"]}), 12)
        self.assertEqual(requested_page_size({"per_page": ["bad"]}), 12)
        self.assertEqual(requested_page_size({"per_page": ["200"]}), 40)

    def test_list_pages_expose_adaptive_pagination(self):
        server_source = read_server_application(ROOT)
        script = read_static_scripts(ROOT)
        self.assertIn("data-adaptive-pagination", server_source)
        self.assertIn("adaptivePageSize", script)
        self.assertIn('url.searchParams.set("per_page"', script)

    def test_list_pages_expose_work_filters_and_attempt_pagination(self):
        server_source = read_server_application(ROOT)
        self.assertIn('"work_status": query.get("work_status"', server_source)
        self.assertIn("QUESTION_WORK_STATUS_OPTIONS", server_source)
        self.assertIn("PAPER_WORK_STATUS_OPTIONS", server_source)
        self.assertIn('"question_type": query.get("question_type"', server_source)
        self.assertIn('pagination_html("/attempts"', server_source)
        attempts_start = server_source.index("def page_attempts")
        attempts_end = server_source.index("def page_notes", attempts_start)
        attempts_source = server_source[attempts_start:attempts_end]
        self.assertIn("page_size = 10", attempts_source)
        self.assertIn('class="record-word-count"', attempts_source)
        self.assertIn("份报告</small>", attempts_source)
        self.assertIn('"noted", "有笔记"', attempts_source)
        self.assertIn("HAVING has_note > 0", attempts_source)
        self.assertIn("<b>{esc(state)}</b></div>", attempts_source)
        self.assertNotIn("<b>{esc(state)}</b>{note_badge}", attempts_source)
        self.assertNotIn('name="per_page"', attempts_source)
        self.assertIn('action="/settings/export"', server_source)
        self.assertIn('action="/settings/import"', server_source)

    def test_question_and_paper_overviews_support_reference_count_sorting(self):
        server_source = read_server_application(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn('"sort_refs": "1" if query.get("sort_refs"', server_source)
        self.assertIn("reference_count DESC, q.year DESC", server_source)
        self.assertIn("average_reference_count DESC, reference_count DESC", server_source)
        self.assertIn('name="sort_refs" value="1"', server_source)
        self.assertIn("答案数优先", server_source)
        self.assertIn("题均答案优先", server_source)
        self.assertIn("average_reference_count", server_source)
        self.assertIn("overview-filters", css)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn("@media (min-width: 1121px)", css)

        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "reference-sort.sqlite3"
            prepare_user_database(user_db, SEED)
            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urlopen(f"{base}/?sort_refs=1&year_from=2020&per_page=20", timeout=10) as response:
                    question_html = response.read().decode("utf-8")
                question_counts = [
                    int(value)
                    for value in re.findall(r'data-reference-count="(\d+)"', question_html)
                ]
                self.assertGreater(len(question_counts), 1)
                self.assertEqual(question_counts, sorted(question_counts, reverse=True))
                self.assertIn('name="sort_refs" value="1" checked', question_html)

                with urlopen(f"{base}/papers?sort_refs=1&year_from=2020&per_page=20", timeout=10) as response:
                    paper_html = response.read().decode("utf-8")
                paper_averages = [
                    float(value)
                    for value in re.findall(r'data-average-reference-count="([0-9.]+)"', paper_html)
                ]
                self.assertGreater(len(paper_averages), 1)
                self.assertEqual(paper_averages, sorted(paper_averages, reverse=True))
                self.assertIn("平均每题", paper_html)
                self.assertIn('name="sort_refs" value="1" checked', paper_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_versioned_autosave_rejects_stale_writes_and_preserves_answer_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "versioned-autosave.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question_id = conn.execute("SELECT id FROM questions ORDER BY id LIMIT 1").fetchone()[0]
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '原答案', 3)",
                    (question_id,),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def autosave(revision, answer):
                body = urlencode({
                    "answer_text": answer,
                    "autosave_session": "test-session-versioned",
                    "autosave_revision": revision,
                }).encode("utf-8")
                request = Request(
                    f"{base}/attempts/{attempt_id}/update",
                    data=body,
                    headers={"Accept": "application/json", "X-Gongkao-Autosave": "1"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                self.assertTrue(autosave(2, "  新答案")["accepted"])
                self.assertFalse(autosave(1, "迟到的旧答案")["accepted"])
                with connect(user_db) as conn:
                    self.assertEqual(
                        conn.execute("SELECT answer_text FROM attempts WHERE id = ?", (attempt_id,)).fetchone()[0],
                        "  新答案",
                    )

                self.assertTrue(autosave(3, "")["accepted"])
                with connect(user_db) as conn:
                    self.assertEqual(
                        conn.execute("SELECT answer_text FROM attempts WHERE id = ?", (attempt_id,)).fetchone()[0],
                        "",
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_workflow_navigation_uses_fixed_parents_and_isolates_evidence_returns(self):
        server_source = read_server_application(ROOT)
        handler = MagicMock()
        query = {"return_to": ["/papers"]}
        self.assertTrue(dispatch_get(handler, "/questions/42", query))
        handler.page_question.assert_called_once_with("/questions/42", query)
        post_handler = MagicMock()
        self.assertTrue(dispatch_post(post_handler, "/attempts/42/update"))
        post_handler.handle_update_attempt.assert_called_once_with("/attempts/42/update")
        self.assertIn("def return_path_from_query", server_source)
        self.assertIn("def return_path_from_form", server_source)
        self.assertIn("def local_url", server_source)
        self.assertIn("def back_link", server_source)
        self.assertIn('question_href = f"/questions/{question[\'id\']}"', server_source)
        self.assertIn('paper_href = f"/papers/{paper[\'id\']}"', server_source)
        self.assertIn('tab_href = local_url(f"/papers/{paper_id}", q=question["id"])', server_source)
        self.assertIn('back_href, back_label, title = "/papers", "返回题库"', server_source)
        self.assertIn('back_href = paper_href if paper_id else "/"', server_source)
        self.assertIn('back_href, back_label, title = question_href, "返回题目"', server_source)
        self.assertIn("data-nav-back", server_source)
        self.assertIn("def evidence_return_path", server_source)
        self.assertIn("关闭引用并返回原位置", server_source)
        self.assertIn('transient_route=citation_target', server_source)

        paper = {"id": 8, "paper_name": "测试卷", "year": 2026, "region": "浙江", "exam_type": "省考"}
        question = {"id": 21, "paper_id": 8, "paper_name": "测试卷", "title": "测试题", "question_number": 2, "year": 2026, "region": "浙江", "exam_type": "省考"}
        paper_html = workflow_header("paper", paper, question=question)
        answer_html = workflow_header("answer", question, question=question)
        grading_html = workflow_header("grading", question, question=question, attempt={"id": 33})
        self.assertIn('href="/papers" data-nav-back>返回题库</a>', paper_html)
        self.assertIn('href="/papers/8?q=21" data-nav-back>返回试卷</a>', answer_html)
        self.assertIn('href="/questions/21" data-nav-back>返回题目</a>', grading_html)
        self.assertEqual(evidence_return_path({"return_to": ["/attempts/7"]}), "")
        self.assertEqual(evidence_return_path({"return_to": ["/agent/conversations/4"]}), "/agent/conversations/4")

    def test_answered_questions_link_to_latest_grading_and_workflow_can_return(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "answer-navigation.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute(
                    """
                    SELECT id, paper_id, title
                      FROM questions
                     WHERE paper_id IS NOT NULL AND year >= 2020
                  ORDER BY year DESC, zhejiang_relevance DESC, id DESC
                     LIMIT 1
                    """
                ).fetchone()
                older_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '较早作答', 4)",
                    (question["id"],),
                ).lastrowid
                latest_attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '最近作答', 4)",
                    (question["id"],),
                ).lastrowid

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urlopen(f"{base}/", timeout=10) as response:
                    library_html = response.read().decode("utf-8")
                with urlopen(f"{base}/questions/{question['id']}", timeout=10) as response:
                    question_html = response.read().decode("utf-8")
                with urlopen(f"{base}/questions/{question['id']}?timer=auto", timeout=10) as response:
                    timed_question_html = response.read().decode("utf-8")
                with urlopen(f"{base}/papers/{question['paper_id']}?q={question['id']}", timeout=10) as response:
                    paper_html = response.read().decode("utf-8")

                self.assertIn(f'href="/attempts/{latest_attempt_id}"', library_html)
                self.assertNotIn(f'href="/attempts/{older_attempt_id}" class="card-main-link"', library_html)
                self.assertIn(f'class="workflow-stage" href="/attempts/{latest_attempt_id}"', question_html)
                self.assertIn(f'class="workflow-stage" href="/attempts/{latest_attempt_id}"', paper_html)
                self.assertNotIn('data-timer-autostart="1"', question_html)
                self.assertIn('data-timer-autostart="1"', timed_question_html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_filter_memory_is_persistent_and_ignores_adaptive_page_size(self):
        server_source = read_server_application(ROOT)
        script = read_static_scripts(ROOT)
        self.assertIn('FILTER_RESTORE_BOOTSTRAP = ""', server_source)
        self.assertIn("viewState.persistentGet(storageKey)", script)
        self.assertIn("viewState.persistentSet(storageKey, sessionFilterState(form))", script)
        self.assertIn('if (key === "per_page") return;', script)
        self.assertIn('savedParams.delete("per_page")', script)
        self.assertIn('name !== "per_page" && currentParams.has(name)', script)
        self.assertIn("this.persistentRemove(this.filterKey(form))", script)

    def test_settings_can_manage_local_records_without_deleting_library_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "local-records.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                question = conn.execute(
                    "SELECT id, paper_id FROM questions WHERE paper_id IS NOT NULL ORDER BY id LIMIT 1"
                ).fetchone()
                attempt_id = conn.execute(
                    "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, '测试作答', 4)",
                    (question["id"],),
                ).lastrowid
                conn.execute(
                    "INSERT INTO grading_reports (attempt_id, report_text) VALUES (?, '测试报告')",
                    (attempt_id,),
                )
                conn.execute(
                    """
                    INSERT INTO text_annotations (
                        annotation_key, target_type, attempt_id, text_hash, annotations_json
                    ) VALUES ('test-answer', 'answer', ?, 'hash', '[]')
                    """,
                    (attempt_id,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO question_favorites (question_id) VALUES (?)",
                    (question["id"],),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO paper_favorites (paper_id) VALUES (?)",
                    (question["paper_id"],),
                )
                run_id = conn.execute(
                    "INSERT INTO agent_runs (task_type, status) VALUES ('diagnosis', 'completed')"
                ).lastrowid
                conversation_id = conn.execute(
                    "INSERT INTO agent_conversations (title) VALUES ('待清理线程')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO agent_messages (conversation_id, run_id, role, content) VALUES (?, ?, 'user', '测试')",
                    (conversation_id, run_id),
                )
                conn.execute(
                    "INSERT INTO training_plan_items (run_id, question_id, title) VALUES (?, ?, '待清理计划')",
                    (run_id, question["id"]),
                )
                question_count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]

            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlencode(
                    {
                        "record_scope": [
                            "attempts",
                            "annotations",
                            "favorites",
                            "agent",
                            "plans",
                            "browser",
                        ]
                    },
                    doseq=True,
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/settings/local-records/clear",
                    data=body,
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("已清理：", html)
                self.assertIn("data-clear-local-record-state", html)
                self.assertIn('action="/settings/local-records/open"', html)
                self.assertIn("打开记录位置", html)
                with connect(user_db) as conn:
                    for table in (
                        "attempts",
                        "grading_reports",
                        "text_annotations",
                        "question_favorites",
                        "paper_favorites",
                        "agent_conversations",
                        "agent_messages",
                        "agent_runs",
                        "training_plan_items",
                    ):
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], question_count)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_settings").fetchone()[0], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_annotation_clear_offers_undo(self):
        script = read_static_scripts(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn("function showUndoToast(message, onUndo)", script)
        self.assertIn('showUndoToast("已清除本材料批注"', script)
        self.assertIn('showUndoToast("已清除本区域批注"', script)
        self.assertIn("writeTextAnnotations(container, previousAnnotations)", script)
        self.assertIn(".undo-toast", css)

    def test_settings_separates_record_management_and_exposes_display_controls(self):
        server_source = read_server_application(ROOT)
        settings_source = (ROOT / "gongkao" / "web" / "controllers" / "settings.py").read_text(encoding="utf-8")
        script = read_static_scripts(ROOT)
        css = read_static_styles(ROOT)
        host_source = (ROOT / "desktop_host" / "Program.cs").read_text(encoding="utf-8")

        self.assertIn('href="/settings/local-records"', server_source)
        self.assertIn("def page_settings_local_records(", server_source)
        self.assertIn('class="button primary settings-import-submit"', server_source)
        self.assertIn('data-display-profile="fullscreen"', server_source)
        self.assertIn('data-display-profile="window"', server_source)
        self.assertNotIn('data-display-profile="1080p"', server_source)
        self.assertNotIn('data-display-profile="2k"', server_source)
        self.assertIn('data-ui-zoom-adjust="-1"', server_source)
        self.assertIn('data-ui-zoom-adjust="1"', server_source)
        self.assertIn('data-startup-restore="last-page"', server_source)
        self.assertIn('data-startup-restore="scroll"', server_source)
        self.assertIn("<span>01</span><div><h2>AI 批改与教练</h2>", settings_source)
        self.assertNotIn("<h2>教练索引</h2>", settings_source)
        self.assertIn("<span>02</span><div><h2>数据管理</h2>", settings_source)
        self.assertIn("<span>03</span><div><h2>启动与恢复</h2>", settings_source)
        self.assertIn("<span>04</span><div><h2>显示与缩放</h2>", settings_source)
        self.assertLess(settings_source.index("settings-data-panel"), settings_source.index("settings-restore-panel"))
        self.assertLess(settings_source.index("settings-restore-panel"), settings_source.index("display-settings"))
        self.assertIn('name="agent_connection_mode"', settings_source)
        self.assertIn('value="inherit"', settings_source)
        self.assertIn('value="custom"', settings_source)
        self.assertIn("沿用批改 API", settings_source)
        self.assertNotIn("可留空沿用现有值", settings_source)
        self.assertIn("STARTUP_RESTORE_BOOTSTRAP", server_source)
        self.assertEqual(server_source.count("{settings_credits()}"), 2)
        self.assertNotIn("1080P 全屏默认 90%", server_source)
        self.assertIn("DISPLAY_PREFERENCES_KEY", script)
        self.assertIn("function defaultUiZoom()", script)
        self.assertIn("<= 1080 ? 0.9 : 1", script)
        self.assertIn('window.chrome?.webview?.postMessage', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("UI_ZOOM_STEP = 0.1", script)
        self.assertIn("function syncSidebarNavigation(", script)
        self.assertIn("stableSubnavUpdate", script)
        self.assertIn("const filterSections = {", script)
        self.assertIn("const savedParams = new URLSearchParams(saved)", script)
        self.assertIn("savedParams.forEach((value, name) => url.searchParams.append(name, value))", script)
        self.assertIn("STARTUP_RESTORE_PREFERENCES_KEY", script)
        self.assertIn("function initializeStartupRestoreSettings(", script)
        self.assertIn("restorePreferences.restoreScroll", script)
        self.assertEqual(server_source.count('filter_panel_hidden = " hidden"'), 2)
        self.assertNotIn("nav-primary {'active' if", server_source)
        self.assertIn(".display-profile-card.is-active", css)
        self.assertIn("grid-template-columns: minmax(0, 1.15fr) minmax(390px, .85fr)", css)
        self.assertIn(".settings-block-inline", css)
        self.assertIn(".settings-toggle-row", css)
        self.assertIn(".settings-contributor", css)
        self.assertIn("top: -1px", css)
        self.assertIn("CoreWebView2.WebMessageReceived += HandleWebMessage", host_source)
        self.assertIn('profile == "fullscreen"', host_source)
        self.assertIn("windowedBounds", host_source)
        self.assertIn("browser.ZoomFactor", host_source)

    def test_ai_coach_can_inherit_or_override_grading_api(self):
        with tempfile.TemporaryDirectory() as directory:
            user_db = Path(directory) / "agent-api-settings.sqlite3"
            prepare_user_database(user_db, SEED)
            with connect(user_db) as conn:
                conn.execute(
                    """
                    UPDATE ai_settings
                       SET mode = 'codex', provider_name = 'GradingAI',
                           model = 'grading-model', api_key = 'grading-key'
                     WHERE id = 1
                    """
                )
                inherited = load_effective_agent_settings(conn)
                self.assertEqual(inherited["mode"], "api")
                self.assertEqual(inherited["provider_name"], "GradingAI")
                self.assertEqual(inherited["model"], "grading-model")
                conn.execute(
                    """
                    UPDATE agent_ai_settings
                       SET use_grading_api = 0, provider_name = 'CoachAI',
                           model = 'coach-model', api_key = 'coach-key', temperature = 0.4
                     WHERE id = 1
                    """
                )
                overridden = load_effective_agent_settings(conn)
                self.assertEqual(overridden["provider_name"], "CoachAI")
                self.assertEqual(overridden["model"], "coach-model")
                self.assertEqual(overridden["api_key"], "coach-key")
                self.assertEqual(overridden["temperature"], 0.4)
                grading = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                self.assertEqual(grading["provider_name"], "GradingAI")
                self.assertEqual(grading["api_key"], "grading-key")
            server = create_server(port=0, db_path=user_db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base}/settings", timeout=10) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("AI 批改与教练", html)
                self.assertIn("沿用批改 API", html)
                self.assertIn("单独设置", html)
                body = urlencode(
                    {
                        "mode": "api",
                        "grading_mode": "enhanced",
                        "provider_name": "GradingAI",
                        "api_base_url": "https://grading.example",
                        "model": "grading-model",
                        "temperature": "0.2",
                        "api_key_env": "GRADING_API_KEY",
                        "agent_provider_name": "CoachNext",
                        "agent_connection_mode": "custom",
                        "agent_api_base_url": "https://coach.example",
                        "agent_model": "coach-next",
                        "agent_temperature": "0.6",
                        "agent_api_key_env": "COACH_API_KEY",
                        "agent_api_key": "coach-next-key",
                    }
                ).encode("utf-8")
                request = Request(f"{base}/settings", data=body, method="POST")
                with urlopen(request, timeout=10) as response:
                    self.assertIn("设置已保存", response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            with connect(user_db) as conn:
                saved = conn.execute("SELECT * FROM agent_ai_settings WHERE id = 1").fetchone()
                self.assertEqual(saved["use_grading_api"], 0)
                self.assertEqual(saved["provider_name"], "CoachNext")
                self.assertEqual(saved["model"], "coach-next")
                self.assertEqual(saved["temperature"], 0.6)
                self.assertEqual(saved["api_key"], "coach-next-key")

    def test_app_icon_is_wired_into_browser_and_windows_build(self):
        shell_html = layout("图标测试", "<p>内容</p>", "home")
        css = read_static_styles(ROOT)
        svg = (ROOT / "static" / "app-icon.svg").read_text(encoding="utf-8")
        spec = (ROOT / "研申.spec").read_text(encoding="utf-8")
        self.assertTrue((ROOT / "static" / "app-icon.svg").exists())
        self.assertTrue((ROOT / "static" / "app-icon.png").exists())
        self.assertTrue((ROOT / "assets" / "app-icon.ico").exists())
        self.assertRegex(shell_html, r'rel="icon" href="/static/app-icon\.svg(?:\?v=[^"]+)?"')
        self.assertRegex(shell_html, r'<img src="/static/app-icon\.svg(?:\?v=[^"]+)?"')
        self.assertIn(".brand-mark img", css)
        self.assertIn('x="49" y="49" width="158" height="158"', svg)
        self.assertIn('x="31" y="31" width="17" height="17"', svg)
        self.assertIn("font-family=\"'Songti SC','STSong','SimSun',serif\"", svg)
        self.assertIn(">申</text>", svg)
        self.assertIn('fill="#f8f5ec"', svg)
        self.assertIn('icon="assets/app-icon.ico"', spec)
        self.assertIn('("assets/app-icon.ico", "assets")', spec)
        self.assertIn('("desktop_host/gongkao_desktop_host.exe", ".")', spec)
        self.assertIn('("desktop_host/WebView2Loader.dll", ".")', spec)
        self.assertIn('collect_submodules("webview")', spec)
        self.assertIn('(\"evals\", \"evals\")', spec)
        self.assertIn('collect_submodules("langgraph")', spec)
        self.assertIn('collect_submodules("langchain_openai")', spec)
        self.assertIn('collect_submodules("langgraph.checkpoint.sqlite")', spec)

        host_source = (ROOT / "desktop_host" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn("LoadAppIcon(iconPath)", host_source)
        self.assertIn("Icon.ExtractAssociatedIcon(Application.ExecutablePath)", host_source)

    def test_question_list_does_not_expose_minimum_reference_filter(self):
        server_source = read_server_application(ROOT)
        self.assertNotIn('"min_refs": query.get("min_refs"', server_source)
        self.assertNotIn('name="min_refs"', server_source)
        self.assertNotIn("答案数至少", server_source)

    def test_statistics_navigation_and_module_trends_are_visible(self):
        server_source = read_server_application(ROOT)
        css = read_static_styles(ROOT)
        self.assertIn('href="/statistics"', server_source)
        self.assertIn("模块均分与趋势", server_source)
        self.assertIn("statistics-compact", server_source)
        self.assertIn("score_mode=best", server_source)
        self.assertIn("module-trend-chart", server_source)
        self.assertIn("module-score-table-wrap", server_source)
        self.assertIn(".module-score-card", css)
        self.assertIn(".statistics-compact .stat-metrics", css)
        self.assertIn("grid-template-columns: repeat(9, minmax(0, 1fr))", css)
        self.assertIn("max-height: 432px", css)
        self.assertIn(".grading-annotation", css)
        self.assertIn("总练习时长", server_source)
        self.assertNotIn("平均单题用时", server_source)

    def test_attempt_sidebar_panel_order(self):
        server_source = read_server_application(ROOT)
        sidebar_start = server_source.index('<aside class="grading-tools"')
        sidebar_end = server_source.index("</aside>", sidebar_start)
        sidebar = server_source[sidebar_start:sidebar_end]
        self.assertLess(sidebar.index("开始批改"), sidebar.index("AI 训练教练"))
        self.assertLess(sidebar.index("AI 训练教练"), sidebar.index("批改参考"))
        self.assertLess(sidebar.index("开始批改"), sidebar.index("Codex 手动模式"))
        self.assertIn("批改设置与高级工具", sidebar)
        self.assertIn('name="use_smart_grading"', sidebar)
        self.assertIn('name="use_deep_thinking"', sidebar)

    def test_card_grid_keeps_stable_card_widths(self):
        css = read_static_styles(ROOT)
        self.assertIn("repeat(auto-fill, minmax(310px, 1fr))", css)
        self.assertNotIn("repeat(auto-fit, minmax(310px, 1fr))", css)

    def test_sidebar_nav_turns_horizontal_in_compact_layout(self):
        css = read_static_styles(ROOT)
        compact_start = css.index("@media (max-width: 1120px)")
        compact_end = css.index("@media (max-width: 640px)", compact_start)
        compact_css = css[compact_start:compact_end]
        self.assertIn(".sidebar", compact_css)
        self.assertIn("border-bottom: 1px solid var(--line)", compact_css)
        self.assertIn(".nav", compact_css)
        self.assertIn("display: flex", compact_css)
        self.assertIn("overflow-x: auto", compact_css)
        self.assertIn("white-space: nowrap", compact_css)

    def test_desktop_sidebar_can_switch_to_horizontal_top_nav(self):
        shell_html = layout("侧边栏测试", "<p>内容</p>", "home")
        css = read_static_styles(ROOT)
        script = read_static_scripts(ROOT)
        self.assertIn("data-sidebar", shell_html)
        self.assertIn("data-sidebar-toggle", shell_html)
        self.assertNotIn("data-sidebar-resizer", shell_html)
        self.assertIn("icon-top-nav", shell_html)
        self.assertIn("icon-side-nav", shell_html)
        self.assertNotIn("&#8593;", shell_html)
        self.assertIn("--sidebar-width: 224px", css)
        self.assertIn("grid-template-columns: var(--sidebar-width) minmax(0, 1fr)", css)
        self.assertIn(".shell.sidebar-top", css)
        self.assertIn(".shell.sidebar-top .nav", css)
        self.assertIn(".sidebar-toggle-icon", css)
        self.assertIn("overflow-x: auto", css)
        self.assertNotIn(".shell.sidebar-compact", css)
        self.assertNotIn(".sidebar-resizer", css)
        self.assertNotIn("sidebar-resizing", css)
        self.assertIn("gongkao.sidebarMode", script)
        self.assertIn("shell.classList.toggle(\"sidebar-top\"", script)
        self.assertIn("viewState.persistentSet(storageKey, mode)", script)
        self.assertNotIn("icon.textContent", script)

    def test_release_ui_does_not_show_publish_placeholder(self):
        server_source = read_server_application(ROOT)
        css = read_static_styles(ROOT)
        script = read_static_scripts(ROOT)
        self.assertNotIn("发布预留", server_source)
        self.assertNotIn("后续可以封装成 exe 或桌面壳", server_source)
        self.assertNotIn("favorite_count", server_source)
        self.assertIn('<svg aria-hidden="true" viewBox="0 0 24 24">', server_source)
        self.assertIn("workflow-more", server_source)
        self.assertIn("data-workflow-menu", server_source)
        self.assertIn("data-preserve-sidebar", server_source)
        self.assertIn('form.hasAttribute("data-preserve-sidebar")', script)
        self.assertIn("grading-compare-panel", server_source)
        self.assertIn("initializeWorkflowMenus", script)
        self.assertIn('details:not([data-workflow-menu])', script)
        self.assertIn('details.dataset.defaultOpen === "1"', script)
        self.assertIn("data-default-side-ratio", server_source)
        self.assertIn("responsiveDefaultWidth", script)
        self.assertIn('.workflow-title-row h1', css)
        self.assertIn('font-family: "Microsoft YaHei UI"', css)
        self.assertNotIn("STZhongsong", css)
        self.assertNotIn("SimSun", css)
        grading_body = server_source.split('{workflow_header("grading"', 1)[1].split("self.send_html(", 1)[0]
        self.assertLess(grading_body.index('class="text-block answer-editor"'), grading_body.index("{report_section}"))
        self.assertLess(grading_body.index('class="text-block attempt-note-panel"'), grading_body.index("{report_section}"))
        self.assertGreater(grading_body.index("grading-compare-panel"), grading_body.index("grading-tools"))
        self.assertIn("grid-template-columns: repeat(3, 132px)", css)
        self.assertIn("workflow-stagebar", server_source)
        self.assertIn("width: max-content", css)
        self.assertIn("left: -46px", css)
        self.assertIn("width: 70px", css)
        self.assertIn("z-index: 100", css)
        self.assertIn("FILTER_RESTORE_BOOTSTRAP", server_source)
        self.assertIn('FILTER_RESTORE_BOOTSTRAP = ""', server_source)
        shell_html = layout("筛选恢复", "<p>内容</p>", "papers")
        self.assertNotIn("filters:papers", shell_html)
        self.assertIn("gongkao.viewState.v2:", server_source)
        self.assertIn("gradingPaneWidth.v2", server_source)
        self.assertIn("data-default-open", server_source)
        self.assertIn(">开始作答</a>", server_source)
        self.assertNotIn("开始 / 继续作答", server_source)
        self.assertIn("package-basis-note", server_source)
        self.assertIn(".reference-disclosure", css)
        self.assertIn(".grading-compare-panel .reference.tab-panel", css)
        self.assertIn(".grading-empty h3", css)
        self.assertNotIn("repeating-linear-gradient", css)
        self.assertIn(".home-dashboard", css)
        self.assertIn(".workflow-more-menu .favorite-form { display: block;", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn(".workflow-more-menu .favorite-button svg { flex: 0 0 18px;", css)
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("defaultRatio", script)

    def test_launcher_uses_native_webview_host_and_random_port(self):
        launcher_source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        host_source = (ROOT / "desktop_host" / "Program.cs").read_text(encoding="utf-8")
        host_build_source = (ROOT / "scripts" / "build_desktop_host.ps1").read_text(encoding="utf-8")
        self.assertIn("subprocess.run", launcher_source)
        self.assertIn("desktop_host_path", launcher_source)
        self.assertIn('START_PATH = "/home"', launcher_source)
        self.assertIn('HEALTH_PATH = "/health"', launcher_source)
        self.assertIn('APP_USER_MODEL_ID = "GongkaoShenlun.Desktop"', launcher_source)
        self.assertIn("SetCurrentProcessExplicitAppUserModelID", launcher_source)
        self.assertIn("CoreWebView2CreationProperties", host_source)
        self.assertIn("EnsureCoreWebView2Async", host_source)
        self.assertIn("IsStatusBarEnabled = false", host_source)
        self.assertIn("SetProcessDpiAwarenessContext", host_source)
        self.assertIn("AutoScaleMode.Dpi", host_source)
        self.assertIn("Application.Run(window)", host_source)
        self.assertIn("IsInPrivateModeEnabled = false", host_source)
        self.assertIn("GongkaoShenlun.Desktop", host_source)
        self.assertIn("/target:winexe", host_build_source)
        self.assertIn("/win32manifest:", host_build_source)
        manifest_source = (ROOT / "desktop_host" / "app.manifest").read_text(encoding="utf-8")
        self.assertIn("PerMonitorV2,PerMonitor", manifest_source)
        self.assertIn("wait_for_server_ready", launcher_source)
        self.assertIn("class StartupSplash", launcher_source)
        self.assertIn("SERVER_READY_TIMEOUT_SECONDS = 90", launcher_source)
        self.assertIn("CreateStartupPanel", host_source)
        self.assertIn("NavigationCompleted", host_source)
        self.assertIn("urllib.request.urlopen", launcher_source)
        self.assertIn("create_server(port=0)", launcher_source)
        self.assertIn('SERVER_PORT_FILE = "server-port.txt"', launcher_source)
        self.assertIn("preferred_server_port", launcher_source)
        self.assertIn("remember_server_port", launcher_source)
        self.assertIn("pywebview==6.2.1", requirements)
        self.assertNotIn("pythonnet==", requirements)
        self.assertNotIn("msedge.exe", launcher_source)
        self.assertNotIn("webbrowser", launcher_source)

    def test_launcher_opens_home_in_native_window(self):
        app = desktop_launcher.Launcher()
        app.url = "http://127.0.0.1:45678"
        app.start_url = f"{app.url}/home"
        fake_result = MagicMock(returncode=0)
        icon_path = ROOT / "assets" / "app-icon.ico"
        host_path = ROOT / "desktop_host" / "gongkao_desktop_host.exe"

        with (
            patch("launcher.user_data_dir", return_value=ROOT / ".test_tmp"),
            patch("launcher.app_icon_path", return_value=icon_path),
            patch("launcher.desktop_host_path", return_value=host_path),
            patch("launcher.log_path", return_value=ROOT / ".test_tmp" / "gongkao.log"),
            patch("pathlib.Path.mkdir"),
            patch("launcher.subprocess.run", return_value=fake_result) as run_mock,
        ):
            app.run_app_window()

        run_call = run_mock.call_args
        command = run_call.args[0]
        self.assertEqual(command[0], str(host_path))
        self.assertEqual(command[1], "http://127.0.0.1:45678/home")
        self.assertTrue(command[2].endswith("webview_profile"))
        self.assertEqual(command[3], str(icon_path))
        self.assertTrue(command[4].endswith("desktop_host.log"))
        self.assertFalse(run_call.kwargs["check"])

    def test_launcher_uses_embedded_host_icon_when_packaged_icon_is_missing(self):
        app = desktop_launcher.Launcher()
        app.start_url = "http://127.0.0.1:45678/home"
        missing_icon = ROOT / ".test_tmp" / "missing-app-icon.ico"
        host_path = ROOT / "desktop_host" / "gongkao_desktop_host.exe"

        with (
            patch("launcher.user_data_dir", return_value=ROOT / ".test_tmp"),
            patch("launcher.app_icon_path", return_value=missing_icon),
            patch("launcher.desktop_host_path", return_value=host_path),
            patch("launcher.log_path", return_value=ROOT / ".test_tmp" / "gongkao.log"),
            patch("pathlib.Path.mkdir"),
            patch("launcher.subprocess.run", return_value=MagicMock(returncode=0)) as run_mock,
        ):
            app.run_app_window()

        self.assertEqual(run_mock.call_args.args[0][3], "")

    def test_launcher_remembers_port_and_uses_health_check(self):
        app = desktop_launcher.Launcher()
        fake_server = MagicMock()
        fake_server.server_address = ("127.0.0.1", 43123)

        with (
            patch("launcher.create_server", return_value=fake_server) as create_server_mock,
            patch.object(app, "preferred_server_port", return_value=0),
            patch.object(app, "remember_server_port") as remember_port_mock,
            patch.object(app, "wait_for_server_ready") as ready_mock,
        ):
            app.start_server()
            if app.server_thread:
                app.server_thread.join(timeout=1)
            app.stop()

        create_server_mock.assert_called_once_with(port=0)
        remember_port_mock.assert_called_once_with(43123)
        ready_mock.assert_called_once_with()
        self.assertEqual(app.start_url, "http://127.0.0.1:43123/home")
        fake_server.shutdown.assert_called_once_with()
        fake_server.server_close.assert_called_once_with()

    def test_launcher_readiness_probe_skips_database_backed_home_page(self):
        app = desktop_launcher.Launcher()
        app.url = "http://127.0.0.1:43123"
        app.start_url = f"{app.url}/home"
        response = MagicMock(status=200)
        response_context = MagicMock()
        response_context.__enter__.return_value = response

        with patch("launcher.urllib.request.urlopen", return_value=response_context) as open_mock:
            app.wait_for_server_ready()

        open_mock.assert_called_once_with("http://127.0.0.1:43123/health", timeout=2.0)

    def test_launcher_reports_missing_webview2_runtime(self):
        with patch("launcher.webview2_runtime_version", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "WebView2 Runtime"):
                desktop_launcher.ensure_webview2_runtime()


if __name__ == "__main__":
    unittest.main()

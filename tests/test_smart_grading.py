import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gongkao.db import connect, init_db, prepare_user_database
from gongkao.grading_pipeline.orchestration import (
    QUESTION_TYPE_PROFILES,
    apply_report_feedback,
    build_grading_prompt,
    build_rubric_prompt,
    compact_reference_consensus,
    create_grading_job,
    grading_job_payload,
    question_display_max_score,
    question_score_is_estimated,
    render_grading_report,
    retrieve_grading_evidence,
    run_grading_job,
    validate_grading_result,
    validate_rubric,
)
from gongkao.grading_pipeline.rubric import build_fenbi_tree_rubric, fenbi_tree_available
from gongkao.statistics import build_training_statistics
from tests.asset_bundle import (
    read_server_application,
    read_static_scripts,
    read_static_styles,
)

ROOT = Path(__file__).resolve().parents[1]


class SmartGradingTest(unittest.TestCase):
    def make_database(self, directory):
        path = Path(directory) / "smart.sqlite3"
        init_db(path)
        with connect(path) as conn:
            paper_id = conn.execute(
                """
                INSERT INTO papers (paper_code, paper_name, exam_type, year, region)
                VALUES ('P-SMART', '智能批改测试卷', '国考', 2026, '全国')
                """
            ).lastrowid
            question_id = conn.execute(
                """
                INSERT INTO questions (
                    question_code, paper_id, paper_name, exam_type, year, region,
                    question_type, title, prompt, materials, requirements, word_limit
                ) VALUES (
                    'Q-SMART', ?, '智能批改测试卷', '国考', 2026, '全国',
                    '归纳概括', '概括变化', '概括数字服务带来的变化。', '',
                    '全面、准确，分点作答。', '250字以内'
                )
                """,
                (paper_id,),
            ).lastrowid
            conn.execute(
                "INSERT INTO paper_materials (paper_id, material_number, content) VALUES (?, 1, ?)",
                (paper_id, "数字平台上线后，村民办事更加方便，政策查询也更加及时。"),
            )
            reference_ids = [
                conn.execute(
                    """
                    INSERT INTO reference_answers (
                        question_id, organization, canonical_organization, answer_text
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (question_id, org, org, answer),
                ).lastrowid
                for org, answer in (
                    ("机构甲", "数字平台提高办事便利度，政策信息获取更及时。"),
                    ("机构乙", "群众办事更加方便，也能及时查询政策。"),
                )
            ]
            attempt_id = conn.execute(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, ?, 18)",
                (question_id, "数字平台让村民办事更加方便，政策查询更加及时。"),
            ).lastrowid
        return path, question_id, attempt_id, reference_ids

    def fake_chat(self, reference_ids, calls):
        def chat(settings, prompt, request_options=None):
            calls.append(prompt)
            chat.request_options.append(request_options or {})
            if "<rubric_json>" in prompt:
                payload = {
                    "question_id": 1,
                    "task_constraints": {"object": "数字服务变化", "required_structure": ["分点"], "format_rules": []},
                    "points": [
                        {
                            "point_key": "point-convenience", "label": "办事便利",
                            "canonical_expression": "提高村民办事便利度", "aliases": ["办事方便"],
                            "tier": "core", "importance": "critical", "suggested_weight": 45,
                            "weight_reason": "直接回应主要变化。", "required_for_full_score": True,
                            "material_evidence": [{"material_number": 1, "quote": "村民办事更加方便"}],
                            "reference_ids": reference_ids, "confidence": 0.92,
                        },
                        {
                            "point_key": "point-timely", "label": "查询及时",
                            "canonical_expression": "政策查询更加及时", "aliases": [],
                            "tier": "core", "importance": "major", "suggested_weight": 25,
                            "weight_reason": "属于另一项重要变化。", "required_for_full_score": True,
                            "material_evidence": [{"material_number": 1, "quote": "政策查询也更加及时"}],
                            "reference_ids": reference_ids, "confidence": 0.9,
                        },
                    ],
                    "conflicts": [],
                }
                text = f"<rubric_json>{json.dumps(payload, ensure_ascii=False)}</rubric_json>"
                return text, text
            if "<smart_grading_json>" in prompt:
                rubric_payload = None
                if "系统校验后的评分基准：\n" in prompt:
                    cached = json.loads(
                        prompt.split("系统校验后的评分基准：\n", 1)[1].split(
                            "\n\n本题已选择", 1
                        )[0]
                    )
                    point_keys = [point["point_key"] for point in cached["points"]]
                else:
                    rubric_payload = {
                    "question_id": 1,
                    "task_constraints": {"object": "数字服务变化", "required_structure": ["分点"], "format_rules": []},
                    "points": [
                        {
                            "point_key": "point-convenience",
                            "label": "办事便利",
                            "canonical_expression": "提高村民办事便利度",
                            "aliases": ["办事方便"],
                            "tier": "core",
                            "importance": "critical",
                            "suggested_weight": 45,
                            "weight_reason": "直接回应主要变化。",
                            "required_for_full_score": True,
                            "material_evidence": [{"material_number": 1, "quote": "村民办事更加方便"}],
                            "reference_ids": reference_ids,
                            "confidence": 0.92,
                        },
                        {
                            "point_key": "point-timely",
                            "label": "查询及时",
                            "canonical_expression": "政策查询更加及时",
                            "aliases": [],
                            "tier": "core",
                            "importance": "major",
                            "suggested_weight": 25,
                            "weight_reason": "属于另一项重要变化。",
                            "required_for_full_score": True,
                            "material_evidence": [{"material_number": 1, "quote": "政策查询也更加及时"}],
                            "reference_ids": reference_ids,
                            "confidence": 0.9,
                        },
                    ],
                    "conflicts": [],
                    }
                    point_keys = ["point-convenience", "point-timely"]
                evaluation = {
                    "point_matches": [
                        {"point_key": point_keys[0], "status": "hit", "coverage_ratio": 1, "answer_quote": "办事更加方便", "reason": "原文命中"},
                        {"point_key": point_keys[1], "status": "hit", "coverage_ratio": 1, "answer_quote": "政策查询更加及时", "reason": "原文命中"},
                    ],
                    "dimension_scores": [
                        {"dimension": "content", "score": 65, "reason": "两项核心变化均准确覆盖。"},
                        {"dimension": "structure", "score": 15, "reason": "层次清楚。"},
                        {"dimension": "expression", "score": 10, "reason": "表达准确。"},
                        {"dimension": "format", "score": 5, "reason": "符合要求。"},
                    ],
                    "holistic_adjustment_reason": "",
                    "annotations": [],
                    "reference_fusion": "两家机构共同支持两个要点。",
                    "material_reading": ["数字平台上线 -> 办事便利、查询及时"],
                    "optimization_suggestions": [],
                    "personalized_findings": [],
                    "overall_summary": "核心采分点均已命中。",
                    "revised_answer": "数字平台提高村民办事便利度，政策查询更加及时。",
                }
                payload = {"evaluation": evaluation}
                if rubric_payload is not None:
                    payload["rubric"] = rubric_payload
                text = f"<smart_grading_json>{json.dumps(payload, ensure_ascii=False)}</smart_grading_json>"
                return text, text
            raise AssertionError("出现非预期模型调用")

        chat.request_options = []
        return chat

    def test_first_run_builds_rubric_and_second_run_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            chat = self.fake_chat(reference_ids, calls)
            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                self.assertEqual(settings["grading_mode"], "basic")
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], chat)
            self.assertIsNotNone(report_id)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("基础模式正式批改", calls)
            self.assertIn("机构参考答案样本提示", calls[0])
            self.assertIn("需结合题干与材料核验共同采分点", calls[0])
            self.assertEqual([item.get("thinking") for item in chat.request_options], ["disabled", "disabled"])
            self.assertEqual(
                [item.get("response_format") for item in chat.request_options],
                [{"type": "json_object"}, {"type": "json_object"}],
            )

            with connect(path) as conn:
                first_validation = json.loads(conn.execute(
                    "SELECT validation_json FROM grading_report_contexts WHERE report_id = ?",
                    (report_id,),
                ).fetchone()[0])
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                second_job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                second_report_id = run_grading_job(path, second_job["id"], chat)
            self.assertIsNotNone(second_report_id)
            self.assertEqual(len(calls), 3)
            with connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM grading_rubrics").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT status FROM grading_jobs WHERE id = ?", (second_job["id"],)).fetchone()[0], "completed")
                self.assertEqual(conn.execute("SELECT api_call_count FROM grading_report_contexts WHERE report_id = ?", (second_report_id,)).fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM grading_reports WHERE attempt_id = ?", (attempt_id,)).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM grading_reports WHERE id = ?", (report_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT report_id FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()[0]
                )
                second_validation = json.loads(conn.execute(
                    "SELECT validation_json FROM grading_report_contexts WHERE report_id = ?",
                    (second_report_id,),
                ).fetchone()[0])
            self.assertEqual(
                [item["purpose"] for item in first_validation["api_calls"]],
                ["rubric_generation", "grading"],
            )
            self.assertEqual(
                [item["purpose"] for item in second_validation["api_calls"]],
                ["grading"],
            )

    def test_consensus_preprocessing_never_starts_dense_model_and_bounds_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _, reference_ids = self.make_database(directory)
            with connect(path) as conn:
                references = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM reference_answers WHERE id IN ({','.join('?' for _ in reference_ids)})",
                        reference_ids,
                    )
                ]
                materials = [
                    {
                        "material_number": number,
                        "content": "。".join(
                            f"第{number}则材料的第{index}个完整事实描述"
                            for index in range(120)
                        ),
                    }
                    for number in range(1, 7)
                ]
                with patch(
                    "gongkao.agent_modules._load_dense_model",
                    side_effect=AssertionError("批改预处理不应启动稠密向量模型"),
                ):
                    consensus = compact_reference_consensus(conn, references, materials)

            self.assertEqual(consensus["embedding_model"], "feature-hash-v1")
            self.assertEqual(consensus["preprocessing_mode"], "lightweight")
            self.assertFalse(consensus["degraded"])
            self.assertLessEqual(consensus["material_clause_count"], 240)
            self.assertGreater(consensus["source_clause_count"], 0)

    def test_grading_evidence_uses_snapshot_without_dense_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            path, question_id, attempt_id, _ = self.make_database(directory)
            with connect(path) as conn:
                conn.execute(
                    "UPDATE agent_context_index_state SET dirty = 1, full_rebuild = 0 WHERE id = 1"
                )
                question = dict(
                    conn.execute(
                        "SELECT * FROM questions WHERE id = ?",
                        (question_id,),
                    ).fetchone()
                )
                attempt = dict(
                    conn.execute(
                        "SELECT * FROM attempts WHERE id = ?",
                        (attempt_id,),
                    ).fetchone()
                )
                with patch(
                    "gongkao.agent_modules.ensure_agent_context_index",
                    side_effect=AssertionError("正式批改不应同步历史索引"),
                ), patch(
                    "gongkao.agent_modules._load_dense_model",
                    side_effect=AssertionError("正式批改不应启动稠密向量模型"),
                ):
                    evidence, meta = retrieve_grading_evidence(
                        conn,
                        question,
                        attempt,
                        {"points": [], "selected_references": []},
                        {"analogies": True, "knowledge": True, "history": True},
                    )
                state = conn.execute(
                    "SELECT dirty FROM agent_context_index_state WHERE id = 1"
                ).fetchone()

            self.assertTrue(evidence)
            self.assertEqual(meta["retrieval_mode"], "lightweight_snapshot")
            self.assertTrue(meta["index_dirty"])
            self.assertEqual(state["dirty"], 1)

    def test_deep_thinking_can_be_disabled_for_formal_grading_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            chat = self.fake_chat(reference_ids, calls)
            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(
                    conn,
                    attempt,
                    settings,
                    reference_ids,
                    "",
                    {"deep_thinking": False},
                )
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], chat)
            self.assertIsNotNone(report_id)
            self.assertEqual([item.get("thinking") for item in chat.request_options], ["disabled", "disabled"])
            with connect(path) as conn:
                options = json.loads(conn.execute("SELECT options_json FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()[0])
                validation = json.loads(conn.execute("SELECT validation_json FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()[0])
            self.assertFalse(options["deep_thinking"])
            self.assertFalse(validation["deep_thinking"])

    def test_ai_supplied_revised_answer_is_ignored_without_repair_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            base_chat = self.fake_chat(reference_ids, calls)

            def overflowing_chat(settings, prompt, request_options=None):
                if "上一版超限报告如下" in prompt:
                    calls.append(prompt)
                    return f"<revised_answer>{'乙' * 250}</revised_answer>", "repair-raw"
                text, raw = base_chat(settings, prompt, request_options)
                if "<smart_grading_json>" in text:
                    payload = json.loads(
                        text.split("<smart_grading_json>", 1)[1].split(
                            "</smart_grading_json>", 1
                        )[0]
                    )
                    payload["evaluation"]["revised_answer"] = "甲" * 250
                    text = f"<smart_grading_json>{json.dumps(payload, ensure_ascii=False)}</smart_grading_json>"
                    raw = text
                return text, raw

            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], overflowing_chat)

            self.assertIsNotNone(report_id)
            self.assertEqual(len(calls), 2)
            with connect(path) as conn:
                failed_job = conn.execute("SELECT * FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()
                saved_report = conn.execute(
                    "SELECT * FROM grading_reports WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                validation = json.loads(conn.execute(
                    "SELECT validation_json FROM grading_report_contexts WHERE report_id = ?",
                    (report_id,),
                ).fetchone()[0])
            self.assertEqual(failed_job["status"], "completed")
            self.assertNotIn("超出字数限制", failed_job["message"])
            self.assertEqual(failed_job["report_id"], report_id)
            self.assertIsNotNone(saved_report)
            self.assertEqual(saved_report["status"], "ok")
            self.assertNotIn("甲" * 250, saved_report["report_text"])
            self.assertNotIn("乙" * 250, saved_report["report_text"])
            self.assertIn("机构甲参考答案", saved_report["report_text"])
            self.assertFalse(validation["word_count_status"]["over_limit"])
            self.assertEqual(
                [item["purpose"] for item in validation["api_calls"]],
                ["rubric_generation", "grading"],
            )
            payload = grading_job_payload(failed_job)
            self.assertFalse(payload["preview_available"])
            self.assertEqual(payload["report_id"], report_id)

    def test_failed_preview_polling_forces_server_render_reload(self):
        script = read_static_scripts(ROOT)
        preview_branch = script.split("if (payload.preview_available) {", 1)[1].split(
            "if (gradeButton)", 1
        )[0]
        self.assertIn("navigatePartial(target, { replace: true, silent: true });", preview_branch)

    def test_combined_grading_maps_declared_source_point_key_without_guessing_position(self):
        rubric = {
            "question_type": "归纳概括",
            "points": [{
                "point_key": "point-stable",
                "source_point_key": "模型原始键",
                "weight": 70,
                "importance": "critical",
                "label": "核心要点",
            }],
        }
        result = validate_grading_result(
            {
                "point_matches": [{
                    "point_key": "模型原始键",
                    "status": "hit",
                    "coverage_ratio": 1,
                    "answer_quote": "核心原句",
                    "reason": "已覆盖",
                }],
                "dimension_scores": [
                    {"dimension": "content", "score": 70, "reason": "完整"},
                    {"dimension": "structure", "score": 15, "reason": "清楚"},
                    {"dimension": "expression", "score": 10, "reason": "准确"},
                    {"dimension": "format", "score": 5, "reason": "规范"},
                ],
            },
            rubric,
            "核心原句",
            [],
        )
        self.assertEqual(result["point_matches"][0]["status"], "hit")
        self.assertEqual(result["point_matches"][0]["point_key"], "point-stable")

    def test_dimension_scores_are_never_guessed_and_rescaled(self):
        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 20,
            "points": [{"point_key": "p1", "weight": 70, "label": "要点"}],
        }
        result = validate_grading_result(
            {
                "point_matches": [{"point_key": "p1", "status": "miss", "answer_quote": ""}],
                "dimension_scores": [
                    {"dimension": "content", "score": 0, "reason": "未覆盖"},
                    {"dimension": "structure", "score": 4, "reason": "结构"},
                    {"dimension": "expression", "score": 4, "reason": "表达"},
                    {"dimension": "format", "score": 2, "reason": "格式"},
                ],
            },
            rubric,
            "无关内容",
            [],
        )
        scores = {item["dimension"]: item["score"] for item in result["dimension_scores"]}
        self.assertEqual(scores["structure"], 4)
        self.assertEqual(scores["expression"], 4)
        self.assertEqual(scores["format"], 2)

    def test_invalid_quotes_cannot_receive_content_score(self):
        rubric = {
            "question_type": "归纳概括",
            "points": [{"point_key": "p1", "weight": 70, "label": "要点"}],
            "criteria": [],
        }
        with self.assertRaisesRegex(ValueError, "得分证据无法在用户原文中定位"):
            validate_grading_result(
                {
                    "point_matches": [{"point_key": "p1", "status": "hit", "answer_quote": "并不存在的原句"}],
                    "dimension_scores": [
                        {"dimension": "content", "score": 0, "reason": "没有有效证据"},
                        {"dimension": "structure", "score": 0, "reason": "未评分"},
                        {"dimension": "expression", "score": 0, "reason": "未评分"},
                        {"dimension": "format", "score": 0, "reason": "未评分"},
                    ],
                },
                rubric,
                "用户实际答案",
                [],
            )

    def test_partial_matches_use_teacher_completion_bands(self):
        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 10,
            "points": [{"point_key": "p1", "weight": 70, "label": "要点"}],
            "criteria": [],
        }
        result = validate_grading_result(
            {
                "point_matches": [
                    {
                        "point_key": "p1",
                        "status": "partial",
                        "coverage_ratio": 0.75,
                        "answer_quote": "已覆盖三个核心要素",
                        "reason": "四个必要语义中覆盖三个。",
                    }
                ],
                "dimension_scores": [
                    {"dimension": "content", "score": 50, "reason": "核心语义大部分覆盖"},
                    {"dimension": "structure", "score": 0, "reason": "未评分"},
                    {"dimension": "expression", "score": 0, "reason": "未评分"},
                    {"dimension": "format", "score": 0, "reason": "未评分"},
                ],
            },
            rubric,
            "已覆盖三个核心要素",
            [],
        )
        match = result["point_matches"][0]
        self.assertEqual(match["coverage_ratio"], 0.75)
        self.assertEqual(match["score_level"], "mostly")
        self.assertEqual(match["awarded_score"], 52.5)
        self.assertEqual(result["weighted_coverage_score"], 52.5)
        self.assertEqual(result["display_score"], 5.0)
        self.assertIn("大部分得分", render_grading_report(result, rubric, []))

    def test_rubric_makes_non_requested_overall_effect_optional(self):
        materials = [{"material_number": 1, "content": "建立夜话机制，密切干群关系，形成良好示范效应。"}]
        references = [
            {"id": 1, "organization": "甲", "answer_text": "建立夜话机制，密切干群关系。"},
            {"id": 2, "organization": "乙", "answer_text": "建立夜话机制，形成示范效应。"},
        ]
        question = {
            "id": 1,
            "question_type": "归纳概括",
            "prompt": "请概括当地文明实践活动的主要做法。",
            "requirements": "全面、准确，350字以内。",
            "word_limit": "350字以内",
        }
        raw = {
            "points": [
                {
                    "label": "夜话机制",
                    "canonical_expression": "建立夜话机制，密切干群关系",
                    "tier": "core",
                    "required_for_full_score": True,
                    "required_elements": ["建立夜话机制", "密切干群关系"],
                    "minimum_expression": "建立夜话机制，密切干群关系",
                    "reference_ids": [1, 2],
                    "material_evidence": [{"material_number": 1, "quote": "建立夜话机制，密切干群关系"}],
                },
                {
                    "label": "整体成效",
                    "canonical_expression": "形成良好示范效应",
                    "tier": "core",
                    "required_for_full_score": True,
                    "reference_ids": [1, 2],
                    "material_evidence": [{"material_number": 1, "quote": "形成良好示范效应"}],
                },
            ]
        }
        rubric = validate_rubric(raw, question, materials, references)
        points = {point["label"]: point for point in rubric["points"]}
        self.assertEqual(points["夜话机制"]["score_role"], "required")
        self.assertGreater(points["夜话机制"]["weight"], 0)
        self.assertEqual(points["整体成效"]["score_role"], "supplementary")
        self.assertEqual(points["整体成效"]["weight"], 0)

    def test_smart_prompts_include_grid_rules_and_budget_aware_scoring(self):
        question = {
            "id": 1,
            "question_type": "归纳概括",
            "prompt": "概括做法",
            "requirements": "350字以内",
            "word_limit": "350字以内",
        }
        rubric_prompt = build_rubric_prompt(question, [], [], {})
        grading_prompt = build_grading_prompt(
            question,
            [],
            {"id": 1, "answer_text": "作答"},
            {"points": [], "criteria": [], "word_budget": {}},
            [],
        )
        for prompt in (rubric_prompt, grading_prompt):
            self.assertIn("连续英文、半角数字每2个字符占1格", prompt)
            self.assertIn("手动换行会立即结算当前行", prompt)
        self.assertIn("required_for_full_score", rubric_prompt)
        self.assertIn("optional_details", rubric_prompt)
        self.assertIn("coverage_ratio", grading_prompt)
        self.assertIn("mostly=0.75、half=0.5 或 slight=0.25", grading_prompt)
        self.assertIn("0.5只是分档刻度", grading_prompt)
        self.assertIn("禁止生成、改写、压缩或润色任何完整答案", grading_prompt)
        self.assertNotIn('"revised_answer"', grading_prompt)
        self.assertIn("符合真实考场阅卷强度的“得分制”", grading_prompt)
        self.assertIn("普通“写到了”不能进入此档", grading_prompt)
        self.assertIn('"max_score": 70.0', grading_prompt)

    def test_essay_prompt_requires_band_first_scoring_contract(self):
        prompt = build_grading_prompt(
            {
                "id": 2,
                "question_type": "综合写作",
                "prompt": "请围绕材料主题写一篇文章。",
                "requirements": "观点明确，论证充分。",
                "word_limit": "1000字左右",
            },
            [{"material_number": 1, "content": "材料主题与案例。"}],
            {"id": 2, "answer_text": "一篇待评作文。"},
            {
                "points": [],
                "dimensions": [
                    {"dimension": "content", "weight": 30},
                    {"dimension": "structure", "weight": 20},
                    {"dimension": "reasoning", "weight": 20},
                    {"dimension": "material", "weight": 15},
                    {"dimension": "expression", "weight": 15},
                ],
            },
            [],
        )
        self.assertIn('"overall_band": "A|B|C|D|E"', prompt)
        self.assertIn('"high_band_evidence"', prompt)
        self.assertIn("先定档，后分维度", prompt)
        self.assertIn("普通模板化、仅语句流畅", prompt)
        self.assertIn('"dimension": "material", "max_score": 15.0', prompt)

    def test_selected_reference_full_content_is_kept_in_both_prompts(self):
        question = {
            "id": 7,
            "question_code": "Q-7",
            "question_type": "综合分析",
            "title": "分析原因",
            "prompt": "分析问题产生的原因。（15分）",
            "requirements": "观点准确。",
            "word_limit": "250字左右",
        }
        materials = [{"material_number": 1, "title": "", "content": "考核机制不科学。"}]
        references = [{
            "id": 19,
            "organization": "机构甲",
            "canonical_organization": "机构甲",
            "answer_text": "完整机构答案正文。",
            "scoring_points": "机构采分点全文。",
            "notes": "机构答案备注。",
        }]
        rubric_prompt = build_rubric_prompt(question, materials, references, {"clusters": []})
        grading_prompt = build_grading_prompt(
            question,
            materials,
            {"answer_text": "我的答案。"},
            {"points": [], "criteria": []},
            [],
            references=references,
        )
        for prompt in (rubric_prompt, grading_prompt):
            self.assertIn("完整机构答案正文。", prompt)
            self.assertIn("机构采分点全文。", prompt)
            self.assertIn("机构答案备注。", prompt)
            self.assertIn("共 1 份", prompt)
            self.assertIn("唯一参考答案规则", prompt)
            self.assertIn("内容采分点只能从这份答案", prompt)
            self.assertIn("核心动作、对象或效果同义即应命中全分", prompt)

    def test_internal_score_is_rendered_on_question_point_scale(self):
        self.assertEqual(question_display_max_score({"prompt": "分析原因。（15分）"}), 15)
        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 15,
            "selected_reference_count": 1,
            "selected_references": [{"reference_id": 9, "organization": "机构甲"}],
            "points": [{"point_key": "p1", "weight": 70, "label": "要点"}],
            "criteria": [],
        }
        result = validate_grading_result(
            {
                "point_matches": [{"point_key": "p1", "status": "hit", "answer_quote": "有效原句"}],
                "dimension_scores": [
                    {"dimension": "content", "score": 70, "reason": "内容完整"},
                    {"dimension": "structure", "score": 0, "reason": "未评分"},
                    {"dimension": "expression", "score": 0, "reason": "未评分"},
                    {"dimension": "format", "score": 0, "reason": "未评分"},
                ],
                "reference_fusion": "本题无额外参考答案。",
            },
            rubric,
            "有效原句",
            [],
        )
        report = render_grading_report(result, rubric, [])
        self.assertEqual(result["score"], 70.0)
        self.assertEqual(result["display_score"], 10.5)
        self.assertIn("总分：10.5/15", report)
        self.assertIn("10.5/10.5", report)
        self.assertNotIn("## 维度评分", report)
        self.assertIn("参考答案使用说明", report)
        self.assertIn("本题仅有 1 份粉笔参考答案（机构甲）", report)
        self.assertIn("材料仅用于核验明显错误，不新增或扩写扣分条件", report)
        self.assertNotIn("参考答案融合说明", report)
        self.assertNotIn("共性核心点", result["reference_fusion"])
        self.assertNotIn("无额外参考答案", report)

    def test_cross_question_retrieval_failure_does_not_block_current_question_grading(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                side_effect=RuntimeError("索引不可用"),
            ):
                report_id = run_grading_job(path, job["id"], self.fake_chat(reference_ids, calls))
            self.assertIsNotNone(report_id)
            with connect(path) as conn:
                validation = json.loads(conn.execute("SELECT validation_json FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()[0])
            self.assertTrue(validation["history_meta"]["retrieval_degraded"])

    def test_grading_job_persists_click_time_answer_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            chat = self.fake_chat(reference_ids, calls)
            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                original_answer = attempt["answer_text"]
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
                conn.execute(
                    "UPDATE attempts SET answer_text = '批改开始后编辑的新答案' WHERE id = ?",
                    (attempt_id,),
                )
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], chat)
            with connect(path) as conn:
                result = json.loads(
                    conn.execute(
                        "SELECT result_json FROM grading_report_contexts WHERE report_id = ?",
                        (report_id,),
                    ).fetchone()[0]
                )
            self.assertEqual(result["answer_snapshot"], original_answer)
            self.assertIn(original_answer, calls[1])
            self.assertNotIn("批改开始后编辑的新答案", calls[1])

    def test_feedback_marks_holistic_score_stale_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []
            chat = self.fake_chat(reference_ids, calls)
            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], chat)
            with connect(path) as conn:
                result = json.loads(conn.execute("SELECT result_json FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()[0])
                point_key = result["point_matches"][0]["point_key"]
                before = result["score"]
                updated = apply_report_feedback(conn, report_id, point_key, "miss", "", "人工确认未命中")
                self.assertEqual(updated["score"], before)
                self.assertEqual(updated["score_status"], "stale")
                report_text = conn.execute(
                    "SELECT report_text FROM grading_reports WHERE id = ?", (report_id,)
                ).fetchone()[0]
                self.assertIn("原评分（已过期）", report_text)
                self.assertEqual(build_training_statistics(conn)["recognized_scores"], 0)
            self.assertEqual(len(calls), 2)

    def test_question_type_profiles_match_holistic_scoring_plan(self):
        self.assertEqual(QUESTION_TYPE_PROFILES["归纳概括"], {"content": 70, "structure": 15, "expression": 10, "format": 5})
        self.assertEqual(QUESTION_TYPE_PROFILES["综合分析"], {"content": 55, "reasoning": 25, "structure": 10, "expression": 10})
        self.assertEqual(QUESTION_TYPE_PROFILES["提出对策"], {"content": 60, "feasibility": 20, "structure": 10, "expression": 10})
        self.assertEqual(QUESTION_TYPE_PROFILES["公文写作"], {"content": 50, "format": 20, "structure": 20, "expression": 10})
        self.assertEqual(
            QUESTION_TYPE_PROFILES["综合写作"],
            {"content": 30, "structure": 20, "reasoning": 20, "material": 15, "expression": 15},
        )
        self.assertTrue(all(sum(profile.values()) == 100 for profile in QUESTION_TYPE_PROFILES.values()))

    def test_dynamic_point_weights_are_preserved_and_normalized(self):
        rubric = validate_rubric(
            {
                "points": [
                    {
                        "point_key": "critical-point",
                        "label": "核心点",
                        "canonical_expression": "建立夜话机制",
                        "tier": "core",
                        "importance": "critical",
                        "suggested_weight": 50,
                        "weight_reason": "直接完成主要任务",
                        "material_evidence": [{"material_number": 1, "quote": "建立夜话机制"}],
                    },
                    {
                        "point_key": "major-point",
                        "label": "重要点",
                        "canonical_expression": "密切干群关系",
                        "tier": "material_core",
                        "importance": "major",
                        "suggested_weight": 20,
                        "weight_reason": "补足关键效果",
                        "material_evidence": [{"material_number": 1, "quote": "密切干群关系"}],
                    },
                ]
            },
            {
                "id": 1,
                "question_type": "归纳概括",
                "prompt": "概括主要做法。（10分）",
                "requirements": "全面、准确。",
                "word_limit": "200字以内",
            },
            [{"material_number": 1, "content": "建立夜话机制，密切干群关系。"}],
            [],
        )
        weights = {point["point_key"]: point["weight"] for point in rubric["points"]}
        self.assertEqual(weights, {"critical-point": 50.0, "major-point": 20.0})
        self.assertEqual(sum(weights.values()), 70)

    def test_point_without_material_evidence_cannot_become_a_scoring_anchor(self):
        with self.assertRaisesRegex(ValueError, "有效采分点"):
            validate_rubric(
                {
                    "points": [{
                        "point_key": "invented",
                        "label": "虚构要点",
                        "canonical_expression": "材料完全没有出现的虚构结论",
                        "tier": "core",
                        "importance": "critical",
                        "suggested_weight": 70,
                        "material_evidence": [{"material_number": 1, "quote": "并不存在的材料原句"}],
                    }]
                },
                {
                    "id": 1,
                    "question_type": "归纳概括",
                    "prompt": "概括主要做法。",
                    "requirements": "全面、准确。",
                    "word_limit": "200字以内",
                },
                [{"material_number": 1, "content": "材料只写了建立夜话机制。"}],
                [],
            )

    def test_point_based_content_score_comes_from_resolved_required_coverage(self):
        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 10,
            "points": [{"point_key": "p1", "weight": 70, "importance": "critical", "label": "要点"}],
        }
        raw = {
            "point_matches": [{"point_key": "p1", "status": "hit", "answer_quote": "有效原句"}],
            "dimension_scores": [
                {"dimension": "content", "score": 50, "reason": "整体内容仍有不足"},
                {"dimension": "structure", "score": 15, "reason": "结构清楚"},
                {"dimension": "expression", "score": 10, "reason": "表达准确"},
                {"dimension": "format", "score": 5, "reason": "格式正确"},
            ],
        }
        result = validate_grading_result(raw, rubric, "有效原句", [])
        self.assertEqual(result["content_score"], 70.0)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["display_score"], 10.0)
        self.assertEqual(result["score_calibration"]["adjustment"], 0.0)
        self.assertEqual(result["score_status"], "valid")

    def test_dimension_prompt_starts_from_evidence_not_full_marks(self):
        from gongkao.grading_pipeline.evidence import DIMENSION_SCORING_GUIDANCE, _dimension_score_template

        template = _dimension_score_template([{"dimension": "content", "weight": 70}])
        self.assertEqual(template[0]["score"], 0)
        self.assertIn("不是从满分起步", DIMENSION_SCORING_GUIDANCE)
        self.assertIn("90%—100%仅用于", DIMENSION_SCORING_GUIDANCE)

    def test_miss_reason_cannot_claim_the_point_was_fully_covered(self):
        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 10,
            "points": [{"point_key": "p1", "weight": 70, "importance": "critical", "label": "依靠力量"}],
        }
        raw = {
            "point_matches": [{
                "point_key": "p1",
                "status": "miss",
                "answer_quote": "",
                "reason": "完整覆盖了依靠力量的核心要素。",
            }],
            "dimension_scores": [
                {"dimension": "content", "score": 0, "reason": "未覆盖"},
                {"dimension": "structure", "score": 10, "reason": "结构一般"},
                {"dimension": "expression", "score": 8, "reason": "表达尚可"},
                {"dimension": "format", "score": 4, "reason": "格式基本正确"},
            ],
        }
        result = validate_grading_result(raw, rubric, "答案没有写依靠力量", [])
        reason = result["point_matches"][0]["reason"]
        self.assertIn("未提供", reason)
        self.assertNotIn("完整覆盖", reason)

    def test_missing_question_max_uses_estimated_hundred_point_scale(self):
        self.assertEqual(question_display_max_score({"prompt": "概括主要做法。"}), 100)
        self.assertTrue(question_score_is_estimated({"prompt": "概括主要做法。"}))
        self.assertFalse(question_score_is_estimated({"prompt": "分析原因。（100分）"}))

    def test_validation_failure_never_uses_more_than_three_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []

            valid_rubric_chat = self.fake_chat(reference_ids, [])

            def invalid_chat(settings, prompt, request_options=None):
                calls.append(prompt)
                if "<rubric_json>" in prompt:
                    return valid_rubric_chat(settings, prompt, request_options)
                text = "<smart_grading_json>{\"evaluation\": {}}</smart_grading_json>"
                return text, text

            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], invalid_chat)
            self.assertIsNone(report_id)
            self.assertEqual(len(calls), 3)
            with connect(path) as conn:
                self.assertEqual(
                    conn.execute("SELECT status FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()[0],
                    "failed",
                )

    def test_five_question_types_rank_strong_medium_and_weak_answers(self):
        evaluated = 0
        for question_type, profile in QUESTION_TYPE_PROFILES.items():
            rubric = {
                "question_type": question_type,
                "display_max_score": 20,
                "points": [{
                    "point_key": "core",
                    "weight": profile["content"],
                    "importance": "critical",
                    "label": "核心任务",
                }],
            }
            scores = []
            for label, coverage, factor, answer in (
                ("strong", 1.0, 1.0, "核心原句"),
                ("medium", 0.5, 0.6, "核心原句"),
                ("weak", 0.0, 0.2, "无关内容"),
            ):
                dimensions = []
                for dimension, maximum in profile.items():
                    if dimension == "content":
                        value = maximum * (1.0 if label == "strong" else (0.55 if label == "medium" else 0.1))
                    else:
                        value = maximum * factor
                    dimensions.append({"dimension": dimension, "score": value, "reason": f"{label}表现"})
                result = validate_grading_result(
                    {
                        "point_matches": [{
                            "point_key": "core",
                            "status": "hit" if coverage == 1 else ("partial" if coverage else "miss"),
                            "coverage_ratio": coverage,
                            "answer_quote": "核心原句" if coverage else "",
                            "reason": f"{label}覆盖情况",
                        }],
                        "dimension_scores": dimensions,
                        "holistic_adjustment_reason": "答案虽未命中核心点，但仍有少量任务相关表达。" if label == "weak" else "",
                    },
                    rubric,
                    answer,
                    [],
                )
                scores.append(result["score"])
                evaluated += 1
            self.assertGreater(scores[0], scores[1], question_type)
            self.assertGreater(scores[1], scores[2], question_type)
        self.assertEqual(evaluated, 15)

    def test_long_non_essay_rubric_rejects_two_composite_points(self):
        reference_text = (
            "关于W市商业航天的发言提纲。背景：前瞻布局新赛道。经验：一、科学决策，"
            "掌握产业前沿信息，论证落地条件，划定产业先行区。二、搭建产业链，帮助企业技术升级，"
            "引入链主和上下游企业。三、打造土地超市，提供灵活用地，建设一站式测试平台。"
            "四、全域统筹、错位发展，形成核心引领、多点支撑的互补生态。"
        )
        question = {
            "id": 7638,
            "question_type": "公文写作",
            "prompt": "介绍W市发展商业航天的创新实践",
            "requirements": "内容全面，不超过450字",
            "word_limit": "不超过450字",
        }
        references = [{"id": 82928, "organization": "粉笔", "answer_text": reference_text}]
        materials = [{"material_number": 4, "content": reference_text}]
        malformed = {
            "points": [
                {
                    "label": "标题与文种",
                    "canonical_expression": "发言提纲标题",
                    "tier": "material_core",
                    "suggested_weight": 20,
                    "reference_ids": [82928],
                    "reference_quote": "关于W市商业航天的发言提纲",
                    "material_evidence": [{"material_number": 4, "quote": "关于W市商业航天的发言提纲"}],
                },
                {
                    "label": "背景与经验",
                    "canonical_expression": "前瞻布局并形成完整产业生态",
                    "tier": "material_core",
                    "suggested_weight": 30,
                    "reference_ids": [82928],
                    "reference_quote": "背景：前瞻布局新赛道",
                    "material_evidence": [{"material_number": 4, "quote": "前瞻布局新赛道"}],
                },
            ]
        }
        with self.assertRaisesRegex(ValueError, "合理归并"):
            validate_rubric(malformed, question, materials, references)

    def test_real_report_7638_uses_fenbi_only_teacher_points_and_promotes_material_only_partial(self):
        reference_text = """关于W市“无中生有”发展商业航天的发言提纲

背景：近年来，商业航天领域正成为全球瞩目的焦点。此前，W市没有相关产业积淀，但敢为人先、前瞻布局新赛道。

经验：一、科学决策选准赛道。综合产业前沿信息和本地制造业优势，论证产业落地基础条件，将商业航天纳入未来产业发展规划，以细分领域作为切入口，划定产业先行区。二、搭建完整产业链条。帮助本地传统制造业企业进行技术升级和产品迭代，跨界加入商业航天产业链，补齐本地配套供给短板；引入链主企业及产业链上下游企业，实现强链补链延链。三、创新要素保障供给。打造“土地超市”，整合土地和存量厂房数据，提供灵活用地期限，快速满足企业用地需求；从零搭建测试验证平台，就近为企业提供全流程“一站式”测试服务。四、注重全域统筹规划。各先行区因地制宜，错位发展，形成“核心引领、多点支撑”的互补产业生态，避免同质化竞争。

结尾：W市通过发挥自身优势，跨界转型，成功培育出极具活力的商业航天产业新生态。"""
        user_answer = """关于产业发展会议上经验交流的发言提纲
背景：w市积极布局新赛道，跨界入局商业航天，培育出极具活力的商业航天产业新生态。
措施：
1，科学决策。了解行业发展趋势，论证落地基础条件。瞄准细分领域，打造产业先行区。
2，搭建完整产业链条。政府帮助企业完成产品迭代和技术升级，加入航天产业链，补齐本地配套供给端斑。招揽链主企业和上下游企业，强链补链延链，实现产业积聚。
3，提供要素保障供给。推出土地超市，整合相关数据。根据企业土地需求锁定地点，根据实际情况提供灵活用地期限，加快投产。打造检测服务平台，提供全过程一站式服务，在家门口完成实验。
5，做好全域规划。不同区域结合实际，错位发展，形成核心引领，多点支撑格局，避免同质化竞争，形成互补生态。
结语：w市的做法应当值得大力推广。"""
        question = {
            "id": 7638,
            "question_type": "公文写作",
            "prompt": "假如你是W市有关部门工作人员，请拟写一份介绍商业航天发展经验的发言提纲。（20分）",
            "requirements": "内容全面，条理清晰，不超过450字。",
            "word_limit": "不超过450字",
        }
        references = [{"id": 82928, "organization": "粉笔", "answer_text": reference_text}]
        materials = [{"material_number": 4, "content": reference_text}]
        point_specs = [
            ("background", "背景", 1, "敢为人先、前瞻布局新赛道", 1.0),
            ("decision-basis", "科学决策", 2, "综合产业前沿信息和本地制造业优势，论证产业落地基础条件", 2.0),
            ("future-plan", "科学决策", 2, "将商业航天纳入未来产业发展规划", 1.2),
            ("narrow-field", "科学决策", 2, "以细分领域作为切入口", 1.0),
            ("pilot-zone", "科学决策", 2, "划定产业先行区", 1.0),
            ("local-upgrade", "产业链", 3, "帮助本地传统制造业企业进行技术升级和产品迭代，跨界加入商业航天产业链，补齐本地配套供给短板", 2.1),
            ("chain-owner", "产业链", 3, "引入链主企业及产业链上下游企业，实现强链补链延链", 1.8),
            ("land-support", "创新要素保障供给", 4, "打造“土地超市”，整合土地和存量厂房数据，提供灵活用地期限，快速满足企业用地需求", 1.7),
            ("test-support", "创新要素保障供给", 4, "从零搭建测试验证平台，就近为企业提供全流程“一站式”测试服务", 1.6),
            ("regional-plan", "全域统筹", 5, "各先行区因地制宜，错位发展，形成“核心引领、多点支撑”的互补产业生态，避免同质化竞争", 2.6),
        ]
        raw_points = []
        for order, (key, group, group_order, quote, weight) in enumerate(point_specs, 1):
            raw_points.append({
                "point_key": key,
                "group_key": f"group-{group_order}",
                "group_label": group,
                "group_order": group_order,
                "point_order": order,
                "label": quote[:28],
                "canonical_expression": quote,
                "tier": "material_core",
                "importance": "major",
                "suggested_weight": weight,
                "weight_reason": "按阅卷时可一次判断的完整语义单元分配。",
                "required_for_full_score": True,
                "required_elements": ["材料中自行扩写的额外条件"],
                "optional_details": ["北京、西安等材料例子"],
                "reference_ids": [82928],
                "reference_quote": quote,
                "material_evidence": [{"material_number": 4, "quote": quote}],
            })
        rubric = validate_rubric(
            {"points": raw_points}, question, materials, references
        )
        self.assertEqual(len([p for p in rubric["points"] if p["coverage_role"] == "required"]), 10)
        self.assertTrue(all(not p["required_elements"] for p in rubric["points"]))
        self.assertTrue(all(not p["optional_details"] for p in rubric["points"]))
        self.assertEqual(
            [p["group_label"] for p in rubric["points"]],
            [spec[1] for spec in point_specs],
        )
        self.assertAlmostEqual(sum(p["display_weight"] for p in rubric["points"]), 10.0)

        quotes = {
            "background": "积极布局新赛道",
            "decision-basis": "了解行业发展趋势，论证落地基础条件",
            "future-plan": "",
            "narrow-field": "瞄准细分领域",
            "pilot-zone": "打造产业先行区",
            "local-upgrade": "帮助企业完成产品迭代和技术升级，加入航天产业链，补齐本地配套供给端斑",
            "chain-owner": "招揽链主企业和上下游企业，强链补链延链",
            "land-support": "推出土地超市，整合相关数据。根据企业土地需求锁定地点，根据实际情况提供灵活用地期限，加快投产",
            "test-support": "打造检测服务平台，提供全过程一站式服务，在家门口完成实验",
            "regional-plan": "不同区域结合实际，错位发展，形成核心引领，多点支撑格局，避免同质化竞争，形成互补生态",
        }
        matches = []
        for key, *_ in point_specs:
            status = "miss" if key == "future-plan" else "hit"
            item = {
                "point_key": key,
                "status": status,
                "score_level": "none" if status == "miss" else "full",
                "answer_quote": quotes[key],
                "reason": "按粉笔答案核心语义判断。",
                "missing_elements": [],
            }
            if key == "decision-basis":
                item.update(status="partial", score_level="mostly", missing_elements=["本地制造业优势"])
            if key == "narrow-field":
                item.update(status="partial", score_level="half", missing_elements=["可回收火箭技术、关键零部件和材料"])
            if key == "pilot-zone":
                item.update(status="partial", score_level="half", missing_elements=["梁田区、高新区、惠南区"])
            matches.append(item)
        raw_result = {
            "point_matches": matches,
            "dimension_scores": [
                {"dimension": "content", "score": 1, "reason": "该值必须由逐点累计覆盖。"},
                {"dimension": "format", "score": 14, "reason": "文种基本正确，标题和结尾不够贴合。"},
                {"dimension": "structure", "score": 19, "reason": "主体层次清楚，序号有跳号。"},
                {"dimension": "expression", "score": 10, "reason": "表达简洁准确。"},
            ],
        }
        result = validate_grading_result(raw_result, rubric, user_answer, [])
        by_key = {item["point_key"]: item for item in result["point_matches"]}
        self.assertEqual(by_key["narrow-field"]["status"], "hit")
        self.assertEqual(by_key["pilot-zone"]["status"], "hit")
        self.assertEqual(by_key["decision-basis"]["status"], "partial")
        self.assertEqual(by_key["future-plan"]["status"], "miss")
        self.assertGreaterEqual(result["display_score"], 17.0)
        self.assertLessEqual(result["display_score"], 18.0)
        report = render_grading_report(result, rubric, [])
        self.assertIn("| 创新要素保障供给 |", report)
        self.assertIn("瞄准细分领域", report)
        self.assertIn("打造产业先行区", report)
        self.assertNotIn("覆盖50%", report)
        self.assertIn("逐点累计内容分", report)

    def test_report_7638_scores_atomic_points_and_uses_one_color_status_source(self):
        reference_text = (
            "科学决策选准赛道；论证产业落地基础条件；划定产业先行区；"
            "帮助企业技术升级和产品迭代；引入链主及上下游企业；打造土地超市；"
            "提供灵活用地期限；搭建测试验证平台；各先行区错位发展；形成互补产业生态。"
        )
        user_answer = (
            "了解行业发展趋势，论证落地基础条件，打造产业先行区。帮助企业完成产品迭代和技术升级，"
            "招揽链主企业和上下游企业。推出土地超市，提供灵活用地期限。打造检测服务平台。"
            "不同区域错位发展，形成互补生态。"
        )
        labels_and_quotes = [
            ("科学决策", "科学决策选准赛道", "了解行业发展趋势"),
            ("论证条件", "论证产业落地基础条件", "论证落地基础条件"),
            ("先行区", "划定产业先行区", "打造产业先行区"),
            ("技术迭代", "帮助企业技术升级和产品迭代", "完成产品迭代和技术升级"),
            ("招引产业链", "引入链主及上下游企业", "招揽链主企业和上下游企业"),
            ("土地超市", "打造土地超市", "推出土地超市"),
            ("灵活用地", "提供灵活用地期限", "提供灵活用地期限"),
            ("测试平台", "搭建测试验证平台", "打造检测服务平台"),
            ("错位发展", "各先行区错位发展", "不同区域错位发展"),
            ("互补生态", "形成互补产业生态", "形成互补生态"),
        ]
        points = []
        matches = []
        for index, (label, reference_quote, answer_quote) in enumerate(labels_and_quotes, 1):
            key = f"point-{index}"
            points.append({
                "point_key": key,
                "label": label,
                "canonical_expression": reference_quote,
                "reference_quote": reference_quote,
                "weight": 5,
                "suggested_weight": 1,
                "required_for_full_score": True,
                "coverage_role": "required",
            })
            matches.append({
                "point_key": key,
                "status": "hit",
                "coverage_ratio": 0.2,  # must be ignored by discrete scoring
                "answer_quote": answer_quote,
                "reason": "核心动作与粉笔小点同义命中。",
            })
        rubric = {
            "question_type": "公文写作",
            "display_max_score": 20,
            "scoring_mode": "point_based",
            "selected_references": [{"id": 82928, "organization": "粉笔", "answer_text": reference_text}],
            "points": points,
        }
        result = validate_grading_result(
            {
                "point_matches": matches,
                "dimension_scores": [
                    {"dimension": "content", "score": 50, "reason": "十个主体措施小点全部命中"},
                    {"dimension": "format", "score": 15, "reason": "开头结尾尚可规范"},
                    {"dimension": "structure", "score": 16, "reason": "主体分点完整"},
                    {"dimension": "expression", "score": 9, "reason": "表达准确"},
                ],
            },
            rubric,
            user_answer,
            [],
        )
        report = render_grading_report(result, rubric, [])
        self.assertEqual(result["weighted_coverage_score"], 50.0)
        self.assertAlmostEqual(result["display_score"], 18.0, places=1)
        for index in range(1, 11):
            key = f"point-{index}"
            self.assertIn(f"|{key}|", report)
            self.assertNotIn("[标答点|miss|", report)
        self.assertNotIn("覆盖20%", report)

    def test_partial_master_point_and_user_redundancy_share_visible_annotations(self):
        from gongkao.grading_pipeline.report import build_user_mirrored_answer

        rubric = {
            "question_type": "归纳概括",
            "display_max_score": 20,
            "selected_references": [{"id": 1, "organization": "粉笔", "answer_text": "建立线上课堂，扩大教育覆盖。"}],
            "points": [{
                "point_key": "online-class",
                "label": "线上课堂",
                "reference_quote": "建立线上课堂，扩大教育覆盖",
                "weight": 35,
                "display_weight": 7,
                "coverage_role": "required",
            }],
        }
        result = {
            "score": 50,
            "display_score": 10,
            "display_max_score": 20,
            "weighted_coverage_score": 17.5,
            "content_score": 17.5,
            "point_matches": [{
                "point_key": "online-class",
                "status": "partial",
                "coverage_ratio": 0.5,
                "awarded_score": 17.5,
                "answer_quote": "建立线上课堂",
                "evidence_spans": [{"start": 0, "end": 6}],
                "reason": "未体现扩大覆盖。",
                "missing_elements": ["扩大教育覆盖"],
            }],
            "redundancies": [{
                "quote": "值得大力推广",
                "wasted_chars": 6,
                "reason": "空泛表态，无信息增量",
            }],
            "dimension_scores": [],
        }
        report = render_grading_report(result, rubric, [])
        self.assertIn("[标答点|partial|", report)
        self.assertIn("|建立线上课堂，扩大教育覆盖]", report)
        mirrored = build_user_mirrored_answer(
            "建立线上课堂，值得大力推广。", result, rubric, 0.2
        )
        self.assertIn("[作答点|partial|", mirrored)
        self.assertIn("[冗余|6字|空泛表态，无信息增量|值得大力推广]", mirrored)

    def test_report_ui_has_holistic_score_and_dimension_cards(self):
        server_source = read_server_application(ROOT)
        stylesheet = read_static_styles(ROOT)
        self.assertIn("grading-score-overview", server_source)
        self.assertIn("grading-dimension-card", server_source)
        self.assertIn("待重新批改", server_source)
        self.assertIn(".grading-score-overview", stylesheet)
        self.assertIn(".grading-dimension-card", stylesheet)

    def test_fenbi_score_tree_rubric_scores_by_official_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path, question_id, attempt_id, reference_ids = self.make_database(directory)
            with connect(path) as conn:
                conn.execute(
                    "UPDATE reference_answers SET score_tree_json = ? WHERE id = ?",
                    (
                        json.dumps(
                            {
                                "name": "得分分析",
                                "full_mark": 20,
                                "children": [
                                    {"id": 1, "name": "线上课堂", "full_mark": 14, "children": []},
                                    {"id": 2, "name": "政策宣传", "full_mark": 6, "children": []},
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        reference_ids[0],
                    ),
                )
                conn.execute(
                    "UPDATE questions SET prompt = prompt || '（20分）' WHERE id = ?",
                    (question_id,),
                )
                question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
                references = conn.execute(
                    "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id",
                    (question_id,),
                ).fetchall()
            self.assertTrue(fenbi_tree_available(references))
            rubric = build_fenbi_tree_rubric(question, references)
            self.assertEqual(rubric["scoring_mode"], "fenbi_tree")
            self.assertEqual(sum(point["display_weight"] for point in rubric["points"]), 20)

            matches = [
                {
                    "point_key": point["point_key"],
                    "status": "hit",
                    "score_level": "full",
                    "answer_quote": point["label"],
                    "reason": "完整覆盖",
                }
                for point in rubric["points"]
            ]
            raw = {
                "evaluation": {
                    "point_matches": matches,
                    "dimension_scores": [{"dimension": "content", "score": 0, "reason": ""}],
                    "holistic_adjustment_reason": "",
                    "annotations": [],
                    "redundancies": [],
                    "reference_fusion": "",
                    "material_reading": [],
                    "optimization_suggestions": [],
                    "personalized_findings": [],
                    "summary": {},
                }
            }
            result = validate_grading_result(
                raw,
                rubric,
                "数字平台让村民办事更加方便，政策查询更加及时。",
                [],
                calibration_policy=None,
            )
            self.assertAlmostEqual(result["score"], 100.0)

    def test_essay_with_fenbi_tree_builds_holistic_rubric_and_bands_total(self):
        with tempfile.TemporaryDirectory() as directory:
            path, question_id, attempt_id, reference_ids = self.make_database(directory)
            tree = {
                "name": "得分分析",
                "full_mark": 35,
                "children": [{"id": 1, "name": "中心立意", "full_mark": 35, "children": []}],
            }
            with connect(path) as conn:
                conn.execute(
                    """
                    UPDATE questions
                       SET question_type = '综合写作',
                           prompt = '请围绕数字治理写一篇文章。（35分）',
                           requirements = '观点明确，论证充分。',
                           word_limit = '1000字左右'
                     WHERE id = ?
                    """,
                    (question_id,),
                )
                conn.execute(
                    "UPDATE reference_answers SET score_tree_json = ? WHERE id = ?",
                    (json.dumps(tree, ensure_ascii=False), reference_ids[0]),
                )
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                job, _ = create_grading_job(conn, attempt, settings, reference_ids[:1], "", {})

            calls = []

            def essay_chat(settings, prompt, request_options=None):
                calls.append(prompt)
                if "<rubric_json>" in prompt:
                    payload = {
                        "question_id": question_id,
                        "task_constraints": {
                            "object": "数字治理",
                            "required_structure": ["五段三分"],
                            "format_rules": [],
                        },
                        "points": [
                            {
                                "point_key": "thesis",
                                "group_key": "group-1",
                                "group_label": "中心立意",
                                "point_order": 1,
                                "label": "数字治理提升服务效率",
                                "canonical_expression": "数字平台提高办事便利度，政策信息获取更及时。",
                                "aliases": ["数字服务便民"],
                                "tier": "core",
                                "importance": "critical",
                                "suggested_weight": 30,
                                "weight_reason": "中心立意是袁东定档的首要依据。",
                                "required_for_full_score": True,
                                "required_elements": ["数字化", "服务效率"],
                                "optional_details": [],
                                "minimum_expression": "数字治理提升服务效率",
                                "reference_quote": "数字平台提高办事便利度，政策信息获取更及时。",
                                "material_evidence": [{"material_number": 1, "quote": "村民办事更加方便"}],
                                "reference_ids": [reference_ids[0]],
                                "confidence": 0.95,
                            }
                        ],
                        "equal_weight_reason": "",
                        "conflicts": [],
                    }
                    text = f"<rubric_json>{json.dumps(payload, ensure_ascii=False)}</rubric_json>"
                    return text, text

                evaluation = {
                    "overall_band": "B",
                    "band_reason": "立意切题，结构完整，三个主要论证成立。",
                    "high_band_evidence": {
                        "precise_task_and_theme": True,
                        "clear_thesis": True,
                        "coherent_argument_structure": True,
                        "material_accurate_and_specific": True,
                        "major_arguments_fully_developed": True,
                        "depth_or_innovation": False,
                        "no_fact_or_logic_hard_error": True,
                    },
                    "point_matches": [
                        {
                            "point_key": "thesis",
                            "status": "hit",
                            "score_level": "full",
                            "answer_quote": "数字平台让村民办事更加方便",
                            "reason": "中心立意与材料方向一致。",
                        }
                    ],
                    "dimension_scores": [
                        {"dimension": "content", "score": 30, "reason": "立意准确切题。"},
                        {"dimension": "structure", "score": 20, "reason": "结构完整。"},
                        {"dimension": "reasoning", "score": 20, "reason": "主要论证成立。"},
                        {"dimension": "material", "score": 15, "reason": "材料使用准确。"},
                        {"dimension": "expression", "score": 15, "reason": "语言流畅。"},
                    ],
                    "holistic_adjustment_reason": "",
                    "annotations": [],
                    "summary": {},
                }
                text = f"<smart_grading_json>{json.dumps({'evaluation': evaluation}, ensure_ascii=False)}</smart_grading_json>"
                return text, text

            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], essay_chat)

            self.assertIsNotNone(report_id)
            self.assertEqual(len(calls), 2)
            self.assertIn("<rubric_json>", calls[0])
            self.assertIn("禁止把下面 points 的权重逐点相加得到作文总分", calls[0])
            with connect(path) as conn:
                context = conn.execute(
                    "SELECT * FROM grading_report_contexts WHERE report_id = ?",
                    (report_id,),
                ).fetchone()
                rubric = json.loads(context["rubric_snapshot_json"])
                result = json.loads(context["result_json"])
            self.assertEqual(rubric["scoring_mode"], "holistic_essay")
            self.assertNotEqual(rubric.get("source"), "fenbi_score_tree")
            self.assertEqual(result["score"], 79.0)
            self.assertEqual(result["essay_band"], "B")
    def test_grading_preserves_accuracy_and_comprehensiveness_partial_scores(self):
        question = {
            "id": 101,
            "question_type": "归纳概括",
            "prompt": "请概括某市推进产业升级的主要举措。（20分）",
            "requirements": "全面、准确。",
            "display_max_score": 20,
        }
        point = {
            "point_key": "p1",
            "label": "搭建全流程服务平台，提供一站式服务",
            "canonical_expression": "搭建全流程服务平台，提供一站式服务",
            "reference_quote": "搭建全流程服务平台，提供一站式服务",
            "weight": 50.0,
            "display_weight": 10.0,
            "coverage_role": "required",
            "required_for_full_score": True,
        }
        rubric = {
            "question_type": "归纳概括",
            "scoring_mode": "point_based",
            "display_max_score": 20,
            "points": [point],
            "criteria": [
                {"dimension": "content", "weight": 70.0},
                {"dimension": "structure", "weight": 15.0},
                {"dimension": "expression", "weight": 10.0},
                {"dimension": "format", "weight": 5.0},
            ],
        }
        raw_1 = {
            "point_matches": [{
                "point_key": "p1",
                "status": "partial",
                "score_level": "half",
                "answer_quote": "打造服务平台",
                "reason": "写出了平台建设，但表述过于宽泛笼统，缺少全流程一站式服务的精准提炼。",
                "missing_elements": ["表述过于宽泛，不够精准"],
            }],
            "dimension_scores": [
                {"dimension": "content", "score": 10, "reason": ""},
                {"dimension": "structure", "score": 15, "reason": ""},
                {"dimension": "expression", "score": 10, "reason": ""},
                {"dimension": "format", "score": 5, "reason": ""},
            ],
        }
        res_1 = validate_grading_result(raw_1, rubric, "打造服务平台进行服务。", [])
        m_1 = res_1["point_matches"][0]
        self.assertEqual(m_1["status"], "partial")
        self.assertEqual(m_1["score_level"], "half")
        self.assertEqual(m_1["coverage_ratio"], 0.5)
        self.assertEqual(m_1["awarded_score"], 25.0)

        raw_2 = {
            "point_matches": [{
                "point_key": "p1",
                "status": "partial",
                "score_level": "mostly",
                "answer_quote": "搭建服务平台，提供便民服务",
                "reason": "要素基本具备，但概括不够全面，遗漏了一站式服务机制。",
                "missing_elements": [],
            }],
            "dimension_scores": [
                {"dimension": "content", "score": 10, "reason": ""},
                {"dimension": "structure", "score": 15, "reason": ""},
                {"dimension": "expression", "score": 10, "reason": ""},
                {"dimension": "format", "score": 5, "reason": ""},
            ],
        }
        res_2 = validate_grading_result(raw_2, rubric, "搭建服务平台，提供便民服务。", [])
        m_2 = res_2["point_matches"][0]
        self.assertEqual(m_2["status"], "partial")
        self.assertEqual(m_2["score_level"], "mostly")
        self.assertEqual(m_2["coverage_ratio"], 0.75)
        self.assertEqual(m_2["awarded_score"], 37.5)

    def test_schema3_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outdated.sqlite3"
            init_db(path)
            with connect(path) as conn:
                conn.execute("PRAGMA user_version = 3")
            with self.assertRaisesRegex(RuntimeError, "unsupported database schema 3"):
                prepare_user_database(path)


if __name__ == "__main__":
    unittest.main()

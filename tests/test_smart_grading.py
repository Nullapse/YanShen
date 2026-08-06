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
                job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                report_id = run_grading_job(path, job["id"], chat)
            self.assertIsNotNone(report_id)
            self.assertEqual(len(calls), 1)
            self.assertIn("机构参考答案样本提示", calls[0])
            self.assertIn("样本不足", calls[0])
            self.assertEqual([item.get("thinking") for item in chat.request_options], ["disabled"])
            self.assertEqual(
                [item.get("response_format") for item in chat.request_options],
                [{"type": "json_object"}],
            )

            with connect(path) as conn:
                attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                second_job, _ = create_grading_job(conn, attempt, settings, reference_ids, "", {})
            with patch(
                "gongkao.grading_pipeline.orchestration.retrieve_grading_evidence",
                return_value=([], {"history_attempt_count": 0, "history_stable": False}),
            ):
                second_report_id = run_grading_job(path, second_job["id"], chat)
            self.assertIsNotNone(second_report_id)
            self.assertEqual(len(calls), 2)
            with connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM grading_rubrics").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT status FROM grading_jobs WHERE id = ?", (second_job["id"],)).fetchone()[0], "completed")
                self.assertEqual(conn.execute("SELECT api_call_count FROM grading_report_contexts WHERE report_id = ?", (second_report_id,)).fetchone()[0], 1)

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
            self.assertEqual([item.get("thinking") for item in chat.request_options], ["disabled"])
            with connect(path) as conn:
                options = json.loads(conn.execute("SELECT options_json FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()[0])
                validation = json.loads(conn.execute("SELECT validation_json FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()[0])
            self.assertFalse(options["deep_thinking"])
            self.assertFalse(validation["deep_thinking"])

    def test_over_limit_repair_is_saved_as_formal_report(self):
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
            self.assertIn("超出字数限制", failed_job["message"])
            self.assertEqual(failed_job["report_id"], report_id)
            self.assertIsNotNone(saved_report)
            self.assertEqual(saved_report["status"], "ok")
            self.assertIn("乙" * 250, saved_report["report_text"])
            self.assertTrue(validation["word_count_status"]["over_limit"])
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
        result = validate_grading_result(
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
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["point_matches"][0]["status"], "miss")

    def test_partial_matches_use_ai_coverage_instead_of_fixed_half(self):
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
        self.assertEqual(result["weighted_coverage_score"], 52.5)
        self.assertEqual(result["display_score"], 5.0)
        self.assertIn("部分命中（覆盖75%）", render_grading_report(result, rubric, []))

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
        self.assertIn("不得把所有 partial 机械写成0.5", grading_prompt)
        self.assertIn("修改版答案目标为 315—336 格", grading_prompt)
        self.assertIn("最终结果必须严格低于 350 格", grading_prompt)
        self.assertIn("符合真实考场阅卷强度的“得分制”", grading_prompt)
        self.assertIn("普通“写到了”不能进入此档", grading_prompt)
        self.assertIn('"max_score": 70.0', grading_prompt)

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
            self.assertIn("本题仅有 1 份机构答案，样本不足", prompt)
            self.assertIn("可以结合现有机构答案、题干任务和材料原文自行分析", prompt)

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
        self.assertIn("本题仅有 1 份机构参考答案（机构甲）", report)
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
            self.assertIn(original_answer, calls[0])
            self.assertNotIn("批改开始后编辑的新答案", calls[0])

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
            self.assertEqual(len(calls), 1)

    def test_question_type_profiles_match_holistic_scoring_plan(self):
        self.assertEqual(QUESTION_TYPE_PROFILES["归纳概括"], {"content": 70, "structure": 15, "expression": 10, "format": 5})
        self.assertEqual(QUESTION_TYPE_PROFILES["综合分析"], {"content": 55, "reasoning": 25, "structure": 10, "expression": 10})
        self.assertEqual(QUESTION_TYPE_PROFILES["提出对策"], {"content": 60, "feasibility": 20, "structure": 10, "expression": 10})
        self.assertEqual(QUESTION_TYPE_PROFILES["公文写作"], {"content": 50, "format": 20, "structure": 20, "expression": 10})
        self.assertEqual(QUESTION_TYPE_PROFILES["综合写作"], {"content": 40, "reasoning": 25, "structure": 20, "expression": 10, "format": 5})
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

    def test_holistic_score_is_calibrated_to_weighted_coverage(self):
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
        self.assertEqual(result["content_score"], 57.7)
        self.assertEqual(result["score"], 84.6)
        self.assertEqual(result["display_score"], 8.5)
        self.assertIn("采分点覆盖校准", result["holistic_adjustment_reason"])
        self.assertNotIn("真实考场高分稀缺度", result["holistic_adjustment_reason"])
        self.assertTrue(any("真实考场高分稀缺度" in note for note in result["validation_errors"]))

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

    def test_validation_failure_never_uses_more_than_two_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, attempt_id, reference_ids = self.make_database(directory)
            calls = []

            def invalid_chat(settings, prompt, request_options=None):
                calls.append(prompt)
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
            self.assertEqual(len(calls), 2)
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

    def test_report_ui_has_holistic_score_and_dimension_cards(self):
        server_source = read_server_application(ROOT)
        stylesheet = read_static_styles(ROOT)
        self.assertIn("grading-score-overview", server_source)
        self.assertIn("grading-dimension-card", server_source)
        self.assertIn("待重新批改", server_source)
        self.assertIn(".grading-score-overview", stylesheet)
        self.assertIn(".grading-dimension-card", stylesheet)

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

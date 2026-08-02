import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from gongkao.ai import build_chat_url, chat_completion
from gongkao.db import connect, init_db
from gongkao.grading import (
    answer_grid_metrics,
    build_grading_package,
    build_revised_answer_retry_prompt,
    compact_revised_answer_linebreaks,
    count_cjk_chars,
    normalize_revised_answer_word_count,
    parse_revised_answer_repair,
    referenced_material_numbers,
    replace_revised_answer_body,
    revised_answer_word_count_status,
    select_relevant_materials,
    should_use_whole_paper_materials,
    word_limit_budget,
    word_limit_max,
)
from gongkao.importer import create_import_record, finish_import_record, import_answers, import_questions


class FakeUpload:
    def __init__(self, filename, text):
        self.filename = filename
        self._stream = io.BytesIO(text.encode("utf-8-sig"))

    def read(self):
        return self._stream.read()


class AnswerGridCountTest(unittest.TestCase):
    def test_grid_count_uses_exam_paper_rules(self):
        self.assertEqual(count_cjk_chars("申论，"), 3)
        self.assertEqual(count_cjk_chars("2026"), 2)
        self.assertEqual(count_cjk_chars("ABC"), 2)
        self.assertEqual(count_cjk_chars("ＡＢＣ"), 3)
        self.assertEqual(count_cjk_chars("——"), 2)
        self.assertEqual(count_cjk_chars("—"), 2)
        self.assertEqual(count_cjk_chars("……"), 2)
        self.assertEqual(count_cjk_chars("…"), 2)
        self.assertEqual(count_cjk_chars(" "), 1)

    def test_manual_line_break_consumes_remaining_grid_cells(self):
        metrics = answer_grid_metrics("甲" * 18 + "\n" + "乙" * 30)
        self.assertEqual(metrics["occupied_cells"], 55)
        self.assertEqual(metrics["lines"], 3)
        self.assertEqual(answer_grid_metrics("甲\n乙")["occupied_cells"], 26)
        self.assertEqual(
            answer_grid_metrics("甲\n\n乙"),
            {"occupied_cells": 26, "lines": 2, "columns": 25, "current_line_cells": 1},
        )
        self.assertEqual(answer_grid_metrics("甲" * 25)["current_line_cells"], 25)
        self.assertEqual(answer_grid_metrics("甲" * 26)["current_line_cells"], 1)
        self.assertEqual(answer_grid_metrics("甲\n")["current_line_cells"], 0)
        self.assertEqual(answer_grid_metrics("甲" * 300)["lines"], 12)

    def test_revised_answer_layout_compacts_only_body_linebreaks(self):
        answer = "\n".join(
            [
                "倡议书" + "甲" * 13,
                "各位从业人员：" + "乙" * 3,
                "丙" * 82,
                "丁" * 22,
                "戊" * 21,
                "己" * 24,
                "庚" * 23,
                "辛" * 34,
                "壬" * 67,
                "此致敬礼",
                "D市市场监管局",
            ]
        )
        self.assertGreaterEqual(answer_grid_metrics(answer)["occupied_cells"], 400)
        compacted = compact_revised_answer_linebreaks(answer, "400字左右")
        self.assertLess(answer_grid_metrics(compacted)["occupied_cells"], 400)
        self.assertEqual(compacted.replace("\n", ""), answer.replace("\n", ""))
        self.assertTrue(compacted.startswith(answer.splitlines()[0] + "\n"))
        self.assertTrue(compacted.endswith("\n".join(answer.splitlines()[-3:])))


def upload(name, text):
    return FakeUpload(name, text)


class ImportAndPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(".test_tmp")
        self.tmpdir.mkdir(exist_ok=True)
        self.db_file = self.tmpdir / f"{uuid4().hex}.sqlite3"
        self.db_path = str(self.db_file)
        init_db(self.db_path)

    def tearDown(self):
        if self.db_file.exists():
            self.db_file.unlink()
        if self.tmpdir.exists() and not any(self.tmpdir.iterdir()):
            self.tmpdir.rmdir()

    def test_import_questions_answers_and_build_package(self):
        questions_csv = (
            "题目编号,考试类型,年份,地区,来源省份,训练优先级,题型,标题,题干,材料,作答要求,字数限制,来源备注\n"
            "T-001,广东省考,2024,广东,广东,5,概括归纳,数字服务,概括问题,材料内容,全面准确,300字以内,测试\n"
        )
        answers_csv = (
            "题目编号,机构名,参考答案原文,采分点,备注,是否已校对\n"
            "T-001,机构甲,答案内容,采分点一；采分点二,测试,是\n"
        )

        with connect(self.db_path) as conn:
            import_id = create_import_record(conn, "unit.csv")
            q_result = import_questions(conn, upload("q.csv", questions_csv), import_id)
            a_result = import_answers(conn, upload("a.csv", answers_csv), import_id)
            finish_import_record(conn, import_id, "ok", [], 1, 1)

            self.assertEqual(q_result["imported"], 1)
            self.assertEqual(a_result["imported"], 1)

            question = conn.execute("SELECT * FROM questions WHERE question_code = 'T-001'").fetchone()
            refs = conn.execute("SELECT * FROM reference_answers WHERE question_id = ?", (question["id"],)).fetchall()
            conn.execute(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, ?, ?)",
                (question["id"], "我的答案", 4),
            )
            attempt = conn.execute("SELECT * FROM attempts WHERE question_id = ?", (question["id"],)).fetchone()

        package = build_grading_package(question, refs, attempt)
        self.assertIn("申论作答批改包", package)
        self.assertIn("机构甲", package)
        self.assertIn("我的答案", package)
        self.assertIn("建议作答区间：270—288字", package)
        self.assertIn("硬限制：必须低于300字", package)
        self.assertIn("建议作答区间”只用于指导首轮生成", package)
        self.assertIn("共性核心点", package)
        self.assertIn("不得把已经写出的原句判为未命中", package)
        self.assertIn("实际字数：X 字；建议区间：A—B 字；硬限制：低于 N 字", package)
        self.assertIn("系统保存报告时会按网格规则重新计算", package)
        self.assertIn("原文可视化批注", package)
        self.assertIn("[亮点|原文短句|为什么有效||positive|]", package)
        self.assertIn("踩点对比", package)
        self.assertIn("材料领读", package)
        self.assertIn("修改版答案", package)
        self.assertNotIn("来源链接", package)
        self.assertIn("本题仅有 1 份机构答案，样本不足", package)
        self.assertIn("可以结合现有机构答案、题干任务和材料原文自行分析", package)

        sufficient_reference_package = build_grading_package(
            question,
            list(refs) * 4,
            attempt,
        )
        self.assertNotIn("机构参考答案样本提示", sufficient_reference_package)

        shared_materials = [
            {"material_number": 1, "title": "材料1", "content": "整卷共享材料"},
            {"material_number": 2, "title": "材料2", "content": "第二则材料"},
        ]
        shared_package = build_grading_package(
            question, refs, attempt, materials=shared_materials
        )
        self.assertIn("整卷共享材料", shared_package)
        self.assertIn("第二则材料", shared_package)
        self.assertNotIn("\n材料内容\n", shared_package)

        custom_package = build_grading_package(
            question,
            [],
            attempt,
            custom_reference_answer="我补充的参考答案",
        )
        self.assertIn("用户补充参考答案", custom_package)
        self.assertIn("我补充的参考答案", custom_package)
        self.assertNotIn("本次未提供参考答案", custom_package)

        material_only_package = build_grading_package(question, [], attempt)
        self.assertIn("本次未提供参考答案", material_only_package)
        self.assertIn("仅依据题目、作答要求和材料", material_only_package)
        self.assertIn("本题未提供机构答案，样本不足", material_only_package)

        uncached_basis_package = build_grading_package(
            question,
            refs,
            attempt,
            grading_basis={
                "kind": "uncached",
                "consensus": {
                    "embedding_model": "feature-hash-v1",
                    "clusters": [{"representative": "错误的本地聚类候选"}],
                },
            },
        )
        self.assertIn("本题尚未生成 AI 智能评分基准", uncached_basis_package)
        self.assertIn("独立提炼 4—12 个有材料依据", uncached_basis_package)
        self.assertNotIn("错误的本地聚类候选", uncached_basis_package)
        self.assertNotIn("feature-hash-v1", uncached_basis_package)

        cached_basis_package = build_grading_package(
            question,
            refs,
            attempt,
            grading_basis={
                "kind": "cached_rubric",
                "rubric": {
                    "points": [
                        {
                            "tier": "core",
                            "label": "AI核验要点",
                            "canonical_expression": "规范表达",
                            "material_evidence": [{"quote": "材料原文依据"}],
                            "support_org_count": 2,
                        }
                    ]
                },
            },
        )
        self.assertIn("## AI 智能评分基准", cached_basis_package)
        self.assertIn("AI核验要点", cached_basis_package)
        self.assertIn("材料原文依据", cached_basis_package)

    def test_answer_requires_existing_question(self):
        answers_csv = (
            "题目编号,机构名,参考答案原文,采分点,备注,是否已校对\n"
            "MISSING,机构甲,答案内容,采分点,测试,是\n"
        )
        with connect(self.db_path) as conn:
            import_id = create_import_record(conn, "missing.csv")
            result = import_answers(conn, upload("a.csv", answers_csv), import_id)

        self.assertEqual(result["imported"], 0)
        self.assertTrue(result["errors"])

    def test_ai_settings_and_report_cascade(self):
        questions_csv = (
            "题目编号,考试类型,年份,地区,来源省份,训练优先级,题型,标题,题干,材料,作答要求,字数限制,来源备注\n"
            "T-002,国考,2026,全国,全国,3,提出对策,科技创新,提出措施,材料内容,全面准确,300字以内,测试\n"
        )
        with connect(self.db_path) as conn:
            settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            self.assertEqual(settings["provider_name"], "DeepSeek")
            self.assertEqual(settings["mode"], "api")
            self.assertEqual(settings["model"], "deepseek-v4-pro")

            import_id = create_import_record(conn, "unit.csv")
            import_questions(conn, upload("q.csv", questions_csv), import_id)
            question = conn.execute("SELECT * FROM questions WHERE question_code = 'T-002'").fetchone()
            cursor = conn.execute(
                "INSERT INTO attempts (question_id, answer_text, word_count) VALUES (?, ?, ?)",
                (question["id"], "我的答案", 4),
            )
            attempt_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO grading_reports (attempt_id, provider, model, report_text) VALUES (?, ?, ?, ?)",
                (attempt_id, "Codex", "manual", "批改报告"),
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM grading_reports").fetchone()["c"], 1)
            conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM grading_reports").fetchone()["c"], 0)

    def test_chat_url_builder(self):
        self.assertEqual(build_chat_url("https://api.deepseek.com"), "https://api.deepseek.com/chat/completions")
        self.assertEqual(build_chat_url("https://api.openai.com/v1"), "https://api.openai.com/v1/chat/completions")

    def test_deepseek_thinking_option_is_added_only_for_supported_model(self):
        captured = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"OK"}}]}'

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        settings = {
            "api_key": "test-key",
            "api_key_env": "",
            "api_base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
            "temperature": 0.2,
        }
        with patch("gongkao.ai.urlopen", side_effect=fake_urlopen):
            chat_completion(settings, "test", {"thinking": "disabled"})
            chat_completion({**settings, "api_base_url": "https://example.com"}, "test", {"thinking": "enabled"})
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", captured[1])

    def test_json_output_options_are_forwarded_to_the_api(self):
        captured = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"{}"}}]}'

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        settings = {
            "api_key": "test-key",
            "api_key_env": "",
            "api_base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
            "temperature": 0.2,
        }
        with patch("gongkao.ai.urlopen", side_effect=fake_urlopen):
            chat_completion(
                settings,
                "return json",
                {
                    "thinking": "disabled",
                    "response_format": {"type": "json_object"},
                    "max_tokens": 8192,
                },
            )
        self.assertEqual(captured[0]["response_format"], {"type": "json_object"})
        self.assertEqual(captured[0]["max_tokens"], 8192)

    def test_current_default_ai_settings_do_not_overwrite_custom_values(self):
        with connect(self.db_path) as conn:
            settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            self.assertEqual(settings["mode"], "api")
            self.assertEqual(settings["model"], "deepseek-v4-pro")
            conn.execute(
                "UPDATE ai_settings SET provider_name = 'LocalAI', model = 'custom-model', api_key = 'custom-key' WHERE id = 1"
            )
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
            self.assertEqual(settings["provider_name"], "LocalAI")
            self.assertEqual(settings["model"], "custom-model")
            self.assertEqual(settings["api_key"], "custom-key")
        self.assertEqual(build_chat_url("https://example.com"), "https://example.com/v1/chat/completions")

    def test_select_relevant_materials_from_prompt(self):
        question = {"prompt": "根据给定资料1-3，概括做法。", "requirements": "", "title": "第1题"}
        materials = [
            {"material_number": 1, "title": "材料1", "content": "一"},
            {"material_number": 2, "title": "材料2", "content": "二"},
            {"material_number": 3, "title": "材料3", "content": "三"},
            {"material_number": 4, "title": "材料4", "content": "四"},
        ]
        self.assertEqual(referenced_material_numbers(question), [1, 2, 3])
        self.assertEqual([m["material_number"] for m in select_relevant_materials(question, materials)], [1, 2, 3])
        question = {"prompt": "根据资料1、2，提出建议。", "requirements": "", "title": "第2题"}
        self.assertEqual(referenced_material_numbers(question), [1, 2])

    def test_comprehensive_writing_uses_whole_paper_when_prompt_quotes_one_material(self):
        question = {
            "question_code": "T-WRITE-001",
            "year": 2026,
            "region": "全国",
            "exam_type": "国考",
            "paper_name": "测试卷",
            "prompt": "“给定资料5”中提到“多样性蕴含着创造力”，请你对此深入思考，参考给定资料，联系实际，自选角度，自拟题目，写一篇文章。",
            "requirements": "",
            "title": "第5题",
            "question_type": "综合写作",
            "word_limit": "1000字左右",
            "zhejiang_relevance": 3,
            "is_full_original": True,
            "materials": "",
        }
        materials = [
            {"material_number": 1, "title": "材料1", "content": "材料1内容"},
            {"material_number": 2, "title": "材料2", "content": "材料2内容"},
            {"material_number": 3, "title": "材料3", "content": "材料3内容"},
            {"material_number": 4, "title": "材料4", "content": "材料4内容"},
            {"material_number": 5, "title": "材料5", "content": "材料5内容"},
        ]
        self.assertEqual(referenced_material_numbers(question), [5])
        self.assertTrue(should_use_whole_paper_materials(question))
        self.assertEqual(
            [m["material_number"] for m in select_relevant_materials(question, materials)],
            [1, 2, 3, 4, 5],
        )
        package = build_grading_package(question, [], materials=materials)
        for number in range(1, 6):
            self.assertIn(f"材料{number}内容", package)

    def test_word_limit_max_extracts_upper_bound_for_grading_prompt(self):
        self.assertEqual(word_limit_max("300字以内"), 300)
        self.assertEqual(word_limit_max("不超过 500 字"), 500)
        self.assertEqual(word_limit_max("450-500字"), 500)
        self.assertEqual(word_limit_max("350字左右"), 350)
        self.assertEqual(word_limit_max("1000字左右"), 0)
        self.assertEqual(word_limit_max("不少于300字"), 0)
        self.assertEqual(word_limit_max("未标注"), 0)

    def test_word_limit_budget_distinguishes_target_hard_range_and_minimum(self):
        self.assertEqual(
            word_limit_budget("250字以内"),
            {
                "raw": "250字以内",
                "mode": "hard_max",
                "minimum": 0,
                "suggested_min": 225,
                "suggested_max": 240,
                "hard_max_exclusive": 250,
            },
        )
        ranged = word_limit_budget("200—250字")
        self.assertEqual(
            (ranged["mode"], ranged["minimum"], ranged["suggested_min"], ranged["suggested_max"], ranged["hard_max_exclusive"]),
            ("range", 200, 225, 240, 250),
        )
        approximate = word_limit_budget("1000字左右")
        self.assertEqual(
            (approximate["suggested_min"], approximate["suggested_max"], approximate["hard_max_exclusive"]),
            (950, 1050, 0),
        )
        short_approximate = word_limit_budget("350字左右")
        self.assertEqual(
            (short_approximate["mode"], short_approximate["suggested_min"], short_approximate["suggested_max"], short_approximate["hard_max_exclusive"]),
            ("hard_max", 315, 336, 350),
        )
        minimum = word_limit_budget("不少于300字")
        self.assertEqual(
            (minimum["minimum"], minimum["suggested_min"], minimum["suggested_max"], minimum["hard_max_exclusive"]),
            (300, 300, 330, 0),
        )

    def test_normalize_revised_answer_word_count_overwrites_model_claim(self):
        report = (
            "## 总体评分\n"
            "- 总分：12/20\n\n"
            "## 修改版答案\n"
            "估算字数：999 字；字数上限：300 字\n\n"
            "标题\n"
            "第一条建议。\n"
            "第二条建议。\n"
        )
        normalized = normalize_revised_answer_word_count(report, "300字以内")
        self.assertIn("实际字数：56字；建议区间：270—288字；硬限制：低于300字；状态：符合字数要求，篇幅偏短", normalized)
        self.assertNotIn("估算字数：999", normalized)
        self.assertIn("标题\n第一条建议。\n第二条建议。", normalized)

    def test_normalize_revised_answer_word_count_marks_over_limit_answer(self):
        report = (
            "## 修改版答案\n"
            "实际字数：10 字；字数上限：5 字\n\n"
            "四个字吧\n"
        )
        normalized = normalize_revised_answer_word_count(report, "5字以内")
        self.assertIn("实际字数：4字；建议区间：4—4字；硬限制：低于5字；状态：符合字数要求，处于建议区间", normalized)

        over_limit = normalize_revised_answer_word_count(
            "## 修改版答案\n实际字数：1 字；字数上限：4 字\n\n超过四个字",
            "4字以内",
        )
        self.assertIn("实际字数：5字；建议区间：3—3字；硬限制：低于4字；状态：超出硬限制", over_limit)
        self.assertIn("系统提示：修改版答案未满足严格硬限制", over_limit)
        status = revised_answer_word_count_status(over_limit, "4字以内")
        self.assertTrue(status["over_limit"])
        self.assertEqual(status["over_by"], 2)

    def test_hard_limit_statuses_never_trigger_on_short_or_near_limit_answers(self):
        expected = {
            208: "符合字数要求，篇幅偏短",
            224: "符合字数要求，篇幅偏短",
            225: "符合字数要求，处于建议区间",
            232: "符合字数要求，处于建议区间",
            240: "符合字数要求，处于建议区间",
            241: "符合字数要求，接近上限",
            249: "符合字数要求，接近上限",
        }
        for actual, label in expected.items():
            with self.subTest(actual=actual):
                report = normalize_revised_answer_word_count(
                    f"## 修改版答案\n\n{'甲' * actual}",
                    "250字以内",
                )
                status = revised_answer_word_count_status(report, "250字以内")
                self.assertFalse(status["over_limit"])
                self.assertEqual(status["budget_status"], label)
                self.assertIn(f"状态：{label}", report)

        report = normalize_revised_answer_word_count(
            f"## 修改版答案\n\n{'甲' * 250}",
            "250字以内",
        )
        status = revised_answer_word_count_status(report, "250字以内")
        self.assertTrue(status["over_limit"])
        self.assertEqual(status["over_by"], 1)

    def test_non_hard_word_limits_only_report_guidance(self):
        approximate = revised_answer_word_count_status(
            f"## 修改版答案\n\n{'甲' * 1100}",
            "1000字左右",
        )
        self.assertFalse(approximate["over_limit"])
        self.assertEqual(approximate["budget_status"], "高于建议区间")

        minimum = revised_answer_word_count_status(
            f"## 修改版答案\n\n{'甲' * 299}",
            "不少于300字",
        )
        self.assertFalse(minimum["over_limit"])
        self.assertEqual(minimum["budget_status"], "低于最低要求")

        ranged = revised_answer_word_count_status(
            f"## 修改版答案\n\n{'甲' * 199}",
            "200—250字",
        )
        self.assertFalse(ranged["over_limit"])
        self.assertEqual(ranged["budget_status"], "符合硬限制，低于最低要求")

    def test_short_around_limit_is_a_hard_answer_sheet_limit(self):
        report = normalize_revised_answer_word_count(
            f"## 修改版答案\n\n{'甲' * 393}",
            "350字左右",
        )
        self.assertIn(
            "实际字数：393字；建议区间：315—336字；硬限制：低于350字；状态：超出硬限制",
            report,
        )
        status = revised_answer_word_count_status(report, "350字左右")
        self.assertTrue(status["over_limit"])
        self.assertEqual(status["over_by"], 44)

    def test_build_revised_answer_retry_prompt_keeps_over_limit_answer_out_of_saved_report(self):
        report = "## 修改版答案\n实际字数：1 字；字数上限：4 字\n\n超过四个字"
        prompt = build_revised_answer_retry_prompt("原始批改任务", report, "4字以内")
        self.assertIn("实际占格 5 字；硬限制为低于 4 字；至少需要压缩 2 字", prompt)
        self.assertIn("只重写修改版答案正文", prompt)
        self.assertIn("重复同义和空泛表达 → 差异补充点与非必要例证", prompt)
        self.assertIn("<revised_answer>", prompt)
        self.assertNotIn("重新输出一份完整 Markdown 批改报告", prompt)
        self.assertIn("上一版超限报告如下", prompt)

    def test_local_repair_parser_and_replacement_only_change_answer_section(self):
        self.assertEqual(
            parse_revised_answer_repair("<revised_answer>压缩后的答案。</revised_answer>"),
            "压缩后的答案。",
        )
        self.assertEqual(
            parse_revised_answer_repair("```text\n压缩后的答案。\n```"),
            "压缩后的答案。",
        )
        self.assertEqual(
            parse_revised_answer_repair("## 修改版答案\n\n压缩后的答案。"),
            "压缩后的答案。",
        )
        self.assertEqual(parse_revised_answer_repair("## 总体评分\n- 总分：10"), "")

        original = (
            "## 总体评分\n- 总分：10/20\n\n"
            "## 修改版答案\n实际字数：999字\n\n旧答案" + "甲" * 250 + "\n\n"
            "## 优化建议\n1. 保持不变"
        )
        replaced = replace_revised_answer_body(original, "新答案" + "乙" * 220, "250字以内")
        self.assertEqual(replaced.split("## 修改版答案", 1)[0], original.split("## 修改版答案", 1)[0])
        self.assertEqual(replaced.split("## 优化建议", 1)[1], original.split("## 优化建议", 1)[1])
        self.assertNotIn("旧答案", replaced)
        self.assertIn("新答案", replaced)
        self.assertFalse(revised_answer_word_count_status(replaced, "250字以内")["over_limit"])


if __name__ == "__main__":
    unittest.main()

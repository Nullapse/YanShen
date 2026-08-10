import sqlite3
import unittest

from gongkao.grading_pipeline.calibration import (
    apply_score_calibration,
    build_exam_anchor_manifest,
    fit_high_score_calibration,
    parse_exam_score_label,
)
from gongkao.grading_pipeline.common import question_word_limit_text
from gongkao.grading_pipeline.evidence_resolution import resolve_answer_evidence
from gongkao.grading_pipeline.report import render_grading_report
from gongkao.grading_pipeline.rubric import build_rubric_prompt, normalize_essay_coverage_roles
from gongkao.grading_pipeline.validation import validate_grading_result


class GradingV5Test(unittest.TestCase):
    def test_exam_score_parser_requires_explicit_score_context(self):
        self.assertEqual(parse_exam_score_label("张三考场实战81分答案"), 81)
        self.assertEqual(parse_exam_score_label("85.5分考生答案（回忆版）"), 85.5)
        self.assertIsNone(parse_exam_score_label("申论25套题第3题"))
        self.assertIsNone(parse_exam_score_label("2025年点赞量80"))
        self.assertIsNone(parse_exam_score_label("第81题参考答案"))

    def test_manifest_distinguishes_complete_and_partial_exam(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE questions (
                id INTEGER, paper_id INTEGER, paper_name TEXT, question_number TEXT,
                question_type TEXT, prompt TEXT, requirements TEXT, word_limit TEXT,
                title TEXT, original_text TEXT
            );
            CREATE TABLE reference_answers (
                id INTEGER, question_id INTEGER, organization TEXT, answer_text TEXT
            );
            INSERT INTO questions VALUES
                (1, 10, '完整卷', '1', '归纳概括', '作答。（50分）', '', '', '', ''),
                (2, 10, '完整卷', '2', '综合写作', '作答。（50分）', '', '', '', ''),
                (3, 20, '残缺卷', '1', '归纳概括', '作答。（50分）', '', '', '', ''),
                (4, 20, '残缺卷', '2', '综合写作', '作答。（50分）', '', '', '', '');
            INSERT INTO reference_answers VALUES
                (101, 1, '张三考场81分答案', '答案一'),
                (102, 2, '张三考场81分答案', '答案二'),
                (201, 3, '李四实战79分考生答案', '只有第一题');
            """
        )
        manifest = build_exam_anchor_manifest(conn)
        self.assertEqual(manifest["counts"]["complete_exam"], 1)
        self.assertEqual(manifest["counts"]["partial_exam"], 1)
        complete = next(item for item in manifest["anchors"] if item["classification"] == "complete_exam")
        self.assertEqual(complete["paper_max_score"], 100)
        self.assertEqual(complete["question_ids"], [1, 2])

    def test_evidence_resolution_accepts_formatting_and_ordered_ellipsis(self):
        answer = "坚持生态建设，推动文化保护与活态传承，让城市更宜居。"
        normalized = resolve_answer_evidence("生态 建设", answer)
        fragments = resolve_answer_evidence("文化保护……活态传承", answer)
        missing = resolve_answer_evidence("并不存在的表述", answer)
        self.assertEqual(normalized["status"], "resolved")
        self.assertEqual(normalized["spans"][0]["mode"], "normalized")
        self.assertEqual(fragments["status"], "resolved")
        self.assertEqual(len(fragments["spans"]), 2)
        self.assertEqual(missing["status"], "unresolved")

    def test_high_score_calibration_uses_leave_one_paper_validation(self):
        rows = [
            {
                "classification": "complete_exam",
                "paper_id": paper_id,
                "actual_score": 80 + paper_id,
                "predicted_score": 74 + paper_id,
            }
            for paper_id in range(1, 5)
        ]
        policy = fit_high_score_calibration(rows)
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["validation_method"], "leave_one_paper_out")
        self.assertLess(policy["calibrated_mae"], policy["baseline_mae"])
        low, low_meta = apply_score_calibration(65, policy)
        high, high_meta = apply_score_calibration(80, policy)
        self.assertEqual(low, 65)
        self.assertEqual(low_meta["adjustment"], 0)
        self.assertGreater(high, 80)
        self.assertLessEqual(high_meta["adjustment"], 3)

    def test_calibration_closes_when_direction_is_inconsistent(self):
        rows = [
            {
                "classification": "complete_exam",
                "paper_id": index,
                "actual_score": 80,
                "predicted_score": 74 if index % 2 else 86,
            }
            for index in range(1, 7)
        ]
        self.assertFalse(fit_high_score_calibration(rows)["enabled"])

    def test_essay_score_is_holistic_and_report_has_one_total(self):
        answer = (
            "坚持生态建设，以文化保护带动活态传承，让城市更宜居；"
            "始终相信人民是城市发展的主体。"
        )
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "display_max_score": 35,
            "dimensions": [
                {"dimension": "content", "weight": 40},
                {"dimension": "reasoning", "weight": 25},
                {"dimension": "structure", "weight": 20},
                {"dimension": "expression", "weight": 10},
                {"dimension": "format", "weight": 5},
            ],
            "points": [
                {"point_key": "ecology", "label": "生态", "weight": 7.5, "coverage_role": "alternative"},
                {"point_key": "culture", "label": "文化", "weight": 7.5, "coverage_role": "alternative"},
                {"point_key": "city", "label": "城市", "weight": 7.5, "coverage_role": "alternative"},
                {"point_key": "people", "label": "人民主体", "weight": 7.5, "coverage_role": "alternative"},
                {"point_key": "material4", "label": "材料四案例", "weight": 0, "coverage_role": "bonus"},
            ],
        }
        raw = {
            "point_matches": [
                {"point_key": "ecology", "status": "hit", "answer_quote": "生态 建设"},
                {"point_key": "culture", "status": "hit", "answer_quote": "文化保护……活态传承"},
                {"point_key": "city", "status": "hit", "answer_quote": "城市更宜居"},
                {"point_key": "people", "status": "hit", "answer_quote": "人民是城市发展的主体"},
                {"point_key": "material4", "status": "miss", "answer_quote": ""},
            ],
            "dimension_scores": [
                {"dimension": "content", "score": 24, "reason": "立意准确，论据转化仍可深化。"},
                {"dimension": "reasoning", "score": 14, "reason": "论证基本成立。"},
                {"dimension": "structure", "score": 15, "reason": "结构完整。"},
                {"dimension": "expression", "score": 7, "reason": "表达顺畅。"},
                {"dimension": "format", "score": 5, "reason": "格式正确。"},
            ],
            "overall_summary": "总评：24+14+15+7+5=65分。立意准确。",
            "summary": {"verdict": "立意准确", "strengths": ["结构完整"], "weaknesses": ["论证可深化"]},
        }
        result = validate_grading_result(raw, rubric, answer, [])
        report = render_grading_report(result, rubric, [])
        self.assertEqual(result["score"], 65)
        self.assertEqual(result["display_score"], 23)
        self.assertEqual(result["content_score"], 24)
        self.assertEqual(result["score_status"], "valid")
        self.assertNotIn("24+14+15+7+5", report)
        self.assertEqual(report.count("- 总分：23/35"), 1)

    def test_word_limit_prefers_specific_range_across_source_fields(self):
        question = {
            "word_limit": "1000字以上",
            "requirements": "文章控制在1000～1200字。",
            "prompt": "请写一篇文章。",
        }
        self.assertEqual(question_word_limit_text(question), "1000～1200字")

    def test_high_essay_score_conflicting_with_diagnosis_triggers_review(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "dimensions": [
                {"dimension": "content", "weight": 40},
                {"dimension": "reasoning", "weight": 25},
                {"dimension": "structure", "weight": 20},
                {"dimension": "expression", "weight": 10},
                {"dimension": "format", "weight": 5},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 28, "reason": "存在材料事实偏差。"},
                    {"dimension": "reasoning", "score": 18, "reason": "一个主要段落论证不足。"},
                    {"dimension": "structure", "score": 16, "reason": "结构完整。"},
                    {"dimension": "expression", "score": 7, "reason": "表达流畅。"},
                    {"dimension": "format", "score": 4, "reason": "格式正确。"},
                ],
            },
            rubric,
            "测试作文",
            [],
        )
        self.assertEqual(result["score"], 73)
        self.assertEqual(result["score_status"], "provisional")
        self.assertIn("essay_high_band_diagnostic_conflict", result["review"]["reasons"])

    def test_rubric_prompt_exposes_weight_contract(self):
        prompt = build_rubric_prompt(
            {"id": 1, "question_type": "归纳概括", "prompt": "概括。", "requirements": "", "word_limit": "200字以内"},
            [{"material_number": 1, "content": "材料原文。"}],
            [],
            {"clusters": []},
        )
        self.assertIn('"suggested_weight": 0.0', prompt)
        self.assertIn('"weight_reason"', prompt)
        self.assertIn('"equal_weight_reason"', prompt)

    def test_essay_only_thesis_is_required(self):
        rubric = {
            "question_type": "综合写作",
            "points": [
                {"label": "中心立意", "weight": 12, "coverage_role": "required"},
                {"label": "生态案例", "weight": 7, "coverage_role": "required"},
                {"label": "城市案例", "weight": 7, "coverage_role": "required"},
            ],
        }
        normalize_essay_coverage_roles(rubric)
        self.assertEqual([p["coverage_role"] for p in rubric["points"]], ["required", "alternative", "alternative"])
        self.assertEqual(rubric["points"][2]["alternative_group"], "essay-evidence")


if __name__ == "__main__":
    unittest.main()

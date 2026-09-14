import json
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
from gongkao.grading_pipeline.rubric import (
    build_rubric_prompt,
    fenbi_tree_is_applicable,
    is_current_holistic_essay_rubric,
    manual_grading_basis,
    normalize_essay_coverage_roles,
)
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
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
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
                {"dimension": "content", "score": 20, "reason": "立意准确，论据转化仍可深化。"},
                {"dimension": "structure", "score": 15, "reason": "结构完整。"},
                {"dimension": "reasoning", "score": 14, "reason": "论证基本成立。"},
                {"dimension": "material", "score": 9, "reason": "材料使用基本准确。"},
                {"dimension": "expression", "score": 7, "reason": "表达顺畅。"},
            ],
            "overall_summary": "总评：20+15+14+9+7=65分。立意准确。",
            "summary": {"verdict": "立意准确", "strengths": ["结构完整"], "weaknesses": ["论证可深化"]},
        }
        result = validate_grading_result(raw, rubric, answer, [])
        report = render_grading_report(result, rubric, [])
        self.assertEqual(result["score"], 65)
        self.assertEqual(result["display_score"], 23)
        self.assertEqual(result["content_score"], 20)
        self.assertEqual(result["score_status"], "valid")
        self.assertNotIn("20+15+14+9+7", report)
        self.assertEqual(report.count("- 总分：23/35"), 1)
        self.assertIn("## 袁东定档与五维评分", report)
        self.assertNotIn("逐点累计内容分", report)

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
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 28, "reason": "存在材料事实偏差。"},
                    {"dimension": "structure", "score": 16, "reason": "结构完整。"},
                    {"dimension": "reasoning", "score": 18, "reason": "一个主要段落论证不足。"},
                    {"dimension": "material", "score": 10, "reason": "材料事实存在偏差。"},
                    {"dimension": "expression", "score": 13, "reason": "表达流畅。"},
                ],
            },
            rubric,
            "测试作文",
            [],
        )
        self.assertEqual(result["score"], 69.0)
        self.assertEqual(result["essay_band"], "C")
        self.assertEqual(result["score_status"], "provisional")
        self.assertIn("essay_high_band_diagnostic_conflict", result["review"]["reasons"])

    def test_essay_unjustified_first_class_score_is_capped_at_third_band(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "dimensions": [
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "overall_band": "A",
                "band_reason": "结构完整、语言流畅",
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 30, "reason": "内容完整"},
                    {"dimension": "structure", "score": 20, "reason": "结构完整"},
                    {"dimension": "reasoning", "score": 20, "reason": "论证完整"},
                    {"dimension": "material", "score": 15, "reason": "素材运用充分"},
                    {"dimension": "expression", "score": 15, "reason": "表达流畅"},
                ],
            },
            rubric,
            "一篇只有模板和套话的作文。",
            [],
        )
        self.assertEqual(result["score"], 69.0)
        self.assertEqual(result["essay_band"], "C")
        self.assertFalse(result["essay_high_band_eligible"])
        self.assertIn("缺少一类文", result["essay_band_reason"])
        self.assertAlmostEqual(sum(item["score"] for item in result["dimension_scores"]), 69.0)

    def test_essay_first_class_score_requires_all_high_band_evidence(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "dimensions": [
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "overall_band": "A",
                "high_band_evidence": {
                    "precise_task_and_theme": True,
                    "clear_thesis": True,
                    "coherent_argument_structure": True,
                    "material_accurate_and_specific": True,
                    "major_arguments_fully_developed": True,
                    "depth_or_innovation": True,
                    "no_fact_or_logic_hard_error": True,
                },
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 26, "reason": "立意准确深刻"},
                    {"dimension": "structure", "score": 17, "reason": "结构严密"},
                    {"dimension": "reasoning", "score": 17, "reason": "论证充分深入"},
                    {"dimension": "material", "score": 13, "reason": "材料转化准确且具体"},
                    {"dimension": "expression", "score": 12, "reason": "表达准确流畅"},
                ],
            },
            rubric,
            "审题精准、论证充分、材料使用具体的完整作文。",
            [],
        )
        self.assertEqual(result["score"], 85.0)
        self.assertEqual(result["essay_band"], "A")
        self.assertTrue(result["essay_high_band_eligible"])

    def test_second_class_essay_cannot_keep_a_full_marks_total(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "dimensions": [
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "overall_band": "二类文",
                "high_band_evidence": {
                    "precise_task_and_theme": True,
                    "clear_thesis": True,
                    "coherent_argument_structure": True,
                    "material_accurate_and_specific": True,
                    "major_arguments_fully_developed": True,
                    "depth_or_innovation": False,
                    "no_fact_or_logic_hard_error": True,
                },
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 30, "reason": "立意准确"},
                    {"dimension": "structure", "score": 20, "reason": "结构完整"},
                    {"dimension": "reasoning", "score": 20, "reason": "论证较强"},
                    {"dimension": "material", "score": 15, "reason": "素材运用准确"},
                    {"dimension": "expression", "score": 15, "reason": "表达流畅"},
                ],
            },
            rubric,
            "一篇明确判定为二类文、但模型维度合计给到满分的作文。",
            [],
        )
        self.assertEqual(result["score"], 79.0)
        self.assertEqual(result["essay_band"], "B")
        self.assertEqual(sum(item["score"] for item in result["dimension_scores"]), 79.0)

    def test_legacy_fenbi_essay_rubric_is_scored_holistically(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "fenbi_tree",
            "dimensions": [{"dimension": "content", "weight": 100}],
            "points": [{"point_key": "fenbi-1", "weight": 100, "coverage_role": "required"}],
        }
        result = validate_grading_result(
            {
                "overall_band": "B",
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
                    {"point_key": "fenbi-1", "status": "hit", "answer_quote": "原文"},
                ],
                "dimension_scores": [
                    {"dimension": "content", "score": 100, "reason": "踩点均已命中"},
                ],
            },
            rubric,
            "原文",
            [],
        )
        self.assertEqual(result["score"], 79.0)
        self.assertEqual(result["essay_band"], "B")
        self.assertEqual(result["score_calibration"].get("essay_band_cap"), 79.0)

    def test_manual_essay_basis_never_uses_fenbi_score_tree(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE grading_rubrics (
                id INTEGER PRIMARY KEY,
                question_id INTEGER,
                reference_set_hash TEXT,
                source_hash TEXT,
                rubric_version TEXT,
                rubric_json TEXT,
                status TEXT,
                updated_at TEXT
            )
            """
        )
        tree = {
            "name": "得分分析",
            "full_mark": 35,
            "children": [{"id": 1, "name": "中心立意", "full_mark": 35, "children": []}],
        }
        question = {"id": 1, "question_type": "综合写作", "prompt": "写一篇文章。", "requirements": ""}
        references = [
            {
                "id": 10,
                "organization": "粉笔",
                "answer_text": "参考范文",
                "score_tree_json": json.dumps(tree, ensure_ascii=False),
            }
        ]
        basis = manual_grading_basis(conn, question, [], references)
        self.assertEqual(basis["kind"], "uncached")
        self.assertFalse(fenbi_tree_is_applicable(question, references))
        self.assertFalse(
            is_current_holistic_essay_rubric(
                {
                    "scoring_mode": "holistic_essay",
                    "dimensions": [
                        {"dimension": "content", "weight": 40},
                        {"dimension": "reasoning", "weight": 25},
                        {"dimension": "structure", "weight": 20},
                        {"dimension": "expression", "weight": 10},
                        {"dimension": "format", "weight": 5},
                    ],
                }
            )
        )
        self.assertTrue(
            is_current_holistic_essay_rubric(
                {
                    "scoring_mode": "holistic_essay",
                    "dimensions": [
                        {"dimension": "content", "weight": 30},
                        {"dimension": "structure", "weight": 20},
                        {"dimension": "reasoning", "weight": 20},
                        {"dimension": "material", "weight": 15},
                        {"dimension": "expression", "weight": 15},
                    ],
                }
            )
        )
        self.assertTrue(
            fenbi_tree_is_applicable(
                {**question, "question_type": "归纳概括"},
                references,
            )
        )

    def test_essay_missing_first_round_band_does_not_preserve_a_high_total(self):
        rubric = {
            "question_type": "综合写作",
            "scoring_mode": "holistic_essay",
            "dimensions": [
                {"dimension": "content", "weight": 30},
                {"dimension": "structure", "weight": 20},
                {"dimension": "reasoning", "weight": 20},
                {"dimension": "material", "weight": 15},
                {"dimension": "expression", "weight": 15},
            ],
            "points": [],
        }
        result = validate_grading_result(
            {
                "point_matches": [],
                "dimension_scores": [
                    {"dimension": "content", "score": 24, "reason": "立意尚可"},
                    {"dimension": "structure", "score": 16, "reason": "结构尚可"},
                    {"dimension": "reasoning", "score": 16, "reason": "论证尚可"},
                    {"dimension": "material", "score": 12, "reason": "素材尚可"},
                    {"dimension": "expression", "score": 12, "reason": "表达尚可"},
                ],
            },
            rubric,
            "缺少第一轮档位字段的作文。",
            [],
        )
        self.assertEqual(result["score"], 69.0)
        self.assertEqual(result["essay_band"], "C")
        self.assertEqual(result["essay_band_declared"], "")

    def test_essay_report_uses_yuandong_five_class_labels(self):
        report = render_grading_report(
            {
                "score": 65,
                "display_score": 65,
                "display_max_score": 100,
                "essay_band": "C",
                "essay_band_label": "三类文（中上）",
                "essay_band_reason": "按实际完成质量定档。",
                "overall_summary": "立意基本切题，但论证仍显空泛。",
                "weighted_coverage_score": 0,
                "content_score": 26,
                "dimension_scores": [],
                "point_matches": [],
            },
            {"question_type": "综合写作", "points": [], "selected_references": []},
            [],
        )
        self.assertIn("等级：三类文（中上）", report)
        self.assertNotIn("等级：良好", report)

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

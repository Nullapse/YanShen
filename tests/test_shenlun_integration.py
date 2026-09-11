import unittest

from gongkao.grading import (
    build_grading_package,
    extract_revised_answer_streams,
    normalize_revised_answer_word_count,
    revised_answer_word_count_status,
)
from gongkao.knowledge_cards import load_knowledge_cards
from gongkao.taxonomy import (
    classify_analysis_subtype,
    classify_essay_theme_type,
)
from gongkao.web.runtime import markdownish


class ShenlunIntegrationTest(unittest.TestCase):
    def test_analysis_subtype_classification(self):
        # 1. 词句理解类
        subtype, guide = classify_analysis_subtype("请谈谈对“穷理以致其知，反躬以践其实”这句话的理解。")
        self.assertEqual(subtype, "词句理解类")
        self.assertIn("表层含义", guide)

        # 2. 观点评析类
        subtype, guide = classify_analysis_subtype("有人认为‘数字化是万灵药’，对此观点进行评析。")
        self.assertEqual(subtype, "观点评析类")
        self.assertIn("亮明态度", guide)

        # 3. 现象分析类
        subtype, guide = classify_analysis_subtype("分析材料中基层青年返乡创业现象及其成因。")
        self.assertEqual(subtype, "现象分析类")
        self.assertIn("原因", guide)

        # 4. 关系分析类
        subtype, guide = classify_analysis_subtype("阐述经济发展与环境保护之间的辩证关系。")
        self.assertEqual(subtype, "关系分析类")
        self.assertIn("双向论述互动", guide)

    def test_essay_theme_classification(self):
        # 单主题
        theme, guide = classify_essay_theme_type("围绕‘扎根基层的实干精神’自选角度写一篇文章。")
        self.assertEqual(theme, "单主题")

        # 双主题AB型
        theme, guide = classify_essay_theme_type("围绕‘守正与创新’这一主题，自选角度写一篇文章。")
        self.assertEqual(theme, "双主题AB型")
        self.assertIn("双向互动", guide)

        # 多主题
        theme, guide = classify_essay_theme_type("统筹“产业振兴”“生态宜居”“乡村治理”写一篇文章。")
        self.assertEqual(theme, "多主题")

    def test_dual_stream_revised_answers_extraction_and_word_count(self):
        sample_report = (
            "## 总体评分\n"
            "- 总分：82/100\n\n"
            "## 修改版答案\n"
            "实际字数：待复核\n\n"
            "### 小马哥版（极简·原词直抄流）\n"
            "1. 统筹规划。将基础设施纳入财政专项资金，重点改造老旧管网。\n"
            "2. 拓宽渠道。建立直供冷链物流体系，畅通农产品销售通道。\n\n"
            "### 白鹭版（近义提炼·轻串联流）\n"
            "一、健全基础设施。硬化通村道路，铺设自来水管网，改善出行与饮水条件。\n"
            "二、拓宽引才渠道。设立引才工作站，实施住房补贴政策，吸引青年返乡。\n"
        )
        streams = extract_revised_answer_streams(sample_report)
        self.assertEqual(len(streams), 2)
        self.assertEqual(streams[0]["title"], "小马哥版（极简·原词直抄流）")
        self.assertEqual(streams[1]["title"], "白鹭版（近义提炼·轻串联流）")

        status = revised_answer_word_count_status(sample_report, "200字以内")
        self.assertFalse(status["over_limit"])
        self.assertGreater(status["actual_chars"], 0)
        self.assertLess(status["actual_chars"], 200)

        normalized = normalize_revised_answer_word_count(sample_report, "200字以内")
        self.assertIn("小马哥版", normalized)
        self.assertIn("白鹭版", normalized)
        self.assertNotIn("系统提示：修改版答案未满足严格硬限制", normalized)

    def test_markdownish_renders_dual_stream_tabs(self):
        sample = (
            "## 修改版答案\n"
            "实际字数：小马哥版 68字 · 白鹭版 72字\n\n"
            "### 小马哥版（极简·原词直抄流）\n"
            "1. 要点一内容。\n\n"
            "### 白鹭版（近义提炼·轻串联流）\n"
            "一、概括标题。展开措施。\n"
        )
        html = markdownish(sample, annotation_scope="test-scope")
        self.assertIn('class="tabbed-content revised-answer-tabs" data-tabs', html)
        self.assertIn('role="tab"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn("小马哥版（极简·原词直抄流）", html)
        self.assertIn("白鹭版（近义提炼·轻串联流）", html)

    def test_master_methodology_knowledge_cards_loaded(self):
        cards = load_knowledge_cards("knowledge")
        master_ids = {c["id"] for c in cards if "master" in c["id"]}
        expected = {
            "knowledge:master:xiaoma_summary",
            "knowledge:master:bailu_summary",
            "knowledge:master:writing_seven_rules",
            "knowledge:master:analysis_subtypes",
            "knowledge:master:material_signals",
            "knowledge:master:yuandong_essay",
            "knowledge:master:scoring_standards",
        }
        self.assertTrue(expected.issubset(master_ids))

    def test_build_grading_package_injects_subtypes(self):
        question = {
            "question_code": "Q001",
            "year": 2024,
            "region": "浙江",
            "exam_type": "省考",
            "paper_name": "综合卷",
            "question_type": "综合分析",
            "title": "综合分析题",
            "word_limit": "300字以内",
            "zhejiang_relevance": 5,
            "is_full_original": 1,
            "prompt": "请谈谈对“这句话”的理解。",
            "requirements": "准确全面，不超过300字。",
            "materials": "材料内容",
        }
        package = build_grading_package(question, [])
        self.assertIn("综合分析题型细分：词句理解类", package)
        self.assertIn("Shenlun.skill 名师做题法与批改铁律", package)
        self.assertIn("错别字免扣分铁律", package)
        self.assertIn("AI 仅评分诊断", package)

    def test_report_instructions_contains_typo_exemption(self):
        from gongkao.grading import REPORT_INSTRUCTIONS
        self.assertIn("错别字免扣分铁律", REPORT_INSTRUCTIONS)
        self.assertIn("不是参考答案作者", REPORT_INSTRUCTIONS)
        self.assertIn("禁止生成、改写、压缩", REPORT_INSTRUCTIONS)

    def test_render_grading_report_displays_reference_answers(self):
        from gongkao.grading_pipeline.report import render_grading_report
        result = {
            "score": 75,
            "display_score": 75,
            "display_max_score": 100,
            "overall_summary": "表现良好",
            "reference_audit": "机构答案自创成语过多，缺少材料原词。",
        }
        rubric = {
            "question_type": "提出对策",
            "selected_references": [
                {"organization": "粉笔", "answer_text": "粉笔参考答案"},
                {"organization": "中公", "answer_text": "中公参考答案"},
            ],
            "points": [],
        }
        report = render_grading_report(result, rubric, [])
        self.assertIn("## 粉笔参考答案与采分对照", report)
        self.assertIn("粉笔参考答案", report)
        self.assertNotIn("小马哥版", report)
        self.assertNotIn("白鹭版", report)
        self.assertIn("## 机构参考答案对照", report)
        self.assertIn("### 参考答案 · 中公", report)
        self.assertIn("名师解题逻辑 vs 机构参考答案对照审计", report)
        self.assertIn("机构答案自创成语过多", report)

    def test_yuandong_essay_grading_package(self):
        from gongkao.grading import build_grading_package, REPORT_INSTRUCTIONS_ESSAY
        question = {
            "id": 100,
            "question_code": "2026-GK-05",
            "year": 2026,
            "region": "国考",
            "exam_type": "行政执法",
            "paper_name": "行政执法卷",
            "question_type": "综合写作",
            "title": "大作文",
            "word_limit": "1000-1200字",
            "zhejiang_relevance": 5,
            "is_full_original": 1,
            "prompt": "请以“守正与创新”为主题写一篇文章。",
            "requirements": "自选角度，立意明确，结构完整，字数1000-1200字。",
            "materials": "材料内容：守正为根基，创新为动力。",
        }
        references = [
            {"organization": "粉笔", "answer_text": "粉笔大作文范文参考", "scoring_points": "立意：守正创新相辅相成"}
        ]
        package = build_grading_package(question, references)
        self.assertIn("Shenlun.skill 袁东大作文方法论与评审铁律", package)
        self.assertIn("【粉笔大作文参考答案仅作立意校核依据】", package)
        self.assertIn("双主题AB型", package)
        self.assertIn("五段三分规范结构与段内四层链条", package)
        self.assertIn("政策理论线", package)
        self.assertIn("案例分析线", package)
        self.assertIn("模板与创新规则（模板是底线，不是天花板）", package)
        self.assertIn("## 袁东审题与立意诊断", package)
        self.assertIn("## 五段三分与双线论证分析", package)
        self.assertIn("正文字数规则：仅统计用户作答纯正文字数", package)
        self.assertIn("错别字免扣分铁律", package)
        self.assertIn(REPORT_INSTRUCTIONS_ESSAY, package)

    def test_render_grading_report_essay_reference_audit(self):
        from gongkao.grading_pipeline.report import render_grading_report
        result = {
            "score": 82,
            "display_score": 82,
            "display_max_score": 100,
            "overall_summary": "立意高远，论据扎实",
            "point_matches": [
                {
                    "point_key": "p1",
                    "status": "hit",
                    "coverage_ratio": 1.0,
                    "awarded_score": 20,
                    "answer_quote": "守正为基，创新为翼",
                    "reason": "中心论点明确切题",
                }
            ],
            "annotations": [],
        }
        rubric = {
            "question_type": "综合写作",
            "selected_references": [
                {"organization": "粉笔", "answer_text": "粉笔大作文参考答案"},
            ],
            "points": [
                {"point_key": "p1", "label": "中心论点：守正创新", "weight": 20, "group_label": "立意"}
            ],
        }
        report = render_grading_report(result, rubric, [])
        self.assertIn("## 袁东方法论核心论点与论据判定", report)
        self.assertIn("## 粉笔参考答案与立意校核（仅作论点切题参考）", report)
        self.assertIn("【立意校核说明】：粉笔大作文参考答案仅用于核验考生的中心立意与分论点是否切题", report)


    def test_benchmark_count_excludes_diagnostics_and_decodes_entities(self):
        from gongkao.grading import count_cjk_chars, normalize_revised_answer_word_count
        from gongkao.web.runtime import markdownish

        report = (
            "## 名师标杆答案与采分对照\n"
            "实际字数：999字\n\n"
            "[标答点|hit|+2分 / 满分2分|很长的作答诊断|很长的材料出处|point-1|科学决策。]"
        )
        normalized = normalize_revised_answer_word_count(report, "50字以内")
        self.assertIn("实际字数：5字", normalized)
        self.assertEqual(
            count_cjk_chars("[标答点|hit|+2分|诊断|出处|point-1|科学决策。]"),
            count_cjk_chars("科学决策。"),
        )
        html = markdownish("## 粉笔参考答案与采分对照\n&emsp;&emsp;科学决策。")
        self.assertNotIn("&amp;emsp;", html)
        self.assertNotIn("&emsp;", html)

    def test_smart_prompt_forbids_complete_answer_generation(self):
        from gongkao.grading_pipeline.evidence import build_grading_prompt

        prompt = build_grading_prompt(
            {"id": 1, "question_type": "归纳概括", "prompt": "概括做法", "requirements": "准确", "word_limit": "200字以内"},
            [{"material_number": 1, "content": "完善机制。"}],
            {"id": 1, "answer_text": "完善机制。"},
            {"points": [], "criteria": [], "word_budget": {}},
            [],
            references=[{"id": 1, "organization": "粉笔", "answer_text": "完善机制。"}],
        )
        self.assertIn("不是参考答案作者", prompt)
        self.assertIn("禁止生成、改写、压缩或润色任何完整答案", prompt)
        self.assertNotIn('"revised_answer"', prompt)

    def test_relay_base_url_normalization_and_headers(self):
        from gongkao.ai import build_chat_url, DEFAULT_USER_AGENT
        from gongkao.web.controllers.settings import _clean_base_url
        from gongkao.agent_graph import _normalize_base_url

        # Test URL normalization for OpenCode Go
        self.assertEqual(build_chat_url("https://opencode.ai/go"), "https://opencode.ai/zen/go/v1/chat/completions")
        self.assertEqual(build_chat_url("https://opencode.ai/go/v1"), "https://opencode.ai/zen/go/v1/chat/completions")
        self.assertEqual(build_chat_url("https://opencode.ai/zen/go/v1"), "https://opencode.ai/zen/go/v1/chat/completions")

        self.assertEqual(_clean_base_url("https://opencode.ai/go"), "https://opencode.ai/zen/go/v1")
        self.assertEqual(_clean_base_url("https://opencode.ai/go/v1"), "https://opencode.ai/zen/go/v1")
        self.assertEqual(_normalize_base_url("https://opencode.ai/go"), "https://opencode.ai/zen/go/v1")

        # Standard browser User-Agent
        self.assertIn("Mozilla/5.0", DEFAULT_USER_AGENT)
        self.assertNotIn("Python-urllib", DEFAULT_USER_AGENT)

    def test_detect_provider_preset(self):
        from gongkao.web.controllers.settings import detect_provider_preset

        self.assertEqual(detect_provider_preset("DeepSeek", "https://api.deepseek.com"), "official")
        self.assertEqual(detect_provider_preset("DeepSeek 官方", "https://api.deepseek.com"), "official")
        self.assertEqual(detect_provider_preset("OpenCode Go", "https://opencode.ai/zen/go/v1"), "opencode")
        self.assertEqual(detect_provider_preset("", "https://opencode.ai/go"), "opencode")
        self.assertEqual(detect_provider_preset("自定义中转", "https://api.siliconflow.cn/v1"), "custom")
        self.assertEqual(detect_provider_preset("", "https://my-oneapi-relay.com/v1"), "custom")

    def test_settings_provider_presets_save(self):
        import io
        import tempfile
        from pathlib import Path
        from urllib.parse import urlencode
        from gongkao.db import connect, prepare_user_database
        from gongkao.web.controllers.settings import SettingsController

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "test_settings.sqlite3"
            prepare_user_database(db_path)

            class DummyHandler:
                def __init__(self, db, post_body):
                    self.db_path = db
                    encoded = urlencode(post_body).encode("utf-8")
                    self.rfile = io.BytesIO(encoded)
                    self.headers = {"Content-Length": str(len(encoded))}
                    self.flashes = []

                def page_settings(self, flashes=None):
                    self.flashes = flashes or []

            # 1. 官方预设保存
            handler = DummyHandler(db_path, {
                "mode": "api",
                "provider_preset": "official",
                "api_key": "sk-official-test",
            })
            SettingsController.handle_settings(handler)
            with connect(db_path) as conn:
                row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                self.assertEqual(row["provider_name"], "DeepSeek 官方")
                self.assertEqual(row["api_base_url"], "https://api.deepseek.com")
                self.assertEqual(row["api_key"], "sk-official-test")

            # 2. 留空 key 不覆盖现有 key
            handler = DummyHandler(db_path, {
                "mode": "api",
                "provider_preset": "opencode",
                "api_key": "",  # 留空
            })
            SettingsController.handle_settings(handler)
            with connect(db_path) as conn:
                row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                self.assertEqual(row["provider_name"], "OpenCode Go")
                self.assertEqual(row["api_base_url"], "https://opencode.ai/zen/go/v1")
                self.assertEqual(row["api_key"], "sk-official-test")  # 仍然保留

            # 3. 自定义中转
            handler = DummyHandler(db_path, {
                "mode": "api",
                "provider_preset": "custom",
                "provider_name": "硅基流动",
                "api_base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V3",
                "api_key": "sk-silicon-test",
            })
            SettingsController.handle_settings(handler)
            with connect(db_path) as conn:
                row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                self.assertEqual(row["provider_name"], "硅基流动")
                self.assertEqual(row["api_base_url"], "https://api.siliconflow.cn/v1")
                self.assertEqual(row["model"], "deepseek-ai/DeepSeek-V3")
                self.assertEqual(row["api_key"], "sk-silicon-test")


if __name__ == "__main__":
    unittest.main()



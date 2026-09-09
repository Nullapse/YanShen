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
        self.assertIn("名师标答与参考答案对照", package)

    def test_report_instructions_contains_typo_exemption(self):
        from gongkao.grading import REPORT_INSTRUCTIONS
        self.assertIn("错别字免扣分铁律", REPORT_INSTRUCTIONS)
        self.assertIn("名师标答与参考答案对照", REPORT_INSTRUCTIONS)

    def test_render_grading_report_displays_reference_answers(self):
        from gongkao.grading_pipeline.report import render_grading_report
        result = {
            "score": 75,
            "display_score": 75,
            "display_max_score": 100,
            "overall_summary": "表现良好",
            "revised_answer": "### 小马哥版\n1. 措施一。\n### 白鹭版\n一、小标题。展开。",
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
        self.assertIn("## 机构参考答案对照", report)
        self.assertIn("### 参考答案 · 粉笔", report)
        self.assertIn("粉笔参考答案", report)
        self.assertIn("### 参考答案 · 中公", report)
        self.assertIn("名师解题逻辑 vs 机构参考答案对照审计", report)
        self.assertIn("机构答案自创成语过多", report)

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



import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gongkao.ai_solver import (
    build_solver_prompt,
    extract_solution_parts,
    get_question_solution,
    solve_question_with_ai,
)
from gongkao.db import connect, init_db, prepare_user_database
from gongkao.services.paper_builder import (
    add_paper_question,
    add_question_reference_answer,
    create_custom_paper,
    parse_raw_paper_text,
)
from gongkao.web.application import create_server

SAMPLE_RAW_PAPER = """2024年某省公务员考试《申论》真题（A卷）

【给定资料】
给定资料1：
某市积极实施数字经济创新提质“一号发展工程”，推进政务服务增值化改革。通过构建跨部门协同流转机制，打破部门数据孤岛，实现企业开办“一键直达”。
给定资料2：
某县坚持党建引领，立足特色生态资源禀赋，做优农文旅融合全产业链，吸引广大青年人才回乡创业，激发乡村内生动力。

【作答要求】
一、根据“给定资料1”，概括该市推进政务服务增值化改革的主要举措。（20分）
要求：全面、准确、有条理，字数不超过250字。
参考答案（粉笔）：
一是创新协同流转机制，打破数据壁垒。二是打造一键直达平台，便利企业办件。

二、结合“给定资料2”，谈谈该县推动乡村产业振兴给我们的启示。（30分）
要求：分析透彻，逻辑严密，不超过400字。
"""


class PaperBuilderAndSolverTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.sqlite3")
        init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_raw_paper_text(self):
        parsed = parse_raw_paper_text(SAMPLE_RAW_PAPER)
        self.assertEqual(parsed["paper_name"], "2024年某省公务员考试《申论》真题（A卷）")
        self.assertEqual(parsed["year"], 2024)
        self.assertEqual(parsed["region"], "某省")
        self.assertEqual(parsed["exam_type"], "省考")
        self.assertEqual(parsed["paper_category"], "A卷")
        self.assertEqual(len(parsed["materials"]), 2)
        self.assertEqual(parsed["materials"][0]["material_number"], 1)
        self.assertIn("政务服务增值化改革", parsed["materials"][0]["material_text"])
        self.assertEqual(parsed["materials"][1]["material_number"], 2)
        self.assertIn("激发乡村内生动力", parsed["materials"][1]["material_text"])

        self.assertEqual(len(parsed["questions"]), 2)
        q1 = parsed["questions"][0]
        self.assertEqual(q1["score"], 20)
        self.assertEqual(q1["word_limit"], "不超过250字")
        self.assertIn("主要举措", q1["prompt"])
        self.assertEqual(q1["question_type"], "归纳概括")
        self.assertIsNotNone(q1["reference_answer"])
        self.assertEqual(q1["reference_answer"]["organization"], "粉笔")
        self.assertIn("创新协同流转机制", q1["reference_answer"]["answer_text"])

        q2 = parsed["questions"][1]
        self.assertEqual(q2["score"], 30)
        self.assertEqual(q2["word_limit"], "不超过400字")
        self.assertEqual(q2["question_type"], "综合分析")

    def test_create_custom_paper_and_questions(self):
        with connect(self.db_path) as conn:
            materials = [
                {"material_number": 1, "title": "资料1", "material_text": "资料1内容"},
                {"material_number": 2, "title": "资料2", "material_text": "资料2内容"},
            ]
            paper_id = create_custom_paper(
                conn=conn,
                paper_name="2025年新版测试套卷",
                year=2025,
                region="浙江",
                exam_type="省考申论",
                paper_category="A卷",
                materials=materials,
            )
            self.assertGreater(paper_id, 0)

            # verify materials
            mats = conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (paper_id,),
            ).fetchall()
            self.assertEqual(len(mats), 2)
            self.assertEqual(mats[0]["material_number"], 1)
            self.assertEqual(mats[0]["content"], "资料1内容")

            # add question
            q_id = add_paper_question(
                conn=conn,
                paper_id=paper_id,
                question_type="归纳概括",
                prompt="概括主要做法",
                requirements="全面准确",
                word_limit="不超过200字",
                score=20,
                question_number=1,
            )
            self.assertGreater(q_id, 0)

            # add reference answer
            ref_id = add_question_reference_answer(
                conn=conn,
                question_id=q_id,
                organization="粉笔",
                answer_text="一、党建引领；二、要素集聚。",
                score=20,
            )
            self.assertGreater(ref_id, 0)

            ref_row = conn.execute("SELECT * FROM reference_answers WHERE id = ?", (ref_id,)).fetchone()
            self.assertEqual(ref_row["organization"], "粉笔")
            self.assertEqual(ref_row["canonical_organization"], "粉笔")

    def test_ai_solver_prompt_and_flow(self):
        with connect(self.db_path) as conn:
            paper_id = create_custom_paper(
                conn=conn,
                paper_name="2025年AI解题专项卷",
                year=2025,
                region="国考",
                exam_type="申论",
                materials=[{"material_number": 1, "material_text": "给定资料1重点阐述绿色低碳发展转型。"}],
            )
            q_id = add_paper_question(
                conn=conn,
                paper_id=paper_id,
                question_type="归纳概括",
                prompt="请概括材料中的绿色低碳转型举措。",
                requirements="要点齐全，不超过200字。",
                word_limit="不超过200字",
                score=20,
            )
            add_question_reference_answer(
                conn=conn,
                question_id=q_id,
                organization="某机构",
                answer_text="机构答案自编成语很多，过度发挥。",
            )

            # test prompt generation
            prompt = build_solver_prompt(conn, q_id)
            self.assertIn("绿色低碳发展转型", prompt)
            self.assertIn("请概括材料中的绿色低碳转型举措", prompt)
            self.assertIn("小马哥版", prompt)
            self.assertIn("白鹭版", prompt)
            self.assertIn("绝不能直接作为唯一标准", prompt)

            # Mock AI completion
            fake_ai_output = """### 题目深度剖析
本题为归纳概括题，作答范围为材料1。

### 小马哥原词流标答
1. 聚焦绿色低碳发展。
2. 加快产业结构升级转型。

### 白鹭提炼流标答
一、统筹产业升级，培育绿色动能。
二、健全机制保障，落实双碳目标。

### 客观采分点与赋分清单
- 绿色低碳（2分）
- 转型（2分）
- 结构升级（2分）

### 机构参考答案审计纠错
机构答案过度使用成语，脱离材料原词。
"""
            with patch("gongkao.ai_solver.chat_completion", return_value=(fake_ai_output, {})):
                sol = solve_question_with_ai(conn, q_id, model_name="test-model")
                self.assertIsNotNone(sol)
                self.assertIn("聚焦绿色低碳发展", sol["xiaomage_answer"])
                self.assertIn("统筹产业升级", sol["bailu_answer"])
                self.assertIn("绿色低碳（2分）", sol["scoring_points"])
                self.assertIn("过度使用成语", sol["reference_audit"])

                cached = get_question_solution(conn, q_id)
                self.assertEqual(cached["model_name"], "test-model")
                self.assertEqual(cached["xiaomage_answer"], sol["xiaomage_answer"])

    def test_personal_paper_dynamic_workflow(self):
        """Test user's exact flow:
        - 1-sentence paper info
        - Sequential materials added via [+]
        - Sequential questions added via [+], each with prompt + 1 reference answer
        """
        with connect(self.db_path) as conn:
            # 1. User inputs a single sentence paper info
            paper_res = create_custom_paper(
                conn=conn,
                paper_name="2024申论模拟考个人练习一",
                materials=[
                    "第一篇给定资料：某市推行网格化基层治理体系，提升服务质效。",
                    "第二篇给定资料：强化科技赋能与数字底座建设，解决群众急难愁盼。",
                ],
            )
            paper_id = int(paper_res)
            self.assertGreater(paper_id, 0)

            # verify materials in DB
            mats = conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (paper_id,),
            ).fetchall()
            self.assertEqual(len(mats), 2)
            self.assertEqual(mats[0]["material_number"], 1)
            self.assertEqual(mats[0]["title"], "给定资料1")
            self.assertEqual(mats[1]["material_number"], 2)
            self.assertEqual(mats[1]["title"], "给定资料2")

            # 2. Add Question 1 with prompt and 1 reference answer
            q1_res = add_paper_question(
                conn=conn,
                paper_id=paper_id,
                prompt="根据“给定资料1”，概括网格化基层治理的主要做法。要求：准确、全面，不超过200字。（20分）",
            )
            q1_id = int(q1_res)
            add_question_reference_answer(
                conn=conn,
                question_id=q1_id,
                organization="参考答案",
                answer_text="参考答案：推行网格化体系，提升服务效能。",
            )

            # 3. Add Question 2 with prompt and 1 reference answer
            q2_res = add_paper_question(
                conn=conn,
                paper_id=paper_id,
                prompt="根据“给定资料2”，谈谈科技赋能对基层治理的启示。不超过300字。（30分）",
            )
            q2_id = int(q2_res)
            add_question_reference_answer(
                conn=conn,
                question_id=q2_id,
                organization="参考答案",
                answer_text="参考答案：数字底座建设与精准服务。",
            )

            # Verify questions and reference answers in DB
            q1_row = conn.execute("SELECT * FROM questions WHERE id = ?", (q1_id,)).fetchone()
            self.assertEqual(q1_row["question_number"], 1)
            self.assertIn("全面", q1_row["requirements"])
            self.assertIn("不超过200字", q1_row["word_limit"])

            ref1 = conn.execute("SELECT * FROM reference_answers WHERE question_id = ?", (q1_id,)).fetchall()
            self.assertEqual(len(ref1), 1)
            self.assertEqual(ref1[0]["organization"], "参考答案")

            ref2 = conn.execute("SELECT * FROM reference_answers WHERE question_id = ?", (q2_id,)).fetchall()
            self.assertEqual(len(ref2), 1)
            self.assertEqual(ref2[0]["organization"], "参考答案")

    def test_web_routes_and_paper_creation(self):
        server = create_server(port=0, db_path=self.db_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"

            # 1. GET /papers/new
            with urlopen(f"{base}/papers/new", timeout=10) as res:
                self.assertEqual(res.status, 200)
                html = res.read().decode("utf-8")
                self.assertIn("录入新套卷", html)
                self.assertIn("新增给定资料", html)
                self.assertIn("新增题目", html)

            # 2. POST /papers/parse-text
            req_data = json.dumps({"raw_text": SAMPLE_RAW_PAPER}).encode("utf-8")
            req = Request(
                f"{base}/papers/parse-text",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as res:
                self.assertEqual(res.status, 200)
                resp_json = json.loads(res.read().decode("utf-8"))
                self.assertTrue(resp_json["ok"])
                self.assertEqual(resp_json["parsed"]["year"], 2024)

            # 3. POST /papers/new
            form_payload = urlencode({
                "paper_name": "Web创建测试卷",
                "year": "2024",
                "region": "浙江",
                "exam_type": "申论",
                "paper_category": "B卷",
                "materials_text": "【给定资料1】\n第一则材料正文。\n【给定资料2】\n第二则材料正文。",
            }).encode("utf-8")
            req = Request(f"{base}/papers/new", data=form_payload, method="POST")
            with urlopen(req, timeout=10) as res:
                self.assertEqual(res.status, 200)
                final_url = res.geturl()
                self.assertIn("/papers/", final_url)
                paper_id = int(final_url.rstrip("/").split("/")[-1])

            # 4. GET /papers/{id}/questions/new
            with urlopen(f"{base}/papers/{paper_id}/questions/new", timeout=10) as res:
                self.assertEqual(res.status, 200)
                html = res.read().decode("utf-8")
                self.assertIn("添加题目", html)
                self.assertIn("材料1", html)

            # 5. POST /papers/{id}/questions/new
            q_payload = urlencode({
                "question_type": "归纳概括",
                "prompt": "概括第一则材料核心要点",
                "requirements": "准确全面",
                "word_limit": "200字",
                "score": "20",
                "ref_organization": "华图",
                "ref_answer_text": "华图参考答案：注重基层治理协同。",
            }).encode("utf-8")
            req = Request(f"{base}/papers/{paper_id}/questions/new", data=q_payload, method="POST")
            with urlopen(req, timeout=10) as res:
                self.assertEqual(res.status, 200)
                final_url = res.geturl()
                self.assertIn("/questions/", final_url)
                question_id = int(final_url.rstrip("/").split("/")[-1])

            # 6. POST /questions/{id}/references/new
            ref_payload = urlencode({
                "organization": "粉笔",
                "answer_text": "粉笔参考答案：强化数字赋能与制度协同。",
                "score": "20",
            }).encode("utf-8")
            req = Request(f"{base}/questions/{question_id}/references/new", data=ref_payload, method="POST")
            with urlopen(req, timeout=10) as res:
                self.assertEqual(res.status, 200)

            # 7. POST /questions/{id}/solve (Mock chat_completion)
            mock_solver_text = """### 题目深度剖析
考查归纳概括能力。

### 小马哥原词流标答
1. 第一则材料正文。
2. 基层治理协同。

### 白鹭提炼流标答
一、坚持协同治理。
二、提升服务能级。

### 客观采分点与赋分清单
- 协同（5分）
- 基层（5分）

### 机构参考答案审计纠错
华图与粉笔均抓住了协同要点，但华图缺少数据支撑。
"""
            with patch("gongkao.ai_solver.chat_completion", return_value=(mock_solver_text, {})):
                # Mock API key in ai_settings
                with connect(self.db_path) as conn:
                    conn.execute("UPDATE ai_settings SET api_key = 'test-key' WHERE id = 1")

                req = Request(f"{base}/questions/{question_id}/solve", data=b"", method="POST")
                with urlopen(req, timeout=10) as res:
                    self.assertEqual(res.status, 200)

            # 8. GET /questions/{id} to verify AI solution rendered
            with urlopen(f"{base}/questions/{question_id}", timeout=10) as res:
                self.assertEqual(res.status, 200)
                html = res.read().decode("utf-8")
                self.assertIn("Shenlun.skill 体系自主解题", html)
                self.assertIn("小马哥原词流标答", html)
                self.assertIn("白鹭提炼流标答", html)
                self.assertIn("客观采分点清单", html)
                self.assertIn("参考答案审计纠错", html)
                self.assertIn("+ 录入新参考答案", html)

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

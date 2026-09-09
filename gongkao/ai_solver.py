"""AI autonomous question solver module based on Shenlun.skill methodology.
Empowers the agent to solve questions independently from raw materials,
generating dual-stream master answers and scoring rubrics without being misled
by inaccurate reference answers.
"""

import json
import re
from typing import Any, Dict, List, Optional

from .ai import chat_completion
from .db import connect
from .grading import word_limit_budget
from .grading_pipeline.evidence import _material_text


from pathlib import Path


def build_solver_prompt(
    question_or_conn: Any,
    materials_or_qid: Any = None,
    references: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if hasattr(question_or_conn, "execute"):
        conn = question_or_conn
        question_id = materials_or_qid
        q_row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not q_row:
            raise ValueError(f"题目 ID {question_id} 不存在")
        question = dict(q_row)
        paper_id = question.get("paper_id")
        materials = []
        if paper_id:
            mats = conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (paper_id,),
            ).fetchall()
            materials = [dict(m) for m in mats]
        refs = conn.execute(
            "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id",
            (question_id,),
        ).fetchall()
        references = [dict(r) for r in refs]
    else:
        question = question_or_conn
        materials = materials_or_qid or []

    word_limit = question.get("word_limit") or "未限制"
    budget = word_limit_budget(word_limit)

    ref_section = ""
    if references:
        ref_items = []
        for index, ref in enumerate(references, start=1):
            ref_items.append(
                f"### 参考答案 {index}（来源/机构：{ref.get('organization', '未标注')}）\n"
                f"{ref.get('answer_text', '').strip()}\n"
                f"采分点解析（如有）：{ref.get('scoring_points', '无')}"
            )
        ref_section = (
            f"\n\n## 候选参考答案（用户/机构提供，共 {len(references)} 份）：\n"
            "【警示提示】：以下参考答案仅作为待审计与对比样本，可能存在脱离材料自造假概括词、写空话套话或漏掉核心要点等问题，"
            "绝不能直接作为唯一标准！你必须首先依据给定资料与 Shenlun.skill 名师体系自己独立做题！\n\n"
            + "\n\n".join(ref_items)
        )
    else:
        ref_section = "\n\n## 候选参考答案：\n本题暂未提供任何外部参考答案。你必须完全基于材料原文与题干要求独立自主完成作答并提炼采分点。"

    mat_text = ""
    if materials:
        mat_text = "\n\n".join(
            f"### {m.get('title') or ('材料' + str(m.get('material_number', 1)))}\n{m.get('content') or m.get('material_text', '')}"
            for m in materials
        )
    else:
        mat_text = question.get("materials") or "未提供材料"

    prompt = f"""你是一名精通申论命题意图与阅卷规则的顶尖名师。请根据 Shenlun.skill 体系要求，抛弃一切脱离材料的空话套话，**严格依据给定资料独立自主做题**。

【题型与任务】：
- 题型：{question.get('question_type')}
- 设问（题干）：{question.get('prompt')}
- 要求：{question.get('requirements')}
- 字数预算：{word_limit}（建议区间：{budget.get('suggested_min', '未指定')}—{budget.get('suggested_max', '未指定')}字，硬限制：{budget.get('hard_max_exclusive', '无')}字）

【给定资料】：
{mat_text}
{ref_section}

【作答与解题执行铁律（Shenlun.skill）】：
1. 材料至上，原词为王：申论采分点100%在材料中。严格遵循8类信号词（转折、递进、结论、最高级等）抓取要点。禁止自创材料中没有的四字成语或假大空政论词汇。
2. 动宾结构极简去水：要点严格采用“动词短语+具体对象+成效”，坚决剔除“进一步”“加大力度”“在……过程中”等冗余字眼。
3. 题型专项化：
   - 若是综合分析题，严格按四子类逻辑框架组织（词句理解：表层含义→深层内涵→实质对策；观点评析：亮明态度→辩证分析→结论；现象分析：现象概括→深层原因→治理对策；关系分析：本质关系→双向互动→每点回扣题干词）。
   - 若是大作文，按袁东框架进行分论点三步搜索法（题干关键词→材料高频词→前面小题提炼词），确定主题类型并拟定论点。
4. 阅卷人采分点加分制：列出客观采分点，每个采分点必须附带材料原文原句连续引用（逐字来自材料）。
5. 机构参考答案审计：客观对比提供的参考答案，指出其是否自造概括词、是否写了套话、是否漏掉了材料核心原词。

请按以下格式清晰输出：

## 一、题型任务与解题逻辑
说明本题题型细分特征、解题逻辑链条与字数规划。

## 二、官方阅卷客观采分点清单（加分制）
以编号列表列出所有采分点：
1. 【采分点名称】（满分X分）：规范表达。材料依据：材料X“……”（连续原文）。

## 三、标准答案·小马哥版（极简·原词直抄流）
完全使用材料原词，动宾短语+具体对象+成效，单条要点≤40字，严格去水，字数控制在要求预算内。

## 四、标准答案·白鹭版（提炼·轻串联流）
从材料已有词汇中精炼4/6/8字前置概括小标题，逻辑归并3—5条，每条形成“小标题+举措展开->成效”，轻串联。

## 五、参考答案客观审计与避坑指南
（若提供了参考答案，逐一指出其自创套话与漏点；若无则总结考生容易踩的误区）。
"""
    return prompt


def extract_solution_parts(solution_text: str) -> Dict[str, Any]:
    text = str(solution_text or "").strip()

    # Extract Xiaoma answer
    xiaoma_match = re.search(
        r"(?:##+\s*(?:[三3一1][、.．]?)?\s*(?:标准答案[·\s]*)?小马哥[^\n]*\n)(.*?)(?=\n##+\s*|$)",
        text,
        re.S | re.I,
    )
    xiaoma_answer = xiaoma_match.group(1).strip() if xiaoma_match else ""

    # Extract Bailu answer
    bailu_match = re.search(
        r"(?:##+\s*(?:[四4二2][、.．]?)?\s*(?:标准答案[·\s]*)?白鹭[^\n]*\n)(.*?)(?=\n##+\s*|$)",
        text,
        re.S | re.I,
    )
    bailu_answer = bailu_match.group(1).strip() if bailu_match else ""

    # Extract points
    points_match = re.search(
        r"(?:##+\s*(?:[二2][、.．]?)?\s*(?:客观采分点|采分点|官方阅卷)[^\n]*\n)(.*?)(?=\n##+\s*|$)",
        text,
        re.S | re.I,
    )
    points_text = points_match.group(1).strip() if points_match else ""

    # Extract Reference Audit
    audit_match = re.search(
        r"(?:##+\s*(?:[五5][、.．]?)?\s*[^\n]*(?:参考答案[^\n]*审计|审计[^\n]*纠错|避坑指南)[^\n]*\n)(.*?)(?=\n##+\s*|$)",
        text,
        re.S | re.I,
    )
    reference_audit = audit_match.group(1).strip() if audit_match else ""

    return {
        "solution_text": text,
        "xiaomage_answer": xiaoma_answer,
        "xiaoma_answer": xiaoma_answer,
        "bailu_answer": bailu_answer,
        "scoring_points": points_text,
        "reference_audit": reference_audit,
    }


def get_question_solution(conn, question_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM ai_question_solutions WHERE question_id = ? ORDER BY id DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "question_id": row["question_id"],
        "model_name": row["model_name"],
        "xiaomage_answer": row["xiaomage_answer"],
        "bailu_answer": row["bailu_answer"],
        "scoring_points": row["scoring_points"],
        "reference_audit": row["reference_audit"],
        "raw_response": row["raw_response"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _solve_with_conn(
    conn,
    question_id: int,
    model_name: Optional[str],
    chat_completion_func,
    deep_thinking: bool,
) -> Dict[str, Any]:
    question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not question:
        raise ValueError(f"题目 ID {question_id} 不存在")
    paper_id = question["paper_id"]
    materials = []
    if paper_id:
        materials = conn.execute(
            "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
            (paper_id,),
        ).fetchall()
    references = conn.execute(
        "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id",
        (question_id,),
    ).fetchall()
    settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
    if not settings:
        raise ValueError("AI 设置不存在")

    prompt = build_solver_prompt(dict(question), [dict(m) for m in materials], [dict(r) for r in references])
    request_options = {}
    if deep_thinking:
        request_options["thinking"] = "enabled"
    func = chat_completion_func or chat_completion
    content, raw = func(settings, prompt, request_options=request_options)

    parts = extract_solution_parts(content)
    actual_model = model_name or settings["model"] or "AI"

    conn.execute(
        """
        INSERT INTO ai_question_solutions (
            question_id, model_name, xiaomage_answer, bailu_answer,
            scoring_points, reference_audit, raw_response
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            actual_model,
            parts["xiaomage_answer"],
            parts["bailu_answer"],
            parts["scoring_points"],
            parts["reference_audit"],
            content,
        ),
    )
    return {
        "model_name": actual_model,
        "xiaomage_answer": parts["xiaomage_answer"],
        "bailu_answer": parts["bailu_answer"],
        "scoring_points": parts["scoring_points"],
        "reference_audit": parts["reference_audit"],
        "solution_text": content,
    }


def solve_question_with_ai(
    db_or_conn: Any,
    question_id: int,
    model_name: Optional[str] = None,
    chat_completion_func=None,
    deep_thinking: bool = True,
) -> Dict[str, Any]:
    if isinstance(db_or_conn, (str, Path)):
        with connect(db_or_conn) as conn:
            return _solve_with_conn(conn, question_id, model_name, chat_completion_func, deep_thinking)
    else:
        return _solve_with_conn(db_or_conn, question_id, model_name, chat_completion_func, deep_thinking)

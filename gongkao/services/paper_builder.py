"""Service for creating custom papers, questions, materials, reference answers,
and parsing raw exam paper text into structured objects.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..organizations import canonicalize_organization
from ..taxonomy import classify_question_type

KNOWN_REGIONS = [
    "国家", "浙江", "江苏", "山东", "广东", "四川", "湖北", "湖南", "河南", "河北",
    "北京", "上海", "重庆", "天津", "安徽", "福建", "江西", "陕西", "山西", "辽宁",
    "吉林", "黑龙江", "内蒙古", "广西", "海南", "贵州", "云南", "西藏", "甘肃", "青海",
    "宁夏", "新疆", "兵团", "全国",
]

KNOWN_EXAM_TYPES = [
    "国考", "国家公务员考试", "省考", "选调生", "选调", "事业单位", "军队文职",
    "公安联考", "三支一扶", "联考",
]

CHINESE_NUMS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
}


def _parse_num(val: str, default: int = 1) -> int:
    val = str(val or "").strip()
    if val.isdigit():
        return int(val)
    if val in CHINESE_NUMS:
        return CHINESE_NUMS[val]
    return default


def _content_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def _make_paper_code(year: int, exam_type: str, region: str, paper_name: str, paper_category: str) -> str:
    raw = f"{year}-{exam_type}-{region}-{paper_name}-{paper_category}"
    return "P-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _make_question_code(paper_code: str, question_number: int, prompt: str) -> str:
    raw = f"{paper_code}-Q{question_number}-{prompt[:40]}"
    return "Q-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_raw_paper_text(raw_text: str) -> Dict[str, Any]:
    """Parse raw text of a whole 申论 exam into structured paper, materials, and questions."""
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return {
            "year": 2024,
            "region": "国家",
            "exam_type": "国考",
            "paper_category": "",
            "paper_name": "",
            "materials": [],
            "questions": [],
        }

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    first_few_lines = "\n".join(lines[:5])

    # 1. Year
    year = 2024
    year_match = re.search(r"(20\d{2})年?", first_few_lines)
    if year_match:
        year = int(year_match.group(1))

    # 2. Region
    region = "国家"
    for r in KNOWN_REGIONS:
        if r in first_few_lines:
            region = r
            break
    if region == "国家" and not ("国家" in first_few_lines or "国考" in first_few_lines):
        reg_match = re.search(r"(?:^|年|/|\s)([^\d\s\n《》（）()年]{1,6}(?:省|市|自治区))", first_few_lines)
        if reg_match:
            region = reg_match.group(1).lstrip("年")

    # 3. Exam Type
    exam_type = "国考" if region == "国家" else "省考"
    for e in KNOWN_EXAM_TYPES:
        if e in first_few_lines:
            exam_type = "国考" if "国考" in e or "国家" in e else ("省考" if "省考" in e else e)
            break

    # 4. Paper Category
    category_patterns = [
        r"(副省级|地市级|行政执法类|执法类|综合管理类|县乡类|乡镇类|综合类|A卷|B卷|C卷|申论A|申论B|申论C)",
    ]
    paper_category = ""
    for pat in category_patterns:
        cat_match = re.search(pat, first_few_lines, re.I)
        if cat_match:
            paper_category = cat_match.group(1)
            break

    # 5. Paper Name
    first_line = lines[0] if lines else ""
    if "申论" in first_line or "真题" in first_line or "考试" in first_line or "卷" in first_line:
        paper_name = first_line.strip("【】《》[]()")
    else:
        cat_suffix = f"（{paper_category}）" if paper_category else ""
        paper_name = f"{year}年{region}{exam_type}《申论》真题{cat_suffix}"

    # 6. Split Materials and Questions sections
    question_sec_match = re.search(
        r"(?:^|\n)\s*【?\s*(?:(?:二|三|2|3)[、.．]\s*)?(?:作答要求|作答题目|注意事项与作答要求|题目要求|试题部分|申论试题|题目)\s*】?\s*(?:\n|$)",
        text,
    )
    if question_sec_match:
        materials_raw = text[: question_sec_match.start()]
        questions_raw = text[question_sec_match.end() :]
    else:
        first_q_match = re.search(r"(?:^|\n)\s*(?:(?:第[0-9一二两三四五六七八九十]+题|问题[0-9一二两三四五六七八九十]+|[1-9一二三四五六七八九十][、.．]))", text)
        if first_q_match:
            materials_raw = text[: first_q_match.start()]
            questions_raw = text[first_q_match.start() :]
        else:
            materials_raw = text
            questions_raw = ""

    # 7. Extract materials
    materials: List[Dict[str, Any]] = []
    mat_splits = list(
        re.finditer(
            r"(?:^|\n)\s*(?:【?\s*(?:给定)?(?:资料|材料)\s*([0-9一二两三四五六七八九十]+)\s*】?[:：\s]*)(.*?)(?=(?:\n\s*【?\s*(?:给定)?(?:资料|材料)\s*[0-9一二两三四五六七八九十]+\s*】?[:：\s]*)|$)",
            materials_raw,
            re.S,
        )
    )
    if mat_splits:
        for match in mat_splits:
            num = _parse_num(match.group(1), len(materials) + 1)
            content = match.group(2).strip()
            if content:
                materials.append({
                    "material_number": num,
                    "title": f"材料{num}",
                    "content": content,
                    "material_text": content,
                })
    else:
        clean_mat = re.sub(r"^(?:一[、.．]\s*)?(?:给定)?(?:资料|材料)\s*", "", materials_raw).strip()
        if clean_mat:
            materials.append({
                "material_number": 1,
                "title": "材料1",
                "content": clean_mat,
                "material_text": clean_mat,
            })

    # 8. Extract questions
    questions: List[Dict[str, Any]] = []
    q_splits = list(
        re.finditer(
            r"(?:^|\n)\s*(?:(?:第([0-9一二两三四五六七八九十]+)题|问题([0-9一二两三四五六七八九十]+)|题号\s*[:：]?\s*([0-9]+)|([0-9一二两三四五六七八九十]+)\s*[、.．]))[:：\s]*(.*?)(?=(?:\n\s*(?:(?:第[0-9一二两三四五六七八九十]+题|问题[0-9一二两三四五六七八九十]+|题号\s*[:：]?\s*[0-9]+|[0-9一二两三四五六七八九十]+\s*[、.．]))[:：\s]*)|$)",
            questions_raw,
            re.S,
        )
    )

    for match in q_splits:
        raw_num = match.group(1) or match.group(2) or match.group(3) or match.group(4)
        q_num = _parse_num(raw_num, len(questions) + 1)
        q_block = match.group(5).strip()
        if not q_block:
            continue

        # Extract requirements
        req_match = re.search(r"(?:要求|作答要求|作答规范)[:：\s]*(.*?)(?=(?:\n\s*(?:参考答案|标答|机构答案|答案))|$)", q_block, re.S)
        if req_match:
            prompt = q_block[: req_match.start()].strip()
            requirements = req_match.group(1).strip()
            rest = q_block[req_match.end() :]
        else:
            prompt = q_block
            requirements = "全面、准确、有条理。"
            rest = ""

        # Extract reference answer if present
        ref_match = re.search(
            r"(?:参考答案|标答|机构答案|答案)(?:[（(]([^）)]+)[）)])?[:：\s]*(.*)",
            rest or prompt,
            re.S,
        )
        reference_answer = ""
        ref_org = "参考答案"
        if ref_match:
            ref_org = ref_match.group(1) or "参考答案"
            reference_answer = ref_match.group(2).strip()
            if rest:
                rest = rest[: ref_match.start()].strip()
            else:
                prompt = prompt[: ref_match.start()].strip()

        # Word limit extraction
        word_limit = ""
        limit_match = re.search(r"((?:不超过|在|不少于|控制在)?\s*[0-9]+(?:[-—~～至][0-9]+)?\s*字(?:以内|以下)?)", q_block)
        if limit_match:
            word_limit = limit_match.group(1).strip()

        # Score extraction
        score = 20
        score_match = re.search(r"[（(]\s*([0-9]+)\s*分\s*[）)]", prompt + " " + requirements)
        if score_match:
            score = int(score_match.group(1))

        # Title
        title = f"第{q_num}题"

        # Determine question type
        q_type, _ = classify_question_type(prompt, requirements)

        # Matched materials for this question
        mat_numbers = []
        for m in re.finditer(r"(?:给定)?(?:资料|材料)\s*([0-9一二两三四五六七八九十]+)", prompt):
            mat_numbers.append(_parse_num(m.group(1)))

        # Associated materials text
        q_materials_text = ""
        if mat_numbers:
            q_materials_text = "\n\n".join(
                f"{m['title']}\n{m['content']}"
                for m in materials
                if m["material_number"] in mat_numbers
            )
        if not q_materials_text and materials:
            matching = [m for m in materials if m["material_number"] == q_num]
            if matching:
                q_materials_text = f"{matching[0]['title']}\n{matching[0]['content']}"
            else:
                q_materials_text = "\n\n".join(f"{m['title']}\n{m['content']}" for m in materials)

        ref_dict = None
        if reference_answer:
            ref_dict = {
                "organization": ref_org or "参考答案",
                "answer_text": reference_answer,
                "score": score,
            }

        questions.append({
            "question_number": q_num,
            "title": title,
            "question_type": q_type,
            "prompt": prompt,
            "requirements": requirements,
            "word_limit": word_limit,
            "score": score,
            "materials": q_materials_text,
            "material_numbers": mat_numbers,
            "reference_answer": ref_dict,
            "ref_answer_text": reference_answer,
            "reference_org": ref_org,
        })

    return {
        "year": year,
        "region": region,
        "exam_type": exam_type,
        "paper_category": paper_category,
        "paper_name": paper_name,
        "materials": materials,
        "questions": questions,
    }


class EntityResult(int):
    """An integer ID that can also be unpacked as (id, code)."""
    code: str

    def __new__(cls, val, code=""):
        obj = super().__new__(cls, val)
        obj.code = str(code)
        return obj

    def __iter__(self):
        yield int(self)
        yield self.code


def create_custom_paper(conn, paper_data: Dict[str, Any] | None = None, **kwargs) -> EntityResult:
    """Insert or update a custom paper with its materials.

    Returns EntityResult which acts as paper_id (int) and unpacks as (paper_id, paper_code).
    """
    data = dict(paper_data or {})
    data.update(kwargs)
    paper_name = (data.get("paper_name") or "个人申论练习卷").strip()
    year_val = data.get("year")
    if not year_val:
        m = re.search(r"(20\d\d)", paper_name)
        year = int(m.group(1)) if m else 2024
    else:
        year = int(year_val)

    region = (data.get("region") or "").strip()
    if not region:
        for r in KNOWN_REGIONS:
            if r in paper_name:
                region = r
                break
        if not region:
            region = "个人练习"

    exam_type = (data.get("exam_type") or "").strip()
    if not exam_type:
        exam_type = "申论"

    paper_category = (data.get("paper_category") or "").strip()
    if not paper_category:
        paper_category = "个人定制"

    source_province = (data.get("source_province") or "").strip()
    zhejiang_relevance = max(1, min(5, int(data.get("zhejiang_relevance") or 3)))
    source_url = (data.get("source_url") or "").strip()
    source_kind = (data.get("source_kind") or "").strip()
    source_note = (data.get("source_note") or "").strip()
    if not source_note:
        source_note = (
            f"URL 自动导入（{source_kind or '外部来源'}）：{source_url}；参考答案未核验"
            if source_url
            else "用户录入"
        )
    elif source_url and source_url not in source_note:
        source_note = f"{source_note} 来源 URL：{source_url}"

    paper_code = _make_paper_code(year, exam_type, region, paper_name, paper_category)

    conn.execute(
        """
        INSERT INTO papers (
            paper_code, paper_name, paper_category, exam_type, year, region,
            source_province, zhejiang_relevance, source_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_code) DO UPDATE SET
            paper_name = excluded.paper_name,
            paper_category = excluded.paper_category,
            exam_type = excluded.exam_type,
            year = excluded.year,
            region = excluded.region,
            source_province = excluded.source_province,
            zhejiang_relevance = excluded.zhejiang_relevance,
            source_note = excluded.source_note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            paper_code,
            paper_name,
            paper_category,
            exam_type,
            year,
            region,
            source_province,
            zhejiang_relevance,
            source_note,
        ),
    )
    paper_id = conn.execute("SELECT id FROM papers WHERE paper_code = ?", (paper_code,)).fetchone()["id"]

    # Materials
    materials = data.get("materials") or []
    for index, mat in enumerate(materials, start=1):
        if isinstance(mat, str):
            num = index
            title = f"给定资料{num}"
            content = mat.strip()
        elif isinstance(mat, dict):
            num = int(mat.get("material_number") or index)
            title = (mat.get("title") or f"给定资料{num}").strip()
            content = (mat.get("content") or mat.get("material_text") or "").strip()
        else:
            continue
        if not content:
            continue
        conn.execute(
            """
            INSERT INTO paper_materials (paper_id, material_number, title, content)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id, material_number) DO UPDATE SET
                title = excluded.title,
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (paper_id, num, title, content),
        )

    return EntityResult(paper_id, paper_code)


def add_paper_question(conn, paper_id: int, question_data: Dict[str, Any] | None = None, **kwargs) -> EntityResult:
    """Add a question under a paper and optionally attach an initial reference answer.

    Returns EntityResult which acts as question_id (int) and unpacks as (question_id, question_code).
    """
    data = dict(question_data or {})
    data.update(kwargs)

    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not paper:
        raise ValueError(f"试卷 ID {paper_id} 不存在")

    paper_code = paper["paper_code"]
    paper_name = paper["paper_name"]
    paper_category = paper["paper_category"]
    exam_type = paper["exam_type"]
    year = paper["year"]
    region = paper["region"]
    source_province = paper["source_province"]
    zhejiang_relevance = paper["zhejiang_relevance"]

    question_number = int(data.get("question_number") or 1)
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("题干设问不能为空")

    requirements = (data.get("requirements") or "").strip()
    if not requirements:
        req_match = re.search(r"(?:要求|作答要求)[：:]([^\n]+)", prompt)
        if req_match:
            requirements = req_match.group(1).strip()
        else:
            requirements = "全面、准确、有条理。"

    title = (data.get("title") or f"第{question_number}题").strip()
    word_limit = (data.get("word_limit") or "").strip()
    if not word_limit:
        wl_match = re.search(r"(不超过\d+字|\d+-\d+字|\d+字左右|\d+字以内)", prompt + " " + requirements)
        if wl_match:
            word_limit = wl_match.group(1).strip()

    # Materials: if not provided directly, compose from paper_materials
    materials = (data.get("materials") or data.get("materials_scope") or "").strip()
    if not materials:
        paper_mats = conn.execute(
            "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
            (paper_id,),
        ).fetchall()
        materials = "\n\n".join(f"{m['title']}\n{m['content']}" for m in paper_mats)

    question_type = (data.get("question_type") or "").strip()
    if not question_type:
        question_type, _ = classify_question_type(prompt, requirements)

    score_val = data.get("score")
    if score_val:
        score = int(score_val)
    else:
        score_match = re.search(r"(\d+)\s*分", prompt + " " + requirements)
        score = int(score_match.group(1)) if score_match else 20
    if score and f"{score}分" not in requirements and f"{score}分" not in prompt:
        requirements = f"{requirements}（分值：{score}分）"
    original_text = (data.get("original_text") or prompt).strip()
    source_url = (data.get("source_url") or "").strip()
    source_kind = (data.get("source_kind") or "").strip()
    source_note = (data.get("source_note") or "").strip()
    is_full_original = 1 if data.get("is_full_original", True) else 0
    hash_val = _content_hash(f"{prompt} {requirements} {word_limit}")
    question_code = _make_question_code(paper_code, question_number, prompt)

    conn.execute(
        """
        INSERT INTO questions (
            question_code, paper_id, paper_name, paper_category, question_number,
            exam_type, year, region, source_province, zhejiang_relevance,
            question_type, title, prompt, materials, requirements, word_limit,
            original_text, source_url, source_kind, is_full_original, content_hash, source_note
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(question_code) DO UPDATE SET
            paper_name = excluded.paper_name,
            paper_category = excluded.paper_category,
            question_number = excluded.question_number,
            exam_type = excluded.exam_type,
            year = excluded.year,
            region = excluded.region,
            source_province = excluded.source_province,
            zhejiang_relevance = excluded.zhejiang_relevance,
            question_type = excluded.question_type,
            title = excluded.title,
            prompt = excluded.prompt,
            materials = excluded.materials,
            requirements = excluded.requirements,
            word_limit = excluded.word_limit,
            original_text = excluded.original_text,
            source_url = excluded.source_url,
            source_kind = excluded.source_kind,
            is_full_original = excluded.is_full_original,
            content_hash = excluded.content_hash,
            source_note = excluded.source_note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            question_code,
            paper_id,
            paper_name,
            paper_category,
            question_number,
            exam_type,
            year,
            region,
            source_province,
            zhejiang_relevance,
            question_type,
            title,
            prompt,
            materials,
            requirements,
            word_limit,
            original_text,
            source_url,
            source_kind,
            is_full_original,
            hash_val,
            source_note,
        ),
    )
    q_row = conn.execute("SELECT id FROM questions WHERE question_code = ?", (question_code,)).fetchone()
    question_id = q_row["id"]

    if source_url:
        source_provider = (data.get("source_provider") or ("粉笔" if "fenbi" in source_kind.lower() else "外部来源")).strip()
        conn.execute(
            """
            INSERT INTO question_sources (question_id, provider, source_name, source_path, source_url, section)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(question_id, provider, source_name, source_path, source_url) DO UPDATE SET
                section = excluded.section,
                extracted_at = CURRENT_TIMESTAMP
            """,
            (
                question_id,
                source_provider or "外部来源",
                source_provider or "外部来源",
                source_kind,
                source_url,
                title,
            ),
        )

    # Initial reference answer if supplied
    ref_value = data.get("reference_answer")
    ref_ans = ""
    ref_org = (data.get("reference_org") or data.get("ref_organization") or "参考答案").strip()
    ref_scoring_points = (data.get("scoring_points") or "").strip()
    ref_notes = (data.get("reference_notes") or "").strip()
    ref_is_reviewed = int(data.get("reference_is_reviewed", 1) or 0)
    if isinstance(ref_value, dict):
        ref_ans = (ref_value.get("answer_text") or ref_value.get("answerText") or ref_value.get("content") or "").strip()
        ref_org = (ref_value.get("organization") or ref_value.get("orgName") or ref_org).strip()
        ref_scoring_points = (ref_value.get("scoring_points") or ref_value.get("scoringPoints") or ref_scoring_points).strip()
        ref_notes = (ref_value.get("notes") or ref_notes).strip()
        ref_is_reviewed = int(ref_value.get("is_reviewed", ref_is_reviewed) or 0)
    elif isinstance(ref_value, str):
        ref_ans = ref_value.strip()
    else:
        ref_ans = str(data.get("ref_answer_text") or "").strip()
    if ref_ans:
        add_question_reference_answer(
            conn,
            question_id,
            ref_org,
            ref_ans,
            scoring_points=ref_scoring_points,
            notes=ref_notes,
            is_reviewed=ref_is_reviewed,
        )

    return EntityResult(question_id, question_code)


def add_question_reference_answer(
    conn,
    question_id: int,
    organization: str,
    answer_text: str,
    scoring_points: str = "",
    notes: str = "",
    score: int | None = None,
    is_reviewed: int = 1,
    **kwargs,
) -> int:
    """Add or update a reference answer for a question."""
    org = (organization or "参考答案").strip()
    canonical_org = canonicalize_organization(org)
    ans = (answer_text or "").strip()
    if not ans:
        raise ValueError("参考答案正文不能为空")

    conn.execute(
        """
        INSERT INTO reference_answers (
            question_id, organization, canonical_organization, answer_text,
            scoring_points, notes, is_reviewed
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(question_id, organization) DO UPDATE SET
            canonical_organization = excluded.canonical_organization,
            answer_text = excluded.answer_text,
            scoring_points = excluded.scoring_points,
            notes = excluded.notes,
            is_reviewed = excluded.is_reviewed,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            question_id,
            org,
            canonical_org,
            ans,
            (scoring_points or "").strip(),
            (notes or "").strip(),
            1 if is_reviewed else 0,
        ),
    )
    row = conn.execute(
        "SELECT id FROM reference_answers WHERE question_id = ? AND organization = ?",
        (question_id, org),
    ).fetchone()
    return row["id"]

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gongkao.db import connect, init_db
from gongkao.taxonomy import infer_question_type


DB_PATH = ROOT / "instance" / "gongkao.sqlite3"
EXPORT_DIR = ROOT / "exports" / "srnz"
INVALID_SOURCE_TEXTS = {"", "暂无答案数据", "??????"}


def normalize(text):
    return re.sub(r"\s+", "", text or "")


def split_question(raw):
    raw = re.sub(r"[ \t\u00a0]+", " ", (raw or "").strip())
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    match = re.search(r"要求\s*(?:[：:]|\n)", raw)
    if not match:
        match = re.search(r"\n\s*[（(]1[）)]", raw)
    if not match:
        trailing = re.search(
            r"(?P<req>(?:(?:本题)?\s*\d+\s*分\s*[，,、]\s*)?(?:\d+\s*字(?:以内|左右)?|(?:不超过|不多于|不少于|字数在)\s*\d+(?:\s*[-—至到]\s*\d+)?\s*字(?:以内|左右)?)(?:[。；;，,、]\s*)?(?:[（(]\s*(?:分值)?\s*\d+\s*分\s*[)）])?|[（(][^）)]*(?:字|分)[^）)]*[)）])\s*$",
            raw,
        )
        if trailing:
            return raw[: trailing.start()].rstrip("。；;，,、 "), trailing.group("req").strip()
        return raw, ""
    return raw[: match.start()].strip(), raw[match.start() :].strip()


def infer_word_limit(raw):
    match = re.search(
        r"(?:\d+\s*字\s*左右|(?:不超过|不多于|不少于|字数在)\s*\d+(?:\s*[-—至到]\s*\d+)?\s*字(?:以内|左右)?)",
        raw,
    )
    return match.group(0) if match else ""


def category_from_title(title):
    match = re.search(r"（([^）]+)）", title)
    return match.group(1).strip() if match else ""


def paper_fields(title):
    year_match = re.search(r"(20\d{2})年", title)
    if not year_match:
        raise ValueError(f"无法识别年份：{title}")
    year = int(year_match.group(1))
    if "国家公考" in title:
        return year, "国考", "全国", "全国", 4
    if "公安院校联考" in title:
        return year, "公安院校联考", "全国", "全国", 3
    if any(word in title for word in ("法检", "司法", "政法干警")):
        return year, "公安院校联考", "全国", "全国", 3
    direct_city_map = {
        "广州市": ("广州公考", "广州", "广东"),
        "深圳市": ("深圳公考", "深圳", "广东"),
        "上海市": ("上海市考", "上海", "上海"),
        "北京市": ("北京市考", "北京", "北京"),
        "天津市": ("天津市考", "天津", "天津"),
        "重庆市": ("重庆市考", "重庆", "重庆"),
    }
    for marker, fields in direct_city_map.items():
        if marker in title:
            exam_type, region, source_province = fields
            if "选调生" in title:
                exam_type = f"{region}选调"
            return year, exam_type, region, source_province, 3
    province_map = {
        "安徽": ("安徽省考", "安徽", "安徽"),
        "福建": ("福建省考", "福建", "福建"),
        "甘肃": ("甘肃省考", "甘肃", "甘肃"),
        "广东": ("广东省考", "广东", "广东"),
        "广西": ("广西省考", "广西", "广西"),
        "贵州": ("贵州省考", "贵州", "贵州"),
        "海南": ("海南省考", "海南", "海南"),
        "河北": ("河北省考", "河北", "河北"),
        "河南": ("河南省考", "河南", "河南"),
        "黑龙江": ("黑龙江省考", "黑龙江", "黑龙江"),
        "湖北": ("湖北省考", "湖北", "湖北"),
        "湖南": ("湖南省考", "湖南", "湖南"),
        "吉林": ("吉林省考", "吉林", "吉林"),
        "江苏": ("江苏省考", "江苏", "江苏"),
        "江西": ("江西省考", "江西", "江西"),
        "辽宁": ("辽宁省考", "辽宁", "辽宁"),
        "内蒙古": ("内蒙古省考", "内蒙古", "内蒙古"),
        "宁夏": ("宁夏省考", "宁夏", "宁夏"),
        "青海": ("青海省考", "青海", "青海"),
        "山东": ("山东省考", "山东", "山东"),
        "山西": ("山西省考", "山西", "山西"),
        "陕西": ("陕西省考", "陕西", "陕西"),
        "天津": ("天津市考", "天津", "天津"),
        "四川": ("四川省考", "四川", "四川"),
        "西藏": ("西藏省考", "西藏", "西藏"),
        "新疆兵团": ("新疆省考", "新疆", "新疆"),
        "新疆": ("新疆省考", "新疆", "新疆"),
        "云南": ("云南省考", "云南", "云南"),
        "浙江": ("浙江省考", "浙江", "浙江"),
        "重庆": ("重庆省考", "重庆", "重庆"),
    }
    for province, (exam_type, region, source_province) in sorted(
        province_map.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if province in title:
            if "选调生" in title:
                exam_type = f"{province}选调"
            if province == "浙江":
                return year, exam_type, region, source_province, 5
            if province in {"江苏", "上海"}:
                return year, exam_type, region, source_province, 4
            return year, exam_type, region, source_province, 3
    raise ValueError(f"不在覆盖清单导入范围：{title}")


def target_group_for(exam_type):
    if exam_type == "国考":
        return "国考"
    if exam_type in {"浙江省考", "浙江选调"}:
        return "核心题库"
    return "拓展题库"


def prompt_hash(prompt, requirements):
    raw = normalize(prompt + requirements)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def import_paper(conn, data):
    year, exam_type, region, province, relevance = paper_fields(data["title"])
    paper_code = f"SRNZ-{data['id']}"
    category = category_from_title(data["title"])
    if exam_type == "浙江选调" and not category:
        category = "选调生"
    materials = data.get("materials", [])
    has_full_materials = bool(materials) and all(
        (material.get("text") or "").strip() for material in materials
    )
    source_note = "囊中对比网页版当前登录状态下可见内容"
    if not has_full_materials:
        source_note += "；网站原页存在空白材料，待从其他公开来源补齐"
    conn.execute(
        """
        INSERT INTO papers (
            paper_code, paper_name, paper_category, exam_type, year, region,
            source_province, target_group, zhejiang_relevance, source_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_code) DO UPDATE SET
            paper_name = excluded.paper_name,
            paper_category = excluded.paper_category,
            exam_type = excluded.exam_type,
            year = excluded.year,
            region = excluded.region,
            source_province = excluded.source_province,
            target_group = excluded.target_group,
            zhejiang_relevance = excluded.zhejiang_relevance,
            source_note = excluded.source_note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            paper_code,
            data["title"],
            category,
            exam_type,
            year,
            region,
            province,
            target_group_for(exam_type),
            relevance,
            source_note,
        ),
    )
    paper_id = conn.execute("SELECT id FROM papers WHERE paper_code = ?", (paper_code,)).fetchone()["id"]

    conn.execute("DELETE FROM paper_materials WHERE paper_id = ?", (paper_id,))
    for material in materials:
        text = (material.get("text") or "").strip()
        if not text:
            continue
        number = int(material["number"])
        conn.execute(
            """
            INSERT INTO paper_materials (paper_id, material_number, title, content)
            VALUES (?, ?, ?, ?)
            """,
            (paper_id, number, f"材料{number}", text),
        )

    imported_questions = 0
    imported_answers = 0
    for item in data.get("questions", []):
        number = int(item["number"])
        prompt, requirements = split_question(item.get("raw", ""))
        if not prompt:
            continue
        question_code = f"SRNZ-{data['id']}-Q{number}"
        existing = conn.execute(
            "SELECT id FROM questions WHERE question_code = ?", (question_code,)
        ).fetchone()
        question_id = existing["id"] if existing else None
        values = (
            paper_id,
            data["title"],
            category,
            number,
            exam_type,
            year,
            region,
            province,
            relevance,
            infer_question_type(prompt, requirements),
            f"第{number}题",
            prompt,
            requirements,
            infer_word_limit(item.get("raw", "")),
            data["url"],
            "囊中对比",
            1 if has_full_materials else 0,
            prompt_hash(prompt, requirements),
            "材料按整张试卷共享保存；" + source_note,
        )
        if question_id:
            conn.execute(
                """
                UPDATE questions SET
                    paper_id = ?, paper_name = ?, paper_category = ?, question_number = ?,
                    exam_type = ?, year = ?, region = ?, source_province = ?,
                    zhejiang_relevance = ?, question_type = ?, title = ?, prompt = ?,
                    original_text = '', materials = '', requirements = ?, word_limit = ?,
                    source_url = ?, source_kind = ?, is_full_original = ?,
                    content_hash = ?, source_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                values + (question_id,),
            )
        else:
            conn.execute(
                """
                INSERT INTO questions (
                    question_code, paper_id, paper_name, paper_category, question_number,
                    exam_type, year, region, source_province, zhejiang_relevance,
                    question_type, title, prompt, original_text, materials, requirements,
                    word_limit, source_url, source_kind, is_full_original, content_hash,
                    source_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)
                """,
                (question_code,) + values,
            )
            question_id = conn.execute(
                "SELECT id FROM questions WHERE question_code = ?", (question_code,)
            ).fetchone()["id"]

        conn.execute(
            """
            INSERT OR IGNORE INTO question_sources (
                question_id, provider, source_name, source_url, section
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (question_id, "囊中对比", data["title"], data["url"], f"第{number}题"),
        )
        answers = item.get("answers") or []
        if not answers and (item.get("answer") or "").strip():
            answers = [
                {
                    "organization": item.get("answerOrganization") or "囊中可见答案",
                    "answer": item.get("answer") or "",
                    "notes": "囊中对比网页版当前可见答案",
                }
            ]
        for answer_item in answers:
            answer = (answer_item.get("answer") or answer_item.get("answer_text") or "").strip()
            if answer in INVALID_SOURCE_TEXTS:
                continue
            organization = (answer_item.get("organization") or answer_item.get("answerOrganization") or "囊中可见答案").strip()
            if organization in INVALID_SOURCE_TEXTS:
                organization = "囊中可见答案"
            notes = (answer_item.get("notes") or "囊中对比网页版当前登录状态下可见答案").strip()
            conn.execute(
                """
                INSERT INTO reference_answers (
                    question_id, organization, answer_text, notes, is_reviewed
                ) VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(question_id, organization) DO UPDATE SET
                    answer_text = excluded.answer_text,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (question_id, organization, answer, notes),
            )
            imported_answers += 1
        imported_questions += 1
    return imported_questions, imported_answers


def merge_course_duplicates(conn):
    srnz_by_prompt = {}
    for row in conn.execute(
        "SELECT id, prompt FROM questions WHERE question_code LIKE 'SRNZ-%'"
    ):
        srnz_by_prompt.setdefault(normalize(row["prompt"]), []).append(row["id"])

    merged = 0
    course_questions = conn.execute(
        "SELECT id, prompt FROM questions WHERE question_code NOT LIKE 'SRNZ-%'"
    ).fetchall()
    for question in course_questions:
        targets = srnz_by_prompt.get(normalize(question["prompt"]), [])
        if not targets:
            continue
        sources = conn.execute(
            "SELECT * FROM question_sources WHERE question_id = ? AND provider <> '囊中对比'",
            (question["id"],),
        ).fetchall()
        references = conn.execute(
            "SELECT * FROM reference_answers WHERE question_id = ?", (question["id"],)
        ).fetchall()
        for target_id in targets:
            for source in sources:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO question_sources (
                        question_id, provider, source_name, source_path,
                        source_url, section, extracted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        source["provider"],
                        source["source_name"],
                        source["source_path"],
                        source["source_url"],
                        source["section"],
                        source["extracted_at"],
                    ),
                )
            for reference in references:
                conn.execute(
                    """
                    INSERT INTO reference_answers (
                        question_id, organization, answer_text, scoring_points,
                        notes, import_id, is_reviewed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(question_id, organization) DO NOTHING
                    """,
                    (
                        target_id,
                        reference["organization"],
                        reference["answer_text"],
                        reference["scoring_points"],
                        reference["notes"],
                        reference["import_id"],
                        reference["is_reviewed"],
                    ),
                )
        conn.execute(
            "UPDATE attempts SET question_id = ? WHERE question_id = ?",
            (targets[0], question["id"]),
        )
        conn.execute("DELETE FROM questions WHERE id = ?", (question["id"],))
        merged += 1
    return merged


def main():
    init_db(DB_PATH)
    files = sorted(EXPORT_DIR.glob("*.json"), key=lambda path: int(path.stem))
    question_count = 0
    answer_count = 0
    paper_count = 0
    with connect(DB_PATH) as conn:
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            year, exam_type, _, _, _ = paper_fields(data["title"])
            if year < 2020:
                continue
            questions, answers = import_paper(conn, data)
            paper_count += 1
            question_count += questions
            answer_count += answers
        merged = merge_course_duplicates(conn)
        conn.execute(
            """
            DELETE FROM papers
             WHERE id NOT IN (SELECT DISTINCT paper_id FROM questions WHERE paper_id IS NOT NULL)
               AND paper_code NOT LIKE 'SRNZ-%'
            """
        )
    print(
        f"导入试卷 {paper_count} 套，题目 {question_count} 道，可见答案 {answer_count} 份；"
        f"合并课程重复题 {merged} 道。"
    )


if __name__ == "__main__":
    main()

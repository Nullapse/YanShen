import re

from .agent_tools import load_user_context, retrieve_candidates

QUESTION_TYPES = ("归纳概括", "综合分析", "提出对策", "公文写作", "综合写作")
REGIONS = ("浙江", "江苏", "上海", "山东", "广东", "安徽", "北京", "全国")


def latest_attempt_id(conn):
    row = conn.execute(
        "SELECT id FROM attempts ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def latest_attempt_ids(conn, limit=1, filters=None):
    limit = max(1, min(int(limit or 1), 5))
    filters = filters or {}
    q_type = filters.get("question_type") or ""
    region = filters.get("region") or ""
    work_status = filters.get("work_status") or ""
    q_search = filters.get("q") or ""

    query = """
        SELECT a.id AS id
          FROM attempts a
          JOIN questions q ON q.id = a.question_id
         WHERE 1=1
    """
    params = []
    if q_type:
        query += " AND q.question_type = ?"
        params.append(q_type)
    if region:
        query += " AND q.region = ?"
        params.append(region)
    if work_status == "graded":
        query += " AND EXISTS(SELECT 1 FROM grading_reports r WHERE r.attempt_id = a.id)"
    elif work_status == "ungraded":
        query += " AND NOT EXISTS(SELECT 1 FROM grading_reports r WHERE r.attempt_id = a.id)"
    
    if q_search:
        query += " AND (q.title LIKE ? OR q.question_code LIKE ?)"
        params.append(f"%{q_search}%")
        params.append(f"%{q_search}%")

    query += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    params.append(limit)

    return [
        row["id"]
        for row in conn.execute(query, params).fetchall()
    ]


def requested_review_count(text="", default=1):
    text = text or ""
    digits = re.search(r"最近\s*(\d+)\s*(?:道|题|次|个|篇)", text)
    if digits:
        return max(1, min(int(digits.group(1)), 5))
    chinese_numbers = {
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
    }
    for word, count in chinese_numbers.items():
        if re.search(rf"最近[^，。！？\s]*{word}\s*(?:道|题|次|个|篇)", text):
            return count
        if re.search(rf"{word}\s*(?:道|题|次|个|篇)", text) and "复盘" in text:
            return count
    if any(key in text for key in ("几道", "几题", "多道", "多个")):
        return 3
    return default


def infer_intent(text="", entrypoint="chat", has_attempt=False):
    text = (text or "").strip()
    combined = f"{entrypoint} {text}"
    if any(key in text for key in ("整个模块", "整个", "所有", "全部", "整体", "汇总", "大类", "整身")):
        return "today"
    has_specific_review_keyword = any(key in text for key in ("复盘", "最近一次作答", "本题答案", "这份答案")) or (
        "失分" in text and any(prn in text for prn in ("这次", "本题", "这题", "这份", "这道", "这篇", "刚才", "那题", "那道"))
    )
    if has_specific_review_keyword:
        return "recent_review" if has_attempt else "next_question"
    if any(key in text for key in ("下一题", "推荐", "练什么", "今天", "安排")):
        return "today" if "今天" in combined or "练什么" in combined else "next_question"
    if any(key in text for key in ("诊断", "短板", "弱项", "计划")):
        return "today"
    if entrypoint in {"recent_review", "next_question", "today"}:
        return entrypoint
    return "today"


def task_type_for(entrypoint):
    if entrypoint == "recent_review":
        return "review"
    if entrypoint in {"next_question", "question_search"}:
        return "recommend"
    return "diagnosis"


def filters_from_text(text="", existing=None):
    filters = dict(existing or {})
    text = text or ""
    mentioned_types = [
        (text.rfind(question_type), question_type)
        for question_type in QUESTION_TYPES
        if question_type in text
    ]
    if mentioned_types:
        _, latest_type = max(mentioned_types)
        exclusion = any(
            marker in text[max(0, text.rfind(latest_type) - 5) : text.rfind(latest_type)]
            for marker in ("不要", "排除", "不练", "别看")
        )
        if exclusion:
            filters["exclude_question_type"] = latest_type
            if filters.get("question_type") == latest_type:
                filters.pop("question_type", None)
        else:
            filters["question_type"] = latest_type
            filters.pop("exclude_question_type", None)
    mentioned_regions = [
        (text.rfind(region), region)
        for region in REGIONS
        if region in text
    ]
    if "国考" in text:
        mentioned_regions.append((text.rfind("国考"), "全国"))
    if mentioned_regions:
        filters["region"] = max(mentioned_regions)[1]
    if "未做" in text or "没做" in text or "新题" in text:
        filters["work_status"] = "unattempted"
    elif "未批改" in text or "没批改" in text:
        filters["work_status"] = "ungraded"
    elif "已批改" in text:
        filters["work_status"] = "graded"
    elif "已做" in text or "做过" in text:
        filters["work_status"] = "attempted"
    return filters


def first_screen_judgement(conn):
    context = load_user_context(conn)
    summary = context["summary"]
    candidates = retrieve_candidates(conn, {"work_status": "unattempted"}, limit=3)
    if summary["attempt_count"] == 0:
        lead = "先选一个训练模块，再直接问你的问题。"
    elif summary["report_count"] == 0:
        lead = "可以先从最近作答开始复盘，也可以按题型问短板。"
    else:
        lead = "选择一个模块，我会结合你的作答、批改和笔记回答。"
    return {
        "lead": lead,
        "context": context,
        "candidates": candidates,
        "latest_attempt_id": latest_attempt_id(conn),
    }

from .grading import select_relevant_materials
from .skill_graph import related_skill_context
from .statistics import build_training_statistics, parse_report_score
from .timeutils import format_beijing_time


def _dict(row):
    return dict(row) if row is not None else {}


def _clip(value, limit=420):
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _task_label(task_type):
    return {
        "diagnosis": "训练诊断",
        "review": "本题复盘",
        "recommend": "择题助手",
    }.get(task_type, "AI 训练教练")


def _weakness_profile_summary(conn):
    rows = conn.execute(
        """
        SELECT module, question_type, problem_type, frequency, severity, evidence_json, last_seen_at
          FROM agent_weakness_profile
      ORDER BY severity DESC, frequency DESC, updated_at DESC
         LIMIT 10
        """
    ).fetchall()
    profile = []
    for row in rows:
        profile.append(
            {
                "module": row["module"],
                "question_type": row["question_type"],
                "problem_type": row["problem_type"],
                "frequency": row["frequency"],
                "severity": row["severity"],
                "last_seen_at": row["last_seen_at"],
            }
        )
    return profile


def load_user_context(conn):
    stats = build_training_statistics(conn)
    recent_attempts = [
        _dict(row)
        for row in conn.execute(
            """
            SELECT a.id, a.question_id, a.word_count, a.created_at,
                   q.title, q.question_type, q.year, q.region, q.exam_type,
                   COUNT(gr.id) AS report_count
              FROM attempts a
              JOIN questions q ON q.id = a.question_id
         LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
          GROUP BY a.id
          ORDER BY a.created_at DESC, a.id DESC
             LIMIT 8
            """
        )
    ]
    recent_notes = [
        {
            "attempt_id": row["id"],
            "question_id": row["question_id"],
            "title": row["title"],
            "note": _clip(row["personal_note"], 180),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            """
            SELECT a.id, a.question_id, a.personal_note, a.created_at, q.title
              FROM attempts a
              JOIN questions q ON q.id = a.question_id
             WHERE TRIM(a.personal_note) <> ''
          ORDER BY a.created_at DESC, a.id DESC
             LIMIT 5
            """
        )
    ]
    weakness_profile = _weakness_profile_summary(conn)
    skill_gaps = []
    seen_skills = set()
    for weakness in weakness_profile:
        for row in related_skill_context(conn, "error", weakness.get("problem_type"), limit=4):
            item = _dict(row)
            skill_key = item.get("skill_key")
            if not skill_key or skill_key in seen_skills:
                continue
            seen_skills.add(skill_key)
            item["problem_type"] = weakness.get("problem_type")
            item["severity"] = weakness.get("severity")
            skill_gaps.append(item)
    return {
        "summary": {
            "attempt_count": stats["attempt_count"],
            "question_count": stats["question_count"],
            "report_count": stats["report_count"],
            "active_days": stats["active_days"],
            "streak": stats["streak"],
            "average_score": stats["average_score"],
            "recognized_scores": stats["recognized_scores"],
            "favorite_questions": stats["favorite_questions"],
            "favorite_papers": stats["favorite_papers"],
        },
        "type_stats": stats["type_stats"],
        "regions": stats["regions"][:8],
        "recent_attempts": recent_attempts,
        "recent_notes": recent_notes,
        "weakness_profile": weakness_profile,
        "skill_gaps": skill_gaps[:12],
    }


def retrieve_candidates(conn, filters=None, limit=8):
    filters = filters or {}
    clauses = ["1 = 1"]
    params = []
    question_type = (filters.get("question_type") or "").strip()
    exclude_question_type = (filters.get("exclude_question_type") or "").strip()
    region = (filters.get("region") or "").strip()
    work_status = (filters.get("work_status") or "").strip()
    q = (filters.get("q") or "").strip()
    if question_type:
        clauses.append("q.question_type = ?")
        params.append(question_type)
    if exclude_question_type:
        clauses.append("q.question_type <> ?")
        params.append(exclude_question_type)
    if region:
        clauses.append("q.region = ?")
        params.append(region)
    if q:
        clauses.append("(q.title LIKE ? OR q.prompt LIKE ? OR q.paper_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    having = ""
    if work_status == "unattempted":
        having = "HAVING attempt_count = 0"
    elif work_status == "ungraded":
        having = "HAVING report_count = 0"
    elif work_status == "graded":
        having = "HAVING report_count > 0"
    elif work_status == "attempted":
        having = "HAVING attempt_count > 0"
    rows = conn.execute(
        f"""
        SELECT q.id, q.question_code, q.title, q.question_type, q.year, q.region,
               q.exam_type, q.paper_name, q.question_number, q.word_limit,
               q.zhejiang_relevance, COUNT(DISTINCT a.id) AS attempt_count,
               COUNT(DISTINCT gr.id) AS report_count
          FROM questions q
     LEFT JOIN attempts a ON a.question_id = q.id
     LEFT JOIN grading_reports gr ON gr.attempt_id = a.id
         WHERE {' AND '.join(clauses)}
      GROUP BY q.id
        {having}
      ORDER BY report_count ASC,
               attempt_count ASC,
               q.zhejiang_relevance DESC,
               q.year DESC,
               q.id DESC
         LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    candidates = []
    for row in rows:
        candidate = _dict(row)
        candidate["skill_targets"] = [
            _dict(skill)
            for skill in related_skill_context(conn, "question", candidate["id"], limit=8)
        ]
        candidates.append(candidate)
    return candidates


def get_attempt_review_context(conn, attempt_id):
    attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        return {}
    question = conn.execute(
        "SELECT * FROM questions WHERE id = ?",
        (attempt["question_id"],),
    ).fetchone()
    references = conn.execute(
        "SELECT organization, answer_text, scoring_points FROM reference_answers WHERE question_id = ? ORDER BY organization LIMIT 6",
        (question["id"],),
    ).fetchall()
    materials = conn.execute(
        "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
        (question["paper_id"],),
    ).fetchall() if question["paper_id"] else []
    relevant_materials = select_relevant_materials(question, materials)
    reports = conn.execute(
        "SELECT * FROM grading_reports WHERE attempt_id = ? ORDER BY created_at DESC, id DESC LIMIT 3",
        (attempt_id,),
    ).fetchall()
    latest_score = None
    for report in reports:
        latest_score = parse_report_score(report["report_text"])
        if latest_score is not None:
            break
    return {
        "attempt": {
            "id": attempt["id"],
            "question_id": attempt["question_id"],
            "answer_text": _clip(attempt["answer_text"], 1600),
            "word_count": attempt["word_count"],
            "personal_note": _clip(attempt["personal_note"], 700),
            "created_at": format_beijing_time(attempt["created_at"]),
        },
        "question": {
            "id": question["id"],
            "question_code": question["question_code"],
            "title": question["title"],
            "question_type": question["question_type"],
            "year": question["year"],
            "region": question["region"],
            "exam_type": question["exam_type"],
            "paper_name": question["paper_name"],
            "question_number": question["question_number"],
            "prompt": _clip(question["prompt"], 900),
            "requirements": _clip(question["requirements"], 600),
            "word_limit": question["word_limit"],
        },
        "materials": [
            {
                "material_number": row["material_number"],
                "title": row["title"],
                "content": _clip(row["content"], 900),
            }
            for row in relevant_materials[:6]
        ],
        "references": [
            {
                "organization": row["organization"],
                "answer_text": _clip(row["answer_text"], 700),
                "scoring_points": _clip(row["scoring_points"], 500),
            }
            for row in references
        ],
        "reports": [
            {
                "provider": row["provider"],
                "model": row["model"],
                "created_at": format_beijing_time(row["created_at"]),
                "report_text": _clip(row["report_text"], 1200),
            }
            for row in reports
        ],
        "latest_score": latest_score,
    }


def get_attempts_review_context(conn, attempt_ids):
    contexts = []
    seen = set()
    for attempt_id in attempt_ids or []:
        if attempt_id in seen:
            continue
        seen.add(attempt_id)
        context = get_attempt_review_context(conn, attempt_id)
        if context:
            contexts.append(context)
    if not contexts:
        return {}
    if len(contexts) == 1:
        return contexts[0]
    return {
        "mode": "multi_attempt_review",
        "requested_attempt_count": len(attempt_ids or []),
        "loaded_attempt_count": len(contexts),
        "attempt_reviews": contexts,
    }


def input_summary(task_type, user_context=None, candidates=None, review_context=None):
    user_context = user_context or {}
    candidates = candidates or []
    summary = user_context.get("summary", {})
    parts = [
        f"任务：{_task_label(task_type)}",
        f"累计作答：{summary.get('attempt_count', 0)} 次",
        f"已练题目：{summary.get('question_count', 0)} 道",
        f"批改报告：{summary.get('report_count', 0)} 份",
    ]
    if summary.get("average_score") is not None:
        parts.append(f"AI 百分制均分：{summary['average_score']}")
    if review_context and review_context.get("attempt_reviews"):
        titles = []
        for item in review_context["attempt_reviews"][:3]:
            question = item.get("question") or {}
            if question:
                titles.append(f"{question['question_code']} {question['title']}")
        if titles:
            parts.append(f"复盘题目：{'；'.join(titles)}")
    elif review_context and review_context.get("question"):
        question = review_context["question"]
        parts.append(f"复盘题目：{question['question_code']} {question['title']}")
    if candidates:
        parts.append(f"候选题：{len(candidates)} 道")
    return "；".join(parts)

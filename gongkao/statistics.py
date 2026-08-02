import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .timeutils import BEIJING_TIMEZONE

SCORE_PATTERN = re.compile(
    r"(?:总分|得分)\s*[：:]\s*\**\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(?:分)?",
    re.I,
)


def parse_report_score(text):
    match = SCORE_PATTERN.search(text or "")
    if not match:
        return None
    score = float(match.group(1))
    maximum = float(match.group(2))
    if maximum <= 0 or score < 0 or score > maximum:
        return None
    return round(score * 100 / maximum, 1)


def utc_text_to_beijing(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING_TIMEZONE)


def calculate_streak(date_values, today=None):
    dates = sorted(set(date_values), reverse=True)
    if not dates:
        return 0
    today = today or datetime.now(BEIJING_TIMEZONE).date()
    if dates[0] not in (today, today - timedelta(days=1)):
        return 0
    streak = 1
    expected = dates[0] - timedelta(days=1)
    for value in dates[1:]:
        if value != expected:
            break
        streak += 1
        expected -= timedelta(days=1)
    return streak


def build_training_statistics(conn, now=None):
    now = now or datetime.now(BEIJING_TIMEZONE)
    attempts = conn.execute(
        """
        SELECT a.id, a.question_id, a.word_count, a.duration_seconds, a.created_at,
               q.question_type, q.region
          FROM attempts a
          JOIN questions q ON q.id = a.question_id
         ORDER BY a.created_at, a.id
        """
    ).fetchall()
    reports = conn.execute(
        """
        SELECT gr.*
          FROM grading_reports gr
          JOIN (
                SELECT candidate.attempt_id, MAX(candidate.id) AS latest_id
                  FROM grading_reports candidate
             LEFT JOIN grading_report_contexts context
                    ON context.report_id = candidate.id
                 WHERE candidate.status = 'ok'
                   AND COALESCE(json_extract(context.result_json, '$.score_status'), 'valid') <> 'stale'
              GROUP BY candidate.attempt_id
          ) latest ON latest.latest_id = gr.id
        """
    ).fetchall()
    report_counts = {
        row["attempt_id"]: row["count"]
        for row in conn.execute(
            "SELECT attempt_id, COUNT(*) AS count FROM grading_reports GROUP BY attempt_id"
        )
    }

    attempt_dates = [utc_text_to_beijing(row["created_at"]).date() for row in attempts]
    report_attempts = {row["attempt_id"] for row in reports}
    recognized_scores = [
        score
        for row in reports
        if (score := parse_report_score(row["report_text"])) is not None
    ]

    start_date = now.date() - timedelta(days=364)
    daily_questions = {start_date + timedelta(days=offset): set() for offset in range(365)}
    for row in attempts:
        value = utc_text_to_beijing(row["created_at"]).date()
        if value in daily_questions:
            daily_questions[value].add(row["question_id"])
    daily_counts = {value: len(question_ids) for value, question_ids in daily_questions.items()}

    type_totals = {
        row["question_type"]: row["count"]
        for row in conn.execute(
            "SELECT question_type, COUNT(*) AS count FROM questions GROUP BY question_type"
        )
    }
    type_stats = []
    for question_type in ("归纳概括", "综合分析", "提出对策", "公文写作", "综合写作"):
        matching = [row for row in attempts if row["question_type"] == question_type]
        distinct_questions = {row["question_id"] for row in matching}
        total = type_totals.get(question_type, 0)
        type_stats.append(
            {
                "name": question_type,
                "attempts": len(matching),
                "questions": len(distinct_questions),
                "reports": sum(report_counts.get(row["id"], 0) for row in matching),
                "total": total,
                "completion": round(len(distinct_questions) * 100 / total, 1) if total else 0,
            }
        )

    region_counts = {}
    for row in attempts:
        region_counts[row["region"]] = region_counts.get(row["region"], 0) + 1

    favorite_counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM question_favorites) AS questions,
          (SELECT COUNT(*) FROM paper_favorites) AS papers
        """
    ).fetchone()

    timed_attempts = [row["duration_seconds"] for row in attempts if row["duration_seconds"] > 0]

    return {
        "attempt_count": len(attempts),
        "question_count": len({row["question_id"] for row in attempts}),
        "report_count": conn.execute("SELECT COUNT(*) FROM grading_reports").fetchone()[0],
        "word_count": sum(row["word_count"] for row in attempts),
        "total_duration_seconds": sum(timed_attempts),
        "timed_attempt_count": len(timed_attempts),
        "active_days": len(set(attempt_dates)),
        "streak": calculate_streak(attempt_dates, now.date()),
        "last_practice": attempts[-1]["created_at"] if attempts else "",
        "daily": list(daily_counts.items()),
        "type_stats": type_stats,
        "regions": sorted(region_counts.items(), key=lambda item: (-item[1], item[0])),
        "favorite_questions": favorite_counts["questions"],
        "favorite_papers": favorite_counts["papers"],
        "average_score": (
            round(sum(recognized_scores) / len(recognized_scores), 1)
            if recognized_scores else None
        ),
        "recognized_scores": len(recognized_scores),
        "reported_attempts": len(report_attempts),
    }


def build_module_score_statistics(conn, score_mode="first"):
    score_mode = "best" if score_mode == "best" else "first"
    rows = conn.execute(
        """
        SELECT a.id AS attempt_id,
               a.question_id,
               a.created_at,
               q.question_type,
               q.question_code,
               q.title,
               q.year,
               q.region,
               q.exam_type,
               a.duration_seconds,
               gr.report_text,
               gr.created_at AS report_created_at
          FROM attempts a
          JOIN questions q ON q.id = a.question_id
          JOIN (
                SELECT candidate.attempt_id, MAX(candidate.id) AS latest_id
                  FROM grading_reports candidate
             LEFT JOIN grading_report_contexts context
                    ON context.report_id = candidate.id
                 WHERE candidate.status = 'ok'
                   AND COALESCE(json_extract(context.result_json, '$.score_status'), 'valid') <> 'stale'
              GROUP BY candidate.attempt_id
          ) latest ON latest.attempt_id = a.id
          JOIN grading_reports gr ON gr.id = latest.latest_id
      ORDER BY a.created_at, a.id
        """
    ).fetchall()
    scored_attempts = []
    for row in rows:
        score = parse_report_score(row["report_text"])
        if score is None:
            continue
        scored_attempts.append(
            {
                "attempt_id": row["attempt_id"],
                "question_id": row["question_id"],
                "created_at": row["created_at"],
                "created_date": utc_text_to_beijing(row["created_at"]).date().isoformat(),
                "question_type": row["question_type"] or "未分类",
                "question_code": row["question_code"],
                "title": row["title"],
                "year": row["year"],
                "region": row["region"],
                "exam_type": row["exam_type"],
                "duration_seconds": row["duration_seconds"],
                "score": score,
            }
        )
    by_question = defaultdict(list)
    for item in scored_attempts:
        by_question[item["question_id"]].append(item)
    selected = []
    for attempts in by_question.values():
        attempts = sorted(attempts, key=lambda item: (item["created_at"], item["attempt_id"]))
        if score_mode == "best":
            selected.append(max(attempts, key=lambda item: (item["score"], item["created_at"], item["attempt_id"])))
        else:
            selected.append(attempts[0])
    modules = []
    by_module = defaultdict(list)
    for item in selected:
        by_module[item["question_type"]].append(item)
    module_order = ["归纳概括", "综合分析", "提出对策", "公文写作", "综合写作"]
    names = sorted(by_module, key=lambda name: (module_order.index(name) if name in module_order else 999, name))
    for name in names:
        items = sorted(by_module[name], key=lambda item: (item["created_at"], item["attempt_id"]))
        trend = []
        for index in range(0, len(items), 5):
            chunk = items[index : index + 5]
            trend.append(
                {
                    "label": f"{index + 1}-{index + len(chunk)}",
                    "average": round(sum(item["score"] for item in chunk) / len(chunk), 1),
                    "count": len(chunk),
                    "start_date": chunk[0]["created_date"],
                    "end_date": chunk[-1]["created_date"],
                }
            )
        modules.append(
            {
                "name": name,
                "question_count": len(items),
                "average_score": round(sum(item["score"] for item in items) / len(items), 1) if items else None,
                "latest_score": items[-1]["score"] if items else None,
                "trend": trend,
                "questions": items,
            }
        )
    return {
        "mode": score_mode,
        "modules": modules,
        "scored_questions": len(selected),
        "scored_attempts": len(scored_attempts),
    }

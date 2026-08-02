import json

from ..db import connect
from ..grading import select_relevant_materials


def row_dict(row):
    return dict(row) if row is not None else {}


def update_job(db_path, job_id, status, progress, message, **fields):
    assignments = ["status = ?", "progress = ?", "message = ?"]
    params = [status, int(progress), message]
    for key in ("error_text", "report_id", "retryable"):
        if key in fields:
            assignments.append(f"{key} = ?")
            params.append(fields[key])
    if status == "preparing":
        assignments.append("started_at = COALESCE(started_at, CURRENT_TIMESTAMP)")
    if status in {"completed", "failed", "interrupted"}:
        assignments.append("finished_at = CURRENT_TIMESTAMP")
    params.append(int(job_id))
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE grading_jobs SET {', '.join(assignments)} WHERE id = ?",
            params,
        )


def load_job_context(conn, job_id):
    job = conn.execute(
        "SELECT * FROM grading_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not job:
        raise ValueError("批改任务不存在")
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE id = ?",
        (job["attempt_id"],),
    ).fetchone()
    if not attempt:
        raise ValueError("作答不存在")
    question = conn.execute(
        "SELECT * FROM questions WHERE id = ?",
        (attempt["question_id"],),
    ).fetchone()
    materials = (
        conn.execute(
            "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
            (question["paper_id"],),
        ).fetchall()
        if question["paper_id"]
        else []
    )
    materials = select_relevant_materials(question, materials)
    options = json.loads(job["options_json"] or "{}")
    reference_ids = [int(value) for value in options.get("reference_ids") or []]
    placeholders = ",".join("?" for _ in reference_ids)
    references = (
        conn.execute(
            f"SELECT * FROM reference_answers WHERE question_id = ? AND id IN ({placeholders}) ORDER BY id",
            (question["id"], *reference_ids),
        ).fetchall()
        if reference_ids
        else []
    )
    settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
    feedback = conn.execute(
        """
        SELECT * FROM grading_feedback
         WHERE question_id = ? AND scope = 'question'
      ORDER BY updated_at DESC
        """,
        (question["id"],),
    ).fetchall()
    return (
        row_dict(job),
        row_dict(attempt),
        row_dict(question),
        [row_dict(row) for row in materials],
        [row_dict(row) for row in references],
        row_dict(settings),
        [row_dict(row) for row in feedback],
        options,
    )

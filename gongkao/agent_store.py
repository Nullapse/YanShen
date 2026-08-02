import json
import re
import sqlite3
import time
from datetime import datetime, timedelta

from .agent_tools import retrieve_candidates

RECENT_CONVERSATION_MESSAGE_LIMIT = 12
RECENT_CONVERSATION_CHAR_LIMIT = 8000
CONVERSATION_SUMMARY_CHAR_LIMIT = 2400


def _json_dumps(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _retry_execute(conn, sql, params=()):
    for attempt in range(5):
        try:
            res = conn.execute(sql, params)
            conn.commit()
            return res
        except Exception as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                time.sleep(0.1 * (attempt + 1))
            else:
                raise


def create_run(conn, task_type, subject_type="", subject_id=None, user_goal="", provider="", model=""):
    cursor = _retry_execute(
        conn,
        """
        INSERT INTO agent_runs (
            task_type, subject_type, subject_id, status, user_goal, provider, model
        ) VALUES (?, ?, ?, 'running', ?, ?, ?)
        """,
        (task_type, subject_type or "", subject_id, user_goal or "", provider or "", model or ""),
    )
    return cursor.lastrowid


def add_step(conn, run_id, step_type, tool_name="", input_data=None, output_data=None):
    row = conn.execute(
        "SELECT COALESCE(MAX(step_index), 0) + 1 AS next_index FROM agent_steps WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    _retry_execute(
        conn,
        """
        INSERT INTO agent_steps (
            run_id, step_index, step_type, tool_name, input_json, output_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            row["next_index"],
            step_type,
            tool_name or "",
            _json_dumps(input_data),
            _json_dumps(output_data),
        ),
    )


def complete_run(conn, run_id, final_text, input_summary="", status="completed"):
    _retry_execute(
        conn,
        """
        UPDATE agent_runs
           SET status = ?,
               input_summary = ?,
               final_text = ?,
               completed_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        (status, input_summary or "", final_text or "", run_id),
    )


def fail_run(conn, run_id, message):
    complete_run(conn, run_id, message, status="failed")


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()


def get_run_steps(conn, run_id):
    return conn.execute(
        "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_index, id",
        (run_id,),
    ).fetchall()


def add_feedback(conn, run_id, rating, note=""):
    rating = max(1, min(5, int(rating or 1)))
    _retry_execute(
        conn,
        "INSERT INTO agent_feedback (run_id, rating, note) VALUES (?, ?, ?)",
        (run_id, rating, note or ""),
    )


def get_feedback(conn, run_id):
    return conn.execute(
        "SELECT * FROM agent_feedback WHERE run_id = ? ORDER BY id DESC",
        (run_id,),
    ).fetchall()


def recent_runs(conn, limit=8):
    return conn.execute(
        "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _target_date(offset):
    return (datetime.now() + timedelta(days=offset)).date().isoformat()


def save_training_plan_items(conn, run_id, candidates, reason=""):
    candidates = candidates or []
    saved = 0
    for index, candidate in enumerate(candidates[:5], start=1):
        question_id = candidate.get("id")
        if not question_id:
            continue
        exists = conn.execute(
            """
            SELECT id FROM training_plan_items
             WHERE run_id = ? AND question_id = ? AND status <> 'deleted'
             LIMIT 1
            """,
            (run_id, question_id),
        ).fetchone()
        if exists:
            continue
        title = " ".join(
            str(part)
            for part in [candidate.get("question_code"), candidate.get("title")]
            if part
        ).strip() or "训练题目"
        item_reason = reason or (
            f"{candidate.get('question_type') or '申论'} · "
            f"{candidate.get('year') or ''} {candidate.get('region') or ''}，"
            f"当前作答 {candidate.get('attempt_count', 0)} 次，批改 {candidate.get('report_count', 0)} 份。"
        )
        _retry_execute(
            conn,
            """
            INSERT INTO training_plan_items (run_id, question_id, title, reason, target_date, status)
            VALUES (?, ?, ?, ?, ?, 'todo')
            """,
            (run_id, question_id, title, item_reason, _target_date(index - 1)),
        )
        saved += 1
    return saved


def create_training_todos_from_run(conn, run_id, limit=3):
    run = get_run(conn, run_id)
    if not run:
        return 0
    filters = {}
    step = conn.execute(
        """
        SELECT input_json
          FROM agent_steps
         WHERE run_id = ? AND tool_name = 'retrieve_candidates'
      ORDER BY step_index DESC, id DESC
         LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if step:
        try:
            filters = json.loads(step["input_json"] or "{}")
        except json.JSONDecodeError:
            filters = {}
    candidates = retrieve_candidates(conn, filters, limit=limit)
    reason = run["user_goal"] or run["input_summary"] or "来自 AI 训练分析的关联训练。"
    return save_training_plan_items(conn, run_id, candidates, reason=reason)


def get_training_plan_items(conn, run_id=None, limit=8):
    params = []
    where = ["t.status <> 'deleted'"]
    if run_id:
        where.append("t.run_id = ?")
        params.append(run_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT t.*, q.question_type, q.year, q.region, q.question_number
          FROM training_plan_items t
     LEFT JOIN questions q ON q.id = t.question_id
         WHERE {' AND '.join(where)}
      ORDER BY CASE t.status WHEN 'todo' THEN 0 ELSE 1 END,
               t.target_date,
               t.id DESC
         LIMIT ?
        """,
        params,
    ).fetchall()


def create_conversation(conn, title="", entrypoint="chat"):
    cursor = _retry_execute(
        conn,
        """
        INSERT INTO agent_conversations (title, entrypoint, status)
        VALUES (?, ?, 'active')
        """,
        (title or "", entrypoint or "chat"),
    )
    return cursor.lastrowid


def get_conversation(conn, conversation_id):
    return conn.execute(
        "SELECT * FROM agent_conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()


def delete_conversation(conn, conversation_id):
    try:
        _retry_execute(
            conn,
            """
            DELETE FROM agent_run_steps
             WHERE run_id IN (
                 SELECT run_id FROM agent_messages WHERE conversation_id = ? AND run_id IS NOT NULL
             )
            """,
            (conversation_id,),
        )
        _retry_execute(
            conn,
            """
            DELETE FROM agent_runs
             WHERE id IN (
                 SELECT run_id FROM agent_messages WHERE conversation_id = ? AND run_id IS NOT NULL
             )
            """,
            (conversation_id,),
        )
    except sqlite3.OperationalError:
        pass
    _retry_execute(conn, "DELETE FROM agent_messages WHERE conversation_id = ?", (conversation_id,))
    _retry_execute(conn, "DELETE FROM agent_conversations WHERE id = ?", (conversation_id,))


def recent_conversations(conn, limit=8):
    return conn.execute(
        """
        SELECT c.*,
               (SELECT content FROM agent_messages m
                 WHERE m.conversation_id = c.id
              ORDER BY m.id DESC LIMIT 1) AS latest_message,
               (SELECT metadata_json FROM agent_messages m
                 WHERE m.conversation_id = c.id
                   AND m.role = 'assistant'
              ORDER BY m.id DESC LIMIT 1) AS latest_metadata_json
          FROM agent_conversations c
      ORDER BY c.updated_at DESC, c.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def add_message(conn, conversation_id, role, content, run_id=None, message_type="text", metadata=None):
    cursor = _retry_execute(
        conn,
        """
        INSERT INTO agent_messages (
            conversation_id, run_id, role, content, message_type, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            run_id,
            role,
            content or "",
            message_type or "text",
            _json_dumps(metadata),
        ),
    )
    _retry_execute(
        conn,
        "UPDATE agent_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (conversation_id,),
    )
    return cursor.lastrowid


def update_message(conn, message_id, content, run_id=None, message_type="text", metadata=None):
    _retry_execute(
        conn,
        """
        UPDATE agent_messages
           SET run_id = ?,
               content = ?,
               message_type = ?,
               metadata_json = ?
         WHERE id = ?
        """,
        (
            run_id,
            content or "",
            message_type or "text",
            _json_dumps(metadata),
            message_id,
        ),
    )
    row = conn.execute(
        "SELECT conversation_id FROM agent_messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE agent_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (row["conversation_id"],),
        )


def get_messages(conn, conversation_id):
    return conn.execute(
        """
        SELECT * FROM agent_messages
         WHERE conversation_id = ?
      ORDER BY id
        """,
        (conversation_id,),
    ).fetchall()


def _compact_message(role, content, limit=220):
    text = " ".join(str(content or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    label = "用户" if role == "user" else "教练"
    return f"{label}：{text}" if text else ""


def conversation_context(
    conn,
    conversation_id,
    recent_limit=RECENT_CONVERSATION_MESSAGE_LIMIT,
    recent_char_limit=RECENT_CONVERSATION_CHAR_LIMIT,
):
    rows = conn.execute(
        """
        SELECT id, role, content, message_type
          FROM agent_messages
         WHERE conversation_id = ?
           AND role IN ('user', 'assistant')
           AND message_type NOT IN ('pending', 'error')
      ORDER BY id
        """,
        (conversation_id,),
    ).fetchall()
    recent_limit = max(2, int(recent_limit or RECENT_CONVERSATION_MESSAGE_LIMIT))
    older = rows[:-recent_limit]
    recent_rows = list(rows[-recent_limit:])
    total_chars = sum(len(row["content"] or "") for row in recent_rows)
    while len(recent_rows) > 2 and total_chars > recent_char_limit:
        moved = recent_rows.pop(0)
        older.append(moved)
        total_chars -= len(moved["content"] or "")
    summary_lines = [
        _compact_message(row["role"], row["content"])
        for row in older
    ]
    summary = "\n".join(line for line in summary_lines if line)
    if len(summary) > CONVERSATION_SUMMARY_CHAR_LIMIT:
        head_limit = CONVERSATION_SUMMARY_CHAR_LIMIT // 3
        tail_limit = CONVERSATION_SUMMARY_CHAR_LIMIT - head_limit - 3
        summary = summary[:head_limit].rstrip() + "\n…\n" + summary[-tail_limit:].lstrip()
    messages = [
        {
            "id": row["id"],
            "role": row["role"],
            "content": (row["content"] or "")[:2000],
        }
        for row in recent_rows
    ]
    return {
        "conversation_id": conversation_id,
        "summary": summary,
        "messages": messages,
        "message_count": len(rows),
        "summarized_count": len(older),
    }


def extract_explicit_memories(text):
    text = " ".join(str(text or "").split())
    if not text:
        return []
    memories = []

    def add(memory_type, key, content, confidence=0.9):
        content = str(content or "").strip(" ，。；：:")
        if len(content) < 2:
            return
        memories.append(
            {
                "memory_type": memory_type,
                "memory_key": key,
                "content": content[:160],
                "confidence": confidence,
            }
        )

    target = re.search(
        r"(?:我(?:的)?目标(?:考试)?是|我主要备考|我备考|我准备|我的目标是|(?:我|本线程|线程[甲乙])?只备考)\s*([^，。；\n]{2,30})",
        text,
    )
    if target:
        add("semantic", "target_exam", target.group(1))

    schedule = re.search(
        r"((?:每天|每周\s*\d+\s*次|工作日|周末)[^，。；\n]{0,30}(?:训练|练习)[^，。；\n]{0,16})",
        text,
    )
    if schedule:
        add("procedural", "training_rhythm", schedule.group(1))

    if re.search(r"(?:(?:回答|回复|分析).{0,12})?(?:尽量|要|希望|以后|还是).{0,8}(?:简洁|精炼|短一点|先说结论|只说重点)", text):
        add("procedural", "response_style", "偏好简洁回答，优先给结论和可执行动作")
    elif re.search(r"(?:(?:回答|回复|分析).{0,12})?(?:尽量|要|希望|以后|还是).{0,8}(?:详细|展开|多解释)", text):
        add("procedural", "response_style", "偏好详细回答，需要展开依据和推理过程")

    weakness = re.search(
        r"(?:我的|我最明显的)(?:弱点|短板|问题)是\s*([^，。；\n]{2,50})",
        text,
    )
    if weakness:
        add("semantic", "self_reported_weakness", weakness.group(1), confidence=0.8)

    milestone = re.search(
        r"((?:我已经|这次我|最近我)(?:改掉|改善|解决|克服)[^，。；\n]{2,60})",
        text,
    )
    if milestone:
        add("episodic", "latest_improvement", milestone.group(1), confidence=0.8)

    return memories


def remember_explicit_user_facts(conn, conversation_id, message_id, text):
    saved = []
    for memory in extract_explicit_memories(text):
        conn.execute(
            """
            INSERT INTO agent_memories (
                memory_type, memory_key, content, source_conversation_id,
                source_message_id, confidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(memory_type, memory_key) DO UPDATE SET
                content = excluded.content,
                source_conversation_id = excluded.source_conversation_id,
                source_message_id = excluded.source_message_id,
                confidence = excluded.confidence,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                memory["memory_type"],
                memory["memory_key"],
                memory["content"],
                conversation_id,
                message_id,
                memory["confidence"],
            ),
        )
        saved.append(memory)
    return saved


def active_memories(conn, limit=20):
    return conn.execute(
        """
        SELECT * FROM agent_memories
         WHERE status = 'active'
      ORDER BY updated_at DESC, id DESC
         LIMIT ?
        """,
        (max(1, int(limit or 20)),),
    ).fetchall()


def delete_memory(conn, memory_id):
    cursor = conn.execute("DELETE FROM agent_memories WHERE id = ?", (memory_id,))
    return cursor.rowcount > 0


def clear_memories(conn):
    cursor = conn.execute("DELETE FROM agent_memories")
    return cursor.rowcount


def ensure_conversation(conn, conversation_id=None, title="", entrypoint="chat"):
    if conversation_id:
        found = get_conversation(conn, conversation_id)
        if found:
            return found["id"]
    return create_conversation(conn, title, entrypoint)

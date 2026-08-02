import json
import logging
import threading
import time

from .agent_coach import (
    filters_from_text,
    infer_intent,
    latest_attempt_id,
    latest_attempt_ids,
    requested_review_count,
    task_type_for,
)
from .agent_graph import create_agent_run, run_agent
from .agent_store import (
    active_memories,
    add_message,
    complete_run,
    conversation_context,
    ensure_conversation,
    fail_run,
    get_messages,
    get_run,
    get_run_steps,
    remember_explicit_user_facts,
    update_message,
)
from .db import connect


def _title_for(text, entrypoint):
    text = (text or "").strip()
    if text:
        return text[:36]
    return {
        "today": "今日训练建议",
        "recent_review": "复盘最近一次作答",
        "next_question": "推荐下一题",
    }.get(entrypoint, "AI 教练对话")


def _prepare_chat_request(
    db_path,
    conversation_id=None,
    user_text="",
    entrypoint="chat",
    filters=None,
    module="",
    review_attempt_id=None,
):
    with connect(db_path) as conn:
        previous_filters = {}
        previous_entrypoint = "chat"
        previous_subject_ids = []
        if conversation_id:
            last = conn.execute(
                """
                SELECT metadata_json
                  FROM agent_messages
                 WHERE conversation_id = ? AND role = 'assistant'
              ORDER BY id DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if last:
                try:
                    metadata = json.loads(last["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                previous_filters = metadata.get("filters") or {}
                if not module:
                    module = metadata.get("module") or ""
                previous_entrypoint = metadata.get("entrypoint") or "chat"
                raw_subject_ids = metadata.get("subject_ids") or []
                if not raw_subject_ids and metadata.get("subject_id"):
                    raw_subject_ids = [metadata.get("subject_id")]
                for subject_id in raw_subject_ids:
                    try:
                        previous_subject_ids.append(int(subject_id))
                    except (TypeError, ValueError):
                        pass
        filters = filters_from_text(user_text, {**previous_filters, **(filters or {})})
        has_attempt = latest_attempt_id(conn) is not None
        if review_attempt_id:
            resolved_entrypoint = "recent_review"
        else:
            resolved_entrypoint = infer_intent(
                user_text,
                previous_entrypoint if entrypoint == "chat" else entrypoint,
                has_attempt,
            )
        conversation_id = ensure_conversation(
            conn,
            conversation_id,
            _title_for(user_text, resolved_entrypoint),
            resolved_entrypoint,
        )
        remembered = []
        if user_text:
            user_message_id = add_message(conn, conversation_id, "user", user_text)
            remembered = remember_explicit_user_facts(conn, conversation_id, user_message_id, user_text)
        subject_ids = []
        if review_attempt_id:
            subject_ids = [review_attempt_id]
        elif resolved_entrypoint == "recent_review":
            explicit_recent_request = any(key in (user_text or "") for key in ("最近", "几道", "几题", "多道", "多个"))
            if previous_subject_ids and not explicit_recent_request:
                subject_ids = previous_subject_ids
            else:
                subject_ids = latest_attempt_ids(conn, requested_review_count(user_text), filters)
        subject_id = subject_ids[0] if subject_ids else None
        thread_context = conversation_context(conn, conversation_id)
        long_term_memories = [dict(row) for row in active_memories(conn)]
    task_type = task_type_for(resolved_entrypoint)
    return {
        "conversation_id": conversation_id,
        "task_type": task_type,
        "subject_id": subject_id,
        "subject_ids": subject_ids,
        "user_goal": user_text or _title_for("", resolved_entrypoint),
        "filters": filters,
        "module": module,
        "entrypoint": resolved_entrypoint,
        "conversation_messages": thread_context["messages"],
        "conversation_summary": thread_context["summary"],
        "conversation_message_count": thread_context["message_count"],
        "conversation_summarized_count": thread_context["summarized_count"],
        "long_term_memories": long_term_memories,
        "remembered_count": len(remembered),
        "remembered_memories": remembered,
    }


def _is_memory_only_request(prepared):
    if not prepared.get("remembered_memories"):
        return False
    text = prepared.get("user_goal") or ""
    action_keys = ("推荐", "分析", "复盘", "怎么", "为什么", "总结", "整理", "比较", "改写", "判断", "安排", "练什么")
    return not any(key in text for key in action_keys)


def _complete_memory_acknowledgement(db_path, prepared):
    labels = {
        "target_exam": "目标考试",
        "response_style": "回答风格",
        "training_rhythm": "训练节奏",
        "self_reported_weakness": "自报短板",
        "latest_improvement": "最近改进",
    }
    items = [
        f"{labels.get(item.get('memory_key'), item.get('memory_key'))}：{item.get('content')}"
        for item in prepared.get("remembered_memories") or []
    ]
    summary = "；".join(items)
    final_text = (
        f"已记住：{summary}。后续回答会按这个约束执行，你也可以在“长期记忆”中查看或删除。\n\n"
        "```json\n"
        + json.dumps(
            {
                "summary": f"已记录：{summary}",
                "weaknesses": [],
                "next_actions": [],
                "recommended_questions": [],
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    run_id = create_agent_run(
        db_path,
        prepared["task_type"],
        subject_id=prepared.get("subject_id"),
        subject_ids=prepared.get("subject_ids") or [],
        user_goal=prepared.get("user_goal") or "",
    )
    with connect(db_path) as conn:
        complete_run(conn, run_id, final_text, "显式长期记忆已保存")
        message_id = add_message(
            conn,
            prepared["conversation_id"],
            "assistant",
            final_text,
            run_id=run_id,
            message_type="suggestion",
            metadata=_assistant_metadata(prepared),
        )
    return run_id, message_id


def _rag_metadata_from_run(conn, run_id):
    if not run_id:
        return {}
    for step in reversed(get_run_steps(conn, run_id)):
        if step["tool_name"] != "build_rag_context":
            continue
        try:
            payload = json.loads(step["output_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return {
            "rag_route": payload.get("rag_route"),
            "query_plan": payload.get("query_plan") or {},
            "retrieval_policy": payload.get("retrieval_policy"),
            "evidence_sufficiency": payload.get("evidence_sufficiency") or {},
            "evidence_card_count": payload.get("evidence_card_count") or 0,
            "current_attempt_only": bool(payload.get("current_attempt_only")),
            "allowed_evidence_ids": payload.get("allowed_evidence_ids") or [],
            "evidence_cards": payload.get("evidence_cards") or [],
        }
    return {}


def _assistant_metadata(prepared, rag_metadata=None):
    metadata = {
        "entrypoint": prepared["entrypoint"],
        "task_type": prepared["task_type"],
        "filters": prepared["filters"],
        "module": prepared["module"],
        "subject_id": prepared["subject_id"],
        "subject_ids": prepared["subject_ids"],
        "conversation_context": {
            "message_count": prepared.get("conversation_message_count", 0),
            "summarized_count": prepared.get("conversation_summarized_count", 0),
            "long_term_memory_count": len(prepared.get("long_term_memories") or []),
            "remembered_count": prepared.get("remembered_count", 0),
        },
    }
    if rag_metadata:
        metadata["rag"] = rag_metadata
        planned_module = (rag_metadata.get("query_plan") or {}).get("module")
        if planned_module:
            metadata["module"] = planned_module
    return metadata


def start_or_continue_chat(
    db_path,
    conversation_id=None,
    user_text="",
    entrypoint="chat",
    filters=None,
    module="",
    auto_approve=False,
    review_attempt_id=None,
):
    prepared = _prepare_chat_request(
        db_path,
        conversation_id=conversation_id,
        user_text=user_text,
        entrypoint=entrypoint,
        filters=filters,
        module=module,
        review_attempt_id=review_attempt_id,
    )
    if _is_memory_only_request(prepared):
        run_id, _ = _complete_memory_acknowledgement(db_path, prepared)
        return prepared["conversation_id"], run_id
    run_id = run_agent(
        db_path,
        prepared["task_type"],
        subject_id=prepared["subject_id"],
        subject_ids=prepared["subject_ids"],
        user_goal=prepared["user_goal"],
        filters=prepared["filters"],
        auto_approve=auto_approve,
        module=prepared["module"],
        conversation_id=prepared["conversation_id"],
        conversation_messages=prepared["conversation_messages"],
        conversation_summary=prepared["conversation_summary"],
        long_term_memories=prepared["long_term_memories"],
    )
    with connect(db_path) as conn:
        run = get_run(conn, run_id)
        rag_metadata = _rag_metadata_from_run(conn, run_id)
        add_message(
            conn,
            prepared["conversation_id"],
            "assistant",
            run["final_text"] if run else "",
            run_id=run_id,
            message_type="suggestion",
            metadata=_assistant_metadata(prepared, rag_metadata),
        )
    return prepared["conversation_id"], run_id


def _complete_chat_in_background(db_path, prepared, pending_message_id, auto_approve=False):
    run_id = prepared.get("run_id")
    try:
        run_agent(
            db_path,
            prepared["task_type"],
            subject_id=prepared["subject_id"],
            subject_ids=prepared["subject_ids"],
            user_goal=prepared["user_goal"],
            filters=prepared["filters"],
            auto_approve=auto_approve,
            module=prepared["module"],
            conversation_id=prepared["conversation_id"],
            conversation_messages=prepared["conversation_messages"],
            conversation_summary=prepared["conversation_summary"],
            long_term_memories=prepared["long_term_memories"],
            run_id=run_id,
        )
        for attempt in range(5):
            try:
                with connect(db_path) as conn:
                    run = get_run(conn, run_id)
                    rag_metadata = _rag_metadata_from_run(conn, run_id)
                    run_status = (run["status"] if run else "") or ""
                    message_type = "error" if run_status == "failed" else "suggestion"
                    final_content = (run["final_text"] if run else "") or (
                        "AI 教练生成失败，请到模型设置检查 API 配置。" if run_status == "failed" else "AI 教练分析完成。"
                    )
                    update_message(
                        conn,
                        pending_message_id,
                        final_content,
                        run_id=run_id,
                        message_type=message_type,
                        metadata=_assistant_metadata(prepared, rag_metadata),
                    )
                break
            except Exception as exc:
                if "locked" in str(exc).lower() and attempt < 4:
                    time.sleep(0.2 * (attempt + 1))
                else:
                    raise
    except Exception as exc:
        logging.exception("Async agent chat failed")
        for attempt in range(5):
            try:
                with connect(db_path) as conn:
                    update_message(
                        conn,
                        pending_message_id,
                        f"AI 教练运行失败：{exc}",
                        message_type="error",
                        metadata=_assistant_metadata(prepared),
                    )
                break
            except Exception:
                if attempt < 4:
                    time.sleep(0.2 * (attempt + 1))

_ACTIVE_CHAT_THREADS = []


def _track_thread(thread):
    global _ACTIVE_CHAT_THREADS
    _ACTIVE_CHAT_THREADS = [t for t in _ACTIVE_CHAT_THREADS if t.is_alive()]
    _ACTIVE_CHAT_THREADS.append(thread)


def start_or_continue_chat_async(
    db_path,
    conversation_id=None,
    user_text="",
    entrypoint="chat",
    filters=None,
    module="",
    auto_approve=False,
    review_attempt_id=None,
):
    prepared = _prepare_chat_request(
        db_path,
        conversation_id=conversation_id,
        user_text=user_text,
        entrypoint=entrypoint,
        filters=filters,
        module=module,
        review_attempt_id=review_attempt_id,
    )
    if _is_memory_only_request(prepared):
        _, message_id = _complete_memory_acknowledgement(db_path, prepared)
        return prepared["conversation_id"], message_id
    run_id = create_agent_run(
        db_path,
        prepared["task_type"],
        subject_id=prepared["subject_id"],
        subject_ids=prepared["subject_ids"],
        user_goal=prepared["user_goal"],
    )
    prepared["run_id"] = run_id
    with connect(db_path) as conn:
        pending_id = add_message(
            conn,
            prepared["conversation_id"],
            "assistant",
            "已收到问题，正在后台生成回复。你可以先去看题库、统计或其它页面，回来后这里会自动更新。",
            run_id=run_id,
            message_type="pending",
            metadata=_assistant_metadata(prepared),
        )
    thread = threading.Thread(
        target=_complete_chat_in_background,
        args=(db_path, prepared, pending_id, auto_approve),
        # A stalled provider request must not keep the desktop process alive
        # after the window and local HTTP server have both been closed.
        daemon=True,
    )
    thread.start()
    _track_thread(thread)
    return prepared["conversation_id"], pending_id


def _cleanup_orphaned_pending_messages(conn, conversation_id=None, max_age_seconds=180):
    sql = "SELECT id, run_id, created_at FROM agent_messages WHERE message_type = 'pending'"
    params = []
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        params.append(conversation_id)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).timestamp()
    for row in rows:
        run_id = row["run_id"]
        run = get_run(conn, run_id) if run_id else None
        if run and run["status"] == "completed" and run["final_text"]:
            update_message(
                conn,
                row["id"],
                run["final_text"],
                run_id=run_id,
                message_type="suggestion",
            )
            continue
        if run and run["status"] == "failed":
            update_message(
                conn,
                row["id"],
                run["final_text"] or "AI 教练生成失败，请重试。",
                run_id=run_id,
                message_type="error",
            )
            continue

        created_at_str = str(row["created_at"] or "")
        if not created_at_str:
            continue
        try:
            clean_str = created_at_str.strip().replace(" ", "T")
            if "T" in clean_str and not clean_str.endswith("Z") and "+" not in clean_str:
                clean_str += "Z"
            dt = datetime.fromisoformat(clean_str)
            created_ts = dt.timestamp()
            if (now_ts - created_ts) > max_age_seconds:
                if run_id:
                    fail_run(conn, run_id, "AI 教练生成超时")
                update_message(
                    conn,
                    row["id"],
                    "AI 教练生成超时。请到模型设置检查 API Key 或重新发送询问。",
                    message_type="error",
                )
        except Exception as exc:
            logging.warning("Error parsing pending message created_at: %s", exc)


def conversation_payload(conn, conversation_id):
    _cleanup_orphaned_pending_messages(conn, conversation_id)
    conversation = conn.execute(
        "SELECT * FROM agent_conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if not conversation:
        return None, []
    return conversation, get_messages(conn, conversation_id)

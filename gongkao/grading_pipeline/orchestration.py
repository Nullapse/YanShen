import json
import logging
import threading

from ..agent_modules import FEATURE_HASH_MODEL
from ..db import connect
from ..grading import (
    build_ai_prompt,
    build_revised_answer_retry_prompt,
    compact_revised_answer_linebreaks,
    normalize_revised_answer_word_count,
    parse_revised_answer_repair,
    replace_revised_answer_body,
    revised_answer_word_count_status,
)
from .common import (
    PIPELINE_VERSION,
    QUESTION_TYPE_PROFILES,
    RUBRIC_VERSION,
    _clean,
    _row_dict,
    grading_input_hash,
    question_display_max_score,
    question_score_is_estimated,
    reference_set_hash,
    rubric_source_hash,
)
from .contracts import GradingJobOptions
from .evidence import (
    _save_rubric_to_db,
    build_combined_grading_prompt,
    build_grading_prompt,
    retrieve_grading_evidence,
)
from .persistence import load_job_context as _load_job_context
from .persistence import update_job as _update_job
from .report import render_grading_report
from .rubric import (
    build_rubric_prompt,
    compact_reference_consensus,
    extract_tagged_json,
    manual_grading_basis,
    validate_rubric,
)
from .state import ACTIVE_JOB_STATUSES, GradingRunState, classify_grading_error
from .validation import validate_grading_result

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "QUESTION_TYPE_PROFILES",
    "apply_report_feedback",
    "build_grading_prompt",
    "build_rubric_prompt",
    "compact_reference_consensus",
    "create_grading_job",
    "grading_job_payload",
    "invalidate_question_rubrics",
    "manual_grading_basis",
    "question_display_max_score",
    "question_score_is_estimated",
    "render_grading_report",
    "retrieve_grading_evidence",
    "rubric_cache_status",
    "run_grading_job",
    "start_grading_job",
    "validate_grading_result",
    "validate_rubric",
]


def _repair_json_prompt(original, response, tag):
    return f"""下面的响应未能解析为规定 JSON。只修复格式，不改变事实、判断或正文。
请只输出 <{tag}> 与 </{tag}> 包裹的合法 JSON，不要输出 Markdown。

原任务：
{original}

待修复响应：
{response}
"""


def _repair_smart_response_prompt(original, response, error):
    return f"""下面的申论批改响应未通过系统校验。请只修复指出的问题，不改变材料事实。
只输出一个 <smart_grading_json> 与 </smart_grading_json> 包裹的完整合法 JSON，不要输出 Markdown。

dimension_scores 里每个维度的 score 必须是该维度 weight 的绝对分数（0 到 weight 之间），
不要按题目满分折算成十分制或别的比例；维度权重和满分都在原任务的评分基准里。

校验错误：
{error}

原任务：
{original}

待修复响应：
{response}
"""


def _smart_response_parts(response, expects_rubric):
    payload = extract_tagged_json(response, "smart_grading_json")
    if not isinstance(payload, dict):
        raise ValueError("智能批改响应不是 JSON 对象")
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("智能批改响应缺少 evaluation")
    rubric = payload.get("rubric")
    if expects_rubric and not isinstance(rubric, dict):
        raise ValueError("首次智能批改响应缺少 rubric")
    return rubric, evaluation


def _call_grading_model(
    chat_completion_func,
    settings,
    prompt,
    deep_thinking=False,
    structured=False,
):
    request_options = {
        "thinking": "enabled" if deep_thinking else "disabled",
    }
    if structured:
        request_options.update(
            {
                "response_format": {"type": "json_object"},
                # Deep-think reasoning consumes tokens before the final JSON,
                # so the structured response needs headroom to avoid truncation
                # (which used to force a repair round).
                "max_tokens": 16384,
            }
        )
    return chat_completion_func(
        settings,
        prompt,
        request_options,
    )


def run_grading_job(db_path, job_id, chat_completion_func):
    """Run the ordered grading state machine.

    Keeping status writes, the two-call retry budget, and final persistence in
    one visible sequence makes interrupted jobs auditable.
    """
    run_state = GradingRunState()
    raw_parts = run_state.raw_parts
    prompts = run_state.prompts
    logging.info("Smart grading job %s started", job_id)
    try:
        _update_job(db_path, job_id, "preparing", 8, "正在准备本题数据…")
        with connect(db_path) as conn:
            job, attempt, question, materials, references, settings, question_feedback, options = _load_job_context(
                conn, job_id
            )
        # A grading job is evaluated against the answer as it existed when the
        # user started the job, even if autosave changes the attempt meanwhile.
        attempt["answer_text"] = options.get("answer_snapshot", attempt.get("answer_text") or "")
        if (settings.get("grading_mode") or "enhanced") == "basic":
            _update_job(db_path, job_id, "grading", 40, "正在按基础规则评估作答…")
            prompt = build_ai_prompt(
                question,
                references,
                attempt,
                materials,
                options.get("custom_reference_answer") or "",
            )
            run_state.reserve_model_call()
            response, raw = _call_grading_model(
                chat_completion_func,
                settings,
                prompt,
                False,
            )
            raw_parts.append(raw)
            report_text = normalize_revised_answer_word_count(response, question.get("word_limit") or "")
            with connect(db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO grading_reports (
                        attempt_id, provider, model, report_text, prompt_text, raw_response, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ok')
                    """,
                    (
                        attempt["id"],
                        settings.get("provider_name") or "",
                        settings.get("model") or "",
                        report_text,
                        prompt,
                        "\n\n--- basic grading call ---\n".join(raw_parts),
                    ),
                )
                report_id = cursor.lastrowid
            _update_job(
                db_path,
                job_id,
                "completed",
                100,
                "基础批改完成。",
                report_id=report_id,
                retryable=0,
            )
            return report_id

        ref_hash = reference_set_hash(references)
        source_hash = rubric_source_hash(question, materials, references)
        cached_row = None
        rubric = None
        reused = False
        with connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM grading_rubrics
                 WHERE question_id = ? AND reference_set_hash = ? AND source_hash = ?
                   AND rubric_version = ? AND status = 'ready'
              ORDER BY updated_at DESC LIMIT 1
                """,
                (question["id"], ref_hash, source_hash, RUBRIC_VERSION),
            ).fetchone()
            if row:
                cached_row = _row_dict(row)
                rubric = json.loads(row["rubric_json"])
                reused = True

        deep_thinking = bool(options.get("deep_thinking"))

        if reused:
            _update_job(db_path, job_id, "reusing_rubric", 42, "已复用评分基准，正在准备综合批改…")
            consensus = {}
            retrieval_rubric = rubric
        else:
            _update_job(db_path, job_id, "building_rubric", 20, "正在轻量整理材料与参考答案…")
            try:
                with connect(db_path) as conn:
                    consensus = compact_reference_consensus(conn, references, materials)
            except Exception as consensus_error:
                logging.warning(
                    "Smart grading job %s consensus preprocessing degraded: %s",
                    job_id,
                    consensus_error,
                )
                consensus = {
                    "embedding_model": FEATURE_HASH_MODEL,
                    "preprocessing_mode": "fallback",
                    "organization_count": len(references),
                    "clusters": [],
                    "degraded": True,
                    "error": str(consensus_error)[:300],
                }
            _update_job(db_path, job_id, "building_rubric", 34, "材料预处理完成，正在整理检索证据…")
            retrieval_rubric = {"points": [], "question_type": question.get("question_type")}

        _update_job(db_path, job_id, "retrieving", 46, "正在检索本题相关训练证据…")
        try:
            with connect(db_path) as conn:
                evidence, history_meta = retrieve_grading_evidence(conn, question, attempt, retrieval_rubric, options)
        except Exception as retrieval_error:
            evidence = []
            history_meta = {
                "history_attempt_count": 0,
                "history_stable": False,
                "retrieval_degraded": True,
                "retrieval_error": str(retrieval_error)[:300],
            }

        _update_job(
            db_path,
            job_id,
            "grading",
            58,
            "已连接 AI，正在完成采分点分析与综合评分…",
        )
        prompt = (
            build_grading_prompt(
                question,
                materials,
                attempt,
                rubric,
                evidence,
                options.get("custom_reference_answer") or "",
                history_meta,
                question_feedback,
                references,
            )
            if reused
            else build_combined_grading_prompt(
                question,
                materials,
                references,
                attempt,
                consensus,
                evidence,
                options.get("custom_reference_answer") or "",
                history_meta,
                question_feedback,
            )
        )
        prompts.append(prompt)
        try:
            run_state.reserve_model_call()
            response, raw = _call_grading_model(
                chat_completion_func,
                settings,
                prompt,
                deep_thinking,
                structured=True,
            )
            raw_parts.append(raw)
        except Exception:
            if run_state.api_calls >= 2:
                raise
            run_state.reserve_model_call()
            response, raw = _call_grading_model(
                chat_completion_func,
                settings,
                prompt,
                deep_thinking,
                structured=True,
            )
            raw_parts.append(raw)

        def parse_and_validate(candidate_response):
            rubric_payload, evaluation = _smart_response_parts(candidate_response, expects_rubric=not reused)
            candidate_rubric = rubric
            if not reused:
                candidate_rubric = validate_rubric(
                    rubric_payload,
                    question,
                    materials,
                    references,
                    question_feedback,
                )
                candidate_rubric["source_hash"] = source_hash
                candidate_rubric["reference_set_hash"] = ref_hash
                candidate_rubric["consensus_summary"] = {
                    "embedding_model": consensus.get("embedding_model"),
                    "preprocessing_mode": consensus.get("preprocessing_mode"),
                    "degraded": bool(consensus.get("degraded")),
                    "organization_count": consensus.get("organization_count"),
                    "source_clause_count": consensus.get("source_clause_count"),
                    "material_clause_count": consensus.get("material_clause_count"),
                }
            candidate_result = validate_grading_result(
                evaluation,
                candidate_rubric,
                attempt.get("answer_text") or "",
                evidence,
            )
            return rubric_payload, candidate_rubric, candidate_result

        _update_job(db_path, job_id, "validating", 82, "正在校验权重、引用与综合维度分…")
        try:
            parsed_rubric, rubric, result = parse_and_validate(response)
        except Exception as validation_error:
            if run_state.api_calls >= 2:
                raise
            repair_prompt = _repair_smart_response_prompt(prompt, response, str(validation_error))
            run_state.reserve_model_call()
            response, repair_raw = _call_grading_model(
                chat_completion_func,
                settings,
                repair_prompt,
                False,
                structured=True,
            )
            raw_parts.append(repair_raw)
            parsed_rubric, rubric, result = parse_and_validate(response)

        if not reused:
            cached_row, rubric = _save_rubric_to_db(
                db_path,
                question,
                references,
                materials,
                settings,
                question_feedback,
                parsed_rubric,
                consensus,
            )
        result["revised_answer"] = compact_revised_answer_linebreaks(
            result.get("revised_answer") or "",
            question.get("word_limit") or "",
        )
        result["answer_snapshot"] = attempt.get("answer_text") or ""
        report_text = render_grading_report(result, rubric, evidence)
        report_text = normalize_revised_answer_word_count(report_text, question.get("word_limit") or "")
        status = revised_answer_word_count_status(report_text, question.get("word_limit") or "")
        if status["over_limit"] and run_state.api_calls < 2:
            _update_job(db_path, job_id, "repairing_answer", 90, "修改版答案超出硬限制，正在局部压缩…")
            retry_prompt = build_revised_answer_retry_prompt(prompt, report_text, question.get("word_limit") or "")
            run_state.reserve_model_call()
            repair_response, repair_raw = _call_grading_model(
                chat_completion_func,
                settings,
                retry_prompt,
                False,
            )
            raw_parts.append(repair_raw)
            repaired_answer = parse_revised_answer_repair(repair_response)
            if repaired_answer:
                result["revised_answer"] = repaired_answer
                report_text = render_grading_report(result, rubric, evidence)
                report_text = replace_revised_answer_body(
                    report_text, repaired_answer, question.get("word_limit") or ""
                )
        status = revised_answer_word_count_status(report_text, question.get("word_limit") or "")
        latency_ms = run_state.latency_ms()
        validation = {
            "errors": result.get("validation_errors") or [],
            "score": result.get("score"),
            "history_meta": history_meta,
            "deep_thinking": deep_thinking,
            "word_count_status": status,
        }
        with connect(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO grading_reports (
                    attempt_id, provider, model, report_text, prompt_text, raw_response, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'ok')
                """,
                (
                    attempt["id"],
                    settings.get("provider_name") or "",
                    settings.get("model") or "",
                    report_text,
                    prompt,
                    "\n\n--- smart grading call ---\n".join(raw_parts),
                ),
            )
            report_id = cursor.lastrowid
            conn.execute(
                """
                INSERT INTO grading_report_contexts (
                    report_id, rubric_id, pipeline_version, retrieval_json, result_json,
                    validation_json, rubric_snapshot_json, api_call_count, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    cached_row.get("id") if cached_row else None,
                    PIPELINE_VERSION,
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(validation, ensure_ascii=False),
                    json.dumps(rubric, ensure_ascii=False),
                    run_state.api_calls,
                    latency_ms,
                ),
            )
        completed_message = (
            f"智能批改完成，修改版答案超出字数限制 {status['over_by']} 字。"
            if status["over_limit"]
            else "智能批改完成。"
        )
        _update_job(db_path, job_id, "completed", 100, completed_message, report_id=report_id, retryable=0)
        logging.info(
            "Smart grading job %s completed in %sms with %s API call(s)",
            job_id,
            latency_ms,
            run_state.api_calls,
        )
        return report_id
    except Exception as exc:
        logging.exception("Smart grading job %s failed", job_id)
        category = classify_grading_error(exc)
        _update_job(
            db_path,
            job_id,
            "failed",
            100,
            f"智能批改失败（{category.value}）。",
            error_text=f"{category.value}: {str(exc)[:950]}",
            retryable=1,
        )
        return None


def create_grading_job(
    conn,
    attempt,
    settings,
    reference_ids,
    custom_answer,
    options: GradingJobOptions | dict | None = None,
):
    options = dict(options or {})
    options.update(
        {
            "reference_ids": sorted({int(value) for value in reference_ids}),
            "custom_reference_answer": custom_answer or "",
            "analogies": bool(options.get("analogies", True)),
            "knowledge": bool(options.get("knowledge", True)),
            "history": bool(options.get("history", True)),
            "deep_thinking": bool(options.get("deep_thinking", False)),
            "answer_snapshot": _row_dict(attempt).get("answer_text") or "",
        }
    )
    active = conn.execute(
        f"SELECT * FROM grading_jobs WHERE attempt_id = ? AND status IN ({','.join('?' for _ in ACTIVE_JOB_STATUSES)}) ORDER BY id DESC LIMIT 1",
        (attempt["id"], *ACTIVE_JOB_STATUSES),
    ).fetchone()
    if active:
        return _row_dict(active), False
    input_hash = grading_input_hash(
        _row_dict(attempt),
        options["reference_ids"],
        custom_answer,
        {key: options[key] for key in ("analogies", "knowledge", "history", "deep_thinking")},
        settings["model"] if settings else "",
    )
    cursor = conn.execute(
        """
        INSERT INTO grading_jobs (attempt_id, input_hash, status, progress, message, options_json)
        VALUES (?, ?, 'queued', 0, '等待开始智能批改…', ?)
        """,
        (attempt["id"], input_hash, json.dumps(options, ensure_ascii=False)),
    )
    return _row_dict(conn.execute("SELECT * FROM grading_jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()), True


def start_grading_job(db_path, job_id, chat_completion_func):
    thread = threading.Thread(
        target=run_grading_job,
        args=(db_path, int(job_id), chat_completion_func),
        name=f"grading-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return thread


def grading_job_payload(row):
    row = _row_dict(row)
    try:
        preview = json.loads(row.get("options_json") or "{}").get("_failed_preview") or {}
    except (TypeError, json.JSONDecodeError):
        preview = {}
    return {
        "job_id": row.get("id"),
        "attempt_id": row.get("attempt_id"),
        "status": row.get("status"),
        "progress": row.get("progress") or 0,
        "message": row.get("message") or "",
        "error": row.get("error_text") or "",
        "report_id": row.get("report_id"),
        "retryable": bool(row.get("retryable")),
        "preview_available": bool(preview.get("report_text")),
        "preview_anchor": f"grading-preview-{row.get('id')}",
        "complete": row.get("status") in {"completed", "failed", "interrupted"},
    }


def rubric_cache_status(conn, question_id, references, question, materials):
    ref_hash = reference_set_hash(references)
    source_hash = rubric_source_hash(question, materials, references)
    row = conn.execute(
        """
        SELECT id, updated_at FROM grading_rubrics
         WHERE question_id = ? AND reference_set_hash = ? AND source_hash = ?
           AND rubric_version = ? AND status = 'ready'
      ORDER BY updated_at DESC LIMIT 1
        """,
        (question_id, ref_hash, source_hash, RUBRIC_VERSION),
    ).fetchone()
    return {
        "cached": bool(row),
        "rubric_id": row["id"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


def apply_report_feedback(conn, report_id, point_key, corrected_status, corrected_quote="", note="", scope="report"):
    report = conn.execute(
        """
        SELECT gr.*, a.question_id, a.answer_text
          FROM grading_reports gr JOIN attempts a ON a.id = gr.attempt_id
         WHERE gr.id = ?
        """,
        (report_id,),
    ).fetchone()
    context = conn.execute("SELECT * FROM grading_report_contexts WHERE report_id = ?", (report_id,)).fetchone()
    if not report or not context:
        raise ValueError("该报告不是可逐点纠错的智能批改报告")
    corrected_status = corrected_status if corrected_status in {"hit", "partial", "miss", "invalid"} else ""
    scope = "question" if scope == "question" else "report"
    if corrected_status in {"hit", "partial"} and (not corrected_quote or corrected_quote not in report["answer_text"]):
        raise ValueError("命中或部分命中时，修正引用必须是用户答案中的连续原文")
    conn.execute(
        """
        INSERT INTO grading_feedback (
            report_id, attempt_id, question_id, point_key, scope,
            corrected_status, corrected_quote, note, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(report_id, point_key, scope) DO UPDATE SET
            corrected_status = excluded.corrected_status,
            corrected_quote = excluded.corrected_quote,
            note = excluded.note,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            report_id,
            report["attempt_id"],
            report["question_id"],
            point_key,
            scope,
            corrected_status,
            corrected_quote or "",
            note or "",
        ),
    )
    rubric_row = conn.execute("SELECT * FROM grading_rubrics WHERE id = ?", (context["rubric_id"],)).fetchone()
    rubric_json = rubric_row["rubric_json"] if rubric_row else context["rubric_snapshot_json"]
    rubric = json.loads(rubric_json or "{}")
    if not rubric.get("points"):
        raise ValueError("评分基准快照已不存在")
    result = json.loads(context["result_json"])
    evidence = json.loads(context["retrieval_json"] or "[]")
    for match in result.get("point_matches", []):
        if match.get("point_key") != point_key:
            continue
        match["status"] = corrected_status
        match["coverage_ratio"] = 1.0 if corrected_status == "hit" else (0.5 if corrected_status == "partial" else 0.0)
        match["answer_quote"] = corrected_quote if corrected_status in {"hit", "partial"} else ""
        match["feedback_applied"] = True
        match["feedback_note"] = _clean(note, 240)
        break
    result["score_status"] = "stale"
    result["feedback_applied"] = True
    report_text = render_grading_report(result, rubric, evidence)
    question = conn.execute("SELECT word_limit FROM questions WHERE id = ?", (report["question_id"],)).fetchone()
    report_text = normalize_revised_answer_word_count(report_text, question["word_limit"] if question else "")
    conn.execute("UPDATE grading_reports SET report_text = ? WHERE id = ?", (report_text, report_id))
    conn.execute(
        "UPDATE grading_report_contexts SET result_json = ?, validation_json = ? WHERE report_id = ?",
        (
            json.dumps(result, ensure_ascii=False),
            json.dumps(
                {
                    "feedback_applied": True,
                    "score_status": "stale",
                    "previous_score": result.get("score"),
                    "errors": result.get("validation_errors") or [],
                },
                ensure_ascii=False,
            ),
            report_id,
        ),
    )
    return result


def invalidate_question_rubrics(conn, question_id):
    cursor = conn.execute(
        "UPDATE grading_rubrics SET status = 'stale', updated_at = CURRENT_TIMESTAMP WHERE question_id = ? AND status = 'ready'",
        (int(question_id),),
    )
    return cursor.rowcount

import hashlib
import json
import math
import sys
import time
import types
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .agent_chat import start_or_continue_chat
from .agent_graph import AgentRunError, create_agent_run, run_agent
from .agent_modules import classify_module_heuristic, retrieve_module_evidence
from .agent_prompts import AGENT_PROMPT_VERSION
from .agent_store import get_run, get_run_steps
from .agent_tools import retrieve_candidates
from .ai import chat_completion, resolve_api_key
from .ai_config import load_effective_agent_settings
from .db import connect
from .paths import resource_root

EVAL_SUITE_NAME = "agent-v2-m0-v1"
EVAL_DATASET_VERSION = "agent-v2-regression-v1"
MULTITURN_DATASET_VERSION = "agent-v2-multiturn-v1"
EVAL_DATASET_PATH = resource_root() / "evals" / "agent_v2" / "regression-v1.jsonl"
DETERMINISTIC_DATASET_PATH = resource_root() / "evals" / "agent_v2" / "deterministic-v1.jsonl"
MULTITURN_DATASET_PATH = resource_root() / "evals" / "agent_v2" / "multiturn-v1.jsonl"
VALID_TASK_TYPES = {"diagnosis", "review", "recommend"}
REQUIRED_CASE_FIELDS = {"id", "title", "task_type", "goal"}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_version(*names):
    digest = hashlib.sha256()
    package_dir = Path(__file__).resolve().parent
    for name in names:
        path = package_dir / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def load_eval_cases(dataset_path=None, tags=None):
    path = Path(dataset_path or EVAL_DATASET_PATH)
    if not path.exists():
        return []
    wanted_tags = {str(tag).strip() for tag in (tags or []) if str(tag).strip()}
    cases = []
    seen_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON：{exc}") from exc
            missing = REQUIRED_CASE_FIELDS - set(case)
            if missing:
                raise ValueError(f"{path}:{line_number} 缺少字段：{', '.join(sorted(missing))}")
            case_id = str(case["id"]).strip()
            if not case_id or case_id in seen_ids:
                raise ValueError(f"{path}:{line_number} 用例 ID 为空或重复：{case_id}")
            if case["task_type"] not in VALID_TASK_TYPES:
                raise ValueError(f"{path}:{line_number} task_type 非法：{case['task_type']}")
            seen_ids.add(case_id)
            case["id"] = case_id
            case["tags"] = [str(tag) for tag in case.get("tags") or []]
            if wanted_tags and not wanted_tags.intersection(case["tags"]):
                continue
            cases.append(case)
    if not cases:
        raise ValueError(f"评测数据集没有匹配用例：{path}")
    return cases


def load_multiturn_cases(dataset_path=None):
    path = Path(dataset_path or MULTITURN_DATASET_PATH)
    cases = []
    seen_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON：{exc}") from exc
            missing = {"id", "title", "category", "expected_context_terms"} - set(case)
            if missing:
                raise ValueError(f"{path}:{line_number} 缺少字段：{', '.join(sorted(missing))}")
            case_id = str(case["id"]).strip()
            if not case_id or case_id in seen_ids:
                raise ValueError(f"{path}:{line_number} 用例 ID 为空或重复：{case_id}")
            if not case.get("turns") and not case.get("threads"):
                raise ValueError(f"{path}:{line_number} 必须包含 turns 或 threads")
            seen_ids.add(case_id)
            cases.append(case)
    if not cases:
        raise ValueError(f"多轮评测数据集为空：{path}")
    return cases


def _term_accuracy(text, terms):
    terms = [str(term).strip() for term in (terms or []) if str(term).strip()]
    if not terms:
        return 1.0
    text = text or ""
    return round(sum(1 for term in terms if term in text) / len(terms), 4)


def _json_from_model(text):
    text = text or ""
    fenced = __import__("re").findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=__import__("re").S | __import__("re").I)
    candidates = list(reversed(fenced)) + [text]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (TypeError, json.JSONDecodeError):
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                parsed = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _bounded_score(value):
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return None


def _judge_multiturn_case(settings, case, transcript):
    """Use the configured model as a semantic judge without persisting full answers."""
    compact_transcript = []
    for item in transcript:
        content = " ".join(str(item.get("content") or "").split())
        compact_transcript.append({"thread": item.get("thread"), "role": item.get("role"), "content": content[:2600]})
    prompt = (
        "你是多轮 Agent 回归评测器。只依据对话判断，不评价题库事实是否正确。"
        "请检查最后一个目标线程的回答是否真正理解了指代、继承了仍有效的约束、接受了后续纠正，"
        "并且没有把其他线程的临时信息串入。各分数为 0 到 1。只返回 JSON。\n"
        f"用例类别：{case.get('category')}\n"
        f"应保留的语义：{json.dumps(case.get('expected_context_terms') or [], ensure_ascii=False)}\n"
        f"不得保留的语义：{json.dumps(case.get('forbidden_context_terms') or [], ensure_ascii=False)}\n"
        f"最后回答应覆盖：{json.dumps(case.get('final_expected_keywords') or [], ensure_ascii=False)}\n"
        f"对话：{json.dumps(compact_transcript, ensure_ascii=False)}\n"
        '返回：{"context_retention":0.0,"constraint_adherence":0.0,"correction_adherence":0.0,'
        '"thread_isolation":0.0,"answer_relevance":0.0,"reason":"不超过80字"}'
    )
    try:
        content, _ = chat_completion(settings, prompt)
        parsed = _json_from_model(content)
    except Exception as exc:
        return {"status": "failed", "error": _clip(exc, 500), "scores": {}, "reason": ""}
    scores = {}
    for key in ("context_retention", "constraint_adherence", "correction_adherence", "thread_isolation", "answer_relevance"):
        value = _bounded_score(parsed.get(key))
        if value is not None:
            scores[key] = value
    return {
        "status": "ok" if scores else "invalid",
        "scores": scores,
        "reason": _clip(parsed.get("reason"), 180),
    }


def _clear_multiturn_runtime(conn):
    """Reset only ephemeral Agent state inside an isolated evaluation database."""
    for table in (
        "training_plan_items",
        "agent_feedback",
        "agent_memories",
        "agent_messages",
        "agent_conversations",
        "agent_steps",
        "agent_runs",
        "agent_eval_results",
        "agent_weakness_profile",
    ):
        conn.execute(f"DELETE FROM {table}")


def _message_metadata(conn, conversation_id, run_id):
    row = conn.execute(
        """
        SELECT metadata_json
          FROM agent_messages
         WHERE conversation_id = ? AND run_id = ? AND role = 'assistant'
      ORDER BY id DESC LIMIT 1
        """,
        (conversation_id, run_id),
    ).fetchone()
    return _json_dict(row["metadata_json"] if row else "")


def _multiturn_case_score(case, transcript, turn_records, judge):
    final_text = next(
        (item["content"] for item in reversed(transcript) if item.get("role") == "assistant"),
        "",
    )
    expected_keywords = case.get("final_expected_keywords") or []
    expected_context = case.get("expected_context_terms") or []
    forbidden = case.get("forbidden_context_terms") or []
    keyword_accuracy = _term_accuracy(final_text, expected_keywords)
    context_literal_accuracy = _term_accuracy(final_text, expected_context)
    forbidden_accuracy = 1.0 if not forbidden else round(
        sum(1 for term in forbidden if term not in final_text) / len(forbidden), 4
    )
    continuity_checks = []
    turns_by_thread = defaultdict(int)
    for turn in turn_records:
        turns_by_thread[turn["thread"]] += 1
        expected_message_count = turns_by_thread[turn["thread"]] * 2 - 1
        actual = ((turn.get("metadata") or {}).get("conversation_context") or {}).get("message_count", 0)
        continuity_checks.append(actual >= expected_message_count)
    continuity = round(sum(continuity_checks) / len(continuity_checks), 4) if continuity_checks else 0.0
    judge_scores = judge.get("scores") or {}
    semantic_keys = ["context_retention", "answer_relevance"]
    if case.get("category") == "constraint":
        semantic_keys.append("constraint_adherence")
    elif case.get("category") == "correction":
        semantic_keys.append("correction_adherence")
    elif case.get("category") == "isolation":
        semantic_keys.append("thread_isolation")
    semantic = _mean([judge_scores.get(key) for key in semantic_keys])
    deterministic = (
        keyword_accuracy * 0.4
        + forbidden_accuracy * 0.25
        + continuity * 0.25
        + context_literal_accuracy * 0.1
    )
    score = deterministic if semantic is None else deterministic * 0.25 + semantic * 0.75
    return {
        "score": round(score * 100, 1),
        "final_keyword_accuracy": keyword_accuracy,
        "context_literal_accuracy": context_literal_accuracy,
        "forbidden_term_accuracy": forbidden_accuracy,
        "thread_continuity": continuity,
        "semantic_score": semantic,
        "semantic_success": semantic is not None and semantic >= 0.8,
        "deterministic_score": round(deterministic, 4),
        "judge": judge,
    }


def run_multiturn_eval_suite(
    db_path,
    dataset_path=None,
    categories=None,
    case_limit=None,
    run_judge=True,
    output_path=None,
    cases=None,
    reset_between_cases=True,
):
    """Run multi-turn cases sequentially on one isolated worker database."""
    dataset_path = Path(dataset_path or MULTITURN_DATASET_PATH)
    retrieval_version = _source_version("agent_rag.py", "agent_modules.py")
    cases = list(cases or load_multiturn_cases(dataset_path))
    wanted = {str(value) for value in (categories or []) if str(value)}
    if wanted:
        cases = [case for case in cases if case.get("category") in wanted]
    if case_limit is not None:
        cases = cases[: max(0, int(case_limit))]
    results = []
    for case in cases:
        if reset_between_cases:
            with connect(db_path) as conn:
                _clear_multiturn_runtime(conn)
        transcript = []
        turn_records = []
        errors = []
        thread_conversations = {}
        thread_groups = case.get("threads") or {"main": case.get("turns") or []}
        for thread_name, turns in thread_groups.items():
            conversation_id = None
            for index, turn in enumerate(turns, start=1):
                user_text = str(turn.get("user") or "").strip()
                transcript.append({"thread": thread_name, "role": "user", "content": user_text})
                started = time.perf_counter()
                try:
                    conversation_id, run_id = start_or_continue_chat(
                        db_path,
                        conversation_id=conversation_id,
                        user_text=user_text,
                        auto_approve=True,
                    )
                except Exception as exc:
                    errors.append(f"{thread_name}:{index}: {_clip(exc, 500)}")
                    break
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                with connect(db_path) as conn:
                    run = get_run(conn, run_id)
                    steps = get_run_steps(conn, run_id)
                    final_text = run["final_text"] if run else ""
                    metadata = _message_metadata(conn, conversation_id, run_id)
                transcript.append({"thread": thread_name, "role": "assistant", "content": final_text})
                turn_records.append(
                    {
                        "thread": thread_name,
                        "turn": index,
                        "run_id": run_id,
                        "duration_ms": duration_ms,
                        "status": run["status"] if run else "missing",
                        "token_usage": _token_usage(steps),
                        "metadata": metadata,
                        "answer_length": len(final_text or ""),
                        "answer_sha256": hashlib.sha256((final_text or "").encode("utf-8")).hexdigest()[:16],
                        "answer_preview": _clip(final_text, 260),
                    }
                )
            if conversation_id is not None:
                thread_conversations[thread_name] = conversation_id
        with connect(db_path) as conn:
            settings = load_effective_agent_settings(conn)
            judge = (
                _judge_multiturn_case(settings, case, transcript)
                if run_judge and settings and not errors
                else {"status": "skipped", "scores": {}, "reason": ""}
            )
        metrics = _multiturn_case_score(case, transcript, turn_records, judge)
        metrics["errors"] = errors
        metrics["turn_count"] = len(turn_records)
        metrics["duration_ms"] = round(sum(turn["duration_ms"] for turn in turn_records), 2)
        metrics["token_usage"] = {
            key: sum(int((turn["token_usage"] or {}).get(key) or 0) for turn in turn_records)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        metrics["runtime"] = {
            "prompt_version": AGENT_PROMPT_VERSION,
            "retrieval_version": retrieval_version,
            "dataset_version": MULTITURN_DATASET_VERSION,
            "case_sha256": _case_hash(case),
        }
        if errors:
            metrics["score"] = 0.0
        results.append(
            {
                "case": case,
                "conversations": thread_conversations,
                "turns": turn_records,
                "metrics": metrics,
            }
        )
    if output_path:
        write_multiturn_report(results, output_path, dataset_path)
    return results


def build_multiturn_report(results, dataset_path=None):
    dataset_path = Path(dataset_path or MULTITURN_DATASET_PATH)
    categories = defaultdict(list)
    durations = []
    tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for item in results:
        metrics = item["metrics"]
        categories[item["case"]["category"]].append(metrics.get("score"))
        durations.extend(turn.get("duration_ms", 0) for turn in item.get("turns") or [])
        for key in tokens:
            tokens[key] += int((metrics.get("token_usage") or {}).get(key) or 0)
    sorted_durations = sorted(durations)

    def percentile(fraction):
        if not sorted_durations:
            return None
        index = min(len(sorted_durations) - 1, max(0, math.ceil(len(sorted_durations) * fraction) - 1))
        return round(sorted_durations[index], 2)

    return {
        "schema_version": "agent-multiturn-eval-report-v1",
        "suite_name": MULTITURN_DATASET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"path": str(dataset_path), "sha256": _sha256_file(dataset_path), "case_count": len(results)},
        "versions": {
            "prompt": next(
                (item["metrics"].get("runtime", {}).get("prompt_version") for item in results if item["metrics"].get("runtime")),
                AGENT_PROMPT_VERSION,
            ),
            "retrieval": next(
                (item["metrics"].get("runtime", {}).get("retrieval_version") for item in results if item["metrics"].get("runtime")),
                _source_version("agent_rag.py", "agent_modules.py"),
            ),
        },
        "summary": {
            "average_score": _mean([item["metrics"].get("score") for item in results]),
            "failed_cases": sum(1 for item in results if item["metrics"].get("errors")),
            "case_count": len(results),
            "turn_count": sum(len(item.get("turns") or []) for item in results),
            "category_scores": {key: _mean(values) for key, values in sorted(categories.items())},
            "semantic_average": _mean([item["metrics"].get("semantic_score") for item in results]),
            "semantic_success_rate": _mean([
                1.0 if item["metrics"].get("semantic_success") else 0.0
                for item in results
                if item["metrics"].get("semantic_score") is not None
            ]),
            "latency_ms": {"p50": percentile(0.5), "p95": percentile(0.95)},
            "token_usage": tokens,
        },
        "results": results,
    }


def write_multiturn_report(results, output_path, dataset_path=None):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = build_multiturn_report(results, dataset_path)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


EVAL_CASES = load_eval_cases(tags={"smoke"})


class RagasUnavailable(Exception):
    pass


def _normalize_base_url(value):
    base = (value or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    return base


def _clip(value, limit=900):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _install_ragas_import_shims():
    # Ragas 0.4.x imports this legacy LangChain module even when Vertex AI is not used.
    # The app only uses OpenAI-compatible clients, so a narrow placeholder keeps imports local.
    module_name = "langchain_community.chat_models.vertexai"
    if module_name not in sys.modules:
        shim = types.ModuleType(module_name)

        class ChatVertexAI:
            pass

        shim.ChatVertexAI = ChatVertexAI
        sys.modules[module_name] = shim


def _load_ragas_components():
    _install_ragas_import_shims()
    try:
        from openai import OpenAI
        from ragas.llms import llm_factory
        from ragas.metrics.collections import (
            ContextPrecision,
            ContextRecall,
            FactualCorrectness,
            Faithfulness,
        )
    except ImportError as exc:
        raise RagasUnavailable(str(exc)) from exc
    return {
        "OpenAI": OpenAI,
        "llm_factory": llm_factory,
        "Faithfulness": Faithfulness,
        "ContextPrecision": ContextPrecision,
        "ContextRecall": ContextRecall,
        "FactualCorrectness": FactualCorrectness,
    }


def _metric_value(result):
    raw_value = result.value if hasattr(result, "value") else result
    if isinstance(raw_value, bool):
        return 1.0 if raw_value else 0.0
    if isinstance(raw_value, (int, float)):
        return max(0.0, min(1.0, float(raw_value)))
    return None


def _metric_reason(result):
    reason = getattr(result, "reason", None)
    return _clip(reason, 500) if reason else ""


def _run_metric(metric, name, args, scores, reasons, errors):
    try:
        result = metric.score(**args)
    except Exception as exc:  # Ragas metrics are model-backed; keep failures isolated.
        errors[name] = _clip(str(exc), 700)
        return
    value = _metric_value(result)
    if value is None:
        errors[name] = f"指标返回了非数值结果：{_clip(result, 180)}"
        return
    scores[name] = round(value, 4)
    reason = _metric_reason(result)
    if reason:
        reasons[name] = reason


def _candidate_context(question):
    bits = [
        f"题目：{question.get('title')}",
        f"题型：{question.get('question_type')}",
        f"地区：{question.get('region')}",
        f"年份：{question.get('year')}",
        f"试卷：{question.get('paper_name') or question.get('exam_type')}",
        f"题号：{question.get('question_number') or question.get('question_code')}",
        f"字数：{question.get('word_limit') or '未标注'}",
        f"已作答：{question.get('attempt_count', 0)} 次",
        f"批改报告：{question.get('report_count', 0)} 份",
    ]
    return "；".join(bits)


def build_ragas_contexts(conn, case, run):
    contexts = []
    input_summary = run["input_summary"] if run else ""
    if input_summary:
        contexts.append(f"训练画像摘要：{_clip(input_summary, 800)}")
    for question in retrieve_candidates(conn, case.get("filters") or {}, limit=6):
        contexts.append(_candidate_context(question))
    if case.get("task_type") == "diagnosis":
        module_context = retrieve_module_evidence(
            conn,
            classify_module_heuristic(case.get("goal") or ""),
            case.get("goal") or "",
            case.get("filters") or {},
        )
        for chunk in (module_context.get("evidence_chunks") or [])[:6]:
            contexts.append(
                "；".join(
                    [
                        f"证据：{chunk.get('evidence_ref')}",
                        f"来源：{chunk.get('source_type')}",
                        f"标题：{chunk.get('title')}",
                        f"内容：{_clip(chunk.get('body'), 500)}",
                    ]
                )
            )
        for item in (module_context.get("weakness_profile") or [])[:4]:
            contexts.append(
                f"能力画像：{item.get('problem_type')}；频次：{item.get('frequency')}；严重度：{item.get('severity')}"
            )
    return [context for context in contexts if context.strip()]


def compute_ragas_metrics(settings, case, final_text, retrieved_contexts):
    metrics = {
        "ragas_available": False,
        "ragas_status": "not_run",
        "ragas_scores": {},
        "ragas_reasons": {},
        "ragas_errors": {},
    }
    if not retrieved_contexts:
        metrics["ragas_status"] = "skipped_no_context"
        metrics["ragas_errors"]["context"] = "没有可用于 Ragas 的检索上下文。"
        return metrics
    if not (final_text or "").strip():
        metrics["ragas_status"] = "skipped_empty_response"
        metrics["ragas_errors"]["response"] = "Agent 没有生成可评测的回答。"
        return metrics
    api_key = resolve_api_key(settings) if settings else ""
    if not api_key:
        metrics["ragas_status"] = "skipped_no_model"
        metrics["ragas_errors"]["model"] = "当前没有可用模型连接，无法运行模型评审指标。"
        return metrics

    try:
        components = _load_ragas_components()
    except RagasUnavailable as exc:
        metrics["ragas_status"] = "unavailable"
        metrics["ragas_errors"]["import"] = _clip(str(exc), 700)
        return metrics

    metrics["ragas_available"] = True
    scores = {}
    reasons = {}
    errors = {}
    reference = case.get("reference") or ""
    user_input = case.get("goal") or case.get("title") or ""

    try:
        client = components["OpenAI"](
            api_key=api_key,
            base_url=_normalize_base_url(settings["api_base_url"]),
            timeout=120,
        )
        ragas_llm = components["llm_factory"](
            (settings["model"] or "").strip(),
            client=client,
        )
    except Exception as exc:
        metrics["ragas_status"] = "failed_model_setup"
        metrics["ragas_errors"]["model"] = _clip(str(exc), 700)
        return metrics

    common_context_args = {
        "user_input": user_input,
        "response": final_text,
        "retrieved_contexts": retrieved_contexts,
    }
    _run_metric(
        components["Faithfulness"](llm=ragas_llm),
        "faithfulness",
        common_context_args,
        scores,
        reasons,
        errors,
    )
    if reference:
        _run_metric(
            components["ContextPrecision"](llm=ragas_llm),
            "context_precision",
            {
                "user_input": user_input,
                "reference": reference,
                "retrieved_contexts": retrieved_contexts,
            },
            scores,
            reasons,
            errors,
        )
        _run_metric(
            components["ContextRecall"](llm=ragas_llm),
            "context_recall",
            {
                "user_input": user_input,
                "reference": reference,
                "retrieved_contexts": retrieved_contexts,
            },
            scores,
            reasons,
            errors,
        )
        _run_metric(
            components["FactualCorrectness"](llm=ragas_llm),
            "factual_correctness",
            {"response": final_text, "reference": reference},
            scores,
            reasons,
            errors,
        )

    metrics["ragas_scores"] = scores
    metrics["ragas_reasons"] = reasons
    metrics["ragas_errors"] = errors
    for key, value in scores.items():
        metrics[f"ragas_{key}"] = value
    if scores:
        metrics["ragas_average"] = round(sum(scores.values()) / len(scores), 4)
        metrics["ragas_status"] = "partial" if errors else "ok"
    else:
        metrics["ragas_status"] = "failed"
    return metrics


def _json_dict(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _step_output(step):
    try:
        value = step["output_json"]
    except (KeyError, TypeError, IndexError):
        value = step.get("output") if isinstance(step, dict) else None
    return _json_dict(value)


def _step_value(step, key, default=""):
    try:
        return step[key]
    except (KeyError, TypeError, IndexError):
        return step.get(key, default) if isinstance(step, dict) else default


def retrieval_ranking_metrics(retrieved_ids, gold_ids):
    retrieved = list(dict.fromkeys(str(item) for item in (retrieved_ids or []) if item))
    gold = set(str(item) for item in (gold_ids or []) if item)
    metrics = {
        "retrieved_count": len(retrieved),
        "gold_count": len(gold),
        "recall_at_5": None,
        "recall_at_10": None,
        "mrr": None,
        "ndcg_at_10": None,
    }
    if not gold:
        return metrics
    metrics["recall_at_5"] = round(len(gold.intersection(retrieved[:5])) / len(gold), 4)
    metrics["recall_at_10"] = round(len(gold.intersection(retrieved[:10])) / len(gold), 4)
    relevant_ranks = [index for index, item in enumerate(retrieved, start=1) if item in gold]
    metrics["mrr"] = round(1.0 / relevant_ranks[0], 4) if relevant_ranks else 0.0
    dcg = sum(1.0 / math.log2(index + 1) for index, item in enumerate(retrieved[:10], start=1) if item in gold)
    ideal_count = min(len(gold), 10)
    ideal_dcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
    metrics["ndcg_at_10"] = round(dcg / ideal_dcg, 4) if ideal_dcg else 0.0
    return metrics


def _retrieved_evidence_ids(steps):
    for step in reversed(steps):
        if _step_value(step, "tool_name") != "build_rag_context":
            continue
        output = _step_output(step)
        return list(output.get("allowed_evidence_ids") or output.get("retrieved_evidence_ids") or [])
    return []


def _planner_metrics(steps, expected_plan):
    planner_step = next(
        (step for step in steps if _step_value(step, "step_type") == "planner"),
        None,
    )
    if not planner_step:
        return {
            "step_present": False,
            "structured_output": False,
            "expected_fields": len(expected_plan or {}),
            "matched_fields": 0,
            "accuracy": 0.0 if expected_plan else None,
        }
    output = _step_output(planner_step)
    plan = output.get("rag_query_plan") if isinstance(output.get("rag_query_plan"), dict) else output
    expected_plan = expected_plan or {}
    matched = sum(1 for key, value in expected_plan.items() if plan.get(key) == value or output.get(key) == value)
    return {
        "step_present": True,
        "structured_output": bool(output),
        "expected_fields": len(expected_plan),
        "matched_fields": matched,
        "accuracy": round(matched / len(expected_plan), 4) if expected_plan else None,
        "module": output.get("module") or plan.get("module"),
        "action": plan.get("action"),
        "scope": plan.get("scope"),
    }


def _token_usage(steps):
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    available = False
    for step in steps:
        usage = _step_output(step).get("token_usage")
        if not isinstance(usage, dict):
            continue
        for key in totals:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
                available = True
    totals["available"] = available
    return totals


def _score_case(final_text, steps, case, ragas_metrics=None, runtime_metadata=None):
    ragas_metrics = ragas_metrics or {}
    runtime_metadata = runtime_metadata or {}
    step_tools = {_step_value(step, "tool_name") for step in steps}
    required_tools = set(case.get("required_tools") or [])
    tool_call_accuracy = (
        len(step_tools & required_tools) / len(required_tools)
        if required_tools else 1.0
    )
    expected = case.get("expected_keywords") or []
    goal_accuracy = (
        sum(1 for keyword in expected if keyword in final_text) / len(expected)
        if expected else 1.0
    )
    response_completeness = 1.0 if len((final_text or "").strip()) >= 80 else 0.5 if final_text.strip() else 0.0
    evidence_proxy = 1.0 if any(
        keyword in final_text for keyword in ("题", "材料", "作答", "报告", "训练", "复盘", "推荐")
    ) else 0.5 if final_text.strip() else 0.0
    internal_score = round(
        (tool_call_accuracy * 0.3 + goal_accuracy * 0.35 + response_completeness * 0.2 + evidence_proxy * 0.15) * 100,
        1,
    )
    ragas_average = ragas_metrics.get("ragas_average")
    score = internal_score
    if isinstance(ragas_average, (int, float)):
        score = round(internal_score * 0.55 + float(ragas_average) * 100 * 0.45, 1)
    metrics = {
        "score": score,
        "internal_score": internal_score,
        "tool_call_accuracy": round(tool_call_accuracy, 3),
        "agent_goal_accuracy": round(goal_accuracy, 3),
        "response_completeness": round(response_completeness, 3),
        "evidence_proxy": round(evidence_proxy, 3),
        "layers": {
            "planner": _planner_metrics(steps, case.get("expected_plan") or {}),
            "retriever": retrieval_ranking_metrics(
                _retrieved_evidence_ids(steps),
                case.get("gold_evidence_ids") or [],
            ),
            "tool": {
                "required": sorted(required_tools),
                "called": sorted(tool for tool in step_tools if tool),
                "accuracy": round(tool_call_accuracy, 4),
            },
            "answer": {
                "expected_keywords": expected,
                "goal_accuracy": round(goal_accuracy, 4),
                "response_completeness": round(response_completeness, 4),
            },
        },
        "runtime": {
            **runtime_metadata,
            "prompt_version": AGENT_PROMPT_VERSION,
            "retrieval_version": _source_version("agent_rag.py", "agent_modules.py"),
            "token_usage": _token_usage(steps),
        },
    }
    metrics.update(ragas_metrics)
    return metrics


def _case_hash(case):
    payload = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _persist_eval_result(conn, suite_name, case, run_id, metrics):
    notes = f"run_id={run_id}" if run_id is not None else "deterministic_fixture"
    if metrics.get("error"):
        notes += f"; error={_clip(metrics['error'], 300)}"
    conn.execute(
        """
        INSERT INTO agent_eval_results (
            suite_name, case_id, case_title, task_type, score, metrics_json, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            suite_name,
            case["id"],
            case["title"],
            case["task_type"],
            metrics["score"],
            json.dumps(metrics, ensure_ascii=False),
            notes,
        ),
    )


def run_eval_suite(
    db_path,
    suite_name=EVAL_SUITE_NAME,
    dataset_path=None,
    tags=("smoke",),
    case_limit=None,
    run_ragas=True,
    output_path=None,
):
    dataset_path = Path(dataset_path or EVAL_DATASET_PATH)
    cases = load_eval_cases(dataset_path, tags=tags)
    if case_limit is not None:
        cases = cases[: max(0, int(case_limit))]
    results = []
    dataset_hash = _sha256_file(dataset_path)
    for case in cases:
        started = time.perf_counter()
        run_id = create_agent_run(
            db_path,
            case["task_type"],
            user_goal=case["goal"],
        )
        error = ""
        try:
            run_agent(
                db_path,
                case["task_type"],
                user_goal=case["goal"],
                filters=case.get("filters") or {},
                auto_approve=True,
                module=(case.get("expected_plan") or {}).get("module", ""),
                run_id=run_id,
            )
        except AgentRunError as exc:
            error = str(exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        with connect(db_path) as conn:
            settings = load_effective_agent_settings(conn)
            run = get_run(conn, run_id)
            steps = get_run_steps(conn, run_id)
            final_text = run["final_text"] if run else ""
            ragas_contexts = build_ragas_contexts(conn, case, run) if run and not error and run_ragas else []
            ragas_metrics = (
                compute_ragas_metrics(settings, case, final_text, ragas_contexts)
                if run_ragas and not error
                else {
                    "ragas_available": False,
                    "ragas_status": "skipped_by_config" if not error else "skipped_run_failed",
                    "ragas_scores": {},
                    "ragas_reasons": {},
                    "ragas_errors": {},
                }
            )
            runtime = {
                "dataset_version": EVAL_DATASET_VERSION,
                "dataset_sha256": dataset_hash,
                "case_sha256": _case_hash(case),
                "duration_ms": duration_ms,
                "status": run["status"] if run else "missing",
                "provider": run["provider"] if run else "",
                "model": run["model"] if run else "",
            }
            metrics = _score_case(final_text, steps, case, ragas_metrics, runtime)
            metrics["ragas_context_count"] = len(ragas_contexts)
            if error:
                metrics["score"] = 0.0
                metrics["error"] = error
            _persist_eval_result(conn, suite_name, case, run_id, metrics)
            results.append({"case": case, "run_id": run_id, "metrics": metrics})
    if output_path:
        write_eval_report(
            results,
            output_path,
            suite_name=suite_name,
            dataset_path=dataset_path,
            mode="live",
        )
    return results


def evaluate_deterministic_suite(dataset_path=None, output_path=None):
    dataset_path = Path(dataset_path or DETERMINISTIC_DATASET_PATH)
    cases = load_eval_cases(dataset_path)
    results = []
    for case in cases:
        fixture = case.get("fixture") or {}
        steps = fixture.get("steps") or []
        metrics = _score_case(
            fixture.get("final_text") or "",
            steps,
            case,
            {
                "ragas_available": False,
                "ragas_status": "deterministic",
                "ragas_scores": {},
                "ragas_reasons": {},
                "ragas_errors": {},
            },
            {
                "dataset_version": "agent-v2-deterministic-v1",
                "dataset_sha256": _sha256_file(dataset_path),
                "case_sha256": _case_hash(case),
                "duration_ms": 0,
                "status": "fixture",
                "provider": "none",
                "model": "none",
            },
        )
        results.append({"case": case, "run_id": None, "metrics": metrics})
    if output_path:
        write_eval_report(
            results,
            output_path,
            suite_name="agent-v2-deterministic-v1",
            dataset_path=dataset_path,
            mode="deterministic",
        )
    return results


def _mean(values):
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return round(sum(numeric) / len(numeric), 4) if numeric else None


def build_eval_report(results, suite_name, dataset_path, mode):
    scores = [item["metrics"].get("score") for item in results]
    layer_names = ("planner", "retriever", "tool", "answer")
    layer_summary = {}
    for layer in layer_names:
        if layer == "planner":
            values = [item["metrics"]["layers"][layer].get("accuracy") for item in results]
        elif layer == "retriever":
            values = [item["metrics"]["layers"][layer].get("recall_at_10") for item in results]
        elif layer == "tool":
            values = [item["metrics"]["layers"][layer].get("accuracy") for item in results]
        else:
            values = [item["metrics"]["layers"][layer].get("goal_accuracy") for item in results]
        layer_summary[layer] = _mean(values)
    return {
        "schema_version": "agent-eval-report-v1",
        "suite_name": suite_name,
        "mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(Path(dataset_path)),
            "sha256": _sha256_file(dataset_path),
            "case_count": len(results),
        },
        "versions": {
            "prompt": AGENT_PROMPT_VERSION,
            "retrieval": _source_version("agent_rag.py", "agent_modules.py"),
        },
        "summary": {
            "average_score": _mean(scores),
            "failed_cases": sum(1 for item in results if item["metrics"].get("error")),
            "layers": layer_summary,
        },
        "results": results,
    }


def write_eval_report(results, output_path, suite_name=EVAL_SUITE_NAME, dataset_path=None, mode="live"):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = build_eval_report(results, suite_name, dataset_path or EVAL_DATASET_PATH, mode)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def compare_eval_reports(baseline, candidate):
    def load(value):
        if isinstance(value, dict):
            return value
        return json.loads(Path(value).read_text(encoding="utf-8"))

    baseline_report = load(baseline)
    candidate_report = load(candidate)
    baseline_by_id = {item["case"]["id"]: item for item in baseline_report.get("results") or []}
    candidate_by_id = {item["case"]["id"]: item for item in candidate_report.get("results") or []}
    shared_ids = sorted(set(baseline_by_id).intersection(candidate_by_id))
    cases = []
    for case_id in shared_ids:
        before = float(baseline_by_id[case_id]["metrics"].get("score") or 0)
        after = float(candidate_by_id[case_id]["metrics"].get("score") or 0)
        cases.append({"case_id": case_id, "baseline": before, "candidate": after, "delta": round(after - before, 4)})
    return {
        "schema_version": "agent-eval-comparison-v1",
        "shared_case_count": len(shared_ids),
        "baseline_only": sorted(set(baseline_by_id) - set(candidate_by_id)),
        "candidate_only": sorted(set(candidate_by_id) - set(baseline_by_id)),
        "average_delta": _mean([item["delta"] for item in cases]),
        "improved": sum(1 for item in cases if item["delta"] > 0),
        "regressed": sum(1 for item in cases if item["delta"] < 0),
        "unchanged": sum(1 for item in cases if item["delta"] == 0),
        "cases": cases,
    }


def latest_eval_results(conn, limit=20):
    return conn.execute(
        """
        SELECT * FROM agent_eval_results
      ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

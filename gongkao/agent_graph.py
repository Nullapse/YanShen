import json
import hashlib
import re
import time
from contextlib import ExitStack
from typing import Any, Dict, TypedDict

from .agent_modules import classify_module_heuristic
from .agent_prompts import (
    AGENT_PROMPT_VERSION,
    build_agent_messages,
    build_module_messages,
    wants_concise_response,
    with_conversation_history,
    with_long_term_memories,
)
from .agent_rag import normalize_query_plan
from .agent_store import add_step, complete_run, create_run, fail_run
from .agent_tools import (
    get_attempt_review_context,
    get_attempts_review_context,
    input_summary,
    load_user_context,
    retrieve_candidates,
)
from .ai import resolve_api_key
from .ai_config import load_effective_agent_settings
from .db import connect
from .agent_react import (
    MAX_MODEL_CALLS, MAX_TOOL_CALLS, MAX_RUN_SECONDS, REACT_INSTRUCTION,
    tool_specs, execute_tool, observation,
)


class AgentDependencyError(Exception):
    pass


class AgentRunError(Exception):
    pass


class AgentState(TypedDict, total=False):
    db_path: str
    run_id: int
    task_type: str
    subject_id: Any
    subject_ids: list[Any]
    user_goal: str
    filters: Dict[str, Any]
    auto_approve: bool
    module: str
    context_plan: Dict[str, Any]
    module_context: Dict[str, Any]
    rag_context: Dict[str, Any]
    user_context: Dict[str, Any]
    candidate_questions: list[Dict[str, Any]]
    review_context: Dict[str, Any]
    analysis: str
    final_text: str
    structured_output: Dict[str, Any]
    conversation_id: int
    conversation_messages: list[Dict[str, Any]]
    conversation_summary: str
    long_term_memories: list[Dict[str, Any]]
    react_messages: list[Any]
    model_calls: int
    tool_calls_count: int
    deadline: float
    pending_calls: list[Dict[str, Any]]
    tool_cache: Dict[str, Any]
    stop_reason: str


def _subject_type(task_type):
    return "attempt" if task_type == "review" else "global"


def _load_langgraph():
    try:
        from langchain_openai import ChatOpenAI
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise AgentDependencyError(
            f"未安装 LangGraph/LangChain 依赖 ({exc})。请确认环境或重新安装依赖。"
        ) from exc
    return ChatOpenAI, StateGraph, START, END


def _load_sqlite_checkpointer(db_path, stack):
    return None


def _normalize_base_url(value):
    base = (value or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    return base


def _settings(conn):
    return load_effective_agent_settings(conn)


def _record_step(db_path, run_id, step_type, tool_name, input_data, output_data):
    for attempt in range(5):
        try:
            with connect(db_path) as conn:
                add_step(conn, run_id, step_type, tool_name, input_data, output_data)
            break
        except Exception as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                time.sleep(0.1 * (attempt + 1))
            else:
                pass


def _json_object(text):
    text = text or ""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _structured_output(text):
    text = text or ""
    blocks = re.findall(r"`{3,}json\s*(.*?)\s*`{3,}", text, flags=re.S | re.I)
    for raw_json in reversed(blocks):
        try:
            parsed = json.loads(raw_json.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    parsed = _json_object(text)
    if {"summary", "weaknesses", "next_actions", "recommended_questions"} & set(parsed.keys()):
        return parsed
    return {}


def _compact_concise_response(text, structured):
    if not structured:
        return text

    def clipped(value, limit):
        value = " ".join(str(value or "").split())
        return value if len(value) <= limit else value[:limit].rstrip() + "…"

    weaknesses = list(structured.get("weaknesses") or [])[:1]
    actions = list(structured.get("next_actions") or [])[:2]
    recommendations = list(structured.get("recommended_questions") or [])[:1]
    compact = {
        "summary": clipped(structured.get("summary"), 90),
        "weaknesses": [
            {
                "name": clipped(item.get("name"), 30),
                "severity": item.get("severity") or "medium",
                "evidence_refs": list(item.get("evidence_refs") or [])[:2],
                "reason": clipped(item.get("reason"), 80),
            }
            for item in weaknesses
        ],
        "next_actions": [
            {
                "action": clipped(item.get("action"), 60),
                "target": clipped(item.get("target"), 30),
                "timebox": clipped(item.get("timebox"), 20),
            }
            for item in actions
        ],
        "recommended_questions": [
            {
                "question_id": item.get("question_id") or 0,
                "title": clipped(item.get("title"), 40),
                "reason": clipped(item.get("reason"), 60),
            }
            for item in recommendations
        ],
    }
    body_lines = [compact["summary"]]
    if compact["weaknesses"]:
        weakness = compact["weaknesses"][0]
        evidence = "、".join(str(value) for value in weakness["evidence_refs"])
        body_lines.append(f"依据：{weakness['reason']}" + (f"（{evidence}）" if evidence else ""))
    if compact["next_actions"]:
        body_lines.append("动作：" + "；".join(item["action"] for item in compact["next_actions"] if item["action"]))
    body = "\n\n".join(line for line in body_lines if line)[:260].rstrip()
    return body + "\n\n```json\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n```"


def _response_usage(response):
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, dict):
            usage = metadata.get("token_usage") or metadata.get("usage")
    if not isinstance(usage, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    normalized = {}
    for target, keys in aliases.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                normalized[target] = int(value)
                break
    if "total_tokens" not in normalized and normalized:
        normalized["total_tokens"] = normalized.get("input_tokens", 0) + normalized.get("output_tokens", 0)
    return normalized


def _conversation_excerpt(state):
    lines = []
    memories = state.get("long_term_memories") or []
    if memories:
        memory_text = "；".join(
            f"{item.get('memory_key')}={item.get('content')}"
            for item in memories[:8]
            if item.get("content")
        )
        if memory_text:
            lines.append(f"用户长期记忆：{memory_text}")
    summary = (state.get("conversation_summary") or "").strip()
    if summary:
        lines.append(f"较早消息摘要：{summary}")
    for message in (state.get("conversation_messages") or [])[-6:]:
        role = "用户" if message.get("role") == "user" else "教练"
        content = " ".join((message.get("content") or "").split())
        if len(content) > 350:
            content = content[:350].rstrip() + "…"
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _response_style(state):
    for item in state.get("long_term_memories") or []:
        if item.get("memory_key") == "response_style":
            return item.get("content") or ""
    return ""


def _rerank_evidence_with_llm(llm, user_goal, rag_context, limit=20):
    cards = list(rag_context.get("evidence_cards") or [])
    return rag_context, {"status": "ranked_by_score", "token_usage": {}}
    candidates = [
        {
            "evidence_id": card.get("evidence_id"),
            "source_type": card.get("source_type"),
            "title": card.get("title"),
            "claim": card.get("claim"),
            "content": " ".join(str(card.get("content") or "").split())[:220],
            "local_confidence": card.get("confidence"),
        }
        for card in cards[:limit]
    ]
    try:
        response = llm.invoke(
            [
                (
                    "system",
                    "你是证据重排器。只重排给定 evidence_id，不生成答案，不得添加或改写 ID。只返回 JSON。",
                ),
                (
                    "human",
                    f"用户问题：{user_goal}\n候选证据：{json.dumps(candidates, ensure_ascii=False)}\n"
                    '返回：{"ordered_evidence_ids":["id1","id2"],"reason":"一句话"}',
                ),
            ]
        )
    except Exception as exc:
        return rag_context, {"status": "failed", "error": str(exc)[:300], "token_usage": {}}
    parsed = _json_object(getattr(response, "content", str(response)))
    allowed = {card.get("evidence_id") for card in candidates if card.get("evidence_id")}
    ordered_ids = []
    for evidence_id in parsed.get("ordered_evidence_ids") or []:
        if evidence_id in allowed and evidence_id not in ordered_ids:
            ordered_ids.append(evidence_id)
    if not ordered_ids:
        return rag_context, {
            "status": "invalid",
            "token_usage": _response_usage(response),
            "reason": str(parsed.get("reason") or "")[:180],
        }
    by_id = {card.get("evidence_id"): card for card in cards}
    reordered = [by_id[evidence_id] for evidence_id in ordered_ids]
    reordered.extend(card for card in cards if card.get("evidence_id") not in ordered_ids)
    updated = dict(rag_context)
    updated["evidence_cards"] = reordered
    grounding = dict(updated.get("grounding_contract") or {})
    grounding["allowed_evidence_ids"] = [card.get("evidence_id") for card in reordered if card.get("evidence_id")]
    updated["grounding_contract"] = grounding
    return updated, {
        "status": "ok",
        "ordered_evidence_ids": ordered_ids,
        "reason": str(parsed.get("reason") or "")[:180],
        "token_usage": _response_usage(response),
    }


def _graph_for(settings, db_path, stack=None):
    from langchain_core.messages import ToolMessage

    ChatOpenAI, StateGraph, START, END = _load_langgraph()
    temperature = settings.get("temperature")
    llm = ChatOpenAI(
        model=settings["model"], api_key=resolve_api_key(settings),
        base_url=_normalize_base_url(settings["api_base_url"]),
        temperature=float(temperature if temperature not in (None, "") else 0.2),
        timeout=45, max_retries=0, max_tokens=2048,
    )

    def prepare_node(state):
        module = classify_module_heuristic(state.get("user_goal", ""), state.get("module", ""))
        plan = normalize_query_plan(None, state.get("user_goal", ""), state["task_type"], state.get("subject_ids") or [], module)
        return {"module": module, "context_plan": {"module": module, "rag_query_plan": plan},
                "react_messages": [], "pending_calls": [], "model_calls": 0,
                "tool_calls_count": 0, "tool_cache": {}, "deadline": time.monotonic() + MAX_RUN_SECONDS}

    def model_node(state):
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0 or state["model_calls"] >= MAX_MODEL_CALLS:
            return {"pending_calls": [], "final_text": "本轮分析达到运行预算，请缩小问题范围后重试。", "stop_reason": "budget"}
        if state.get("module_context"):
            messages = build_module_messages(state.get("user_goal", ""), state.get("user_context", {}),
                                            state["module_context"], state.get("rag_context", {}), _response_style(state))
        else:
            messages = build_agent_messages(state["task_type"], state.get("user_goal", ""),
                                           state.get("user_context", {}), state.get("candidate_questions", []),
                                           state.get("review_context", {}), state.get("rag_context", {}), _response_style(state))
        messages = with_conversation_history(messages, state.get("conversation_messages") or [],
                                             state.get("conversation_summary") or "", state.get("user_goal") or "")
        messages = with_long_term_memories(messages, state.get("long_term_memories") or [])
        final_round = state["model_calls"] == MAX_MODEL_CALLS - 1 or state["tool_calls_count"] >= MAX_TOOL_CALLS
        messages.insert(1, ("system", REACT_INSTRUCTION + ("\n已到最后一轮，请依据现有资料回复并明确证据缺口，停止调用工具。" if final_round else "")))
        messages.extend(state["react_messages"])
        try:
            response = llm.bind_tools(tool_specs(state), tool_choice="none" if final_round else "auto").invoke(
                messages, timeout=min(45, remaining))
        except Exception as exc:
            # Keep provider response bodies and credentials out of persisted user-facing errors.
            raise AgentRunError("教练模型调用失败，请检查连接及模型的工具调用支持后重试。") from exc
        if getattr(response, "invalid_tool_calls", None):
            raise AgentRunError("模型返回了无法解析的工具参数，请重试或更换支持工具调用的模型。")
        calls = list(getattr(response, "tool_calls", None) or [])
        if len(calls) > MAX_TOOL_CALLS:
            raise AgentRunError("模型单轮请求的工具数量超过上限，请缩小问题范围。")
        if any(not call.get("id") for call in calls) or len({c["id"] for c in calls}) != len(calls):
            raise AgentRunError("模型返回了无效的工具调用标识。")
        _record_step(state["db_path"], state["run_id"], "llm", "react_decide",
                     {"round": state["model_calls"] + 1, "final_round": final_round},
                     {"tool_names": [c.get("name") for c in calls], "token_usage": _response_usage(response),
                      "prompt_version": AGENT_PROMPT_VERSION})
        update = {"react_messages": [*state["react_messages"], response], "model_calls": state["model_calls"] + 1,
                  "pending_calls": calls}
        if calls and not final_round:
            return update
        if calls:
            text = "本轮工具调用已达到预算，请缩小问题范围后重试。"
            update["stop_reason"] = "budget"
        else:
            content = response.content
            text = content if isinstance(content, str) else "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict))
            if not text.strip():
                raise AgentRunError("模型未返回有效回复，请重试。")
            update["stop_reason"] = "completed"
        structured = _structured_output(text)
        if wants_concise_response(state.get("user_goal", ""), _response_style(state)):
            text = _compact_concise_response(text, structured)
            structured = _structured_output(text)
        update.update(pending_calls=[], analysis=text, final_text=text, structured_output=structured)
        return update

    def tools_node(state):
        working = dict(state)
        transcript = list(state["react_messages"])
        cache = dict(state["tool_cache"])
        count = state["tool_calls_count"]
        updates = {}
        for call in state["pending_calls"]:
            name, args = call.get("name", ""), call.get("args")
            status = "ok"
            if count >= MAX_TOOL_CALLS or time.monotonic() >= state["deadline"]:
                result = json.dumps({"ok": False, "error": "本轮工具预算耗尽，请使用现有结果。"}, ensure_ascii=False)
                status = "budget"
            else:
                count += 1
                key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                if name == "search_evidence":
                    dependencies = [working.get(field) for field in ("user_context", "review_context", "candidate_questions")]
                    key += hashlib.sha256(json.dumps(dependencies, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
                try:
                    # Cache updates as well as observations so revisiting a query restores its evidence contract.
                    if key in cache:
                        change, result = cache[key]
                        status = "cached"
                    else:
                        change = execute_tool(working, name, args)
                        result = observation(change)
                        cache[key] = (change, result)
                    working.update(change)
                    updates.update(change)
                except ValueError as exc:
                    result = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
                    status = "invalid_arguments"
                except Exception:
                    result = json.dumps({"ok": False, "error": "工具读取失败，可调整查询或说明资料暂不可用。"}, ensure_ascii=False)
                    status = "error"
            transcript.append(ToolMessage(content=result, tool_call_id=call["id"], name=name))
            _record_step(state["db_path"], state["run_id"], "tool", name,
                         {"call_id": call["id"], "arguments": args}, {"status": status, "observation": result})
        updates.update(react_messages=transcript, tool_calls_count=count, tool_cache=cache, pending_calls=[])
        return updates

    def persist_node(state):
        summary = input_summary(state["task_type"], state.get("user_context", {}),
                                state.get("candidate_questions", []), state.get("review_context", {}))
        with connect(state["db_path"]) as conn:
            complete_run(conn, state["run_id"], state.get("final_text", ""), summary)
        _record_step(state["db_path"], state["run_id"], "agent", "react_complete", {},
                     {"model_calls": state["model_calls"], "tool_calls": state["tool_calls_count"],
                      "stop_reason": state.get("stop_reason")})
        return {}

    builder = StateGraph(AgentState)
    builder.add_node("prepare", prepare_node)
    builder.add_node("agent", model_node)
    builder.add_node("tools", tools_node)
    builder.add_node("persist_result", persist_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges("agent", lambda state: "tools" if state.get("pending_calls") else "persist_result")
    builder.add_edge("tools", "agent")
    builder.add_edge("persist_result", END)
    return builder.compile()


def create_agent_run(db_path, task_type, subject_id=None, subject_ids=None, user_goal=""):
    task_type = task_type if task_type in {"diagnosis", "review", "recommend"} else "diagnosis"
    subject_ids = subject_ids or ([subject_id] if subject_id is not None else [])
    stored_subject_id = subject_id if subject_id is not None else (subject_ids[0] if subject_ids else None)
    for attempt in range(5):
        try:
            with connect(db_path) as conn:
                settings = _settings(conn)
                run_id = create_run(
                    conn,
                    task_type,
                    _subject_type(task_type),
                    stored_subject_id,
                    user_goal,
                    settings["provider_name"] if settings else "",
                    settings["model"] if settings else "",
                )
            return run_id
        except Exception as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                time.sleep(0.15 * (attempt + 1))
            else:
                raise


def run_agent(
    db_path,
    task_type,
    subject_id=None,
    subject_ids=None,
    user_goal="",
    filters=None,
    auto_approve=True,
    module="",
    run_id=None,
    conversation_id=None,
    conversation_messages=None,
    conversation_summary="",
    long_term_memories=None,
):
    task_type = task_type if task_type in {"diagnosis", "review", "recommend"} else "diagnosis"
    filters = filters or {}
    subject_ids = subject_ids or ([subject_id] if subject_id is not None else [])
    stored_subject_id = subject_id if subject_id is not None else (subject_ids[0] if subject_ids else None)
    if run_id is None:
        run_id = create_agent_run(db_path, task_type, stored_subject_id, subject_ids, user_goal)
    with connect(db_path) as conn:
        settings = _settings(conn)
        api_ready = bool(settings and settings["mode"] == "api" and resolve_api_key(settings))
    if not api_ready:
        message = "AI 教练还没有连接可用模型。请先到模型设置完成连接。"
        with connect(db_path) as conn:
            fail_run(conn, run_id, message)
        raise AgentRunError(message)

    try:
        with ExitStack() as stack:
            graph = _graph_for(settings, db_path, stack)
            graph.invoke(
                {
                    "db_path": str(db_path),
                    "run_id": run_id,
                    "task_type": task_type,
                    "subject_id": stored_subject_id,
                    "subject_ids": subject_ids,
                    "user_goal": user_goal,
                    "filters": filters,
                    "auto_approve": auto_approve,
                    "module": module,
                    "conversation_id": conversation_id,
                    "conversation_messages": list(conversation_messages or []),
                    "conversation_summary": conversation_summary or "",
                    "long_term_memories": list(long_term_memories or []),
                },
                {
                    "configurable": {
                        "thread_id": (
                            f"agent-conversation-{conversation_id}"
                            if conversation_id is not None
                            else f"agent-run-{run_id}"
                        )
                    }
                },
            )
    except AgentDependencyError as exc:
        with connect(db_path) as conn:
            fail_run(conn, run_id, str(exc))
        raise AgentRunError(str(exc)) from exc
    except Exception as exc:
        with connect(db_path) as conn:
            fail_run(conn, run_id, f"Agent 运行失败：{exc}")
        raise AgentRunError(str(exc)) from exc
    return run_id

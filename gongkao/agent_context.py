"""Stable prefixes, single-copy tool evidence and bounded request assembly."""

import hashlib
import json

from langchain_core.messages import AIMessage, ToolMessage, convert_to_messages

MAX_CONTEXT_UNITS = 32768
OUTPUT_RESERVE = 2048
PROTOCOL_RESERVE = 1024
MAX_INPUT_UNITS = MAX_CONTEXT_UNITS - OUTPUT_RESERVE - PROTOCOL_RESERVE


class ContextBudgetError(ValueError):
    pass


def wire_data(message):
    value = {"role": message.type, "content": message.content}
    if isinstance(message, AIMessage) and message.tool_calls:
        value["tool_calls"] = message.tool_calls
    if isinstance(message, ToolMessage):
        value.update(tool_call_id=message.tool_call_id, name=message.name)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def input_units(messages, tools):
    # Conservative UTF-8 byte estimate, including schemas and message framing.
    # Actual tokenizer/accounting belongs to the provider; report estimates explicitly.
    return len(encoded(tools)) + sum(len(encoded(wire_data(m))) + 32 for m in messages)


def result_id(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def receipt(identifier):
    return json.dumps({"result_ref": identifier}, ensure_ascii=False)


def _shrink(value, text_limit):
    if isinstance(value, str):
        return value[:text_limit]
    if isinstance(value, list):
        return [_shrink(item, text_limit) for item in value[: max(1, text_limit // 100)]]
    if isinstance(value, dict):
        return {k: _shrink(v, text_limit) for k, v in value.items()}
    return value


def _groups(transcript):
    groups = []
    for message in transcript:
        if isinstance(message, AIMessage):
            groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            raise ContextBudgetError("工具消息缺少对应模型调用")
    for group in groups:
        requested = {call["id"] for call in group[0].tool_calls}
        received = [m.tool_call_id for m in group[1:] if isinstance(m, ToolMessage)]
        if requested != set(received) or len(received) != len(set(received)):
            raise ContextBudgetError("工具调用与结果没有完整配对")
    return groups


def _materialize(groups, store, text_limit=None):
    seen = set()
    messages = []
    for group in groups:
        for message in group:
            if not isinstance(message, ToolMessage):
                messages.append(message)
                continue
            reference = json.loads(message.content).get("result_ref")
            if not reference:
                messages.append(message)
                continue
            if reference not in store:
                raise ContextBudgetError("工具结果引用已失效")
            if reference in seen:
                content = json.dumps({"ok": True, "result_ref": reference, "reused": True})
            else:
                payload = json.loads(store[reference])
                if text_limit is not None:
                    payload = _shrink(payload, text_limit)
                    payload["truncated"] = True
                payload["result_id"] = reference
                content = json.dumps(payload, ensure_ascii=False)
                seen.add(reference)
            messages.append(message.model_copy(update={"content": content}))
    return messages


def pack_messages(prefix, transcript, tools, store, final_round=False, limit=MAX_INPUT_UNITS):
    prefix = convert_to_messages(prefix)
    groups = _groups(transcript)
    tail = (
        convert_to_messages([("human", "本轮达到预算上限，请停止调用工具，依据现有资料回复并说明证据缺口。")])
        if final_round
        else []
    )
    dropped = 0
    shrink_limit = None
    while True:
        notice = (
            convert_to_messages([("human", "部分较早工具交互已移出上下文；仅依据当前可见资料回答，缺少的依据需说明。")])
            if dropped
            else []
        )
        messages = [*prefix, *notice, *_materialize(groups, store, shrink_limit), *tail]
        units = input_units(messages, tools)
        if units <= limit:
            fingerprints = [hashlib.sha256(encoded(wire_data(m))).hexdigest() for m in messages]
            return messages, {
                "input_units_estimate": units,
                "input_units_limit": limit,
                "estimate_method": "utf8_bytes_with_framing",
                "dropped_tool_rounds": dropped,
                "tool_text_limit": shrink_limit,
                "message_fingerprints": fingerprints,
            }
        if len(groups) > 1:
            groups.pop(0)
            dropped += 1
        elif groups and (shrink_limit is None or shrink_limit > 80):
            shrink_limit = 800 if shrink_limit is None else shrink_limit // 2
        else:
            raise ContextBudgetError("任务与必需上下文超出输入预算，请缩短问题或选定更少作答。")


def stable_prefix(messages, tools, history_limit=10000):
    """Budget history separately and keep room for a subsequent tool result."""
    messages = convert_to_messages(messages)
    removed = 0
    if len(messages) < 2:
        return messages, removed
    mandatory_units = input_units([messages[0], messages[-1]], tools)
    if mandatory_units > MAX_INPUT_UNITS:
        raise ContextBudgetError("当前问题超出输入预算，请缩短输入。")
    # A larger task must not silently turn the optional-history allowance into zero.
    budget = min(max(0, history_limit), max(0, MAX_INPUT_UNITS - mandatory_units - 4000))
    while len(messages) > 2 and input_units(messages, tools) - mandatory_units > budget:
        # Keep preference/summary system messages while dropping the oldest full
        # conversational turn. Never leave an orphaned assistant answer behind.
        index = next((i for i in range(1, len(messages) - 1) if messages[i].type != "system"), 1)
        message = messages.pop(index)
        removed += 1
        if message.type == "human" and index < len(messages) - 1 and messages[index].type == "ai":
            messages.pop(index)
            removed += 1
    return messages, removed

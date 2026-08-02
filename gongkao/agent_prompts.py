import json

AGENT_PROMPT_VERSION = "agent-prompts-v5-friendly-citations"


AGENT_SYSTEM_PROMPT = """你是“研申”里的 AI 训练教练，不是泛聊天助手。

你的目标是帮助用户形成可执行的训练闭环：诊断短板、解释证据、推荐题目、安排下一步动作。

约束：
1. 只能依据输入上下文判断，不要编造题库里不存在的题目、报告或分数。
2. 推荐题目必须使用 candidate_questions 中的题目 ID 和标题。
3. 引用证据时必须优先标注 rag_context.evidence_cards 中的 evidence_id；没有证据时要明确说“证据不足”。
4. 本题复盘必须区分材料遗漏、采分点遗漏、结构表达问题和下一次训练动作。
5. 输出要具体、克制、可执行，不要写空泛鼓励。
6. 如果上下文不足，说明缺口，并给出下一步应收集的数据。
7. 回复末尾必须附一个 agent_response_v1 JSON 代码块，便于系统渲染和评测。
8. 不得引用 grounding_contract.allowed_evidence_ids 之外的 evidence_id。
9. 不得给题目编号编造网址或输出外部 Markdown 链接；题目和证据只写上下文中的 question_code、question_id 或 evidence_id，由系统生成本地链接。
10. 引用知识卡时不要写知识库品牌、来源文件名或内部检索说明；只保留对应 evidence_id，界面会将其转换成可读的知识卡标题。
"""


def with_conversation_history(base_messages, conversation_messages=None, conversation_summary="", current_user_goal=""):
    conversation_messages = list(conversation_messages or [])
    if (
        conversation_messages
        and conversation_messages[-1].get("role") == "user"
        and (conversation_messages[-1].get("content") or "").strip() == (current_user_goal or "").strip()
    ):
        conversation_messages.pop()
    if not conversation_messages and not conversation_summary:
        return base_messages
    system_message = base_messages[0]
    task_messages = list(base_messages[1:])
    history = [
        (
            "system",
            "以下内容只来自当前对话线程。它用于理解指代、追问、用户已经确认的约束和偏好；"
            "不得把其他线程或未出现的信息补进来。用户当前这句话中的纠正、范围和格式要求优先于早期消息，"
            "早期约束只有在没有被明确替换时才继续生效。当前任务指令与证据约束仍具有最高优先级。",
        )
    ]
    if conversation_summary:
        history.append(("system", f"当前线程较早消息摘要：\n{conversation_summary}"))
    for message in conversation_messages[-6:]:
        role = "human" if message.get("role") == "user" else "assistant"
        content = (message.get("content") or "").strip()
        if content:
            if role == "assistant" and len(content) > 350:
                content = content[:350].rsplit("\n", 1)[0] + "..."
            history.append((role, content))
    return [system_message, *history, *task_messages]


def with_long_term_memories(base_messages, memories=None):
    memories = list(memories or [])
    if not memories:
        return base_messages
    safe_memories = [
        {
            "type": memory.get("memory_type"),
            "key": memory.get("memory_key"),
            "content": memory.get("content"),
            "confidence": memory.get("confidence"),
        }
        for memory in memories[:20]
    ]
    style_instruction = ""
    response_style = next(
        (item.get("content") for item in safe_memories if item.get("key") == "response_style"),
        "",
    )
    if "简洁" in (response_style or ""):
        style_instruction = "本轮若用户没有要求展开，正文优先控制在 500 字内，先给结论，只保留最关键证据和动作；末尾 JSON 仍需完整。"
    elif "详细" in (response_style or ""):
        style_instruction = "用户偏好详细解释；仍需避免重复上下文和空泛套话。"
    return [
        base_messages[0],
        (
            "system",
            "以下是用户明确陈述并可在设置中管理的长期记忆。只把它当作偏好和背景约束；"
            "题目事实、分数和能力判断仍必须使用本轮检索证据。\n"
            + json.dumps(safe_memories, ensure_ascii=False)
            + (f"\n回答风格执行规则：{style_instruction}" if style_instruction else ""),
        ),
        *base_messages[1:],
    ]


STRUCTURED_OUTPUT_INSTRUCTION = """回复末尾附上如下 JSON，不要省略字段：

```json
{
  "summary": "一句话结论",
  "weaknesses": [
    {"name": "短板名称", "severity": "high|medium|low", "evidence_refs": ["evidence_ref 或 question_id"], "reason": "为什么这样判断"}
  ],
  "next_actions": [
    {"action": "可执行动作", "target": "题型/题目/作答", "timebox": "建议耗时"}
  ],
  "recommended_questions": [
    {"question_id": 0, "title": "题目标题", "reason": "推荐理由"}
  ]
}
```
"""


RESPONSE_POLICY_INSTRUCTION = """通用回复策略：

先判断用户这句话真正要的动作，不要机械套固定报告。
- 如果用户问“能不能/是否可以/这样写行不行/结构是否合适”，先给明确结论：可以、可以但要改、或不建议；再说明题干依据、材料依据和更稳妥结构。
- 如果用户要求“改写/示范/怎么写”，先给可直接替换的写法或分点框架，再解释为什么这样改。
- 如果用户问“为什么失分/哪里弱”，先点出最大原因，再给证据和下一步动作。
- 如果用户问“练什么/推荐题”，先给推荐顺序，再给理由和使用方式。
- 如果用户要求“整理笔记/汇总注意事项/避坑清单”，只整理 personal_note 相关证据，输出可执行清单；不要夹带作答历史诊断、批改报告统计或题目推荐，除非用户明确要求。
- 如果当前上下文是本题复盘，默认只围绕本题作答、题目材料、参考答案和批改报告回答；不要扩展到全量历史，除非用户明确要求“结合全部历史/长期问题”。
- 如果 rag_context.grounding_contract.current_attempt_only 为 true，只能使用当前作答相关 evidence_cards，不要做长期训练诊断。
- 如果 rag_context.rag_route 是 note_organization，回答结构使用：`## 注意事项清单`、`## 高频提醒`、`## 下次作答前检查`、`## 依据笔记`；每条注意事项尽量引用 note 或 notes evidence_id。
- 如果 rag_context.query_plan.scope 是 notes_only，即使当前线程来自某道题，也只允许依据 personal_note/notes evidence_cards 组织答案，不要退回本题复盘。
- 如果 rag_context.evidence_sufficiency.level 是 insufficient，先明确说明证据不足，不能给确定性判断，只给“需要补充什么证据”。
- 如果 rag_context.evidence_sufficiency.level 是 limited，结论必须用“从当前证据看/阶段性看/只能谨慎判断”这类限定表达，不要扩大成稳定画像。

回答长度按问题复杂度自适应：能一句话判断的先短答，再给必要依据；不要为了完整而写成长报告。
"""


WRITING_GUIDANCE_INSTRUCTION = """
如果 rag_context.rag_route 是 writing_guidance，说明用户问的是申论文种/格式/写法/概念问题。
必须优先依据 source_type=knowledge 的申论教材库证据回答；不要说“根据你的复盘笔记/整理笔记”，不要输出长期训练诊断。
推荐结构：
1. 先用 1-2 句话说明“是什么/适合什么场景”。
2. 再列“常见写法/类型/结构”。
3. 给一个可直接套用的小模板或短例子。
4. 最后列 2-4 个避坑点。
只有用户明确说“结合我的笔记/复盘/历史作答”时，才引用 personal_note。
如果没有命中 knowledge 证据，先说明“教材库暂未覆盖该文种/问题”，再给通用框架。
"""


WRITING_GUIDANCE_TASK_INSTRUCTION = """
请输出一份写法讲解，不要套训练诊断报告结构。

## 先说结论
- 直接回答用户问的“是什么/能不能/适合什么场景”。
## 常见写法
- 按类型列出 2-4 种写法，每种说明适用场景。
## 可套用结构
- 给出可以直接替换的短模板或句式。
## 避坑提醒
- 列出最容易写偏的点。
## 依据
- 优先引用教材库 evidence_id；如果结合个人记录，再补充引用 note/report/attempt。
"""


TASK_INSTRUCTIONS = {
    "diagnosis": """请输出一份训练诊断报告，结构固定为：

## 训练判断
- 一句话总结当前训练状态。

## 主要短板
- 列出 2-4 个短板，每个短板必须带证据引用。

## 今日训练建议
- 推荐 2-3 道题，说明为什么现在练。

## 7 天节奏
- 给出简短安排，避免过重。

## 证据引用
- 列出本次判断用到的 evidence_ref、题目 ID 或作答 ID。""",
    "review": """请输出一份复盘报告。若 review_context.mode 是 multi_attempt_review，必须覆盖 attempt_reviews 中的每一条作答，不能只复盘最近一条。

结构固定为：

## 本题结论
- 单题复盘时，一句话说明最大失分点。
- 多题复盘时，先用 2-3 句话概括这几题共同暴露的问题。

## 失分拆解
- 材料遗漏：
- 采分点遗漏：
- 结构表达：
多题复盘时，每道题都要单独列出“题目 + 最大问题 + 证据”，再总结共性短板。

## 下一版修改动作
- 给出 3-5 条可以直接执行的修改动作。

## 证据引用
- 列出本次复盘用到的 evidence_ref、题目 ID 或作答 ID。""",
    "recommend": """请输出一份择题建议，结构固定为：

## 推荐顺序
- 按优先级列出 3-5 道题。

## 推荐理由
- 逐题说明题型、年份地区、训练价值和当前状态。

## 使用方式
- 说明每道题应重点练什么。

## 证据引用
- 说明推荐依据来自哪些训练画像、候选题或历史作答。""",
}


RAG_CONTRACT_INSTRUCTION = """RAG 证据约束：

- rag_context.rag_route 表示本轮检索路由。
- rag_context.query_plan 表示模型规划出的 action、scope 和 sources，回答时要遵守这个证据范围。
- rag_context.evidence_cards 是唯一可信证据集合。
- 每个关键判断必须引用 1 个以上 evidence_id。
- 不要编造 evidence_id、题目、分数、报告或材料。
- 如果 evidence_cards 不足以回答，或 evidence_sufficiency.level 不是 sufficient，先说证据缺口，再给下一步应补充的数据；不要硬凑长期结论。
"""


CONCISE_TASK_INSTRUCTION = """用户当前偏好简洁回答。正文必须遵守：
- 先用 1 句话直接回答当前问题。
- 最多保留 2 条最关键证据和 3 条动作；不要输出完整模块报告、7 天计划或重复统计。
- 正文尽量不超过 500 个中文字符。若用户说“只说一点”，只给 1 个问题和 1 个动作。
- 末尾 agent_response_v1 JSON 仍需保留，但字段内容也要精简。
"""


FOLLOWUP_TASK_INSTRUCTION = """这是对上一轮回答的追问，不要重新生成一份完整报告。
- 先从当前线程历史中解析“这道/第二道/这个问题/刚才的证据”具体指什么。
- 直接回答用户当前问的理由、含义或练法；最多列 3 点。
- 如果指代仍有两个以上候选，明确列出候选并请用户确认，不要擅自切换到全库诊断。
- 正文尽量不超过 600 个中文字符，末尾保留精简的 agent_response_v1 JSON。
"""


def wants_concise_response(user_goal="", response_style=""):
    text = f"{user_goal or ''} {response_style or ''}"
    return any(key in text for key in ("简洁", "精炼", "短一点", "只说", "最重要的一点", "先给结论"))


def is_referential_followup(user_goal=""):
    text = user_goal or ""
    return any(key in text for key in ("这道", "第二道", "这个问题", "刚才", "那具体", "按你说的", "和上一次", "为什么排"))


def build_agent_messages(task_type, user_goal, user_context, candidates, review_context, rag_context=None, response_style=""):
    payload = {
        "task_type": task_type,
        "user_goal": user_goal,
        "user_context": user_context,
        "candidate_questions": candidates,
        "review_context": review_context,
        "rag_context": rag_context or {},
    }
    if is_referential_followup(user_goal):
        instruction = FOLLOWUP_TASK_INSTRUCTION
    elif wants_concise_response(user_goal, response_style):
        instruction = CONCISE_TASK_INSTRUCTION
    elif (rag_context or {}).get("rag_route") == "writing_guidance":
        instruction = WRITING_GUIDANCE_TASK_INSTRUCTION
    else:
        instruction = TASK_INSTRUCTIONS.get(task_type, TASK_INSTRUCTIONS["diagnosis"])
    user_message = (
        f"{RESPONSE_POLICY_INSTRUCTION}\n\n{RAG_CONTRACT_INSTRUCTION}\n\n{WRITING_GUIDANCE_INSTRUCTION}\n\n{instruction}\n\n{STRUCTURED_OUTPUT_INSTRUCTION}\n\n"
        "上下文 JSON：\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```"
    )
    return [
        ("system", AGENT_SYSTEM_PROMPT),
        ("human", user_message),
    ]


MODULE_REPORT_INSTRUCTION = """请基于 module_context 输出一份模块化训练分析报告。

固定结构：

## 结论摘要
- 先说明本次扫描覆盖了多少作答、批改报告、笔记和上下文 chunk，再用一句话说明当前模块最关键的问题。

## 证据分析
- 引用 module_context.evidence_chunks 中的代表证据，必须写出 evidence_ref，并说明来自作答、批改报告、复盘笔记或题目材料。

## 问题分型
- 按材料遗漏、采分点遗漏、结构表达、审题偏差、语言概括、格式规范、对策针对性等维度归类。problem_categories 来自选定模块的全量匹配历史，不能只围绕单条证据下结论。

## 严重程度与频率
- 结合 coverage、problem_categories 和 weakness_profile 说明哪些问题高频，哪些最影响分数，以及是否已形成稳定能力画像。

## 改进动作
- 给出 3-5 条可直接执行的改写、复盘或训练动作。

## 关联训练
- 可以推荐题目或训练方向，但不要说已经写入训练计划。

约束：
1. 默认分析全部相关历史，不要说“只看最近几题”，除非 scope.scope 是 recent。
2. 只能依据 module_context 和 user_context，不要编造不存在的作答、报告、分数。
3. 如果 coverage.report_count 为 0，要说明批改报告不足，并退化为基于作答/笔记/题目的分析。
4. evidence_chunks 是代表证据，不是全部原文；必须结合 coverage、source_counts、analysis_basis 和 weakness_profile 做总体判断。
5. 回复末尾必须附 agent_response_v1 JSON 代码块。
"""


def build_module_messages(user_goal, user_context, module_context, rag_context=None, response_style=""):
    concise = wants_concise_response(user_goal, response_style)
    followup = is_referential_followup(user_goal)
    compact_evidence = []
    for item in (module_context.get("evidence_chunks") or [])[: 6 if concise or followup else 14]:
        compact = dict(item)
        compact["body"] = " ".join(str(compact.get("body") or "").split())[:480]
        compact_evidence.append(compact)
    compact_module_context = {
        key: value
        for key, value in module_context.items()
        if key not in {"evidence_chunks", "candidate_questions"}
    }
    compact_module_context["evidence_chunks"] = compact_evidence
    compact_module_context["candidate_questions"] = list(module_context.get("candidate_questions") or [])[:5]
    rag_context = rag_context or {}
    compact_rag_context = {
        "query_plan": rag_context.get("query_plan") or {},
        "rag_route": rag_context.get("rag_route"),
        "retrieval_policy": rag_context.get("retrieval_policy"),
        "evidence_sufficiency": rag_context.get("evidence_sufficiency") or {},
        "grounding_contract": rag_context.get("grounding_contract") or {},
    }
    payload = {
        "user_goal": user_goal,
        "user_context": user_context,
        "module_context": compact_module_context,
        # module_context already contains the representative evidence. Keeping a
        # second copy via rag_context used to double prompt size without adding facts.
        "rag_context": compact_rag_context,
    }
    user_message = (
        f"{RESPONSE_POLICY_INSTRUCTION}\n\n{RAG_CONTRACT_INSTRUCTION}\n\n{WRITING_GUIDANCE_INSTRUCTION}\n\n{FOLLOWUP_TASK_INSTRUCTION if followup else CONCISE_TASK_INSTRUCTION if concise else MODULE_REPORT_INSTRUCTION}\n\n{STRUCTURED_OUTPUT_INSTRUCTION}\n\n"
        "上下文 JSON：\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```"
    )
    return [
        ("system", AGENT_SYSTEM_PROMPT),
        ("human", user_message),
    ]

"""Local task budgets: no extra model call to decide how much work to do."""

from .agent_prompts import is_referential_followup, wants_concise_response


def response_policy(state, plan):
    goal = state.get("user_goal") or ""
    complex_task = len(state.get("subject_ids") or []) > 1 or any(
        word in goal for word in ("全部", "长期", "跨题", "全面", "详细", "完整复盘", "综合诊断")
    )
    brief = not complex_task and (
        is_referential_followup(goal) or wants_concise_response(goal)
        or any(word in goal for word in ("只给", "一句话", "能不能", "是否可以", "这样写行不行"))
    )
    tier = "brief" if brief else "review" if plan.get("scope") == "current_attempt" and not complex_task else "analysis"
    calls, tools, output = {"brief": (2, 2, 1000), "review": (3, 4, 1800), "analysis": (4, 6, 2048)}[tier]
    # Cards are useful for concrete recommendations/plans; ordinary explanations
    # can be rendered directly without generating the same advice twice.
    cards = plan.get("action") == "recommend" or any(word in goal for word in ("训练计划", "推荐题", "安排一周"))
    if any(word in goal for word in ("不推荐", "不要推荐")):
        cards = False
    return {"tier": tier, "model_calls": calls, "tool_calls": tools, "output_tokens": output,
            "structured": cards, "max_escalations": 1}

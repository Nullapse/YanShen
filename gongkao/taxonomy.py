import re

QUESTION_TYPES = ("归纳概括", "综合分析", "提出对策", "公文写作", "综合写作")

COMPREHENSIVE_WRITING_CUES = (
    "写一篇文章",
    "写一篇关于",
    "写一篇议论文",
    "写一篇议论",
    "议论文",
    "议论性文章",
    "讨论性文章",
    "策论文",
    "对策性文章",
    "作文",
    "自拟题目",
    "自选角度",
)

OFFICIAL_DOCUMENT_GENRES = (
    "宣传稿",
    "宣传材料",
    "讲话稿",
    "发言稿",
    "发言提纲",
    "讲话提纲",
    "演讲稿",
    "致辞稿",
    "倡议书",
    "公开信",
    "感谢信",
    "建议书",
    "推荐信",
    "一封信",
    "简报",
    "短评",
    "编者按",
    "按语",
    "新闻稿",
    "宣传报道稿",
    "报道稿",
    "广播稿",
    "发布词",
    "简讯",
    "请示",
    "汇报稿",
    "报告提纲",
    "推介稿",
    "政务信息",
    "工作信息",
    "招募书",
    "告知书",
    "征集启事",
    "宣传单",
    "直播代言稿",
    "宣讲稿",
    "消息稿",
    "通讯稿",
    "通知",
    "通告",
    "公告",
    "回信",
    "复信",
    "回复函",
    "答复",
    "舆情摘要",
    "解说词",
    "导游词",
    "主持词",
    "开幕词",
    "闭幕词",
    "结束语",
    "推荐材料",
    "推介材料",
    "介绍材料",
    "申报材料",
    "参评材料",
    "经验交流材料",
    "总结材料",
    "文字材料",
    "推广材料",
    "调研报告",
    "调查报告",
    "情况报告",
    "情况汇报",
    "汇报材料",
    "汇报提纲",
    "案例摘要",
    "情况介绍",
    "招聘启事",
    "策划书",
    "工作指南",
    "温馨提示",
    "工作事项",
    "文稿",
    "推文",
    "微评论",
    "约稿",
    "推荐理由",
    "经验交流发言",
    "提案",
    "案例材料",
    "问题清单",
    "若干措施",
    "工作方案",
    "活动方案",
    "实施方案",
    "工作计划",
    "工作要点",
)

OFFICIAL_DOCUMENT_ACTIONS = (
    "拟写",
    "撰写",
    "起草",
    "草拟",
    "拟定",
    "拟出",
    "写一份",
    "写一则",
    "写一封",
    "写一篇",
)

OFFICIAL_DOCUMENT_GENERIC_GENRES = (
    "提纲",
    "方案",
    "计划",
    "要点",
    "回复",
    "指南",
    "建议",
    "汇报",
    "流程",
    "内容要点",
    "新闻报道",
    "评论",
    "点评",
)

COUNTERMEASURE_CUES = (
    "提出建议",
    "提出对策",
    "提出解决",
    "提出措施",
    "提出改进",
    "提出工作建议",
    "提出下一步",
    "并提出",
    "提出相应",
    "对策建议",
    "解决对策",
    "解决办法",
    "解决措施",
    "改进措施",
    "工作措施",
    "应对措施",
    "完善措施",
    "解决思路",
    "如何改进",
    "如何避免",
    "如何才能",
    "如何增强",
    "如何破解",
    "要如何解决",
    "应如何解决",
    "应如何",
    "如何进一步",
    "需要重点做好",
    "应重点把握",
    "你的思路",
    "工作思路",
    "主要思路",
    "参考建议",
    "主要任务及措施",
    "怎么办",
)

STRONG_ANALYSIS_CUES = (
    "分析",
    "理解",
    "认识",
    "启示",
    "启发",
    "评价",
    "评析",
    "看法",
    "谈谈理解",
    "谈谈认识",
    "谈谈看法",
    "谈一谈对",
    "为什么",
    "为何",
    "是什么让",
    "阐述",
    "论述",
    "剖析",
    "反驳",
    "内涵",
    "含义",
    "可行性条件",
)

WEAK_ANALYSIS_CUES = (
    "原因",
    "意义",
    "影响",
    "作用",
    "关系",
    "学到什么",
    "借鉴",
)

SUMMARY_CUES = (
    "概括",
    "归纳",
    "总结",
    "梳理",
    "简述",
    "简要说明",
    "主要做法",
    "主要问题",
    "主要特点",
    "主要经验",
    "主要成效",
    "基本情况",
    "经验做法",
    "哪些问题",
    "存在的问题",
    "面临的问题",
    "遇到的问题",
    "遇到哪些",
    "如何做好",
    "如何推进",
    "如何解决",
    "怎样解决",
    "怎么解决",
    "提炼",
    "概述",
    "分类整理",
    "是如何",
    "有哪些做法",
    "采取了哪些",
    "其中的变化",
    "共同点",
    "侧重点",
    "主要举措",
    "有利条件",
    "特点",
    "体现",
    "变化",
    "做法",
    "举措",
    "经验",
    "怎样",
    "哪些方面",
    "哪些工作",
    "哪些努力",
    "哪些亮点",
    "哪些办法",
    "采取了哪些措施",
    "措施办法",
    "在哪些地方",
    "简要介绍",
    "如何让",
    "工作创新",
    "喜”和“盼",
)


def _contains_any(text, cues):
    return next((cue for cue in cues if cue in text), "")


def classify_question_type(prompt, requirements="", fallback="综合分析"):
    text = re.sub(r"\s+", "", prompt or "")

    genre = _contains_any(text, OFFICIAL_DOCUMENT_GENRES)
    if genre:
        return "公文写作", f"明确文种：{genre}"

    writing_cue = _contains_any(text, COMPREHENSIVE_WRITING_CUES)
    if writing_cue:
        return "综合写作", f"文章写作：{writing_cue}"

    action = _contains_any(text, OFFICIAL_DOCUMENT_ACTIONS)
    generic_genre = _contains_any(text, OFFICIAL_DOCUMENT_GENERIC_GENRES)
    if action and generic_genre:
        return "公文写作", f"成文任务：{action}{generic_genre}"

    if "把你要说的话写下来" in text or "简短的发言" in text:
        return "公文写作", "成文任务：现场发言"

    titled_document_match = re.search(
        r"(?:拟写|撰写|起草|草拟|拟定|拟出).{0,16}《[^》]+》", text
    )
    if titled_document_match:
        return "公文写作", f"成文任务：{titled_document_match.group(0)}"

    report_match = re.search(r"写一篇.{0,24}报道", text)
    if report_match:
        return "公文写作", f"成文任务：{report_match.group(0)}"

    countermeasure_match = re.search(
        r"提出.{0,24}(?:建议|对策|措施|办法|意见)|"
        r"(?:针对|围绕).{0,24}(?:问题|困难).{0,16}(?:建议|对策|措施|办法)",
        text,
    )
    if countermeasure_match:
        return "提出对策", f"对策任务：{countermeasure_match.group(0)}"

    countermeasure_cue = _contains_any(text, COUNTERMEASURE_CUES)
    if countermeasure_cue:
        return "提出对策", f"对策任务：{countermeasure_cue}"

    analysis_cue = _contains_any(text, STRONG_ANALYSIS_CUES)
    if analysis_cue:
        return "综合分析", f"分析任务：{analysis_cue}"

    summary_cue = _contains_any(text, SUMMARY_CUES)
    if summary_cue:
        return "归纳概括", f"概括任务：{summary_cue}"

    analysis_cue = _contains_any(text, WEAK_ANALYSIS_CUES)
    if analysis_cue:
        return "综合分析", f"分析任务：{analysis_cue}"

    return fallback if fallback in QUESTION_TYPES else "综合分析", "未命中明确规则"


def infer_question_type(prompt, requirements=""):
    return classify_question_type(prompt, requirements)[0]

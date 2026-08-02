MODULES = {
    "overview": {
        "label": "总览",
        "question_type": "",
        "prompt": "分析我的整体训练状态、主要缺点和下一步怎么练。",
        "focus": "跨题型训练状态、长期弱项和优先改进方向",
    },
    "summary": {
        "label": "归纳概括",
        "question_type": "归纳概括",
        "prompt": "分析我归纳概括题的缺点、最大失分点和怎么改进。",
        "focus": "材料提取、要点合并、概括准确性和表达简洁度",
    },
    "analysis": {
        "label": "综合分析",
        "question_type": "综合分析",
        "prompt": "分析我综合分析题的缺点、最大失分点和怎么改进。",
        "focus": "观点提炼、关系拆解、解释深度和逻辑层次",
    },
    "countermeasure": {
        "label": "提出对策",
        "question_type": "提出对策",
        "prompt": "分析我提出对策题的缺点、最大失分点和怎么改进。",
        "focus": "问题归因、对策针对性、可操作性和材料转化",
    },
    "document": {
        "label": "公文写作",
        "question_type": "公文写作",
        "prompt": "分析我公文写作题的缺点、最大失分点和怎么改进。",
        "focus": "格式规范、对象意识、任务完成度和语言风格",
    },
    "essay": {
        "label": "综合写作",
        "question_type": "综合写作",
        "prompt": "分析我综合写作题的缺点、最大失分点和怎么改进。",
        "focus": "立意、论证结构、材料转化和语言表达",
    },
    "top_loss": {
        "label": "最大失分点",
        "question_type": "",
        "prompt": "分析我所有题目里最主要、最反复、最影响分数的失分点。",
        "focus": "跨题型高频失分点、严重失分原因和代表证据",
    },
    "improvement": {
        "label": "怎么改进",
        "question_type": "",
        "prompt": "基于我的全部训练记录，给出最该执行的改进动作。",
        "focus": "可执行改写动作、复盘动作、训练节奏和下一题选择",
    },
}

def module_definition(module_id):
    return MODULES.get(module_id) or MODULES["overview"]


def valid_module_id(value):
    return value if value in MODULES else "overview"
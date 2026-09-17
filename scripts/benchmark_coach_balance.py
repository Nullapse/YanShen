"""Opt-in live API comparison using synthetic data, never personal conversations.

Example: python scripts/benchmark_coach_balance.py --source-root . --output .test-tmp/coach-balance/new
Settings are read in memory from the local application database. No credentials
are copied to the test database or report. Each case makes at most six model calls.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=("followup", "review", "diagnosis", "recommend"))
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from gongkao.agent_graph import run_agent
    from gongkao.ai_config import load_effective_agent_settings
    from gongkao.db import SCHEMA
    from gongkao.paths import user_db_path

    with sqlite3.connect(user_db_path().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        settings = load_effective_agent_settings(conn)
    args.output.mkdir(parents=True, exist_ok=True)
    db = args.output / "synthetic.sqlite3"
    if db.exists():
        raise SystemExit("Choose a fresh output directory to preserve previous measurements")
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)
        conn.execute("""INSERT INTO papers(id,paper_code,paper_name,exam_type,year,region)
                        VALUES(1,'SYNTHETIC','合成测试卷','省考',2025,'浙江')""")
        conn.execute("""INSERT INTO questions(id,question_code,paper_id,exam_type,year,region,
                        question_type,title,prompt,materials,requirements)
                        VALUES(1,'SYNTHETIC-1',1,'省考',2025,'浙江','归纳概括','养老服务',
                        '根据给定资料概括社区养老服务的主要措施。','','全面准确，不超过200字。')""")
        material = (
            "社区设立助餐点，解决老人吃饭难；提供送餐上门，覆盖行动不便老人。"
            "建立需求台账，每月回访，动态调整助餐服务；引入第三方评估，检查食品安全和服务满意度。"
        )
        for number in range(1, 4):
            conn.execute("INSERT INTO paper_materials(paper_id,material_number,content) VALUES(1,?,?)",
                         (number, material if number == 1 else "社区养老服务背景介绍。" * 100))
        conn.execute("""INSERT INTO attempts(id,question_id,answer_text,personal_note)
                        VALUES(1,1,'建设助餐点，提供上门送餐，方便老人生活。','下次检查服务闭环。')""")
        conn.execute("""INSERT INTO grading_reports(id,attempt_id,report_text) VALUES(1,1,?)""",
                     ("总分：12/20。已覆盖助餐点、上门送餐。主要遗漏：需求台账与每月回访、"
                      "动态调整服务、第三方食品安全与满意度评估。应概括为需求反馈和服务监督闭环。",))
        conn.execute("""INSERT INTO reference_answers(question_id,organization,answer_text)
                        VALUES(1,'合成参考','完善助餐服务；建立需求台账和回访机制，动态调整服务；引入第三方监督评估。')""")
    cases = [
        {"id": "followup", "task_type": "review", "subject_ids": [1],
         "user_goal": "那具体怎么练？只给今天的安排。",
         "conversation_messages": [
             {"role": "user", "content": "我每天只有15分钟，只练需求反馈闭环，不推荐新题。"},
             {"role": "assistant", "content": "今天先在原作答上补上需求台账、每月回访和动态调整。"}],
         "checks": {"time_constraint": ["15", "十五"], "specific_action": ["台账", "回访", "动态调整"]}},
        {"id": "review", "task_type": "review", "subject_ids": [1],
         "user_goal": "根据批改报告解释本题最主要的失分原因，并给一版200字以内的改写。",
         "checks": {"feedback_gap": ["回访", "台账"], "supervision_gap": ["第三方", "监督", "评估"],
                    "evidence": ["grading_report:1", "material:1"]}},
        {"id": "diagnosis", "task_type": "diagnosis", "subject_ids": [],
         "user_goal": "结合全部训练历史，指出有证据支持的主要问题，给一个下一步动作。不要推荐新题。",
         "checks": {"limited_evidence": ["仅", "一", "有限", "不足"], "specific_gap": ["闭环", "回访", "台账"]}},
        {"id": "recommend", "task_type": "recommend", "subject_ids": [],
         "user_goal": "推荐一道适合补练要点遗漏的题，允许做过的题，只安排10分钟。",
         "checks": {"known_question": ["养老服务"], "recommendation_card": ["recommended_questions"],
                    "time_constraint": ["10", "十分钟"]}},
    ]
    selected = set(args.case or ["followup", "review", "diagnosis"])
    results = []
    for case in cases:
        if case["id"] not in selected:
            continue
        request = {k: v for k, v in case.items() if k not in {"id", "checks"}}
        started = time.monotonic()
        error = None
        try:
            with patch("gongkao.agent_graph._settings", return_value=settings):
                run_agent(db, **request)
        except Exception as exc:
            error = type(exc).__name__  # Provider errors may contain secrets; never serialize them.
        duration = round((time.monotonic() - started) * 1000)
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT id,final_text,status FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
            steps = conn.execute("SELECT tool_name,output_json FROM agent_steps WHERE run_id=? ORDER BY id",
                                 (row[0],)).fetchall() if row else []
        decisions = [json.loads(raw) for name, raw in steps if name == "react_decide"]
        answer = row[1] if row else ""
        checks = {key: any(term in answer for term in terms) for key, terms in case["checks"].items()}
        if case["id"] == "followup":
            checks["time_constraint"] &= not any(term in answer for term in ("45 分钟", "45分钟", "40 分钟", "40分钟"))
        item = {"case": case["id"], "duration_ms": duration, "model_calls": len(decisions),
                "error": error, "status": row[2] if row else "failed", "answer": answer,
                "usage": {key: sum((d.get("token_usage") or {}).get(key, 0) for d in decisions)
                          for key in ("input_tokens", "output_tokens", "cached_input_tokens")},
                "first_round_first_text_ms": decisions[0].get("first_text_ms") if decisions else None,
                "checks": checks,
                "decisions": decisions}
        results.append(item)
        (args.output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in item.items() if k not in {"answer", "decisions"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

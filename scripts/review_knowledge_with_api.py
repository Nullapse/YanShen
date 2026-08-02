import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.ai import chat_completion
from gongkao.db import connect
from gongkao.knowledge_cards import KnowledgeCard
from gongkao.paths import user_db_path


def parse_json(text):
    blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", text or "", flags=re.S | re.I)
    for candidate in [*reversed(blocks), text or ""]:
        try:
            value = json.loads(candidate.strip())
        except json.JSONDecodeError:
            start, end = candidate.find("{"), candidate.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                value = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    return {}


def load_cards(path):
    return [
        KnowledgeCard.model_validate_json(line).model_dump()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def stratified_sample(cards):
    groups = defaultdict(list)
    for card in cards:
        groups[(card["module"], card["kind"])].append(card)
    return [sorted(values, key=lambda item: item["id"])[0] for _, values in sorted(groups.items())]


def main():
    parser = argparse.ArgumentParser(description="使用当前 API 对知识卡做分层交叉评审。")
    parser.add_argument("--db", default=str(user_db_path()))
    parser.add_argument("--knowledge", default="knowledge/knowledge_cards_v2.jsonl")
    parser.add_argument("--output", default="evals/agent_v2/results/knowledge-api-review-v2.json")
    parser.add_argument("--batch-size", type=int, default=6)
    args = parser.parse_args()
    cards = stratified_sample(load_cards(args.knowledge))
    with connect(args.db) as conn:
        settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
    results = []
    for start in range(0, len(cards), max(1, args.batch_size)):
        batch = cards[start : start + max(1, args.batch_size)]
        review_payload = [
            {
                "id": card["id"],
                "module": card["module"],
                "kind": card["kind"],
                "skill": card["skill"],
                "content": card["content"],
                "example": card["examples"][0],
                "counterexample": card["counterexamples"][0],
                "applicable_when": card["applicable_when"],
                "not_applicable_when": card["not_applicable_when"],
            }
            for card in batch
        ]
        prompt = (
            "你是严格的申论领域知识审校员。逐卡检查：规则是否正确、正反例是否匹配、适用边界是否清楚、"
            "是否会诱导脱离材料或机械套模板。只返回 JSON，不要改写全文。\n"
            f"知识卡：{json.dumps(review_payload, ensure_ascii=False)}\n"
            '格式：{"reviews":[{"id":"...","pass":true,"severity":"none|minor|major","reason":"不超过80字"}]}'
        )
        content, _ = chat_completion(settings, prompt)
        parsed = parse_json(content)
        by_id = {item.get("id"): item for item in parsed.get("reviews") or [] if isinstance(item, dict)}
        for card in batch:
            review = by_id.get(card["id"]) or {
                "id": card["id"],
                "pass": False,
                "severity": "major",
                "reason": "模型返回缺少该卡评审结果",
            }
            results.append(review)
    report = {
        "schema_version": "knowledge-api-review-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(cards),
        "pass_count": sum(item.get("pass") is True for item in results),
        "pass_rate": round(sum(item.get("pass") is True for item in results) / max(1, len(results)), 4),
        "major_issues": sum(item.get("severity") == "major" for item in results),
        "minor_issues": sum(item.get("severity") == "minor" for item in results),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

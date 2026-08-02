import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.knowledge_cards import KnowledgeCard


def shingles(text):
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    return {normalized[index : index + 3] for index in range(max(0, len(normalized) - 2))}


def jaccard(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def load_public_cards(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cards = []
    for name in manifest.get("public_files") or []:
        path = root / name
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                try:
                    cards.append(KnowledgeCard.model_validate_json(raw).model_dump())
                except Exception as exc:
                    raise RuntimeError(f"{path}:{line_number}: {exc}") from exc
    return manifest, cards


def audit(root, near_duplicate_threshold=0.93):
    manifest, cards = load_public_cards(root)
    errors = []
    ids = [card["id"] for card in cards]
    duplicate_ids = [item for item, count in Counter(ids).items() if count > 1]
    if duplicate_ids:
        errors.append(f"duplicate ids: {duplicate_ids[:5]}")
    minimum = int(manifest.get("minimum_public_cards") or 300)
    if len(cards) < minimum:
        errors.append(f"public card count {len(cards)} < {minimum}")
    required_modules = set(manifest.get("required_modules") or [])
    missing_modules = required_modules - {card["module"] for card in cards}
    if missing_modules:
        errors.append(f"missing modules: {sorted(missing_modules)}")
    unreviewed = [card["id"] for card in cards if card["review"]["status"] != "reviewed"]
    if unreviewed:
        errors.append(f"unreviewed public cards: {unreviewed[:5]}")
    incomplete_source = [
        card["id"]
        for card in cards
        if card["source"]["visibility"] != "public"
        or not all(card["source"].get(key) for key in ("name", "section", "license"))
    ]
    if incomplete_source:
        errors.append(f"invalid public sources: {incomplete_source[:5]}")

    duplicate_pairs = []
    card_shingles = [(card["id"], shingles(card["content"])) for card in cards]
    for index, (left_id, left) in enumerate(card_shingles):
        for right_id, right in card_shingles[index + 1 :]:
            similarity = jaccard(left, right)
            if similarity >= near_duplicate_threshold:
                duplicate_pairs.append((left_id, right_id, round(similarity, 4)))
    duplicate_ratio = len({item for pair in duplicate_pairs for item in pair[:2]}) / max(1, len(cards))
    if duplicate_ratio >= 0.03:
        errors.append(f"near duplicate ratio {duplicate_ratio:.4f} >= 0.03; first={duplicate_pairs[:3]}")

    summary = {
        "schema_version": manifest.get("schema_version"),
        "public_cards": len(cards),
        "module_counts": dict(sorted(Counter(card["module"] for card in cards).items())),
        "kind_counts": dict(sorted(Counter(card["kind"] for card in cards).items())),
        "reviewed_rate": round(sum(card["review"]["status"] == "reviewed" for card in cards) / max(1, len(cards)), 4),
        "source_complete_rate": round((len(cards) - len(incomplete_source)) / max(1, len(cards)), 4),
        "near_duplicate_pairs": len(duplicate_pairs),
        "near_duplicate_ratio": round(duplicate_ratio, 4),
        "errors": errors,
    }
    if errors:
        raise RuntimeError(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description="审计 schema v2 领域知识卡。")
    parser.add_argument("--root", default="knowledge")
    parser.add_argument("--output")
    args = parser.parse_args()
    summary = audit(Path(args.root))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

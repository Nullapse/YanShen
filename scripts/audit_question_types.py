import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gongkao.taxonomy import QUESTION_TYPES, classify_question_type


def audit(database, output=None, verbose=True):
    database = Path(database)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, question_code, year, region, paper_name, question_number,
                   question_type, prompt, requirements
              FROM questions
             ORDER BY year DESC, region, paper_name, question_number, id
            """
        ).fetchall()
    finally:
        conn.close()

    changes = []
    fallback_rows = []
    inferred_counts = Counter()
    for row in rows:
        inferred, reason = classify_question_type(
            row["prompt"], row["requirements"], fallback=row["question_type"]
        )
        inferred_counts[inferred] += 1
        item = {
            "id": row["id"],
            "question_code": row["question_code"],
            "year": row["year"],
            "region": row["region"],
            "paper_name": row["paper_name"],
            "question_number": row["question_number"],
            "old_type": row["question_type"],
            "new_type": inferred,
            "reason": reason,
            "prompt": row["prompt"].replace("\n", " "),
        }
        if inferred != row["question_type"]:
            changes.append(item)
        if reason == "未命中明确规则":
            fallback_rows.append(item)

    print(f"database: {database}")
    print(f"questions: {len(rows)}")
    print("inferred:", ", ".join(f"{name}={inferred_counts[name]}" for name in QUESTION_TYPES))
    print(f"changes: {len(changes)}")
    print(f"fallback review: {len(fallback_rows)}")
    if verbose:
        for item in changes:
            print(
                f"{item['question_code']}\t{item['old_type']} -> {item['new_type']}\t"
                f"{item['reason']}\t{item['prompt'][:160]}"
            )

    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=changes[0].keys() if changes else item.keys())
            writer.writeheader()
            writer.writerows(changes)
        print(f"written: {output}")

    return changes, fallback_rows


def main():
    parser = argparse.ArgumentParser(description="Audit question taxonomy against shared rules.")
    parser.add_argument("--database", default="data/gongkao_seed.sqlite3")
    parser.add_argument("--output")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    audit(args.database, args.output, verbose=not args.summary_only)


if __name__ == "__main__":
    main()

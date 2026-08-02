import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gongkao.taxonomy import QUESTION_TYPES, classify_question_type


def reclassify(database, make_backup=True):
    database = Path(database).resolve()
    if not database.exists():
        raise FileNotFoundError(database)

    if make_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = database.with_name(f"{database.stem}.before-taxonomy-{stamp}{database.suffix}")
        shutil.copy2(database, backup)
        print(f"backup: {backup}")

    conn = sqlite3.connect(database)
    try:
        rows = conn.execute(
            "SELECT id, question_code, prompt, requirements, question_type FROM questions"
        ).fetchall()
        changes = []
        for row_id, question_code, prompt, requirements, old_type in rows:
            new_type, reason = classify_question_type(
                prompt, requirements, fallback=old_type
            )
            if new_type != old_type:
                changes.append((new_type, row_id, question_code, old_type, reason))

        conn.executemany(
            "UPDATE questions SET question_type = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [(new_type, row_id) for new_type, row_id, *_ in changes],
        )
        conn.commit()
        counts = Counter(
            row[0] for row in conn.execute("SELECT question_type FROM questions").fetchall()
        )
    finally:
        conn.close()

    print(f"database: {database}")
    print(f"updated: {len(changes)}")
    print("counts:", ", ".join(f"{name}={counts[name]}" for name in QUESTION_TYPES))
    return changes


def main():
    parser = argparse.ArgumentParser(description="Reclassify all questions using shared taxonomy rules.")
    parser.add_argument("database")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    reclassify(args.database, make_backup=not args.no_backup)


if __name__ == "__main__":
    main()

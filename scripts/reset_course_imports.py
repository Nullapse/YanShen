import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "instance" / "gongkao.sqlite3"


COURSE_PROVIDERS = ("白鹭破题阵", "袁东超大杯")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in COURSE_PROVIDERS)
    question_ids = [
        row["question_id"]
        for row in conn.execute(
            f"SELECT DISTINCT question_id FROM question_sources WHERE provider IN ({placeholders})",
            COURSE_PROVIDERS,
        )
    ]
    if question_ids:
        id_placeholders = ",".join("?" for _ in question_ids)
        conn.execute(f"DELETE FROM questions WHERE id IN ({id_placeholders})", question_ids)
    conn.execute("DELETE FROM question_sources WHERE provider IN (" + placeholders + ")", COURSE_PROVIDERS)
    conn.execute("DELETE FROM question_sources WHERE question_id NOT IN (SELECT id FROM questions)")
    conn.execute("DELETE FROM paper_materials WHERE paper_id NOT IN (SELECT id FROM papers)")
    conn.execute("DELETE FROM papers WHERE id NOT IN (SELECT DISTINCT paper_id FROM questions WHERE paper_id IS NOT NULL)")
    conn.commit()
    conn.close()
    print(f"removed_course_questions={len(question_ids)}")


if __name__ == "__main__":
    main()

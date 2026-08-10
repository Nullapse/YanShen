import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.db import connect
from gongkao.grading import normalize_revised_answer_word_count
from gongkao.grading_pipeline.common import question_word_limit_text
from gongkao.grading_pipeline.report import render_grading_report
from gongkao.grading_pipeline.rubric import normalize_essay_coverage_roles


def main():
    parser = argparse.ArgumentParser(description="Refresh a v5 report from its persisted structured result.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report-id", type=int, required=True)
    args = parser.parse_args()

    with connect(args.db) as conn:
        row = conn.execute(
            """
            SELECT c.*, r.attempt_id
              FROM grading_report_contexts c
              JOIN grading_reports r ON r.id = c.report_id
             WHERE c.report_id = ?
            """,
            (args.report_id,),
        ).fetchone()
        if row is None:
            raise SystemExit("report context not found")
        question = dict(
            conn.execute(
                "SELECT q.* FROM questions q JOIN attempts a ON a.question_id = q.id WHERE a.id = ?",
                (row["attempt_id"],),
            ).fetchone()
        )
        rubric = json.loads(row["rubric_snapshot_json"] or "{}")
        result = json.loads(row["result_json"] or "{}")
        normalize_essay_coverage_roles(rubric)
        word_limit = question_word_limit_text(question)
        rubric["word_limit"] = word_limit
        result["word_limit"] = word_limit
        point_roles = {
            point.get("point_key"): point.get("coverage_role")
            for point in rubric.get("points", [])
        }
        for match in result.get("point_matches", []):
            if match.get("point_key") in point_roles:
                match["coverage_role"] = point_roles[match["point_key"]]
        report_text = render_grading_report(result, rubric, [])
        report_text = normalize_revised_answer_word_count(report_text, word_limit)
        conn.execute(
            "UPDATE grading_reports SET report_text = ? WHERE id = ?",
            (report_text, args.report_id),
        )
        conn.execute(
            "UPDATE grading_report_contexts SET rubric_snapshot_json = ?, result_json = ? WHERE report_id = ?",
            (
                json.dumps(rubric, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                args.report_id,
            ),
        )
        if row["rubric_id"]:
            conn.execute(
                "UPDATE grading_rubrics SET rubric_json = ? WHERE id = ?",
                (json.dumps(rubric, ensure_ascii=False), row["rubric_id"]),
            )
    print(
        json.dumps(
            {
                "report_id": args.report_id,
                "score": result.get("score"),
                "display_score": result.get("display_score"),
                "word_limit": word_limit,
                "score_status": result.get("score_status"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

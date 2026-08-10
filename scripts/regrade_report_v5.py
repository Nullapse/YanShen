import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.ai import chat_completion
from gongkao.db import connect
from gongkao.grading_pipeline.orchestration import create_grading_job, run_grading_job


def main():
    parser = argparse.ArgumentParser(description="Regrade an existing report with the current smart-grading pipeline.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report-id", type=int, required=True)
    args = parser.parse_args()

    with connect(args.db) as conn:
        report = conn.execute("SELECT * FROM grading_reports WHERE id = ?", (args.report_id,)).fetchone()
        if report is None:
            raise SystemExit("report not found")
        attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (report["attempt_id"],)).fetchone()
        settings = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
        old_job = conn.execute(
            "SELECT * FROM grading_jobs WHERE report_id = ? ORDER BY id DESC LIMIT 1",
            (args.report_id,),
        ).fetchone()
        options = json.loads(old_job["options_json"] or "{}") if old_job else {}
        reference_ids = [int(value) for value in options.get("reference_ids") or []]
        custom_reference = str(options.get("custom_reference_answer") or "")
        job, _ = create_grading_job(
            conn,
            attempt,
            settings,
            reference_ids,
            custom_reference,
            {
                "analogies": bool(options.get("analogies", True)),
                "knowledge": bool(options.get("knowledge", True)),
                "history": bool(options.get("history", True)),
                "deep_thinking": bool(options.get("deep_thinking", False)),
            },
        )
    print(json.dumps({"job_id": job["id"], "source_report_id": args.report_id}, ensure_ascii=False), flush=True)
    new_report_id = run_grading_job(args.db, job["id"], chat_completion)
    if new_report_id is None:
        with connect(args.db) as conn:
            failed = conn.execute("SELECT status, error_text FROM grading_jobs WHERE id = ?", (job["id"],)).fetchone()
        print(json.dumps({"job_id": job["id"], "status": failed["status"], "error": failed["error_text"]}, ensure_ascii=False))
        raise SystemExit(1)
    with connect(args.db) as conn:
        context = conn.execute(
            "SELECT pipeline_version, result_json, validation_json, api_call_count FROM grading_report_contexts WHERE report_id = ?",
            (new_report_id,),
        ).fetchone()
        result = json.loads(context["result_json"] or "{}")
    print(
        json.dumps(
            {
                "report_id": new_report_id,
                "pipeline_version": context["pipeline_version"],
                "score": result.get("score"),
                "display_score": result.get("display_score"),
                "display_max_score": result.get("display_max_score"),
                "score_status": result.get("score_status"),
                "calibration": result.get("score_calibration"),
                "review": result.get("review"),
                "api_call_count": context["api_call_count"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

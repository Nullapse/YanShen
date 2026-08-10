import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.ai import chat_completion
from gongkao.db import connect
from gongkao.grading import select_relevant_materials
from gongkao.grading_pipeline.calibration import fit_high_score_calibration
from gongkao.grading_pipeline.evidence import build_grading_prompt
from gongkao.grading_pipeline.orchestration import (
    _build_review_prompt,
    _call_grading_model,
    _merge_review_evaluation,
    _repair_json_prompt,
    _repair_smart_response_prompt,
    _smart_response_parts,
)
from gongkao.grading_pipeline.rubric import (
    build_rubric_prompt,
    compact_reference_consensus,
    extract_tagged_json,
    validate_rubric,
)
from gongkao.grading_pipeline.validation import validate_grading_result

DISABLED_POLICY = fit_high_score_calibration([])


def _call(settings, prompt):
    return _call_grading_model(
        chat_completion,
        settings,
        prompt,
        deep_thinking=False,
        structured=True,
    )[0]


def _load_question_context(db_path, question_id, excluded_reference_ids):
    with connect(db_path) as conn:
        question = dict(conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone())
        materials = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM paper_materials WHERE paper_id = ? ORDER BY material_number",
                (question["paper_id"],),
            )
        ]
        materials = select_relevant_materials(question, materials)
        references = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM reference_answers WHERE question_id = ? ORDER BY id",
                (question_id,),
            )
            if row["id"] not in excluded_reference_ids
        ]
        target = conn.execute(
            "SELECT * FROM reference_answers WHERE question_id = ? AND id IN ({}) ORDER BY id LIMIT 1".format(
                ",".join("?" for _ in excluded_reference_ids)
            ),
            (question_id, *sorted(excluded_reference_ids)),
        ).fetchone()
        settings = dict(conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone())
        consensus = compact_reference_consensus(conn, references, materials)
    if target is None:
        raise ValueError(f"question {question_id}: anchor answer missing")
    return question, materials, references, dict(target), settings, consensus


def evaluate_question(db_path, anchor, question_id):
    excluded = set(anchor["reference_ids"])
    question, materials, references, target, settings, consensus = _load_question_context(
        db_path, question_id, excluded
    )
    rubric_prompt = build_rubric_prompt(question, materials, references, consensus)
    rubric_response = _call(settings, rubric_prompt)
    try:
        raw_rubric = extract_tagged_json(rubric_response, "rubric_json")
        rubric = validate_rubric(raw_rubric, question, materials, references, [])
    except Exception:
        repaired = _call(settings, _repair_json_prompt(rubric_prompt, rubric_response, "rubric_json"))
        raw_rubric = extract_tagged_json(repaired, "rubric_json")
        rubric = validate_rubric(raw_rubric, question, materials, references, [])

    attempt = {
        "id": 0,
        "question_id": question_id,
        "answer_text": target["answer_text"],
        "word_count": len(str(target["answer_text"] or "").strip()),
    }
    grading_prompt = build_grading_prompt(
        question,
        materials,
        attempt,
        rubric,
        [],
        "",
        {"history_attempt_count": 0, "history_stable": False},
        [],
        references,
    )
    grading_response = _call(settings, grading_prompt)

    def parse(response, reviewed=False, original=None):
        _, evaluation = _smart_response_parts(response, expects_rubric=False)
        if original is not None:
            evaluation = _merge_review_evaluation(original, evaluation)
        result = validate_grading_result(
            evaluation,
            rubric,
            target["answer_text"],
            [],
            calibration_policy=DISABLED_POLICY,
            reviewed=reviewed,
        )
        return evaluation, result

    try:
        evaluation, result = parse(grading_response)
    except Exception as error:
        repaired = _call(settings, _repair_smart_response_prompt(grading_prompt, grading_response, str(error)))
        evaluation, result = parse(repaired)
    if result.get("review", {}).get("triggered"):
        review_response = _call(
            settings,
            _build_review_prompt(question, rubric, target["answer_text"], result),
        )
        evaluation, result = parse(review_response, reviewed=True, original=evaluation)
    return {
        "question_id": question_id,
        "question_max_score": result["display_max_score"],
        "predicted_score": result["display_score"],
        "score_status": result["score_status"],
        "review_triggered": bool(result.get("review", {}).get("triggered")),
        "reference_count_after_exclusion": len(references),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate complete exam anchors without answer leakage.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=ROOT / "knowledge" / "grading_calibration_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "grading_anchor_residuals_v1.json")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed-results", type=Path)
    parser.add_argument("--only-question-ids", nargs="*", type=int)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    anchors = [item for item in manifest.get("anchors", []) if item.get("classification") == "complete_exam"]
    selected_ids = set(args.only_question_ids or [])
    tasks = [
        (anchor, question_id)
        for anchor in anchors
        for question_id in anchor["question_ids"]
        if not selected_ids or question_id in selected_ids
    ]
    question_results = {}
    if args.seed_results and args.seed_results.exists():
        seed = json.loads(args.seed_results.read_text(encoding="utf-8"))
        for item in seed.get("question_results", []):
            question_results[(item["anchor_id"], item["question_id"])] = {
                key: value for key, value in item.items() if key != "anchor_id"
            }
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as pool:
        future_map = {
            pool.submit(evaluate_question, args.db, anchor, question_id): (anchor, question_id)
            for anchor, question_id in tasks
        }
        for future in as_completed(future_map):
            anchor, question_id = future_map[future]
            try:
                result = future.result()
                question_results[(anchor["anchor_id"], question_id)] = result
                print(
                    json.dumps(
                        {"anchor_id": anchor["anchor_id"], **result},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as error:
                failures.append(
                    {"anchor_id": anchor["anchor_id"], "question_id": question_id, "error": str(error)[:500]}
                )
                print(json.dumps(failures[-1], ensure_ascii=False), flush=True)

    residuals = []
    for anchor in anchors:
        items = [
            question_results.get((anchor["anchor_id"], question_id))
            for question_id in anchor["question_ids"]
        ]
        if any(item is None or item["score_status"] != "valid" for item in items):
            continue
        predicted = round(sum(float(item["predicted_score"]) for item in items), 1)
        residuals.append(
            {
                "anchor_id": anchor["anchor_id"],
                "paper_id": anchor["paper_id"],
                "classification": "complete_exam",
                "actual_score": anchor["actual_score"],
                "predicted_score": predicted,
                "residual": round(float(anchor["actual_score"]) - predicted, 1),
                "questions": items,
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    audit_payload = {
        "schema_version": "grading-anchor-eval-v1",
        "question_results": [
            {"anchor_id": anchor_id, **item}
            for (anchor_id, _question_id), item in sorted(question_results.items())
        ],
        "failures": failures,
        "residuals": residuals,
    }
    args.output.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "output": str(args.output),
        "completed_anchors": len(residuals),
        "failures": failures,
        "policy": fit_high_score_calibration(residuals),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

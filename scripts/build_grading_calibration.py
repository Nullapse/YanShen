import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.db import connect
from gongkao.grading_pipeline.calibration import (
    build_exam_anchor_manifest,
    fit_high_score_calibration,
)


def main():
    parser = argparse.ArgumentParser(description="Build metadata-only exam score calibration artifacts.")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "gongkao_seed.sqlite3")
    parser.add_argument("--residuals", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "knowledge" / "grading_calibration_v1.json")
    args = parser.parse_args()

    with connect(args.db) as conn:
        manifest = build_exam_anchor_manifest(conn)
    residuals = []
    if args.residuals:
        residuals = json.loads(args.residuals.read_text(encoding="utf-8"))
        if isinstance(residuals, dict):
            residuals = residuals.get("residuals") or []
    manifest["policy"] = fit_high_score_calibration(residuals)
    manifest["anchors"] = [
        {
            key: value
            for key, value in anchor.items()
            if key not in {"source_labels"}
        }
        for anchor in manifest["anchors"]
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "counts": manifest["counts"], "policy": manifest["policy"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

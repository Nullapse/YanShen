import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.agent_eval import (
    DETERMINISTIC_DATASET_PATH,
    EVAL_DATASET_PATH,
    MULTITURN_DATASET_PATH,
    build_multiturn_report,
    compare_eval_reports,
    evaluate_deterministic_suite,
    load_multiturn_cases,
    run_eval_suite,
    run_multiturn_eval_suite,
)
from gongkao.paths import user_db_path

DEFAULT_RESULTS_DIR = ROOT / "evals" / "agent_v2" / "results"


def _default_output(mode):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_RESULTS_DIR / f"{mode}-{stamp}.json"


def _print_summary(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    print(json.dumps({"report": str(report_path), **report["summary"]}, ensure_ascii=False, indent=2))


def _sqlite_snapshot(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source), timeout=60)
    destination_conn = sqlite3.connect(str(destination), timeout=60)
    try:
        source_conn.backup(destination_conn, pages=4096)
    finally:
        destination_conn.close()
        source_conn.close()


def _run_multiturn_isolated(args, output):
    source_db = Path(args.db).resolve()
    if not source_db.exists():
        raise FileNotFoundError(f"数据库不存在：{source_db}")
    dataset = Path(args.dataset).resolve()
    cases = load_multiturn_cases(dataset)
    if args.case_id:
        wanted_ids = set(args.case_id)
        cases = [case for case in cases if case.get("id") in wanted_ids]
    if args.category:
        wanted = set(args.category)
        cases = [case for case in cases if case.get("category") in wanted]
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    if not cases:
        raise ValueError("没有匹配的多轮评测用例。")

    worker_count = max(1, min(int(args.workers or 1), len(cases), 4))
    temp_root = Path(tempfile.mkdtemp(prefix="gongkao-agent-eval-"))
    try:
        base_snapshot = temp_root / "base.sqlite3"
        _sqlite_snapshot(source_db, base_snapshot)
        assignments = [[] for _ in range(worker_count)]
        for index, case in enumerate(cases):
            assignments[index % worker_count].append(case)
        jobs = []
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="agent-eval") as executor:
            for index, assigned in enumerate(assignments):
                worker_dir = temp_root / f"worker-{index + 1}"
                worker_dir.mkdir()
                worker_db = worker_dir / "gongkao.sqlite3"
                shutil.copy2(base_snapshot, worker_db)
                jobs.append(
                    executor.submit(
                        run_multiturn_eval_suite,
                        worker_db,
                        dataset_path=dataset,
                        cases=assigned,
                        run_judge=not args.no_judge,
                        reset_between_cases=True,
                    )
                )
            merged = []
            for job in as_completed(jobs):
                merged.extend(job.result())
        ordering = {case["id"]: index for index, case in enumerate(cases)}
        merged.sort(key=lambda item: ordering[item["case"]["id"]])
        report = build_multiturn_report(merged, dataset)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if args.keep_workspace:
            print(f"隔离评测目录已保留：{temp_root}")
        else:
            resolved = temp_root.resolve()
            if resolved.name.startswith("gongkao-agent-eval-") and resolved.parent == Path(tempfile.gettempdir()).resolve():
                shutil.rmtree(resolved, ignore_errors=False)


def main():
    parser = argparse.ArgumentParser(description="运行 Agent v2 的版本化评测或比较两份报告。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    live = subparsers.add_parser("live", help="调用当前配置的真实模型运行回归集。")
    live.add_argument("--db", default=str(user_db_path()))
    live.add_argument("--dataset", default=str(EVAL_DATASET_PATH))
    live.add_argument("--tag", default="smoke", help="默认只跑 5 条 smoke 用例。")
    live.add_argument("--all", action="store_true", help="运行数据集全部用例，可能产生较高模型费用。")
    live.add_argument("--limit", type=int)
    live.add_argument("--ragas", action="store_true", help="额外运行模型评审指标。")
    live.add_argument("--output")

    deterministic = subparsers.add_parser("deterministic", help="运行不需要模型或 API Key 的 CI 子集。")
    deterministic.add_argument("--dataset", default=str(DETERMINISTIC_DATASET_PATH))
    deterministic.add_argument("--output")

    multiturn = subparsers.add_parser("multiturn", help="在隔离数据库副本上运行多轮 Agent 回归集。")
    multiturn.add_argument("--db", default=str(user_db_path()), help="只读源数据库；运行期间不会写入它。")
    multiturn.add_argument("--dataset", default=str(MULTITURN_DATASET_PATH))
    multiturn.add_argument("--category", action="append", choices=("reference", "constraint", "correction", "summary", "isolation"))
    multiturn.add_argument("--case-id", action="append", help="只运行指定用例，可重复传入。")
    multiturn.add_argument("--limit", type=int)
    multiturn.add_argument("--workers", type=int, default=2, help="按用例并发；同一用例中的轮次仍串行。")
    multiturn.add_argument("--no-judge", action="store_true", help="跳过额外的语义评审模型调用。")
    multiturn.add_argument("--keep-workspace", action="store_true", help="调试时保留含 API Key 的隔离数据库。")
    multiturn.add_argument("--output")

    compare = subparsers.add_parser("compare", help="比较 baseline 与 candidate 报告。")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--output")

    args = parser.parse_args()
    if args.command == "live":
        output = Path(args.output) if args.output else _default_output("live")
        run_eval_suite(
            args.db,
            dataset_path=args.dataset,
            tags=() if args.all else (args.tag,),
            case_limit=args.limit,
            run_ragas=args.ragas,
            output_path=output,
        )
        _print_summary(output)
        return
    if args.command == "deterministic":
        output = Path(args.output) if args.output else _default_output("deterministic")
        evaluate_deterministic_suite(args.dataset, output_path=output)
        _print_summary(output)
        return
    if args.command == "multiturn":
        output = Path(args.output) if args.output else _default_output("multiturn")
        _run_multiturn_isolated(args, output)
        _print_summary(output)
        return

    comparison = compare_eval_reports(args.baseline, args.candidate)
    rendered = json.dumps(comparison, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(str(output))
    else:
        print(rendered)


if __name__ == "__main__":
    main()

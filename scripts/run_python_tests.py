"""Run the unittest suite in isolated parallel shards."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def iter_test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_test_ids(item)
        else:
            yield item.id()


def discover_test_ids():
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"),
        top_level_dir=str(ROOT),
    )
    test_ids = sorted(iter_test_ids(suite))
    failed_imports = [
        test_id
        for test_id in test_ids
        if test_id.startswith("unittest.loader._FailedTest")
    ]
    if failed_imports:
        raise RuntimeError(
            "Test discovery failed: " + ", ".join(failed_imports)
        )
    return test_ids


def partition_tests(test_ids, worker_count):
    shards = [[] for _ in range(worker_count)]
    for index, test_id in enumerate(test_ids):
        shards[index % worker_count].append(test_id)
    return [shard for shard in shards if shard]


def run_shard(index, test_ids, *, verbose):
    temp_root = ROOT / ".test-tmp" / "python-tests" / f"worker-{index + 1}"
    temp_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
        }
    )
    command = [sys.executable, "-m", "unittest"]
    command.append("-v" if verbose else "-q")
    command.extend(test_ids)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "index": index + 1,
        "count": len(test_ids),
        "seconds": time.perf_counter() - started,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="parallel worker count (default: up to 4)",
    )
    parser.add_argument("--verbose", action="store_true", help="show every test name")
    args = parser.parse_args()

    test_ids = discover_test_ids()
    if not test_ids:
        print("No Python tests discovered.")
        return 1
    worker_count = max(1, min(args.workers, len(test_ids)))
    shards = partition_tests(test_ids, worker_count)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = [
            executor.submit(run_shard, index, shard, verbose=args.verbose)
            for index, shard in enumerate(shards)
        ]
        results = [future.result() for future in futures]

    failed = False
    for result in sorted(results, key=lambda item: item["index"]):
        status = "PASS" if result["returncode"] == 0 else "FAIL"
        print(
            f"[{status}] shard {result['index']}: "
            f"{result['count']} tests in {result['seconds']:.1f}s"
        )
        if result["returncode"] != 0:
            failed = True
            if result["stdout"]:
                print(result["stdout"])
            if result["stderr"]:
                print(result["stderr"], file=sys.stderr)
    elapsed = time.perf_counter() - started
    print(f"Python tests: {len(test_ids)} total, {len(shards)} workers, {elapsed:.1f}s")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

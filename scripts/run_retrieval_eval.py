import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.agent_eval import retrieval_ranking_metrics
from gongkao.agent_modules import (
    configure_dense_embedding,
    ensure_agent_context_index,
    retrieve_knowledge_evidence,
    sync_dense_embeddings,
)
from gongkao.db import connect
from gongkao.paths import user_db_path

DEFAULT_DATASET = ROOT / "evals" / "agent_v2" / "retrieval-gold-v1.jsonl"


def load_cases(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def mean(values):
    values = [float(value) for value in values if value is not None]
    return round(statistics.mean(values), 4) if values else None


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = min(len(values) - 1, max(0, int(len(values) * fraction + 0.9999) - 1))
    return round(values[index], 3)


def main():
    parser = argparse.ArgumentParser(description="运行 Agent v2 Hybrid RAG gold retrieval 评测。")
    parser.add_argument("--db", default=str(user_db_path()))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--backend", choices=("feature", "bge", "current"), default="current")
    parser.add_argument("--output")
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    results = []
    with connect(args.db) as conn:
        state = conn.execute(
            "SELECT embedding_model, embedding_dimensions FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        original = (state["embedding_model"], state["embedding_dimensions"])
        try:
            if args.backend == "feature":
                configure_dense_embedding(conn, enabled=False)
            elif args.backend == "bge":
                configure_dense_embedding(conn, enabled=True, allow_download=False)
            ensure_agent_context_index(conn)
            if args.backend == "bge":
                while True:
                    dense_status = sync_dense_embeddings(conn, batch_size=48, limit=768)
                    if not dense_status.get("available") or dense_status.get("remaining", 0) == 0:
                        break
            active = conn.execute(
                "SELECT embedding_model, embedding_dimensions FROM agent_context_index_state WHERE id = 1"
            ).fetchone()
            for case in cases:
                started = time.perf_counter()
                evidence = retrieve_knowledge_evidence(
                    conn,
                    case["module"],
                    case["query"],
                    limit=10,
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                retrieved = [item["evidence_ref"] for item in evidence]
                metrics = retrieval_ranking_metrics(retrieved, case["gold_evidence_ids"])
                results.append(
                    {
                        "case": case,
                        "retrieved_ids": retrieved,
                        "latency_ms": latency_ms,
                        "metrics": metrics,
                        "vector_backend": (evidence[0].get("retrieval") or {}).get("vector_backend") if evidence else "none",
                    }
                )
        finally:
            conn.execute(
                "UPDATE agent_context_index_state SET embedding_model = ?, embedding_dimensions = ? WHERE id = 1",
                original,
            )
    latencies = [item["latency_ms"] for item in results]
    report = {
        "schema_version": "agent-retrieval-eval-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(Path(args.dataset)),
        "case_count": len(results),
        "requested_backend": args.backend,
        "active_embedding_model": active["embedding_model"],
        "active_dimensions": active["embedding_dimensions"],
        "summary": {
            "recall_at_5": mean(item["metrics"]["recall_at_5"] for item in results),
            "recall_at_10": mean(item["metrics"]["recall_at_10"] for item in results),
            "mrr": mean(item["metrics"]["mrr"] for item in results),
            "ndcg_at_10": mean(item["metrics"]["ndcg_at_10"] for item in results),
            "latency_ms_p50": percentile(latencies, 0.5),
            "latency_ms_p95": percentile(latencies, 0.95),
        },
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

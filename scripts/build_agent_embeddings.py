import argparse
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gongkao.agent_modules import (
    BGE_DIMENSIONS,
    BGE_MODEL,
    configure_dense_embedding,
    ensure_agent_context_index,
    sync_dense_embeddings,
    sync_sqlite_vec_index,
)
from gongkao.db import connect
from gongkao.paths import user_db_path


def main():
    parser = argparse.ArgumentParser(description="构建或关闭本地中文 Agent embedding 索引。")
    parser.add_argument("--db", default=str(user_db_path()))
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--chunk-limit", type=int, default=512)
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    with connect(db_path) as conn:
        if args.disable:
            status = configure_dense_embedding(conn, enabled=False)
            print(status)
            return
        # Build or refresh the cheap fallback index first, then enable BGE so
        # progress can be committed in bounded batches instead of one long transaction.
        configure_dense_embedding(conn, enabled=False)
        ensure_agent_context_index(conn)
        configure_dense_embedding(conn, enabled=True, allow_download=True)
        total = conn.execute("SELECT COUNT(*) FROM agent_context_chunks").fetchone()[0]

    started = time.perf_counter()
    completed = 0
    while True:
        with connect(db_path) as conn:
            status = sync_dense_embeddings(
                conn,
                batch_size=max(1, args.batch_size),
                limit=max(1, args.chunk_limit),
                sync_vector_index=False,
            )
        completed += status.get("updated", 0)
        elapsed = time.perf_counter() - started
        print(
            f"{BGE_MODEL}: {min(total, completed)}/{total}, "
            f"remaining={status.get('remaining')}, elapsed={elapsed:.1f}s",
            flush=True,
        )
        if not status.get("available"):
            raise RuntimeError(f"embedding backend unavailable: {status}")
        if status.get("remaining", 0) == 0:
            break
    with connect(db_path) as conn:
        vec_status = sync_sqlite_vec_index(conn, BGE_MODEL, BGE_DIMENSIONS)
    print({"embedding": status, "sqlite_vec": vec_status, "elapsed_seconds": round(time.perf_counter() - started, 2)})


if __name__ == "__main__":
    main()

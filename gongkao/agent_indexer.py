import logging
import threading

from .agent_retrieval.indexing import (
    coalesce_agent_context_pending,
    finalize_agent_context_index,
    process_agent_context_pending_batch,
    rebuild_agent_context_index_if_needed,
    refresh_agent_knowledge_if_needed,
)


class AgentIndexWorker:
    def __init__(self, db_path, batch_size=8, idle_seconds=1.0, batch_pause_seconds=0.15):
        self.db_path = str(db_path)
        self.batch_size = max(1, int(batch_size))
        self.idle_seconds = max(0.1, float(idle_seconds))
        self.batch_pause_seconds = max(0.05, float(batch_pause_seconds))
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="gongkao-agent-indexer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout=5):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self):
        while not self._stop_event.is_set():
            try:
                coalesce_agent_context_pending(self.db_path)
                if rebuild_agent_context_index_if_needed(self.db_path):
                    self._stop_event.wait(self.batch_pause_seconds)
                    continue
                processed = process_agent_context_pending_batch(
                    self.db_path,
                    batch_size=self.batch_size,
                )
                if processed:
                    self._stop_event.wait(self.batch_pause_seconds)
                    continue
                refreshed = refresh_agent_knowledge_if_needed(self.db_path)
                finalized = finalize_agent_context_index(self.db_path)
                if refreshed or finalized:
                    self._stop_event.wait(self.batch_pause_seconds)
                    continue
            except Exception:
                logging.exception("Background agent index cycle failed")
            self._stop_event.wait(self.idle_seconds)

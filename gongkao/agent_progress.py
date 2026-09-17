"""Ephemeral streamed text for local status polling; no database writes per token."""

import threading
import time

_lock = threading.Lock()
_runs = {}
_TTL_SECONDS = 300


def _key(db_path, run_id):
    from pathlib import Path

    return str(Path(db_path).resolve()), run_id


def update_progress(db_path, run_id, stage, text=""):
    now = time.monotonic()
    with _lock:
        for key in list(_runs):
            if now - _runs[key]["updated"] > _TTL_SECONDS:
                _runs.pop(key, None)
        key = _key(db_path, run_id)
        if key not in _runs and len(_runs) >= 128:
            _runs.pop(min(_runs, key=lambda k: _runs[k]["updated"]))
        _runs[key] = {"stage": stage, "text": text[:16000].split("```", 1)[0], "updated": now}


def read_progress(db_path, run_id):
    with _lock:
        value = _runs.get(_key(db_path, run_id))
        if not value or time.monotonic() - value["updated"] > _TTL_SECONDS:
            return {}
        return {"stage": value["stage"], "text": value["text"]}


def clear_progress(db_path, run_id):
    with _lock:
        _runs.pop(_key(db_path, run_id), None)

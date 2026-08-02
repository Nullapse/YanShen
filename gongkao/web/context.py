import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Hashable

AutosaveRevision = tuple[str, int]


@dataclass
class AutosaveState:
    """Per-server ordering state for asynchronous editor saves."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    revisions: dict[Hashable, AutosaveRevision] = field(default_factory=dict)

    def apply(
        self,
        resource_key: Hashable,
        session_id: str,
        revision: int,
        save_callback: Callable[[], None],
    ) -> bool:
        if not session_id or revision <= 0:
            save_callback()
            return True
        with self.lock:
            previous_session, previous_revision = self.revisions.get(
                resource_key,
                ("", 0),
            )
            if previous_session == session_id and revision < previous_revision:
                return False
            save_callback()
            self.revisions[resource_key] = (session_id, revision)
            return True


@dataclass
class ApplicationContext:
    """Runtime resources owned by one HTTP server instance."""

    db_path: Path
    resource_root: Path
    autosave: AutosaveState = field(default_factory=AutosaveState)

    @classmethod
    def create(cls, db_path: str | Path, resource_root: str | Path):
        return cls(
            db_path=Path(db_path),
            resource_root=Path(resource_root),
        )

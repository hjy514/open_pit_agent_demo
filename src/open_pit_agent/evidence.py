"""Run-scoped evidence logging for reports and regression tests."""

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from uuid import uuid4

from .sqlite_store import SqliteRunStore


class EvidenceRecorder:
    def __init__(self, root: Path, scenario_id: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = "{}-{}-{}".format(scenario_id, timestamp, uuid4().hex[:8])
        self.run_dir = Path(root).expanduser().resolve() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.events_path = self.run_dir / "events.jsonl"
        self.database_path = self._default_database_path(Path(root))
        self.store = self._open_store(scenario_id)

    @staticmethod
    def _default_database_path(root: Path) -> Path:
        """Place runtime data beside the project, not inside artifacts."""
        resolved_root = Path(root).expanduser().resolve()
        project_root = (
            resolved_root.parents[1]
            if len(resolved_root.parents) >= 2
            else resolved_root.parent
        )
        # Some legacy callers pass a relative ``artifacts/runs`` path while
        # their process cwd is ``/``. Never turn that into a system ``/data``
        # directory; use this package's project root in that compatibility
        # case instead.
        if project_root == Path("/"):
            project_root = Path(__file__).resolve().parents[2]
        return project_root / "data" / "database" / "openpit.db"

    def _open_store(self, scenario_id: str):
        try:
            store = SqliteRunStore(self.database_path)
            store.start_run(self.run_id, scenario_id)
            return store
        except Exception as exc:  # pragma: no cover - safety fallback
            warnings.warn(
                "SQLite evidence sidecar unavailable; JSON evidence remains "
                "enabled: {}".format(exc),
                RuntimeWarning,
            )
            return None

    def _store(self, operation) -> None:
        if self.store is None:
            return
        try:
            operation(self.store)
        except Exception as exc:  # pragma: no cover - safety fallback
            warnings.warn(
                "SQLite evidence write failed; JSON evidence remains enabled: "
                "{}".format(exc),
                RuntimeWarning,
            )

    def record(self, event_type: str, payload: Dict[str, Any]) -> None:
        item = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        self._store(
            lambda store: store.record_event(
                self.run_id,
                item["timestamp"],
                event_type,
                payload,
            )
        )

    def record_episode(self, episode: Any, config_path: Path = None) -> None:
        """Index a resolved episode without affecting CARLA execution."""

        self._store(
            lambda store: store.record_episode(episode, config_path=config_path)
        )

    def write_json(self, name: str, payload: Dict[str, Any]) -> Path:
        path = self.run_dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path
            )
        )
        if name == "summary.json":
            self._store(
                lambda store: store.update_from_summary(self.run_id, payload)
            )
        return path

    def write_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        record_count = 0
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
                record_count += 1
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path, record_count
            )
        )
        return path

    def append_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        record_count = 0
        with path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
                record_count += 1
        self._store(
            lambda store: store.record_artifact(
                self.run_id, path.stem, path, record_count
            )
        )
        return path

    def close(self) -> None:
        """Close the optional sidecar store after the run has finished."""
        if self.store is not None:
            self.store.close()

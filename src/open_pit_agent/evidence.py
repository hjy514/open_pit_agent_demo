"""Run-scoped evidence logging for reports and regression tests."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from uuid import uuid4


class EvidenceRecorder:
    def __init__(self, root: Path, scenario_id: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = "{}-{}-{}".format(scenario_id, timestamp, uuid4().hex[:8])
        self.run_dir = Path(root).expanduser().resolve() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.events_path = self.run_dir / "events.jsonl"

    def record(self, event_type: str, payload: Dict[str, Any]) -> None:
        item = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    def write_json(self, name: str, payload: Dict[str, Any]) -> Path:
        path = self.run_dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def write_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
        return path

    def append_jsonl(
        self, name: str, items: Iterable[Dict[str, Any]]
    ) -> Path:
        path = self.run_dir / name
        with path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
        return path

"""Episode-scoped road state; static map resources remain immutable."""
from dataclasses import dataclass
from typing import Dict


@dataclass
class RoadState:
    statuses: Dict[str, str]

    def close(self, road_id: str) -> None:
        self.statuses[str(road_id)] = "CLOSED"

    def reopen(self, road_id: str) -> None:
        self.statuses[str(road_id)] = "OPEN"

    def is_closed(self, road_id: str) -> bool:
        return self.statuses.get(str(road_id), "OPEN") == "CLOSED"

"""CARLA road-centerline extraction for the visualization layer."""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .adapters.carla_adapter import CarlaAdapter
from .config import load_config


class CarlaMapRepository:
    def __init__(
        self,
        config_path: Path,
        waypoint_distance_m: float = 5.0,
    ) -> None:
        self.config_path = Path(config_path)
        self.waypoint_distance_m = float(
            waypoint_distance_m
        )
        self._cached = None

    def load(self, refresh: bool = False) -> Dict[str, Any]:
        if self._cached is not None and not refresh:
            return self._cached
        config = load_config(self.config_path)
        adapter = CarlaAdapter(config)
        adapter.connect()
        try:
            carla_map = adapter.world.get_map()
            waypoints = carla_map.generate_waypoints(
                self.waypoint_distance_m
            )
            self._cached = build_map_payload(
                carla_map.name.split("/")[-1],
                waypoints,
                self.waypoint_distance_m,
            )
            return self._cached
        finally:
            adapter.close()


def build_map_payload(
    map_name: str,
    waypoints: Iterable[object],
    waypoint_distance_m: float,
) -> Dict[str, Any]:
    grouped: Dict[
        Tuple[int, int, int],
        List[Tuple[float, float, float]],
    ] = {}
    all_points = []
    for waypoint in waypoints:
        location = waypoint.transform.location
        x = round(float(location.x), 3)
        y = round(float(location.y), 3)
        key = (
            int(waypoint.road_id),
            int(waypoint.section_id),
            int(waypoint.lane_id),
        )
        grouped.setdefault(key, []).append(
            (float(waypoint.s), x, y)
        )
        all_points.append((x, y))

    polylines = []
    for key in sorted(grouped):
        ordered = sorted(
            grouped[key], key=lambda item: item[0]
        )
        if len(ordered) < 2:
            continue
        polylines.append(
            {
                "road_id": key[0],
                "section_id": key[1],
                "lane_id": key[2],
                "points": [
                    [item[1], item[2]]
                    for item in ordered
                ],
            }
        )

    bounds = None
    if all_points:
        xs = [item[0] for item in all_points]
        ys = [item[1] for item in all_points]
        bounds = {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
        }
    return {
        "map_name": str(map_name),
        "coordinate_system": "carla_local_meters",
        "waypoint_distance_m": waypoint_distance_m,
        "polyline_count": len(polylines),
        "polylines": polylines,
        "bounds": bounds,
    }

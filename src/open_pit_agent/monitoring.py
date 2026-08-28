"""Synthetic fixed-station monitoring data foundation for the demo."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class MonitoringConfigError(ValueError):
    """Raised when the monitoring layout is invalid."""


@dataclass(frozen=True)
class MonitoringArea:
    area_id: str
    display_name: str
    area_type: str
    center_position: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FixedMonitoringStation:
    station_id: str
    display_name: str
    area_id: str
    station_type: str
    position: Dict[str, float]
    metrics: List[str]
    baseline_metrics: Dict[str, float]
    trend_per_sample: Dict[str, float]
    risk_source_zone_id: Optional[str]
    latency_ms: int
    online: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MonitoringLayout:
    schema_version: str
    layout_id: str
    dataset_label: str
    synthetic_data: bool
    sample_ticks: List[int]
    areas: List[MonitoringArea]
    stations: List[FixedMonitoringStation]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MonitoringObservation:
    observation_id: str
    tick: int
    sample_id: str
    collected_at: str
    timestamp_source: str
    source_id: str
    source_display_name: str
    source_type: str
    station_type: str
    area_id: str
    position: Dict[str, float]
    metrics: Dict[str, float]
    quality: Dict[str, Any]
    dataset_label: str
    synthetic: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_monitoring_layout(path: Path) -> MonitoringLayout:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MonitoringConfigError(
            "Unable to load monitoring config {}: {}".format(path, exc)
        ) from exc

    try:
        areas = [
            MonitoringArea(
                area_id=str(item["area_id"]),
                display_name=str(item["display_name"]),
                area_type=str(item["area_type"]),
                center_position=_position(item["center_position"]),
            )
            for item in raw["areas"]
        ]
        stations = [
            FixedMonitoringStation(
                station_id=str(item["station_id"]),
                display_name=str(item["display_name"]),
                area_id=str(item["area_id"]),
                station_type=str(item["station_type"]),
                position=_position(item["position"]),
                metrics=[str(value) for value in item["metrics"]],
                baseline_metrics={
                    str(key): float(value)
                    for key, value in item["baseline_metrics"].items()
                },
                trend_per_sample={
                    str(key): float(value)
                    for key, value in item.get(
                        "trend_per_sample", {}
                    ).items()
                },
                risk_source_zone_id=(
                    str(item["risk_source_zone_id"])
                    if item.get("risk_source_zone_id")
                    else None
                ),
                latency_ms=int(item.get("latency_ms", 0)),
                online=bool(item.get("online", True)),
            )
            for item in raw["stations"]
        ]
        layout = MonitoringLayout(
            schema_version=str(raw["schema_version"]),
            layout_id=str(raw["layout_id"]),
            dataset_label=str(raw["dataset_label"]),
            synthetic_data=bool(raw["synthetic_data"]),
            sample_ticks=sorted(
                {int(value) for value in raw["sample_ticks"]}
            ),
            areas=areas,
            stations=stations,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MonitoringConfigError(
            "Invalid monitoring config {}: {}".format(path, exc)
        ) from exc

    _validate_layout(layout)
    return layout


def build_fixed_observations(
    layout: MonitoringLayout,
    risk_observations: Optional[Sequence[Any]] = None,
) -> List[MonitoringObservation]:
    """Create station-level raw observations without risk decisions."""

    risk_by_tick = {
        int(item.tick): item for item in (risk_observations or [])
    }
    collected_at = datetime.now(timezone.utc).isoformat()
    result = []
    for sample_index, tick in enumerate(layout.sample_ticks):
        risk_observation = risk_by_tick.get(tick)
        sample_id = (
            str(risk_observation.sample_id)
            if risk_observation is not None
            else "monitoring-sample-{:06d}".format(tick)
        )
        for station in layout.stations:
            metrics = _station_metrics(
                station,
                sample_index,
                risk_observation,
            )
            missing = [
                metric
                for metric in station.metrics
                if metric not in metrics
            ]
            online = station.online
            quality_status = (
                "offline"
                if not online
                else ("invalid" if missing else "valid")
            )
            result.append(
                MonitoringObservation(
                    observation_id="{}-{}-{:06d}".format(
                        layout.layout_id,
                        station.station_id,
                        tick,
                    ),
                    tick=tick,
                    sample_id=sample_id,
                    collected_at=collected_at,
                    timestamp_source="simulation_tick",
                    source_id=station.station_id,
                    source_display_name=station.display_name,
                    source_type="fixed_station",
                    station_type=station.station_type,
                    area_id=station.area_id,
                    position=dict(station.position),
                    metrics=metrics if online else {},
                    quality={
                        "status": quality_status,
                        "online": online,
                        "latency_ms": station.latency_ms,
                        "missing_metrics": missing,
                        "completeness": round(
                            (
                                len(metrics)
                                / float(len(station.metrics))
                            )
                            if station.metrics and online
                            else 0.0,
                            4,
                        ),
                    },
                    dataset_label=layout.dataset_label,
                    synthetic=layout.synthetic_data,
                )
            )
    return result


def monitoring_summary(
    layout: MonitoringLayout,
    observations: Sequence[MonitoringObservation],
) -> Dict[str, Any]:
    return {
        "monitoring_layout_id": layout.layout_id,
        "monitoring_dataset_label": layout.dataset_label,
        "monitoring_synthetic_data": layout.synthetic_data,
        "monitoring_area_count": len(layout.areas),
        "fixed_station_count": len(layout.stations),
        "fixed_observation_count": len(observations),
        "mobile_equipment_count": 0,
        "mobile_observation_count": 0,
        "monitoring_artifact": "monitoring_observations.jsonl",
        "monitoring_areas": [item.to_dict() for item in layout.areas],
        "fixed_monitoring_stations": [
            item.to_dict() for item in layout.stations
        ],
    }


def build_mobile_observations(
    layout: MonitoringLayout,
    states: Sequence[Any],
    tick: int,
    risk_observation: Optional[Any] = None,
    telemetry_source: str = "carla_runtime",
) -> List[MonitoringObservation]:
    """Build lightweight mobile-equipment records for the demo."""

    result = []
    collected_at = datetime.now(timezone.utc).isoformat()
    for state in states:
        vehicle_id = str(state.vehicle_id)
        equipment_type = str(state.equipment_type)
        metrics = {
            "speed_mps": round(float(state.speed_mps), 4),
            "battery_percent": round(
                float(state.battery_percent), 2
            ),
            "yaw_deg": round(float(state.yaw_deg), 2),
        }
        if equipment_type == "fusion_slope_monitoring":
            mobile_metrics = (
                dict(risk_observation.mobile_equipment)
                if risk_observation is not None
                else {
                    "mobile_displacement_mm": 4.0
                    + min(max(tick, 0), 600) * 0.01,
                    "mobile_crack_width_mm": 0.8
                    + min(max(tick, 0), 600) * 0.002,
                }
            )
            metrics.update(
                {
                    str(key): round(float(value), 4)
                    for key, value in mobile_metrics.items()
                }
            )
        elif equipment_type == "mine_emergency_execution":
            metrics.update(
                {
                    "vibration_mm_s": round(
                        2.0 + (tick % 100) * 0.005, 4
                    ),
                    "surface_temperature_c": round(
                        32.0 + (tick % 80) * 0.02, 4
                    ),
                }
            )
        else:
            metrics.update(
                {
                    "dust_intensity_0_1": round(
                        0.18 + (tick % 120) * 0.001, 4
                    ),
                    "visibility_m": round(
                        1450.0 - (tick % 120) * 1.5, 2
                    ),
                }
            )

        healthy = str(state.health).lower() not in {
            "fault", "failed", "error"
        }
        result.append(
            MonitoringObservation(
                observation_id="{}-{}-mobile-{:06d}".format(
                    layout.layout_id, vehicle_id, tick
                ),
                tick=int(tick),
                sample_id=(
                    str(risk_observation.sample_id)
                    if risk_observation is not None
                    else "mobile-sample-{:06d}".format(tick)
                ),
                collected_at=collected_at,
                timestamp_source="simulation_tick",
                source_id=vehicle_id,
                source_display_name=str(state.display_name),
                source_type="mobile_equipment",
                station_type=equipment_type,
                area_id=(
                    str(risk_observation.zone_id)
                    if risk_observation is not None
                    else "mobile_route"
                ),
                position={
                    "x": round(float(state.position.x), 4),
                    "y": round(float(state.position.y), 4),
                    "z": round(float(state.position.z), 4),
                },
                metrics=metrics,
                quality={
                    "status": "valid" if healthy else "degraded",
                    "online": True,
                    "latency_ms": 100,
                    "missing_metrics": [],
                    "completeness": 1.0,
                    "telemetry_source": telemetry_source,
                    "sensor_values_source": "synthetic_demo",
                    "health": str(state.health),
                    "available": bool(state.available),
                    "task_status": str(state.task_status),
                    "current_task_id": state.current_task_id,
                },
                dataset_label=layout.dataset_label,
                synthetic=True,
            )
        )
    return result


def _station_metrics(
    station: FixedMonitoringStation,
    sample_index: int,
    risk_observation: Optional[Any],
) -> Dict[str, float]:
    risk_metrics = {}
    if (
        risk_observation is not None
        and station.risk_source_zone_id
        == str(risk_observation.zone_id)
    ):
        risk_metrics = dict(risk_observation.fixed_station)

    values = {}
    for metric in station.metrics:
        if metric in risk_metrics:
            values[metric] = round(float(risk_metrics[metric]), 4)
            continue
        if metric not in station.baseline_metrics:
            continue
        baseline = station.baseline_metrics[metric]
        trend = station.trend_per_sample.get(metric, 0.0)
        values[metric] = round(
            baseline + trend * sample_index,
            4,
        )
    return values


def _position(raw: Dict[str, Any]) -> Dict[str, float]:
    return {
        "x": float(raw["x"]),
        "y": float(raw["y"]),
        "z": float(raw["z"]),
    }


def _validate_layout(layout: MonitoringLayout) -> None:
    if len(layout.areas) < 3:
        raise MonitoringConfigError(
            "Monitoring layout requires at least three target areas"
        )
    if len(layout.stations) < 3:
        raise MonitoringConfigError(
            "Monitoring layout requires at least three fixed stations"
        )
    if not layout.sample_ticks:
        raise MonitoringConfigError("Monitoring sample_ticks is empty")

    area_ids = [item.area_id for item in layout.areas]
    station_ids = [item.station_id for item in layout.stations]
    if len(area_ids) != len(set(area_ids)):
        raise MonitoringConfigError("Monitoring area_id values must be unique")
    if len(station_ids) != len(set(station_ids)):
        raise MonitoringConfigError(
            "Monitoring station_id values must be unique"
        )

    known_areas = set(area_ids)
    covered_areas = set()
    for station in layout.stations:
        if station.area_id not in known_areas:
            raise MonitoringConfigError(
                "Station {} references unknown area {}".format(
                    station.station_id, station.area_id
                )
            )
        if not station.metrics:
            raise MonitoringConfigError(
                "Station {} has no metrics".format(station.station_id)
            )
        covered_areas.add(station.area_id)
    missing_coverage = known_areas.difference(covered_areas)
    if missing_coverage:
        raise MonitoringConfigError(
            "Monitoring areas without stations: {}".format(
                sorted(missing_coverage)
            )
        )

"""SQLite sidecar storage for run-scoped evidence.

The existing JSON/JSONL artifacts remain the canonical competition evidence.
This module adds a structured, queryable index without participating in
CARLA control, scheduling, or safety decisions.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


SCHEMA_VERSION = "2"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SqliteRunStore:
    """Append-oriented structured index for one or more simulation runs."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.database_path))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scenario_runs (
                run_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                scenario_seed INTEGER,
                scenario_mode TEXT,
                simulator_mode TEXT,
                status TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tick INTEGER,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run_tick
                ON events(run_id, tick, event_id);

            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                zone_id TEXT,
                task_type TEXT,
                priority INTEGER,
                status TEXT,
                assigned_vehicle_id TEXT,
                updated_tick INTEGER,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS task_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT,
                timestamp TEXT NOT NULL,
                tick INTEGER,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                reason TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS risk_assessments (
                assessment_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tick INTEGER,
                zone_id TEXT,
                level TEXT,
                trend TEXT,
                reasons_json TEXT,
                metrics_json TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, assessment_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                decision_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tick INTEGER,
                context TEXT,
                task_id TEXT,
                vehicle_id TEXT,
                score REAL,
                policy_version TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, decision_key),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS monitoring_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                record_count INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS work_orders (
                run_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL,
                task_id TEXT,
                status TEXT,
                priority INTEGER,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, work_order_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS metrics (
                run_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value REAL,
                unit TEXT,
                source TEXT NOT NULL,
                PRIMARY KEY(run_id, metric_name),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                record_count INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS scenario_templates (
                scenario_id TEXT NOT NULL,
                scenario_version TEXT NOT NULL,
                scenario_name TEXT,
                config_path TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(scenario_id, scenario_version)
            );

            CREATE TABLE IF NOT EXISTS vehicle_master (
                vehicle_id TEXT PRIMARY KEY,
                display_name TEXT,
                equipment_type TEXT,
                blueprint_id TEXT,
                capabilities_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_vehicles (
                run_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                initial_role TEXT,
                initial_status TEXT,
                available INTEGER NOT NULL,
                active INTEGER NOT NULL,
                in_traffic INTEGER NOT NULL,
                spawn_point_index INTEGER,
                capability_snapshot_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, vehicle_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id),
                FOREIGN KEY(vehicle_id) REFERENCES vehicle_master(vehicle_id)
            );

            CREATE TABLE IF NOT EXISTS episode_tasks (
                run_id TEXT NOT NULL,
                episode_task_id TEXT NOT NULL,
                zone_id TEXT,
                task_type TEXT,
                priority INTEGER,
                required_capabilities_json TEXT NOT NULL,
                preferred_vehicle_id TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, episode_task_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS episode_events (
                run_id TEXT NOT NULL,
                episode_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                trigger_tick INTEGER,
                target_vehicle_id TEXT,
                parameters_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, episode_event_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS decision_candidates (
                run_id TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                candidate_rank INTEGER,
                score REAL,
                selected INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, decision_key, vehicle_id),
                FOREIGN KEY(run_id, decision_key)
                    REFERENCES decisions(run_id, decision_key)
            );

            CREATE TABLE IF NOT EXISTS route_plans (
                run_id TEXT NOT NULL,
                route_plan_id TEXT NOT NULL,
                vehicle_id TEXT,
                task_id TEXT,
                planner_version TEXT,
                start_node TEXT,
                goal_node TEXT,
                distance_m REAL,
                travel_time_s REAL,
                risk_cost REAL,
                status TEXT,
                replan_reason TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, route_plan_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );
            """
        )
        self._ensure_v2_columns()
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )
        self.connection.commit()

    def _ensure_v2_columns(self) -> None:
        """Add V2 metadata columns without invalidating an existing V1 DB."""

        additions = {
            "scenario_version": "TEXT",
            "fleet_size": "INTEGER",
            "available_fleet_size": "INTEGER",
            "active_fleet_size": "INTEGER",
            "traffic_fleet_size": "INTEGER",
            "task_load": "TEXT",
            "traffic_density": "TEXT",
            "policy_version": "TEXT",
            "route_planner_version": "TEXT",
            "risk_model_version": "TEXT",
            "episode_json": "TEXT",
        }
        existing = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(scenario_runs)")
        }
        for name, definition in additions.items():
            if name not in existing:
                self.connection.execute(
                    "ALTER TABLE scenario_runs ADD COLUMN {} {}".format(
                        name, definition
                    )
                )

    def start_run(self, run_id: str, scenario_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO scenario_runs(run_id, scenario_id, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, scenario_id, _utc_now(), "running"),
        )
        self.connection.commit()

    def record_episode(
        self,
        episode: Any,
        config_path: Optional[Path] = None,
        policy_version: Optional[str] = None,
        route_planner_version: Optional[str] = None,
        risk_model_version: Optional[str] = None,
    ) -> None:
        """Persist one planned episode before CARLA execution begins.

        Planned tasks and events use dedicated ``episode_*`` tables.  The
        ``events`` table remains reserved for facts that actually occurred.
        """

        snapshot = dict(episode.fleet_snapshot)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO scenario_runs(run_id, scenario_id, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (episode.run_id, episode.scenario_id, _utc_now(), "planned"),
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO scenario_templates(
                scenario_id, scenario_version, scenario_name, config_path, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                episode.scenario_id,
                episode.scenario_version,
                episode.scenario_name,
                str(config_path) if config_path is not None else None,
                _utc_now(),
            ),
        )
        self.connection.execute(
            """
            UPDATE scenario_runs
            SET scenario_seed = ?, scenario_version = ?, fleet_size = ?,
                available_fleet_size = ?, active_fleet_size = ?,
                traffic_fleet_size = ?, task_load = ?, traffic_density = ?,
                policy_version = ?, route_planner_version = ?, risk_model_version = ?,
                episode_json = ?
            WHERE run_id = ?
            """,
            (
                episode.seed,
                episode.scenario_version,
                snapshot.get("total_vehicles"),
                snapshot.get("available_vehicles"),
                snapshot.get("active_vehicles"),
                snapshot.get("traffic_vehicles"),
                snapshot.get("task_load"),
                snapshot.get("traffic_density"),
                policy_version,
                route_planner_version,
                risk_model_version,
                _json(episode.to_dict()),
                episode.run_id,
            ),
        )
        for vehicle in episode.vehicles:
            master_payload = {
                "vehicle_id": vehicle.vehicle_id,
                "display_name": vehicle.display_name,
                "equipment_type": vehicle.equipment_type,
                "blueprint": vehicle.blueprint,
                "capabilities": vehicle.capabilities,
            }
            self.connection.execute(
                """
                INSERT OR REPLACE INTO vehicle_master(
                    vehicle_id, display_name, equipment_type, blueprint_id,
                    capabilities_json, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vehicle.vehicle_id,
                    vehicle.display_name,
                    vehicle.equipment_type,
                    vehicle.blueprint,
                    _json(vehicle.capabilities),
                    1,
                    _utc_now(),
                ),
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO run_vehicles(
                    run_id, vehicle_id, initial_role, initial_status, available,
                    active, in_traffic, spawn_point_index,
                    capability_snapshot_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.run_id,
                    vehicle.vehicle_id,
                    vehicle.initial_role,
                    vehicle.initial_status,
                    int(vehicle.available),
                    int(vehicle.active),
                    int(vehicle.in_traffic),
                    vehicle.spawn_point_index,
                    _json(vehicle.capabilities),
                    _json(master_payload),
                ),
            )
        for task in episode.tasks:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO episode_tasks(
                    run_id, episode_task_id, zone_id, task_type, priority,
                    required_capabilities_json, preferred_vehicle_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.run_id,
                    task.task_id,
                    task.zone_id,
                    task.task_type,
                    task.priority,
                    _json(task.required_capabilities),
                    task.preferred_vehicle_id,
                    _json(task.__dict__),
                ),
            )
        for event in episode.events:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO episode_events(
                    run_id, episode_event_id, event_type, trigger_tick,
                    target_vehicle_id, parameters_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.run_id,
                    event.event_id,
                    event.event_type,
                    event.trigger_tick,
                    event.target_vehicle_id,
                    _json(event.parameters),
                    _json(event.__dict__),
                ),
            )
        self.connection.commit()

    def record_event(
        self,
        run_id: str,
        timestamp: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        tick = payload.get("tick")
        self.connection.execute(
            """
            INSERT INTO events(run_id, timestamp, tick, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, timestamp, tick, event_type, _json(payload)),
        )
        if event_type == "risk_assessed":
            assessment_id = str(payload.get("assessment_id", "unknown"))
            self.connection.execute(
                """
                INSERT OR REPLACE INTO risk_assessments(
                    assessment_id, run_id, timestamp, tick, zone_id, level,
                    trend, reasons_json, metrics_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    run_id,
                    timestamp,
                    tick,
                    payload.get("zone_id"),
                    payload.get("level"),
                    payload.get("trend"),
                    _json(payload.get("reasons", [])),
                    _json(payload.get("metrics", {})),
                    _json(payload),
                ),
            )
        elif event_type == "agent_decision":
            action = payload.get("scheduler_agent_action", {})
            decision_key = str(
                payload.get("decision_id")
                or "{}:{}:{}".format(
                    payload.get("task_id", "task"),
                    payload.get("tick", "none"),
                    action.get("assigned_vehicle_id", "vehicle"),
                )
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO decisions(
                    decision_key, run_id, timestamp, tick, context, task_id,
                    vehicle_id, score, policy_version, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_key,
                    run_id,
                    timestamp,
                    payload.get("tick"),
                    payload.get("context"),
                    payload.get("task_id"),
                    action.get("assigned_vehicle_id"),
                    action.get("score"),
                    action.get("policy_version"),
                    _json(payload),
                ),
            )
            candidates = payload.get("candidate_evaluations", [])
            if not isinstance(candidates, list):
                candidates = []
            selected_vehicle_id = action.get("assigned_vehicle_id")
            for rank, candidate in enumerate(candidates, start=1):
                if not isinstance(candidate, dict) or not candidate.get("vehicle_id"):
                    continue
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO decision_candidates(
                        run_id, decision_key, vehicle_id, candidate_rank, score,
                        selected, reason, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        decision_key,
                        str(candidate["vehicle_id"]),
                        rank,
                        candidate.get("score"),
                        int(candidate.get("vehicle_id") == selected_vehicle_id),
                        candidate.get("reason"),
                        _json(candidate),
                    ),
                )
        elif event_type.startswith("task_") or event_type in {
            "hazard_task_released",
            "task_reassigned",
        }:
            self.connection.execute(
                """
                INSERT INTO task_transitions(
                    run_id, task_id, timestamp, tick, event_type, from_status,
                    to_status, reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    payload.get("task_id"),
                    timestamp,
                    tick,
                    event_type,
                    payload.get("from_status"),
                    payload.get("to_status") or payload.get("status"),
                    payload.get("reason") or payload.get("status_reason"),
                    _json(payload),
                ),
            )
        self.connection.commit()

    def record_route_plan(self, run_id: str, route_plan: Dict[str, Any]) -> None:
        """Record a route/replanning summary, not high-frequency waypoints."""

        route_plan_id = str(route_plan.get("route_plan_id", "")).strip()
        if not route_plan_id:
            raise ValueError("route_plan_id is required")
        self.connection.execute(
            """
            INSERT OR REPLACE INTO route_plans(
                run_id, route_plan_id, vehicle_id, task_id, planner_version,
                start_node, goal_node, distance_m, travel_time_s, risk_cost,
                status, replan_reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                route_plan_id,
                route_plan.get("vehicle_id"),
                route_plan.get("task_id"),
                route_plan.get("planner_version"),
                route_plan.get("start_node"),
                route_plan.get("goal_node"),
                route_plan.get("distance_m"),
                route_plan.get("travel_time_s"),
                route_plan.get("risk_cost"),
                route_plan.get("status"),
                route_plan.get("replan_reason"),
                _json(route_plan),
            ),
        )
        self.connection.commit()

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        artifact_path: Path,
        record_count: Optional[int] = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO artifacts(
                run_id, artifact_type, artifact_path, record_count, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                artifact_type,
                str(artifact_path),
                record_count,
                _utc_now(),
            ),
        )
        if artifact_type == "monitoring_observations":
            self.connection.execute(
                """
                INSERT INTO monitoring_batches(
                    run_id, source, artifact_path, record_count, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, "fixed_and_mobile", str(artifact_path), record_count, _utc_now()),
            )
        self.connection.commit()

    def update_from_summary(
        self, run_id: str, summary: Dict[str, Any]
    ) -> None:
        self.connection.execute(
            """
            UPDATE scenario_runs
            SET scenario_seed = ?, scenario_mode = ?, simulator_mode = ?,
                status = ?, ended_at = ?, summary_json = ?
            WHERE run_id = ?
            """,
            (
                summary.get("scenario_seed"),
                summary.get("scenario_mode"),
                summary.get("mode"),
                summary.get("status"),
                _utc_now(),
                _json(summary),
                run_id,
            ),
        )
        for task in summary.get("tasks", []):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO tasks(
                    run_id, task_id, zone_id, task_type, priority, status,
                    assigned_vehicle_id, updated_tick, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task.get("task_id"),
                    task.get("zone_id"),
                    task.get("task_type"),
                    task.get("priority"),
                    task.get("status"),
                    task.get("assigned_vehicle_id"),
                    task.get("completed_tick") or task.get("started_tick"),
                    _json(task),
                ),
            )
        for work_order in summary.get("work_orders", []):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO work_orders(
                    run_id, work_order_id, task_id, status, priority, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    work_order.get("work_order_id"),
                    work_order.get("task_id"),
                    work_order.get("status"),
                    work_order.get("priority"),
                    _json(work_order),
                ),
            )
        self._store_metrics(run_id, summary)
        self.connection.commit()

    def _store_metrics(self, run_id: str, summary: Dict[str, Any]) -> None:
        candidates = {}
        for container_name in ("result", "metrics"):
            container = summary.get(container_name, {})
            if isinstance(container, dict):
                candidates.update(container)
        for key in (
            "task_completion_rate",
            "completion_rate",
            "mobile_observation_count",
            "fixed_observation_count",
            "closed_loop_feedback_count",
            "takeover_completed",
            "failure_injected",
        ):
            if key in summary:
                candidates[key] = summary[key]
        for name, value in candidates.items():
            if isinstance(value, bool):
                value = int(value)
            if not isinstance(value, (int, float)):
                continue
            self.connection.execute(
                """
                INSERT OR REPLACE INTO metrics(
                    run_id, metric_name, metric_value, unit, source
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, name, float(value), None, "summary"),
            )

    def close(self) -> None:
        self.connection.close()

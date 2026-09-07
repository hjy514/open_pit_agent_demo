"""SQLite sidecar storage for run-scoped evidence.

The existing JSON/JSONL artifacts remain the canonical competition evidence.
This module adds a structured, queryable index without participating in
CARLA control, scheduling, or safety decisions.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


SCHEMA_VERSION = "3"


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

            CREATE TABLE IF NOT EXISTS closed_loop_cycles (
                run_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                scenario_key TEXT,
                status TEXT NOT NULL,
                revision_before INTEGER,
                revision_after INTEGER,
                physical_execution INTEGER NOT NULL DEFAULT 0,
                measurement_status TEXT,
                state_before_json TEXT NOT NULL,
                risk_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                scheduling_json TEXT NOT NULL,
                route_json TEXT NOT NULL,
                safety_json TEXT NOT NULL,
                command_json TEXT NOT NULL,
                feedback_json TEXT NOT NULL,
                next_state_json TEXT NOT NULL,
                trace_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, cycle_id),
                FOREIGN KEY(run_id) REFERENCES scenario_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_closed_loop_cycles_run_revision
                ON closed_loop_cycles(run_id, revision_before, revision_after);
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

    def finalize_unfinished_run(
        self, run_id: str,
        status: str = "INCOMPLETE",
    ) -> bool:
        """Close only a still-running/planned run without overwriting results."""
        cursor = self.connection.execute(
            "UPDATE scenario_runs SET status=?, ended_at=? "
            "WHERE run_id=? AND status IN ('running','planned')",
            (str(status), _utc_now(), str(run_id)),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def mark_stale_runs_incomplete(
        self, stale_after_hours: float = 24.0
    ) -> List[str]:
        """Explicitly close old interrupted runs; never delete their evidence."""
        hours = float(stale_after_hours)
        if hours <= 0:
            raise ValueError("stale_after_hours must be positive")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        run_ids = [
            str(row[0]) for row in self.connection.execute(
                "SELECT run_id FROM scenario_runs "
                "WHERE status IN ('running','planned') AND started_at<? "
                "ORDER BY started_at", (cutoff,),
            ).fetchall()
        ]
        if run_ids:
            self.connection.execute(
                "UPDATE scenario_runs SET status='INCOMPLETE', ended_at=? "
                "WHERE status IN ('running','planned') AND started_at<?",
                (_utc_now(), cutoff),
            )
            self.connection.commit()
        return run_ids

    def database_health_report(
        self, stale_after_hours: float = 24.0
    ) -> Dict[str, Any]:
        """Return read-only lifecycle, volume and closed-loop coverage facts."""
        hours = float(stale_after_hours)
        if hours <= 0:
            raise ValueError("stale_after_hours must be positive")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        tables = [
            str(row[0]) for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        row_counts = {}
        for table in tables:
            row_counts[table] = int(self.connection.execute(
                'SELECT count(*) FROM "{}"'.format(table.replace('"', '""'))
            ).fetchone()[0])
        run_status_counts = {
            str(row[0] if row[0] is not None else "NULL"): int(row[1])
            for row in self.connection.execute(
                "SELECT status,count(*) FROM scenario_runs GROUP BY status"
            ).fetchall()
        }
        cycle_scenarios = {
            str(row[0] if row[0] is not None else "unknown"): int(row[1])
            for row in self.connection.execute(
                "SELECT scenario_key,count(*) FROM closed_loop_cycles "
                "GROUP BY scenario_key"
            ).fetchall()
        }
        event_types = [{"event_type": str(row[0]), "count": int(row[1])}
                       for row in self.connection.execute(
            "SELECT event_type,count(*) AS total FROM events "
            "GROUP BY event_type ORDER BY total DESC LIMIT 10"
        ).fetchall()]
        high_volume_runs = [{"run_id": str(row[0]), "event_count": int(row[1])}
                            for row in self.connection.execute(
            "SELECT run_id,count(*) AS total FROM events GROUP BY run_id "
            "HAVING total>=1000 ORDER BY total DESC LIMIT 20"
        ).fetchall()]
        stale_count = int(self.connection.execute(
            "SELECT count(*) FROM scenario_runs "
            "WHERE status IN ('running','planned') AND started_at<?",
            (cutoff,),
        ).fetchone()[0])
        cycle_count = row_counts.get("closed_loop_cycles", 0)
        physical_cycles = int(self.connection.execute(
            "SELECT count(*) FROM closed_loop_cycles WHERE physical_execution=1"
        ).fetchone()[0])
        return {
            "schema_version": SCHEMA_VERSION,
            "database_path": str(self.database_path),
            "database_size_bytes": self.database_path.stat().st_size,
            "table_count": len(tables),
            "row_counts": row_counts,
            "run_status_counts": dict(sorted(run_status_counts.items())),
            "stale_after_hours": hours,
            "stale_unfinished_run_count": stale_count,
            "closed_loop": {
                "cycle_count": cycle_count,
                "physical_cycle_count": physical_cycles,
                "structural_cycle_count": cycle_count - physical_cycles,
                "scenario_counts": dict(sorted(cycle_scenarios.items())),
            },
            "event_volume": {
                "event_count": row_counts.get("events", 0),
                "average_events_per_run": round(
                    row_counts.get("events", 0)
                    / float(max(1, row_counts.get("scenario_runs", 0))), 3
                ),
                "top_event_types": event_types,
                "runs_with_at_least_1000_events": high_volume_runs,
            },
            "actions": {
                "automatic_deletion_performed": False,
                "stale_repair_available": True,
                "event_retention_change_applied": False,
            },
        }

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

    def record_closed_loop_cycle(
        self, run_id: str, scenario_key: str, cycle: Dict[str, Any]
    ) -> None:
        """Persist one low-frequency state/action/feedback transition."""

        if cycle.get("schema_version") != "openpit.closed-loop-cycle.v1":
            raise ValueError("unsupported closed-loop cycle schema")
        cycle_id = str(cycle.get("cycle_id") or "").strip()
        if not cycle_id:
            raise ValueError("closed-loop cycle_id is required")
        stage_results = cycle.get("stage_results", {})
        if not isinstance(stage_results, dict):
            raise ValueError("closed-loop stage_results must be an object")
        feedback = cycle.get("execution_feedback", [])
        if not isinstance(feedback, list):
            raise ValueError("closed-loop execution_feedback must be a list")
        physical_execution = any(
            bool(item.get("physical_execution"))
            for item in feedback if isinstance(item, dict)
        )
        measurement_values = sorted({
            str(item.get("measurement_status"))
            for item in feedback
            if isinstance(item, dict) and item.get("measurement_status")
        })
        scheduling = stage_results.get("scheduling", {})
        command = (
            scheduling.get("command", {})
            if isinstance(scheduling, dict) else {}
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO closed_loop_cycles(
                run_id, cycle_id, schema_version, scenario_key, status,
                revision_before, revision_after, physical_execution,
                measurement_status, state_before_json, risk_json,
                decision_json, scheduling_json, route_json, safety_json,
                command_json, feedback_json, next_state_json, trace_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, cycle_id, cycle["schema_version"], scenario_key,
                cycle.get("status"), cycle.get("revision_before"),
                cycle.get("revision_after"), int(physical_execution),
                ",".join(measurement_values) if measurement_values else None,
                _json(cycle.get("state_before", {})),
                _json(stage_results.get("risk", {})),
                _json(stage_results.get("decision", {})),
                _json(scheduling if isinstance(scheduling, dict) else {}),
                _json(stage_results.get("planning", {})),
                _json(stage_results.get("safety", {})),
                _json(command), _json(feedback),
                _json(cycle.get("next_state", {})),
                _json(cycle.get("trace", [])), _utc_now(),
            ),
        )
        self.connection.commit()

    def load_closed_loop_cycles(
        self, scenario_keys: Optional[Sequence[str]] = None,
        run_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Read complete V3 decision cycles for dataset/export consumers.

        This is deliberately a low-frequency business-data query.  It does
        not reconstruct missing cycles from events and it never treats the
        map-resource database as runtime state.
        """

        fields = (
            "state_before", "risk", "decision", "scheduling", "route",
            "safety", "command", "feedback", "next_state", "trace",
        )
        sql = """
            SELECT c.run_id, c.cycle_id, c.schema_version, c.scenario_key,
                   c.status, c.revision_before, c.revision_after,
                   c.physical_execution, c.measurement_status,
                   c.state_before_json, c.risk_json, c.decision_json,
                   c.scheduling_json, c.route_json, c.safety_json,
                   c.command_json, c.feedback_json, c.next_state_json,
                   c.trace_json, c.created_at,
                   r.scenario_id, r.scenario_seed, r.scenario_mode,
                   r.simulator_mode, r.status, r.summary_json
            FROM closed_loop_cycles AS c
            JOIN scenario_runs AS r ON r.run_id = c.run_id
        """
        parameters = []
        normalized_keys = sorted(set(
            str(item).strip() for item in (scenario_keys or ())
            if str(item).strip()
        ))
        normalized_run_ids = sorted(set(
            str(item).strip() for item in (run_ids or ())
            if str(item).strip()
        ))
        conditions = []
        if normalized_keys:
            conditions.append("c.scenario_key IN ({})".format(
                ",".join("?" for _ in normalized_keys)
            ))
            parameters.extend(normalized_keys)
        if normalized_run_ids:
            conditions.append("c.run_id IN ({})".format(
                ",".join("?" for _ in normalized_run_ids)
            ))
            parameters.extend(normalized_run_ids)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY c.created_at, c.run_id, c.cycle_id"
        rows = self.connection.execute(sql, parameters).fetchall()
        output = []
        for row in rows:
            item = {
                "run_id": row[0], "cycle_id": row[1],
                "schema_version": row[2], "scenario_key": row[3],
                "status": row[4], "revision_before": row[5],
                "revision_after": row[6],
                "physical_execution": bool(row[7]),
                "measurement_status": row[8], "created_at": row[19],
                "scenario_id": row[20], "scenario_seed": row[21],
                "scenario_mode": row[22], "simulator_mode": row[23],
                "run_status": row[24],
            }
            load_errors = []
            for index, name in enumerate(fields, 9):
                try:
                    item[name] = json.loads(row[index])
                except (TypeError, ValueError) as exc:
                    item[name] = None
                    load_errors.append("{}: {}".format(name, exc))
            try:
                item["run_summary"] = (
                    json.loads(row[25]) if row[25] else {}
                )
            except (TypeError, ValueError) as exc:
                item["run_summary"] = {}
                load_errors.append("run_summary: {}".format(exc))
            item["load_errors"] = load_errors
            output.append(item)
        return output

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
                status = ?, ended_at = ?, summary_json = ?,
                policy_version = COALESCE(?, policy_version),
                route_planner_version = COALESCE(?, route_planner_version),
                risk_model_version = COALESCE(?, risk_model_version)
            WHERE run_id = ?
            """,
            (
                summary.get("scenario_seed"),
                summary.get("scenario_mode"),
                summary.get("mode"),
                summary.get("status"),
                _utc_now(),
                _json(summary),
                summary.get("policy_version"),
                summary.get("route_planner_version"),
                summary.get("risk_model_version"),
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

    def validate_closed_loop_evidence(
        self, run_id: str, scenario_key: str, expected: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate persisted business evidence and store the binary metric."""
        checks = []

        def check(name, passed, detail):
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        run = self.connection.execute(
            "SELECT status FROM scenario_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        check("db_run_pass", bool(run) and run[0] == "PASS", run[0] if run else None)
        task_count, completed_count = self.connection.execute(
            "SELECT count(*),sum(CASE WHEN status='completed' THEN 1 ELSE 0 END) "
            "FROM tasks WHERE run_id=?", (run_id,)
        ).fetchone()
        expected_tasks = int(expected.get("task_count") or 0)
        check("db_task_count", task_count == expected_tasks,
              "{} expected {}".format(task_count, expected_tasks))
        check("db_all_tasks_completed", expected_tasks > 0 and completed_count == expected_tasks,
              "{}/{}".format(completed_count or 0, expected_tasks))
        event_rows = self.connection.execute(
            "SELECT event_type FROM events WHERE run_id=? ORDER BY event_id", (run_id,)
        ).fetchall()
        event_types = [str(row[0]) for row in event_rows]
        if isinstance(expected.get("closed_loop_cycle"), dict):
            cycle_rows = self.connection.execute(
                "SELECT schema_version,status,revision_before,revision_after "
                "FROM closed_loop_cycles WHERE run_id=?", (run_id,)
            ).fetchall()
            check(
                "db_closed_loop_cycle_present",
                len(cycle_rows) == 1
                and cycle_rows[0][0] == "openpit.closed-loop-cycle.v1"
                and cycle_rows[0][1] == expected["closed_loop_cycle"].get("status")
                and cycle_rows[0][2] == expected["closed_loop_cycle"].get(
                    "revision_before"
                )
                and cycle_rows[0][3] == expected["closed_loop_cycle"].get(
                    "revision_after"
                ),
                cycle_rows,
            )

        def ordered(required):
            cursor = -1
            for event_type in required:
                try:
                    cursor = event_types.index(event_type, cursor + 1)
                except ValueError:
                    return False
            return True

        scenario_key = str(scenario_key)
        if scenario_key == "s01":
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s01_decisions_present", decisions >= expected_tasks,
                  decisions)
        elif scenario_key == "s02":
            check("db_s02_event_order", ordered([
                "vehicle_fault", "task_released", "task_reassigned",
                "task_completed", "run_completed",
            ]), event_types)
            check("db_s02_reassignment_count", event_types.count("task_reassigned") == int(
                expected.get("reassignment_count") or 0
            ), event_types.count("task_reassigned"))
        elif scenario_key == "s03":
            check("db_s03_event_order", ordered([
                "equipment_fault", "task_paused", "agent_decision",
                "task_work_point_switched", "task_completed",
                "equipment_recovered", "run_completed",
            ]), event_types)
            affected = int(expected.get("affected_task_count") or 0)
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s03_decision_count", decisions == affected,
                  "{} expected {}".format(decisions, affected))
            check("db_s03_pause_count", event_types.count("task_paused") == affected,
                  event_types.count("task_paused"))
            check("db_s03_switch_count", event_types.count(
                "task_work_point_switched") == affected,
                event_types.count("task_work_point_switched"))
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s03_route_plan_count", route_count == expected_tasks + affected,
                  "{} expected {}".format(route_count, expected_tasks + affected))
        elif scenario_key == "s04":
            check("db_s04_event_order", ordered([
                "blast_announced", "blast_control_activated", "agent_decision",
                "task_completed", "blast_area_cleared", "road_reopened",
                "run_completed",
            ]), event_types)
            affected = int(expected.get("affected_task_count") or 0)
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s04_decision_count", decisions == affected,
                  "{} expected {}".format(decisions, affected))
            response_count = (
                event_types.count("blast_safe_route_planned")
                + event_types.count("vehicle_held_for_blast")
            )
            check("db_s04_response_count", response_count == affected,
                  "{} expected {}".format(response_count, affected))
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s04_route_plan_count", route_count == expected_tasks + affected,
                  "{} expected {}".format(route_count, expected_tasks + affected))
        elif scenario_key == "s05":
            check("db_s05_event_order", ordered([
                "weather_started", "road_restricted", "agent_decision",
                "task_completed", "road_restored", "weather_recovered",
                "run_completed",
            ]), event_types)
            affected = int(expected.get("affected_task_count") or 0)
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s05_decision_count", decisions == affected,
                  "{} expected {}".format(decisions, affected))
            response_count = (
                event_types.count("weather_route_replanned")
                + event_types.count("weather_speed_restricted")
            )
            check("db_s05_response_count", response_count == affected,
                  "{} expected {}".format(response_count, affected))
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s05_route_plan_count", route_count == expected_tasks + affected,
                  "{} expected {}".format(route_count, expected_tasks + affected))
        elif scenario_key == "s06":
            check("db_s06_event_order", ordered([
                "congestion_detected", "traffic_control_activated",
                "agent_decision", "task_completed", "congestion_cleared",
                "traffic_control_released", "run_completed",
            ]), event_types)
            affected = int(expected.get("affected_task_count") or 0)
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s06_decision_count", decisions == affected,
                  "{} expected {}".format(decisions, affected))
            response_count = (
                event_types.count("vehicle_held")
                + event_types.count("bottleneck_entry_authorized")
            )
            check("db_s06_response_count", response_count == affected,
                  "{} expected {}".format(response_count, affected))
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s06_route_plan_count", route_count == expected_tasks,
                  "{} expected {}".format(route_count, expected_tasks))
        elif scenario_key == "s07":
            check("db_s07_event_order", ordered([
                "road_closed", "agent_decision", "route_replanned",
                "task_completed", "road_reopened", "run_completed",
            ]), event_types)
            affected = int(expected.get("affected_task_count") or 0)
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s07_route_plan_count", route_count == expected_tasks + affected,
                  "{} expected {}".format(route_count, expected_tasks + affected))
            check("db_s07_replan_event_count", event_types.count("route_replanned") == affected,
                  event_types.count("route_replanned"))
            check("db_s07_takeover_event_count", event_types.count("task_reassigned") == int(
                expected.get("takeover_count") or 0
            ), event_types.count("task_reassigned"))
        elif scenario_key == "s09":
            check("db_s09_event_order", ordered([
                "road_closed", "agent_decision", "route_replanned",
                "vehicle_fault", "task_released", "agent_decision",
                "task_reassigned", "task_completed", "road_reopened",
                "run_completed",
            ]), event_types)
            road_affected = int(expected.get("road_affected_task_count") or 0)
            expected_decisions = road_affected + int(
                expected.get("reassignment_count") or 0
            )
            decisions = self.connection.execute(
                "SELECT count(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            check("db_s09_decision_count", decisions == expected_decisions,
                  "{} expected {}".format(decisions, expected_decisions))
            check("db_s09_replan_count", event_types.count(
                "route_replanned") == road_affected,
                event_types.count("route_replanned"))
            check("db_s09_release_reassignment_count",
                  event_types.count("task_released") == 1
                  and event_types.count("task_reassigned") == 1,
                  "release={} reassign={}".format(
                      event_types.count("task_released"),
                      event_types.count("task_reassigned")))
            route_count = self.connection.execute(
                "SELECT count(*) FROM route_plans WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            expected_routes = expected_tasks + road_affected + 1
            check("db_s09_route_plan_count", route_count == expected_routes,
                  "{} expected {}".format(route_count, expected_routes))

        failed = [item["check"] for item in checks if not item["passed"]]
        result = {
            "status": "EVIDENCE_PASS" if not failed else "EVIDENCE_FAIL",
            "run_id": run_id,
            "scenario_key": scenario_key,
            "check_count": len(checks),
            "passed_check_count": len(checks) - len(failed),
            "failed_checks": failed,
            "checks": checks,
        }
        self.connection.execute(
            "INSERT OR REPLACE INTO metrics(run_id,metric_name,metric_value,unit,source) "
            "VALUES(?,?,?,?,?)",
            (run_id, "closed_loop_evidence_pass", 1.0 if not failed else 0.0,
             "boolean", "automatic_closed_loop_validation"),
        )
        self.connection.commit()
        return result

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

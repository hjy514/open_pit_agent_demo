"""SQLite storage skeleton for the Mine Spatial Resource Library V1.

The database is deliberately separate from ``openpit.db``.  ``openpit.db``
records what happened in one run; this module will record durable facts and
calibration results about a CARLA map.  Phase 1 creates only the schema and
the map/version metadata.  It does not infer that any point is safe.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional


SCHEMA_VERSION = "4"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Optional[Path]) -> Optional[str]:
    """Return a content hash for provenance, or None when no file is known."""
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        return None
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MapResourceStore:
    """Versioned static map-resource store.

    All map point/route tables are empty after initialisation.  They are
    filled only by later XODR and CARLA calibration phases.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.database_path))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS maps (
                map_id TEXT PRIMARY KEY,
                map_name TEXT NOT NULL,
                carla_map_name TEXT NOT NULL,
                xodr_file TEXT,
                xodr_hash TEXT,
                carla_version TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS map_resource_versions (
                resource_version TEXT NOT NULL,
                map_id TEXT NOT NULL,
                xodr_hash TEXT,
                carla_map_version TEXT,
                vehicle_blueprint TEXT NOT NULL,
                planner_version TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notes TEXT,
                PRIMARY KEY(resource_version, map_id),
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );

            CREATE TABLE IF NOT EXISTS calibration_runs (
                calibration_run_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT,
                calibration_type TEXT NOT NULL,
                tool_version TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                summary_json TEXT,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );

            CREATE TABLE IF NOT EXISTS road_clusters (
                cluster_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                cluster_type TEXT NOT NULL,
                node_count INTEGER,
                edge_count INTEGER,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );

            CREATE TABLE IF NOT EXISTS map_points (
                point_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                carla_spawn_point_index INTEGER,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                yaw REAL,
                road_id TEXT,
                lane_id INTEGER,
                s REAL,
                lane_type TEXT,
                travel_direction TEXT,
                road_cluster_id TEXT,
                heavy_truck_allowed INTEGER,
                validation_status TEXT NOT NULL,
                clearance_radius_m REAL,
                nearest_point_distance_m REAL,
                hazard_sensitive INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                notes TEXT,
                UNIQUE(map_id, carla_spawn_point_index),
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(road_cluster_id) REFERENCES road_clusters(cluster_id)
            );
            CREATE INDEX IF NOT EXISTS idx_map_points_map_status
                ON map_points(map_id, validation_status);

            CREATE TABLE IF NOT EXISTS point_roles (
                point_id TEXT NOT NULL,
                role TEXT NOT NULL,
                priority INTEGER,
                validated INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                notes TEXT,
                PRIMARY KEY(point_id, role),
                FOREIGN KEY(point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS operating_areas (
                area_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                display_name TEXT NOT NULL,
                area_type TEXT NOT NULL,
                selection_mode TEXT NOT NULL,
                capacity INTEGER,
                allowed_roles_json TEXT NOT NULL,
                point_ids_json TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );
            CREATE INDEX IF NOT EXISTS idx_operating_areas_map_status
                ON operating_areas(map_id, resource_version, validation_status);

            CREATE TABLE IF NOT EXISTS point_conflicts (
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                point_a_id TEXT NOT NULL,
                point_b_id TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                minimum_clearance_m REAL,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                CHECK(point_a_id < point_b_id),
                PRIMARY KEY(resource_version, point_a_id, point_b_id, conflict_type),
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(point_a_id) REFERENCES map_points(point_id),
                FOREIGN KEY(point_b_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS road_nodes (
                node_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                point_id TEXT,
                road_id TEXT,
                lane_id INTEGER,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                node_type TEXT NOT NULL,
                source TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS road_edges (
                edge_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                from_node_id TEXT NOT NULL,
                to_node_id TEXT NOT NULL,
                road_id TEXT,
                lane_id INTEGER,
                length_m REAL,
                speed_limit_kmh REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                source TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                geometry_json TEXT,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(from_node_id) REFERENCES road_nodes(node_id),
                FOREIGN KEY(to_node_id) REFERENCES road_nodes(node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_road_edges_from_to
                ON road_edges(resource_version, from_node_id, to_node_id);

            CREATE TABLE IF NOT EXISTS reachable_pairs (
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                from_point_id TEXT NOT NULL,
                to_point_id TEXT NOT NULL,
                planner_version TEXT NOT NULL,
                reachable INTEGER NOT NULL,
                route_length_m REAL,
                endpoint_error_m REAL,
                junction_count INTEGER,
                route_hash TEXT,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                PRIMARY KEY(resource_version, from_point_id, to_point_id, planner_version),
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(from_point_id) REFERENCES map_points(point_id),
                FOREIGN KEY(to_point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS route_execution_validations (
                validation_id TEXT PRIMARY KEY,
                calibration_run_id TEXT NOT NULL,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                route_profile_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                from_point_id TEXT NOT NULL,
                to_point_id TEXT NOT NULL,
                vehicle_blueprint TEXT NOT NULL,
                target_speed_kmh REAL NOT NULL,
                arrival_tolerance_m REAL NOT NULL,
                validation_status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                duration_seconds REAL,
                tick_count INTEGER,
                initial_distance_m REAL,
                final_distance_m REAL,
                distance_travelled_m REAL,
                notes TEXT,
                FOREIGN KEY(calibration_run_id) REFERENCES calibration_runs(calibration_run_id),
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(from_point_id) REFERENCES map_points(point_id),
                FOREIGN KEY(to_point_id) REFERENCES map_points(point_id)
            );
            CREATE INDEX IF NOT EXISTS idx_route_execution_validations_route
                ON route_execution_validations(resource_version, from_point_id, to_point_id, validation_status);

            CREATE TABLE IF NOT EXISTS route_candidates (
                route_candidate_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                from_point_id TEXT NOT NULL,
                to_point_id TEXT NOT NULL,
                candidate_rank INTEGER NOT NULL,
                planner_version TEXT NOT NULL,
                route_hash TEXT NOT NULL,
                route_length_m REAL,
                junction_count INTEGER,
                road_lane_sequence_json TEXT,
                waypoint_artifact_path TEXT,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                UNIQUE(resource_version, from_point_id, to_point_id, candidate_rank, planner_version),
                FOREIGN KEY(map_id) REFERENCES maps(map_id),
                FOREIGN KEY(from_point_id) REFERENCES map_points(point_id),
                FOREIGN KEY(to_point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS task_points (
                point_id TEXT PRIMARY KEY,
                task_types_json TEXT NOT NULL,
                capability_requirements_json TEXT,
                capacity INTEGER,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS safe_wait_points (
                point_id TEXT PRIMARY KEY,
                capacity INTEGER,
                allowed_vehicle_types_json TEXT,
                minimum_risk_level TEXT,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(point_id) REFERENCES map_points(point_id)
            );

            CREATE TABLE IF NOT EXISTS junctions (
                junction_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                junction_type TEXT NOT NULL,
                name TEXT,
                validation_status TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );

            CREATE TABLE IF NOT EXISTS junction_connections (
                junction_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                connection_id TEXT,
                incoming_road TEXT,
                connecting_road TEXT,
                contact_point TEXT,
                from_lane_id INTEGER,
                to_lane_id INTEGER,
                source TEXT NOT NULL,
                PRIMARY KEY(junction_id, resource_version, connection_id, from_lane_id, to_lane_id),
                FOREIGN KEY(junction_id) REFERENCES junctions(junction_id)
            );

            CREATE TABLE IF NOT EXISTS hazard_zones (
                hazard_zone_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                resource_version TEXT NOT NULL,
                hazard_type TEXT NOT NULL,
                geometry_json TEXT,
                geometry_status TEXT NOT NULL,
                anchor_point_ids_json TEXT,
                affected_edge_ids_json TEXT,
                source TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY(map_id) REFERENCES maps(map_id)
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )
        self.connection.commit()

    def initialise_map(
        self,
        map_id: str,
        map_name: str,
        carla_map_name: str,
        vehicle_blueprint: str,
        resource_version: str = "1.0-draft",
        xodr_path: Optional[Path] = None,
        carla_version: Optional[str] = None,
        planner_version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Create map and draft-version metadata without importing map facts."""
        xodr_value = str(Path(xodr_path).expanduser().resolve()) if xodr_path else None
        xodr_hash = file_sha256(xodr_path)
        now = _utc_now()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO maps(
                map_id, map_name, carla_map_name, xodr_file, xodr_hash,
                carla_version, source, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (map_id, map_name, carla_map_name, xodr_value, xodr_hash,
             carla_version, "PHASE1_MANUAL_METADATA", now, notes),
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO map_resource_versions(
                resource_version, map_id, xodr_hash, carla_map_version,
                vehicle_blueprint, planner_version, status, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (resource_version, map_id, xodr_hash, carla_version,
             vehicle_blueprint, planner_version, "DRAFT", now,
             "Phase 1 schema only: no point, route, or safety verification yet."),
        )
        self.connection.commit()

    def import_xodr(self, map_id: str, resource_version: str, xodr_path: Path, spacing_m: float = 10.0) -> Dict[str, object]:
        """Import static OpenDRIVE facts without requiring a CARLA runtime."""
        from .xodr import import_xodr
        return import_xodr(self, map_id, resource_version, xodr_path, spacing_m)

    def has_map_resource_version(self, map_id: str, resource_version: str) -> bool:
        """Return whether the requested pre-created resource version exists."""
        row = self.connection.execute(
            "SELECT 1 FROM map_resource_versions WHERE map_id = ? AND resource_version = ?",
            (map_id, resource_version),
        ).fetchone()
        return row is not None

    def start_calibration_run(
        self,
        calibration_run_id: str,
        map_id: str,
        resource_version: str,
        calibration_type: str,
        tool_version: str,
        source: str,
        notes: Optional[str] = None,
    ) -> None:
        """Persist a pending calibration run; map metadata must exist first."""
        if not self.has_map_resource_version(map_id, resource_version):
            raise ValueError(
                "map resource version does not exist: {}/{}".format(map_id, resource_version)
            )
        self.connection.execute(
            """
            INSERT INTO calibration_runs(
                calibration_run_id, map_id, resource_version, calibration_type,
                tool_version, started_at, status, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (calibration_run_id, map_id, resource_version, calibration_type,
             tool_version, _utc_now(), "RUNNING", source, notes),
        )
        self.connection.commit()

    def finish_calibration_run(
        self, calibration_run_id: str, status: str, summary: Dict[str, object]
    ) -> None:
        """Finish a run with a JSON-safe factual summary."""
        self.connection.execute(
            """
            UPDATE calibration_runs
            SET ended_at = ?, status = ?, summary_json = ?
            WHERE calibration_run_id = ?
            """,
            (_utc_now(), status, json.dumps(summary, ensure_ascii=False, sort_keys=True), calibration_run_id),
        )
        self.connection.commit()

    def upsert_spawn_calibration_points(self, map_id: str, records: Iterable[Dict[str, object]]) -> int:
        """Write CARLA spawn-point calibration facts without inferring route safety.

        Records are keyed by map and CARLA spawn-point index, so a later run
        updates the latest factual result for that point while calibration_runs
        retains the history of each scan.
        """
        rows = []
        for item in records:
            required = ("spawn_point_index", "x", "y", "z", "validation_status")
            missing = [name for name in required if item.get(name) is None]
            if missing:
                raise ValueError("spawn calibration record missing: {}".format(", ".join(missing)))
            index = int(item["spawn_point_index"])
            rows.append((
                "carla-spawn:{}".format(index), map_id, index,
                float(item["x"]), float(item["y"]), float(item["z"]), item.get("yaw"),
                item.get("road_id"), item.get("lane_id"), item.get("s"), item.get("lane_type"),
                item.get("travel_direction"), item.get("heavy_truck_allowed"),
                item.get("nearest_point_distance_m"),
                str(item["validation_status"]), str(item.get("source", "CARLA_SPAWN_CALIBRATION")),
                item.get("notes"),
            ))
        self.connection.executemany(
            """
            INSERT INTO map_points(
                point_id, map_id, carla_spawn_point_index, x, y, z, yaw,
                road_id, lane_id, s, lane_type, travel_direction,
                heavy_truck_allowed, nearest_point_distance_m, validation_status, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(map_id, carla_spawn_point_index) DO UPDATE SET
                x = excluded.x, y = excluded.y, z = excluded.z, yaw = excluded.yaw,
                road_id = excluded.road_id, lane_id = excluded.lane_id, s = excluded.s,
                lane_type = excluded.lane_type,
                heavy_truck_allowed = excluded.heavy_truck_allowed,
                nearest_point_distance_m = excluded.nearest_point_distance_m,
                validation_status = excluded.validation_status, source = excluded.source,
                notes = excluded.notes
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def verified_spawn_points(self, map_id: str) -> Iterable[Dict[str, object]]:
        """Return P3-verified spawn facts, not route or multi-vehicle facts."""
        rows = self.connection.execute(
            """
            SELECT point_id, carla_spawn_point_index, x, y, z, yaw
            FROM map_points
            WHERE map_id = ? AND validation_status = 'VERIFIED_SPAWN'
            ORDER BY point_id
            """,
            (map_id,),
        ).fetchall()
        return [
            {"point_id": row[0], "spawn_point_index": row[1], "x": row[2],
             "y": row[3], "z": row[4], "yaw": row[5]}
            for row in rows
        ]

    def blocked_dual_spawn_pairs(
        self, map_id: str, resource_version: str
    ) -> Iterable[tuple]:
        """Return only observed dual-spawn failures, never static inferences.

        The absence of a pair from this result is not multi-vehicle clearance
        evidence.  It only means that this resource selector has no recorded
        ``DUAL_SPAWN_BLOCKED`` result for that pair.
        """
        rows = self.connection.execute(
            """
            SELECT point_a_id, point_b_id
            FROM point_conflicts
            WHERE map_id = ? AND resource_version = ?
              AND validation_status = 'DUAL_SPAWN_BLOCKED'
            """,
            (map_id, resource_version),
        ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    def static_inferred_conflict_pairs(
        self, map_id: str, resource_version: str
    ) -> Iterable[tuple]:
        """Return conservative P4 proximity candidates for episode selection.

        These are *not* physical collision results.  The scenario generator
        may avoid them while choosing simultaneous initial spawns, which is a
        conservative sampling rule only.  They must never be reported as
        verified fleet-clearance evidence.
        """
        rows = self.connection.execute(
            """
            SELECT point_a_id, point_b_id
            FROM point_conflicts
            WHERE map_id = ? AND resource_version = ?
              AND validation_status = 'STATIC_INFERRED'
            """,
            (map_id, resource_version),
        ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    def physical_route_validations(
        self, map_id: str, resource_version: str
    ) -> Iterable[Dict[str, object]]:
        """Return physical single-truck route facts without promoting them.

        These records are for scenario admission/reporting.  A reached route
        remains an isolated single-truck result, not fleet-safety evidence.
        """
        rows = self.connection.execute(
            """
            SELECT route_id, from_point_id, to_point_id, validation_status,
                   target_speed_kmh, arrival_tolerance_m,
                   duration_seconds, final_distance_m
            FROM route_execution_validations
            WHERE map_id = ? AND resource_version = ?
            ORDER BY started_at, validation_id
            """,
            (map_id, resource_version),
        ).fetchall()
        return [
            {
                "route_id": str(row[0]),
                "from_point_id": str(row[1]),
                "to_point_id": str(row[2]),
                "validation_status": str(row[3]),
                "target_speed_kmh": float(row[4]),
                "arrival_tolerance_m": float(row[5]),
                "duration_seconds": row[6],
                "final_distance_m": row[7],
            }
            for row in rows
        ]

    def upsert_operating_areas(self, records: Iterable[Dict[str, object]]) -> int:
        """Write versioned operating-area semantics for a map resource set.

        Area records describe an intended selection pool.  Their validation
        status must remain explicit: a candidate pool is not promoted to a
        physically drivable multi-vehicle area merely by being recorded here.
        """
        rows = []
        for item in records:
            required = (
                "area_id", "map_id", "resource_version", "display_name",
                "area_type", "selection_mode", "allowed_roles", "point_ids",
                "validation_status", "source",
            )
            missing = [key for key in required if item.get(key) is None]
            if missing:
                raise ValueError("operating area missing: {}".format(", ".join(missing)))
            point_ids = [str(value) for value in item["point_ids"]]
            if not point_ids or len(point_ids) != len(set(point_ids)):
                raise ValueError("operating area requires unique point_ids")
            rows.append((
                str(item["area_id"]), str(item["map_id"]),
                str(item["resource_version"]), str(item["display_name"]),
                str(item["area_type"]), str(item["selection_mode"]),
                item.get("capacity"),
                json.dumps(list(item["allowed_roles"]), ensure_ascii=False, sort_keys=True),
                json.dumps(point_ids, ensure_ascii=False, sort_keys=True),
                str(item["validation_status"]), str(item["source"]), item.get("notes"),
            ))
        self.connection.executemany(
            """
            INSERT INTO operating_areas(
                area_id, map_id, resource_version, display_name, area_type,
                selection_mode, capacity, allowed_roles_json, point_ids_json,
                validation_status, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(area_id) DO UPDATE SET
                map_id = excluded.map_id,
                resource_version = excluded.resource_version,
                display_name = excluded.display_name,
                area_type = excluded.area_type,
                selection_mode = excluded.selection_mode,
                capacity = excluded.capacity,
                allowed_roles_json = excluded.allowed_roles_json,
                point_ids_json = excluded.point_ids_json,
                validation_status = excluded.validation_status,
                source = excluded.source,
                notes = excluded.notes
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def operating_areas(
        self, map_id: str, resource_version: str
    ) -> Iterable[Dict[str, object]]:
        """Return stored area semantics; map facts remain in their own tables."""
        rows = self.connection.execute(
            """
            SELECT area_id, display_name, area_type, selection_mode, capacity,
                   allowed_roles_json, point_ids_json, validation_status, source, notes
            FROM operating_areas
            WHERE map_id = ? AND resource_version = ?
            ORDER BY area_id
            """,
            (map_id, resource_version),
        ).fetchall()
        return [
            {
                "area_id": str(row[0]), "display_name": str(row[1]),
                "area_type": str(row[2]), "selection_mode": str(row[3]),
                "capacity": row[4], "allowed_roles": json.loads(row[5]),
                "point_ids": json.loads(row[6]), "validation_status": str(row[7]),
                "source": str(row[8]), "notes": row[9],
            }
            for row in rows
        ]

    def upsert_point_conflicts(self, records: Iterable[Dict[str, object]]) -> int:
        """Persist static inferences or explicitly-labelled pair-test results."""
        rows = []
        for item in records:
            required = ("map_id", "resource_version", "point_a_id", "point_b_id",
                        "conflict_type", "validation_status", "source")
            missing = [name for name in required if item.get(name) is None]
            if missing:
                raise ValueError("point conflict record missing: {}".format(", ".join(missing)))
            point_a, point_b = sorted((str(item["point_a_id"]), str(item["point_b_id"])))
            if point_a == point_b:
                raise ValueError("point conflict requires two distinct point IDs")
            rows.append((
                str(item["map_id"]), str(item["resource_version"]), point_a, point_b,
                str(item["conflict_type"]), item.get("minimum_clearance_m"),
                str(item["validation_status"]), str(item["source"]), item.get("notes"),
            ))
        self.connection.executemany(
            """
            INSERT INTO point_conflicts(
                map_id, resource_version, point_a_id, point_b_id, conflict_type,
                minimum_clearance_m, validation_status, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_version, point_a_id, point_b_id, conflict_type) DO UPDATE SET
                minimum_clearance_m = excluded.minimum_clearance_m,
                validation_status = excluded.validation_status,
                source = excluded.source,
                notes = excluded.notes
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def upsert_reachable_pairs(self, records: Iterable[Dict[str, object]]) -> int:
        """Persist directed planner reachability facts idempotently.

        These rows describe route-planner output only.  They must not be
        interpreted as evidence that a heavy truck physically traversed the
        route, passed a clearance check, or avoided collisions.
        """
        rows = []
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("reachable pair must be an object")
            required = (
                "map_id", "resource_version", "from_point_id", "to_point_id",
                "planner_version", "reachable", "validation_status", "source",
            )
            missing = [name for name in required if item.get(name) is None]
            if missing:
                raise ValueError("reachable pair record missing: {}".format(", ".join(missing)))
            from_point_id = str(item["from_point_id"])
            to_point_id = str(item["to_point_id"])
            if from_point_id == to_point_id:
                raise ValueError("reachable pair requires two distinct point IDs")
            rows.append((
                str(item["map_id"]), str(item["resource_version"]),
                from_point_id, to_point_id, str(item["planner_version"]),
                1 if bool(item["reachable"]) else 0,
                item.get("route_length_m"), item.get("endpoint_error_m"),
                item.get("junction_count"), item.get("route_hash"),
                str(item["validation_status"]), str(item["source"]), item.get("notes"),
            ))
        self.connection.executemany(
            """
            INSERT INTO reachable_pairs(
                map_id, resource_version, from_point_id, to_point_id,
                planner_version, reachable, route_length_m, endpoint_error_m,
                junction_count, route_hash, validation_status, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_version, from_point_id, to_point_id, planner_version)
            DO UPDATE SET
                map_id = excluded.map_id,
                reachable = excluded.reachable,
                route_length_m = excluded.route_length_m,
                endpoint_error_m = excluded.endpoint_error_m,
                junction_count = excluded.junction_count,
                route_hash = excluded.route_hash,
                validation_status = excluded.validation_status,
                source = excluded.source,
                notes = excluded.notes
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def terminal_reachable_pair_keys(self, map_id: str, resource_version: str,
                                     planner_version: str):
        """Return completed directed-pair keys for resumable P5 scans."""
        rows = self.connection.execute(
            """
            SELECT from_point_id, to_point_id
            FROM reachable_pairs
            WHERE map_id = ? AND resource_version = ? AND planner_version = ?
              AND validation_status IN (
                  'PLANNER_REACHABLE', 'PLANNER_UNREACHABLE',
                  'PLANNER_NEAR_ENDPOINT', 'PLANNER_ENDPOINT_MISMATCH'
              )
            """,
            (map_id, resource_version, planner_version),
        ).fetchall()
        return {(row[0], row[1]) for row in rows}

    def reachable_pair_summary(self, map_id: str, resource_version: str,
                               planner_version: str) -> Dict[str, int]:
        """Return factual P5 status counts for reporting and preflight use."""
        rows = self.connection.execute(
            """
            SELECT validation_status, count(*)
            FROM reachable_pairs
            WHERE map_id = ? AND resource_version = ? AND planner_version = ?
            GROUP BY validation_status
            """,
            (map_id, resource_version, planner_version),
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def planner_reachable_pairs(self, map_id: str, resource_version: str):
        """Return only strict P5 planner candidates for offline selection.

        This deliberately excludes ``NEAR_ENDPOINT`` records.  Returned rows
        are navigation-planner facts only, never physical-route approval.
        """
        rows = self.connection.execute(
            """
            SELECT from_point_id, to_point_id, route_length_m,
                   endpoint_error_m, junction_count, planner_version
            FROM reachable_pairs
            WHERE map_id = ? AND resource_version = ?
              AND validation_status = 'PLANNER_REACHABLE' AND reachable = 1
            ORDER BY from_point_id, to_point_id, planner_version
            """,
            (map_id, resource_version),
        ).fetchall()
        return [{
            "from_point_id": str(row[0]), "to_point_id": str(row[1]),
            "route_length_m": row[2], "endpoint_error_m": row[3],
            "junction_count": row[4], "planner_version": str(row[5]),
        } for row in rows]

    def planner_route_facts(self, map_id: str, resource_version: str):
        """Read all P5 route facts without changing their validation meaning."""
        rows = self.connection.execute(
            """
            SELECT from_point_id, to_point_id, route_length_m,
                   endpoint_error_m, junction_count, planner_version,
                   validation_status, reachable
            FROM reachable_pairs
            WHERE map_id = ? AND resource_version = ?
            ORDER BY from_point_id, to_point_id, planner_version
            """,
            (map_id, resource_version),
        ).fetchall()
        return [{
            "from_point_id": str(row[0]), "to_point_id": str(row[1]),
            "route_length_m": row[2], "endpoint_error_m": row[3],
            "junction_count": row[4], "planner_version": str(row[5]),
            "validation_status": str(row[6]), "reachable": bool(row[7]),
        } for row in rows]

    def add_route_execution_validation(self, record: Dict[str, object]) -> None:
        """Append one physical traversal result without replacing P5 facts."""
        required = (
            "validation_id", "calibration_run_id", "map_id", "resource_version",
            "route_profile_id", "route_id", "from_point_id", "to_point_id",
            "vehicle_blueprint", "target_speed_kmh", "arrival_tolerance_m",
            "validation_status", "started_at", "ended_at",
        )
        missing = [key for key in required if record.get(key) is None]
        if missing:
            raise ValueError("route execution validation missing: {}".format(", ".join(missing)))
        self.connection.execute(
            """
            INSERT INTO route_execution_validations(
                validation_id, calibration_run_id, map_id, resource_version,
                route_profile_id, route_id, from_point_id, to_point_id,
                vehicle_blueprint, target_speed_kmh, arrival_tolerance_m,
                validation_status, started_at, ended_at, duration_seconds,
                tick_count, initial_distance_m, final_distance_m,
                distance_travelled_m, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record["validation_id"]), str(record["calibration_run_id"]),
                str(record["map_id"]), str(record["resource_version"]),
                str(record["route_profile_id"]), str(record["route_id"]),
                str(record["from_point_id"]), str(record["to_point_id"]),
                str(record["vehicle_blueprint"]), float(record["target_speed_kmh"]),
                float(record["arrival_tolerance_m"]), str(record["validation_status"]),
                str(record["started_at"]), str(record["ended_at"]),
                record.get("duration_seconds"), record.get("tick_count"),
                record.get("initial_distance_m"), record.get("final_distance_m"),
                record.get("distance_travelled_m"), record.get("notes"),
            ),
        )
        self.connection.commit()


    def upsert_route_candidates(self, records: Iterable[Dict[str, object]]) -> int:
        """Idempotently write static route candidates into the map library.

        This stores planner output only. It does not mark a route as CARLA-
        verified or safe for heavy trucks.
        """
        rows = []
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("route candidate must be an object")
            required = ("route_candidate_id", "map_id", "resource_version", "from_point_id", "to_point_id")
            missing = [name for name in required if not item.get(name)]
            if missing:
                raise ValueError("route candidate missing: {}".format(", ".join(missing)))
            sequence = item.get("road_lane_sequence_json", item.get("road_lane_sequence", []))
            if not isinstance(sequence, str):
                import json
                sequence = json.dumps(sequence, ensure_ascii=False, sort_keys=True)
            rows.append((
                str(item["route_candidate_id"]), str(item["map_id"]), str(item["resource_version"]),
                str(item["from_point_id"]), str(item["to_point_id"]), int(item.get("candidate_rank", 1)),
                str(item.get("planner_version", "unknown")), str(item.get("route_hash", item["route_candidate_id"])),
                item.get("route_length_m"), item.get("junction_count"), sequence,
                item.get("waypoint_artifact_path"), str(item.get("validation_status", "CANDIDATE")),
                str(item.get("source", "ROUTE_IMPORT")), item.get("notes"),
            ))
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO route_candidates(
                route_candidate_id, map_id, resource_version, from_point_id, to_point_id,
                candidate_rank, planner_version, route_hash, route_length_m, junction_count,
                road_lane_sequence_json, waypoint_artifact_path, validation_status, source, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows
        )
        self.connection.commit()
        return len(rows)

    def replace_route_candidates(self, records: Iterable[Dict[str, object]],
                                 map_id: str, resource_version: str,
                                 source: str) -> int:
        """Replace one derivation source without touching captured/manual routes."""
        items = list(records)
        for item in items:
            if (str(item.get("map_id")) != str(map_id)
                    or str(item.get("resource_version")) != str(resource_version)
                    or str(item.get("source")) != str(source)):
                raise ValueError("route candidate does not match replacement scope")
        self.upsert_route_candidates(items)
        identifiers = {str(item["route_candidate_id"]) for item in items}
        existing = {
            str(row[0]) for row in self.connection.execute(
                "SELECT route_candidate_id FROM route_candidates WHERE map_id=? "
                "AND resource_version=? AND source=?",
                (str(map_id), str(resource_version), str(source)),
            )
        }
        self.connection.executemany(
            "DELETE FROM route_candidates WHERE route_candidate_id=?",
            [(route_id,) for route_id in sorted(existing - identifiers)],
        )
        self.connection.commit()
        return len(items)
    def table_names(self) -> Iterable[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]

    def validate_schema(self) -> Dict[str, object]:
        required = {
            "schema_migrations", "maps", "map_resource_versions",
            "calibration_runs", "map_points", "point_roles", "operating_areas",
            "point_conflicts", "road_nodes", "road_edges", "road_clusters",
            "reachable_pairs", "route_execution_validations", "route_candidates", "task_points",
            "safe_wait_points", "hazard_zones", "junctions", "junction_connections",
        }
        present = set(self.table_names())
        foreign_keys_enabled = self.connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0] == 1
        return {
            "schema_version": SCHEMA_VERSION,
            "database_path": str(self.database_path),
            "required_tables": len(required),
            "missing_tables": sorted(required - present),
            "foreign_keys_enabled": foreign_keys_enabled,
            "valid": not (required - present) and foreign_keys_enabled,
        }

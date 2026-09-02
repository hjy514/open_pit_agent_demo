"""SQLite storage skeleton for the Mine Spatial Resource Library V1.

The database is deliberately separate from ``openpit.db``.  ``openpit.db``
records what happened in one run; this module will record durable facts and
calibration results about a CARLA map.  Phase 1 creates only the schema and
the map/version metadata.  It does not infer that any point is safe.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional


SCHEMA_VERSION = "2"


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
    def table_names(self) -> Iterable[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]

    def validate_schema(self) -> Dict[str, object]:
        required = {
            "schema_migrations", "maps", "map_resource_versions",
            "calibration_runs", "map_points", "point_roles",
            "point_conflicts", "road_nodes", "road_edges", "road_clusters",
            "reachable_pairs", "route_candidates", "task_points",
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

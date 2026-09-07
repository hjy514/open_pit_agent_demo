#!/usr/bin/env python3
"""Read-only shortlist of P5 route candidates for a fixed Spawn Point."""

import argparse
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "database" / "map_resources.db")
    parser.add_argument("--map-id", default="0325_5")
    parser.add_argument("--resource-version", default="1.0-draft")
    parser.add_argument("--from-spawn-point-index", type=int, required=True)
    parser.add_argument("--max-route-length-m", type=float, default=1000.0)
    parser.add_argument("--max-endpoint-error-m", type=float, default=15.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--exclude-target-spawn-point-index", type=int, action="append", default=[],
        help="不显示已被场景占用或已知不可用的目标点；可重复使用。",
    )
    args = parser.parse_args()
    connection = sqlite3.connect(str(args.database))
    rows = connection.execute(
        """
        SELECT target.carla_spawn_point_index, pair.validation_status,
               pair.route_length_m, pair.endpoint_error_m, pair.junction_count,
               CASE WHEN EXISTS (
                   SELECT 1 FROM point_conflicts conflict
                   WHERE conflict.map_id = pair.map_id
                     AND conflict.resource_version = pair.resource_version
                     AND conflict.validation_status = 'DUAL_SPAWN_BLOCKED'
                     AND (conflict.point_a_id = origin.point_id OR conflict.point_b_id = origin.point_id)
                     AND (conflict.point_a_id = target.point_id OR conflict.point_b_id = target.point_id)
               ) THEN 1 ELSE 0 END AS p4_blocked
        FROM reachable_pairs pair
        JOIN map_points origin ON origin.point_id = pair.from_point_id
        JOIN map_points target ON target.point_id = pair.to_point_id
        WHERE pair.map_id = ? AND pair.resource_version = ?
          AND origin.carla_spawn_point_index = ?
          AND pair.validation_status IN ('PLANNER_REACHABLE', 'PLANNER_NEAR_ENDPOINT')
          AND pair.route_length_m <= ? AND pair.endpoint_error_m <= ?
        ORDER BY pair.route_length_m ASC, target.carla_spawn_point_index ASC
        """,
        (args.map_id, args.resource_version, args.from_spawn_point_index,
         args.max_route_length_m, args.max_endpoint_error_m),
    ).fetchall()
    excluded = {int(value) for value in args.exclude_target_spawn_point_index}
    rows = [row for row in rows if not row[5] and row[0] not in excluded][:args.limit]
    print("起点{}的P6候选（P5筛选，不等同于实跑验证）：{} 条".format(
        args.from_spawn_point_index, len(rows)))
    if excluded:
        print("已排除目标点：{}".format(sorted(excluded)))
    for target, status, length, endpoint, junctions, _ in rows:
        print("target={} | {} | length={:.2f}m | endpoint_error={:.2f}m | junctions={}".format(
            target, status, length, endpoint, junctions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

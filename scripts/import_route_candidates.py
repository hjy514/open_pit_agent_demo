#!/usr/bin/env python3
"""Import static route candidates into Map Resource Library V1.

The input is a JSON list or an object with a ``route_candidates`` list. This
tool records planner output only; it does not perform CARLA traversal or
heavy-truck safety validation.
"""

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.map_resources import MapResourceStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="候选路线 JSON 文件")
    parser.add_argument(
        "--database", type=Path,
        default=PROJECT_ROOT / "data" / "database" / "map_resources.db",
    )
    parser.add_argument("--map-id", help="覆盖输入记录中的 map_id")
    parser.add_argument("--resource-version", help="覆盖输入记录中的 resource_version")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    records = payload.get("route_candidates", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise SystemExit("输入必须是 JSON 列表或包含 route_candidates 列表的对象")
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("每条候选路线必须是 JSON 对象")
        item = dict(record)
        if args.map_id:
            item["map_id"] = args.map_id
        if args.resource_version:
            item["resource_version"] = args.resource_version
        normalized.append(item)

    with MapResourceStore(args.database) as store:
        count = store.upsert_route_candidates(normalized)
        schema = store.validate_schema()
    print("路线候选写入：{} 条".format(count))
    print("数据库：{}".format(schema["database_path"]))
    print("状态：静态候选已记录；未进行 CARLA 或重型矿卡安全验证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

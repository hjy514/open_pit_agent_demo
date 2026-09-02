# 矿区空间资源库 V1：Phase 2（XODR 静态导入）

Phase 2 在 Phase 1 独立数据库骨架上增加无 CARLA 依赖的 OpenDRIVE 导入。建库脚本传入 `--xodr-path` 后，会解析道路 `planView` 几何、车道元数据和 `junction`/`laneLink` 连接，并写入 `SOURCE_XODR` 来源记录。

```bash
python scripts/build_map_resource_db.py --xodr-path /path/to/0325_5.xodr \
  --resource-version 2.0-xodr --spacing-m 10
```

导入结果包括：

- `road_nodes` / `road_edges`：按采样间距生成的静态参考几何；
- `map_points`：几何点（不是 CARLA spawn point）；
- `road_clusters`：道路级聚类；
- `junctions` / `junction_connections`：路口和车道连接关系。

所有记录均为 `validation_status=UNVERIFIED`，不会推断重型矿卡可通行、净空、风险或安全等待点。重复导入同一资源版本会先删除该版本的 `SOURCE_XODR` 记录再重建，因此可安全重跑。后续 Ubuntu + CARLA 0.9.10 环境再执行独立标定，把验证结果写入同一资源版本或新的版本。

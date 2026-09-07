# 矿区空间资源库 V1：Phase 3

Phase 3 对已启动的 CARLA 0.9.10 / `0325_5` 逐一读取 Spawn Point，并尝试生成一辆临时 `vehicle.cat.cat`。结果仅写入 `data/database/map_resources.db`，不会写入 `openpit.db`，也不会修改三车边坡 Demo、调度、风险、控制或 PyQt 界面。

运行前必须先用 Phase 1 工具创建目标地图和资源版本元数据，并确认 CARLA 服务已启动且地图已加载。执行命令：

```bash
$OPENPIT_PYTHON scripts/calibrate_map_spawn_points.py --expected-map-name 0325_5 --expected-carla-version 0.9.10
```

每个 Spawn Point 都会尝试生成一辆临时矿卡，并记录坐标、yaw、road/lane/s、基础车道类型和本轮的结果。临时 Actor 无论成功、失败或异常都会尝试在 `finally` 中销毁。脚本在写入前会检查 CARLA 服务版本及地图名；每次全图扫描会在 `calibration_runs` 留下独立运行记录；`map_points` 保存该点最新的标定事实。

基础高程检查会记录 Spawn 高程与其 CARLA waypoint 高程的差值。默认超过 2 米会写入 `ELEVATION_REFERENCE_WARNING`，但不会单独排除一个已成功生成且位于 Driving Lane 的矿卡点；自定义地图可能存在系统性的 OpenDRIVE/Spawn 高程参考差。该值保存在 `nearest_point_distance_m` 字段中，供后续真实行驶验证复核。它不是车辆净空验证，也不能据此证明点位不存在悬空或埋入问题。

状态边界：

- `VERIFIED_SPAWN`：单辆 `vehicle.cat.cat` 成功生成，且 Spawn 对应 CARLA waypoint 为 Driving Lane。
- `EXCLUDED`：本轮无法投影到道路、不在 Driving Lane、生成失败或 Actor 销毁失败。
- `UNKNOWN` / `CANDIDATE`：尚未由本工具验证的状态。

Phase 3 不验证路线可达、实际通行、碰撞、卡死、净空、转弯半径、双车冲突或道路封闭绕行。因此 `VERIFIED_SPAWN` 不能直接作为真实多车运行许可；这些结论分别留给 P4/P5 及后续实车执行验证。

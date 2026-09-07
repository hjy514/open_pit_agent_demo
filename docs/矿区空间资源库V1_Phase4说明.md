# 矿区空间资源库 V1：Phase 4

P4 的第一步仅在 P3 `VERIFIED_SPAWN` 点之间计算三维中心距离，并把小于显式提供的保守策略阈值的点对写入 `point_conflicts`。结果状态为 `STATIC_INFERRED`，它表示“需要进一步核验的候选冲突”，不是 CARLA 碰撞结论，也不是 `vehicle.cat.cat` 实测外廓尺寸。

执行前应先保留 P3 的真实标定结果；命令中的阈值必须由项目方按保守运营策略决定，例如：

```bash
$OPENPIT_PYTHON scripts/infer_static_point_conflicts.py --minimum-center-distance-m <已确认的保守阈值>
```

该命令只写 `map_resources.db` 的 `point_conflicts`，不写 `openpit.db`，不启动 CARLA，也不修改三车边坡 Golden Demo。下一步必须从这些候选对中选择关键点对，在 CARLA 中做双 `vehicle.cat.cat` 同时 Spawn 与 Actor 清理验证；即使双 Spawn 成功，路线通行、碰撞、净空和多车容量仍需后续阶段验证。

双车工具默认按中心距离从小到大选择候选点对，例如先验证最紧密的 10 对：

```bash
$OPENPIT_PYTHON scripts/validate_dual_spawn_pairs.py --pair-limit 10
```

其结果作为独立 `calibration_runs` 记录，并以 `DUAL_HEAVY_TRUCK_SPAWN_CHECK` 写入 `point_conflicts`。`DUAL_SPAWN_VERIFIED` 仅表示两辆临时矿卡曾同时生成且已销毁；`DUAL_SPAWN_BLOCKED` 表示其中至少一辆无法生成或清理失败。

验证工具直接使用当前 CARLA 地图按 Spawn Point 索引返回的原始 Transform，不以数据库坐标重新构造位置。已经存在 `DUAL_HEAVY_TRUCK_SPAWN_CHECK` 结果的点对会从后续候选中排除，因此重复执行可逐批覆盖剩余候选，不会永远重复验证距离最近的同一批点对。CARLA RPC、世界或 Actor 清理异常会把本轮 `calibration_runs` 标为 `FAILED`，不会被误写成点位的 `DUAL_SPAWN_BLOCKED` 结论。

预检和核心校验都会拒绝包含既有 `vehicle.*` Actor 的 CARLA 世界，防止遗留 Demo 车辆占用点位并造成假冲突。工具只安全停止，不会替用户删除任何已有车辆；运行 P4 前应重新启动一个干净的 CARLA 世界并加载 `0325_5`。

对于部分 CARLA 0.9.10 异步 world 中 `actor.destroy()` 返回 `False`、但 Actor 延迟消失的兼容情况，工具会在最多 2 秒内轮询当前 world：仅当 Actor 已确认不存在时才接受清理，并在标定摘要中累计 `cleanup_compatibility_warnings`；超时后仍存在或无法确认时，本轮仍会失败，不能写入点对结论。

当前 `0.9.10-dirty` 构建实测发现，`world.get_actor(id)` 可能在 Actor 已停止存活且已从实时 `vehicle.*` 列表移除后继续返回陈旧代理。因此清理确认优先使用当前 world Actor 列表；只有测试替身或不提供 Actor 列表的运行时才回退到单 ID 查询。

`DUAL_SPAWN_VERIFIED` 表示该点对在本次测试条件下“可同时生成”，语义上是兼容结果而不是冲突；后续空间生成器必须按 `conflict_type` 和 `validation_status` 联合解释，不能因为记录位于 `point_conflicts` 表就直接排除。

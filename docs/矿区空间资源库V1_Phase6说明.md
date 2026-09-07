# 矿区空间资源库 V1：Phase 6 单矿卡物理路线验证

Phase 6 是对 Phase 5 规划器结果的补充。P5只证明CARLA规划器能否生成路线；P6使用一辆默认参数的`vehicle.cat.cat`实际执行`BasicAgent`导航，并记录该次行驶尝试。

## 验证范围

当前配置只包含边坡Demo的五条业务路线：三辆矿卡原任务和两条候选接管任务。每条路线按以下边界执行：

```text
单条路线
→ 单辆 vehicle.cat.cat
→ 实际 BasicAgent 导航
→ 记录到达/超时/卡死/终点偏差
→ 销毁 Actor
```

不会同时生成第二辆车，不验证碰撞、会车、交通拥堵、净空或多车安全。

## 启动条件

1. CARLA已启动，且当前地图为`0325_5`。
2. 不要同时运行正式边坡Demo，避免占用相同出生点。
3. P3和P5数据库记录保留即可；P6不会覆盖它们。

先确认地图：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./check_environment.sh --require-target-map
```

## 运行

运行全部五条关键路线：

```bash
./map_resources.sh p6-slope
```

默认速度为15 km/h、到达容差12米、每条最长360秒。若仅需要复核一条路线：

```bash
./map_resources.sh p6-slope --route-id vehicle03_original_patrol
```

## 结果解释

结果写入独立表`route_execution_validations`，与P5的`reachable_pairs`并存：

- `PHYSICAL_REACHED`：该次单车尝试到达容差内；可作为后续多车候选路线的候选，但仍未完成多车安全验证。
- `PHYSICAL_TIMEOUT`：在最大时长内未完成；暂不进入候选池。
- `PHYSICAL_STUCK`：连续25秒进展不足3米；暂不进入候选池。
- `PHYSICAL_ENDPOINT_MISMATCH`：BasicAgent结束但距请求终点仍超出容差；暂不进入候选池。
- `PHYSICAL_SPAWN_FAILED` / `PHYSICAL_EXECUTION_ERROR`：基础设施或运行异常，需要复核后再试。

Phase 6完成后，只有同时具备P3出生点验证、P5规划器可达性和P6物理到达记录的路线，才可以进入首个六车CARLA基准场景的候选任务池。

## P6.1：替换不可用任务路线

当前原任务`28 → 63`已记录为`PHYSICAL_STUCK`，不能进入多车任务池。首批人工候选均出现不合理绕行，因此使用起点28定向扫描得到47、10两个待P6复核的候选，而不是继续根据直线距离猜测终点。

定向扫描会对28点到未占用P3验证点的路线执行P5，并自动输出终点误差不超过15米、规划长度不超过1000米、且无P4双车阻塞记录的候选：

```bash
./map_resources.sh p5-vehicle02-scan
```

旧的四条人工候选保留作对照，可按需执行：

```bash
./map_resources.sh p5-vehicle02-candidates
```

只对输出为`PLANNER_REACHABLE`或`PLANNER_NEAR_ENDPOINT`的候选执行物理验证：

```bash
./map_resources.sh p6-vehicle02 --route-id <通过P5的route_id>
```

P6.1已完成验证后，正式边坡Demo已将矿卡02调整为H1驻守待命角色；其接管矿卡01任务的`28 → 48`路线已通过P6物理验证。原`28 → 63`初始巡检路线及两个替代候选在P6中均判定为卡滞，不再作为正式演示的初始行驶任务。

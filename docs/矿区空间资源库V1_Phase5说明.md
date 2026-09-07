# 矿区空间资源库 V1：Phase 5 路线规划可达性

Phase 5 基于 Phase 3 已完成的 `VERIFIED_SPAWN` 点，使用 CARLA 0.9.10
`GlobalRoutePlannerDAO + GlobalRoutePlanner` 对两两有向点对进行路线规划。
结果写入独立的 `data/database/map_resources.db`，不写入运行业务库
`openpit.db`。

## 验证边界

Phase 5 只证明 CARLA 规划器是否可以从起点生成到终点的路线。
它不代表：

- 矿卡已真实跑完路线；
- 路线满足重型矿卡净空和转弯要求；
- 多车同时行驶不会堵塞或碰撞；
- 路线已经完成安全性验收。

`reachable_pairs.validation_status` 使用：

- `PLANNER_REACHABLE`：路线非空且终点误差在5米内；
- `PLANNER_UNREACHABLE`：规划器返回空路线；
- `PLANNER_NEAR_ENDPOINT`：终点偏差在5～15米，只作为待单车真实行驶复核样本，不可进入自主派单或多车编组。
- `PLANNER_ENDPOINT_MISMATCH`：生成了路线，但路线终点偏差超过15米；
- `PLANNER_ERROR`：规划调用异常，不当作真实“不可达”结论。

## 运行前提

1. 启动 CARLA 0.9.10，并加载 `0325_5`。
2. `map_resources.db` 内已有 Phase 3 生成的 `VERIFIED_SPAWN`。
3. 使用 `OPENPIT_PYTHON` 指向项目的 Python 3.7 环境。

当前自定义矿山图应使用CARLA默认启动参数，避免附加低画质或渲染后端参数导致地图资源显示异常：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_carla.sh
```

## 分批标定

### 当前边坡 Demo 的关键路线（优先执行）

正式 Demo 不需要等待全部6642条有向点对完成。当前三辆矿卡的原任务、两条候选接管路线和边坡风险区参考路线已整理在：

```text
configs/map_resource_slope_demo_critical_routes.json
```

CARLA已加载`0325_5`后，直接执行：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./map_resources.sh p5-slope
```

该命令仅验证9条关键有向路线，写入`map_resources.db`，不生成矿卡、不驾驶车辆、不修改边坡Demo配置。每条结果仍只属于规划器可达性，不可把`PLANNER_NEAR_ENDPOINT`或`PLANNER_ENDPOINT_MISMATCH`作为自主派单安全路线。

### 全图分批标定

默认每次处理250个有向点对：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
$OPENPIT_PYTHON scripts/calibrate_map_reachability.py \
  --expected-map-name 0325_5 \
  --expected-carla-version 0.9.10 \
  --pair-limit 250
```

默认采用严格可用阈值5米和待复核上限15米。两个值均可通过
`--endpoint-tolerance-m` 和 `--near-endpoint-tolerance-m` 显式调整，但不应为了提高可达率而将待复核路线直接当作安全路线。

重复执行同一命令会自动跳过已终结的点对，继续未完成部分。
`PLANNER_ERROR` 不是终结状态，下次执行会再次尝试。如连续出现3次规划异常，
程序会中止本批，防止把CARLA/RPC故障大量记录为路网不可达。

确认CARLA长时间稳定后，可以一次处理所有剩余点对：

```bash
$OPENPIT_PYTHON scripts/calibrate_map_reachability.py --all
```

`--force` 会重新计算已终结结果，仅在更换阈值或确认需要重建时使用。

## 结果查询

```bash
$OPENPIT_PYTHON -c "import sqlite3; c=sqlite3.connect('data/database/map_resources.db'); print(c.execute(\"select validation_status,count(*) from reachable_pairs where map_id='0325_5' and resource_version='1.0-draft' group by validation_status\").fetchall())"
```

查看最新Phase 5标定批次：

```bash
$OPENPIT_PYTHON -c "import sqlite3; c=sqlite3.connect('data/database/map_resources.db'); print(c.execute(\"select calibration_run_id,status,summary_json from calibration_runs where calibration_type='CARLA_PLANNER_REACHABILITY' order by started_at desc limit 1\").fetchone())"
```

## 后续阶段

Phase 5 完成后，才能结合 Phase 4 点位冲突数据生成有约束的6～8车编组。
规划可达的候选路线仍需后续进行单矿卡真实行驶、净空、卡死、碰撞和
多车通行验证。

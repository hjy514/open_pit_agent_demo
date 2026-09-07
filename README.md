# OpenPit-Agent 露天矿智能调度与风险闭环平台

OpenPit-Agent 是一个面向露天矿多车辆协同调度、风险监测和闭环验证的工程项目。系统以 CARLA 0.9.10 的 `0325_5` 矿山地图为高保真验证环境，以 Python Agent 后端提供场景、风险、调度、路线与数据闭环能力，并通过 PyQt6 调度中心完成人机协同展示。

当前稳定比赛资产是 S08 三车边坡渐进失稳 Golden Demo；S01–S07、S09 已具备统一结构化场景入口，并正在逐步接入受地图资源约束的 CARLA 多车执行。

## 正式入口

| 命令 | 用途 |
|---|---|
| `./start_carla.sh` | 启动 CARLA；默认不附加低画质或离屏参数 |
| `./start_api.sh` | 启动 Agent FastAPI 服务 |
| `./start_dispatch_app.sh` | 启动 PyQt6 调度中心 |
| `./start_slope_demo.sh` | 启动 S08 正式边坡失稳 Golden Demo |
| `./run_scenario.sh` | 统一运行 S01–S09 场景 |
| `./map_resources.sh` | 建设、查询与验证矿区空间资源库 |
| `./check_environment.sh` | 只读环境检查 |

历史的 `start_demo.sh`、`start_desktop.sh` 与 `start_mine_demo.sh` 不再是正式比赛流程入口。

## 最短启动流程

在四个终端中依次执行：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_carla.sh
```

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_api.sh
```

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_dispatch_app.sh
```

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_slope_demo.sh
```

启动前可检查环境：

```bash
./check_environment.sh --require-target-map --check-api --check-ui
```

## 多场景运行

查看当前可用场景：

```bash
./run_scenario.sh --list-scenarios
```

结构化闭环示例：

```bash
./run_scenario.sh --scenario s02 --mode structural \
  --vehicle-count 6 --seed 202601 --policy auto
```

CARLA 多车执行必须先通过地图资源准入；只有 P6 `PHYSICAL_REACHED` 的路线才能作为物理运行候选：

```bash
./run_scenario.sh --scenario s02 --mode carla \
  --vehicle-count 6 --seed 202601 --policy auto --check-only
```

若输出 `NO_TAKEOVER_CANDIDATE`、`INSUFFICIENT_PHYSICAL_ROUTES` 或其他准入失败，系统会停止而不是降低车辆数或放宽安全约束。

## 数据与真实性边界

- `data/database/map_resources.db`：静态地图事实，包括出生点、拓扑、P5 规划可达性、P6 物理路线验证与作业区。
- `data/database/openpit.db`：运行事实，包括 Episode、任务、事件、决策、工单、闭环与指标。
- `artifacts/`：本地运行证据、日志、批次和摄像头产物；均不提交 Git。
- P5 `PLANNER_REACHABLE` 只表示规划器可生成路线；P6 `PHYSICAL_REACHED` 才表示 `vehicle.cat.cat` 单车实际到达目标附近。
- 当前边坡风险数据是参数化合成数据，不代表真实矿山灾害观测；没有可信来源的能耗、产量等指标保持 `NOT_AVAILABLE` 或 `SURROGATE_ONLY`。

## 工程文档

- [项目状态](PROJECT_STATUS.md)
- [技术交接与运行说明](项目技术交接与运行说明.md)
- [系统总体架构](docs/系统总体架构.md)
- [场景系统设计](docs/场景系统设计.md)
- [矿区空间资源库](docs/矿区空间资源库.md)
- [调度算法与 Cost Model](docs/调度算法与CostModel.md)
- [项目系统讲解与答辩口径](docs/项目系统讲解与答辩口径.md)

## 基础验证

```bash
/home/xiaoa/miniconda3/envs/openpit-agent/bin/python -m compileall -q src scripts open_pit_dispatch_app
/home/xiaoa/miniconda3/envs/openpit-agent/bin/python -m unittest discover -s tests -q
bash -n check_environment.sh map_resources.sh run_scenario.sh start_api.sh start_carla.sh start_dispatch_app.sh start_slope_demo.sh
```

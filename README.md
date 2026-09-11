# OpenPit-Agent 露天矿智能调度与风险闭环平台

OpenPit-Agent 面向露天矿多车辆协同调度、异常处置和数据闭环。系统以 CARLA 0.9.10 的 `0325_5` 自定义矿山地图作为物理执行环境，由 Python Agent 负责场景、监测、风险、调度、路线、反馈和数据存储，通过 PyQt6 调度中心完成人机协同展示。

项目保留 Rule-based Baseline 和 S08 边坡 Golden Demo，并在同一工程中提供统一多场景、多车 CARLA 执行、地图资源库、多目标代价、闭环数据集和候选策略离线评估能力。

## 比赛版场景

| 场景 | 主要流程 | 定位 |
|---|---|---|
| S01 正常作业 | 随机任务→多车调度→CARLA执行→任务反馈 | 核心多车场景 |
| S02 车辆故障 | 故障→任务释放→候选评分→人工确认→换车接管 | 核心调度场景 |
| S04 爆破管控 | 爆破窗口→暂停或绕行→管控解除→原任务恢复 | 核心安全场景 |
| S09 复合扰动 | 道路封闭→路线处置→车辆故障→安全接管→恢复 | 核心综合场景 |
| S08 边坡风险 | 固定/移动监测→风险升级→人工确认→接管→原任务恢复 | Golden Demo |

S03、S05、S06、S07继续保留为统一场景框架的扩展能力，但不显示在比赛版场景选择器中。代表性 Seed 的成功结果属于回归基线，不表示所有随机工况均已经完成CARLA物理验证。

## 系统架构

```text
PyQt6 调度中心
        ↕ FastAPI / RuntimeState
ScenarioSpec → Seed随机化 → ConcreteEpisode
        ↓
监测/风险 → WorldState → Hard Constraints
        ↓
Heuristic Baseline / MultiObjective Cost → Safety Shield
        ↓
Map Resources / Route Planner → CARLA Adapter
        ↓
任务反馈 → openpit.db → 数据集/候选策略离线评估
```

核心目录：

- `src/open_pit_agent/`：Agent、风险、调度、CARLA适配和数据闭环。
- `src/open_pit_agent/scenario/`：统一场景模型、随机生成、事件与CARLA执行桥。
- `src/open_pit_agent/decision/`：多目标代价、优化调度和离线BC候选策略。
- `src/open_pit_agent/map_resources/`：点位、拓扑、可达性和P6物理路线证据。
- `open_pit_dispatch_app/`：PyQt6调度中心，不重复实现Agent业务逻辑。
- `configs/`：场景、监测、风险、代价和地图资源配置。
- `scripts/`：统一场景、数据治理和地图资源工具。
- `tests/`：离线回归测试，不替代CARLA物理验收。

## 最短启动流程

在三个终端中依次启动CARLA、Agent API和调度中心：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_carla.sh
```

`start_carla.sh` 最终只执行 `./CarlaUE4.sh`，不附加离屏、低画质或无渲染参数。CARLA启动后应加载地图 `0325_5`。

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_api.sh
```

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_dispatch_app.sh
```

随后在调度中心选择场景、车辆数、Seed和策略并启动。事件需要人工决策时，主界面只发出提醒；进入决策中心查看候选车辆、硬约束、综合代价或处置方案后再批准或驳回。

启动前可执行只读检查：

```bash
./check_environment.sh --require-target-map --check-api --check-ui
```

## 命令行场景入口

```bash
./run_scenario.sh --list-scenarios
```

无CARLA结构化运行：

```bash
./run_scenario.sh --scenario s02 --mode structural \
  --vehicle-count 6 --seed 202601 --policy auto
```

全部场景CARLA资源准入检查，不生成车辆：

```bash
./run_scenario.sh --scenario all --mode carla --check-only \
  --vehicle-count 6 --seed 202601 --policy auto
```

核心CARLA场景：

```bash
./run_scenario.sh --scenario s02 --mode carla --random-map \
  --vehicle-count 6 --seed 202601 --policy auto
```

S08边坡Golden Demo：

```bash
./run_scenario.sh --scenario s08 --mode carla
```

CARLA多车执行只使用 `map_resources.db` 中已经取得P6 `PHYSICAL_REACHED` 证据的路线。地图、蓝图、路线或场景硬约束不满足时，系统会在生成车辆前拒绝运行，不伪造可达结果。

## 调度与安全

```text
Hard Constraints → Feasible Candidate Set
→ Heuristic Baseline / Normalized Multi-Objective Cost
→ Scheduler → Safety Shield → Route Planner → CARLA
```

- 故障、不可用、能力不匹配、道路关闭、不可达和风险禁入均为硬约束。
- Heuristic Baseline V0继续作为比赛回归基线和Teacher。
- MultiObjective Cost V1记录时间、等待、延误、运输、负载、恢复与切换等可用代价。
- 缺乏可信数据的产量和能耗保持 `NOT_AVAILABLE` 或 `SURROGATE_ONLY`。
- S02和S09故障决策显示候选车辆及代价；S04显示暂停、等待或绕行方案，不伪造无需换车的评分。
- 学习策略必须经过离线评估和Policy Promotion；不能在线改写Safety Shield。

## 数据库与闭环

- `data/database/map_resources.db`：静态地图事实，包括出生点、拓扑、P5、P6和作业区。
- `data/database/openpit.db`：运行事实，包括Run、Episode、任务、事件、决策、候选评分、工单和反馈。
- `artifacts/`：本地运行结果、日志、摄像头帧和比赛证据。
- `data/datasets/`：从有效运行导出的状态、动作、回报、下一状态和终止状态。
- `data/models/`：离线训练和评估的Shadow候选策略，不自动替换正式策略。

```bash
./run_scenario.sh --database-health
./run_scenario.sh --learning-status
```

## 地图资源

`map_resources.sh` 是唯一顶层地图资源入口：

```bash
./map_resources.sh --help
./map_resources.sh report-coverage
```

P5 `PLANNER_REACHABLE` 只表示规划器能生成路线。P6 `PHYSICAL_REACHED` 只表示 `vehicle.cat.cat` 在隔离单车验证中到达目标附近，不等同于已经完成全部多车会车、碰撞、净空和安全认证。

## 验证

```bash
/home/xiaoa/miniconda3/envs/openpit-agent/bin/python -m unittest discover -s tests -q
/home/xiaoa/miniconda3/envs/openpit-agent/bin/python -m compileall -q src scripts open_pit_dispatch_app
bash -n check_demo_result.sh check_environment.sh map_resources.sh run_scenario.sh \
  start_api.sh start_carla.sh start_dispatch_app.sh start_slope_demo.sh
```

离线测试验证数据契约、场景逻辑和调度接口；地图、蓝图、物理路线与多车运行仍需在目标机器上进行CARLA验收。

## 真实性边界

- 风险、天气、爆破、故障和拥堵是可复现的参数化合成事件，不代表真实矿山事故数据。
- 固定站与车载监测数据用于比赛流程展示，必须标注为合成数据。
- 当前正式策略以Rule/Optimization Baseline为主；BC只是离线Shadow候选。
- 安全硬约束和Safety Shield始终独立于学习模型。

## 文档

- [项目系统讲解与答辩口径](docs/项目系统讲解与答辩口径.md)
- [技术交接与运行说明](项目技术交接与运行说明.md)
- [系统总体架构](docs/系统总体架构.md)
- [场景系统设计](docs/场景系统设计.md)
- [矿区空间资源库](docs/矿区空间资源库.md)
- [调度算法与Cost Model](docs/调度算法与CostModel.md)

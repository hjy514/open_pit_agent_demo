# OpenPit-Agent 露天矿智能调度与风险闭环平台

OpenPit-Agent 面向露天矿多车辆协同调度、异常处置和数据闭环。系统以 CARLA 0.9.10 的 `0325_5` 自定义矿山地图作为物理执行环境，由 Python Agent 负责场景、监测、风险、调度、路线、反馈和数据存储，通过 PyQt6 调度中心完成人机协同展示。

项目以可解释的 Rule-based Baseline 和 S08 边坡 Golden Demo 为稳定基础，并在同一工程中形成统一多场景、多车 CARLA 执行、地图资源库、多目标代价、闭环数据集和候选策略离线评估能力，为后续工程化接入和策略增强提供基础。

## 比赛版场景

| 场景 | 主要流程 | 定位 |
|---|---|---|
| S01 正常作业 | 随机任务→多车调度→CARLA执行→任务反馈 | 核心多车场景 |
| S02 车辆故障 | 故障→任务释放→候选评分→人工确认→换车接管 | 核心调度场景 |
| S04 爆破管控 | 爆破窗口→暂停或绕行→管控解除→原任务恢复 | 核心安全场景 |
| S09 复合扰动 | 道路封闭→路线处置→车辆故障→安全接管→恢复 | 核心综合场景 |
| S08 边坡风险 | 固定/移动监测→风险升级→人工确认→接管→原任务恢复 | Golden Demo |

S03、S05、S06、S07继续作为统一场景框架的扩展能力保留，比赛版场景选择器聚焦核心展示场景。代表性 Seed 的运行结果用于回归和功能验证，更多随机工况可在相同框架下持续扩展。

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
- `tests/`：离线回归测试，用于验证数据契约、场景逻辑和调度接口；目标机器可进一步开展CARLA物理验收。

环境版本、依赖安装、CARLA/地图附件和跨电脑部署步骤见：[环境配置与部署说明](docs/环境配置与部署说明.md)。

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

全部场景CARLA资源准入检查（不进入车辆执行阶段）：

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

CARLA多车执行默认优先使用 `map_resources.db` 中已有路线证据的候选路线。地图、蓝图、路线或场景硬约束不满足时，系统会在生成车辆前进行准入校验，确保运行条件和路线依据清晰可追溯。

## 调度与安全

```text
Hard Constraints → Feasible Candidate Set
→ Heuristic Baseline / Normalized Multi-Objective Cost
→ Scheduler → Safety Shield → Route Planner → CARLA
```

- 故障、不可用、能力不匹配、道路关闭、不可达和风险禁入均为硬约束。
- Heuristic Baseline V0继续作为比赛回归基线和Teacher。
- MultiObjective Cost V1记录时间、等待、延误、运输、负载、恢复与切换等可用代价。
- 对暂时缺少充分数据支撑的产量和能耗，系统保留相应接口，并采用 `NOT_AVAILABLE` 或 `SURROGATE_ONLY` 状态管理。
- S02和S09故障决策显示候选车辆及代价；S04根据事件类型展示暂停、等待或绕行方案，使调度建议与场景状态保持一致。
- 学习策略按照离线评估和 Policy Promotion 流程逐步引入，Safety Shield 始终作为独立安全保障层。

## 数据库与闭环

- `data/database/map_resources.db`：静态地图事实，包括出生点、拓扑、P5、P6和作业区。
- `data/database/openpit.db`：运行事实，包括Run、Episode、任务、事件、决策、候选评分、工单和反馈。
- `artifacts/`：本地运行结果、日志、摄像头帧和比赛证据。
- `data/datasets/`：从有效运行导出的状态、动作、回报、下一状态和终止状态。
- `data/models/`：离线训练和评估的Shadow候选策略，不自动替换正式策略。

## 地图资源

`map_resources.sh` 统一管理矿区点位、道路拓扑、作业区域、路线候选和物理验证证据，为场景随机生成、路线规划和多车辆调度提供空间资源基础。

地图资源库持续沉淀规划路线和车辆运行证据，为后续扩大场景规模、完善多车协同和接入真实矿区数据提供基础。

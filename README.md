# 露天矿 Agent 装备集群 Demo

本仓库用于 `CS-202616` 赛题的第三方调度平台与 CARLA 可行性验证。

## 统一仓库结构（后端 + 调度桌面端）

当前仓库同时包含后端与 PyQt6 调度桌面端：

```text
open_pit_agent_demo/
├── src/                         # Agent、调度、CARLA 适配与 API
├── configs/                     # 场景与地图资源配置
├── tests/                       # 后端与结构化 Mock 测试
└── open_pit_dispatch_app/      # PyQt6 调度中心
```

桌面端启动：

```bash
cd open_pit_dispatch_app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./start_app.sh
```

后端 API 仍从仓库根目录启动：

```bash
./start_api.sh
```

## 正式运行入口

日常运行和比赛演示只需关注下列入口：

| 入口 | 用途 |
|---|---|
| `./start_carla.sh` | 以CARLA默认画质启动仿真端 |
| `./start_api.sh` | 启动Agent API |
| `./start_dispatch_app.sh` | 启动PyQt6调度中心 |
| `./start_slope_demo.sh` | 启动正式边坡失稳Demo |
| `./check_environment.sh` | 执行只读启动前健康检查 |
| `./run_scenario.sh` | 正式统一场景入口；按场景和模式路由 |
| `scripts/run_scenario.py` | 结构化多场景内部实现，一般无需直接调用 |
| `./map_resources.sh` | 统一的地图资源建设与验证入口 |

`start_demo.sh`、`start_desktop.sh`和`start_mine_demo.sh`是早期浏览器态势台/启动器的历史兼容入口，不再作为当前正式比赛流程的首选命令。

查看所有已实现及规划中的场景：

```bash
./run_scenario.sh --list-scenarios
```

六车随机地图S01结构化运行：

```bash
./run_scenario.sh \
  --scenario s01 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202601
```

该入口默认执行`--policy heuristic`，并将运行摘要、V0实际基线决策和
V1影子多目标决策写入`artifacts/runs/`和`data/database/openpit.db`。
仅需临时调试且不保存时可加`--no-record`。

同一个S01 Seed也可以真正执行多目标优化策略：

```bash
./run_scenario.sh \
  --scenario s01 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202601 \
  --policy multi-objective
```

该模式按“硬约束→合法候选→归一化多目标代价→全局唯一分配”执行，
同时保留Heuristic V0对照，输出两者的选择变化、归一化总代价和路线总长。
`openpit.db`中的`scenario_runs.policy_version`记录本轮真实执行策略。
当前多目标执行开关覆盖随机地图S01初始分配和S02故障接管；S07仍执行确定性
路网决策，不能把本功能说成CARLA车辆控制策略已切换。

S01–S07与S09已接入同一CARLA多场景执行桥接。场景由
`--scenario`选择，不为每个场景另建启动脚本。首先进行不生成车辆的连接和资源准入检查：

```bash
./run_scenario.sh \
  --scenario s01 \
  --mode carla \
  --random-map \
  --vehicle-count 6 \
  --seed 202601 \
  --check-only
```

将`s01`换成`s02`、`s03`、`s04`、`s05`、`s06`、`s07`或`s09`
即可检查相应场景；S09建议使用`--vehicle-count 8`。检查通过后可先执行短流程验证：

```bash
./run_scenario.sh \
  --scenario s01 \
  --mode carla \
  --random-map \
  --vehicle-count 6 \
  --seed 202601 \
  --ticks 120
```

该路径复用结构化决策、`CarlaAdapter`和`BasicAgent`。S02执行故障停车与任务改派，
S03切换作业目标，S04/S06执行暂停与恢复，S05执行限速与速度恢复，
S07/S09执行重规划或换车接管。执行反馈、事件和闭环Cycle由统一证据入口写入
`openpit.db`与`artifacts/runs/`，退出时只销毁本轮生成车辆。封路和天气是参数化合成事件；
未经某个Seed的CARLA完整实跑，不应宣称该Seed已通过物理验证。

S02可在同一地图资源工作负载中按Seed选择一个具有合法备用接管车辆的
故障对象，验证任务释放、硬约束剔除、V0/V1接管对比和数据库记录：

```bash
./run_scenario.sh \
  --scenario s02 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202602
```

指定`--policy multi-objective`后，初始任务分配仍保持Heuristic V0，故障后的
接管车辆改由MultiObjective V1实际选择。当前结构化S02可用的真实区分量主要是
P5路线长度；等待、延误和恢复时间仍为`NOT_AVAILABLE`，因此多目标结果可能与
最短路线V0完全相同。这是数据边界，不应人为增加Penalty制造“优化效果”。
随机地图S01/S02中，Heuristic V0和MultiObjective V1现在都通过同一
`ClosedLoopCoordinator`和Safety Shield执行门，并向`closed_loop_cycles`
写入相同格式的State、Action、Feedback和Next State，可用于同Seed对照。
可使用现有统一入口自动完成配对A/B运行与汇总：

```bash
./run_scenario.sh \
  --scenario s01 \
  --compare-policies \
  --runs 10 \
  --vehicle-count 6 \
  --seed 202700
```

`--compare-policies`仅支持随机地图S01/S02，会对每个Seed分别执行
Heuristic V0和MultiObjective V1，并在同一批次摘要中输出配对通过率、
车辆选择变化、归一化代价、路线长度变化和Safety Shield状态。

该命令仍是结构化Mock；P5可达路线是静态规划事实，不等同于多车CARLA
物理行驶、会车、净空或碰撞安全验证。

S03装载设备故障与替代作业点切换：

```bash
./run_scenario.sh \
  --scenario s03 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202603
```

系统从本轮运输任务中选择一个关联装载设备，注入参数化设备故障，将故障
作业点作为硬约束排除，并从`map_resources.db`中选择该车辆可达且不同的替代
作业点。受影响车辆保持任务所有权并切换路线，其他车辆继续原任务。设备故障
和作业点切换时间是合成场景输入，路线是静态地图资源事实，不代表CARLA设备
物理故障、真实产量或真实作业节拍。

S04爆破作业与临时危险区管控：

```bash
./run_scenario.sh \
  --scenario s04 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202604
```

系统复用S07的真实拓扑道路约束与绕行能力，在爆破预告后对一条只影响部分
车辆的道路实施临时管控。有安全绕行路线时由原车绕行；没有安全绕行路线时，
原车等待爆破区域解除后继续任务，不因短时管控无依据地换车。当前危险范围只
声明为`TOPOLOGY_EDGE_ANCHOR_ONLY`，不等同于真实爆破半径或CARLA爆破物理模型。

S05极端降雨与道路能力下降：

```bash
./run_scenario.sh \
  --scenario s05 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202605
```

系统从当前任务使用的真实拓扑边中选择只影响部分车辆的路段，按Seed生成
参数化降雨、能见度和安全速度折减，然后比较原路线降速通行与规避该路段的
绕行ETA。天气数值属于`PARAMETERIZED_SYNTHETIC_SCENARIO`，ETA属于
`SURROGATE_ONLY_NOT_CARLA_MEASURED`，均不代表真实矿山观测或CARLA实测。

S06共享道路拥堵与安全放行：

```bash
./run_scenario.sh \
  --scenario s06 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202606
```

系统从本轮任务的真实拓扑路线中选择至少由两辆车共享、但不影响全部车辆的
内部道路段，将其容量设置为1，并依据预计到达时间、任务优先级、占用时间和
最小安全车头时距生成排队放行决策。道路拓扑和路线长度来自`map_resources.db`；
初始阻塞、容量及车头时距是可复现的参数化场景输入；到达、等待和通过时间是
基于路线长度与配置目标速度的工程估算，不是Traffic Manager或CARLA实测。

当P5全图点对标定完成且路网已导入后，可将严格可达点对转换为
可供S07识别受影响任务的有向路网边序列：

```bash
./map_resources.sh build-route-candidates
```

该命令只读取已有的CARLA拓扑与P5结果，无需启动CARLA。符合
P5/拓扑长度一致性门槛的记录标为`TOPOLOGY_DERIVED_UNVERIFIED`，偏差
过大的记录标为`TOPOLOGY_LENGTH_MISMATCH`并不进入S07；两者都不是
重型矿卡物理通行证明。

S07可在这些路网候选上运行可复现的6/8车随机道路中断闭环：

```bash
./run_scenario.sh \
  --scenario s07 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202607
```

场景只从当前任务路线的真实内部路网边中选择中断点，要求只影响
部分车辆。原车有合法绕行路线时保持原任务；原车无路可绕时，才从非受影响
车辆中按能力、路网一致性、关闭边规避和绕行比例硬约束选择接管车辆。
接管车的原任务保留，新任务作为后续任务；其他未受影响任务不重新调度。
这仍是离线结构化验证，不是多车
CARLA物理通行结论。

S01至S07及S09的统一批量闭环入口：

```bash
./run_scenario.sh \
  --scenario all \
  --mode structural \
  --runs 10 \
  --vehicle-count 6 \
  --seed 202607 \
  --policy auto
```

`--scenario all`会自动启用地图资源约束随机模式。每个场景每轮都作为独立
Run写入`openpit.db`，并在`artifacts/batches/<batch_id>/summary.json`
生成任务完成率、故障接管率、路线重规划率和失败原因汇总。临时验证
可加`--no-record`，此时数据库和批次文件都不写入。

`--policy auto`是批量采集策略编排：S01/S02自动执行多目标调度，
S03至S07及S09执行各自的安全事件策略。每批结束后还会自动将
本批`closed_loop_cycles`导出到
`data/datasets/openpit-closed-loop-transition-v1/<batch_id>/`；通过
`run_id_filter`保证不混入历史运行。

批量采集前可对运行数据库做只读健康检查：

```bash
./run_scenario.sh --database-health --stale-hours 24
```

报告包含Run状态、表记录数、事件量、高量Run和闭环场景覆盖。
该命令不修改数据。确认后可显式将超过阈值且仍为
`running/planned`的中断Run标记为`INCOMPLETE`：

```bash
./run_scenario.sh --repair-stale-runs --stale-hours 24
```

修复不删除运行、事件或证据。新Run在未产生终结摘要即退出时，
会自动收尾为`INCOMPLETE`，避免继续积累伪`running`记录。

现有CARLA边坡失稳Golden Demo也通过同一入口启动：

```bash
./run_scenario.sh --scenario s08 --mode carla
```

它内部复用`start_slope_demo.sh`，没有复制风险、调度或CARLA控制代码。
运行前仍需先启动CARLA、Agent API和PyQt调度中心。仅检查启动条件时使用：

```bash
./run_scenario.sh --scenario s08 --mode carla --check-only
```

S09复合扰动已纳入同一入口：

```bash
./run_scenario.sh \
  --scenario s09 \
  --mode structural \
  --random-map \
  --vehicle-count 6 \
  --seed 202609
```

S09 V1在同一份Seed地图、车队和任务状态上，按顺序施加“道路封闭→车辆
故障”两个参数化合成事件。系统先处理受影响路线，再释放故障车任务，
只在能力匹配、P5点对可用且路线避开封闭边的合法候选中选择接管车。
不可执行的随机组合会在20次上限内按固定规则重采样，并保留`workload_seed`
和`generation_attempt`；不会放松安全硬约束。当前仍属于结构化闭环验证，
不代表六车或八车已完成CARLA物理实跑。

默认记录模式还会在
`data/datasets/openpit-structural-transition-v1/<batch_id>/`生成
`transitions.jsonl`和`manifest.json`。S01任务分派、S02车辆故障重分配、
S03作业点切换、S04爆破管控、S05天气响应、S06道路放行、S07封路重规划和
S09复合事件共用同一
`state/action/result/next_state/done`格式。
当前是“决策到结构化终态”的离线样本，可用于BC数据准备和分析。
`structural-terminal-reward-v1`只对任务终态、闭环证据、场景恢复、换车和可计算绕行代价
进行透明评价，权重为尚未经敏感性验证的初始工程权重。CARLA逐步物理反馈、
真实能耗和碰撞指标仍为`NOT_AVAILABLE`，且数据仍明确标记不可直接用于PPO训练。

`manifest.json` 内置数据质量报告，统计每个场景的Seed覆盖、动作类型、
车辆选择、故障车、封路边和Reward分布，并检查重复ID、关键字段缺失、
场景缺失、闭环证据及源运行失败。失败运行只如实列入报告，不会伪造成训练Transition。

将已通过质量检查的多个批次聚合成不可变数据集版本：

```bash
./run_scenario.sh \
  --aggregate-datasets \
  --dataset-version structural-bc-v1-YYYYMMDD \
  --split-seed 202616
```

输出位于`data/datasets/openpit-structural-transition-v1/versions/<version>/`，
包含`train.jsonl`、`validation.jsonl`、`test.jsonl`和`manifest.json`。
划分以Seed为最小单位，不允许同一Seed跨集合；在保持集合大小和Seed隔离的
前提下，系统会交换整组Seed以尽量覆盖所有场景和动作类型。
若原始数据无法满足覆盖，则输出`DATASET_VERSION_READY_WITH_COVERAGE_WARNINGS`，
不隐藏数据偏斜。

训练不带CARLA执行权限的BC候选车辆排序模型：

```bash
./run_scenario.sh \
  --train-bc \
  --dataset-version structural-bc-v1-110seeds-20260906 \
  --model-version bc-dispatch-v1-YYYYMMDD \
  --epochs 400
```

模型仅在硬约束通过的候选集中学习Softmax排序，并保存在
`data/models/dispatch_policy/<model_version>/`。当前仅为离线Shadow Policy，
无权向CARLA发送命令；未通过A/B、Safety Shield和CARLA回归前不得提升为执行策略。
首个110-Seed模型的多候选测试准确率为48.94%，仅略高于47.52%的均匀机会基线；
它能完整复现故障重分配样本，但不能仅凭单任务特征复现全局唯一分配。
因此当前`promotion_status=NOT_PROMOTED`。

批量结果同时区分`status=PASS`和`closed_loop_status=CLOSED_LOOP_PASS`。
前者只表示程序正常结束，后者还要通过场景业务规则和SQLite证据检查。
默认记录模式会在`metrics`表写入`closed_loop_evidence_pass`；
`--no-record`时数据库验收状态为`NOT_AVAILABLE`，不会伪造入库结论。

## 新电脑快速配置

本项目已将 open_pit_dispatch_app 纳入同一仓库。默认情况下，
运行结果会导出到仓库内的 open_pit_dispatch_app/data；如需自定义，可通过 OPENPIT_DISPATCH_DATA_DIR 指定数据目录。

CARLA 0.9.10 使用 Python 3.7 API。在新电脑上安装或激活对应环境后执行：

```bash
cd ~/open_pit_agent_demo
python -m pip install -r requirements.txt

export OPENPIT_PYTHON="$(command -v python)"
export OPENPIT_CARLA_ROOT="/实际路径/CARLA_0.9.10"
```

这些变量解决了原电脑用户名、Conda 安装位置和 CARLA 安装位置不同的问题。
也可以把它们加入本机 shell 配置，但不要把带有内部路径或凭据的 `.env`
文件提交到 GitHub。

启动供 `open_pit_dispatch_app` 使用的本地 API：

```bash
./start_api.sh
```

启动 CARLA、Agent API 和 PyQt6 调度中心后，运行当前正式边坡失稳演示场景：

```bash
./start_slope_demo.sh
```

只检查 CARLA 和 Agent API 是否就绪：

```bash
./start_slope_demo.sh --check-only
```

正式启动场景前，建议先运行只读健康检查。它会检查当前 CARLA
服务、目标矿山地图的出生点和路网拓扑、矿卡蓝图及两个 SQLite
数据库；不会切换地图、生成车辆或修改任何数据：

```bash
./check_environment.sh
```

如 Agent API 与调度中心已经启动，可额外检查：

```bash
./check_environment.sh \
  --check-api \
  --check-ui
```

如需回归早期浏览器态势台，可使用历史兼容入口：

```bash
./start_demo.sh --check-only
python run_demo.py --mode mock --inject-failure
```

不同电脑应通过环境变量和本地配置指定 Python 与 CARLA 路径。
CARLA 本体、矿山地图、真实坐标与生产数据不随本仓库上传。

项目开发、测试和演示人员应先阅读：

```text
项目技术交接与运行说明.md
```

该文件集中记录当前完成状态、验证边界、操作方法和后续优先级。

当前正式 Demo 使用 CARLA 自定义露天矿地图 `0325_5`、
3辆 `vehicle.cat.cat` 矿卡、8个固定监测站和3辆移动监测装备，
验证边坡风险预警、候选矿卡评估、人工确认、任务接管和复核反馈闭环。

## 当前已实现

- Python 3.7 兼容的统一配置与数据模型；
- 三种等效装备角色及能力标签；
- 基于能力、优先级、距离和负载的确定性基线调度器；
- 无需启动 CARLA 的 Mock 可重复演示；
- CARLA 0.9.10 连接、地图核对、三车发现/生成、状态读取；
- 使用 CARLA `BasicAgent` 下发目标点并执行基础导航；
- 使用显式距离容差验收任务到达，支持任务超时；
- CARLA观察者相机自动跟随当前最高优先级任务车辆；
- 长程连续巡检场景支持三车各两段路线和阶段切换；
- 任务结束后施加真实物理制动，并按实测车速确认驻车；
- 场景V2变量集中描述环境、灾害、任务背景和随机事件；
- 固定/覆盖/随机种子运行，并保存可复现的场景快照；
- 高优先级任务抢占、完成后恢复原任务；
- 车辆故障注入、任务释放和能力约束重新分配；
- 正常闭环与安全隔离故障接管使用独立场景配置；
- 路线长度、直线距离和路线终点误差校准工具；
- 固定站与移动巡检合成数据的透明蓝、黄、橙风险规则；
- 橙色风险动态生成复核任务并触发能力约束抢占；
- 风险复核工单的待处理、已派发、执行中、待复核和关闭状态机；
- 红色风险联动生成风险复核、道路管控和应急响应任务；
- 多动作独立工单、不同自动复核延迟和完整责任链；
- 策略层道路限制登记，以及普通任务禁入、授权应急任务放行；
- 红色边坡风险下，CARLA应急任务按“安全中间点→处置终点”分段导航；
- 风险影响、预防措施、环境注意项和规划动作的可解释输出；
- 感知、评估、调度和执行智能体之间的结构化决策记录；
- 每次运行导出可供模仿学习/离线训练的状态—动作—结果经验集；
- 后续运行读取同场景成功经验，形成不越过能力与安全约束的有界模仿偏好；
- 固定与随机种子场景文件，以及运行级场景快照；
- 每次运行生成事件日志、测试摘要和保守的自动功能验收报告。

当前尚未接入真实矿山监测数据或经验证的风险模型。比赛 Demo 已实现策略层封控和 CARLA 预设安全中间点分段导航，但尚不是通用路网边级动态避障规划器；经验数据已经导出，但尚未训练或部署学习模型；真实人工审批、多渠道通知、数据库服务和大模型 Agent 也未实现。

## 工程结构

```text
.
├── configs/
│   ├── town03.json
│   ├── town03_competition_demo.json
│   ├── town03_long_patrol.json
│   ├── town03_fault_recovery.json
│   ├── town03_risk_response.json
│   ├── risk_slope_synthetic.json
│   ├── town03_red_response.json
│   ├── risk_slope_competition_synthetic.json
│   ├── risk_slope_red_synthetic.json
│   └── open_pit_mine.example.json
├── scripts/
│   ├── check_environment.py
│   ├── dashboard_server.py
│   └── inspect_map_routes.py
├── dashboard/
│   ├── index.html
│   ├── dashboard.css
│   └── dashboard.js
├── src/open_pit_agent/
│   ├── adapters/
│   ├── cli.py
│   ├── config.py
│   ├── decision_intelligence.py
│   ├── evidence.py
│   ├── map_data.py
│   ├── models.py
│   ├── risk.py
│   ├── restrictions.py
│   ├── scheduler.py
│   └── work_order.py
├── tests/
├── requirements.txt
├── start_api.sh
├── artifacts/runs/
└── run_demo.py
```

CARLA 本体和 UE 地图不复制进本仓库。当前配置只引用：

```text
/home/hjy/桌面/carla0.9.10_package/CARLA_0.9.10-dirty
```

## 1. 环境自检

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python scripts/check_environment.py
```

## 2. 不启动 CARLA 的 Mock 演示

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode mock \
  --inject-failure
```

该命令会完成初始任务分配、`inspection_vehicle_01` 故障注入和任务重新分配，并在 `artifacts/runs/` 生成证据文件。

## 3. CARLA 三车正常闭环

先在桌面终端启动 CARLA：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_carla.sh
```

另开终端，只检查连接和当前车辆：

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode carla-check
```

确认后加载 `Town03`、生成缺失车辆并执行导航：

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode carla-run \
  --load-map \
  --spawn-missing \
  --ticks 600
```

正式通过证据：

```text
artifacts/runs/town03-feasibility-v1-20260801T062422Z-07286056
```

该轮三项任务全部在 8 米容差内完成，`completion_rate=1.0`、`status=PASS`，并在第 423 tick 自动结束。

## 4. CARLA 安全隔离故障接管

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --config configs/town03_fault_recovery.json \
  --mode carla-run \
  --load-map \
  --spawn-missing \
  --ticks 1800 \
  --inject-failure
```

正式通过证据：

```text
artifacts/runs/town03-fault-recovery-v1-20260801T065022Z-a977eb2c
```

该轮在第 80 tick 注入安全隔离后的执行能力故障，巡检车02抢占高优先级任务，完成后恢复原任务；三项任务全部完成，`completion_rate=1.0`、`status=PASS`，第 899 tick 自动结束。

`12m` 横向隔离仅用于超过 CARLA 0.9.10 `BasicAgent` 的 `10m` 车辆避障阈值，不是矿山安全距离。道路中央抛锚、道路封控和绕行尚未在该场景中验证。

`--load-map` 会切换 CARLA 当前世界；`--spawn-missing` 只生成配置中尚不存在的 `role_name`。程序退出时不会删除车辆，方便继续观察和调试。

## 5. 路线校准

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python scripts/inspect_map_routes.py \
  --minimum-route-m 1 \
  --maximum-route-m 200 \
  --common-vehicle-ids \
    inspection_vehicle_01 \
    inspection_vehicle_02 \
    emergency_vehicle_01
```

工具同时检查路径长度、直线距离和路线终点误差。后续导入矿山地图后，应先用它校准出生点和任务区，再进行集群测试。

## 6. 合成边坡风险驱动复核

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --config configs/town03_risk_response.json \
  --risk-config configs/risk_slope_synthetic.json \
  --mode carla-run \
  --load-map \
  --spawn-missing \
  --ticks 1400
```

正式通过证据：

```text
artifacts/runs/town03-risk-response-v1-20260801T071448Z-a49d4c86
```

该轮风险在 tick 0/40/80 依次评估为蓝/黄/橙；橙色风险在同一 tick 创建复核任务和工单，车辆02抢占普通巡检。风险任务在 tick 655 完成，工单进入待复核；20 tick 后由明确标注的 Demo 规则关闭。最终4项任务全部完成、工单关闭率100%，`status=PASS`。

风险数据和阈值明确标注为合成 Demo 数据，不代表真实矿山测量值或预警标准。当前异步仿真的 tick 不能直接换算成真实秒级响应时间。

工单自动复核结果为 `approved_by_demo_rule_no_human_review`，只证明状态机和事件联动，不代表真实人员审批。

## 7. 红色风险多装备联动

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --config configs/town03_red_response.json \
  --risk-config configs/risk_slope_red_synthetic.json \
  --mode carla-run \
  --load-map \
  --spawn-missing \
  --ticks 3000
```

正式通过证据：

```text
artifacts/runs/town03-red-response-v1-20260801T073742Z-9ae20f38
```

该轮在 tick 80 触发橙色复核，在 tick 120 升至红色并登记道路限制，同时生成道路管控、应急响应和红色复核任务。6项任务全部完成、4张风险工单全部关闭，`completion_rate=1.0`、`status=PASS`，第1089 tick自动结束。被抢占的常规任务在风险任务完成后恢复并完成。

`route_avoidance_enforced=false` 是有意保留的验收边界：当前证明的是限制策略、授权任务和处置闭环，不证明车辆能绕开任意封闭道路边。矿山地图阶段需将 `road_segment_id` 绑定真实路网边，并把活动限制交给路径规划器。

完整设计和结果见 `docs/05_red_risk_multi_action_response.md`。

## 8. 历史兼容：浏览器态势台一键启动

该入口用于回归早期浏览器态势台：

```bash
cd /home/hjy/open_pit_agent_demo
./start_demo.sh
```

脚本会：

1. 检查Google Chrome/Chromium；
2. 通过CARLA Python API检查 `127.0.0.1:2000`；
3. CARLA未运行时，自动以Low画质启动并等待最多120秒；
4. CARLA已运行时直接复用，不启动第二个实例；
5. 弹出独立态势台窗口；
6. 窗口关闭后，只停止本脚本自己启动的CARLA。

窗口中的推荐用法：

- `比赛一键综合演示（推荐）`：自然语言任务、故障接管、风险升级、道路管控、应急响应和4张工单的一次闭环；
- `正常三车巡检`：约十几秒，用于快速检查连接、调度、导航和驻车；
- `长程连续巡检（推荐观察）`：三辆车各执行两段路线，用于观察、讲解和录屏；
- 故障、橙色风险和红色风险场景：用于验证异常接管与风险处置闭环。

长程场景运行时，CARLA镜头会跟随当前最高优先级任务车辆；该车辆完成阶段任务后，镜头按仍在执行的任务自动切换。不要把短健康检查的持续时间当作正式展示时长。

比赛全屏：

```bash
./start_demo.sh --fullscreen
```

只检查环境：

```bash
./start_demo.sh --check-only
```

如果希望窗口关闭后仍保留由脚本启动的CARLA：

```bash
./start_demo.sh --keep-carla
```

## 9. 历史兼容：仅启动早期独立桌面窗口

该入口用于回归早期独立窗口：

```bash
cd /home/hjy/open_pit_agent_demo
./start_desktop.sh
```

脚本会自动：

1. 选择一个本机空闲端口；
2. 启动数据与场景控制服务；
3. 使用独立应用配置弹出“集群安全态势台”窗口；
4. 隐藏浏览器地址栏、标签页和导航按钮；
5. 窗口关闭后停止它启动的后台服务并清理临时配置。

演示全屏：

```bash
./start_desktop.sh --fullscreen
```

只检查依赖和服务、不弹出窗口：

```bash
./start_desktop.sh --check-only
```

窗口启动器不会自动启动CARLA主程序。应先启动CARLA，再从窗口内选择Town03场景。诊断日志写入 `artifacts/control/*-desktop-window.log`。

如果CARLA已经由其他方式管理，也可以只启动窗口：

```bash
./start_desktop.sh
```

## 10. 本地可视化服务

启动只读数据服务和浏览器页面：

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python \
  scripts/dashboard_server.py
```

浏览器访问：

```text
http://127.0.0.1:8080
```

页面每秒读取一次最新状态，可选择历史运行，也能跟踪正在写入 `events.jsonl` 的运行。当前显示Town03车道中心线、车辆位置和轨迹、任务区域、风险趋势、道路限制、工单和事件流。

接口：

```text
GET /api/health
GET /api/map
GET /api/runs
GET /api/state?run_id=<运行ID>
GET /api/events?run_id=<运行ID>&limit=100
GET /api/control/scenarios
GET /api/control/status
POST /api/control/start
POST /api/control/stop
```

页面现在可以从白名单中选择并启动短正常巡检、长程连续巡检、故障接管、橙色风险和红色风险场景，也可以请求安全停止。启动场景会使用 `--load-map` 重新加载Town03并清除当前CARLA世界状态，因此页面会在执行前再次确认。

控制接口只允许固定场景ID，不接受任意命令、配置路径或shell文本。带控制功能的服务强制只监听本机地址。

网页一键启动正式验收证据：

```text
artifacts/runs/town03-feasibility-v1-20260801T075839Z-33239927
```

该轮由 `/api/control/start` 启动，控制进程退出码为0；3项任务全部完成，`completion_rate=1.0`、`status=PASS`，第413 tick自动结束。

地图和风险图层验收证据：

```text
artifacts/runs/town03-red-response-v1-20260801T081712Z-80d72c94
```

该轮同样由网页控制接口启动并获得 `PASS`；地图接口提取341条Town03车道折线，运行状态包含5个任务区域、3辆车的165个轨迹采样点、4次风险评估和1条活动道路限制。

## 11. 自动测试

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python -m unittest discover -s tests -v
```

当前共有 53 项测试，并覆盖安全中间点分段导航、中间点到达不误结单和绕行状态记录。

## 13. 比赛一键综合演示

首选从态势台选择：

```text
比赛一键综合演示（推荐）
```

固定流程：

```text
自然语言任务记录
→ 感知巡检车执行常规任务
→ tick 120 可复现故障
→ 应急车接管
→ 蓝/黄/橙/红风险升级
→ 融合车风险复核
→ 道路管控与应急响应
→ 完成车辆释放共享车道
→ 恢复常规任务
→ 4张工单关闭
```

正式通过证据：

```text
artifacts/runs/town03-competition-demo-v1-20260801T125018Z-e322e748
```

该轮5项任务全部完成，4张工单全部关闭，完成率100%，`status=PASS`，第1785 tick自动结束。场景使用固定种子`202616`，道路限制仍明确标注为策略层约束，未声称实现CARLA路网边级绕行。

## 14. 决策智能、经验数据与自动验收

新的比赛综合运行会在原有 `summary.json` 和 `events.jsonl` 之外生成：

```text
agent_decisions.jsonl
experience_dataset.jsonl
acceptance_report.json
```

- `agent_decisions.jsonl`：记录场景状态、智能体分工、调度动作、评分和理由；
- `experience_dataset.jsonl`：每项任务一条状态—动作—结果—奖励样本，可用于后续模仿学习或离线训练；
- `acceptance_report.json`：自动检查任务闭环、终态驻车、故障接管、动态风险、解释与预防、工单、道路限制和数据导出。

经验文件中的 `learning_status=offline_dataset_only_no_model_trained` 是明确的能力边界：当前没有训练神经网络模型。后续运行会读取同场景成功样本，形成最大 `2m` 等效分值的有界模仿偏好；能力、健康、安全限制仍先于经验偏好，风险阈值也不会自行改变。

运行比赛综合场景结束后，检查最新 CARLA 结果：

```bash
cd /home/hjy/open_pit_agent_demo
./check_demo_result.sh
```

脚本会显示通过/失败、功能得分、任务完成数、风险等级序列、决策记录数、经验样本数和失败检查项。若要检查指定运行：

```bash
./check_demo_result.sh \
  --run-dir artifacts/runs/<运行目录名>
```

`acceptance_report.json` 的通过只表示 Town03 普通车辆代理下的软件功能闭环通过，不是矿山工业安全认证。

## 12. 场景变量与可复现随机试验

`configs/town03_fault_recovery.json` 已接入场景V2变量，集中记录环境、灾害背景、任务目标、成功条件和随机事件。

默认使用配置中的固定种子：

```bash
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode mock \
  --config configs/town03_fault_recovery.json \
  --inject-failure
```

指定种子复现实验：

```bash
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode mock \
  --config configs/town03_fault_recovery.json \
  --inject-failure \
  --seed 12345
```

生成新的随机种子：

```bash
/home/hjy/miniconda3/envs/tcp37/bin/python run_demo.py \
  --mode mock \
  --config configs/town03_fault_recovery.json \
  --inject-failure \
  --randomize
```

每次运行目录都会生成 `scenario_snapshot.json`，记录本轮实际种子、环境变量和已解析事件。固定种子 `202616` 的CARLA正式回归将故障解析为 `emergency_vehicle_01`、tick `81`，实际事件与快照一致，最终状态为 `PASS`：

```text
artifacts/runs/town03-fault-recovery-v1-20260801T120438Z-bbfb3215
```

## 场景迁移原则

Town03 阶段的结论应表述为“城镇道路代理环境下的功能级验证”，不能表述为露天矿工业验证。

迁移到矿山环境时，主要替换：

- `map_name` 和地图资产路径；
- 车辆蓝图、出生点和目标区域；
- 传感器与装备能力；
- 矿山路网和禁行边；
- 固定监测站及移动监测数据源。

调度器、任务模型、风险事件接口、工单状态机和证据输出保持不变。

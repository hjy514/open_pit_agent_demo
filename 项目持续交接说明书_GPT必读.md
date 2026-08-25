# 露天矿 Agent 装备集群项目持续交接说明书（GPT/Codex 必读）

> 当前状态日期：2026-08-01  
> 原始验证工作区：`/home/hjy/open_pit_agent_demo`  
> 迁移后以仓库根目录为准；历史绝对路径仅用于追溯原环境。  
> 用途：在新对话中把本文件交给 GPT/Codex 阅读，使其准确理解项目已经完成什么、尚未完成什么，以及如何继续操作。  
> 维护规则：每完成一个重要功能或正式 CARLA 验收，应更新“当前真实状态”“证据目录”“下一步优先级”和文末变更记录。

---

## 0. 给接手 GPT/Codex 的强制工作规则

1. **先阅读本文件，再阅读实际代码和相关里程碑文档，不能只根据聊天描述判断项目状态。**
2. 任何能力必须区分：
   - 已实现并通过 CARLA；
   - 已实现但只通过单元测试或 Mock；
   - 仅设计、尚未实现。
3. 用户希望每一步操作前都说明：
   - 接下来要做什么；
   - 为什么要做；
   - 对后续比赛目标和矿山迁移有什么影响。
4. 用户当前时间和使用额度都很紧张。优先一次性完成相互关联的改动，先运行单元测试和 Mock，避免频繁重启 CARLA。
5. 当前可视化窗口的总体布局已经冻结。除非用户明确同意，不要重做 UI，不要擅自更换可视化技术路线。
6. 工作区位置已经确定为 `/home/hjy/open_pit_agent_demo`，后续矿山地图版本仍在同一工作区演进，不要再次询问是否另建目录。
7. 保持 Python 3.7 和 CARLA 0.9.10 兼容，不能随意使用高版本 Python 语法。
8. 调度平台必须保持为 CARLA 外部第三方系统。不要把业务调度器写进 UE4/CARLA 源码。
9. 不得把合成风险数据称为真实矿山监测，不得把策略层道路限制称为真实路径绕行，不得把有界经验记忆称为已训练大模型。
10. 改动后至少运行：

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python -m unittest discover -s tests -v
```

---

## 1. 项目目标和比赛背景

赛题编号：

```text
CS-202616
```

赛题名称：

```text
Agent・具身智能集群矿山安全高效成套装备创新应用解决方案
```

项目聚焦露天矿，不同时扩展井工矿、尾矿库等其他方向。冻结的核心业务场景是：

**面向露天矿边坡失稳风险的异构装备集群日常巡检、动态监测、主动预警、故障接管和应急调度。**

总体目标分为三层：

1. 装备集群层：多车/多装备协同运行、任务执行、故障接管和容错重构；
2. 感知决策层：环境、场景、灾害和任务变量驱动动态风险评估及调度决策；
3. 执行闭环层：预警、风险复核、道路管控、应急响应、工单追踪和可视化说明。

项目名称可表述为：

**面向露天矿边坡风险的 AI Agent 驱动异构具身装备集群智能监测、预警与应急调度系统。**

---

## 2. 比赛时间节点

### 2026-08-15 前：纸质材料

已知需要重点准备：

- 作品设计报告；
- 测试报告；
- 总结报告；
- 使用说明；
- 报名表；
- 建议附总体架构图、系统截图、测试证据、装备设计和初步 BOM。

内部应尽量在 2026-08-13 前完成寄送。仍需向主办方确认邮寄地址、份数、装订格式，以及 8 月 15 日是寄出还是送达日期。

### 2026-09-15 前：电子成果

需要继续准备：

- 源代码与可执行程序；
- 实验结果；
- 测试系统部署包；
- 工业性试验证明；
- 样机图纸与 BOM；
- 报名表及其他要求文件。

当前策略：

**先用可运行 Town03 Demo、截图、日志和测试结果支撑 8 月纸质材料；之后继续完成矿山地图、真实数据接口和工程化增强。**

---

## 3. 当前阶段为什么使用 Town03 普通车辆

用户已确认：Demo 第一阶段不先制作露天矿地图，而是在 CARLA 自带 `Town03` 中用普通车辆代理装备角色，先验证系统闭环可行性。

这不是改变最终目标，而是分阶段验证：

```text
Town03普通车功能代理
→ 验证集群软件、风险、调度、执行和工单闭环
→ 后续替换矿山地图、矿用装备和真实监测数据
→ 核心代码与证据格式继续复用
```

迁移矿山环境时继续使用同一工作区，主要替换：

- CARLA 地图和道路资产；
- 普通车蓝图与矿用装备模型；
- 出生点、任务区域和真实停车/会车点；
- 合成风险观测与矿山真实/典型数据；
- 代理道路段与真实矿山路网边；
- 演示指标与现场安全、效率指标。

调度器、任务模型、Agent 事件、风险接口、工单状态机、经验格式和验收报告应尽量保持不变。

---

## 4. 当前系统真实状态总览

### 4.1 已经通过 CARLA 正式验证的能力

- 外部 Python 程序连接 CARLA 0.9.10；
- 加载 Town03、发现或生成3辆配置车辆；
- 读取车辆位置、速度、健康和任务状态；
- 按能力、优先级、距离和负载调度任务；
- 使用 CARLA `BasicAgent` 导航到任务点；
- 通过显式距离容差判断任务完成；
- 多任务队列、高优先级抢占及原任务恢复；
- 车辆故障注入、任务释放和其他车辆接管；
- 蓝、黄、橙、红合成风险演化；
- 橙色风险复核；
- 红色风险下的融合复核、道路管控和应急响应；
- 风险工单从创建、派发、执行、待复核到关闭；
- 策略层道路限制登记和任务准入限制；
- 完成任务后车辆驶离共享车道并待命；
- 所有任务结束后物理制动和低速终态确认；
- CARLA 观察相机自动跟随最高优先级任务车辆；
- 运行事件、摘要、车辆轨迹和风险趋势的可视化；
- 从窗口白名单启动不同演示场景；
- 一键启动 CARLA 和独立应用式态势台窗口。

### 4.2 已实现并通过38项测试及 Mock，等待新一轮 CARLA 回归

2026-08-01 最新增加：

- 风险影响说明；
- 预防措施；
- 环境注意项；
- 规划处置动作；
- 感知、风险评估、调度、执行 Agent 的结构化协同决策记录；
- `state → action → result → reward` 经验数据导出；
- 同场景历史成功经验形成最大 `2m` 等效分值的有界模仿偏好；
- 自动功能验收报告；
- 中文结果检查脚本；
- 现有风险卡片内显示依据、影响、预防和动作，不改变整体 UI 布局。

这些代码已经通过：

```text
38项 Python 单元测试：PASS
比赛综合 Mock：PASS
```

但接手者必须注意：

**上述最新解释、学习和自动验收层尚未由用户完成一轮新的 CARLA 综合回归。下一项最高优先级任务就是运行并确认。**

### 4.3 尚未实现或不能宣称已实现

- 真实露天矿地图和矿用装备模型；
- 真实矿山监测数据；
- 经矿山数据标定或验证的风险模型；
- CARLA 道路边级动态绕行；
- 完整时空冲突消解和矿山路权模型；
- 已训练的神经网络调度策略；
- 真正在线强化学习或大模型自动进化；
- 正式大语言模型 Agent 推理；
- MFS-LLM 与论文正式调度算法接入；
- 多渠道真实通知；
- 人工审批系统；
- SQLite/PostgreSQL 等数据库服务；
- 工业现场试验和安全认证。

当前自然语言任务是白名单结构化基线：

```text
large_language_model_used=false
```

历史模仿记忆只是安全约束后的微小调度偏好：

```text
maximum_score_bonus_m=2.0
safety_constraints_overridable=false
risk_thresholds_self_modified=false
model_trained=false
```

---

## 5. 当前系统架构

```text
场景配置/自然语言任务
          |
          v
第三方 Agent 调度平台
  |       |        |         |
  |       |        |         +--> 工单与道路限制
  |       |        +------------> 动态风险评估与处置解释
  |       +---------------------> 能力约束调度与经验记忆
  +-----------------------------> CARLA适配器
                                       |
                                       v
                              Town03三车执行与状态回传
                                       |
                                       v
                     事件日志、可视化、经验数据、验收报告
```

Agent 角色：

- `perception_agent`：汇总车辆、位置、能力、环境与风险观测；
- `risk_assessment_agent`：使用透明规则输出风险等级和依据；
- `scheduling_agent`：按能力、健康、优先级、距离、负载及有界经验偏好选车；
- `safety_execution_agent`：下发任务、处理故障、道路限制、应急任务和终态驻车。

硬约束先于经验偏好：

```text
能力/健康/安全准入
→ 基线距离与负载评分
→ 最多2m等效历史模仿偏好
→ 任务派发
```

---

## 6. 比赛综合场景

窗口白名单名称：

```text
比赛一键综合演示（推荐）
```

配置文件：

- `configs/town03_competition_demo.json`
- `configs/risk_slope_competition_synthetic.json`

固定种子：

```text
202616
```

预期事件链：

```text
自然语言任务记录
→ 常规巡检
→ Tick 120 巡检车故障
→ 应急车接管
→ Tick 0/220/360/520 蓝/黄/橙/红风险
→ 融合车执行橙色和红色风险复核
→ 应急车执行道路管控和应急响应
→ 4张风险工单关闭
→ 常规任务恢复
→ 5项任务全部完成
→ 车辆停车并自动结束
```

风险等级对应的演示动作：

| 等级 | 动态说明 | 动作 |
|---|---|---|
| 蓝 | 未触发黄色阈值 | 常规监测 |
| 黄 | 指标上升 | 加密复测、准备融合装备 |
| 橙 | 风险区安全裕度下降 | 相机+激光雷达融合复核 |
| 红 | 风险区通行和作业受影响 | 风险复核、道路管控、应急响应、策略限制 |

风险卡片会显示：

- 评估依据；
- 可能影响；
- 预防措施；
- 环境注意项；
- 规划动作。

---

## 7. 从头运行和验收

### 7.1 一键启动

如果旧窗口仍在运行，先关闭旧窗口，使后端重新加载最新代码。

```bash
cd /home/hjy/open_pit_agent_demo
./start_demo.sh
```

脚本会：

1. 检查 Chrome/Chromium 窗口引擎；
2. 检查 `127.0.0.1:2000`；
3. CARLA 未运行时自动启动；
4. 弹出无地址栏、标签页和导航按钮的独立应用式窗口；
5. 提供白名单场景控制。

界面底层使用本地 HTML/CSS/JavaScript，但用户体验是独立应用窗口。用户已决定暂不更换为 Qt/Electron。

### 7.2 启动比赛场景

在窗口中选择：

```text
比赛一键综合演示（推荐）
```

点击“启动场景”，确认后等待运行结束。

重点观察：

- CARLA 车辆是否实际移动；
- 故障后是否有车辆接管；
- 风险是否按蓝、黄、橙、红变化；
- 风险卡片是否显示影响和预防；
- 橙色后是否出现风险复核任务；
- 红色后是否出现道路管控和应急响应；
- 最终任务是否为 `5/5`；
- 工单是否为 `4/4`；
- 场景是否自动结束；
- 车辆是否停止。

### 7.3 自动检查最新 CARLA 结果

场景结束后，在另一个终端运行：

```bash
cd /home/hjy/open_pit_agent_demo
./check_demo_result.sh
```

第一次新 CARLA 回归的理想结果：

```text
运行结果：PASS
任务完成：5/5
风险等级序列：blue → yellow → orange → red
协同决策记录：约6条
经验样本：5条
历史模仿记忆：0条或已有历史成功经验
```

第一次运行主要建立成功经验。第二次运行应能在摘要的 `imitation_memory` 中看到历史成功经验和有界偏好。

如果脚本提示没有验收报告，通常表示：

- 新综合场景还未运行；
- 场景还没有结束；
- 运行在最新功能加入前完成。

指定运行目录检查：

```bash
./check_demo_result.sh \
  --run-dir artifacts/runs/<运行目录名>
```

### 7.4 只做离线回归

```bash
cd /home/hjy/open_pit_agent_demo
/home/hjy/miniconda3/envs/tcp37/bin/python -m unittest discover -s tests -v
```

### 7.5 只检查启动条件

```bash
./start_demo.sh --check-only
```

---

## 8. 每次运行的证据文件

运行目录：

```text
artifacts/runs/<scenario-id>-<timestamp>-<suffix>/
```

主要文件：

| 文件 | 含义 |
|---|---|
| `scenario_snapshot.json` | 实际种子、环境、灾害、任务及随机事件 |
| `events.jsonl` | 全部运行事件和状态变化 |
| `summary.json` | 本轮最终任务、车辆、风险、工单和指标 |
| `agent_decisions.jsonl` | 多 Agent 协同和调度理由 |
| `experience_dataset.jsonl` | 可用于模仿学习/离线训练的经验样本 |
| `acceptance_report.json` | 保守的自动功能验收报告 |

经验样本包含：

- 场景与种子；
- 任务类型、区域、优先级和能力要求；
- 风险状态和指标；
- 选择的车辆；
- 执行结果和起止 tick；
- 显式奖励；
- 是否适合作为模仿样本。

奖励公式当前为：

```text
completed = 1.0
cancelled = -0.25
timed_out = -1.0
otherwise = 0.0
```

---

## 9. 已有关键证据

### 9.1 CARLA 比赛综合闭环正式通过

```text
/home/hjy/open_pit_agent_demo/artifacts/runs/
town03-competition-demo-v1-20260801T125018Z-e322e748
```

结果：

| 指标 | 结果 |
|---|---:|
| 状态 | PASS |
| 任务 | 5/5 |
| 工单 | 4/4 |
| 故障注入 | 成功 |
| 风险 | 蓝、黄、橙、红 |
| 道路限制 | 1 |
| 自动结束 | Tick 1785 |
| 终态稳定 | 87 Tick |

注意：这次正式 CARLA 证据生成于最新解释/学习/自动验收层加入之前，因此没有新的三个数据文件。

### 9.2 最新解释、经验和验收层 Mock 通过

```text
/home/hjy/open_pit_agent_demo/artifacts/runs/
town03-competition-demo-v1-20260801T130529Z-d16356ec
```

结果：

- Mock 功能验收：PASS；
- 风险序列：蓝、黄、橙、红；
- 协同决策记录：6条；
- 经验样本：5条；
- 自动验收：9/9；
- Mock 不执行真实车辆运动，因此任务状态不是 CARLA 完成证据。

### 9.3 其他里程碑证据

更多正式证据和设计说明见：

- `docs/01_town03_adapter_milestone.md`
- `docs/02_task_lifecycle_and_fault_recovery.md`
- `docs/03_synthetic_slope_risk_response.md`
- `docs/04_risk_work_order_lifecycle.md`
- `docs/05_red_risk_multi_action_response.md`
- `docs/06_realtime_visualization_service.md`
- `docs/07_dashboard_scenario_control.md`
- `docs/08_operational_map_and_risk_trend.md`
- `docs/09_desktop_application_window.md`
- `docs/10_one_command_demo_launcher.md`
- `docs/11_long_patrol_and_spectator_camera.md`
- `docs/12_reproducible_scenario_variables.md`
- `docs/13_competition_integrated_demo.md`
- `docs/14_decision_learning_acceptance.md`

---

## 10. 关键代码和配置地图

| 路径 | 职责 |
|---|---|
| `start_demo.sh` | CARLA和态势台一键启动入口 |
| `start_desktop.sh` | 只启动独立态势台窗口 |
| `run_demo.py` | Demo CLI入口 |
| `src/open_pit_agent/cli.py` | Mock/CARLA主运行闭环 |
| `src/open_pit_agent/adapters/carla_adapter.py` | CARLA连接、车辆、导航、任务和相机 |
| `src/open_pit_agent/scheduler.py` | 能力、距离、负载和经验偏好调度 |
| `src/open_pit_agent/risk.py` | 透明风险规则和风险任务 |
| `src/open_pit_agent/decision_intelligence.py` | 风险解释、Agent决策、经验记忆和验收 |
| `src/open_pit_agent/work_order.py` | 工单生命周期 |
| `src/open_pit_agent/restrictions.py` | 策略层道路限制 |
| `src/open_pit_agent/scenario_runtime.py` | 固定/随机种子和场景快照 |
| `src/open_pit_agent/dashboard.py` | 可视化状态投影 |
| `src/open_pit_agent/control.py` | 窗口内白名单场景控制 |
| `dashboard/` | 当前已冻结的界面 |
| `scripts/check_latest_run.py` | 中文验收结果读取 |
| `check_demo_result.sh` | 结果检查入口 |
| `configs/town03_competition_demo.json` | 比赛综合车辆、区域、环境、故障和任务 |
| `configs/risk_slope_competition_synthetic.json` | 风险阈值、观测时序、动作和道路限制 |

---

## 11. 常见调整入口

### 修改故障车辆、故障时间、环境或随机种子

编辑：

```text
configs/town03_competition_demo.json
```

主要字段：

```text
scenario_variables.environment
scenario_variables.disaster
scenario_variables.mission
scenario_variables.randomization
demo.failure_vehicle_id
demo.failure_tick
```

影响：

- 改固定种子会影响随机事件复现；
- 改故障时间会改变任务接管时机；
- 改环境变量只会影响当前解释与场景记录，尚不代表真实物理高温或强风模拟。

### 修改蓝黄橙红演化时间或数值

编辑：

```text
configs/risk_slope_competition_synthetic.json
```

主要字段：

```text
thresholds
observations[*].tick
observations[*].fixed_station
observations[*].mobile_equipment
action
additional_actions
restriction
```

影响：

- 改 Tick 会改变演示节奏；
- 改阈值或观测值会改变风险等级；
- 所有阈值仍必须标注为合成 Demo 数据。

### 修改车辆、任务区和能力

编辑：

```text
configs/town03_competition_demo.json
```

主要字段：

```text
vehicles
zones
```

必须保证至少存在满足每项 `required_capabilities` 的健康车辆，否则调度器会拒绝任务。

### 修改模仿学习影响强度

位置：

```text
src/open_pit_agent/decision_intelligence.py
MAX_IMITATION_BONUS_M = 2.0
```

不要为了表现“智能”随意增大。该值现在故意很小，确保能力、健康、安全、负载和距离规则仍是主要决策依据。

---

## 12. 下一步优先级

### P0：立即完成

1. 关闭旧态势台并重新运行 `./start_demo.sh`；
2. 启动“比赛一键综合演示（推荐）”；
3. 确认风险解释在窗口显示；
4. 场景结束后运行 `./check_demo_result.sh`；
5. 保存新 CARLA `PASS` 目录；
6. 更新本说明书的正式证据路径。

### P1：纸质材料

在 P0 通过后，立即从稳定运行中准备：

- 总体架构图；
- 运行流程图；
- CARLA三车协同截图；
- 蓝/黄/橙/红风险趋势截图；
- 红色风险影响、预防和规划动作截图；
- 故障接管截图；
- 任务5/5、工单4/4截图；
- `acceptance_report.json` 的关键指标；
- 作品设计报告；
- 测试报告；
- 总结报告；
- 使用说明。

纸质材料中应把能力分成：

```text
已完成并验证
已完成待矿山回归
后续工程化计划
```

### P2：Demo增强（根据剩余时间选择）

- 真实路网边级封路与绕行；
- 场景文件增加低电量、传感器退化、通信中断等随机事件；
- 固定站与移动装备数据融合权重；
- 运行结果自动生成 Markdown 测试报告；
- 本地 SQLite 工单和经验持久化；
- 打包与换电脑部署检查。

### P3：8月15日后正式目标

- 矿山地图和矿用装备资产；
- 真实/典型矿山数据标定；
- 正式 LLM Agent 工具调用；
- MFS-LLM/论文调度算法；
- 训练与基线对照实验；
- ROS/工程接口；
- 工业性试验、部署包、图纸和 BOM。

---

## 13. 接手 GPT 的推荐开场指令

用户可以在新对话中发送：

```text
请先完整阅读：
/home/hjy/open_pit_agent_demo/项目持续交接说明书_GPT必读.md

再检查实际项目文件，不要把计划当成已完成能力。
继续操作前，请说明：
1. 你准备做什么；
2. 为什么要做；
3. 对后续比赛材料和矿山迁移有什么影响。

当前首先帮助我完成说明书中 P0 的新 CARLA 综合回归。
```

如果已经完成 P0，可把最后一句改为：

```text
当前首先读取最新CARLA运行证据，并帮助我准备纸质作品设计报告和测试报告。
```

---

## 14. 外部原始材料

原榜题 PDF：

```text
/home/hjy/下载/第二批榜题方案汇总材料/湖南长沙市-高端装备领域14个/
CS-202616山东泽明能源科技有限公司-Agent・具身智能集群矿山安全高效成套装备创新应用解决方案比赛方案.pdf
```

早期讨论上下文：

```text
/home/hjy/下载/open_pit_agent_project_context.md
```

早期上下文记录的是设计与计划。本说明书记录截至 2026-08-01 的实际工程状态；若两者表述冲突，应先检查当前代码和运行证据。

---

## 15. 变更记录

### 2026-08-01

- 建立 Town03 外部第三方调度工作区；
- 完成适配器、调度、故障接管、风险、工单、道路限制和可视化；
- 完成一键 CARLA 与独立态势台启动；
- 完成比赛综合场景 CARLA `5/5`、工单 `4/4` 正式验收；
- 增加环境、灾害、任务和随机种子场景文件；
- 增加风险影响、预防措施和环境注意项；
- 增加多 Agent 决策、经验数据、有界模仿记忆和自动验收；
- 38项自动测试通过；
- 最新智能层完成 Mock 验收，等待用户执行新 CARLA 回归。

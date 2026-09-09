# 项目状态

更新日期：2026-09-07。此文件只记录当前真实状态，不以规划代替已完成能力。

## 已完成且保留

- S08 三车边坡渐进失稳 Golden Demo：监测、风险研判、人工确认、任务接管、CARLA 执行、反馈和证据记录链路。
- S01–S09：统一 ScenarioSpec、运行入口和结果契约；S01–S07/S09进入统一事件管线，S08当前由Golden兼容适配器执行。
- 调度中心场景控制：Agent API 已提供 S01–S09 目录、启动、状态和停止接口，PyQt 从 API 动态读取场景、车辆数、Seed、Policy 和 Mode。
- 结构化可视化：S01–S07/S09 已按统一阶段同步到 RuntimeState，可在 PyQt 显示 6/8 车、地图资源坐标、CARLA 静态拓扑底图、事件、决策、任务结果和闭环状态；不宣称 CARLA 物理执行。
- 统一运行数据：每轮输出 ScenarioSpec、ConcreteEpisode、WorldState、ScenarioEvent、DecisionRecord、TaskResult、RoutePlan、Metrics 和闭环验证，并保存 `scenario_contract.json` 与 SQLite Episode 索引。
- 调度策略：Heuristic Baseline V0 与 MultiObjective Cost Model V1；安全条件先作为硬约束过滤，再对合法候选排序。
- 数据分层：`map_resources.db` 保存静态地图事实，`openpit.db` 保存运行事实，`artifacts/` 保存本地证据。
- 地图资源：0325_5 已读取 82 个出生点、118 个拓扑节点、130 条拓扑边；P5 与 P6 独立记录。
- 自动验证：206 项单元测试、Python 编译和正式 Shell 语法检查通过。

## 当前真实验证状态

| 项目 | 状态 | 说明 |
|---|---|---|
| P3 单车出生点标定 | 已完成 | 82 个出生点已记录为可生成矿卡的点 |
| P4 静态冲突候选 | 已完成 | 仅为候选冲突，不等同真实碰撞结论 |
| P5 路线规划可达性 | 部分完成 | 仅表示 GlobalRoutePlanner 可生成路线 |
| P6 单车物理路线 | 部分完成 | 当前有 13 条 `PHYSICAL_REACHED`，6 条 `PHYSICAL_STUCK` |
| S01 CARLA 多车 | 准入中 | 需基于 P6 池验证完整任务集 |
| S02 结构化故障接管 Episode | 已完成 | 统一生成器先构造 `A→T / B→D / B→T` 接管三元组，再补齐其余车辆任务；失败时明确拒绝 |
| S02 CARLA 故障接管 | 准入完成，待CARLA联机预检 | 已可由 P6 成功路线生成六车接管三元组；仍未证明多车会车、碰撞或净空安全 |
| S07 CARLA 道路封闭 | 结构化完成 | 多车物理改道仍需 P6 路线与道路边覆盖 |
| 多车会车/碰撞/净空 | 未验证 | 不得宣称已经完成 |

## 当前关键阻塞

S02 不能只要求“有足够多的成功路线”，还必须存在“故障车辆任务目标 + 另一能力匹配车辆到同一目标”的接管组合。该约束已由 Episode Generator 在生成阶段执行；CARLA 模式再以 P6 单车成功路线收窄候选集，并保留每条路线原始 P5 状态，不满足时返回 `NO_TAKEOVER_CANDIDATE`。

## 下一阶段

1. 用统一 Resource Admission 判断场景、车辆数、Seed、模式是否可运行，并将 S02 P6 接管三元组纳入 CARLA 预检。
2. 再将场景选择、车辆数、Seed、Policy 和 Mode 接入 FastAPI 与 PyQt。
3. 在资源准入稳定后做 CARLA 多车运行与冲突验证；不能以现有 P6 单车结果替代该验证。

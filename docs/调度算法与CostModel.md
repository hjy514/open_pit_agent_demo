# 调度算法与 Cost Model

## 决策链

```text
World State → Hard Constraints → Feasible Candidates → Policy
          → Candidate Ranking / Decision → Safety Shield → Execution
```

安全条件不是普通罚分。故障、不可用、能力不匹配、目标不可达、道路关闭和 RED 风险禁入必须在候选集合阶段直接排除。

## Heuristic Baseline V0

`scheduler.py` 保留比赛演示和回归比较所需的规则调度器。其基础排序参考路线距离、任务优先级和当前负载；它是 Baseline 和后续学习策略的 Teacher，不是最终论文级目标函数。

## MultiObjective V1

`decision/cost_model.py` 与 `optimization_scheduler.py` 为合法候选计算归一化多目标代价。当前可靠或可明确标注为代理的项目包括：

- 路线长度 / ETA / 运输代价；
- 等待、任务延误和车辆负载；
- 故障恢复时间与任务接管切换代价；
- 每个候选的硬约束结果、子代价和最终选择原因。

总形式为：

```text
J = Σ wk × Ĵk
```

所有子目标先归一化；权重来自配置，当前不声称为最优权重。没有可信数据来源的产量、铲装设备空闲、能耗和燃油指标必须记录为 `NOT_AVAILABLE` 或 `SURROGATE_ONLY`。

## 策略版本路线

```text
Heuristic V0 → MultiObjective V1 → Global Optimization → BC → PPO / MARL
```

当前正式策略为 V0 和 V1。BC 相关代码仅保留训练数据接口；在随机 Episode、CARLA 闭环、数据库记录与固定 Seed A/B 实验稳定前，不扩大 BC/PPO/MARL 实现。

## 可解释性与记录

每个决策应记录到 `openpit.db` 或对应 evidence：候选车辆、硬约束结果、路线事实、各项 Cost、总 Cost、选中车辆、策略版本和 Safety Shield 结果。这样可以回答“为什么选择该车辆”，也可以用于后续固定 Seed 的 V0/V1 对比。

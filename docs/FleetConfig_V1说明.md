# FleetConfig V1：多车辆场景配置基础

## 目的

`FleetConfig` 为露天矿多场景系统提供统一的车队规模描述。它只描述本次
场景配置预期的车队规模和初始运行容量；不在本版本自动生成车辆、随机分配
角色或改变调度器行为。

这样现有三车边坡场景仍保持原样，同时后续六车及以上场景可以采用同一格式。

## 兼容规则

旧配置不含 `fleet` 时，系统自动根据 `vehicles` 生成固定车队：

- `total_vehicles`、`available_vehicles`、`active_vehicles`、`traffic_vehicles`
  均等于配置车辆数；
- `role_policy` 为 `fixed`；
- `task_load` 与 `traffic_density` 为 `legacy`。

因此当前三车正式边坡 Demo 的业务行为不会因为本项改动而变化。

## V1 配置格式

```json
"fleet": {
  "total_vehicles": 6,
  "available_vehicles": 5,
  "active_vehicles": 4,
  "traffic_vehicles": 3,
  "role_policy": "randomized",
  "task_load": "medium",
  "traffic_density": "medium",
  "role_counts": {
    "production": 4,
    "inspection": 2
  }
}
```

字段含义：

- `total_vehicles`：本次配置的总车辆数；V1 必须与 `vehicles` 列表长度一致。
- `available_vehicles`：初始可参与任务分派的车辆数。
- `active_vehicles`：初始正在执行任务或生产活动的车辆数。
- `traffic_vehicles`：初始进入道路网络的车辆数。
- `role_policy`：`fixed` 或 `randomized`。V1 仅保存声明；实际随机角色将在
  Concrete Episode 生成阶段实现。
- `task_load`、`traffic_density`：`low`、`medium`、`high`，供后续任务和事件
  生成器读取。
- `role_counts`：角色目标数量，不可超过总车辆数。

## 本版本边界

FleetConfig V1 不会：

- 自动从 3 辆扩展到 6 辆；
- 自动抽取可用车辆或随机角色；
- 修改 CARLA Actor 创建、候选评分、路线规划或 UI；
- 修改现有三车边坡回归场景。

下一阶段的 Concrete Episode Resolver 才会根据场景、Seed、FleetConfig、任务
负载和事件约束，生成实际的车辆角色、初始状态、任务及事件参数。

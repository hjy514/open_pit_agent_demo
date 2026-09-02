# Concrete Episode V1：场景实例生成基础

## 作用

`ConcreteEpisode` 将一个场景配置解析为某次实际运行的、可复现的输入快照：

```text
ScenarioConfig + run_id + seed -> ConcreteEpisode
```

快照包含车队容量、车辆初始角色和运行状态、初始任务、配置故障事件及随机种子。
它不控制 CARLA，也不会改变当前三车边坡 Demo 的执行逻辑。

## 随机性与可复现性

- 同一配置和同一 `seed` 必须得到相同的可用车辆、活跃车辆、道路车辆及随机角色；
- 不同 `seed` 可以得到受 FleetConfig 约束的不同组合；
- `run_id` 用于标识一次运行，不作为随机源。

## V1 边界

本版本只生成数据对象，不会：

- 自动新增 CARLA Actor；
- 随机选择故障车辆或故障时刻；
- 根据任务负载新增任务；
- 将 Episode 写入 SQLite；
- 替换现有 CLI 和三车边坡运行流程。

这些能力将在下一步把 Episode 接入 SQLite V2 和六车 S01 正常生产场景时逐项实现。

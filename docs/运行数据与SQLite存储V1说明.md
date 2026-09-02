# 运行数据与 SQLite 存储 V1 说明

## 目的

系统在不改变现有 CARLA、风险、调度或任务接管行为的前提下，为每次 Demo 运行建立结构化数据索引。

原有 JSON/JSONL 运行证据继续保留；SQLite 仅作为旁路、可查询的结构化索引，不参与任何实时安全控制。

## 目录

```text
artifacts/runs/<run_id>/       # 原有、不可变的运行证据
data/database/openpit.db       # 本地 SQLite 结构化索引
data/logs/                     # 预留：系统日志
data/runs/                     # 预留：未来统一运行目录
data/datasets/                 # 预留：训练 transition
data/models/                   # 预留：模型 artifact
```

`data/` 下的运行数据已加入 `.gitignore`，不会被误提交到源码仓库。

## 当前保存内容

| SQLite 表 | 保存内容 |
|---|---|
| `scenario_runs` | run、场景、seed、模式、状态、最终 summary |
| `events` | 所有 `EvidenceRecorder.record()` 写入的事件 |
| `risk_assessments` | `risk_assessed` 事件中的等级、区域、理由、指标 |
| `decisions` | `agent_decision` 中的候选任务、车辆、评分和策略版本 |
| `tasks` | summary 中的任务最终快照 |
| `task_transitions` | 已记录的任务状态类事件 |
| `work_orders` | summary 中的工单最终快照 |
| `metrics` | summary/result 中可直接计算的数值指标 |
| `monitoring_batches` | 监测 JSONL 文件及其记录数索引 |
| `artifacts` | JSON、JSONL、summary 等证据文件路径索引 |

监测原始记录、相机图片、视频和未来高频 CARLA 数据仍应保存在文件中；SQLite 只保存索引和业务结构化信息。

## 使用方式

按原方式运行即可，无需增加启动参数。例如：

```bash
cd /home/xiaoa/矿山调度/open_pit_agent_demo
./start_slope_demo.sh
```

运行结束后数据库位于：

```text
/home/xiaoa/矿山调度/open_pit_agent_demo/data/database/openpit.db
```

可使用 Python 标准库进行只读查询：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/database/openpit.db'); print(c.execute('select run_id, scenario_id, status from scenario_runs order by started_at desc limit 5').fetchall())"
```

查看某次运行的事件数量：

```bash
python -c "import sqlite3; c=sqlite3.connect('data/database/openpit.db'); print(c.execute('select run_id, count(*) from events group by run_id order by max(event_id) desc limit 5').fetchall())"
```

## 可靠性边界

- SQLite 写入失败时，系统会发出告警，但继续写原 JSON/JSONL，不会干扰 CARLA 安全停车或任务执行；
- 数据库不替代当前 `artifacts/runs` 证据；
- 后续 schema 扩展应通过 migration 增加表/字段，不能覆盖历史 run；
- 后续需要接入的内容包括：闭环验证记录、路线计划、RoadState、标准 transition、模型版本和统一 KPI。

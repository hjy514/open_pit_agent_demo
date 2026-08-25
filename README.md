# 露天矿 Agent 装备集群 Demo

本仓库用于 `CS-202616` 赛题的第三方调度平台与 CARLA 可行性验证。

## 新电脑快速配置

本项目可与 `open_pit_dispatch_app` 分别克隆到任意位置。默认情况下，
运行结果会导出到 `~/open_pit_dispatch_app/data`；如果调度应用不在该位置，
可通过 `OPENPIT_DISPATCH_DATA_DIR` 指定数据目录。

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

另开终端运行 Mock 演示或完整演示：

```bash
./start_demo.sh --check-only
python run_demo.py --mode mock --inject-failure
```

本文后面的 `/home/hjy/...` 命令保留了原始验证环境记录；在其他电脑上
优先使用上述环境变量和仓库内的便携启动脚本。CARLA 本体、矿山地图、
真实坐标与生产数据不随本仓库上传。

新会话中的 GPT/Codex 应先完整阅读：

```text
项目持续交接说明书_GPT必读.md
```

该文件集中记录当前真实完成状态、验证边界、操作方法、证据目录和下一步优先级。

当前阶段先在 CARLA 自带 `Town03` 地图中使用三辆普通汽车，验证车辆发现、状态读取、能力约束任务分配、目标点下发、到达验收、故障抢占和任务恢复。后续切换到露天矿地图时，保留核心业务代码，只替换场景配置、地图路网和仿真资产。

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
- 风险影响、预防措施、环境注意项和规划动作的可解释输出；
- 感知、评估、调度和执行智能体之间的结构化决策记录；
- 每次运行导出可供模仿学习/离线训练的状态—动作—结果经验集；
- 后续运行读取同场景成功经验，形成不越过能力与安全约束的有界模仿偏好；
- 固定与随机种子场景文件，以及运行级场景快照；
- 每次运行生成事件日志、测试摘要和保守的自动功能验收报告。

当前尚未接入真实矿山监测数据或经验证的风险模型。道路限制目前是策略层代理约束，尚未接入 CARLA 路网边级绕行；经验数据已经导出，但尚未训练或部署学习模型；真实人工审批、多渠道通知、数据库服务和大模型 Agent 也未实现。这些模块将在现有闭环基线上依次接入。

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
cd /home/hjy/桌面/carla0.9.10_package/CARLA_0.9.10-dirty
./CarlaUE4.sh -quality-level=Low
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

## 8. 一键启动完整Demo

首选使用方式：

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

## 9. 仅启动独立桌面窗口

推荐使用方式：

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

当前共有 38 项测试，覆盖配置、场景种子复现、综合场景白名单、调度、故障、控制器生命周期、任务抢占与恢复、共享车道路侧待命、显式到达、终态驻车、观察相机优先级跟随、风险等级、风险影响与预防解释、经验数据、有界模仿记忆、自动验收、风险任务追溯、红色风险多动作、道路限制、完整工单状态迁移、实时状态投影、CARLA车道折线转换、运行目录安全、桌面窗口参数、CARLA一键启动命令、非法跳步拦截和超时升级。

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

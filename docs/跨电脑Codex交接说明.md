# Open-Pit Agent Demo：跨电脑 Codex 交接说明

本文档用于将项目交给另一台 Ubuntu 电脑上的 Codex 继续实施。

## 1. 仓库与基线

- GitHub：`https://github.com/hjy514/open_pit_agent_demo.git`
- 当前主分支：`main`
- 已上传基线提交：`140e4c6`
- 单仓库根目录：`open_pit_agent_demo/`
- 桌面调度端目录：`open_pit_dispatch_app/`
- 原来的独立桌面端目录可以保留作回滚参考，但正式开发使用单仓库内的子目录。

首次接手后执行：

```bash
git clone https://github.com/hjy514/open_pit_agent_demo.git
cd open_pit_agent_demo
git status
```

## 2. 不可改变的技术约束

- Python 3.7；
- CARLA 0.9.10；
- 现有三车 Slope/Competition Golden Demo 必须保留；
- S01、S02、S07 六车结构化场景不能替换三车基线；
- 地图资源库与运行证据库保持分离；
- 未经真实 CARLA 运行，不得将静态 XODR、Mock 或候选路线称为已验证路线；
- 未经重型矿卡实测，不得声称路线满足重型矿卡安全通行要求。

## 3. 当前已完成内容

### Map Resource Library Phase 2

- OpenDRIVE/XODR 无 CARLA 依赖解析；
- 道路、节点、边、车道元数据、路口和车道连接导入；
- `map_resources.db` 独立数据库；
- `route_candidates` 候选路线表；
- 路线候选幂等写入接口；
- 路线候选 JSON 导入工具：

```bash
python scripts/import_route_candidates.py route_candidates.json \
  --database data/database/map_resources.db
```

### 场景与车辆模型

- Logical Scenario / Event Engine 基础；
- Fleet / Episode 六车模型；
- S01 六车正常生产结构化 Mock；
- S02 六车车辆故障、任务释放和重分配；
- S07 道路封闭、受影响任务识别和选择性重规划；
- S07 支持从 `route_candidates` 读取路线边序列。

### 桌面端

- PyQt6 调度端已纳入 `open_pit_dispatch_app/`；
- 后端根目录启动脚本：`start_api.sh`；
- 桌面端根目录包装启动脚本：`start_dispatch_app.sh`；
- 桌面端自身启动脚本：`open_pit_dispatch_app/start_app.sh`。

## 4. 当前验证状态

在没有 CARLA 的电脑上已完成：

```text
91 项 Python 单元测试：全部通过
桌面端 Python 语法检查：通过
```

尚未完成的真实验证：

1. CARLA 是否成功加载 `0325_5` 地图；
2. CARLA GlobalRoutePlanner 实际路线采集；
3. 起终点与 `map_points` 的真实对应；
4. 路线实际行驶、碰撞和卡死检查；
5. 重型矿卡转弯半径、道路宽度和净空检查；
6. 道路封闭后的真实选择性重规划；
7. Golden Demo 回归运行。

## 5. Ubuntu 环境准备

建议使用 Python 3.7 虚拟环境：

```bash
cd open_pit_agent_demo
python3.7 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

设置 CARLA 路径：

```bash
export OPENPIT_PYTHON="$PWD/.venv/bin/python"
export OPENPIT_CARLA_ROOT="/实际路径/CARLA_0.9.10"
```

先执行离线回归：

```bash
python -m unittest discover -s tests
```

## 6. CARLA 验证顺序

不要直接修改 Golden Demo。建议按以下顺序进行：

1. 启动 CARLA Server；
2. 使用 `scripts/capture_carla_routes.py` 采集 GlobalRoutePlanner 路线：

```bash
python scripts/capture_carla_routes.py route_endpoints.json \
  --map-id 0325_5 \
  --resource-version 1.0-draft \
  --expected-map-name 0325_5 \
  --output route_candidates.carla.json
```

3. 检查输出中的起终点、路点数量、路线长度和路口数量；
4. 确认对应 `map_points` 已存在后，再导入 `map_resources.db`；
5. 运行 S07 结构化和 CARLA 场景；
6. 记录失败路线、碰撞、阻塞和净空问题；
7. 修正资源库或场景配置后重复验证；
8. 最后回归三车 Golden Demo。

路线采集脚本只生成 `CARLA_CAPTURED_UNVERIFIED` 候选记录，不能直接改成 `VERIFIED`。

## 7. 交接时必须向用户报告的内容

每次真实 CARLA 验证后，应报告：

- 使用的 CARLA 版本和地图名；
- 使用的资源库版本；
- 采集或验证了哪些路线；
- 哪些路线成功、失败或需要人工复核；
- 是否出现碰撞、卡死、路线越界或净空问题；
- 是否影响三车 Golden Demo；
- 相关日志、JSON 证据和数据库路径。

如果当前电脑没有 CARLA，只能继续完善代码、配置、数据库工具和 Mock 测试，并明确说明“尚未进行 CARLA 验证”。

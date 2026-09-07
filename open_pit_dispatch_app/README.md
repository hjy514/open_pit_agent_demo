# OpenPit 调度中心

本目录是 OpenPit-Agent 的 PyQt6 调度中心子模块，不单独保存场景、调度或风险业务逻辑。场景生成、决策和数据库均由根目录 Agent 后端负责，界面通过 API 读取状态并发送人工命令。

从项目根目录启动：

```bash
./start_api.sh
./start_dispatch_app.sh
```

需要 PyQt6 的独立环境时，`start_dispatch_app.sh` 会依次查找：

1. `OPENPIT_DISPATCH_PYTHON`；
2. `~/miniconda3/envs/openpit-ui/bin/python`；
3. `open_pit_dispatch_app/.venv/bin/python`。

正式启动流程、CARLA、场景和数据库说明见根目录 [README](../README.md) 与 [技术交接说明](../项目技术交接与运行说明.md)。

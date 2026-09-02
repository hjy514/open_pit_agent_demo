# 露天矿调度桌面应用

该 PyQt6 应用是 `open_pit_agent_demo` 的桌面调度界面，通过本机
`http://127.0.0.1:8000` API 获取车辆、任务、地图、风险和事件状态。
当前已纳入后端仓库，作为同一 Git 仓库中的 `open_pit_dispatch_app/` 子目录。

## 新电脑安装

项目现在只需克隆一个仓库：

```text
~
└── open_pit_agent_demo
    └── open_pit_dispatch_app
```

为桌面应用创建独立 Python 环境：

```bash
cd ~/open_pit_agent_demo/open_pit_dispatch_app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 启动顺序

终端一，启动 Agent API：

```bash
cd ~/open_pit_agent_demo
./start_api.sh
```

终端二，启动桌面应用：

```bash
cd ~/open_pit_agent_demo/open_pit_dispatch_app
./start_app.sh
```

也可以从仓库根目录执行 `./start_dispatch_app.sh`。

终端三，按需启动 Mock 或 CARLA 场景：

```bash
cd ~/open_pit_agent_demo
python run_demo.py --mode mock --inject-failure
```

如不使用仓库内 `.venv`，可指定解释器：

```bash
OPENPIT_DISPATCH_PYTHON=/实际路径/python ./start_app.sh
```

## 数据与安全

`data/*.json` 是运行时快照，不提交到 GitHub。真实矿山地图、坐标、生产
记录和企业凭据也不应放入本仓库；应使用本机配置或经授权的专用存储。

原独立仓库中的 `backup/` 目录未纳入合并仓库；如需历史参考，仍保留在原目录。
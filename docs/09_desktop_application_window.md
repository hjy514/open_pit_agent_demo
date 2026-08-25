# 里程碑09：独立桌面应用窗口

日期：2026-08-01

## 1. 用户使用方式

```bash
cd /home/hjy/open_pit_agent_demo
./start_desktop.sh
```

运行脚本后直接弹出独立的“集群安全态势台”窗口。窗口没有浏览器地址栏、标签页和前进/后退按钮。

关闭窗口后，启动器自动停止本次启动的数据服务。如果窗口内仍有Demo运行，数据服务会先请求安全中断Demo并释放CARLA控制器。

## 2. 为什么保留现有界面技术

电脑当前具备PyQt5，但没有 `QtWebEngineWidgets`；系统已安装Google Chrome。

启动器使用Chrome应用窗口模式承载现有界面。对用户而言是独立软件窗口，对工程而言继续复用：

- Town03和后续矿山地图；
- 车辆轨迹；
- 风险趋势；
- 任务和工单表格；
- 场景控制；
- 实时与历史运行数据。

如果改为纯PyQt原生控件，需要重写地图、图表和界面组件，但不会增加当前Demo的业务验证能力。

## 3. 启动生命周期

```text
start_desktop.sh
      ↓
查找Google Chrome/Chromium
      ↓
选择127.0.0.1空闲端口
      ↓
启动dashboard_server.py
      ↓
健康检查通过
      ↓
Chrome --app 独立窗口
      ↓
用户关闭窗口
      ↓
安全停止服务并删除临时应用配置
```

每次使用独立临时浏览器配置，避免窗口被合并到用户已有的Chrome进程。

## 4. 可用参数

普通窗口：

```bash
./start_desktop.sh
```

比赛展示全屏：

```bash
./start_desktop.sh --fullscreen
```

不弹窗自检：

```bash
./start_desktop.sh --check-only
```

诊断日志：

```text
artifacts/control/*-desktop-window.log
```

## 5. 验证结果

启动器自检已验证：

- 能找到 `/usr/bin/google-chrome`；
- 能分配本机空闲端口；
- 能启动并访问健康接口；
- 自检结束后自动停止服务。

实际弹窗验证已确认窗口成功加载：

- 首页、样式和交互脚本；
- 场景控制白名单；
- 历史运行选择；
- Town03地图接口；
- 最新红色风险联动状态。

## 6. 当前边界

启动窗口不等于启动CARLA主程序。当前推荐流程：

1. 启动CARLA；
2. 运行 `./start_desktop.sh`；
3. 在窗口中选择并启动场景。

下一阶段可以增加统一启动脚本，检查CARLA是否运行，并在用户确认后启动CARLA主程序。

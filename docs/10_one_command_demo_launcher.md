# 里程碑10：CARLA与桌面窗口一键启动

日期：2026-08-01

## 1. 首选启动命令

```bash
cd /home/hjy/open_pit_agent_demo
./start_demo.sh
```

该命令完成CARLA就绪检查和独立态势台窗口启动。

## 2. 两种CARLA分支

### 2.1 CARLA已经运行

启动器通过CARLA Python API读取：

- 主机和端口；
- 当前世界；
- 当前地图名称。

API可用时直接复用现有CARLA，不启动新实例，也不获得该进程的关闭权限。

### 2.2 CARLA尚未运行

启动器固定执行：

```text
/home/hjy/桌面/carla0.9.10_package/CARLA_0.9.10-dirty/CarlaUE4.sh
-quality-level=Low
```

CARLA使用独立进程组启动，日志写入：

```text
artifacts/control/*-carla-runtime.log
```

启动器等待CARLA API就绪，默认上限120秒。

## 3. 进程所有权

| CARLA来源 | 窗口关闭后的行为 |
|---|---|
| 用户原先已经启动 | 保持运行 |
| `start_demo.sh`启动 | 默认停止 |
| 脚本启动且使用`--keep-carla` | 保持运行 |

清理目标始终是本次启动器记录的明确进程组，不根据模糊进程名批量终止。

## 4. 可用参数

全屏演示：

```bash
./start_demo.sh --fullscreen
```

环境自检：

```bash
./start_demo.sh --check-only
```

保留脚本启动的CARLA：

```bash
./start_demo.sh --keep-carla
```

修改CARLA等待上限：

```bash
./start_demo.sh --carla-timeout 180
```

## 5. 实际验证

自检结果：

```text
桌面窗口引擎：/usr/bin/google-chrome
CARLA已就绪：127.0.0.1:2000，地图=Town03
```

一键启动验证：

- 正确识别现有CARLA；
- 输出“检测到现有CARLA，正在复用”；
- 未创建第二个CARLA进程；
- 成功弹出独立态势台窗口；
- 桌面服务使用动态本机端口；
- 技术日志写入 `artifacts/control/`。

## 6. 比赛现场推荐流程

正式展示前先运行：

```bash
./start_demo.sh --check-only
```

确认后使用：

```bash
./start_demo.sh --fullscreen
```

进入窗口后选择红色风险多装备联动场景并启动。演示结束后关闭窗口；如果CARLA是比赛前手动启动的，它会继续运行，便于再次演示。

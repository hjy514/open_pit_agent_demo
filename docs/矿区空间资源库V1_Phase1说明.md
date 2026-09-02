# 0325_5 矿区空间资源库 V1：Phase 1

## 目的

本阶段只建立地图长期资源的独立 SQLite 数据库骨架。它不连接 CARLA，不导入 XODR，不生成车辆，也不改变既有边坡失稳 Demo 的配置、调度与执行结果。

运行数据库 `data/database/openpit.db` 继续记录每次仿真；新建的 `data/database/map_resources.db` 只记录地图和标定成果。两个数据库不得混用。

## 当前数据可信边界

首次运行建库脚本后，只有地图元数据与 `1.0-draft` 资源版本存在。`map_points`、可达性、冲突、路网和风险区域表均为空，这是正确状态：任何点都尚未被标定为重型矿卡可用点。

## 建库命令

在 `open_pit_agent_demo` 目录执行：

```bash
$OPENPIT_PYTHON scripts/build_map_resource_db.py
```

若要将本机 XODR 文件路径和内容 Hash 写入元数据：

```bash
$OPENPIT_PYTHON scripts/build_map_resource_db.py \
  --xodr-path /实际路径/0325_5.xodr
```

重复执行是安全的：不会重复创建 `0325_5` 或 `1.0-draft` 元数据记录。

## 下一阶段

Phase 2 才解析 XODR，写入静态道路/车道/路口信息，并统一标记为 `SOURCE_XODR`；仍不能把它们称为 CARLA 或矿卡实测结果。

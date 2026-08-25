# 里程碑04：风险复核应急工单闭环

日期：2026-08-01

## 1. 本阶段目标

在“合成边坡风险—动态复核任务—CARLA执行”的基础上增加可审计工单，使一个橙色风险具备完整业务责任链：

```text
橙色风险评估
      ↓
创建复核任务 + 待处理工单
      ↓
调度器分配车辆：已派发
      ↓
CARLA开始任务：执行中
      ↓
车辆到达复核区：待复核
      ↓
Demo规则复核：已关闭
```

该能力对应赛题目标2中的“工单闭环追踪”，并连接目标3的风险预警结果。

## 2. 工单关联关系

每张工单保存：

- 工单ID；
- 工单类型；
- 来源风险事件ID；
- 风险等级；
- 风险区域；
- 关联任务ID；
- 分配车辆ID；
- 创建、分配、开始、任务完成、复核和关闭tick；
- 复核结果；
- 每次状态迁移的执行者、原因和时间戳。

当前工单ID：

```text
wo-synthetic-east-slope-escalation-v1-sample-080
```

它关联：

```text
risk_event_id:
  synthetic-east-slope-escalation-v1-sample-080

task_id:
  risk-review-synthetic-east-slope-escalation-v1-sample-080
```

因此可以从工单追溯风险观测、规则评估、任务、车辆和执行结果。

## 3. 状态机

正常路径：

```text
pending
  → assigned
  → in_progress
  → pending_review
  → closed
```

异常路径：

```text
in_progress
  → escalated
```

如果关联任务超时，工单进入 `escalated`，不会被自动复核关闭。状态机拒绝非法跳步，例如 `pending → in_progress`。

## 4. 状态由谁驱动

| 状态 | 触发来源 | 当前执行者 |
|---|---|---|
| pending | 橙色风险生成任务 | risk_engine |
| assigned | 调度器选定车辆 | scheduler |
| in_progress | CARLA发出task_started | carla_adapter |
| pending_review | CARLA发出task_completed | carla_adapter |
| closed | 等待配置延迟后通过Demo复核 | demo_auto_reviewer |
| escalated | CARLA发出task_timed_out | carla_adapter |

工单不是根据预计时间自行推进；开始和任务完成必须来自CARLA适配器的真实事件。

## 5. Demo复核规则

风险配置中设置：

```text
work_order_type: slope_risk_review
auto_review_delay_ticks: 20
```

复核任务完成后等待20 tick，再将工单关闭。复核结果固定、明确标注为：

```text
approved_by_demo_rule_no_human_review
```

它只用于验证状态机、延迟和审计链，不代表人工复核、专家签字或真实矿山处置结论。后续可用网页审批或外部接口替换 `demo_auto_reviewer`。

## 6. 复现命令

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

## 7. 正式验收结果

证据目录：

```text
artifacts/runs/town03-risk-response-v1-20260801T071448Z-a49d4c86
```

任务结果：

| 指标 | 结果 |
|---|---:|
| 总体状态 | PASS |
| 任务完成率 | 100%（4/4） |
| 风险任务触发tick | 80 |
| 风险任务完成tick | 655 |
| 原巡检恢复完成tick | 903 |
| 自动结束tick | 904 |

工单结果：

| 指标 | 结果 |
|---|---:|
| 工单数 | 1 |
| 工单最终状态 | closed |
| 工单关闭率 | 100% |
| 创建tick | 80 |
| 分配tick | 80 |
| 开始tick | 80 |
| 进入待复核tick | 655 |
| 关闭tick | 675 |
| 分配延迟 | 0 tick |
| 开始延迟 | 0 tick |
| 行动完成延迟 | 575 tick |
| 总关闭延迟 | 595 tick |

完整状态历史：

1. `None → pending`：risk_engine创建橙色风险复核工单；
2. `pending → assigned`：scheduler分配给`inspection_vehicle_02`；
3. `assigned → in_progress`：CARLA开始复核任务；
4. `in_progress → pending_review`：车辆进入8米到达范围；
5. `pending_review → closed`：20 tick后由Demo规则复核关闭。

## 8. 自动测试

当前共有14项测试，本阶段新增：

1. 完整工单状态链和时效指标；
2. 非法状态跳步必须报错；
3. 关联任务超时后工单必须升级。

## 9. 已证明与未证明

已证明：

- 风险事件可自动创建任务和工单；
- 工单能关联风险、任务和车辆；
- 状态可由调度和CARLA真实事件推进；
- 任务完成后进入待复核，而不是直接关闭；
- 复核延迟和关闭时效可以量化；
- 工单完整历史可写入JSON证据；
- 任务超时具有升级路径。

尚未证明：

- 人工或专家真实审批；
- 工单数据库持久化和服务重启恢复；
- 多用户权限、签名和防篡改；
- 短信、邮件、企业微信等通知送达；
- 红色风险下的多子任务应急工单；
- 道路封控和避险路线；
- 同步仿真下的真实秒级时效；
- 矿山工业环境中的业务有效性。

## 10. 下一里程碑

下一步建议实现“红色风险—道路封控—应急处置”最小闭环：

1. 合成时序增加临滑红色阶段；
2. 红色风险生成风险复核、道路封控和应急响应三类任务；
3. 使用应急车的 `road_control` 和 `emergency_response` 能力；
4. 建立禁行路段数据结构；
5. 调度器在分配和路径规划时避开禁行区域；
6. 工单记录多个子任务的完成情况；
7. 全部子任务完成并复核后关闭总工单。

这将进一步覆盖赛题目标3中的“应急预案调用、避险路线规划和装备资源调度”。

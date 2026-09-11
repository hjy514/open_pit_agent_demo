"""调度界面中机器字段的统一中文显示。

这里只转换用户可见文字，API、数据库和场景运行仍使用原始英文代码。
"""

import re


VEHICLE_NAME_FALLBACKS = {
    "inspection_vehicle_01": "矿区巡检矿卡01",
    "inspection_vehicle_02": "边坡复核矿卡02",
    "emergency_vehicle_01": "综合巡检矿卡03",
}

STATUS_LABELS = {
    "pending": "等待调度",
    "queued": "已进入队列",
    "idle": "待命",
    "assigned": "已派发",
    "manual_assigned": "人工派发",
    "executing": "执行中",
    "running": "执行中",
    "working": "执行中",
    "paused": "已暂停",
    "pause": "已暂停",
    "completed": "已完成",
    "succeeded": "执行成功",
    "rejected": "已驳回",
    "cancelled": "已取消",
    "timed_out": "执行超时",
    "fault": "故障",
    "failed": "故障",
    "emergency_stop": "紧急停车",
    "emergency_stopped": "紧急停车",
    "online": "在线",
    "offline": "离线",
    "ready": "就绪",
    "pass": "通过",
    "partial": "部分完成",
    "blocked": "已阻断",
}

HEALTH_LABELS = {
    "healthy": "正常",
    "normal": "正常",
    "fault": "故障",
    "failed": "故障",
    "error": "异常",
}

TYPE_LABELS = {
    "haul_truck": "运输矿卡",
    "inspection_vehicle": "巡检矿卡",
    "support_vehicle": "保障矿卡",
    "emergency_vehicle": "应急矿卡",
    "slope_perception_inspection": "矿区日常巡检",
    "fusion_slope_monitoring": "边坡融合复核",
    "mine_emergency_execution": "道路管控与应急处置",
    "multi_role_inspection": "综合巡检与任务接管",
}

ZONE_LABELS = {
    "routine_zone_01": "东帮主运输通道任务区",
    "secondary_patrol_zone_02": "西区设备巡查区",
    "southern_transport_patrol_zone_03": "南部运输节点巡查区",
    "slope_review_safe_point": "H1边坡安全复核点",
    "risk_zone_02": "东帮边坡2号风险区",
    "road_control_zone": "应急道路管控区",
    "emergency_response_zone": "应急处置区",
    "inspection_zone_01": "1号巡检区",
    "inspection_zone_02": "2号巡检区",
    "inspection_zone_03": "3号巡检区",
}

CAPABILITY_LABELS = {
    "inspection": "矿区巡检",
    "camera": "视觉感知",
    "lidar": "激光雷达",
    "routine_patrol": "日常巡查",
    "emergency_response": "应急处置",
    "road_control": "道路管控",
    "fault_recovery": "故障处理",
}

ACTION_LABELS = {
    "manual_dispatch": "下派/改派任务",
    "approve_ai_plan": "接受智能调度方案",
    "reject_ai_plan": "驳回智能调度方案",
    "pause_vehicle": "暂停车辆",
    "resume_vehicle": "恢复车辆",
    "emergency_stop": "紧急停车",
    "apply_scenario_event_response": "执行场景事件响应",
    "assign_task": "分配任务",
    "dispatch_tasks": "下发调度任务",
    "hold": "安全等待",
    "hold_for_blast": "爆破期间安全等待",
    "hold_for_safe_headway": "保持安全车距",
    "hold_for_temporary_control": "临时管控等待",
    "hold_until_blast_clearance": "等待爆破管控解除",
    "reassign_released_task": "重新分配释放任务",
    "reassign_task": "重新分配任务",
    "reassign_waiting_recovery_task": "分配待恢复任务",
    "route_replan_same_vehicle": "当前车辆路线重规划",
    "runtime_task_recovery": "运行中任务恢复",
    "switch_to_alternative_work_point": "切换备用作业点",
    "task_assignment": "任务分配",
    "task_takeover_after_no_safe_bypass": "无安全绕行路线后任务接管",
    "task_takeover_during_road_closure": "道路封闭期间任务接管",
    "weather_speed_restriction": "恶劣天气限速",
}

COMMUNICATION_LABELS = {
    "online": "通信正常",
    "connected": "通信正常",
    "normal": "通信正常",
    "offline": "通信中断",
    "disconnected": "通信中断",
    "degraded": "通信受限",
}

SOURCE_LABELS = {
    "human_operator": "调度员",
    "human_approved_ai_takeover": "调度员确认智能接管方案",
    "ai_agent": "智能决策系统",
    "system": "系统",
}

MODE_LABELS = {
    "structural": "结构化仿真",
    "carla": "CARLA物理仿真",
}

POLICY_LABELS = {
    "auto": "自动选择",
    "heuristic": "启发式基线策略",
    "multi-objective": "多目标代价策略",
}

EVENT_TYPE_LABELS = {
    "scenario_dispatch": "场景任务已下发",
    "decision_point_pending": "决策等待人工确认",
    "human_decision_point_approve": "调度员已批准决策",
    "human_decision_point_reject": "调度员已驳回决策",
    "scenario_event_applied": "场景事件已触发",
    "scenario_recovery_applied": "场景恢复调度已执行",
    "vehicle_navigation_refreshed_after_resume": "车辆导航已恢复",
    "decision_experience_captured": "决策经验已记录",
    "run_completed": "本轮运行已完成",
}

PHASE_LABELS = {
    "prepare": "运行准备",
    "start": "启动仿真",
    "execute": "任务执行",
    "event": "事件响应",
    "recovery": "恢复调度",
    "finish": "运行收尾",
}


def vehicle_name(vehicle):
    vehicle_id = str(vehicle.get("id", "unknown_vehicle"))
    return str(
        vehicle.get("display_name")
        or VEHICLE_NAME_FALLBACKS.get(vehicle_id)
        or vehicle_id_label(vehicle_id)
    )


def vehicle_name_by_id(vehicle_id, vehicles=None):
    text = str(vehicle_id or "")
    for vehicle in vehicles or []:
        if str(vehicle.get("id", "")) == text:
            return vehicle_name(vehicle)
    if not text:
        return "未分配"
    return VEHICLE_NAME_FALLBACKS.get(text, vehicle_id_label(text))


def vehicle_id_label(vehicle_id):
    """将随机车队ID显示为中文名称，但不修改原始ID。"""
    text = str(vehicle_id or "")
    match = re.match(r"^(haul|inspection|support|emergency)_vehicle_(\d+)$", text)
    if not match:
        return text or "未知车辆"
    role, number = match.groups()
    role_label = {
        "haul": "运输矿卡",
        "inspection": "巡检矿卡",
        "support": "保障矿卡",
        "emergency": "应急矿卡",
    }[role]
    return "{}{}".format(role_label, number)


def status_label(value):
    text = str(value or "-")
    return STATUS_LABELS.get(text.lower(), text)


def health_label(value):
    text = str(value or "-")
    return HEALTH_LABELS.get(text.lower(), text)


def communication_label(value):
    text = str(value or "-")
    return COMMUNICATION_LABELS.get(text.lower(), text)


def type_label(value):
    text = str(value or "-")
    return TYPE_LABELS.get(text, text)


def task_label(value):
    text = str(value or "")
    if not text:
        return "暂无任务"
    if text.startswith("inspect-secondary_patrol_zone_02"):
        return "西区设备巡查"
    if text.startswith("inspect-southern_transport_patrol_zone_03"):
        return "南部运输节点巡查"
    if text.startswith("inspect-routine_zone_01"):
        return "东帮运输通道巡检"
    if text.startswith("inspect-"):
        return "矿区日常巡检"
    if text.startswith("risk-review-"):
        return "边坡风险复核"
    if text.startswith("road-control-"):
        return "运输道路管控"
    if text.startswith("emergency-response-"):
        return "边坡应急处置"
    return text


def zone_label(value):
    text = str(value or "")
    return ZONE_LABELS.get(text, text or "未指定区域")


def capability_label(value):
    text = str(value or "")
    return CAPABILITY_LABELS.get(text, text)


def capability_list_label(values):
    labels = [capability_label(value) for value in values or []]
    return "、".join(labels) if labels else "无特殊能力要求"


def priority_label(value):
    text = str(value or "").lower()
    if text == "urgent":
        return "紧急"
    if text == "important":
        return "重要"
    if text == "normal":
        return "常规"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or "-")
    if number >= 200:
        return "紧急（{}）".format(number)
    if number >= 80:
        return "重要（{}）".format(number)
    return "常规（{}）".format(number)


def action_label(value):
    text = str(value or "")
    return ACTION_LABELS.get(text, text or "未知操作")


def source_label(value):
    text = str(value or "")
    return SOURCE_LABELS.get(text, text or "系统")


def mode_label(value):
    text = str(value or "")
    return MODE_LABELS.get(text.lower(), text or "未知模式")


def policy_label(value):
    text = str(value or "")
    return POLICY_LABELS.get(text.lower(), text or "未指定策略")


def vehicle_count_label(value, scenario_id=None):
    count = int(value)
    if str(scenario_id or "").lower() == "s08":
        return "{}辆（经典演示）".format(count)
    if count == 6:
        return "6辆（已验证）"
    if count == 8:
        return "8辆（实验模式）"
    return "{}辆".format(count)


def event_type_label(value):
    text = str(value or "")
    return EVENT_TYPE_LABELS.get(text, text or "系统事件")


def phase_label(value):
    text = str(value or "")
    return PHASE_LABELS.get(text.lower(), text or "等待运行")

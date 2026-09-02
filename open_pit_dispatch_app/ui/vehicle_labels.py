"""调度界面车辆名称和状态的统一中文显示。"""


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
}

HEALTH_LABELS = {
    "healthy": "正常",
    "normal": "正常",
    "fault": "故障",
    "failed": "故障",
    "error": "异常",
}

TYPE_LABELS = {
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
    "approve_ai_plan": "接受AI方案",
    "reject_ai_plan": "驳回AI方案",
    "pause_vehicle": "暂停车辆",
    "resume_vehicle": "恢复车辆",
    "emergency_stop": "紧急停车",
}

SOURCE_LABELS = {
    "human_operator": "调度员",
    "human_approved_ai_takeover": "调度员确认AI接管方案",
    "ai_agent": "AI Agent",
    "system": "系统",
}


def vehicle_name(vehicle):
    vehicle_id = str(vehicle.get("id", "unknown_vehicle"))
    return str(
        vehicle.get("display_name")
        or VEHICLE_NAME_FALLBACKS.get(vehicle_id)
        or vehicle_id
    )


def vehicle_name_by_id(vehicle_id, vehicles=None):
    text = str(vehicle_id or "")
    for vehicle in vehicles or []:
        if str(vehicle.get("id", "")) == text:
            return vehicle_name(vehicle)
    if not text:
        return "未分配"
    return VEHICLE_NAME_FALLBACKS.get(text, text)


def status_label(value):
    text = str(value or "-")
    return STATUS_LABELS.get(text.lower(), text)


def health_label(value):
    text = str(value or "-")
    return HEALTH_LABELS.get(text.lower(), text)


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

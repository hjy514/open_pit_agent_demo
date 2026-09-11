import json
from pathlib import Path

import requests

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from ui.vehicle_labels import VEHICLE_NAME_FALLBACKS, vehicle_id_label


API_BASE_URL = "http://127.0.0.1:8000"
# The desktop app is now inside the backend repository.
AGENT_RUNS_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "runs"


class ClosedLoopPanel(QFrame):
    PHASES = [
        "数据采集",
        "风险分析",
        "任务调度",
        "装备执行",
        "执行反馈",
        "闭环完成",
    ]

    RISK_NAMES = {
        "blue": "蓝色（正常）",
        "yellow": "黄色（关注）",
        "orange": "橙色（预警）",
        "red": "红色（应急）",
        "unknown": "等待分析",
    }

    RISK_COLORS = {
        "blue": "#64d2ff",
        "yellow": "#ffd60a",
        "orange": "#ff9f0a",
        "red": "#ff453a",
        "unknown": "#8e8e93",
    }

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.Box)
        self.setStyleSheet(
            "QFrame{background:#111b24;border:1px solid #33424d;}"
            "QLabel{color:#e8eef2;border:0;}"
        )
        self._build_ui()
        self.refresh_data()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(1000)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 9, 14, 9)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("实时执行闭环：监测—预警—调度—执行—反馈")
        title.setStyleSheet("font-size:20px;font-weight:bold;color:white;")
        header.addWidget(title)
        self.connection_label = QLabel("正在连接实时数据……")
        self.connection_label.setStyleSheet("color:#ffd60a;")
        header.addWidget(self.connection_label)
        root.addLayout(header)

        self.phase_label = QLabel()
        self.phase_label.setTextFormat(self.phase_label.textFormat())
        self.phase_label.setStyleSheet("font-size:15px;padding:3px 0;")
        root.addWidget(self.phase_label)

        metrics = QHBoxLayout()
        self.data_label = self._metric_label()
        self.risk_label = self._metric_label()
        self.order_label = self._metric_label()
        self.safety_label = self._metric_label()
        metrics.addWidget(self.data_label, 1)
        metrics.addWidget(self.risk_label, 1)
        metrics.addWidget(self.order_label, 1)
        metrics.addWidget(self.safety_label, 2)
        root.addLayout(metrics)

        self.event_label = QLabel("最新闭环事件：等待场景运行")
        self.event_label.setWordWrap(True)
        self.event_label.setStyleSheet(
            "color:#b9c7d0;background:#0c141b;padding:5px;"
        )
        root.addWidget(self.event_label)

        self.learning_label = QLabel(
            "学习数据闭环：运行中持续记录状态、决策、代价与反馈；"
            "当前为数据积累与离线评估阶段，不会自动替换正式策略。"
        )
        self.learning_label.setWordWrap(True)
        self.learning_label.setStyleSheet(
            "color:#8fd3ff;background:#0c141b;padding:5px;"
        )
        root.addWidget(self.learning_label)

    @staticmethod
    def _metric_label():
        label = QLabel()
        label.setWordWrap(True)
        label.setStyleSheet(
            "background:#172630;padding:7px;border-radius:3px;"
        )
        return label

    def fetch_data(self):
        try:
            response = requests.get(
                API_BASE_URL + "/monitoring",
                timeout=1.2,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("/monitoring返回数据不是对象")
            try:
                learning_response = requests.get(
                    API_BASE_URL + "/learning/status", timeout=1.2
                )
                learning_response.raise_for_status()
                learning_status = learning_response.json()
                if isinstance(learning_status, dict):
                    data["learning_policy_status"] = learning_status
            except (requests.RequestException, ValueError):
                pass
            return data
        except requests.HTTPError as error:
            status_code = getattr(error.response, "status_code", None)
            if status_code != 404:
                raise
            return self._fetch_legacy_state()

    def _fetch_legacy_state(self):
        response = requests.get(
            API_BASE_URL + "/state",
            timeout=1.2,
        )
        response.raise_for_status()
        state = response.json()
        if not isinstance(state, dict):
            raise ValueError("/state返回数据不是对象")
        monitoring = state.get("monitoring")
        if isinstance(monitoring, dict):
            result = dict(monitoring)
            result["compatibility_mode"] = True
        else:
            result = self._derive_legacy_monitoring(state)
        return self._enrich_from_run_artifacts(result, state)

    @staticmethod
    def _read_jsonl(path):
        rows = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        except OSError:
            pass
        return rows

    @classmethod
    def _enrich_from_run_artifacts(cls, data, state):
        run_id = str(state.get("run_id") or "").strip()
        if not run_id or "/" in run_id or "\\" in run_id:
            return data
        run_dir = AGENT_RUNS_ROOT / run_id
        if not run_dir.is_dir():
            return data

        result = dict(data)
        observations = cls._read_jsonl(
            run_dir / "monitoring_observations.jsonl"
        )
        fixed_rows = [
            item
            for item in observations
            if item.get("source_type") == "fixed_station"
        ]
        mobile_rows = [
            item
            for item in observations
            if item.get("source_type") == "mobile_equipment"
        ]
        if observations:
            result["fixed_observation_count"] = len(fixed_rows)
            result["mobile_observation_count"] = len(mobile_rows)
            result["total_observation_count"] = len(observations)
            result["fixed_station_count"] = len(
                {item.get("source_id") for item in fixed_rows}
            )
            result["mobile_equipment_count"] = len(
                {item.get("source_id") for item in mobile_rows}
            )

        feedback_rows = cls._read_jsonl(
            run_dir / "feedback_observations.jsonl"
        )
        if feedback_rows:
            result["feedback_count"] = len(feedback_rows)

        summary_path = run_dir / "summary.json"
        try:
            summary = json.loads(
                summary_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            summary = None
        if isinstance(summary, dict):
            result["fixed_observation_count"] = int(
                summary.get(
                    "fixed_observation_count",
                    result.get("fixed_observation_count", 0),
                )
            )
            result["mobile_observation_count"] = int(
                summary.get(
                    "mobile_observation_count",
                    result.get("mobile_observation_count", 0),
                )
            )
            result["total_observation_count"] = (
                result["fixed_observation_count"]
                + result["mobile_observation_count"]
            )
            result["feedback_count"] = int(
                summary.get(
                    "closed_loop_feedback_count",
                    result.get("feedback_count", 0),
                )
            )
            orders = summary.get("work_orders", [])
            if isinstance(orders, list):
                result["work_order_count"] = len(orders)
                result["closed_work_order_count"] = sum(
                    isinstance(item, dict)
                    and item.get("status") == "closed"
                    for item in orders
                )
            complete = bool(
                summary.get("monitoring_dispatch_closed_loop")
            )
            if complete:
                result.update(
                    {
                        "phase": "闭环完成",
                        "phase_index": 5,
                        "closed_loop_complete": True,
                        "road_control_status": "已解除",
                        "route_safety_status": (
                            "复核安全，CARLA导航路线已恢复"
                        ),
                    }
                )
            feedback_records = summary.get(
                "closed_loop_feedback_records", []
            )
            if feedback_records:
                assessment = feedback_records[-1].get(
                    "assessment", {}
                )
                if isinstance(assessment, dict):
                    result["risk_level"] = assessment.get(
                        "level", result.get("risk_level", "UNKNOWN")
                    )
                    result["previous_risk_level"] = assessment.get(
                        "previous_level",
                        result.get("previous_risk_level", "UNKNOWN"),
                    )
        result["compatibility_mode"] = True
        result["artifact_fallback"] = True
        return result

    @staticmethod
    def _derive_legacy_monitoring(state):
        environment = state.get("environment", {})
        if not isinstance(environment, dict):
            environment = {}
        mirrored = environment.get("monitoring_runtime")
        if isinstance(mirrored, dict):
            result = dict(mirrored)
            result["compatibility_mode"] = True
            return result
        vehicles = state.get("vehicles", [])
        tasks = state.get("tasks", [])
        events = state.get("events", [])
        risk = state.get("risk", {})
        if not isinstance(vehicles, list):
            vehicles = []
        if not isinstance(tasks, list):
            tasks = []
        if not isinstance(events, list):
            events = []
        if not isinstance(risk, dict):
            risk = {}

        fixed_stations = environment.get(
            "fixed_monitoring_stations", []
        )
        if not isinstance(fixed_stations, list):
            fixed_stations = []
        risk_tasks = [
            item
            for item in tasks
            if isinstance(item, dict) and item.get("source_event_id")
        ]
        feedback_events = [
            item
            for item in events
            if isinstance(item, dict)
            and (
                item.get("type") == "closed_loop_feedback"
                or item.get("event_type")
                == "closed_loop_feedback"
            )
        ]
        run_completed = any(
            isinstance(item, dict)
            and (
                item.get("type") == "run_completed"
                or item.get("event_type") == "run_completed"
            )
            for item in events
        )
        if feedback_events and run_completed:
            phase, phase_index = "闭环完成", 5
        elif feedback_events:
            phase, phase_index = "复核反馈", 4
        elif risk_tasks:
            phase, phase_index = "装备执行", 3
        elif str(risk.get("level", "UNKNOWN")).upper() != "UNKNOWN":
            phase, phase_index = "风险分析", 1
        else:
            phase, phase_index = "数据采集", 0

        closed_orders = sum(
            item.get("status") == "completed"
            for item in risk_tasks
        )
        complete = bool(
            feedback_events
            and risk_tasks
            and closed_orders == len(risk_tasks)
        )
        latest_event = "旧版API兼容模式：已从/state读取运行状态"
        if events and isinstance(events[-1], dict):
            latest_event = str(
                events[-1].get("message") or latest_event
            )
        return {
            "phase": phase,
            "phase_index": phase_index,
            "fixed_station_count": len(fixed_stations),
            "mobile_equipment_count": len(vehicles),
            "fixed_observation_count": 0,
            "mobile_observation_count": 0,
            "total_observation_count": 0,
            "risk_level": risk.get("level", "UNKNOWN"),
            "previous_risk_level": risk.get(
                "previous_level", "UNKNOWN"
            ),
            "work_order_count": len(risk_tasks),
            "closed_work_order_count": closed_orders,
            "feedback_count": len(feedback_events),
            "road_control_status": (
                "已解除" if complete else "按风险策略管控"
            ),
            "route_safety_status": (
                "复核安全，CARLA导航路线已恢复"
                if complete
                else "CARLA导航与风险区准入管控中"
            ),
            "closed_loop_complete": complete,
            "latest_event": latest_event,
            "compatibility_mode": True,
        }

    def refresh_data(self):
        try:
            data = self.fetch_data()
            if data.get("compatibility_mode"):
                self.connection_label.setText("● 实时更新（兼容模式）")
                self.connection_label.setStyleSheet("color:#ff9f0a;")
            else:
                self.connection_label.setText("● 实时更新")
                self.connection_label.setStyleSheet("color:#32d74b;")
            self.render_data(data)
        except Exception as error:
            self.connection_label.setText("● 数据接口断开")
            self.connection_label.setStyleSheet("color:#ff453a;")
            self.event_label.setText(
                "闭环数据接口连接失败：{}".format(error)
            )

    def render_data(self, data):
        phase_index = max(
            0,
            min(int(data.get("phase_index", 0)), len(self.PHASES) - 1),
        )
        complete = bool(data.get("closed_loop_complete"))
        phase_parts = []
        for index, phase in enumerate(self.PHASES):
            if complete or index < phase_index:
                color = "#32d74b"
                marker = "✓"
            elif index == phase_index:
                color = "#ffd60a"
                marker = "●"
            else:
                color = "#65747e"
                marker = "○"
            phase_parts.append(
                '<span style="color:{};font-weight:bold;">{} {}</span>'.format(
                    color, marker, phase
                )
            )
        self.phase_label.setText(" &nbsp;→&nbsp; ".join(phase_parts))

        fixed_count = int(data.get("fixed_station_count", 0))
        mobile_count = int(data.get("mobile_equipment_count", 0))
        total_count = int(data.get("total_observation_count", 0))
        self.data_label.setText(
            "<b>感知数据</b><br>"
            "固定站：{} 个　移动装备：{} 辆<br>"
            "累计观测：{} 条".format(
                fixed_count, mobile_count, total_count
            )
        )

        risk = str(data.get("risk_level", "unknown")).lower()
        previous = str(data.get("previous_risk_level", "unknown")).lower()
        risk_name = self.RISK_NAMES.get(risk, risk.upper())
        previous_name = self.RISK_NAMES.get(previous, previous.upper())
        risk_color = self.RISK_COLORS.get(risk, "#8e8e93")
        slope_state = str(data.get("slope_state_label", "待监测"))
        self.risk_label.setText(
            "<b>风险研判</b><br>"
            "边坡：{}<br>"
            '当前：<span style="color:{};font-weight:bold;">{}</span><br>'
            "变化：{} → {}".format(
                slope_state, risk_color, risk_name, previous_name, risk_name
            )
        )

        work_orders = int(data.get("work_order_count", 0))
        closed_orders = int(data.get("closed_work_order_count", 0))
        feedback_count = int(data.get("feedback_count", 0))
        takeover_count = int(data.get("takeover_task_count", 0))
        if takeover_count:
            selected_id = (
                data.get("takeover_selected_vehicle_id")
                or data.get("takeover_recommended_vehicle_id")
            )
            selected_name = VEHICLE_NAME_FALLBACKS.get(
                str(selected_id),
                vehicle_id_label(selected_id) if selected_id else "等待推荐",
            )
            self.order_label.setText(
                "<b>动态任务接管</b><br>"
                "状态：{}<br>"
                "推荐/执行：{}<br>"
                "候选车辆：{} 辆".format(
                    data.get("takeover_status", "智能体正在评估"),
                    selected_name,
                    int(data.get("takeover_candidate_count", 0)),
                )
            )
        else:
            self.order_label.setText(
                "<b>处置反馈</b><br>"
                "工单关闭：{} / {}<br>"
                "复核反馈：{} 条".format(
                    closed_orders, work_orders, feedback_count
                )
            )

        self.safety_label.setText(
            "<b>边坡信息与路线安全</b><br>"
            "风险策略：{}<br>"
            "路线状态：{}".format(
                data.get("road_control_status", "未启动"),
                data.get("route_safety_status", "等待路线规划"),
            )
        )
        self.event_label.setText(
            "最新闭环事件：{}".format(
                data.get("latest_event") or "等待场景运行"
            )
        )
        learning = data.get("learning_policy_status")
        if isinstance(learning, dict) and learning.get("formal_policy"):
            formal = learning.get("formal_policy") or {}
            candidate = learning.get("candidate_policy") or {}
            evaluation = learning.get("latest_candidate_evaluation") or {}
            valid_runs = int(learning.get("valid_carla_run_count") or 0)
            total_runs = int(
                learning.get("valid_carla_run_count_total") or valid_runs
            )
            required_runs = int(learning.get("minimum_valid_carla_runs") or 20)
            experience_total = int(
                learning.get("decision_experience_count_total")
                or learning.get("decision_experience_count")
                or 0
            )
            stage = (
                "已达到更新检查门槛"
                if learning.get("status") == "UPDATE_CHECK_DUE"
                else "正在积累有效运行"
            )
            candidate_text = (
                "{}（仅影子评估）".format(candidate.get("model_version"))
                if candidate else "暂无"
            )
            evaluation_names = {
                "OFFLINE_EVALUATED_SHADOW_ONLY": "离线评估完成（待CARLA A/B）",
                "OFFLINE_EVALUATION_FAILED": "离线评估未通过",
            }
            evaluation_text = evaluation_names.get(
                evaluation.get("status"),
                "尚未进行候选策略评估",
            )
            status_color = (
                "#ffd60a"
                if learning.get("status") == "UPDATE_CHECK_DUE"
                else "#64d2ff"
            )
            self.learning_label.setStyleSheet(
                "color:{};background:#0c141b;padding:7px;".format(
                    status_color
                )
            )
            self.learning_label.setText(
                "<b>学习优化慢闭环</b>　{}：本轮 {} / {} 次；"
                "历史有效运行 {} 次，决策经验 {} 条。<br>"
                "正式策略：{}　|　候选策略：{}　|　{}<br>"
                "安全边界：Shadow候选不会自动替换正式策略。".format(
                    stage, valid_runs, required_runs, total_runs,
                    experience_total,
                    formal.get("model_version", "未登记"), candidate_text,
                    evaluation_text,
                )
            )
        elif complete:
            self.learning_label.setText(
                "学习数据闭环：本轮执行已形成状态—决策—反馈经验，"
                "由后端写入运行证据和数据库；策略需离线评估后才能晋级。"
            )
        else:
            self.learning_label.setText(
                "学习数据闭环：正在积累状态、决策、代价与执行反馈；"
                "当前不进行未验证的在线模型替换。"
            )

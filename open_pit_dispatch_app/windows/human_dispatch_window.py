import requests

from ui.vehicle_labels import (
    action_label,
    capability_list_label,
    health_label,
    priority_label,
    source_label,
    status_label,
    task_label,
    vehicle_name,
    vehicle_name_by_id,
    zone_label,
)

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)


API_BASE_URL = "http://127.0.0.1:8000"


class HumanDispatchWindow(QDialog):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("人机协同调度中心")
        self.resize(1240, 860)

        self.dispatch_data = {}
        self.vehicle_data = []
        self.command_data = []
        self.pending_decision_points = []
        self.api_error = None

        self.init_ui()
        self.refresh_data()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.refresh_timer.start(1000)

    def get_json(self, endpoint):
        response = requests.get(
            "{}{}".format(API_BASE_URL, endpoint),
            timeout=1.5,
        )
        response.raise_for_status()
        return response.json()

    def post_json(self, endpoint, payload):
        response = requests.post(
            "{}{}".format(API_BASE_URL, endpoint),
            json=payload,
            timeout=2.0,
        )
        response.raise_for_status()
        return response.json()

    def init_ui(self):
        root = QVBoxLayout()

        title = QLabel("人机协同调度与CARLA控制中心")
        title.setStyleSheet("font-size:28px;font-weight:bold;")
        root.addWidget(title)

        subtitle = QLabel(
            "控制指令进入API命令队列，由正在运行的CARLA进程真实执行。"
        )
        subtitle.setStyleSheet("font-size:15px;color:#555;")
        root.addWidget(subtitle)

        self.connection_label = QLabel("智能体服务：正在连接")
        root.addWidget(self.connection_label)

        body = QHBoxLayout()

        recommendation_box = QGroupBox("智能决策建议与候选评分")
        recommendation_layout = QVBoxLayout()
        self.recommendation_text = QTextEdit()
        self.recommendation_text.setReadOnly(True)
        recommendation_layout.addWidget(self.recommendation_text)

        self.accept_button = QPushButton("按智能推荐下派接管任务")
        self.accept_button.clicked.connect(self.accept_ai_plan)
        recommendation_layout.addWidget(self.accept_button)

        self.reject_button = QPushButton("驳回智方案并保持安全等待")
        self.reject_button.clicked.connect(self.reject_ai_plan)
        recommendation_layout.addWidget(self.reject_button)

        recommendation_box.setLayout(recommendation_layout)
        body.addWidget(recommendation_box, 1)

        control_box = QGroupBox("方案下派与车辆控制")
        control_layout = QVBoxLayout()

        self.task_plan_label = QLabel("当前暂无可下派方案")
        self.task_plan_label.setWordWrap(True)
        self.task_plan_label.setStyleSheet(
            "padding:10px;background:#eef6ff;border:1px solid #8ab4df;"
        )
        control_layout.addWidget(self.task_plan_label)

        form = QFormLayout()

        self.vehicle_combo = QComboBox()
        self.vehicle_combo.currentIndexChanged.connect(
            self.update_selected_vehicle_text
        )
        form.addRow("目标车辆：", self.vehicle_combo)

        self.selected_vehicle_label = QLabel("暂无车辆")
        self.selected_vehicle_label.setWordWrap(True)
        form.addRow("实时状态：", self.selected_vehicle_label)

        self.task_combo = QComboBox()
        self.task_combo.currentIndexChanged.connect(
            self.on_task_selection_changed
        )
        form.addRow("待下派任务：", self.task_combo)

        priority_row = QHBoxLayout()
        self.normal_priority = QRadioButton("普通")
        self.important_priority = QRadioButton("重要")
        self.urgent_priority = QRadioButton("紧急")
        self.important_priority.setChecked(True)
        priority_row.addWidget(self.normal_priority)
        priority_row.addWidget(self.important_priority)
        priority_row.addWidget(self.urgent_priority)
        form.addRow("任务优先级：", priority_row)

        self.speed_limit = QSpinBox()
        self.speed_limit.setRange(1, 60)
        self.speed_limit.setValue(15)
        self.speed_limit.setSuffix(" km/h")
        form.addRow("改派速度：", self.speed_limit)
        control_layout.addLayout(form)

        self.send_button = QPushButton("将所选任务下派给所选车辆")
        self.send_button.clicked.connect(self.send_manual_task)
        control_layout.addWidget(self.send_button)

        button_row = QHBoxLayout()
        pause_button = QPushButton("暂停")
        pause_button.clicked.connect(self.pause_vehicle)
        button_row.addWidget(pause_button)

        resume_button = QPushButton("恢复")
        resume_button.clicked.connect(self.resume_vehicle)
        button_row.addWidget(resume_button)

        emergency_button = QPushButton("紧急停止")
        emergency_button.setStyleSheet(
            "font-weight:bold;color:white;background:#b71c1c;"
        )
        emergency_button.clicked.connect(self.emergency_stop)
        button_row.addWidget(emergency_button)
        control_layout.addLayout(button_row)

        note = QLabel(
            "说明：暂停和急停会保留当前任务与BasicAgent路线；"
            "点击恢复后继续执行。故障车辆不能直接恢复。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        control_layout.addWidget(note)

        control_box.setLayout(control_layout)
        body.addWidget(control_box, 1)
        root.addLayout(body)

        history_box = QGroupBox("任务下派与执行回执")
        history_layout = QVBoxLayout()
        self.history_view = QTextEdit()
        self.history_view.setReadOnly(True)
        history_layout.addWidget(self.history_view)
        history_box.setLayout(history_layout)
        root.addWidget(history_box)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        root.addWidget(close_button)

        self.setLayout(root)

    def refresh_data(self):
        vehicle_id = self.vehicle_combo.currentData()
        task_id = self.task_combo.currentData()

        try:
            self.dispatch_data = self.get_json("/dispatch")
            self.vehicle_data = self.get_json("/vehicles")
            self.command_data = self.get_json("/commands?limit=100")
            self.pending_decision_points = self.get_json(
                "/decision-points/pending"
            )
            if not isinstance(self.dispatch_data, dict):
                self.dispatch_data = {}
            if not isinstance(self.vehicle_data, list):
                self.vehicle_data = []
            if not isinstance(self.command_data, list):
                self.command_data = []
            if not isinstance(self.pending_decision_points, list):
                self.pending_decision_points = []
            self.api_error = None
            self.connection_label.setText("智能体服务：已连接")
            self.connection_label.setStyleSheet("color:#1b8f3a;")
        except Exception as error:
            self.api_error = str(error)
            self.connection_label.setText(
                "智能体服务连接失败：{}".format(error)
            )
            self.connection_label.setStyleSheet("color:#b71c1c;")
            return

        self.refresh_vehicle_combo(vehicle_id)
        self.refresh_task_combo(task_id)
        self.set_text_preserving_scroll(
            self.recommendation_text,
            self.build_recommendation_text(),
        )
        self.update_selected_vehicle_text()
        self.update_selected_task_text()
        self.update_plan_buttons()
        self.refresh_command_history()

    @staticmethod
    def set_text_preserving_scroll(widget, content):
        """定时刷新时保留阅读位置，数据未变化则不重写文本。"""
        if widget.toPlainText() == content:
            return
        scroll_bar = widget.verticalScrollBar()
        previous_position = scroll_bar.value()
        was_at_bottom = (
            previous_position >= scroll_bar.maximum() - 2
            and scroll_bar.maximum() > 0
        )
        widget.setPlainText(content)
        if was_at_bottom:
            scroll_bar.setValue(scroll_bar.maximum())
        else:
            scroll_bar.setValue(
                min(previous_position, scroll_bar.maximum())
            )

    def refresh_vehicle_combo(self, selected_id):
        self.vehicle_combo.blockSignals(True)
        self.vehicle_combo.clear()
        selected_index = -1
        for index, vehicle in enumerate(self.vehicle_data):
            vehicle_id = vehicle.get("id", "unknown")
            label = "{}｜{}｜{}".format(
                vehicle_name(vehicle),
                status_label(vehicle.get("status", "-")),
                vehicle.get("speed", "-"),
            )
            self.vehicle_combo.addItem(label, vehicle_id)
            if vehicle_id == selected_id:
                selected_index = index
        if selected_index >= 0:
            self.vehicle_combo.setCurrentIndex(selected_index)
        self.vehicle_combo.blockSignals(False)

    def refresh_task_combo(self, selected_id):
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        selected_index = -1
        tasks = list(self.dispatch_data.get("tasks", []))
        tasks.sort(
            key=lambda item: (
                0
                if item.get("handover_reason")
                and item.get("recommended_vehicle_id")
                else 1,
                0
                if str(item.get("status", "")).lower()
                not in {"completed", "cancelled", "failed"}
                else 1,
                -int(item.get("priority", 0) or 0),
            )
        )
        for index, task in enumerate(tasks):
            task_id = task.get("task_id", "unknown_task")
            recommended_id = task.get("recommended_vehicle_id")
            assigned_id = task.get("assigned_vehicle_id")
            if task.get("handover_reason") and recommended_id:
                task_status = str(task.get("status", "")).lower()
                phase = {
                    "pending": "待确认",
                    "assigned": "已下派",
                    "executing": "执行中",
                    "running": "执行中",
                    "completed": "已完成",
                }.get(task_status, status_label(task_status))
                prefix = "【智能接管·{}】".format(phase)
                vehicle_text = "接管车：{}".format(
                    vehicle_name_by_id(
                        assigned_id or recommended_id,
                        self.vehicle_data,
                    )
                )
            else:
                prefix = "【{}】".format(
                    status_label(task.get("status", "-"))
                )
                vehicle_text = "执行车：{}".format(
                    vehicle_name_by_id(assigned_id, self.vehicle_data)
                )
            label = "{}{} ｜ {}".format(
                prefix,
                task_label(task_id),
                vehicle_text,
            )
            self.task_combo.addItem(label, task_id)
            if task_id == selected_id:
                selected_index = index
        if self.task_combo.count() == 0:
            self.task_combo.addItem("暂无可用任务", None)
        elif selected_index >= 0:
            self.task_combo.setCurrentIndex(selected_index)
        self.task_combo.blockSignals(False)

        if selected_id is None:
            self.sync_vehicle_to_selected_task()

    def selected_task(self):
        selected_id = self.task_combo.currentData()
        for task in self.dispatch_data.get("tasks", []):
            if task.get("task_id") == selected_id:
                return task
        return None

    def on_task_selection_changed(self):
        self.sync_vehicle_to_selected_task()
        self.update_selected_task_text()
        self.update_selected_vehicle_text()

    def sync_vehicle_to_selected_task(self):
        task = self.selected_task()
        if task is None:
            return
        vehicle_id = (
            task.get("recommended_vehicle_id")
            or task.get("assigned_vehicle_id")
        )
        if not vehicle_id:
            return
        index = self.vehicle_combo.findData(vehicle_id)
        if index >= 0:
            self.vehicle_combo.setCurrentIndex(index)

    def selected_vehicle(self):
        selected_id = self.vehicle_combo.currentData()
        for vehicle in self.vehicle_data:
            if vehicle.get("id") == selected_id:
                return vehicle
        return None

    def update_selected_vehicle_text(self):
        vehicle = self.selected_vehicle()
        if vehicle is None:
            self.selected_vehicle_label.setText("暂无车辆")
            return
        self.selected_vehicle_label.setText(
            "{}：{}；健康{}；速度{}；当前任务：{}".format(
                vehicle_name(vehicle),
                status_label(vehicle.get("status", "-")),
                health_label(vehicle.get("health", "-")),
                vehicle.get("speed", "-"),
                task_label(vehicle.get("task")),
            )
        )

    def update_selected_task_text(self):
        task = self.selected_task()
        if task is None:
            self.task_plan_label.setText("当前暂无可下派方案")
            self.send_button.setEnabled(False)
            return
        task_status = str(task.get("status", "")).lower()
        self.send_button.setEnabled(
            task_status not in {"completed", "cancelled", "failed"}
        )
        original_id = task.get("original_vehicle_id")
        recommended_id = task.get("recommended_vehicle_id")
        assigned_id = task.get("assigned_vehicle_id")
        if task.get("handover_reason") and recommended_id:
            if task_status == "completed":
                title = "智能接管方案（已完成）"
            elif task_status in {"executing", "assigned", "running"}:
                title = "智能接管方案（执行中）"
            else:
                title = "智能接管方案（待调度员确认）"
            relation = "原车：{} → 推荐接管：{}".format(
                vehicle_name_by_id(original_id, self.vehicle_data),
                vehicle_name_by_id(recommended_id, self.vehicle_data),
            )
        else:
            title = "当前任务"
            relation = "执行车辆：{}".format(
                vehicle_name_by_id(assigned_id, self.vehicle_data)
            )
        self.task_plan_label.setText(
            "<b>{}</b><br>"
            "任务：{}<br>"
            "目标：{}<br>"
            "{}<br>"
            "状态：{} ｜ 优先级：{}".format(
                title,
                task_label(task.get("task_id")),
                zone_label(task.get("zone_id")),
                relation,
                status_label(task.get("status", "-")),
                priority_label(task.get("priority", "-")),
            )
        )

    def update_plan_buttons(self):
        if self.pending_decision_points:
            self.accept_button.setEnabled(True)
            self.reject_button.setEnabled(True)
            self.accept_button.setText("批准当前事件调度方案")
            self.reject_button.setText("驳回并保持安全暂停")
            return
        task = self.ai_recommended_task()
        status = str((task or {}).get("status", "")).lower()
        pending_handover = bool(
            task
            and task.get("handover_reason")
            and task.get("recommended_vehicle_id")
            and status == "pending"
        )
        self.accept_button.setEnabled(pending_handover)
        self.reject_button.setEnabled(pending_handover)
        if pending_handover:
            self.accept_button.setText("按智能推荐下派接管任务")
        elif task and status in {"executing", "assigned", "running"}:
            self.accept_button.setText("接管方案执行中")
        elif task and status == "completed":
            self.accept_button.setText("接管任务已完成")
        else:
            self.accept_button.setText("暂无待确认接管方案")

    def build_recommendation_text(self):
        if self.pending_decision_points:
            point = self.pending_decision_points[0]
            candidate_lines = []
            candidates = (
                point.get("candidate_evaluations")
                or point.get("candidate_actions")
                or []
            )
            for index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    continue
                vehicle_id = (
                    candidate.get("vehicle_id")
                    or candidate.get("selected_vehicle_id")
                    or (candidate.get("native_payload") or {}).get(
                        "vehicle_id"
                    )
                )
                score = candidate.get("total_cost")
                if score is None:
                    score = candidate.get("score")
                feasible = candidate.get("feasible")
                constraint_results = candidate.get(
                    "constraint_results", []
                )
                if isinstance(constraint_results, dict):
                    failed_constraints = [
                        key for key, value in constraint_results.items()
                        if value is False
                    ]
                else:
                    failed_constraints = [
                        str(item.get("constraint") or item.get("name"))
                        for item in constraint_results
                        if isinstance(item, dict)
                        and item.get("passed") is False
                    ]
                state_text = (
                    "通过硬约束" if feasible is not False
                    else "不可行：{}".format(
                        "、".join(failed_constraints) or "未通过安全约束"
                    )
                )
                score_text = (
                    "综合代价 {:.3f}".format(float(score))
                    if score is not None else "综合代价待评估"
                )
                candidate_lines.append(
                    "{}. {} ｜ {} ｜ {}".format(
                        index + 1,
                        vehicle_name_by_id(vehicle_id, self.vehicle_data),
                        state_text,
                        score_text,
                    )
                )
            recommendation = point.get("recommended_action") or {}
            recommended_vehicle = (
                point.get("recommended_vehicle_id")
                or recommendation.get("selected_vehicle_id")
                or recommendation.get("vehicle_id")
            )
            return (
                "【需要人工确认】\n"
                "决策编号：{}\n"
                "场景：{}\n"
                "推荐动作：{}\n"
                "推荐车辆：{}\n"
                "决策原因：{}\n\n"
                "【候选车辆与约束】\n{}\n\n"
                "【调度员操作】\n"
                "接受推荐：点击下方“批准当前事件调度方案”。\n"
                "人工改派：在右侧选择任务和车辆并下派，"
                "确认指令入队后再批准当前事件方案。\n"
                "暂不处理或驳回时，受影响车队保持安全等待。"
            ).format(
                point.get("decision_point_id", "-"),
                point.get("scenario_key", "-"),
                action_label(point.get("action_type", "-")),
                vehicle_name_by_id(
                    recommended_vehicle, self.vehicle_data
                ),
                point.get("reason", "-"),
                "\n".join(candidate_lines) or "暂无可显示的候选评分",
            )
        tasks = self.dispatch_data.get("tasks", [])
        if not tasks:
            return "当前没有智能调度建议。\n\n请先运行场景。"
        recommended_task = self.ai_recommended_task()
        if recommended_task is None:
            return "当前没有智能调度建议。"
        is_takeover = bool(recommended_task.get("handover_reason"))
        candidate_lines = []
        if is_takeover:
            for index, candidate in enumerate(
                recommended_task.get("candidate_evaluations", [])
            ):
                candidate_id = candidate.get("vehicle_id", "-")
                reason = str(candidate.get("reason", ""))
                reason = reason.replace(
                    "capabilities matched", "能力满足"
                ).replace("distance=", "距目标=").replace(
                    "active_load=", "当前任务数="
                ).replace("imitation_bonus=", "经验修正=")
                candidate_lines.append(
                    "{}. {}：综合代价 {:.1f}；{}".format(
                        index + 1,
                        vehicle_name_by_id(
                            candidate_id, self.vehicle_data
                        ),
                        float(candidate.get("score", 0.0)),
                        reason,
                    )
                )

        vehicle_id = (
            recommended_task.get("recommended_vehicle_id")
            or recommended_task.get("assigned_vehicle_id")
        )
        lines = [
            "【事件判断】",
            (
                "矿区巡检矿卡01受边坡风险影响，原任务中止，"
                "需要由其他可用矿卡接管。"
                if is_takeover
                else "当前未发现需要接管的突发任务。"
            ),
            "",
            "【推荐方案】",
            "待完成任务：{}".format(
                task_label(recommended_task.get("task_id"))
            ),
            "目标任务区：{}".format(
                zone_label(recommended_task.get("zone_id"))
            ),
            "原执行车辆：{}".format(
                vehicle_name_by_id(
                    recommended_task.get("original_vehicle_id")
                    or recommended_task.get("assigned_vehicle_id"),
                    self.vehicle_data,
                )
            ),
            "推荐接管车辆：{}".format(
                vehicle_name_by_id(vehicle_id, self.vehicle_data)
            ),
            "任务状态：{}".format(
                status_label(recommended_task.get("status", "-"))
            ),
            "优先级：{}".format(
                priority_label(recommended_task.get("priority", "-"))
            ),
            "能力要求：{}".format(
                capability_list_label(
                    recommended_task.get("required_capabilities", [])
                )
            ),
        ]
        if is_takeover:
            lines.extend(
                [
                    "",
                    "【执行影响】",
                    "推荐矿卡从当前安全位置前往原任务区，接续完成矿卡01的任务。",
                    "未被选中的矿卡继续原来的任务和路线。",
                    "风险点保留预警信息，不在本方案中扩大道路封锁。",
                    "",
                    "【候选车辆比较】",
                    *(
                        candidate_lines
                        if candidate_lines
                        else ["暂无候选评分数据"]
                    ),
                ]
            )
        task_status = str(recommended_task.get("status", "")).lower()
        if is_takeover and task_status == "pending":
            next_step = (
                "点击“按智能推荐下派接管任务”，"
                "或在右侧更换车辆后人工下派。"
            )
        elif task_status in {"executing", "assigned", "running"}:
            next_step = "方案已下派，请在下方执行回执中跟踪CARLA执行结果。"
        elif task_status == "completed":
            next_step = "接管任务已完成，本次调度闭环已结束。"
        else:
            next_step = "可在右侧查看任务与执行车辆的对应关系。"
        lines.extend(["", "【下一步】", next_step])
        return "\n".join(lines)

    def ai_recommended_task(self):
        tasks = self.dispatch_data.get("tasks", [])
        handover_tasks = [
            item
            for item in tasks
            if item.get("handover_reason")
            and item.get("recommended_vehicle_id")
        ]
        if handover_tasks:
            return handover_tasks[0]
        if not tasks:
            return None
        return max(tasks, key=lambda item: item.get("priority", 0))

    def selected_priority(self):
        if self.urgent_priority.isChecked():
            return "urgent"
        if self.important_priority.isChecked():
            return "important"
        return "normal"

    def queue_control_command(self, command, title):
        try:
            response = self.post_json("/commands", command)
            queued = response.get("command", {})
            command_id = queued.get("command_id", "-")
            QMessageBox.information(
                self,
                title,
                "指令已进入队列。\n命令编号：{}\n"
                "请在下方查看CARLA执行回执。".format(command_id),
            )
            self.refresh_data()
        except Exception as error:
            QMessageBox.critical(
                self,
                "发送失败",
                "无法将指令写入命令队列。\n\n{}".format(error),
            )

    def post_decision(self, command, title):
        try:
            self.post_json("/manual_dispatch", command)
            QMessageBox.information(self, title, "操作已记录。")
            self.refresh_data()
        except Exception as error:
            QMessageBox.critical(self, "操作失败", str(error))

    def accept_ai_plan(self):
        if self.pending_decision_points:
            self.resolve_pending_decision_point("approve")
            return
        task = self.ai_recommended_task()
        if task is None:
            QMessageBox.warning(self, "没有方案", "当前没有智能调度方案。")
            return
        is_handover = bool(
            task.get("handover_reason")
            and task.get("recommended_vehicle_id")
        )
        vehicle_id = (
            task.get("recommended_vehicle_id")
            or task.get("assigned_vehicle_id")
        )
        self.post_decision(
            {
                "action": "approve_ai_plan",
                "run_id": self.dispatch_data.get("run_id"),
                "task_id": task.get("task_id"),
                "vehicle_id": vehicle_id,
                "source": "human_operator",
            },
            "已接受智能调度方案",
        )
        if is_handover and vehicle_id:
            self.queue_control_command(
                {
                    "action": "manual_dispatch",
                    "vehicle_id": vehicle_id,
                    "task_id": task.get("task_id"),
                    "priority": "urgent",
                    "speed_limit_kmh": self.speed_limit.value(),
                    "source": "human_approved_ai_takeover",
                },
                "任务接管已下发",
            )

    def reject_ai_plan(self):
        if self.pending_decision_points:
            self.resolve_pending_decision_point("reject")
            return
        self.post_decision(
            {
                "action": "reject_ai_plan",
                "run_id": self.dispatch_data.get("run_id"),
                "source": "human_operator",
            },
            "已驳回智能调度方案",
        )

    def resolve_pending_decision_point(self, response):
        point = self.pending_decision_points[0]
        point_id = point.get("decision_point_id")
        if not point_id:
            return
        try:
            self.post_json(
                "/decision-points/{}/resolve".format(point_id),
                {"response": response, "source": "human_dispatch_center"},
            )
            QMessageBox.information(
                self,
                "人工确认已提交",
                "已{}事件响应方案，CARLA将据此继续或以安全结果结束本轮。".format(
                    "批准" if response == "approve" else "驳回"
                ),
            )
            self.refresh_data()
        except Exception as error:
            QMessageBox.critical(self, "操作失败", str(error))

    def send_manual_task(self):
        vehicle_id = self.vehicle_combo.currentData()
        task_id = self.task_combo.currentData()
        if not vehicle_id or not task_id:
            QMessageBox.warning(
                self, "参数不完整", "请选择车辆和任务。"
            )
            return
        task = self.selected_task()
        if str((task or {}).get("status", "")).lower() in {
            "completed",
            "cancelled",
            "failed",
        }:
            QMessageBox.warning(
                self,
                "任务不可下派",
                "该任务已终止或完成，不能重复下派。",
            )
            return
        self.queue_control_command(
            {
                "action": "manual_dispatch",
                "vehicle_id": vehicle_id,
                "task_id": task_id,
                "priority": self.selected_priority(),
                "speed_limit_kmh": self.speed_limit.value(),
                "source": "human_operator",
            },
            "任务下派已提交",
        )

    def pause_vehicle(self):
        vehicle_id = self.vehicle_combo.currentData()
        if vehicle_id:
            self.queue_control_command(
                {
                    "action": "pause_vehicle",
                    "vehicle_id": vehicle_id,
                    "source": "human_operator",
                },
                "暂停指令已提交",
            )

    def resume_vehicle(self):
        vehicle_id = self.vehicle_combo.currentData()
        if vehicle_id:
            self.queue_control_command(
                {
                    "action": "resume_vehicle",
                    "vehicle_id": vehicle_id,
                    "source": "human_operator",
                },
                "恢复指令已提交",
            )

    def emergency_stop(self):
        vehicle_id = self.vehicle_combo.currentData()
        if not vehicle_id:
            return
        answer = QMessageBox.question(
            self,
            "确认紧急停止",
            "确认对{}执行紧急停止吗？".format(
                vehicle_name_by_id(vehicle_id, self.vehicle_data)
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.queue_control_command(
            {
                "action": "emergency_stop",
                "vehicle_id": vehicle_id,
                "source": "human_operator",
            },
            "紧急停止已提交",
        )

    def refresh_command_history(self):
        lines = [
            "最新任务下派和CARLA执行结果（最新在上）",
            "",
        ]
        for item in reversed(self.command_data[-30:]):
            created = str(item.get("created_at", ""))
            time_text = created[11:19] if len(created) >= 19 else created
            task_text = ""
            if item.get("task_id"):
                task_text = " ｜ 任务：{}".format(
                    task_label(item.get("task_id"))
                )
            command_status = {
                "pending": "等待CARLA执行",
                "executing": "CARLA执行中",
                "succeeded": "执行成功",
                "failed": "执行失败",
                "rejected": "已拒绝",
            }.get(
                str(item.get("status", "")).lower(),
                status_label(item.get("status", "-")),
            )
            lines.append(
                "{} ｜ {} ｜ {} ｜ {}{} ｜ 来源：{}{}".format(
                    time_text,
                    vehicle_name_by_id(
                        item.get("vehicle_id"), self.vehicle_data
                    ),
                    action_label(item.get("action")),
                    command_status,
                    task_text,
                    source_label(item.get("source")),
                    (
                        " ｜ 回执：{}".format(item.get("message"))
                        if item.get("message")
                        else ""
                    ),
                )
            )
        if len(lines) == 2:
            lines.append("暂无下派记录")
        self.set_text_preserving_scroll(
            self.history_view, "\n".join(lines)
        )

    def closeEvent(self, event):
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.stop()
        super().closeEvent(event)

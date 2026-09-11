import requests

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QTimer

from widgets.event_panel import EventPanel
from widgets.closed_loop_panel import ClosedLoopPanel
from widgets.camera_wall import CameraWall
from widgets.map_widget import MapWidget
from widgets.vehicle_panel import VehiclePanel
from windows.ai_agent_window import AIAgentWindow
from windows.human_dispatch_window import HumanDispatchWindow
from ui.vehicle_labels import (
    action_label,
    mode_label,
    phase_label,
    policy_label,
    vehicle_count_label,
)


API_BASE_URL = "http://127.0.0.1:8000"
COMPETITION_SCENARIO_IDS = {"s01", "s02", "s04", "s08", "s09"}


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "OpenPit-Agent 智能矿山调度中心"
        )
        self.resize(1700, 1000)

        self.agent_window = None
        self.dispatch_window = None
        self.map_panel = None
        self.scenario_catalog = []
        self._shown_decision_point_ids = set()

        self.init_ui()

    def create_panel(self, title):
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.Box)

        layout = QVBoxLayout()

        label = QLabel(title)
        label.setStyleSheet(
            "font-size:20px;font-weight:bold;"
        )

        layout.addWidget(label)
        frame.setLayout(layout)

        return frame

    def show_agent_window(self):
        self.agent_window = AIAgentWindow()
        self.agent_window.show()

    def show_dispatch_window(self):
        if self.dispatch_window is None:
            self.dispatch_window = HumanDispatchWindow()
        else:
            self.dispatch_window.refresh_data()
        self.dispatch_window.show()
        self.dispatch_window.raise_()
        self.dispatch_window.activateWindow()

    def create_scenario_control(self):
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.Box)
        layout = QHBoxLayout(frame)

        layout.addWidget(QLabel("运行场景"))
        self.scenario_combo = QComboBox()
        self.scenario_combo.setMinimumWidth(220)
        self.scenario_combo.currentIndexChanged.connect(
            self.on_scenario_changed
        )
        layout.addWidget(self.scenario_combo)

        layout.addWidget(QLabel("模式"))
        self.scenario_mode_combo = QComboBox()
        self.scenario_mode_combo.currentTextChanged.connect(
            self.on_scenario_mode_changed
        )
        layout.addWidget(self.scenario_mode_combo)

        layout.addWidget(QLabel("车辆"))
        self.scenario_vehicle_combo = QComboBox()
        layout.addWidget(self.scenario_vehicle_combo)

        layout.addWidget(QLabel("随机种子"))
        self.scenario_seed = QSpinBox()
        self.scenario_seed.setRange(0, 2147483647)
        self.scenario_seed.setValue(202616)
        layout.addWidget(self.scenario_seed)

        self.scenario_random_seed = QCheckBox("每次随机")
        self.scenario_random_seed.toggled.connect(self.on_random_seed_changed)
        layout.addWidget(self.scenario_random_seed)

        layout.addWidget(QLabel("策略"))
        self.scenario_policy_combo = QComboBox()
        layout.addWidget(self.scenario_policy_combo)

        self.scenario_check_only = QCheckBox("仅预检")
        self.scenario_check_only.setEnabled(False)
        layout.addWidget(self.scenario_check_only)

        refresh_button = QPushButton("刷新场景")
        refresh_button.clicked.connect(self.load_scenario_catalog)
        layout.addWidget(refresh_button)

        self.scenario_start_button = QPushButton("启动")
        self.scenario_start_button.clicked.connect(self.start_scenario)
        layout.addWidget(self.scenario_start_button)

        self.scenario_pause_button = QPushButton("暂停")
        self.scenario_pause_button.clicked.connect(self.pause_or_resume_scenario)
        layout.addWidget(self.scenario_pause_button)

        self.scenario_end_button = QPushButton("结束场景")
        self.scenario_end_button.clicked.connect(self.end_scenario)
        layout.addWidget(self.scenario_end_button)

        self.scenario_status = QLabel("场景控制：等待智能体服务")
        self.scenario_status.setMinimumWidth(190)
        layout.addWidget(self.scenario_status, 1)
        return frame

    def load_scenario_catalog(self):
        try:
            response = requests.get(
                API_BASE_URL + "/scenario/catalog", timeout=1.5
            )
            response.raise_for_status()
            # The backend keeps the complete engineering catalog for command
            # line regression and later expansion.  The competition UI only
            # exposes the four accepted multi-vehicle scenarios plus the
            # preserved slope Golden Demo, so operators are not distracted by
            # unfinished research entries.
            self.scenario_catalog = [
                item for item in response.json().get("scenarios", [])
                if item.get("scenario_id") in COMPETITION_SCENARIO_IDS
            ]
            selected = self.scenario_combo.currentData()
            self.scenario_combo.blockSignals(True)
            self.scenario_combo.clear()
            for item in self.scenario_catalog:
                self.scenario_combo.addItem(
                    "{}  {}".format(
                        item.get("scenario_id", "").upper(),
                        item.get("display_name", ""),
                    ),
                    item.get("scenario_id"),
                )
            if selected:
                index = self.scenario_combo.findData(selected)
                if index >= 0:
                    self.scenario_combo.setCurrentIndex(index)
            self.scenario_combo.blockSignals(False)
            self.on_scenario_changed()
            self.scenario_status.setText("场景控制：就绪")
        except (requests.RequestException, ValueError) as exc:
            self.scenario_status.setText("场景控制：API不可用")
            self.scenario_catalog = []

    def on_scenario_changed(self):
        scenario_id = self.scenario_combo.currentData()
        item = next((value for value in self.scenario_catalog
                     if value.get("scenario_id") == scenario_id), None)
        if item is None:
            return
        self.scenario_mode_combo.clear()
        for mode in item.get("modes", []):
            self.scenario_mode_combo.addItem(mode_label(mode), mode)
        self.scenario_vehicle_combo.clear()
        for value in item.get("vehicle_counts", []):
            self.scenario_vehicle_combo.addItem(
                vehicle_count_label(value, scenario_id), int(value)
            )
        default_count = item.get("default_vehicle_count")
        index = self.scenario_vehicle_combo.findData(default_count)
        if index >= 0:
            self.scenario_vehicle_combo.setCurrentIndex(index)
        self.scenario_policy_combo.clear()
        self.scenario_policy_combo.addItem(policy_label("auto"), "auto")
        for policy in item.get("policies", []):
            if policy != "auto":
                self.scenario_policy_combo.addItem(
                    policy_label(policy), policy
                )
        fixed_golden = item.get("implementation_mode") == (
            "legacy_golden_compatibility_adapter"
        )
        self.scenario_seed.setEnabled(
            not fixed_golden and not self.scenario_random_seed.isChecked()
        )
        self.scenario_random_seed.setEnabled(not fixed_golden)
        if fixed_golden:
            self.scenario_random_seed.setChecked(False)
        self.scenario_policy_combo.setEnabled(not fixed_golden)
        self.on_scenario_mode_changed(
            self.scenario_mode_combo.currentData()
        )

    def on_scenario_mode_changed(self, mode):
        carla_mode = str(
            self.scenario_mode_combo.currentData() or mode
        ) == "carla"
        self.scenario_check_only.setEnabled(carla_mode)
        if not carla_mode:
            self.scenario_check_only.setChecked(False)

    def on_random_seed_changed(self, enabled):
        """A fixed seed remains available for replay and policy comparison."""
        self.scenario_seed.setEnabled(not bool(enabled))

    @staticmethod
    def _response_error(response):
        try:
            return str(response.json().get("detail") or response.text)
        except ValueError:
            return response.text

    def start_scenario(self):
        if not self.scenario_combo.currentData():
            QMessageBox.warning(self, "场景启动", "请先刷新场景目录。")
            return
        payload = {
            "scenario_id": self.scenario_combo.currentData(),
            "mode": self.scenario_mode_combo.currentData(),
            "vehicle_count": self.scenario_vehicle_combo.currentData(),
            "seed": self.scenario_seed.value(),
            "random_seed": self.scenario_random_seed.isChecked(),
            "policy": self.scenario_policy_combo.currentData(),
            "check_only": self.scenario_check_only.isChecked(),
        }
        try:
            # Logical decision-point IDs may repeat under a replayed seed.
            # A new run must therefore be allowed to notify the operator again.
            self._shown_decision_point_ids.clear()
            response = requests.post(
                API_BASE_URL + "/scenario/control/start",
                json=payload, timeout=3.0,
            )
            if not response.ok:
                raise RuntimeError(self._response_error(response))
            status = response.json()
            resolved_seed = (status.get("request") or {}).get("seed")
            if resolved_seed is not None:
                self.scenario_seed.setValue(int(resolved_seed))
            self.apply_scenario_status(status)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, "场景启动失败", str(exc))

    def pause_or_resume_scenario(self):
        action = "resume" if self.scenario_pause_button.text() == "继续" else "pause"
        try:
            response = requests.post(
                API_BASE_URL + "/scenario/control/{}".format(action),
                timeout=3.0,
            )
            if not response.ok:
                raise RuntimeError(self._response_error(response))
            self.refresh_scenario_status()
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "场景控制失败", str(exc))

    def end_scenario(self):
        answer = QMessageBox.question(
            self, "结束场景", "结束后本轮场景不能继续，确认结束吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            response = requests.post(
                API_BASE_URL + "/scenario/control/terminate", timeout=3.0
            )
            if not response.ok:
                raise RuntimeError(self._response_error(response))
            self.refresh_scenario_status()
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "结束场景失败", str(exc))

    def refresh_scenario_status(self):
        try:
            response = requests.get(
                API_BASE_URL + "/scenario/control/status", timeout=0.8
            )
            response.raise_for_status()
            status = response.json()
            self.apply_scenario_status(status)
            self.check_pending_decision_points()
        except (requests.RequestException, ValueError):
            self.scenario_status.setText("场景控制：API不可用")

    def apply_scenario_status(self, status):
        """Render the backend state machine without duplicating UI logic."""
        request = status.get("request") or {}
        scenario = str(
            status.get("scenario_id") or request.get("scenario_id") or "-"
        ).upper()
        state = str(status.get("state") or "unknown")
        state_labels = {
            "idle": "等待启动",
            "starting_carla": "正在启动CARLA",
            "loading_map": "CARLA就绪，正在加载地图和场景",
            "starting_scenario": "正在生成场景",
            "running": "运行中",
            "waiting_confirmation": "等待调度员确认",
            "paused": "已暂停",
            "stopping": "正在结束",
            "terminated": "已终止",
            "completed": "已完成",
            "partial": "部分完成",
            "failed": "运行失败",
        }
        result = status.get("result") or {}
        completed = status.get("completed_task_count")
        total = status.get("task_count")
        task_text = (
            " ｜ 任务 {}/{}".format(completed, total)
            if completed is not None and total is not None else ""
        )
        phase = status.get("current_phase")
        phase_text = " ｜ 阶段 {}".format(phase_label(phase)) if phase else ""
        self.scenario_status.setText(
            "场景控制：{} {}{}{}".format(
                scenario, state_labels.get(state, state), task_text, phase_text
            )
        )

        error_text = status.get("error")
        log_tail = status.get("log_tail") or []
        tooltip = [
            "运行ID：{}".format(
                status.get("run_id") or result.get("run_id") or "未生成"
            ),
            "数据库：{}".format(
                status.get("database_path")
                or result.get("database_path") or "未记录"
            ),
        ]
        if error_text:
            tooltip.append("错误：{}".format(error_text))
        if state == "failed" and log_tail:
            tooltip.append("最近日志：\n{}".format("\n".join(log_tail[-8:])))
        self.scenario_status.setToolTip("\n".join(tooltip))

        startup_states = {
            "starting_carla", "loading_map", "starting_scenario", "stopping",
        }
        active = bool(status.get("running")) or state in startup_states
        for widget in (
            self.scenario_combo,
            self.scenario_mode_combo,
            self.scenario_vehicle_combo,
            self.scenario_seed,
            self.scenario_random_seed,
            self.scenario_policy_combo,
            self.scenario_check_only,
        ):
            widget.setEnabled(not active)
        # Restore catalog-specific availability without rebuilding combo-box
        # contents; rebuilding here would overwrite an operator's mode choice
        # on every status refresh.
        if not active:
            scenario_id = self.scenario_combo.currentData()
            catalog_item = next((
                item for item in self.scenario_catalog
                if item.get("scenario_id") == scenario_id
            ), {})
            fixed_golden = catalog_item.get("implementation_mode") == (
                "legacy_golden_compatibility_adapter"
            )
            self.scenario_random_seed.setEnabled(not fixed_golden)
            self.scenario_seed.setEnabled(
                not fixed_golden
                and not self.scenario_random_seed.isChecked()
            )
            self.scenario_policy_combo.setEnabled(not fixed_golden)
            self.scenario_check_only.setEnabled(
                self.scenario_mode_combo.currentData() == "carla"
            )

        paused = state == "paused"
        self.scenario_start_button.setEnabled(not active)
        self.scenario_pause_button.setText("继续" if paused else "暂停")
        self.scenario_pause_button.setEnabled(
            bool(status.get("running"))
            and state not in startup_states
            and state != "waiting_confirmation"
            and str(request.get("scenario_id") or "") != "s08"
        )
        self.scenario_end_button.setEnabled(active)

    def check_pending_decision_points(self):
        """Show one explicit operator gate per incident decision point."""
        try:
            response = requests.get(
                API_BASE_URL + "/decision-points/pending", timeout=0.8
            )
            response.raise_for_status()
            pending = response.json()
        except (requests.RequestException, ValueError):
            return
        if not isinstance(pending, list):
            return
        pending_ids = {
            str(item.get("decision_point_id")) for item in pending
            if isinstance(item, dict) and item.get("decision_point_id")
        }
        # A new run may reuse logical decision IDs; allow it to prompt again
        # after the old pending set has disappeared.
        self._shown_decision_point_ids.intersection_update(pending_ids)
        for point in pending:
            if not isinstance(point, dict):
                continue
            point_id = str(point.get("decision_point_id") or "")
            if not point_id or point_id in self._shown_decision_point_ids:
                continue
            self._shown_decision_point_ids.add(point_id)
            action_type = action_label(
                point.get("action_type", "事件响应")
            )
            reason = point.get("reason", "")
            notice = QMessageBox(self)
            notice.setIcon(QMessageBox.Icon.Warning)
            notice.setWindowTitle("调度事件提醒")
            notice.setText("场景事件已触发，车队正在安全等待。")
            notice.setInformativeText(
                "建议响应：{}\n原因：{}\n\n"
                "请进入调度决策中心，查看候选车辆、代价和"
                "安全约束后下派任务。".format(action_type, reason)
            )
            open_button = notice.addButton(
                "进入调度决策中心",
                QMessageBox.ButtonRole.AcceptRole,
            )
            notice.addButton(
                "稍后处理（保持安全等待）",
                QMessageBox.ButtonRole.RejectRole,
            )
            notice.exec()
            if notice.clickedButton() is open_button:
                self.show_dispatch_window()
            break

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout()

        header = QHBoxLayout()

        title = QLabel(
            "OpenPit-Agent 多智能体矿山装备集群调度平台"
        )
        title.setStyleSheet(
            "font-size:26px;font-weight:bold;"
        )
        header.addWidget(title, 1)

        dispatch_button = QPushButton("进入人工调度中心")
        dispatch_button.setStyleSheet(
            "font-size:16px;font-weight:bold;padding:8px 18px;"
        )
        dispatch_button.clicked.connect(
            self.show_dispatch_window
        )
        header.addWidget(dispatch_button)

        main_layout.addLayout(header)

        main_layout.addWidget(self.create_scenario_control())

        middle = QHBoxLayout()

        self.map_panel = MapWidget()
        self.camera_wall = CameraWall()
        self.operations_tabs = QTabWidget()
        self.operations_tabs.setStyleSheet(
            "QTabBar::tab{font-size:16px;font-weight:bold;"
            "padding:8px 24px;}"
        )
        self.operations_tabs.addTab(
            self.map_panel, "动态态势地图"
        )
        self.operations_tabs.addTab(
            self.camera_wall, "全局与多车视角"
        )
        middle.addWidget(self.operations_tabs, 3)

        right = QVBoxLayout()

        vehicle_panel = VehiclePanel()
        right.addWidget(vehicle_panel, 4)

        agent_frame = QFrame()
        agent_frame.setFrameShape(QFrame.Shape.Box)
        agent_layout = QVBoxLayout()

        agent_title = QLabel("智能体决策中心")
        agent_title.setStyleSheet(
            "font-size:20px;font-weight:bold;"
        )
        agent_layout.addWidget(agent_title)

        agent_button = QPushButton(
            "展开智能体决策中心"
        )
        agent_button.clicked.connect(
            self.show_agent_window
        )
        agent_layout.addWidget(agent_button)

        dispatch_summary = QLabel(
            "人机协同状态：\n"
            "智能方案等待调度员确认"
        )
        agent_layout.addWidget(dispatch_summary)

        dispatch_shortcut = QPushButton(
            "打开人工调度中心"
        )
        dispatch_shortcut.clicked.connect(
            self.show_dispatch_window
        )
        agent_layout.addWidget(dispatch_shortcut)

        agent_frame.setLayout(agent_layout)
        right.addWidget(agent_frame, 3)

        event_panel = EventPanel()
        right.addWidget(event_panel, 2)

        middle.addLayout(right, 2)
        main_layout.addLayout(middle, 5)

        self.closed_loop_panel = ClosedLoopPanel()
        main_layout.addWidget(self.closed_loop_panel, 2)

        central.setLayout(main_layout)
        self.load_scenario_catalog()
        self.scenario_control_timer = QTimer(self)
        self.scenario_control_timer.timeout.connect(
            self.refresh_scenario_status
        )
        self.scenario_control_timer.start(1500)

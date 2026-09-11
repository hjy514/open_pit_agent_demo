import requests

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QTextEdit,
    QHeaderView,
)

from ui.vehicle_labels import (
    communication_label,
    health_label,
    status_label,
    source_label,
    task_label,
    type_label,
    vehicle_name,
)


API_BASE_URL = "http://127.0.0.1:8000"


class VehicleManagementWindow(QDialog):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("装备集群管理中心")
        self.resize(1150, 700)

        self.vehicle_data = []
        self.selected_vehicle_id = None

        self.init_ui()

        # 打开窗口时立即请求一次数据
        self.refresh_data()

        # 每2秒从智能体服务获取一次最新车辆状态
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.refresh_timer.start(2000)

    def init_ui(self):
        layout = QVBoxLayout()

        header_layout = QHBoxLayout()

        title = QLabel("装备集群管理中心")
        title.setStyleSheet(
            "font-size:26px;"
            "font-weight:bold;"
        )

        self.connection_label = QLabel("智能体服务：正在连接")
        self.connection_label.setStyleSheet(
            "font-size:14px;"
            "padding:6px 12px;"
            "border:1px solid #999;"
        )

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(self.connection_label)

        layout.addLayout(header_layout)

        # 状态统计卡片
        card_layout = QHBoxLayout()

        self.total_card = self.create_card("设备总数", 0)
        self.online_card = self.create_card("在线设备", 0)
        self.task_card = self.create_card("执行任务", 0)
        self.fault_card = self.create_card("故障设备", 0)

        card_layout.addWidget(self.total_card)
        card_layout.addWidget(self.online_card)
        card_layout.addWidget(self.task_card)
        card_layout.addWidget(self.fault_card)

        layout.addLayout(card_layout)

        body = QHBoxLayout()

        self.table = QTableWidget()

        headers = [
            "装备名称",
            "类型",
            "状态",
            "速度",
            "位置",
            "任务",
            "健康",
            "通信",
            "来源",
        ]

        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        self.table.cellClicked.connect(self.show_detail)

        body.addWidget(self.table, 3)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setText("请选择车辆查看详细状态")

        body.addWidget(self.detail, 2)

        layout.addLayout(body)

        self.history_label = QLabel(
            """
历史任务：

等待智能体系统返回任务执行记录。
"""
        )
        layout.addWidget(self.history_label)

        button_layout = QHBoxLayout()

        refresh_button = QPushButton("立即刷新")
        refresh_button.clicked.connect(self.refresh_data)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)

        button_layout.addStretch()
        button_layout.addWidget(refresh_button)
        button_layout.addWidget(close_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def create_card(self, title, value):
        label = QLabel(f"{title}\n{value}")
        label.setStyleSheet(
            """
            border:1px solid #999;
            padding:18px;
            font-size:18px;
            """
        )
        return label

    def fetch_vehicle_data(self):
        """
        从 open_pit_agent_demo 的 FastAPI 服务获取车辆状态。
        """
        response = requests.get(
            f"{API_BASE_URL}/vehicles",
            timeout=1.5,
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                "API /vehicles 返回的数据不是车辆列表"
            )

        return data

    def refresh_data(self):
        """
        刷新车辆数据、统计卡片和车辆表格。
        """
        try:
            self.vehicle_data = self.fetch_vehicle_data()

            self.connection_label.setText(
                "智能体服务：已连接"
            )
            self.connection_label.setStyleSheet(
                "font-size:14px;"
                "padding:6px 12px;"
                "border:1px solid #2e7d32;"
            )

            self.update_statistics()
            self.update_table()
            self.restore_selected_detail()

        except Exception as error:
            self.connection_label.setText(
                "智能体服务：连接失败"
            )
            self.connection_label.setStyleSheet(
                "font-size:14px;"
                "padding:6px 12px;"
                "border:1px solid #c62828;"
            )

            self.detail.setText(
                "无法连接智能体服务。\n\n"
                f"接口地址：{API_BASE_URL}/vehicles\n\n"
                f"错误信息：{error}\n\n"
                "请确认 open_pit_agent_demo 的 API 服务正在运行。"
            )

    def update_statistics(self):
        total_count = len(self.vehicle_data)

        online_count = sum(
            1
            for vehicle in self.vehicle_data
            if str(
                vehicle.get("communication", "")
            ).lower() == "online"
        )

        executing_statuses = {
            "assigned",
            "executing",
            "running",
            "working",
        }

        task_count = sum(
            1
            for vehicle in self.vehicle_data
            if str(
                vehicle.get("status", "")
            ).lower() in executing_statuses
        )

        fault_count = sum(
            1
            for vehicle in self.vehicle_data
            if str(
                vehicle.get("health", "")
            ).lower()
            not in {
                "healthy",
                "normal",
                "正常",
            }
        )

        self.total_card.setText(
            f"设备总数\n{total_count}"
        )
        self.online_card.setText(
            f"在线设备\n{online_count}"
        )
        self.task_card.setText(
            f"执行任务\n{task_count}"
        )
        self.fault_card.setText(
            f"故障设备\n{fault_count}"
        )

    def update_table(self):
        scroll_bar = self.table.verticalScrollBar()
        previous_position = scroll_bar.value()
        self.table.setRowCount(len(self.vehicle_data))

        for row, vehicle in enumerate(self.vehicle_data):
            values = [
                vehicle_name(vehicle),
                type_label(vehicle.get("type", "-")),
                status_label(vehicle.get("status", "-")),
                vehicle.get("speed", "-"),
                vehicle.get("position", "-"),
                task_label(vehicle.get("task")),
                health_label(vehicle.get("health", "-")),
                communication_label(vehicle.get("communication", "-")),
                source_label(vehicle.get("source", "-")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))

                self.table.setItem(
                    row,
                    column,
                    item,
                )

        QTimer.singleShot(
            0,
            lambda: scroll_bar.setValue(
                min(previous_position, scroll_bar.maximum())
            ),
        )

    def show_detail(self, row, column):
        if row < 0 or row >= len(self.vehicle_data):
            return

        vehicle = self.vehicle_data[row]

        self.selected_vehicle_id = vehicle.get("id")

        self.display_vehicle_detail(vehicle)

    def restore_selected_detail(self):
        """
        自动刷新后，继续显示之前选中的车辆。
        """
        if self.selected_vehicle_id is None:
            return

        for vehicle in self.vehicle_data:
            if vehicle.get("id") == self.selected_vehicle_id:
                self.display_vehicle_detail(vehicle)
                return

        self.selected_vehicle_id = None
        self.detail.setText(
            "之前选择的车辆已不在当前车辆列表中。"
        )

    def display_vehicle_detail(self, vehicle):
        content = f"""
车辆详细信息

装备名称：
{vehicle_name(vehicle)}

内部编号：
{vehicle.get('id', '-')}

类型：
{type_label(vehicle.get('type', '-'))}

状态：
{status_label(vehicle.get('status', '-'))}

速度：
{vehicle.get('speed', '-')}

当前位置：
{vehicle.get('position', '-')}

当前任务：
{task_label(vehicle.get('task'))}

健康状态：
{health_label(vehicle.get('health', '-'))}

通信状态：
{communication_label(vehicle.get('communication', '-'))}

任务来源：
{source_label(vehicle.get('source', '-'))}
"""
        if self.detail.toPlainText() == content:
            return
        scroll_bar = self.detail.verticalScrollBar()
        previous_position = scroll_bar.value()
        self.detail.setPlainText(content)
        scroll_bar.setValue(
            min(previous_position, scroll_bar.maximum())
        )

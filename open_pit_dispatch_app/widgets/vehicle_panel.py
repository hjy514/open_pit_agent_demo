import requests

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QPushButton,
)

from windows.vehicle_management_window import VehicleManagementWindow
from ui.vehicle_labels import (
    health_label,
    status_label,
    task_label,
    type_label,
    vehicle_name,
)


API_BASE_URL = "http://127.0.0.1:8000"


class VehicleCard(QFrame):

    def __init__(self, data):
        super().__init__()

        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout()

        name = vehicle_name(data)
        vehicle_type = type_label(data.get("type", "-"))
        status = status_label(data.get("status", "-"))
        speed = data.get("speed", "-")
        task = task_label(data.get("task"))
        health = health_label(data.get("health", "-"))
        communication = data.get("communication", "-")

        label = QLabel(
            "<b>{}</b><br>"
            "类型：{}<br>"
            "状态：{} ｜ 速度：{}<br>"
            "任务：{}<br>"
            "健康：{} ｜ 通信：{}".format(
                name,
                vehicle_type,
                status,
                speed,
                task,
                health,
                communication,
            )
        )
        label.setWordWrap(True)

        layout.addWidget(label)
        self.setLayout(layout)


class VehiclePanel(QWidget):

    def __init__(self):
        super().__init__()

        self.window = None
        self.vehicle_data = []

        self.init_ui()
        self.refresh_data()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(2000)

    def init_ui(self):
        layout = QVBoxLayout()

        title = QLabel("装备集群状态")
        title.setStyleSheet(
            "font-size:20px;"
            "font-weight:bold;"
        )
        layout.addWidget(title)

        button = QPushButton("展开装备管理中心")
        button.clicked.connect(self.open_window)
        layout.addWidget(button)

        self.connection_label = QLabel("Agent API：正在连接")
        layout.addWidget(self.connection_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)

        layout.addWidget(self.scroll)

        self.setLayout(layout)

    def fetch_vehicle_data(self):
        response = requests.get(
            f"{API_BASE_URL}/vehicles",
            timeout=1.5,
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                "/vehicles 返回数据不是列表"
            )

        return data

    def refresh_data(self):
        try:
            self.vehicle_data = self.fetch_vehicle_data()

            self.connection_label.setText(
                "Agent API：已连接"
            )

            self.rebuild_cards()

        except Exception as error:
            self.connection_label.setText(
                f"Agent API：连接失败\n{error}"
            )

    def rebuild_cards(self):
        scroll_bar = self.scroll.verticalScrollBar()
        previous_position = scroll_bar.value()
        was_at_bottom = (
            previous_position >= scroll_bar.maximum() - 2
            and scroll_bar.maximum() > 0
        )

        container = QWidget()
        vehicle_layout = QVBoxLayout()

        if not self.vehicle_data:
            vehicle_layout.addWidget(
                QLabel("当前没有车辆状态数据")
            )
        else:
            for vehicle in self.vehicle_data:
                vehicle_layout.addWidget(
                    VehicleCard(vehicle)
                )

        vehicle_layout.addStretch()

        container.setLayout(vehicle_layout)
        self.scroll.setWidget(container)

        # 每2秒会更新车辆数据，但不应打断用户正在滚动查看。
        def restore_scroll_position():
            if was_at_bottom:
                scroll_bar.setValue(scroll_bar.maximum())
            else:
                scroll_bar.setValue(
                    min(previous_position, scroll_bar.maximum())
                )

        QTimer.singleShot(0, restore_scroll_position)

    def open_window(self):
        self.window = VehicleManagementWindow()
        self.window.exec()

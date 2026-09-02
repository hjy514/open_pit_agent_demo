import requests

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QLabel,
    QPushButton,
)

from windows.event_center_window import EventCenterWindow


API_BASE_URL = "http://127.0.0.1:8000"


class EventPanel(QFrame):

    def __init__(self):
        super().__init__()

        self.window = None
        self.events = []

        self.init_ui()
        self.refresh_events()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_events)
        self.timer.start(2000)

    def init_ui(self):
        layout = QVBoxLayout()

        title = QLabel("实时事件流")
        title.setStyleSheet(
            "font-size:20px;"
            "font-weight:bold;"
        )
        layout.addWidget(title)

        self.event_label = QLabel(
            "正在获取实时事件……"
        )
        self.event_label.setWordWrap(True)
        layout.addWidget(self.event_label)

        button = QPushButton(
            "展开实时事件中心"
        )
        button.clicked.connect(self.show_window)
        layout.addWidget(button)

        self.setLayout(layout)

    def fetch_events(self):
        response = requests.get(
            f"{API_BASE_URL}/events",
            timeout=1.5,
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            raise ValueError(
                "/events 返回数据不是列表"
            )

        return data

    def refresh_events(self):
        try:
            self.events = self.fetch_events()

            if not self.events:
                self.event_label.setText(
                    "最新事件：\n\n"
                    "当前暂无实时事件"
                )
                return

            latest_events = self.events[-5:]

            lines = [
                "最新事件：",
                "",
            ]

            for event in latest_events:
                lines.append(
                    self.format_event(event)
                )

            self.event_label.setText(
                "\n".join(lines)
            )

        except Exception as error:
            self.event_label.setText(
                "事件接口连接失败：\n"
                f"{error}"
            )

    def format_event(self, event):
        if isinstance(event, str):
            return event

        if not isinstance(event, dict):
            return str(event)

        timestamp = (
            event.get("time")
            or event.get("timestamp")
            or event.get("created_at")
            or ""
        )

        event_type = (
            event.get("type")
            or event.get("event_type")
            or event.get("name")
            or "事件"
        )

        message = (
            event.get("message")
            or event.get("description")
            or event.get("detail")
            or ""
        )

        return (
            f"{timestamp} "
            f"{event_type} "
            f"{message}"
        ).strip()

    def show_window(self):
        self.window = EventCenterWindow()
        self.window.show()

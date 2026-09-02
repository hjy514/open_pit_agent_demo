from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from widgets.event_panel import EventPanel
from widgets.closed_loop_panel import ClosedLoopPanel
from widgets.camera_wall import CameraWall
from widgets.map_widget import MapWidget
from widgets.vehicle_panel import VehiclePanel
from windows.ai_agent_window import AIAgentWindow
from windows.human_dispatch_window import HumanDispatchWindow


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
        self.dispatch_window = HumanDispatchWindow()
        self.dispatch_window.show()

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

        agent_title = QLabel("AI Agent决策中心")
        agent_title.setStyleSheet(
            "font-size:20px;font-weight:bold;"
        )
        agent_layout.addWidget(agent_title)

        agent_button = QPushButton(
            "展开AI Agent决策中心"
        )
        agent_button.clicked.connect(
            self.show_agent_window
        )
        agent_layout.addWidget(agent_button)

        dispatch_summary = QLabel(
            "人机协同状态：\n"
            "AI方案等待调度员确认"
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

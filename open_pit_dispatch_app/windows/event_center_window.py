from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTextEdit, QPushButton


class EventCenterWindow(QDialog):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenPit-Agent 实时事件中心")
        self.resize(1000, 700)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        title = QLabel("实时事件中心")
        title.setStyleSheet("font-size:28px;font-weight:bold;")
        layout.addWidget(title)

        view = QTextEdit()
        view.setReadOnly(True)
        view.setText("""
实时事件时间线

10:20:01
[环境感知智能体]
检测到大风环境

10:21:20
[风险分析智能体]
边坡风险 GREEN -> ORANGE

10:22:05
[任务调度智能体]
重新分配任务:
矿卡02执行风险复核

10:25:10
[车辆执行智能体]
任务完成


事件统计:

环境事件: 3
风险事件: 2
调度事件: 5
车辆事件: 8
""")
        layout.addWidget(view)

        btn = QPushButton("关闭")
        btn.clicked.connect(self.close)
        layout.addWidget(btn)

        self.setLayout(layout)

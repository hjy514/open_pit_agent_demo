import requests

from ui.vehicle_labels import (
    status_label,
    task_label,
    vehicle_name_by_id,
    zone_label,
)

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
)


API_BASE_URL = "http://127.0.0.1:8000"


class AIAgentWindow(QDialog):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "智能体决策中心"
        )
        self.resize(900, 700)

        self.init_ui()
        self.refresh_data()

        self.timer = QTimer(self)
        self.timer.timeout.connect(
            self.refresh_data
        )
        self.timer.start(2000)

    def init_ui(self):
        layout = QVBoxLayout()

        title = QLabel(
            "智能体决策中心"
        )
        title.setStyleSheet(
            "font-size:28px;"
            "font-weight:bold;"
        )
        layout.addWidget(title)

        self.connection_label = QLabel(
            "智能体服务：正在连接"
        )
        layout.addWidget(
            self.connection_label
        )

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text)

        refresh_button = QPushButton(
            "立即刷新"
        )
        refresh_button.clicked.connect(
            self.refresh_data
        )
        layout.addWidget(refresh_button)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(
            self.close
        )
        layout.addWidget(close_button)

        self.setLayout(layout)

    def fetch_data(self):
        response = requests.get(
            f"{API_BASE_URL}/agents",
            timeout=1.5,
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            raise ValueError(
                "/agents 返回数据不是对象"
            )

        return data

    def refresh_data(self):
        try:
            data = self.fetch_data()

            self.connection_label.setText(
                "智能体服务：已连接"
            )

            self.set_content(self.build_content(data))

        except Exception as error:
            self.connection_label.setText(
                "智能体服务：连接失败"
            )

            self.set_content(
                "无法获取智能体状态。\n\n"
                f"错误信息：{error}"
            )

    def set_content(self, content):
        if self.text.toPlainText() == content:
            return
        scroll_bar = self.text.verticalScrollBar()
        previous_position = scroll_bar.value()
        self.text.setPlainText(content)
        scroll_bar.setValue(
            min(previous_position, scroll_bar.maximum())
        )

    def build_content(self, data):
        lines = ["【智能体运行状态】", ""]

        agents = data.get("agents", [])

        if not agents:
            lines.append(
                "当前没有智能体状态数据"
            )
        else:
            for agent in agents:
                agent_name = {
                    "Perception Agent": "环境感知智能体",
                    "Risk Agent": "风险分析智能体",
                    "Scheduler Agent": "任务调度智能体",
                    "Memory Agent": "经验记忆智能体",
                }.get(
                    str(agent.get("name", "")),
                    str(agent.get("name", "未知智能体")),
                )
                online_status = {
                    "ONLINE": "在线",
                    "OFFLINE": "离线",
                }.get(
                    str(agent.get("status", "")).upper(),
                    str(agent.get("status", "-")),
                )
                lines.append(
                    "• {} ｜ {} ｜ {}".format(
                        agent_name,
                        online_status,
                        agent.get("function", "-"),
                    )
                )

        lines.extend(
            [
                "【矿山环境与监测概况】",
                "",
            ]
        )

        environment = data.get(
            "environment",
            {},
        )

        if environment:
            runtime = environment.get("monitoring_runtime", {})
            lines.extend(
                [
                    "CARLA地图：{}".format(
                        environment.get("map_name", "-")
                    ),
                    "任务区域：{} 个".format(
                        len(environment.get("zones", []) or [])
                    ),
                    "固定监测站：{} 个".format(
                        runtime.get(
                            "fixed_station_count",
                            len(
                                environment.get(
                                    "fixed_monitoring_stations", []
                                )
                                or []
                            ),
                        )
                    ),
                    "移动监测装备：{} 辆".format(
                        runtime.get("mobile_equipment_count", "-")
                    ),
                    "当前闭环阶段：{}".format(
                        runtime.get("phase", "等待运行")
                    ),
                ]
            )
        else:
            lines.append(
                "当前暂无环境感知数据"
            )

        lines.extend(
            [
                "",
                "【风险分析】",
                "",
            ]
        )

        risk = data.get("risk", {})

        risk_level = str(risk.get("level", "UNKNOWN")).upper()
        lines.append(
            "风险等级：{}".format(
                {
                    "BLUE": "蓝色（正常）",
                    "YELLOW": "黄色（注意）",
                    "ORANGE": "橙色（警戒）",
                    "RED": "红色（应急）",
                    "UNKNOWN": "待评估",
                }.get(risk_level, risk_level)
            )
        )

        area = risk.get(
            "area",
            risk.get("zone", "-"),
        )

        if area != "-":
            lines.append(
                "风险区域：{}".format(zone_label(area))
            )

        assessments = (
            risk.get("assessment")
            or risk.get("reason")
            or []
        )

        if assessments:
            lines.append("")
            lines.append("评估依据：")

            for item in assessments:
                if isinstance(item, dict):
                    lines.append(
                        "- {}".format(
                            item.get(
                                "message",
                                item.get(
                                    "description",
                                    str(item),
                                ),
                            )
                        )
                    )
                else:
                    lines.append(
                        f"- {item}"
                    )
        else:
            lines.append(
                "当前暂无风险评估依据"
            )

        lines.extend(
            [
                "",
                "【智能任务决策】",
                "",
            ]
        )

        decision = data.get(
            "decision",
            {},
        )

        if not decision:
            lines.append(
                "当前暂无决策结果"
            )
        else:
            decision_status = {
                "PASS": "暂无新调度动作",
                "SCHEDULED": "智能体已完成任务分配",
                "APPROVED_BY_HUMAN": "调度员已接受智能方案",
                "REJECTED_BY_HUMAN": "调度员已驳回智能方案",
            }.get(
                str(decision.get("status", "")),
                status_label(decision.get("status", "-")),
            )
            lines.append("决策状态：{}".format(decision_status))
            assignments = decision.get("assignments", []) or []
            if assignments:
                lines.append("任务分配：")
                for assignment in assignments:
                    lines.append(
                        "• {} → {} → {}".format(
                            task_label(assignment.get("task_id")),
                            vehicle_name_by_id(
                                assignment.get("vehicle_id")
                            ),
                            zone_label(assignment.get("zone_id")),
                        )
                    )
            elif decision.get("task_id") or decision.get("vehicle_id"):
                lines.append(
                    "执行对应：{} → {}".format(
                        task_label(decision.get("task_id")),
                        vehicle_name_by_id(decision.get("vehicle_id")),
                    )
                )

        return "\n".join(lines)

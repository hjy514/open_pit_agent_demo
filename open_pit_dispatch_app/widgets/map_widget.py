import requests

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsView,
    QMessageBox,
    QMenu,
)

from ui.vehicle_labels import (
    health_label,
    status_label,
    task_label,
    vehicle_name,
)


API_BASE_URL = "http://127.0.0.1:8000"
SCENE_WIDTH = 1100.0
SCENE_HEIGHT = 760.0
SCENE_MARGIN = 55.0


class VehicleItem(QGraphicsPolygonItem):

    def __init__(
        self, vehicle_data, x, y, yaw, color, command_callback
    ):
        super().__init__()

        self.vehicle_data = dict(vehicle_data)
        self.command_callback = command_callback
        self.vehicle_id = str(
            vehicle_data.get("id", "unknown_vehicle")
        )

        polygon = QPolygonF(
            [
                QPointF(18.0, 0.0),
                QPointF(-13.0, -10.0),
                QPointF(-8.0, 0.0),
                QPointF(-13.0, 10.0),
            ]
        )
        self.setPolygon(polygon)
        self.setBrush(QBrush(color))
        self.setPen(QPen(QColor("#f5f5f5"), 1.5))
        self.setPos(x, y)
        self.setRotation(yaw)
        self.setZValue(30.0)
        self.setToolTip(self._detail_text())

    def _detail_text(self):
        data = self.vehicle_data
        position = data.get("position", {})
        return (
            "车辆：{}\n"
            "状态：{}\n"
            "健康：{}\n"
            "速度：{}\n"
            "任务：{}\n"
            "位置：({:.2f}, {:.2f}, {:.2f})\n"
            "航向：{:.1f}°"
        ).format(
            vehicle_name(data),
            status_label(data.get("status", "-")),
            health_label(data.get("health", "-")),
            data.get("speed", "-"),
            task_label(data.get("task_id") or data.get("task")),
            float(position.get("x", 0.0)),
            float(position.get("y", 0.0)),
            float(position.get("z", 0.0)),
            float(data.get("yaw_deg", 0.0)),
        )

    def mousePressEvent(self, event):
        """
        把交互交给MapWidget处理。

        地图每500 ms会重绘并调用scene.clear()。如果在VehicleItem内部
        打开模态QMessageBox或QMenu，Qt会进入嵌套事件循环，定时器可能
        在弹窗打开期间删除当前VehicleItem。弹窗关闭后再调用super()
        就会触发“wrapped C/C++ object ... has been deleted”。

        因此这里先复制纯Python数据、接受事件，然后让MapWidget暂停刷新
        后完成弹窗或右键菜单操作；不再调用super().mousePressEvent()。
        """

        vehicle_data = dict(self.vehicle_data)
        button = event.button()
        screen_position = event.screenPos()
        event.accept()

        interaction_handler = getattr(
            self.command_callback,
            "__self__",
            None,
        )
        handle_interaction = getattr(
            interaction_handler,
            "handle_vehicle_interaction",
            None,
        )

        if callable(handle_interaction):
            handle_interaction(
                vehicle_data,
                button,
                screen_position,
            )
            return

        # 兼容旧的command_callback绑定方式。
        if button == Qt.MouseButton.RightButton:
            return

        detail_text = self._detail_text()
        QMessageBox.information(
            None,
            "车辆实时信息",
            detail_text
            + "\n\n提示：右键车辆可暂停、恢复或急停。",
        )
        return


class MapWidget(QGraphicsView):

    ROUTE_COLORS = [
        "#64d2ff",
        "#ff9f0a",
        "#bf5af2",
        "#30d158",
        "#ff375f",
    ]

    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#101820"))
        self.setSceneRect(
            0.0,
            0.0,
            SCENE_WIDTH,
            SCENE_HEIGHT,
        )

        self.map_state = {}
        self.connection_error = None
        self._initial_fit_done = False
        self._manual_zoom = False

        self.draw_map()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(
            self.refresh_map_state
        )
        self.refresh_timer.start(500)
        self.refresh_map_state()

    def handle_vehicle_interaction(
        self,
        vehicle_data,
        button,
        screen_position,
    ):
        """
        暂停地图刷新后处理车辆详情或右键控制菜单。

        这样可以保证模态弹窗存在期间，scene.clear()不会删除触发事件的
        VehicleItem。交互结束后恢复500 ms刷新并立即拉取一次最新状态。
        """

        timer_was_active = (
            hasattr(self, "refresh_timer")
            and self.refresh_timer.isActive()
        )
        if timer_was_active:
            self.refresh_timer.stop()

        try:
            vehicle_id = str(
                vehicle_data.get("id", "unknown_vehicle")
            )

            if button == Qt.MouseButton.RightButton:
                menu = QMenu(self)
                pause_action = menu.addAction("暂停车辆")
                resume_action = menu.addAction("恢复车辆")
                menu.addSeparator()
                emergency_action = menu.addAction("紧急停止")

                selected = menu.exec(screen_position)
                action_name = None

                if selected == pause_action:
                    action_name = "pause_vehicle"
                elif selected == resume_action:
                    action_name = "resume_vehicle"
                elif selected == emergency_action:
                    answer = QMessageBox.question(
                        self,
                        "确认紧急停止",
                        "确认紧急停止{}吗？".format(
                            vehicle_id
                        ),
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if (
                        answer
                        == QMessageBox.StandardButton.Yes
                    ):
                        action_name = "emergency_stop"

                command_sender = getattr(
                    self,
                    "queue_vehicle_command",
                    None,
                )
                if (
                    action_name
                    and callable(command_sender)
                ):
                    command_sender(
                        action_name,
                        vehicle_id,
                    )
                return

            QMessageBox.information(
                self,
                "车辆实时信息",
                self._vehicle_detail_text(vehicle_data)
                + "\n\n提示：右键车辆可暂停、恢复或急停。",
            )
        finally:
            if timer_was_active:
                self.refresh_timer.start(500)
            QTimer.singleShot(
                0,
                self.refresh_map_state,
            )

    @staticmethod
    def _vehicle_detail_text(vehicle_data):
        position = vehicle_data.get("position", {})
        return (
            "车辆：{}\n"
            "状态：{}\n"
            "健康：{}\n"
            "速度：{}\n"
            "任务：{}\n"
            "位置：({:.2f}, {:.2f}, {:.2f})\n"
            "航向：{:.1f}°"
        ).format(
            vehicle_name(vehicle_data),
            status_label(vehicle_data.get("status", "-")),
            health_label(vehicle_data.get("health", "-")),
            vehicle_data.get("speed", "-"),
            task_label(
                vehicle_data.get("task_id")
                or vehicle_data.get("task")
            ),
            float(position.get("x", 0.0)),
            float(position.get("y", 0.0)),
            float(position.get("z", 0.0)),
            float(vehicle_data.get("yaw_deg", 0.0)),
        )

    def fetch_map_state(self):
        response = requests.get(
            API_BASE_URL + "/map_state",
            timeout=1.2,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("/map_state返回数据不是对象")

        # 兼容已启动但尚未重启的旧版API进程：旧版
        # /map_state不暴露监测图层，但/runtime/sync已将它们
        # 保存在/state.environment中。
        if not data.get("monitoring_areas") and not data.get(
            "fixed_monitoring_stations"
        ):
            try:
                state_response = requests.get(
                    API_BASE_URL + "/state",
                    timeout=1.2,
                )
                state_response.raise_for_status()
                state = state_response.json()
                environment = (
                    state.get("environment", {})
                    if isinstance(state, dict)
                    else {}
                )
                if isinstance(environment, dict):
                    for key in (
                        "monitoring_areas",
                        "fixed_monitoring_stations",
                    ):
                        value = environment.get(key)
                        if isinstance(value, list):
                            data[key] = value
            except Exception:
                # 主地图接口仍然可用时，兼容查询失败不应
                # 将整个地图标记为断开。
                pass
        return data

    def refresh_map_state(self):
        try:
            self.map_state = self.fetch_map_state()
            self.connection_error = None
        except Exception as error:
            self.connection_error = str(error)

        self.draw_map()

        if not self._initial_fit_done:
            self.fitInView(
                self.scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            self._initial_fit_done = True

    @staticmethod
    def _status_color(vehicle):
        status = str(vehicle.get("status", "")).lower()
        health = str(vehicle.get("health", "")).lower()

        if health in {"fault", "failed", "error"}:
            return QColor("#ff3b30")
        if status in {
            "emergency_stop",
            "emergency_stopped",
            "fault",
        }:
            return QColor("#ff3b30")
        if status in {"paused", "pause"}:
            return QColor("#ff9500")
        if status in {"executing", "running", "working"}:
            return QColor("#32d74b")
        if status in {"assigned", "manual_assigned"}:
            return QColor("#0a84ff")
        if status in {"completed", "idle"}:
            return QColor("#8e8e93")
        return QColor("#ffd60a")

    @staticmethod
    def _safe_bounds(bounds):
        if not isinstance(bounds, dict):
            bounds = {}

        min_x = float(bounds.get("min_x", -50.0))
        max_x = float(bounds.get("max_x", 50.0))
        min_y = float(bounds.get("min_y", -50.0))
        max_y = float(bounds.get("max_y", 50.0))

        if max_x - min_x < 1.0:
            max_x = min_x + 1.0
        if max_y - min_y < 1.0:
            max_y = min_y + 1.0

        return min_x, max_x, min_y, max_y

    def world_to_scene(self, x, y, bounds):
        min_x, max_x, min_y, max_y = self._safe_bounds(
            bounds
        )

        usable_width = SCENE_WIDTH - 2.0 * SCENE_MARGIN
        usable_height = SCENE_HEIGHT - 2.0 * SCENE_MARGIN

        sx = (
            SCENE_MARGIN
            + (float(x) - min_x)
            / (max_x - min_x)
            * usable_width
        )
        # CARLA/Unreal采用左手坐标系：
        # +X为世界前向，+Y为世界右向。
        # QGraphicsScene的+Y方向同样向屏幕下方，因此这里直接映射Y，
        # 不能再做上下翻转，否则道路、车道和转弯方向会左右镜像。
        sy = (
            SCENE_MARGIN
            + (float(y) - min_y)
            / (max_y - min_y)
            * usable_height
        )
        return sx, sy

    def draw_grid(self):
        grid_pen = QPen(QColor("#1d2d38"), 1.0)

        for index in range(0, 12):
            x = index * (SCENE_WIDTH / 11.0)
            self.scene.addLine(
                x,
                0.0,
                x,
                SCENE_HEIGHT,
                grid_pen,
            )

        for index in range(0, 9):
            y = index * (SCENE_HEIGHT / 8.0)
            self.scene.addLine(
                0.0,
                y,
                SCENE_WIDTH,
                y,
                grid_pen,
            )

    def draw_roads(self, bounds):
        normal_pen = QPen(QColor(85, 104, 116, 180), 2.0)
        junction_pen = QPen(
            QColor(117, 134, 145, 195),
            2.6,
        )

        for segment in self.map_state.get("roads", []):
            start = segment.get("start")
            end = segment.get("end")
            if not isinstance(start, dict):
                continue
            if not isinstance(end, dict):
                continue

            x1, y1 = self.world_to_scene(
                start.get("x", 0.0),
                start.get("y", 0.0),
                bounds,
            )
            x2, y2 = self.world_to_scene(
                end.get("x", 0.0),
                end.get("y", 0.0),
                bounds,
            )

            pen = (
                junction_pen
                if segment.get("is_junction")
                else normal_pen
            )
            item = self.scene.addLine(
                x1,
                y1,
                x2,
                y2,
                pen,
            )
            item.setZValue(2.0)

    def draw_zones(self, bounds):
        for zone in self.map_state.get("zones", []):
            position = zone.get("position")
            if not isinstance(position, dict):
                continue

            x, y = self.world_to_scene(
                position.get("x", 0.0),
                position.get("y", 0.0),
                bounds,
            )

            radius = 16.0
            item = self.scene.addEllipse(
                x - radius,
                y - radius,
                radius * 2.0,
                radius * 2.0,
                QPen(QColor("#ffd60a"), 2.0),
                QBrush(QColor(255, 214, 10, 45)),
            )
            item.setZValue(7.0)
            item.setToolTip(
                "任务区域：{}".format(
                    zone.get(
                        "display_name",
                        zone.get("zone_id", "未命名区域"),
                    )
                )
            )

    def draw_monitoring_areas(self, bounds):
        colors = [
            QColor(48, 209, 88, 40),
            QColor(100, 210, 255, 40),
            QColor(191, 90, 242, 40),
            QColor(255, 159, 10, 40),
        ]
        border_colors = [
            QColor(48, 209, 88, 150),
            QColor(100, 210, 255, 150),
            QColor(191, 90, 242, 150),
            QColor(255, 159, 10, 150),
        ]
        for index, area in enumerate(
            self.map_state.get("monitoring_areas", [])
        ):
            position = area.get("center_position")
            if not isinstance(position, dict):
                continue
            x, y = self.world_to_scene(
                position.get("x", 0.0),
                position.get("y", 0.0),
                bounds,
            )
            radius = 30.0
            item = self.scene.addEllipse(
                x - radius,
                y - radius,
                radius * 2.0,
                radius * 2.0,
                QPen(border_colors[index % len(border_colors)], 1.5),
                QBrush(colors[index % len(colors)]),
            )
            item.setZValue(5.0)
            item.setToolTip(
                "监测区域：{}\n类型：{}".format(
                    area.get("display_name", area.get("area_id", "-")),
                    area.get("area_type", "-"),
                )
            )

    def draw_fixed_monitoring_stations(self, bounds):
        for station in self.map_state.get(
            "fixed_monitoring_stations", []
        ):
            position = station.get("position")
            if not isinstance(position, dict):
                continue
            x, y = self.world_to_scene(
                position.get("x", 0.0),
                position.get("y", 0.0),
                bounds,
            )
            radius = 5.5
            color = QColor("#00e5ff")
            if not station.get("online", True):
                color = QColor("#8e8e93")
            item = self.scene.addEllipse(
                x - radius,
                y - radius,
                radius * 2.0,
                radius * 2.0,
                QPen(QColor("#ffffff"), 1.2),
                QBrush(color),
            )
            item.setZValue(20.0)
            item.setToolTip(
                "固定监测站：{}\n类型：{}\n所属区域：{}".format(
                    station.get(
                        "display_name", station.get("station_id", "-")
                    ),
                    station.get("station_type", "-"),
                    station.get("area_id", "-"),
                )
            )

    def _draw_polyline(
        self,
        points,
        bounds,
        pen,
        z_value,
    ):
        if not isinstance(points, list) or len(points) < 2:
            return

        path = QPainterPath()
        started = False

        for point in points:
            if not isinstance(point, dict):
                continue
            x, y = self.world_to_scene(
                point.get("x", 0.0),
                point.get("y", 0.0),
                bounds,
            )
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)

        if not started:
            return

        item = self.scene.addPath(path, pen)
        item.setZValue(z_value)

    def draw_trajectories(self, bounds):
        for index, trajectory in enumerate(
            self.map_state.get("trajectories", [])
        ):
            color = QColor(
                self.ROUTE_COLORS[
                    index % len(self.ROUTE_COLORS)
                ]
            )
            color.setAlpha(115)
            pen = QPen(color, 1.4)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._draw_polyline(
                trajectory.get("points", []),
                bounds,
                pen,
                9.0,
            )

    def draw_routes(self, bounds):
        for index, route in enumerate(
            self.map_state.get("routes", [])
        ):
            color = QColor(
                self.ROUTE_COLORS[
                    index % len(self.ROUTE_COLORS)
                ]
            )
            pen = QPen(color, 3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self._draw_polyline(
                route.get("points", []),
                bounds,
                pen,
                12.0,
            )

    def draw_targets(self, bounds):
        for target in self.map_state.get("targets", []):
            position = target.get("position")
            if not isinstance(position, dict):
                continue

            x, y = self.world_to_scene(
                position.get("x", 0.0),
                position.get("y", 0.0),
                bounds,
            )

            radius = 10.0
            item = self.scene.addEllipse(
                x - radius,
                y - radius,
                radius * 2.0,
                radius * 2.0,
                QPen(QColor("#bf5af2"), 2.0),
                QBrush(QColor(191, 90, 242, 90)),
            )
            item.setZValue(18.0)

    def draw_vehicles(self, bounds):
        vehicles = self.map_state.get("vehicles", [])

        for vehicle in vehicles:
            position = vehicle.get("position")
            if not isinstance(position, dict):
                continue

            x, y = self.world_to_scene(
                position.get("x", 0.0),
                position.get("y", 0.0),
                bounds,
            )

            # CARLA正yaw与QGraphicsScene默认屏幕坐标中的正旋转
            # 视觉方向一致。Y轴直接映射后，不应再对yaw取负。
            yaw = float(vehicle.get("yaw_deg", 0.0))
            color = self._status_color(vehicle)

            item = VehicleItem(
                vehicle,
                x,
                y,
                yaw,
                color,
                self.queue_vehicle_command,
            )
            self.scene.addItem(item)

            label = self.scene.addText(
                "{}\n{} | {}".format(
                    vehicle_name(vehicle),
                    status_label(vehicle.get("status", "-")),
                    vehicle.get("speed", "-"),
                )
            )
            label.setDefaultTextColor(QColor("white"))
            label.setPos(x + 15.0, y - 27.0)
            label.setZValue(31.0)

    def draw_header(self):
        map_name = self.map_state.get(
            "map_name", "Town03"
        )
        run_id = self.map_state.get(
            "run_id", "runtime"
        )
        vehicle_count = len(
            self.map_state.get("vehicles", [])
        )
        road_count = len(
            self.map_state.get("roads", [])
        )

        status_text = "API已连接"
        status_color = QColor("#32d74b")
        if self.connection_error:
            status_text = "API连接失败"
            status_color = QColor("#ff453a")

        title = self.scene.addText(
            "动态态势地图 | {} | 车辆:{} | 道路段:{}".format(
                map_name,
                vehicle_count,
                road_count,
            )
        )
        title.setDefaultTextColor(QColor("white"))
        title.setPos(18.0, 8.0)
        title.setZValue(100.0)

        run_label = self.scene.addText(
            "运行编号：{}".format(run_id)
        )
        run_label.setDefaultTextColor(
            QColor("#b0bec5")
        )
        run_label.setPos(18.0, 34.0)
        run_label.setZValue(100.0)

        status = self.scene.addText(status_text)
        status.setDefaultTextColor(status_color)
        status.setPos(
            SCENE_WIDTH - 150.0,
            8.0,
        )
        status.setZValue(100.0)

        orientation = self.scene.addText(
            "CARLA原生坐标显示：+X →，+Y ↓"
        )
        orientation.setDefaultTextColor(
            QColor("#78909c")
        )
        orientation.setPos(
            18.0,
            SCENE_HEIGHT - 34.0,
        )
        orientation.setZValue(100.0)

        if self.connection_error:
            error_label = self.scene.addText(
                self.connection_error[:100]
            )
            error_label.setDefaultTextColor(
                QColor("#ff9f0a")
            )
            error_label.setPos(
                18.0,
                SCENE_HEIGHT - 42.0,
            )
            error_label.setZValue(100.0)

    def draw_legend(self):
        legend_lines = [
            ("#556874", "CARLA道路"),
            ("#64d2ff", "当前规划路线"),
            ("#64d2ff", "历史轨迹(虚线)"),
            ("#ffd60a", "任务区域"),
            ("#30d158", "监测区域"),
            ("#00e5ff", "固定监测站"),
            ("#bf5af2", "任务目标"),
            ("#32d74b", "执行中车辆"),
            ("#ff3b30", "故障/急停"),
        ]

        start_x = SCENE_WIDTH - 165.0
        start_y = 60.0

        for index, (color_value, text_value) in enumerate(
            legend_lines
        ):
            y = start_y + index * 23.0
            self.scene.addEllipse(
                start_x,
                y,
                11.0,
                11.0,
                QPen(QColor(color_value)),
                QBrush(QColor(color_value)),
            )
            label = self.scene.addText(text_value)
            label.setDefaultTextColor(
                QColor("#e0e0e0")
            )
            label.setPos(
                start_x + 16.0,
                y - 7.0,
            )

    def draw_empty_message(self):
        if self.map_state.get("vehicles"):
            return

        message = self.scene.addText(
            "当前没有可绘制的车辆坐标。\n"
            "请先启动API，再从界面启动结构化或CARLA场景。"
        )
        message.setDefaultTextColor(
            QColor("#ffcc80")
        )
        message.setPos(
            SCENE_WIDTH * 0.35,
            SCENE_HEIGHT * 0.45,
        )
        message.setZValue(50.0)

    def draw_map(self):
        self.scene.clear()
        self.scene.setSceneRect(
            0.0,
            0.0,
            SCENE_WIDTH,
            SCENE_HEIGHT,
        )

        bounds = self.map_state.get("bounds", {})

        self.draw_grid()
        self.draw_roads(bounds)
        self.draw_monitoring_areas(bounds)
        self.draw_zones(bounds)
        self.draw_trajectories(bounds)
        self.draw_routes(bounds)
        self.draw_targets(bounds)
        self.draw_fixed_monitoring_stations(bounds)
        self.draw_vehicles(bounds)
        self.draw_header()
        self.draw_legend()
        self.draw_empty_message()

    def queue_vehicle_command(self, action, vehicle_id):
        try:
            response = requests.post(
                API_BASE_URL + "/commands",
                json={
                    "action": action,
                    "vehicle_id": vehicle_id,
                    "source": "map_context_menu",
                },
                timeout=1.5,
            )
            response.raise_for_status()
            data = response.json()
            command_id = data.get("command", {}).get(
                "command_id", "-"
            )
            QMessageBox.information(
                self,
                "指令已进入队列",
                "车辆：{}\n操作：{}\n命令编号：{}\n\n"
                "CARLA运行进程将执行该指令。".format(
                    vehicle_id,
                    action,
                    command_id,
                ),
            )
        except Exception as error:
            QMessageBox.critical(
                self,
                "指令发送失败",
                str(error),
            )

    def wheelEvent(self, event):
        self._manual_zoom = True
        zoom_factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(
                zoom_factor,
                zoom_factor,
            )
        else:
            self.scale(
                1.0 / zoom_factor,
                1.0 / zoom_factor,
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(
                QGraphicsView.DragMode.ScrollHandDrag
            )
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(
                QGraphicsView.DragMode.NoDrag
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._manual_zoom:
            self.fitInView(
                self.scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

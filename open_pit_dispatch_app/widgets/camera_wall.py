"""CARLA overview and multi-vehicle camera wall for the dispatch UI."""

import time

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QPixmap
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


API_BASE_URL = "http://127.0.0.1:8000"

VIEW_SLOTS = [
    ("global", "矿山全局视角", False),
    ("vehicle_slot_1", "车辆视角 1", True),
    ("vehicle_slot_2", "车辆视角 2", True),
    ("vehicle_slot_3", "车辆视角 3", True),
]


class CameraTile(QFrame):

    def __init__(self, slot_id, title, selectable=False, parent=None):
        super().__init__(parent)
        self.slot_id = slot_id
        self.stream_id = "global" if slot_id == "global" else None
        self._source_pixmap = None
        self.setObjectName("cameraTile")
        self.setStyleSheet(
            "QFrame#cameraTile{background:#0b151d;border:1px solid #304553;}"
            "QLabel{border:0;color:#e8f1f5;}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 7)
        layout.setSpacing(5)

        if selectable:
            title_row = QHBoxLayout()
            self.title_label = QLabel(title)
            self.title_label.setStyleSheet(
                "font-size:15px;font-weight:bold;color:#e9f3f7;"
            )
            title_row.addWidget(self.title_label)
            self.selector = QComboBox()
            self.selector.setMinimumWidth(190)
            self.selector.setToolTip("选择要跟随的运行车辆")
            self.selector.currentIndexChanged.connect(
                self._on_stream_changed
            )
            title_row.addWidget(self.selector, 1)
            layout.addLayout(title_row)
        else:
            self.selector = None
            self.title_label = QLabel(title)
            self.title_label.setStyleSheet(
                "font-size:15px;font-weight:bold;color:#e9f3f7;"
            )
            layout.addWidget(self.title_label)

        self.image_label = QLabel("等待 CARLA 摄像头画面……")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(300, 168)
        self.image_label.setStyleSheet(
            "background:#071017;color:#7f939f;font-size:14px;"
        )
        layout.addWidget(self.image_label, 1)

    def set_title(self, title):
        self.title_label.setText(str(title))

    def set_stream_choices(self, streams, preferred_index=0):
        if self.selector is None:
            return
        previous = self.stream_id
        self.selector.blockSignals(True)
        self.selector.clear()
        for item in streams:
            self.selector.addItem(str(item.get("name") or item["id"]), item["id"])
        selected = self.selector.findData(previous) if previous else -1
        if selected < 0 and self.selector.count():
            selected = min(preferred_index, self.selector.count() - 1)
        self.selector.setCurrentIndex(selected)
        self.stream_id = self.selector.currentData() if selected >= 0 else None
        self.selector.blockSignals(False)
        if previous != self.stream_id:
            self._source_pixmap = None
            self.set_waiting("等待所选车辆画面……")

    def _on_stream_changed(self):
        self.stream_id = self.selector.currentData()
        self._source_pixmap = None
        self.set_waiting("正在切换车辆视角……")

    def set_frame(self, payload):
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload, "PNG"):
            return False
        self._source_pixmap = pixmap
        self._render_pixmap()
        return True

    def set_waiting(self, message):
        if self._source_pixmap is None:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(message)

    def _render_pixmap(self):
        if self._source_pixmap is None:
            return
        size = self.image_label.size()
        scaled = self._source_pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setText("")
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_pixmap()


class CameraWall(QWidget):
    """Asynchronous four-channel camera monitor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.tiles = {}
        self.available_stream_ids = set()
        self._manifest_request_active = False
        self._frame_requests_active = set()
        self._last_frame_time = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        header = QHBoxLayout()
        title = QLabel("CARLA 多车视频监控")
        title.setStyleSheet(
            "font-size:19px;font-weight:bold;color:#eef6f8;"
        )
        header.addWidget(title)
        header.addStretch(1)
        self.connection_label = QLabel("● 等待视频流")
        self.connection_label.setStyleSheet(
            "color:#f2c94c;font-weight:bold;"
        )
        header.addWidget(self.connection_label)
        root.addLayout(header)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(7)
        for index, (slot_id, stream_name, selectable) in enumerate(VIEW_SLOTS):
            tile = CameraTile(slot_id, stream_name, selectable=selectable)
            self.tiles[slot_id] = tile
            grid.addWidget(tile, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        root.addLayout(grid, 1)

        self.setStyleSheet("background:#0e1b24;")

        self.manifest_timer = QTimer(self)
        self.manifest_timer.timeout.connect(self.refresh_manifest)
        self.manifest_timer.start(2000)
        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self.refresh_frames)
        self.frame_timer.start(500)
        self.refresh_manifest()

    def _request(self, path):
        request = QNetworkRequest(QUrl(API_BASE_URL + path))
        request.setRawHeader(b"Cache-Control", b"no-cache")
        request.setTransferTimeout(1200)
        return self.network.get(request)

    def refresh_manifest(self):
        if self._manifest_request_active:
            return
        self._manifest_request_active = True
        reply = self._request("/camera/streams")
        reply.finished.connect(
            lambda current_reply=reply: self._handle_manifest(current_reply)
        )

    def _handle_manifest(self, reply):
        self._manifest_request_active = False
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._set_offline("● 视频接口未连接")
                return
            import json

            payload = json.loads(bytes(reply.readAll()).decode("utf-8"))
            streams = payload.get("streams", [])
            self.available_stream_ids = {
                str(item.get("id"))
                for item in streams
                if item.get("frame_available")
            }
            overview = next(
                (item for item in streams if item.get("kind") == "overview"),
                None,
            )
            global_tile = self.tiles["global"]
            global_tile.stream_id = str(
                overview.get("id", "global") if overview else "global"
            )
            if overview and overview.get("name"):
                global_tile.set_title(overview["name"])
            vehicle_streams = [
                {"id": str(item.get("id", "")),
                 "name": str(item.get("name") or item.get("id", ""))}
                for item in streams if item.get("kind") == "vehicle"
            ]
            for index, slot_id in enumerate(
                    ("vehicle_slot_1", "vehicle_slot_2", "vehicle_slot_3")):
                self.tiles[slot_id].set_stream_choices(
                    vehicle_streams, preferred_index=index
                )
            if payload.get("status") == "online":
                self.connection_label.setText("● 四路画面实时更新")
                self.connection_label.setStyleSheet(
                    "color:#31dc72;font-weight:bold;"
                )
            else:
                self._set_offline("● 等待 CARLA 场景运行")
        except (UnicodeDecodeError, ValueError, TypeError):
            self._set_offline("● 视频状态解析失败")
        finally:
            reply.deleteLater()

    def refresh_frames(self):
        for slot_id, tile in self.tiles.items():
            stream_id = tile.stream_id
            if not stream_id or stream_id not in self.available_stream_ids:
                continue
            if slot_id in self._frame_requests_active:
                continue
            self._frame_requests_active.add(slot_id)
            reply = self._request(
                "/camera/{}/frame?t={}".format(
                    stream_id, int(time.time() * 1000)
                )
            )
            reply.setProperty("stream_id", stream_id)
            reply.setProperty("slot_id", slot_id)
            reply.finished.connect(
                lambda current_reply=reply: self._handle_frame(current_reply)
            )

    def _handle_frame(self, reply):
        stream_id = str(reply.property("stream_id"))
        slot_id = str(reply.property("slot_id"))
        self._frame_requests_active.discard(slot_id)
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                return
            tile = self.tiles.get(slot_id)
            if (tile is not None and tile.stream_id == stream_id
                    and tile.set_frame(bytes(reply.readAll()))):
                self._last_frame_time = time.monotonic()
        finally:
            reply.deleteLater()

    def _set_offline(self, message):
        self.available_stream_ids.clear()
        self.connection_label.setText(message)
        self.connection_label.setStyleSheet(
            "color:#f2c94c;font-weight:bold;"
        )
        for tile in self.tiles.values():
            tile.set_waiting("等待 CARLA 摄像头画面……")

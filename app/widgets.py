"""목업에 그려진 화면 조각들.

기본 위젯으로는 안 나오는 모양만 여기서 만든다 — 눈금과 IN/OUT 마커가 있는
타임라인, 아이콘·이름·단축키가 쌓인 조작 버튼, 카드형 파일 정보 패널.
색은 하나도 안 들고 있고 전부 app/theme.py 의 토큰을 쓴다.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import icons, timecode
from .theme import C, FONT_MONO


# ==================================================================== 타임라인

class TimelineBar(QWidget):
    """눈금 · 시각 · IN/OUT 마커가 함께 있는 타임라인.

    QSlider 로는 이 모양이 안 나온다 — 홈 하나에 손잡이 하나가 전부라
    마커를 얹을 자리가 없다. 그래서 직접 그린다. 바깥에서 보는 규약은
    QSlider 와 같게 뒀다: 값은 **프레임 번호**이고, 사용자가 끌면
    `seek_requested` 가 나간다.
    """

    seek_requested = Signal(int)
    scrub_started = Signal()
    scrub_finished = Signal()

    # 끌 때 seek 을 이 간격보다 촘촘히 보내지 않는다. 마우스는 초당 수백 번
    # 움직이는데 mpv 의 seek 은 그보다 훨씬 느려서, 그대로 흘리면 요청이 쌓여
    # 손을 뗀 뒤에도 한참 따라온다 (실제로 그렇게 느렸다).
    # 화면의 재생 위치 표시는 seek 과 무관하게 즉시 움직인다.
    SEEK_THROTTLE_MS = 40

    PAD = 10          # 좌우 여백 — 마커 꼬리표가 잘리지 않을 만큼
    TAG_H = 20        # IN/OUT 꼬리표 줄
    LABEL_H = 17      # 시각 글자 줄
    TICK_H = 11       # 눈금 줄
    GAP = 5
    TRACK_H = 10      # 진행 막대
    TAIL_H = 13       # 재생 위치 선이 막대 아래로 더 내려가는 만큼

    # 눈금 간격 후보 (초). 글자가 겹치지 않는 가장 촘촘한 값을 고른다.
    STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.TAG_H + self.LABEL_H + self.TICK_H
                            + self.GAP + self.TRACK_H + self.TAIL_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        # 바탕을 우리가 직접 칠한다고 알려 준다. 안 그러면 이 위젯을 다시 그릴
        # 때마다 뒤에 있는 조상 위젯까지 같이 그린다 (재생 중 초당 30번).
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self._count = 0
        self._frame = 0
        self._fps = 0.0
        self._cfr = True
        self._mark_in: int | None = None
        self._mark_out: int | None = None
        self._progress: float | None = None     # 인덱싱 진행률 (0~1), 없으면 None
        self._dragging = False
        self._enabled = False

        # 눈금·시각 글자·바탕 막대는 값이 바뀌지 않는 한 늘 같은 그림이다.
        # 매 프레임 다시 그리면 (눈금 수십 개 + 글자 여덟 개) 10ms 가까이 든다 —
        # 재생 중에는 초당 30번 그리므로 그것만으로 앱이 버벅였다.
        # 한 번 그려서 픽스맵에 담아 두고, 움직이는 것(재생 위치·구간·진행률)만
        # 그 위에 얹는다.
        self._static: QPixmap | None = None
        self._seek_clock = QElapsedTimer()
        self._seek_clock.start()
        self._pending_seek: int | None = None

    # ------------------------------------------------------------- 값 설정

    def set_media(self, count: int, fps: float, cfr: bool = True) -> None:
        self._count = max(0, int(count))
        self._fps = float(fps or 0.0)
        self._cfr = bool(cfr)
        self._enabled = self._count > 1
        self._invalidate()

    def _invalidate(self) -> None:
        self._static = None
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._invalidate()

    def set_frame(self, frame: int) -> None:
        frame = max(0, min(int(frame), max(0, self._count - 1)))
        if frame != self._frame:
            self._frame = frame
            self.update()

    def set_marks(self, mark_in: int | None, mark_out: int | None) -> None:
        if (mark_in, mark_out) != (self._mark_in, self._mark_out):
            self._mark_in, self._mark_out = mark_in, mark_out
            self.update()

    def set_index_progress(self, ratio: float | None) -> None:
        """인덱싱 진행률을 막대 위에 옅게 겹쳐 보여 준다. None 이면 지운다."""
        self._progress = None if ratio is None else max(0.0, min(1.0, ratio))
        self.update()

    @property
    def frame(self) -> int:
        return self._frame

    # ------------------------------------------------------------- 좌표 변환

    def _span(self) -> float:
        return max(1.0, self.width() - 2 * self.PAD)

    def _x_of(self, frame: int) -> float:
        if self._count <= 1:
            return float(self.PAD)
        ratio = max(0.0, min(1.0, frame / (self._count - 1)))
        return self.PAD + ratio * self._span()

    def _frame_at(self, x: float) -> int:
        if self._count <= 1:
            return 0
        ratio = (x - self.PAD) / self._span()
        return int(round(max(0.0, min(1.0, ratio)) * (self._count - 1)))

    def _track_rect(self) -> QRectF:
        top = self.TAG_H + self.LABEL_H + self.TICK_H + self.GAP
        return QRectF(self.PAD, top, self._span(), self.TRACK_H)

    # ------------------------------------------------------------- 마우스

    def _scrub_to(self, x: float, force: bool) -> None:
        """끄는 동안의 한 지점. 표시는 즉시, seek 은 간격을 두고 보낸다."""
        frame = self._frame_at(x)
        if frame != self._frame:
            self._frame = frame            # 화면은 손을 따라 바로 움직인다
            self.update()

        self._pending_seek = frame
        if force or self._seek_clock.elapsed() >= self.SEEK_THROTTLE_MS:
            self._seek_clock.restart()
            self._pending_seek = None
            self.seek_requested.emit(frame)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._enabled:
            return
        self._dragging = True
        self.scrub_started.emit()
        self._scrub_to(event.position().x(), force=True)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._scrub_to(event.position().x(), force=False)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._dragging:
            return
        self._dragging = False
        # 손을 뗀 자리는 무조건 보낸다 — 간격 때문에 마지막 위치가 빠지면
        # 화면과 실제 프레임이 어긋난 채로 남는다.
        self._scrub_to(event.position().x(), force=True)
        self.scrub_finished.emit()

    def wheelEvent(self, event) -> None:
        if not self._enabled:
            return
        step = 1 if event.angleDelta().y() > 0 else -1
        self.seek_requested.emit(max(0, min(self._count - 1, self._frame + step)))

    # ------------------------------------------------------------- 그리기

    def _tick_step(self) -> float:
        """글자가 겹치지 않는 가장 촘촘한 눈금 간격(초)."""
        duration = self._duration()
        if duration <= 0:
            return 0.0
        min_px = 132.0                       # HH:MM:SS:FF 글자 + 여유
        for step in self.STEPS:
            if self._span() * (step / duration) >= min_px:
                return float(step)
        return duration / 2.0

    def _duration(self) -> float:
        if self._fps > 0 and self._count > 1:
            return (self._count - 1) / self._fps
        return 0.0

    def _label_at(self, seconds: float) -> str:
        if self._fps > 0 and self._cfr:
            return timecode.format_timecode(int(round(seconds * self._fps)), self._fps)
        return timecode.format_clock(seconds)

    def paintEvent(self, event) -> None:
        track = self._track_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        painter.drawPixmap(0, 0, self._static_layer())
        self._paint_progress(painter, track)
        self._paint_played(painter, track)
        self._paint_marks(painter, track)
        if self._enabled:
            self._paint_playhead(painter, track)
        painter.end()

    def _static_layer(self) -> QPixmap:
        """눈금·시각 글자·바탕 막대 — 크기나 영상이 바뀔 때만 다시 그린다."""
        if self._static is not None:
            return self._static

        dpr = self.devicePixelRatioF() or 1.0
        pixmap = QPixmap(max(1, int(self.width() * dpr)),
                         max(1, int(self.height() * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QColor(C.BG_APP))          # WA_OpaquePaintEvent 의 약속

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self._track_rect()
        self._paint_ticks(painter, track)
        self._paint_base_track(painter, track)
        painter.end()

        self._static = pixmap
        return pixmap

    def _paint_ticks(self, painter: QPainter, track: QRectF) -> None:
        step = self._tick_step()
        if step <= 0:
            return
        duration = self._duration()
        label_y = self.TAG_H
        tick_top = self.TAG_H + self.LABEL_H
        tick_bottom = tick_top + self.TICK_H

        font = QFont(FONT_MONO)
        font.setPixelSize(11)
        painter.setFont(font)

        # 잔눈금 — 큰 눈금 사이를 10 등분. 자로 잰 느낌을 내는 부분이다.
        minor = step / 10.0
        painter.setPen(QPen(QColor("#2f3641"), 1.0))
        t = 0.0
        while t <= duration + 1e-6:
            x = self.PAD + (t / duration) * self._span()
            painter.drawLine(QPointF(x, tick_bottom - 4), QPointF(x, tick_bottom))
            t += minor

        painter.setPen(QPen(QColor("#4b5563"), 1.0))
        t = 0.0
        while t <= duration + 1e-6:
            x = self.PAD + (t / duration) * self._span()
            painter.drawLine(QPointF(x, tick_bottom - 8), QPointF(x, tick_bottom))
            t += step

        # 시각 글자 — 왼쪽 끝은 왼쪽 정렬, 나머지는 눈금 가운데.
        painter.setPen(QColor(C.TEXT_MUTED))
        t = 0.0
        while t <= duration + 1e-6:
            x = self.PAD + (t / duration) * self._span()
            text = self._label_at(t)
            width = painter.fontMetrics().horizontalAdvance(text)
            left = x if t == 0.0 else x - width / 2.0
            left = max(self.PAD, min(left, self.width() - self.PAD - width))
            painter.drawText(QRectF(left, label_y, width, self.LABEL_H),
                             Qt.AlignVCenter | Qt.AlignLeft, text)
            t += step

    def _paint_base_track(self, painter: QPainter, track: QRectF) -> None:
        radius = track.height() / 2.0
        base = QPainterPath()
        base.addRoundedRect(track, radius, radius)
        painter.fillPath(base, QColor("#2b323d"))

    def _paint_progress(self, painter: QPainter, track: QRectF) -> None:
        """인덱싱 진행률 — 재생 위치와 헷갈리지 않게 아주 옅게만."""
        if self._progress is None:
            return
        radius = track.height() / 2.0
        done = QRectF(track)
        done.setWidth(track.width() * self._progress)
        path = QPainterPath()
        path.addRoundedRect(done, radius, radius)
        painter.fillPath(path, QColor(59, 130, 246, 60))

    def _paint_played(self, painter: QPainter, track: QRectF) -> None:
        if not self._enabled:
            return
        radius = track.height() / 2.0
        played = QRectF(track)
        played.setWidth(max(radius * 2, self._x_of(self._frame) - track.left()))
        gradient = QLinearGradient(played.topLeft(), played.topRight())
        gradient.setColorAt(0.0, QColor("#1f4fa8"))
        gradient.setColorAt(1.0, QColor(C.ACCENT))
        path = QPainterPath()
        path.addRoundedRect(played, radius, radius)
        painter.fillPath(path, gradient)

    def _paint_marks(self, painter: QPainter, track: QRectF) -> None:
        font = QFont()
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)

        for frame, text, color in ((self._mark_in, "IN", C.GREEN),
                                   (self._mark_out, "OUT", C.RED)):
            if frame is None or self._count <= 1:
                continue
            x = self._x_of(frame)
            painter.setPen(QPen(QColor(color), 1.4))
            painter.drawLine(QPointF(x, self.TAG_H - 2), QPointF(x, track.bottom()))

            width = painter.fontMetrics().horizontalAdvance(text) + 12
            left = max(0.0, min(x - width / 2.0, self.width() - width))
            tag = QRectF(left, 0, width, self.TAG_H - 4)
            path = QPainterPath()
            path.addRoundedRect(tag, 3, 3)
            painter.fillPath(path, QColor(color))
            painter.setPen(QColor("#0d1117"))
            painter.drawText(tag, Qt.AlignCenter, text)

    def _paint_playhead(self, painter: QPainter, track: QRectF) -> None:
        x = self._x_of(self._frame)
        top = self.TAG_H + self.LABEL_H
        bottom = track.bottom() + self.TAIL_H - 4

        painter.setPen(QPen(QColor(C.ACCENT), 1.6))
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPointF(x, track.center().y()), 5.6, 5.6)
        painter.setBrush(QColor(C.ACCENT))
        painter.drawEllipse(QPointF(x, track.center().y()), 3.6, 3.6)
        painter.setBrush(Qt.NoBrush)


# ============================================================== 조작 버튼

class TransportButton(QPushButton):
    """아이콘 · 이름 · 단축키가 세로로 쌓인 큰 버튼 (목업 하단 줄)."""

    VARIANTS = {
        "normal": ("TransportButton", C.TEXT, "TransportLabel"),
        "accent": ("TransportAccent", C.ACCENT_HI, "TransportLabelAcc"),
        "in": ("TransportIn", C.GREEN, "TransportLabel"),
        "out": ("TransportOut", C.RED, "TransportLabel"),
    }

    def __init__(self, label: str, shortcut: str = "", icon_name: str | None = None,
                 chip: str | None = None, variant: str = "normal",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        object_name, color, label_style = self.VARIANTS[variant]
        self.setObjectName(object_name)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(QSize(104, 86))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._icon_name = icon_name
        self._icon_color = color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 9)
        layout.setSpacing(4)

        if chip is not None:
            head: QWidget = QLabel(chip)
            head.setObjectName("ChipIn" if variant == "in" else "ChipOut")
            head.setAlignment(Qt.AlignCenter)
        else:
            head = QLabel()
            head.setAlignment(Qt.AlignCenter)
            head.setFixedHeight(26)
        self._head = head

        self._label = QLabel(label)
        self._label.setObjectName(label_style)
        self._label.setAlignment(Qt.AlignCenter)

        self._key = QLabel(shortcut)
        self._key.setObjectName("TransportKey")
        self._key.setAlignment(Qt.AlignCenter)

        layout.addWidget(head, 0, Qt.AlignCenter)
        layout.addWidget(self._label)
        layout.addWidget(self._key)

        for child in (head, self._label, self._key):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._refresh_icon()

    # 아이콘 색이 활성/비활성을 따라가야 한다 — QSS 는 QLabel 의 pixmap 색을
    # 못 바꾸므로 상태가 바뀔 때마다 다시 그린다.
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self._refresh_icon()

    def set_icon(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self._refresh_icon()

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def _refresh_icon(self) -> None:
        if self._icon_name is None or not isinstance(self._head, QLabel):
            return
        if self._head.objectName().startswith("Chip"):
            return
        color = self._icon_color if self.isEnabled() else C.TEXT_OFF
        ratio = self.devicePixelRatioF() or 1.0
        self._head.setPixmap(icons.pixmap(self._icon_name, color, 22, max(1.0, ratio)))


# ================================================================== 입력칸

class FieldBox(QWidget):
    """`Frame [2114]` 처럼 이름표와 입력칸이 한 덩어리로 묶인 칸."""

    committed = Signal(str)

    def __init__(self, name: str, width: int = 118, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FieldBox")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(34)

        self.edit = QLineEdit()
        self.edit.setObjectName("FieldEdit")
        self.edit.setFixedWidth(width)
        self.edit.setAlignment(Qt.AlignCenter)
        self.edit.setEnabled(False)
        self.edit.returnPressed.connect(
            lambda: self.committed.emit(self.edit.text().strip()))

        label = QLabel(name)
        label.setObjectName("FieldName")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 0, 8, 0)
        layout.setSpacing(10)
        layout.addWidget(label)
        layout.addWidget(self.edit)

    def set_text(self, text: str) -> None:
        # 같은 글자를 다시 넣지 않는다 — QLineEdit.setText 는 값이 같아도
        # 커서·선택을 되돌리고 다시 그린다. 폴링(초당 30번)마다 하면 낭비다.
        if not self.edit.hasFocus() and self.edit.text() != text:
            self.edit.setText(text)

    def set_active(self, active: bool) -> None:
        self.edit.setEnabled(active)


# =============================================================== 정보 패널

class InfoPanel(QWidget):
    """파일 정보 카드 — 제목줄 · 상태 배지 · 항목들 · 접기 버튼."""

    collapse_toggled = Signal(bool)

    def __init__(self, title: str = "파일 정보", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InfoCard")
        # QWidget 을 상속한 위젯은 이 속성이 있어야 스타일시트의 배경·테두리를 그린다.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(44)

        self._collapsed = False

        # --- 제목줄 ---
        self._title = QLabel(title)
        self._title.setObjectName("InfoTitle")

        self._chevron = QPushButton()
        self._chevron.setObjectName("IconButton")
        self._chevron.setFocusPolicy(Qt.NoFocus)
        self._chevron.setCursor(Qt.PointingHandCursor)
        self._chevron.setFixedSize(26, 26)
        self._chevron.clicked.connect(lambda: self.set_collapsed(not self._collapsed))

        self._header = QWidget()
        self._header.setObjectName("InfoHeader")
        self._header.setAttribute(Qt.WA_StyledBackground, True)
        self._header.setFixedHeight(46)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(14, 0, 8, 0)
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        header_layout.addWidget(self._chevron)

        # --- 본문 ---
        self._rows = QWidget()
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(14, 12, 14, 12)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._rows)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QScrollArea.NoFrame)

        # --- 바닥 ---
        self._footer = QPushButton("  접기")
        self._footer.setIcon(icons.icon("chevron_right", C.TEXT_DIM, 16))
        self._footer.setFocusPolicy(Qt.NoFocus)
        self._footer.setCursor(Qt.PointingHandCursor)
        self._footer.clicked.connect(lambda: self.set_collapsed(True))

        footer_box = QWidget()
        footer_layout = QHBoxLayout(footer_box)
        footer_layout.setContentsMargins(12, 6, 12, 12)
        footer_layout.addWidget(self._footer)

        self._footer_box = footer_box

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header)
        root.addWidget(self._scroll, 1)
        root.addWidget(footer_box)

        self._badge: QLabel | None = None
        self._refresh_chevron()

    # ------------------------------------------------------------- 접기

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._title.setVisible(not collapsed)
        self._scroll.setVisible(not collapsed)
        self._footer_box.setVisible(not collapsed)
        self._refresh_chevron()
        self.collapse_toggled.emit(collapsed)

    def _refresh_chevron(self) -> None:
        name = "chevron_left" if self._collapsed else "chevron_right"
        self._chevron.setIcon(icons.icon(name, C.TEXT_DIM, 18))
        self._chevron.setToolTip("펼치기" if self._collapsed else "접기")

    # ------------------------------------------------------------- 내용

    def set_rows(self, rows: list[tuple[str, str]], badge: tuple[str, bool] | None = None,
                 note: str = "") -> None:
        """항목을 통째로 다시 그린다. rows 는 (이름, 값) 목록."""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if badge is not None:
            text, ok = badge
            chip = QLabel(text)
            chip.setObjectName("BadgeOk" if ok else "BadgeWait")
            chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 10)
            holder_layout.addWidget(chip)
            holder_layout.addStretch(1)
            self._rows_layout.addWidget(holder)

        for i, (key, value) in enumerate(rows):
            if i:
                rule = QWidget()
                rule.setObjectName("InfoRule")
                rule.setFixedHeight(1)
                self._rows_layout.addWidget(rule)

            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 10, 0, 10)
            row_layout.setSpacing(3)

            key_label = QLabel(key)
            key_label.setObjectName("InfoKey")
            value_label = QLabel(value)
            value_label.setObjectName("InfoValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

            row_layout.addWidget(key_label)
            row_layout.addWidget(value_label)
            self._rows_layout.addWidget(row)

        if note:
            note_label = QLabel(note)
            note_label.setObjectName("InfoNote")
            note_label.setWordWrap(True)
            note_label.setContentsMargins(0, 14, 0, 0)
            self._rows_layout.addWidget(note_label)

        self._rows_layout.addStretch(1)


# ================================================================ 상태 표시줄

class StatusStrip(QWidget):
    """창 맨 아래 한 줄. QStatusBar 를 안 쓰는 이유는 app/chrome.py 참고."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusStrip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(28)

        self._label = QLabel("준비됨")
        self._label.setObjectName("StatusText")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.addWidget(self._label)
        layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._label.setText(self._default))
        self._default = "준비됨"

    def showMessage(self, text: str, timeout: int = 0) -> None:   # noqa: N802
        """QStatusBar 와 이름을 맞춰 둔다 — 부르는 쪽 코드를 안 고쳐도 되게."""
        self._label.setText(text)
        self._timer.stop()
        if timeout > 0:
            self._timer.start(timeout)

    def clearMessage(self) -> None:                                # noqa: N802
        self._timer.stop()
        self._label.setText(self._default)


# ================================================================== 조각들

def video_frame(parent: QWidget | None = None) -> QWidget:
    """영상이 들어갈 테두리 있는 검은 판. **비어 있는 채로** 돌려준다.

    쓰는 쪽은 이 판을 부모로 삼아 mpv 위젯을 만들고 넣어야 한다 —

        holder = video_frame(stack)
        player = MpvWidget(holder)
        holder.layout().addWidget(player)

    다른 데서 만들어 온 mpv 위젯을 나중에 여기 넣으면(reparent) Qt 가 그
    네이티브 창을 부수고 다시 만든다. 비교 탭에서 그것 때문에 2초가 걸렸다.

    mpv 는 네이티브 자식 창이라 위에 위젯을 겹칠 수도 없다 (윈도우에서
    네이티브 창은 항상 형제 위젯보다 위에 그려진다). 목업에서 영상 모서리에
    떠 있던 배율·색공간 표시를 영상 바로 위 띠로 옮긴 건 그래서다.
    """
    holder = QWidget(parent)
    holder.setObjectName("VideoFrame")
    holder.setAttribute(Qt.WA_StyledBackground, True)
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(1, 1, 1, 1)
    return holder


def lock_text_height(label: QLabel) -> None:
    """글자가 바뀌어도 라벨 높이가 흔들리지 않게 못박는다.

    Consolas 에는 한글이 없다. 한글이 섞이면 그 글자만 대체 글꼴로 그려지는데,
    그쪽이 2px 더 높아서 **라벨 높이가 통째로 2px 커진다.** 비교 탭 카운터처럼
    `(어긋남 +1)` 이 붙었다 떨어졌다 하는 자리는 그 2px 때문에 줄 높이가
    오르내리고, 같은 세로 배치에 있는 영상까지 위아래로 움찔거린다.

    숫자만 있는 글자와 한글이 섞인 글자를 둘 다 재서 큰 쪽으로 고정한다.
    """
    label.ensurePolished()
    keep = label.text()
    tallest = 0
    for sample in ("0123456789", "가나다 0123"):
        label.setText(sample)
        tallest = max(tallest, label.sizeHint().height())
    label.setText(keep)
    label.setFixedHeight(tallest)


def hairline(parent: QWidget | None = None) -> QWidget:
    """1px 구분선."""
    line = QWidget(parent)
    line.setObjectName("InfoRule")
    line.setFixedHeight(1)
    return line


def label(text: str, style: str = "DimLabel") -> QLabel:
    """objectName 만 붙인 라벨 — 색은 테마가 정한다."""
    widget = QLabel(text)
    widget.setObjectName(style)
    return widget

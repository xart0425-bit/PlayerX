"""창틀 — 목업의 상단 헤더와, 그 헤더를 쓰기 위해 직접 만든 창 테두리.

목업의 창 위쪽은 `PlayerX │ 파일명 … ─ □ ✕` 한 줄이다. 윈도우가 그려 주는
제목 표시줄로는 그 모양이 안 나오므로 `FramelessWindowHint` 로 창틀을 끄고
헤더를 위젯으로 그린다. 대신 창틀이 하던 일을 우리가 해야 한다.

* **옮기기** — 헤더를 끌면 `QWindow.startSystemMove()`. 윈도우가 직접 옮기므로
  화면 가장자리로 끌었을 때 스냅(Aero Snap)도 그대로 동작한다.
* **크기 조절** — 창 가장자리 6px 띠에서 `startSystemResize()`.
* **최대화/복원** — 헤더 더블클릭.

`startSystemMove`/`startSystemResize` 를 쓰는 이유는 이게 OS 에 "지금부터 네가
끌어라"라고 넘기는 방식이라서다. 좌표를 직접 계산해 창을 옮기면 스냅·다중 모니터·
DPI 가 섞였을 때 어긋난다.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from . import icons
from .theme import C


class TitleBar(QWidget):
    """창 맨 위 한 줄."""

    open_requested = Signal()
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    HEIGHT = 46
    DRAG_SLOP = 4          # 이만큼 움직여야 '끄는 것'으로 본다 (더블클릭과 구분)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(self.HEIGHT)
        # QWidget 을 상속한 위젯은 이 속성이 있어야 스타일시트의 배경·테두리를 그린다.
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._press_pos: QPoint | None = None
        self._menu: QMenu | None = None

        logo = QLabel()
        logo.setPixmap(icons.pixmap("logo", C.ACCENT, 21))
        logo.setFixedWidth(24)

        app_name = QLabel("PlayerX")
        app_name.setObjectName("TitleAppName")

        separator = QLabel("│")
        separator.setObjectName("TitleSep")

        self.file_label = QLabel("파일이 열려 있지 않습니다")
        self.file_label.setObjectName("TitleFile")

        for widget in (logo, app_name, separator, self.file_label):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # 파일이 열려 있을 때도 '열기' 가 늘 손 닿는 자리에 있어야 한다 —
        # 메뉴 안에만 있으면 어디서 여는지 못 찾는다.
        self.open_button = QPushButton("  영상 열기")
        self.open_button.setObjectName("HeaderButton")
        self.open_button.setIcon(icons.icon("folder", C.TEXT_DIM, 16))
        self.open_button.setFixedHeight(30)
        self.open_button.setCursor(Qt.PointingHandCursor)
        self.open_button.setFocusPolicy(Qt.NoFocus)
        self.open_button.setToolTip("영상 열기 (Ctrl+O)")
        self.open_button.clicked.connect(self.open_requested)

        self.menu_button = self._icon_button("menu", "메뉴", width=44)
        self.menu_button.clicked.connect(self._show_menu)

        self._min_button = self._icon_button("win_min", "최소화")
        self._min_button.clicked.connect(self.minimize_requested)
        self._max_button = self._icon_button("win_max", "최대화")
        self._max_button.clicked.connect(self.maximize_requested)
        self._close_button = self._icon_button("win_close", "닫기", close=True)
        self._close_button.clicked.connect(self.close_requested)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(logo)
        layout.addSpacing(6)
        layout.addWidget(app_name)
        layout.addSpacing(12)
        layout.addWidget(separator)
        layout.addSpacing(12)
        layout.addWidget(self.file_label)
        layout.addStretch(1)
        layout.addWidget(self.open_button)
        layout.addSpacing(10)
        layout.addWidget(self.menu_button)
        layout.addSpacing(6)
        layout.addWidget(self._min_button)
        layout.addWidget(self._max_button)
        layout.addWidget(self._close_button)

    def _icon_button(self, name: str, tip: str, width: int = 46,
                     close: bool = False) -> QPushButton:
        button = QPushButton()
        button.setObjectName("WinClose" if close else "WinButton")
        button.setFixedSize(width, self.HEIGHT)
        button.setFocusPolicy(Qt.NoFocus)
        button.setToolTip(tip)
        button.setIcon(icons.icon(name, C.TEXT_DIM, 20))
        return button

    # ------------------------------------------------------------- 내용

    def set_file(self, text: str) -> None:
        self.file_label.setText(text)

    def set_menu(self, menu: QMenu) -> None:
        self._menu = menu

    def set_maximized(self, maximized: bool) -> None:
        self._max_button.setIcon(
            icons.icon("win_restore" if maximized else "win_max", C.TEXT_DIM, 20))
        self._max_button.setToolTip("이전 크기로" if maximized else "최대화")

    def _show_menu(self) -> None:
        if self._menu is None:
            return
        below = self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height()))
        self._menu.exec(below)

    # ------------------------------------------------------------- 끌기

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is None:
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < self.DRAG_SLOP:
            return
        self._press_pos = None
        handle = self.window().windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = None
            self.maximize_requested.emit()


class WindowFrame(QWidget):
    """창 내용을 담는 그릇이자, 가장자리 크기 조절 띠.

    가장자리 6px 를 여백으로 비워 둔다. 그 띠에는 자식 위젯이 없으므로
    마우스 이벤트가 이 위젯까지 온다 — 거기서 크기 조절을 시작한다.
    최대화 상태에서는 띠도 테두리도 없앤다.
    """

    MARGIN = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("WindowFrame")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setProperty("maximized", "false")

        self._cursor_edges: Qt.Edges | None = None

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(self.MARGIN, self.MARGIN,
                                        self.MARGIN, self.MARGIN)
        self._layout.setSpacing(0)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.addWidget(widget, stretch)

    def set_maximized(self, maximized: bool) -> None:
        margin = 0 if maximized else self.MARGIN
        self._layout.setContentsMargins(margin, margin, margin, margin)
        self.setProperty("maximized", "true" if maximized else "false")
        # 동적 속성은 다시 폴리시해야 스타일시트가 반영된다.
        self.style().unpolish(self)
        self.style().polish(self)
        if maximized:
            self._cursor_edges = None
            self.unsetCursor()

    # ------------------------------------------------------------- 가장자리

    def _edges(self, pos) -> Qt.Edges:
        if self.property("maximized") == "true":
            return Qt.Edges()
        x, y, m = pos.x(), pos.y(), self.MARGIN
        edges = Qt.Edges()
        if x <= m:
            edges |= Qt.LeftEdge
        elif x >= self.width() - m:
            edges |= Qt.RightEdge
        if y <= m:
            edges |= Qt.TopEdge
        elif y >= self.height() - m:
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edges):
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            return Qt.SizeFDiagCursor
        if edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            return Qt.SizeBDiagCursor
        if edges in (Qt.LeftEdge, Qt.RightEdge):
            return Qt.SizeHorCursor
        if edges in (Qt.TopEdge, Qt.BottomEdge):
            return Qt.SizeVerCursor
        return None

    def mouseMoveEvent(self, event) -> None:
        # 마우스가 움직일 때마다 커서를 다시 지정하지 않는다 — 바뀔 때만.
        edges = self._edges(event.position().toPoint())
        if edges == self._cursor_edges:
            return
        self._cursor_edges = edges
        cursor = self._cursor_for(edges)
        if cursor is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor)

    def leaveEvent(self, event) -> None:
        self._cursor_edges = None
        self.unsetCursor()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        edges = self._edges(event.position().toPoint())
        if not edges:
            return
        handle = self.window().windowHandle()
        if handle is not None:
            handle.startSystemResize(edges)

"""비교 탭 — 두 영상을 같은 프레임 번호로 묶어 놓고 본다.

세 가지 모드가 있고, 셋 다 "같은 프레임"을 보고 있다는 전제 위에 있다.

* **좌우 분할** — A 는 왼쪽 절반, B 는 오른쪽 절반에 통째로. 전체 구도를 나란히 본다.
* **와이프** — 둘을 같은 자리에 겹쳐 놓고 경계선 왼쪽은 A, 오른쪽은 B 만 보이게
  잘라 낸다. **같은 화면 위치**를 비교하는 모드다.
* **차분** — 두 프레임을 빼서 차이만 증폭해 보여 준다 (app/diff.py).

와이프를 어떻게 잘라 내는가
---------------------------
mpv 는 네이티브 창 안에 직접 그리기 때문에, 위젯을 반으로 줄이면 영상도 반으로
**축소**된다 — 그건 와이프가 아니다. 그래서 두 위젯을 둘 다 전체 크기로 두고
`setMask()` 로 보이는 영역만 잘라 낸다. 마스크는 크기를 안 건드리고 잘라내기만
하므로 두 영상의 배율과 위치가 정확히 겹친다. (Windows 에서 실제로 확인했다)

차분만 mpv 가 아니라 PyAV 로 그리는 이유
----------------------------------------
화면에 그려진 픽셀을 빼면 창 크기·GPU 의 색 변환이 차이에 섞인다. 인코딩 차이를
보려는 건데 렌더링 차이가 같이 나오면 쓸모가 없다. 그래서 차분은 캡쳐와 같은
경로(원본 재디코딩)를 쓴다. 계산은 워커 스레드에서 한다 — 4K 한 쌍이면
1초가 넘게 걸린다.

동기화
------
**A 가 마스터다.** A 가 움직이면 B 를 `A 프레임 + 오프셋`으로 보낸다.
기본값은 정지 + 프레임 잠금이라, 멈춘 상태에서는 항상 정확히 같은 프레임이다.
연속 재생 중에는 두 디코더가 조금씩 어긋나므로(clock drift) 주기적으로 검사해
어긋남이 허용치를 넘을 때만 B 를 강제로 끌어온다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    QObject,
    QRect,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import diff as diff_mod
from . import icons
from . import timecode
from .capture import FrameCapturer
from .player import MpvWidget, PlayerError
from .theme import C
from .widgets import TimelineBar, TransportButton, lock_text_height
from .workers import IndexWorker

MODE_SIDE = "side"
MODE_WIPE = "wipe"
MODE_DIFF = "diff"

MODES = [
    (MODE_SIDE, "좌우 분할"),
    (MODE_WIPE, "슬라이더 와이프"),
    (MODE_DIFF, "차분 (diff)"),
]

# 재생 중 드리프트를 이 주기로 검사한다. 매 프레임 확인할 필요는 없다 —
# 어긋남은 서서히 쌓인다.
DRIFT_CHECK_MS = 500

# 이만큼 넘게 어긋났을 때만 B 를 끌어온다. 1~2 프레임 어긋남까지 매번 고치면
# 그림이 계속 튀어서 오히려 보기 나쁘다.
DRIFT_TOLERANCE_FRAMES = 2

# ------------------------------------------------------------------ 차분 계산

class DiffEngine(QObject):
    """워커 스레드에 얹혀 사는 차분 계산기.

    파일마다 `FrameCapturer` 를 열어 둔 채 재사용한다. 한 프레임씩 넘겨 볼 때
    이어서 디코딩하는 빠른 경로가 살아 있어야 반응이 쓸 만해진다
    (실측 26배). PyAV 컨테이너는 스레드 안전하지 않으므로 **이 객체의 메서드는
    전부 워커 스레드에서만 실행된다** — 그래서 시그널로만 부른다.
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._capturers: dict[str, FrameCapturer] = {}

    def _capturer(self, path: str, index) -> FrameCapturer:
        cap = self._capturers.get(path)
        if cap is None or cap.index is not index:
            if cap is not None:
                cap.close()
            cap = FrameCapturer(path, index)
            self._capturers[path] = cap
        return cap

    @Slot(str, object, str, object, int, int, int)
    def compute(self, path_a: str, index_a, path_b: str, index_b,
                frame_a: int, frame_b: int, gain: int) -> None:
        try:
            result = diff_mod.compute_diff(
                self._capturer(path_a, index_a),
                self._capturer(path_b, index_b),
                frame_a, frame_b, gain,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(result)

    @Slot()
    def release(self) -> None:
        for cap in self._capturers.values():
            cap.close()
        self._capturers.clear()

# -------------------------------------------------------------------- 비교 뷰

class CompareView(QWidget):
    """A/B 두 mpv 위젯과 차분 그림을 모드에 따라 배치하는 자리.

    레이아웃 매니저를 안 쓰고 좌표를 직접 준다 — 와이프는 두 위젯을 **겹쳐** 놓는
    배치라서 어떤 레이아웃으로도 표현할 수 없다.
    """

    wipe_dragged = Signal(float)

    SEAM_WIDTH = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {C.BG_VIDEO};")
        self.setMinimumSize(480, 270)

        self.player_a = MpvWidget(self)
        self.player_b = MpvWidget(self)
        # B 의 소리는 끈다 — 두 영상이 같이 울리면 아무것도 못 듣는다.
        self.player_b.set_mute(True)

        self.diff_view = QLabel(self)
        self.diff_view.setAlignment(Qt.AlignCenter)
        self.diff_view.setStyleSheet(
            f"background-color: {C.BG_VIDEO}; color: {C.TEXT_MUTED};")
        self.diff_view.setText("두 파일을 열면 차분이 여기 표시됩니다.")
        self.diff_view.hide()

        self._mode = MODE_SIDE
        self._wipe = 0.5
        self._diff_pixmap: QPixmap | None = None

        # 경계선은 위젯이 아니다. **두 마스크 사이에 틈을 내고 그 틈으로 부모(이 위젯)가
        # 그린다.** Windows 에서 네이티브 자식 창(mpv)은 언제나 일반 위젯보다 위에
        # 그려져서, 경계선을 위젯으로 만들면 raise_() 를 해도 영상에 가린다.
        # 틈은 어느 자식도 덮지 않으므로 부모의 그림과 마우스 이벤트가 그대로 닿는다.
        self._seam_rect = QRect()
        self._dragging_seam = False
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ 상태

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._relayout()

    def set_wipe(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        if abs(fraction - self._wipe) < 1e-4:
            return
        self._wipe = fraction
        if self._mode == MODE_WIPE:
            self._relayout()

    def set_diff_image(self, image: np.ndarray | None, note: str = "") -> None:
        if image is None:
            self._diff_pixmap = None
            self.diff_view.setPixmap(QPixmap())
            self.diff_view.setText(note or "차분을 계산할 수 없습니다.")
            return
        h, w, _ = image.shape
        qimage = QImage(image.data, w, h, w * 3, QImage.Format_RGB888)
        # copy() 가 필요하다 — QImage 는 numpy 버퍼를 참조만 하는데
        # 그 배열은 이 함수가 끝나면 언제든 회수될 수 있다.
        self._diff_pixmap = QPixmap.fromImage(qimage.copy())
        self._rescale_diff()

    # ---------------------------------------------------------------- 배치

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # **배치를 미루면 안 된다.** 한때 리사이즈 중 그림 찢어짐을 줄이려고
        # 30ms 로 묶어 봤는데, 그러면 Qt 가 아는 위젯 좌표와 실제 네이티브 창
        # 위치가 어긋난다 (mpv 창이 44px 밀려 있었다). 그 상태에서는 같은
        # 좌표를 다시 줘도 Qt 가 "안 바뀌었다"며 창을 안 옮긴다.
        self._relayout()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 안 보이는 동안 크기가 바뀌었을 수 있다. 보일 때 한 번 맞춘다.
        self._relayout()

    def _relayout(self) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        if self._mode == MODE_DIFF:
            self.player_a.hide()
            self.player_b.hide()
            self._seam_rect = QRect()
            self.diff_view.setGeometry(0, 0, w, h)
            self.diff_view.show()
            self._rescale_diff()
            return

        self.diff_view.hide()
        self.player_a.show()
        self.player_b.show()

        if self._mode == MODE_SIDE:
            # 마스크를 반드시 풀어야 한다 — 와이프에서 넘어오면 잘린 채로 남는다.
            self.player_a.clearMask()
            self.player_b.clearMask()
            half = w // 2
            self.player_a.setGeometry(0, 0, half, h)
            self.player_b.setGeometry(half, 0, w - half, h)
            self._seam_rect = QRect()
            self.update()
            return

        # 와이프: 둘 다 전체 크기로 겹쳐 놓고 보이는 부분만 잘라 낸다.
        # 두 마스크 사이에 SEAM_WIDTH 만큼 틈을 남긴다 — 그게 경계선 자리다.
        seam_x = int(round(w * self._wipe))
        left = max(0, min(w - self.SEAM_WIDTH, seam_x - self.SEAM_WIDTH // 2))
        right = left + self.SEAM_WIDTH
        self.player_a.setGeometry(0, 0, w, h)
        self.player_b.setGeometry(0, 0, w, h)
        self.player_a.setMask(QRegion(0, 0, left, h))
        self.player_b.setMask(QRegion(right, 0, max(0, w - right), h))
        self._seam_rect = QRect(left, 0, self.SEAM_WIDTH, h)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._mode != MODE_WIPE or self._seam_rect.isEmpty():
            return
        painter = QPainter(self)
        painter.fillRect(self._seam_rect, QColor("#ffcc00"))

    def _rescale_diff(self) -> None:
        if self._diff_pixmap is None:
            return
        self.diff_view.setPixmap(self._diff_pixmap.scaled(
            self.diff_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # -------------------------------------------------------------- 경계 끌기

    # 마우스로 잡을 때는 경계선보다 조금 넉넉하게 인정한다 (6px 은 겨냥하기 좁다).
    GRAB_SLACK = 5

    def _on_seam(self, x: int) -> bool:
        return (self._mode == MODE_WIPE and not self._seam_rect.isEmpty()
                and self._seam_rect.left() - self.GRAB_SLACK <= x
                <= self._seam_rect.right() + self.GRAB_SLACK)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._on_seam(int(event.position().x())):
            self._dragging_seam = True
            event.accept()
            return                # 누름을 받아 두면 Qt 가 이동 이벤트를 계속 보내 준다
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x = int(event.position().x())
        if self._dragging_seam:
            self.wipe_dragged.emit(x / max(1, self.width()))
            event.accept()
            return
        self.setCursor(Qt.SizeHorCursor if self._on_seam(x) else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._dragging_seam = False
        super().mouseReleaseEvent(event)

    def shutdown(self) -> None:
        self.player_a.shutdown()
        self.player_b.shutdown()

# ------------------------------------------------------------------- 비교 탭

class ComparisonTab(QWidget):
    """A/B 파일 선택 · 공용 타임라인 · 모드 전환 · 프레임 잠금."""

    status_message = Signal(str, int)
    diff_requested = Signal(str, object, str, object, int, int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 자기 배경을 직접 칠한다. mpv 때문에 이 탭의 조상들이 전부 네이티브
        # 창으로 승격되는데, 아무도 배경을 안 칠하면 탭을 옮겼을 때 앞 탭이
        # 그려 놓은 픽셀이 그대로 남아 겹쳐 보인다 (실제로 그랬다).
        self.setAutoFillBackground(True)

        self.view = CompareView(self)
        self.view.wipe_dragged.connect(self._on_wipe_dragged)
        self.player_a = self.view.player_a
        self.player_b = self.view.player_b
        self.player_a.state_changed.connect(self._on_a_changed)
        self.player_b.state_changed.connect(self._update_labels)
        self.player_a.index_changed.connect(self._on_index_changed)
        self.player_b.index_changed.connect(self._on_index_changed)

        self._workers: dict[str, IndexWorker] = {}
        self._b_requested: int | None = None
        self._slider_busy = False
        self._diff_busy = False
        self._diff_dirty = False
        self._drift_corrections = 0

        self._build_ui()
        self._start_diff_thread()

        self._drift_timer = QTimer(self)
        self._drift_timer.setInterval(DRIFT_CHECK_MS)
        self._drift_timer.timeout.connect(self._correct_drift)
        self._drift_timer.start()

        self._update_labels()

    # ------------------------------------------------------------------ 구성

    def _build_ui(self) -> None:
        # --- A/B 파일 선택 줄 ---
        self.name_a = QLabel("— 열지 않음 —")
        self.name_b = QLabel("— 열지 않음 —")
        self.bar_a = self._make_progress()
        self.bar_b = self._make_progress()

        file_row = QHBoxLayout()
        file_row.setContentsMargins(6, 6, 6, 0)
        for tag, name_label, bar, slot in (
            ("A", self.name_a, self.bar_a, lambda: self.choose_file("A")),
            ("B", self.name_b, self.bar_b, lambda: self.choose_file("B")),
        ):
            box = QVBoxLayout()
            head = QHBoxLayout()
            tag_label = QLabel(tag)
            tag_label.setObjectName("TagA" if tag == "A" else "TagB")
            name_label.setObjectName("DimLabel")
            name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            button = QPushButton(f"  {tag} 열기")
            button.setObjectName("AccentButton")     # 이 탭에서 제일 먼저 누를 버튼
            button.setIcon(icons.icon("folder", C.ACCENT_HI, 15))
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            head.addWidget(tag_label)
            head.addWidget(name_label, 1)
            head.addWidget(button)
            box.addLayout(head)
            box.addWidget(bar)
            file_row.addLayout(box, 1)

        # --- 타임라인 ---
        self.timeline = TimelineBar()
        self.timeline.seek_requested.connect(self.goto_frame)
        self.timeline.scrub_started.connect(lambda: setattr(self, "_slider_busy", True))
        self.timeline.scrub_finished.connect(lambda: setattr(self, "_slider_busy", False))

        # --- 카운터 ---
        self.counter = QLabel("A —   B —")
        self.counter.setObjectName("MonoLabel")
        self.counter.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 어긋남·오프셋은 카운터가 아니라 이 라벨에 적는다.
        # 카운터는 Consolas 인데 Consolas 에는 한글이 없다 — 한글이 섞이는
        # 순간 그 줄의 글꼴 지표가 바뀌어서, 높이를 못박아 놔도 그 안에서
        # 글자가 1px 씩 위아래로 흔들린다 (가운데 정렬 기준이 달라지므로).
        # 그래서 **카운터에는 한글을 아예 넣지 않는다.**
        self.drift_label = QLabel("")
        self.drift_label.setObjectName("MutedLabel")

        self.state_label = QLabel("A · B 두 파일을 열어 주세요")
        self.state_label.setObjectName("MutedLabel")
        for label in (self.counter, self.drift_label, self.state_label):
            lock_text_height(label)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(8, 0, 8, 0)
        counter_row.addWidget(self.counter)
        counter_row.addSpacing(12)
        counter_row.addWidget(self.drift_label)
        counter_row.addStretch(1)
        counter_row.addWidget(self.state_label)

        # --- 조작 줄 ---
        self.mode_box = QComboBox()
        for key, label in MODES:
            self.mode_box.addItem(label, key)
        self.mode_box.setFocusPolicy(Qt.NoFocus)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)

        self.wipe_slider = QSlider(Qt.Horizontal)
        self.wipe_slider.setRange(0, 1000)
        self.wipe_slider.setValue(500)
        self.wipe_slider.setFixedWidth(140)
        self.wipe_slider.setFocusPolicy(Qt.NoFocus)
        self.wipe_slider.valueChanged.connect(lambda v: self.view.set_wipe(v / 1000.0))

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-1_000_000, 1_000_000)
        self.offset_spin.setFocusPolicy(Qt.StrongFocus)
        self.offset_spin.setToolTip(
            "B 가 A 보다 몇 프레임 뒤인가.\n"
            "시작점이 다른 두 영상을 맞출 때 쓴다 — B 프레임 = A 프레임 + 오프셋."
        )
        self.offset_spin.valueChanged.connect(self._on_offset_changed)

        self.gain_spin = QSpinBox()
        self.gain_spin.setRange(diff_mod.GAIN_MIN, diff_mod.GAIN_MAX)
        self.gain_spin.setValue(1)
        self.gain_spin.setPrefix("×")
        self.gain_spin.setFocusPolicy(Qt.StrongFocus)
        self.gain_spin.setToolTip("차분을 몇 배로 증폭해서 보여 줄지.\n"
                                  "옆에 적히는 숫자는 증폭 전 실제 값이다.")
        self.gain_spin.valueChanged.connect(self._on_gain_changed)

        self.lock_check = QCheckBox("프레임 잠금")
        self.lock_check.setChecked(True)
        self.lock_check.setFocusPolicy(Qt.NoFocus)
        self.lock_check.setToolTip("B 를 항상 'A 프레임 + 오프셋'에 맞춰 둔다.")
        self.lock_check.toggled.connect(self._on_lock_toggled)

        control_row = QHBoxLayout()
        control_row.setContentsMargins(8, 0, 8, 0)
        control_row.setSpacing(8)
        control_row.addWidget(QLabel("모드"))
        control_row.addWidget(self.mode_box)
        self.wipe_label = QLabel("경계")
        control_row.addWidget(self.wipe_label)
        control_row.addWidget(self.wipe_slider)
        self.gain_label = QLabel("증폭")
        control_row.addWidget(self.gain_label)
        control_row.addWidget(self.gain_spin)
        control_row.addSpacing(12)
        control_row.addWidget(QLabel("오프셋"))
        control_row.addWidget(self.offset_spin)
        control_row.addSpacing(12)
        control_row.addWidget(self.lock_check)
        control_row.addStretch(1)

        # --- 이동 버튼 ---
        # 재생 탭과 같은 모양의 큰 버튼을 쓴다. 세 탭에서 같은 자리·같은 크기라
        # 탭을 옮겨도 손이 기억한 자리를 그대로 누르게 된다.
        button_row = QHBoxLayout()
        button_row.setContentsMargins(8, 0, 8, 8)
        button_row.setSpacing(8)

        self.buttons: list[TransportButton] = []
        for label, key, icon_name, delta in (
            ("-10 프레임", "Shift+←", "skip_back", -10),
            ("-1 프레임", "←", "step_back", -1),
            ("재생 / 정지", "Space", "play", None),
            ("+1 프레임", "→", "step_fwd", 1),
            ("+10 프레임", "Shift+→", "skip_fwd", 10),
        ):
            button = TransportButton(label, key, icon_name=icon_name)
            button.setEnabled(False)
            if delta is None:
                button.clicked.connect(self.toggle_pause)
                self.play_button = button
            else:
                button.clicked.connect(lambda _=False, d=delta: self.step_frames(d))
            button_row.addWidget(button, 1)
            self.buttons.append(button)
        button_row.addStretch(2)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addLayout(file_row)
        root.addWidget(self.view, 1)
        root.addWidget(self.timeline)
        root.addLayout(counter_row)
        root.addLayout(control_row)
        root.addLayout(button_row)

        self._apply_mode_visibility()

    @staticmethod
    def _make_progress() -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setFixedHeight(14)
        bar.setTextVisible(True)
        bar.hide()
        return bar

    def _start_diff_thread(self) -> None:
        self._diff_thread = QThread(self)
        self._diff_engine = DiffEngine()
        self._diff_engine.moveToThread(self._diff_thread)
        self.diff_requested.connect(self._diff_engine.compute)
        self._diff_engine.done.connect(self._on_diff_done)
        self._diff_engine.failed.connect(self._on_diff_failed)
        # 스레드가 끝날 때 열어 둔 컨테이너를 닫는다 (그 스레드 안에서 닫아야 한다).
        self._diff_thread.finished.connect(self._diff_engine.release)
        self._diff_thread.start()

    # ------------------------------------------------------------------ 파일

    def choose_file(self, side: str) -> None:
        from .main_window import FILE_FILTER  # noqa: PLC0415  (순환 import 회피)

        path, _ = QFileDialog.getOpenFileName(self, f"{side} 영상 열기", "", FILE_FILTER)
        if path:
            self.open_file(side, path)

    def open_file(self, side: str, path: str) -> None:
        """A 또는 B 에 파일을 연다. 인덱싱은 백그라운드에서 각자 돈다."""
        player = self.player_a if side == "A" else self.player_b
        label = self.name_a if side == "A" else self.name_b

        self._stop_worker(side)
        try:
            player.open(path)
        except PlayerError as exc:
            self.status_message.emit(f"{side} 열기 실패: {exc}", 10000)
            label.setText("— 열기 실패 —")
            return

        label.setText(Path(path).name)
        label.setToolTip(path)
        self._b_requested = None
        self.set_playing(False)
        self._start_indexing(side, path)
        self._update_labels()

    def _start_indexing(self, side: str, path: str) -> None:
        bar = self.bar_a if side == "A" else self.bar_b
        bar.setRange(0, 1000)
        bar.setValue(0)
        bar.setFormat(f"{side} · 프레임 인덱스 준비 중…")
        bar.show()

        worker = IndexWorker(path, self)
        worker.progress.connect(lambda r, n, s=side: self._on_index_progress(s, r, n))
        worker.ready.connect(lambda idx, cached, s=side: self._on_index_ready(s, idx, cached))
        worker.failed.connect(lambda msg, s=side: self._on_index_failed(s, msg))
        worker.finished.connect(bar.hide)
        self._workers[side] = worker
        worker.start()

    def _on_index_progress(self, side: str, ratio: float, frames: int) -> None:
        bar = self.bar_a if side == "A" else self.bar_b
        if ratio < 0:
            bar.setRange(0, 0)
            bar.setFormat(f"{side} · 인덱스 생성 중… {frames:,} 프레임")
            return
        bar.setRange(0, 1000)
        bar.setValue(int(ratio * 1000))
        bar.setFormat(f"{side} · 인덱스 생성 중… %p%  ({frames:,} 프레임)")

    def _on_index_ready(self, side: str, index, from_cache: bool) -> None:
        player = self.player_a if side == "A" else self.player_b
        player.set_index(index)
        source = "캐시" if from_cache else "새로 생성"
        self.status_message.emit(
            f"{side} 프레임 인덱스 {source} — {index.count:,} 프레임"
            + ("" if index.cfr else " · VFR"), 5000)

    def _on_index_failed(self, side: str, message: str) -> None:
        self.status_message.emit(
            f"{side} 프레임 인덱스를 만들지 못했습니다 ({message}). "
            f"이 파일은 차분에 쓸 수 없습니다.", 10000)

    def _stop_worker(self, side: str) -> None:
        worker = self._workers.pop(side, None)
        if worker is None:
            return
        worker.cancel()
        if not worker.wait(3000):
            worker.terminate()
            worker.wait(1000)

    # ------------------------------------------------------------------ 상태

    @property
    def both_loaded(self) -> bool:
        return self.player_a.has_file and self.player_b.has_file

    @property
    def both_indexed(self) -> bool:
        return self.player_a.index_ready and self.player_b.index_ready

    @property
    def offset(self) -> int:
        return self.offset_spin.value()

    @property
    def locked(self) -> bool:
        return self.lock_check.isChecked()

    def _b_frame_for(self, frame_a: int) -> int:
        last = max(0, self.player_b.frame_count - 1)
        return max(0, min(frame_a + self.offset, last))

    # -------------------------------------------------------- 이동 (A 가 마스터)

    def step_frames(self, n: int) -> None:
        self.player_a.step_frames(n)

    def goto_frame(self, frame: int) -> None:
        self.player_a.seek_to_frame(frame)

    def goto_start(self) -> None:
        self.goto_frame(0)

    def goto_end(self) -> None:
        self.goto_frame(max(0, self.player_a.frame_count - 1))

    def toggle_pause(self) -> None:
        if not self.player_a.has_file:
            return
        self.set_playing(self.player_a.paused)

    def set_playing(self, playing: bool) -> None:
        """A 와 B 를 같이 재생/정지시킨다.

        토글로는 안 된다 — 한쪽만 어긋나 있으면 토글이 둘을 반대로 만든다.
        """
        for player in (self.player_a, self.player_b):
            if player.has_file:
                player.set_paused(not playing)
        if not playing:
            # 멈추는 순간에는 어긋남을 즉시 없앤다 (검수는 멈춘 그림에서 한다).
            self._b_requested = None
            self._sync_b()

    def capture_current_frame(self) -> None:
        # 비교 탭에는 캡쳐가 없다 (3단계 범위 밖). 조용히 무시하지는 않는다.
        self.status_message.emit(
            "캡쳐는 재생 탭에서 됩니다. 비교 탭 캡쳐는 아직 없습니다.", 4000)

    # ---------------------------------------------------------------- 동기화

    def _on_a_changed(self) -> None:
        self._sync_b()
        self._update_labels()

    def _sync_b(self) -> None:
        """B 를 'A 프레임 + 오프셋'으로 보낸다.

        재생 중에는 손대지 않는다 — 매 폴링마다 seek 을 걸면 B 가 계속 끊긴다.
        재생 중 어긋남은 _correct_drift() 가 주기적으로만 고친다.
        """
        if not (self.locked and self.both_loaded):
            return
        if not self.player_a.paused:
            return
        want = self._b_frame_for(self.player_a.current_frame)
        if want == self._b_requested:
            return                        # 이미 그 프레임을 요청해 뒀다
        self._b_requested = want
        self.player_b.seek_to_frame(want)
        self._request_diff()

    def _correct_drift(self) -> None:
        """3-8. 연속 재생 중 A 를 마스터로 B 를 끌어온다.

        두 디코더는 각자의 시계로 도는데 프레임레이트·GOP 구조가 다르면 조금씩
        어긋난다. 허용치를 넘을 때만 고치는 이유는, 매번 고치면 B 쪽 그림이
        계속 튀어서 비교 자체가 어려워지기 때문이다.
        """
        if not (self.locked and self.both_loaded) or self.player_a.paused:
            return
        want = self._b_frame_for(self.player_a.current_frame)
        drift = self.player_b.current_frame - want
        if abs(drift) <= DRIFT_TOLERANCE_FRAMES:
            return
        self.player_b.seek_to_frame(want, keep_playing=True)
        self._drift_corrections += 1
        self._b_requested = None

    # ------------------------------------------------------------------ 모드

    def _on_mode_changed(self) -> None:
        self.view.set_mode(self.mode_box.currentData())
        self._apply_mode_visibility()
        if self.view.mode == MODE_DIFF:
            self._request_diff()

    def _apply_mode_visibility(self) -> None:
        mode = self.mode_box.currentData()
        for widget in (self.wipe_label, self.wipe_slider):
            widget.setVisible(mode == MODE_WIPE)
        for widget in (self.gain_label, self.gain_spin):
            widget.setVisible(mode == MODE_DIFF)

    def _on_wipe_dragged(self, fraction: float) -> None:
        self.wipe_slider.blockSignals(True)
        self.wipe_slider.setValue(int(round(fraction * 1000)))
        self.wipe_slider.blockSignals(False)
        self.view.set_wipe(fraction)

    def _on_offset_changed(self) -> None:
        self._b_requested = None
        self._sync_b()
        self._update_labels()
        self._request_diff()

    def _on_gain_changed(self) -> None:
        self._request_diff()

    def _on_lock_toggled(self, on: bool) -> None:
        if on:
            self._b_requested = None
            self._sync_b()
        self._update_labels()

    def _on_index_changed(self) -> None:
        a = self.player_a
        cfr = a.index.cfr if a.index is not None else True
        self.timeline.set_media(a.frame_count if a.can_step else 0, a.fps, cfr)
        self.timeline.set_frame(a.current_frame)
        for button in self.buttons:
            button.setEnabled(self.player_a.has_file)
        self._b_requested = None
        self._sync_b()
        self._update_labels()
        self._request_diff()

    # ------------------------------------------------------------------ 차분

    def _request_diff(self) -> None:
        """차분 계산을 워커에 맡긴다. 앞선 계산이 도는 중이면 끝난 뒤 한 번만 다시 한다.

        프레임을 빠르게 넘길 때 요청이 쌓이면 한참 뒤처진 그림이 계속 올라온다.
        '지금 상태'만 중요하므로 하나만 밀어 두고 나머지는 버린다.
        """
        if self.view.mode != MODE_DIFF:
            return
        if not self.both_indexed:
            self.view.set_diff_image(
                None, "차분에는 A · B 양쪽의 프레임 인덱스가 필요합니다.\n"
                      "두 파일을 열고 인덱싱이 끝나기를 기다려 주세요.")
            return
        if self._diff_busy:
            self._diff_dirty = True
            return

        self._diff_busy = True
        frame_a = self.player_a.current_frame
        self.diff_requested.emit(
            self.player_a.path, self.player_a.index,
            self.player_b.path, self.player_b.index,
            frame_a, self._b_frame_for(frame_a), self.gain_spin.value(),
        )

    def _on_diff_done(self, result) -> None:
        self._diff_busy = False
        if self.view.mode == MODE_DIFF:
            self.view.set_diff_image(result.image)
            self.state_label.setText(result.summary())
        if self._diff_dirty:
            self._diff_dirty = False
            self._request_diff()

    def _on_diff_failed(self, message: str) -> None:
        self._diff_busy = False
        self.view.set_diff_image(None, f"차분 실패:\n{message}")
        self.status_message.emit(f"차분 실패: {message}", 8000)
        self._diff_dirty = False

    # ------------------------------------------------------------------ 표시

    def _update_labels(self) -> None:
        a, b = self.player_a, self.player_b

        if a.has_file:
            total_a = f"{a.frame_count:,}" if a.frame_count else "?"
            head = ("VFR" if (a.index is not None and not a.index.cfr)
                    else timecode.format_timecode(a.frame, a.fps))
            text_a = f"A {head} · {a.current_frame:,} / {total_a}"
        else:
            text_a = "A —"

        if b.has_file:
            total_b = f"{b.frame_count:,}" if b.frame_count else "?"
            want = self._b_frame_for(a.current_frame) if a.has_file else b.current_frame
            drift = b.current_frame - want if (self.locked and a.has_file) else 0
            text_b = f"B · {b.current_frame:,} / {total_b}"
        else:
            drift = 0
            text_b = "B —"

        # 숫자와 타임코드만 (한글 금지 — 위 drift_label 설명 참고)
        self.counter.setText(f"{text_a}    {text_b}")

        notes = []
        if drift:
            notes.append(f"어긋남 {drift:+d}")
        if self.offset:
            notes.append(f"오프셋 {self.offset:+d}")
        self.drift_label.setText(" · ".join(notes))

        if a.has_file:
            self.play_button.set_icon("play" if a.paused else "pause")
            if not self._slider_busy:
                self.timeline.set_frame(a.current_frame)

        if self.view.mode != MODE_DIFF or not self.both_indexed:
            self.state_label.setText(self._state_text())

    def _state_text(self) -> str:
        if not self.both_loaded:
            missing = []
            if not self.player_a.has_file:
                missing.append("A")
            if not self.player_b.has_file:
                missing.append("B")
            return f"{' · '.join(missing)} 파일을 열어 주세요"
        parts = ["정지" if self.player_a.paused else "재생 중"]
        parts.append("프레임 잠금" if self.locked
                     else "잠금 해제 (B 가 A 를 따라가지 않음)")
        if not self.both_indexed:
            parts.append("인덱싱 중 — 차분은 아직 못 씁니다")
        if self._drift_corrections:
            parts.append(f"드리프트 보정 {self._drift_corrections}회")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 종료

    def shutdown(self) -> None:
        self._drift_timer.stop()
        for side in ("A", "B"):
            self._stop_worker(side)
        self._diff_thread.quit()
        self._diff_thread.wait(5000)
        self.view.shutdown()

"""PlayerX 메인 창.

1단계 범위(열기 · 재생/정지 · 프레임 이동 · 프레임 번호 표시)에
2단계에서 두 가지가 붙었다.

* **프레임 인덱스** — 파일을 열면 백그라운드 스레드가 PTS 표를 만든다.
  만드는 동안에도 영상은 볼 수 있고, 진행률이 타임라인 위에 겹쳐 뜬다.
  다 되면 프레임 번호가 추정에서 인덱스 기반으로 조용히 넘어간다.
* **무손실 PNG 캡쳐** — S 또는 캡쳐 버튼. 화면이 아니라 원본 파일에서
  다시 디코딩해 저장한다 (app/capture.py).

화면 생김새는 docs/mockups 의 목업을 따른다. 창틀을 직접 그리기 때문에
(app/chrome.py) QMainWindow 의 메뉴 표시줄·상태 표시줄은 쓰지 않는다 —
메뉴는 헤더의 ☰ 버튼에, 상태 표시줄은 창 맨 아래 StatusStrip 에 있다.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import applog
from . import capture as capture_mod
from . import icons
from . import settings as settings_mod
from . import theme
from . import timecode
from .chrome import TitleBar, WindowFrame
from .compare import ComparisonTab
from .player import MpvWidget, PlayerError
from .theme import C
from .trim import TrimTab
from .widgets import (
    FieldBox,
    InfoPanel,
    StatusStrip,
    TimelineBar,
    TransportButton,
    video_frame,
)
from .workers import CaptureWorker, IndexWorker

VIDEO_SUFFIXES = {
    ".mkv", ".mp4", ".mov", ".m4v", ".avi", ".webm", ".ts", ".m2ts",
    ".mts", ".h264", ".h265", ".hevc", ".264", ".265", ".mxf", ".wmv", ".flv",
}

FILE_FILTER = (
    "영상 파일 (*.mkv *.mp4 *.mov *.m4v *.avi *.webm *.ts *.m2ts *.mts "
    "*.h264 *.h265 *.hevc *.264 *.265 *.mxf *.wmv *.flv);;모든 파일 (*.*)"
)

# (이름, 단축키 표기, 아이콘, 프레임 이동량) — 이동량이 None 이면 재생/정지 토글
STEP_BUTTONS = [
    ("-10 프레임", "Shift+←", "skip_back", -10),
    ("-1 프레임", "←", "step_back", -1),
    ("재생 / 정지", "Space", "play", None),
    ("+1 프레임", "→", "step_fwd", 1),
    ("+10 프레임", "Shift+→", "skip_fwd", 10),
]


class PlaybackTab(QWidget):
    """재생 탭 — 영상 + 타임라인 + 프레임 카운터 + 파일 정보."""

    status_message = Signal(str, int)   # 상태바에 흘릴 말 (문구, 유지 ms)
    marks_changed = Signal()            # IN/OUT 이 바뀌었다 (트림 탭이 이어받는다)
    open_requested = Signal()           # '영상 열기' 를 눌렀다

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 탭 페이지는 자기 배경을 칠한다 (MainWindow._lazy_page 설명 참고).
        self.setAutoFillBackground(True)

        self.player = MpvWidget(self)
        self.player.state_changed.connect(self._sync)
        self.player.file_loaded.connect(self._on_file_loaded)
        self.player.index_changed.connect(self._on_index_changed)

        self._slider_busy = False
        self._index_worker: IndexWorker | None = None
        self._capture_worker: CaptureWorker | None = None
        self.shot_dir = capture_mod.default_shot_dir()
        # 설정이 덮어쓴다 (MainWindow._apply_settings). None 이면 소스 비트깊이를 따른다.
        self.force_bit_depth: int | None = None

        # 재생 탭에서 찍은 IN/OUT. 트림 탭으로 넘어가면 그대로 이어받는다.
        self.mark_in: int | None = None
        self.mark_out: int | None = None

        # --- 영상 위 띠 (배율 · 색공간) ---
        self.zoom_chip = QLabel("100%")
        self.zoom_chip.setObjectName("ViewerChip")
        self.colorspace_chip = QLabel("—")
        self.colorspace_chip.setObjectName("ViewerChip")

        self.viewer_bar = QWidget()
        self.viewer_bar.setObjectName("ViewerBar")
        viewer_layout = QHBoxLayout(self.viewer_bar)
        viewer_layout.setContentsMargins(2, 0, 2, 0)
        viewer_layout.addWidget(self.zoom_chip)
        viewer_layout.addStretch(1)
        viewer_layout.addWidget(self.colorspace_chip)
        self.viewer_bar.hide()          # 파일이 있어야 뜻이 있는 표시다

        # --- 영상 자리: 파일이 없으면 안내, 있으면 영상 ---
        self.viewer_stack = QStackedWidget()
        self.viewer_stack.addWidget(self._build_empty_state())     # 0
        video = video_frame(self.viewer_stack)
        video.layout().addWidget(self.player)
        self.viewer_stack.addWidget(video)                         # 1

        # --- 타임라인 ---
        self.timeline = TimelineBar()
        self.timeline.seek_requested.connect(self._on_timeline_seek)
        self.timeline.scrub_started.connect(lambda: setattr(self, "_slider_busy", True))
        self.timeline.scrub_finished.connect(lambda: setattr(self, "_slider_busy", False))

        # --- 인덱싱 진행률 ---
        # 타임라인 위에 옅게 겹쳐 그리고(진행 정도), 글자는 이 막대가 맡는다.
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.progress.hide()

        # --- 프레임 카운터 ---
        self.counter = QLabel("--:--:--:--")
        self.counter.setObjectName("BigTimecode")
        self.counter.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.total_label = QLabel("— / —")
        self.total_label.setObjectName("FrameCount")
        self.total_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.clock = QLabel("")
        self.clock.setObjectName("MutedLabel")
        self.clock.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.state_label = QLabel("파일을 열어 주세요 (Ctrl+O 또는 창에 끌어다 놓기)")
        self.state_label.setObjectName("MutedLabel")

        self.frame_field = FieldBox("프레임", 86)
        self.frame_field.committed.connect(self._goto_frame_text)
        self.timecode_field = FieldBox("타임코드", 128)
        self.timecode_field.committed.connect(self._goto_timecode_text)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(6, 2, 6, 2)
        counter_row.setSpacing(10)
        counter_row.addWidget(self.counter)
        counter_row.addWidget(QLabel("·"))
        counter_row.addWidget(self.total_label)
        counter_row.addSpacing(10)
        counter_row.addWidget(self.clock)
        counter_row.addStretch(1)
        counter_row.addWidget(self.state_label)
        counter_row.addSpacing(14)
        counter_row.addWidget(self.frame_field)
        counter_row.addWidget(self.timecode_field)

        # --- 버튼 ---
        button_row = QHBoxLayout()
        button_row.setContentsMargins(6, 0, 6, 6)
        button_row.setSpacing(8)

        self.buttons: list[TransportButton] = []
        for label, key, icon_name, delta in STEP_BUTTONS:
            btn = TransportButton(label, key, icon_name=icon_name)
            btn.setEnabled(False)
            if delta is None:
                btn.clicked.connect(self.player.toggle_pause)
                self.play_button = btn
            else:
                btn.clicked.connect(lambda _=False, d=delta: self.player.step_frames(d))
            button_row.addWidget(btn, 1)
            self.buttons.append(btn)

        button_row.addSpacing(12)

        self.in_button = TransportButton("IN 찍기", "I", chip="IN", variant="in")
        self.in_button.clicked.connect(self.set_mark_in)
        self.out_button = TransportButton("OUT 찍기", "O", chip="OUT", variant="out")
        self.out_button.clicked.connect(self.set_mark_out)
        for btn in (self.in_button, self.out_button):
            btn.setEnabled(False)
            button_row.addWidget(btn, 1)
            self.buttons.append(btn)

        button_row.addSpacing(12)

        self.capture_button = TransportButton("PNG 캡쳐", "S", icon_name="camera",
                                              variant="accent")
        self.capture_button.setEnabled(False)
        self.capture_button.setToolTip(
            "지금 프레임을 원본 해상도 그대로 PNG 로 저장합니다.\n"
            "화면이 아니라 원본 파일에서 다시 디코딩하므로 창 크기·OSD 의 영향이 없습니다."
        )
        self.capture_button.clicked.connect(self.capture_current_frame)
        button_row.addWidget(self.capture_button, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 4, 4)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.viewer_bar)
        left_layout.addWidget(self.viewer_stack, 1)
        left_layout.addWidget(self.timeline)
        left_layout.addWidget(self.progress)
        left_layout.addLayout(counter_row)
        left_layout.addLayout(button_row)

        # --- 파일 정보 패널 ---
        self.info_panel = InfoPanel("파일 정보")
        self.info_panel.collapse_toggled.connect(self._on_info_collapsed)
        self._rebuild_info()

        panel_holder = QWidget()
        panel_layout = QVBoxLayout(panel_holder)
        panel_layout.setContentsMargins(4, 10, 10, 10)
        panel_layout.addWidget(self.info_panel)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(panel_holder)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([980, 300])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.splitter)

    def _build_empty_state(self) -> QWidget:
        """파일이 없을 때 영상 자리에 뜨는 안내.

        여는 방법이 메뉴와 단축키뿐이면 처음 켠 사람은 무엇을 눌러야 할지
        모른다. 영상이 들어올 자리에 그대로 '영상 열기' 를 놓는다.
        """
        panel = QWidget()
        panel.setObjectName("EmptyState")
        panel.setAttribute(Qt.WA_StyledBackground, True)

        icon = QLabel()
        icon.setPixmap(icons.pixmap("folder", C.TEXT_MUTED, 46))
        icon.setAlignment(Qt.AlignCenter)

        title = QLabel("영상을 열어 주세요")
        title.setObjectName("EmptyTitle")
        title.setAlignment(Qt.AlignCenter)

        button = QPushButton("  영상 열기")
        button.setObjectName("PrimaryButton")
        button.setIcon(icons.icon("folder", "#ffffff", 17))
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(self.open_requested)

        hint = QLabel("Ctrl+O 로 열거나, 창에 파일을 끌어다 놓아도 됩니다\n"
                      "mkv · mp4 · mov · avi · ts · h264 · hevc · mxf …")
        hint.setObjectName("EmptyHint")
        hint.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addStretch(1)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(button, 0, Qt.AlignCenter)
        layout.addWidget(hint)
        layout.addStretch(1)
        return panel

    # ------------------------------------------------------------------ 동작

    # 단축키가 "지금 보고 있는 탭"에 가도록 세 탭이 같은 이름의 메서드를 갖는다.
    # (MainWindow._current_tab 참고)

    def step_frames(self, n: int) -> None:
        self.player.step_frames(n)

    def goto_frame(self, frame: int) -> None:
        self.player.seek_to_frame(frame)

    def goto_start(self) -> None:
        self.player.seek_to_frame(0)

    def goto_end(self) -> None:
        self.player.seek_to_frame(max(0, self.player.frame_count - 1))

    def toggle_pause(self) -> None:
        self.player.toggle_pause()

    def open_file(self, path: str) -> None:
        self._stop_index_worker()
        self.mark_in = self.mark_out = None
        self.timeline.set_marks(None, None)
        self.player.open(path)

    # ------------------------------------------------------------- IN/OUT

    def set_mark_in(self) -> None:
        if not self.player.has_file:
            return
        self.mark_in = self.player.current_frame
        if self.mark_out is not None and self.mark_out < self.mark_in:
            self.mark_out = None      # 뒤집힌 구간은 성립하지 않는다
        self.timeline.set_marks(self.mark_in, self.mark_out)
        self.marks_changed.emit()
        self.status_message.emit(
            f"IN = {self.mark_in:,} · 트림/클립 탭으로 넘어가면 그대로 이어집니다.", 3000)

    def set_mark_out(self) -> None:
        if not self.player.has_file:
            return
        frame = self.player.current_frame
        if self.mark_in is not None and frame < self.mark_in:
            self.status_message.emit("OUT 은 IN 보다 뒤여야 합니다.", 3000)
            return
        self.mark_out = frame
        self.timeline.set_marks(self.mark_in, self.mark_out)
        self.marks_changed.emit()
        self.status_message.emit(f"OUT = {self.mark_out:,}", 3000)

    # ------------------------------------------------------------- 타임라인

    def _on_timeline_seek(self, frame: int) -> None:
        self.player.seek_to_frame(frame)

    def _goto_frame_text(self, text: str) -> None:
        frame = timecode.parse_timecode(text, 0.0)
        if frame is None:
            self.status_message.emit("프레임 번호를 숫자로 적어 주세요.", 3000)
            self._sync()
            return
        self.player.seek_to_frame(frame)

    def _goto_timecode_text(self, text: str) -> None:
        frame = timecode.parse_timecode(text, self.player.fps)
        if frame is None:
            self.status_message.emit(
                "타임코드는 HH:MM:SS:FF 로 적어 주세요 (앞자리는 생략해도 됩니다).", 4000)
            self._sync()
            return
        self.player.seek_to_frame(frame)

    def _on_file_loaded(self, path: str) -> None:
        self.viewer_stack.setCurrentIndex(1)     # 안내를 치우고 영상을 보인다
        self.viewer_bar.show()
        self._resize_slider()
        for btn in self.buttons:
            btn.setEnabled(True)
        self.capture_button.setEnabled(False)   # 인덱스가 붙어야 캡쳐할 수 있다
        self.frame_field.set_active(True)
        self.timecode_field.set_active(True)
        self._rebuild_info()
        self._sync()
        self._start_indexing(path)

    def _resize_slider(self) -> None:
        p = self.player
        cfr = p.index.cfr if p.index is not None else True
        self.timeline.set_media(p.frame_count, p.fps, cfr)
        self.timeline.set_frame(p.frame)

    def _on_info_collapsed(self, collapsed: bool) -> None:
        total = sum(self.splitter.sizes()) or 1280
        self.splitter.setSizes([total - 46, 46] if collapsed else [total - 300, 300])

    # -------------------------------------------------------------- 인덱싱

    def _start_indexing(self, path: str) -> None:
        """PTS 인덱스를 백그라운드에서 만든다 (캐시가 있으면 그걸 읽는다).

        영상은 이미 화면에 떠 있다 — 인덱싱은 프레임 번호를 추정에서
        정확한 값으로 바꾸는 작업이라 재생을 막을 이유가 없다.
        """
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("프레임 인덱스 준비 중…")
        self.progress.show()
        self.timeline.set_index_progress(0.0)

        worker = IndexWorker(path, self)
        worker.progress.connect(self._on_index_progress)
        worker.ready.connect(self._on_index_ready)
        worker.failed.connect(self._on_index_failed)
        worker.finished.connect(self._on_index_finished)
        self._index_worker = worker
        worker.start()

    def _on_index_finished(self) -> None:
        self.progress.hide()
        self.timeline.set_index_progress(None)

    def _on_index_progress(self, ratio: float, frames: int) -> None:
        if ratio < 0:
            # 길이도 파일 위치도 모르는 컨테이너 — 막대를 흐르게 두고 개수만 센다.
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"프레임 인덱스 생성 중… {frames:,} 프레임")
            self.timeline.set_index_progress(None)
            return
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(ratio * 1000))
        self.progress.setFormat(f"프레임 인덱스 생성 중… %p%  ({frames:,} 프레임)")
        self.timeline.set_index_progress(ratio)

    def _on_index_ready(self, index, from_cache: bool) -> None:
        self.player.set_index(index)
        source = "캐시" if from_cache else "새로 생성"
        vfr = "" if index.cfr else " · VFR"
        self.status_message.emit(
            f"프레임 인덱스 {source} — {index.count:,} 프레임{vfr}. "
            f"이제 프레임 번호가 PTS 기준입니다.", 6000
        )

    def _on_index_failed(self, message: str) -> None:
        # 인덱싱이 실패해도 재생은 계속된다 — fps 추정으로 물러날 뿐이다.
        self.status_message.emit(
            f"프레임 인덱스를 만들지 못했습니다 ({message}). fps 추정으로 계속합니다.", 10000
        )
        self._rebuild_info()

    def _on_index_changed(self) -> None:
        self._resize_slider()
        self.capture_button.setEnabled(self.player.index_ready)
        self._rebuild_info()
        self._sync()

    def _stop_index_worker(self) -> None:
        worker, self._index_worker = self._index_worker, None
        if worker is None:
            return
        worker.cancel()
        # demux 루프가 취소를 확인하는 주기(500 패킷)는 짧다. 잠깐만 기다린다.
        if not worker.wait(3000):
            worker.terminate()
            worker.wait(1000)

    # --------------------------------------------------------------- 캡쳐

    def capture_current_frame(self) -> None:
        """지금 프레임을 무손실 PNG 로 저장한다."""
        player = self.player
        if not player.has_file:
            return
        if not player.index_ready:
            self.status_message.emit(
                "프레임 인덱스가 아직 없습니다. 인덱싱이 끝나면 캡쳐할 수 있습니다.", 4000
            )
            return
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self.status_message.emit("앞선 캡쳐가 아직 저장 중입니다.", 2000)
            return

        frame = player.current_frame
        try:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            out_path = capture_mod.build_filename(player.path, frame, self.shot_dir)
        except OSError as exc:
            self.status_message.emit(f"캡쳐 폴더를 만들지 못했습니다: {exc}", 8000)
            return

        self.capture_button.setEnabled(False)
        self.status_message.emit(f"{frame} 번 프레임 캡쳐 중…", 0)

        worker = CaptureWorker(player.path, player.index, frame, out_path,
                               self.force_bit_depth, self)
        worker.done.connect(self._on_capture_done)
        worker.failed.connect(self._on_capture_failed)
        worker.finished.connect(
            lambda: self.capture_button.setEnabled(self.player.index_ready)
        )
        self._capture_worker = worker
        worker.start()

    def _on_capture_done(self, result) -> None:
        self.status_message.emit("캡쳐 저장: " + result.summary(), 8000)

    def _on_capture_failed(self, message: str) -> None:
        self.status_message.emit(f"캡쳐 실패: {message}", 10000)

    # --------------------------------------------------------------- 정보 패널

    def _rebuild_info(self) -> None:
        info = self.player.video_info
        if not info:
            self.info_panel.set_rows(
                [], note="파일을 열면 코덱 · 해상도 · 프레임레이트가 여기에 표시됩니다.")
            self.colorspace_chip.setText("—")
            return

        rows = [(str(key), str(value)) for key, value in info.items()]

        if self.player.index_ready:
            badge = ("인덱스 완료", True)
            note = (
                "프레임 번호와 이동은 파일에서 읽은 PTS 표를 따릅니다 — "
                "가변 프레임레이트(VFR) 파일에서도 정확합니다.\n"
                "캡쳐는 화면이 아니라 원본을 다시 디코딩해 저장합니다 "
                "(원본 해상도 · 스케일/OSD 영향 없음)."
            )
        else:
            badge = ("인덱싱 중", False)
            note = (
                "프레임 번호는 아직 time-pos x fps 로 낸 추정값입니다. "
                "인덱싱이 끝나면 PTS 기준으로 바뀝니다."
            )

        self.info_panel.set_rows(rows, badge=badge, note=note)
        self.colorspace_chip.setText(str(info.get("색공간", "—")).split(" (")[0])

    def shutdown(self) -> None:
        self._stop_index_worker()
        if self._capture_worker is not None:
            self._capture_worker.wait(5000)
        self.player.shutdown()

    def _sync(self) -> None:
        p = self.player
        if not p.has_file:
            return
        total = f"{p.frame_count:,}" if p.frame_count else "?"
        # VFR 에서는 HH:MM:SS:FF 가 성립하지 않는다 (프레임 간격이 일정하지 않으니
        # 마지막 칸이 무의미해진다). 그럴 땐 프레임 번호와 실제 시각만 보여 준다.
        if p.index is not None and not p.index.cfr:
            head = "VFR"
        else:
            head = timecode.format_timecode(p.frame, p.fps)
        self.counter.setText(head)
        self.total_label.setText(f"{p.frame:,} / {total}")
        self.clock.setText(
            f"{timecode.format_clock(p.time_pos)} / {timecode.format_clock(p.duration)}"
        )
        self.state_label.setText("정지" if p.paused else "재생 중")
        self.play_button.set_icon("play" if p.paused else "pause")
        self.frame_field.set_text(f"{p.frame}")
        self.timecode_field.set_text(head)
        if not self._slider_busy:
            self.timeline.set_frame(p.frame)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PlayerX")
        self.resize(1280, 800)
        self.setMinimumSize(940, 620)
        self.setAcceptDrops(True)
        # 창틀은 우리가 그린다 (app/chrome.py).
        self.setWindowFlag(Qt.FramelessWindowHint, True)

        # 보통은 app/main.py 가 먼저 입힌다. 검사 도구(tools/)처럼 MainWindow 를
        # 직접 만드는 진입점에서도 같은 화면이 나오도록 여기서 한 번 더 확인한다.
        app = QApplication.instance()
        if app is not None and not app.styleSheet():
            theme.apply(app)

        self.settings = settings_mod.load()

        self.playback = PlaybackTab(self)
        self.playback.player.mpv_message.connect(self._on_mpv_message)
        self.playback.status_message.connect(self._on_status_message)
        self.playback.open_requested.connect(self.choose_file)

        # 비교·트림 탭은 **처음 열 때 만든다.**
        # mpv 인스턴스 하나가 창 크기 조절을 20~35ms 씩 무겁게 만든다 (실측).
        # 셋을 다 미리 만들면 켜자마자 4개가 되고, 재생 탭만 쓰는 동안에도
        # 그 값을 계속 낸다. 처음 열 때 만들면 보통 사용에서 1개로 시작한다.
        self._compare: ComparisonTab | None = None
        self._trim: TrimTab | None = None

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.playback, icons.icon("play", C.TEXT_DIM, 16), "  재생")
        self.tabs.addTab(self._lazy_page(), icons.icon("columns", C.TEXT_DIM, 16),
                         "  비교")
        self.tabs.addTab(self._lazy_page(), icons.icon("scissors", C.TEXT_DIM, 16),
                         "  트림/클립")
        # 탭을 누른 순간(= 바뀌기 전)에 만들어 두면 빈 자리가 스치지 않는다.
        self.tabs.tabBarClicked.connect(self._ensure_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # --- 창틀 ---
        self.title_bar = TitleBar()
        self.title_bar.open_requested.connect(self.choose_file)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)

        self.status = StatusStrip()

        self.frame = WindowFrame()
        self.frame.add(self.title_bar)
        self.frame.add(self.tabs, 1)
        self.frame.add(self.status)
        self.setCentralWidget(self.frame)

        # 헤더의 파일명은 어느 경로로 열든 따라가야 한다 — 메뉴·드래그앤드롭뿐
        # 아니라 각 탭의 '열기…' 버튼으로 열었을 때도. 그래서 창이 아니라
        # 플레이어가 파일을 실제로 물었을 때를 신호로 삼는다.
        for player in self._all_players():
            player.file_loaded.connect(lambda _path: self._refresh_title())

        self._build_menu()
        self._build_shortcuts()
        self._apply_settings()
        self._warm_ffmpeg_cache()

        # 1분에 한 줄씩 살아 있다고 적는다. 앱이 조용히 사라진 적이 두 번 있는데,
        # 마지막 줄의 시각이 "언제 어떤 상태에서 죽었는가"를 알려 준다.
        # (죽는 순간을 가로채는 방식은 쓰지 않는다 — app/applog.py 설명 참고)
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(60_000)
        self._heartbeat.timeout.connect(self._log_heartbeat)
        self._heartbeat.start()
        applog.write(f"창 준비됨 · 탭 {self.tabs.count()}개")

    # QMainWindow 의 상태 표시줄 대신 우리 것을 돌려준다. 이름을 맞춰 뒀으므로
    # self.statusBar().showMessage(...) 를 쓰던 코드는 그대로 동작한다.
    def statusBar(self) -> StatusStrip:                       # noqa: N802
        return self.status

    def _log_heartbeat(self) -> None:
        players = self._all_players()
        opened = sum(1 for p in players if p.has_file)
        playing = sum(1 for p in players if p.has_file and not p.paused)
        applog.write(f"살아 있음 · 탭={self.tabs.currentIndex()} "
                     f"mpv={len(players)}개(열림 {opened} · 재생 {playing}) "
                     f"프레임={self.playback.player.frame}")

    @staticmethod
    def _warm_ffmpeg_cache() -> None:
        """`ffmpeg -encoders` 를 미리 한 번 돌려 둔다.

        트림 탭은 이 빌드에 실제로 있는 인코더만 콤보에 채우는데, 그 조회가
        외부 프로세스라 0.34초쯤 걸린다. 탭을 열 때 그 시간을 다 기다리게 하지
        말고 시작하자마자 백그라운드에서 받아 둔다 — 결과는
        `ffmpeg_loader` 안에 캐시되므로 탭은 그걸 그냥 집어 간다.

        Qt 객체를 건드리지 않으므로 평범한 스레드로 충분하다.
        """
        def probe() -> None:
            try:
                from .export import usable_encoders
                from .ffmpeg_loader import find_ffmpeg

                exe = find_ffmpeg()
                if exe is not None:
                    usable_encoders(exe)
            except Exception:
                pass          # 미리 데우기가 실패해도 탭이 열 때 다시 한다

        threading.Thread(target=probe, name="ffmpeg-warmup", daemon=True).start()

    # -------------------------------------------------------------- 늦게 만들기

    @staticmethod
    def _lazy_page() -> QWidget:
        """탭의 껍데기. 안이 비어 있다가 처음 열 때 진짜 탭이 들어온다.

        **껍데기를 두는 이유**: 탭 자체를 뺐다 끼우면(removeTab/insertTab) 안 된다.
        탭을 누른 그 순간에 QTabWidget 의 탭 목록을 건드리게 되는데, 탭바는
        아직 그 탭을 누르는 중이라 이미 사라진 위젯을 계속 쓴다 — 실제로
        세그폴트로 죽었다. 껍데기는 자리에 계속 있고, 우리는 그 **안쪽 내용만**
        바꾼다. QTabWidget 은 아무것도 모른 채 그대로 있으면 된다.
        """
        page = QWidget()
        # 탭 페이지는 자기 배경을 칠해야 한다. 안 그러면 탭을 옮겼을 때
        # 앞 탭이 그려 놓은 픽셀이 그대로 남아 겹쳐 보인다 (실제로 그랬다).
        page.setAutoFillBackground(True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("여는 중…")
        label.setObjectName("MutedLabel")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        return page

    @property
    def compare(self) -> ComparisonTab:
        """비교 탭. 처음 쓰는 순간 만들어진다 (검사 도구도 이 길로 들어온다)."""
        if self._compare is None:
            self._ensure_tab(1)
        return self._compare

    @property
    def trim(self) -> TrimTab:
        if self._trim is None:
            self._ensure_tab(2)
        return self._trim

    def _ensure_tab(self, index: int) -> None:
        if index == 1 and self._compare is None:
            page = self._empty_page(1)
            self._compare = ComparisonTab(page)
            page.layout().addWidget(self._compare)
            self._wire_compare()
        elif index == 2 and self._trim is None:
            page = self._empty_page(2)
            self._trim = TrimTab(page)
            page.layout().addWidget(self._trim)
            self._wire_trim()

    def _empty_page(self, index: int) -> QWidget:
        """껍데기에서 '여는 중…' 을 치우고 빈 페이지를 돌려준다.

        **탭을 여기서 만들어 넣는 이유**: 탭을 다른 부모로 만들어 두었다가
        나중에 이 페이지로 옮기면(reparent), 그 안의 mpv 위젯은 네이티브 창을
        가지고 있어서 창을 부수고 다시 만든다 — 실측으로 비교 탭 여는 데
        2.0초가 들었고, 그 2.0초 중 1.97초가 옮기는 데서 나왔다.
        처음부터 이 페이지의 자식으로 만들면 그 일이 아예 없다.
        """
        page = self.tabs.widget(index)
        layout = page.layout()
        while layout.count():
            item = layout.takeAt(0)
            old = item.widget()
            if old is not None:
                old.setParent(None)
                old.deleteLater()
        return page

    def _wire_compare(self) -> None:
        tab = self._compare
        tab.status_message.connect(self._on_status_message)
        for player in (tab.player_a, tab.player_b):
            player.mpv_message.connect(self._on_mpv_message)
            player.file_loaded.connect(lambda _path: self._refresh_title())
        self._apply_compare_settings()

    def _wire_trim(self) -> None:
        tab = self._trim
        tab.status_message.connect(self._on_status_message)
        tab.player.mpv_message.connect(self._on_mpv_message)
        tab.player.file_loaded.connect(lambda _path: self._refresh_title())
        self._apply_trim_settings()

    # ------------------------------------------------------------------ 구성

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            maximized = self.isMaximized()
            self.frame.set_maximized(maximized)
            self.title_bar.set_maximized(maximized)

    def _build_menu(self) -> None:
        """헤더의 ☰ 버튼에 달릴 메뉴.

        메뉴 표시줄을 쓰지 않는다 — 목업의 창 위쪽은 헤더 한 줄뿐이다.
        단축키가 걸린 항목은 창에도 직접 등록해야 메뉴를 열지 않아도 듣는다.
        """
        menu = QMenu(self)

        file_menu = menu.addMenu("파일")

        open_action = QAction("열기...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.choose_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        capture_action = QAction("현재 프레임 캡쳐 (PNG)", self)
        capture_action.setShortcut(QKeySequence("S"))
        capture_action.setShortcutContext(Qt.ApplicationShortcut)
        capture_action.triggered.connect(lambda: self._current_tab().capture_current_frame())
        file_menu.addAction(capture_action)
        self.capture_action = capture_action

        open_shots = QAction("캡쳐 폴더 열기", self)
        open_shots.triggered.connect(self._open_shot_dir)
        file_menu.addAction(open_shots)

        file_menu.addSeparator()
        quit_action = QAction("종료", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # 메뉴가 닫혀 있어도 단축키가 듣게 창에 붙인다.
        for action in (open_action, capture_action, quit_action):
            self.addAction(action)

        self._build_settings_menu(menu)
        self.title_bar.set_menu(menu)
        self.menu = menu

    def _build_settings_menu(self, parent_menu: QMenu) -> None:
        """설정 메뉴.

        별도 설정 창을 만들지 않는다 — 계획서가 "혼자 쓰는 도구라 설정 화면도
        최소로"라고 못박아 뒀고, 실제로 고를 게 몇 개 안 된다. 메뉴의 라디오
        항목이면 지금 무엇이 켜져 있는지도 같이 보인다.
        """
        menu = parent_menu.addMenu("설정")

        # --- 하드웨어 디코딩 ---
        hwdec_menu = menu.addMenu("하드웨어 디코딩")
        self._hwdec_group = QActionGroup(self)
        self._hwdec_group.setExclusive(True)
        for value, label in settings_mod.HWDEC_CHOICES:
            action = QAction(label, self, checkable=True)
            action.setData(value)
            action.triggered.connect(lambda _=False, v=value: self.set_hwdec(v))
            self._hwdec_group.addAction(action)
            hwdec_menu.addAction(action)
        hwdec_menu.addSeparator()
        note = QAction("고른 값이 이 PC 에서 안 되면 소프트웨어로 물러납니다 "
                       "— 실제로 쓰이는 건 파일 정보 패널에 나옵니다", self)
        note.setEnabled(False)
        hwdec_menu.addAction(note)

        # --- 캡쳐 ---
        depth_menu = menu.addMenu("캡쳐 비트 깊이")
        self._depth_group = QActionGroup(self)
        self._depth_group.setExclusive(True)
        for value, label in settings_mod.BIT_DEPTH_CHOICES:
            action = QAction(label, self, checkable=True)
            action.setData(value)
            action.triggered.connect(lambda _=False, v=value: self.set_capture_bit_depth(v))
            self._depth_group.addAction(action)
            depth_menu.addAction(action)

        shot_action = QAction("캡쳐 폴더 바꾸기…", self)
        shot_action.triggered.connect(self.choose_shot_dir)
        menu.addAction(shot_action)

        menu.addSeparator()
        open_settings = QAction("설정 파일 폴더 열기", self)
        open_settings.triggered.connect(
            lambda: self._open_dir(settings_mod.settings_path().parent))
        menu.addAction(open_settings)

        reset_action = QAction("설정 초기화", self)
        reset_action.triggered.connect(self.reset_settings)
        menu.addAction(reset_action)

    # ------------------------------------------------------------------ 설정

    def _apply_settings(self) -> None:
        """읽어 온 설정을 위젯들에 실제로 반영한다."""
        s = self.settings

        for action in self._hwdec_group.actions():
            action.setChecked(action.data() == s.hwdec)
        for action in self._depth_group.actions():
            action.setChecked(action.data() == s.capture_bit_depth)

        for player in self._all_players():
            player.set_hwdec(s.hwdec)

        self.playback.force_bit_depth = s.force_bit_depth
        if s.shot_dir:
            self.playback.shot_dir = Path(s.shot_dir)

        # 아직 안 만든 탭은 건드리지 않는다 — 만들 때 각자 받아 간다.
        if self._compare is not None:
            self._apply_compare_settings()
        if self._trim is not None:
            self._apply_trim_settings()

        if s.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(s.window_geometry.encode("ascii")))
            except Exception:
                pass

    def _apply_compare_settings(self) -> None:
        s, tab = self.settings, self._compare
        index = tab.mode_box.findData(s.compare_mode)
        if index >= 0:
            tab.mode_box.setCurrentIndex(index)
        tab.wipe_slider.setValue(int(round(s.wipe * 1000)))
        tab.gain_spin.setValue(s.diff_gain)
        for player in (tab.player_a, tab.player_b):
            player.set_hwdec(s.hwdec)

    def _apply_trim_settings(self) -> None:
        s, tab = self.settings, self._trim
        tab.player.set_hwdec(s.hwdec)
        if s.shot_dir:
            tab.shot_dir = Path(s.shot_dir)
        tab.force_bit_depth = s.force_bit_depth
        tab.range_check.setChecked(s.trim_range_only)
        tab.loop_check.setChecked(s.trim_loop)
        if s.trim_encoder:
            for i in range(tab.encoder_box.count()):
                choice = tab.encoder_box.itemData(i)
                if choice is not None and choice.name == s.trim_encoder:
                    tab.encoder_box.setCurrentIndex(i)
                    break

    def _collect_settings(self) -> settings_mod.Settings:
        """지금 화면 상태를 설정으로 긁어모은다 (종료할 때 저장할 값).

        안 만든 탭은 읽을 게 없다 — 지난번에 저장해 둔 값을 그대로 넘긴다.
        """
        s = self.settings
        if self._compare is not None:
            s.compare_mode = self._compare.mode_box.currentData() or s.compare_mode
            s.wipe = self._compare.wipe_slider.value() / 1000.0
            s.diff_gain = self._compare.gain_spin.value()
        if self._trim is not None:
            encoder = self._trim.encoder
            s.trim_encoder = encoder.name if encoder is not None else ""
            s.trim_range_only = self._trim.range_check.isChecked()
            s.trim_loop = self._trim.loop_check.isChecked()
        s.shot_dir = str(self.playback.shot_dir)
        s.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        return s

    def _all_players(self):
        """지금 살아 있는 플레이어만. 안 만든 탭의 것은 없다."""
        players = [self.playback.player]
        if self._compare is not None:
            players += [self._compare.player_a, self._compare.player_b]
        if self._trim is not None:
            players.append(self._trim.player)
        return tuple(players)

    # mpv 가 디코더를 다시 만들 때까지 기다릴 시간(ms).
    # 설정 직후에 읽으면 아직 이전 값이 나온다 (MpvWidget.set_hwdec 설명 참고).
    HWDEC_SETTLE_MS = 700

    def set_hwdec(self, value: str) -> None:
        self.settings.hwdec = value
        for player in self._all_players():
            player.set_hwdec(value)
        self.statusBar().showMessage(f"하드웨어 디코딩: {value} — 전환 중…", 0)
        QTimer.singleShot(self.HWDEC_SETTLE_MS, self._report_hwdec)

    def _report_hwdec(self) -> None:
        self.playback._rebuild_info()
        current = self.playback.player.video_info.get("하드웨어 디코딩", "?")
        self.statusBar().showMessage(
            f"하드웨어 디코딩: {self.settings.hwdec} — 지금 쓰이는 것: {current}", 8000)

    def set_capture_bit_depth(self, value: str) -> None:
        self.settings.capture_bit_depth = value
        self.playback.force_bit_depth = self.settings.force_bit_depth
        label = dict(settings_mod.BIT_DEPTH_CHOICES)[value]
        self.statusBar().showMessage(f"캡쳐 비트 깊이: {label}", 6000)

    def choose_shot_dir(self) -> None:
        start = str(self.playback.shot_dir)
        path = QFileDialog.getExistingDirectory(self, "캡쳐 폴더 고르기", start)
        if not path:
            return
        self.playback.shot_dir = Path(path)
        if self._trim is not None:      # 아직 안 만들었으면 만들 때 설정에서 받아 간다
            self._trim.shot_dir = Path(path)
        self.settings.shot_dir = path
        self.statusBar().showMessage(f"캡쳐 폴더: {path}", 6000)

    def reset_settings(self) -> None:
        if QMessageBox.question(self, "설정 초기화",
                                "모든 설정을 기본값으로 되돌릴까요?") != QMessageBox.Yes:
            return
        self.settings = settings_mod.Settings()
        self._apply_settings()
        self.statusBar().showMessage("설정을 기본값으로 되돌렸습니다.", 5000)

    def _open_dir(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "폴더 열기", f"폴더를 만들지 못했습니다:\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _build_shortcuts(self) -> None:
        # 슬롯이 위젯이 아니라 **지금 보고 있는 탭**을 향한다. 세 탭이 같은 이름의
        # 메서드를 갖고 있어서(step_frames·toggle_pause·goto_*) 이렇게 묶을 수 있다.
        bindings = [
            ("Space", lambda: self._current_tab().toggle_pause()),
            ("Right", lambda: self._current_tab().step_frames(1)),
            ("Left", lambda: self._current_tab().step_frames(-1)),
            ("Shift+Right", lambda: self._current_tab().step_frames(10)),
            ("Shift+Left", lambda: self._current_tab().step_frames(-10)),
            ("Ctrl+Right", lambda: self._current_tab().step_frames(100)),
            ("Ctrl+Left", lambda: self._current_tab().step_frames(-100)),
            ("Home", lambda: self._current_tab().goto_start()),
            ("End", lambda: self._current_tab().goto_end()),
            # 재생 탭과 트림 탭에서 뜻이 있다 (아래 _mark).
            ("I", lambda: self._mark("in")),
            ("O", lambda: self._mark("out")),
            ("[", lambda: self._trim_only("previous_clip")),
            ("]", lambda: self._trim_only("next_clip")),
        ]
        self._shortcuts = []
        for key, slot in bindings:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

    def _current_tab(self):
        """지금 보고 있는 탭의 **내용** 위젯.

        비교·트림은 껍데기 안에 들어 있어서 `currentWidget()` 은 껍데기를
        돌려준다. 여기서 번호로 풀어 준다 (아직 안 만들었으면 만든다).
        """
        index = self.tabs.currentIndex()
        self._ensure_tab(index)
        if index == 1:
            return self._compare
        if index == 2:
            return self._trim
        return self.playback

    def _mark(self, which: str) -> None:
        """I · O — 재생 탭과 트림 탭 양쪽에서 쓴다.

        재생 탭에서 찍은 구간은 트림 탭으로 넘어갈 때 그대로 이어진다
        (_on_tab_changed).
        """
        current = self._current_tab()
        if current is self.playback:
            (self.playback.set_mark_in if which == "in"
             else self.playback.set_mark_out)()
            return
        if current is self._trim:
            (self._trim.mark_in if which == "in" else self._trim.mark_out)()
            return
        self.statusBar().showMessage("I · O 는 재생 탭과 트림/클립 탭의 키입니다.", 3000)

    def _trim_only(self, method: str) -> None:
        """[ · ] 는 트림 탭에서만 뜻이 있다.

        다른 탭에서 눌렀을 때 아무 반응이 없으면 고장으로 보이니, 어디서 쓰는
        키인지 알려 준다.
        """
        current = self._current_tab()
        if current is self._trim:
            getattr(current, method)()
            return
        self.statusBar().showMessage("[ · ] 는 트림/클립 탭의 키입니다.", 3000)

    def _on_tab_changed(self, index: int) -> None:
        # 키보드로 옮겼을 때를 위해 여기서도 확인한다 (탭을 눌렀을 때는
        # tabBarClicked 가 이미 만들어 뒀다).
        self._ensure_tab(index)

        # 탭을 옮기면 나머지는 멈춘다. 안 보이는 영상이 계속 도는 건
        # 소리도 그렇고 디코딩 부하도 그렇고 아무 이득이 없다.
        current = self._current_tab()
        if current is not self.playback:
            self.playback.player.pause()
        if self._compare is not None and current is not self._compare:
            self._compare.set_playing(False)
        if self._trim is not None and current is not self._trim:
            self._trim.player.pause()

        # 트림 탭에 아직 파일이 없으면 재생 탭에서 보던 것을 이어받는다.
        # 인덱스는 캐시돼 있어 즉시 열리고, 같은 영상을 다시 고르는 수고가 없다.
        if current is self._trim:
            if not self._trim.player.has_file and self.playback.player.path:
                self._trim.open_file(self.playback.player.path)
            self._hand_over_marks()

        self._refresh_title()

        if current is self._compare:
            message = "비교 탭 — A · B 파일을 열면 같은 프레임 번호로 묶입니다."
        elif current is self._trim:
            message = "트림/클립 탭 — I 로 IN, O 로 OUT. [ ] 로 클립 사이를 오갑니다."
        elif current is self.playback:
            message = "재생 탭"
        else:
            message = ""
        self.statusBar().showMessage(message, 4000)

    def _refresh_title(self) -> None:
        """헤더와 창 제목을 지금 보고 있는 탭의 파일에 맞춘다."""
        def name_of(player) -> str:
            return Path(player.path).name if player.has_file else "—"

        current = self._current_tab()
        if current is self._compare:
            text = (f"A {name_of(self._compare.player_a)}"
                    f"   ·   B {name_of(self._compare.player_b)}")
        elif current is self._trim:
            text = name_of(self._trim.player)
        else:
            text = name_of(self.playback.player)

        if text in ("—", "A —   ·   B —"):
            text = "파일이 열려 있지 않습니다"
        self.title_bar.set_file(text)
        self.setWindowTitle("PlayerX" if text.startswith("파일이") else f"PlayerX — {text}")

    def _hand_over_marks(self) -> None:
        """재생 탭에서 찍은 IN/OUT 을 트림 탭에 넘긴다 (같은 파일일 때만)."""
        if self._trim is None:
            return
        if self.playback.mark_in is None and self.playback.mark_out is None:
            return
        if self._trim.player.path != self.playback.player.path:
            return
        if self._trim.pending_in is None and self._trim.pending_out is None:
            self._trim.pending_in = self.playback.mark_in
            self._trim.pending_out = self.playback.mark_out
            self._trim._update_labels()

    # ------------------------------------------------------------------ 파일

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "영상 열기", "", FILE_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        # 비교 탭을 보고 있으면 그쪽으로 보낸다. 빈 자리부터 채우고,
        # 둘 다 차 있으면 A 를 갈아 끼운다 (A 가 마스터라 보통 그쪽을 바꾼다).
        if self._current_tab() is self._compare:
            side = "A" if not self._compare.player_a.has_file else (
                "B" if not self._compare.player_b.has_file else "A")
            self._compare.open_file(side, path)
            return
        if self._current_tab() is self._trim:
            self._trim.open_file(path)
            return

        try:
            self.playback.open_file(path)
        except PlayerError as exc:
            QMessageBox.critical(self, "열기 실패", str(exc))
            self.statusBar().showMessage(f"열기 실패: {Path(path).name}")
            return
        p = self.playback.player
        self._refresh_title()
        self.statusBar().showMessage(
            f"{Path(path).name} · {p.fps:.6g} fps · {p.frame_count:,} 프레임 (추정) "
            f"· 프레임 인덱스 준비 중…"
        )

    # -------------------------------------------------------------- 드래그앤드롭

    def dragEnterEvent(self, event) -> None:
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        path = self._dropped_path(event)
        if path is not None:
            event.acceptProposedAction()
            self.open_path(path)

    @staticmethod
    def _dropped_path(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if not isinstance(url, QUrl) or not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES:
                return str(p)
        return None

    # ------------------------------------------------------------------ 기타

    def _on_mpv_message(self, text: str) -> None:
        # mpv 경고/에러는 상태바에 흘린다. 조용히 실패하는 게 제일 나쁘다.
        self.statusBar().showMessage(text, 8000)

    def _on_status_message(self, text: str, timeout: int) -> None:
        self.statusBar().showMessage(text, timeout)

    def _open_shot_dir(self) -> None:
        self._open_dir(self.playback.shot_dir)

    def closeEvent(self, event) -> None:
        # 설정을 먼저 저장한다. 위젯을 정리한 뒤에는 상태를 읽을 수 없다.
        settings_mod.save(self._collect_settings())
        self.playback.shutdown()
        for tab in (self._compare, self._trim):
            if tab is not None:
                tab.shutdown()
        super().closeEvent(event)

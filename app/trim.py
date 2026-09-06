"""트림/클립 탭 — IN/OUT 을 찍어 구간을 모으고, 잘라 낸다.

왼쪽에 클립 목록, 오른쪽에 미리보기. 아래에 "무손실로 내보내기"와
"정확히 자르기(재인코딩)" 두 버튼을 나란히 둔다.

두 버튼을 나란히 두는 게 중요하다
--------------------------------
무손실 컷은 **시작점이 키프레임으로 밀린다.** 이건 고칠 수 있는 버그가 아니라
"다시 인코딩하지 않는다"는 선택의 대가다. 그래서 둘 중 하나를 몰래 고르지 않고,
버튼을 나란히 두고 **누르기 전에 결과를 문장으로 미리 보여 준다** —

    무손실 · 실제로는 60 번부터 시작합니다 (요청 100 보다 40 프레임 앞, ...) · 91 프레임
    정확 · 100 ~ 150 (51 프레임) · 재인코딩 무손실 (ffv1 · 화질 그대로, 파일이 크다)

이 예고가 가능한 건 2단계 PTS 인덱스에 키프레임 목록을 같이 저장해 뒀기 때문이다
(`FrameIndex.nearest_keyframe`). 목록에도 클립마다 "무손실 시작" 칸을 둬서,
어떤 구간이 키프레임에 잘 맞고 어떤 구간이 많이 밀리는지 한눈에 보이게 한다.

동작
----
`I` 로 IN, `O` 로 OUT 을 찍는다. IN 이 이미 있는 상태에서 `O` 를 누르면
**그 자리에서 클립이 목록에 들어간다** (한 구간에 두 번만 누르면 끝난다).
`[` `]` 로 클립 사이를 오간다. 목록은 `<영상파일>.clips.json` 으로 저장한다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import clips as clips_mod
from . import export as export_mod
from . import icons
from . import timecode
from .clips import Clip, ClipsError
from .capture import default_shot_dir
from .ffmpeg_loader import FFmpegNotFound, find_ffmpeg, require_ffmpeg
from .player import MpvWidget, PlayerError
from .theme import C
from .widgets import TimelineBar, TransportButton, lock_text_height, video_frame
from .workers import ExportWorker, IndexWorker, SequenceWorker

COLUMNS = ["#", "IN", "OUT", "길이", "무손실 시작", "이름"]
COL_NAME = 5

# 무손실 컷이 이만큼 넘게 밀리면 목록에서 눈에 띄게 표시한다.
# 한두 프레임은 어차피 신경 안 쓰지만 수십 프레임이면 결정이 달라진다.
SHIFT_WARN_FRAMES = 5


class TrimTab(QWidget):
    """트림/클립 탭."""

    status_message = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 자기 배경을 직접 칠한다 (compare.ComparisonTab 설명 참고).
        self.setAutoFillBackground(True)

        self.player = MpvWidget(self)
        # 구간을 먼저 붙잡고 나서 표시를 갱신한다 (연결 순서 = 호출 순서).
        self.player.state_changed.connect(self._enforce_range)
        self.player.state_changed.connect(self._update_labels)
        self.player.index_changed.connect(self._on_index_changed)

        self.clips: list[Clip] = []
        self.pending_in: int | None = None
        self.pending_out: int | None = None
        self._index_worker: IndexWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._sequence_worker: SequenceWorker | None = None
        self._slider_busy = False
        self._ffmpeg = find_ffmpeg()
        # 설정이 덮어쓴다 (MainWindow._apply_settings).
        self.shot_dir = default_shot_dir()
        self.force_bit_depth: int | None = None

        self._build_ui()
        self._refresh_encoders()
        self._update_labels()

    # ------------------------------------------------------------------ 구성

    def _build_ui(self) -> None:
        # --- 파일 줄 ---
        self.name_label = QLabel("— 열지 않음 —")
        self.name_label.setObjectName("DimLabel")
        self.name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_button = QPushButton("  영상 열기")
        open_button.setObjectName("AccentButton")
        open_button.setIcon(icons.icon("folder", C.ACCENT_HI, 15))
        open_button.setCursor(Qt.PointingHandCursor)
        open_button.setFocusPolicy(Qt.NoFocus)
        open_button.clicked.connect(self.choose_file)

        self.save_button = QPushButton("클립 저장…")
        self.load_button = QPushButton("클립 불러오기…")
        for button, slot in ((self.save_button, self.save_clips),
                             (self.load_button, self.load_clips)):
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            button.setEnabled(False)

        file_row = QHBoxLayout()
        file_row.setContentsMargins(6, 6, 6, 0)
        file_row.addWidget(QLabel("파일"))
        file_row.addWidget(self.name_label, 1)
        file_row.addWidget(open_button)
        file_row.addSpacing(16)
        file_row.addWidget(self.save_button)
        file_row.addWidget(self.load_button)

        # --- 클립 목록 ---
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFocusPolicy(Qt.ClickFocus)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        for col in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)

        self.delete_button = QPushButton("선택 삭제")
        self.clear_button = QPushButton("전체 삭제")
        for button, slot in ((self.delete_button, self.delete_selected),
                             (self.clear_button, self.clear_clips)):
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            button.setEnabled(False)

        # --- 고른 클립 재생 방식 ---
        self.range_check = QCheckBox("선택 구간만 재생")
        self.range_check.setChecked(True)
        self.range_check.setFocusPolicy(Qt.NoFocus)
        self.range_check.setToolTip(
            "목록에서 고른 클립의 IN ~ OUT 안에서만 재생합니다.\n"
            "타임라인을 손으로 끄는 건 구간 밖으로도 됩니다."
        )
        self.loop_check = QCheckBox("구간 반복")
        self.loop_check.setChecked(True)
        self.loop_check.setFocusPolicy(Qt.NoFocus)
        self.loop_check.setToolTip(
            "구간 끝에 닿으면 IN 으로 돌아가 계속 재생합니다.\n"
            "끄면 OUT 에서 멈춥니다."
        )
        self.loop_check.setEnabled(self.range_check.isChecked())
        self.range_check.toggled.connect(self.loop_check.setEnabled)

        play_row = QHBoxLayout()
        play_row.setContentsMargins(0, 0, 0, 0)
        play_row.addWidget(self.range_check)
        play_row.addSpacing(12)
        play_row.addWidget(self.loop_check)
        play_row.addStretch(1)

        list_buttons = QHBoxLayout()
        list_buttons.setContentsMargins(0, 0, 0, 0)
        list_buttons.addWidget(self.delete_button)
        list_buttons.addWidget(self.clear_button)
        list_buttons.addStretch(1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 0, 3, 0)
        left_layout.setSpacing(4)
        list_title = QLabel("클립 목록  (I: IN · O: OUT · [ ]: 클립 이동)")
        list_title.setObjectName("SectionLabel")
        left_layout.addWidget(list_title)
        left_layout.addWidget(self.table, 1)
        left_layout.addLayout(play_row)
        left_layout.addLayout(list_buttons)

        # --- 미리보기 ---
        self.timeline = TimelineBar()
        self.timeline.seek_requested.connect(self.goto_frame)
        self.timeline.scrub_started.connect(lambda: setattr(self, "_slider_busy", True))
        self.timeline.scrub_finished.connect(lambda: setattr(self, "_slider_busy", False))

        self.index_bar = QProgressBar()
        self.index_bar.setRange(0, 1000)
        self.index_bar.setFixedHeight(14)
        self.index_bar.hide()

        self.counter = QLabel("—")
        self.counter.setObjectName("MonoLabel")
        self.counter.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mark_label = QLabel("IN —   OUT —")
        self.mark_label.setObjectName("MarkLabel")
        # 글자에 한글이 붙었다 떨어졌다 해도 줄 높이가 안 흔들리게 (compare 참고).
        for label in (self.counter, self.mark_label):
            lock_text_height(label)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(4, 0, 4, 0)
        counter_row.addWidget(self.counter)
        counter_row.addStretch(1)
        counter_row.addWidget(self.mark_label)

        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(4, 0, 4, 4)
        mark_row.setSpacing(8)

        self.in_button = TransportButton("IN 찍기", "I", chip="IN", variant="in")
        self.in_button.clicked.connect(self.mark_in)
        self.out_button = TransportButton("OUT 찍기", "O", chip="OUT", variant="out")
        self.out_button.clicked.connect(self.mark_out)
        self.add_button = TransportButton("클립 추가", "IN+OUT", icon_name="plus")
        self.add_button.clicked.connect(self.add_pending_clip)
        for button in (self.in_button, self.out_button, self.add_button):
            button.setEnabled(False)
            mark_row.addWidget(button, 1)

        mark_row.addSpacing(12)

        self.step_buttons: list[TransportButton] = []
        for label, key, icon_name, delta in (
            ("-10 프레임", "Shift+←", "skip_back", -10),
            ("-1 프레임", "←", "step_back", -1),
            ("재생 / 정지", "Space", "play", None),
            ("+1 프레임", "→", "step_fwd", 1),
            ("+10 프레임", "Shift+→", "skip_fwd", 10),
        ):
            button = TransportButton(label, key, icon_name=icon_name)
            if delta is None:
                button.clicked.connect(self.toggle_pause)
                self.play_button = button
            else:
                button.clicked.connect(lambda _=False, d=delta: self.step_frames(d))
            mark_row.addWidget(button, 1)
            self.step_buttons.append(button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(3, 0, 6, 0)
        right_layout.setSpacing(5)
        video = video_frame(right)
        video.layout().addWidget(self.player)
        right_layout.addWidget(video, 1)
        right_layout.addWidget(self.timeline)
        right_layout.addWidget(self.index_bar)
        right_layout.addLayout(counter_row)
        right_layout.addLayout(mark_row)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 900])

        # --- 내보내기 줄 ---
        self.plan_label = QLabel("클립을 고르면 어떻게 잘릴지 여기에 미리 나옵니다.")
        self.plan_label.setWordWrap(True)
        self.plan_label.setObjectName("DimLabel")

        self.encoder_box = QComboBox()
        self.encoder_box.setFocusPolicy(Qt.NoFocus)
        self.encoder_box.currentIndexChanged.connect(self._update_plan_label)

        self.lossless_button = QPushButton("무손실로 내보내기")
        self.lossless_button.setToolTip(
            "다시 인코딩하지 않고 컨테이너만 새로 묶습니다.\n"
            "빠르고 화질이 원본 그대로지만, 시작점이 가장 가까운 키프레임으로 밀립니다."
        )
        self.precise_button = QPushButton("정확히 자르기 (재인코딩)")
        self.precise_button.setToolTip(
            "IN 프레임부터 정확히 시작합니다.\n"
            "다시 인코딩하므로 시간이 걸립니다. 인코더를 무손실(ffv1)로 두면 화질은 그대로입니다."
        )
        for button, mode in ((self.lossless_button, export_mod.MODE_LOSSLESS),
                             (self.precise_button, export_mod.MODE_PRECISE)):
            button.setFocusPolicy(Qt.NoFocus)
            button.setMinimumWidth(170)
            button.setEnabled(False)
            button.clicked.connect(lambda _=False, m=mode: self.export_selected(m))

        self.sequence_button = QPushButton("PNG 시퀀스로")
        self.sequence_button.setFocusPolicy(Qt.NoFocus)
        self.sequence_button.setMinimumWidth(130)
        self.sequence_button.setEnabled(False)
        self.sequence_button.setToolTip(
            "구간의 모든 프레임을 무손실 PNG 한 장씩으로 저장합니다.\n"
            "ffmpeg 이 아니라 캡쳐와 같은 경로(원본 재디코딩)를 씁니다 —\n"
            "원본 해상도, 10bit 소스는 16bit PNG."
        )
        self.sequence_button.clicked.connect(self.export_sequence)

        self.export_bar = QProgressBar()
        self.export_bar.setRange(0, 1000)
        self.export_bar.setFixedHeight(16)
        self.export_bar.hide()

        export_row = QHBoxLayout()
        export_row.setContentsMargins(6, 0, 6, 6)
        export_row.addWidget(QLabel("정확 컷 인코더"))
        export_row.addWidget(self.encoder_box)
        export_row.addSpacing(12)
        export_row.addWidget(self.lossless_button)
        export_row.addWidget(self.precise_button)
        export_row.addWidget(self.sequence_button)
        export_row.addWidget(self.export_bar, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(5)
        root.addLayout(file_row)
        root.addWidget(self.splitter, 1)
        root.addWidget(self.plan_label)
        root.addLayout(export_row)

    def _refresh_encoders(self) -> None:
        """ffmpeg 빌드에 실제로 있는 인코더만 콤보에 채운다."""
        self.encoder_box.clear()
        if self._ffmpeg is None:
            self.encoder_box.addItem("ffmpeg.exe 가 없습니다", None)
            self.encoder_box.setEnabled(False)
            return
        choices = export_mod.usable_encoders(self._ffmpeg)
        if not choices:
            self.encoder_box.addItem("쓸 수 있는 인코더가 없습니다", None)
            self.encoder_box.setEnabled(False)
            return
        for choice in choices:
            self.encoder_box.addItem(choice.label, choice)
        self.encoder_box.setEnabled(True)

    @property
    def encoder(self):
        return self.encoder_box.currentData()

    # ------------------------------------------------------------------ 파일

    def choose_file(self) -> None:
        from .main_window import FILE_FILTER  # noqa: PLC0415  (순환 import 회피)

        path, _ = QFileDialog.getOpenFileName(self, "영상 열기", "", FILE_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        self._stop_index_worker()
        try:
            self.player.open(path)
        except PlayerError as exc:
            self.status_message.emit(f"열기 실패: {exc}", 10000)
            return

        self.name_label.setText(Path(path).name)
        self.name_label.setToolTip(path)
        self.clips = []
        self.pending_in = self.pending_out = None
        self._rebuild_table()
        self._start_indexing(path)
        self._update_labels()

        # 같은 영상의 클립 목록이 옆에 있으면 자동으로 읽어 온다.
        # 트림은 여러 번에 걸쳐 하는 작업이라, 다시 열 때마다 불러오기를
        # 눌러야 하면 목록이 있으나 마나다.
        sidecar = clips_mod.clips_path(path)
        if sidecar.is_file():
            self._load_clips_from(sidecar, announce=f"클립 목록을 함께 열었습니다: {sidecar.name}")

    def _start_indexing(self, path: str) -> None:
        self.index_bar.setRange(0, 1000)
        self.index_bar.setValue(0)
        self.index_bar.setFormat("프레임 인덱스 준비 중…")
        self.index_bar.show()

        worker = IndexWorker(path, self)
        worker.progress.connect(self._on_index_progress)
        worker.ready.connect(self._on_index_ready)
        worker.failed.connect(self._on_index_failed)
        worker.finished.connect(self.index_bar.hide)
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, ratio: float, frames: int) -> None:
        if ratio < 0:
            self.index_bar.setRange(0, 0)
            self.index_bar.setFormat(f"인덱스 생성 중… {frames:,} 프레임")
            return
        self.index_bar.setRange(0, 1000)
        self.index_bar.setValue(int(ratio * 1000))
        self.index_bar.setFormat(f"인덱스 생성 중… %p%  ({frames:,} 프레임)")

    def _on_index_ready(self, index, from_cache: bool) -> None:
        self.player.set_index(index)
        self.status_message.emit(
            f"프레임 인덱스 {'캐시' if from_cache else '새로 생성'} — "
            f"{index.count:,} 프레임 · 키프레임 {len(index.keyframes):,}개", 5000)

    def _on_index_failed(self, message: str) -> None:
        self.status_message.emit(
            f"프레임 인덱스를 만들지 못했습니다 ({message}). "
            f"키프레임을 몰라서 자르기를 쓸 수 없습니다.", 10000)

    def _stop_index_worker(self) -> None:
        worker, self._index_worker = self._index_worker, None
        if worker is None:
            return
        worker.cancel()
        if not worker.wait(3000):
            worker.terminate()
            worker.wait(1000)

    # ------------------------------------------------------------ 이동/재생

    def step_frames(self, n: int) -> None:
        self.player.step_frames(n)

    def goto_frame(self, frame: int) -> None:
        self.player.seek_to_frame(frame)

    def goto_start(self) -> None:
        self.goto_frame(0)

    def goto_end(self) -> None:
        self.goto_frame(max(0, self.player.frame_count - 1))

    # -------------------------------------------------------- 구간 재생

    def play_range(self) -> Clip | None:
        """지금 재생을 가둬 둘 구간. '선택 구간만 재생'이 꺼져 있으면 None.

        타임라인을 손으로 끄는 건 구간 밖으로도 된다 — 가두는 건 '재생'뿐이다.
        검수 중에 구간 바깥을 잠깐 확인하는 일이 흔해서 그렇게 뒀다.
        """
        if not self.player.has_file or not self.range_check.isChecked():
            return None
        return self.selected_clip()

    def toggle_pause(self) -> None:
        clip = self.play_range()
        if clip is not None and self.player.paused:
            frame = self.player.current_frame
            # 구간 밖이거나 이미 끝에 있으면 IN 부터 다시 튼다.
            if frame < clip.in_frame or frame >= clip.out_frame:
                self.player.seek_to_frame(clip.in_frame)
            self.status_message.emit(
                f"구간 재생 {clip.in_frame:,} ~ {clip.out_frame:,}"
                + (" · 반복" if self.loop_check.isChecked() else ""), 3000)
        self.player.toggle_pause()

    def _enforce_range(self) -> None:
        """재생이 고른 구간을 넘어가지 않게 붙잡는다 (폴링마다 확인)."""
        if self.player.paused:
            return
        clip = self.play_range()
        if clip is None or self.player.seek_pending:
            return
        if self.player.current_frame < clip.out_frame:
            return
        if self.loop_check.isChecked():
            self.player.seek_to_frame(clip.in_frame, keep_playing=True)
            return
        self.player.set_paused(True)
        self.player.seek_to_frame(clip.out_frame)

    def capture_current_frame(self) -> None:
        self.status_message.emit(
            "캡쳐는 재생 탭에서 됩니다. 트림 탭 캡쳐는 아직 없습니다.", 4000)

    # ------------------------------------------------------------ IN/OUT 마킹

    def mark_in(self) -> None:
        if not self.player.has_file:
            return
        self.pending_in = self.player.current_frame
        if self.pending_out is not None and self.pending_out < self.pending_in:
            self.pending_out = None      # 뒤집힌 구간은 성립하지 않는다
        self.status_message.emit(f"IN = {self.pending_in}", 2500)
        self._update_labels()

    def mark_out(self) -> None:
        """OUT 을 찍는다. IN 이 이미 있으면 **그 자리에서 클립이 된다.**

        한 구간을 잡는 데 키를 두 번만 누르면 끝나게 하려는 것이다.
        IN 이 없으면 OUT 만 기억해 뒀다가 나중에 IN 과 맞춘다.
        """
        if not self.player.has_file:
            return
        self.pending_out = self.player.current_frame
        if self.pending_in is not None and self.pending_out >= self.pending_in:
            self.add_pending_clip()
            return
        self.status_message.emit(
            f"OUT = {self.pending_out} · IN 을 찍으면 클립이 됩니다", 3000)
        self._update_labels()

    def add_pending_clip(self) -> None:
        if self.pending_in is None or self.pending_out is None:
            self.status_message.emit("IN 과 OUT 을 둘 다 찍어야 클립이 됩니다.", 3000)
            return
        if self.pending_out < self.pending_in:
            self.status_message.emit("OUT 이 IN 보다 앞입니다.", 3000)
            return

        clip = Clip(self.pending_in, self.pending_out)
        self.clips.append(clip)
        self.clips.sort(key=lambda c: (c.in_frame, c.out_frame))
        self.pending_in = self.pending_out = None
        self._rebuild_table()
        self._select_clip(self.clips.index(clip))
        self.status_message.emit(
            f"클립 추가: {clip.in_frame} ~ {clip.out_frame} ({clip.length} 프레임)", 4000)

    # -------------------------------------------------------------- 클립 목록

    def _rebuild_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.clips))
        index = self.player.index
        for row, clip in enumerate(self.clips):
            if index is not None:
                keyframe = index.nearest_keyframe(clip.in_frame)
                shift = clip.in_frame - keyframe
                shift_text = f"{keyframe}" + (f"  ({shift:+d})" if shift else "  (정확)")
            else:
                shift = 0
                shift_text = "—"
            values = [str(row + 1), str(clip.in_frame), str(clip.out_frame),
                      str(clip.length), shift_text, clip.name]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col != COL_NAME:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                # 무손실 컷이 많이 밀리는 구간은 눈에 띄게 — 그때는 정확 컷을
                # 골라야 한다는 신호다.
                if col == 4 and shift > SHIFT_WARN_FRAMES:
                    item.setForeground(QColor("#ff9955"))
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self._update_buttons()
        self._update_plan_label()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        # 편집할 수 있는 칸은 이름뿐이다.
        if item.column() != COL_NAME:
            return
        row = item.row()
        if 0 <= row < len(self.clips):
            self.clips[row].name = item.text()

    def _on_selection_changed(self) -> None:
        clip = self.selected_clip()
        if clip is not None:
            self.goto_frame(clip.in_frame)
        self._update_labels()      # 타임라인의 구간 표시를 고른 클립에 맞춘다
        self._update_plan_label()

    def selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def selected_clip(self) -> Clip | None:
        row = self.selected_row()
        return self.clips[row] if 0 <= row < len(self.clips) else None

    def _select_clip(self, row: int) -> None:
        if 0 <= row < len(self.clips):
            self.table.selectRow(row)

    def delete_selected(self) -> None:
        row = self.selected_row()
        if row < 0:
            return
        gone = self.clips.pop(row)
        self._rebuild_table()
        self._select_clip(min(row, len(self.clips) - 1))
        self.status_message.emit(f"클립 삭제: {gone.in_frame} ~ {gone.out_frame}", 3000)

    def clear_clips(self) -> None:
        if not self.clips:
            return
        if QMessageBox.question(self, "전체 삭제",
                                f"클립 {len(self.clips)}개를 모두 지울까요?") != QMessageBox.Yes:
            return
        self.clips = []
        self._rebuild_table()

    def previous_clip(self) -> None:
        """`[` — 이전 클립으로. 아무것도 안 골라 뒀으면 마지막 클립으로."""
        self._move_clip(-1)

    def next_clip(self) -> None:
        """`]` — 다음 클립으로."""
        self._move_clip(+1)

    def _move_clip(self, delta: int) -> None:
        if not self.clips:
            self.status_message.emit("클립이 없습니다. I · O 로 구간을 찍어 주세요.", 3000)
            return
        row = self.selected_row()
        if row < 0:
            row = 0 if delta > 0 else len(self.clips) - 1
        else:
            row = max(0, min(row + delta, len(self.clips) - 1))
        self._select_clip(row)
        clip = self.clips[row]
        self.status_message.emit(
            f"클립 {row + 1}/{len(self.clips)} · {clip.in_frame} ~ {clip.out_frame}", 3000)

    # -------------------------------------------------------- 클립 저장/불러오기

    def save_clips(self) -> None:
        if not self.player.has_file:
            return
        default = str(clips_mod.clips_path(self.player.path))
        path, _ = QFileDialog.getSaveFileName(
            self, "클립 목록 저장", default, "클립 목록 (*.clips.json);;모든 파일 (*.*)")
        if not path:
            return
        try:
            out = clips_mod.save_clips(path, self.clips, self.player.path, self.player.index)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))
            return
        self.status_message.emit(f"클립 {len(self.clips)}개 저장: {out.name}", 5000)

    def load_clips(self) -> None:
        if not self.player.has_file:
            return
        start = str(clips_mod.clips_path(self.player.path))
        path, _ = QFileDialog.getOpenFileName(
            self, "클립 목록 불러오기", start, "클립 목록 (*.clips.json);;모든 파일 (*.*)")
        if path:
            self._load_clips_from(Path(path))

    def _load_clips_from(self, path: Path, announce: str = "") -> None:
        try:
            loaded = clips_mod.load_clips(path, self.player.path, self.player.frame_count)
        except ClipsError as exc:
            QMessageBox.warning(self, "불러오기 실패", str(exc))
            return
        self.clips = loaded.clips
        self._rebuild_table()
        message = announce or f"클립 {len(self.clips)}개 불러옴: {path.name}"
        if loaded.warning:
            message += "  " + loaded.warning
        self.status_message.emit(message, 9000)

    # ------------------------------------------------------------- 내보내기

    def _current_plan(self, mode: str):
        clip = self.selected_clip()
        if clip is None or self.player.index is None or not self.player.has_file:
            return None
        try:
            return export_mod.plan_export(
                self.player.path, clip, self.player.index, mode,
                self.encoder if mode == export_mod.MODE_PRECISE else None)
        except export_mod.ExportError:
            return None

    def _update_plan_label(self) -> None:
        clip = self.selected_clip()
        if clip is None:
            self.plan_label.setText("클립을 고르면 어떻게 잘릴지 여기에 미리 나옵니다.")
            return
        if self.player.index is None:
            self.plan_label.setText("프레임 인덱스가 아직 없어 키프레임 위치를 모릅니다.")
            return

        lines = []
        for mode in (export_mod.MODE_LOSSLESS, export_mod.MODE_PRECISE):
            plan = self._current_plan(mode)
            lines.append(plan.summary() if plan else f"{mode}: 계산할 수 없음")

        # 무손실 인코더(ffv1 등)로 나온 파일은 화질이 원본 그대로인 대신
        # 윈도우 기본 플레이어가 못 연다 ("지원되지 않는 형식" 0xC00D5212).
        # 파일이 잘못된 게 아니라 그 코덱을 모르는 것이다 — 누르기 전에 알려 준다.
        encoder = self.encoder
        if encoder is not None and encoder.lossless:
            lines.append(
                f"※ 정확 컷 결과({encoder.name})는 PlayerX·VLC 에서는 열리지만 "
                "윈도우 기본 플레이어로는 안 열립니다. "
                "어디서나 열리는 파일이 필요하면 인코더를 H.264 로 바꾸세요."
            )
        self.plan_label.setText("\n".join(lines))

    def export_selected(self, mode: str) -> None:
        if self._export_worker is not None and self._export_worker.isRunning():
            self.status_message.emit("앞선 내보내기가 아직 돌고 있습니다.", 3000)
            return
        clip = self.selected_clip()
        if clip is None:
            self.status_message.emit("내보낼 클립을 목록에서 골라 주세요.", 3000)
            return
        try:
            exe = require_ffmpeg()
        except FFmpegNotFound as exc:
            QMessageBox.critical(self, "ffmpeg 을 찾지 못했습니다", str(exc))
            return
        if mode == export_mod.MODE_PRECISE and self.encoder is None:
            QMessageBox.warning(self, "인코더 없음",
                                "이 ffmpeg 빌드에 쓸 수 있는 인코더가 없습니다.")
            return

        plan = self._current_plan(mode)
        if plan is None:
            self.status_message.emit("자를 구간을 계산하지 못했습니다.", 5000)
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "클립 내보내기", str(plan.out_path),
            f"영상 (*{plan.out_path.suffix});;모든 파일 (*.*)")
        if not path:
            return
        plan.out_path = Path(path)

        self.export_bar.setValue(0)
        self.export_bar.setFormat("자르는 중… %p%")
        self.export_bar.show()
        self.lossless_button.setEnabled(False)
        self.precise_button.setEnabled(False)
        self.status_message.emit(f"내보내는 중: {plan.out_path.name}", 0)

        worker = ExportWorker(
            exe, plan, export_mod.estimate_bitrate(self.player.path, self.player.index), self)
        worker.progress.connect(
            lambda ratio, frames: self.export_bar.setValue(int(ratio * 1000)))
        worker.done.connect(lambda out, p=plan: self._on_export_done(out, p))
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(self._on_export_finished)
        self._export_worker = worker
        worker.start()

    # -------------------------------------------------------- PNG 시퀀스 (5-3)

    # 이 장수를 넘으면 한 번 물어본다. 4K 라면 한 장에 20MB 가 넘어서
    # 실수로 만 장을 뽑으면 디스크가 순식간에 찬다.
    SEQUENCE_CONFIRM_FRAMES = 300

    def export_sequence(self) -> None:
        """고른 클립의 모든 프레임을 PNG 한 장씩으로 저장한다.

        ffmpeg 이 아니라 2단계 캡쳐 경로(원본 재디코딩)를 쓴다 — 낱장 캡쳐와
        완전히 같은 그림이 나와야 하기 때문이다. 색 변환과 비트 깊이 규칙이
        한 군데(app/capture.py)에만 있어야 둘이 어긋나지 않는다.
        """
        if self._sequence_worker is not None and self._sequence_worker.isRunning():
            self.status_message.emit("앞선 시퀀스 내보내기가 아직 돌고 있습니다.", 3000)
            return
        clip = self.selected_clip()
        if clip is None or self.player.index is None:
            self.status_message.emit("내보낼 클립을 목록에서 골라 주세요.", 3000)
            return

        if clip.length > self.SEQUENCE_CONFIRM_FRAMES:
            answer = QMessageBox.question(
                self, "PNG 시퀀스",
                f"{clip.length:,} 장을 저장합니다.\n"
                f"({self.player.index.width}x{self.player.index.height} · "
                f"{16 if self.player.index.bit_depth > 8 else 8}bit PNG)\n\n계속할까요?")
            if answer != QMessageBox.Yes:
                return

        default = self.shot_dir / f"{Path(self.player.path).stem}_{clip.in_frame:06d}-{clip.out_frame:06d}"
        path = QFileDialog.getExistingDirectory(
            self, "PNG 시퀀스를 저장할 폴더", str(default.parent))
        if not path:
            return
        out_dir = Path(path) / default.name

        self.export_bar.setValue(0)
        self.export_bar.setFormat("PNG 시퀀스… %p%")
        self.export_bar.show()
        self._set_export_buttons(False)
        self.status_message.emit(f"PNG 시퀀스 저장 중: {out_dir.name}", 0)

        worker = SequenceWorker(
            self.player.path, self.player.index, clip.in_frame, clip.out_frame,
            out_dir, self.force_bit_depth, self)
        worker.progress.connect(self._on_sequence_progress)
        worker.done.connect(self._on_sequence_done)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(self._on_export_finished)
        self._sequence_worker = worker
        worker.start()

    def _on_sequence_progress(self, ratio: float, done: int, total: int) -> None:
        self.export_bar.setValue(int(ratio * 1000))
        self.export_bar.setFormat(f"PNG 시퀀스… {done:,} / {total:,}")

    def _on_sequence_done(self, out_dir: Path, written: list) -> None:
        total_bytes = sum(p.stat().st_size for p in written if p.is_file())
        self.status_message.emit(
            f"PNG 시퀀스 완료: {len(written):,} 장 · {total_bytes / 1e6:.1f} MB · {out_dir}",
            12000)

    def _set_export_buttons(self, enabled: bool) -> None:
        for button in (self.lossless_button, self.precise_button, self.sequence_button):
            button.setEnabled(enabled)

    def _on_export_done(self, out: Path, plan) -> None:
        size = out.stat().st_size if out.is_file() else 0
        note = ""
        encoder = getattr(plan, "encoder", None)
        if encoder is not None and encoder.lossless:
            note = " · 무손실 코덱이라 윈도우 기본 플레이어로는 안 열립니다 (PlayerX 로 여세요)"
        self.status_message.emit(
            f"내보내기 완료: {out.name} · {plan.frames} 프레임 · {size / 1e6:.1f} MB "
            f"({plan.start_frame} ~ {plan.end_frame}){note}", 12000)

    def _on_export_failed(self, message: str) -> None:
        QMessageBox.critical(self, "내보내기 실패", message)
        self.status_message.emit("내보내기 실패", 6000)

    def _on_export_finished(self) -> None:
        self.export_bar.hide()
        self._update_buttons()

    # ------------------------------------------------------------------ 표시

    def _on_index_changed(self) -> None:
        p = self.player
        cfr = p.index.cfr if p.index is not None else True
        self.timeline.set_media(p.frame_count if p.can_step else 0, p.fps, cfr)
        self.timeline.set_frame(p.current_frame)
        self._rebuild_table()
        self._update_labels()

    def _update_labels(self) -> None:
        player = self.player
        if not player.has_file:
            self.counter.setText("—")
            self.mark_label.setText("IN —   OUT —")
            self._update_buttons()
            return

        total = f"{player.frame_count:,}" if player.frame_count else "?"
        head = ("VFR" if (player.index is not None and not player.index.cfr)
                else timecode.format_timecode(player.frame, player.fps))
        self.counter.setText(f"{head} · {player.current_frame:,} / {total}")

        in_text = "—" if self.pending_in is None else f"{self.pending_in:,}"
        out_text = "—" if self.pending_out is None else f"{self.pending_out:,}"
        length = ""
        if self.pending_in is not None and self.pending_out is not None:
            length = f"   길이 {self.pending_out - self.pending_in + 1:,}"
        self.mark_label.setText(f"IN {in_text}   OUT {out_text}{length}")

        # 찍는 중인 구간이 없으면 고른 클립의 구간을 타임라인에 보여 준다 —
        # 목록의 숫자만 보고 "어디쯤인지" 가늠하는 것보다 훨씬 빠르다.
        if self.pending_in is None and self.pending_out is None:
            clip = self.selected_clip()
            self.timeline.set_marks(
                *((clip.in_frame, clip.out_frame) if clip is not None else (None, None)))
        else:
            self.timeline.set_marks(self.pending_in, self.pending_out)
        self.play_button.set_icon("play" if player.paused else "pause")
        if not self._slider_busy:
            self.timeline.set_frame(player.current_frame)
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_file = self.player.has_file
        has_index = self.player.index is not None
        has_selection = self.selected_clip() is not None
        running = self._export_worker is not None and self._export_worker.isRunning()

        for button in (self.in_button, self.out_button):
            button.setEnabled(has_file)
        self.add_button.setEnabled(
            self.pending_in is not None and self.pending_out is not None)
        self.save_button.setEnabled(has_file)
        self.load_button.setEnabled(has_file)
        self.delete_button.setEnabled(has_selection)
        self.clear_button.setEnabled(bool(self.clips))
        busy = running or (self._sequence_worker is not None
                           and self._sequence_worker.isRunning())
        self.lossless_button.setEnabled(
            has_selection and has_index and self._ffmpeg is not None and not busy)
        self.precise_button.setEnabled(
            has_selection and has_index and self._ffmpeg is not None
            and self.encoder is not None and not busy)
        # PNG 시퀀스는 ffmpeg 이 필요 없다 — 캡쳐와 같은 PyAV 경로를 쓴다.
        self.sequence_button.setEnabled(has_selection and has_index and not busy)

    # ------------------------------------------------------------------ 종료

    def shutdown(self) -> None:
        self._stop_index_worker()
        for worker in (self._export_worker, self._sequence_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(5000)
        self.player.shutdown()

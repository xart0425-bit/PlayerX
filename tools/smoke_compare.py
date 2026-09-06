"""비교 탭을 실제로 띄우고 확인한다 — 3단계 검증.

GUI 를 진짜로 열고 진짜 키 이벤트를 보낸다. 단계마다 스크린샷을 남긴다.
검사하는 것:

  A. **프레임 잠금** (3-3) — A 를 움직이면 B 가 같은 프레임 번호로 따라오는가.
     단축키(←/→/Home/End)가 비교 탭으로 가는지도 같이 본다.
  B. **오프셋 정렬** (3-7) — testdata 의 A/B 쌍은 B 가 A 를 7 프레임 밀어 놓은
     파일이다. **오프셋을 +7 로 맞추면 차분이 정확히 0** 이어야 하고, 어긋난
     오프셋에서는 0 이 아니어야 한다. 이게 "같은 프레임을 보고 있다"에 대한
     픽셀 수준 증거다.
  C. **세 가지 모드** (3-4·3-5·3-6) — 좌우 분할 / 와이프 / 차분의 배치가
     의도대로인가. 와이프는 두 위젯이 겹쳐 있고 마스크로 잘려 있어야 한다.
  D. **드리프트 보정** (3-8) — fps 가 다른 두 파일을 같이 재생시켜 놓고,
     B 가 A 에서 허용치 이상 멀어지지 않는가.

    python tools/smoke_compare.py [<스크린샷폴더>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import screengrab  # noqa: E402

from app.compare import DRIFT_TOLERANCE_FRAMES, MODE_DIFF, MODE_SIDE, MODE_WIPE  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from make_testdata import PAIR_SHIFT  # noqa: E402

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
INDEX_TIMEOUT_MS = 60_000
DIFF_TIMEOUT_MS = 30_000


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def grab(name: str, out_dir: Path) -> str:
    return screengrab.grab_screen(out_dir / f"{name}.png")


class Harness:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.failures: list[str] = []
        self.records: list[dict] = []
        self.app = QApplication.instance() or QApplication([])
        self.window = MainWindow()
        self.window.showMaximized()
        # 검증 내내 '항상 위'로 둔다 — 안 그러면 스크린샷에 편집기 창이 찍힌다.
        screengrab.pin_on_top(self.window)
        pump(1200)
        self.tab = self.window.compare
        self.window.tabs.setCurrentIndex(1)
        pump(400)
        self.last_diff = None
        self.tab._diff_engine.done.connect(self._on_diff)

    def _on_diff(self, result) -> None:
        self.last_diff = result

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "OK " if ok else "!! "
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}", flush=True)
        if not ok:
            self.failures.append(f"{label} {detail}".strip())
        return ok

    # ------------------------------------------------------------------ 준비

    def open_pair(self, name_a: str, name_b: str) -> bool:
        self.tab.open_file("A", str(TESTDATA / name_a))
        self.tab.open_file("B", str(TESTDATA / name_b))
        waited = 0
        while not self.tab.both_indexed and waited < INDEX_TIMEOUT_MS:
            pump(200)
            waited += 200
        a, b = self.tab.player_a, self.tab.player_b
        print(f"\n열림: A={name_a} {a.frame_count} 프레임 · "
              f"B={name_b} {b.frame_count} 프레임 · 인덱싱 {waited}ms", flush=True)
        return self.check(self.tab.both_indexed, "A · B 양쪽 인덱스 준비")

    def key(self, key, modifier=Qt.NoModifier, times: int = 1) -> None:
        for _ in range(times):
            QTest.keyClick(self.window, key, modifier)
            pump(320)

    def set_mode(self, mode: str) -> None:
        """모드를 이름으로 고른다 (콤보박스 순서가 바뀌어도 안 깨지게)."""
        index = self.tab.mode_box.findData(mode)
        assert index >= 0, f"알 수 없는 모드: {mode}"
        self.tab.mode_box.setCurrentIndex(index)

    def wait_diff(self) -> object | None:
        """차분이 새로 올라올 때까지 기다린다."""
        self.last_diff = None
        self.tab._request_diff()
        waited = 0
        while self.last_diff is None and waited < DIFF_TIMEOUT_MS:
            pump(150)
            waited += 150
        return self.last_diff

    # ---------------------------------------------------------------- 검사 A

    def check_frame_lock(self) -> None:
        print("\n[A] 프레임 잠금 — A 를 움직이면 B 가 따라오는가 (단축키 경유)", flush=True)
        a, b = self.tab.player_a, self.tab.player_b

        # (라벨, 보낼 키, 그 뒤 A 가 있어야 할 프레임)
        last = max(0, a.frame_count - 1)
        steps = [
            ("Home", lambda: self.key(Qt.Key_Home), 0),
            ("→ x5", lambda: self.key(Qt.Key_Right, times=5), 5),
            ("← x2", lambda: self.key(Qt.Key_Left, times=2), 3),
            ("Shift+→", lambda: self.key(Qt.Key_Right, Qt.ShiftModifier), 13),
            ("Ctrl+→", lambda: self.key(Qt.Key_Right, Qt.ControlModifier), 113),
            ("End", lambda: self.key(Qt.Key_End), last),
        ]
        for label, send, want_a in steps:
            send()
            pump(500)
            want_a = min(want_a, last)
            want_b = min(want_a + self.tab.offset, b.frame_count - 1)
            ok = a.current_frame == want_a and b.current_frame == want_b
            self.check(ok, f"{label:<10}",
                       f"A={a.current_frame} (기대 {want_a}) · "
                       f"B={b.current_frame} (기대 {want_b})")
        self.records.append({"step": "frame_lock", "last_a": a.current_frame})

    # ---------------------------------------------------------------- 검사 B

    def check_offset_alignment(self) -> None:
        print(f"\n[B] 오프셋 정렬 — B 는 A 를 {PAIR_SHIFT} 프레임 밀어 놓은 파일", flush=True)
        self.set_mode(MODE_DIFF)
        pump(400)

        for offset, expect_zero in ((PAIR_SHIFT, True), (0, False), (PAIR_SHIFT + 1, False)):
            self.tab.offset_spin.setValue(offset)
            self.tab.goto_frame(40)
            pump(600)
            result = self.wait_diff()
            if result is None:
                self.check(False, f"오프셋 {offset:+d}", "차분이 돌아오지 않음")
                continue
            got_zero = result.identical
            detail = (f"A={self.tab.player_a.current_frame} B={self.tab.player_b.current_frame} "
                      f"· 최대차 {result.max_diff:.0f} · 차이 0 = {got_zero}")
            self.check(got_zero == expect_zero,
                       f"오프셋 {offset:+d} -> 차분 {'0' if expect_zero else '0 아님'}", detail)
            self.records.append({"step": f"offset{offset:+d}", "identical": got_zero,
                                 "max_diff": result.max_diff})

        self.tab.offset_spin.setValue(PAIR_SHIFT)
        pump(300)
        self.wait_diff()
        grab("cmp_01_diff_aligned", self.out_dir)

    # ---------------------------------------------------------------- 검사 C

    def check_modes(self) -> None:
        print("\n[C] 세 가지 모드의 배치", flush=True)
        view = self.tab.view
        a, b = view.player_a, view.player_b

        self.set_mode(MODE_SIDE)
        pump(900)
        w = view.width()
        ok = (a.isVisible() and b.isVisible()
              and a.mask().isEmpty() and b.mask().isEmpty()
              and a.geometry().width() <= w // 2 + 1
              and b.geometry().left() >= w // 2 - 1)
        self.check(ok, "좌우 분할",
                   f"A={a.geometry().getRect()} B={b.geometry().getRect()} 마스크 없음")
        grab("cmp_02_side", self.out_dir)

        self.set_mode(MODE_WIPE)
        self.tab.wipe_slider.setValue(350)
        pump(900)
        seam = view._seam_rect
        ok = (a.geometry() == b.geometry() == view.rect()
              and not a.mask().isEmpty() and not b.mask().isEmpty()
              and not seam.isEmpty()
              and abs(seam.center().x() - int(w * 0.35)) <= view.SEAM_WIDTH)
        self.check(ok, "와이프 35%",
                   f"둘 다 전체크기={a.geometry() == view.rect()} · "
                   f"마스크 A={a.mask().boundingRect().getRect()} "
                   f"B={b.mask().boundingRect().getRect()} · 경계={seam.getRect()}")
        grab("cmp_03_wipe35", self.out_dir)

        # 와이프 -> 좌우 분할로 돌아올 때 마스크가 풀리는가 (안 풀면 잘린 채 남는다)
        self.set_mode(MODE_SIDE)
        pump(700)
        self.check(a.mask().isEmpty() and b.mask().isEmpty(),
                   "와이프에서 나오면 마스크 해제")

        self.set_mode(MODE_DIFF)
        pump(900)
        ok = (view.diff_view.isVisible() and not a.isVisible() and not b.isVisible())
        self.check(ok, "차분 모드에서 영상 위젯 감춤")
        grab("cmp_04_diff", self.out_dir)

    # ---------------------------------------------------------------- 검사 D

    def check_drift(self) -> None:
        print("\n[D] 드리프트 보정 — fps 가 다른 두 파일을 같이 재생", flush=True)
        if not self.open_pair("test_60fps.mkv", "test_120fps.mkv"):
            return
        self.set_mode(MODE_SIDE)
        self.tab.offset_spin.setValue(0)
        self.tab.goto_frame(0)
        pump(500)

        before = self.tab._drift_corrections
        self.tab.set_playing(True)
        worst = 0
        for _ in range(14):
            pump(400)
            a, b = self.tab.player_a, self.tab.player_b
            drift = abs(b.current_frame - self.tab._b_frame_for(a.current_frame))
            worst = max(worst, drift)
        self.tab.set_playing(False)
        pump(600)

        corrections = self.tab._drift_corrections - before
        a, b = self.tab.player_a, self.tab.player_b
        # 120fps 를 60fps 에 프레임 번호로 묶어 놨으니 B 는 두 배 속도로 달아나려 한다.
        # 보정이 없으면 5.6초에 300 프레임 넘게 벌어진다 (60fps x 5.6s).
        # 보정이 실제로 도는지, 그리고 어긋남이 폭주하지 않는지를 본다.
        runaway = int(5.6 * 60)
        print(f"    재생 5.6초 · 최대 어긋남 {worst} 프레임 · 보정 {corrections}회 "
              f"(보정이 없으면 ~{runaway} 프레임)", flush=True)
        self.check(corrections > 0, "재생 중 보정이 실제로 걸림", f"{corrections}회")
        self.check(worst < runaway // 3, "어긋남이 폭주하지 않음",
                   f"최대 {worst} < {runaway // 3} 프레임")

        # 멈추면 즉시 정확히 맞아야 한다 (검수는 멈춘 그림에서 한다)
        self.check(b.current_frame == self.tab._b_frame_for(a.current_frame),
                   "정지하면 즉시 정확히 일치",
                   f"A={a.current_frame} B={b.current_frame}")
        self.records.append({"step": "drift_mismatched_fps",
                             "worst": worst, "corrections": corrections})
        grab("cmp_05_drift", self.out_dir)

        # 실제 용도(같은 fps 두 인코딩)에서는 허용치 안에 머물러야 한다.
        print("\n[D2] 같은 fps 두 파일 — 허용치 안에 머무는가", flush=True)
        if not self.open_pair("test_pair_a.mkv", "test_pair_b.mkv"):
            return
        self.tab.offset_spin.setValue(0)
        self.tab.goto_frame(0)
        pump(400)
        self.tab.set_playing(True)
        worst_same = 0
        for _ in range(8):
            pump(400)
            a, b = self.tab.player_a, self.tab.player_b
            worst_same = max(worst_same, abs(b.current_frame
                                             - self.tab._b_frame_for(a.current_frame)))
        self.tab.set_playing(False)
        pump(500)
        print(f"    재생 3.2초 · 최대 어긋남 {worst_same} 프레임 "
              f"(허용치 {DRIFT_TOLERANCE_FRAMES})", flush=True)
        self.check(worst_same <= DRIFT_TOLERANCE_FRAMES,
                   "같은 fps 에서는 허용치를 안 넘음",
                   f"최대 {worst_same} <= {DRIFT_TOLERANCE_FRAMES}")
        self.records.append({"step": "drift_same_fps", "worst": worst_same})

    def check_layout_steady(self) -> None:
        """재생 중에 영상과 타임라인이 위아래로 움찔거리지 않는가.

        카운터 글자에 `(어긋남 +1)` 이 붙었다 떨어졌다 하는데, Consolas 에는
        한글이 없어서 그 글자만 대체 글꼴로 그려진다 — 그쪽이 2px 더 높아서
        **라벨 높이가 통째로 2px 커진다.** 그 2px 이 같은 세로 배치에 있는
        영상 높이를 밀어내서 화면 전체가 떨렸다.
        높이를 못박아 고쳤고(`app/widgets.lock_text_height`), 여기서 지킨다.
        """
        print("\n[E] 재생 중 화면이 위아래로 움찔거리지 않는가", flush=True)
        if not self.open_pair("test_60fps.mkv", "test_120fps.mkv"):
            return
        self.set_mode(MODE_SIDE)
        self.tab.goto_frame(0)
        pump(400)

        self.tab.set_playing(True)
        tops: set[int] = set()
        heights: set[int] = set()
        counters: set[str] = set()
        saw_note = False
        for _ in range(100):                     # 약 4초
            pump(40)
            tops.add(self.tab.timeline.pos().y())
            heights.add(self.tab.view.height())
            counters.add(self.tab.counter.text())
            if "어긋남" in self.tab.drift_label.text():
                saw_note = True
        self.tab.set_playing(False)
        pump(300)

        shift = max(tops) - min(tops)
        print(f"    타임라인 y {sorted(tops)} · 영상 높이 {sorted(heights)}", flush=True)

        # 위젯 좌표가 그대로여도 **라벨 안에서 글자만** 위아래로 밀릴 수 있다.
        # 고정 높이 안에서 글자를 가운데 맞추는데, 한글이 섞이면 그 줄의 글꼴
        # 지표가 바뀌어 가운데가 달라지기 때문이다. 그래서 카운터에는 한글을
        # 넣지 않기로 했고, 여기서 두 가지로 지킨다.
        hangul = [t for t in counters if any("가" <= ch <= "힣" for ch in t)]
        self.check(not hangul, "카운터에 한글이 섞이지 않음",
                   f"섞인 예: {hangul[0]}" if hangul else f"{len(counters)}종 확인")
        self.check(saw_note, "어긋남·오프셋 표시는 옆 라벨에 그대로 나옴",
                   "drift_label 에 표시됨" if saw_note else "표시가 아예 없었음")

        # **재생 중에 실제로 나왔던 글자들**로만 비교한다. 손으로 지어낸 예를
        # 섞으면 글자 모양 차이(`/` 는 숫자보다 1px 위까지 올라간다)까지 잡혀서,
        # 흔들림이 없는데도 실패한다.
        ink = {self._ink_top(self.tab.counter, text)
               for text in sorted(counters)[:8]}
        self._restore_counter()
        self.check(len(ink) == 1, "글자가 라벨 안에서 위아래로 밀리지 않음",
                   f"글자 시작 줄 {sorted(ink)} ({len(counters)}종 중 8종 확인)")
        # 글자가 실제로 바뀌지 않았다면 이 검사는 아무것도 지키지 못한 것이다.
        self.check(len(counters) > 1 and saw_note,
                   "검사 조건 성립 — 글자가 실제로 바뀌고 어긋남도 생김",
                   f"카운터 {len(counters)}종 · 어긋남 표시 {saw_note}")
        self.check(shift == 0, "재생 중 타임라인이 제자리에 있음", f"{shift}px 움직임")
        self.check(len(heights) == 1, "재생 중 영상 높이가 그대로", f"{sorted(heights)}")
        self.records.append({"step": "layout_steady", "shift_px": shift})
        grab("cmp_06_layout", self.out_dir)

    def check_no_ghost(self) -> None:
        """앞 탭 그림이 비교 탭 영상 자리에 비쳐 보이지 않는가.

        mpv 위젯은 진짜 윈도우 창이고 `WA_OpaquePaintEvent` 가 켜져 있다 —
        "바탕은 내가 칠한다"는 약속이라 Qt 는 안 칠한다. **파일이 없는 동안**
        mpv 도 아무것도 안 그리면 그 창에는 그 자리에 예전에 있던 그림이 그대로
        남는다. 실제로 재생 탭을 보다가 비교 탭으로 넘어가면 재생 탭 화면이
        비쳐 보였다.

        **위젯을 그려 보는 방법으로는 이걸 못 잡는다** (`QWidget.grab()` 은
        네이티브 자식 창을 빼고 그리므로 언제나 깨끗하다). 그래서 윈도우에
        "네 내용을 직접 그려라"라고 시켜서(PrintWindow) 실제 화면을 받아 온다.
        """
        print("\n[F] 앞 탭 그림이 비쳐 보이지 않는가", flush=True)

        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget  # noqa: PLC0415

        from app.player import MpvWidget  # noqa: PLC0415

        # 밝은 그림을 먼저 그려 놓는다 (= '앞 탭 화면' 역할).
        holder = QWidget()
        holder.setWindowTitle("ghost-check")
        holder.setStyleSheet("background: #ffffff;")
        holder.setAutoFillBackground(True)
        layout = QVBoxLayout(holder)
        label = QLabel("앞 탭 그림" * 40)
        label.setWordWrap(True)
        label.setStyleSheet("background: #ffffff; color: #000000; font-size: 28px;")
        layout.addWidget(label)
        holder.resize(700, 460)
        holder.show()
        pump(700)

        # 그 위에 파일 없는 mpv 위젯을 덮는다. mpv 는 아무것도 안 그리므로,
        # 위젯이 자기 바탕을 안 칠하면 아래 밝은 그림이 그대로 비친다.
        player = MpvWidget(holder)
        player.setGeometry(0, 0, holder.width(), holder.height())
        player.show()
        player.raise_()
        pump(900)

        image = self._print_window(holder)
        if image is None:
            self.check(False, "창 내용을 받아오지 못함", "PrintWindow 실패")
        else:
            # PrintWindow 는 제목 표시줄까지 같이 준다. mpv 위젯이 확실히
            # 덮고 있는 가운데만 본다.
            width, height = image.width(), image.height()
            image = image.copy(int(width * 0.2), int(height * 0.35),
                               int(width * 0.6), int(height * 0.5))
            bright = counted = 0
            for y in range(0, image.height(), 4):
                for x in range(0, image.width(), 4):
                    counted += 1
                    pixel = image.pixel(x, y)
                    if ((pixel >> 16 & 0xFF) + (pixel >> 8 & 0xFF) + (pixel & 0xFF)) > 150:
                        bright += 1
            share = bright / max(1, counted) * 100
            image.save(str(self.out_dir / "cmp_07_ghost.png"))
            print(f"    덮은 뒤 가운데 {image.width()}x{image.height()} · "
                  f"밝은 픽셀 {share:.2f}%", flush=True)
            self.check(share < 1.0, "파일 없는 mpv 위젯이 자기 바탕을 검게 칠함",
                       f"밝은 픽셀 {share:.2f}% < 1.0%")

        player.shutdown()
        holder.close()
        pump(300)

    @staticmethod
    def _print_window(widget):
        """창더러 자기 내용을 직접 그리게 해서 받아 온다 (네이티브 자식 창 포함)."""
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        from PySide6.QtGui import QImage  # noqa: PLC0415

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = int(widget.winId())
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        gdi32.SelectObject(mem_dc, bitmap)
        ok = user32.PrintWindow(hwnd, mem_dc, 0x00000002)   # PW_RENDERFULLCONTENT

        image = None
        if ok:
            class Header(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            header = Header()
            header.biSize = ctypes.sizeof(Header)
            header.biWidth, header.biHeight = width, -height
            header.biPlanes, header.biBitCount, header.biCompression = 1, 32, 0
            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            image = QImage(bytes(buffer), width, height, QImage.Format_RGB32).copy()

        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)
        return image

    def _ink_top(self, label, text: str) -> int:
        """라벨에 그 글자를 넣고 **실제로 그려서** 글자가 시작하는 줄을 돌려준다.

        위젯 좌표가 아니라 그려진 픽셀을 본다 — 고정 높이 안에서 글자만
        위아래로 밀리는 종류의 흔들림은 좌표로는 절대 안 잡힌다.
        """
        from PySide6.QtGui import QColor, QPixmap  # noqa: PLC0415

        if getattr(self, "_counter_keep", None) is None:
            self._counter_keep = label.text()
        label.setText(text)
        label.adjustSize()

        pixmap = QPixmap(label.size())
        pixmap.fill(QColor("#000000"))
        label.render(pixmap)
        image = pixmap.toImage()
        background = image.pixel(0, 0)
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixel(x, y) != background:
                    return y
        return -1

    def _restore_counter(self) -> None:
        keep = getattr(self, "_counter_keep", None)
        if keep is not None:
            self.tab.counter.setText(keep)
            self._counter_keep = None

    # ---------------------------------------------------------------- 마무리

    def finish(self) -> int:
        (self.out_dir / "result_compare.json").write_text(
            json.dumps({"failures": self.failures, "records": self.records},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        screengrab.release(self.window)
        self.window.compare.shutdown()
        self.window.playback.shutdown()
        self.window.close()
        print("\n" + "=" * 60)
        if self.failures:
            print(f"실패 {len(self.failures)}건:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("전부 통과 — 두 영상이 같은 프레임 번호로 묶이고, 오프셋 정렬이 픽셀로 확인됩니다.")
        print(f"스크린샷: {self.out_dir}")
        return 0


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else (
        Path(__file__).resolve().parent.parent / "shots" / "compare")
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in ("test_pair_a.mkv", "test_pair_b.mkv"):
        if not (TESTDATA / name).is_file():
            print(f"{name} 이 없습니다. 먼저: python tools/make_testdata.py")
            return 1

    h = Harness(out_dir)
    if h.open_pair("test_pair_a.mkv", "test_pair_b.mkv"):
        h.check_frame_lock()
        h.check_offset_alignment()
        h.check_modes()
    h.check_drift()
    h.check_layout_steady()
    h.check_no_ghost()
    return h.finish()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


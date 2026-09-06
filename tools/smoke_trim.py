"""트림/클립 탭을 실제로 띄우고 확인한다 — 4단계 검증.

GUI 를 진짜로 열고 진짜 키 이벤트를 보낸다. 검사하는 것:

  A. **IN/OUT 마킹** (4-2) — `I`·`O` 단축키로 구간이 잡히는가. `O` 를 누르면
     그 자리에서 클립이 되는가.
  B. **클립 목록** (4-3) — 추가·정렬·삭제·전체 삭제, 그리고 길이 계산(양끝 포함).
  C. **클립 간 이동** (4-4) — `[`·`]` 가 선택과 재생 위치를 같이 옮기는가.
  D. **JSON 왕복** (4-5) — 저장했다 불러온 목록이 원본과 같은가.
  E. **자른 결과가 예고와 맞는가** (4-7·4-8·4-9) — 이 검증의 핵심이다.
     UI 가 "무손실은 60 번부터 시작합니다"라고 예고했으면, 잘라 낸 파일을
     **우리 인덱서로 다시 읽어서** 정말 60 번부터인지, 프레임 수가 맞는지,
     그리고 **픽셀이 원본과 같은지**까지 대조한다.
  F. **인코더 탐지** (4-6) — ffmpeg 빌드에 실제로 있는 인코더만 UI 에 뜨는가.

E 가 없으면 나머지는 "UI 가 그렇게 주장한다"에 불과하다.

    python tools/smoke_trim.py [<스크린샷폴더>]
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
from png_probe import read_png_header  # noqa: E402

from app import clips as clips_mod  # noqa: E402
from app import export as export_mod  # noqa: E402
from app import frame_index as fi  # noqa: E402
from app.capture import FrameCapturer  # noqa: E402
from app.clips import Clip  # noqa: E402
from app.ffmpeg_loader import find_ffmpeg, version_line  # noqa: E402
from app.main_window import MainWindow  # noqa: E402

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
SOURCE = TESTDATA / "test_60fps.mkv"
INDEX_TIMEOUT_MS = 60_000
EXPORT_TIMEOUT_MS = 120_000


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


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
        self.tab = self.window.trim
        self.window.tabs.setCurrentIndex(2)
        pump(400)

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "OK " if ok else "!! "
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}", flush=True)
        if not ok:
            self.failures.append(f"{label} {detail}".strip())
        return ok

    def grab(self, name: str) -> None:
        screengrab.pin_on_top(self.window)
        pump(400)
        screengrab.grab_screen(self.out_dir / f"{name}.png")

    def key(self, key, times: int = 1) -> None:
        for _ in range(times):
            QTest.keyClick(self.window, key)
            pump(320)

    # ------------------------------------------------------------------ 준비

    def open_source(self) -> bool:
        self.tab.open_file(str(SOURCE))
        waited = 0
        while not self.tab.player.index_ready and waited < INDEX_TIMEOUT_MS:
            pump(200)
            waited += 200
        index = self.tab.player.index
        print(f"\n열림: {SOURCE.name} · {self.tab.player.frame_count} 프레임 · "
              f"키프레임 {len(index.keyframes) if index else 0}개 · 인덱싱 {waited}ms",
              flush=True)
        return self.check(self.tab.player.index_ready, "프레임 인덱스 준비")

    # ---------------------------------------------------------------- 검사 F

    def check_ffmpeg(self) -> None:
        print("\n[F] ffmpeg 과 인코더 탐지", flush=True)
        exe = find_ffmpeg()
        if not self.check(exe is not None, "vendor/ffmpeg.exe 를 찾음",
                          "없으면: python tools/fetch_ffmpeg.py"):
            return
        print(f"    {version_line(exe)[:70]}", flush=True)

        listed = [self.tab.encoder_box.itemData(i)
                  for i in range(self.tab.encoder_box.count())]
        listed = [c for c in listed if c is not None]
        available = export_mod.available_encoders(exe)
        self.check(bool(listed), "쓸 수 있는 인코더가 목록에 있음",
                   ", ".join(c.name for c in listed))
        self.check(all(c.name in available for c in listed),
                   "목록에 있는 인코더는 전부 이 빌드에 실제로 존재")
        self.check(listed and listed[0].lossless,
                   "기본 인코더가 무손실", listed[0].label if listed else "")
        self.records.append({"step": "encoders", "listed": [c.name for c in listed]})

    # ---------------------------------------------------------------- 검사 A/B

    def check_marking(self) -> None:
        print("\n[A] IN/OUT 마킹 (I · O 단축키)", flush=True)
        tab = self.tab

        tab.goto_frame(100)
        pump(600)
        self.key(Qt.Key_I)
        self.check(tab.pending_in == 100, "I 로 IN 찍힘", f"IN={tab.pending_in}")

        tab.goto_frame(150)
        pump(600)
        self.key(Qt.Key_O)
        self.check(len(tab.clips) == 1 and tab.clips[0].in_frame == 100
                   and tab.clips[0].out_frame == 150,
                   "O 를 누르면 그 자리에서 클립이 됨",
                   f"{[(c.in_frame, c.out_frame) for c in tab.clips]}")
        self.check(tab.pending_in is None and tab.pending_out is None,
                   "클립이 되면 대기 중인 IN/OUT 이 비워짐")
        self.check(tab.clips[0].length == 51, "길이는 양끝 포함",
                   f"100~150 -> {tab.clips[0].length} 프레임")

        print("\n[B] 클립 목록 관리", flush=True)
        # 일부러 뒤 구간을 먼저 넣어서 정렬되는지 본다
        for a, b in ((300, 320), (10, 40)):
            tab.goto_frame(a)
            pump(500)
            self.key(Qt.Key_I)
            tab.goto_frame(b)
            pump(500)
            self.key(Qt.Key_O)
        starts = [c.in_frame for c in tab.clips]
        self.check(starts == sorted(starts), "IN 순서로 정렬됨", str(starts))
        self.check(self.tab.table.rowCount() == len(tab.clips),
                   "표의 줄 수가 클립 수와 같음", f"{self.tab.table.rowCount()}줄")

        # 무손실 시작(키프레임) 칸이 인덱스와 일치하는가 — 4-9
        index = tab.player.index
        mismatches = []
        for row, clip in enumerate(tab.clips):
            shown = tab.table.item(row, 4).text()
            want = str(index.nearest_keyframe(clip.in_frame))
            if not shown.startswith(want):
                mismatches.append((row, shown, want))
        self.check(not mismatches, "표의 '무손실 시작' 칸이 인덱스의 키프레임과 일치",
                   str(mismatches[:3]))
        self.grab("trim_01_clips")

        before = len(tab.clips)
        tab._select_clip(0)
        pump(300)
        tab.delete_selected()
        pump(300)
        self.check(len(tab.clips) == before - 1, "선택 삭제",
                   f"{before} -> {len(tab.clips)}")
        self.records.append({"step": "clips",
                             "list": [(c.in_frame, c.out_frame) for c in tab.clips]})

    # ---------------------------------------------------------------- 검사 C

    def check_navigation(self) -> None:
        print("\n[C] 클립 간 이동 ( [ · ] )", flush=True)
        tab = self.tab
        tab._select_clip(len(tab.clips) - 1)
        pump(400)

        self.key(Qt.Key_BracketLeft)
        row = tab.selected_row()
        clip = tab.clips[row]
        self.check(row == len(tab.clips) - 2 and tab.player.current_frame == clip.in_frame,
                   "[ 로 이전 클립 · 재생 위치도 IN 으로",
                   f"행={row} 프레임={tab.player.current_frame} (IN={clip.in_frame})")

        self.key(Qt.Key_BracketRight)
        row = tab.selected_row()
        clip = tab.clips[row]
        self.check(row == len(tab.clips) - 1 and tab.player.current_frame == clip.in_frame,
                   "] 로 다음 클립",
                   f"행={row} 프레임={tab.player.current_frame} (IN={clip.in_frame})")

        # 끝에서 더 눌러도 넘어가지 않아야 한다
        self.key(Qt.Key_BracketRight, times=3)
        self.check(tab.selected_row() == len(tab.clips) - 1, "마지막 클립에서 멈춤")

    # ---------------------------------------------------------------- 검사 D

    def check_json_roundtrip(self) -> None:
        print("\n[D] 클립 목록 JSON 왕복", flush=True)
        tab = self.tab
        before = [(c.in_frame, c.out_frame, c.name) for c in tab.clips]
        path = self.out_dir / "clips_roundtrip.clips.json"

        clips_mod.save_clips(path, tab.clips, tab.player.path, tab.player.index)
        self.check(path.is_file(), "저장됨", f"{path.stat().st_size} 바이트")

        obj = json.loads(path.read_text(encoding="utf-8"))
        self.check(obj.get("source", {}).get("name") == SOURCE.name
                   and obj.get("source", {}).get("frame_count") == tab.player.frame_count,
                   "어느 영상의 목록인지 파일에 적혀 있음",
                   str(obj.get("source")))

        tab.clips = []
        tab._rebuild_table()
        tab._load_clips_from(path)
        pump(300)
        after = [(c.in_frame, c.out_frame, c.name) for c in tab.clips]
        self.check(after == before, "불러온 목록이 저장 전과 같음", f"{after}")

        # 다른 영상의 목록을 불러오면 경고가 나와야 한다 (막지는 않는다)
        loaded = clips_mod.load_clips(path, TESTDATA / "test_120fps.mkv", 1200)
        self.check(bool(loaded.warning), "다른 영상의 목록이면 경고", loaded.warning[:60])
        # 파일 끝을 넘는 구간은 잘려야 한다
        short = clips_mod.load_clips(path, SOURCE, 200)
        self.check(all(c.out_frame <= 199 for c in short.clips) and bool(short.warning),
                   "파일 끝을 넘는 구간은 잘림", short.warning[:60])
        self.records.append({"step": "json", "clips": after})

    # ---------------------------------------------------------------- 검사 E

    def check_export(self) -> None:
        print("\n[E] 자른 결과가 예고와 맞는가", flush=True)
        tab = self.tab
        if find_ffmpeg() is None:
            self.check(False, "ffmpeg 이 없어 내보내기를 검사할 수 없음")
            return

        # IN 이 키프레임이 아닌 구간을 일부러 고른다 — 무손실 컷이 밀리는 경우다.
        index = tab.player.index
        target = Clip(100, 150)
        self.check(index.nearest_keyframe(target.in_frame) != target.in_frame,
                   "검사용 구간의 IN 이 키프레임이 아님",
                   f"IN={target.in_frame} 키프레임={index.nearest_keyframe(target.in_frame)}")

        tab.clips = [target]
        tab._rebuild_table()
        tab._select_clip(0)
        pump(400)
        print(f"    UI 예고:\n      {tab.plan_label.text().replace(chr(10), chr(10) + '      ')}",
              flush=True)
        self.grab("trim_02_plan")

        source_index = index
        source_cap = FrameCapturer(SOURCE, source_index)
        try:
            for mode in (export_mod.MODE_LOSSLESS, export_mod.MODE_PRECISE):
                self._export_and_verify(mode, target, source_cap)
        finally:
            source_cap.close()

    def _export_and_verify(self, mode: str, clip: Clip, source_cap) -> None:
        tab = self.tab
        plan = tab._current_plan(mode)
        if plan is None:
            self.check(False, f"{mode}: 계획을 세우지 못함")
            return
        plan.out_path = self.out_dir / f"cut_{mode}{plan.out_path.suffix}"
        if plan.out_path.exists():
            plan.out_path.unlink()

        exe = find_ffmpeg()
        try:
            export_mod.run_export(
                exe, plan, export_mod.estimate_bitrate(SOURCE, tab.player.index))
        except export_mod.ExportError as exc:
            self.check(False, f"{mode}: 내보내기 실패", str(exc)[:120])
            return

        label = "무손실" if mode == export_mod.MODE_LOSSLESS else "정확"
        print(f"    [{label}] 예고: {plan.start_frame} ~ {plan.end_frame} "
              f"({plan.frames} 프레임) -> {plan.out_path.name} "
              f"{plan.out_path.stat().st_size:,} 바이트", flush=True)

        # 잘라 낸 파일을 **우리 인덱서로 다시 읽어서** 예고와 대조한다.
        out_index = fi.build_index(plan.out_path)
        self.check(out_index.count == plan.frames,
                   f"{label}: 프레임 수가 예고와 일치",
                   f"결과 {out_index.count} · 예고 {plan.frames}")

        # 그리고 픽셀까지 — 결과의 k 번 프레임이 원본의 (start_frame + k) 인가.
        out_cap = FrameCapturer(plan.out_path, out_index)
        try:
            probes = sorted({0, 1, out_index.count // 2, out_index.count - 1})
            bad = [k for k in probes
                   if _planes(out_cap, k) != _planes(source_cap, plan.start_frame + k)]
            self.check(not bad,
                       f"{label}: 결과 픽셀이 원본의 {plan.start_frame}+k 프레임과 일치",
                       f"확인 {probes} · 불일치 {bad}")
        finally:
            out_cap.close()

        if mode == export_mod.MODE_LOSSLESS:
            self.check(plan.start_frame == tab.player.index.nearest_keyframe(clip.in_frame),
                       "무손실: 시작이 가장 가까운 키프레임",
                       f"{plan.start_frame} (요청 {clip.in_frame})")
        else:
            self.check(plan.start_frame == clip.in_frame,
                       "정확: 요청한 IN 에서 정확히 시작", f"{plan.start_frame}")

        self.records.append({"step": f"export_{mode}", "start": plan.start_frame,
                             "frames": plan.frames, "out_frames": out_index.count,
                             "bytes": plan.out_path.stat().st_size})

    # ---------------------------------------------------------------- 검사 G

    def check_sequence(self) -> None:
        """5-3. 구간을 통째로 PNG 시퀀스로 뽑는다.

        낱장 캡쳐(S)와 **같은 그림**이 나와야 한다 — 색 변환과 비트 깊이 규칙이
        app/capture.py 한 군데에만 있는지 확인하는 셈이다.
        """
        print("\n[G] PNG 시퀀스 내보내기", flush=True)
        tab = self.tab
        clip = Clip(200, 209)          # 10 장이면 검증에 충분하다
        out_dir = self.out_dir / "sequence"
        if out_dir.exists():
            for old in out_dir.glob("*.png"):
                old.unlink()

        from app import capture as capture_mod  # noqa: PLC0415

        written = capture_mod.capture_sequence(
            SOURCE, tab.player.index, clip.in_frame, clip.out_frame, out_dir)

        self.check(len(written) == clip.length, "장수가 구간 길이와 같음",
                   f"{len(written)} 장 (구간 {clip.length})")
        names = [p.name for p in written]
        self.check(names[0].endswith("_000200.png") and names[-1].endswith("_000209.png"),
                   "파일명이 원본 프레임 번호를 따름", f"{names[0]} … {names[-1]}")

        heads = {read_png_header(p)["width"] for p in written}
        depths = {read_png_header(p)["bit_depth"] for p in written}
        self.check(heads == {tab.player.index.width}, "전부 원본 해상도", str(heads))
        self.check(depths == {8 if tab.player.index.bit_depth <= 8 else 16},
                   "비트 깊이가 소스 규칙을 따름", str(depths))

        # 낱장 캡쳐와 바이트 단위로 같은가 — 두 경로가 어긋나면 여기서 잡힌다.
        single = self.out_dir / "sequence_single.png"
        capture_mod.capture_frame(SOURCE, tab.player.index, 205, single)
        same = single.read_bytes() == (out_dir / written[5].name).read_bytes()
        self.check(same, "시퀀스의 한 장이 낱장 캡쳐와 완전히 같음",
                   f"{written[5].name} vs {single.name}")
        self.records.append({"step": "sequence", "count": len(written)})

    # ---------------------------------------------------------------- 마무리

    def finish(self) -> int:
        (self.out_dir / "result_trim.json").write_text(
            json.dumps({"failures": self.failures, "records": self.records},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        screengrab.release(self.window)
        self.window.close()
        print("\n" + "=" * 62)
        if self.failures:
            print(f"실패 {len(self.failures)}건:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("전부 통과 — 잘라 낸 클립이 UI 가 예고한 프레임과 픽셀 단위로 일치합니다.")
        print(f"결과물: {self.out_dir}")
        return 0


def _planes(cap, frame_no: int) -> tuple[bytes, ...]:
    return tuple(bytes(p) for p in cap.decode_frame(frame_no)[0].planes)


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else (
        Path(__file__).resolve().parent.parent / "shots" / "trim")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not SOURCE.is_file():
        print(f"{SOURCE} 가 없습니다.")
        return 1

    h = Harness(out_dir)
    h.check_ffmpeg()
    if h.open_source():
        h.check_marking()
        h.check_navigation()
        h.check_json_roundtrip()
        h.check_export()
        h.check_sequence()
    return h.finish()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

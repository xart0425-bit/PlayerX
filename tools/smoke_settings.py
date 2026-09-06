"""설정과 정보 표시를 확인한다 — 5단계 검증 (5-1 · 5-2 · 5-4).

**진짜 설정 파일은 건드리지 않는다.** `settings_path()` 를 임시 경로로 갈아 끼우고
검사한다 — 검증을 돌렸더니 내 설정이 초기화되는 일은 없어야 한다.

검사하는 것:

  A. **설정 왕복** (5-4) — 화면에서 바꾼 값이 저장되고, 다시 켰을 때 그대로 돌아오는가.
  B. **깨진 설정** (5-4) — 이상한 값이 든 파일을 읽어도 앱이 기본값으로 살아나는가.
  C. **디코더 전환** (5-1) — 메뉴로 고른 값이 mpv 에 실제로 먹는가. 그리고 이 PC 에서
     안 되는 걸 고르면 **고른 값이 아니라 실제로 쓰이는 값**을 보여 주는가.
  D. **키프레임 간격** (5-2) — 정보 패널의 숫자가 인덱스의 키프레임 목록과 맞는가.

    python tools/smoke_settings.py [<스크린샷폴더>]
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import screengrab  # noqa: E402
from app import settings as settings_mod  # noqa: E402

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"
SOURCE = TESTDATA / "test_60fps.mkv"
INDEX_TIMEOUT_MS = 60_000


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class Harness:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.failures: list[str] = []
        self.records: list[dict] = []
        self.config = out_dir / "settings_test.json"
        if self.config.exists():
            self.config.unlink()
        # 진짜 설정 파일 대신 임시 파일을 쓰게 만든다.
        settings_mod.settings_path = lambda: self.config
        self.app = QApplication.instance() or QApplication([])

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "OK " if ok else "!! "
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}", flush=True)
        if not ok:
            self.failures.append(f"{label} {detail}".strip())
        return ok

    def open_window(self, with_file: bool = True):
        from app.main_window import MainWindow  # noqa: PLC0415

        window = MainWindow()
        window.showMaximized()
        screengrab.pin_on_top(window)
        pump(1000)
        if with_file:
            window.open_path(str(SOURCE))
            waited = 0
            while not window.playback.player.index_ready and waited < INDEX_TIMEOUT_MS:
                pump(200)
                waited += 200
        return window

    # ---------------------------------------------------------------- 검사 A

    def check_roundtrip(self) -> None:
        print("\n[A] 설정 왕복 — 바꾼 값이 다시 켰을 때 돌아오는가", flush=True)
        window = self.open_window()

        window.set_hwdec("no")
        window.set_capture_bit_depth("16")
        index = window.compare.mode_box.findData("wipe")
        window.compare.mode_box.setCurrentIndex(index)
        window.compare.wipe_slider.setValue(250)
        window.compare.gain_spin.setValue(12)
        pump(400)
        geometry_before = bytes(window.saveGeometry().toBase64()).decode("ascii")
        window.close()
        pump(600)

        self.check(self.config.is_file(), "설정 파일이 생김", str(self.config))
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.check(saved.get("hwdec") == "no"
                   and saved.get("capture_bit_depth") == "16"
                   and saved.get("compare_mode") == "wipe"
                   and abs(saved.get("wipe", 0) - 0.25) < 1e-6
                   and saved.get("diff_gain") == 12,
                   "바꾼 값이 그대로 저장됨",
                   f"hwdec={saved.get('hwdec')} depth={saved.get('capture_bit_depth')} "
                   f"mode={saved.get('compare_mode')} wipe={saved.get('wipe')} "
                   f"gain={saved.get('diff_gain')}")

        window2 = self.open_window(with_file=False)
        checked = [a.data() for a in window2._hwdec_group.actions() if a.isChecked()]
        depth = [a.data() for a in window2._depth_group.actions() if a.isChecked()]
        self.check(checked == ["no"], "다시 켰을 때 디코더 메뉴가 체크돼 있음", str(checked))
        self.check(depth == ["16"], "비트 깊이 메뉴도 복원", str(depth))
        self.check(window2.playback.force_bit_depth == 16,
                   "캡쳐가 쓸 값까지 반영됨", str(window2.playback.force_bit_depth))
        self.check(window2.compare.mode_box.currentData() == "wipe"
                   and window2.compare.wipe_slider.value() == 250
                   and window2.compare.gain_spin.value() == 12,
                   "비교 탭 상태 복원",
                   f"{window2.compare.mode_box.currentData()} "
                   f"{window2.compare.wipe_slider.value()} "
                   f"{window2.compare.gain_spin.value()}")
        self.check(bytes(window2.saveGeometry().toBase64()).decode("ascii") == geometry_before,
                   "창 크기·위치 복원")
        window2.close()
        pump(400)
        self.records.append({"step": "roundtrip", "saved": saved})

    # ---------------------------------------------------------------- 검사 B

    def check_broken_settings(self) -> None:
        print("\n[B] 깨진 설정 파일", flush=True)
        cases = {
            "쓰레기 값": {"hwdec": "존재하지않는디코더", "wipe": "abc",
                       "diff_gain": 9999, "compare_mode": "???"},
            "모르는 키": {"hwdec": "no", "옛날키": 1, "another": [1, 2]},
            "JSON 아님": None,
        }
        for label, payload in cases.items():
            if payload is None:
                self.config.write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
            else:
                self.config.write_text(json.dumps(payload, ensure_ascii=False),
                                       encoding="utf-8")
            loaded = settings_mod.load(self.config)
            valid = (loaded.hwdec in {v for v, _ in settings_mod.HWDEC_CHOICES}
                     and 0.0 <= loaded.wipe <= 1.0
                     and 1 <= loaded.diff_gain <= 64
                     and loaded.compare_mode in {"side", "wipe", "diff"})
            self.check(valid, f"{label} -> 기본값으로 물러남",
                       f"hwdec={loaded.hwdec} wipe={loaded.wipe} "
                       f"gain={loaded.diff_gain} mode={loaded.compare_mode}")
        self.config.unlink(missing_ok=True)

    # ------------------------------------------------------------- 검사 C/D

    def check_hwdec_and_keyframes(self) -> None:
        print("\n[C] 디코더 전환 (5-1)", flush=True)
        window = self.open_window()
        player = window.playback.player
        info = player.video_info

        first = info.get("하드웨어 디코딩", "")
        print(f"    기본값(auto-safe)에서 실제로 쓰이는 것: {first}", flush=True)

        # mpv 가 디코더를 다시 만들 시간을 준다. 바로 읽으면 이전 값이 나온다.
        settle = window.HWDEC_SETTLE_MS + 600

        window.set_hwdec("no")
        pump(settle)
        text = window.playback.player.video_info.get("하드웨어 디코딩", "")
        self.check(text == "no", "끄기를 고르면 소프트웨어로 (군더더기 설명 없이)", text)

        # 하드웨어로 **되돌아가는 것**이 중요하다. 여기서 예전에 틀렸었다 —
        # 설정 직후에 읽는 바람에 되는 디코더를 두고 "안 된다"고 표시했다.
        window.set_hwdec("d3d11va")
        pump(settle)
        text = window.playback.player.video_info.get("하드웨어 디코딩", "")
        self.check(text == first or text.startswith("d3d11va")
                   or "안 돼서 소프트웨어로" in text,
                   "하드웨어로 되돌아감 (안 되는 PC 면 이유를 붙여 실제 값)", text)

        window.set_hwdec("nvdec")
        pump(settle)
        text = window.playback.player.video_info.get("하드웨어 디코딩", "")
        self.check("[" not in text, "리스트가 그대로 새어 나오지 않음", text)

        window.set_hwdec("auto-safe")
        pump(settle)
        text = window.playback.player.video_info.get("하드웨어 디코딩", "")
        self.check(text == first, "auto-safe 로 돌아오면 처음과 같은 디코더",
                   f"{text} (처음 {first})")
        self.records.append({"step": "hwdec", "auto": first, "back": text})

        print("\n[D] 키프레임 간격 (5-2)", flush=True)
        index = player.index
        shown = window.playback.player.video_info.get("키프레임 간격", "")
        gaps = index.keyframe_gaps
        want_median = int(statistics.median(gaps))
        want_seconds = statistics.median(
            [index.times[index.keyframes[i + 1]] - index.times[index.keyframes[i]]
             for i in range(len(index.keyframes) - 1)])
        print(f"    표시: {shown}", flush=True)
        self.check(str(want_median) in shown, "간격(프레임)이 인덱스와 일치",
                   f"인덱스 {want_median}")
        self.check(f"{want_seconds:.2f}초" in shown, "간격(초)이 실제 표에서 계산됨",
                   f"인덱스 {want_seconds:.2f}초")
        self.check(f"{len(index.keyframes):,}개" in shown, "키프레임 개수가 맞음",
                   f"{len(index.keyframes)}개")

        screengrab.pin_on_top(window)
        pump(400)
        screengrab.grab_screen(self.out_dir / "settings_info.png")
        screengrab.release(window)
        window.close()
        pump(400)
        self.records.append({"step": "keyframes", "shown": shown})

    # ---------------------------------------------------------------- 마무리

    def finish(self) -> int:
        (self.out_dir / "result_settings.json").write_text(
            json.dumps({"failures": self.failures, "records": self.records},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n" + "=" * 62)
        if self.failures:
            print(f"실패 {len(self.failures)}건:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("전부 통과 — 설정이 왕복하고, 정보 패널이 실제 값을 보여 줍니다.")
        return 0


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else (
        Path(__file__).resolve().parent.parent / "shots" / "settings")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not SOURCE.is_file():
        print(f"{SOURCE} 가 없습니다.")
        return 1

    h = Harness(out_dir)
    h.check_roundtrip()
    h.check_broken_settings()
    h.check_hwdec_and_keyframes()
    return h.finish()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""GUI 를 실제로 띄우고 단축키를 두드려 화면과 카운터가 맞는지 확인한다.

프레임 번호가 그림에 박힌 테스트 영상으로 돌리는 게 전제다. 각 단계마다
전체 화면을 PNG 로 남기므로, 영상 안의 숫자와 앱이 표시하는 프레임 번호를
눈으로 대조할 수 있다.

QShortcut 을 통과하도록 QTest.keyClick 으로 진짜 키 이벤트를 보낸다 —
player.step_frames() 를 직접 부르면 단축키 연결이 끊겨 있어도 통과해 버린다.
2단계에서 붙은 S(캡쳐)도 같은 이유로 키 이벤트로 두드린다.

프레임 이동을 시작하기 전에 **PTS 인덱스가 붙기를 기다린다.** 인덱싱은
백그라운드 스레드라서, 안 기다리면 fps 추정 상태에서 검사해 버린다.

    python tools/smoke_gui.py <영상파일> <스크린샷폴더>
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

from app.main_window import MainWindow  # noqa: E402

INDEX_TIMEOUT_MS = 60_000
CAPTURE_TIMEOUT_MS = 30_000


def pump(ms: int) -> None:
    """이벤트 루프를 ms 만큼 돌린다 (mpv 의 seek 이 끝날 시간을 준다)."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def grab(name: str, out_dir: Path) -> str:
    return screengrab.grab_screen(out_dir / f"{name}.png")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1

    video = argv[1]
    out_dir = Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(argv)
    window = MainWindow()
    window.showMaximized()
    # 검증 내내 '항상 위'로 둔다 — 안 그러면 스크린샷에 편집기 창이 찍힌다.
    screengrab.pin_on_top(window)
    pump(1200)

    window.open_path(video)
    pump(1500)

    player = window.playback.player

    # 인덱싱이 끝나기를 기다린다 (백그라운드 스레드).
    waited = 0
    while not player.index_ready and waited < INDEX_TIMEOUT_MS:
        pump(200)
        waited += 200
    if not player.index_ready:
        print("!!  PTS 인덱스가 붙지 않았다 — fps 추정 상태로 검사한다", flush=True)
    else:
        idx = player.index
        print(f"인덱스: {idx.count} 프레임 · {'CFR' if idx.cfr else 'VFR'} · "
              f"{idx.bit_depth}bit · {waited}ms 대기", flush=True)

    print(f"열림: fps={player.fps:.6g} 전체={player.frame_count} 현재={player.frame}", flush=True)

    steps: list[dict] = []

    def record(name: str, expected: int | None) -> None:
        pump(700)
        entry = {
            "step": name,
            "expected": expected,
            "frame": player.frame,
            # 타임코드와 "현재 / 전체"가 서로 다른 라벨로 나뉘어 있다 (목업 배치).
            "counter": (f"{window.playback.counter.text()} · "
                        f"{window.playback.total_label.text()}"),
            "shot": grab(name, out_dir),
        }
        ok = expected is None or entry["frame"] == expected
        entry["ok"] = ok
        steps.append(entry)
        mark = "OK " if ok else "!! "
        print(f"  {mark} {name:<22} 기대={expected} 실제={entry['frame']}  '{entry['counter']}'", flush=True)

    def key(name: str, modifier=Qt.NoModifier, times: int = 1) -> None:
        for _ in range(times):
            QTest.keyClick(window, name, modifier)
            pump(300)

    # 기대값은 파일 끝에서 잘린다 — 짧은 테스트 영상에서 +100 은 끝에 멈추는 게 맞다.
    last = max(0, player.frame_count - 1)

    def clamp(n: int) -> int:
        return max(0, min(n, last))

    key(Qt.Key_Home)
    record("01_home", 0)

    key(Qt.Key_Right, times=5)
    record("02_right_x5", clamp(5))

    key(Qt.Key_Left, times=2)
    record("03_left_x2", clamp(clamp(5) - 2))

    cursor = clamp(clamp(5) - 2)
    key(Qt.Key_Right, Qt.ShiftModifier)
    cursor = clamp(cursor + 10)
    record("04_shift_right", cursor)

    key(Qt.Key_Left, Qt.ShiftModifier)
    cursor = clamp(cursor - 10)
    record("05_shift_left", cursor)

    key(Qt.Key_Right, Qt.ControlModifier)
    cursor = clamp(cursor + 100)
    record("06_ctrl_right", cursor)

    key(Qt.Key_End)
    record("07_end", last)

    key(Qt.Key_Left, times=3)
    record("08_end_left_x3", clamp(last - 3))

    # 재생/정지 토글이 실제로 먹는지.
    # 파일 앞쪽으로 돌아와서 시험한다 — 끝에서 누르면 몇 프레임 재생하고
    # keep-open 때문에 곧바로 다시 멈춰서, 재생된 적이 없는 것처럼 보인다.
    key(Qt.Key_Home)
    pump(600)

    key(Qt.Key_Space)
    pump(400)
    playing = not player.paused
    key(Qt.Key_Space)
    pump(400)
    paused_again = player.paused
    print(f"  {'OK ' if playing and paused_again else '!! '} space 토글: 재생됨={playing} 다시정지={paused_again}", flush=True)

    capture = check_capture_shortcut(window, player)

    (out_dir / "result.json").write_text(
        json.dumps(
            {"video": video, "fps": player.fps, "frame_count": player.frame_count,
             "index_ready": player.index_ready, "steps": steps,
             "space_toggle": playing and paused_again, "capture": capture},
            ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    failed = [s for s in steps if not s["ok"]]
    screengrab.release(window)
    window.close()          # closeEvent 가 모든 탭을 정리한다

    if failed or not (playing and paused_again) or not capture["ok"]:
        print(f"\n실패 {len(failed) + (0 if capture['ok'] else 1)}건", flush=True)
        return 1
    print("\n전부 통과", flush=True)
    return 0


def check_capture_shortcut(window, player) -> dict:
    """S 를 눌러 실제로 PNG 가 떨어지는지 본다.

    파일이 생겼는지만 보지 않는다 — **원본 해상도**로 나왔는지까지 본다.
    화면 픽셀을 긁는 식으로 잘못 구현하면 창 크기가 나오기 때문이다.
    """
    shot_dir = window.playback.shot_dir
    before = set(shot_dir.glob("*.png")) if shot_dir.is_dir() else set()

    QTest.keyClick(window, Qt.Key_S)
    waited, new = 0, set()
    while waited < CAPTURE_TIMEOUT_MS:
        pump(200)
        waited += 200
        new = (set(shot_dir.glob("*.png")) if shot_dir.is_dir() else set()) - before
        if new and not (window.playback._capture_worker
                        and window.playback._capture_worker.isRunning()):
            break

    result: dict = {"ok": False, "frame": player.current_frame, "waited_ms": waited}
    if not new:
        print(f"  !!  S 캡쳐: {shot_dir} 에 PNG 가 생기지 않았다", flush=True)
        return result

    path = sorted(new)[0]
    head = read_png_header(path)
    idx = player.index
    want = (idx.width, idx.height) if idx else None
    want_depth = (16 if idx and idx.bit_depth > 8 else 8)
    result.update({
        "file": path.name, "width": head["width"], "height": head["height"],
        "bit_depth": head["bit_depth"],
    })
    result["ok"] = (want is None
                    or ((head["width"], head["height"]) == want
                        and head["bit_depth"] == want_depth))
    print(f"  {'OK ' if result['ok'] else '!! '} S 캡쳐: {path.name} "
          f"{head['width']}x{head['height']} {head['bit_depth']}bit "
          f"(원본 {want[0]}x{want[1]} {want_depth}bit 기대)", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

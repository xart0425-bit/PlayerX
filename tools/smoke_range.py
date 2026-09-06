"""구간 재생 검사 — 고른 클립의 IN~OUT 밖으로 재생이 새지 않는가.

목록에서 클립을 고르고 재생하면 그 구간만 돌아야 하고, '구간 반복'을 켜 두면
끝에서 IN 으로 돌아가야 한다. 꺼 두면 OUT 에서 멈춰야 한다.

**실제로 재생시켜 놓고 프레임 번호를 계속 받아 적는 방식**으로 확인한다.
`_enforce_range()` 를 직접 불러 보는 건 의미가 없다 — 그 함수가 폴링에 제대로
연결돼 있는지, mpv 가 실제로 그 자리에 서는지가 확인하려는 것이기 때문이다.

    python tools/smoke_range.py [<스크린샷폴더>]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import screengrab  # noqa: E402

from app.main_window import MainWindow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "testdata" / "test_60fps.mkv"

IN_FRAME, OUT_FRAME = 100, 160
# seek 은 목표 프레임에 정확히 서지만, 재생 중에는 폴링 사이에 한두 프레임
# 더 지나간 뒤에 잡힌다. 그 정도는 새는 게 아니다.
SLACK = 6

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'OK ' if ok else '!! '} {label}  {detail}", flush=True)
    if not ok:
        failures.append(label)


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def sample(player, seconds: float, step_ms: int = 40) -> list[int]:
    """재생하는 동안 프레임 번호를 계속 받아 적는다."""
    frames: list[int] = []
    for _ in range(int(seconds * 1000 / step_ms)):
        pump(step_ms)
        frames.append(player.current_frame)
    return frames


def wraps(frames: list[int]) -> int:
    """앞으로 가다가 뒤로 크게 되돌아간 횟수 = 반복한 횟수."""
    return sum(1 for a, b in zip(frames, frames[1:]) if b < a - 20)


def longest_stall(frames: list[int]) -> int:
    """같은 프레임 번호가 연달아 몇 번이나 나왔는가.

    재생 중이면 표시가 계속 움직여야 한다. 한 자리에 오래 붙어 있으면
    타임라인이 멈춰 보인다 — 구간 반복 직후에 실제로 그랬다.
    """
    longest = run = 1
    for a, b in zip(frames, frames[1:]):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    return longest


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "shots" / "range"
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(argv[:1])
    window = MainWindow()
    window.show()
    window.showNormal()
    window.resize(1400, 900)
    pump(700)
    screengrab.pin_on_top(window)

    trim = window.trim
    window.tabs.setCurrentIndex(2)
    pump(300)
    trim.open_file(str(VIDEO))

    waited = 0
    while not trim.player.index_ready and waited < 60_000:
        pump(200)
        waited += 200
    check(trim.player.index_ready, "프레임 인덱스 준비", f"{trim.player.frame_count} 프레임")

    # --- 클립 만들기 (I, O 두 번으로 목록에 들어간다) ---
    trim.goto_frame(IN_FRAME)
    pump(400)
    trim.mark_in()
    trim.goto_frame(OUT_FRAME)
    pump(400)
    trim.mark_out()
    pump(300)

    clip = trim.selected_clip()
    check(clip is not None and (clip.in_frame, clip.out_frame) == (IN_FRAME, OUT_FRAME),
          "IN·OUT 으로 클립이 목록에 들어가고 선택됨",
          f"{clip.in_frame} ~ {clip.out_frame}" if clip else "없음")

    print("\n[A] 구간 반복 켜짐 — IN~OUT 만 돌아야 한다", flush=True)
    trim.range_check.setChecked(True)
    trim.loop_check.setChecked(True)
    trim.goto_frame(IN_FRAME)
    pump(400)

    trim.toggle_pause()
    frames = sample(trim.player, 4.0)
    trim.player.set_paused(True)
    pump(300)

    low, high = min(frames), max(frames)
    check(low >= IN_FRAME - SLACK, "구간 앞으로 새지 않음", f"가장 낮은 프레임 {low} (IN {IN_FRAME})")
    check(high <= OUT_FRAME + SLACK, "구간 뒤로 새지 않음", f"가장 높은 프레임 {high} (OUT {OUT_FRAME})")
    check(wraps(frames) >= 2, "구간 끝에서 IN 으로 되돌아감", f"{wraps(frames)}회 반복")

    # 40ms 간격으로 받아 적으므로, 60fps 영상이면 한 칸에 두 번 이상 머물 일이
    # 거의 없다. 되돌아간 직후의 seek 대기까지 감안해도 5칸을 넘으면(=0.2초)
    # 눈에 "멈췄다"로 보인다.
    stall = longest_stall(frames)
    check(stall <= 5, "되돌아간 뒤 타임라인이 멈추지 않음",
          f"같은 프레임 최대 {stall}회 연속 ({stall * 40}ms)")
    screengrab.grab_screen(out_dir / "range_01_반복재생.png")

    print("\n[B] 구간 반복 꺼짐 — OUT 에서 멈춰야 한다", flush=True)
    trim.loop_check.setChecked(False)
    trim.goto_frame(IN_FRAME)
    pump(400)

    trim.toggle_pause()
    frames = sample(trim.player, 3.0)
    pump(400)

    check(trim.player.paused, "OUT 에서 스스로 멈춤", f"정지={trim.player.paused}")
    check(abs(trim.player.current_frame - OUT_FRAME) <= SLACK,
          "멈춘 자리가 OUT", f"{trim.player.current_frame} (OUT {OUT_FRAME})")
    check(max(frames) <= OUT_FRAME + SLACK, "멈추기 전에도 안 새어 나감",
          f"가장 높은 프레임 {max(frames)}")
    screengrab.grab_screen(out_dir / "range_02_OUT정지.png")

    print("\n[C] '선택 구간만 재생' 을 끄면 구간을 넘어가야 한다", flush=True)
    trim.range_check.setChecked(False)
    trim.goto_frame(IN_FRAME)
    pump(400)

    trim.toggle_pause()
    frames = sample(trim.player, 2.5)
    trim.player.set_paused(True)
    pump(300)

    check(max(frames) > OUT_FRAME + SLACK, "구간을 지나 계속 재생됨",
          f"가장 높은 프레임 {max(frames)} > OUT {OUT_FRAME}")

    print("\n[D] 타임라인은 구간 밖으로도 갈 수 있어야 한다 (가두는 건 재생뿐)", flush=True)
    trim.range_check.setChecked(True)
    pump(200)
    trim.goto_frame(500)
    pump(600)
    check(abs(trim.player.current_frame - 500) <= 1, "정지 상태에서 구간 밖으로 이동 가능",
          f"{trim.player.current_frame}")

    screengrab.release(window)
    window.showNormal()
    window.close()
    pump(300)

    print("\n" + "=" * 62)
    if failures:
        print(f"실패 {len(failures)}건: " + ", ".join(failures))
        return 1
    print("전부 통과 — 고른 클립의 구간 안에서만 재생되고, 반복·정지가 맞습니다.")
    print(f"스크린샷: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""프레임 이동이 실제로 그 프레임에 도착하는지 검증한다.

GUI 없이(vo=null) libmpv 를 띄워 app/player.py 와 똑같은 seek 계산을 돌린다.
검사하는 것:

  A. 절대 seek — 목표 프레임 f 로 보냈을 때 time-pos 가 f 프레임 안에 들어오는가
  B. 뒤로 1프레임 반복 — 우리 방식이 매번 정확히 1씩 줄어드는가
  C. 같은 조건에서 mpv 의 frame-back-step 은 어떻게 되는가 (비교용, 실패해도 통과)

C 는 우리 설계(뒤로 이동을 mpv 에 안 맡김)의 근거를 눈으로 확인하려고 같이 돈다.

    python tools/verify_frame_accuracy.py <영상파일> [<영상파일> ...]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import timecode  # noqa: E402
from app.mpv_loader import load_mpv  # noqa: E402
from app.player import LOAD_TIMEOUT, MPV_OPTIONS  # noqa: E402

SETTLE_TIMEOUT = 8.0


def open_headless(mpv, path: str):
    options = dict(MPV_OPTIONS)
    options["vo"] = "null"        # 창 없이 디코딩만
    options["ao"] = "null"
    options.pop("profile", None)  # low-latency 는 창 있을 때 얘기
    m = mpv.MPV(**options)
    m.play(path)
    # app/player.py 와 같은 대기 방식 — pause 로 열기 때문에 core-idle 은 안 풀린다.
    m.wait_for_property("video-params", lambda v: bool(v), timeout=LOAD_TIMEOUT)
    m.pause = True
    settle(m)
    return m


def settle(m) -> None:
    """seek 이 끝나기를 기다린다. 특정 값이 나오기를 기다리지 않는다."""
    deadline = time.monotonic() + SETTLE_TIMEOUT
    stable = 0
    last = object()
    while time.monotonic() < deadline:
        if not bool(m.seeking):
            now = m.time_pos
            if now == last:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
                last = now
        time.sleep(0.01)


def current_frame(m, fps: float) -> int:
    return timecode.frame_at_time(float(m.time_pos or 0.0), fps)


def check_absolute_seek(m, fps: float, count: int) -> list[str]:
    """A. 흩어진 목표 프레임으로 절대 seek 했을 때 정확히 도착하는가."""
    targets = [0, 1, 2, 7, 33, 100, 101, 149, count // 2, count // 2 + 1, count - 2, count - 1]
    targets = sorted({t for t in targets if 0 <= t < count})

    failures = []
    for want in targets:
        m.command("seek", timecode.seek_target(want, fps), "absolute", "exact")
        settle(m)
        got = current_frame(m, fps)
        mark = "OK " if got == want else "!! "
        print(f"    {mark} 목표 {want:>7} -> 도착 {got:>7}   time-pos={float(m.time_pos):.6f}")
        if got != want:
            failures.append(f"절대 seek: {want} 로 보냈는데 {got} 에 도착")
    return failures


def check_back_step(m, fps: float, count: int, steps: int = 30) -> list[str]:
    """B. 우리 방식으로 뒤로 1프레임씩 갔을 때 매번 정확히 1씩 줄어드는가."""
    start = min(count - 1, max(200, count // 2))
    m.command("seek", timecode.seek_target(start, fps), "absolute", "exact")
    settle(m)

    cursor = current_frame(m, fps)
    failures = []
    trace = [cursor]
    for _ in range(steps):
        want = cursor - 1
        if want < 0:
            break
        m.command("seek", timecode.seek_target(want, fps), "absolute", "exact")
        settle(m)
        got = current_frame(m, fps)
        trace.append(got)
        if got != want:
            failures.append(f"뒤로 이동: {cursor} 에서 {want} 로 가려 했는데 {got}")
        cursor = want  # 우리 커서는 계산값을 따라간다 (앱과 같은 동작)

    print(f"    자취: {trace[0]} -> {trace[-1]} ({len(trace) - 1} 스텝)")
    deltas = {trace[i] - trace[i + 1] for i in range(len(trace) - 1)}
    print(f"    스텝 간격 집합: {sorted(deltas)}  (전부 1 이어야 정상)")
    return failures


def observe_mpv_back_step(m, fps: float, count: int, steps: int = 30) -> None:
    """C. mpv 의 frame-back-step 은 어떻게 되는지 관찰만 한다."""
    start = min(count - 1, max(200, count // 2))
    m.command("seek", timecode.seek_target(start, fps), "absolute", "exact")
    settle(m)

    trace = [current_frame(m, fps)]
    for _ in range(steps):
        m.command("frame-back-step")
        settle(m)
        trace.append(current_frame(m, fps))

    deltas = [trace[i] - trace[i + 1] for i in range(len(trace) - 1)]
    skipped = [d for d in deltas if d != 1]
    print(f"    자취: {trace[0]} -> {trace[-1]} ({len(trace) - 1} 스텝)")
    print(f"    스텝 간격 집합: {sorted(set(deltas))}")
    if skipped:
        print(f"    -> 1이 아닌 간격 {len(skipped)}회 발생. 우리가 이걸 안 쓰는 이유.")
    else:
        print("    -> 이 파일에서는 mpv 도 정확했다 (그래도 신뢰하지 않는다).")


def verify(path: str) -> list[str]:
    mpv = load_mpv()
    print(f"\n=== {Path(path).name} ===")
    m = open_headless(mpv, path)
    try:
        fps = float(m.container_fps or 0.0)
        count = int(m.estimated_frame_count or 0)
        duration = float(m.duration or 0.0)
        print(f"  fps={fps:.6g}  전체프레임={count}  길이={duration:.3f}s  코덱={m.video_format}")
        if fps <= 0 or count <= 0:
            return [f"{Path(path).name}: fps/프레임수를 읽지 못함"]

        failures: list[str] = []
        print("  [A] 절대 seek 정확도")
        failures += check_absolute_seek(m, fps, count)
        print("  [B] 뒤로 1프레임 반복 (PlayerX 방식)")
        failures += check_back_step(m, fps, count)
        print("  [C] 참고: mpv frame-back-step")
        observe_mpv_back_step(m, fps, count)
        return [f"{Path(path).name}: {f}" for f in failures]
    finally:
        m.terminate()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    all_failures: list[str] = []
    for path in argv[1:]:
        all_failures += verify(path)

    print("\n" + "=" * 60)
    if all_failures:
        print(f"실패 {len(all_failures)}건:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    print("전부 통과 — 프레임 이동이 목표 프레임에 정확히 도착합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""timecode 모듈 단위 테스트 — 파일 없이 계산만 검증한다.

    python tools/test_timecode.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.timecode import (  # noqa: E402
    SEEK_LEAD,
    format_clock,
    format_timecode,
    frame_at_time,
    seek_target,
    time_of_frame,
)

FPS_LIST = [23.976023976023978, 24.0, 25.0, 29.97002997002997, 30.0, 50.0, 59.94, 60.0, 120.0, 240.0]


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def test_roundtrip(failures: list[str]) -> None:
    """이론상 PTS 를 다시 프레임 번호로 되돌린다."""
    for fps in FPS_LIST:
        for n in [0, 1, 2, 47, 300, 599, 1000, 12345]:
            got = frame_at_time(time_of_frame(n, fps), fps)
            check(got == n, f"왕복 실패 fps={fps} n={n} -> {got}", failures)


def test_quantized_pts(failures: list[str]) -> None:
    """mkv 처럼 PTS 가 1ms 로 양자화돼도 프레임 번호를 되찾아야 한다.

    이게 처음에 한 프레임 어긋나게 만들었던 실제 케이스다.
    """
    for fps in FPS_LIST:
        for n in [0, 1, 59, 300, 599, 1799, 5000]:
            quantized = round(time_of_frame(n, fps) * 1000) / 1000.0
            got = frame_at_time(quantized, fps)
            check(got == n, f"1ms 양자화 실패 fps={fps} n={n} pts={quantized} -> {got}", failures)


def test_seek_target_lands(failures: list[str]) -> None:
    """seek 목표가 '프레임 n-1 의 PTS 보다 뒤, 프레임 n 의 PTS 보다 앞'에 있어야 한다.

    mpv 의 exact seek 은 목표 이후 첫 프레임으로 가므로 이 조건이면 n 에 착지한다.
    """
    for fps in FPS_LIST:
        for n in [1, 2, 60, 599, 4321]:
            target = seek_target(n, fps)
            prev_pts = round(time_of_frame(n - 1, fps) * 1000) / 1000.0
            this_pts = round(time_of_frame(n, fps) * 1000) / 1000.0
            check(prev_pts < target, f"목표가 너무 앞 fps={fps} n={n}: {prev_pts} !< {target}", failures)
            check(target <= this_pts, f"목표가 너무 뒤 fps={fps} n={n}: {target} !<= {this_pts}", failures)

    check(seek_target(0, 60.0) == 0.0, "0번 프레임 목표는 0 이어야 한다", failures)
    check(0 < SEEK_LEAD < 0.75, "SEEK_LEAD 는 0 과 0.75 사이여야 안전하다", failures)


def test_formatting(failures: list[str]) -> None:
    cases = [
        ((0, 24.0), "00:00:00:00"),
        ((23, 24.0), "00:00:00:23"),
        ((24, 24.0), "00:00:01:00"),
        ((24 * 60, 24.0), "00:01:00:00"),
        ((24 * 3600, 24.0), "01:00:00:00"),
        ((30, 29.97), "00:00:01:00"),
        ((0, 0.0), "--:--:--:--"),
        # 120fps 는 프레임 칸이 세 자리 (0~119)
        ((103, 120.0), "00:00:00:103"),
        ((119, 120.0), "00:00:00:119"),
        ((120, 120.0), "00:00:01:000"),
        ((1199, 120.0), "00:00:09:119"),
    ]
    for (frame, fps), want in cases:
        got = format_timecode(frame, fps)
        check(got == want, f"타임코드 fps={fps} frame={frame}: {got} != {want}", failures)

    check(format_clock(83.5) == "0:01:23.500", f"clock: {format_clock(83.5)}", failures)
    check(format_clock(3661.007) == "1:01:01.007", f"clock: {format_clock(3661.007)}", failures)


def test_edges(failures: list[str]) -> None:
    check(frame_at_time(-1.0, 60.0) == 0, "음수 시각은 0 프레임", failures)
    check(frame_at_time(1.0, 0.0) == 0, "fps 0 이면 0 프레임", failures)
    check(seek_target(5, 0.0) == 0.0, "fps 0 이면 목표 0", failures)


def main() -> int:
    failures: list[str] = []
    for fn in (test_roundtrip, test_quantized_pts, test_seek_target_lands, test_formatting, test_edges):
        before = len(failures)
        fn(failures)
        status = "OK" if len(failures) == before else f"실패 {len(failures) - before}"
        print(f"  {fn.__name__:<26} {status}")

    if failures:
        print(f"\n실패 {len(failures)}건:")
        for f in failures[:20]:
            print(f"  - {f}")
        return 1
    print("\n전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

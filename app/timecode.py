"""프레임 번호 <-> 타임코드 <-> 시각 변환.

여기 있는 두 함수(frame_at_time, seek_target)가 "프레임 정확도"의 전부다.
둘 다 실제 파일로 검증하면서 다음 두 가지를 알아내고 지금 형태가 됐다.
(tools/verify_frame_accuracy.py 가 그 검증을 자동화한 것)

1) **PTS 는 양자화돼 있다.** mkv 의 기본 타임베이스는 1ms 라서 60fps 영상의
   599번 프레임 PTS 는 9.98333... 이 아니라 9.983 으로 저장된다.
   그래서 floor(time * fps) 로 프레임 번호를 내면 598 이 나온다 — 한 프레임 어긋난다.
   CFR 파일에서 PTS 는 n/fps 근처에 ±(타임베이스/2) 안에 들어오므로,
   **반올림**이 프레임 번호를 되찾는 올바른 방법이다.

2) **mpv 의 exact seek 은 목표 시각 '이후' 첫 프레임으로 간다** (그 시각을 '포함하는'
   프레임이 아니다). 그래서 프레임 한가운데((n+0.5)/fps)를 겨냥하면 항상 n+1 에
   도착한다. 프레임 시작보다 살짝 앞(SEEK_LEAD 만큼)을 겨냥해야 n 에 도착한다.

논드롭프레임(NDF) 기준이다. 29.97 같은 소수 fps 에서도 타임코드는 프레임을 그냥
세는 방식으로 표시한다 — 방송용 드롭프레임 타임코드는 검수 용도에 필요 없다.
"""

from __future__ import annotations

import math

# seek 목표를 프레임 시작보다 이만큼(프레임 단위) 앞에 둔다.
# 너무 작으면 부동소수점/양자화 오차로 다음 프레임에 걸리고,
# 너무 크면(0.75 초과) 이전 프레임에 걸린다. 0.25 면 양쪽으로 여유가 넉넉하다.
SEEK_LEAD = 0.25


def frame_at_time(seconds: float, fps: float) -> int:
    """시각(초)에 해당하는 프레임 번호(0-based).

    반올림이다. 이유는 모듈 설명 (1) 참고.
    """
    if fps <= 0 or seconds is None:
        return 0
    return max(0, math.floor(seconds * fps + 0.5))


def time_of_frame(frame: int, fps: float) -> float:
    """프레임의 이론상 시작 시각(초). 표시용."""
    if fps <= 0:
        return 0.0
    return max(0.0, frame / fps)


def seek_target(frame: int, fps: float) -> float:
    """그 프레임에 착지하기 위해 mpv 에 건네야 할 시각(초).

    프레임 시작보다 SEEK_LEAD 만큼 앞. 이유는 모듈 설명 (2) 참고.
    """
    if fps <= 0:
        return 0.0
    return max(0.0, (frame - SEEK_LEAD) / fps)


def format_timecode(frame: int, fps: float) -> str:
    """프레임 번호를 HH:MM:SS:FF 로.

    마지막 칸의 자릿수는 fps 를 따른다 — 120fps 면 0~119 라서 세 자리다.
    두 자리로 고정하면 119 가 넘쳐서 자릿수가 들쭉날쭉해진다.
    """
    if fps <= 0:
        return "--:--:--:--"
    fps_int = max(1, int(round(fps)))
    frame = max(0, int(frame))
    ff = frame % fps_int
    total_seconds = frame // fps_int
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    ff_width = max(2, len(str(fps_int - 1)))
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:0{ff_width}d}"


def parse_timecode(text: str, fps: float) -> int | None:
    """사람이 친 타임코드를 프레임 번호로. 못 읽으면 None.

    받아 주는 형태는 셋이다 — `HH:MM:SS:FF` · `MM:SS:FF` · 그냥 프레임 번호.
    검수 중에 손으로 칠 때는 앞자리를 생략하는 쪽이 훨씬 빠르다.
    """
    text = (text or "").strip().replace(";", ":")
    if not text:
        return None

    if ":" not in text:
        try:
            return max(0, int(text.replace(",", "")))
        except ValueError:
            return None

    if fps <= 0:
        return None

    parts = text.split(":")
    if len(parts) > 4:
        return None
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in numbers):
        return None

    ff = numbers[-1]
    ss = numbers[-2] if len(numbers) >= 2 else 0
    mm = numbers[-3] if len(numbers) >= 3 else 0
    hh = numbers[-4] if len(numbers) >= 4 else 0

    fps_int = max(1, int(round(fps)))
    if ff >= fps_int:
        return None
    return ((hh * 3600 + mm * 60 + ss) * fps_int) + ff


def format_clock(seconds: float) -> str:
    """초를 H:MM:SS.mmm 로. 타임코드와 나란히 보여 주는 용도."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return "-:--:--.---"
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    ss = total_s % 60
    mm = (total_s // 60) % 60
    hh = total_s // 3600
    return f"{hh}:{mm:02d}:{ss:02d}.{ms:03d}"

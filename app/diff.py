"""두 프레임의 차분(diff) 계산.

비교 모드의 세 번째 모드다. 좌우 분할과 와이프는 "눈으로 보이는 차이"를 찾는
도구지만, 인코딩 비교에서 정작 중요한 차이는 대개 **눈에 안 보인다**.
그래서 두 프레임을 빼서 그 차이만 남기고, 증폭해서 보여 준다.

지키는 원칙 두 가지.

* **화면 픽셀이 아니라 원본을 다시 디코딩해서 뺀다.** 캡쳐와 같은 이유다
  (app/capture.py 모듈 설명 참고). 화면에 그려진 그림을 빼면 창 크기·GPU 의
  색 변환이 차이에 섞여서, 인코딩 차이인지 렌더링 차이인지 구분할 수 없다.
* **16bit 공간에서 뺀다.** 8bit 로 내려서 빼면 10bit 소스의 미세한 차이가
  반올림에 먹힌다. 두 쪽 다 rgb48 로 올린 뒤 빼고, 표시할 때만 8bit 로 내린다.

**증폭(gain)에 대해** — 차이는 대개 0~2 수준이라 그냥 그리면 새까만 그림이 된다.
그래서 gain 배로 곱해서 보여 준다. 그림은 '보이라고' 증폭된 값이고, 옆에 적히는
숫자(최대·평균·PSNR)는 **증폭 전 실제 값**이다. 둘을 섞으면 안 된다.

해상도가 다르면 뺄 수가 없다. 이때만 B 를 A 크기로 리샘플링하는데, 그러면
**리샘플링 오차가 차분에 섞인다** — 그 사실을 결과에 표시해서 UI 가 경고하게 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# 차분을 계산하는 작업 공간. 8bit 소스도 여기로 올려서 뺀다 (v -> v*257).
WORK_PIX_FMT = "rgb48be"
WORK_MAX = 65535
# 16bit 값을 8bit 기준 숫자로 환산할 때 쓰는 나눗수. 255*257 == 65535 이다.
TO_8BIT = 257.0

GAIN_MIN, GAIN_MAX = 1, 64


@dataclass
class DiffResult:
    """차분 한 장. image 만 보여 주는 게 아니라 숫자도 같이 낸다."""

    image: np.ndarray          # HxWx3 uint8 — 증폭된 차분 그림 (화면용)
    width: int
    height: int
    gain: int
    max_diff: float            # 8bit 기준 최대 채널 차이 (증폭 전)
    mean_diff: float           # 8bit 기준 평균 채널 차이 (증폭 전)
    psnr: float | None         # None 이면 완전히 동일 (mse == 0)
    identical: bool
    changed_ratio: float       # 한 채널이라도 다른 픽셀의 비율
    resampled: bool            # 해상도가 달라 B 를 늘렸는가
    size_a: tuple[int, int]
    size_b: tuple[int, int]

    def summary(self) -> str:
        if self.identical:
            head = "두 프레임이 완전히 동일합니다 (차이 0)"
        else:
            psnr = "∞" if self.psnr is None else f"{self.psnr:.2f} dB"
            head = (f"최대 {self.max_diff:.0f} · 평균 {self.mean_diff:.3f} · "
                    f"PSNR {psnr} · 다른 픽셀 {self.changed_ratio * 100:.2f}%")
        if self.resampled:
            head += (f"   ※ 해상도가 달라 B({self.size_b[0]}x{self.size_b[1]})를 "
                     f"A 크기로 늘려서 뺐습니다 — 리샘플링 오차가 섞여 있습니다")
        return head


def _to_work_array(capturer, frame_no: int, width: int | None, height: int | None) -> np.ndarray:
    """프레임을 16bit RGB numpy 배열로. 색 변환은 그 파일의 인덱스 정보를 따른다."""
    frame, _exact = capturer.decode_frame(frame_no)
    rgb = capturer.to_rgb(frame, WORK_PIX_FMT, width, height)
    # rgb48be 는 빅엔디안이라 numpy 로 볼 때 dtype 을 명시해야 한다.
    array = np.frombuffer(bytes(rgb.planes[0]), dtype=">u2")
    stride = rgb.planes[0].line_size // 2
    array = array.reshape(rgb.height, stride)[:, : rgb.width * 3]
    return array.reshape(rgb.height, rgb.width, 3).astype(np.int32)


def compute_diff(capturer_a, capturer_b, frame_a: int, frame_b: int, gain: int = 1) -> DiffResult:
    """A 의 frame_a 와 B 의 frame_b 를 빼서 DiffResult 를 만든다.

    두 capturer 는 각자 자기 파일의 FrameCapturer 다 (색 정보도 각자 것을 쓴다).
    같은 스레드에서 불러야 한다 — PyAV 컨테이너는 스레드 안전하지 않다.
    """
    gain = max(GAIN_MIN, min(int(gain), GAIN_MAX))

    size_a = (capturer_a.index.width, capturer_a.index.height)
    size_b = (capturer_b.index.width, capturer_b.index.height)
    resampled = size_a != size_b

    a = _to_work_array(capturer_a, frame_a, None, None)
    # 크기가 다르면 B 를 A 에 맞춘다. 뺄 수 있게 만드는 게 우선이고,
    # 그 대가(리샘플링 오차)는 결과에 적어 UI 가 알리게 한다.
    b = _to_work_array(capturer_b, frame_b, size_a[0] if resampled else None,
                       size_a[1] if resampled else None)
    if a.shape != b.shape:
        raise ValueError(f"차분할 수 없습니다: {a.shape} vs {b.shape}")

    delta = np.abs(a - b)
    max_raw = int(delta.max())
    identical = max_raw == 0

    mse = float(np.mean((a - b).astype(np.float64) ** 2))
    psnr = None if mse <= 0 else 10.0 * math.log10((WORK_MAX ** 2) / mse)
    changed = float(np.count_nonzero(delta.any(axis=2))) / (a.shape[0] * a.shape[1])

    # 화면용: 증폭한 뒤 8bit 로. 곱하기 전에 8bit 로 내리면 1 미만 차이가
    # 전부 0 이 돼서 증폭할 게 없어진다 — 순서가 중요하다.
    shown = np.clip(delta * gain, 0, WORK_MAX) / TO_8BIT
    image = np.ascontiguousarray(shown.astype(np.uint8))

    return DiffResult(
        image=image,
        width=a.shape[1],
        height=a.shape[0],
        gain=gain,
        max_diff=max_raw / TO_8BIT,
        mean_diff=float(delta.mean()) / TO_8BIT,
        psnr=psnr,
        identical=identical,
        changed_ratio=changed,
        resampled=resampled,
        size_a=size_a,
        size_b=size_b,
    )

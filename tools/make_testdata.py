"""2단계 검증용 영상을 만든다 — 10bit 와 VFR.

1단계 테스트 파일(60/120/23.976fps)로는 2단계에서 새로 생긴 두 가지를
확인할 수 없다.

  * **10bit 소스** — 16bit PNG 로 저장되는지 확인할 대상이 없다.
  * **VFR** — PTS 인덱스가 fps 곱셈보다 나은 이유가 VFR 인데 VFR 파일이 없다.

그래서 여기서 만든다. 두 파일 모두 **프레임 n 의 색이 n 을 인코딩한 값**이다.
그래서 "n 번 프레임을 캡쳐했다"는 주장을 픽셀 값으로 검산할 수 있다 —
검증 도구(verify_index_capture.py)가 그걸 한다.

코덱은 ffv1(무손실)이다. 손실 압축이면 색이 미세하게 변해서 검산이 안 된다.

    python tools/make_testdata.py
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import av  # noqa: E402
import numpy as np  # noqa: E402
from av.video.reformatter import ColorRange, Colorspace, VideoReformatter  # noqa: E402

WIDTH, HEIGHT = 320, 180
TESTDATA = Path(__file__).resolve().parent.parent / "testdata"

# 이 파일들이 스스로 주장하는 색 정보. 아래 to_yuv() 가 실제로 이 값으로
# 변환하고, 컨테이너에도 같은 값을 적는다 — 둘이 어긋나면 캡쳐가 되살린
# 색이 원본과 달라진다. (처음에 여기서 걸렸다: 기본값 BT.601 로 변환해
# 놓고 파일에는 BT.709 라고 적어 뒀었다)
MATRIX = Colorspace.ITU709
RANGE = ColorRange.MPEG          # limited (tv)


# 프레임 번호를 색에 심을 때 쓰는 눈금. 값을 4씩 띄워 놓으면 되읽을 때
# ±1 오차가 껴도 원래 자리로 반올림된다.
#
# 왜 오차가 끼나: RGB -> YUV(limited) -> RGB 왕복은 8bit 에서 무손실이 아니다.
# limited 레인지는 256 단계 중 219 단계만 쓰므로 되돌릴 때 ±1 이 뜬다.
# 눈금 1 로 심으면 89 번 프레임이 90 번으로 읽히는 일이 실제로 생긴다.
# (처음에 여기서 걸렸다)
STEP = 4
DIGIT_BITS = 6                       # 채널당 0~63 -> 두 채널로 4096 프레임까지


def frame_color(n: int) -> tuple[int, int, int]:
    """프레임 번호 n 을 8bit RGB 로 인코딩한다.

    64 진수 두 자리다. 윗자리를 R, 아랫자리를 G 에, 각각 STEP 배로 벌려 넣는다.
    B 는 "이 파일이 맞다"를 확인할 상수.

    **기준을 항상 8bit 로 두는 게 요점이다.** 원본이 10bit 든 캡쳐가 16bit PNG 든,
    값을 8bit 로 정규화하면 같은 번호가 나온다. 그래서 검증 도구는 비트 뎁스와
    무관하게 같은 방식으로 번호를 되읽을 수 있다.
    """
    mask = (1 << DIGIT_BITS) - 1
    return STEP * ((n >> DIGIT_BITS) & mask), STEP * (n & mask), 0x80


def _rgb_frame(n: int, bits: int) -> av.VideoFrame:
    """번호가 박힌 단색 프레임.

    목표 비트 뎁스에 맞춰 만든다. 16bit RGB 로 만들어 놓고 8bit YUV 로 내리면
    swscale 이 **디더링**을 걸어서 단색이 얼룩진다 — 그러면 "이 픽셀이 몇 번
    프레임인가"를 읽을 수 없다. (처음에 여기서 걸렸다)

    8bit 값 v 를 16bit 에 넣을 때는 v*257 이다 (v<<8 이 아니다 —
    255 가 65535 로 정확히 가야 한다).
    """
    r, g, b = frame_color(n)
    if bits > 8:
        arr = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint16)
        arr[..., 0], arr[..., 1], arr[..., 2] = r * 257, g * 257, b * 257
        return av.VideoFrame.from_ndarray(arr, format="rgb48le")
    arr = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = r, g, b
    return av.VideoFrame.from_ndarray(arr, format="rgb24")


def to_yuv(rgb: av.VideoFrame, pix_fmt: str) -> av.VideoFrame:
    """RGB(full) -> YUV. 매트릭스와 레인지를 파일에 적을 값 그대로 명시한다.

    reformat 에 아무것도 안 주면 swscale 기본값(BT.601 계열)으로 변환된다.
    그래 놓고 컨테이너에는 BT.709 라고 적으면, 캡쳐가 BT.709 로 되돌릴 때
    원본과 다른 색이 나온다 — 검증이 통째로 무의미해진다.
    """
    return VideoReformatter().reformat(
        rgb, format=pix_fmt,
        src_colorspace=MATRIX, dst_colorspace=MATRIX,
        src_color_range=ColorRange.JPEG,   # 우리가 만든 RGB 는 풀레인지
        dst_color_range=RANGE,
    )


def make(path: Path, frames: int, pix_fmt: str, bits: int,
         pts_list: list[int], time_base: Fraction, rate: Fraction,
         shift: int = 0) -> None:
    """프레임마다 색이 다른 무손실 mkv 를 쓴다. PTS 는 직접 지정한다.

    **인코더의 time_base 를 직접 박는 게 핵심이다.** rate 만 주면 인코더
    time_base 가 1/rate 가 되고, 우리가 준 ms 단위 PTS 가 프레임 단위로
    반올림된다 — VFR 파일에서는 서로 다른 두 프레임의 PTS 가 같은 값으로
    뭉개져서 애초에 VFR 파일이 만들어지지 않는다. (처음에 여기서 걸렸다)
    """
    with av.open(str(path), "w") as out:
        stream = out.add_stream("ffv1", rate=rate)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = pix_fmt
        stream.time_base = time_base
        stream.codec_context.time_base = time_base
        # 색 정보를 파일에 적어 둔다 — 캡쳐가 이걸 읽어 쓰는지도 검증 대상이다.
        stream.codec_context.colorspace = int(MATRIX)
        stream.codec_context.color_range = int(RANGE)

        for n in range(frames):
            # shift 를 주면 n 번 프레임이 (n - shift) 번의 그림을 담는다.
            # "B 는 A 를 shift 만큼 밀어 놓은 파일" 을 만드는 데 쓴다 —
            # 오프셋 정렬이 맞으면 두 그림이 완전히 같아야 한다.
            frame = to_yuv(_rgb_frame((n - shift) % 4096, bits), pix_fmt)
            frame.pts = pts_list[n]
            frame.time_base = time_base
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def make_10bit() -> Path:
    """10bit CFR 30fps, 90 프레임."""
    path = TESTDATA / "test_10bit_30fps.mkv"
    tb = Fraction(1, 1000)
    pts = [round(n * 1000 / 30) for n in range(90)]
    make(path, 90, "yuv444p10le", 10, pts, tb, Fraction(30, 1))
    return path


def make_vfr() -> Path:
    """VFR — 간격이 33ms / 100ms / 16ms 로 계속 바뀐다, 90 프레임.

    fps 곱셈으로 프레임 번호를 내면 반드시 틀리는 구조다. 그게 이 파일의 목적이다.
    """
    path = TESTDATA / "test_vfr.mkv"
    tb = Fraction(1, 1000)
    gaps = [33, 100, 16, 16, 50, 200, 33, 16]
    pts, t = [], 0
    for n in range(90):
        pts.append(t)
        t += gaps[n % len(gaps)]
    make(path, 90, "yuv420p", 8, pts, tb, Fraction(30, 1))
    return path


PAIR_SHIFT = 7


def make_pair() -> list[Path]:
    """비교 모드용 한 쌍. B 는 A 를 PAIR_SHIFT 프레임 밀어 놓은 파일이다.

    B 의 n 번 프레임이 A 의 (n - PAIR_SHIFT) 번 그림을 담는다. 그래서
    **오프셋을 +PAIR_SHIFT 로 맞추면 두 그림이 완전히 같아야 하고**, 차분이
    정확히 0 이 나와야 한다. 오프셋 정렬(3-7)과 프레임 잠금(3-3)이 실제로
    맞는지 픽셀로 검산하는 근거다.

    무손실(ffv1)이라 인코딩 오차가 끼지 않는다 — 0 이 아니면 우리 잘못이다.
    """
    tb = Fraction(1, 1000)
    pts = [round(n * 1000 / 30) for n in range(120)]
    paths = []
    for name, shift in (("test_pair_a.mkv", 0), ("test_pair_b.mkv", PAIR_SHIFT)):
        path = TESTDATA / name
        make(path, 120, "yuv420p", 8, pts, tb, Fraction(30, 1), shift=shift)
        paths.append(path)
    return paths


def main() -> int:
    TESTDATA.mkdir(parents=True, exist_ok=True)
    for maker in (make_10bit, make_vfr):
        path = maker()
        print(f"  만듦: {path.name}  ({path.stat().st_size:,} 바이트)")
    for path in make_pair():
        print(f"  만듦: {path.name}  ({path.stat().st_size:,} 바이트)")
    print(f"       -> B 는 A 를 {PAIR_SHIFT} 프레임 밀어 놓은 파일. "
          f"비교 탭에서 오프셋 +{PAIR_SHIFT} 면 차분이 0 이어야 한다.")
    print("\n검증:  python tools/verify_index_capture.py testdata/*.mkv testdata/*.mp4")
    print("       python tools/smoke_compare.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

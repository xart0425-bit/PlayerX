"""파일 전체의 PTS 표 — "몇 번째 프레임인가"의 근거.

1단계까지 프레임 번호는 `time-pos x fps` 였다. CFR(고정 프레임레이트) 파일에서는
맞지만 두 가지가 걸린다.

  1) **VFR(가변 프레임레이트)** 파일은 프레임 간격이 일정하지 않다. 곱셈으로 낸
     번호는 실제 몇 번째 프레임인지와 어긋난다. 화면 녹화물·애니메이션 원본·
     휴대폰 영상이 흔히 VFR 이다.
  2) **전체 프레임 수도 추정치**였다 (duration x fps). 끝부분에서 한두 프레임 틀린다.

그래서 파일을 한 번 훑어 모든 프레임의 PTS 를 표로 만든다. **디코딩은 하지 않고
패킷만 demux 한다** — 픽셀을 만들지 않으니 1시간짜리도 보통 몇 초면 끝난다.

알아 둘 점 두 가지.

* **패킷은 DTS 순서로 나온다.** B 프레임이 있으면 PTS 가 뒤죽박죽으로 나오므로
  다 모은 뒤 정렬해야 표시 순서가 된다. 프레임 번호는 표시 순서 기준이다.
* **mpv 의 time-pos 는 파일 시작을 0 으로 재정렬한 값이다** (mpv 의
  `--rebase-start-time` 기본값이 yes). 그래서 여기서도 첫 프레임 PTS 를 빼서
  같은 기준으로 맞춘다. 안 그러면 시작 PTS 가 0 이 아닌 파일에서 통째로 어긋난다.

인덱스는 `<영상파일>.idx.json` 으로 옆에 저장해 두 번째 열기부터는 즉시 로드한다.
영상 폴더에 쓸 수 없으면(읽기 전용 공유 등) 사용자 캐시 폴더로 물러난다.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import statistics
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

INDEX_VERSION = 1
INDEX_SUFFIX = ".idx.json"

# mpv 의 time-pos(부동소수점)와 우리 표의 값을 비교할 때 허용할 오차.
# 프레임 간격에 대한 비율이라 fps 가 얼마든 같은 의미가 된다.
TIME_TOLERANCE = 1e-3

# seek 목표를 프레임 시작보다 이만큼(앞뒤 간격 중 짧은 쪽 기준) 앞에 둔다.
# 1단계에서 실측으로 얻은 값이다 — 0.25 는 맞고 0.75 는 이전 프레임에 걸린다.
# VFR 에서 앞 간격만 보면(긴 정지 프레임 뒤) 목표가 너무 앞으로 가므로
# 앞뒤 간격 중 **짧은 쪽**에 비례시킨다.
SEEK_LEAD_RATIO = 0.25

# 간격이 이 비율 안에서만 흔들리면 CFR 로 본다.
# mkv 의 기본 타임베이스가 1ms 라 60fps 는 16/17ms 로 번갈아 저장된다 —
# 이건 VFR 이 아니라 양자화다.
CFR_SPREAD_RATIO = 0.02


# ---------------------------------------------------------------- 색 정보 해석

# FFmpeg AVColorSpace -> ffmpeg scale 필터의 in_color_matrix 이름.
# 여기 없는 값(YCgCo 등)은 auto 로 두고 swscale 판단에 맡긴다.
_MATRIX_NAMES = {
    1: "bt709",
    4: "fcc",
    5: "bt601",       # BT.470BG
    6: "smpte170m",
    7: "smpte240m",
    9: "bt2020",      # BT.2020 NCL
    10: "bt2020",     # BT.2020 CL
}

_PRIMARIES_NAMES = {
    1: "bt709", 4: "bt470m", 5: "bt470bg", 6: "smpte170m",
    7: "smpte240m", 8: "film", 9: "bt2020", 11: "smptest428",
    12: "smpte431", 13: "smpte432",
}

_TRANSFER_NAMES = {
    1: "bt709", 4: "gamma22", 5: "gamma28", 6: "smpte170m", 7: "smpte240m",
    8: "linear", 9: "log100", 10: "log316", 11: "iec61966-2-4",
    13: "iec61966-2-1", 14: "bt2020-10", 15: "bt2020-12",
    16: "smpte2084", 18: "arib-std-b67",
}

# 이 픽셀 포맷들은 정의상 풀레인지다. 컨테이너가 range 를 안 적어 놨어도
# limited 로 읽으면 안 된다.
_FULL_RANGE_PREFIXES = ("yuvj", "rgb", "bgr", "gbr", "argb", "abgr", "rgba", "bgra", "pal8")


def resolve_color(
    pix_fmt: str,
    height: int,
    colorspace: int | None,
    color_range: int | None,
    primaries: int | None = None,
    transfer: int | None = None,
) -> dict:
    """컨테이너가 적어 둔 색 정보를 캡쳐가 쓸 수 있는 형태로 확정한다.

    "명시 고정"이 이 함수의 목적이다. 컨테이너가 비워 둔 항목(UNSPECIFIED)을
    라이브러리 기본값에 맡기면 도구/버전마다 다른 그림이 나온다. 그러면
    "무손실 캡쳐"라는 말이 성립하지 않는다. 그래서 여기서 관례대로 채우고,
    **무엇이 파일에 적혀 있었고 무엇을 우리가 채웠는지**를 같이 남긴다.

    관례:
      * 매트릭스 미기재 -> 세로 720 이상이면 BT.709, 아니면 BT.601 (HD/SD 관례)
      * 레인지 미기재  -> YUV 는 limited(tv), RGB/yuvj 계열은 full(pc)
    """
    fmt = (pix_fmt or "").lower()
    is_full_by_format = fmt.startswith(_FULL_RANGE_PREFIXES)

    matrix_declared = colorspace in _MATRIX_NAMES
    if matrix_declared:
        matrix = _MATRIX_NAMES[colorspace]
    elif colorspace == 0:                      # AVCOL_SPC_RGB — 변환할 매트릭스가 없다
        matrix = "bt709"
    else:
        matrix = "bt709" if height >= 720 else "bt601"

    # AVColorRange: 0 미지정 / 1 MPEG(limited) / 2 JPEG(full)
    range_declared = color_range in (1, 2)
    if range_declared:
        rng = "tv" if color_range == 1 else "pc"
    else:
        rng = "pc" if is_full_by_format else "tv"

    return {
        "matrix": matrix,
        "range": rng,
        "primaries": _PRIMARIES_NAMES.get(primaries, "unknown"),
        "transfer": _TRANSFER_NAMES.get(transfer, "unknown"),
        "matrix_declared": bool(matrix_declared),
        "range_declared": bool(range_declared),
    }


# -------------------------------------------------------------------- 인덱스

class IndexError_(RuntimeError):
    """인덱싱 실패. (내장 IndexError 와 헷갈리지 않게 이름 끝에 _)"""


@dataclass
class FrameIndex:
    """한 파일의 프레임 표. 프레임 번호는 전부 0-based, 표시 순서 기준이다."""

    times: list[float]                 # 프레임 n 의 표시 시각(초), 파일 시작이 0
    pts: list[int]                     # 프레임 n 의 원본 PTS (time_base 단위) — 캡쳐가 쓴다
    keyframes: list[int]               # 키프레임인 프레임 번호
    time_base: Fraction
    start_pts: int
    width: int
    height: int
    pix_fmt: str
    bit_depth: int
    codec: str
    color: dict
    avg_rate: Fraction | None
    source: dict
    cfr: bool = True
    from_cache: bool = False           # 파일에서 읽어 왔는가 (새로 만든 게 아니라)
    cache_path: Path | None = field(default=None, repr=False)

    # ------------------------------------------------------------ 기본 정보

    @property
    def count(self) -> int:
        return len(self.times)

    @property
    def duration(self) -> float:
        """마지막 프레임이 사라지는 시각(초). 즉 재생 길이."""
        if not self.times:
            return 0.0
        return self.times[-1] + self.median_gap

    @property
    def median_gap(self) -> float:
        """프레임 간격의 중앙값(초). 대표 간격."""
        if len(self.times) < 2:
            return 0.0
        gaps = [self.times[i + 1] - self.times[i] for i in range(len(self.times) - 1)]
        return statistics.median(gaps)

    @property
    def fps(self) -> float:
        """표시용 대표 프레임레이트.

        VFR 이면 '평균'이라는 뜻이지 그 값으로 이동하지 않는다 —
        이동은 언제나 times/pts 표를 따른다.
        """
        if self.avg_rate:
            return float(self.avg_rate)
        gap = self.median_gap
        return (1.0 / gap) if gap > 0 else 0.0

    # ------------------------------------------------------------ 핵심 변환

    def frame_at_time(self, seconds: float) -> int:
        """그 시각에 화면에 떠 있는 프레임 번호.

        프레임 n 은 [times[n], times[n+1]) 동안 보인다. 경계에서 부동소수점
        오차로 한 프레임 밀리지 않도록, 다음 프레임 시작에 거의 닿았으면
        (간격의 TIME_TOLERANCE 이내) 다음 프레임으로 본다.
        """
        if not self.times:
            return 0
        i = bisect.bisect_right(self.times, seconds) - 1
        if i < 0:
            return 0
        if i + 1 < len(self.times):
            gap = self.times[i + 1] - self.times[i]
            if gap > 0 and (self.times[i + 1] - seconds) <= gap * TIME_TOLERANCE:
                i += 1
        return min(i, len(self.times) - 1)

    def time_of_frame(self, frame: int) -> float:
        if not self.times:
            return 0.0
        return self.times[max(0, min(frame, len(self.times) - 1))]

    def pts_of_frame(self, frame: int) -> int:
        if not self.pts:
            return 0
        return self.pts[max(0, min(frame, len(self.pts) - 1))]

    def seek_target(self, frame: int) -> float:
        """그 프레임에 착지하기 위해 mpv 에 건넬 시각(초).

        프레임 시작을 그대로 겨냥하면 부동소수점 오차로 다음 프레임에 걸린다.
        살짝 앞을 겨냥하되, 앞뒤 간격 중 짧은 쪽에 비례시켜 이전 프레임까지
        넘어가지 않게 한다 (VFR 대응).
        """
        n = max(0, min(frame, len(self.times) - 1)) if self.times else 0
        if not self.times or n == 0:
            return 0.0

        gap_before = self.times[n] - self.times[n - 1]
        gaps = [gap_before]
        if n + 1 < len(self.times):
            gaps.append(self.times[n + 1] - self.times[n])
        lead = SEEK_LEAD_RATIO * min(g for g in gaps if g > 0) if any(g > 0 for g in gaps) else 0.0
        return max(0.0, self.times[n] - lead)

    def nearest_keyframe(self, frame: int) -> int:
        """frame 이하의 가장 가까운 키프레임 번호. (4단계 무손실 컷이 여기 기댄다)"""
        if not self.keyframes:
            return 0
        i = bisect.bisect_right(self.keyframes, frame) - 1
        return self.keyframes[max(0, i)]

    @property
    def keyframe_gaps(self) -> list[int]:
        """이웃한 키프레임 사이의 프레임 수."""
        k = self.keyframes
        return [k[i + 1] - k[i] for i in range(len(k) - 1)]

    def keyframe_summary(self) -> str:
        """키프레임 간격을 한 줄로. 무손실 컷이 얼마나 밀릴지의 척도다.

        간격이 60 이면 무손실 컷은 최대 59 프레임 앞에서 시작될 수 있다.
        이 숫자를 미리 보면 "무손실로 자를 만한 파일인가"를 판단할 수 있다.
        """
        if not self.keyframes:
            return "없음"
        gaps = self.keyframe_gaps
        if not gaps:
            return f"키프레임 1개 (첫 프레임뿐)"

        low, high = min(gaps), max(gaps)
        median = int(statistics.median(gaps))
        # 초는 표에서 직접 잰다. 프레임 수 x 대표 간격으로 내면 mkv 의 1ms
        # 양자화가 누적돼서 60프레임/60fps 가 1.02초로 나온다.
        k = self.keyframes
        spans = [self.times[k[i + 1]] - self.times[k[i]] for i in range(len(k) - 1)]
        seconds = statistics.median(spans)
        spread = f"{median}" if low == high else f"{median} (최소 {low} · 최대 {high})"
        return (f"{spread} 프레임 = {seconds:.2f}초 · 총 {len(self.keyframes):,}개"
                + ("  · 전부 키프레임(all-intra)" if high == 1 else ""))

    # ------------------------------------------------------------ 저장/로드

    def to_json_obj(self) -> dict:
        # PTS 를 차분으로 저장한다. 간격은 대부분 같은 작은 수라서
        # 2시간 60fps 짜리도 파일이 서너 배 작아진다.
        deltas: list[int] = []
        prev = 0
        for i, p in enumerate(self.pts):
            deltas.append(p if i == 0 else p - prev)
            prev = p
        return {
            "index_version": INDEX_VERSION,
            "source": self.source,
            "stream": {
                "codec": self.codec,
                "width": self.width,
                "height": self.height,
                "pix_fmt": self.pix_fmt,
                "bit_depth": self.bit_depth,
                "time_base": [self.time_base.numerator, self.time_base.denominator],
                "start_pts": self.start_pts,
                "avg_rate": ([self.avg_rate.numerator, self.avg_rate.denominator]
                             if self.avg_rate else None),
            },
            "color": self.color,
            "frames": {
                "count": len(self.pts),
                "cfr": self.cfr,
                "pts_delta": deltas,
                "keyframes": self.keyframes,
            },
        }

    @classmethod
    def from_json_obj(cls, obj: dict) -> "FrameIndex":
        if obj.get("index_version") != INDEX_VERSION:
            raise IndexError_(f"인덱스 형식이 다릅니다 (version={obj.get('index_version')})")

        st = obj["stream"]
        fr = obj["frames"]
        time_base = Fraction(st["time_base"][0], st["time_base"][1])
        start_pts = int(st["start_pts"])

        pts: list[int] = []
        run = 0
        for i, d in enumerate(fr["pts_delta"]):
            run = d if i == 0 else run + d
            pts.append(run)

        tb = float(time_base)
        times = [(p - start_pts) * tb for p in pts]
        rate = st.get("avg_rate")
        return cls(
            times=times,
            pts=pts,
            keyframes=list(fr.get("keyframes", [])),
            time_base=time_base,
            start_pts=start_pts,
            width=int(st["width"]),
            height=int(st["height"]),
            pix_fmt=st["pix_fmt"],
            bit_depth=int(st["bit_depth"]),
            codec=st.get("codec", "?"),
            color=obj["color"],
            avg_rate=Fraction(rate[0], rate[1]) if rate else None,
            source=obj["source"],
            cfr=bool(fr.get("cfr", True)),
            from_cache=True,
        )

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 같은 폴더에 임시 파일로 쓰고 갈아 끼운다 — 쓰다 죽어도 반쪽짜리
        # 인덱스가 남지 않는다.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json_obj(), separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        self.cache_path = path
        return path


# ------------------------------------------------------------ 캐시 파일 위치

def _source_stamp(video_path: Path) -> dict:
    st = video_path.stat()
    return {"name": video_path.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def sidecar_path(video_path: str | Path) -> Path:
    """영상 옆에 두는 인덱스 파일 경로 — `<영상파일>.idx.json`."""
    p = Path(video_path)
    return p.with_name(p.name + INDEX_SUFFIX)


def fallback_path(video_path: str | Path) -> Path:
    """영상 폴더에 쓸 수 없을 때 쓸 사용자 캐시 경로.

    읽기 전용 공유 폴더나 광학 매체에서 열었을 때를 위한 자리다.
    파일명이 겹치지 않게 전체 경로의 해시를 쓴다.
    """
    p = Path(video_path).resolve()
    digest = hashlib.sha1(str(p).lower().encode("utf-8")).hexdigest()[:16]
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home())
    return Path(base) / "PlayerX" / "index" / f"{p.stem}.{digest}{INDEX_SUFFIX}"


def load_index(video_path: str | Path) -> FrameIndex | None:
    """캐시된 인덱스를 읽는다. 없거나 원본이 바뀌었으면 None.

    실패는 전부 None 이다 — 캐시가 깨졌다고 파일을 못 열면 안 된다.
    """
    video = Path(video_path)
    try:
        stamp = _source_stamp(video)
    except OSError:
        return None

    for candidate in (sidecar_path(video), fallback_path(video)):
        try:
            if not candidate.is_file():
                continue
            obj = json.loads(candidate.read_text(encoding="utf-8"))
            index = FrameIndex.from_json_obj(obj)
        except (OSError, ValueError, KeyError, IndexError_):
            continue
        # 원본이 그 사이 바뀌었으면 버린다. 이름은 옮겨졌을 수 있으니 안 본다.
        if (index.source.get("size") != stamp["size"]
                or index.source.get("mtime_ns") != stamp["mtime_ns"]):
            continue
        index.cache_path = candidate
        return index
    return None


def save_index(index: FrameIndex, video_path: str | Path) -> Path | None:
    """인덱스를 영상 옆에 저장한다. 못 쓰면 사용자 캐시로, 그것도 안 되면 None."""
    for candidate in (sidecar_path(video_path), fallback_path(video_path)):
        try:
            return index.save(candidate)
        except OSError:
            continue
    return None


# --------------------------------------------------------------- 인덱스 만들기

def _probe_stream(video_path: Path) -> dict:
    """첫 프레임을 한 장 디코딩해서 해상도·픽셀포맷·색 정보를 확정한다.

    컨테이너 헤더만 믿지 않는 이유: 코덱에 따라 헤더의 pix_fmt 가 비어 있거나
    실제 디코딩 결과와 다르다. 캡쳐가 이 값들로 색 변환을 하기 때문에
    한 장 풀어 보는 값(몇 ms)이 아깝지 않다.
    """
    import av  # noqa: PLC0415  (무거운 import 를 앱 기동 경로에서 빼 둔다)

    with av.open(str(video_path)) as container:
        if not container.streams.video:
            raise IndexError_("영상 스트림이 없습니다.")
        stream = container.streams.video[0]
        cc = stream.codec_context

        info = {
            "codec": (cc.name if cc else "?") or "?",
            "width": int(cc.width or 0),
            "height": int(cc.height or 0),
            "pix_fmt": cc.pix_fmt or "",
            "colorspace": int(cc.colorspace) if cc.colorspace is not None else None,
            "color_range": int(cc.color_range) if cc.color_range is not None else None,
            "primaries": int(cc.color_primaries) if cc.color_primaries is not None else None,
            "transfer": int(cc.color_trc) if cc.color_trc is not None else None,
            "time_base": Fraction(stream.time_base) if stream.time_base else Fraction(1, 1000),
            "avg_rate": Fraction(stream.average_rate) if stream.average_rate else None,
        }

        try:
            frame = next(container.decode(stream))
        except Exception:
            # 한 장도 못 풀어도 인덱싱 자체는 계속한다 — 헤더 값으로 물러난다.
            frame = None
        if frame is not None:
            info["width"] = frame.width
            info["height"] = frame.height
            info["pix_fmt"] = frame.format.name
            info["bit_depth"] = max(c.bits for c in frame.format.components)
            if frame.colorspace is not None:
                info["colorspace"] = int(frame.colorspace)
            if frame.color_range is not None:
                info["color_range"] = int(frame.color_range)

    if "bit_depth" not in info:
        try:
            from av.video.format import VideoFormat  # noqa: PLC0415

            info["bit_depth"] = max(c.bits for c in VideoFormat(info["pix_fmt"]).components)
        except Exception:
            info["bit_depth"] = 8

    if info["width"] <= 0 or info["height"] <= 0:
        raise IndexError_("해상도를 읽지 못했습니다.")
    return info


def build_index(
    video_path: str | Path,
    progress: Callable[[float, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> FrameIndex:
    """파일을 훑어 프레임 표를 만든다.

    progress(비율 0~1, 지금까지 모은 프레임 수) 를 주기적으로 부른다. 비율은
    컨테이너가 길이를 안 알려 주면 파일 안 읽은 위치로 낸다. 둘 다 없으면 -1
    (진행률 모름) 을 넘긴다.

    cancelled() 가 True 를 돌려주면 즉시 멈추고 IndexError_ 를 낸다.
    """
    import av  # noqa: PLC0415

    video = Path(video_path)
    if not video.is_file():
        raise IndexError_(f"파일이 없습니다: {video}")

    stamp = _source_stamp(video)
    info = _probe_stream(video)
    time_base = info["time_base"]
    tb = float(time_base)

    entries: list[tuple[int, bool]] = []      # (pts, 키프레임인가)
    file_size = max(1, stamp["size"])

    with av.open(str(video)) as container:
        stream = container.streams.video[0]

        # 길이를 알면 시간으로, 모르면 파일 안 위치로 진행률을 낸다.
        total_seconds = 0.0
        if stream.duration:
            total_seconds = float(stream.duration) * tb
        elif container.duration:
            total_seconds = float(container.duration) / 1_000_000.0

        report_every = 500
        for packet in container.demux(stream):
            if packet.pts is None and packet.dts is None:
                continue                       # 스트림 끝의 flush 패킷
            pts = packet.pts if packet.pts is not None else packet.dts
            entries.append((int(pts), bool(packet.is_keyframe)))

            if len(entries) % report_every == 0:
                if cancelled is not None and cancelled():
                    raise IndexError_("사용자가 인덱싱을 취소했습니다.")
                if progress is not None:
                    if total_seconds > 0:
                        ratio = ((int(pts) - entries[0][0]) * tb) / total_seconds
                    elif packet.pos is not None and packet.pos > 0:
                        ratio = packet.pos / file_size
                    else:
                        ratio = -1.0
                    progress(min(1.0, max(0.0, ratio)) if ratio >= 0 else -1.0, len(entries))

    if not entries:
        raise IndexError_("프레임을 하나도 찾지 못했습니다.")

    # 표시 순서로 정렬하고 중복 PTS 는 버린다.
    # (B 프레임 때문에 demux 순서는 표시 순서가 아니다 — 모듈 설명 참고)
    entries.sort(key=lambda e: e[0])
    pts_list: list[int] = []
    keyframes: list[int] = []
    for p, is_key in entries:
        if pts_list and p == pts_list[-1]:
            continue
        if is_key:
            keyframes.append(len(pts_list))
        pts_list.append(p)

    start_pts = pts_list[0]
    times = [(p - start_pts) * tb for p in pts_list]

    if progress is not None:
        progress(1.0, len(pts_list))

    return FrameIndex(
        times=times,
        pts=pts_list,
        keyframes=keyframes,
        time_base=time_base,
        start_pts=start_pts,
        width=info["width"],
        height=info["height"],
        pix_fmt=info["pix_fmt"],
        bit_depth=int(info["bit_depth"]),
        codec=info["codec"],
        color=resolve_color(
            info["pix_fmt"], info["height"],
            info["colorspace"], info["color_range"],
            info["primaries"], info["transfer"],
        ),
        avg_rate=info["avg_rate"],
        source=stamp,
        cfr=_looks_cfr(pts_list),
        from_cache=False,
    )


def _looks_cfr(pts_list: list[int]) -> bool:
    """간격이 일정한가. 타임베이스 양자화는 VFR 로 치지 않는다.

    mkv 의 기본 타임베이스는 1ms 라서 60fps 도 16/17ms 로 번갈아 저장된다.
    그걸 VFR 이라고 부르면 거의 모든 mkv 가 VFR 이 된다.
    """
    if len(pts_list) < 3:
        return True
    deltas = [pts_list[i + 1] - pts_list[i] for i in range(len(pts_list) - 1)]
    median = statistics.median(deltas)
    if median <= 0:
        return False
    spread = max(deltas) - min(deltas)
    return spread <= max(1.0, median * CFR_SPREAD_RATIO)

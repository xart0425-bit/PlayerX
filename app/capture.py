"""무손실 PNG 캡쳐 — 화면이 아니라 원본 파일에서 다시 꺼낸다.

이 프로젝트의 이중 경로(dual path) 설계가 여기서 완성된다.
재생은 libmpv 가 하고, **캡쳐는 PyAV 가 원본 파일을 다시 디코딩한다.**

화면 픽셀을 긁지 않는 이유는 세 가지다.
  1) 창 크기에 맞춰 스케일된 그림이 나온다 (원본 해상도가 아니다)
  2) OSD·자막·커서가 섞인다
  3) GPU 가 이미 YUV->RGB 를 제 마음대로 해 버린 뒤다 (레인지/매트릭스 불명)

원본을 다시 읽으면 셋 다 없다. 대신 **색 변환을 우리가 명시해야 한다.**

색 변환에 대해
--------------
YUV 영상을 PNG(RGB)로 저장하려면 반드시 변환이 일어난다. 이때 필요한 정보 두 개:

* **매트릭스** — BT.709 냐 BT.601 이냐. 틀리면 색이 눈에 띄게 틀어진다.
* **레인지** — limited(16~235) 냐 full(0~255) 이냐. 틀리면 명암이 뜨거나 뭉갠다.

컨테이너가 이 둘을 비워 두는 일이 흔하다. 비워 둔 채 라이브러리에 맡기면
버전/도구마다 다른 그림이 나온다. 그래서 frame_index.resolve_color() 가
관례대로 확정해 두고, 여기서는 그 값을 **ffmpeg scale 필터에 명시로 박아 넣는다**
(in_range/out_range/in_color_matrix/out_color_matrix).

PyAV 의 VideoFrame.reformat() 을 안 쓰는 이유가 있다. 그쪽은 src 와 dst 의
레인지가 같으면 sws_setColorspaceDetails() 호출 자체를 건너뛴다 — 풀레인지
원본을 풀레인지로 뽑을 때 설정이 반영되지 않는다. 필터그래프는 그런 구멍이 없다.

비트 뎁스
---------
8bit 소스는 rgb24 -> 8bit PNG, 10/12bit 소스는 rgb48be -> **16bit PNG** 로 간다.
10bit 를 8bit PNG 로 저장하면 그 자리에서 정보가 날아간다. PNG 는 채널당
8 또는 16 비트만 있어서 10bit 를 담을 그릇은 16bit 뿐이다.

PNG 인코딩은 FFmpeg 의 png 인코더로 한다 (Pillow 는 16bit RGB PNG 를 못 쓴다).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .frame_index import FrameIndex

# 목표 PTS 를 찾다가 이만큼 넘게 디코딩하면 포기한다.
# 정상적인 GOP 는 아무리 길어도 수백 프레임이다.
MAX_DECODE_AHEAD = 5000

# 직전에 꺼낸 프레임보다 이만큼 이내로 앞이면 탐색하지 않고 이어서 디코딩한다.
# 한 프레임씩 넘겨 보는 게 검수의 기본 동작인데, 그때마다 키프레임으로 되돌아가
# GOP 를 통째로 다시 푸는 건 낭비다. 이 값보다 멀면 탐색이 더 싸다.
FORWARD_DECODE_MAX = 60

# 파일명에 못 쓰는 글자
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class CaptureError(RuntimeError):
    pass


@dataclass
class CaptureResult:
    path: Path
    frame: int
    width: int
    height: int
    bit_depth: int          # PNG 채널당 비트 (8 또는 16)
    source_bit_depth: int
    matrix: str
    range: str
    exact: bool             # 목표 PTS 의 프레임을 정확히 집었는가
    size_bytes: int

    def summary(self) -> str:
        depth = f"{self.bit_depth}bit PNG"
        if self.source_bit_depth > 8:
            depth += f" ({self.source_bit_depth}bit 소스)"
        note = "" if self.exact else "  ※ 목표 PTS 를 정확히 찾지 못해 가장 가까운 프레임"
        return (f"{self.path.name} · {self.width}x{self.height} · {depth} · "
                f"{self.matrix}/{self.range}{note}")


def default_shot_dir(project_root: Path | None = None) -> Path:
    """캡쳐 기본 저장 폴더.

    소스로 돌 때는 프로젝트의 `shots/`. **PyInstaller 로 묶였을 때는 exe 옆**이다 —
    `__file__` 은 번들 안(`_internal/app/`)을 가리키므로 그대로 쓰면 캡쳐가
    프로그램 내부에 쌓인다.
    """
    if project_root is not None:
        return Path(project_root) / "shots"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "shots"
    return Path(__file__).resolve().parent.parent / "shots"


def build_filename(video_path: str | Path, frame: int, out_dir: Path) -> Path:
    """`<영상이름>_<프레임번호 6자리>.png`. 이미 있으면 뒤에 _2, _3 을 붙인다.

    프레임 번호를 파일명에 넣는 건 검수 도구의 기본이다 — 나중에 이 PNG 만
    보고도 몇 번째 프레임인지 알아야 한다.
    """
    stem = _UNSAFE.sub("_", Path(video_path).stem)
    base = f"{stem}_{frame:06d}"
    candidate = out_dir / f"{base}.png"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{base}_{n}.png"
        n += 1
    return candidate


def png_bit_depth(source_bit_depth: int, force: int | None = None) -> int:
    """저장할 PNG 의 채널당 비트 수."""
    if force in (8, 16):
        return force
    return 16 if source_bit_depth > 8 else 8


class FrameCapturer:
    """한 파일에 대해 컨테이너를 열어 두고 여러 프레임을 꺼내는 객체.

    S 를 연타할 때마다 파일을 다시 여는 건 낭비라서 열어 둔 채 재사용한다.
    PyAV 컨테이너는 스레드 안전하지 않으니 **한 스레드에서만** 쓴다.
    """

    def __init__(self, video_path: str | Path, index: FrameIndex) -> None:
        import av  # noqa: PLC0415

        self.path = Path(video_path)
        self.index = index
        self._av = av
        self._container = av.open(str(self.path))
        if not self._container.streams.video:
            self._container.close()
            raise CaptureError("영상 스트림이 없습니다.")
        self._stream = self._container.streams.video[0]
        # 디코딩만 하면 되니 스레드를 붙여 준다 (4K 에서 체감된다).
        self._stream.thread_type = "AUTO"
        self._graphs: dict[tuple, object] = {}

        # 이어서 디코딩하기 위한 상태. _cursor 는 '직전에 돌려준 프레임 번호',
        # _frames 는 그 다음 프레임부터 이어지는 디코딩 이터레이터다.
        self._cursor: int | None = None
        self._frames = None

    # ------------------------------------------------------------------ 수명

    def close(self) -> None:
        self._graphs.clear()
        self._frames = None
        try:
            self._container.close()
        except Exception:
            pass

    def __enter__(self) -> "FrameCapturer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 프레임 꺼내기

    def decode_frame(self, frame_no: int):
        """그 프레임의 원본 픽셀(YUV 그대로)을 담은 VideoFrame 을 돌려준다.

        기본 경로는 '가장 가까운 앞쪽 키프레임으로 seek → 목표 PTS 까지 전진
        디코딩'이다. 디코더는 표시 순서로 프레임을 내보내므로 PTS 는 증가한다.

        직전에 꺼낸 프레임보다 조금 앞이면(FORWARD_DECODE_MAX 이내) 탐색을
        건너뛰고 **이어서 디코딩한다**. 한 프레임씩 넘겨 보는 검수 동작에서
        매번 GOP 를 다시 푸는 걸 막는다. 결과는 어느 경로든 같은 프레임이다.
        """
        target = self.index.pts_of_frame(frame_no)

        if (self._frames is not None and self._cursor is not None
                and 0 < frame_no - self._cursor <= FORWARD_DECODE_MAX):
            found, exact = self._continue_to(target)
            if found is not None:
                self._cursor = frame_no
                return found, exact
            # 이어 읽기가 어긋났다 — 탐색 경로로 되돌아간다.

        found, exact = self._decode_at(target)
        if found is None:
            # 키프레임 탐색이 어긋났을 수 있다. 파일 맨 앞부터 다시 훑는다.
            found, exact = self._decode_at(target, from_start=True)
        if found is None:
            self._cursor = None
            raise CaptureError(f"{frame_no} 번 프레임(PTS {target})을 디코딩하지 못했습니다.")
        self._cursor = frame_no
        return found, exact

    def _decode_at(self, target_pts: int, from_start: bool = False):
        seek_to = self.index.start_pts if from_start else target_pts
        try:
            self._container.seek(int(seek_to), stream=self._stream,
                                 backward=True, any_frame=False)
        except Exception as exc:
            self._frames = None
            self._cursor = None
            raise CaptureError(f"탐색에 실패했습니다: {exc}") from exc

        self._frames = self._container.decode(self._stream)
        return self._scan(target_pts)

    def _continue_to(self, target_pts: int):
        """탐색 없이 지금 이터레이터에서 계속 읽는다. 지나쳤으면 (None, False)."""
        try:
            return self._scan(target_pts)
        except Exception:
            self._frames = None
            self._cursor = None
            return None, False

    def _scan(self, target_pts: int):
        """현재 이터레이터에서 target_pts 를 찾는다.

        찾으면 (프레임, True). 지나쳐 버렸으면 (직전 프레임, False) —
        VFR 이나 PTS 가 어긋난 파일에서 목표에 딱 맞는 프레임이 없을 때다.
        """
        best = None
        seen = 0
        for frame in self._frames:
            if frame.pts is None:
                continue
            seen += 1
            if frame.pts == target_pts:
                return frame, True
            if frame.pts < target_pts:
                best = frame
            else:
                # 목표를 지나쳤다. 이터레이터는 이미 한 프레임 더 소비했으므로
                # 이어 읽기 상태를 버린다 — 다음 호출은 탐색부터 다시 한다.
                self._frames = None
                self._cursor = None
                break
            if seen > MAX_DECODE_AHEAD:
                self._frames = None
                self._cursor = None
                break
        return best, False

    # ---------------------------------------------------------------- 저장

    def save_png(
        self,
        frame_no: int,
        out_path: str | Path,
        force_bit_depth: int | None = None,
    ) -> CaptureResult:
        """그 프레임을 PNG 로 저장한다. 스케일 없음, OSD 없음, 원본 해상도."""
        frame, exact = self.decode_frame(frame_no)

        depth = png_bit_depth(self.index.bit_depth, force_bit_depth)
        pix_fmt = "rgb48be" if depth == 16 else "rgb24"
        rgb = self.to_rgb(frame, pix_fmt)

        data = self._encode_png(rgb, pix_fmt)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)

        return CaptureResult(
            path=out,
            frame=frame_no,
            width=rgb.width,
            height=rgb.height,
            bit_depth=depth,
            source_bit_depth=self.index.bit_depth,
            matrix=self.index.color["matrix"],
            range=self.index.color["range"],
            exact=exact,
            size_bytes=len(data),
        )

    # ------------------------------------------------------------ 색 변환

    def to_rgb(self, frame, pix_fmt: str, width: int | None = None, height: int | None = None):
        """YUV -> RGB. 매트릭스와 레인지를 명시로 고정한다.

        width/height 를 주지 않으면 크기를 건드리지 않는다. 주는 경우는 비교
        모드에서 해상도가 다른 두 영상을 차분할 때뿐이다 (그때는 리샘플링
        오차가 차분에 섞이므로 UI 가 그렇다고 알린다).

        flags 의 full_chroma_int + accurate_rnd 는 4:2:0 크로마를 올려 붙일 때
        반올림을 제대로 하라는 뜻이다 — 검수용이라 품질 쪽으로 붙인다.
        """
        out_w = width or frame.width
        out_h = height or frame.height
        if frame.format.name == pix_fmt and out_w == frame.width and out_h == frame.height:
            return frame

        key = (frame.format.name, frame.width, frame.height, pix_fmt, out_w, out_h)
        graph = self._graphs.get(key)
        if graph is None:
            graph = self._build_graph(frame, pix_fmt, out_w, out_h)
            self._graphs[key] = graph

        graph.push(frame)
        return graph.pull()

    def _build_graph(self, frame, pix_fmt: str, out_w: int, out_h: int):
        color = self.index.color
        scale_args = (
            f"w={out_w}:h={out_h}"
            f":in_range={color['range']}:out_range=full"
            f":in_color_matrix={color['matrix']}:out_color_matrix={color['matrix']}"
            ":flags=full_chroma_int+accurate_rnd"
        )
        graph = self._av.filter.Graph()
        src = graph.add(
            "buffer",
            width=str(frame.width),
            height=str(frame.height),
            pix_fmt=frame.format.name,
            time_base=str(self.index.time_base),
        )
        scale = graph.add("scale", scale_args)
        fmt = graph.add("format", pix_fmts=pix_fmt)
        sink = graph.add("buffersink")
        src.link_to(scale)
        scale.link_to(fmt)
        fmt.link_to(sink)
        graph.configure()
        return graph

    # ------------------------------------------------------------ PNG 인코딩

    def _encode_png(self, rgb_frame, pix_fmt: str) -> bytes:
        """FFmpeg png 인코더로 한 장 인코딩해서 바이트를 돌려준다.

        PNG 는 정의상 무손실이라 압축률만 신경 쓰면 된다. 인코더가 내놓는
        패킷 자체가 완결된 PNG 파일이라 그대로 쓰면 된다.
        """
        codec = self._av.CodecContext.create("png", "w")
        codec.pix_fmt = pix_fmt
        codec.width = rgb_frame.width
        codec.height = rgb_frame.height
        try:
            packets = list(codec.encode(rgb_frame)) + list(codec.encode(None))
        except Exception as exc:
            raise CaptureError(f"PNG 인코딩에 실패했습니다: {exc}") from exc
        if not packets:
            raise CaptureError("PNG 인코더가 아무것도 내놓지 않았습니다.")
        return b"".join(bytes(p) for p in packets)


def capture_frame(
    video_path: str | Path,
    index: FrameIndex,
    frame_no: int,
    out_path: str | Path,
    force_bit_depth: int | None = None,
) -> CaptureResult:
    """한 장만 뽑고 끝낼 때 쓰는 편의 함수 (검증 도구용)."""
    with FrameCapturer(video_path, index) as cap:
        return cap.save_png(frame_no, out_path, force_bit_depth)


def capture_sequence(
    video_path: str | Path,
    index: FrameIndex,
    first_frame: int,
    last_frame: int,
    out_dir: str | Path,
    force_bit_depth: int | None = None,
    on_progress=None,
    cancelled=None,
) -> list[Path]:
    """구간을 통째로 PNG 시퀀스로 뽑는다 (양끝 포함).

    한 장씩 `capture_frame` 을 부르지 않는 이유: 그러면 매번 컨테이너를 열고
    키프레임으로 되돌아간다. 하나의 `FrameCapturer` 로 **순서대로** 부르면
    이어서 디코딩하는 빠른 경로가 살아 있어서 수십 배 빠르다.

    파일명은 `<영상이름>_<프레임번호 6자리>.png` — 낱장 캡쳐와 같은 규칙이라
    나중에 섞여도 어느 프레임인지 알 수 있다. 번호가 원본 프레임 번호라
    0 부터 다시 세지 않는다는 점이 중요하다.
    """
    first = max(0, min(first_frame, index.count - 1))
    last = max(first, min(last_frame, index.count - 1))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    stem = _UNSAFE.sub("_", Path(video_path).stem)
    total = last - first + 1
    written: list[Path] = []

    with FrameCapturer(video_path, index) as cap:
        for i, frame_no in enumerate(range(first, last + 1)):
            if cancelled is not None and cancelled():
                raise CaptureError("사용자가 시퀀스 내보내기를 취소했습니다.")
            target = out / f"{stem}_{frame_no:06d}.png"
            cap.save_png(frame_no, target, force_bit_depth)
            written.append(target)
            if on_progress is not None:
                on_progress((i + 1) / total, i + 1, total)
    return written

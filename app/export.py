"""클립 내보내기 — ffmpeg 명령을 짜고 결과를 예고한다.

두 가지 방식이 있고, **둘의 차이를 숨기지 않는 게 이 모듈의 요점이다.**

무손실 컷 (`-c copy`)
--------------------
다시 인코딩하지 않고 컨테이너만 새로 묶는다. 화질이 원본 그대로고 몇 초면 끝난다.
대신 **시작점이 키프레임으로 밀린다.** 디코딩을 안 하니 키프레임이 아닌 프레임부터
시작할 방법이 없기 때문이다.

여기가 2단계 PTS 인덱스가 다시 쓰이는 자리다. 인덱스에 키프레임 목록을 같이
저장해 뒀으므로(`FrameIndex.nearest_keyframe`), **자르기 전에 "실제로는 몇 번
프레임부터 시작될지"를 정확히 계산해서 UI 에 보여 줄 수 있다.** 잘라 보고 나서
어긋난 걸 발견하는 것과, 자르기 전에 알고 누르는 것은 전혀 다르다.

정확 컷 (재인코딩)
-----------------
IN 프레임부터 정확히 시작한다. 대신 다시 인코딩하므로 시간이 걸리고, 무손실
인코더가 아니면 화질이 조금 떨어진다.

**어느 인코더를 쓸 수 있는지는 ffmpeg 빌드가 정한다.** 계획서 판단대로 LGPL
빌드를 기본으로 받는데 거기엔 x264 가 없다. 그래서 있는 것 중에서 고르되,
기본값은 `ffv1` — 무손실이라 "정확하게 자르되 화질은 그대로"가 된다. 검수 도구가
원하는 건 대개 그쪽이고, 클립은 보통 짧으니 파일 크기도 견딜 만하다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .clips import Clip
from .ffmpeg_loader import available_encoders

MODE_LOSSLESS = "lossless"
MODE_PRECISE = "precise"

# ffv1 은 mp4/mov 에 못 들어간다. 그런 인코더는 컨테이너를 mkv 로 강제한다.
MKV_ONLY_ENCODERS = {"ffv1", "utvideo", "ffvhuff"}

# 어느 컨테이너가 H.264 를 담을 수 있는가 (정확 컷에서 확장자를 정할 때).
H264_CONTAINERS = {".mkv", ".mp4", ".mov", ".m4v", ".ts", ".m2ts", ".mts"}


@dataclass(frozen=True)
class EncoderChoice:
    """정확 컷에 쓸 수 있는 인코더 하나."""

    name: str                 # ffmpeg 인코더 이름
    label: str                # UI 에 보일 말
    lossless: bool
    args: tuple[str, ...] = ()
    needs_bitrate: bool = False


# 선호 순서. 앞에 있을수록 먼저 고른다.
# ffv1 이 맨 앞인 이유: "정확 컷"의 목적은 IN 을 정확히 맞추는 것이지 화질을
# 깎는 게 아니다. 화질까지 지키는 선택지가 있으면 그게 기본이어야 한다.
ENCODER_TABLE: tuple[EncoderChoice, ...] = (
    EncoderChoice(
        "ffv1", "무손실 (ffv1 · 화질 그대로, 파일이 크다)", lossless=True,
        # level 3 + slicecrc: 표준 보존용 설정. g=1 은 모든 프레임을 키프레임으로
        # 만든다 — 잘라 낸 클립을 또 자를 때 편하다.
        args=("-level", "3", "-coder", "1", "-context", "1",
              "-g", "1", "-slices", "4", "-slicecrc", "1"),
    ),
    EncoderChoice(
        "libx264", "H.264 (x264 · GPL 빌드에서만 보인다)", lossless=False,
        args=("-crf", "18", "-preset", "veryfast"),
    ),
    EncoderChoice(
        "libopenh264", "H.264 (openh264 · 소스 비트레이트 기준)", lossless=False,
        needs_bitrate=True,
    ),
    EncoderChoice("h264_nvenc", "H.264 (NVIDIA 하드웨어)", lossless=False, needs_bitrate=True),
    EncoderChoice("h264_qsv", "H.264 (Intel 하드웨어)", lossless=False, needs_bitrate=True),
    EncoderChoice("h264_amf", "H.264 (AMD 하드웨어)", lossless=False, needs_bitrate=True),
)


def usable_encoders(exe: Path) -> list[EncoderChoice]:
    """이 ffmpeg 빌드에 실제로 들어 있는 인코더만 선호 순서대로."""
    have = available_encoders(exe)
    return [choice for choice in ENCODER_TABLE if choice.name in have]


class ExportError(RuntimeError):
    pass


@dataclass
class ExportPlan:
    """자르기 전에 "이렇게 나올 것이다"를 미리 계산한 것.

    UI 는 이걸 그대로 문장으로 보여 준다. 버튼을 누르기 전에 결과를 알 수 있어야
    무손실/정확 중 무엇을 고를지 판단할 수 있다.
    """

    mode: str
    clip: Clip
    source: Path
    out_path: Path
    start_frame: int          # 실제로 출력이 시작될 프레임
    end_frame: int            # 실제로 출력이 끝날 프레임 (포함)
    start_time: float         # ffmpeg 에 넘길 -ss 값(초)
    duration: float
    encoder: EncoderChoice | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame + 1

    @property
    def shifted(self) -> int:
        """요청한 IN 보다 몇 프레임 앞에서 시작하게 되는가 (무손실 컷의 대가)."""
        return self.clip.in_frame - self.start_frame

    def summary(self) -> str:
        if self.mode == MODE_LOSSLESS:
            if self.shifted == 0:
                head = (f"무손실 · {self.start_frame} ~ {self.end_frame} "
                        f"({self.frames} 프레임) — IN 이 마침 키프레임이라 정확합니다")
            else:
                head = (f"무손실 · 실제로는 {self.start_frame} 번부터 시작합니다 "
                        f"(요청 {self.clip.in_frame} 보다 {self.shifted} 프레임 앞, "
                        f"가장 가까운 키프레임) · {self.frames} 프레임")
        else:
            head = (f"정확 · {self.start_frame} ~ {self.end_frame} "
                    f"({self.frames} 프레임) · 재인코딩 "
                    f"{self.encoder.label if self.encoder else '?'}")
        return "  ".join([head, *self.notes])


def default_out_path(source: str | Path, clip: Clip, mode: str,
                     encoder: EncoderChoice | None) -> Path:
    """기본 저장 경로 — 원본 옆에 `<이름>_<IN>-<OUT>_<방식>.<확장자>`."""
    src = Path(source)
    suffix = _output_suffix(src, mode, encoder)
    tag = "lossless" if mode == MODE_LOSSLESS else "exact"
    return src.with_name(f"{src.stem}_{clip.in_frame:06d}-{clip.out_frame:06d}_{tag}{suffix}")


def _output_suffix(source: Path, mode: str, encoder: EncoderChoice | None) -> str:
    """출력 컨테이너 확장자.

    무손실 컷은 원본 확장자를 그대로 쓴다 (같은 스트림을 담으니까).
    정확 컷은 인코더가 담길 수 있는 컨테이너를 골라야 한다 — ffv1 은 mp4 에 못 들어간다.
    """
    if mode == MODE_LOSSLESS:
        return source.suffix or ".mkv"
    if encoder is None:
        return ".mkv"
    if encoder.name in MKV_ONLY_ENCODERS:
        return ".mkv"
    return source.suffix if source.suffix.lower() in H264_CONTAINERS else ".mkv"


def plan_export(source: str | Path, clip: Clip, index, mode: str,
                encoder: EncoderChoice | None = None,
                out_path: str | Path | None = None) -> ExportPlan:
    """자르기 전 계산. 실제로 어느 프레임부터 어디까지 나올지 정한다."""
    src = Path(source)
    if index is None or index.count <= 0:
        raise ExportError("프레임 인덱스가 없어 구간을 계산할 수 없습니다.")

    last = index.count - 1
    in_frame = max(0, min(clip.in_frame, last))
    out_frame = max(in_frame, min(clip.out_frame, last))
    notes: list[str] = []

    if mode == MODE_LOSSLESS:
        # 디코딩을 안 하므로 키프레임에서만 시작할 수 있다.
        start_frame = index.nearest_keyframe(in_frame)
        # ffmpeg 은 -ss 시각 '이전'의 키프레임을 찾는다. 그 키프레임의 표시 시각을
        # 그대로 주면 부동소수점 때문에 한 칸 앞 키프레임으로 갈 수 있어서,
        # 그 프레임이 떠 있는 구간 안쪽(다음 프레임 직전)을 겨냥한다.
        start_time = _inside_frame(index, start_frame)
    else:
        start_frame = in_frame
        # 정확 컷은 디코딩하므로 인덱스가 쓰는 것과 같은 겨냥법을 쓴다.
        start_time = index.seek_target(in_frame)
        if encoder is None:
            raise ExportError("정확 컷에 쓸 인코더가 지정되지 않았습니다.")
        if not encoder.lossless:
            notes.append("※ 다시 인코딩하므로 화질이 원본과 완전히 같지는 않습니다.")

    end_time = (index.time_of_frame(out_frame + 1) if out_frame + 1 <= last
                else index.duration)
    duration = max(0.0, end_time - start_time)

    return ExportPlan(
        mode=mode,
        clip=Clip(in_frame, out_frame, clip.name),
        source=src,
        out_path=Path(out_path) if out_path else default_out_path(src, Clip(in_frame, out_frame), mode, encoder),
        start_frame=start_frame,
        end_frame=out_frame,
        start_time=start_time,
        duration=duration,
        encoder=encoder,
        notes=notes,
    )


def _inside_frame(index, frame: int) -> float:
    """그 프레임이 화면에 떠 있는 구간의 안쪽 시각.

    프레임 시작을 그대로 쓰면 경계라서 앞뒤 어느 쪽으로도 해석될 수 있다.
    다음 프레임까지의 간격 4분의 1만큼 들어간 지점이면 어느 쪽으로도 안 샌다.
    """
    t = index.time_of_frame(frame)
    if frame + 1 < index.count:
        return t + (index.time_of_frame(frame + 1) - t) * 0.25
    return t


def estimate_bitrate(source: str | Path, index) -> int | None:
    """원본의 대략적인 비트레이트(bps).

    CRF 가 없는 인코더(openh264·하드웨어)에 넘길 목표값이다. 파일 크기 ÷ 길이라
    소리와 컨테이너 오버헤드까지 들어간 값이지만, 영상 비트레이트를 조금 넉넉하게
    잡는 셈이라 이 용도에는 오히려 안전한 쪽으로 틀린다.
    """
    try:
        size = Path(source).stat().st_size
    except OSError:
        return None
    duration = getattr(index, "duration", 0.0) or 0.0
    if duration <= 0:
        return None
    return int(size * 8 / duration)


def build_command(exe: Path, plan: ExportPlan, source_bitrate: int | None = None) -> list[str]:
    """ffmpeg 명령줄을 만든다.

    `-ss` 를 `-i` **앞에** 둔다 (입력 탐색). 뒤에 두면 파일 처음부터 훑어서
    긴 파일에서 몇 분씩 걸린다.

    그 대신 `-ss` 가 앞에 오면 `-t` 는 **자른 뒤의 시간축** 기준이 된다는 걸
    잊으면 안 된다. 그래서 `-to`(절대 시각)가 아니라 `-t`(길이)를 쓴다.
    """
    args: list[str] = [
        str(exe), "-hide_banner", "-nostdin", "-y",
        "-ss", f"{plan.start_time:.6f}",
        "-i", str(plan.source),
    ]

    if plan.mode == MODE_LOSSLESS:
        args += [
            "-t", f"{plan.duration:.6f}",
            "-map", "0",            # 영상·소리·자막 전부 그대로
            "-c", "copy",
            # 중간부터 잘라 내면 타임스탬프가 음수로 시작할 수 있다.
            # 0 으로 당겨 놔야 플레이어들이 제대로 연다.
            "-avoid_negative_ts", "make_zero",
        ]
    else:
        encoder = plan.encoder
        args += [
            "-frames:v", str(plan.frames),   # 정확히 이 장수만
            "-map", "0:v:0",
            "-map", "0:a?",                  # 소리는 있으면 가져온다
            "-c:v", encoder.name,
            *encoder.args,
        ]
        if encoder.needs_bitrate:
            # CRF 가 없는 인코더용. 원본 비트레이트를 알면 그걸, 모르면 넉넉하게.
            bitrate = source_bitrate or 12_000_000
            args += ["-b:v", str(int(bitrate))]
        args += ["-c:a", "copy"]

    # 진행률을 stdout 으로 기계가 읽을 수 있게 뽑는다.
    args += ["-progress", "pipe:1", "-nostats", str(plan.out_path)]
    return args


# ---------------------------------------------------------------- 진행률 파싱

_PROGRESS_LINE = re.compile(r"^(\w+)=(.*)$")


def parse_progress(line: str) -> tuple[str, str] | None:
    """`-progress pipe:1` 이 뱉는 `키=값` 한 줄. 아니면 None."""
    m = _PROGRESS_LINE.match(line.strip())
    return (m.group(1), m.group(2)) if m else None


def run_export(exe: Path, plan: ExportPlan, source_bitrate: int | None = None,
               on_progress=None, cancelled=None) -> Path:
    """ffmpeg 을 돌려 클립을 만든다. 실패하면 ExportError.

    on_progress(비율 0~1, 지금까지 프레임 수) 를 주기적으로 부른다.
    cancelled() 가 True 면 프로세스를 끝내고 만들다 만 파일을 지운다.
    """
    command = build_command(exe, plan, source_bitrate)
    tail: list[str] = []                 # 실패했을 때 보여 줄 마지막 로그
    frames_done = 0

    from .ffmpeg_loader import _NO_WINDOW  # noqa: PLC0415

    try:
        process = subprocess.Popen(  # noqa: S603
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise ExportError(f"ffmpeg 을 실행하지 못했습니다: {exc}") from exc

    try:
        for line in process.stdout:
            parsed = parse_progress(line)
            if parsed is None:
                continue
            key, value = parsed
            if key == "frame":
                try:
                    frames_done = int(value)
                except ValueError:
                    continue
                if on_progress is not None and plan.frames > 0:
                    on_progress(min(1.0, frames_done / plan.frames), frames_done)
            elif key == "out_time_us" and plan.duration > 0 and on_progress is not None:
                try:
                    ratio = int(value) / 1e6 / plan.duration
                except ValueError:
                    continue
                on_progress(min(1.0, max(0.0, ratio)), frames_done)
            if cancelled is not None and cancelled():
                process.kill()
                process.wait(timeout=5)
                _remove_partial(plan.out_path)
                raise ExportError("사용자가 내보내기를 취소했습니다.")
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            tail = process.stderr.read().splitlines()[-12:]
            process.stderr.close()
        code = process.wait()

    if code != 0:
        _remove_partial(plan.out_path)
        detail = "\n".join(tail) or f"종료 코드 {code}"
        raise ExportError(f"ffmpeg 이 실패했습니다.\n{detail}")
    if not plan.out_path.is_file() or plan.out_path.stat().st_size == 0:
        raise ExportError("ffmpeg 이 끝났는데 결과 파일이 없습니다.")

    if on_progress is not None:
        on_progress(1.0, frames_done)
    return plan.out_path


def _remove_partial(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass

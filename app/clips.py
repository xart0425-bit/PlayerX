"""클립 목록 — IN/OUT 구간을 담고, 파일로 오가게 한다.

프레임 번호로만 다룬다. 시각(초)이 아니라 프레임 번호가 기준인 이유는
2단계에서 만든 PTS 인덱스가 있기 때문이다 — 프레임 번호는 파일에 대해 절대적이고,
시각으로 적어 두면 VFR 파일에서 다시 열 때 다른 프레임을 가리킬 수 있다.

**IN 과 OUT 은 둘 다 포함이다.** IN=10, OUT=12 면 10·11·12 세 프레임이다.
검수 도구에서 "이 프레임부터 이 프레임까지"라고 말할 때의 자연스러운 뜻이고,
길이가 `out - in + 1` 로 떨어진다.

목록은 `<영상파일>.clips.json` 으로 영상 옆에 저장한다 (인덱스 캐시와 같은 자리).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CLIPS_VERSION = 1
CLIPS_SUFFIX = ".clips.json"


@dataclass
class Clip:
    """IN~OUT 한 구간. 양끝 포함."""

    in_frame: int
    out_frame: int
    name: str = ""

    def __post_init__(self) -> None:
        self.in_frame = max(0, int(self.in_frame))
        self.out_frame = max(self.in_frame, int(self.out_frame))

    @property
    def length(self) -> int:
        """프레임 수. 양끝 포함이라 +1."""
        return self.out_frame - self.in_frame + 1

    def clamped(self, frame_count: int) -> "Clip":
        """파일 끝을 넘지 않게 자른다 (다른 파일에서 만든 목록을 불러올 때)."""
        last = max(0, frame_count - 1)
        return Clip(min(self.in_frame, last), min(self.out_frame, last), self.name)

    def to_json_obj(self) -> dict:
        obj = {"in": self.in_frame, "out": self.out_frame}
        if self.name:
            obj["name"] = self.name
        return obj

    @classmethod
    def from_json_obj(cls, obj: dict) -> "Clip":
        return cls(int(obj["in"]), int(obj["out"]), str(obj.get("name", "")))


class ClipsError(RuntimeError):
    pass


@dataclass
class LoadedClips:
    clips: list[Clip]
    warning: str = ""
    source_name: str = ""
    fields: dict = field(default_factory=dict)


def clips_path(video_path: str | Path) -> Path:
    """영상 옆에 두는 클립 목록 경로 — `<영상파일>.clips.json`."""
    p = Path(video_path)
    return p.with_name(p.name + CLIPS_SUFFIX)


def save_clips(path: str | Path, clips: list[Clip], video_path: str | Path,
               index=None) -> Path:
    """클립 목록을 JSON 으로 쓴다.

    fps 와 전체 프레임 수도 같이 적는다. 쓰이지는 않지만 사람이 파일을 열어 봤을 때
    "이게 어느 영상의 몇 프레임짜리 목록인지" 알 수 있어야 한다.
    """
    video = Path(video_path)
    payload = {
        "clips_version": CLIPS_VERSION,
        "source": {"name": video.name},
        "clips": [c.to_json_obj() for c in clips],
    }
    try:
        stat = video.stat()
        payload["source"]["size"] = stat.st_size
        payload["source"]["mtime_ns"] = stat.st_mtime_ns
    except OSError:
        pass
    if index is not None:
        payload["source"]["fps"] = round(index.fps, 6)
        payload["source"]["frame_count"] = index.count

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out


def load_clips(path: str | Path, video_path: str | Path | None = None,
               frame_count: int = 0) -> LoadedClips:
    """클립 목록을 읽는다.

    **다른 영상의 목록이어도 거절하지 않는다.** 두 인코딩본에 같은 구간을 쓰는 건
    이 도구의 흔한 용도라서, 막는 대신 경고만 남기고 프레임 수에 맞춰 잘라 준다.
    """
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClipsError(f"클립 목록을 읽지 못했습니다: {exc}") from exc

    if obj.get("clips_version") != CLIPS_VERSION:
        raise ClipsError(f"클립 목록 형식이 다릅니다 (version={obj.get('clips_version')})")

    try:
        clips = [Clip.from_json_obj(c) for c in obj["clips"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ClipsError(f"클립 항목이 깨졌습니다: {exc}") from exc

    source = obj.get("source", {}) or {}
    warning = ""
    if video_path is not None and source.get("name") and Path(video_path).name != source["name"]:
        warning = (f"이 목록은 '{source['name']}' 의 것입니다. "
                   f"프레임 번호가 지금 영상과 맞는지 확인하세요.")

    if frame_count > 0:
        trimmed = [c.clamped(frame_count) for c in clips]
        if any(a.out_frame != b.out_frame or a.in_frame != b.in_frame
               for a, b in zip(clips, trimmed)):
            warning = (warning + " " if warning else "") + \
                      f"파일 끝({frame_count - 1})을 넘는 구간을 잘랐습니다."
        clips = trimmed

    return LoadedClips(clips=clips, warning=warning,
                       source_name=source.get("name", ""), fields=source)

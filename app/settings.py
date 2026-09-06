"""설정 저장 — 다시 켰을 때 같은 상태로 돌아오게 한다.

사용자가 한 명이라 DB 도 레지스트리도 필요 없다. JSON 한 장이면 된다.
`%LOCALAPPDATA%\\PlayerX\\settings.json` 에 둔다 — 프로젝트 폴더가 아니라
사용자 폴더인 이유는, 5단계에서 PyInstaller 로 묶으면 프로그램 폴더가
읽기 전용일 수 있기 때문이다.

**무엇을 저장하고 무엇을 저장하지 않는가**

저장하는 것: 매번 다시 고르기 귀찮은 것들 — 디코더, 캡쳐 폴더/비트깊이,
비교 모드와 경계 위치, 정확 컷 인코더, 창 크기.

저장하지 않는 것: 열었던 파일, 클립 목록, 재생 위치. 파일에 딸린 정보는
파일 옆에 둔다(`.idx.json`, `.clips.json`) — 그래야 영상을 다른 데로 옮겨도
따라가고, 설정 파일이 특정 영상에 묶이지 않는다.

**깨진 설정 때문에 앱이 안 켜지면 안 된다.** 읽기는 무슨 일이 있어도 기본값으로
물러난다. 값 하나가 이상해도 나머지는 살린다.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SETTINGS_VERSION = 1

# (mpv 값, 화면에 보일 말)
HWDEC_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto-safe", "자동 (안전) — 검증된 디코더만"),
    ("auto", "자동 (전부) — 되는 건 다 시도"),
    ("d3d11va", "d3d11va — Direct3D 11"),
    ("nvdec", "nvdec — NVIDIA"),
    ("no", "끄기 — 소프트웨어 디코딩"),
)

# (저장 값, 화면에 보일 말)
BIT_DEPTH_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "자동 — 10bit 이상 소스만 16bit PNG"),
    ("8", "항상 8bit PNG"),
    ("16", "항상 16bit PNG"),
)


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME") \
        or str(Path.home())
    return Path(base) / "PlayerX" / "settings.json"


@dataclass
class Settings:
    hwdec: str = "auto-safe"
    capture_bit_depth: str = "auto"      # "auto" | "8" | "16"
    shot_dir: str = ""                   # 비어 있으면 프로젝트의 shots/
    compare_mode: str = "side"
    wipe: float = 0.5
    diff_gain: int = 1
    trim_encoder: str = ""               # 인코더 이름. 비어 있으면 첫 번째(무손실)
    trim_range_only: bool = True         # 고른 클립의 IN~OUT 안에서만 재생
    trim_loop: bool = True               # 구간 끝에 닿으면 IN 으로 돌아가 반복
    window_geometry: str = ""            # QMainWindow.saveGeometry() 를 base64 로

    # ------------------------------------------------------------------ 검사

    def normalized(self) -> "Settings":
        """이상한 값을 기본값으로 되돌린다.

        설정 파일은 사람이 손으로 고칠 수 있는 자리다. 오타 하나로 앱이
        이상하게 도는 것보다 조용히 기본값으로 가는 편이 낫다.
        """
        clean = Settings(**asdict(self))
        if clean.hwdec not in {value for value, _ in HWDEC_CHOICES}:
            clean.hwdec = Settings.hwdec
        if clean.capture_bit_depth not in {value for value, _ in BIT_DEPTH_CHOICES}:
            clean.capture_bit_depth = Settings.capture_bit_depth
        if clean.compare_mode not in {"side", "wipe", "diff"}:
            clean.compare_mode = Settings.compare_mode
        try:
            clean.wipe = min(1.0, max(0.0, float(clean.wipe)))
        except (TypeError, ValueError):
            clean.wipe = Settings.wipe
        try:
            clean.diff_gain = max(1, min(64, int(clean.diff_gain)))
        except (TypeError, ValueError):
            clean.diff_gain = Settings.diff_gain
        for name in ("shot_dir", "trim_encoder", "window_geometry"):
            if not isinstance(getattr(clean, name), str):
                setattr(clean, name, "")
        for name in ("trim_range_only", "trim_loop"):
            setattr(clean, name, bool(getattr(clean, name)))
        return clean

    @property
    def force_bit_depth(self) -> int | None:
        """캡쳐가 쓰는 형태로. None 이면 소스에 맡긴다."""
        return int(self.capture_bit_depth) if self.capture_bit_depth in ("8", "16") else None


def load(path: Path | None = None) -> Settings:
    """설정을 읽는다. 없거나 깨졌으면 기본값."""
    target = Path(path) if path else settings_path()
    try:
        obj = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(obj, dict):
        return Settings()

    known = {f.name for f in fields(Settings)}
    # 모르는 키는 버리고, 아는 키만 가져온다. 옛 버전이 남긴 찌꺼기나
    # 손으로 넣은 오타가 있어도 생성자가 터지지 않게.
    data = {k: v for k, v in obj.items() if k in known}
    try:
        return Settings(**data).normalized()
    except TypeError:
        return Settings()


def save(settings: Settings, path: Path | None = None) -> Path | None:
    """설정을 쓴다. 못 쓰면 None (설정 저장 실패로 앱이 죽으면 안 된다)."""
    target = Path(path) if path else settings_path()
    payload = {"settings_version": SETTINGS_VERSION, **asdict(settings.normalized())}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        return None
    return target

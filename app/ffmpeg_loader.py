"""vendor/ffmpeg.exe 를 찾고, 그 빌드가 무엇을 할 수 있는지 알아낸다.

libmpv 와 같은 취급이다 — 100MB 넘는 네이티브 바이너리라 저장소에 없고
`tools/fetch_ffmpeg.py` 로 받는다.

**빌드마다 인코더가 다르다는 게 중요하다.** 계획서 판단대로 LGPL 빌드를 기본으로
받는데, LGPL 빌드에는 x264/x265 가 없다(`--disable-libx264`). 무손실 컷은
`-c copy` 라 인코더가 필요 없으니 상관없지만, 정확 컷(재인코딩)은 이 빌드에
실제로 들어 있는 인코더 중에서 골라야 한다. 그래서 `ffmpeg -encoders` 를 한 번
읽어서 쓸 수 있는 것만 UI 에 내놓는다 — 없는 인코더를 눌러 보고 실패하는 것보다
애초에 안 보이는 편이 낫다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

# 자식 프로세스의 콘솔 창을 띄우지 않는다 (Windows GUI 앱에서 까만 창이 번쩍인다).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class FFmpegNotFound(RuntimeError):
    pass


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        # exe 옆이 먼저다 — LGPL 빌드로 묶어 놓고 나중에 GPL 빌드로 바꿔 넣으려면
        # 번들 안을 건드리지 않고 여기에 떨어뜨릴 수 있어야 한다.
        dirs.append(Path(sys.executable).parent)
        dirs.append(Path(sys.executable).parent / "vendor")
        # PyInstaller 6 은 번들 내용물을 `_internal/` 에 넣는다 (mpv_loader 참고).
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            dirs.append(Path(bundle))

    project_root = Path(__file__).resolve().parent.parent
    dirs.append(project_root / "vendor")
    dirs.append(project_root)

    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw.strip():
            dirs.append(Path(raw))

    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def find_ffmpeg() -> Path | None:
    for d in _candidate_dirs():
        try:
            candidate = d / EXE_NAME
            if candidate.is_file():
                return candidate
        except OSError:
            # 끊긴 네트워크 드라이브가 PATH 에 있으면 여기로 온다
            continue
    return None


def require_ffmpeg() -> Path:
    exe = find_ffmpeg()
    if exe is None:
        raise FFmpegNotFound(
            f"{EXE_NAME} 을 찾지 못했습니다.\n\n"
            "다음을 실행해 받아 주세요:\n"
            "    .venv\\Scripts\\python tools\\fetch_ffmpeg.py\n\n"
            f"받은 파일은 {Path(__file__).resolve().parent.parent / 'vendor'} 에 들어갑니다."
        )
    return exe


def run(exe: Path, args: list[str], timeout: float = 30.0) -> str:
    """ffmpeg 을 짧게 한 번 돌리고 출력을 돌려준다 (정보 조회용)."""
    result = subprocess.run(  # noqa: S603
        [str(exe), "-hide_banner", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=_NO_WINDOW,
    )
    return (result.stdout or "") + (result.stderr or "")


_encoder_cache: dict[str, frozenset[str]] = {}


def available_encoders(exe: Path) -> frozenset[str]:
    """이 빌드에 들어 있는 인코더 이름 집합. 한 번 읽고 캐시한다."""
    key = str(exe)
    cached = _encoder_cache.get(key)
    if cached is not None:
        return cached

    names: set[str] = set()
    try:
        text = run(exe, ["-encoders"])
    except (OSError, subprocess.SubprocessError):
        text = ""
    for line in text.splitlines():
        # " V....D libopenh264          OpenH264 H.264 / ..." 형태.
        # 앞 6칸이 능력 플래그이고 7번째부터 이름이다.
        if len(line) > 8 and line[1] in "VAS" and line[0] == " ":
            name = line[7:].split(None, 1)
            if name:
                names.add(name[0])
    _encoder_cache[key] = frozenset(names)
    return _encoder_cache[key]


def version_line(exe: Path) -> str:
    try:
        text = run(exe, ["-version"], timeout=15.0)
    except (OSError, subprocess.SubprocessError):
        return "알 수 없음"
    first = text.splitlines()[0] if text else ""
    return first.strip() or "알 수 없음"

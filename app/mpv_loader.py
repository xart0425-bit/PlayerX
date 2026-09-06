"""libmpv-2.dll 을 찾아서 로딩 경로에 등록한 뒤 python-mpv 를 import 한다.

python-mpv README 가 경고하는 지점이다 — Windows 는 DLL 탐색 순서가 지저분해서
`import mpv` 만으로는 vendor/ 안의 DLL 을 못 찾는다. ctypes 가 DLL 을 열기 전에
os.add_dll_directory() 로 폴더를 명시해 줘야 한다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DLL_NAME = "libmpv-2.dll"


def _candidate_dirs() -> list[Path]:
    """libmpv-2.dll 이 있을 만한 폴더를 우선순위대로."""
    dirs: list[Path] = []

    if getattr(sys, "frozen", False):
        # exe 옆이 먼저다 — 사용자가 다른 빌드를 떨어뜨려 놓으면 그게 이긴다.
        dirs.append(Path(sys.executable).parent)
        dirs.append(Path(sys.executable).parent / "vendor")
        # PyInstaller 6 은 번들 내용물을 exe 옆이 아니라 `_internal/` 에 넣는다.
        # sys._MEIPASS 가 그 폴더를 가리킨다 (5.x 는 exe 옆이었다).
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            dirs.append(Path(bundle))

    project_root = Path(__file__).resolve().parent.parent
    dirs.append(project_root / "vendor")
    dirs.append(project_root)

    # 시스템에 mpv 가 이미 깔려 있다면 그것도 후보
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


def find_libmpv() -> Path | None:
    for d in _candidate_dirs():
        try:
            candidate = d / DLL_NAME
            if candidate.is_file():
                return candidate
        except OSError:
            # 끊긴 네트워크 드라이브가 PATH 에 있으면 여기로 온다
            continue
    return None


class LibmpvNotFound(RuntimeError):
    pass


def load_mpv():
    """python-mpv 모듈을 돌려준다. DLL 을 못 찾으면 LibmpvNotFound."""
    dll = find_libmpv()
    if dll is None:
        raise LibmpvNotFound(
            f"{DLL_NAME} 을 찾지 못했습니다.\n\n"
            "https://github.com/shinchiro/mpv-winbuild-cmake/releases 에서\n"
            "mpv-dev-x86_64-*.7z 를 받아 안의 libmpv-2.dll 을\n"
            f"{Path(__file__).resolve().parent.parent / 'vendor'} 에 넣어 주세요."
        )

    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(dll.parent))
    # ctypes.util.find_library 는 PATH 도 본다 — 양쪽 다 채워 둔다
    os.environ["PATH"] = str(dll.parent) + os.pathsep + os.environ.get("PATH", "")

    import mpv  # noqa: PLC0415  (DLL 경로 등록 뒤에 import 해야 한다)

    return mpv

"""파일로 남기는 실행 기록.

**왜 필요한가**: 이 앱은 콘솔이 없다. 조용히 사라지면(실제로 두 번 그랬다)
무슨 일이 있었는지 알 방법이 아무것도 없다. mpv 가 죽기 직전에 뱉은 경고 한 줄이
결정적일 때가 많은데, 그건 상태 표시줄에만 흘러가고 앱과 함께 사라진다.

**`faulthandler` 는 쓰지 않는다.** 죽는 순간의 스택을 남기려고 한 번 붙였다가
오히려 앱을 죽였다 — 윈도우에서 mpv 와 그래픽 드라이버는 정상적으로 처리되는
예외를 수시로 던지는데, faulthandler 가 그것까지 치명적으로 보고 프로세스를
끝내 버린다 (자세한 경위는 docs/BUILD-LOG.md).

여기서 하는 일은 **그냥 파일에 한 줄씩 적는 것**뿐이다. 프로세스 안에서
아무것도 가로채지 않는다.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

MAX_BYTES = 2_000_000        # 이보다 커지면 새로 시작한다 (무한히 자라지 않게)

_path: Path | None = None
_broken = False              # 한 번 실패하면 다시 시도하지 않는다


def log_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) / "PlayerX" if root else Path.cwd()
    return base / "playerx.log"


def start(note: str = "") -> Path | None:
    """새 실행을 기록하기 시작한다. 실패하면 조용히 포기한다."""
    global _path, _broken
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            path.unlink()
        _path = path
        write(f"===== 시작 (pid {os.getpid()}) {note} =====")
        return path
    except Exception:
        _broken = True
        return None


def write(line: str) -> None:
    """한 줄 적는다. 기록이 실패한다고 앱이 멈추면 안 된다."""
    global _broken
    if _path is None or _broken:
        return
    try:
        with _path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  {line}\n")
    except Exception:
        _broken = True

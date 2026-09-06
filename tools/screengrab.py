"""검증 도구용 스크린샷 — 창을 확실히 맨 앞에 놓고 화면을 찍는다.

**화면 전체를 찍어야 하는 이유**: mpv 는 네이티브 자식 창에 GPU 로 직접 그린다.
`QScreen.grabWindow(창 ID)` 로 창만 찍으면 그 부분이 까맣게 나온다.
`grabWindow(0)`(화면 전체)이라야 영상이 들어온다.

**그런데 화면 전체를 찍으면 앞에 있는 다른 창이 같이 찍힌다.** Windows 는 포그라운드가
아닌 프로세스가 `SetForegroundWindow` 로 앞에 나서는 걸 막기 때문에, `raise_()` 나
`activateWindow()` 로는 편집기 창 뒤에 그대로 남는 일이 생긴다 (실제로 겪었다 —
검증 스크린샷에 VS Code 가 찍혔다).

그래서 `SetWindowPos(HWND_TOPMOST)` 로 **항상 위** 속성을 준다. 이건 포그라운드
권한이 필요 없어서 확실히 먹는다. 검증이 끝나면 `release()` 로 되돌린다.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from PySide6.QtGui import QGuiApplication

_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040
_SW_RESTORE = 9
_FLAGS = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW


def _user32():
    """argtypes 를 지정한 user32. 지정이 **꼭 필요하다**.

    창 핸들은 64비트인데 ctypes 는 기본으로 인자를 c_int(32비트)로 넘긴다.
    그대로 부르면 핸들이 잘려서 조용히 아무 일도 안 일어난다 — 실제로 여기서
    한 번 걸렸다 (스크린샷에 계속 편집기 창이 찍혔다).
    """
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return None                          # Windows 가 아니면 조용히 포기

    c_void_p, c_int, c_uint = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    user32.ShowWindow.argtypes = [c_void_p, c_int]
    user32.SetWindowPos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
    user32.SetForegroundWindow.argtypes = [c_void_p]
    user32.IsIconic.argtypes = [c_void_p]
    return user32


def pin_on_top(window) -> None:
    """창을 '항상 위'로 만든다. 검증하는 동안만 켜 둔다."""
    user32 = _user32()
    if user32 is None:
        window.raise_()
        window.activateWindow()
        return
    hwnd = int(window.winId())
    # 최소화됐을 때만 되돌린다. 무조건 SW_RESTORE 를 부르면 **최대화까지 풀려서**
    # 창이 작아진 채로 스크린샷이 찍힌다 (여기서도 한 번 걸렸다).
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _FLAGS)
    user32.SetForegroundWindow(hwnd)
    window.raise_()
    window.activateWindow()


def release(window) -> None:
    """'항상 위'를 푼다."""
    user32 = _user32()
    if user32 is None:
        return
    user32.SetWindowPos(int(window.winId()), _HWND_NOTOPMOST, 0, 0, 0, 0, _FLAGS)


def grab_screen(path: str | Path) -> str:
    """화면 전체를 PNG 로 저장한다."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    QGuiApplication.primaryScreen().grabWindow(0).save(str(out), "PNG")
    return str(out)

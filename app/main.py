"""PlayerX 진입점."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from .mpv_loader import LibmpvNotFound


def selftest(out_path: str | None = None) -> int:
    """이 빌드가 필요한 것을 다 갖췄는지 확인하고 보고서를 남긴다.

    묶어 놓은(PyInstaller) 앱은 창만 안 뜨고 왜 그런지 알 방법이 없다 —
    콘솔이 없어서 예외조차 안 보인다. 그래서 **창을 띄우지 않고** 네이티브
    의존물을 하나씩 찾아 보고 결과를 JSON 으로 적는다.

        PlayerX.exe --selftest [보고서.json]

    배포판을 받은 사람이 "왜 안 켜지지"를 스스로 확인하는 데도 쓴다.
    """
    report: dict = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "bundle_dir": getattr(sys, "_MEIPASS", None),
        "python": sys.version.split()[0],
        "ok": True,
        "problems": [],
    }

    def fail(message: str) -> None:
        report["ok"] = False
        report["problems"].append(message)

    # --- libmpv (없으면 재생 자체가 안 된다) ---
    try:
        from .mpv_loader import DLL_NAME, find_libmpv

        dll = find_libmpv()
        report["libmpv"] = str(dll) if dll else None
        if dll is None:
            fail(f"{DLL_NAME} 을 찾지 못했습니다.")
    except Exception as exc:
        report["libmpv"] = None
        fail(f"libmpv 확인 중 오류: {exc}")

    # --- ffmpeg (없으면 자르기만 안 된다 — 치명적이지는 않다) ---
    try:
        from .ffmpeg_loader import available_encoders, find_ffmpeg, version_line

        exe = find_ffmpeg()
        report["ffmpeg"] = str(exe) if exe else None
        if exe is not None:
            report["ffmpeg_version"] = version_line(exe)
            encoders = available_encoders(exe)
            report["ffmpeg_encoders"] = len(encoders)
            report["has_x264"] = "libx264" in encoders
        else:
            report["problems"].append(
                "ffmpeg.exe 가 없습니다 — 자르기(트림 탭)만 못 씁니다.")
    except Exception as exc:
        report["ffmpeg"] = None
        report["problems"].append(f"ffmpeg 확인 중 오류: {exc}")

    # --- 파이썬 쪽 의존물 ---
    for name in ("PySide6", "av", "numpy", "mpv"):
        try:
            module = __import__(name)
            report[f"{name}_version"] = getattr(module, "__version__", "?")
        except Exception as exc:
            report[f"{name}_version"] = None
            fail(f"{name} 를 불러오지 못했습니다: {exc}")

    # 보고서는 파일로 남긴다. 콘솔이 없는 GUI 빌드에서는 print 가 아무 데도 안 간다.
    target = Path(out_path) if out_path else _default_report_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        report["report_path"] = str(target)
    except OSError:
        pass

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _default_report_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "selftest.json"
    return Path.cwd() / "selftest.json"


# faulthandler 는 여기 쓰면 안 된다 — 실제로 앱을 죽였다.
#
# 죽는 순간의 파이썬 스택을 남기려고 `faulthandler.enable()` 을 붙였다가,
# 그것 때문에 앱이 계속 세그폴트로 죽었다 (끄면 5/5 정상, 켜면 6/6 죽음).
# 윈도우에서 mpv 와 그래픽 드라이버는 **정상적으로 처리되는** 예외
# (여기서는 코드 0xE24C4A02)를 수시로 던지는데, faulthandler 는 그것까지
# 치명적인 것으로 보고 프로세스를 끝내 버린다. 파일 경로를 다루는
# `pathlib.resolve()` 한복판에서도 그 예외가 잡힌 걸 보고 알았다 —
# mpv 와 아무 상관 없는 자리였다.
#
# 앱이 조용히 사라지는 문제를 다시 쫓아야 한다면, 프로세스 안에서 예외를
# 가로채는 방식 말고 바깥에서 보는 방식(윈도우 오류 보고의 로컬 덤프 등)을
# 쓸 것.


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    if "--selftest" in argv:
        rest = [a for a in argv[argv.index("--selftest") + 1:] if not a.startswith("-")]
        return selftest(rest[0] if rest else None)

    from . import applog

    applog.start(f"argv={argv[1:]}")

    app = QApplication(argv)
    app.setApplicationName("PlayerX")
    app.aboutToQuit.connect(lambda: applog.write("===== 정상 종료 ====="))

    # 화면 테마는 창을 만들기 전에 입힌다 — 나중에 입히면 이미 만들어진
    # 위젯이 기본 스타일로 한 번 그려졌다가 바뀌면서 깜빡인다.
    from . import theme

    theme.apply(app)

    # MainWindow 를 여기서 import 하는 이유: libmpv 로딩 실패를 콘솔이 아니라
    # 대화상자로 보여 주려면 QApplication 이 먼저 살아 있어야 한다.
    try:
        from .main_window import MainWindow

        window = MainWindow()
    except LibmpvNotFound as exc:
        QMessageBox.critical(None, "libmpv 를 찾지 못했습니다", str(exc))
        return 2

    window.show()

    # 명령줄로 넘어온 첫 파일은 바로 연다.
    for arg in argv[1:]:
        if not arg.startswith("-"):
            window.open_path(arg)
            break

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

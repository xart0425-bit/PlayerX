"""묶어 놓은 배포판을 실제로 실행해서 확인한다 — 5단계 검증 (5-5 · 5-6).

**소스로 도는 앱이 멀쩡한 것과 묶어 놓은 앱이 멀쩡한 것은 다른 문제다.**
PyInstaller 는 네이티브 DLL 을 다른 자리(`_internal/`)에 넣고, `__file__` 기준
경로가 전부 번들 안을 가리키게 된다. 그래서 묶은 결과물을 직접 돌려 봐야 한다.

계획서가 5-6 으로 따로 잡아 둔 것도 이것이다 — 이 프로젝트는 네트워크 드라이브
(`Z:` = UNC 공유)에 있고, 거기서 실행되는지가 미지수였다.

검사하는 것:

  A. **자기 점검** — `PlayerX.exe --selftest` 가 libmpv·ffmpeg·PyAV 를 다 찾는가.
     묶인 앱은 콘솔이 없어서 실패해도 아무 말이 없다. 그래서 앱 자신이 확인하고
     JSON 으로 적게 해 뒀다 (app/main.py).
  B. **진짜로 창이 뜨는가** — 영상을 인자로 주고 띄운 뒤, 창 제목에 파일 이름이
     들어오기를 기다린다. 제목이 바뀌었다는 건 libmpv 가 파일을 열었다는 뜻이다.
  C. **묶인 앱 안에서 PyAV 가 도는가** — 인덱스 캐시(`.idx.json`)가 생기는지 본다.
     그게 생겼다면 번들 안의 FFmpeg 라이브러리가 살아 있다는 뜻이다.
  D. **경로** — vendor 바이너리가 배포판 안에 실제로 들어갔는가, 크기는 얼마인가.

    python tools/verify_package.py [<dist/PlayerX 경로>]
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import screengrab  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = PROJECT / "dist" / "PlayerX"
TEST_VIDEO = PROJECT / "testdata" / "test_60fps.mkv"

WINDOW_TIMEOUT = 60.0
INDEX_TIMEOUT = 60.0


# ------------------------------------------------------------------ 창 찾기

def find_windows_of(pid: int) -> list[str]:
    """그 프로세스가 가진 최상위 창들의 제목.

    1단계에서 겪은 것: `Start-Process` 로 띄우면 `MainWindowHandle` 이 0 으로
    나와서 프로세스 속성으로는 창을 못 찾는다. EnumWindows 로 직접 훑어야 한다.
    """
    user32 = ctypes.windll.user32
    titles: list[str] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            if buffer.value:
                titles.append(buffer.value)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return titles


class Checker:
    def __init__(self, dist: Path) -> None:
        self.dist = dist
        self.exe = dist / "PlayerX.exe"
        self.failures: list[str] = []
        self.records: dict = {}

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "OK " if ok else "!! "
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}", flush=True)
        if not ok:
            self.failures.append(f"{label} {detail}".strip())
        return ok

    # ---------------------------------------------------------------- 검사 D

    def check_layout(self) -> bool:
        print("\n[D] 배포판 구성", flush=True)
        if not self.check(self.exe.is_file(), "PlayerX.exe 가 있음", str(self.exe)):
            return False

        files = list(self.dist.rglob("*"))
        total = sum(f.stat().st_size for f in files if f.is_file())
        count = sum(1 for f in files if f.is_file())
        self.records["size_mb"] = round(total / 1e6, 1)
        self.records["file_count"] = count
        print(f"    {total / 1e6:,.0f} MB · 파일 {count:,}개", flush=True)

        found = {}
        for name in ("libmpv-2.dll", "ffmpeg.exe"):
            hits = [f for f in files if f.name == name and f.is_file()]
            found[name] = str(hits[0].relative_to(self.dist)) if hits else None
            self.check(bool(hits), f"{name} 이 배포판 안에 있음",
                       found[name] or "없음")
        self.records["vendor"] = found

        # 안 쓰는 Qt 모듈을 실제로 뺐는지 (스펙의 excludes 가 먹었는지)
        heavy = [f.name for f in files
                 if f.name.startswith("Qt6WebEngine") or f.name.startswith("Qt6Quick")]
        self.check(not heavy, "안 쓰는 Qt 모듈이 빠졌음", str(heavy[:3]))
        return True

    # ---------------------------------------------------------------- 검사 A

    def check_selftest(self) -> None:
        print("\n[A] 자기 점검 (--selftest)", flush=True)
        report_path = self.dist / "selftest.json"
        if report_path.exists():
            report_path.unlink()

        try:
            proc = subprocess.run(  # noqa: S603
                [str(self.exe), "--selftest", str(report_path)],
                capture_output=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.check(False, "--selftest 실행", str(exc))
            return

        if not self.check(report_path.is_file(), "보고서가 생김", str(report_path)):
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.records["selftest"] = report

        self.check(report.get("frozen") is True, "묶인 상태로 인식",
                   f"bundle={report.get('bundle_dir')}")
        self.check(bool(report.get("libmpv")), "libmpv 를 찾음",
                   str(report.get("libmpv")))
        self.check(bool(report.get("ffmpeg")), "ffmpeg 을 찾음",
                   f"{report.get('ffmpeg')} · 인코더 {report.get('ffmpeg_encoders')}개")
        for name in ("PySide6", "av", "numpy", "mpv"):
            self.check(report.get(f"{name}_version") is not None,
                       f"{name} 를 불러옴", str(report.get(f"{name}_version")))
        self.check(proc.returncode == 0, "--selftest 가 성공으로 끝남",
                   f"exit={proc.returncode} · 문제 {report.get('problems')}")

    # ------------------------------------------------------------- 검사 B/C

    def check_launch(self) -> None:
        print("\n[B/C] 실제 실행 — 창이 뜨고 파일이 열리는가", flush=True)
        if not TEST_VIDEO.is_file():
            self.check(False, "테스트 영상이 있음", str(TEST_VIDEO))
            return

        # 인덱스 캐시를 지워 놓고 시작해야 "묶인 앱이 새로 만들었다"를 확인할 수 있다.
        from app.frame_index import sidecar_path  # noqa: PLC0415

        cache = sidecar_path(TEST_VIDEO)
        if cache.exists():
            cache.unlink()

        proc = subprocess.Popen([str(self.exe), str(TEST_VIDEO)])  # noqa: S603
        try:
            title = self._wait_for_title(proc.pid, TEST_VIDEO.name)
            self.check(title is not None,
                       "창 제목에 파일 이름이 들어옴 (libmpv 가 파일을 열었다)",
                       str(title))
            self.records["window_title"] = title

            if title is not None:
                shot = PROJECT / "shots" / "package" / "packaged_app.png"
                time.sleep(2.0)
                screengrab.grab_screen(shot)
                print(f"    스크린샷: {shot}", flush=True)

            got_cache = self._wait_for_cache(cache)
            self.check(got_cache,
                       "묶인 앱이 프레임 인덱스를 만듦 (번들 안 PyAV 가 돈다)",
                       f"{cache.name} · {cache.stat().st_size if got_cache else 0} 바이트")

            self.check(proc.poll() is None, "실행 중에 죽지 않음",
                       f"exit={proc.poll()}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _wait_for_title(self, pid: int, needle: str) -> str | None:
        deadline = time.monotonic() + WINDOW_TIMEOUT
        seen: str | None = None
        while time.monotonic() < deadline:
            for title in find_windows_of(pid):
                seen = title
                if needle in title:
                    return title
            time.sleep(0.5)
        return None if seen is None or needle not in seen else seen

    @staticmethod
    def _wait_for_cache(path: Path) -> bool:
        deadline = time.monotonic() + INDEX_TIMEOUT
        while time.monotonic() < deadline:
            if path.is_file() and path.stat().st_size > 0:
                return True
            time.sleep(0.5)
        return False

    # ---------------------------------------------------------------- 마무리

    def finish(self) -> int:
        out = PROJECT / "shots" / "package"
        out.mkdir(parents=True, exist_ok=True)
        (out / "result_package.json").write_text(
            json.dumps({"failures": self.failures, "records": self.records},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "=" * 62)
        if self.failures:
            print(f"실패 {len(self.failures)}건:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("전부 통과 — 묶어 놓은 배포판이 이 경로에서 실제로 실행됩니다.")
        print(f"  경로: {self.dist}")
        return 0


def main(argv: list[str]) -> int:
    # 스크린샷을 찍으려면 Qt 애플리케이션이 하나 있어야 한다. 이 도구는 GUI 가
    # 아니지만 화면을 캡쳐하므로 최소한의 QGuiApplication 을 세워 둔다.
    from PySide6.QtGui import QGuiApplication  # noqa: PLC0415

    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841

    dist = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIST
    if not dist.is_dir():
        print(f"{dist} 가 없습니다. 먼저 빌드하세요:\n"
              f"    .venv\\Scripts\\pyinstaller PlayerX.spec")
        return 1

    print(f"배포판: {dist}")
    print(f"경로 종류: {'네트워크(UNC/매핑 드라이브)' if _is_network(dist) else '로컬 디스크'}")

    checker = Checker(dist)
    if checker.check_layout():
        checker.check_selftest()
        checker.check_launch()
    return checker.finish()


def _is_network(path: Path) -> bool:
    resolved = str(path.resolve())
    if resolved.startswith("\\\\"):
        return True
    try:
        drive = str(path.absolute())[:2]
        # DRIVE_REMOTE == 4
        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 4
    except Exception:
        return False


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT))
    raise SystemExit(main(sys.argv))

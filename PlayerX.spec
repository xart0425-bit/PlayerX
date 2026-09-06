# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 스펙 — onedir 로 묶는다.

    .venv\\Scripts\\pyinstaller PlayerX.spec

**onefile 이 아니라 onedir 인 이유** (계획서 기술스택에 적어 둔 판단):
onefile 은 실행할 때마다 임시 폴더에 통째로 풀고 그 안에서 DLL 을 찾는다.
libmpv 처럼 큰 네이티브 DLL 이 끼면 시작이 느려지고 DLL 탐색이 꼬인다.
onedir 는 폴더째 복사하면 끝이고, 우리 로더들이 이미 "exe 옆"을 먼저 본다.

**vendor 바이너리가 어디로 가는가**: PyInstaller 6 은 번들 내용물을 exe 옆이 아니라
`_internal/` 안에 넣는다 (5.x 는 exe 옆이었다). 그래서 `libmpv-2.dll` 과 `ffmpeg.exe`
도 거기로 간다. `app/mpv_loader.py` 와 `app/ffmpeg_loader.py` 는 frozen 일 때
**exe 옆 → exe 옆 vendor/ → `sys._MEIPASS`(= `_internal/`)** 순으로 찾는다.
exe 옆을 먼저 보는 게 중요하다 — 나중에 ffmpeg 을 다른 빌드(예: GPL)로 바꾸고 싶으면
번들 안을 건드리지 않고 exe 옆에 떨어뜨리면 그게 이긴다.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT = Path(SPECPATH)
VENDOR = PROJECT / "vendor"

# exe 옆에 그대로 놓을 것들. (원본, 놓을 자리)
binaries = []
for name in ("libmpv-2.dll", "ffmpeg.exe"):
    path = VENDOR / name
    if path.is_file():
        binaries.append((str(path), "."))
    else:
        print(f"[PlayerX.spec] 경고: {path} 가 없습니다 — "
              f"tools/fetch_ffmpeg.py 나 README 의 libmpv 절차를 보세요.")

# 안 쓰는 Qt 모듈을 덜어 낸다. PySide6_Addons 는 통째로 넣으면 수백 MB 다.
# 지금 쓰는 건 QtCore/QtGui/QtWidgets 뿐이고, 검증 도구가 QtTest 를 쓴다.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DLogic",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtSql", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtWebSockets", "PySide6.QtWebChannel",
    # 개발용으로만 쓰는 것들
    "tkinter", "unittest", "pydoc", "doctest", "pytest", "setuptools", "pip",
    "PyInstaller",
]

a = Analysis(
    ["run.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=[],
    # python-mpv 는 ctypes 로 DLL 을 여는 단일 모듈이라 자동 추적이 안 된다.
    hiddenimports=["mpv", *collect_submodules("av")],
    hookspath=[],
    excludes=excludes,
    noarchive=False,
)

# excludes 로 파이썬 모듈은 빠지는데, Qt 의 **DLL** 은 의존 그래프를 타고 그대로
# 딸려 온다. QML 스택(Qt6Quick·Qt6Qml, 약 11MB)이 그렇다 — 이 앱은 QML 을 한 줄도
# 안 쓴다. 그래서 바이너리 목록에서 직접 덜어 낸다.
#
# 이렇게 뺀 게 사실은 필요했던 것이면 앱이 안 켜진다. 그래서
# tools/verify_package.py 가 묶은 결과물을 실제로 실행해 본다 — 그 검증이
# 있어서 이 정도는 손대도 된다.
_DROP_PREFIXES = ("Qt6Quick", "Qt6Qml")
a.binaries = TOC([entry for entry in a.binaries
                  if not Path(entry[0]).name.startswith(_DROP_PREFIXES)])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PlayerX",
    debug=False,
    strip=False,
    upx=False,              # UPX 로 압축하면 네이티브 DLL 로딩이 꼬이는 사례가 있다
    console=False,          # GUI 앱 — 콘솔 창을 띄우지 않는다
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PlayerX",
)

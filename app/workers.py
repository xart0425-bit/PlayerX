"""무거운 작업을 백그라운드 스레드로 돌리는 Qt 래퍼.

인덱싱과 캡쳐는 둘 다 파일을 읽는다. 메인 스레드에서 하면 그동안 창이 굳는다
(4K 장시간 파일이면 수십 초). 그래서 QThread 로 뺀다.

지키는 규칙 두 가지.

* **PyAV 객체는 그 스레드 안에서만 만들고 쓴다.** 컨테이너를 스레드 밖으로
  넘기지 않는다. 결과로 넘어가는 FrameIndex/CaptureResult 는 순수 파이썬
  데이터라 넘겨도 안전하다.
* **결과는 시그널로만 돌려준다.** 워커가 UI 위젯을 직접 건드리지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import capture as capture_mod
from . import export as export_mod
from . import frame_index as fi


class IndexWorker(QThread):
    """프레임 인덱스를 만들거나 캐시에서 읽어 온다.

    캐시가 맞으면 곧장 ready 를 낸다 (이게 '재열기 시 즉시 로드'다).
    """

    progress = Signal(float, int)     # 비율(0~1, 모르면 -1), 지금까지 프레임 수
    ready = Signal(object, bool)      # FrameIndex, 캐시에서 왔는가
    failed = Signal(str)

    def __init__(self, video_path: str, parent=None) -> None:
        super().__init__(parent)
        self.video_path = video_path
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            cached = fi.load_index(self.video_path)
            if cached is not None:
                self.ready.emit(cached, True)
                return

            index = fi.build_index(
                self.video_path,
                progress=lambda ratio, n: self.progress.emit(ratio, n),
                cancelled=lambda: self._cancel,
            )
            if self._cancel:
                return
            # 저장에 실패해도(읽기 전용 폴더 등) 인덱스 자체는 쓸 수 있다.
            fi.save_index(index, self.video_path)
            self.ready.emit(index, False)
        except Exception as exc:
            if not self._cancel:
                self.failed.emit(str(exc))


class CaptureWorker(QThread):
    """현재 프레임을 PNG 로 저장한다.

    긴 GOP 의 4K 파일이면 목표 프레임까지 디코딩하는 데 1초 넘게 걸린다.
    그동안 창이 멈추면 안 되니 스레드로 뺀다.
    """

    done = Signal(object)             # CaptureResult
    failed = Signal(str)

    def __init__(self, video_path: str, index, frame_no: int,
                 out_path: Path, force_bit_depth: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.video_path = video_path
        self.index = index
        self.frame_no = frame_no
        self.out_path = out_path
        self.force_bit_depth = force_bit_depth

    def run(self) -> None:
        try:
            result = capture_mod.capture_frame(
                self.video_path, self.index, self.frame_no,
                self.out_path, self.force_bit_depth,
            )
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class SequenceWorker(QThread):
    """구간을 통째로 PNG 시퀀스로 뽑는다.

    수백~수천 장이 나올 수 있어서 진행률과 취소가 꼭 필요하다.
    """

    progress = Signal(float, int, int)   # 비율, 저장한 장수, 전체 장수
    done = Signal(object, object)        # 저장 폴더(Path), 파일 목록(list[Path])
    failed = Signal(str)

    def __init__(self, video_path: str, index, first_frame: int, last_frame: int,
                 out_dir: Path, force_bit_depth: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.video_path = video_path
        self.index = index
        self.first_frame = first_frame
        self.last_frame = last_frame
        self.out_dir = out_dir
        self.force_bit_depth = force_bit_depth
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            written = capture_mod.capture_sequence(
                self.video_path, self.index, self.first_frame, self.last_frame,
                self.out_dir, self.force_bit_depth,
                on_progress=lambda r, n, t: self.progress.emit(r, n, t),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(self.out_dir, written)
        except Exception as exc:
            if not self._cancel:
                self.failed.emit(str(exc))


class ExportWorker(QThread):
    """클립 하나를 ffmpeg 으로 잘라 낸다.

    ffmpeg 은 별도 프로세스라 그 자체로는 창을 막지 않지만, 출력을 읽는 루프가
    블로킹이라 스레드가 필요하다. 취소하면 프로세스를 죽이고 만들다 만 파일을 지운다.
    """

    progress = Signal(float, int)     # 비율(0~1), 지금까지 프레임 수
    done = Signal(object)             # 결과 경로(Path)
    failed = Signal(str)

    def __init__(self, exe: Path, plan, source_bitrate: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.exe = exe
        self.plan = plan
        self.source_bitrate = source_bitrate
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            out = export_mod.run_export(
                self.exe, self.plan, self.source_bitrate,
                on_progress=lambda ratio, frames: self.progress.emit(ratio, frames),
                cancelled=lambda: self._cancel,
            )
            self.done.emit(out)
        except Exception as exc:
            if not self._cancel:
                self.failed.emit(str(exc))

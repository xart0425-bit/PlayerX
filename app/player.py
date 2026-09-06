"""libmpv 를 Qt 위젯 안에 박아 넣은 재생 표면.

역할은 두 가지다.
  1) 위젯의 네이티브 창 핸들(winId)을 mpv 에 넘겨 그 안에서 그리게 한다.
  2) "프레임 단위 이동"을 mpv 에 맡기지 않고 여기서 계산해서 절대 시각으로 seek 한다.

(2)가 이 프로젝트의 존재 이유다. mpv 의 frame-back-step 은 고프레임레이트에서
프레임을 건너뛴 전력이 있다 (mpv issue #7208). 그래서 뒤로 이동은 항상
'현재 프레임 번호를 구하고 -> 목표 프레임의 시각으로 hr-seek' 로 처리한다.

**프레임 번호의 근거는 PTS 인덱스다** (2단계). app/frame_index.py 가 파일 전체를
훑어 만든 PTS 표를 set_index() 로 받으면, 프레임 번호와 seek 목표를 전부 그
표에서 낸다 — VFR 파일도 정확하고 전체 프레임 수도 추정이 아니다.

인덱스가 아직 없는 동안(만드는 중이거나 실패했을 때)에는 1단계 방식인
time-pos x container-fps 로 물러난다. 그래야 인덱싱이 끝나기 전에도 파일을
볼 수 있다. 지금 어느 쪽인지는 index_ready 로 알 수 있다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from . import timecode
from .frame_index import FrameIndex
from .mpv_loader import load_mpv

# 창 안에 박아 넣고 프레임 검수에 쓰기 위한 mpv 설정.
MPV_OPTIONS: dict[str, object] = {
    # --- 창/입력: UI 는 전부 Qt 가 그린다 ---
    "osc": False,                     # mpv 자체 컨트롤 오버레이 끔
    "osd_level": 0,                   # OSD 도 끔 (캡쳐에 섞이면 안 된다)
    "input_default_bindings": False,  # 단축키는 Qt 쪽에서만 받는다
    "input_vo_keyboard": False,
    "cursor_autohide": "no",
    "border": False,
    "keepaspect": True,

    # --- 정확도 ---
    "keep_open": "always",       # 끝에 도달해도 마지막 프레임을 유지 (검수에 필수)
    "hr_seek": "yes",            # 항상 정밀 탐색
    "hr_seek_framedrop": False,  # 정밀 탐색 중 프레임 버리지 않기
    "pause": True,               # 열면 첫 프레임에서 멈춘 채 시작

    # --- 되감기 캐시: 뒤로 이동을 견딜 만하게 만든다 ---
    "cache": "yes",
    "demuxer_seekable_cache": "yes",
    "cache_secs": 30,
    "demuxer_max_back_bytes": "256MiB",

    # --- 출력 ---
    "vo": "gpu",
    "hwdec": "auto-safe",
    "profile": "low-latency",
}


# 파일을 열고 영상 정보가 올라오기를 기다리는 한계 시간(초).
LOAD_TIMEOUT = 20.0


def _as_text(value) -> str:
    """mpv 속성을 화면에 적을 문자열로.

    mpv 는 목록을 받는 옵션(hwdec 등)을 리스트로 돌려준다. 그대로 str() 하면
    `['nvdec']` 같은 게 정보 패널에 뜬다.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


class PlayerError(RuntimeError):
    pass


class MpvWidget(QWidget):
    """영상이 그려지는 위젯. 재생 상태가 바뀌면 state_changed 를 낸다."""

    state_changed = Signal()          # 프레임/시각/재생상태가 갱신됨
    file_loaded = Signal(str)         # 새 파일이 열림 (경로)
    mpv_message = Signal(str)         # mpv 로그 (에러 진단용)
    index_changed = Signal()          # PTS 인덱스가 붙거나 떨어짐

    POLL_MS = 30                      # 화면 갱신 주기
    HWDEC_POLL_EVERY = 8              # 디코더 이름은 이 폴링 횟수마다 한 번만

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # mpv 가 그릴 실제 HWND 를 갖도록 강제한다.
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        # "이 위젯은 자기 바탕을 스스로 칠한다" 는 약속. Qt 는 그 말을 믿고
        # 배경을 안 칠한다 — 그래서 paintEvent 에서 우리가 반드시 칠해야 한다.
        # 안 칠하면 파일이 없는 동안 이 창에 **앞서 그려져 있던 그림이 그대로
        # 남는다** (탭을 옮기면 앞 탭 화면이 비쳐 보이던 원인).
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)

        self._mpv_module = load_mpv()
        self._mpv = self._mpv_module.MPV(
            wid=str(int(self.winId())),
            log_handler=self._on_log,
            loglevel="warn",
            **MPV_OPTIONS,
        )

        self.path: str | None = None
        self.fps: float = 0.0
        self.frame_count: int = 0
        self.duration: float = 0.0
        self.time_pos: float = 0.0
        self.frame: int = 0
        self.paused: bool = True
        self.video_info: dict[str, object] = {}

        # PTS 인덱스. 붙어 있으면 프레임 번호·이동의 근거가 전부 이쪽으로 넘어간다.
        self.index: FrameIndex | None = None

        # seek 을 걸어 두고 mpv 가 아직 그 프레임에 도착하지 않은 상태.
        # 화살표 키를 누르고 있으면 seek 보다 키 반복이 빨라서, 이걸 안 두면
        # 이동한 걸 모르고 같은 자리에서 다시 계산해 프레임을 흘린다.
        self._pending_frame: int | None = None
        self._pending_polls: int = 0
        self._pending_from: int = 0        # seek 을 걸 때 있던 자리
        self._pending_playing: bool = False
        self._hwdec_polls: int = 0

        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

    def paintEvent(self, event) -> None:
        """바탕을 검게 칠한다.

        영상이 올라와 있는 동안에는 mpv 가 이 창에 직접 그리므로 여기까지 올 일이
        거의 없다. **파일이 없을 때**가 문제였다 — mpv 는 아무것도 그리지 않고,
        `WA_OpaquePaintEvent` 때문에 Qt 도 안 칠하니, 이 창에는 이 자리에 예전에
        있던 그림이 그대로 남아 있었다. 탭을 옮겼을 때 앞 탭 화면이 비쳐 보이던
        게 이것이다 (스타일시트로 배경색을 줘도 소용없다 — 그 속성이 있으면
        스타일시트 배경도 안 그린다).
        """
        QPainter(self).fillRect(event.rect(), QColor(0, 0, 0))

    # ------------------------------------------------------------------ mpv

    def _on_log(self, loglevel: str, component: str, message: str) -> None:
        # mpv 의 이벤트 스레드에서 불린다 — 시그널로 넘겨야 Qt 쪽이 안전하다.
        self.mpv_message.emit(f"[{loglevel}] {component}: {message.strip()}")

    def _get(self, name: str, default=None):
        """mpv 속성 읽기. 아직 값이 없으면 default."""
        try:
            value = getattr(self._mpv, name)
        except Exception:
            return default
        return default if value is None else value

    # ----------------------------------------------------------------- 파일

    def open(self, path: str) -> None:
        p = Path(path)
        if not p.is_file():
            raise PlayerError(f"파일이 없습니다: {path}")

        self.path = str(p)
        self.index = None                # 새 파일 — 이전 인덱스는 남의 것이다
        self._pending_frame = None
        self._pending_polls = 0
        self._mpv.pause = True
        self._mpv.play(self.path)

        # 파일 정보(fps, 길이)가 올라올 때까지 기다린다.
        # wait_until_playing() 은 쓸 수 없다 — 그건 core-idle 이 풀리기를 기다리는데,
        # 우리는 일부러 pause 상태로 여니까 영영 풀리지 않는다.
        try:
            self._mpv.wait_for_property(
                "video-params", lambda v: bool(v), timeout=LOAD_TIMEOUT
            )
        except Exception as exc:  # TimeoutError, 디코딩 실패 등
            raise PlayerError(
                f"열지 못했습니다: {p.name}\n"
                f"영상 스트림을 찾지 못했거나 디코딩에 실패했습니다.\n{exc}"
            ) from exc

        self._mpv.pause = True
        self._read_media_info()
        self._refresh()
        self.file_loaded.emit(self.path)

    def _read_media_info(self) -> None:
        self.fps = float(self._get("container_fps", 0.0) or 0.0)
        if self.fps <= 0:
            self.fps = float(self._get("estimated_vf_fps", 0.0) or 0.0)

        self.duration = float(self._get("duration", 0.0) or 0.0)

        count = int(self._get("estimated_frame_count", 0) or 0)
        if count <= 0 and self.fps > 0 and self.duration > 0:
            count = int(round(self.duration * self.fps))
        self.frame_count = count

        params = self._get("video_params", {}) or {}
        self.video_info = {
            "파일": Path(self.path).name if self.path else "",
            "코덱": self._get("video_format", "?"),
            "해상도": f"{params.get('w', '?')} x {params.get('h', '?')}",
            "픽셀 포맷": params.get("pixelformat", "?"),
            "색공간": params.get("colormatrix", "?"),
            "프레임레이트": f"{self.fps:.6g} fps" if self.fps else "알 수 없음",
            "길이": timecode.format_clock(self.duration),
            "전체 프레임": f"{self.frame_count} (추정)",
            "프레임 근거": "fps 추정 (인덱스 없음)",
            "키프레임 간격": "인덱싱 중…",
            "하드웨어 디코딩": self._hwdec_text(),
            "오디오": self._get("audio_codec_name", "없음"),
        }

    # --------------------------------------------------------------- PTS 인덱스

    @property
    def index_ready(self) -> bool:
        """프레임 번호가 인덱스에서 나오고 있는가 (fps 추정이 아니라)."""
        return self.index is not None and self.index.count > 0

    @property
    def seek_pending(self) -> bool:
        """걸어 둔 seek 이 아직 목표 프레임에 도착하지 않았다.

        구간 반복처럼 '넘어갔으면 되돌린다'를 폴링마다 판단하는 쪽에서 필요하다 —
        방금 건 점프가 도착하기 전에 또 걸면 같은 자리를 계속 다시 찾는다.
        """
        return self._pending_frame is not None

    def set_index(self, index: FrameIndex | None) -> None:
        """PTS 인덱스를 붙인다. 이 순간부터 프레임 번호·이동이 표 기반이 된다.

        인덱스가 붙으면 전체 프레임 수도 추정이 아니라 실제 개수가 된다.
        화면에 떠 있던 프레임 번호가 한둘 바뀔 수 있는데, 그게 정확해진 값이다.
        """
        self.index = index if (index is not None and index.count > 0) else None

        if self.index is not None:
            self.frame_count = self.index.count
            if self.index.fps > 0:
                self.fps = self.index.fps
            if self.index.duration > 0:
                self.duration = self.index.duration
            self._update_index_info()

        self._pending_frame = None
        self._pending_polls = 0
        self._refresh()
        self.index_changed.emit()

    def _update_index_info(self) -> None:
        """정보 패널에 인덱스가 알아낸 사실을 반영한다."""
        idx = self.index
        if idx is None:
            return
        color = idx.color
        matrix = color["matrix"] + ("" if color["matrix_declared"] else " (파일에 없어 관례로 확정)")
        rng = color["range"] + ("" if color["range_declared"] else " (파일에 없어 관례로 확정)")

        self.video_info["해상도"] = f"{idx.width} x {idx.height}"
        self.video_info["픽셀 포맷"] = f"{idx.pix_fmt} · {idx.bit_depth}bit"
        self.video_info["색공간"] = f"{matrix} / {rng}"
        self.video_info["길이"] = timecode.format_clock(idx.duration)
        self.video_info["전체 프레임"] = f"{idx.count} (인덱스)"
        self.video_info["프레임레이트"] = (
            f"{idx.fps:.6g} fps" + ("" if idx.cfr else " (VFR · 평균)")
        )
        self.video_info["프레임 근거"] = (
            "PTS 인덱스 · " + ("캐시에서 읽음" if idx.from_cache else "이번에 생성")
            + (" · CFR" if idx.cfr else " · VFR")
        )
        # 무손실 컷이 얼마나 밀릴 수 있는지의 척도다 (4단계 트림 탭 참고).
        self.video_info["키프레임 간격"] = idx.keyframe_summary()
        self.video_info["캡쳐 저장 형식"] = f"{16 if idx.bit_depth > 8 else 8}bit PNG"

    # ------------------------------------------------------- 프레임 <-> 시각

    def frame_at(self, seconds: float) -> int:
        """시각 -> 프레임 번호. 인덱스가 있으면 표에서, 없으면 fps 추정으로."""
        if self.index is not None:
            return self.index.frame_at_time(seconds)
        return timecode.frame_at_time(seconds, self.fps)

    def time_of(self, frame: int) -> float:
        """프레임 번호 -> 표시 시각(초)."""
        if self.index is not None:
            return self.index.time_of_frame(frame)
        return timecode.time_of_frame(frame, self.fps)

    def _seek_target(self, frame: int) -> float:
        if self.index is not None:
            return self.index.seek_target(frame)
        return timecode.seek_target(frame, self.fps)

    # --------------------------------------------------------------- 재생 제어

    @property
    def has_file(self) -> bool:
        return self.path is not None

    @property
    def can_step(self) -> bool:
        """프레임 단위 이동이 성립하는가. 인덱스가 있거나 fps 를 알거나."""
        return self.has_file and (self.index is not None or self.fps > 0)

    @property
    def current_frame(self) -> int:
        """지금 기준으로 삼아야 할 프레임 번호.

        seek 이 아직 도착하지 않았다면 화면에 남아 있는 프레임이 아니라
        '가고 있는 프레임'이 기준이다.
        """
        return self.frame if self._pending_frame is None else self._pending_frame

    def toggle_pause(self) -> None:
        if not self.has_file:
            return
        self.set_paused(not bool(self._get("pause", True)))

    def pause(self) -> None:
        self.set_paused(True)

    def set_paused(self, paused: bool) -> None:
        """재생/정지를 명시적으로 지정한다.

        비교 모드는 두 인스턴스를 같이 재생/정지시켜야 해서 토글로는 안 된다 —
        한쪽만 어긋나 있으면 토글이 둘을 반대 상태로 만든다.
        """
        if not self.has_file:
            return
        self._mpv.pause = bool(paused)
        self._refresh()

    def set_hwdec(self, value: str) -> None:
        """하드웨어 디코딩 방식을 바꾼다. 파일을 다시 열 필요는 없다.

        **바로 읽으면 안 된다.** mpv 는 디코더를 그 자리에서 갈아 끼우지 않고
        다음 프레임(또는 seek) 때 다시 만든다. 설정 직후 `hwdec-current` 를 읽으면
        아직 이전 값이라, "골랐는데 안 됐다"고 잘못 판단하게 된다.
        (실제로 여기서 걸렸다 — d3d11va 로 되돌렸는데 '이 PC 에서 안 된다'고 떴다)

        그래서 여기서는 값만 넘기고, 표시는 `_refresh()` 가 매 폴링마다 갱신한다.
        """
        try:
            self._mpv.hwdec = value
        except Exception:
            return

    def _hwdec_text(self) -> str:
        """정보 패널에 적을 '실제로 쓰이는' 디코딩 방식.

        고른 값이 아니라 mpv 가 정말 쓰고 있는 것(`hwdec-current`)을 보여 준다.
        고른 게 이 PC 에서 안 되면 mpv 는 조용히 소프트웨어로 물러나기 때문에,
        고른 값을 그대로 적으면 거짓말이 된다. 그럴 때만 이유를 덧붙인다.

        (mpv 의 `hwdec` 는 콤마로 여러 개를 줄 수 있어서 리스트로 돌아온다 —
        그대로 찍으면 `['nvdec']` 이 뜬다)
        """
        current = _as_text(self._get("hwdec_current", "no")) or "no"
        wanted = _as_text(self._get("hwdec", ""))
        # 사용자가 일부러 끈 경우(no)는 "안 돼서 물러났다"가 아니다.
        if wanted and wanted != "no" and current == "no":
            return f"{current}  (요청: {wanted} — 이 PC 에서 안 돼서 소프트웨어로)"
        return current

    def set_mute(self, mute: bool) -> None:
        """소리를 끈다. 비교 모드에서 B 의 소리를 죽이는 데 쓴다 (겹쳐 들리면 성가시다)."""
        try:
            self._mpv.mute = bool(mute)
        except Exception:
            pass

    def step_frames(self, n: int) -> None:
        """n 프레임 이동. 음수면 뒤로.

        앞으로 1프레임일 때만 mpv 의 frame-step 에 맡긴다 (빠르고 정확하다).
        나머지는 전부 목표 프레임을 계산해 절대 시각으로 hr-seek 한다.
        특히 뒤로 이동은 mpv 의 frame-back-step 을 쓰지 않는다 — 고프레임레이트에서
        프레임을 건너뛴 전력이 있다 (mpv issue #7208).
        """
        if not self.has_file or n == 0:
            return

        if n == 1 and self._pending_frame is None:
            self.pause()
            self._mpv.command("frame-step")
            self._refresh()
            return

        if not self.can_step:
            # 인덱스도 fps 도 없으면 프레임 이동 자체가 성립하지 않는다.
            return

        self.seek_to_frame(self.current_frame + n)

    def seek_to_frame(self, frame: int, keep_playing: bool = False) -> None:
        """그 프레임으로 이동한다.

        기본은 정지 상태로 만든다 — 프레임 단위 이동은 멈춰서 보라는 뜻이니까.
        비교 모드의 드리프트 보정만 keep_playing=True 로 재생을 유지한 채 부른다
        (재생 중에 매번 멈췄다 켜면 그림이 튄다).
        """
        if not self.can_step:
            return
        last = max(0, self.frame_count - 1) if self.frame_count else None
        frame = max(0, frame if last is None else min(frame, last))
        if not keep_playing:
            self.pause()
        self._pending_from = self.frame
        self._pending_playing = keep_playing
        self._pending_frame = frame
        self._pending_polls = 0
        self._mpv.command("seek", self._seek_target(frame), "absolute", "exact")
        self._refresh()

    def seek_seconds(self, delta: float) -> None:
        if not self.has_file:
            return
        self._pending_frame = None
        self._pending_polls = 0
        self._mpv.command("seek", delta, "relative", "exact")
        self._refresh()

    # ----------------------------------------------------------------- 갱신

    # seek 이 목표에 도착하기를 기다리는 최대 폴링 횟수. 도착 못 해도
    # (VFR 이라 그 시각에 프레임이 없다든지) 영영 붙잡고 있으면 안 된다.
    PENDING_MAX_POLLS = 20

    def _seek_arrived(self) -> bool:
        """걸어 둔 seek 이 도착했다고 볼 수 있는가.

        **정지 상태**에서는 정확히 그 프레임에 서야 도착이다. 프레임 단위 이동은
        한 칸도 어긋나면 안 되고, 화살표 키를 누르고 있을 때 다음 칸을 목표에서
        세야 하기 때문이다.

        **재생을 유지한 채 건 seek**(구간 반복 · 비교 탭 드리프트 보정)은 다르다.
        폴링(30ms) 사이에 여러 프레임이 지나가서 목표 번호를 그냥 지나쳐 버린다.
        그러면 `==` 는 영영 성립하지 않고, 도착을 못 알아챈 채 최대 20폴링(0.6초)
        동안 `current_frame` 이 목표에 붙박인다 — 구간 반복에서 영상은 도는데
        타임라인만 멈춰 보이던 원인이 이것이었다.
        그래서 재생 중에는 **출발점보다 목표에 더 가까워졌으면 도착**으로 본다.
        """
        target = self._pending_frame
        if self.frame == target:
            return True
        if not self._pending_playing:
            return False
        return abs(self.frame - target) < abs(self.frame - self._pending_from)

    def _refresh(self) -> None:
        if not self.has_file:
            return
        self.time_pos = float(self._get("time_pos", 0.0) or 0.0)
        self.paused = bool(self._get("pause", True))
        # 디코더는 재생 중에도 바뀔 수 있다 (설정 변경, mpv 의 자체 폴백).
        # 다시 읽어야 정보 패널이 거짓말을 하지 않는다 — 다만 초당 30번씩
        # 물어볼 값은 아니다 (속성 조회 2회 × 플레이어 4개). 0.25초에 한 번이면
        # 정보 패널이 늦었다고 느낄 일이 없다.
        if self.video_info:
            self._hwdec_polls += 1
            if self._hwdec_polls >= self.HWDEC_POLL_EVERY:
                self._hwdec_polls = 0
                self.video_info["하드웨어 디코딩"] = self._hwdec_text()
        self.frame = self.frame_at(self.time_pos)
        if self.frame_count:
            self.frame = min(self.frame, self.frame_count - 1)

        if self._pending_frame is not None:
            self._pending_polls += 1
            if self._seek_arrived() or self._pending_polls > self.PENDING_MAX_POLLS:
                self._pending_frame = None
                self._pending_polls = 0
        elif not self.paused:
            # 재생 중에는 기준이 계속 움직인다 — 붙잡아 둘 게 없다.
            self._pending_polls = 0

        self.state_changed.emit()

    def shutdown(self) -> None:
        self._poll.stop()
        try:
            self._mpv.terminate()
        except Exception:
            pass

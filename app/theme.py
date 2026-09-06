"""PlayerX 화면 테마 — 목업(docs/mockups/mockup-20260905-1909.png)의 색을 코드로 옮긴 것.

색을 위젯마다 흩어 놓지 않고 여기 한곳에 모은다. 위젯 코드는 색을 모르고
`objectName` 만 정한다 — 그래야 나중에 톤을 바꿀 때 이 파일만 고치면 된다.

`apply(app)` 을 QApplication 을 만든 직후에 한 번 부르면 된다 (app/main.py).
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


class C:
    """목업에서 뽑은 색. 이름은 쓰임새로 짓는다 (파랑1·파랑2 말고)."""

    BG_APP = "#0d1117"        # 창 바탕
    BG_TITLE = "#11161d"      # 상단 헤더
    BG_PANEL = "#161b22"      # 파일 정보 같은 카드
    BG_ELEV = "#1b212a"       # 버튼·입력칸
    BG_HOVER = "#232b36"
    BG_PRESS = "#151a21"
    BG_VIDEO = "#000000"

    BORDER = "#2a313c"
    BORDER_SOFT = "#21262d"

    TEXT = "#e6edf3"
    TEXT_DIM = "#9aa5b1"
    TEXT_MUTED = "#6e7781"
    TEXT_OFF = "#4d5661"      # 비활성

    ACCENT = "#3b82f6"
    ACCENT_HI = "#5b9bff"
    ACCENT_BG = "rgba(59, 130, 246, 0.12)"
    ACCENT_BG_HI = "rgba(59, 130, 246, 0.20)"

    GREEN = "#3fb950"
    GREEN_BG = "rgba(63, 185, 80, 0.12)"
    RED = "#f85149"
    RED_BG = "rgba(248, 81, 73, 0.12)"
    AMBER = "#d29922"

    CLOSE_HOVER = "#c42b1c"

    # A/B 태그 색 (비교 탭)
    TAG_A = "#d29922"
    TAG_B = "#5b9bff"


FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


def _stylesheet() -> str:
    return f"""
/* ---------------------------------------------------------------- 바탕 */
/* 여기에 `QWidget {{ ... }}` 같은 전체 선택자를 두면 안 된다.
   그 규칙 하나 때문에 모든 위젯의 그리기를 스타일시트 엔진이 떠맡고,
   `background: transparent` 는 위젯을 다시 그릴 때마다 그 뒤의 조상까지
   같이 그리게 만든다. 타임라인 한 번 그리는 데 10ms 가 들던 원인이었다.
   글자색과 글꼴은 팔레트와 앱 기본 글꼴로 준다 (_palette · apply). */
QMainWindow, QDialog {{ background: {C.BG_APP}; }}

/* 창 테두리 — 프레임리스라 우리가 직접 그린다 */
#WindowFrame {{
    background: {C.BG_APP};
    border: 1px solid {C.BORDER};
}}
#WindowFrame[maximized="true"] {{ border: none; }}

/* ---------------------------------------------------------------- 헤더 */
#TitleBar {{
    background: {C.BG_TITLE};
    border-bottom: 1px solid {C.BORDER_SOFT};
}}
#TitleAppName {{ color: {C.TEXT}; font-size: 13.5px; font-weight: 600; }}
#TitleSep     {{ color: {C.BORDER}; font-size: 15px; }}
#TitleFile    {{ color: {C.TEXT_DIM}; font-size: 13px; }}

#WinButton {{
    background: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
}}
#WinButton:hover   {{ background: {C.BG_HOVER}; }}
#WinButton:pressed {{ background: {C.BG_PRESS}; }}
#WinClose {{ background: transparent; border: none; border-radius: 0px; padding: 0px; }}
#WinClose:hover    {{ background: {C.CLOSE_HOVER}; }}
#WinClose:pressed  {{ background: #a32318; }}

/* ---------------------------------------------------------------- 탭 */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {C.BORDER_SOFT};
    background: {C.BG_APP};
    top: -1px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {C.TEXT_DIM};
    padding: 11px 20px;
    margin: 0px 2px -1px 0px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13.5px;
}}
QTabBar::tab:hover    {{ color: {C.TEXT}; }}
QTabBar::tab:selected {{ color: {C.ACCENT_HI}; border-bottom: 2px solid {C.ACCENT}; }}

/* ---------------------------------------------------------------- 버튼 */
QPushButton {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    color: {C.TEXT};
}}
QPushButton:hover    {{ background: {C.BG_HOVER}; border-color: #39424f; }}
QPushButton:pressed  {{ background: {C.BG_PRESS}; }}
QPushButton:disabled {{ background: #14181e; border-color: #23282f; color: {C.TEXT_OFF}; }}

/* 파일 열기처럼 '지금 눌러야 할' 버튼 — 채운 파랑 */
#PrimaryButton {{
    background: {C.ACCENT};
    border: 1px solid {C.ACCENT};
    border-radius: 6px;
    padding: 9px 20px;
    color: #ffffff;
    font-size: 13.5px;
    font-weight: 600;
}}
#PrimaryButton:hover   {{ background: #4c8ffb; border-color: #4c8ffb; }}
#PrimaryButton:pressed {{ background: #2f6fd0; border-color: #2f6fd0; }}

/* 같은 '열기'인데 이미 파일이 있는 자리 — 테두리만 파랑 */
#AccentButton {{
    background: {C.ACCENT_BG};
    border: 1px solid {C.ACCENT};
    border-radius: 6px;
    padding: 6px 14px;
    color: {C.ACCENT_HI};
}}
#AccentButton:hover   {{ background: {C.ACCENT_BG_HI}; }}
#AccentButton:pressed {{ background: {C.BG_PRESS}; }}

/* 파일이 없을 때 영상 자리에 뜨는 안내 */
#EmptyState {{
    background: #0b0f14;
    border: 1px dashed {C.BORDER};
    border-radius: 8px;
}}
#EmptyTitle {{ color: {C.TEXT}; font-size: 16px; font-weight: 600; }}
#EmptyHint  {{ color: {C.TEXT_MUTED}; font-size: 12.5px; }}

/* 헤더의 '열기' — 파일이 열려 있어도 항상 손 닿는 자리 */
#HeaderButton {{
    background: transparent;
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 5px 12px;
    color: {C.TEXT_DIM};
    font-size: 12.5px;
}}
#HeaderButton:hover {{
    background: {C.ACCENT_BG}; border-color: {C.ACCENT}; color: {C.ACCENT_HI};
}}
#HeaderButton:pressed {{ background: {C.BG_PRESS}; }}

/* 재생 조작 버튼 — 아이콘 · 이름 · 단축키가 세로로 쌓인 큰 카드 */
#TransportButton, #TransportAccent, #TransportIn, #TransportOut {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    padding: 0px;
}}
#TransportButton:hover, #TransportIn:hover, #TransportOut:hover {{
    background: {C.BG_HOVER}; border-color: #39424f;
}}
#TransportAccent {{ background: {C.ACCENT_BG}; border-color: {C.ACCENT}; }}
#TransportAccent:hover {{ background: {C.ACCENT_BG_HI}; }}
#TransportButton:pressed, #TransportAccent:pressed,
#TransportIn:pressed, #TransportOut:pressed {{ background: {C.BG_PRESS}; }}
#TransportButton:disabled, #TransportAccent:disabled,
#TransportIn:disabled, #TransportOut:disabled {{
    background: #14181e; border-color: #23282f;
}}

#TransportLabel    {{ color: {C.TEXT};       font-size: 12.5px; }}
#TransportKey      {{ color: {C.TEXT_MUTED}; font-size: 11px; }}
#TransportLabelAcc {{ color: {C.ACCENT_HI};  font-size: 12.5px; }}

/* IN / OUT 칩 */
#ChipIn {{
    color: {C.GREEN}; background: {C.GREEN_BG};
    border: 1px solid {C.GREEN}; border-radius: 5px;
    padding: 3px 12px; font-size: 12px; font-weight: 600;
}}
#ChipOut {{
    color: {C.RED}; background: {C.RED_BG};
    border: 1px solid {C.RED}; border-radius: 5px;
    padding: 3px 12px; font-size: 12px; font-weight: 600;
}}

/* ---------------------------------------------------------------- 영상 */
#VideoFrame {{
    background: {C.BG_VIDEO};
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 6px;
}}
#ViewerBar {{ background: transparent; }}
#ViewerChip {{
    color: {C.TEXT_DIM};
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 5px;
    padding: 3px 9px;
    font-size: 11.5px;
}}

/* ---------------------------------------------------------------- 타임코드 */
#BigTimecode {{
    color: {C.TEXT};
    font-family: "{FONT_MONO}", monospace;
    font-size: 26px;
}}
#FrameCount {{ color: {C.TEXT_DIM}; font-family: "{FONT_MONO}", monospace; font-size: 15px; }}
#MonoLabel  {{ color: {C.TEXT}; font-family: "{FONT_MONO}", monospace; font-size: 14px; }}
#DimLabel   {{ color: {C.TEXT_DIM}; }}
#MutedLabel {{ color: {C.TEXT_MUTED}; font-size: 11.5px; }}
#MarkLabel  {{ color: {C.AMBER}; font-family: "{FONT_MONO}", monospace; font-size: 13px; }}
#SectionLabel {{ color: {C.TEXT_DIM}; font-size: 12px; }}

/* 입력칸 묶음 (Frame · Timecode) */
#FieldBox {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
}}
#FieldName {{ color: {C.TEXT_DIM}; font-size: 12px; }}
#FieldEdit {{
    background: transparent; border: none;
    color: {C.TEXT};
    font-family: "{FONT_MONO}", monospace; font-size: 13.5px;
    padding: 0px;
}}
#FieldEdit:disabled {{ color: {C.TEXT_OFF}; }}

/* ---------------------------------------------------------------- 정보 패널 */
#InfoCard {{
    background: {C.BG_PANEL};
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 8px;
}}
#InfoHeader {{ background: transparent; border-bottom: 1px solid {C.BORDER_SOFT}; }}
#InfoTitle  {{ color: {C.TEXT}; font-size: 13.5px; font-weight: 600; }}
#InfoKey    {{ color: {C.TEXT_MUTED}; font-size: 11.5px; }}
#InfoValue  {{ color: {C.TEXT}; font-size: 13px; }}
#InfoNote   {{ color: {C.TEXT_MUTED}; font-size: 11px; }}
#InfoRule   {{ background: {C.BORDER_SOFT}; }}
#IconButton {{ background: transparent; border: none; border-radius: 4px; padding: 2px; }}
#IconButton:hover {{ background: {C.BG_HOVER}; }}

#BadgeOk {{
    color: {C.GREEN}; background: {C.GREEN_BG};
    border: 1px solid rgba(63, 185, 80, 0.45); border-radius: 10px;
    padding: 3px 10px; font-size: 11.5px;
}}
#BadgeWait {{
    color: {C.AMBER}; background: rgba(210, 153, 34, 0.12);
    border: 1px solid rgba(210, 153, 34, 0.45); border-radius: 10px;
    padding: 3px 10px; font-size: 11.5px;
}}
#TagA {{ color: {C.TAG_A}; font-weight: 700; }}
#TagB {{ color: {C.TAG_B}; font-weight: 700; }}

/* ---------------------------------------------------------------- 입력 위젯 */
QLineEdit, QSpinBox, QComboBox {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    color: {C.TEXT};
    selection-background-color: {C.ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {C.ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: #14181e; color: {C.TEXT_OFF}; border-color: #23282f;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {C.BG_PANEL};
    border: 1px solid {C.BORDER};
    selection-background-color: {C.ACCENT};
    selection-color: #ffffff;
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent; border: none; width: 14px;
}}

/* 체크 표시(∨)는 Fusion 이 팔레트를 보고 그리게 둔다. 여기서 indicator 를
   직접 칠하면 배경만 파랗게 차고 체크 표시가 사라져서 켜졌는지 알기 어렵다. */
QCheckBox {{ color: {C.TEXT}; spacing: 7px; }}

/* ---------------------------------------------------------------- 슬라이더 */
QSlider::groove:horizontal {{
    height: 4px; background: {C.BORDER}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {C.ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {C.TEXT}; width: 12px; height: 12px;
    margin: -4px 0px; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}

/* ---------------------------------------------------------------- 진행률 */
QProgressBar {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 4px;
    text-align: center;
    color: {C.TEXT_DIM};
    font-size: 11px;
}}
QProgressBar::chunk {{ background: {C.ACCENT}; border-radius: 3px; }}

/* ---------------------------------------------------------------- 표 */
QTableWidget {{
    background: {C.BG_PANEL};
    alternate-background-color: #12171e;
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 8px;
    gridline-color: {C.BORDER_SOFT};
    selection-background-color: {C.ACCENT_BG_HI};
    selection-color: {C.TEXT};
    outline: none;
}}
QTableWidget::item {{ padding: 4px 6px; border: none; }}
QTableWidget::item:selected {{ background: {C.ACCENT_BG_HI}; color: {C.TEXT}; }}
QHeaderView::section {{
    background: {C.BG_ELEV};
    color: {C.TEXT_DIM};
    border: none;
    border-right: 1px solid {C.BORDER_SOFT};
    border-bottom: 1px solid {C.BORDER_SOFT};
    padding: 6px 8px;
    font-size: 12px;
}}
QTableCornerButton::section {{ background: {C.BG_ELEV}; border: none; }}

/* ---------------------------------------------------------------- 메뉴 */
QMenu {{
    background: {C.BG_PANEL};
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 26px 6px 22px; border-radius: 5px; color: {C.TEXT}; }}
QMenu::item:selected {{ background: {C.ACCENT}; color: #ffffff; }}
QMenu::item:disabled {{ color: {C.TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {C.BORDER_SOFT}; margin: 5px 8px; }}

/* ---------------------------------------------------------------- 스크롤바 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
QScrollBar::handle:vertical {{
    background: #333b47; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #414a58; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
QScrollBar::handle:horizontal {{
    background: #333b47; border-radius: 5px; min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}

/* ---------------------------------------------------------------- 기타 */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:hover {{ background: {C.BORDER}; }}

QToolTip {{
    background: {C.BG_PANEL};
    color: {C.TEXT};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
    padding: 6px 8px;
}}

#StatusStrip {{
    background: {C.BG_TITLE};
    border-top: 1px solid {C.BORDER_SOFT};
}}
#StatusText {{ color: {C.TEXT_DIM}; font-size: 12px; }}
"""


def _palette() -> QPalette:
    """QSS 가 닿지 않는 곳(네이티브 대화상자 등)까지 어둡게 맞춘다."""
    p = QPalette()
    bg, panel, text = QColor(C.BG_APP), QColor(C.BG_PANEL), QColor(C.TEXT)
    p.setColor(QPalette.Window, bg)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor(C.BG_ELEV))
    p.setColor(QPalette.AlternateBase, panel)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor(C.BG_ELEV))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.ToolTipBase, panel)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Highlight, QColor(C.ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor(C.ACCENT_HI))
    p.setColor(QPalette.PlaceholderText, QColor(C.TEXT_MUTED))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(C.TEXT_OFF))
    return p


def apply(app: QApplication) -> None:
    """QApplication 하나에 테마를 입힌다."""
    # Fusion 을 쓰는 이유: 윈도우 기본 스타일은 QSS 로 못 바꾸는 부분(콤보 화살표·
    # 체크 표시 등)을 시스템 테마대로 밝게 그린다. Fusion 은 팔레트를 그대로 따른다.
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont(FONT_UI)
    font.setPixelSize(13)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(_stylesheet())


def dark_titlebar(widget) -> None:
    """윈도우 창틀을 다크로 — 창틀이 있는 창(대화상자)에만 뜻이 있다.

    실패해도 그냥 밝은 창틀로 남을 뿐이라 조용히 넘어간다.
    """
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (구버전 윈10 은 19)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return
    except Exception:
        pass

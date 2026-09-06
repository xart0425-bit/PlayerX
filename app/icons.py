"""아이콘 — 파일이 아니라 코드로 그린다.

목업의 아이콘은 전부 단순한 선/삼각형이라 QPainter 로 직접 그리는 편이
낫다. 이유가 셋이다.

* PyInstaller 로 묶을 때 챙길 리소스가 늘지 않는다 (지금도 DLL 때문에 복잡하다).
* 색을 인자로 받으므로 활성/비활성/강조를 같은 그림 하나로 낸다.
* 화면 배율(DPI)이 얼마든 그 배율로 그려서 흐려지지 않는다.

좌표는 전부 24x24 기준으로 쓰고, 마지막에 원하는 크기로 스케일한다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_BOX = 24.0


# ------------------------------------------------------------------ 그리기

def _tri(painter: QPainter, points: list[tuple[float, float]], color: QColor) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(QPointF(x, y))
    path.closeSubpath()
    painter.fillPath(path, color)


def _bar(painter: QPainter, x: float, y: float, w: float, h: float,
         color: QColor, radius: float = 0.8) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), radius, radius)
    painter.fillPath(path, color)


def _draw(name: str, painter: QPainter, color: QColor) -> None:
    pen = QPen(color)
    pen.setWidthF(1.9)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    if name == "play":
        _tri(painter, [(7, 4.5), (19.5, 12), (7, 19.5)], color)

    elif name == "pause":
        _bar(painter, 7, 4.5, 3.6, 15, color, 1.2)
        _bar(painter, 13.4, 4.5, 3.6, 15, color, 1.2)

    elif name == "step_back":
        _tri(painter, [(17, 4.5), (17, 19.5), (5.5, 12)], color)

    elif name == "step_fwd":
        _tri(painter, [(7, 4.5), (18.5, 12), (7, 19.5)], color)

    elif name == "skip_back":
        _bar(painter, 4.5, 5, 2.4, 14, color, 1.0)
        _tri(painter, [(20, 5), (20, 19), (9, 12)], color)

    elif name == "skip_fwd":
        _tri(painter, [(4, 5), (15, 12), (4, 19)], color)
        _bar(painter, 17.1, 5, 2.4, 14, color, 1.0)

    elif name == "camera":
        painter.drawRoundedRect(QRectF(3, 7, 18, 13.5), 2.6, 2.6)
        painter.drawEllipse(QPointF(12, 13.7), 3.6, 3.6)
        path = QPainterPath(QPointF(8.6, 7))
        path.lineTo(10.3, 4)
        path.lineTo(13.7, 4)
        path.lineTo(15.4, 7)
        painter.drawPath(path)

    elif name == "scissors":
        painter.drawEllipse(QPointF(6, 18), 3.0, 3.0)
        painter.drawEllipse(QPointF(6, 6), 3.0, 3.0)
        painter.drawLine(QPointF(20, 4), QPointF(8.1, 15.9))
        painter.drawLine(QPointF(14.6, 13.5), QPointF(20, 20))
        painter.drawLine(QPointF(8.1, 8.1), QPointF(11.2, 10.8))

    elif name == "columns":
        painter.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2.2, 2.2)
        painter.drawLine(QPointF(12, 4.5), QPointF(12, 19.5))

    elif name == "chevron_left":
        painter.drawPolyline([QPointF(15, 5.5), QPointF(9, 12), QPointF(15, 18.5)])

    elif name == "chevron_right":
        painter.drawPolyline([QPointF(9, 5.5), QPointF(15, 12), QPointF(9, 18.5)])

    elif name == "chevron_up":
        painter.drawPolyline([QPointF(5.5, 15), QPointF(12, 8.5), QPointF(18.5, 15)])

    elif name == "chevron_down":
        painter.drawPolyline([QPointF(5.5, 9), QPointF(12, 15.5), QPointF(18.5, 9)])

    elif name == "plus":
        painter.drawLine(QPointF(12, 5.5), QPointF(12, 18.5))
        painter.drawLine(QPointF(5.5, 12), QPointF(18.5, 12))

    elif name == "menu":
        for y in (7.5, 12, 16.5):
            painter.drawLine(QPointF(4.5, y), QPointF(19.5, y))

    elif name == "folder":
        path = QPainterPath(QPointF(3.5, 19))
        path.lineTo(3.5, 6)
        path.lineTo(10, 6)
        path.lineTo(12, 8.5)
        path.lineTo(20.5, 8.5)
        path.lineTo(20.5, 19)
        path.closeSubpath()
        painter.drawPath(path)

    # --- 창 조작 (12x12 안쪽에 그린다 — 윈도우 관례에 맞춘 얇은 선) ---
    elif name == "win_min":
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawLine(QPointF(7, 12), QPointF(17, 12))

    elif name == "win_max":
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawRect(QRectF(7.5, 7.5, 9, 9))

    elif name == "win_restore":
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawRect(QRectF(7, 9, 8, 8))
        painter.drawPolyline([QPointF(9.6, 9), QPointF(9.6, 6.6),
                              QPointF(17.4, 6.6), QPointF(17.4, 14.4),
                              QPointF(15, 14.4)])

    elif name == "win_close":
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.drawLine(QPointF(7.5, 7.5), QPointF(16.5, 16.5))
        painter.drawLine(QPointF(16.5, 7.5), QPointF(7.5, 16.5))

    elif name == "logo":
        path = QPainterPath()
        path.addRoundedRect(QRectF(1.5, 1.5, 21, 21), 6.0, 6.0)
        painter.fillPath(path, color)
        _tri(painter, [(9, 6.8), (18, 12), (9, 17.2)], QColor("#ffffff"))

    else:                                    # 모르는 이름이면 아무것도 안 그린다
        return


# ------------------------------------------------------------------ 만들기

_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def pixmap(name: str, color: str, size: int = 20, dpr: float = 2.0) -> QPixmap:
    key = (name, color, size, dpr)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    # devicePixelRatio 를 준 픽스맵의 그리기 좌표는 이미 논리 좌표다 —
    # 여기에 dpr 을 또 곱하면 그림이 두 배로 커져서 잘린다.
    painter.scale(size / _BOX, size / _BOX)
    _draw(name, painter, QColor(color))
    painter.end()

    _cache[key] = pm
    return pm


def icon(name: str, color: str, size: int = 20) -> QIcon:
    return QIcon(pixmap(name, color, size))

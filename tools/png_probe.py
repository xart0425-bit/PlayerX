"""검증용 PNG 판독기 — 헤더와 픽셀을 직접 읽는다.

Pillow 를 안 쓰는 이유: **16bit RGB PNG 를 제대로 못 읽는다.** 그게 우리가
확인해야 할 바로 그 형식이라, 검증 도구가 못 읽으면 검증이 안 된다.

PNG 는 구조가 단순해서 직접 읽는 편이 낫다 — 청크를 훑어 IHDR 로 형식을 알고,
IDAT 을 zlib 로 풀고, 행 필터만 되돌리면 픽셀이 나온다.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_TYPE_RGB = 2


def read_png_header(path: str | Path) -> dict:
    """IHDR 만 읽는다 — 해상도, 채널당 비트 수, 컬러 타입."""
    raw = Path(path).read_bytes()
    if raw[:8] != PNG_SIGNATURE:
        raise ValueError("PNG 시그니처가 아닙니다.")
    length, tag = struct.unpack(">I4s", raw[8:16])
    if tag != b"IHDR":
        raise ValueError("첫 청크가 IHDR 이 아닙니다.")
    w, h, depth, color_type = struct.unpack(">IIBB", raw[16:26])
    return {"width": w, "height": h, "bit_depth": depth, "color_type": color_type}


def read_png_row_median(path: str | Path, y: int = 0) -> tuple[np.ndarray, int]:
    """한 행의 RGB 중앙값과 채널당 비트 수.

    픽셀 하나가 아니라 한 행의 중앙값을 쓴다. 단색 프레임이라도 인코딩 과정에
    디더링이 끼면 픽셀 하나는 ±몇 씩 흔들리기 때문이다. 중앙값은 안 흔들린다.
    """
    raw = Path(path).read_bytes()
    if raw[:8] != PNG_SIGNATURE:
        raise ValueError("PNG 시그니처가 아닙니다.")

    pos, header, idat = 8, None, bytearray()
    while pos < len(raw):
        length, tag = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length

    if header is None:
        raise ValueError("IHDR 이 없습니다.")
    w, h, depth, color_type = header[0], header[1], header[2], header[3]
    if color_type != COLOR_TYPE_RGB:
        raise ValueError(f"RGB(2) PNG 가 아닙니다: color_type={color_type}")

    channels, bpc = 3, depth // 8
    stride = w * channels * bpc
    step = channels * bpc
    data = zlib.decompress(bytes(idat))

    # 행 필터 해제. 각 행 앞에 필터 종류 1바이트가 붙어 있다.
    prev = bytearray(stride)
    line = prev
    for row in range(min(y + 1, h)):
        ftype = data[row * (stride + 1)]
        line = bytearray(data[row * (stride + 1) + 1:(row + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - step] if i >= step else 0
            b = prev[i]
            c = prev[i - step] if i >= step else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        prev = line

    dtype = np.dtype(">u2") if bpc == 2 else np.dtype("u1")
    px = np.frombuffer(bytes(line), dtype=dtype).reshape(w, channels).astype(np.int64)
    return np.median(px, axis=0), depth

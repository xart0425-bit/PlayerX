"""2단계 검증 — PTS 인덱스와 무손실 캡쳐가 실제로 맞는지 확인한다.

GUI 없이 app/frame_index.py 와 app/capture.py 를 그대로 돌린다.
검사하는 것:

  A. **인덱스** — 프레임 수, 시각이 단조 증가, CFR/VFR 판정, PTS 표가 실제
     디코딩 결과와 일치하는가 (앞뒤 몇 프레임을 직접 디코딩해 대조)
  B. **캐시 왕복** — 저장했다 읽은 인덱스가 원본과 같은가, 파일이 바뀌면
     캐시를 버리는가
  C. **PNG 형식** — 원본 해상도가 나오는가, 10bit 소스가 16bit PNG 로 가는가
  D. **프레임 동일성** — 캡쳐한 PNG 가 정말 그 번호의 프레임인가
     (tools/make_testdata.py 로 만든 파일에서만. 프레임 색에 번호가 박혀 있다)
  E. **mpv 착지** — 인덱스가 계산한 seek 목표로 보내면 그 프레임에 도착하는가
     (--with-mpv 를 줬을 때만. libmpv 를 창 없이 띄운다)

    python tools/verify_index_capture.py <영상> [<영상> ...] [--with-mpv]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import make_testdata  # noqa: E402  (색에 심은 프레임 번호를 되읽는 규칙을 공유)
from png_probe import read_png_header, read_png_row_median  # noqa: E402

from app import capture as capture_mod  # noqa: E402
from app import frame_index as fi  # noqa: E402

SETTLE_TIMEOUT = 8.0


# ------------------------------------------------------------------- 검사들

def check_index(index: fi.FrameIndex, path: Path) -> list[str]:
    """A. 인덱스 자체의 앞뒤가 맞는가 + 실제 디코딩 결과와 대조."""
    fails = []
    print(f"    프레임 {index.count:,}개 · {index.fps:.6g} fps · "
          f"{'CFR' if index.cfr else 'VFR'} · {index.pix_fmt} {index.bit_depth}bit")
    print(f"    색: matrix={index.color['matrix']}"
          f"{'' if index.color['matrix_declared'] else '(관례)'}"
          f"  range={index.color['range']}"
          f"{'' if index.color['range_declared'] else '(관례)'}")

    if index.count < 2:
        return [f"{path.name}: 프레임이 2개 미만"]
    if index.times[0] != 0.0:
        fails.append(f"{path.name}: 첫 프레임 시각이 0 이 아님 ({index.times[0]})")
    bad = [i for i in range(index.count - 1) if index.times[i + 1] <= index.times[i]]
    if bad:
        fails.append(f"{path.name}: 시각이 증가하지 않는 지점 {len(bad)}곳 (예: {bad[:3]})")
    if not index.keyframes or index.keyframes[0] != 0:
        fails.append(f"{path.name}: 첫 프레임이 키프레임이 아님")

    # PTS 표가 실제 디코딩 결과와 같은가 — 앞 20 프레임을 순서대로 디코딩해 대조.
    import av

    with av.open(str(path)) as c:
        s = c.streams.video[0]
        decoded = []
        for frame in c.decode(s):
            if frame.pts is not None:
                decoded.append(frame.pts)
            if len(decoded) >= 20:
                break
    want = index.pts[:len(decoded)]
    if decoded != want:
        fails.append(f"{path.name}: 인덱스 PTS 가 디코딩 PTS 와 다름 "
                     f"(인덱스 {want[:5]} vs 디코딩 {decoded[:5]})")
    else:
        print(f"    앞 {len(decoded)} 프레임 PTS 가 디코딩 결과와 일치")
    return fails


def check_cache_roundtrip(index: fi.FrameIndex, path: Path) -> list[str]:
    """B. 저장 -> 로드 왕복이 손실 없이 되는가, 원본이 바뀌면 버리는가."""
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        shutil.copy2(path, copy)

        saved = fi.save_index(index, copy)
        if saved is None or not saved.is_file():
            return [f"{path.name}: 인덱스를 저장하지 못함"]
        if saved.name != copy.name + ".idx.json":
            fails.append(f"{path.name}: 캐시 파일명이 규칙과 다름 ({saved.name})")

        t0 = time.perf_counter()
        loaded = fi.load_index(copy)
        elapsed = time.perf_counter() - t0
        if loaded is None:
            return fails + [f"{path.name}: 저장한 인덱스를 다시 읽지 못함"]

        print(f"    캐시 {saved.stat().st_size:,} 바이트 · 로드 {elapsed * 1000:.0f} ms")
        if loaded.pts != index.pts:
            fails.append(f"{path.name}: 왕복 후 PTS 가 달라짐")
        if loaded.keyframes != index.keyframes:
            fails.append(f"{path.name}: 왕복 후 키프레임 목록이 달라짐")
        if loaded.color != index.color:
            fails.append(f"{path.name}: 왕복 후 색 정보가 달라짐")
        if (loaded.width, loaded.height, loaded.bit_depth) != (
                index.width, index.height, index.bit_depth):
            fails.append(f"{path.name}: 왕복 후 스트림 정보가 달라짐")
        if loaded.cfr != index.cfr:
            fails.append(f"{path.name}: 왕복 후 CFR/VFR 판정이 달라짐")

        # 원본을 건드리면 캐시를 버려야 한다 (mtime 이 바뀐다).
        with copy.open("r+b") as f:
            f.seek(0, 2)
            f.write(b"\x00")
        if fi.load_index(copy) is not None:
            fails.append(f"{path.name}: 원본이 바뀌었는데 캐시를 그대로 씀")
        else:
            print("    원본이 바뀌면 캐시를 버린다 — 확인")
    return fails


def naive_decode(path: Path, wanted: list[int]) -> dict[int, object]:
    """파일 맨 앞부터 순서대로 디코딩해서 n 번째로 나온 프레임을 모은다.

    **탐색을 전혀 쓰지 않는 독립 경로다.** 캡쳐(키프레임으로 seek 후 전진 디코딩)가
    이것과 같은 프레임을 집는지 비교하기 위한 기준선이다. 둘이 같은 코드를 쓰면
    검증이 아니라 자기 확인이 되므로, 여기서는 인덱스도 캡쳐 코드도 안 쓴다.
    """
    import av

    need = set(wanted)
    got: dict[int, object] = {}
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        n = 0
        for frame in c.decode(stream):
            if frame.pts is None:
                continue
            if n in need:
                got[n] = (frame.pts, plane_bytes(frame))
            n += 1
            if len(got) == len(need):
                break
    return got


def plane_bytes(frame) -> tuple[bytes, ...]:
    """프레임의 원본 평면 바이트. to_ndarray() 를 안 쓰는 이유는 두 가지다.

    * PyAV 가 yuv444p10le 같은 포맷을 numpy 로 못 바꾼다.
    * 평면 바이트를 그대로 비교하는 게 "같은 프레임인가"에 대한 더 강한 증거다
      (색 변환을 한 번도 안 거친 값이다).
    """
    return tuple(bytes(p) for p in frame.planes)


def check_capture(index: fi.FrameIndex, path: Path, out_dir: Path) -> list[str]:
    """C+D. PNG 형식이 맞는가, 그리고 정말 그 프레임인가.

    "정말 그 프레임인가"는 두 가지로 확인한다.
      D1. 인덱스가 말하는 n 번 PTS 가, 맨 앞부터 세어 n 번째로 나온 프레임의
          PTS 와 같은가 (프레임 '번호'가 맞는가)
      D2. 캡쳐가 꺼낸 픽셀이 그 프레임의 픽셀과 **바이트 단위로 같은가**
          (탐색이 엉뚱한 프레임을 집지 않았는가)
    합성 파일이면 PNG 픽셀에서 프레임 번호까지 되읽어 한 번 더 본다.
    """
    fails = []
    want_depth = 16 if index.bit_depth > 8 else 8
    targets = sorted({0, 1, 7, index.count // 3, index.count // 2, index.count - 1})
    targets = [t for t in targets if 0 <= t < index.count]

    reference = naive_decode(path, targets)
    # 프레임 색에 번호가 박힌 합성 파일인지 (make_testdata.py 산출물)
    synthetic = path.name.startswith(("test_10bit", "test_vfr"))

    with capture_mod.FrameCapturer(path, index) as cap:
        for want in targets:
            out = out_dir / f"{path.stem}_{want:06d}.png"
            result = cap.save_png(want, out)
            head = read_png_header(out)

            ok = True
            if (head["width"], head["height"]) != (index.width, index.height):
                fails.append(f"{path.name} f{want}: 해상도 {head['width']}x{head['height']}"
                             f" != 원본 {index.width}x{index.height}")
                ok = False
            if head["bit_depth"] != want_depth:
                fails.append(f"{path.name} f{want}: PNG {head['bit_depth']}bit"
                             f" (소스 {index.bit_depth}bit 이면 {want_depth}bit 이어야 함)")
                ok = False
            if head["color_type"] != 2:
                fails.append(f"{path.name} f{want}: color_type={head['color_type']} (RGB 아님)")
                ok = False
            if not result.exact:
                fails.append(f"{path.name} f{want}: 목표 PTS 를 정확히 집지 못함")
                ok = False

            # D1 — 인덱스의 번호 매김이 순차 디코딩과 같은가
            ref = reference.get(want)
            if ref is None:
                fails.append(f"{path.name} f{want}: 순차 디코딩으로 그 프레임에 닿지 못함")
                ok = False
            else:
                ref_pts, ref_pixels = ref
                if ref_pts != index.pts[want]:
                    fails.append(f"{path.name} f{want}: 인덱스 PTS {index.pts[want]} "
                                 f"!= 순차 디코딩 {want}번째 PTS {ref_pts}")
                    ok = False
                # D2 — 캡쳐가 집은 픽셀이 그 프레임과 바이트 단위로 같은가
                grabbed, _ = cap.decode_frame(want)
                if plane_bytes(grabbed) != ref_pixels:
                    fails.append(f"{path.name} f{want}: 캡쳐가 집은 픽셀이 "
                                 f"순차 디코딩 결과와 다름")
                    ok = False

            note = ""
            if synthetic:
                px, depth = read_png_row_median(out)
                got = _decode_frame_number(px, depth)
                note = f"  픽셀이 말하는 번호={got}"
                if got != want:
                    fails.append(f"{path.name} f{want}: 캡쳐한 그림이 {got} 번 프레임")
                    ok = False

            print(f"    {'OK ' if ok else '!! '} f{want:>6} -> "
                  f"{head['width']}x{head['height']} {head['bit_depth']}bit "
                  f"{out.stat().st_size:,}B{note}")
    return fails


def _decode_frame_number(px: np.ndarray, depth: int) -> int:
    """make_testdata.py 가 색에 심어 둔 프레임 번호를 되읽는다.

    거기서는 64진수 두 자리를 STEP 배로 벌려 R/G 에 넣고, 16bit 파일이면
    8bit 값 v 를 v*257 로 올려 넣었다. 그대로 되돌린다 — 눈금이 STEP 이라
    YUV 왕복에서 생기는 ±1 오차는 반올림에 흡수된다.
    """
    scale = (257.0 if depth == 16 else 1.0) * make_testdata.STEP
    hi = int(round(float(px[0]) / scale))
    lo = int(round(float(px[1]) / scale))
    return (hi << make_testdata.DIGIT_BITS) | lo


def check_mpv_landing(index: fi.FrameIndex, path: Path) -> list[str]:
    """E. 인덱스가 낸 seek 목표로 보내면 mpv 가 그 프레임에 도착하는가."""
    from app.mpv_loader import load_mpv
    from app.player import LOAD_TIMEOUT, MPV_OPTIONS

    mpv = load_mpv()
    options = dict(MPV_OPTIONS)
    options["vo"] = "null"
    options["ao"] = "null"
    options.pop("profile", None)
    m = mpv.MPV(**options)
    fails = []
    try:
        m.play(str(path))
        m.wait_for_property("video-params", lambda v: bool(v), timeout=LOAD_TIMEOUT)
        m.pause = True

        targets = sorted({0, 1, 2, 7, index.count // 3, index.count // 2,
                          index.count - 2, index.count - 1})
        targets = [t for t in targets if 0 <= t < index.count]
        for want in targets:
            m.command("seek", index.seek_target(want), "absolute", "exact")
            _settle(m)
            got = index.frame_at_time(float(m.time_pos or 0.0))
            mark = "OK " if got == want else "!! "
            print(f"    {mark} 목표 {want:>7} -> 도착 {got:>7}   "
                  f"time-pos={float(m.time_pos):.6f}  표={index.time_of_frame(want):.6f}")
            if got != want:
                fails.append(f"{path.name}: {want} 로 보냈는데 {got} 에 도착")

        # 뒤로 1프레임씩 — 이 프로젝트가 존재하는 이유
        start = min(index.count - 1, max(30, index.count // 2))
        m.command("seek", index.seek_target(start), "absolute", "exact")
        _settle(m)
        cursor = index.frame_at_time(float(m.time_pos or 0.0))
        trace = [cursor]
        for _ in range(min(30, cursor)):
            cursor -= 1
            m.command("seek", index.seek_target(cursor), "absolute", "exact")
            _settle(m)
            got = index.frame_at_time(float(m.time_pos or 0.0))
            trace.append(got)
            if got != cursor:
                fails.append(f"{path.name}: 뒤로 이동 {cursor} 로 가려 했는데 {got}")
        deltas = sorted({trace[i] - trace[i + 1] for i in range(len(trace) - 1)})
        print(f"    뒤로 {len(trace) - 1} 스텝: {trace[0]} -> {trace[-1]}  간격 집합 {deltas}")
    finally:
        m.terminate()
    return fails


def _settle(m) -> None:
    deadline = time.monotonic() + SETTLE_TIMEOUT
    stable, last = 0, object()
    while time.monotonic() < deadline:
        if not bool(m.seeking):
            now = m.time_pos
            if now == last:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable, last = 0, now
        time.sleep(0.01)


# --------------------------------------------------------------------- 실행

def verify(path: Path, out_dir: Path, with_mpv: bool) -> list[str]:
    print(f"\n=== {path.name} ===")
    fails: list[str] = []

    t0 = time.perf_counter()
    reported: list[tuple[float, int]] = []
    index = fi.build_index(path, progress=lambda r, n: reported.append((r, n)))
    build_s = time.perf_counter() - t0
    rate = index.count / build_s if build_s > 0 else 0
    print(f"  [A] 인덱스  ({build_s:.2f}s · 초당 {rate:,.0f} 프레임 · "
          f"진행률 콜백 {len(reported)}회)")
    fails += check_index(index, path)

    print("  [B] 캐시 왕복")
    fails += check_cache_roundtrip(index, path)

    print("  [C/D] 캡쳐")
    fails += check_capture(index, path, out_dir)

    if with_mpv:
        print("  [E] mpv 착지")
        fails += check_mpv_landing(index, path)
    return fails


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    with_mpv = "--with-mpv" in argv
    if not args:
        print(__doc__)
        return 1

    out_dir = Path(__file__).resolve().parent.parent / "shots" / "verify"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_fails: list[str] = []
    for raw in args:
        p = Path(raw)
        if not p.is_file():
            all_fails.append(f"{raw}: 파일이 없습니다")
            continue
        try:
            all_fails += verify(p, out_dir, with_mpv)
        except Exception as exc:
            all_fails.append(f"{p.name}: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 62)
    if all_fails:
        print(f"실패 {len(all_fails)}건:")
        for f in all_fails:
            print(f"  - {f}")
        return 1
    print("전부 통과 — 인덱스가 정확하고, 캡쳐한 PNG 가 그 프레임의 원본입니다.")
    print(f"캡쳐 결과: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""vendor/ffmpeg.exe 를 받아 놓는다.

libmpv 와 같은 취급이다 — 100MB 넘는 네이티브 바이너리라 저장소에 넣지 않고,
필요할 때 이 스크립트로 받는다.

**LGPL 빌드를 받는다.** 계획서 라이선스 표의 판단을 따른 것이다:

    FFmpeg / ffmpeg.exe | LGPLv2.1+ (코어) / --enable-gpl 빌드는 GPL
    -> 디코딩·remux 만 쓰면 LGPL 빌드로 충분. x264 등이 들어간 GPL 빌드를
       동봉하면 GPL 이 전파된다.

무손실 컷(`-c copy`)은 인코더가 필요 없으니 LGPL 빌드로 충분하다. 정확 컷
(재인코딩)은 이 빌드에 들어 있는 인코더 중에서 고른다 — app/export.py 가
`ffmpeg -encoders` 로 실제 목록을 확인해서 쓸 수 있는 것만 UI 에 내놓는다.

    python tools/fetch_ffmpeg.py [--gpl]

--gpl 을 주면 x264/x265 가 든 GPL 빌드를 받는다. 혼자 쓰는 도구라면 문제없지만,
남에게 배포할 생각이 있으면 내 코드까지 GPL 이 된다는 걸 알고 써야 한다.
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

RELEASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
BUILDS = {
    "lgpl": "ffmpeg-master-latest-win64-lgpl.zip",
    "gpl": "ffmpeg-master-latest-win64-gpl.zip",
}

VENDOR = Path(__file__).resolve().parent.parent / "vendor"
WANTED = ("ffmpeg.exe",)          # ffprobe 는 안 쓴다 — 우리는 PyAV 로 파일을 읽는다


def download(url: str) -> bytes:
    print(f"  받는 중: {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        chunks, got, next_mark = [], 0, 10
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if total and got * 100 // total >= next_mark:
                print(f"    {got / 1e6:.0f} / {total / 1e6:.0f} MB", flush=True)
                next_mark += 10
    return b"".join(chunks)


def main(argv: list[str]) -> int:
    flavour = "gpl" if "--gpl" in argv else "lgpl"
    VENDOR.mkdir(parents=True, exist_ok=True)

    target = VENDOR / "ffmpeg.exe"
    if target.is_file() and "--force" not in argv:
        print(f"이미 있습니다: {target} ({target.stat().st_size:,} 바이트)")
        print("다시 받으려면 --force")
        return 0

    print(f"FFmpeg {flavour.upper()} 빌드를 받습니다 (BtbN/FFmpeg-Builds)")
    raw = download(RELEASE + BUILDS[flavour])

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in WANTED:
            member = next((m for m in archive.namelist()
                           if m.endswith(f"/bin/{name}")), None)
            if member is None:
                print(f"압축 안에 {name} 이 없습니다.")
                return 1
            data = archive.read(member)
            out = VENDOR / name
            out.write_bytes(data)
            print(f"  넣음: {out}  ({len(data):,} 바이트)")

    print(f"\n라이선스: {flavour.upper()} 빌드. "
          + ("배포할 생각이면 내 코드도 GPL 이 된다는 걸 기억할 것."
             if flavour == "gpl" else "동적 링크 조건만 지키면 배포에 제약이 없다."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

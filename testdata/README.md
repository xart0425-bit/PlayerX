# 검증용 테스트 영상

프레임 번호가 그림에 노란 글씨로 박혀 있다. 그래서 **앱이 표시하는 프레임 번호와
화면 속 숫자를 눈으로 대조**할 수 있다 — 프레임 정확도를 확인하는 유일하게 확실한 방법.

| 파일 | fps | 프레임 | 컨테이너 | 왜 있는가 |
|---|---|---|---|---|
| `test_60fps.mkv` | 60 | 600 | mkv (타임베이스 1ms) | 기본 케이스. PTS 가 ms 로 양자화돼서 프레임 매핑 오차가 드러난다 |
| `test_120fps.mkv` | 120 | 1200 | mkv (타임베이스 1ms) | 고프레임레이트. mpv issue #7208 이 문제 삼은 조건 |
| `test_23976fps.mp4` | 24000/1001 | 240 | mp4 | 소수 fps + 다른 타임베이스 |

다시 만들려면 (ffmpeg 필요):

```
ffmpeg -f lavfi -i "testsrc=size=1280x720:rate=60:duration=10" \
  -vf "drawtext=fontfile='C\:/Windows/Fonts/consola.ttf':text='%{frame_num}':start_number=0:x=40:y=560:fontsize=140:fontcolor=yellow:box=1:boxcolor=black@0.8" \
  -c:v libx264 -preset ultrafast -crf 16 -pix_fmt yuv420p -g 60 test_60fps.mkv
```

`rate` 와 파일명만 바꾸면 나머지 두 개도 나온다.

# 전원 차단으로 깨진 틱 파일에서 읽기가 멈춘다 — 후속 티켓 (2026-11-09)

## 1. 현상

2026-11-09 09시 틱 파일 `data/strategy_ticks/20261109/267850.09.jsonl.gz`의 앞쪽 gzip 멤버가 깨졌다. 쓰는 중에 PC 전원이
끊긴 탓이다(`docs/RESTART_HOLDING_HIGH_GAP_FOLLOWUP_20261109.md`). 이 파일을 읽는 쪽이 모두 깨진 지점에서 멈췄다.

- 운영 봇: 09:11:15 `TICK_CAPTURE_RESUME_ERROR`·`TICK_CAPTURE_MANIFEST_ERROR`(`EOFError`).
  15:20:00 `TICK_CAPTURE_FINALIZE_ERROR`(`zlib.error` "invalid stored block lengths"). 그래서 이날 `TICK_CAPTURE_FINALIZED`가
  없고, 일일 분석에서 보는 ws 무수신 지표(`max_ws_silence_sec`, `ws_silence_missed_qty` 등)도 없다.
- 보고 스크립트: `scripts/entry_timing_report.py`와 `scripts/ws_frame_check.py`가 `zlib.error`로 끝났다. 일일 분석은 깨진 지점까지만
  읽도록 감싸서 돌렸다.

## 2. 증거

- **데이터는 남아 있다.** 파일 1,174,668바이트 안에 gzip 멤버 머리가 두 개 있다(0, 76,773바이트).
  - 첫 멤버: 1,445행(09:00:18.592 ~ 09:05:27.112)까지 읽히고 깨진다.
  - 둘째 멤버: 재시작 뒤 이어 쓴 것이다. 76,773바이트부터 따로 풀면 24,919행(09:11:15.247 ~ 09:59:59.833)이 온전하다.
- 같은 날 10 ~ 15시 파일 6개는 정상이다.
- **읽는 쪽 예외 처리** (2026-11-09 개발 트리):

| 위치 | 잡는 예외 | 깨진 멤버에서 |
|---|---|---|
| `scripts/entry_timing_report.py` 63행 `_read_gz` | `EOFError`, `OSError` | `zlib.error`로 멈춤 |
| `scripts/ws_frame_check.py` 64행 `_read_frames` | `EOFError` | 멈춤 |
| `scripts/overnight_sieve.py` 131행 | `EOFError` | 멈춤 (추정, 같은 모양) |
| `scripts/trail_near_miss.py` 126행 | `EOFError` | 멈춤 (추정) |
| `scripts/replay_ticks_to_bars.py` 70행 | `ValueError`(줄 단위)만 | 멈춤 (추정) |
| `src/modules/tick_capture.py` 293, 680행 | `OSError`, JSON 오류 | 이날 로그대로 실패 |

- `tick_capture.py`는 `_STRATEGY_FILES`에 없다. 지문 대상이 아니다. `scripts/`도 지문 대상이 아니다.
- 쓰는 쪽은 `gzip.open(path, "at")`으로 이어 쓴다(`tick_capture.py` 417행). 재시작 뒤 이어 쓰기는 깨진 멤버 뒤에 새 멤버를 붙였다.

## 3. 원인

- 파이썬 `gzip`은 멤버를 차례로 푼다. 첫 멤버 안의 압축 블록이 깨지면 `zlib.error`가 나고, 그 뒤 멤버로 건너뛰지 못한다.
- 읽는 쪽은 장중 기록 중인 파일(끝이 잘린 파일, `EOFError`)만 생각하고 짰다. 중간이 깨진 파일은 생각하지 않았다.

## 4. 영향

- 이날 ws 무수신 지표가 없다. 일일 분석 4절의 해당 줄은 이날 비워 둔다.
- 일일 분석 명령이 깨진 파일을 만나면 끝까지 돌지 않는다. 누적 보고(`--since`)도 이날에서 멈춘다.
- 원시 데이터 손실은 첫 멤버의 09:05:27 뒤 부분(전원 차단 직전 몇 초)뿐이다. 나머지는 읽는 방법만 바꾸면 복구된다.
- 같은 일은 보유 중 전원 차단이나 강제 종료 때만 난다. 정상 재시작(승격)에서는 나지 않았다.

## 5. 선택지 (결정 전)

- **가. 멤버 단위로 복구해 읽는 공용 함수를 둔다.** gzip 머리(`1f 8b 08`)를 찾아 멤버마다 따로 풀고, 깨진 멤버는 읽힌 데까지만 쓴다.
  보고 스크립트 5개와 `tick_capture.py` 마감 처리가 같이 쓴다. 지문 대상이 아니다. 이날 마감 지표는 다시 계산할 수 있다.
- **나. 최소 수정: `zlib.error`를 잘린 파일처럼 처리한다.** 스크립트가 멈추지는 않지만 둘째 멤버(이날 09:11 ~ 10:00)는 버린다.
- **다. 쓰는 쪽을 바꾼다.** 재시작 때 이어 쓰기 전에 기존 파일을 검사하고, 깨졌으면 새 파일(예: `267850.09.r1.jsonl.gz`)에 쓴다.
  `tick_capture.py`만 바뀌고 지문 대상이 아니다. 읽는 쪽이 새 이름을 함께 읽어야 한다. 이미 깨진 이날 파일은 가 또는 수동 복구가 필요하다.
- **라. 이날 파일만 손으로 복구하고 둔다.** 두 멤버를 풀어 새 파일로 다시 쓰고 원본은 백업한다. 운영 데이터 폴더를 고치므로
  장 마감 뒤에 한다. 코드는 그대로다.

# F4 청산 중 WS 틱 수신 정지 — 후속 티켓 (2026-09-17)

> 대상 코드: [`src/modules/f4_tracking.py`](../src/modules/f4_tracking.py) `_trigger_close`, `_process_tick`
> 발견: 2026-09-17 운영 로그 분석 (광전자 017900, trade_id 44)

**지금 고치지 않는다.** `f4_tracking.py`는 `src/release.py`의 `_STRATEGY_FILES`에
속해 있어 한 줄만 바꿔도 전략 지문이 리셋된다. 매매 판단에는 영향이 없는 관측
품질 문제이므로, 다음 동결 해제(0단계 재수행) 때 다른 항목과 묶어 처리한다.
근거와 재현 방법을 여기 남긴다.

## 1. 현상

매도 주문 제출부터 체결 확인까지(`orders.ordered_at` ~ `filled_at`) WS 틱을
한 건도 읽지 않는다. 그 사이 도착한 프레임은 소켓 버퍼에 쌓였다가 체결 확인
직후 한꺼번에 처리된다.

`strategy_ticks/<date>/<ticker>.09.jsonl.gz`에서 `source=ws` 행의 `received_at`
간격이 3초를 넘는 구간을 뽑아 `orders`와 대조한 결과, 청산이 있었던 4거래일
모두 주문~체결 창과 정확히 겹친다.

| 거래일 | 종목 | 틱 공백 (`received_at`) | 매도 `ordered_at` → `filled_at` |
|---|---|---|---|
| 20260909 | 052690 | 09:10:55.028 → 09:11:03.458 (8.4s) | 09:10:55.028 → 09:11:03.110 |
| 20260914 | 203650 | 09:20:43.400 → 09:20:49.520 (6.1s) | 09:20:43.402 → 09:20:49.156 |
| 20260916 | 217590 | 09:01:05.931 → 09:01:12.562 (6.6s) | 09:01:05.940 → 09:01:12.436 |
| 20260917 | 017900 | 09:01:11.159 → 09:01:18.186 (7.0s) | 09:01:11.161 → 09:01:17.826 |

20260917의 경우 `source_ts` 09:01:11~09:01:17인 틱 40여 건이 전부
`received_at` 09:01:18.186~18.269로 기록됐다.

## 2. 원인

`_process_tick`이 WS 리더 코루틴 안에서 호출되고, 스탑이 맞으면
`_trigger_close`가 `await asyncio.shield(close_task)`로 청산 태스크 완료를
**인라인 대기**한다 (`f4_tracking.py` 1152~1200행 부근). 리더는 그 동안
`recv()`로 돌아가지 못한다. REST 클라이언트는 `httpx.AsyncClient`라 이벤트
루프 자체는 살아 있다 — 헬스 모니터가 09:01:13.584에 `WS_STALE`을 찍은 것이
그 증거다.

## 3. 영향

- **매매 판단: 없음.** 청산 시작 시점에 `position_status`가 `EXITING`이라
  버퍼링된 틱은 스탑 계산에 쓰이지 않는다.
- **캡처 품질: `received_at` 왜곡.** 청산당 6~8초 구간의 수신 시각이 체결 확인
  시각으로 몰린다. `source_ts`는 온전하므로 거래소 시각 기준 분석은 영향이
  없지만, 수신 지연 분석(`received_at - source_ts`)에서는 이 구간을 제외해야
  한다.
- **가짜 `WS_STALE`.** 청산 중 헬스 모니터가 stale을 감지해 REST 백업이 깨어나
  `valid=False` REST 틱 1건이 캡처에 섞인다 (20260917 09:01:17.801, 10,770).
- **부분체결 시.** 청산 태스크가 끝나면 리더가 재개되므로 `EXITING` 잔여
  추적은 이어진다. 다만 폴링 타임아웃(30초)까지 늘어지면 그만큼 틱을 못 읽는다.

## 4. 수정 방향 (동결 해제 시)

`_trigger_close`가 청산 태스크를 만들고 **대기하지 않고 반환**한다. 리더는
계속 틱을 읽고 캡처에 적재하되, `EXITING`이므로 `_process_tick`은 바로 빠진다.
청산 태스크의 보호(`shield`)와 취소 순서 보장은 `run()`의 `finally`가 이미
`_closing_task`를 기다리므로 그대로 둔다.

바꾸면 함께 확인할 것:

- `F4_CLOSE_CANCEL_REQUESTED` CRIT가 정상 청산 중 거짓으로 뜨지 않는지
  (현재 그 경로가 `_trigger_close`의 `except CancelledError`에 있다).
- `_close_in_progress` 해제 시점 — 지금은 `finally`에서 `close_task.done()`을
  보고 내린다. 대기하지 않으면 `add_done_callback`으로 옮겨야 한다.
- `close_now()` (F3 비상가드 등 외부 호출)는 결과를 기다려야 하므로 기존
  인라인 대기 경로를 남긴다.

## 5. 재측정 방법

수정 배포 후 첫 청산일에 아래를 돌려 공백이 사라졌는지 본다.

```powershell
.\.venv\Scripts\python.exe -c @'
import gzip, json, sqlite3
from datetime import datetime
day, tk = "20260917", "017900"   # 대상 거래일·종목으로 바꾼다
rows = [json.loads(l) for l in gzip.open(f"data/strategy_ticks/{day}/{tk}.09.jsonl.gz", "rt", encoding="utf-8") if l.strip()]
prev = None
for r in rows:
    if r.get("source") != "ws":
        continue
    t = datetime.fromisoformat(r["received_at"])
    if prev and (t - prev).total_seconds() > 3:
        print("gap", prev.time(), "->", t.time(), round((t - prev).total_seconds(), 1))
    prev = t
db = sqlite3.connect("data/db/trading.db")
for row in db.execute("select order_phase, ordered_at, filled_at from orders where ticker=? and ordered_at like ?", (tk, f"{day[:4]}-{day[4:6]}-{day[6:]}%")):
    print(row)
'@
```

통과 조건: `CLOSE_SELL`의 `ordered_at`~`filled_at` 구간에 3초 초과 `gap`이
없고, 같은 구간 로그에 `WS_STALE`이 없다.

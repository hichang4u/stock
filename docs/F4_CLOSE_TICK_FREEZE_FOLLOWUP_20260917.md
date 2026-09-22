# F4 청산 중 WS 틱 수신 정지 — 후속 티켓 (2026-09-17)

> 대상 코드: [`src/modules/f4_tracking.py`](../src/modules/f4_tracking.py) `_trigger_close`, `_process_tick`
> 발견: 2026-09-17 운영 로그 분석 (광전자 017900, trade_id 44)

**처리됨 (2026-09-22, 동결 해제 묶음).** §4의 방향대로 `_trigger_close`가 청산 태스크를
띄우고 바로 돌아오게 바꿨다(`_start_close` + done-callback, `close_now`만 인라인 대기).
같은 묶음에서 지문이 리셋됐다(REAL_TRADING_CHECKLIST 0단계 재수행). §5 재측정은 수정
배포 후 첫 청산일에 한다 — 통과 조건 그대로. 아래는 발견 당시 기록이다.

처음(09-17)엔 관측 품질 문제로 봤으나, 이튿날(09-18) 정지가 27초로 길어지자
**WS 단절과 38초 틱 유실**로 번지는 것을 확인했다(1.1절). 동결 해제 시 우선순위는
그 기준으로 둔다.

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
| 20260918 | 043260 | 09:18:15.837 → 09:18:43.051 (**27.2s**) | 09:18:15.849 → 09:18:42.614 |

20260917의 경우 `source_ts` 09:01:11~09:01:17인 틱 40여 건이 전부
`received_at` 09:01:18.186~18.269로 기록됐다.

### 1.1 2026-09-18 — 정지가 길어지면 단절과 유실로 번진다

모의서버 `inquire-daily-ccld`가 7~8초씩 걸린 날이라 체결 확인 폴링 3회에
27.2초가 걸렸고, 그 길이에서 새 결과가 둘 나왔다.

- 09:18:43에 몰아 처리된 틱이 **정확히 32건**이고 `source_ts`는 09:18:15~22까지만
  있다. 운영 venv의 `websockets 13.1`이 쓰는 legacy 클라이언트(`websockets.connect`
  → `websockets.legacy.client`)의 기본 `max_queue=32`다 — 큐가 차면 라이브러리가
  소켓 읽기를 멈춘다(backpressure). 그 뒤로 도착한 프레임은 큐에도 들어가지 않았다.
- 읽기가 멈추면 pong 프레임도 처리되지 않는다. `kis_ws.py:56`의
  `ping_interval=20, ping_timeout=10` 키프얼라이브가 실패해 09:18:57
  `ConnectionClosedError(Close(code=1011, reason='keepalive ping timeout'))`로
  끊겼고 09:19:00에 재연결됐다.
- 결과적으로 **09:18:22~09:19:00 약 38초분 틱이 캡처에 없다.** 그런데
  `TICK_CAPTURE_FINALIZED`는 `max_ws_outage_sec=2.1`, `data_complete=1`이다 —
  단절 측정(`0fd86e6`)은 `WS_DISCONNECTED`→`WS_CONNECTED`만 재므로, 단절 *이전에*
  큐 포화로 잃은 구간은 보지 못한다. 완전성 플래그의 맹점이다.

즉 청산 확인이 20초를 넘기면(큐 32건이 차는 시간 + 키프얼라이브 30초 창) 이
결함은 관측 품질 문제가 아니라 **WS 단절 유발 결함**이 된다. 우선순위를 그만큼
올려서 본다. 매매 판단에는 여전히 영향이 없다 — 단절 시점에 포지션은 이미
CLOSED였다.

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
- **정지 20초 초과 시 WS 단절 + 틱 유실 (2026-09-18 실측).** 위 1.1절. 폴링
  타임아웃 30초 안에서도 일어난다.
- **`data_complete` 과대 평가.** 큐 포화 유실은 단절 카운터에 잡히지 않는다.
  20260918 행은 `data_complete=1`이지만 09:18:22~09:19:00이 비어 있다. 이 날짜의
  캡처로 수신 지연·연속성을 분석할 때는 그 구간을 제외한다.

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
- 캡처 최종화 시 `received_at` 기준 3초 초과 공백(연결 중)도 `WS_LOSS` 판정에
  넣을지 검토한다. 그래야 큐 포화 유실이 `data_complete`에 반영된다. 이 항목은
  `tick_capture.py`를 건드리므로 같은 동결 해제 창에서 함께 한다.

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
없고, 같은 구간 로그에 `WS_STALE`이 없으며, 청산 후 1분 안에
`keepalive ping timeout` 단절이 없다. 확인 폴링이 20초를 넘긴 날(모의서버
지연일)에 재는 것이 가장 강한 검증이다.

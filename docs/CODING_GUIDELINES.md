# 코딩 가이드라인 — 데일리 갭업 자동매매 시스템

> **버전**: 2.0  
> **최종 수정**: 2026-06-23  
> **적용 범위**: `d:\Private\stock\` 프로젝트 전체

---

## 1. 실행 환경

| 항목 | 값 |
|------|-----|
| 런타임 | Python 3.12+ (단일 프로세스) |
| OS | Windows 11 (Task Scheduler로 프로세스 감시) |
| 비동기 | `asyncio` 단일 이벤트 루프 |
| 스케줄러 | `APScheduler` (AsyncIOScheduler) |
| HTTP | `httpx.AsyncClient` |
| WebSocket | `websockets` |
| DB | `aiosqlite` (SQLite WAL 모드) |
| 환경 변수 | `.env` + `python-dotenv` |
| 알림 | Telegram Bot API |

---

## 1-1. KIS API 클라이언트를 직접 구현하는 이유

위 표의 `httpx.AsyncClient`를 보면 자연스럽게 드는 생각이 있다 — **"KIS 공식/서드파티
라이브러리를 쓰면 더 정확하지 않을까?"** 실제로 반복해서 드는 생각이고, 그래서 여기 적는다.

후보는 둘이다. 한국투자증권 공식 저장소 [`koreainvestment/open-trading-api`](https://github.com/koreainvestment/open-trading-api)
(라이브러리가 아니라 예제 모음이다), 그리고 서드파티 `python-kis`.

**결론: 매매 경로에는 쓰지 않는다.** `src/api/kis_rest.py`·`kis_ws.py`를 계속 직접 관리한다.

### 라이브러리가 실제로 더 나은 점 (인정하고 시작한다)

- **TR 커버리지.** KIS는 TR이 수백 개다. 우리는 열 몇 개만 쓴다. 해외주식·선물옵션까지
  필요해지면 직접 짜는 비용이 진짜로 크다.
- **명세 추적.** 필드 추가·폐기를 남이 따라가 준다.
- **파싱 버그.** 남이 이미 밟은 지뢰를 다시 밟지 않는다.

이 셋은 사실이다. 그런데 아래 이유들이 이긴다.

### 1. 전략 지문 밖에 있다 — 이게 결정적이다

`src/api/kis_rest.py`와 `src/api/kis_ws.py`는 `src/release.py`의 `_STRATEGY_FILES`에
들어 있다. 그런데 `strategy_fingerprint()`가 해시하는 것은 **저장소 파일의 바이트와
환경변수뿐이다. 설치된 패키지 버전은 보지 않는다.**

라이브러리였다면 이렇게 된다.

1. 누가 `pip install -U` 를 한 번 돌린다
2. 재시도 정책이나 타임아웃 기본값이 바뀐다 → **매매 동작이 달라진다**
3. 그런데 지문은 그대로다 → 같은 `experiment_id`로 계속 쌓인다
4. **다른 코드로 만든 결과가 한 표본에 섞인다**

지문이 막으려는 사고가 정확히 이것인데, 라이브러리는 그 사정권 밖에 있다. 파일을 우리가
들고 있으면 한 바이트만 바뀌어도 지문이 돌고 새 실험이 열린다. 그게 맞는 동작이다.

이 문제는 우회할 방법이 없다. 패키지 버전을 지문에 넣더라도 전이 의존성까지 고정해야
하고, 그러면 라이브러리를 쓰는 이점(남이 따라가 주는 것)이 사라진다.

### 2. 유량 예산에 우선순위가 필요하다

PAPER는 **앱 전체가 초당 1건**이다(REAL은 18건). 트랙 A의 09:00~09:11 진입 창과 트랙 B의
분봉 정정이 이 하나를 나눠 쓴다. 그래서 `kis_rest.py`는 요청을 6단계로 세운다.

```
REQUEST_PRIORITY_CRITICAL     0   주문·긴급
REQUEST_PRIORITY_ORDER_STATUS 5   체결 조회
REQUEST_PRIORITY_PRICE       10   현재가
REQUEST_PRIORITY_DEFAULT     20
REQUEST_PRIORITY_VI          30
REQUEST_PRIORITY_BACKGROUND  40   관측(트랙 B 분봉 정정 등)
```

범용 HTTP 래퍼에는 **"이 주문이 저 조회보다 먼저다"라고 말할 자리가 없다.** 그 말을 못 하면
관측 코드가 진입을 밀어낸다. 09:00 진입은 초 단위로 승부가 나는 구간이다.

### 3. 주문 경로의 보장이 KIS 고유하다

- `EGW00201`(유량 초과)을 재시도 대상으로 알고, **GET만 재시도한다.** 주문은 절대 재시도하지
  않는다 — 타임아웃 난 POST를 재전송하는 라이브러리는 **주문을 두 번 낸다.**
- 토큰 만료·transient 오류·유량 초과가 각각 다른 백오프를 쓴다.
- `is_gated_real_buy` — 실계좌 매수를 코드 층위에서 막는다.
- 주문 전송 마감 가드.

라이브러리의 기본 재시도 정책이 우리 주문 안전 규칙과 맞아떨어질 이유가 없고, 어긋났을 때
알아차릴 방법도 없다.

### 4. 감사 기록이 사고 조사의 전부다

모든 호출이 `LATENCY_HIGH`에 `network_ms` / `rate_wait_ms` / `client_setup_ms`를 쪼개
남긴다. 2026-08-28 09:01:19의 1109ms 지연이 네트워크 탓인지 우리 큐 대기 탓인지 즉시
갈렸던 것이 이 분해 덕분이다. 남의 코드 안에서 일어난 대기는 이렇게 못 쪼갠다.

### 5. 같은 판단이 MCP에 대해 이미 기록돼 있다

`docs/PRD.md` §MCP 금지 용도가 같은 논리를 다른 대상에 적용한 것이다.

> 봇 프로세스에서 MCP를 호출하거나 MCP를 F1~F5 실행 경로의 의존성으로 추가하지 않는다.
> **이유: MCP 호출은 `kis_rest.py`의 호출 직렬화, 토큰 갱신, transient 재시도, 주문 전송
> 마감 가드와 DB/JSONL 감사 기록을 우회할 수 있다.**

라이브러리든 MCP든 **`kis_rest.py`를 우회하는 두 번째 KIS 통로**라는 점에서 같다.

### 쓸 수 있는 자리

금지는 **매매 경로**에 한정된다. 봇 프로세스 밖에서 도는 읽기 전용 오프라인 분석에는 써도
된다 — PRD가 MCP에 대해 "장 종료 후 과거 시세 수집"을 허용하는 것과 같은 취급이다. 다만
그 결과를 당일 주문 판단에 직접 주입하지 않는다.

### 이 결론이 바뀔 조건

- 매매 경로가 해외주식·선물옵션 등으로 넓어져 TR 수십 개가 더 필요해질 때. 그때는 위 1~4를
  라이브러리 위에 다시 세울 수 있는지부터 따진다.
- 지문이 코드 파일이 아니라 **실행된 바이트코드 전체**를 잡도록 바뀔 때. 그러면 1번이
  사라진다.

둘 다 아니면 답은 그대로다. `requirements.txt`가 8개인 것은 게으름이 아니라 이 판단의
결과다 — **매매 판단에 관여하는 서드파티가 하나도 없다.**

---

## 2. 프로젝트 구조 원칙

```
src/
├── api/          ← KIS REST/WebSocket 래퍼 (I/O 전담)
├── modules/      ← F1~F5 비즈니스 로직 (순수 로직 전담)
├── utils/        ← logger, time_sync (도구)
├── db.py         ← SQLite 연결 싱글톤
├── state.py      ← 인메모리 전역 상태
├── notifier.py   ← Telegram 알림 큐
└── scheduler.py  ← APScheduler 빌드
```

**규칙**: `modules/` 코드는 `api/`를 직접 import하지 않는다.  
I/O(API 호출)와 로직(판단)은 반드시 분리한다.

---

## 3. 비동기 프로그래밍

- 모든 I/O 함수는 `async def`로 작성한다.
- `time.sleep()` 사용 금지 → `asyncio.sleep()` 사용.
- `threading` 사용 금지 → asyncio 태스크로 처리.
- CPU 집약 연산(백테스트 등)만 예외적으로 `run_in_executor` 허용.

```python
# Python / asyncio → 올바른 비동기 패턴
async def fetch_price(ticker: str) -> float:
    async with httpx.AsyncClient() as client:
        resp = await client.get(...)
    return resp.json()["price"]
```

---

## 4. 전역 상태 관리 (state.py)

- 인메모리 상태는 `src/state.py`의 함수를 통해서만 읽고 쓴다.
- F4 틱 핸들러 내부에서 직접 dict를 수정하는 것은 허용하되,  
  **청산 결정은 반드시 `position_status` 확인 후 atomic하게 처리**한다.

```python
# Python / state.py → 청산 상태 전이 패턴
if not await state.set_exiting(reason):
    return False              # HOLDING이 아님 — 중복 주문 방지

# 전량 체결을 확인한 뒤에만 호출
await state.set_closed(reason)
```

**주의**:
- 상태 전이는 `state.py`의 lock 안에서 수행한다.
- F4의 `HOLDING → EXITING`은 주문 접수 확인 후 수행한다.
- F5의 `HOLDING → EXITING`은 청산 수량 확정 후 첫 주문 전에 수행한다.
- `EXITING → CLOSED`는 전량 체결 확인 후 수행한다.
- 부분·미확인 체결은 `state.set_exit_remaining_qty()`로 잔량을 갱신하고 EXITING을 유지한다.
- CLOSED 전환은 `remaining_qty=0`을 함께 보장한다.
- 당일 EXITING 재시작은 자동 재매도하지 않고 주문·잔고 대사를 요구한다.

---

## 5. DB 접근 (aiosqlite)

- `db.get()`으로 연결을 가져오고, 직접 `aiosqlite.connect()`를 호출하지 않는다.
- 트랜잭션이 필요한 쓰기는 `async with conn.execute(...): await conn.commit()` 패턴.
- SELECT는 `async with conn.execute(...) as cur: rows = await cur.fetchall()`.

```python
# Python / aiosqlite → INSERT 패턴
async def record_trade(ticker: str, entry_price: float) -> int:
    conn = db.get()
    async with conn.execute(
        "INSERT INTO trades (ticker, entry_price) VALUES (?, ?)",
        (ticker, entry_price),
    ) as cur:
        trade_id = cur.lastrowid
    await conn.commit()
    return trade_id
```

---

## 6. F4 Step Trailing 구현 규칙

PRD §3-F4 기준. 아래 수식을 그대로 코드로 옮긴다.

```python
# Python / f4_tracking.py → Step Trailing 핵심 로직
STEP_SIZE  = 0.025   # params에서 로드
STEP_TRAIL = 0.020   # params에서 로드
HARD_STOP  = 0.020   # params에서 로드

def on_tick(price: float) -> None:
    E = state["entry_price"]

    # 스텝 갱신
    pnl = price / E - 1
    s   = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
    if s > state["highest_step"]:
        state["highest_step"] = s
    if state["highest_step"] >= STEP_SIZE:
        state["trailing_active"] = True

    # 고가 갱신
    if price > state["high_price"]:
        state["high_price"] = price

    # 손절 (trailing 미활성 구간에서만)
    if not state["trailing_active"] and price <= E * (1 - HARD_STOP):
        asyncio.create_task(_close("HARD_STOP"))
        return

    # 익절 (Step Trailing)
    if state["trailing_active"]:
        stop = E * (1 + state["highest_step"] - STEP_TRAIL)
        if price <= stop:
            asyncio.create_task(_close("TRAILING"))
```

**규칙**:
- `highest_step`은 절대 감소시키지 않는다.
- `stop_price`는 `high_price`가 아닌 `highest_step`에서 계산한다.
- 청산 주문은 `asyncio.create_task()`로 분리한다 (틱 핸들러 블로킹 금지).
- WS와 REST 가격은 `_handle_price_tick()`에서 공통 처리한다.
- CLOSED 관측 중에는 `live.push_tick()`만 허용하고 `_process_tick()`, VI, 주문 경로는 실행하지 않는다.
- 관측 종료시각과 상태 판단은 `_price_observation_active()` 단일 게이트를 사용한다.

---

## 7. 주문 전송 규칙

- 모든 매도 주문 전송 전에 `state["position_status"] == "HOLDING"` 확인.
- 주문 ID는 `state["order_id"]`에 저장, 중복 전송 방지에 활용.
- 체결 확인은 KIS REST API 폴링 또는 WebSocket 체결 이벤트로 수행.
- 미체결 상태로 모듈별 체결 확인 기한을 초과하면 안전 재시도 또는 수동 확인 경로로 진입.
- 접수된 주문은 `orders`의 PENDING을 기본 상태로 기록하고, DB 기록 실패와 주문 API 실패를 같은 예외 경로에서 처리하지 않는다.
- F5 주문 ID는 주문 발송 직후 DB의 PENDING 주문에 보존해 중복 전송 진단에 활용한다.
- F4는 매도 주문 접수 후, F5는 첫 매도 주문 전에 상태를 EXITING으로 저장해
  프로세스 내부와 재시작 후 중복 청산을 차단한다.
- 매도 재시도 전에는 실제 잔고를 다시 조회한다. 잔고 조회 실패 상태에서 맹목적으로 재주문하지 않는다.
- 잔고 조회 결과는 `None`과 `0`을 구분한다. `None`은 조회 실패, `0`은 실제 미보유이며 `value or fallback` 패턴을 사용하지 않는다.
- 부분체결은 누적 체결수량이 요청수량에 도달할 때까지 기다리고, 타임아웃 뒤에는 실제 잔량만 후속 주문한다.
- 전량 체결과 체결가를 확인하기 전에는 `trades`를 CLOSED로 확정하지 않는다. 체결가 미확인은 긴급 이벤트와 수동 대조 대상으로 남긴다.
- 여러 주문으로 청산할 때 거래 단위 청산가는 체결금액/체결수량 기준 가중평균을 사용한다.
- F4는 부분·미확인 체결을 자동 재주문하지 않고 EXITING과 잔량을 보존한다.
- F5만 직전 주문 상태·취소 확정·실제 잔고를 확인한 뒤 최대 3회 잔량 재주문한다.

---

## 8. 환경 변수

- 모든 민감 정보는 `.env`에 저장, 코드에 하드코딩 금지.
- 코드 내에서는 `os.getenv("KEY", "default")` 패턴으로 접근.
- `.env`는 `.gitignore`에 포함, `.env.example`에 키 목록만 유지.

```python
# Python → 환경 변수 접근 패턴
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
if not KIS_APP_KEY:
    raise EnvironmentError("KIS_APP_KEY 미설정")
```

---

## 9. 로깅 규칙

- 모든 이벤트 로그는 `src/utils/logger.py`의 `log()` 함수를 통한다.
- 형식: `data/logs/YYYYMMDD.jsonl` (1일 1파일, JSONL)
- 로그 레벨: `INFO`, `WARN`, `CRIT`
- 거래 관련 이벤트는 PRD §5의 이벤트 코드를 그대로 사용한다.

```python
# Python / logger.py → 로그 작성 패턴
logger.log("TRAILING_STOP", level="INFO",
           ticker=ticker, exit_price=price,
           highest_step=state["highest_step"],
           stop_price=stop, pnl_pct=pnl_pct)
```

---

## 9-1. 텔레그램 알림 문구 규칙

- 알림은 로그가 아니라 운영자 메시지다. 이벤트 코드를 첫 줄 제목으로 쓰지 않는다.
- 첫 줄은 `긴급/확인/알림: 사람이 읽는 제목` 형식으로 쓴다.
- 본문은 항상 `상황`, `조치`, `세부`, `코드` 순서를 따른다.
- `상황`은 지금 무슨 일이 일어났는지 한 문장으로 쓴다.
- `조치`는 사용자가 바로 해야 할 일을 한 문장으로 쓴다.
- `세부`에는 ticker, qty, price, date, order_id 같은 값만 짧게 넣는다.
- `코드`에는 원래 이벤트 코드(`STALE_POSITION_DETECTED` 등)를 남겨 로그와 대조할 수 있게 한다.
- `CRIT`는 반드시 수동 확인 또는 수동 처리 필요 여부를 `조치`에 포함한다.

예시:

```text
긴급: 전일 포지션 잔류 의심
상황: 이전 거래일 상태 파일에 포지션 정보가 남아 있습니다.
조치: 계좌 보유 수량과 미체결 주문을 확인하고, 필요하면 수동 정리 후 재시작하세요.
세부: date=20260630
코드: STALE_POSITION_DETECTED
```

---

## 9-2. KIS 호출 및 진입 재시도 규칙

- 모든 KIS REST 호출은 `src/api/kis_rest.py`의 전역 rate-limit 경로를 통한다.
- KIS REST는 프로세스 수명 동안 공유 AsyncClient를 사용한다. 호출부에서 요청마다 별도 클라이언트를 만들지 않는다.
- 애플리케이션 종료 시 `kis_rest.close_client()`로 연결 풀을 닫는다.
- 호출 간격은 `KIS_RATE_INTERVAL_SEC`로 제어한다. 개별 모듈에서 별도 sleep으로 rate-limit을 흉내 내지 않는다.
- F1 예상체결가 보강처럼 다수 종목을 조회할 때는 세마포어로 동시성을 제한한다.
- F1 KOSPI/KOSDAQ처럼 시장 단위 조회를 연속 호출할 때는 `F1_MARKET_INTERVAL_SEC`를 사용한다.
- F3 매수 주문 직전에는 `F3_PRE_ORDER_QUIET_SEC` 대기를 거쳐 직전 조회 호출과 주문 호출을 분리한다.
- F2에서 대상 종목이 잠기면 F3는 기본적으로 매수를 시도한다. 주문 전 차단이 필요하면 `F3_ENTRY_BLOCKED`에 표준 `reason`을 남긴다.
- 대체 후보가 남은 후보별 보호 차단(갭 범위 이탈, VI, 주문가능수량 0)은 `INFO`로 기록한다. 모든 후보 소진, 주문/API 실패처럼 거래 파이프라인이 실제로 중단될 때만 `WARN` 이상을 사용한다.
- `ENTRY_QTY_CLAMPED`는 축소율이 `F3_QTY_CLAMP_WARN_PCT` 미만이면 `INFO`, 이상이면 `WARN`으로 기록한다.
- F3 진입 재시도는 마지막 시도까지 미체결 주문 취소가 보장되어야 한다.
- F3 실패 로그에는 주문번호, 주문가, 주문수량, 재시도 횟수, 체결조회 요약을 가능한 한 포함한다.
- DRY_RUN 경로에서는 실제 주문, 실제 KIS API, 운영 DB를 호출하지 않는다.
- `LATENCY_HIGH.latency_ms`는 실제 HTTP 왕복시간과 동일한 `network_ms`다.
  `rate_wait_ms`, `client_setup_ms`, `local_overhead_ms`, `total_ms`를 함께 기록해
  로컬 대기와 상류 API 지연을 구분한다.

---

## 10. 에러 핸들링

- KIS API 호출은 `try/except` + 재시도 (최대 3회, 지수 백오프).
- 청산 주문 실패(`SELL_ORDER_REJECTED`)는 즉시 CRIT 알림 + 수동 처리 요청.
- 처리되지 않은 예외가 `main()` 이벤트 루프를 죽이지 않도록  
  `asyncio.create_task()` 결과에 `.add_done_callback(handle_exception)` 부착.

```python
# Python / asyncio → 태스크 예외 누락 방지 패턴
def _guard(task: asyncio.Task) -> None:
    if task.exception():
        logger.log("UNHANDLED_TASK_ERROR", level="CRIT",
                   error=str(task.exception()))

task = asyncio.create_task(some_coro())
task.add_done_callback(_guard)
```

---

## 11. 코드 컨벤션

| 항목 | 규칙 |
|------|------|
| 포매터 | `ruff format` (Black 호환, 줄 길이 100) |
| 린터 | `ruff check` |
| 타입 힌트 | 모든 공개 함수에 필수 |
| 주석 | WHY가 불명확한 곳에만 한 줄. 코드 설명 주석 금지 |
| 상수 | 모듈 상단 `UPPER_SNAKE_CASE`로 선언 |
| 매직 넘버 | 직접 사용 금지 — 상수 또는 params로 분리 |

---

## 12. 파라미터 관리

- 전략 파라미터(`STEP_SIZE`, `STEP_TRAIL`, `HARD_STOP` 등)는  
  `data/params/params.json`에서 로드한다.
- 코드에 기본값을 하드코딩하는 것은 `params.json` 부재 시 폴백용으로만 허용.
- 파라미터 변경 이력은 `data/params/history.json`에 버전으로 기록한다.

---

## 13. 테스트 가이드라인

- `pytest` 사용, `tests/` 디렉터리에 모듈별 파일 작성.
- KIS API 호출은 반드시 `unittest.mock.AsyncMock`으로 대체한다.
- F4 Step Trailing 로직은 틱 시퀀스 단위 유닛 테스트를 필수로 작성한다.
- F4 WS와 REST가 공용 틱 처리 헬퍼를 통하는지, CLOSED 관측 중에는 가격만 저장하는지 각각 테스트한다.
- `F4_POST_CLOSE_OBSERVE_UNTIL` 오타와 손상된 `entry_at`의 1회 WARN을 테스트한다.
- F5는 잔고 0, 잔고 조회 실패, 체결조회 타임아웃, 부분체결, DB 기록 실패를 각각 회귀 테스트한다.
- 가격흐름은 5,000 tick 초과, 분 이력 보완, 저장 분봉 보완(하루 전체·겹침·빈 분·읽기 실패), 종목·거래일 전환,
  종목 필터, 청산 후 마커, 다운샘플링을 Python/Node 테스트로 나눠 검증한다.

```python
# Python / pytest → Step Trailing 유닛 테스트 패턴
def test_step_trailing_triggers_at_stop():
    state["entry_price"]  = 100_000
    state["highest_step"] = 0.075   # +7.5% 스텝 달성 상태
    state["trailing_active"] = True
    # stop = 100_000 × (1 + 0.075 - 0.020) = 105_500
    on_tick(106_001)   # 발동 안 됨
    on_tick(105_999)   # 발동
    assert state["close_reason"] == "TRAILING"
```

---

## 14. 금지 사항

| 금지 | 이유 |
|------|------|
| `pandas`, `numpy` import (거래 루프 내) | 틱 처리 지연 |
| `time.sleep()` | 이벤트 루프 블로킹 |
| `threading.Thread` | asyncio와 충돌 가능 |
| `print()` (로거 대체) | 로그 누락 |
| DB 직접 `aiosqlite.connect()` | 연결 누수 |
| 청산 주문 중복 전송 | 이중 청산 |
| `state["position_status"]` 직접 문자열 비교 없이 주문 전송 | 중복 청산 |
| 매매 경로에서 KIS 라이브러리·MCP 사용 (`kis_rest.py` 우회) | 전략 지문 밖에서 동작이 바뀐다 — §1-1 |

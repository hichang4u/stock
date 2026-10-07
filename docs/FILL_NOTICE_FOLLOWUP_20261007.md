# 체결통보가 한 건도 기록되지 않았다 — 후속 티켓 (2026-10-07)

관련: `docs/superpowers/specs/2026-10-06-fill-notice-design.md`(B안, `release/20261006-1`, 지문 `5bb284961f7d`),
`docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md` §7, `docs/OPENING_WINDOW_FOLLOWUP_20261006.md` §1

## 1. 현상

승격 후 첫 거래일인 10/07, 체결통보 구독은 성공했다. 그러나 이날 체결된 주문 두 건(매수 212주, 매도 212주)
모두 `FILL_NOTICE_RECEIVED`가 없다. 복호화 실패(`FILL_NOTICE_UNDECRYPTABLE`)나 처리 오류(`FILL_NOTICE_ERROR`)
로그도 없다. 진입 체결은 기존 조회로만 확인됐다(통보 누락 2/2).

## 2. 증거 (운영 로그 `data/logs/20261007.jsonl`, DB `trades.id=53`)

| 시각 | 사건 |
|---|---|
| 09:00:01.207 | `WS_CONNECTED` 001440 |
| 09:00:01.300 | `FILL_NOTICE_SUBSCRIBED` `H0STCNI9` `SUBSCRIBE SUCCESS` `has_key=true` |
| 09:00:02.011 | `WS_DISCONNECTED` `ConnectionClosedError(None, None, None)` |
| 09:00:04.047 | `WS_CONNECTED` (재연결) |
| 09:00:04.082 | `FILL_NOTICE_SUBSCRIBED` `SUBSCRIBE SUCCESS` `has_key=true` |
| 09:00:11.091 | `ENTRY_ORDER_SENT` 주문 `0000001411`, 32,900원 지정가 212주 (주문 API 4.8초) |
| 09:00:13 ~ 09:01:53 | 2초 창 만료 → 취소 2회·체결 조회 4회 모두 `ReadTimeout` |
| 09:02:03.590 | 취소 응답 "모의투자 정정/취소할 수량이 없습니다" |
| 09:02:20.941 | 조회로 체결 확인: 32,600원 212주 (`ENTRY_CANCEL_REJECTED_FILLED`) |
| 09:02:21.395 → 09:02:27.958 | 하드스탑 매도 212주 31,650원 체결 |

- 09:00:04 이후 세션은 끊기지 않았다. 틱 캡처 기준으로 09:00:11~09:02:20에 시세 3,507건,
  매도 구간(09:02:21~27)에 63건을 같은 세션으로 받았다.
- 즉 통보가 왔다면 같은 세션으로 들어왔어야 한다. 그런데 장부에 기록된 통보는 0건이다.
- 이날 `FILL_NOTICE_*` 이벤트는 구독 성공 두 건뿐이다.

## 3. 원인 (미확인)

현재 코드로는 아래 둘을 가를 수 없다.

- **(a) KIS 모의서버가 통보 프레임을 보내지 않았다.**
  - 구독 성공이 실제 발송을 보장하지 않는다.
  - 10/06 실측(설계 10절)은 구독 응답만 확인했다. 실제 체결 통보 프레임은 받아 본 적이 없다.
- **(b) 통보 프레임이 왔지만 `kis_notice.handle_message`가 조용히 버렸다.** 아래 경우는 로그를 남기지 않는다.
  - `parse()`가 None을 돌려줄 때:
    - 필드 수가 26 미만
    - `CNTG_YN != "2"`(접수 통보는 정상적으로 이 경로)
    - `RFUS_YN == "1"`
    - 수량·가격이 숫자가 아님
  - `split_records`가 레코드를 다르게 나눴을 때. 예를 들어 실제 필드 수가 26이 아니면 정렬이 어긋난다
    (H0STCNT0이 46 → 47로 바뀐 전례가 있다, `docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md`).
  - 프레임의 `tr_id`가 `H0STCNI9`/`H0STCNI0`이 아니면 `kis_notice`가 무시한다. 이때 `_parse_ticks`도
    `H0STCNT0`이 아니면 버린다.

**진단의 빈틈:** 장부에 기록한 체결만 로그를 남긴다. 접수 통보나 해석에 실패한 통보, 알 수 없는 TR 프레임은
흔적이 없다. 그래서 (a)와 (b)가 로그에서 똑같이 보인다.

## 4. 영향

- 체결통보로 고치려던 문제(진입 체결을 2초 안에 알지 못하는 것)가 그대로 남았다.
  - 오늘 진입 확정까지 140.9초(`ENTRY_PIPELINE_TIMING.total_ms`)가 걸렸다.
  - 그동안 포지션을 모르는 상태였다(`PENDING_ENTRY_RECOVERED`, ERROR).
- **오늘 손익에는 영향이 없었다(틱 실측).**
  - 손절선 근처(31,600원)를 처음 깬 시각은 09:02:03~08이다. 체결을 바로 알았어도 그 무렵 손절됐을 것이다.
  - 실제 매도가 31,650원과 거의 같다. 보유 중 최고가는 32,850원(+0.77%)이었다.
- 대체 경로(조회)는 설계대로 동작했다. 통보 누락이 새 오동작을 만들지는 않았다.
- 개장 직후 KIS 모의 REST가 심하게 느렸다. 주문 4.8초, 취소·조회 `ReadTimeout` 연속.
  이 상태가 계속되는 한 조회만으로는 체결 확인이 늦다.

## 5. 선택지 (결정 전)

- **가. 통보 진단 로그 추가.** 체결통보 TR 프레임마다 아래 항목만 남긴다.
  - 남길 것: 암호화 여부, 헤더 건수, 필드 수, `CNTG_YN`·`RFUS_YN`·`ACPT_YN`, 주문번호 끝자리
  - 남기지 않을 것: 비밀값, 계좌·HTS ID
  - `H0STCNT0`도 체결통보 TR도 아닌 프레임은 `tr_id`만 남긴다.
  - (a)와 (b)를 하루 만에 가를 수 있다.
  - `kis_notice.py`(또는 `kis_ws.py`)가 지문 대상이라 **현재 지문의 누적 1건이 0이 된다.**
    매매 동작은 바뀌지 않는다.
- **나. KIS 사양·사례 확인.** 모의투자 `H0STCNI9`가 실제 체결 통보를 보내는지, 필드 수가 공식 예제의 26개인지를
  확인한다. 볼 곳은 공식 저장소 이슈, 개발자센터 Q&A, 커뮤니티 사례다. 코드는 바꾸지 않는다.
  확인되지 않으면 가로 넘어간다.
- **다. 운영 캡처에 원시 프레임 저장.** 틱 캡처처럼 `H0STCNT0` 외 프레임을 파일로 남긴다.
  가보다 정보가 많다. 대신 암호문과 계좌 식별 필드가 디스크에 남으므로 마스킹 설계가 필요하다.
  지문 비용은 가와 같다.
- **라. 통보 기능을 끄고 조회만 쓰기** (`F3_FILL_NOTICE_ENABLED=0`). 효과 없는 경로를 정리한다.
  `F3_` 접두어라 이 역시 지문이 바뀐다.

## 6. 선택지 나 결과 — KIS 사양·사례 확인 (2026-10-07 장중, 코드 변경 없음)

대조한 자료는 아래와 같다.
- 공식 저장소 `koreainvestment/open-trading-api` (마지막 커밋 2026-09-28)
  - `examples_llm/domestic_stock/ccnl_notice`
  - `examples_user/kis_auth.py`
  - `backtester/kis_backtest/providers/kis/websocket.py`
  - `legacy/websocket/python/ws_domestic_stock.py`, `multi_processing_sample_ws.py`
  - `MCP/KIS Code Assistant MCP/data.csv`(API 설명)
- 공개 사례 검색

### 6.1 우리 구현과 일치하는 것

| 항목 | 공식 | 우리 (`src/api/kis_notice.py`) |
|---|---|---|
| 모의 구독 TR | `H0STCNI9` ("모의투자는 H0STCNI9 로 변경하여 사용") | 같음 |
| 필드 수 | 26 | 26 |
| 체결 여부 | "14번째 값(CNTG_YN)이 2이면 체결통보, 1이면 주문·정정·취소·거부 접수 통보" | 인덱스 13, `"2"`만 장부 기록 |
| 암호화 | AES256-CBC, 구독 응답의 key/iv | 같음 |
| tr_key | API 설명에는 "종목코드"라고 돼 있으나 예제 코드는 HTS ID | HTS ID (10/06 실측: 가짜 ID는 `OPSP0017`로 거절) |

### 6.2 우리 구현과 다를 수 있는 것 — 조용히 버려지는 경로

1. **수신 프레임의 TR 이름.**
   - 공식 레거시 예제 두 곳은 수신 프레임의 `tr_id`가 `K0STCNI0`·`K0STCNI9`이어도 체결통보로 처리한다
     (`trid0 in ("K0STCNI0", "K0STCNI9", "H0STCNI0", "H0STCNI9")`).
   - 우리 `handle_message`는 `H0STCNI0`/`H0STCNI9`만 받는다. `_parse_ticks`도 `H0STCNT0`이 아니면 버린다.
     그래서 **`K0STCNI9`로 오면 아무 로그 없이 사라진다.**
   - 다만 최신 공식 코드(`examples_user`, `backtester`)는 수신 프레임을 구독 TR(`H0STCNI9`) 그대로 분기한다.
     지금 서버가 어느 이름으로 보내는지는 공개 자료로 확정되지 않는다.
2. **필드 수가 26보다 적을 때.**
   - `split_records`는 헤더 건수로 나누어떨어지지 않으면 한 레코드로 두고, `parse`는 26개 미만이면 None을 돌려준다.
     로그는 없다.
   - 공식 자료는 모두 26개다. 다만 백테스터의 컬럼 이름은 4·17번째가 다르다(`ODER_QTY`, `ACNT_NO2`).
     예제마다 컬럼 정의가 일관되지 않다는 뜻이다. 체결 여부(14번째)·수량(10번째)·단가(11번째)의 위치는 모두 같다.
3. **거부 여부 값.** 우리는 `RFUS_YN == "1"`을 거부로 본다. 공식 백테스터는 `== "Y"`로 본다.
   어느 쪽이든 통보를 버리는 방향은 아니라 이번 누락의 원인 후보는 아니다.

### 6.3 모의투자에서 실제 통보가 오는가

- 공개 자료에서 **"모의투자에서 체결 통보 프레임을 실제로 받았다"는 확인을 찾지 못했다.**
- 같은 기능을 만든 외부 프로젝트(`starwook/quantlog` PR #40)도 모의에서 "두 TR 모두 SUBSCRIBE SUCCESS + 암호화 키
  수신 확인"까지만 적었다. "체결 메시지는 아직 실측 전"이다. 우리 10/06 실측과 같은 단계다.
- 레거시 예제 주석은 `H0STCNI9`를 "테스트용 직원체결통보"라고 부른다.

### 6.4 결론

공개 자료로는 3절의 (a)와 (b)를 가를 수 없다. 대신 (b) 안에서 구체적인 후보 하나(6.2-1, `K0` 접두 TR)가
나왔다. 4절에 정한 대로 **선택지 가(진단 로그)로 넘어간다.** 가를 설계할 때 아래를 기록 범위에 넣는다.

- 체결통보 TR이 아닌 프레임 중 `H0STCNT0`도 아닌 것의 `tr_id`·암호화 여부·헤더 건수
  (`K0STCNI9`가 오면 여기에 잡힌다)
- 체결통보 프레임마다 필드 수, `CNTG_YN`·`RFUS_YN`·`ACPT_YN`, 장부에 기록됐는지 여부

**`K0STCNI*`를 체결통보로 받아들이는 변경은 진단 로그와 별개의 동작 변경이다.**
- 같은 조건("체결을 무엇으로 아는가")에 속하는 버그 수정이다.
- 그래도 진단 로그로 실제 TR 이름을 확인한 뒤에 따로 결정한다.
- 확인 없이 미리 넣으면, 다음 날 통보가 잡혀도 무엇이 원인이었는지 기록이 남지 않는다.

출처:
- https://github.com/koreainvestment/open-trading-api
- https://github.com/starwook/quantlog/pull/40

## 7. 선택지 가 구현 — 진단 로그 (2026-10-07)

매매 동작은 바꾸지 않는다. `src/api/kis_notice.py`에 로그 두 가지를 더한다.

- **`FILL_NOTICE_FRAME`(INFO)** — 체결통보 TR 프레임의 레코드마다 하나.
  - 남기는 것: `tr_id`, `encrypted`, `header_count`, `records`, `field_count`, `cntg_yn`·`rfus_yn`·`acpt_yn`,
    `order_id`, `booked`, `reason`
  - `reason`은 다섯 가지다: `FILL` / `NOT_FILL` / `REJECTED` / `SHORT`(필드 26개 미만) / `BAD_VALUE`(수량·단가)
  - 고객 ID·계좌번호·계좌명은 남기지 않는다(테스트로 고정).
- **`WS_FRAME_UNKNOWN_TR`(WARN)** — `H0STCNT0`도 체결통보 TR도 아닌 프레임.
  - 남기는 것: `tr_id`, `encrypted`, `header_count`, `body_len`, `occurrences`
  - 같은 TR이면 처음과 1,000번째마다만 남긴다.

판정은 다음과 같이 한다(daily-log 4.2절).

| 다음 진입일 로그 | 뜻 |
|---|---|
| `FILL_NOTICE_FRAME`이 있고 `booked=true` | 통보가 온다. 6.2절 후보가 아니면 10/07은 서버 쪽 누락 |
| `FILL_NOTICE_FRAME`이 있고 `SHORT`/`BAD_VALUE` | (b) 해석 결함 — 필드 정의를 고친다 |
| `WS_FRAME_UNKNOWN_TR tr_id=K0STCNI9` | (b) TR 이름 — `K0STCNI*` 수용을 별도 조건으로 결정 |
| 둘 다 없음 | (a) 모의서버가 이 세션으로 통보를 보내지 않는다 |

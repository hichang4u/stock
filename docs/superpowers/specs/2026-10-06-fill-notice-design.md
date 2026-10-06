# 진입 체결을 실시간 체결통보로 확인 — 설계

작성일: 2026-10-06
상태: 설계 승인, 구현 전 실측 확인 대기(10절)
근거: [`ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md`](../../ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md) §5 즉시 착수 조건
충족(2026-10-06 사례, 같은 문서 §7)
원칙: CLAUDE.md "전략 수정은 조건 하나씩" — 이 릴리스가 바꾸는 조건은 **진입 체결을 무엇으로 알아내는가**
하나다. 조회 창·취소·재시도 규칙은 바꾸지 않는다.

## 1. 문제

매수 주문 뒤 체결을 `inquire-daily-ccld` 조회로만 확인한다. 2초 창 안에 확인이 안 되면 취소를
보내고, 취소가 거부되면 다시 조회한다. 그동안 F4 추적은 시작되지 않는다. 2026-10-06 뉴로메카는
09:00:08.24에 체결됐는데 확인은 09:00:09.43이었고, 그 사이 09:00:08.99에 하드스탑가가 뚫렸다.
결과는 −2.60%로 트랙 A 50건 중 최악이다.

## 2. 결정 사항 (2026-10-06, 사용자 승인)

| 항목 | 결정 |
|---|---|
| 방식 | KIS 실시간 체결통보 구독 |
| 복호화 | `pycryptodome` (KIS 공식 예제와 같은 라이브러리) |
| 연결 | 체결통보 **전용 WS 연결**을 개장 전에 열어 장중 유지. 종목 시세 연결과 분리 |
| 판정 | 모든 대기 지점에서 **통보와 조회 중 먼저 전량 체결을 확인한 쪽**. 조회는 예비 경로로 그대로 |
| 구독 키 | `KIS_HTS_ID` (네 개 `.env`에 추가 완료, 지문 대상 아님) |

## 3. KIS 사양 (공식 저장소 `examples_llm/domestic_stock/ccnl_notice`)

- TR: 모의 `H0STCNI9`, 실전 `H0STCNI0`. `tr_key`는 **HTS ID**다(공식 예제 `subscribe(data=[my_htsid])`).
- 주문·정정·취소·거부 **접수** 통보와 **체결** 통보가 모두 온다. 14번째 필드 `CNTG_YN`이 `2`면 체결.
- 데이터부는 AES256-CBC로 암호화돼 온다. 키와 IV는 구독 응답 JSON `body.output.key`, `body.output.iv`.
  복호화: `AES.new(key, MODE_CBC, iv).decrypt(b64decode(c))` 후 PKCS7 unpad.
- 필드(26): `CUST_ID, ACNT_NO, ODER_NO, OODER_NO, SELN_BYOV_CLS, RCTF_CLS, ODER_KIND, ODER_COND,
  STCK_SHRN_ISCD, CNTG_QTY, CNTG_UNPR, STCK_CNTG_HOUR, RFUS_YN, CNTG_YN, ACPT_YN, BRNC_NO, ODER_QTY,
  ACNT_NAME, ORD_COND_PRC, ORD_EXG_GB, POPUP_YN, FILLER, CRDT_CLS, CRDT_LOAN_DATE, CNTG_ISNM40, ODER_PRC`
- `CNTG_QTY`는 그 체결 한 건의 수량이다(누적 아님). 분할 체결은 여러 통보로 온다.

## 4. 구성

| 파일 | 변경 |
|---|---|
| `src/api/kis_notice.py` (신규) | 전용 연결, 구독, 복호화, 해석, 주문별 체결 장부. `_STRATEGY_FILES`에 추가 |
| `src/api/auth.py` | 전역 키를 덮어쓰지 않는 `request_ws_key()` 추가. 기존 `refresh_ws_key()`는 이를 써서 동작 그대로 |
| `src/modules/f3_entry.py` | 주문 등록, 조회 대기·취소 직전·취소 후 확인에서 통보 신호 반영 |
| `main.py` | `kis_notice.run_forever()` 장기 실행 태스크 기동(F4와 같은 방식) |
| `src/release.py` | `_STRATEGY_FILES`에 `src/api/kis_notice.py` 추가 |
| `requirements.txt` | `pycryptodome` 추가 |
| `src/utils/logger.py` | 새 이벤트 라벨 |
| `.claude/skills/daily-log/SKILL.md` | 통보 지연·출처 점검 단계 |

## 5. 연결 수명 (`kis_notice.run_forever`)

- 거래일 08:58 ~ 15:35에만 연결한다. 밖이면 다음 창까지 잔다.
- 접속키는 `auth.request_ws_key()`로 **따로** 받는다. 시세 연결이 쓰는 전역 키를 바꾸지 않는다.
- 구독 요청 → 응답 JSON에서 `rt_cd`, `msg1`, key/iv 저장 → `FILL_NOTICE_SUBSCRIBED` 로그.
  `rt_cd != "0"`이면 `FILL_NOTICE_DISABLED`(사유) 로그 후 그날은 다시 시도하지 않는다(HTS ID 오류 등).
- `PINGPONG`은 그대로 돌려보낸다. 끊기면 시세 연결과 같은 지수 백오프(2 → 30초)로 재연결한다.
- `KIS_HTS_ID`가 없거나 `F3_FILL_NOTICE_ENABLED=0`이면 연결하지 않고 `FILL_NOTICE_DISABLED` 한 번.
- 연결 상태는 `kis_notice.healthy()`(구독 성공 후 연결 유지 중)로 노출한다. 기록용이며 판정에 쓰지 않는다.

## 6. 체결 장부

- `expect(order_id, order_qty)` — 매수 주문 직후 등록. 주문번호는 앞자리 0을 떼고 비교한다.
- 체결 통보(`CNTG_YN=2`, `RFUS_YN != "1"`)가 오면 해당 주문에 수량·금액을 누적. 등록 전에 온 통보도
  보관했다가 등록 시 합친다(주문 응답보다 통보가 빠를 수 있다).
- `fill(order_id) -> dict | None` — `{"status", "order_qty", "fill_qty", "remaining_qty", "fill_price"}`
  (`FillSnapshot.as_fill()`과 같은 모양). `fill_price`는 가중평균.
- `wait_filled(order_id) -> asyncio.Event` — 전량 도달 시 set.
- 장부는 날짜가 바뀌면 비운다.

## 7. f3_entry 연결 (조건 하나: 체결을 무엇으로 아는가)

- `ENTRY_ORDER_SENT` 직후(피라미딩 주문 포함) `kis_notice.expect(order_id, qty)`.
- `_fetch_order_fill_snapshot`: 조회 결과와 `kis_notice.fill(order_id)` 중 더 많이 체결된 쪽을 돌려준다
  (`_more_complete_fill`). 이것만으로 취소 후 확인·최종 대조가 통보를 반영한다.
- `_poll_fill`: 각 조회를 `asyncio.wait({조회, 체결됨 신호}, FIRST_COMPLETED)`로 기다린다. 사이 대기
  (`asyncio.sleep`)도 신호와 경합한다. 신호가 먼저면 즉시 통보 체결을 돌려준다.
- `_cancel_entry_order_confirmed`: 취소를 보내기 **직전**에 통보 장부가 전량이면 취소하지 않고
  `("FILLED", 통보 체결)`을 돌려준다.
- 통보로 확정된 경우 `ENTRY_FILL_CONFIRMED_BY_NOTICE`(주문→통보 지연 ms, 확정 단계)를 남긴다.
- 2초 창, 취소, 재시도, 부분체결 처리 규칙은 그대로다.

## 8. 설정

- `F3_FILL_NOTICE_ENABLED`(기본 `1`) — `F3_` 접두어라 지문에 잡힌다. 끄면 조회만 쓴다.
- `KIS_HTS_ID` — 구독 키. 지문 대상 아님.

## 9. 측정

- 주문마다: `ENTRY_ORDER_SENT` 시각, 첫 체결 통보 수신 시각, 조회로 확인한 시각, 실제 확정 출처.
- `FILL_NOTICE_RECEIVED`(주문번호, 수량, 단가, 체결시각, 수신 지연)는 체결마다 남긴다.
- daily-log에서 "통보가 조회보다 먼저 확정했는가 / 통보가 빠진 체결이 있는가"를 매일 본다.

## 10. 구현 전 실측 확인 (장 마감 후, 개발 트리, 주문 없음)

1. `H0STCNI9` 구독 응답 `rt_cd=0`과 key/iv 수신 → HTS ID 유효.
2. 시세 연결(H0STCNT0, 키1) 유지 중 체결통보 연결(키2)을 열어 두 세션이 60초 이상 함께 유지되는지,
   키2 발급 후 키1 세션이 끊기지 않는지.
3. 둘 중 하나라도 실패하면 구현하지 않고 멈춘다. 2가 실패하면 공용 연결(C안)로 다시 설계한다.

## 11. 테스트 (TDD)

- 복호화: 테스트 안에서 암호화한 프레임 → 원문 복원
- 해석: 체결 통보 / 접수 통보 무시 / 거부 무시, 주문번호 앞자리 0
- 장부: 분할 체결 누적·가중평균, 전량 시 신호, 등록 전 도착 통보 합산, 날짜 변경 초기화
- 연결: 구독 실패 → 비활성화, HTS ID 없음 → 비활성화, PINGPONG 응답, 재연결 (가짜 WS)
- f3: 조회 대기 중 통보 → 즉시 체결, 취소 직전 통보 → 취소 안 보냄, 통보 없음 → 기존 동작 그대로,
  비활성화 시 기존 동작 그대로

## 12. 배포

1. 운영 트리 가상환경에 `pycryptodome` 설치(`promote.ps1`은 설치하지 않는다).
2. 이 기능만 담은 릴리스를 장 마감 후 승격. 지문 리셋(현재 누적 2건 → 0).
3. 다음 거래일 daily-log로 통보 수신·확정 출처 확인.

## 13. 범위 밖

종목 시세 연결 방식 변경(09:00:01 단절, `OPENING_WINDOW_FOLLOWUP_20261006.md` §1), E′ 호출 시점
(같은 문서 §2), 청산(매도) 체결 확인의 통보 전환. 각각 별도 릴리스다.

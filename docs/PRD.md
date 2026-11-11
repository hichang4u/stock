[PRD] 데일리 1종목 타겟 시가 갭 돌파 자동매매 시스템 (당일 All-Out 버전)

문서 버전: v1.4
작성일: 2026년 6월 23일
최종 수정: 2026년 11월 11일 (실전 수익성·실행 경로·계좌 위험 통제 설계 추가)
프로젝트 목적: 한국 증시 개장 직후의 유동성 프리미엄을 포획하고, 청산 시각(F5_EXEC)
미청산 물량의 전량 청산을 시도하며, 계좌 손실 예산 안에서 비용 반영 기대수익을
미래 데이터로 검증하는 퀀트 자동매매 알고리즘 구축. 무손실·정각 전량 체결은 보장하지 않는다.

2026-11-11 설계 보완: [실전 전환의 수익성·실행 경로·위험 통제 설계](REAL_VALIDATION_DESIGN.md).
트랙 A REAL 신규 진입은 기존 운영 안전성 외에 절대 수익성, PAPER/REAL 실행 경로 검증,
계좌 손실 예산과 유효한 승인 증거를 모두 요구한다. 충돌 시 이 보완 설계의 실전 전환 조건을
우선한다. 이 요구사항은 **설계 단계이며 구현·운영 검증은 미완료**다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 시스템 아키텍처 개요 (Architecture Overview)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

본 시스템은 시세 데이터 수집, 필터링 및 락업, 주문 체결, 상태 관리(State Management),
비동기 시간 제어 스케줄러로 구성된 이벤트 기반(Event-driven) 아키텍처를 지향한다.

● 핵심 트리거: 실시간 WebSocket 체결가 데이터 + Time-based 스케줄링(APScheduler 또는 동급)
● 최우선 로직: 청산 트리거는 메인 스레드 부하와 무관하게 독립된 비동기 루프에서
  최우선 실행되어야 함.
● API 기반: 한국투자증권(KIS) Open API (REST + WebSocket)
● 상태 저장: 인메모리 전역 State 객체 (단일 프로세스 기준); 선택적으로 Redis 사용 가능.
● 프로세스 구성: 단일 Python 프로세스 기준 설계. 재시작 복구는 §6 참조.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. 전역 상태 정의 (Global State Schema)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

시스템 전체에서 공유하는 State 객체는 다음 필드를 포함한다.

  state = {
    "trading_date"      : str | None,   # 현재 상태의 거래일 YYYYMMDD
    "target_ticker"      : str | None,   # 락업된 종목 코드
    "target_name"        : str | None,   # 종목명
    "target_candidates"  : list | None,  # F2/F3 후보 목록
    "entry_price"        : float | None, # 체결 확인된 진입 단가
    "entry_at"           : str | None,   # ISO 8601 진입 시각
    "entry_qty"          : int | None,   # 총 보유 수량 (1차 + 2차 합산)
    "remaining_qty"      : int | None,   # 현재 잔여 보유 수량
    "high_price"         : float | None, # 장중 최고가 (체결가 기준)
    "position_status"    : str,          # IDLE | ENTERING | HOLDING | EXITING | CLOSED
    "close_reason"       : str | None,   # TRAILING | HARD_STOP | TIMEOUT
                                         # ENTRY_FAIL | SLIPPAGE_GUARD | GAP_CHANGED
    "order_id"           : str | None,   # 최근 발행된 주문 ID
    "trailing_active"    : bool,         # Step Trailing 활성화 여부 (첫 스텝 +2.5% 달성 후 True)
    "highest_step"       : float,        # 마지막으로 통과한 이익 스텝 수준 (0.025 단위, 예: 0.075)
    "trade_id"           : int,          # DB trades.id (미기록 0)
    "daily_pnl_pct"      : float,        # 당일 누적 실현 손익률 (자본 대비 %, 청산 시 갱신)
    "day_skip"           : bool,         # 당일 거래 스킵 여부
  }

● position_status 전이 규칙:
  IDLE     → ENTERING (F3 1차 주문 전송 직전)
  ENTERING → HOLDING  (F3 1차 체결 확인 후; 2차 피라미딩은 HOLDING 상태에서 수행)
  ENTERING → IDLE     (F3 미체결 확정 시, close_reason = ENTRY_FAIL)
  HOLDING  → EXITING  (F4 주문 접수 확인 후 또는 F5 청산 수량 확정 후·주문 전)
  EXITING  → CLOSED   (요청수량 전량 체결 확인 후, remaining_qty = 0)
  HOLDING  → CLOSED   (F5 사전 잔고조회에서 실제 미보유 확인 시)

● 중복 청산 방지:
  F4의 모든 청산 조건과 F5는 주문 전송 직전에 반드시
  position_status == "HOLDING" 여부를 확인한다.
  F4는 프로세스 내부 `_close_in_progress` 가드로 동시 청산을 차단하고,
  주문 접수 후 atomic하게 EXITING으로 전환한다.
  EXITING 또는 CLOSED 상태이면 신규 청산 주문을 전송하지 않는다.

● 초기값 (F3 체결 확인 시 설정):
  trailing_active = False, highest_step = 0.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. 기능 요구사항 (Functional Requirements)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
F0. 10분 단위 사전 준비 파이프라인 (08:00 ~ 08:30)
──────────────────────────────────────────────────

● 목적:
  F1~F5 매매 파이프라인이 실행되기 전에 시스템, 네트워크, 글로벌 매크로 리스크를 점검한다.
  F0 단계에서 당일 매매 부적합 판정이 내려지면 state["day_skip"] = True로 전환하고,
  이후 F1~F5 스케줄러 작업은 실행하지 않는다.

● 08:00 Infrastructure Boot:
  시스템 자원 상태, 디스크 용량, 프로세스 중복 실행 여부, 이벤트 루프 상태를 점검한다.
  치명적 이상이 있으면 CRIT 로그 및 알림을 전송하고 당일 거래를 중단한다.

● 08:05 Macro Kill Switch:
  외부 금융 데이터 소스(Yahoo Finance, FRED 또는 동급 공급자)를 통해 직전 확정 거래일 기준의
  글로벌 매크로 지표를 수집한다. 모든 수집값은 timestamp, source, raw_value와 함께 로그에 남긴다.

  수집 지표:
  - VIX 지수
  - NASDAQ Composite 전일 대비 등락률
  - USD/KRW 환율 종가, 전일 대비 등락률, 20일 이동평균 대비 이격도

  Severe Kill 조건:
  아래 조건 중 하나라도 충족하면 즉시 당일 매매를 중단한다.
  - VIX >= 30.0
  - NASDAQ Composite daily_return <= -2.5%
  - USD/KRW daily_return >= +1.5%
  - USD/KRW deviation_from_ma20 >= +3.0%

  Composite Kill 조건:
  아래 항목별로 1점씩 부여하고, 총점이 2점 이상이면 당일 매매를 중단한다.
  - VIX >= 25.0
  - NASDAQ Composite daily_return <= -1.8%
  - NASDAQ Composite daily_return <= 최근 10년 rolling 5th percentile
  - USD/KRW daily_return >= +0.8%
  - USD/KRW deviation_from_ma20 >= +2.0%
  - USD/KRW >= 1540.0 AND USD/KRW daily_return > 0

  발동 액션:
  - state["day_skip"] = True
  - state["skip_reason"] = "MACRO_KILL_SWITCH"
  - state["macro_snapshot"]에 수집 지표, score, triggered_rules를 저장
  - today_state.json에 즉시 영속화하여 프로세스 재시작 및 catch-up 경로에서도 재진입을 방지
  - F1~F5 APScheduler job을 메모리에서 제거하거나 실행 전 day_skip guard로 차단
  - DB skip 기록: reason="MACRO_KILL_SWITCH"
  - Telegram/Discord 등 메신저 API로 CRIT 알림 전송

  알림 문구:
  "거시 경제 리스크 초과로 당일 매매(No Trade)를 휴지합니다."

  데이터 실패 정책:
  - REAL 모드: 필수 매크로 데이터 수집 실패 또는 stale data 감지 시 fail-closed로 처리하고 당일 거래 중단
  - PAPER/DEV 모드: 경고 로그 및 알림 후 fail-open 허용 가능

● 08:10 Network Sync:
  KIS REST/WebSocket 및 외부 데이터 공급자 API에 대한 레이턴시와 DNS/네트워크 연결성을 점검한다.

● 08:20 Fail-Safe Link Check:
  Telegram/Discord 등 비상 알림 채널의 송신 가능 여부를 확인한다.

──────────────────────────────────────────────────
F1. 장전 데이터 파싱 모듈 (08:40 ~ 08:58)
──────────────────────────────────────────────────

● 데이터 소스: KIS REST API — 장전 예상 체결가 조회 엔드포인트
  (FHKST03010100: 국내주식 기간별시세 또는 동급 예상 체결가 API)

● 대상 유니버스: KOSPI + KOSDAQ 전 종목 합산

● 필터 1 — 갭 필터:
  Gap = (예상 시가 / 전일 종가) - 1
  조건: Gap >= 0.030 AND Gap < 0.100

● 필터 2 — 유동성 필터:
  최근 5 영업일 일평균 거래대금 기준으로 KOSPI+KOSDAQ 전 종목 합산 모집단 내
  상위 10% 이내 종목만 통과.
  (예: 전체 2,000종목 중 상위 200종목 이내)

● 결과 0건 처리:
  필터 통과 종목이 없으면 state["day_skip"] = True 설정,
  로그에 NO_TARGET 이벤트 기록, 이후 모든 F2~F5 모듈 실행 생략.
  프로세스는 종료하지 않고 익일 스케줄까지 대기 상태 유지.

● 후보 점수:
  정적 VI 이격 배점은 사용하지 않는다. 갭·예상 체결대금·거래대금 급증·
  매수 우위 배점은 합산 후 기존 점수 상한 85점으로 정규화하며, 8~10% 구간의
  과열 페널티 30점은 이 정규화 점수에 적용한다.

──────────────────────────────────────────────────
F2. 타겟 락업 엔진 (08:58:00 ~ 08:59:30)
──────────────────────────────────────────────────

● 실행 전제: state["day_skip"] == False

● 복합 정렬 (내림차순):
  1순위: 예상 체결 대금 (= 예상 체결가 × 예상 체결 수량)
  2순위: 매수 잔량 / 매도 잔량 비율 (매수 우위 강도)

● VI 처리:
  정적 VI 가격과 가깝다는 이유만으로 강한 모멘텀 후보를 제외하지 않는다.
  실제 VI 발동 여부와 주문 가능 시점은 F3 진입 직전에 확인한다.

● 락업:
  정렬 후 Index 0 종목의 Ticker를 state["target_ticker"]에 저장.

──────────────────────────────────────────────────
F3. 진입 주문 모듈 (08:59:40 ~ 09:00:10)
──────────────────────────────────────────────────

● 실행 전제: state["day_skip"] == False AND state["target_ticker"] is not None

● 잔고 기준:
  D+0 기준 실시간 주문 가능 현금 잔고 조회 API 사용.
  미수 및 신용 사용 금지. 순수 예수금(현금) 기준.

● 주문 금액 산정:
  총 주문 금액 = floor(주문 가능 현금 × 0.10)
  총 주문 수량 = floor(총 주문 금액 / 예상 시가)
  1차 주문 수량 = 총 주문 수량 (100%)
  총 주문 수량이 0이면 당일 거래 스킵 (잔고 부족), 로그에 INSUFFICIENT_BALANCE 기록.

● 주문 타입:
  신선한 매도 1호가 + 허용폭과 전일 종가 대비 10% 미만 마지막 틱 중
  낮은 가격을 사용하는 지정가 매수만 허용. 시장가 매수 설정은 지원하지 않음.

● [08:59:40] 진입 직전 갭 재검증:
  F2 락업 시점(08:58)과 진입 시점(08:59:50) 사이 시가 변동 대응.
  예상 시가 재조회 후 Gap 재계산.
  Gap < 0.020 또는 Gap >= 0.100 이면:
    state["day_skip"] = True, close_reason = "GAP_CHANGED"
    로그에 GAP_CHANGED 이벤트 기록, F3~F5 실행 생략.

● 주문 직전 실제 VI 확인:
  대체 후보가 있으면 VI 발동 후보를 즉시 제외하고 다음 후보로 이동.
  단일 후보이고 VI 발동가가 10% 갭 상한 미만일 때만 제한적으로 해제를 대기.
  VI 발동가가 상한 이상이거나 진입 마감 도달 시 즉시 차단.
  해제 확인 후 최종 호가·갭·수량을 다시 검증한 뒤 지정가 주문.

● 1차 주문 전송 (100% 상한 지정가):
  state["position_status"] = "ENTERING" 설정 후 REST API 전송.
  응답에서 order_id를 state["order_id"]에 저장.

● 1차 체결 확인 (설정된 체결 확인 창, 1초 간격 폴링):
  체결 확인 시:
    슬리피지 가드: 체결가 기준 갭 >= 0.100 (체결 상한) 이면
      → 체결 물량 즉시 시장가 매도, close_reason = "SLIPPAGE_GUARD", day_skip = True.
    정상 체결 시:
      state["entry_price"]     = 1차 체결 단가
      state["entry_qty"]       = 1차 체결 수량
      state["remaining_qty"]   = 1차 체결 수량
      state["high_price"]      = 1차 체결 단가
      state["position_status"] = "HOLDING"
      today_state.json 영속화 (§6-7 참조)
  체결 확인 마감까지 미체결 시:
    미체결 잔량 취소 주문 전송.
    state["position_status"] = "IDLE", close_reason = "ENTRY_FAIL"
    로그에 ENTRY_FAIL 이벤트 기록. F4, F5 실행 생략.

● 추가 매수:
  현재 운용 비율은 1차 주문 100%이므로 2차 피라미딩 수량은 0이며 추가
  시장가 매수를 제출하지 않는다.

──────────────────────────────────────────────────
F4. 장중 추적 스탑 모듈 (09:00:00 ~ F5 청산 직전)
──────────────────────────────────────────────────

● 실행 전제: state["position_status"] == "HOLDING"

● 가격 기준:
  WebSocket 실시간 체결가(last traded price) 기준.
  호가(bid/ask) 중간가 또는 이론가 사용 금지.

● High Price 갱신:
  수신 체결가 > state["high_price"] 이면 state["high_price"] 갱신.

● [우선순위 1] 손절 (Hard Stop, -2.0%):
  조건: state["trailing_active"] == False  (첫 스텝 미달성 구간에서만 유효)
        AND 체결가 <= state["entry_price"] × 0.980
  처리:
    position_status == "HOLDING" 확인 후 잔여 수량 전량 시장가 매도.
    주문 접수 확인 후 state["position_status"] = "EXITING" (atomic),
    state["close_reason"] = "HARD_STOP"으로 저장.
    요청수량 전량 체결 확인 후에만 CLOSED로 전환.

● [우선순위 2] Step Trailing (계단형 추적 익절):

  스텝 갱신 (매 틱):
    pnl_pct      = 체결가 / state["entry_price"] - 1
    current_step = floor(pnl_pct / 0.025) × 0.025   ← 내림 (음수 무시)
    current_step = max(current_step, 0.0)
    if current_step > state["highest_step"]:
        state["highest_step"]   = current_step
    if state["highest_step"] >= 0.025:
        state["trailing_active"] = True

  stop_price 계산:
    stop_price = state["entry_price"] × (1 + state["highest_step"] - 0.020)

  발동:
    09:00 ~ 10:49: trailing_active == True  AND  체결가 <= stop_price
    강제 시각 이후: 체결가 <= stop_price
                   (trailing_active == False이면 highest_step=0.0 → stop=entry×0.985 로 강제 발동)

  처리:
    position_status == "HOLDING" 확인 후 잔여 수량 전량 시장가 매도.
    주문 접수 확인 후 state["position_status"] = "EXITING" (atomic),
    state["close_reason"] = "TRAILING"으로 저장.
    요청수량 전량 체결 확인 후에만 CLOSED로 전환.

  ▶ 예시 (진입가 100,000원):
    +2.5% 도달(102,500) → highest_step=0.025, stop=101,000원(+1.0%)
    +5.0% 도달(105,000) → highest_step=0.050, stop=103,500원(+3.5%)
    +7.5% 도달(107,500) → highest_step=0.075, stop=106,000원(+6.0%)
    107,200원으로 하락   → 106,000원 미달 아님, 유지
    106,000원으로 하락   → 발동, 청산 @ +6.0%

● 동일 틱 다중 조건 충족 시 우선순위: Hard Stop > Step Trailing

● 체결 확인과 청산 후 가격 관측:
  - 주문수량과 누적 체결수량을 비교해 FILLED와 PARTIAL_FILL을 구분한다.
  - 부분체결이면 확인된 수량만큼 remaining_qty를 줄이고 EXITING을 유지한다.
  - 체결 미확인이면 주문은 PENDING, 거래는 OPEN, 상태는 EXITING으로 유지하고
    F4_CLOSE_PENDING CRIT 알림으로 주문·잔고 대사를 요청한다.
  - 전량 체결 확인 시에만 trades를 닫고 state를 CLOSED로 전환하며
    remaining_qty를 0으로 만든다.
  - 조기·수동 진입 거래가 설정 시각 전에 청산되면
    `F4_POST_CLOSE_OBSERVE_UNTIL`까지 WS/REST 가격을 차트용으로만 수집한다.
    `_handle_price_tick` 공용 게이트는 CLOSED 중 스탑·주문·VI 처리를 실행하지 않는다.
  - `F4_POST_CLOSE_OBSERVE_UNTIL` 오타와 손상된 `entry_at`은 WARN을 1회 기록하고,
    오타는 09:10으로 폴백하며 손상 시 관측을 안전하게 중단한다.
  - DRY_RUN은 결정론적 합성 틱으로 청산한 뒤 후속 가격 관측을 수행하지 않는다.

● WebSocket 연결 장애 시:
  수신 중단 감지 시 지수 백오프로 재연결을 계속 시도.
  REST 백업 폴러는 WebSocket 미연결·stale 구간에 현재가 조회를 병행.
  한 모니터가 실패해도 다른 모니터를 유지하며, 전송 경로 장애만으로
  체결 확인 없는 강제 CLOSED를 만들지 않음. (§6-3 참조)

──────────────────────────────────────────────────
F5. 마감 청산 스케줄러 (F5_EXEC — 기본 15:15:00)
──────────────────────────────────────────────────

● 이 모듈은 F4의 청산 여부와 무관하게 독립 비동기 루프에서 스케줄링됨.
  실제 청산 실행 여부는 position_status 확인으로 결정함.

● F5_PRECHECK (기본 15:14:50) — Pre-Check:
  실시간 잔고 조회 API 호출.
  해당 Ticker 보유 수량을 state 외부 로컬 변수(prefetch_qty)에 별도 저장.
  잔고 0은 실제 미보유로 구분하고, 조회 실패(None)일 때만 state 잔량을 보조값으로 사용.

● F5_EXEC (기본 15:15:00) — Execute:
  1. state["position_status"] != "HOLDING" 이면 주문 전송 생략 (이미 청산됨).
  2. state["position_status"] == "HOLDING" 이면:
     오늘 유효한 pre-check 수량 또는 상태 잔량을 먼저 확정.
     수량이 0이면 주문 없이 CLOSED로 전환하고 상태·계좌 불일치 CRIT 알림.
     수량이 있으면 state["position_status"] = "EXITING" (atomic),
     state["close_reason"] = "TIMEOUT"으로 저장한 뒤 시장가 매도 주문 전송.
     주문 후 최대 30초 동안 누적 체결을 조회.
  3. 부분체결이면 확인된 체결수량을 기록하고, 실제 잔고를 재조회한 뒤 잔량만 후속 주문.
     확인된 잔량은 상태 파일에도 즉시 반영.
  4. 전량 체결 및 체결가가 확인된 경우에만 trades 청산 이력을 확정하고
     state를 CLOSED, remaining_qty를 0으로 변경.

● Retry 정책:
  주문 전송/체결 확인 실패 또는 부분체결 시 2초 간격으로 최대 3회 처리.
  2회차부터는 직전 주문의 체결·미체결 상태를 확인하고, 살아 있는 미체결 주문은
  취소가 확정된 뒤에만 후속 주문을 허용함. 이후 KIS 잔고를 재조회해 실제 잔량만 주문하며,
  잔고 재조회 실패 상태에서는 중복 매도 위험 때문에 맹목적으로 재주문하지 않음.
  주문 발송 직후 orders 행은 PENDING으로 기록하고, DB 기록 실패는 재주문 사유와 분리.
  여러 주문으로 나뉜 체결은 총 체결대금/총 체결수량의 가중평균을 최종 청산가로 사용.

● Retry 전부 실패 시:
  §5 알림 채널을 통해 즉시 긴급 알림 전송.
  로그에 TIMEOUT_ORDER_FAILED 이벤트 기록.
  state["position_status"]는 EXITING으로 유지하고 확인된 remaining_qty를 저장.
  재시작 시 자동 재매도하지 않고 주문·잔고 수동 대사를 요구.
  운영자가 수동 청산해야 함.

● 잔고 0이나 체결가 미확인 시:
  잔고 0으로 매도 완료가 추정되더라도 체결가가 없으면 임의 가격으로 trades를 닫지 않음.
  TIMEOUT_CLOSE_UNVERIFIED 긴급 이벤트를 남기고 주문/체결 내역 수동 확인을 요청.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. 비기능 요구사항 (Non-Functional Requirements)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

● 지연 시간(Latency):
  시장가 주문 REST API 응답 100ms 이내를 목표로 함.
  실제 HTTP 왕복 `network_ms`가 200ms를 초과하면 LATENCY_HIGH 로그를 기록하고,
  500ms 초과는 WARN으로 기록한다.
  각 로그는 `rate_wait_ms`, `client_setup_ms`, `local_overhead_ms`, `total_ms`를
  함께 포함해 로컬 호출 제한 대기와 KIS 응답 지연을 구분한다.
  KIS REST 연결은 프로세스 수명 동안 공유 AsyncClient의 연결 풀을 재사용한다.

● 예외 처리(Fail-Safe):
  F5 Retry 3회 전부 실패 시 긴급 알림 (§5 참조).
  F4 WebSocket 장애 시 재연결과 REST 백업을 병행하고 모니터 오류를 CRIT로 기록.

● 로깅(Logging):
  모든 이벤트를 JSON Lines 형식으로 data/logs/YYYYMMDD.jsonl 에 적재.
  로그 스키마는 §7 참조.

● 시스템 시간 동기화 (Time Sync):
  운영 PC는 반드시 NTP 서버와 동기화 상태를 유지해야 함.
  권장: Windows Time Service 활성화 + time.windows.com 또는 pool.ntp.org 사용.
  허용 오차: 시스템 클럭 ±200ms 이내.
  프로세스 시작 시 NTP 서버와 시각 차이를 측정하여 로그 기록.
  차이가 500ms 초과 시 CRIT 알림 발송 후 운영자 확인 요청.

● 일일 손실 한도 (Daily Stop):
  당일 누적 실현 손실이 자본 대비 -3.0% 도달 시 당일 추가 거래 중단.
  state["daily_pnl_pct"] 는 청산 이벤트마다 갱신.
  한도 도달 시: state["day_skip"] = True, DAILY_STOP 알림 발송.
  이 전략은 1일 1종목이므로 실질적으로 단일 거래 손실이 -3% 초과 시 발동.
  (Hard Stop -2.0%가 정상 동작하면 이 한도에 도달하지 않아야 함 — 이중 안전장치)

● 모니터링:
  장중 09:00~F5 청산 구간 1분 간격으로 현재 포지션 상태를 콘솔/로그에 출력.
  출력 포맷: [HH:MM:SS] STATUS={status} TICKER={ticker} ENTRY={entry} HIGH={high} CURRENT={current} PNL={pnl}%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. KIS API 운영 요건
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
5-1. API 인증 토큰 관리
──────────────────────────────────────────────────

● KIS Access Token 특성:
  유효 기간: 발급 후 24시간.
  만료 시 모든 REST API 호출이 401 Unauthorized로 실패함.

● 갱신 스케줄:
  매일 08:30 (F1 시작 10분 전) 토큰을 선제적으로 재발급.
  재발급 성공 시: 새 토큰을 메모리 및 data/auth/token_cache.json에 저장.
  재발급 실패 시: 2초 간격 3회 재시도 → 전부 실패 시 CRIT 알림, 당일 거래 중단.

● 장중 토큰 만료 대비:
  API 응답 401 수신 시 즉시 토큰 재발급 시도.
  재발급 후 원래 요청 1회 재시도.
  재발급 실패 시 §6-1 시간대별 장애 대응 계층 적용.
  KIS가 HTTP 200 응답 본문으로 `msg_cd=EGW00123`
  ("기간이 만료된 token 입니다.")를 반환하는 경우도 토큰 만료로 간주한다.
  이 경우 TOKEN_EXPIRED 이벤트를 기록하고 재발급 후 동일 요청을 1회만 재시도한다.
  재시도 후에도 만료 응답이면 원 응답을 호출 모듈에 반환하여
  BALANCE_QUERY_ERROR 등 업무 이벤트로 원인을 남긴다.

──────────────────────────────────────────────────
5-2. API Rate Limit 관리
──────────────────────────────────────────────────

● KIS API 호출 제한 (실전 기준):
  초당 최대 20건 (REST), 분당 최대 500건.
  초과 시 HTTP 429 응답 반환.

● F1 전 종목 조회 전략:
  KOSPI+KOSDAQ 전 종목(약 2,000~2,500종목) 조회 시 페이지네이션 또는 배치 처리.
  호출 간격: 50ms 이상 유지 (초당 20건 한도 준수).
  예상 소요 시간: 약 2~3분 → 08:40 시작으로 08:58 전 완료 가능.
  429 수신 시: 1초 대기 후 재시도.

● F4 WebSocket 구독:
  단일 종목 체결 데이터 구독이므로 Rate Limit 무관.

──────────────────────────────────────────────────
5-3. 휴장일 처리
──────────────────────────────────────────────────

● 휴장일 판단:
  프로세스 시작 시 (08:30 이전) KIS API 장운영시간 조회 엔드포인트로
  당일 개장 여부 확인.
  휴장일 또는 단축 거래일 감지 시:
    당일 모든 모듈 실행 중단.
    MARKET_CLOSED 로그 기록 및 알림 발송.
    다음 영업일 스케줄까지 대기.

● 단축 거래일 (명절 전날 등):
  장 마감 시각이 F5 청산 시각 이전이면 당일 거래 스킵.
  그 외에는 정상 운영 (F5 청산은 F5_EXEC 기준).

──────────────────────────────────────────────────
5-4. API 키 보안 관리
──────────────────────────────────────────────────

● 저장 방식:
  KIS API Key, Secret, 계좌번호는 환경변수로 관리.
  .env 파일 사용 시 .gitignore에 반드시 포함.
  소스코드 및 로그에 직접 기록 금지.

● 로그 마스킹:
  API 응답 및 요청 로그에서 계좌번호, 토큰 값은 앞 4자리만 노출하고 나머지 마스킹.
  예: 계좌번호 12345678 → 1234****

● token_cache.json 접근 제한:
  파일 권한을 소유자 읽기/쓰기 전용으로 설정 (chmod 600 또는 Windows ACL).

──────────────────────────────────────────────────
5-5. 개발·사고 분석용 KIS MCP 운영 정책
──────────────────────────────────────────────────

● 목적과 우선순위:
  KIS MCP는 자동매매 런타임 구성요소가 아니라 개발자 보조 도구로만 사용한다.
  가장 우선하는 용도는 Code Assistant 계열 MCP를 통한 공식 API 레퍼런스 확인이다.
  - 신규 TR 추가 전 TR ID, 요청 파라미터, 응답 필드의 의미와 필수 여부 검증.
  - EGW00201 등 오류 코드와 주문 거부 응답의 공식 의미 확인. 운영 로그의 미확인 코드는
    `msg_cd`만으로 추정하지 않고 `msg1` 및 KIS 공식 문의 결과와 함께 검증한다.
  - 체결통보 WebSocket, 예상체결, 잔고·미체결 조회 등 사용 가능한 API 탐색.
  코드에 TR ID나 필드 해석을 반영할 때에는 MCP 조회 결과만 믿지 않고
  공식 문서의 버전·모의/실전 구분·최종 갱신일을 함께 확인한다.

● 허용 용도:
  1. 개발 및 코드 리뷰 중 API 명세 조회.
  2. 장애 조사 중 계좌 잔고·보유종목·미체결 주문의 읽기 전용 확인.
  3. 장 종료 후 과거 시세 데이터를 수집하여 오프라인 백테스트에 사용.
     F1 갭 구간은 3~8% 코어와 8~10% 조건부 허용 구간을 구분해 검증한다.
  4. MCP로 얻은 과거 데이터는 조회 시각, 데이터 기간, TR/출처와 적용한
     파라미터 버전을 함께 저장한 뒤 분석 입력으로 사용한다.

● 금지 용도:
  - 봇 프로세스에서 MCP를 호출하거나 MCP를 F1~F5 실행 경로의 의존성으로 추가하지 않는다.
  - MCP를 통한 매수·매도·정정·취소 주문은 금지한다.
  - MCP 응답을 `kis_rest.py`의 대체 경로 또는 자동 failover로 사용하지 않는다.
  - MCP가 반환한 미검증 데이터를 당일 주문 판단에 직접 주입하지 않는다.
  이유: MCP 호출은 `kis_rest.py`의 호출 직렬화, 토큰 갱신, transient 재시도,
  주문 전송 마감 가드와 DB/JSONL 감사 기록을 우회할 수 있다.

● 계정·권한 분리:
  - 개발용 MCP는 봇 운영 키와 분리된 PAPER 앱키·모의계좌를 기본으로 사용한다.
  - 사고 조사에 실계좌 조회가 꼭 필요하면 조회 전용 도구만 허용하고 주문 도구는 비활성화한다.
  - MCP 인증정보는 저장소와 프롬프트에 기록하지 않고 운영체제 비밀 저장소 또는
    사용자 전용 환경변수로 관리한다. MCP 설정 파일도 Git 추적 대상에서 제외한다.

● Rate Limit 및 사용 시간:
  - 같은 앱키를 사용하는 MCP와 봇은 KIS 호출 한도를 공유하는 것으로 간주한다.
  - 09:00~09:11 진입 집중 구간에는 KIS MCP 호출을 금지한다.
  - 그 외 장중 조회도 봇 호출과 경쟁하지 않는 별도 PAPER 키를 사용하거나,
    봇이 중지된 장애 조사 상황에서 운영자가 명시적으로 수행할 때만 허용한다.
  - EGW00201, 미확인 호출 제한 코드 또는 HTTP 429 발생 시 MCP 추가 호출을 즉시 중단하고
    봇의 안전한 실행을 우선한다.

● 사고 조사 및 감사 추적:
  - `STALE_POSITION_DETECTED`, `BALANCE_QUERY_FAILED` 조사 시 MCP 조회값은
    KIS 원장 확인을 위한 보조 증거로만 사용하고 DB·today_state.json·JSONL 로그와 대조한다.
  - 조사 기록에는 조회 시각, 사용 도구, 계정 종류(PAPER/REAL), 조회 대상,
    핵심 결과를 남긴다. 인증정보·계좌번호 전체·토큰은 기록하지 않는다.
  - MCP 경로의 주문은 기존 `orders` 테이블과 주문 이벤트 로그 밖에 남으므로 항상 금지한다.

● 설치 및 운영 경계:
  - MCP 클라이언트와 서버 설치 절차는 별도 개발환경 문서에서 관리한다.
  - 설치 전 KIS 공식 배포처의 최신 실행 명령, 지원 TR, 권한 범위를 확인한다.
  - MCP 장애나 미설치는 봇 시작·거래·청산에 영향을 주어서는 안 된다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. 알림 채널 (Notification)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
Telegram Bot 구현 요건
──────────────────────────────────────────────────

● 채널: Telegram Bot (단일 채널로 확정)

● 인증 정보:
  TELEGRAM_BOT_TOKEN: BotFather에서 발급받은 Bot API Token.
  TELEGRAM_CHAT_ID  : 알림을 수신할 Chat ID (개인 DM 또는 그룹).
  두 값 모두 환경변수로 관리. 소스코드 및 로그에 기록 금지.

● API 엔드포인트:
  POST https://api.telegram.org/bot{TOKEN}/sendMessage
  파라미터:
    chat_id    : TELEGRAM_CHAT_ID
    text       : 메시지 본문 (아래 포맷 참조)
    parse_mode : "Markdown" (볼드/이탤릭 서식 사용)

● 메시지 포맷:
  [{심각도}] {이벤트명}
  {메시지 내용}
  {타임스탬프 KST}

  예시:
  [CRIT] TIMEOUT_ORDER_FAILED
  마감 청산 실패! 수동 청산 필요. 005930 200주
  2026-06-23 15:15:06 KST

● Telegram API Rate Limit:
  동일 chat_id 기준 최대 1건/초.
  알림 전송 간 50ms 이상 간격 유지.
  HTTP 429 수신 시: retry_after 값(초) 대기 후 1회 재시도.

● 전송 실패 처리:
  타임아웃(3초 초과) 또는 5xx: 2초 대기 후 최대 2회 재시도.
  재시도 전부 실패: 콘솔 및 로그에 NOTIFICATION_FAILED 기록.
  알림 전송 실패가 거래 로직을 블로킹하면 안 됨 — 비동기 큐 방식으로 발송.

● CRIT 알림 별도 처리:
  심각도 CRIT 이벤트는 메시지 앞에 🔴 이모지를 붙여 즉시 식별 가능하게 함.
  (예: 🔴 [CRIT] SELL_ORDER_REJECTED ...)

● 알림 발송 트리거 및 메시지:

  이벤트                    | 심각도 | 메시지 내용
  ─────────────────────────|--------|──────────────────────────────────────────────
  MARKET_CLOSED             | INFO   | 휴장일 감지. 당일 거래 없음.
  TOKEN_REFRESH_FAIL        | CRIT   | KIS 토큰 갱신 실패. 당일 거래 중단.
  MACRO_KILL_SWITCH         | CRIT   | 거시 경제 리스크 초과. 당일 매매(No Trade) 중단.
  TIME_SYNC_WARN            | CRIT   | 시스템 시각 오차 {ms}ms. 확인 필요.
  NO_TARGET                 | INFO   | 당일 필터 통과 종목 없음. 거래 스킵.
  TARGET_LOCKED             | INFO   | 타겟 확정: {ticker}, 예상갭 {gap}%
  GAP_CHANGED               | INFO/WARN | 대체 후보가 남으면 후보 제외(INFO), 전체 거래 스킵이면 WARN.
  SLIPPAGE_GUARD            | WARN   | 슬리피지 초과 체결. 즉시 청산.
  ENTRY_EXECUTED            | INFO   | 진입: {ticker} {qty}주 @ {price}원
  PYRAMID_SKIPPED           | INFO   | 2차 피라미딩 생략. 갭 미확인.
  ENTRY_FAIL                | WARN   | 진입 미체결. 당일 거래 없음.
  TRAILING_STOP             | INFO   | 익절 청산: {ticker} @ {price}원 (스텝 {step}% → stop {stop}원)
  HARD_STOP                 | WARN   | 손절 청산: {ticker} @ {price}원 (진입 {entry}원)
  TIMEOUT_CLOSE             | INFO   | 마감 청산: {ticker} {qty}주
  TIMEOUT_ORDER_FAILED      | CRIT   | 마감 청산 실패! 수동 청산 필요. {ticker} {qty}주
  F4_CLOSE_PENDING          | CRIT   | F4 매도 주문 부분·미확인 체결. 주문/잔고 대사 필요.
  F4_ENTRY_AT_INVALID       | WARN   | 청산 후 관측용 진입시각 손상. 관측 중단.
  F4_OBSERVE_UNTIL_INVALID  | WARN   | 청산 후 관측 종료시각 오타. 기본 09:10 사용.
  DAILY_STOP                | CRIT   | 일일 손실 한도 도달. 추가 거래 중단.
  LATENCY_HIGH              | WARN   | API 응답 {ms}ms 초과. 지연 감지.
  WS_CONNECTED              | INFO   | WebSocket 연결 또는 재연결 성공.
  WS_DISCONNECTED           | WARN/CRIT | WebSocket 연결 끊김. 연속 실패 횟수 포함.
  F4_REST_BACKUP_START      | INFO   | REST 백업 가격 폴러 시작.
  F4_MONITOR_TASK_ERROR     | CRIT   | WS/REST 모니터 태스크 비정상 종료.
  PROCESS_RESTART_DETECTED  | WARN   | 재시작 감지. 기존 포지션 복구 시도.
  NETWORK_DOWN              | CRIT   | 인터넷 연결 끊김. 복구 대기 중.
  PARTIAL_FILL              | WARN   | 부분 체결. {fill_qty}/{order_qty}주 체결.
  SELL_ORDER_REJECTED       | CRIT   | 매도 주문 거부. 수동 청산 필요. {ticker}
  VI_TRIGGERED              | WARN   | VI 발동. {ticker} 주문 대기.
  CIRCUIT_BREAKER           | CRIT   | 서킷브레이커 발동. 전 종목 거래 정지.
  TRADING_HALTED            | CRIT   | {ticker} 거래 정지. 수동 대응 필요.
  STALE_POSITION_DETECTED   | CRIT   | 전일 포지션 잔류 의심. 즉시 확인 필요.
  BALANCE_QUERY_FAILED      | CRIT   | 잔고 조회 재시도 소진. API 오류로 진입 차단.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. 장애 복구 (Fault Recovery)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
6-0. 핵심 원칙
──────────────────────────────────────────────────

  1. 포지션 보유 중 불확실 상황 = 즉시 청산 시도 (보수적 원칙)
  2. 청산 불가 확정 시    = 긴급 알림 발송 후 수동 처리 요청
  3. 장애 감지 → 대응 → 알림 순서를 항상 유지
  4. 이중 주문 방지: 청산 주문 전 반드시 position_status 확인

──────────────────────────────────────────────────
6-1. 시간대별 장애 대응 계층
──────────────────────────────────────────────────

  시간대              | 포지션 | 대응 방침
  ─────────────────  |--------|─────────────────────────────────────────
  08:40 ~ 08:59      | 없음   | 어떤 장애든 당일 거래 스킵, DAY_SKIP 알림
  09:00 ~ 10:49      | 보유   | 복구 시도 → 실패 시 즉시 청산
  강제 시각 ~ 청산 전 | 보유   | 복구 시도 없이 즉시 청산 시도 → 실패 시 긴급 알림
  F5_EXEC 이후       | 잔류   | 무조건 긴급 알림, 수동 청산 요청

──────────────────────────────────────────────────
6-2. 네트워크 장애
──────────────────────────────────────────────────

● 인터넷 연결 상태 감시:
  10초 간격으로 KIS API 헬스체크 엔드포인트 ping.
  30초 이상 응답 없음 → NETWORK_DOWN 감지.

● 포지션 없는 상태(IDLE/ENTERING)에서 NETWORK_DOWN:
  당일 거래 스킵, 로그 및 알림.

● 포지션 보유(HOLDING) 중 NETWORK_DOWN:
  연결 복구 대기 (최대 60초).
  60초 내 복구 시: 정상 재개.
  60초 초과 또는 강제 trailing 시각 이후: 네트워크 복구 즉시 시장가 청산.
  복구 전 F5_EXEC 도달 시: 복구 후 첫 번째 동작으로 즉시 청산 + TIMEOUT_ORDER_FAILED 알림.

● 이중 네트워크 권장 (운영 환경):
  유선 인터넷 + LTE 핫스팟 자동 전환 (OS 레벨 failover).

──────────────────────────────────────────────────
6-3. WebSocket 장애
──────────────────────────────────────────────────

● 감지 조건: 체결 데이터 수신 중단 5초 이상.

● 대응 순서:
  1. WebSocket 지수 백오프 재연결을 계속 시도.
  2. 재연결 성공: F4 추적 재개, WS_CONNECTED 로그 기록.
  3. WebSocket 미연결 또는 마지막 틱이 stale이면 REST API 백업 폴링을 사용.
  4. WS/REST 태스크 중 하나가 실패해도 나머지 모니터는 유지.
     모든 모니터가 끝났는데 상태가 HOLDING이면 `run_forever()`가 백오프 후
     F4 사이클을 재시작.
  5. 데이터 경로 장애 자체는 매도 체결 근거가 아니므로 자동 CLOSED 처리하지 않음.

──────────────────────────────────────────────────
6-4. 주문/체결 장애
──────────────────────────────────────────────────

● 주문 전송 타임아웃 (2초 초과):
  동일 주문 재전송 전, 반드시 미체결 주문 조회 API로 이중 전송 여부 확인.
  이미 전송된 주문 존재 시: 재전송 생략, 기존 order_id로 체결 대기.

● 부분 체결 처리:
  F3 진입 주문 부분 체결 확인 시:
    부분 체결 수량을 entry_qty로 저장 후 HOLDING 전환.
    미체결 잔량 취소 주문 전송.
    로그에 PARTIAL_FILL 이벤트 기록 (체결 수량, 미체결 수량 포함).
  F4/F5 매도 주문 부분 체결 시:
    F4는 자동 재주문하지 않고 EXITING 상태와 확인된 잔량을 저장한 뒤
    F4_CLOSE_PENDING 긴급 알림으로 수동 대사를 요청.
    F5는 직전 주문 상태와 취소 확정, 실제 잔고를 확인한 뒤 잔량만 재주문 (최대 3회).
    F5 3회 전부 실패 시 EXITING 유지 + 긴급 알림 + 수동 처리 요청.

● 매도 주문 거부 (잔고 오류, API 오류 등):
  오류 코드 분류:
    4xx (클라이언트 오류): 파라미터 재구성 후 1회 재시도.
    5xx (서버 오류): 2초 대기 후 최대 3회 재시도.
    재시도 전부 실패: SELL_ORDER_REJECTED 알림, 수동 청산 요청.

──────────────────────────────────────────────────
6-5. 시세 데이터 스파이크 필터
──────────────────────────────────────────────────

  오류 데이터로 인한 Trailing/Hard Stop 오발동 방지.

● 감지 조건:
  수신 체결가가 직전 체결가 대비 ±3% 초과 순간 변동.

● 처리:
  해당 틱 무시, PRICE_SPIKE_FILTERED 로그 기록.
  연속 2틱 이상 같은 방향으로 ±3% 초과 시: 정상 데이터로 판정, 처리 재개.

● 체결가 데이터 지연 감지:
  수신 타임스탬프와 시스템 시각 차이가 3초 초과 시 PRICE_DELAY_WARN 로그.
  10초 초과 시: WebSocket 장애(§6-3)로 전환 처리.

──────────────────────────────────────────────────
6-6. 거래소 이벤트 장애
──────────────────────────────────────────────────

● 개별 종목 VI 발동:
  감지: WebSocket 또는 REST API의 VI 발동 필드 확인.
  처리:
    신규 주문 전송 즉시 중단. 대체 후보가 있으면 교체하고, 없을 때만
    발동가와 진입 마감 조건을 만족하는 범위에서 VI 해제를 제한 대기.
    VI 해제 후 F5_EXEC 이전: F4 추적 재개.
    VI 해제 후 F5_EXEC 이후: 즉시 시장가 청산.
    VI 발동 중 F5_EXEC 도달: VI 해제 즉시를 첫 번째 동작으로 청산.
  알림: VI_TRIGGERED, VI_RELEASED 이벤트 발송.

● 시장 전체 서킷브레이커:
  처리: VI 발동과 동일 절차.
  추가: CIRCUIT_BREAKER 이벤트 발송 (심각도 CRIT).

● 종목 거래 정지 (관리종목 지정 등):
  매도 불가 상태 = 자동 처리 불가.
  즉시 긴급 알림: TRADING_HALTED 이벤트 (심각도 CRIT).
  거래 재개 감시: 1분 간격으로 거래 가능 상태 조회.
  재개 확인 즉시: 시장가 청산 실행.

──────────────────────────────────────────────────
6-7. 프로세스 크래시 및 재시작 복구
──────────────────────────────────────────────────

● 상태 영속화 파일 (today_state.json):
  경로: data/state/today_state.json
  F3 체결 확인 직후 생성, 상태 변경 시마다 즉시 갱신.
  {
    "date"            : "YYYYMMDD",
    "ticker"          : "...",
    "name"            : "...",
    "target_candidates": list,
    "entry_price"     : float,
    "entry_at"        : str | null,
    "entry_qty"       : int,
    "remaining_qty"   : int,
    "high_price"      : float,
    "trailing_active" : bool,
    "highest_step"    : float,
    "trade_id"        : int,
    "position_status" : "HOLDING | EXITING | CLOSED",
    "close_reason"    : null | "TRAILING | HARD_STOP | TIMEOUT | ..."
  }
  파일 손상 방지: 쓰기 시 임시 파일(today_state.tmp)에 먼저 저장 후 rename (atomic write).

● 프로세스 워치독:
  Windows Task Scheduler로 1분 간격 프로세스 생존 확인.
  프로세스 사망 감지 시 자동 재시작.
  재시작 후 아래 복구 절차 자동 실행.

● 재시작 복구 절차:
  기동 시 `daily_skips` DB의 MARKET_CLOSED/VI_ACTIVE를 먼저 확인해 day_skip을 복원하고,
  이어서 아래 포지션 상태 복구를 수행한다.
  1. today_state.json 존재 확인.
     파일 없음 또는 손상: DB의 최근 OPEN 거래를 조회하고, 거래 정보가 완전하면
     KIS 잔고로 실제 수량을 확인해 HOLDING 상태를 복원.
     DB OPEN 거래와 실제 잔고가 불일치하면 자동 복구를 중단하고 CRIT 알림.
  2. date가 오늘 + position_status == "HOLDING":
     KIS 잔고 조회 API로 실제 보유 수량 확인.
     보유 수량 > 0: state 전체 복원 후 F4/F5 재개.
     보유 수량 == 0: 상태 복원을 생략하고 NO_ACTUAL_HOLDING 경고 기록.
     잔고 조회 실패: 자동 복구를 중단하고 수동 확인 요청.
  3. date가 오늘 + position_status == "EXITING":
     상태를 복원하고 당일 신규 진입과 자동 재매도를 차단.
     PROCESS_RESTART_DETECTED에
     recovered_status="EXITING_REQUIRES_RECONCILIATION"을 기록하고,
     미체결 주문·체결내역·실제 잔고 수동 대사를 요청.
  4. position_status == "CLOSED": 상태를 복원해 당일 재진입을 차단하고
     UI 청산 차트·마커를 유지한 채 정상 대기.
  5. date가 오늘이 아님:
     position_status가 CLOSED/IDLE: 정상 종료된 파일 → 알림 없이 폐기
     (STALE_STATE_DISCARDED, INFO 로그만 기록).
     그 외(HOLDING/ENTERING/EXITING/누락·알 수 없는 상태): 전일 잔여 포지션 또는
     파일 손상 의심 → 즉시 긴급 알림(STALE_POSITION_DETECTED) + 당일 자동
     진입 차단. 파일은 운영자 확인용 증거로 유지하되, 이후 당일 DB OPEN
     거래 복구의 persist가 덮어쓸 수 있으므로 차단 시점에
     today_state.stale_<날짜>.json 사본을 먼저 격리한다.
     사본 파일명의 날짜는 YYYYMMDD만 신뢰하고 그 외는 unknown_<해시>로
     치환한다. 사본 실패는 STALE_BACKUP_FAILED(CRIT) 로그만 남기고
     차단·알림·포지션 복구는 계속한다.
  복구·차단·불일치 결과는 PROCESS_RESTART_DETECTED 또는
  STALE_POSITION_DETECTED 이벤트로 기록하고 필요한 경우 알림.

● today_state.json 초기화:
  거래일 전환 시 인메모리 상태를 초기화하고, F3 체결 확인 또는 이후 상태 변경 시
  해당 날짜의 today_state.json을 원자적으로 저장.
  장애 이벤트 전체 목록은 §6 알림 채널 참조.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
7. 로그 스키마 (Log Schema)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

파일: data/logs/YYYYMMDD.jsonl
형식: JSON Lines (1줄 = 1이벤트)

공통 필드:
  {
    "ts"      : "2026-06-23T09:00:00.123+09:00",  // ISO8601 KST
    "event"   : "EVENT_NAME",
    "level"   : "INFO | WARN | CRIT",
    "ticker"  : "005930",                          // 해당 없으면 null
  }

이벤트별 추가 필드:

  TARGET_LOCKED:
    "gap_pct", "expected_price", "expected_amount", "buy_sell_ratio"

  ENTRY_EXECUTED:
    "order_id", "order_price", "order_qty",
    "fill_price", "fill_qty", "fill_latency_ms"

  ENTRY_FAIL:
    "order_id", "order_price", "order_qty", "reason"

  TRAILING_STOP | HARD_STOP:
    "entry_price", "high_price", "exit_price", "exit_qty",
    "pnl_pct",    // (exit_price / entry_price - 1) * 100
    "fill_latency_ms"

  TIMEOUT_CLOSE:
    "entry_price", "exit_price", "exit_qty",
    "pnl_pct", "fill_latency_ms"

  PARTIAL_FILL:
    "order_id", "fill_qty", "order_qty", "unfilled_qty"

  TIMEOUT_ORDER_FAILED:
    "attempt_count", "last_error_code", "last_error_msg"

  SELL_ORDER_REJECTED:
    "order_id", "error_code", "error_msg", "remaining_qty"

  LATENCY_HIGH:
    "api_endpoint", "latency_ms", "network_ms", "rate_wait_ms",
    "client_setup_ms", "local_overhead_ms", "total_ms"

  PRICE_SPIKE_FILTERED:
    "received_price", "prev_price", "change_pct"

  VI_TRIGGERED | VI_RELEASED:
    "vi_type",    // STATIC_1 | STATIC_2 | DYNAMIC
    "vi_price", "current_price"

  CIRCUIT_BREAKER | TRADING_HALTED:
    "reason", "expected_resume"   // 예상 재개 시각 (null 가능)

  NETWORK_DOWN | NETWORK_RESTORED:
    "down_duration_sec"   // RESTORED 이벤트에만 포함

  PROCESS_RESTART_DETECTED:
    "recovered_status",   // HOLDING_RESUMED | ALREADY_CLOSED | DAY_SKIP_RESTORED | NO_STATE
    "actual_qty"          // KIS 잔고 조회 실제 보유 수량

  MACRO_KILL_SWITCH:
    "vix", "nasdaq_return_pct", "usd_krw",
    "usd_krw_return_pct", "usd_krw_ma20_deviation_pct",
    "risk_score", "triggered_rules", "source", "data_timestamp"

  STALE_POSITION_DETECTED:
    "state_date", "today_date", "actual_qty"

  TRAILING_STOP:
    "entry_price", "exit_price", "highest_step", "stop_price", "pnl_pct", "fill_latency_ms"

  SLIPPAGE_GUARD:
    "expected_price", "fill_price", "slippage_pct"

  GAP_CHANGED:
    "gap_at_lockup", "gap_at_entry", "reason"   // BELOW_MIN | ABOVE_MAX

  PYRAMID_SKIPPED:
    "entry_price", "current_price", "diff_pct"

  DAILY_STOP:
    "daily_pnl_pct", "threshold_pct"

  TIME_SYNC_WARN:
    "ntp_server", "offset_ms"

  NO_TARGET | VI_FILTER_ALL_EXCLUDED | INSUFFICIENT_BALANCE:
    "filter_count",   // 필터 통과 종목 수 (0 고정)
    "reason"

──────────────────────────────────────────────────
7-2. 데이터 디렉토리 보존 정책
──────────────────────────────────────────────────

  경로                      | 내용              | 보존 기간
  ─────────────────────────|-------------------|──────────
  data/logs/YYYYMMDD.jsonl  | 이벤트 로그        | 2년
  data/state/today_state.*  | 당일 포지션 상태    | 30일 (자동 덮어쓰기)
  data/params/history.json  | 파라미터 변경 이력  | 영구
  data/auth/token_cache.json| KIS 토큰 캐시      | 자동 갱신 (24시간)

● 로그 로테이션:
  2년 초과 로그 파일 자동 삭제 (크론 또는 스크립트).
  삭제 전 별도 스토리지(NAS, 클라우드)에 백업 권장.

● 디스크 용량 감시:
  잔여 용량 1GB 미만 시 DISK_LOW_WARN 로그 및 알림 발송.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
8. 파라미터 최적화 방안 (Parameter Optimization)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
8-1. 최적화 대상 파라미터 및 탐색 범위
──────────────────────────────────────────────────

  파라미터                  | 현재값  | 탐색 범위          | 탐색 단위
  ─────────────────────────|---------|--------------------|---------
  갭 하단 필터 (gap_min)    | 3.0%   | 2.0% ~ 4.0%       | 0.5%
  갭 상단 필터 (gap_max)    | 7.0%   | 6.0% ~ 9.0%       | 1.0%
  거래대금 상위 분위 (liq_q) | 10%   | 5% ~ 20%          | 5%
  자본 배분 비율 (alloc)    | 10%    | 5% ~ 20%          | 5%
  Hard Stop (hs)           | -2.0%  | -1.5% ~ -3.0%     | 0.5%
  Step 크기 (ss)           | +2.5%  | +1.5% ~ +4.0%     | 0.5%
  Step Trail 폭 (st)       | -2.0%  | -1.0% ~ -2.5%     | 0.5%
  VIX Severe (vix_severe)  | 30.0   | 27.5 ~ 35.0       | 2.5
  VIX Score (vix_score)    | 25.0   | 22.5 ~ 30.0       | 2.5
  NASDAQ Severe            | -2.5%  | -2.0% ~ -3.5%     | 0.5%
  NASDAQ Score             | -1.8%  | -1.2% ~ -2.5%     | 0.3~0.5%
  USD/KRW 일간 급등 Severe | +1.5%  | +1.0% ~ +2.0%     | 0.25%
  USD/KRW 일간 급등 Score  | +0.8%  | +0.5% ~ +1.2%     | 0.1~0.2%
  USD/KRW MA20 이격 Severe | +3.0%  | +2.0% ~ +4.0%     | 0.5%
  USD/KRW MA20 이격 Score  | +2.0%  | +1.0% ~ +3.0%     | 0.5%
  Macro risk score cutoff  | 2점    | 2점 ~ 3점         | 1점

  우선 최적화 대상 (민감도 높음): hs, ss, st
  2차 최적화 대상: gap_min, gap_max, liq_q, macro risk score cutoff
  고정 권장 (전략 근간): alloc (리스크 관리 정책 영역)

──────────────────────────────────────────────────
8-2. 목적 함수 (Objective Function)
──────────────────────────────────────────────────

단일 지표 최적화는 과적합을 유발한다. 다음 4개 지표를 복합 사용한다.

  ① Profit Factor = 총 수익 합계 / 총 손실 합계 절댓값
     목표: >= 1.5
     해석: 1.0 미만이면 전략 무효

  ② Win Rate = 수익 거래 수 / 전체 거래 수
     목표: >= 45%
     주의: 단독 지표로 쓰면 안 됨 (손익비와 반드시 병행)

  ③ Expectancy = (Win Rate × 평균 수익) - (Loss Rate × 평균 손실)
     목표: > 0 (양수)
     해석: 1회 거래당 기댓값. 가장 핵심 지표.

  ④ Max Drawdown (MDD)
     목표: <= 5% (자본 기준)
     역할: 수익 지표가 좋아도 MDD가 기준 초과 시 파라미터 기각.

  복합 스코어 (최적화 목표):
    Score = Expectancy × Profit_Factor / (1 + |MDD|)
    Score가 높을수록 우선 채택.

  Macro Kill Switch 채택 기준:
    - 킬 스위치 발동일 비율: 전체 거래 가능일의 3% ~ 7%
    - 킬 스위치 적용 후 MDD 감소: 미적용 대비 20% 이상
    - 킬 스위치 적용 후 Expectancy: 0 초과 유지
    - 제외된 날의 평균 손익: 전체 평균 손익보다 낮아야 함
    - Out-of-Sample에서도 손실 축소 방향성이 유지되어야 함

──────────────────────────────────────────────────
8-3. 백테스팅 데이터 요건
──────────────────────────────────────────────────

● 데이터 범위: 최소 2년 (약 500 영업일) KOSPI+KOSDAQ 전종목 분봉 데이터
  권장: 3년 이상 (강세장 + 약세장 + 횡보장 구간 포함)

● 필수 포함 구간:
  - 고변동성 구간: 2022년 하락장, 2024년 8월 급락
  - 저변동성 구간: 2023년 박스권
  이유: 갭 전략은 시장 국면에 따라 성과가 크게 달라짐.

● 데이터 해상도: 1분봉 (F4 Trailing 시뮬레이션용)
  틱 데이터 사용 시 정확도 향상되나 데이터 비용 고려.

● 슬리피지 가정:
  시장가 매수/매도 슬리피지: +0.05% / -0.05% (각각 불리한 방향)
  이유: 실제 체결가는 호가 스프레드만큼 불리하게 체결됨.

● 수수료: 0.015% (증권사 온라인 기준) 왕복 적용.

──────────────────────────────────────────────────
8-4. 최적화 방법론 (Walk-Forward Analysis)
──────────────────────────────────────────────────

단순 전체 구간 최적화(In-Sample Only)는 과적합을 유발한다.
Walk-Forward 방식을 필수 적용한다.

  [구조]
  In-Sample(IS) 12개월 → Out-of-Sample(OOS) 3개월 → 슬라이딩

  예시 (3년 데이터 기준):
  ┌──────────────────────┬──────────┐
  │ IS: 2022.01~2022.12  │ OOS: Q1 2023 │
  ├──────────────────────┼──────────┤
  │ IS: 2022.04~2023.03  │ OOS: Q2 2023 │
  ├──────────────────────┼──────────┤
  │ IS: 2022.07~2023.06  │ OOS: Q3 2023 │
  │ ...                  │ ...      │
  └──────────────────────┴──────────┘

  각 윈도우에서:
  1. IS 구간: Grid Search로 복합 스코어 최고 파라미터 탐색.
  2. OOS 구간: IS에서 선정된 파라미터로 실제 성과 검증.
  3. OOS Expectancy < 0 인 윈도우 비율이 30% 초과 시 전략 재검토.

──────────────────────────────────────────────────
8-5. 과적합 방지 규칙
──────────────────────────────────────────────────

● 파라미터 조합 수 제한:
  전체 탐색 조합이 10,000개 초과 시 Random Search (조합의 10% 무작위 샘플링) 사용.

● 거래 수 최소 기준:
  IS 구간 거래 수 < 60건이면 해당 파라미터 셋 기각.
  (소표본 과적합 방지)

● 민감도 검증 (Robustness Check):
  최적 파라미터 확정 후, 각 파라미터를 ±1 단위 변동 시
  Score 변화가 20% 이내인 경우에만 채택.
  변화가 20% 초과 시 해당 파라미터는 과적합 의심, 인접 안정 구간으로 후퇴.

● IS vs OOS 성과 괴리 기준:
  OOS Expectancy / IS Expectancy < 0.5 이면 해당 윈도우 파라미터 기각.

──────────────────────────────────────────────────
8-6. 실운용 파라미터 갱신 주기
──────────────────────────────────────────────────

● 정기 재최적화: 분기 1회 (매 3개월)
  직전 12개월 IS + 직전 3개월 OOS 검증 후 파라미터 갱신.

● 비정기 재최적화 트리거:
  - 연속 5 거래일 Expectancy < 0
  - 누적 MDD가 목표(5%) 초과
  - 시장 구조 변화 감지 (거래세 변경, VI 기준 변경 등)

● 파라미터 변경 이력은 data/params/history.json 에 버전 관리:
  {
    "version"   : "v3",
    "applied_at": "2026-09-01",
    "params"    : { "hs": -0.020, "ta": 0.015, "tw": -0.025, ... },
    "is_score"  : 0.87,
    "oos_score" : 0.71
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
9. 일일 운영 타임라인 요약
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  시각          | 동작
  ─────────────|──────────────────────────────────────────────────────────────
  08:00:00      | F0 Infrastructure Boot — 시스템 자원, 프로세스, 이벤트 루프 상태 점검
  08:05:00      | F0 Macro Kill Switch — VIX/NASDAQ/USD-KRW 기반 당일 No Trade 판정
  08:10:00      | F0 Network Sync — KIS 및 외부 금융 API 레이턴시/연결성 점검
  08:20:00      | F0 Fail-Safe Link Check — 비상 알림 채널 송신 가능 여부 확인
  08:30:00      | KIS 토큰 선제 갱신 / 휴장일 여부 확인 / NTP 시각 동기화 검증
  08:40:00      | F1 시작 — today_state.json 초기화, 갭/거래대금 필터링 (Rate Limit 준수)
  08:58:00      | F2 시작 — 복합 정렬, 타겟 후보 락업
  진입 가능 시간 | F3 — 갭·실제 VI·최종 호가 재검증 후 100% 상한 지정가 주문
  체결 직후      | F3 체결 확인 / 슬리피지 가드 적용 / F4 WebSocket 추적 시작
  09:00~10:49   | F4 — Hard Stop(-2%) / Step Trailing(스텝-2.0%) 감시
  15:05~15:14   | F4 — Step Trailing 강제 활성화 (스텝 미달성 시 진입가-2.0% 발동)
  15:14:50      | F5 Pre-Check — 잔고 조회 및 prefetch_qty 저장
  15:15:00      | F5 Execute — 미청산 물량 시장가 청산 (Retry 최대 3회)
  15:15:00+     | 로그 최종 기록, today_state.json CLOSED 갱신, daily_pnl_pct 갱신, 알림 발송
---

## 10. 2026-07-01 구현 업데이트

### F1 장전 후보 조회

- F1은 랭킹 후보를 먼저 수집한 뒤 예상체결가/예상체결수량을 보강한다.
- 예상체결가 보강은 `F1_EXPECTED_QUOTE_CONCURRENCY` 세마포어로 동시성을 제한한다.
- 모든 KIS REST 호출은 `KIS_RATE_INTERVAL_SEC` 기준으로 직렬 슬롯을 예약한다.
- KOSPI/KOSDAQ 랭킹 조회 사이에는 `F1_MARKET_INTERVAL_SEC` 대기 시간을 둔다.
- 후보 스냅샷은 `F1_SNAPSHOT_DIR` 단일 상수를 기준으로 저장/조회한다.
- 음수 갭은 `NEGATIVE_GAP`으로 분류해 통과 후보에서 제외한다.
- 화면의 오늘 후보 표시는 랭킹 순서보다 통과 가능성, 유동성, 예상체결 유효성을 우선한다.

### F3 진입 재시도

- F3 진입 실패 시 짧은 재시도를 허용한다.
- 재시도 정책은 다음 환경변수로 제어한다.
  - `F3_ENTRY_MAX_ATTEMPTS`
  - `F3_ENTRY_RETRY_DELAY_SEC`
  - `F3_LIMIT_FILL_TIMEOUT_SEC`
  - `F3_ENTRY_RETRY_DEADLINE`
- 매수 주문 직전에는 `F3_PRE_ORDER_QUIET_SEC`만큼 대기해 직전 조회 호출과 주문 호출이 연속으로 붙지 않게 한다.
- 마감(`F3_ENTRY_RETRY_DEADLINE`, 기본 09:11) 검사는 재시도뿐 아니라 최초 주문에도 적용한다.
  비강제 실행은 잔고 조회 전, 1차 주문 준비 전, 매 시도의 `_send_buy` 호출 직전, 그리고
  KIS rate-limit 대기 후 실제 HTTP 전송 직전에 검사한다 — quiet wait·수량 조회·REST 대기 중
  마감을 넘겨도 주문이 나가지 않는다.
  전송 직전 차단 시점에는 이미 ENTERING이므로 IDLE로 되돌린 뒤
  `ENTRY_DEADLINE_PASSED`로 기록한다 (force 캐치업은 예외).
- 각 시도에서 미체결이면 취소 주문을 전송한다. 마지막 시도 미체결 주문도 반드시 취소 대상이다.
- 체결조회 타임아웃, 주문 전송, 취소 전송, 재시도 시작/생략은 각각 로그 이벤트로 남긴다.
- 최종 미체결이면 `ENTRY_FAIL`로 기록하고 당일 진입은 종료한다.

### DRY_RUN 테스트 모드

- `DRY_RUN=1`은 외부 KIS 인증/API/주문/WebSocket을 사용하지 않는 안전한 시뮬레이션 모드다.
- DRY_RUN 데이터는 별도 경로를 사용한다.
  - `DRY_RUN_LOG_DIR`
  - `DRY_RUN_STATE_DIR`
  - `DRY_RUN_DB_DIR`
- F1/F3/F4 시뮬레이션 입력값은 다음 값으로 조정한다.
  - `DRY_RUN_TICKER`
  - `DRY_RUN_PREV_CLOSE`
  - `DRY_RUN_EXPECTED_PRICE`
  - `DRY_RUN_EXPECTED_QTY`
  - `DRY_RUN_ENTRY_PRICE`
  - `DRY_RUN_ENTRY_QTY`

### Telegram 알림 문구

- Telegram 알림은 내부 이벤트 코드가 아니라 운영자 메시지로 작성한다.
- 형식은 `제목 -> 상황 -> 조치 -> 세부 -> 코드` 순서를 따른다.
- `CRIT` 알림은 조치 문구에 수동 확인 또는 수동 처리 필요 여부를 포함한다.
- 예: `STALE_POSITION_DETECTED`는 “전일 포지션 잔류 의심”으로 표시하고, 계좌 보유 수량과 미체결 주문 확인을 조치로 안내한다.

### UI 진행 단계 표시

- 하단 파이프라인은 현재 `position_status`만 보지 않고 오늘 로그 기준 진행 단계도 반영한다.
- `ENTRY_FAIL` 후 상태가 `IDLE`로 돌아가도 오늘 진행 단계는 F3 실패로 유지한다.
- `/api/status`는 `pipeline_stage`, `pipeline_failed`를 반환한다.

### Catch-up 체인 실행

- F1 시작 이후 F3 체결 확인 마감 전 프로세스가 시작되면 catch-up으로 F1을 보완 실행한다.
- catch-up에서 F1 결과가 나오면 예약 F2 시각을 기다리지 않고 F2/F3 체인을 즉시 실행한다.
- F3 예정 시각이 이미 지났더라도 신규 주문 진입 마감은 우회하지 않는다.
- F3 체결 확인 마감 이후에는 `FORCE_CATCHUP` 설정 여부와 무관하게 신규 진입을 금지한다.

### UI 메뉴 데이터 기준

- `자산` 메뉴는 계좌 현재 스냅샷을 담당한다.
  - 총평가금액, 예수금, 주문가능금액, 보유종목 수, 주식평가금액, 평가손익을 표시한다.
  - 원천 데이터는 KIS 잔고 조회 기반 `/api/assets` 응답이다.
  - KIS 잔고 조회 성공 결과는 `asset_snapshots` DB 테이블에 저장해 감사/장애 분석 이력으로 활용한다.
- `주문` 메뉴는 주문 실행과 처리 결과를 담당한다.
  - 주문번호, 종목, 매수/매도, 주문수량, 주문가격, 체결수량, 상태, 주문 단계, 주문시각/체결시각을 표시한다.
  - 원천 데이터는 `/api/orders`가 반환하는 `orders`와 `entry_order_attempts` 통합 조회 결과다. 체결 전 취소된 진입은 감사 원장에서 보완하고, 체결 후 `orders`가 생성된 동일 KIS 주문번호는 중복 표시하지 않는다. 주문 관련 JSONL 이벤트 로그는 즉시 갱신 신호와 진단 보조 정보로 사용한다.
  - `UNCERTAIN`은 일반 미체결과 구분해 `MTS 즉시확인`으로 표시하고 별도 요약 건수에 포함한다. 감사 원장 보완 행은 화면에서 출처를 표시한다.
- 주문/체결 내역은 `/api/stream` 로그 이벤트 수신 시 즉시 갱신하고, 5초 폴링을 백업으로 사용한다.
- `주문가능금액`은 계좌 자산 데이터로 분류한다. 주문 메뉴에서 표시할 수는 있지만, 주문 판단용 보조 참조값으로만 사용하고 주요 지표처럼 강조하지 않는다.
- 주문 메뉴 제목은 사용자가 수동 주문 화면으로 오해하지 않도록 `주문/체결 내역`을 우선 사용한다. 사이드 메뉴 라벨은 짧게 `주문`을 유지할 수 있다.
- `오늘` 메뉴의 보유 후 가격흐름 차트는 진입 시각부터 현재 또는 청산 시각까지를 표시한다.
  - 보유 중에는 최근 20분 슬라이딩 창을 사용하고, 청산 후에는 진입부터 마지막 체결/tick까지의 범위를 고정한다.
  - `/api/status.tick_history`의 최근 원시 tick과 독립된 `minute_price_history`를 합쳐, 5,000개 tick이 20분을 담지 못해도 앞 구간을 분 단위 이력으로 보완한다.
  - SSE tick은 기존 배열에 증분 추가하고 렌더링은 최소 150ms 간격으로 합친다. 화면 폭을 넘는 데이터는 구간별 최솟값/최댓값을 보존해 다운샘플링한다.
  - x축은 실제 시각이며 범위에 따라 1/2/5/10분 격자와 1/2/5/10/15/30/60분 라벨 간격을 적용한다.
  - 차트는 컨테이너 폭 100%를 사용하며, 당일 체결 주문은 `trade_marks` 매수/매도 마커로 표시한다.
  - 마지막 가격은 가격 점, 현재가, 마지막 매도 마커, 마지막 매수 마커 순으로 보완한다.

---

## 11. 2026-07-24 청산 상태·관측성 구현 업데이트

### 청산 상태 머신

- F4는 매도 접수 확인 후, F5는 청산 수량 확정 후 첫 주문 전에 `EXITING`을
  영속화하고 전량 체결 확인 전에는 `CLOSED`로 확정하지 않는다.
- 부분·미확인 체결은 `remaining_qty`와 OPEN 거래를 보존하며 운영자 대사 대상으로 남긴다.
- 당일 `EXITING` 재시작은 자동 재매도하지 않고 신규 진입을 차단한다.

### F4 가격 관측

- WS와 REST는 `_handle_price_tick`을 공유한다.
- CLOSED 관측 창에서는 차트 가격만 추가하고 `_process_tick`, VI, 주문 경로를 실행하지 않는다.
- `F4_POST_CLOSE_OBSERVE_UNTIL`은 로깅 초기화 후 지연 파싱하며 오타는 1회 경고한다.
- 손상된 `entry_at`은 값별 1회 WARN 후 관측을 중단한다.

### KIS REST·F1 진단

- KIS REST는 공유 AsyncClient로 연결 풀을 재사용하고 종료 시 명시적으로 닫는다.
- `LATENCY_HIGH`는 실제 네트워크 지연과 로컬 rate-limit 대기·클라이언트 준비 시간을 분리한다.
- F1 완료/진행 로그는 `processed_count`, `eligible_count`, `skipped_count`,
  `quote_valid_count`, `quote_fallback_count`, `error_count`, `skip_reasons`를 포함한다.

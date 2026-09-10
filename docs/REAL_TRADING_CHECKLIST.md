# 실전투자 전환 점검표

최종 코드 점검일: 2026-08-11

이 문서는 PAPER에서 정상 동작했다는 사실만으로 REAL을 활성화하지 않도록 코드 수준의 차이와 승인 조건을 기록한다. `python scripts/real_readiness.py`의 `eligible_for_real`이 `false`이거나 아래 미완료 항목이 하나라도 남아 있으면 상주 프로세스를 `KIS_MODE=REAL`로 시작하지 않는다.

## 현재 판정 (2026-08-11)

```text
준비도                  51/100
REAL 진입 가능          아니오
현재 전략 fingerprint   4e34b3d9b787
동일 fingerprint PAPER  0/20
```

현재 점수는 `.env`와 운영 DB를 읽어 산출한 값이다. 문서를 보고 승인값을 먼저 바꾸지 말고 아래 명령으로 매번 다시 확인한다.

```powershell
python scripts/real_readiness.py
```

### 현재 남은 REAL-BLOCKER

| 차단 항목 | 현재값 | 해제 조건 |
|---|---|---|
| 동일 버전 PAPER 정상 청산 | `0/20` | fingerprint `4e34b3d9b787`로 진입·전량 청산이 정상 대사된 PAPER 거래 20회. 카운트 시작 전 "0. 전략 코드 동결" 선행 |
| 실전 접속 설정 검증 | 미승인 | 실전 키·계좌·REST/WS URL을 실제 조회로 확인한 뒤 `REAL_CONFIG_VERIFIED=1` |
| REAL 최소수량 스모크 | 미승인 | 아래 스모크 절차의 주문·취소·체결·잔고 대사를 완료한 뒤 `REAL_SMOKE_TEST_APPROVED=1` |
| CRIT 알림 실수신 | 미승인 | 휴대폰에서 CRIT 메시지를 확인한 뒤 `REAL_ALERT_TEST_APPROVED=1` |
| 운영자 장애 대응 훈련 | 미승인 | MTS 수동청산·프로세스 중지·잔고 대사를 실제로 수행한 뒤 `REAL_OPERATOR_DRILL_APPROVED=1` |

### 현재 통과 중인 REAL 안전 설정

아래 항목은 차단 목록에서 빠졌지만 `src/readiness.py`의 준비도 게이트에는 계속 포함된다. 설정이 바뀌면 즉시 다시 REAL-BLOCKER가 된다.

| 게이트 항목 | 현재값 | 통과 조건 |
|---|---|---|
| 초기 투입비율 | `F3_ALLOC_RATIO=0.20` | `0 < F3_ALLOC_RATIO <= 0.20` |
| PAPER 실험 기능 | `PAPER_FAST_PROBE=0`, `PAPER_FAST_HYBRID=0` | 둘 다 `0` |
| 최종 호가 신선도 | `F3_FINAL_QUOTE_MAX_AGE_MS=500` | `500ms` 이하 |

오늘 09:00 거래는 fingerprint `58a5a3ba697e`로 실행돼 현재 버전 실적에 포함되지 않는다. 현재 코드로 오늘 F1 스냅샷을 재생하면 한화솔루션이 F1·F2·F3 1순위이고 10% 미만 갭/지정가 안전 검사를 통과하지만, 이는 오프라인 재생 결과이지 현재 fingerprint의 정상 청산 실적이 아니다.

## 이번 점검에서 완료된 항목

- KIS REST는 프로세스 수명 동안 공유 `httpx.AsyncClient`를 사용하고, 지연을 `network_ms`, `rate_wait_ms`, `client_setup_ms`, `local_overhead_ms`, `total_ms`로 분리한다.
- 청산 시 `trades.exit_qty`, 수수료·세금 제외 `pnl_amount`, 청산 직전 `high_price`를 하나의 DB UPDATE로 저장한다.
- 기존 CLOSED 거래의 비어 있는 `exit_qty`와 `pnl_amount`는 저장된 매도 체결 주문으로 기동 시 보완한다.
- 이미 DB에 더 높은 `high_price`가 있으면 청산 시 낮은 값으로 덮어쓰지 않는다.
- 시장가 주문의 제출가(`order_price=0`), 판단 기준가(`trigger_price`), 체결가(`fill_price`)를 분리한다.
- F3/F4/F5의 확인된 체결은 주문 호출 직전부터 체결 조회 확인까지의 `fill_latency_ms`를 기록한다. 체결 미확인 주문은 `PENDING`, 부분체결은 `PARTIAL_FILL`로 남긴다.
- F1 후보 처리 로그는 처리·적격·제외·예상가 성공/대체·오류 건수와 제외 사유별 건수를 함께 기록한다.
- 실전투자 준비도를 100점으로 계산하며, 100% 미만이면 REAL 프로세스 시작과 REAL 매수 주문을 이중 차단한다. 청산 매도는 안전을 위해 차단하지 않는다.
- F1은 8~10% 고갭 후보를 정적 VI 근접만으로 제거하지 않는다. 예상 체결대금 50억원 이상인 경우에만 허용하며, 실제 VI 활성 여부는 F3 주문 직전에 확인한다.
- F3 매수는 설정으로 해제할 수 없는 지정가 전용이다. 신선한 최종 1호 매도호가의 1% 상한과 전일 종가 대비 10% 미만의 마지막 유효 호가 중 낮은 가격만 제출한다.
- 10% 경계는 `Decimal`과 KRX 호가단위로 계산해 정적 VI 발동가와 같은 가격에는 주문하지 않는다. 최종 호가가 오래됐거나 갭이 2~10% 범위를 벗어나면 주문 전송 없이 차단한다.
- VI 대기, 잔고 재시도, 주문 재시도 뒤에도 진입 마감(기본 09:11)과 최종 호가 신선도를 다시 확인한다. `FORCE_CATCHUP`은 이 마감을 우회하지 않는다.
- 미체결 지정가는 취소와 잔량 대사를 마친 뒤에만 재시도한다. 레거시 시장가 매수 및 호출자가 없던 `force=True` 진입 경로는 제거했다.
- KIS가 접수한 모든 진입 시도는 상태 파일을 먼저 저장한 뒤 `entry_order_attempts` 감사 원장에 기록한다. 무체결 취소도 `CANCELLED`로 남고, `FIRST_BUY`/`PYRAMID_BUY` 단계가 보존된 채 `/api/orders`에서 조회된다.
- REAL 왕복 스모크 매수도 운영 F3와 동일한 신선 호가·2~10% 갭·1% 호가 상한·절대 10% 미만 지정가 정책을 사용하며 시장가 매수로 폴백하지 않는다.
- pytest의 상태·probe·로그·DB·인증 출력은 테스트별 임시 디렉터리로 격리한다. 테스트 로그를 PAPER 운영 증거로 집계하지 않는다.

## 해결된 이전 REAL-BLOCKER

### R1. F4 전량 체결 확인과 부분체결 처리 [완료]

F4는 주문수량과 확인된 누적 체결수량을 비교한다. 전량 체결이 확인된 경우에만 `orders`를 `FILLED`로 갱신하고 `trades`와 상태를 닫는다.

- 부분체결은 `PARTIAL_FILL`로 기록하고 확인된 체결수량만큼 `remaining_qty`를 줄인다.
- 체결 미확인 주문은 체결가를 트리거 가격으로 대체하지 않고 `PENDING`으로 유지한다.
- 부분·미확인 체결에서는 `trades`를 OPEN으로 유지하고 상태를 `EXITING`으로 보존한다.
- `F4_CLOSE_PENDING` CRIT 로그와 알림으로 주문·잔고 수동 대사를 요청한다.
- F4는 불명확한 주문을 자동 재전송하지 않는다.

### R2. HOLDING과 CLOSED 사이의 영속적인 EXITING 상태 [완료]

F4/F5는 다음 전이를 사용한다.

```text
HOLDING -> EXITING -> CLOSED
                 \-> EXITING (부분체결·체결 미확인·재시도 실패)
```

- F4는 주문 접수 확인 후, F5는 청산 수량 확정 후 첫 주문 전에 `EXITING`을
  상태 파일에 저장하고 확인된 잔여수량을 후속 저장한다.
- 주문번호·주문수량·체결수량은 `orders`에, 현재 잔여수량과 청산 사유는 상태 파일에 저장한다.
- 전량 체결 확인 시에만 `remaining_qty=0`으로 만들고 `CLOSED`로 전환한다. F5 사전 잔고조회에서 실제 미보유가 확인된 경우는 주문 없이 CLOSED로 확정하되 DB 거래는 수동 대사를 위해 열어 둔다.
- 재시작 시 당일 `EXITING`을 복원하고 신규 진입·자동 재매도를 차단한 뒤 `EXITING_REQUIRES_RECONCILIATION` CRIT 알림을 보낸다.
- 전일 `EXITING`은 stale position으로 격리하고 자동 진입을 차단한다.

### R3. F4/F5 주문 응답 유실 및 재시작 대사 [완료]

- 매도 전 로컬 상관 ID와 주문시각·종목·수량을 `orders` 및 상태 파일에 먼저 저장한다.
- 전송 직전 `SUBMITTING`, 정상 응답은 `ACKNOWLEDGED`, 전송 결과가 불명확하면 `UNKNOWN`으로 영속화한다.
- POST 응답 유실 시 당일 주문내역을 조회해 주문시각·종목·수량이 유일하게 일치하는 기존 매도만 복구한다.
- 일치 주문이 없거나 여러 개이거나 조회가 실패하면 자동 재전송하지 않고 `EXITING`을 유지해 중복 매도를 막는다.
- 매도 POST의 HTTP 429 응답은 확정 거절로 간주하지 않는다. 서버 도달 후 접수 여부가 불명확할 수 있으므로 자동 재전송 없이 `UNKNOWN`으로 저장하고 주문내역을 대사한다. 제한된 대사 창에서 주문을 찾지 못하면 `EXITING`과 CRIT 알림을 유지하며, 운영자가 KIS 주문·잔고를 확인한 뒤 수동 청산한다. HTTP 200으로 전달된 명시적 KIS 업무 거절은 확정 거절로 처리한다.
- 재시작 시 DB와 상태 파일의 의도를 병합하고 체결 합계를 대사해 전량 체결일 때만 `CLOSED`로 전환한다.
- F4 손절·트레일링 청산과 F5 마감 청산이 동일한 복구 계층을 사용한다.

## 조건부 REAL-BLOCKER

### R4. 피라미딩을 다시 활성화하려면 평균단가·수량 집계를 수정해야 함

현재 `FIRST_RATIO=1.00`이라 2차 매수는 비활성이다. 이를 낮추면 상태의 수량은 늘지만 `trades.entry_price`와 `entry_qty`가 체결 가중평균과 총수량으로 갱신되지 않아 손익이 틀릴 수 있다. 준비도 게이트도 `FIRST_RATIO=1.00`만 승인한다. 피라미딩을 다시 활성화하려면 해당 집계와 재시작 복구 테스트를 먼저 완성한다.

## 준비도 100점 기준

| 구분 | 배점 | 통과 조건 |
|---|---:|---|
| 주문 안전성 | 30 | 매도 의도 선저장, 응답 유실 대사, 재시작 복구 구현 |
| 동일 버전 PAPER 실적 | 30 | 현재 전략 코드 지문으로 정상 청산 20회(비례 점수) |
| REAL 안전 설정 | 25 | 초기 투입 20% 이하, 실험 기능 OFF, 신선 호가·REST 백업, 실전 설정 검증 등 |
| 실전 운영 검증 | 15 | 최소수량 스모크 테스트, CRIT 알림 실수신, MTS 수동청산 훈련 |

코드 지문은 주문·전략·상태 복구 핵심 파일의 내용으로 계산한다. 대상은 `src/release.py`의 `_STRATEGY_FILES` 19개 파일이며, 이 파일들이 변경되면 PAPER 실적은 새 지문으로 다시 쌓아야 한다. 20회 카운트를 시작하기 전에 반드시 "0. 전략 코드 동결"을 먼저 수행한다. `python scripts/real_readiness.py` 또는 웹의 설정 화면에서 현재 점수와 차단 사유를 확인한다.

PAPER 정상 청산 1회로 인정되려면 다음 조건을 모두 만족해야 한다.

- `trades.execution_mode='PAPER'`이고 `strategy_fingerprint`가 현재 값과 같다.
- `trades.status='CLOSED'`, `entry_qty > 0`, `exit_qty = entry_qty`다.
- 연결된 매도 주문에 `PENDING`, `PARTIAL_FILL`, `FAILED`가 남아 있지 않다.
- pytest·수동 합성 데이터가 아니라 격리된 운영 PAPER 프로세스가 만든 거래다.

## 실전 전 권장 보완

- 이론적 스탑과 KRX 호가단위로 보정한 실행 트리거 가격을 구분해 기록한다.
- F1 종료 시 예상체결가 조회의 실제 네트워크 지연 `p50/p95/max`, 표본 수, 실패·호출 제한 건수를 요약 로그로 남긴다.
- `pnl_amount`는 현재 수수료·세금 제외 값이다. 계좌 실현손익과 대사하려면 비용 컬럼을 별도로 저장한다.
- 실전 TR ID, 응답 필드명, 취소가능수량 조회, 휴장일 조회를 최소 수량 주문으로 검증한다.
- `RATE_LIMIT_HIT`, 토큰 갱신, WebSocket 단절, REST 백업 전환을 장 시작 전 실전 서버에서 확인한다.
- 프로세스·네트워크 장애 시 MTS/HTS로 즉시 수동 청산할 운영자를 09:00~15:15(F5 마감 청산까지) 동안 확보한다.

## REAL 전환 실행 순서

### 0. 전략 코드 동결

1단계의 20회 카운트를 시작하기 전에 전략 코드를 동결한다. 이 단계를 건너뛰면 뒤에서 한 줄만 고쳐도 그때까지 쌓은 실적이 전부 무효가 된다.

- 동결 대상은 `src/release.py`의 `_STRATEGY_FILES` 19개 파일과 비밀값·계좌·경로를 제외한 전략 환경설정이다. 파일이나 로드된 전략 override가 바뀌면 fingerprint가 달라지고 PAPER 실적은 0회부터 다시 시작한다.
- `trades.date`가 `UNIQUE`라 **하루 최대 1거래**다. 20회는 최소 20 거래일이고, F1·F2·F3 게이트를 통과하는 날에만 거래가 생기므로 실제로는 2~3개월을 예상한다. 15거래일째의 사소한 리팩터링 한 건이 그 두 달을 되돌린다.
- 리팩터링, 주석 정리, 죽은 코드 삭제, 로그 문구 변경도 모두 fingerprint를 바꾼다. 동작이 같아도 예외가 아니다.
- 알려진 개선 항목과 미완 정리는 **이 단계에서 모두 소진**한다. "나중에 정리하자"로 남긴 항목은 카운트 도중 손대게 되고, 그 시점에 리셋 비용이 발생한다.
- 동결 대상이 아닌 파일(`docs/`, `tests/`, `api_tests/`, `scripts/`, `src/api/server.py`, `docs/html/`)은 카운트 중에도 자유롭게 수정할 수 있다. 대시보드·문서·테스트 보강은 실적에 영향을 주지 않는다.
- 동결 시점의 fingerprint를 아래 명령으로 기록하고, 이 문서 상단 "현재 판정"에 반영한다.

```powershell
python scripts/real_readiness.py
```

- 카운트 중에는 매 거래일 시작 전 같은 명령으로 fingerprint가 기록값과 동일한지 확인한다. 달라졌다면 그날까지의 실적은 이미 무효이므로, 원인을 확인하고 0단계부터 다시 시작한다.
- 동결 해제가 불가피하면(실전 안전성에 영향을 주는 결함 발견 등) 리셋을 감수하고 명시적으로 0단계를 다시 수행한다. 카운터를 유지한 채 전략 파일을 고치는 예외는 없다.

### 1. PAPER 증거 확정

- `KIS_MODE=PAPER`, `DRY_RUN=0`을 유지한다.
- 0단계에서 기록한 fingerprint와 `python scripts/real_readiness.py`의 현재 fingerprint가 같은지 확인한 뒤 시작한다.
- 같은 fingerprint로 PAPER 정상 청산 20회를 채운다. 전략 파일이 변경돼 fingerprint가 바뀌면 0회부터 다시 시작한다.
- 부분체결, 주문응답 유실, 취소 미확인, SQLite 장애, 재시작 복구 테스트가 모두 통과하는지 확인한다.
- 운영 PAPER 로그와 pytest 출력이 섞이지 않았는지 `LOG_DIR`, `DB_DIR`, `STATE_DIR`, `PAPER_FAST_PROBE_DIR`을 확인한다.

### 2. REAL 읽기 전용 점검

상주 봇은 중지하고 MTS에서 보유수량과 미체결 주문이 0인지 먼저 확인한다. 그 다음 아래 설정을 적용한다.

```dotenv
KIS_MODE=REAL
DRY_RUN=0
FORCE_CATCHUP=0
F3_ALLOC_RATIO=0.20
F3_FINAL_QUOTE_MAX_AGE_MS=500
PAPER_FAST_PROBE=0
PAPER_FAST_HYBRID=0
F4_REST_BACKUP_ENABLED=1
F4_REST_ONLY_WHEN_WS_STALE=1
F4_WS_STALE_SEC=2.0
F4_WS_HEALTH_LOG_COOLDOWN_SEC=60.0
KIS_LOW_PRIORITY_MAX_WAIT_SLOTS=25
```

- 실전용 `KIS_APP_KEY`, `KIS_APP_SECRET`, 계좌번호와 상품코드를 사용한다.
- `KIS_BASE_URL=https://openapi.koreainvestment.com:9443`, `KIS_WS_URL=ws://ops.koreainvestment.com:21000`인지 확인한다.
- `STOCK_SKIP_DOTENV`는 테스트 전용이므로 운영 환경에 설정하지 않는다.
- `F4_REST_BACKUP_ENABLED=1`, `F4_REST_ONLY_WHEN_WS_STALE=1`, 런타임 `FIRST_RATIO=1.00`을 확인한다.
- `F4_WS_STALE_SEC=2.0`은 정확성을 위해 유지한다. 실제 WS 연결 끊김은 즉시 REST 백업을 깨우고, 연결된 상태의 무틱만 `F4_WS_HEALTH_LOG_COOLDOWN_SEC`로 반복 로그를 집계한다. REST 백업이 꺼져 있으면 무틱도 WARN이다.
- `KIS_LOW_PRIORITY_MAX_WAIT_SLOTS=25`는 PAPER/REAL의 서로 다른 호출 간격에 같은 슬롯 수 기준을 적용한다. 주문 전송·최종 호가는 CRITICAL, 체결·주문상태 확인은 ORDER_STATUS이며, 오래 기다린 F4 백업 시세에는 starvation 상한이 적용된다.
- 주문 플래그 없이 `python api_tests/run_all.py`를 실행해 인증·체결조회·잔고·매수가능수량·취소가능수량 조회를 검증한다.
- 조회 결과의 계좌번호, 예수금, 보유종목이 MTS와 일치할 때만 `REAL_CONFIG_VERIFIED=1`로 승인한다.

### 3. REAL 최소수량 스모크

이 단계는 실제 주문을 발생시킨다. 장중에 운영자가 MTS를 열어 둔 상태에서 실행 시점의 F1 적격 종목 1주로만 수행한다. `REAL_SMOKE_TICKER`는 최종 매도호가 기준 갭이 2% 이상 10% 미만인 종목으로 실행 직전에 지정한다.

```powershell
python api_tests/cancel.py --confirm
$env:REAL_SMOKE_TICKER="현재_F1_적격_종목코드"
python api_tests/order.py --confirm
python api_tests/ccld.py
python api_tests/balance.py
```

- `--confirm`이 없으면 주문 HTTP 호출 없이 종료한다.
- `--confirm`은 준비도 100점을 얻기 위한 최초 REAL 스모크 매수에만 명시적인 1회 우회를 허용한다. 상주 `main.py`의 REAL 매수 게이트는 열지 않는다.
- 취소 스모크는 1주 지정가 주문의 주문번호·원주문 조직번호를 받은 뒤 전량 취소하고, 체결량 0·잔량 0을 확인한다.
- 왕복 스모크는 신선 최종 호가의 1% 상한과 전일 종가 대비 10% 미만 상한 중 낮은 가격으로 1주 지정가 매수하고, 같은 수량을 시장가 매도해 모두 체결 확인한다. 호가가 오래됐거나 갭 범위를 벗어나면 주문 없이 실패한다.
- 지정가가 제한 시간 안에 체결되지 않으면 원주문을 취소하고 잔량·체결 경쟁을 대사한다. 취소가 확인되지 않으면 성공으로 처리하지 않으며 MTS 수동 확인을 요구한다.
- 스모크 전후 CCLD 주문번호·매수/매도 체결수량과 잔고 0을 MTS에서 대사한다.
- `data/logs/YYYYMMDD.jsonl`의 `REAL_SMOKE_BUY_AUTHORIZED` CRIT 기록과 터미널 결과를 보존한다. 스모크 스크립트는 운영 거래 DB를 만들지 않으므로 MTS·CCLD·잔고·로그가 승인 근거다.
- 어느 단계에서든 매도 체결 또는 잔고 0이 확인되지 않으면 즉시 MTS로 수동 청산하고 `REAL_SMOKE_TEST_APPROVED`를 `0`으로 유지한다.
- 모든 대사가 끝난 뒤에만 `REAL_SMOKE_TEST_APPROVED=1`로 설정한다.

### 4. 운영 승인과 최초 기동

- CRIT 테스트 메시지를 휴대폰에서 직접 확인한 뒤 `REAL_ALERT_TEST_APPROVED=1`로 설정한다.
- 프로세스 종료·네트워크 단절을 가정하고 MTS 수동 매도 및 잔고 대사를 수행한 뒤 `REAL_OPERATOR_DRILL_APPROVED=1`로 설정한다.
- `python scripts/real_readiness.py` 결과가 정확히 100점, `eligible_for_real=true`, `blockers=[]`인지 확인한다.
- 첫 기동 전 보유수량·미체결 주문 0, 시스템 시간 동기화, 절전 해제, 유선/안정 네트워크, Telegram 수신 상태를 확인한다.
- 첫 1~2주는 09:00~15:15 동안 운영자가 화면과 MTS를 함께 모니터링한다.

## REAL 승인 기준

다음 조건을 모두 만족해야 한다.

1. 실전투자 준비도가 100%이며 REAL 게이트의 차단 사유가 없다.
2. PAPER에서 부분체결, 체결조회 실패, 주문응답 유실, 재시작 시나리오가 통과했다.
3. 실전 서버 최소 수량 스모크 테스트에서 주문·취소·체결·잔고가 KIS CCLD 및 MTS와 일치한다.
4. 청산 후 `trades.exit_qty`가 매도 체결 합계와 같고, `pnl_amount`가 수수료 제외 계산과 일치한다.
5. 운영자가 장애 대응 및 수동 청산 절차를 실제로 수행해 봤다.

## 개발 트리 분리 관련 (2026-09-10 도입)

REAL 전환 시 아래를 조정한다. 근거는
`docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md` 6-5절이다.

1. 운영 `F3_ALLOC_RATIO`를 REAL 정책값으로 변경한다. 현재 운영은 계좌 공유
   때문에 `0.70`인데, `src/readiness.py`의 REAL 게이트는 `0 < 비율 <= 0.20`을
   요구한다. **이 둘은 그대로 두면 충돌한다** — REAL 전환 시 반드시 낮춘다.
2. 개발 `F3_ALLOC_RATIO`를 `0.95`로 되돌린다. 계좌가 물리적으로 갈리므로
   예산 분할이 의미를 잃는다.
3. 개발 `AUTH_DIR`을 자기 트리로 되돌리고 `KIS_AUTH_READONLY=0`으로 끈다.
   REAL 토큰과 PAPER 토큰은 발급 호스트가 달라 공유할 수 없다. 플래그를
   지우면 `.stock-role`이 개발 트리를 읽기 전용으로 되돌리므로, **지우지 말고
   명시적으로 `0`을 적는다.**
4. `DEV_EXCLUDE_HELD_TICKERS`를 해제한다 (선택 — 켜둬도 무해하다).
5. 앱키 분리 여부를 결정한다. `.env.paper`와 `.env.real`이 **같은 앱키를 쓰는
   것을 2026-09-10에 확인했다.** 분리하지 않으면 운영과 개발이 KIS 유량 한도를
   계속 공유한다. 분리하면 개발 스케줄 오버라이드(`SCHEDULE_F*`)도 해제할 수
   있다.

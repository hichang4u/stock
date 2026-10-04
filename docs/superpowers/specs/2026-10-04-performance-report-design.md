# 공통 성과 리포트 — CAGR·MDD 설계

작성일: 2026-10-04
상태: 구현 완료 (2026-10-05)
범위: `scripts/` 신규 모듈 2개 + `overnight_sieve.py` 상수 공유 + daily-log 스킬 단계 추가.
`main.py`·`src/`는 건드리지 않는다 — 전략 지문 무관, 운영 DB는 읽기 전용.

## 1. 왜

백테스트와 운영 성과가 스크립트마다 다른 지표·다른 형식으로 남는다. CAGR은 어디서도 계산하지
않고, MDD는 C1(거래 손익 %p 단순 합산)과 H5(슬롯 방식)에서 각자 다르게 계산한다. 운영 트랙 A는
2026-07-02부터 50건을 매매했지만 CAGR·MDD를 한 번도 낸 적이 없다. 결론은 주로 문서의 표로만
남고, 어떤 코드·파라미터·데이터로 낸 값인지 결과 옆에 남지 않는다.

**핵심 지표는 CAGR과 MDD다.** 공통 계산 모듈 하나가 표준 거래 목록을 받아 두 지표와 보조
지표를 같은 정의로 내고, 실행마다 네 파일을 남긴다. 첫 적용은 운영 트랙 A 실거래다.

## 2. 결정 사항 (2026-10-04, 사용자 승인)

| 항목 | 결정 |
|---|---|
| 자본 기준 | **전략 자본**(거래마다 투입금 전액 복리). 계좌 기준은 참고 지표 |
| 비용 | 두 벌을 모두 낸다. **대표값은 보수**(수수료·세금 + 청산 사유별 슬리피지) |
| MDD | 일별 복리 자산곡선의 고점 대비 최대 하락(청산 기준 자산) |
| CAGR | 첫 진입일 ~ 마지막 청산일 달력일 기준 연환산. **365일 미만이면 참고 표시** |
| 트랙 A 표본 | 2026-07-02 ~ 현재. 수동 청산(`MANUAL`)만 제외하고 건수를 남김 |
| 분해 | 전체 + **구간별**(주요 변화 시점) + **연도별** |
| 실행 시점 | 필요할 때 수동 실행. daily-log 스킬에 단계로 넣어 매일 분석 때 함께 돌린다 |
| 구조 | 공용 계산 모듈 + 데이터별 어댑터(아래 3절) |
| 전략 수정 원칙 | 조건은 한 번에 하나(CLAUDE.md). 구간 안에 지문이 둘 이상이면 자동 경고 |

## 3. 구성

| 파일 | 역할 | 의존 |
|---|---|---|
| `scripts/performance.py` (신규) | 순수 계산 + 파일 쓰기. 표준 거래 목록 → 비용 두 벌, 자산곡선, CAGR/MDD, 구간·연도 요약, 네 파일 | 표준 라이브러리만 |
| `scripts/performance_report.py` (신규) | 트랙 A 어댑터 + CLI. 운영 DB `trades` → 표준 거래, 구간 정의 상수, 계좌 기준 참고 지표 | `performance.py`, `sqlite3`(읽기 전용 URI) |
| `scripts/overnight_sieve.py` (수정) | 비용·슬리피지 상수를 `performance.py`에서 가져다 쓴다. 동작 무변경 | `performance.py` |
| `.claude/skills/daily-log/SKILL.md` (수정) | "7. 누적 성과" 단계 추가 | — |

나중에 다른 백테스트가 같은 형식을 쓰려면, 결과를 표준 거래 목록으로 만들어
`performance.write_run(...)`에 넘기기만 하면 된다(이번 범위 밖).

## 4. 표준 거래

어댑터가 만들고 `performance.py`가 받는 한 건.

| 필드 | 형 | 뜻 |
|---|---|---|
| `trade_id` | str | 식별자 |
| `ticker`, `track` | str | 종목, 트랙 |
| `entry_at`, `exit_at` | str | ISO 8601, KST 오프셋 포함 |
| `gross_pct` | float | 비용 차감 전 손익률(%) |
| `exit_reason` | str | `HARD_STOP` / `GAP_HARD_STOP` / `TRAILING` / `TIMEOUT` / 그 밖 |
| `excluded` | str \| None | 제외 사유(`MANUAL`, `NO_PNL`). None이면 계산 대상 |
| `meta` | dict | 지문·실험 ID 등. 계산에 쓰지 않고 그대로 기록 |

## 5. 계산 규칙

### 5.1 비용 (개선 계획 §2 상수)

| 상수 | 값 |
|---|---|
| `BASE_ROUND_TRIP_COST_PCT` | 0.18 |
| `HARD_STOP_SLIPPAGE_PCT` | 0.30 (`HARD_STOP`, `GAP_HARD_STOP`) |
| `TRAILING_SLIPPAGE_PCT` | 0.15 (`TRAILING`) |
| `TIMEOUT_SLIPPAGE_PCT` | 0.20 (`TIMEOUT`, `DATA_END`, 그 밖 모든 사유) |

- 비용 차감 손익 = `gross − 0.18`
- 보수 손익 = `gross − 0.18 − 사유별 슬리피지` ← **대표값**
- 알 수 없는 사유는 0.20을 적용하고 `unknown_reasons`에 사유별 건수를 센다.
- 실거래는 진입 슬리피지를 따로 빼지 않는다(체결가에 반영돼 있다).

### 5.2 자산곡선

- 시작 자산 1.0. 계산 대상 거래를 `exit_at` 순서로 정렬하고, 청산마다 `자산 × (1 + 손익/100)`.
- **한 번에 한 포지션.** 어떤 거래의 `entry_at`이 직전 거래의 `exit_at`보다 이르면 계산하지 않고
  `OverlapError`(겹친 두 `trade_id` 포함)를 낸다. 동시 보유 전략(H5)은 이 모듈을 쓰지 않는다.
- 보수 기준과 비용 차감 기준을 각각 따로 굴린다.

### 5.3 MDD

`min(자산_t / max(자산_0..t) − 1)`. 매 청산 직후 자산으로 잰다. 시작 자산 1.0도 고점 후보에
넣는다. 하루 넘게 보유하는 전략은 보유 중 평가손이 빠지므로 MDD가 작게 나올 수 있다 — 그런
어댑터가 쓰면 `warnings`에 남긴다(`holding_overnight=True` 인자).

### 5.4 CAGR

- 일수 = (마지막 `exit_at`의 날짜 − 첫 `entry_at`의 날짜).days + 1
- `CAGR = 최종자산 ^ (365.25 / 일수) − 1`
- **일수 < 365면 `cagr_reference_only: true`**, `warnings`에 "1년 미만 CAGR은 참고". 총수익률
  (`최종자산 − 1`)을 항상 함께 낸다.
- 거래가 0건이면 CAGR·MDD는 None.

### 5.5 보조 지표

거래 수, 승률(보수 손익 > 0 비율), 평균·중앙값 손익, 총수익률. 보수·비용 차감 두 벌.

### 5.6 구간·연도

- 각 구간과 각 연도는 **자산을 1.0에서 다시 시작해** 5.2 ~ 5.5를 독립적으로 계산한다.
- 소속은 청산일(`exit_at`의 날짜) 기준이다. 트랙 A는 당일 청산이라 거래일과 같다.
- 구간 정의: `{"name", "start", "end"(없으면 열림), "changed": [바뀐 조건 설명]}`.
- **복수 변경 경고 (자동).** 구간 안 계산 대상 거래의 `meta.strategy_fingerprint`가 서로 다른
  값을 둘 이상 가지면 그 구간 요약에 `multiple_changes: true`와 지문 목록을 남긴다(지문이 없는
  거래는 세지 않는다 — 초기 운영 구간). 지문은 코드와
  `.env` 전략 설정을 모두 반영하므로 손으로 쓴 `changed`보다 우선하는 객관적 신호다. 경계 하나에
  `changed`가 둘 이상 적힌 경우에도 같은 경고를 붙인다.

## 6. 네 파일

위치: 개발 트리 `data/performance/<이름>/<실행ID>/` (`data/`는 git 제외). 실행 ID는 실행 시각
`YYYYMMDD_HHMMSS`(KST). 실행마다 새 폴더를 만든다. 모든 파일은 `write_bytes`(UTF-8, LF).

1. **`trades.jsonl`** — 거래 1건 1줄. 표준 필드 + `net_cost_pct`, `net_conservative_pct`,
   `equity_after`(보수), `segment`, `year`. 제외 거래도 `excluded`와 함께 남기되 자산은 비운다.
2. **`daily.jsonl`** — 청산이 있었던 날 1줄. `date`, `trades`, `day_return_pct`(보수),
   `equity`(보수), `equity_cost`, `peak`, `drawdown_pct`, `segment`.
3. **`summary.json`**
   ```json
   {
     "basis": "strategy_capital", "headline": "conservative",
     "overall": {"conservative": {...}, "cost_only": {...}, "period": {...}},
     "segments": [{"name": "...", "changed": [...], "multiple_changes": false,
                   "fingerprints": [...], "conservative": {...}, "cost_only": {...}}],
     "years": [{"year": 2026, "conservative": {...}, "cost_only": {...}}],
     "account_reference": {...},
     "excluded": {"MANUAL": 1}, "unknown_reasons": {}, "warnings": [...]
   }
   ```
   `{...}` 지표 묶음 = `n`, `cagr`, `cagr_reference_only`, `total_return`, `mdd`, `win_rate`,
   `mean_pct`, `median_pct`. `period` = `start`, `end`, `days`, `trading_days`.
4. **`manifest.json`** — `adapter`, `run_id`, `created_at`, `git_commit`, `git_dirty`, 비용 상수,
   구간 정의 전체, 입력 출처(`db_path`, `rows_read`, `rows_used`, 기간), 계좌 규모 상수.

## 7. 트랙 A 어댑터

- `file:<root>/data/db/trading.db?mode=ro`로 연다. `track='A' AND status='CLOSED'`만 읽는다.
- `gross_pct` = `pnl_pct`. 비어 있으면 `(exit_price / entry_price − 1) × 100`. 둘 다 없으면
  `excluded="NO_PNL"`.
- `close_reason='MANUAL'`은 `excluded="MANUAL"`.
- `meta` = `strategy_fingerprint`, `experiment_id`, `execution_mode`, `name`.
- **구간 정의 (상수, git 이력과 manifest로 추적)**

  | 구간 | start | end | changed |
  |---|---|---|---|
  | 초기 운영 | 20260702 | 20260806 | (PAPER 표시 전, 지문 기록 없음) |
  | PAPER 레거시 | 20260807 | 20260910 | 실행 모드·지문 기록 시작, 진입 안전장치 강화 묶음 |
  | 빠른 경로 | 20260911 | 20261001 | 빠른 경로 하이브리드 활성(`PAPER_FAST_HYBRID=1`). 0914 ~ 1001은 WS 파서 결함 기간 |
  | 파서 수정 후 | 20261002 | — | WS 파서 헤더 분할 수정, 상한가 잠김 예외 |

  `changed`는 설명이다. 구간 안 지문 개수 검사(5.6)가 실제 복수 변경을 잡는다. "파서 수정 후"
  경계는 `changed`가 둘이라 처음부터 복수 변경 경고가 붙는다.
- **계좌 기준 참고 지표** (`account_reference`): `pnl_amount` 누적(원), 원 단위 MDD,
  계좌 규모 상수 `ACCOUNT_CAPITAL_KRW = 500_000_000` 대비 비율. 요약에 "모의 계좌 분기 교체와
  초기 소량 거래 때문에 참고용"을 경고로 남긴다.
- CLI: `python scripts/performance_report.py --track A --root D:/Private/stock-prod
  [--out-dir data/performance] [--until YYYYMMDD]`. 화면에 전체·구간·연도 표. 종료 코드 0,
  계산 대상 0건이면 2.

## 8. daily-log 연결

스킬 끝(보고 전)에 "7. 누적 성과" 단계를 넣는다. 위 명령을 돌려 보고 맨 끝에 전체 기간과 현재
구간의 CAGR(참고 표시 포함)·MDD를 한 줄씩 붙인다. 복수 변경 경고가 있으면 함께 적는다.

## 9. 오류 처리

| 상황 | 동작 |
|---|---|
| 포지션 겹침 | `OverlapError`로 중단, 겹친 `trade_id` 표시 |
| `pnl_pct`·가격 모두 없음 | `NO_PNL`로 제외, 건수 기록 |
| 모르는 청산 사유 | 슬리피지 0.20, `unknown_reasons`에 기록 |
| 계산 대상 0건 | 파일은 남기고 지표는 None, CLI 종료 코드 2 |
| DB 없음 | 즉시 실패(명확한 메시지) |

## 10. 테스트 (TDD, 합성 데이터)

- 비용 두 벌, 알 수 없는 사유
- 복리 자산곡선, MDD(시작 자산을 고점으로 포함), 손실만 있는 경우
- CAGR 연환산과 365일 미만 참고 표시, 0건
- 구간·연도별 자산 재시작, 구간 안 지문 2개 → 복수 변경 경고, `changed` 2개 → 경고
- 겹침 → `OverlapError`, `MANUAL`·`NO_PNL` 제외
- 어댑터: 임시 SQLite로 트랙 A 거래 → 표준 거래, 구간 지정, 계좌 기준 계산, 읽기 전용 열기
- 출력: 네 파일 생성, LF(`\r\n` 없음), manifest 필드, 실행 ID 폴더
- `overnight_sieve.py` 기존 테스트가 상수 공유 후에도 그대로 통과

## 11. 범위 밖 (YAGNI)

다른 백테스트 스크립트 이전, 스케줄러 자동화, 운영 DB 테이블(개선 계획 §4), 시각화, 벤치마크
대비 초과수익. 필요해지면 따로 설계한다.

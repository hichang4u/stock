# PAPER Fast Path 관측 프로브

## 목적

PAPER의 1.1초 REST 호출 제한에서 F1의 약 60회 단건 예상체결 조회를
장전 순위 2회와 멀티시세 2회로 대체할 수 있는지 실서버 응답으로 검증한다.

현재 구현은 두 모드를 지원한다.

- Shadow: 개장 멀티시세 후보와 레거시 F1 결과를 비교만 한다.
- Hybrid: 개장 멀티시세 후보를 F2/F3에 전달한다. 주문 수량·주문 전 최종
  가격/잔고/VI 검증과 주문 전송 경로는 기존 F3를 그대로 사용한다.

두 모드 모두 `KIS_MODE=PAPER`, `DRY_RUN!=1`, `PAPER_FAST_PROBE=1`을
만족해야 한다. `REAL`에서는 관련 환경변수가 켜져 있어도 코드에서 차단한다.

## 실행 시각과 호출

- 08:59:45: KOSPI/KOSDAQ 등락률 순위 API를 각각 호출한다.
- 각 순위 응답의 최대 30종목을 시장별 멀티시세 API로 조회한다.
- 장전 응답에서 `PAPER_FAST_SHADOW_TOP_N`개(기본 30개)의 shortlist를
  선정해 기록한다.
- 09:00:00.300: 기존 F1이 시작되기 직전에 shortlist의 멀티시세를 한 번
  더 조회한다.
- 08:59:50: PAPER 잔고 스냅샷은 Fast/Hybrid 활성 여부와 무관한 독립 스케줄
  잡에서 준비하되 08:59:58에 강제
  종료한다. 느린 계좌 API가 09:00:00.300 멀티시세의 REST 슬롯을
  선점하지 않도록 하기 위한 개장 가드다.
- 09:00:02.800보다 늦게 시작하면 개장 관측은 생략하고 기존 F1을 즉시
  계속한다.

호출 엔드포인트:

- 등락률 순위: `FHPST01710000`
- 관심종목 멀티시세: `FHKST11300006`

## 설정

```dotenv
PAPER_FAST_PROBE=1
PAPER_FAST_SHADOW=1
PAPER_FAST_HYBRID=0
PAPER_FAST_SHADOW_TOP_N=30
PAPER_FAST_SHADOW_REQUIRED_DAYS=10
PAPER_FAST_SHADOW_SCAN_FILE_LIMIT=32
PAPER_FAST_PROBE_DIR=data/paper_fast_probe
PAPER_FAST_PROBE_OPEN_OFFSET_MS=300
PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS=2500
PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC=2.5
```

`PAPER_FAST_HYBRID=0`이면 Shadow 비교만 수행하고 레거시 F1 결과를
사용한다. `1`이면 PAPER에서만 fast 후보를 F2/F3에 전달한다. REAL 또는
DRY_RUN에서는 값이 `1`이어도 코드 레벨에서 실행되지 않는다.

유효한 개장 관측 뒤 3분 이내 레거시 비교가 완료된 날짜만 검증일로 계산한다.
`PAPER_FAST_SHADOW_PROGRESS`에는 누적 검증일, 남은 날짜, rank-1 일치일과
top-3 중복 합계, 시간 초과·불완전 날짜가 기록된다. 최신 32개 파일만 읽고
필요한 두 이벤트가 없는 줄은 JSON 파싱 전에 제외한다. 집계는 F3 진입 처리 후
별도 스레드에서 실행되며, 실패해도 주문 파이프라인에 전파되지 않는다.
10일을 채워도 Hybrid는 자동 활성화되지 않는다.

기존 프로브 파일도 `quality.ok=true`인 `OPEN_DONE`과 180초 이내 `COMPARE`가
함께 있을 때만 집계된다. 이전 파일에 이 조합이 없으면 활성화 직후 진행률은
0일부터 시작하는 것이 정상이다.

Hybrid는 후보 개수가 아니라 개장 응답의 완전성으로 Fast Path 채택 여부를 판단한다.
요청/응답 종목 일치, 전 종목 유효 매도호가, 정상 응답 코드를 모두 만족하면 후보가
1~2개여도 Fast 결과를 사용한다. 응답이 불완전하면 레거시 F1으로 fallback하고,
이미 확인된 Fast 후보와 레거시 결과를 병합한다. 레거시 결과가 비어도 유효 Fast
후보를 `NO_TARGET`으로 덮어쓰지 않는다.

`PAPER_FAST_PROBE_OPEN_DONE`에는 갭·예상체결대금·거래량 급증·VI·고갭 필터별
탈락 건수와 응답 품질 사유가 기록된다. `TOP_N=30`은 최소 5~10거래일 동안 PAPER로
관측한 뒤 후보 재현율과 지연을 평가하고, 59종목 2배치 확대는 그 결과 이후 판단한다.

## 산출물

원시 응답과 타이밍은 다음 파일에 JSON Lines 형식으로 저장한다.

```text
data/paper_fast_probe/YYYYMMDD.jsonl
```

파일에는 공개 시세 응답의 `rt_cd`, `msg_cd`, `msg1`, `output`과 요청
파라미터만 저장한다. 인증 헤더, 앱 키, 토큰, 계좌번호는 저장하지 않는다.

주요 이벤트:

- `PAPER_FAST_PROBE_RANKING`: 시장별 장전 순위 원시 응답
- `PAPER_FAST_PROBE_MULTI`: 시장별 장전 멀티시세 원시 응답
- `PAPER_FAST_PROBE_PREOPEN_DONE`: 요청/응답 종목 대응 및 장전 shortlist
- `PAPER_FAST_PROBE_PREOPEN_SKIPPED`: 09:00 이후 장전 관측 미스파이어 생략
- `PAPER_FAST_PROBE_OPEN_MULTI`: 개장 직후 멀티시세 원시 응답
- `PAPER_FAST_PROBE_OPEN_DONE`: 실제 시작 지연과 유효 매도1호가 개수
- `PAPER_FAST_PROBE_OPEN_SKIPPED`: 지각 또는 장전 대상 부재로 생략
- `PAPER_FAST_SHADOW_PROGRESS`: 유효 비교 거래일 누적 현황

각 API 이벤트의 `timing`에는 전체 소요 시간, 예상 레이트리미터 대기,
이를 뺀 추정 네트워크 처리 시간이 기록된다.

## 다음 거래일 판정 항목

1. 08:59대 등락률 순위가 비어 있지 않고 `fid_rsfl_rate1/2` 필터가
   예상체결 등락률에 맞게 작동하는가.
2. 시장별 멀티시세의 요청 종목과 반환 종목이 일치하는가.
3. 장전 `intr_antc_vol`, `intr_antc_cntg_prdy_ctrt`, `inter2_prpr`,
   `inter2_prdy_clpr`가 실제 의미 있는 값을 갖는가.
4. `mrkt_trtm_cls_name`, `hour_cls_code`가 장전/개장 상태를 구분하며,
   VI 또는 거래정지 판단에 사용할 수 있는가.
5. 09:00:00.300 조회에서 `inter2_askp`가 유효한가. 비어 있다면
   1회 재조회 필요성과 적정 지연 시간을 결정한다.
6. 장전 shortlist 상위 3종목과 기존 F1 결과의 순위·누락 종목 차이가
   허용 가능한가.
7. 추가 5회 호출이 PAPER 제한 오류 없이 끝나고 기존 F1/F2/F3가
   그대로 실행되는가.

Shadow 결과는 `PAPER_FAST_SHADOW_COMPARE`로 계속 기록한다. Hybrid는
이 관측을 바탕으로 구현됐으며, REAL 차단과 기존 F3 안전 가드는 별도
회귀 테스트로 유지한다.

## 2026-07-27 장중 수동 확인

PAPER 서버에서 멀티시세 엔드포인트 접수와 일괄 반환은 확인했다.
KOSPI 30종목 요청은 30종목, KOSDAQ 28종목 요청은 28종목이 반환됐다.
응답에는 `inter2_askp`, `inter2_bidp`, `inter2_prdy_clpr`, `inter2_prpr`,
`intr_antc_vol`, `intr_antc_cntg_prdy_ctrt`, `mrkt_trtm_cls_name`,
`hour_cls_code`가 포함됐다.

다만 이 확인은 개장 후 수행되어 예상체결 관련 필드가 0이었다. 따라서
장전 순위의 의미, 장전 예상체결 필드 값, 상태 필드의 VI 의미는 아직
검증되지 않았으며 위 관측 프로브가 이를 확인한다.

## 전환 판단 도구: 반사실 평가

`PAPER_FAST_SHADOW_PROGRESS`가 세는 `rank1_match_days`와 `top3_overlap_total`은
후보 일치도일 뿐이라 전환 여부를 답하지 못한다. 불일치가 유리했는지 불리했는지를
가리지 못하기 때문이다. `scripts/fast_path_counterfactual.py`가 그 공백을 메운다.

```bash
# 로컬 요약만 (외부 호출 없음)
python scripts/fast_path_counterfactual.py

# 분봉으로 실제 평가 (PAPER, 09:35 이후, 장 마감 후 권장)
python scripts/fast_path_counterfactual.py --with-kis --out data/fast_path_counterfactual.json
```

관측일마다 두 반사실을 나란히 놓는다.

- Fast: 개장 멀티시세 매도1호가(`inter2_askp`)로 09:00에 진입. 하이브리드가 실제로
  주문했을 가격이 프로브 파일에 그대로 남아 있다.
- 레거시: 09:01 분봉 종가로 진입. F1 선정에 68~89초가 걸리므로 09:00 호가로 값을
  매기면 비교 대상인 지연 페널티가 사라진다. 20260814 레거시 진입가 106,700은
  DB의 실제 체결가와 일치해 이 모델을 뒷받침한다.

각 편은 09:00~09:30에서 승인된 장벽(+2.5% / -2.0%) 선착과 MFE/MAE를 낸다. 분봉은
봉 내부 경로를 모르므로 트레일링은 재현하지 않는다. 같은 봉이 양쪽 장벽에 닿으면
`AMBIGUOUS`, 측정 창에 봉이 없으면 미판정으로 남긴다. 빈 창을 "장벽 미접촉"으로
보고하면 데이터 없음이 유리한 결과로 둔갑한다.

분봉은 일별분봉 TR(`FHKST03010230`)로 읽는다. 당일 TR(`FHKST03010200`)은 빈 커서에서
장 마감 직전 30봉을 주므로 개장 30분 창에 쓸 수 없다. 휴장일을 요청하면 KIS가 가장
가까운 거래일로 조용히 대체하므로 요청 날짜와 다른 봉은 전부 버린다.

### 전환 기준 (제안)

10거래일을 채운 뒤 아래를 모두 만족하면 `PAPER_FAST_HYBRID=1`로 전환한다.
자동 전환은 없다.

1. `undecidable`을 뺀 판정일이 6일 이상이다. 표본이 얇으면 판단하지 않는다.
2. `legacy_better`가 `fast_better`를 넘지 않는다.
3. 전 관측일의 개장 응답 품질이 `COMPLETE`다 (`PAPER_FAST_PROBE_OPEN_DONE.quality.ok`).
4. 개장 관측 지각(`lateness_ms`)이 `PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS` 안에 있다.

롤백은 `PAPER_FAST_HYBRID=0` 한 줄이다. `readiness.py`의 `paper_experiments_off`
게이트가 REAL 전환 시 두 플래그를 모두 강제로 확인하므로 실전 경로에는 영향이 없다.

### 전환 판정 (2026-09-10)

네 기준을 모두 만족해 `PAPER_FAST_HYBRID=1`로 전환했다. `.env`와 `.env.paper`만
바꾸고 `.env.real`은 `0`으로 두었다. `.env.example`도 `0`을 유지한다 — 신규 설치는
관측일이 0이므로 검증을 건너뛰게 하면 안 된다.

| 기준 | 실측 | 판정 |
|---|---|---|
| 판정일(`undecidable` 제외) 6일 이상 | 15일 (16 − 1) | 통과 |
| `legacy_better` ≤ `fast_better` | 3 ≤ 5 | 통과 |
| 전 관측일 개장 품질 `COMPLETE` | 16/16 | 통과 |
| `lateness_ms` ≤ 2500 | 최대 25, 중위 10 | 통과 |

반사실 평가 원본은 `data/fast_path_counterfactual_20260910.json`이다. 결정적 판정
8일은 fast 5 / legacy 3이다. **이 표본으로 Fast가 더 낫다고 말할 수는 없다.**
기준 2는 비열등성 조건이고, 전환의 근거는 우열이 아니라 지연 제거다.

전환을 부른 관측은 2026-09-10 장이다. 레거시 F1 선정이 개장 후 78초(`selection_ms`
77,841)가 걸렸고, 그 사이 1순위 187660의 갭이 락업 시점 7.82%에서 재검증 시점
14.56%로 벌어져 `ABOVE_MAX`에 걸렸다. 나머지 두 종목도 고갭 대금 요건에 막혀
후보 3개가 전부 차단됐다(`F3_ENTRY_BLOCKED` `terminal=true`). 지연이 길수록
"장전 예상체결가로 고른 갭"과 "장중 현재가로 재검증한 갭"이 벌어지므로, 레거시
경로에서는 이 차단이 구조적으로 반복된다.

되돌리려면 `.env`와 `.env.paper`의 `PAPER_FAST_HYBRID`를 `0`으로 되돌리고
프로세스를 재시작한다. 환경변수는 기동 시 읽으므로 실행 중 전환은 반영되지 않는다.

### 관측일 계수 주의

`shadow_validation_summary`는 휴장으로 판정된 날을 `skipped_closed_days`로 빼고 센다.
감지가 붙기 전에 기록된 20260817(광복절 대체공휴일)도 파일에 남은 개장 멀티시세로
다시 판정해 제외하므로, 과거 기록이 계수를 부풀리지 않는다.

그래도 반사실 평가의 `evaluated_days`와 `undecidable`은 함께 봐야 한다. 개장일이어도
분봉이 없거나 `AMBIGUOUS`면 판정 표본은 그보다 적다.

## 모의투자 휴장일 감지

`CTCA0903R`(국내휴장일조회)은 모의투자 미지원이라 `_check_market_holiday`가 PAPER에서
즉시 반환한다. 주말은 스케줄러의 `day_of_week="mon-fri"`가 막지만 **평일 공휴일에는
가드가 없었다.** 2026-08-17(월, 광복절 대체공휴일)에 F1이 60종목을 69초간 조회하고
주문까지 전송했다(`40100000 장운영 시간이 아닙니다`로 거부).

장전 멀티시세 응답에 판별 신호가 이미 있어 추가 호출 없이 막는다. 휴장일에는 동시호가가
돌지 않아 예상체결 필드가 비고, 현재가·거래량 자리에 직전 거래일 값이 그대로 내려온다.

| 신호 | 개장 14일 | 휴장 (20260817) |
|---|---|---|
| `hour_cls_code` | `B` | `0` (전 종목) |
| `intr_antc_vol == 0` | 0~1종목 | 전 종목 |
| `acml_vol` 중앙값 | 246 ~ 1,995 | 3,859,277 |

`evaluate_market_closed`는 **세 조건을 모두** 만족할 때만 휴장으로 본다. 휴장 표본이
1일뿐이므로 기존 휴장 판정과 같은 fail-open 원칙을 지킨다 — 거래일을 놓치는 쪽이 더 큰
손실이다.

`any`로 판정하면 안 된다. 20260813 개장일에도 `hour_cls_code='0'`인 종목이 1개
있었다(거래정지로 추정). 이를 휴장으로 오인하면 정상 거래일을 통째로 잃는다.

판정은 08:59:45 장전 프로브에서 이뤄지므로 F1의 개장 후 조회가 시작되기 전에 막힌다.
휴장이면 `_mark_market_closed`로 REAL의 휴장 경로와 똑같이 `day_skip`을 세우고
`MARKET_CLOSED`를 남긴다(`source=FAST_PROBE`). 프로브가 예외로 실패하면 판정하지
않는다 — 실패와 휴장은 다르다.

기록된 17일(개장 16 / 휴장 1) 전체를 되돌려 오판 0건을 확인했다.

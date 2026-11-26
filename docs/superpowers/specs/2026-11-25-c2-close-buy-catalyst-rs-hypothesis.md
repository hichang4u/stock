# C2 — 당일 공시 + 14:50~15:20 시장 대비 강세 종가 매수 가설 (등록 초안)

> 작성: 2026-11-25 (격리 브랜치 `hichang4u/close-buy-rules-20261125`). 같은 날 리뷰를 반영해 초안을 보완했다(§11).
> 상태: **등록 초안이다.** 규칙과 임계값은 고정했고, 사용자 승인은 아직이다. 수집기는 없고 표본은 0이다.
> 운영 변경은 없다. 주문 코드, 설정, 예약 작업, 브로커 API, 인증은 건드리지 않았다.
> 선행 문서:
> [C1 (닫힘)](2026-09-21-c1-overnight-close-hypothesis.md) §7·§8 ·
> [H1 §6 닫힌 축](2026-09-11-h1-gap-selection-hypothesis.md) ·
> [H2 공시 라벨](2026-09-17-h2-catalyst-disclosure-hypothesis.md) ·
> [11/23 종가매수 후보 검토](../../CLOSE_BUY_CANDIDATE_REVIEW_20261123.md)
> (최신 검증본은 `main`의 `0edf389`)

이 문서는 C2의 손익을 한 건도 보지 않은 상태에서 규칙, 상수, 판정 기준을 고정한다. 등록 전에 본 데이터는 세 가지다.
- C1의 기록(§7)
- 저장 자료 점검(§8)
- 11/23 분봉 6종목의 가격 구간(§8.3)

승인 전 수정은 표본이 0일 때만 한다. 무엇을 왜 고쳤는지는 §11에 남긴다. 첫 표본이 기록되면 아무것도 바꾸지 않는다. 바꾸고 싶으면 C3로 새로 등록한다.

## 0. 이 문서가 하는 것과 하지 않는 것

**하는 것**
- 처음 시험할 규칙 하나를 재현할 수 있는 형태로 고정한다(§3, 부록 A의 순수 함수).
- 최소 관찰 증거를 정한다(§5).
- 저장 자료만으로 입력을 시점 일치로 복구할 수 있는지 실제로 점검한다(§8).
- 로컬 합성 사례로 절차와 회귀를 점검한다(§9).

**하지 않는 것**
- C1 재활성화. `StockBot_OvernightScreen`과 `StockBot_OvernightScreen1520`의 비활성 상태는 그대로 둔다.
- 수집기 구현.
- 주문 경로 연결.
- 운영 트리 쓰기(읽기만 했다).

## 1. C1과 무엇이 다른가 (닫힌 축 확인)

| | C1 (2026-11-19 닫힘) | C2 (이 문서) |
|---|---|---|
| 모집단 | 시장별 거래량 상위 30 | 그날 **KIND에 시각과 함께 공개된 화이트리스트 공시**가 있는 종목 |
| 선정 근거 | 등락률 3~25%, 종가 위치 ≥ 0.8, 거래대금 20일 평균 2배 | 재료가 있고, **14:50~15:20에 소속 시장 지수보다 강했다** |
| 순위 | 거래대금 배수 | 시장 대비 초과 상승폭(RS, 정확한 유리수 비교) |
| 시각 | 15:32 (종가 확정 후. 실주문 불가) | **입력 기준 15:20:00, 결정 기록 마감 15:25:00** (종가 단일가 안) |
| 청산 | D+1 트랙 A F4 규칙 | **D+1 시가 단일가**. 체결 확인이 안 되면 보수적 지연 청산 |
| 하루 건수 | 랭크 1 | **최대 1종목, 무거래 허용** |

- **C1 §8:** C1 문서는 "다른 규칙을 C2로 새로 등록하고 등록 뒤 표본으로만 판정"하라고 했다. 이 문서가 그 C2다.
  C1의 15:20 병행 기록(`data/overnight/candidates_1520/`)은 닫힌 축의 자료이고, 14:50 값도 없다. 그래서 C2의 판정에도 소급에도 쓰지 않는다.
- **H1 §6:** 닫힌 축 "진입 시각 09:01~13:00 종가 진입"은 갭상승 종목에 장중에 들어가는 축이다. C2는 장 마감에 들어가므로 이 축에 닿지 않는다.
  H5(신고가 돌파), 3양봉, VWAP 축도 쓰지 않는다.
- **H2:** H2는 열린 가설로 그대로 둔다. H2는 직전 거래일 공시로 트랙 A의 갭 종목을 거른다. C2는 당일 공시를 쓰고 트랙도 다르다.
  H2의 화이트리스트, 시간 창, 판정은 바꾸지 않는다. H2에서 문제가 된 5% 대량보유 보고는 C2 목록에서 처음부터 뺐다.
- **이미 본 것(공개):** 청산 방식을 고르기 전에 C1 랭크 1의 시가 갭을 봤다(11-19 기록, 평균 −0.54%). 이 값은 음수라 시가 청산을 고른 근거가 아니다.
  시가 청산을 고른 이유는 세 가지다.
  - 외부 추천 검토서와 같은 자(종가 → 익일 시가)로 잴 수 있다.
  - 입력이 공식 일봉 값이라 재현성이 가장 높다.
  - 장중 재생의 편향이 없다(C1 11-02의 틱 대 분봉 비교).

## 2. 가설

> 매 정규 거래일 D-0에 15:20:00까지 공개되거나 체결된 입력으로 §3 규칙을 적용해 **최대 1종목**을 고른다. 결정은 15:25:00까지 기록한다.
> 그 종목을 **D-0 종가 단일가**에 사서 **D+1 시가 단일가**에 팔면, 비용 가정(왕복 0.25%p)을 뺀 거래당 기대수익이 0보다 크다.

**메커니즘(한 문장):** 당일 공개된 새 재료에 시장이 장 막판까지 지수보다 강하게 반응한 종목은, 그 반응이 마감까지 다 반영되지 않아 다음 날 시가에서 이어진다.

재료는 "무엇이 새로 알려졌나"를 본다. RS는 "시장이 그것을 호재로 읽었나"를 본다. 재료가 호재인지 악재인지는 사람이 판단하지 않는다.

## 3. 규칙 (고정)

### 3.1 시각과 예외장

시각 네 개를 구분한다.

| 시각 | 뜻 |
|---|---|
| **15:20:00 — 입력 기준 시각** | 공시 공개 시각이 이 시각 이하이고, 가격은 15:19 봉까지(15:20:00 직전 체결)인 입력만 쓴다 |
| **15:20:00 ~ 15:25:00 — 스냅샷 창** | 입력 묶음마다 `source`와 `available_at`을 남긴다. `available_at`은 수집기가 소스에서 값을 받은 실제 시각이다.<br>공시, 종목 분봉, 지수 분봉, 시장조치 묶음은 이 창 안이어야 한다.<br>`daily` 묶음(전일 종가, 상한가, 상장 경과일)은 D-0 00:00~15:25:00이면 된다 |
| **15:25:00 — 결정 기록 마감** | 결정 행의 `decision_recorded_at`이 모든 `available_at` 이후이고, 15:25:00 이하여야 한다.<br>남는 5분은 종가 단일가(15:20~15:30)에 주문을 낼 여유다 |
| 15:30 — 종가 단일가 종료 | 진입 체결 시점이다(§3.6) |

**주 표본은 스냅샷 창과 결정 기록 마감을 모두 지킨 날뿐이다(§5.1).**
- 15:25 이후에 받은 자료(장 마감 후 백필, 정정, 재조회, 사후 재구성)로는 그날의 결정이나 순위를 만들거나 바꾸지 않는다.
- 그런 자료는 **입력 검증용**(`INPUT_CHECK_ONLY`, 스냅샷 값과 사후 값 대조)으로만 쓴다.
- 대조에서 어긋난 건수는 따로 센다.

**결정 전 판단 — 날짜와 세션**

| 경우 | 상태 |
|---|---|
| 날짜가 `YYYYMMDD` 실제 날짜가 아니거나, 세션 값이 `REGULAR`·`LATE_OPEN`·`SHORT`·`HOLIDAY`가 아님 | `INVALID_INPUT`. 무거래로 세지 않는다 |
| D-0이 정규장(09:00~15:30)이 아님 | `SKIP_SPECIAL_SESSION`. 위 시각들이 종가 단일가와 맞지 않는 날이다 |

정규장이 아닌 날의 예:
- `src/market_calendar.py`의 `LATE_OPEN_DAYS`에 있는 10:00 개장, 16:30 마감일
- KRX가 공지한 단축·지연 운영일

목록은 해마다 KRX 공지로 더한다.

**결정 전 판단 — 시장 전체 서킷브레이커(14:50~15:20)**

| 경우 | 상태 |
|---|---|
| 발동 | `SKIP_MARKET_HALT` |
| 발동 여부 미확인 | `INPUT_MISSING` |

사이드카는 기록만 한다.

**D+1이 10:00 개장일이면** 10:00 시가 단일가에서 §3.6과 같은 체결 확인 기준을 쓴다(`D1_LATE_OPEN` 기록).

**거래일 판정**은 KRX 거래일 달력으로 한다. `data/overnight/calendar.json`은 11-19에서 끊겼으므로 C2는 이 파일에 기대지 않는다.

### 3.2 재료: 당일 공개된, 검증 가능한 신규 공시와 회사 발표

**소스는 KIND 공시 목록 하나다.** 공시번호와 분 단위 공개 시각을 준다. DART는 원문 확인 링크로만 쓴다(목록 API에 시각이 없다. H2 §2.1).

**회사 발표**는 거래소 공시(공정공시 포함)로 제출된 것만 인정한다. 보도자료, 홈페이지, 기사, IR 자료는 첫 시험에서 뺀다. 공개 시각을 바꿀 수 없는 기록으로 남길 수 없어서다.

**공시 목록 스냅샷의 완전성.** 스냅샷이 D-0 00:00부터 스냅샷 시각까지 전체를 덮는다는 근거가 있을 때만 `complete=true`다. 근거는 마지막 페이지까지 받았고, 소스가 밝힌 총건수와 행 수가 같은 것이다.
- `complete=true`이고 재료가 없을 때만 **진짜 무재료**(`NO_TRADE_NO_CATALYST`)다.
- 조회 실패, 페이지 누락, 총건수 불일치는 `INPUT_MISSING`이다. 무재료로 세지 않는다.

공시 하나가 재료로 인정되는 조건:

| 조건 | 정의 | 탈락 사유 |
|---|---|---|
| 검증 가능 | 소스가 KIND이고 공시번호가 있다 | `SOURCE_UNVERIFIABLE` |
| 시각 형식 | ISO 8601이고 오프셋이 **`+09:00`**이다. 오프셋이 없거나 다르거나 형식이 틀리면 탈락 | `BAD_TIMESTAMP` |
| 당일 | 공개 일자가 D-0이다. D-1 장 마감 후 공시는 해당하지 않는다 | `NOT_SAME_DAY` |
| 기준 시각 전 | 공개 시각 ≤ D-0 15:20:00 | `AFTER_CUTOFF` |
| 신규 | `[기재정정]`, `[첨부정정]`, `[첨부추가]`, `[변경등록]`, `[정정]`으로 시작하지 않는다 | `AMENDMENT` |
| 유형 | 보고서명(공백 제거)에 아래 키워드가 들어 있다 | `NOT_WHITELISTED` |

**시각 처리:** KIND 시각은 분 단위다. 수집기는 `HH:MM`을 **`HH:MM:59+09:00`**으로 저장한다(보수적 상한).
- 15:19 공시까지 인정된다.
- 15:20 공시는 15:20:59가 되어 탈락한다.

```
단일판매ㆍ공급계약체결   영업(잠정)실적        매출액또는손익구조
자기주식취득결정          자기주식취득신탁계약체결결정   주식소각결정
무상증자결정              유형자산취득결정       타법인주식및출자증권취득결정
신규시설투자              특허권취득            기술도입ㆍ이전ㆍ제휴계약체결   기술이전계약
투자판단관련주요경영사항
```

**목록에서 뺀 유형**
- 대량보유·임원 소유 보고
- 희석 공시: 유상증자, CB, BW, EB
- 기준가가 바뀌는 행위: 합병, 분할, 감자
- 정기·절차 공시: 조회공시 답변, 정기보고서, 주주총회, IR 개최, 배당

### 3.3 가격, 거래대금, 공식 값 정의

**장중 값(결정 입력)**
- 1분봉을 쓴다. KIS 일별분봉 `FHKST03010230` 또는 같은 정의의 소스이고, **KRX 단독** 정규장이어야 한다.
- 봉 라벨은 **분의 시작**이다. `144900`은 14:49:00~14:49:59의 체결을 담는다(§8.2 실측).

| 값 | 정의 |
|---|---|
| `p1450` | 라벨 ≤ `144900`인 마지막 봉의 종가 |
| `p1520` | 라벨 ≤ `151900`인 마지막 봉의 종가 |
| `RS` | `(p1520/p1450 − 1) − (지수 p1520/지수 p1450 − 1)`. 지수는 소속 시장(KOSPI 또는 KOSDAQ)의 같은 정의 값이다 |
| `window_value` | 라벨 `145000`~`151900` 봉의 Σ(거래량 × 봉 종가) |
| `day_value` | 라벨 `090000`~`151900` 봉의 Σ(거래량 × 봉 종가) |
| `prev_close`, `upper_limit` | D-1 KRX 공식 종가, D-0 상한가 (`daily` 묶음) |

체결 없는 분은 봉이 없으므로 직전 봉의 종가로 잇는다.

**정산 값(결정 뒤 값. KRX 공식 값만, 출처 표시 필수)**

| 값 | 허용 출처 | 쓰지 않는 것 |
|---|---|---|
| `close` (D-0 종가) | KRX 공식 정규장 종가(`TDD_CLSPRC`, `KRX_OFFICIAL`) | 네이버 일봉 종가. 19:59 시세라 시간외·NXT가 섞인다(`0edf389`) |
| `close_auction_volume` (종가 단일가 체결량) | 아래 둘 중 하나만 (A) `KRX_OFFICIAL_AUCTION` 또는 (B) `KRX_ONLY_BARS_PROVEN` | 일반 일거래량. KRX `ACC_TRDVOL`도 시간외를 포함한다(`0edf389`). 일거래량에서 장중 봉 합계를 빼는 대체 |
| `d1.open` (D+1 시가) | KRX 공식 시가(`TDD_OPNPRC`) | 09:00 분봉의 종가(시가와 다를 수 있다. `0edf389`). 09:00 봉의 시가 필드. 날짜가 붙은 네이버 일봉 시가(잠정값으로만 기록). NXT가 섞인 통합 시세 |
| `d1.open_auction_volume` | 종가 단일가 체결량과 같은 인증 기준 | |
| `d1.base`, `d1.lower_limit` | D+1 KRX 공식 행. 기준가는 `TDD_CLSPRC − CMPPREVDD_PRC`로 구한다 | |

**종가 단일가 체결량의 두 출처**
- **(A) `KRX_OFFICIAL_AUCTION`:** KRX가 게시한 그날 정규장 종가 단일가 체결량.
- **(B) `KRX_ONLY_BARS_PROVEN`:** KRX 단독 정규장 분봉이나 틱의 15:30 단일가 체결량. 그날 그 종목의 정규장 시계열이 **완전함을 증명한 경우에만** 인정한다.
  - 증명: 09:00 시가 단일가부터 15:30 종가 단일가까지 모든 봉의 거래량 합 = KRX 공식 **정규장 한정** 거래량.
  - 정규장 한정 공식 값이 없으면 증명은 성립하지 않는다.

**출처가 없으면 정산을 보류한다.** 상태는 `PENDING_CLOSE`, `PENDING_AUCTION_DATA`, `PENDING_BASE`, `PENDING_EXIT`이다.
- **인증된 0**은 미체결(`UNFILLED`)이다.
- **미확인**은 0이 아니다. `PENDING`이다.

### 3.4 필터 — 참·거짓·판단 불가

재료 종목마다 아래 조건을 평가한다. 각 조건은 참, 거짓, 판단 불가 중 하나다.
- 거짓이 하나라도 있으면 **`FAIL`**이다. 사유는 이 순서에서 첫 거짓이다.
- 거짓 없이 판단 불가가 있으면 **`UNDETERMINED`**다.
- 전부 참이면 **`PASS`**다.

| 순서 | 조건 | 거짓 | 판단 불가 |
|---|---|---|---|
| 1 | 코스피·코스닥 **보통주**. ETF·ETN·ELW·스팩·리츠·인프라펀드·우선주·코넥스 제외 | `NOT_COMMON_KOSPI_KOSDAQ` | `DATA_MISSING` |
| 2 | 시장조치 대상 아님. 대상은 관리종목, 투자주의환기, 투자경고·위험, 단기과열, D-0 거래정지, 정리매매 | `FLAGGED` | **`FLAGS_NOT_COLLECTED`** |
| 3 | 상장 후 20거래일 이상 | `NEW_LISTING` | `DATA_MISSING` |
| 4 | `p1520 ≥ 1,000원` | `PRICE` | 〃 |
| 5 | 유동성: `day_value ≥ 50억원` 그리고 `window_value ≥ 5억원` | `LIQUIDITY` | 〃 |
| 6 | `p1520 < upper_limit` | `UPPER_LIMIT_1520` | 〃 |
| 7 | `p1520 > prev_close` | `NOT_UP_ON_DAY` | 〃 |
| 8 | **`RS > 0`**. 지수 값이 빠지면 판단 불가 | `RS_NOT_POSITIVE` | 〃 |

**시장조치 플래그**
- **미수집**: 종목 행에 `flags` 키가 없거나 `null`이다. 이 경우 판단 불가다.
- **수집했고 대상 아님**: 빈 목록 `[]`이다. 이 경우 참이다.

**판단 불가 후보가 하나라도 있으면 그날은 `INDETERMINATE`다.**
- 그 후보가 1위를 이겼을 수 있으므로 매수 종목을 내지 않는다.
- 무거래로 세지 않고, 주 표본에서도 뺀다(§5.1).

**임계값과 VI**
- 임계값은 자연 경계(0, 상한가, 1,000원)와 어림수(50억원, 5억원, 20거래일)다. 어느 것도 데이터에 맞추지 않았다.
- VI 단일가 진행은 `vi_active`로 기록만 한다(§6).

### 3.5 순위, 동률, 1종목, 무거래

**순위**
- 기준은 `RS` 내림차순, 다음 `window_value` 내림차순, 다음 종목코드 오름차순이다.
- **RS는 입력 십진값을 정확한 유리수로 계산해 비교한다**(부록 A의 `Fraction(Decimal(str(v)))`). 4자리 반올림 값은 표시용이고 순위에 쓰지 않는다.
- 동률은 정확히 같은 값일 때만이다.

**매수와 기록**
- 랭크 1만 산다. 상위 5개는 기록한다.

**무거래**
- `NO_TRADE_NO_CATALYST`: 완전한 목록에서 재료가 0개인 날.
- `NO_TRADE_NO_PASS`: 모든 재료 종목이 확정 `FAIL`인 날.
- 무거래일은 손익 0으로 표본에 넣지 않고 따로 센다.

### 3.6 진입, 청산, 체결과 미체결 (그림자 가정)

가정 주문 금액은 N = 1,000만원이다. **단일가 체결 확인 기준**: 그 단일가의 인증된 체결대금이 20 × N 이상이어야 한다. 가정 주문이 체결대금의 5% 이하라는 뜻이다.

**진입 (D-0 종가 단일가)** — 위에서부터 차례로 본다.

| 경우 | 상태 |
|---|---|
| 공식 종가나 상한가가 없다 | `PENDING_CLOSE` |
| 종가 단일가 체결량이 인증되지 않았다 | `PENDING_AUCTION_DATA` |
| `close ≥ upper_limit` | `UNFILLED` (`UPPER_LIMIT_CLOSE`) |
| 인증된 체결대금 < 20N (인증된 0 포함) | `UNFILLED` (`AUCTION_TOO_SMALL`) |
| 그 밖 | 체결로 가정하고 청산으로 넘어간다 |

미체결이면 그날은 무거래다. 차순위로 바꾸지 않는다.

**청산 (D+1)** — 진입 뒤 위에서부터 차례로 본다.

| 경우 | 상태 |
|---|---|
| D+1 공식 기준가가 없다 | `PENDING_BASE` |
| 기준가 ≠ `close` (권리락, 배당락, 분할 등) | `EXCLUDED_BASE_ADJUSTED`. 체결된 보유이므로 건수를 판정 표에 따로 적는다 |
| D+1 하한가나 거래정지 여부를 모른다 | `PENDING_EXIT` (`EXIT_STATE_UNKNOWN`) |
| **정상 시가 청산**: 거래정지 아님, 공식 시가 > 하한가, 시가 단일가 체결대금이 확인 기준 충족 | `SETTLED` (`OPEN`), 청산가는 공식 시가 |
| **보수적 지연 청산**: 정상 시가 청산이 아니고, 아래 첫 체결 가능 봉을 재현할 수 있다 | `SETTLED` (`DELAYED`), 보유 거래일 수 기록 |
| 공식 시가가 없다 | `PENDING_EXIT` (`OPEN_DATA_MISSING`) |
| 시가 단일가 체결량이 인증되지 않았다 | `PENDING_EXIT` (`OPEN_AUCTION_VOLUME_MISSING`) |
| 그 밖 (하한가 시가, 거래정지, 시가 단일가가 너무 작음) | `HELD_UNRESOLVED` (`LIMIT_DOWN_OPEN`, `HALTED`, `OPEN_AUCTION_TOO_SMALL`) |

**첫 체결 가능 봉.** 시가 단일가 이후의 KRX 단독 정규장 1분봉 중 아래 셋을 모두 만족하는 첫 봉이다. 청산가는 그 봉의 **저가**다.
- 그 세션이 거래정지가 아니다.
- 봉 저가 > 그 세션의 하한가.
- 봉 거래량 × 봉 저가 ≥ 20N.

조건은 두 가지 더 있다.
- 그 세션 시계열의 완전성이 §3.3 (B)처럼 증명돼야 한다(`KRX_ONLY_BARS_PROVEN`).
- D+1에 그런 봉이 없으면 다음 거래일로 넘어간다.

**미해결 보유는 성과 표본에서 조용히 빠지지 않는다.** 미해결 보유는 `HELD_UNRESOLVED`, `PENDING_EXIT`, `PENDING_BASE`다. 처리는 §5.2를 따른다.

### 3.7 거래 비용

| 항목 | 값 |
|---|---|
| **비용 가정 (왕복)** | **0.25%p.** 이 문서가 정한 가정값이다. 실제 거래세와 수수료를 덮는다고 단정하지 않는다. 프로젝트 연구 상수는 0.18%p다(`src/perf_costs.py`) |
| 단일가 슬리피지 | 0. 진입과 청산이 모두 단일가이고, 가정 주문이 체결대금의 5% 이하다. 지연 청산은 봉 저가로 보수 처리한다 |
| 민감도 격자 (기록만) | 비용 0.18 / 0.25 / 0.35%p × 슬리피지 0 / 0.10 / 0.20%p |

- `gross = 청산가 / close − 1`, `net = gross − 0.25%p`. **판정은 `net`으로 한다.**
- 실제 요율(증권거래세 법령, 증권사 요율표)은 출처와 함께 E3 전에 §10에 적는다. 그 값으로 다시 계산한 `net` 열을 따로 보고한다. 판정 기준은 바꾸지 않는다.

### 3.8 소스와 시각 요약

| 입력 | 1차 소스 | 필요한 시각 |
|---|---|---|
| 공시 목록(공시번호, 분 단위 시각, 완전성) | KIND | 스냅샷 창 15:20~15:25 |
| 종목 1분봉 | KIS `FHKST03010230` 또는 KRX 단독 등가 | 〃 |
| 지수 1분봉 (KOSPI·KOSDAQ) | KIS 업종 분봉 또는 등가. **과거 일자 제공 여부는 미확인** | 〃 |
| 시장조치 플래그 | KIND 시장조치 또는 KIS 현재가 상태 필드 | 〃 |
| 서킷브레이커 여부 | KRX 공지, 지수 시계열 | 〃 |
| `daily` (전일 종가, 상한가, 상장 경과일) | KRX 공식 | D-0 00:00~15:25 |
| 정산 값 (§3.3 표) | KRX 공식, 인증 체결량 | 장 마감 후, D+1 이후 (결정과 무관) |
| 예외장 | `market_calendar.py`, KRX 공지 | 연초 |
| 외부 추천 | 원문 캡처(발송 시각 포함) | 받는 즉시 |

## 4. 기록 형식

하루 한 파일, `data/c2/<D-0>.jsonl`이다. 경로는 수집기 설계 때 확정한다.

| 행 | 내용 |
|---|---|
| 스냅샷 행 | 입력 묶음별 `source`, `available_at`, 원시 응답(또는 해시와 보관 경로), 공시 목록의 `complete` 근거 |
| 공시 행 | 공시번호, 원문 시각, 저장한 `published_at`, 보고서명, 판정 사유 |
| 후보 행 | §3.3의 원시 값, `state`, 사유, 표시용 RS |
| 결정 행 | 상태, 랭크 1~5, `sample`, `sample_problems`, `decision_recorded_at` |
| 정산 행 | 덧붙임. 상태, 출처, 체결 확인값, 비용 격자, 보유 거래일 수 |
| 검증 행 | 덧붙임. 15:25 이후 재조회 값과 스냅샷 값의 대조. 결정은 바꾸지 않는다 |
| 외부 추천 행 | 종목, 발송 시각, 원문 위치, 시간 등급, 가상 정산 |

## 5. 최소 관찰 증거와 판정 (고정)

### 5.1 주 표본 자격

`PRIMARY`가 되려면 두 조건을 모두 만족해야 한다.
- 결정 상태가 `PICKED`, `NO_TRADE_NO_PASS`, `NO_TRADE_NO_CATALYST` 중 하나다.
- `snapshot_problems`가 비어 있다. 즉 모든 입력 묶음이 스냅샷 창 안에 들어왔고, 결정 기록이 15:25:00 안이다.

또 D-0이 사용자 승인일 이후여야 한다.

| 경우 | 표본 처리 |
|---|---|
| 결정 상태는 위와 같으나 스냅샷이나 기록 시각을 어김 (D+1 08:29 기록, 장 마감 후 재구성 포함) | `INPUT_CHECK_ONLY`. 판정에 쓰지 않는다 |
| `SKIP_SPECIAL_SESSION`, `SKIP_MARKET_HALT`, `INVALID_INPUT`, `INPUT_MISSING`, `INDETERMINATE` | `EXCLUDED`. 정상 무거래로 세지 않고 상태별로 센다 |
| 승인 전 날짜의 소급 재구성 | 체로만 쓴다 |

### 5.2 증거 단계

**1거래일은 어떤 단계에서도 다거래일 검증으로 세지 않는다.**

| 단계 | 증거 | 통과 조건 | 뜻 |
|---|---|---|---|
| **E0 절차** | 합성·회귀 사례 (§9) | 28사례 기대값 일치 | 부록 A 코드가 문서 규칙과 같다. **완료 (2026-11-25)** |
| **E1 입력 복구** | 실제 거래일 1일 | 입력 다섯 묶음이 스냅샷 창 안에 있다. 결정 기록이 15:25 안이다. 사후 공식 값과 대조했다 | 수집할 수 있다는 것만 뜻한다 |
| **E2 수집 안정성** | 연속 20거래일 | `INVALID_INPUT + INPUT_MISSING + INDETERMINATE + INPUT_CHECK_ONLY + 파일 없음 ≤ 2일`. `UNFILLED`와 각 `PENDING`도 기록 | 표본을 셀 수 있다. 손익은 보지 않는다 |
| **E3 판정** | 주 표본 `PICKED` 정산 값 **n = 50** | 아래 세 조건 모두 | 그림자 통과. PAPER는 별도 설계 |

**E3 통과 조건** (C1 §4.1과 같은 형식)
1. `net` 평균 > 0
2. bootstrap(10,000회) 95% 신뢰구간 하한 > 0
3. 상위 2건을 뺀 평균 > 0

**표본 값 구성** (부록 A `sample_values`). 주 표본 `PICKED` 행을 시간순으로 읽는다.
- `SETTLED`: `net`을 넣는다.
- **미해결 보유** (`HELD_UNRESOLVED`, `PENDING_EXIT`, `PENDING_BASE`):
  - 진입 뒤 20거래일 안이면 **판정을 보류한다**(`DEFERRED_UNRESOLVED`).
  - 20거래일이 지나도 미해결이면 **−100%**로 넣고 건수를 적는다.
  - 조용히 빼지 않는다.
- **체결 여부 미확인** (`PENDING_CLOSE`, `PENDING_AUCTION_DATA`):
  - 20거래일 안이면 보류한다.
  - 그 뒤에는 `pre_fill_unresolved`로 세서 보고한다.
- `UNFILLED`와 `EXCLUDED_BASE_ADJUSTED`는 건수로 보고한다.

**판정을 미루는 경우.** `pre_fill_unresolved + INDETERMINATE`가 결정일의 10%를 넘으면 판정하지 않는다. 수집기를 먼저 고친다.

**조기 중단.** n = 30에서 **한 번만** 본다. 위와 같은 값 구성의 첫 30건이 다음 중 하나면 축을 닫는다.
- 평균 < −1.0%
- 누적 합산 최대 낙폭 < −15%p

값은 C1과 같다.

**기한.** E2 시작 뒤 250거래일 안에 n = 50을 채우지 못하면 "표본 부족"으로 판정 없이 닫는다.

**실행 가능성 경고.** `UNFILLED`가 `PICKED`의 20%를 넘으면 결과에 경고를 남긴다.

### 5.3 재평가

- 매월 말 수치를 §10에 붙인다.
- 판정은 n = 50에서만 한다.
- 규칙, 임계값, 판정식은 바꾸지 않는다.

## 6. 부 지표 (기록만)

**외부 추천 대비**

시간 등급은 **주문 기회만** 뜻한다. 체결이나 성과를 뜻하지 않는다.

| 등급 | 발송 시각 |
|---|---|
| `SAME_TIME` | 15:20:00 이하 |
| `IN_AUCTION` | 15:30 전 |
| `AFTER_CLOSE` | 15:30 이후 |
| `UNVERIFIED` | 시각이 없거나 형식 오류, 또는 다른 날짜 |

- 가상 체결과 청산은 **C2와 같은 `settle_ticker` 기준**으로 계산한다. 공식 종가, 인증 체결량, 같은 청산 규칙, 같은 비용 가정이다.
- 쌍 비교에는 셋을 모두 만족하는 추천만 넣는다.
  - 등급이 `SAME_TIME` 또는 `IN_AUCTION`이다.
  - 가상 정산이 `SETTLED`다.
  - 그날이 C2 `PRIMARY`다.
- 쌍 비교 수치: 같은 날 C2와 외부 추천 동일가중 평균의 차이. C2 무거래일의 외부 추천 성과. 외부 추천 종목이 C2 재료 종목에 들었는지와 탈락 사유.

**11/23 사례 (`0edf389` 기준)**
- 세 종목의 동일 비중 비용 전 가상 수익률은 **+5.21%이고 잠정값**이다.
  - 진입: 11/23 KRX 공식 종가.
  - 청산: 날짜가 붙은 네이버 일봉의 11/24 시가.
  - 조회 시점(11-25 01:12)에 KRX 11/24 자료는 게시 전이었다.
- 이수페타시스의 네이버 통합 시세 시가 78,000원은 NXT가 섞인 값이라 쓰지 않았다.
- 이 사례는 다음 이유로 **쌍 비교 대상이 아니다**.
  - 발송 시각 미확인: `UNVERIFIED`.
  - 종가 단일가 체결량 미인증: 가상 정산 `PENDING_AUCTION_DATA`.
  - 청산 시가가 KRX 공식이 아니다.
  - C2 등록 전 사례다.

**그 밖의 부 지표**
- 지수 초과: `gross − (지수 D+1 시가 / 지수 D-0 종가 − 1)`.
- 15:20 → 종가 변화: `close / p1520 − 1`.
- 지연 청산 건수와 보유 거래일 수.
- 스냅샷 대 사후 대조 불일치 건수.
- 랭크 2~5 평균, 공시 유형별 평균, `vi_active` 날짜의 성과, 비용 격자, 트랙 A와 같은 날 상관.

## 7. 수집기 요구사항 (범위 밖, 승인 뒤 별도 설계)

**운영과 분리**
- `main.py`, `src/modules`, 주문 경로, 전략 지문 대상 파일과 환경변수를 건드리지 않는다.
- 운영 프로세스와 상태를 공유하지 않는다.
- `scripts/`와 별도 데이터 폴더 안에서 끝낸다.

**실행 시각**
- **결정 수집**은 D-0 15:20:00~15:25:00 안에서 한다. 스냅샷을 받고, 결정 행을 15:25:00까지 쓴다.
  - 이 창을 놓친 날은 그날 다시 돌려도 `INPUT_CHECK_ONLY`다.
- **정산 수집**은 장 마감 후와 D+1 이후에 한다. 대상은 KRX 공식 값이 게시된 뒤의 종가, 인증 체결량, 시가, 기준가, 하한가다.
- **검증 재조회**는 언제든 하되, 결정은 바꾸지 않는다.

**KIS 호출 조건**
- 앱키당 WS 세션은 1개다. 그래서 REST만 쓴다.
- 트랙 A 진입 창(09:00~09:11)은 피한다.
- 운영과 같은 앱키이므로 호출 예산 상한을 둔다.

**첫 확인 항목**
- 지수 분봉의 과거 일자 제공 여부.
- KIND 목록 완전성 근거(총건수).
- KRX 공식 **정규장 한정** 거래량 또는 종가·시가 단일가 체결량을 얻을 수 있는지. 없으면 §3.3 (B)는 성립하지 않고 정산은 계속 `PENDING`이다.
- KIS 분봉이 KRX 단독인지 NXT 통합인지.

## 8. 저장 자료 점검 결과 (2026-11-25 실측, 읽기 전용)

**대상**
- 운영 트리 `D:\Private\stock-prod\data`: `release/20261121-1`, `1e67c7c`
- 개발 트리 `D:\Private\stock\data`

세션 임시 폴더의 읽기 전용 스크립트를 썼다(`scan_inputs.py`, `bar_cov.py`). 네트워크, 브로커, 인증 호출은 없었다.

### 8.1 입력별 가용성

| C2 입력 | 저장 자료 | 시점 일치 복구 |
|---|---|---|
| 스냅샷 `source`와 `available_at`, 15:25 전 결정 기록 | 어떤 저장 자료에도 C2 스냅샷 창 개념이 없다 | **불가.** 값이 다 있어도 과거일은 최대 `INPUT_CHECK_ONLY`다 |
| 당일 공시, 공개 시각, 목록 완전성 | `data/catalyst/labels.jsonl` 149행. 07-01~09-10, 트랙 A 갭 종목만, **시각 필드 없음** | **불가** |
| 지수 14:50·15:20 | 두 트리 `data/` 전체에서 업종·지수 필드가 있는 파일 **0개**. 소스 코드에도 업종 TR 호출이 없다 | **불가** |
| 재료 종목 14:50·15:20 가격과 거래대금 | `backtest_bars/`의 D-0 분봉은 트랙 A 랭크 1~5와 C1 D+1 후보뿐이다(하루 2~12개) | 선택 편향된 일부만 가능. 정의 자체는 작동한다(§8.3) |
| 15:20 장중 스냅샷 | `overnight/candidates_1520/` 26일. 14:50 값 없음, 닫힌 C1 축 | 쓰지 않는다 |
| 시장조치 플래그 | C1 랭킹 원문(11-19까지, 거래량 상위만) | 불가 |
| KRX 공식 종가·시가·기준가 | `krx_daily`가 **2026-09-29까지**. 그 뒤 공식 일별 값은 저장돼 있지 않다 | 09-30 이후 불가 |
| 인증된 종가·시가 단일가 체결량 | 없음. 분봉 `153000` 봉은 출처 인증과 완전성 증명이 없고, 10-14 이후 파일 223개 중 **115개에는 아예 없다** | **불가** |
| 거래일 달력 | `overnight/calendar.json`이 **11-19에서 끝남** | 11-20 이후 불가 |
| 외부 추천 3종목(11/23) | 해당 종목의 저장 분봉·일봉이 없다. 발송 시각 미확인 | 불가. 공식 결과는 `0edf389`와 §6을 보라 |

**결론: 현재 저장된 자료만으로는 C2 입력을 시점 일치로 복구할 수 있는 거래일이 하루도 없다.**
- 스냅샷 시각 규율이 없었다.
- 공시 시각, 지수, 시장조치, 인증 단일가 체결량이 통째로 없다.

그래서 주문 없는 첫 실제 관찰은 수행하지 못했다. 가격 구간 정의만 실제 분봉으로 시험했고(§8.2, §8.3), 절차는 합성 사례로 점검했다(§9).

### 8.2 분봉 라벨 실측 (§3.3 근거)

2026-11-24 005930의 운영 틱 캡처를 `read_jsonl_gz`로 읽어(잘림 없음) 같은 날 백필 분봉과 대조했다.

| 봉 라벨 | 분봉 OHLC · 거래량 | 틱 [라벨, +1분) | 틱 (−1분, 라벨] |
|---|---|---|---|
| 144900 | 192100/192100/191800/191800 · 11,233 | **같음** (300틱) | 다름 (25,558) |
| 145000 | 191800/192000/191800/191900 · 17,063 | **같음** | 다름 |
| 151800 | 188900/189300/188800/188900 · 31,680 | **같음** | 다름 |
| 151900 | 189000/189200/188900/**189200** · 29,072 | 종가 189000 · 28,075 (근접) | 다름 |

- 라벨은 분의 시작이다.
- 틱 캡처는 15:19:59.96에서 끝나 15:20 직전을 놓친다. 그래서 15:20 가격은 분봉 `151900` 종가로 정한다.

### 8.3 저장 분봉으로 가격 구간만 부분 재생 (D-0 2026-11-23)

가격 정의가 계산되는지만 봤다. 지수, 공시, 스냅샷 시각이 없어 **RS와 결정은 없다.** 정산 값은 보지 않았다.

| 종목 | `p1450` | `p1520` | `r_stock` | `window_value` | `day_value` | `153000` 봉 존재 | 창 안 봉 수 |
|---|---:|---:|---:|---:|---:|---|---:|
| 005930 | 178,700 | 177,900 | −0.448% | 1,344.5억 | 29,423억 | 있음 (인증 아님) | 30/30 |
| 005935 | 142,900 | 141,300 | −1.120% | 357.1억 | 5,266억 | 있음 (인증 아님) | 30/30 |
| 009830 | 19,050 | 18,810 | −1.260% | 27.1억 | 412억 | 있음 (인증 아님) | 30/30 |
| 028050 | 55,000 | 55,500 | +0.909% | 239.8억 | 2,228억 | 있음 (인증 아님) | 30/30 |
| 047040 | 9,830 | 9,720 | −1.119% | 29.1억 | 449억 | 있음 (인증 아님) | 30/30 |
| 451250 | 8,260 | 8,170 | −1.090% | 9.2억 | 311억 | **없음** | 30/30 |

이 6종목은 트랙 A의 아침 후보라서 C2 모집단을 대표하지 않는다.

### 8.4 단일가 봉 결측과 인증

10-14 이후 백필 분봉 223개 가운데:
- `144900`, `151900`, `153000`, `090000`이 모두 있는 파일: 104개
- **`153000`이 없는 파일: 115개**
- 14:49 봉이 없는 파일: 6개

`153000` 봉이 있어도 다음 두 가지가 확인되지 않았다.
- 출처가 KRX 단독인지
- 그날 정규장 시계열이 완전한지

그래서 C2에서는 어느 쪽이든 인증 체결량으로 쓰지 않는다(§3.3). 일거래량에서 장중 봉 합계를 빼는 대체도 쓰지 않는다.

### 8.5 필요한 데이터와 정확한 누락

| # | 필요한 것 | 현재 | 채우는 길 (이 작업에서 호출하지 않음) |
|---|---|---|---|
| 1 | 입력 묶음별 `source`·`available_at` 스냅샷, 15:25 전 결정 기록 | 없음 | 수집기(§7) |
| 2 | D-0 KIND 공시 목록 + 완전성 근거 | 없음 | KIND 목록 저장 |
| 3 | KOSPI·KOSDAQ 1분봉 14:49·15:19, 공식 종가, D+1 시가 | 없음 | KIS 업종 분봉·일봉. 과거 일자 **미확인** |
| 4 | 재료 종목 D-0 1분봉 (KRX 단독) | 우연히 겹친 종목만 | KIS `FHKST03010230` (343일 실측) |
| 5 | KRX 공식 D-1·D-0·D+1 일별 값 (종가, 상한가, 시가, 전일대비, 하한가) | 09-29까지 | KRX 일별매매정보 |
| 6 | 인증된 종가·시가 단일가 체결량 | 없음 | KRX 공식 값, 또는 정규장 한정 공식 거래량으로 증명한 KRX 단독 분봉. **공식 출처 존재 여부 미확인** |
| 7 | D-0 시장조치 플래그 | 없음 | KIND 시장조치, KIS 상태 필드 |
| 8 | KRX 거래일 달력 (11-20 이후) | 없음 | KIS 일봉(005930) |
| 9 | 외부 추천 원문과 발송 시각 | 11/23 3종목, 시각 미확인 | 원문 캡처 |

## 9. 합성·회귀 사례 점검 (E0, 2026-11-25 실행)

**구현과 입력**
- 부록 A의 `c2_rule.py`와 부록 B의 `c2_check.py`를 같은 폴더에 두고 실행했다.
- 가짜 날짜(2099-01-05~02-02), 가짜 종목코드, 손으로 만든 입력 28사례다.
- 네트워크, 파일 쓰기, 주문은 없다.

**재현 방법** (저장소 루트, Git Bash):

```sh
DOC=docs/superpowers/specs/2026-11-25-c2-close-buy-catalyst-rs-hypothesis.md; mkdir -p /tmp/c2 && \
awk '/^<!-- code:c2_rule -->$/{f=1;next} f&&/^```python$/{g=1;next} g&&/^```$/{exit} g' "$DOC" > /tmp/c2/c2_rule.py && \
awk '/^<!-- code:c2_check -->$/{f=1;next} f&&/^```python$/{g=1;next} g&&/^```$/{exit} g' "$DOC" > /tmp/c2/c2_check.py && \
(cd /tmp/c2 && PYTHONIOENCODING=utf-8 python -I c2_check.py)
```

**실제 실행 출력** (Python 3.12.10, 개발 트리 `.venv`):

```
[OK ] S01 20990105 PICKED pick=A100 sample=PRIMARY settle=SETTLED OPEN
[OK ] S02 20990106 PICKED pick=B000 sample=PRIMARY settle=UNFILLED UPPER_LIMIT_CLOSE
[OK ] S03 20990107 NO_TRADE_NO_CATALYST pick=None sample=PRIMARY settle=NO_TRADE_NO_CATALYST
[OK ] S04 20990108 NO_TRADE_NO_PASS pick=None sample=PRIMARY settle=NO_TRADE_NO_PASS
[OK ] S05 20990109 SKIP_SPECIAL_SESSION pick=None sample=EXCLUDED settle=SKIP_SPECIAL_SESSION
[OK ] S06 20990112 INDETERMINATE pick=None sample=EXCLUDED settle=INDETERMINATE
[OK ] S07 20990113 PICKED pick=V1 sample=PRIMARY settle=EXCLUDED_BASE_ADJUSTED
[OK ] S08 20990114 NO_TRADE_NO_PASS pick=None sample=PRIMARY settle=NO_TRADE_NO_PASS
[OK ] R1 20990115 INDETERMINATE pick=None sample=EXCLUDED settle=INDETERMINATE  DECISION_RECORD_OUTSIDE_WINDOW
[OK ] R1a 20990115 PICKED pick=Q1 sample=INPUT_CHECK_ONLY settle=PENDING_CLOSE  DECISION_RECORD_OUTSIDE_WINDOW
[OK ] R1b 20990115 INDETERMINATE pick=None sample=EXCLUDED settle=INDETERMINATE
[OK ] R2 20990116 PICKED pick=P1 sample=PRIMARY settle=HELD_UNRESOLVED LIMIT_DOWN_OPEN
[OK ] R2b 20990116 PICKED pick=P1 sample=PRIMARY settle=SETTLED DELAYED
[OK ] R3 20990120 PICKED pick=O1 sample=PRIMARY settle=PENDING_AUCTION_DATA
[OK ] R3b 20990120 PICKED pick=O1 sample=PRIMARY settle=UNFILLED AUCTION_TOO_SMALL
[OK ] R3c 20990120 PICKED pick=O1 sample=PRIMARY settle=PENDING_AUCTION_DATA
[OK ] R4 20990121 INPUT_MISSING pick=None sample=EXCLUDED settle=INPUT_MISSING
[OK ] R5 20990122 INDETERMINATE pick=None sample=EXCLUDED settle=INDETERMINATE
[OK ] R6 20990230 INVALID_INPUT pick=None sample=EXCLUDED settle=INVALID_INPUT
[OK ] R6b 20990123 INVALID_INPUT pick=None sample=EXCLUDED settle=INVALID_INPUT
[OK ] R7 20990126 PICKED pick=M1 sample=PRIMARY settle=PENDING_CLOSE
[OK ] R8 20990127 PICKED pick=L1 sample=PRIMARY settle=PENDING_EXIT OPEN_DATA_MISSING
[OK ] R8b 20990127 PICKED pick=L1 sample=PRIMARY settle=PENDING_BASE
[OK ] R9 20990128 SKIP_MARKET_HALT pick=None sample=EXCLUDED settle=SKIP_MARKET_HALT
[OK ] R9b 20990128 INPUT_MISSING pick=None sample=EXCLUDED settle=INPUT_MISSING
[OK ] R10 20990129 PICKED pick=K1 sample=PRIMARY settle=HELD_UNRESOLVED HALTED
[OK ] R11 20990130 PICKED pick=J1 sample=INPUT_CHECK_ONLY settle=SETTLED OPEN disclosures:OUTSIDE_WINDOW,bars:OUTSIDE_WINDOW,index:OUTSIDE_WINDOW,flags:OUTSIDE_WINDOW
[OK ] R12 20990202 NO_TRADE_NO_CATALYST pick=None sample=PRIMARY settle=NO_TRADE_NO_CATALYST
external S01: [('SAME_TIME', 'SETTLED', 1.4784, True), ('IN_AUCTION', 'SETTLED', 1.2278, True), ('AFTER_CLOSE', 'SETTLED', 1.2278, False), ('UNVERIFIED', 'SETTLED', 1.2278, False)]
regression: ALL PASS (28 cases)
```

**리뷰 반례 재현.** 리뷰가 `96c25d1` 부록에서 찾은 반례 세 개를 그 부록 코드로 다시 돌렸다(부록 C). 셋 다 지적대로 잘못 나왔다.

```
08:29+flags missing -> PICKED Q1 SETTLED
limit-down open -> PICKED Q1 SETTLED
auction volume None -> PICKED Q1 UNFILLED
```

새 코드에서 같은 입력의 결과는 다음과 같다.

| 반례 | 새 결과 (사례) |
|---|---|
| D+1 08:29 기록 + 시장조치 키 없음 | `INDETERMINATE` / `EXCLUDED` / `DECISION_RECORD_OUTSIDE_WINDOW` (R1) |
| 늦은 기록만 | `INPUT_CHECK_ONLY` (R1a) |
| 키 없음만 | `INDETERMINATE` (R1b) |
| 하한가 시가 | `HELD_UNRESOLVED` (`LIMIT_DOWN_OPEN`) (R2). 재현 가능한 지연 청산이 있을 때만 `SETTLED DELAYED` (R2b) |
| 체결량 `None` | `PENDING_AUCTION_DATA` (R3) |
| 인증된 0 | `UNFILLED` (R3b) |
| 미인증 출처 | `PENDING_AUCTION_DATA` (R3c) |

**사례별 확인 내용**

| 사례 | 확인한 것 |
|---|---|
| S01 | 공시 판정 사유 7종. 15:20 분 단위 공시(`:59`)는 탈락, `15:19:59`는 통과, 오프셋 없는 시각은 `BAD_TIMESTAMP`. 필터 탈락 4종. RS 1.30 > 1.25. gross +1.7284%, net +1.4784%. 외부 추천 4등급과 쌍 비교 자격 |
| S02 | 정확한 RS 동률(10000→10120과 20000→20240) → 창 거래대금 → 종목코드. 상한가 마감 미체결, 차순위 대체 없음 |
| S03, R12 | 완전한 목록에서 무재료 |
| R4 | 미완 목록은 `INPUT_MISSING` |
| S04, S08 | `RS ≤ 0` (S08은 RS = 0 경계) |
| S05 | 10시 개장일 |
| R9 | 서킷브레이커 |
| R9b | 서킷브레이커 여부 미확인 |
| R6 | 없는 날짜 (`20990230`) |
| R6b | 알 수 없는 세션 |
| S06 | 지수 결측. **`96c25d1`의 `NO_TRADE_NO_PASS`에서 `INDETERMINATE`로 바뀌었다** |
| R5 | 하위로 보장되지 않는 후보의 판단 불가 → 그날 `INDETERMINATE` |
| S07 | 기준가 조정 |
| R8 | 공식 시가 결측 → `PENDING_EXIT` |
| R8b | 기준가 출처 결측 → `PENDING_BASE` |
| R10 | 거래정지 → `HELD_UNRESOLVED` |
| R7 | 4자리 반올림 RS가 같고(1.23) 창 거래대금은 M2가 크다. 정확한 RS로는 M1이 1위다(반올림 순위였다면 M2) |
| R11 | 스냅샷이 15:20 전(15:19:59)에 받은 값이면 `INPUT_CHECK_ONLY`. 외부 추천 가상 정산도 체결량 미인증이면 쌍 비교에서 빠진다 |

`sample_values` 단언으로 확인한 것:
- 20거래일 안의 미해결 보유는 판정 보류(`DEFERRED_UNRESOLVED`)다.
- 기한이 지난 미해결 보유는 −100%로 들어간다.

**이 점검이 보장하는 것과 보장하지 않는 것**
- 보장한다: 부록 코드가 이 문서의 규칙대로 동작한다.
- 보장하지 않는다: 실제 소스의 필드와 단위, KIND 시각 형식, 지수 분봉 라벨, 공식 단일가 체결량의 존재 여부. 이것들은 E1과 수집기 설계에서 확인한다.

## 10. 남은 관찰

1. **사용자 승인.** 규칙(§3)과 판정(§5)을 이대로 고정할지 정한다. 승인일이 표본 자격의 시작이다.
2. **수집기 설계(§7).** 첫 확인 항목 네 가지를 먼저 본다. 운영 트리 반영은 장 마감 후에만 한다.
3. **E1:** 실제 1일. "수집 가능"만 뜻한다.
4. **E2:** 연속 20거래일.
5. **E3:** n = 50. 조기 중단은 n = 30에서 한 번 본다. 판정 전 실제 비용 요율의 출처를 적는다.
6. **11/23 외부 추천.** `main`의 `0edf389`가 +5.21%(잠정)를 기록했다. KRX 11/24 공식 시가가 게시되면 그 문서에서 다시 대조한다(특히 이수페타시스).
   - C2 표본과는 무관하다.
   - 이 브랜치는 `0edf389` 이전에서 갈라졌다. 두 커밋은 서로 다른 파일만 고친다.

## 11. 수정 기록

**2026-11-25 (1)** 초안 `96c25d1`. 표본 0.

**2026-11-25 (2)** 리뷰 반영. 표본이 0인 상태에서 같은 가설의 초안을 보완한 것이다. 바뀐 것:
- 입력 기준 시각(15:20)과 결정 기록 마감(15:25)을 구분했다.
  - 스냅샷의 `source`와 `available_at`, 마감 안의 결정 기록을 주 표본 조건으로 두었다.
  - `D+1 08:30` 기준을 없앴다. 사후 자료는 입력 검증용으로만 쓴다.
- 공시 목록 미완을 무재료와 구분했다.
- 날짜·세션 오류(`INVALID_INPUT`)와 서킷브레이커 미확인을 정상 무거래와 구분했다.
- 시장조치 미수집을 빈 플래그와 구분했다. 판단 불가 후보가 있는 날은 `INDETERMINATE`로 주 표본에서 뺐다.
- 종가 단일가 체결량을 인증 출처로만 받는다.
  - 일거래량에서 장중 봉 합계를 빼는 대체를 삭제했다.
  - 미확인은 `PENDING_AUCTION_DATA`, 인증된 0은 `UNFILLED`다.
- 하한가 시가, 거래정지, 시가 결측, 시가 단일가 미인증이면 `SETTLED`로 하지 않는다.
  - 재현 가능한 보수적 지연 청산이 있을 때만 정산한다.
  - 미해결은 기한까지 판정을 보류하고, 기한이 지나면 −100%로 넣는다.
  - 기준가 출처가 없으면 보류한다.
- 0.25%p는 설정한 비용 가정이라고 표기했다.
- 외부 추천의 시간 등급은 주문 기회만 뜻한다고 밝혔다. 가상 정산도 같은 체결 기준을 쓴다. 11/23 내용은 `0edf389`에 맞췄다.
- 09:00 분봉 종가는 시가가 아니라고 밝혔다.
- RS 순위는 정확한 유리수로 비교한다(이전은 4자리 반올림). 타임스탬프는 `+09:00` 오프셋을 검증한다.

## 부록 A. 규칙 구현 (§9에서 실행한 `c2_rule.py`와 바이트 단위로 같다)

<!-- code:c2_rule -->
```python
"""C2 규칙 — 순수 함수(주문·네트워크·파일 쓰기 없음). 입력 dict만 보고 실행 시각은 보지 않는다."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction

KST_OFFSET = timedelta(hours=9)
KST = timezone(KST_OFFSET)
INPUT_CUTOFF = time(15, 20, 0)       # 입력 기준 시각: 이때까지 공개·체결된 것만 쓴다
DECISION_DEADLINE = time(15, 25, 0)  # 스냅샷·결정 기록 마감 (종가 단일가 15:20~15:30 안)
AUCTION_END = time(15, 30, 0)
# 입력 묶음별 available_at 허용 하한. daily(전일 종가·상한가·상장일)는 당일 0시 이후면 된다.
INPUT_MIN_AVAIL = {"disclosures": INPUT_CUTOFF, "bars": INPUT_CUTOFF, "index": INPUT_CUTOFF,
                   "flags": INPUT_CUTOFF, "daily": time(0, 0, 0)}
SESSIONS = {"REGULAR", "LATE_OPEN", "SHORT", "HOLIDAY"}

MIN_PRICE = 1_000
MIN_DAY_VALUE = 5_000_000_000        # 09:00~15:19 봉 Σ(거래량×봉 종가) ≥ 50억원
MIN_WINDOW_VALUE = 500_000_000       # 14:50~15:19 봉 Σ(거래량×봉 종가) ≥ 5억원
MIN_LISTED_SESSIONS = 20
NOTIONAL = 10_000_000                # 가정 주문 금액 1,000만원
MIN_FILL_VALUE = 20 * NOTIONAL       # 가정 주문 ≤ 해당 단일가(또는 봉) 체결대금의 5%
COST_ASSUMPTION_PCT = 0.25           # 설정한 왕복 비용 가정(%p). 실제 원가를 덮는다는 단정이 아니다
COST_GRID = (0.18, 0.25, 0.35)
SLIP_GRID = (0.0, 0.10, 0.20)
UNRESOLVED_LIMIT_SESSIONS = 20       # 진입 뒤 이 거래일 수가 지나도 미해결이면 −100%로 넣는다

OFFICIAL_PRICE_SOURCES = {"KRX_OFFICIAL"}                          # KRX 정규장 공식 가격
CERTIFIED_VOLUME_SOURCES = {"KRX_OFFICIAL_AUCTION", "KRX_ONLY_BARS_PROVEN"}
DELAYED_EXIT_SOURCES = {"KRX_ONLY_BARS_PROVEN"}

WHITELIST = (
    "단일판매ㆍ공급계약체결", "단일판매·공급계약체결", "단일판매공급계약체결",
    "영업(잠정)실적", "매출액또는손익구조",
    "자기주식취득결정", "자기주식취득신탁계약체결결정", "주식소각결정",
    "무상증자결정",
    "유형자산취득결정", "타법인주식및출자증권취득결정", "신규시설투자",
    "특허권취득", "기술도입ㆍ이전ㆍ제휴계약체결", "기술이전계약",
    "투자판단관련주요경영사항",
)
AMEND_PREFIXES = ("[기재정정]", "[첨부정정]", "[첨부추가]", "[변경등록]", "[정정]")
EXCLUDED_FLAGS = {"ADMIN", "CAUTION_ENV", "WARNING", "RISK", "OVERHEAT", "HALTED", "LIQUIDATION"}
INDEX_OF = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}
DECIDED = {"PICKED", "NO_TRADE_NO_PASS", "NO_TRADE_NO_CATALYST"}


# ---------- 값 검증 ----------
def parse_kst(ts) -> datetime | None:
    """ISO 8601 + 오프셋 +09:00만 받는다. 오프셋 없는 값·다른 오프셋·형식 오류는 None."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() != KST_OFFSET:
        return None
    return dt


def parse_day(s) -> date | None:
    if not (isinstance(s, str) and len(s) == 8 and s.isdigit()):
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def num(v, allow_zero: bool = False) -> Fraction | None:
    """가격·거래대금을 정확한 유리수로. bool·None·음수·형식 오류는 None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = Fraction(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None
    if f < 0 or (f == 0 and not allow_zero):
        return None
    return f


def _at(d0: date, t: time) -> datetime:
    return datetime.combine(d0, t, KST)


def _pct(f: Fraction) -> float:
    return float(f * 100)


# ---------- 결정 (D-0 15:20 입력) ----------
def snapshot_problems(day: dict, d0: date) -> list[str]:
    """주 표본 자격: 입력 묶음마다 source와 available_at, 마감 안의 결정 기록."""
    snap = day.get("snapshot")
    if not isinstance(snap, dict):
        return ["NO_SNAPSHOT"]
    probs, avail = [], []
    for kind, lo in INPUT_MIN_AVAIL.items():
        m = snap.get(kind)
        if not isinstance(m, dict) or not m.get("source"):
            probs.append(f"{kind}:NO_SOURCE")
            continue
        t = parse_kst(m.get("available_at"))
        if t is None:
            probs.append(f"{kind}:BAD_AVAILABLE_AT")
        elif not (_at(d0, lo) <= t <= _at(d0, DECISION_DEADLINE)):
            probs.append(f"{kind}:OUTSIDE_WINDOW")
        else:
            avail.append(t)
    rec = parse_kst(snap.get("decision_recorded_at"))
    if rec is None:
        probs.append("NO_DECISION_RECORD")
    elif rec > _at(d0, DECISION_DEADLINE) or rec < _at(d0, INPUT_CUTOFF) or (avail and rec < max(avail)):
        probs.append("DECISION_RECORD_OUTSIDE_WINDOW")
    return probs


def eligible_disclosure(d: dict, d0: date) -> str | None:
    """None이면 재료로 인정. KIND 분 단위 시각은 수집기가 HH:MM:59로 넣는다."""
    if d.get("source") != "KIND" or not d.get("id"):
        return "SOURCE_UNVERIFIABLE"
    pub = parse_kst(d.get("published_at"))
    if pub is None:
        return "BAD_TIMESTAMP"
    if pub.date() != d0:
        return "NOT_SAME_DAY"
    if pub > _at(d0, INPUT_CUTOFF):
        return "AFTER_CUTOFF"
    title = d.get("title") or ""
    if title.startswith(AMEND_PREFIXES):
        return "AMENDMENT"
    t = title.replace(" ", "")
    if not any(k.replace(" ", "") in t for k in WHITELIST):
        return "NOT_WHITELISTED"
    return None


def _ret(a: Fraction, b: Fraction) -> Fraction:
    return b / a - 1


def evaluate(s: dict | None, idx: dict) -> tuple[str, str | None, Fraction | None]:
    """('PASS'|'FAIL'|'UNDETERMINED', 사유, RS). 확정 탈락이 하나라도 있으면 FAIL,
    탈락은 없는데 판단 못 한 조건이 있으면 UNDETERMINED."""
    if not isinstance(s, dict):
        return "UNDETERMINED", "DATA_MISSING", None
    g = {k: num(s.get(k)) for k in ("prev_close", "upper_limit", "p1450", "p1520",
                                     "day_value", "window_value")}
    listed = s.get("listed_sessions")
    listed = listed if isinstance(listed, int) and not isinstance(listed, bool) else None
    flags = s.get("flags")  # None/키 없음 = 미수집, [] = 수집했고 없음
    ix = idx.get(INDEX_OF.get(s.get("market"), ""), {}) if isinstance(idx, dict) else {}
    i1450, i1520 = num(ix.get("p1450")), num(ix.get("p1520"))
    rs = None
    if None not in (g["p1450"], g["p1520"], i1450, i1520):
        rs = _ret(g["p1450"], g["p1520"]) - _ret(i1450, i1520)

    def common():
        if s.get("market") is None or s.get("kind") is None:
            return None
        return s["market"] in INDEX_OF and s["kind"] == "COMMON"

    def flag_ok():
        if not isinstance(flags, list):
            return None
        return not (EXCLUDED_FLAGS & set(flags))

    def both(a, b):
        if a is False or b is False:
            return False
        return None if (a is None or b is None) else True

    p = g["p1520"]
    conds = [
        ("NOT_COMMON_KOSPI_KOSDAQ", "DATA_MISSING", common()),
        ("FLAGGED", "FLAGS_NOT_COLLECTED", flag_ok()),
        ("NEW_LISTING", "DATA_MISSING", None if listed is None else listed >= MIN_LISTED_SESSIONS),
        ("PRICE", "DATA_MISSING", None if p is None else p >= MIN_PRICE),
        ("LIQUIDITY", "DATA_MISSING", both(
            None if g["day_value"] is None else g["day_value"] >= MIN_DAY_VALUE,
            None if g["window_value"] is None else g["window_value"] >= MIN_WINDOW_VALUE)),
        ("UPPER_LIMIT_1520", "DATA_MISSING",
         None if None in (p, g["upper_limit"]) else p < g["upper_limit"]),
        ("NOT_UP_ON_DAY", "DATA_MISSING", None if None in (p, g["prev_close"]) else p > g["prev_close"]),
        ("RS_NOT_POSITIVE", "DATA_MISSING", None if rs is None else rs > 0),
    ]
    for fail_reason, _, ok in conds:
        if ok is False:
            return "FAIL", fail_reason, rs
    for _, unknown_reason, ok in conds:
        if ok is None:
            return "UNDETERMINED", unknown_reason, rs
    return "PASS", None, rs


def screen(day: dict) -> dict:
    out: dict = {"date": day.get("date"), "status": None, "pick": None, "ranked": [],
                 "candidates": {}, "disclosures": {}, "sample": "EXCLUDED", "sample_problems": []}
    d0 = parse_day(day.get("date"))
    session = day.get("session")
    if d0 is None or session not in SESSIONS:
        out["status"] = "INVALID_INPUT"
        return out
    if session != "REGULAR":
        out["status"] = "SKIP_SPECIAL_SESSION"
        return out
    halt = day.get("market_halt")
    if halt is True:
        out["status"] = "SKIP_MARKET_HALT"
        return out
    out["sample_problems"] = snapshot_problems(day, d0)
    dl = day.get("disclosure_list")
    if halt is not False or not isinstance(dl, dict) or dl.get("complete") is not True \
            or not isinstance(dl.get("rows"), list):
        out["status"] = "INPUT_MISSING"
        return out
    cands: set[str] = set()
    for i, d in enumerate(dl["rows"]):
        why = eligible_disclosure(d, d0)
        out["disclosures"][str(d.get("id") or f"row{i}")] = why or "OK"
        if why is None and d.get("ticker"):
            cands.add(d["ticker"])
    if not cands:
        out["status"] = "NO_TRADE_NO_CATALYST"
    else:
        passed, undetermined = [], False
        for tk in sorted(cands):
            s = (day.get("stocks") or {}).get(tk)
            state, why, rs = evaluate(s, day.get("index"))
            out["candidates"][tk] = {"state": state, "reason": why,
                                     "rs_pp_display": None if rs is None else round(_pct(rs), 4)}
            undetermined |= state == "UNDETERMINED"
            if state == "PASS":
                passed.append((rs, num(s["window_value"]), tk))
        # 순위: 정확한 유리수 RS 내림차순 → 창 거래대금 내림차순 → 종목코드 오름차순
        passed.sort(key=lambda r: (-r[0], -r[1], r[2]))
        out["ranked"] = [r[2] for r in passed[:5]]
        if undetermined:
            out["status"] = "INDETERMINATE"     # 판단 못 한 후보가 1위를 이겼을 수 있다
        elif passed:
            out["status"], out["pick"] = "PICKED", passed[0][2]
        else:
            out["status"] = "NO_TRADE_NO_PASS"
    if out["status"] in DECIDED:
        out["sample"] = "INPUT_CHECK_ONLY" if out["sample_problems"] else "PRIMARY"
    return out


# ---------- 체결과 청산 (D-0 종가 단일가 → D+1) ----------
def settle_ticker(s: dict | None) -> dict:
    if not isinstance(s, dict):
        return {"status": "PENDING_CLOSE", "reason": "NO_DATA"}
    close = num(s.get("close")) if s.get("close_source") in OFFICIAL_PRICE_SOURCES else None
    upper = num(s.get("upper_limit"))
    if close is None or upper is None:
        return {"status": "PENDING_CLOSE"}
    av = num(s.get("close_auction_volume"), allow_zero=True) \
        if s.get("auction_volume_source") in CERTIFIED_VOLUME_SOURCES else None
    if av is None:
        return {"status": "PENDING_AUCTION_DATA"}           # 미확인 ≠ 0
    if close >= upper:
        return {"status": "UNFILLED", "reason": "UPPER_LIMIT_CLOSE"}
    if av * close < MIN_FILL_VALUE:
        return {"status": "UNFILLED", "reason": "AUCTION_TOO_SMALL"}
    d1 = s.get("d1") if isinstance(s.get("d1"), dict) else {}
    base = num(d1.get("base")) if d1.get("base_source") in OFFICIAL_PRICE_SOURCES else None
    if base is None:
        return {"status": "PENDING_BASE", "filled": True}
    if base != close:
        return {"status": "EXCLUDED_BASE_ADJUSTED", "filled": True}
    lower, halted = num(d1.get("lower_limit")), d1.get("halted")
    if lower is None or not isinstance(halted, bool):
        return {"status": "PENDING_EXIT", "reason": "EXIT_STATE_UNKNOWN", "filled": True}
    opn = num(d1.get("open")) if d1.get("open_source") in OFFICIAL_PRICE_SOURCES else None
    oav = num(d1.get("open_auction_volume"), allow_zero=True) \
        if d1.get("open_auction_volume_source") in CERTIFIED_VOLUME_SOURCES else None
    exit_px, kind = None, None
    if not halted and opn is not None and opn > lower and oav is not None and oav * opn >= MIN_FILL_VALUE:
        exit_px, kind = opn, "OPEN"
    else:
        de = d1.get("delayed_exit") if isinstance(d1.get("delayed_exit"), dict) else None
        if de and de.get("source") in DELAYED_EXIT_SOURCES and parse_day(de.get("date")):
            px, val = num(de.get("price")), num(de.get("bar_value"))
            if px is not None and val is not None and val >= MIN_FILL_VALUE:
                exit_px, kind = px, "DELAYED"
        if exit_px is None:
            if halted:
                why, st = "HALTED", "HELD_UNRESOLVED"
            elif opn is None:
                why, st = "OPEN_DATA_MISSING", "PENDING_EXIT"
            elif opn <= lower:
                why, st = "LIMIT_DOWN_OPEN", "HELD_UNRESOLVED"
            elif oav is None:
                why, st = "OPEN_AUCTION_VOLUME_MISSING", "PENDING_EXIT"
            else:
                why, st = "OPEN_AUCTION_TOO_SMALL", "HELD_UNRESOLVED"
            return {"status": st, "reason": why, "filled": True}
    gross = _pct(_ret(close, exit_px))
    return {"status": "SETTLED", "filled": True, "exit_kind": kind,
            "entry": float(close), "exit": float(exit_px),
            "gross_pct": round(gross, 4), "net_pct": round(gross - COST_ASSUMPTION_PCT, 4),
            "grid": {f"c{c}_s{sl}": round(gross - c - sl, 4) for c in COST_GRID for sl in SLIP_GRID}}


def settle(day: dict, decision: dict) -> dict:
    if decision.get("status") != "PICKED":
        return {"status": decision.get("status")}
    r = settle_ticker((day.get("stocks") or {}).get(decision["pick"]))
    r["ticker"] = decision["pick"]
    return r


UNRESOLVED_HOLD = {"PENDING_BASE", "PENDING_EXIT", "HELD_UNRESOLVED"}
PRE_FILL_PENDING = {"PENDING_CLOSE", "PENDING_AUCTION_DATA"}


def sample_values(rows: list[dict], n: int) -> dict:
    """주 표본(PRIMARY·PICKED) 정산 행을 시간순으로 받아 처음 n건의 net 값을 낸다.
    미해결은 조용히 빼지 않는다: 기한 전이면 판정 보류, 기한이 지난 보유는 −100%로 넣는다."""
    vals, counts = [], {"excluded_base_adjusted": 0, "unfilled": 0, "forced_minus_100": 0,
                        "pre_fill_unresolved": 0}
    for r in rows:
        st, age = r.get("status"), r.get("sessions_since_entry", 0)
        if st == "SETTLED":
            vals.append(r["net_pct"])
        elif st in UNRESOLVED_HOLD:
            if age < UNRESOLVED_LIMIT_SESSIONS:
                return {"status": "DEFERRED_UNRESOLVED", "at": r.get("ticker"), "values": vals, **counts}
            vals.append(-100.0)
            counts["forced_minus_100"] += 1
        elif st in PRE_FILL_PENDING:
            if age < UNRESOLVED_LIMIT_SESSIONS:
                return {"status": "DEFERRED_UNRESOLVED", "at": r.get("ticker"), "values": vals, **counts}
            counts["pre_fill_unresolved"] += 1
        elif st == "EXCLUDED_BASE_ADJUSTED":
            counts["excluded_base_adjusted"] += 1
        elif st == "UNFILLED":
            counts["unfilled"] += 1
        if len(vals) == n:
            return {"status": "READY", "values": vals, **counts}
    return {"status": "NOT_YET", "values": vals, **counts}


# ---------- 외부 추천 ----------
def external(day: dict) -> list[dict]:
    """시간 등급은 주문 기회만 뜻한다. 가상 체결·청산은 C2와 같은 settle_ticker 기준이다."""
    d0, rows = parse_day(day.get("date")), []
    for e in day.get("external") or []:
        sent = parse_kst(e.get("sent_at"))
        if d0 is None or sent is None or sent.date() != d0:
            tier = "UNVERIFIED"
        elif sent <= _at(d0, INPUT_CUTOFF):
            tier = "SAME_TIME"
        elif sent < _at(d0, AUCTION_END):
            tier = "IN_AUCTION"
        else:
            tier = "AFTER_CLOSE"
        hyp = settle_ticker((day.get("stocks") or {}).get(e.get("ticker")))
        rows.append({"ticker": e.get("ticker"), "tier": tier,
                     "order_opportunity": tier in ("SAME_TIME", "IN_AUCTION"),
                     "hypothetical": hyp["status"], "net_pct": hyp.get("net_pct"),
                     "paired_comparable": tier in ("SAME_TIME", "IN_AUCTION") and hyp["status"] == "SETTLED"})
    return rows
```

## 부록 B. 합성·회귀 점검 (`c2_check.py`)

<!-- code:c2_check -->
```python
"""C2 합성 사례·회귀 점검. 실행: python -I c2_check.py (c2_rule.py와 같은 폴더). 네트워크·파일 쓰기 없음."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from c2_rule import external, sample_values, screen, settle  # noqa: E402

E = 100_000_000
OK_SRC = {"close_source": "KRX_OFFICIAL", "auction_volume_source": "KRX_OFFICIAL_AUCTION"}


def d1(open_, base, lower, halted=False, oav=200_000, delayed=None):
    return {"open": open_, "open_source": "KRX_OFFICIAL", "base": base, "base_source": "KRX_OFFICIAL",
            "lower_limit": lower, "halted": halted, "open_auction_volume": oav,
            "open_auction_volume_source": "KRX_OFFICIAL_AUCTION", "delayed_exit": delayed}


def stk(market, p1450, p1520, prev, upper, dv=80 * E, wv=8 * E, flags=(), close=None, auc=50_000,
        nxt=None, **kw):
    s = {"market": market, "kind": "COMMON", "flags": list(flags), "listed_sessions": 300,
         "prev_close": prev, "upper_limit": upper, "p1450": p1450, "p1520": p1520,
         "day_value": dv, "window_value": wv, "close": close, "close_auction_volume": auc,
         "d1": nxt, **OK_SRC}
    s.update(kw)
    return s


IDX = {"KOSPI": {"p1450": "2500.00", "p1520": "2505.00"}, "KOSDAQ": {"p1450": "800.00", "p1520": "798.00"}}


def day(ymd, rows, stocks, ext=None, rec="15:24:00", avail="15:20:30", **kw):
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}T"
    snap = {k: {"source": k.upper(), "available_at": iso + (avail if k != "daily" else "08:00:00") + "+09:00"}
            for k in ("disclosures", "bars", "index", "flags", "daily")}
    snap["decision_recorded_at"] = rec if "T" in rec else iso + rec + "+09:00"
    d = {"date": ymd, "session": "REGULAR", "market_halt": False, "index": IDX, "snapshot": snap,
         "disclosure_list": {"complete": True, "rows": rows}, "stocks": stocks, "external": ext or []}
    d.update(kw)
    return d


def disc(tk, title, at, src="KIND", did=None):
    return {"ticker": tk, "title": title, "published_at": at, "source": src,
            "id": did if did is not None else f"K-{tk}"}


T = "2099-01-05T"
S01 = day("20990105", [
    disc("A100", "단일판매ㆍ공급계약체결", T + "10:12:59+09:00"),
    disc("B200", "[기재정정]단일판매ㆍ공급계약체결", T + "11:00:59+09:00"),
    disc("C300", "자기주식취득결정", T + "15:20:59+09:00"),          # KIND 15:20 → :59 → 탈락
    disc("D400", "주식등의대량보유상황보고서(일반)", T + "09:30:59+09:00"),
    disc("E500", "유형자산취득결정", "2099-01-04T17:00:59+09:00"),
    disc("F600", "연결재무제표기준영업(잠정)실적(공정공시)", T + "13:00:59+09:00"),
    disc("G700", "무상증자결정", T + "08:10:59+09:00"),
    disc("H800", "특허권취득", T + "09:05:59+09:00", src="DART"),
    disc("I900", "투자판단관련주요경영사항", T + "14:00:59+09:00"),
    disc("J010", "타법인주식및출자증권취득결정", T + "15:19:59+09:00"),
    disc("K020", "신규시설투자등", T + "12:00:59+09:00"),
    disc("L030", "단일판매ㆍ공급계약체결", "2099-01-05 10:00"),         # 오프셋 없음
], {
    "A100": stk("KOSPI", 20000, 20300, 19500, 25350, close=20250, auc=30_000, nxt=d1(20600, 20250, 14175)),
    "F600": stk("KOSDAQ", 3000, 3100, 2900, 3770, dv=30 * E),
    "G700": stk("KOSDAQ", 10000, 10100, 9800, 12740, close=10150, auc=40_000, nxt=d1(10300, 10150, 7105)),
    "I900": stk("KOSPI", 5000, 5005, 4900, 6370),
    "J010": stk("KOSDAQ", 7000, 9100, 7000, 9100),
    "K020": stk("KOSPI", 4000, 4200, 3900, 5070, flags=("WARNING",)),
}, ext=[{"ticker": "A100", "sent_at": T + "15:10:00+09:00"},
        {"ticker": "G700", "sent_at": T + "15:25:00+09:00"},
        {"ticker": "G700", "sent_at": T + "15:40:00+09:00"},
        {"ticker": "G700", "sent_at": None}])

T2 = "2099-01-06T"
S02 = day("20990106", [disc(t, "단일판매ㆍ공급계약체결", T2 + "09:40:59+09:00") for t in ("B111", "B222", "B000")], {
    "B111": stk("KOSPI", 10000, 10120, 9900, 12870, wv=6 * E),
    "B222": stk("KOSPI", 20000, 20240, 19800, 25740, wv=8 * E),          # 정확히 같은 RS
    "B000": stk("KOSPI", 10000, 10120, 9900, 12870, wv=8 * E, close=12870, auc=90_000)})
S03 = day("20990107", [disc("Z1", "[기재정정]무상증자결정", "2099-01-07T09:00:59+09:00")], {})
S04 = day("20990108", [disc("Y1", "자기주식취득결정", "2099-01-08T10:00:59+09:00")],
          {"Y1": stk("KOSPI", 5000, 5000, 4900, 6370)})
S05 = day("20990109", [], {}, session="LATE_OPEN")
S06 = day("20990112", [disc("W1", "자기주식취득결정", "2099-01-12T10:00:59+09:00")],
          {"W1": stk("KOSPI", 5000, 5100, 4900, 6370)}, index={"KOSPI": {"p1450": None, "p1520": None}})
S07 = day("20990113", [disc("V1", "무상증자결정", "2099-01-13T08:30:59+09:00")],
          {"V1": stk("KOSDAQ", 10000, 10100, 9800, 12740, close=10100, auc=40_000, nxt=d1(5200, 5050, 3535))})
S08 = day("20990114", [disc("U1", "자기주식취득결정", "2099-01-14T10:00:59+09:00")],
          {"U1": stk("KOSPI", 10000, 10020, 9900, 12870)})

# ---- 회귀: 96c25d1 반례 3건 ----
R1 = day("20990115", [disc("Q1", "자기주식취득결정", "2099-01-15T10:00:59+09:00")],
         {"Q1": stk("KOSPI", 10000, 10200, 9900, 12870)}, rec="2099-01-16T08:29:00+09:00")
del R1["stocks"]["Q1"]["flags"]                                  # D+1 08:29 기록 + 시장조치 키 없음
R1a = copy.deepcopy(R1)
R1a["stocks"]["Q1"]["flags"] = []                                # 늦은 기록만
R1b = copy.deepcopy(R1)
R1b["snapshot"]["decision_recorded_at"] = "2099-01-15T15:24:00+09:00"   # 키 없음만
R2 = day("20990116", [disc("P1", "자기주식취득결정", "2099-01-16T10:00:59+09:00")],
         {"P1": stk("KOSPI", 10000, 10200, 9900, 12870, close=10200, nxt=d1(7140, 10200, 7140))})  # 하한가 시가
R2b = copy.deepcopy(R2)
R2b["stocks"]["P1"]["d1"]["delayed_exit"] = {"date": "20990119", "price": 7200, "bar_value": 3 * E,
                                             "source": "KRX_ONLY_BARS_PROVEN"}
R3 = day("20990120", [disc("O1", "자기주식취득결정", "2099-01-20T10:00:59+09:00")],
         {"O1": stk("KOSPI", 10000, 10200, 9900, 12870, close=10200, auc=None, nxt=d1(10300, 10200, 7140))})
R3b = copy.deepcopy(R3)
R3b["stocks"]["O1"].update(close_auction_volume=0)                     # 확인된 0 → 미체결
R3c = copy.deepcopy(R3)
R3c["stocks"]["O1"].update(close_auction_volume=99_999, auction_volume_source="DAILY_MINUS_BARS")
# ---- 추가 회귀 ----
R4 = day("20990121", [], {}, disclosure_list={"complete": False, "rows": []})      # 목록 미완 ≠ 무재료
R5 = day("20990122", [disc("N1", "자기주식취득결정", "2099-01-22T10:00:59+09:00"),
                      disc("N2", "자기주식취득결정", "2099-01-22T10:00:59+09:00")],
         {"N1": stk("KOSPI", 10000, 10200, 9900, 12870),
          "N2": stk("KOSPI", 10000, 10400, 9900, 12870, wv=None)})              # 하위가 아닐 수 있는 미판정
R6 = day("20990230", [], {})                                                   # 없는 날짜
R6b = day("20990123", [], {}, session="SOMETHING")
R7 = day("20990126", [disc("M1", "자기주식취득결정", "2099-01-26T10:00:59+09:00"),
                      disc("M2", "자기주식취득결정", "2099-01-26T10:00:59+09:00")],
         {"M1": stk("KOSPI", 20000, 20286, 19500, 25350, wv=6 * E),            # r=1.43%
          "M2": stk("KOSPI", 1_000_001, 1_014_301, 990_000, 1_287_000, wv=9 * E)})  # r=1.42999857%
R8 = day("20990127", [disc("L1", "자기주식취득결정", "2099-01-27T10:00:59+09:00")],
         {"L1": stk("KOSPI", 10000, 10200, 9900, 12870, close=10200, nxt=d1(None, 10200, 7140))})
R8b = copy.deepcopy(R8)
R8b["stocks"]["L1"]["d1"].update(open=10300, base_source=None)
R9 = day("20990128", [], {}, market_halt=True)
R9b = day("20990128", [], {}, market_halt=None)
R10 = day("20990129", [disc("K1", "자기주식취득결정", "2099-01-29T10:00:59+09:00")],
          {"K1": stk("KOSPI", 10000, 10200, 9900, 12870, close=10200, nxt=d1(10300, 10200, 7140, halted=True))})
R11 = day("20990130", [disc("J1", "자기주식취득결정", "2099-01-30T10:00:59+09:00")],
          {"J1": stk("KOSPI", 10000, 10200, 9900, 12870, close=10200, nxt=d1(10300, 10200, 7140)),
           "X9": stk("KOSDAQ", 9500, 9900, 9500, 12350, close=10000, auc=None, nxt=d1(10500, 10000, 7000))},
          ext=[{"ticker": "J1", "sent_at": "2099-01-30T15:00:00+09:00"},
               {"ticker": "X9", "sent_at": "2099-01-30T15:00:00+09:00"}], avail="15:19:59")
R12 = day("20990202", [], {})                                                  # 완전한 빈 목록

EXPECT = {  # name: (screen status, pick, sample, settle status)
    "S01": (S01, "PICKED", "A100", "PRIMARY", "SETTLED"),
    "S02": (S02, "PICKED", "B000", "PRIMARY", "UNFILLED"),
    "S03": (S03, "NO_TRADE_NO_CATALYST", None, "PRIMARY", "NO_TRADE_NO_CATALYST"),
    "S04": (S04, "NO_TRADE_NO_PASS", None, "PRIMARY", "NO_TRADE_NO_PASS"),
    "S05": (S05, "SKIP_SPECIAL_SESSION", None, "EXCLUDED", "SKIP_SPECIAL_SESSION"),
    "S06": (S06, "INDETERMINATE", None, "EXCLUDED", "INDETERMINATE"),
    "S07": (S07, "PICKED", "V1", "PRIMARY", "EXCLUDED_BASE_ADJUSTED"),
    "S08": (S08, "NO_TRADE_NO_PASS", None, "PRIMARY", "NO_TRADE_NO_PASS"),
    "R1": (R1, "INDETERMINATE", None, "EXCLUDED", "INDETERMINATE"),
    "R1a": (R1a, "PICKED", "Q1", "INPUT_CHECK_ONLY", "PENDING_CLOSE"),
    "R1b": (R1b, "INDETERMINATE", None, "EXCLUDED", "INDETERMINATE"),
    "R2": (R2, "PICKED", "P1", "PRIMARY", "HELD_UNRESOLVED"),
    "R2b": (R2b, "PICKED", "P1", "PRIMARY", "SETTLED"),
    "R3": (R3, "PICKED", "O1", "PRIMARY", "PENDING_AUCTION_DATA"),
    "R3b": (R3b, "PICKED", "O1", "PRIMARY", "UNFILLED"),
    "R3c": (R3c, "PICKED", "O1", "PRIMARY", "PENDING_AUCTION_DATA"),
    "R4": (R4, "INPUT_MISSING", None, "EXCLUDED", "INPUT_MISSING"),
    "R5": (R5, "INDETERMINATE", None, "EXCLUDED", "INDETERMINATE"),
    "R6": (R6, "INVALID_INPUT", None, "EXCLUDED", "INVALID_INPUT"),
    "R6b": (R6b, "INVALID_INPUT", None, "EXCLUDED", "INVALID_INPUT"),
    "R7": (R7, "PICKED", "M1", "PRIMARY", "PENDING_CLOSE"),
    "R8": (R8, "PICKED", "L1", "PRIMARY", "PENDING_EXIT"),
    "R8b": (R8b, "PICKED", "L1", "PRIMARY", "PENDING_BASE"),
    "R9": (R9, "SKIP_MARKET_HALT", None, "EXCLUDED", "SKIP_MARKET_HALT"),
    "R9b": (R9b, "INPUT_MISSING", None, "EXCLUDED", "INPUT_MISSING"),
    "R10": (R10, "PICKED", "K1", "PRIMARY", "HELD_UNRESOLVED"),
    "R11": (R11, "PICKED", "J1", "INPUT_CHECK_ONLY", "SETTLED"),
    "R12": (R12, "NO_TRADE_NO_CATALYST", None, "PRIMARY", "NO_TRADE_NO_CATALYST"),
}

bad = 0
for name, (dy, st, pk, smp, sst) in EXPECT.items():
    dec = screen(dy)
    se = settle(dy, dec)
    got = (dec["status"], dec["pick"], dec["sample"], se["status"])
    ok = got == (st, pk, smp, sst)
    bad += not ok
    extra = se.get("reason") or se.get("exit_kind") or ""
    print(f"[{'OK ' if ok else 'BAD'}] {name} {dy['date']} {got[0]} pick={got[1]} sample={got[2]} "
          f"settle={got[3]} {extra} {','.join(dec['sample_problems'])}".rstrip())

# 세부 단언
d = screen(S01)
assert d["ranked"] == ["A100", "G700"]
assert {k: v["reason"] for k, v in d["candidates"].items() if v["state"] == "FAIL"} == {
    "F600": "LIQUIDITY", "I900": "RS_NOT_POSITIVE", "J010": "UPPER_LIMIT_1520", "K020": "FLAGGED"}
assert d["disclosures"]["K-C300"] == "AFTER_CUTOFF" and d["disclosures"]["K-J010"] == "OK"
assert d["disclosures"]["K-L030"] == "BAD_TIMESTAMP" and d["disclosures"]["K-H800"] == "SOURCE_UNVERIFIABLE"
assert d["candidates"]["A100"]["rs_pp_display"] == 1.3 and d["candidates"]["G700"]["rs_pp_display"] == 1.25
s = settle(S01, d)
assert (s["gross_pct"], s["net_pct"]) == (1.7284, 1.4784), s
assert screen(S02)["ranked"] == ["B000", "B222", "B111"]                       # 정확한 동률 → 창 거래대금 → 코드
assert screen(R1)["sample_problems"] == ["DECISION_RECORD_OUTSIDE_WINDOW"]
assert screen(R1b)["candidates"]["Q1"]["reason"] == "FLAGS_NOT_COLLECTED"
assert settle(R2, screen(R2))["reason"] == "LIMIT_DOWN_OPEN"
assert settle(R2b, screen(R2b))["exit_kind"] == "DELAYED"
assert settle(R3b, screen(R3b))["reason"] == "AUCTION_TOO_SMALL"
r7 = screen(R7)
assert r7["candidates"]["M1"]["rs_pp_display"] == r7["candidates"]["M2"]["rs_pp_display"] == 1.23
assert r7["ranked"] == ["M1", "M2"]   # 4자리 반올림으로는 동률 → 창 거래대금이면 M2가 1위였을 것
assert screen(R11)["sample_problems"] == [f"{k}:OUTSIDE_WINDOW" for k in ("disclosures", "bars", "index", "flags")]
ex = external(S01)
assert [(r["tier"], r["paired_comparable"]) for r in ex] == [
    ("SAME_TIME", True), ("IN_AUCTION", True), ("AFTER_CLOSE", False), ("UNVERIFIED", False)]
ex11 = external(R11)
assert [(r["hypothetical"], r["paired_comparable"]) for r in ex11] == [("SETTLED", True), ("PENDING_AUCTION_DATA", False)]
# 판정 표본: 미해결 보유는 빼지 않는다
rows = [{"status": "SETTLED", "net_pct": 1.0}, {"status": "HELD_UNRESOLVED", "sessions_since_entry": 3},
        {"status": "SETTLED", "net_pct": 2.0}]
assert sample_values(rows, 3)["status"] == "DEFERRED_UNRESOLVED"
rows[1]["sessions_since_entry"] = 20
sv = sample_values(rows, 3)
assert sv["status"] == "READY" and sv["values"] == [1.0, -100.0, 2.0] and sv["forced_minus_100"] == 1
print("external S01:", [(r["tier"], r["hypothetical"], r["net_pct"], r["paired_comparable"]) for r in ex])
print("regression:", "ALL PASS" if bad == 0 else f"{bad} FAIL", f"({len(EXPECT)} cases)")
```

## 부록 C. `96c25d1` 반례 재현 입력

**방법**
1. `git show 96c25d1:<이 문서 경로>`에서 부록 A 코드를 `fromdoc/c2_rule.py`로 꺼낸다.
2. 그 옆 폴더에서 아래 코드를 실행한다.

출력은 §9에 있다.

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fromdoc"))
from c2_rule import screen, settle
IDX = {"KOSPI": {"p1450": 2500.0, "p1520": 2505.0, "close": 2504.0, "d1_open": 2510.0}}
def s(**kw):
    b = {"market": "KOSPI", "kind": "COMMON", "listed_sessions": 300, "prev_close": 9900, "upper_limit": 12870,
         "p1450": 10000, "p1520": 10200, "day_value": 8e9, "window_value": 8e8, "close": 10200,
         "close_auction_volume": 50_000, "d1_open": 10300}
    b.update(kw); return b
def day(st, **kw):
    d = {"date": "20990115", "session": "REGULAR", "index": IDX,
         "disclosures": [{"ticker": "Q1", "title": "자기주식취득결정", "published_at": "2099-01-15T10:00:00",
                          "source": "KIND", "id": "K1"}], "stocks": {"Q1": st}}
    d.update(kw); return d
a = day(s(), decision_recorded_at="2099-01-16T08:29:00")   # flags 키 없음, D+1 08:29 기록 (구 코드는 둘 다 보지 않음)
b = day(s(d1_open=7140, d1_flag="D1_LIMIT_DOWN_OPEN"))
c = day(s(close_auction_volume=None))
for n, d in (("08:29+flags missing", a), ("limit-down open", b), ("auction volume None", c)):
    dec = screen(d); print(n, "->", dec["status"], dec["pick"], settle(d, dec)["status"])
```

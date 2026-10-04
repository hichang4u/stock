---
name: daily-log
description: 그날의 운영 로그를 분석한다 — 진입·청산 과정, 빠른 경로 판정, WS 상태, 알려진 결함 재발 여부
---

읽기 전용 분석이다. 운영 프로세스를 건드리지 않는다. 날짜를 안 주면 오늘로 본다.

## 1. 파이프라인 전체 흐름

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
rows=[json.loads(l) for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8') if l.strip()]
print('총', len(rows), '줄')
KEY=('PREOPEN','OPEN','F1_','F2_','F3_','F4_','F5_','ENTRY','ORDER','TRADE','POSITION','MARKET','SKIP','CAPPED','BUDGET','CLOSE','EXIT','HARD_STOP','TRAIL')
for r in rows:
    e=r.get('event','')
    if any(k in e for k in KEY) or r.get('level') in ('error','crit'):
        x={k:v for k,v in r.items() if k not in ('ts','level','event','logger','event_label')}
        x={k:v for k,v in x.items() if v not in (None,'',[],{})}
        print(r['ts'][11:23], r.get('level','').upper()[:4], e, json.dumps(x,ensure_ascii=False)[:190])
"
```

`F4_HEARTBEAT`가 30초마다 찍혀 결과를 덮으므로, 길면 `and 'HEARTBEAT' not in e`를 더한다.

## 2. 거래 결과

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import sqlite3
db=sqlite3.connect('file:data/db/trading.db?mode=ro',uri=True); db.row_factory=sqlite3.Row
for r in db.execute(\"select * from trades where date='<YYYYMMDD>'\"):
    print({k:r[k] for k in r.keys() if r[k] not in (None,'')})
for r in db.execute(\"select order_phase,ticker,order_price,order_qty,fill_price,fill_qty,status,ordered_at,filled_at from orders where ordered_at like '<YYYY-MM-DD>%'\"):
    print(dict(r))
"
```

`ordered_at`→`filled_at` 간격을 본다. 진입은 보통 1초 내, 길면 `F3_LIMIT_FILL_TIMEOUT_SEC`
경로를 확인한다.

## 3. 빠른 경로가 이겼는지

`ENTRY_PIPELINE_TIMING.selection_source`가 `FAST_MULTI`면 정상,
`LEGACY_SINGLE_QUOTE`면 폴백이다(80~120초 지연). 폴백이면 `PAPER_FAST_PROBE_OPEN_DONE`의
`filter_pass_count`와 거부 사유별 카운트를 본다.

`filter_pass_count=0`이면 프로브 유니버스에 통과 종목이 없었던 것이다. 그때는 실제
매수 종목이 유니버스에 있었는지 대조한다 — 없었다면
`docs/FAST_PATH_UNIVERSE_FOLLOWUP_20260929.md`의 구조적 맹점이다.

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from pathlib import Path
from scripts.probe_universe import load_probe_universe
u=load_probe_universe(Path('data/paper_fast_probe/<YYYYMMDD>.jsonl'))
print(len(u),'행 |', '<티커>' in [str(c.get('ticker')) for c in u])
"
```

## 4. WS 상태

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l); e=d.get('event','')
    if any(k in e for k in ('WS_','STALE','DISCONNECT','RECONNECT','KEEPALIVE')):
        print(d['ts'][11:23], d.get('level','').upper()[:4], e, d.get('error',''))
"
```

`keepalive ping timeout` 단절은 큐 포화 계열이라 무겁게 본다. 다른 사유의 단절은
서버·네트워크 쪽일 수 있다.

### 4.1 체결 프레임 점검 — 매일 돌린다

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/ws_frame_check.py --root D:/Private/stock-prod --date <YYYYMMDD>
```

종료 코드 1이면 보고 맨 앞에 쓴다. 이상 종류는 네 가지다.

- `FIELD_COUNT_CHANGED`: KIS가 `H0STCNT0` 필드 수를 또 바꿨다(기대값 47).
- `PARSER_DROPS`: 파서가 다건 프레임의 체결을 버리고 있다(`coverage_pct` < 100).
- `UNALIGNED`: 레코드로 정렬되지 않는 프레임이 있다.
- `HEADER_MISMATCH`: 프레임 헤더의 건수와 파서가 낸 레코드 수가 다르다(tick-schema-3 이후).

2026-09-14에 46 → 47로 바뀐 뒤 11거래일 동안 아무도 몰랐다
(`docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md`).

**무엇을 재는지는 캡처 형식에 따라 다르다.**

- **tick-schema-3**(파서 수정 승격 이후): 행 하나가 운영 파서가 실제로 낸 체결 하나다.
  `coverage_pct`가 그날 운영의 실제 값이다. **`PARSER_DROPS`가 나오면 결함이다.**
- **그 전 형식**(행 하나 = 프레임 하나): 이 트리의 **현재** 파서로 다시 쪼갠 결과다. 파서가
  고쳐진 뒤에는 옛 날짜도 100%로 나온다. 그날 운영 F4가 실제로 본 비율은 `프레임 ÷ 체결`이다
  (0914 ~ 승격 전에는 다건 프레임의 첫 체결만 봤다).

파서 수정 이후에는 운영 로그에도 `WS_FRAME_UNSPLIT`·`WS_FRAME_HEADER_MISMATCH`(WARN)가
남는다. 같은 원인이면 처음과 1,000번째마다만 찍히고 `occurrences`에 누계가 있다. 4절의 WS
검색에 함께 걸린다.

## 5. 청산이 있었으면 — F4 틱 공백 재측정

`docs/F4_CLOSE_TICK_FREEZE_FOLLOWUP_20260917.md` §5의 통과 조건을 다시 잰다.
체결 확인이 20초를 넘긴 날이 가장 강한 검증이다.

- 캡처의 ws 행 하나는 **체결 하나가 아니라 프레임 하나**다. 아래 공백은 프레임 도착 간격이라
  파서 결함과 무관하다. 그러나 `price`·`qty`는 프레임의 첫 체결뿐이다. 가격이나 체결량을
  볼 때는 `raw`를 47필드씩 쪼갠다(2 = 가격, 12 = 체결량, 13 = 누계거래량).
- 체결이 얇은 종목은 공백이 잦다. "체결이 없었다"와 "ws가 놓쳤다"를 가르려면 전 레코드의
  누계거래량이 이어지는지 본다(`raw[13]` 차 = 다음 레코드 `raw[12]`).
- 시간별 틱 파일은 **장 마감(15:20)에 한꺼번에 닫힌다.** 장중에는 그 시각 이전 파일도 잘려
  있으니, 청산 구간까지 읽히지 않으면 15:20 이후에 다시 잰다.

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import gzip, json
from datetime import datetime
rows=[]
try:
    with gzip.open('data/strategy_ticks/<YYYYMMDD>/<티커>.09.jsonl.gz','rt',encoding='utf-8') as fh:
        for line in fh:
            line=line.strip()
            if line:
                try: rows.append(json.loads(line))
                except ValueError: pass
except EOFError: pass   # 장중이면 기록 중이라 잘려 있다
rows.sort(key=lambda r: r.get('received_at',''))
ws=[r for r in rows if r.get('source')=='ws']
o=datetime.fromisoformat('<CLOSE_SELL ordered_at>'); f=datetime.fromisoformat('<filled_at>')
prev=None
for r in ws:
    t=datetime.fromisoformat(r['received_at'])
    if prev and (t-prev).total_seconds()>3:
        print('gap', prev.time(), '->', t.time(), round((t-prev).total_seconds(),1),
              '<<< 창 안' if (prev<=f and t>=o) else '')
    prev=t
print('창 안 ws 틱', sum(1 for r in ws if o<=datetime.fromisoformat(r['received_at'])<=f), '건')
"
```

통과 조건: 창 안 3초 초과 공백 0건, 같은 구간 `WS_STALE` 없음, 청산 후 1분 내
keepalive 단절 없음.

체결이 얇은 종목이라 공백이나 `WS_STALE`이 걸리면 F4 티켓 §7의 보조 기준을 적용한다.
공백 앞뒤 누계거래량 차이가 직후 체결량과 같으면 "체결 없음"이다. 그렇게만 만족하면
**약한 통과**(큐 포화 경로는 시험하지 못함)로 적는다. 설명되지 않는 공백이 있으면 실패다.
누계거래량에는 KIS 쪽 바탕 잡음이 있다. 같은 프레임 안에서도 불연속이 난다(§7.5). 그러므로
공백 경계의 불연속은 같은 날 "같은 프레임 안" 불연속 빈도와 비교해서만 판단한다.

## 5.5 누적 성과 (CAGR·MDD)

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/performance_report.py --track A --root D:/Private/stock-prod
```

보고 맨 끝에 전체 기간과 현재 구간("파서 수정 후")의 CAGR·MDD를 한 줄씩 붙인다. 대표값은
전략 자본 기준 보수 손익이다. 1년 미만 CAGR에는 "(참고)"가 붙는다 — 총수익률을 함께 적는다.
`※ 복수 변경`이 붙은 구간은 성과 변화를 한 조건에 귀속하지 않는다. 결과 폴더
`data/performance/track_a/<실행ID>/`에 네 파일이 남는다(설계:
`docs/superpowers/specs/2026-10-04-performance-report-design.md`).

## 6. 보고할 때

- 손익은 금액과 %를 함께 쓴다(`trades.pnl_pct`, `pnl_amount`).
- 주문금액 상한이 걸렸으면 `ENTRY_BUDGET_CAPPED`의 `uncapped_amount`와 함께 보인다.
- **추정과 실측을 섞지 않는다.** "늦어서 손해였다" 같은 말은 분봉으로 확인한 뒤에만
  한다(`FHKST03010200`, `FID_INPUT_HOUR_1`로 30봉씩 되짚는다).
- 결함을 찾으면 `docs/<주제>_FOLLOWUP_<날짜>.md` 형식으로 티켓을 남긴다 —
  현상 / 증거 / 원인 / 영향 / 선택지 순서이고, **선택지는 고르지 않고 나열만 한다.**

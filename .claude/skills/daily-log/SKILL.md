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

### 2.1 보유 중 재시작 — `HOLDING_RESUMED`가 있으면 돌린다

재시작 복구는 상태 파일의 고가·스텝을 그대로 이어받고, 꺼져 있던 동안의 고가를 다시 보지 않는다
(`docs/RESTART_HOLDING_HIGH_GAP_FOLLOWUP_20261109.md`, 2026-11-10 결정 나). 그래서 그날마다 공백 구간 분봉과 대조한다.

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json, math, sqlite3
D='<YYYYMMDD>'; STEP=0.025; TRAIL=0.020; HARD=0.02   # f4_tracking.py 상수
rows=[json.loads(l) for l in open(f'data/logs/{D}.jsonl',encoding='utf-8') if l.strip()]
db=sqlite3.connect('file:data/db/trading.db?mode=ro',uri=True)
def step(p,e): return max(math.floor((p/e-1)/STEP)*STEP,0.0)
def stop(e,s): return e*(1+s-TRAIL) if s>=STEP else e*(1-HARD)
for i,r in enumerate(rows):
    if r.get('recovered_status')!='HOLDING_RESUMED': continue
    t1=r['ts']
    live=[x for x in rows[:i] if x.get('event') in ('F4_HEARTBEAT','F4_STATE_PERSISTED')]
    if not live: print(t1[11:23],'재시작 전 F4 기록 없음'); continue
    t0=live[-1]['ts']   # 꺼지기 전 마지막 생존 신호(실제 정지는 그 뒤 30초 안)
    saved=[x for x in live if x['event']=='F4_STATE_PERSISTED' and x.get('state_saved')]
    src=saved[-1] if saved else live[-1]   # 재시작이 복원하는 것은 마지막으로 저장된 상태다
    tk=src['ticker']; kept=src['high_price']
    if not saved: print('  (저장 기록이 없어 하트비트 고가를 썼다. 이어받은 값과 다를 수 있다)')
    e=db.execute('select entry_price from trades where date=? and ticker=? order by id desc',(D,tk)).fetchone()[0]
    try: allbars=json.load(open(f'data/bars/{D}_{tk}.json',encoding='utf-8'))
    except OSError: print(tk,t1[11:23],'분봉 파일 없음'); continue
    # 공백 안에 통째로 든 분만 쓴다. 경계 분봉에는 봇이 살아 있던 동안의 틱이 섞인다.
    bars=[b for b in allbars if t0[11:13]+t0[14:16] < b['time'][:4] < t1[11:13]+t1[14:16]]
    if not bars: print(tk,t0[11:23],'->',t1[11:23],'공백 안에 온전한 분봉 없음'); continue
    hi=max(b['high'] for b in bars); lo=min(b['low'] for b in bars)
    print(tk,'공백',t0[11:23],'->',t1[11:23],'| 분봉',bars[0]['time'][:4],'~',bars[-1]['time'][:4],'| 진입',e)
    print('  이어받은 고가',kept,'스텝',step(kept,e),'스탑',round(stop(e,step(kept,e))))
    print('  공백 고가',hi,'스텝',step(max(hi,kept),e),'스탑',round(stop(e,step(max(hi,kept),e))),'| 공백 저가',lo,
          '<<< 이어받은 스탑 아래' if lo<=stop(e,step(kept,e)) else '')
"
```

- 공백 시작은 재시작 전 마지막 F4 기록(`F4_HEARTBEAT` 또는 `F4_STATE_PERSISTED`)이다. 하트비트가 30초 간격이라 실제로 멈춘
  시각은 그 뒤 30초 안이다.
- 이어받은 고가는 마지막 `F4_STATE_PERSISTED`(`state_saved=true`)의 값이다. 재시작은 상태 파일을 복원하므로, 하트비트 뒤에
  고가가 갱신돼 저장됐으면 하트비트 값과 다르다. 11/09에는 둘이 같았다(11,610원).
- 공백 안에 통째로 든 분봉만 쓴다. 경계 분봉에는 봇이 살아 있던 동안의 틱이 섞인다.
- **공백 고가로 스텝이 올라갔어야 했으면 맨 앞에 쓴다.** 이어받은 스탑, 올라갔어야 할 스탑, 실제 청산을 함께 적는다.
  금액 차이는 분봉 안 가격 순서를 모르므로 추정이다.
- **공백 저가가 이어받은 스탑 아래였으면(`<<<`) 맨 앞에 쓴다.** 꺼져 있는 동안은 스탑이 없었다는 뜻이다.
- 둘 중 하나라도 나오면 티켓 7절 표에 한 줄 더하고, 재검토 조건(7절)에 닿았는지 본다.
- 재시작 전 F4 기록이 없는 옛 로그(2026-08 이전)나 분봉 파일이 없는 날은 대조하지 못한다고 적는다.

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

### 3.1 진입 타이밍 — 진입 시도가 있었으면 돌린다

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/entry_timing_report.py --root D:/Private/stock-prod --date <YYYYMMDD>
```

첫 체결(시가)과 주문의 앞뒤, 주문별 갈래(창 안 체결 / 늦은 확인 / 취소), 주문 API 통신 시간, 시가 대비
진입가를 한 줄로 낸다. 그 행을 `docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md` 8절에 옮긴다. 시가 대비 금액은
추정이다. 누적은 `--since 20261013`으로 본다(맨 아래 요약 줄). 레거시 경로 날은 캡처가 종목 고정 뒤부터라
첫 체결을 비워 둔다.

**주문 거부(`msg_cd=40070000` "매매불가 종목" 등)가 있으면 맨 앞에 쓴다.** 보고에는 "주문 거부" 갈래로 나오고, 요약 줄에서
"주문 거부로 무산"으로 따로 센다. 체결 대기 실패가 아니다. 그날 15:40 이후 그 종목의 상태를 조회해
`docs/ORDER_REJECTED_PAPER_UNTRADABLE_FOLLOWUP_20261106.md` 7절 표에 한 줄 더한다. 다음날이면 당시 상태를 잃는다(7/8이 그랬다).
결정은 같은 티켓 6절이다. 장중에는 돌리지 않는다.

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import asyncio,sys
sys.path.insert(0,'.')
from dotenv import load_dotenv; load_dotenv('.env')
from src.api import auth,kis_rest
async def m():
    assert await auth.load_or_refresh()
    r=await kis_rest.get('/uapi/domestic-stock/v1/quotations/inquire-price',tr_id='FHKST01010100',params={'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':'<티커>'})
    o=r.get('output') or {}
    print(r.get('rt_cd'),{k:o.get(k) for k in ('iscd_stat_cls_code','mang_issu_cls_code','marg_rate','temp_stop_yn','mrkt_warn_cls_code')})
asyncio.run(m())
" 2>&1 | grep -v '^{'
```

`release/20261027-1`부터 09:00:00 ~ 09:00:45에 낸 주문은 그 종목의 첫 체결까지 기다렸다가 2초 확인 창을 연다
(진입 티켓 8.12절). 빠른 경로 날마다 1절 출력에서 다음을 본다.
- `ENTRY_FIRST_TRADE_WAIT` → `ENTRY_FIRST_TRADE_SEEN`: `waited_ms`, `via`(`FIRST_TRADE`/`FILL_NOTICE`),
  `first_trade_at`. 통보 체결 확정 릴리스(통보 티켓 8.9절) 전에는 모의 통보가 장부에 오르지 않아 `via`가 늘
  `FIRST_TRADE`였다. 그 뒤로는 통보가 먼저 오면 `FILL_NOTICE`로 찍힌다.
- **`ENTRY_FIRST_TRADE_WAIT_TIMEOUT`(WARN)이 있으면 맨 앞에 쓴다.** 첫 체결 신호를 09:00:45까지 못 받았다는
  뜻이다. 그 무렵 WS 단절과 틱 캡처의 첫 ws 행 `source_ts`를 대조한다.
- 09시 정각 단절(09:00:02 ~ 04 무렵)과 첫 체결이 겹친 날은 `first_trade_at`과 `ENTRY_FIRST_TRADE_SEEN` 로그
  시각의 차이를 적는다(재접속 뒤 첫 틱으로 늦게 잡힌다).
- `entry_timing_report.py`에서는 "1차 첫 체결 전 · 창 안 체결" 갈래로 나온다. "1차 첫 체결 전 · 취소"가 다시
  나오면 맨 앞에 쓴다.

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

**정각이 아닌 단절을 매일 센다**(`docs/WS_REST_OUTAGE_FOLLOWUP_20261021.md` 10절, 2026-11-03 결정 사).
매시 정각(`hh:00:00 ~ 04`) 밖의 `WS_DISCONNECTED`와 `keepalive ping timeout` 건수를 보고에 한 줄로 적는다.
0건이어도 적는다. 그날 보유 중(진입 ~ 청산)이었던 단절은 따로 표시한다.

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
off=[]; ka=0
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    try: d=json.loads(l)
    except ValueError: continue
    if d.get('event')!='WS_DISCONNECTED': continue
    t=d['ts'][11:19]; err=str(d.get('error',''))
    ka+='keepalive' in err
    if not (t[3:5]=='00' and t[6:8]<'05'): off.append(t+(' KA' if 'keepalive' in err else ''))
print('정각 밖 단절', len(off), off, '| keepalive', ka)
"
```

- 정각 밖 단절이 **보유 중**에 났으면 맨 앞에 쓴다. 그 무수신 구간 가격과 스탑선 거리를 함께 적는다. 스탑선
  근처였으면 티켓 10절의 재검토 조건(선택지 라)에 닿은 것이다.
- 보유가 끝난 뒤에 났으면 시각·사유·놓친 체결량만 티켓 아래에 날짜별로 적는다.
- 10/14 ~ 11/02 기준값: 14거래일 중 5일(10/14, 21, 22, 23, 29), 보유 중은 2일(10/21, 10/22의 09:07).

`WS_SUBSCRIBE_RESPONSE`(2026-10-07~)는 시세 구독 응답이다. 위 검색에 걸리지만 `error`만 찍히므로
`rt_cd`·`msg_cd`·`msg1`을 따로 본다. 09:00:01 직후 단절 앞에 `msg_cd=OPSP8996`(WARN)이 있으면 같은 앱키의
다른 세션 때문이다. `OPSP0000`(성공) 뒤에 끊기면 그 원인은 아니다
(`docs/OPENING_WINDOW_FOLLOWUP_20261006.md` 1.4절).

15:20 `TICK_CAPTURE_FINALIZED`(2026-10-26 승격분~)의 ws 무수신 필드를 본다. `max_ws_outage_sec`는 단절
기록부터만 재므로 서버가 끊기 전 이미 조용했던 시간을 놓친다(`docs/WS_REST_OUTAGE_FOLLOWUP_20261021.md` 8.4절).
- `max_ws_silence_sec`·`_from`·`_to`: ws 체결 사이 최대 간격. `_missed_qty`가 0이면 그 사이 체결이 없었다(VI 등).
- `ws_silences_over_limit`·`ws_silence_missed_qty`: 10초 넘는 공백 건수와 그 사이 놓친 체결량 합계.
  `ws_silence_missed_qty` > 0이면 `data_complete=1`이어도 실제 체결이 빠진 날이다. 아래 명령으로 어디서 빠졌는지
  가른다.
  - **매시 정각 단절**(`hh:00:00 ~ 03`): 이미 기록된 사안이다(`docs/OPENING_WINDOW_FOLLOWUP_20261006.md`
    1.6절). 맨 앞에 쓰지 않고 시각별 양만 적는다. 이 지표는 정각 손실을 공백이 10초를 넘을 때만 잡는다.
    2026-10-26에는 정각 네 번에 4,767주가 빠졌는데, 지표에는 13:00의 1,396주만 잡혔다.
  - **그 밖의 시각**에서 빠졌으면 **맨 앞에 쓴다.**
  - 09시대 공백 0초 안팎의 작은 불연속은 KIS 누계거래량 잡음이다(`docs/F4_CLOSE_TICK_FREEZE_FOLLOWUP_20260917.md`
    7.5절). 같은 날 그런 불연속이 얼마나 잦은지와 비교해서만 판단한다.
- `source_ts_reversals`는 실제 체결 시각 역전이 아니다. ws 행은 거래소 시각(초 단위), REST 행은 수신 시각(ms
  단위)으로 비교하므로 REST 백업 틱이 끼면 그만큼 는다. 2026-10-28에는 1,325건이었지만 ws 행끼리는 0건이었다.
  REST 백업이 많은 날(WS_STALE이 잦은 보유일) 크게 나오면 정상이다.

틱 파일은 `src/utils/jsonl_gz.py`의 `read_jsonl_gz`로 읽는다(개발 트리에서 돌리고 운영 파일을 경로로 준다).
장중 기록 중인 파일은 읽힌 데까지, 전원 차단으로 끊긴 멤버가 있으면 그 뒤에 이어 쓴 멤버까지 읽는다. 표준 `gzip`은
끊긴 멤버에서 `zlib.error`로 멈추고 뒤 멤버를 잃는다(`docs/TICK_GZIP_CORRUPTION_FOLLOWUP_20261109.md`). `damaged_at`이
비어 있지 않으면 그 파일에 끊긴 멤버가 있었다는 뜻이니 보고에 적는다.

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
import glob
from datetime import datetime
from src.utils.jsonl_gz import read_jsonl_gz
rows=[]
for p in sorted(glob.glob('D:/Private/stock-prod/data/strategy_ticks/<YYYYMMDD>/<티커>.*.jsonl.gz')):
    got=read_jsonl_gz(p); rows+=got.rows
    if got.damaged_at: print('끊긴 멤버', p[-22:], got.damaged_at)
prev=None
for r in (r for r in rows if r.get('source')=='ws'):
    f=r['raw'] if isinstance(r['raw'],list) else str(r['raw']).split('^')
    t=datetime.fromisoformat(r['received_at']); cum=int(f[13])
    if prev and cum-prev[1]-int(f[12])>0:
        print(prev[0].time(),'->',t.time(),round((t-prev[0]).total_seconds(),2),'초 | 빠진',cum-prev[1]-int(f[12]),'주')
    prev=(t,cum)
"
```

### 4.0 KIS 호출 한도 — 매일 센다

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
from collections import Counter
c=Counter(); paths=Counter()
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l); e=d.get('event','')
    if e in ('RATE_LIMIT_HIT','TRANSIENT_ERROR_RETRY','TRACK_B_CORRECTION_FAILED','TRACK_B_CORRECTION_BLOCKED','WS_STALE','LATENCY_HIGH'):
        c[e]+=1
        if e=='RATE_LIMIT_HIT': paths[str(d.get('path','')).rsplit('/',1)[-1]]+=1
print(dict(c)); print('한도 초과 경로', dict(paths))
"
```

- 평소(2026-10-22 ~ 27) `RATE_LIMIT_HIT`은 0 ~ 2건이다. 2026-10-28에는 11건이었다. 거래가 얇은 종목을 보유한 날
  WS_STALE이 잦아 REST 시세 백업(`inquire-price`, 1초 간격)과 매분 분봉 보정(`inquire-time-itemchartprice`)이
  겹친 것으로 본다(`docs/RATE_LIMIT_FOLLOWUP_20261028.md`).
- **한도 초과 경로에 주문·체결 조회(`order-cash`, `order-rvsecncl`, `inquire-daily-ccld`, `inquire-balance`)가
  있으면 맨 앞에 쓴다.** 청산 지연으로 이어질 수 있다.
- `TRACK_B_CORRECTION_FAILED`는 다음 분 보정이 최근 봉을 다시 고치므로 그 자체로는 가볍다. 마지막
  `TRACK_B_BARS_CORRECTED`가 15:20 이후에 있는지만 본다.
- `release/20261028-2`부터 A 보유 중 REST 백업이 돌면 분봉 정정을 건너뛴다
  (`TRACK_B_CORRECTION_BLOCKED` `reason=A_HOLDING_REST_BACKUP`, 티켓 6절). `release/20261028-3`부터 진입·청산
  주문 중에도 건너뛴다(`reason=A_ORDER_IN_FLIGHT`). 그 뒤에도 분봉 조회가 한도에 걸리면
  적는다. 건너뛴 횟수는 위 명령의 `TRACK_B_CORRECTION_BLOCKED` 수로 본다.

### 4.1 체결 프레임 점검 — 매일 돌린다

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/ws_frame_check.py --root D:/Private/stock-prod --date <YYYYMMDD>
```

종료 코드 1이면 보고 맨 앞에 쓴다. 이상 종류는 다섯 가지다.

- `FIELD_COUNT_CHANGED`: KIS가 `H0STCNT0` 필드 수를 또 바꿨다(기대값 47).
- `PARSER_DROPS`: 파서가 다건 프레임의 체결을 버리고 있다(`coverage_pct` < 100).
- `UNALIGNED`: 레코드로 정렬되지 않는 프레임이 있다.
- `HEADER_MISMATCH`: 프레임 헤더의 건수와 파서가 낸 레코드 수가 다르다(tick-schema-3 이후).
- `DAMAGED_MEMBER`: 틱 파일에 끊긴 gzip 멤버가 있다(전원 차단·강제 종료). 뒤 멤버는 읽었지만 끊긴 지점의 행은 잃었다.
  파서 결함이 아니다. 그날 재시작·전원 기록과 대조한다(`docs/TICK_GZIP_CORRUPTION_FOLLOWUP_20261109.md`).

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

### 4.2 체결통보 (2026-10 이후)

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l); e=d.get('event','')
    if e.startswith('FILL_NOTICE') or e in ('WS_FRAME_UNKNOWN_TR','ENTRY_ORDER_SENT','ENTRY_FILL_CONFIRMED_BY_NOTICE','ENTRY_EXECUTED'):
        x={k:v for k,v in d.items() if k in ('order_id','stage','notice_latency_ms','qty','price','exchange_time','reason','msg1','error','tr_id','encrypted','header_count','records','field_count','cntg_yn','rfus_yn','acpt_yn','booked','occurrences','side','ticker','cntg_qty','cntg_unpr')}
        print(d['ts'][11:23], e, x)
"
```

- `FILL_NOTICE_SUBSCRIBED`가 F4 시세 세션이 열릴 때마다(재연결 포함) 있어야 한다. `FILL_NOTICE_DISABLED`면
  사유(`msg_cd`·`msg1`)를 보고 맨 앞에 쓴다.
- 진입마다 `ENTRY_ORDER_SENT` → `FILL_NOTICE_RECEIVED` → (`ENTRY_FILL_CONFIRMED_BY_NOTICE` 또는 조회 확인) 순서를
  본다. 통보 없이 조회로만 확인된 진입은 "통보 누락"으로 센다.
- `notice_latency_ms`(주문 등록 → 첫 체결 통보)와 조회 확인 시각을 비교한다.
- `FILL_NOTICE_FRAME`은 체결통보 레코드마다 하나다(접수 통보 포함, 2026-10-07 진단 로그). 주문 하나에 보통
  접수(`cntg_yn=1`, `reason=NOT_FILL`) → 체결(`cntg_yn=2`, `booked=true`) 순서다. `SHORT`·`BAD_VALUE`가 있으면
  해석 결함이다.
- 모의 통보는 23필드다. 통보 체결 확정 릴리스(티켓 8.9절) 전에는 전부 `SHORT`로 버려졌고, 그 뒤로는 체결 통보가
  `booked=true`로 장부에 오른다. 그 뒤의 `SHORT`·`LONG`은 필드 수가 23·26이 아니라는 뜻이니 맨 앞에 쓴다.
  2026-10-13 이후 로그의 `FILL_NOTICE_FRAME`에는
  `side`·`ticker`·`cntg_qty`·`cntg_unpr`·`exchange_time`(공식 순서 인덱스 4·8~11의 원문)이 있다. 체결 통보
  (`cntg_yn=2`)의 `cntg_qty` 합이 주문 체결수량과, `cntg_unpr`가 DB 체결가와 맞는지 대조한다(분할 체결이면 수량
  가중 평균). 맞으면 위치가 확인된 것이다. `<len=N>`은 코드 모양이 아닌 값이라 가렸다는 뜻이다.
  **10/14 ~ 11/05 체결 주문 28건이 모두 맞았다(티켓 8.9절).** 이후에는 어긋나는 날만 맨 앞에 쓴다. 통보로 확정한
  진입이 어긋나면 진입가가 틀어졌을 수 있으니 특히 먼저 본다. 접수 통보
  (`cntg_yn=1`)의 수량·가격은 주문값이므로 대조에서 뺀다.
- `WS_FRAME_UNKNOWN_TR`(WARN)는 시세도 체결통보도 아닌 TR 프레임이다. `tr_id`가 `K0STCNI9`면 통보가 그 이름으로
  오고 있다는 뜻이다(`docs/FILL_NOTICE_FOLLOWUP_20261007.md` 6.2절).
- 주문이 있었는데 `FILL_NOTICE_FRAME`도 `WS_FRAME_UNKNOWN_TR`도 없으면, 그 세션으로는 통보가 오지 않은 것이다.
- 설계: `docs/superpowers/specs/2026-10-06-fill-notice-design.md`(B안 — F4 시세 세션에 얹음).

### 4.3 알림 (텔레그램)

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l); e=d.get('event','')
    if e.startswith('NOTIFICATION') or e=='PROCESS_RESTART_DETECTED':
        print(d['ts'][11:23], e, {k:v for k,v in d.items() if k in ('alert','attempts','errors','error','status','delivery','possible_duplicate','count','recovered_status')})
"
```

- `NOTIFICATION_FAILED`: 세 번 다 실패. `delivery`가 셋이다.
  - `UNCONFIRMED`: 응답 타임아웃 등 도착했을 수 있는 시도가 있었다(중복 가능).
  - `NOT_SENT`: 연결 실패(`ConnectError`·`ConnectTimeout`)뿐이었다. 도착하지 않았다. 이 구분은 2026-11-11 커밋분이 승격된
    뒤부터다. 그 전 로그에서는 연결 실패뿐이어도 `UNCONFIRMED`로 남는다(`errors`로 가른다).
  - `REJECTED`: 서버가 오류 응답으로 거부했다.
- **`NOTIFICATION_FAILED` 뒤에 `NOTIFICATION_UNCONFIRMED_REPORTED` 없이 `PROCESS_RESTART_DETECTED`가 오면 맨 앞에 쓴다.**
  실패한 알림 목록은 프로세스 메모리에만 있어서 재시작하면 사라진다. 사용자는 그 알림이 빠졌다는 표시를 받지 못했다
  (`docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md` 8.8절, 2026-11-11 결정 라). 그 절의 표에 한 줄 더하고 재검토 조건에 닿았는지 본다.
- `NOTIFICATION_RETRIED`(`release/20261028-1`~): 재시도 끝에 성공. `possible_duplicate=true`면 사용자가 같은 알림을
  두 번 이상 받았을 수 있다. 건수를 `docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md` 8절 아래에 날짜별로 적는다.
- 로그에 아무것도 없는데 사용자가 중복을 보고하면, 그 알림 이벤트(`TIMEOUT_CLOSE` 등)가 한 번뿐인지와 운영
  `main.py`가 하나뿐인지부터 본다(2026-10-28 8절).

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
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -c "
from datetime import datetime
from src.utils.jsonl_gz import read_jsonl_gz
rows=read_jsonl_gz('D:/Private/stock-prod/data/strategy_ticks/<YYYYMMDD>/<티커>.09.jsonl.gz').rows  # 장중이면 잘려 있다
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

보고 맨 끝에 전체 기간과 현재 구간(`src/perf_costs.py`의 끝이 열린 구간)의 CAGR·MDD를
한 줄씩 붙인다. 대표값은
전략 자본 기준 보수 손익이다. 1년 미만 CAGR에는 "(참고)"가 붙는다 — 총수익률을 함께 적는다.
기간 30일 미만인 구간은 CAGR이 "— (30일 미만)"으로 나온다. 그 구간은 총수익률만 적는다.
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

---
name: promote
description: 개발 트리 변경을 운영 트리로 승격한다 (preflight → 태그 → promote.ps1 2패스)
disable-model-invocation: true
---

운영 프로세스를 재시작하는 절차다. 사용자가 명시적으로 요청할 때만 돈다.

## 0. 먼저 막을 것

**장중(08:00~15:30)이면 멈추고 사용자에게 알린다.** 재시작 공백이 08:59~09:01에
걸리면 그날 진입을 통째로 놓친다(2026-09-17 사례). 마감 후로 미루자고 제안한다.

```bash
powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')"
```

15:20까지 틱 캡처가 돌고 15:32에 C1 스크리닝이 있으니, 마감일이면 그 뒤가 깔끔하다.

## 1. 지문이 바뀌는지 먼저 판단한다

`src/release.py`의 `_STRATEGY_FILES` 19개 파일이나 `F1_`~`F5_`·`PAPER_FAST_`·
`TRAILING_SHADOW_`·`STRATEGY_TICK_`·`VI_`·`BALANCE_SNAPSHOT_`·`EXIT_RECONCILE_`·
`KIS_RATE_`·`KIS_*TRANSIENT_`·`KIS_LOW_PRIORITY_` 환경변수가 바뀌었으면 지문이
바뀌고 **실계좌 20건 카운터가 0이 된다.**

바뀐다면 현재 누적 건수를 확인해 리셋 비용을 사용자에게 말한 뒤 진행한다.

```bash
curl -s http://127.0.0.1:8899/api/readiness | python -c "import sys,json; d=json.load(sys.stdin); print(d['strategy_fingerprint'], d['clean_paper_trades'], '/', d['required_paper_trades'])"
```

## 2. 개발 트리 preflight

```powershell
Set-Location D:\Private\stock; .\scripts\preflight.ps1
```

ruff 0건 + mypy 신규 0건 + pytest 전체 통과여야 한다. **게이트를 우회하지 않는다.**
mypy가 "새 오류"라고 하는데 실제로는 기존 오류가 줄만 옮긴 것이면
`scripts\mypy_baseline.py --write`로 재동결하고 그 사실을 커밋 메시지에 적는다.

워킹트리가 더러우면 태그 전에 커밋한다.

## 3. 커밋·푸시·태그

```bash
cd /d/Private/stock && git push origin main
git tag -a release/<YYYYMMDD>-<n> -F - <<'EOF'
<제목 한 줄>

<왜 이 릴리스가 필요한지, 포함 커밋, 지문 변경 여부>
EOF
git push origin release/<YYYYMMDD>-<n>
```

같은 날 두 번째면 `-2`, 세 번째면 `-3`이다.

## 4. 운영 승격

```powershell
Set-Location D:\Private\stock-prod; .\scripts\promote.ps1 -Tag release/<YYYYMMDD>-<n>
```

- 워킹트리가 더러우면 멈춘다. `.env.bak*` 같은 게 남아 있으면 정리한다.
- 지문이 바뀌면 1패스가 **되돌리고** 정확한 값을 알려준다. 그 값으로 재실행한다:

```powershell
.\scripts\promote.ps1 -Tag release/<YYYYMMDD>-<n> -AcknowledgeFingerprint <값>
```

개발 트리 preflight가 찍는 지문은 `.env`가 달라 참고용일 뿐이다 — **운영이 알려준
값만 쓴다.**

## 5. 승격 후 확인

```bash
cd /d/Private/stock-prod && git describe --tags
python -c "
import json
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l)
    if d.get('event')=='STRATEGY_FINGERPRINT_LOCKED': print(d['ts'][11:19], d['fingerprint'])
"
curl -s http://127.0.0.1:8899/api/readiness | head -c 200
```

정상 기동 로그는 `STRATEGY_FINGERPRINT_LOCKED` → `BASELINE_EXPERIMENT_REGISTERED`
→ `DAILY_STATE_RESET` → `TOKEN_LOADED_FROM_CACHE` → `TIME_SYNC_OK` 순이다.
`PROCESS_RESTART_DETECTED`·`TRADE_ALREADY_EXISTS`는 그날 거래가 이미 끝났으면
정상이다.

프로세스가 둘로 보이면 놀라지 않는다 — `.venv\Scripts\python.exe`는 런처 스텁이고
자식이 실제 프로세스다. `main.pid`의 값이 자식 PID와 맞는지로 확인한다.

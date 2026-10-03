# 데일리 갭업 자동매매 — 작업 규칙

모르면 사고가 나는 것만 적었다. 긴 근거는 `docs/`에 있으니 필요할 때 읽는다.

## 트리가 둘이다

| | 경로 | 역할 |
|---|---|---|
| 개발 | `D:\Private\stock` | `main` 브랜치. **코드는 여기서만 고친다** |
| 운영 | `D:\Private\stock-prod` | 릴리스 태그의 detached HEAD. 직접 고치지 않는다 |

`.stock-role` 파일이 역할을 정한다. 운영 반영은 승격 절차로만 한다.

## 승격 절차

1. 개발에서 `.\scripts\preflight.ps1` — ruff + mypy 베이스라인 + pytest + 지문
2. 커밋·푸시 → `release/<YYYYMMDD>-<n>` 태그 푸시
3. 운영에서 `.\scripts\promote.ps1 -Tag <태그>`
4. 지문이 바뀌면 1패스가 멈추고 정확한 값을 알려준다 →
   `-AcknowledgeFingerprint <값>`으로 재실행 (2패스)

**장중(08:00~15:30)에는 승격하지 않는다.** 재시작 공백이 08:59~09:01에 걸리면
그날 진입을 통째로 놓친다(2026-09-17 실제 사례).

## 전략 지문 = 실계좌 20건 카운터

`src/release.py`의 `_STRATEGY_FILES`(19개 파일)나 `_STRATEGY_ENV_PREFIXES`
(`F1_`~`F5_`, `PAPER_FAST_`, `TRAILING_SHADOW_`, `STRATEGY_TICK_`, `VI_`,
`BALANCE_SNAPSHOT_`, `EXIT_RECONCILE_`, `KIS_RATE_`, `KIS_*TRANSIENT_`,
`KIS_LOW_PRIORITY_`) 환경변수를 건드리면 **지문이 바뀌고 20건 카운터가 0이 된다.**

IMPORTANT: 그 파일들을 고치기 전에 현재 누적 건수를 확인하고, 리셋 비용을 먼저
말한 뒤 진행한다. `scripts/`, `docs/`, `src/notifier.py`, `src/readiness.py`는
지문 대상이 아니라 자유롭게 고쳐도 된다.

## 전략 수정은 조건 하나씩

전략 동작을 바꾸는 변경은 **릴리스(=지문)당 조건 하나**만 낸다. 여러 조건을 묶으면 그 뒤
성과 변화를 어느 조건 탓으로도 돌릴 수 없다. 지문 리셋 비용을 아끼려고 묶지 않는다
(2026-10-01 파서 수정 + 상한가 잠김 예외 묶음 승격이 위반 사례). 동작이 바뀌는 버그 수정도
조건 하나로 센다.

## 사전등록 규율

`docs/superpowers/specs/`의 가설(H1~H4, C1)은 임계값을 **미리** 고정한 것이다.
결과를 보고 임계값을 조정하지 않는다. H1 스펙 6절의 "닫힌 축"은 다시 열지
않으므로, 새 아이디어가 그 축에 닿으면 구현 전에 먼저 말한다.

## Windows 제약

- `.ps1`은 **PowerShell 5.1**이다. `??`, `?.`, 삼항 연산자, `&&`, `||`가 없다.
  네이티브 명령에 `2>&1`과 `$ErrorActionPreference="Stop"`을 함께 쓰면
  `NativeCommandError`로 스크립트가 죽는다(2026-09-18 백필이 그렇게 죽었다).
- `.gitattributes`가 `* text=auto eol=lf`다(`.bat`/`.cmd`만 CRLF).
  파이썬 `write_text`는 Windows에서 `\n`을 CRLF로 바꿔 저장하므로
  **파일을 쓸 때는 `write_bytes`를 쓴다.** `.ps1`은 BOM을 유지한다.

## 손대면 안 되는 것

- `.env`, `.env.paper`, `.env.real`은 자격증명이다. 값을 출력하지도 커밋하지도
  않는다(마스킹해서 확인만 한다).
- 개발 트리는 `KIS_AUTH_READONLY=1`이고 `AUTH_DIR`이 운영을 가리킨다 —
  **두 트리가 같은 앱키를 써야 한다.** 한쪽만 바꾸면 개발은 옛 앱키로 발급된
  토큰을 쓰게 되고 스스로 복구하지 못한다.
- 진단·분석 스크립트가 운영 매매 동작을 바꾸면 안 된다. 읽기 전용으로 짠다.

## 자주 쓰는 명령

```powershell
.\scripts\preflight.ps1                                       # 승격 전 검사
.\.venv\Scripts\python.exe -m pytest -q                       # 테스트만
.\.venv\Scripts\python.exe scripts\mypy_baseline.py --write   # 베이스라인 재동결
.\.venv\Scripts\python.exe scripts\restart_guard.py --root .  # 재시작 안전 여부
```

## 데이터 위치

| 대상 | 경로 |
|---|---|
| 운영 로그 | `data/logs/<YYYYMMDD>.jsonl` (JSON Lines) |
| 빠른 경로 프로브 덤프 | `data/paper_fast_probe/<YYYYMMDD>.jsonl` |
| 틱 캡처 | `data/strategy_ticks/<YYYYMMDD>/<티커>.<시>.jsonl.gz` |
| DB | `data/db/trading.db` (`trades`, `orders`) |
| 백업 | `D:\Private\stock_backups\data_<날짜>_<시각>` |

장중에는 틱 `.gz`가 기록 중이라 잘린 스트림이다 — 읽을 때 `EOFError`를 삼키고
읽힌 데까지만 쓴다.

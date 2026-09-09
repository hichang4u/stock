[개발환경] 데일리 갭 자동매매 시스템 개발환경 설정 가이드

문서 버전: v1.1
작성일: 2026년 6월 23일 / 최종 수정: 2026년 7월 14일
대상 OS: Windows 11 (운영 환경) / Windows 11 또는 WSL2 (개발 환경)
연관 문서: PRD.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 시스템 요구사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  항목                | 최소           | 권장
  ─────────────────  |---------------|──────────────────
  OS                  | Windows 10 64bit | Windows 11 Pro
  Python              | 3.11           | 3.12
  RAM                 | 4GB            | 8GB
  디스크 여유 공간      | 10GB           | 50GB (2년 로그 기준)
  인터넷               | 유선 100Mbps   | 유선 + LTE 백업
  시스템 시각          | NTP 동기화 필수 | 오차 ±200ms 이내

● Python 3.11 이상 필수 이유:
  asyncio 안정성 개선, tomllib 내장, Self 타입 힌트 지원.
  3.12 권장: 더 낮은 asyncio 오버헤드, 더 명확한 예외 메시지.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. 프로젝트 디렉토리 구조
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  stock/                        # 프로젝트 루트
  ├── .env                      # API 키 및 환경변수 (git 제외)
  ├── .env.example              # 환경변수 템플릿 (git 포함)
  ├── .gitignore
  ├── requirements.txt
  ├── requirements-dev.txt      # 개발/테스트 전용
  ├── main.py                   # 진입점 (스케줄러 부트스트랩)
  │
  ├── src/                      # 핵심 소스
  │   ├── __init__.py
  │   ├── state.py              # 전역 State 스키마 및 atomic 조작
  │   ├── live.py               # SSE 상태, 원시 tick/분 단위 가격 이력
  │   ├── db.py                 # SQLite 스키마 및 비동기 CRUD
  │   ├── scheduler.py          # APScheduler 설정 (F1~F5 등록)
  │   ├── notifier.py           # Telegram 알림 비동기 큐
  │   │
  │   ├── modules/
  │   │   ├── __init__.py
  │   │   ├── f1_filter.py      # F1: 갭/유동성 필터링
  │   │   ├── f1_selector.py    # F1 후보 점수 계산 및 최종 선택
  │   │   ├── f2_lockup.py      # F2: 타겟 락업 엔진
  │   │   ├── f3_entry.py       # F3: 진입 주문 모듈
  │   │   ├── f4_tracking.py    # F4: 장중 추적 스탑
  │   │   └── f5_timeout.py     # F5: 마감 청산
  │   │
  │   ├── api/
  │   │   ├── __init__.py
  │   │   ├── kis_rest.py       # KIS REST API 래퍼 (rate limit 포함)
  │   │   ├── kis_ws.py         # KIS WebSocket 클라이언트
  │   │   ├── auth.py           # 토큰 발급/갱신/캐시 관리
  │   │   ├── status_logic.py    # API 상태/로그 해석 보조 로직
  │   │   └── server.py          # Web UI 정적 파일 및 JSON/SSE API
  │   │
  │   └── utils/
  │       ├── __init__.py
  │       ├── time_sync.py      # NTP 검증
  │       ├── logger.py         # JSON Lines 로거 설정
  │       └── spike_filter.py   # 시세 스파이크 필터
  │
  ├── data/                     # 런타임 데이터 (git 제외)
  │   ├── logs/                 # YYYYMMDD.jsonl
  │   ├── state/                # today_state.json
  │   ├── params/               # history.json
  │   └── auth/                 # token_cache.json
  │
  ├── tests/
  │   ├── __init__.py
  │   ├── test_state.py
  │   ├── test_f1_filter.py
  │   ├── test_live.py
  │   ├── test_f5_timeout.py
  │   ├── test_api_server.py
  │   ├── test_state_daily_reset.py
  │   ├── test_f4_tracking.py
  │   ├── js/price_flow_checks.js
  │   └── fixtures/             # 테스트용 mock 시세 데이터
  │
  ├── scripts/
  │   ├── init_dirs.py          # data/ 하위 디렉토리 초기화
  │   ├── watchdog_check.py     # Task Scheduler에서 호출하는 워치독
  │   └── backtest.py           # 백테스트 진입점 (§8 최적화)
  │
  └── docs/
      ├── PRD.md
      ├── DEV_ENV.md            # 이 문서
      ├── UI_DESIGN.md
      ├── DB_DESIGN.md
      ├── TABLE_DESIGN.md
      ├── CODING_GUIDELINES.md
      └── html/                 # 운영 UI 정적 파일

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. Python 환경 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
3-1. Python 설치 확인
──────────────────────────────────────────────────

  python --version          # 3.11.x 또는 3.12.x 확인
  python -m pip --version   # pip 최신 버전 확인

  Python이 없으면: https://www.python.org/downloads/
  설치 시 "Add python.exe to PATH" 반드시 체크.

──────────────────────────────────────────────────
3-2. 가상환경 생성 및 활성화
──────────────────────────────────────────────────

  # 프로젝트 루트에서 실행 (PowerShell)
  python -m venv .venv

  # 활성화 (PowerShell)
  .\.venv\Scripts\Activate.ps1

  # 활성화 확인 — 프롬프트 앞에 (.venv) 표시되어야 함
  python --version

  ※ PowerShell 실행 정책 오류 발생 시:
     Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

──────────────────────────────────────────────────
3-3. 패키지 설치
──────────────────────────────────────────────────

  # 운영 패키지
  pip install -r requirements.txt

  # 개발/테스트 패키지 추가 설치
  pip install -r requirements-dev.txt

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. 패키지 목록 (requirements.txt)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
requirements.txt (운영)
──────────────────────────────────────────────────

  # HTTP 클라이언트 (비동기, rate limit 레이어 구현 용이)
  httpx==0.27.*

  # WebSocket 클라이언트
  websockets==13.*

  # 비동기 스케줄러 (APScheduler asyncio 백엔드)
  APScheduler==3.10.*

  # 환경변수 관리 (.env 파일 로드)
  python-dotenv==1.0.*

  # NTP 시각 동기화 검증
  ntplib==0.4.*

  # 설정 파일 파싱 (선택 — config.toml 사용 시)
  # tomli==2.0.*   # Python 3.11+ 는 내장 tomllib 사용

──────────────────────────────────────────────────
requirements-dev.txt (개발/테스트 전용)
──────────────────────────────────────────────────

  -r requirements.txt

  # 테스트 프레임워크
  pytest==8.*
  pytest-asyncio==0.23.*

  # HTTP mock (KIS API 테스트용)
  respx==0.21.*

  # WebSocket mock
  pytest-mock==3.14.*

  # 코드 스타일
  ruff==0.4.*

  # 타입 검사
  mypy==1.10.*

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. 환경변수 설정 (.env)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
.env.example (git에 포함 — 실제 값 없음)
──────────────────────────────────────────────────

  # ── KIS API ──────────────────────────────────────
  KIS_APP_KEY=your_app_key_here
  KIS_APP_SECRET=your_app_secret_here
  # Account env priority: KIS_ACCT_NO/KIS_ACCT_CD override KIS_ACCOUNT_NO/KIS_ACCOUNT_TYPE.
  # Use one pair only in normal operation; KIS_ACCOUNT_* is the documented default.
  # Empty KIS_ACCT_* values do not fall back; they are treated as invalid configuration.
  KIS_ACCOUNT_NO=your_account_number         # 예: 12345678-01
  KIS_ACCOUNT_TYPE=01                        # 01: 종합, 03: 선물옵션
  # KIS_ACCT_NO=your_account_number
  # KIS_ACCT_CD=01
  KIS_BASE_URL=https://openapi.koreainvestment.com:9443

  # ── 운영 모드 ──────────────────────────────────────
  # REAL: 실계좌 / PAPER: 모의투자 (기본값)
  KIS_MODE=PAPER

  # ── Telegram ──────────────────────────────────────
  TELEGRAM_BOT_TOKEN=your_bot_token_here
  TELEGRAM_CHAT_ID=your_chat_id_here

  # ── 시스템 ────────────────────────────────────────
  NTP_SERVER=pool.ntp.org
  LOG_DIR=data/logs
  STATE_DIR=data/state
  PARAMS_DIR=data/params
  AUTH_DIR=data/auth

──────────────────────────────────────────────────
실제 .env 파일 생성
──────────────────────────────────────────────────

  # .env.example을 복사 후 실제 값으로 채움
  copy .env.example .env
  # 이후 .env 파일을 편집기로 열어 값 입력

  ● .env는 절대 git commit 하지 않음 (.gitignore에 포함).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. KIS API 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
6-1. KIS Open API 신청 절차
──────────────────────────────────────────────────

  1. 한국투자증권 계좌 개설 (비대면 가능).
  2. KIS Developers 포털 접속: https://apiportal.koreainvestment.com
  3. 앱 등록 → App Key / App Secret 발급.
  4. 모의투자 신청 (선택): 실계좌 전 테스트용.
     모의투자 URL: https://openapivts.koreainvestment.com:29443

──────────────────────────────────────────────────
6-2. 모의투자(PAPER) vs 실계좌(REAL) 환경 분리
──────────────────────────────────────────────────

  구분          | Base URL                                        | KIS_MODE
  ─────────────|─────────────────────────────────────────────── |─────────
  모의투자       | https://openapivts.koreainvestment.com:29443   | PAPER
  실계좌         | https://openapi.koreainvestment.com:9443       | REAL

  ● KIS_MODE=PAPER 상태에서는 실제 주문이 발생하지 않음.
  ● 개발 및 테스트는 반드시 PAPER 모드에서 진행.
  ● REAL 전환 전 아래 항목 최종 확인:
    □ .env의 KIS_MODE=REAL로 변경
    □ KIS_BASE_URL을 실계좌 URL로 변경
    □ 계좌번호(KIS_ACCOUNT_NO 또는 우선 적용되는 KIS_ACCT_NO) 실계좌 번호로 변경
    □ 잔고 확인 (소액 테스트 권장)

──────────────────────────────────────────────────
6-3. WebSocket 접속 정보
──────────────────────────────────────────────────

  모의투자 WS: ws://ops.koreainvestment.com:31000
  실계좌  WS: ws://ops.koreainvestment.com:21000

  구독 TR: H0STCNT0 (주식 체결 실시간 조회)
  승인 방식: sendMessage로 구독 요청 시 Access Token 포함.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
7. Telegram Bot 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Telegram에서 @BotFather 검색 → /newbot 명령 실행.
  2. Bot 이름 및 username 입력 → API Token 발급.
  3. 발급된 토큰을 TELEGRAM_BOT_TOKEN에 저장.

  4. Chat ID 확인 방법:
     a. 발급된 Bot과 1:1 채팅 시작 (아무 메시지나 전송).
     b. 브라우저에서 아래 URL 접속:
        https://api.telegram.org/bot{TOKEN}/getUpdates
     c. 응답 JSON에서 "chat" → "id" 값 확인.
     d. 해당 값을 TELEGRAM_CHAT_ID에 저장.

  5. 테스트 알림 발송 확인 (PowerShell):
     $TOKEN = $env:TELEGRAM_BOT_TOKEN
     $CHAT  = $env:TELEGRAM_CHAT_ID
     Invoke-RestMethod -Uri "https://api.telegram.org/bot$TOKEN/sendMessage" `
       -Method POST `
       -Body @{ chat_id=$CHAT; text="[TEST] 알림 채널 연결 확인" }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
8. NTP 시간 동기화 설정 (Windows)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PRD §4 요건: 시스템 클럭 오차 ±200ms 이내. 500ms 초과 시 CRIT 알림.

──────────────────────────────────────────────────
8-1. Windows Time Service 설정 (관리자 PowerShell)
──────────────────────────────────────────────────

  # NTP 서버 설정 (pool.ntp.org 권장)
  w32tm /config /manualpeerlist:"pool.ntp.org,0x9" /syncfromflags:manual /reliable:YES /update

  # Windows Time 서비스 재시작
  Restart-Service w32tm

  # 즉시 동기화
  w32tm /resync /force

  # 동기화 상태 확인
  w32tm /query /status

  "Leap Indicator: 0 (no warning)" 및 "Stratum: 3" 이하 확인.

──────────────────────────────────────────────────
8-2. 애플리케이션 레벨 NTP 검증 (시작 시 자동 실행)
──────────────────────────────────────────────────

  # src/utils/time_sync.py 에서 다음 로직 구현
  # ntplib를 사용하여 시스템 시각 오차를 측정하고
  # 허용 범위(±200ms) 초과 시 CRIT 알림 발송.

  허용 기준:
    오차 <= 200ms  → 정상 (INFO 로그)
    200ms < 오차 <= 500ms → WARN 로그
    오차 > 500ms  → CRIT 알림 + 운영자 확인 요청

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
9. 데이터 디렉토리 초기화
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # scripts/init_dirs.py 실행 (최초 1회)
  python scripts/init_dirs.py

  위 스크립트는 아래 디렉토리를 생성한다:
    data/logs/
    data/state/
    data/params/
    data/auth/

  ● data/ 전체는 .gitignore에 추가 (API 토큰, 포지션 상태 등 민감 정보 포함).
  ● data/params/history.json 은 최초 빈 배열 [] 로 초기화.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
10. .gitignore 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  .venv/
  .env
  __pycache__/
  *.pyc
  *.pyo
  .mypy_cache/
  .ruff_cache/
  .pytest_cache/
  data/
  *.log

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
11. 프로세스 워치독 설정 (Windows Task Scheduler)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PRD §6-7 요건: 프로세스 사망 시 1분 이내 자동 재시작.

──────────────────────────────────────────────────
11-1. 작업 스케줄러 등록 (관리자 PowerShell)
──────────────────────────────────────────────────

  $Action = New-ScheduledTaskAction `
    -Execute "C:\path\to\.venv\Scripts\python.exe" `
    -Argument "C:\path\to\stock\scripts\watchdog_check.py" `
    -WorkingDirectory "C:\path\to\stock"

  $Trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -Once `
    -At (Get-Date)

  $Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -RestartCount 0

  Register-ScheduledTask `
    -TaskName "StockBot_Watchdog" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest

  ※ C:\path\to\ 부분은 실제 절대 경로로 교체.

──────────────────────────────────────────────────
11-2. watchdog_check.py 동작 명세
──────────────────────────────────────────────────

  1. main.py 생존 여부를 PID 파일의 **락**으로 확인한다.
     main.py는 msvcrt.LK_NBLCK 로 main.pid 의 0번 바이트를 잠근 채 돈다
     (main.py `_try_lock_pid_file`). 워치독도 같은 락을 시도해 실패하면
     '살아 있다'로 판정하고, 성공하면 곧바로 풀고 '죽었다'로 판정한다.

     ※ PID 파일을 **읽어서** 판정하면 안 된다. 락 때문에 살아 있는 동안
       읽기가 PermissionError 로 실패하므로 판정이 정확히 뒤집힌다. 예전
       구현이 그랬고, 건강한 봇 위에 매분 새 인스턴스를 띄우려 들었다.
       그 인스턴스는 같은 락에 막혀 PROCESS_ALREADY_RUNNING(CRIT)으로
       즉시 죽는다 — 계좌 사고는 안 나지만 워치독으로서는 무용지물이다.

  2. 프로세스 없음 → main.py 재시작. PID 파일은 쓰지 않는다.
     main.py 가 락을 잡은 뒤 스스로 쓴다.
  3. 재기동 창은 **08:00 ~ 10:00** 이다. 상한만 두면 새벽에도 봇을 띄운다.
  4. 재시작 시 Telegram 알림: PROCESS_RESTART_DETECTED.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12. 실행 방법
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
12-1. 정상 실행 (운영)
──────────────────────────────────────────────────

  # 가상환경 활성화
  .\.venv\Scripts\Activate.ps1

  # 실행
  python main.py

● main.py는 08:30에 KIS 토큰 갱신 및 NTP 검증을 수행하고,
    이후 APScheduler에 F1~F5를 등록한 뒤 이벤트 루프를 유지한다.
  ● 청산 완료 후 다음 날 08:30까지 대기 상태로 유지된다 (종료 안 함).
  ● 09:00 이후 F3 체결 확인 마감 전 재시작하면 catch-up으로 F1을 보완 실행하고,
    F1 결과가 나오면 F2/F3 체인을 즉시 이어서 실행한다.
    F3 예정 시각이 이미 지났으면 force 모드로 F3 내부 시각 대기를 건너뛴다.

──────────────────────────────────────────────────
12-2. 개발/테스트 실행
──────────────────────────────────────────────────

  # 단위 테스트
  pytest tests/ -v

  # 특정 모듈만 테스트
  pytest tests/test_f4_tracking.py -v

  # 전체 커버리지
  pytest tests/ --cov=src --cov-report=term-missing

──────────────────────────────────────────────────
12-3. 코드 품질 검사
──────────────────────────────────────────────────

  # 린트 + 자동 수정
  ruff check src/ --fix

  # 타입 검사
  mypy src/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
13. 개발 환경 체크리스트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  최초 환경 설정 완료 여부를 아래 순서로 확인한다.

  □ Python 3.11+ 설치 및 PATH 등록 확인
  □ 가상환경 생성 및 활성화 확인
  □ requirements.txt 패키지 설치 완료
  □ .env 파일 생성 및 KIS API Key 입력 완료
  □ KIS_MODE=PAPER 확인 (실계좌 전환 전)
  □ data/ 디렉토리 초기화 완료 (init_dirs.py 실행)
  □ NTP 동기화 상태 확인 (w32tm /query /status)
  □ Telegram Bot 알림 테스트 발송 확인
  □ PAPER 모드 단순 주문 API 호출 테스트 성공
  □ WebSocket 체결 데이터 수신 테스트 성공
  □ pytest 전체 통과 확인
  □ Task Scheduler 워치독 등록 완료

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
14. 알려진 제약 및 주의사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

● KIS API 모의투자 제한:
  - 모의투자 환경은 장전 예상 체결가(F1 데이터 소스)가 실계좌 대비
    일부 제한될 수 있음. 실계좌 전환 전 데이터 품질 확인 필요.
  - WebSocket 체결 데이터 지연이 실계좌보다 클 수 있음.

● Windows 절전 모드:
  운영 PC의 절전/화면 보호기를 반드시 비활성화.
  절전 진입 시 스케줄러 타이밍 오차 발생 가능.
  설정 경로: 전원 관리 → 절전 모드 → "안 함"

● KIS API 점검 시간:
  평일 05:00~07:00 (API 정기 점검 가능).
  08:30 토큰 갱신 로직이 이 시간대 이후 실행되므로 일반적으로 무관.
  그러나 점검 연장 시 08:30 토큰 갱신 실패 가능 → CRIT 알림으로 감지.

● KIS 토큰 만료 응답:
  REST 래퍼는 HTTP 401뿐 아니라 KIS 응답 본문 `msg_cd=EGW00123`
  ("기간이 만료된 token 입니다.")도 토큰 만료로 처리한다.
  이 경우 `TOKEN_EXPIRED` 로그를 남기고 `auth.refresh()` 후 동일 요청을 1회 재시도한다.
  재시도 후에도 같은 응답이면 원 응답을 반환하고, 호출 모듈이 `BALANCE_QUERY_ERROR` 등
  업무 로그를 남긴다.

● 방화벽:
  KIS REST API (포트 9443) 및 WebSocket (포트 21000/31000) 아웃바운드 허용 필요.
  회사 네트워크 사용 시 방화벽 예외 등록 확인.
---

## 15. 2026-07-01 운영 변수 및 DRY_RUN 업데이트

### 추가 환경변수

아래 값은 `.env.example`에 반영되어 있으며, 실제 운영 시 `.env`에서 조정한다.

```env
DRY_RUN=0
DRY_RUN_TICKER=005930
DRY_RUN_PREV_CLOSE=10000
DRY_RUN_EXPECTED_PRICE=10300
DRY_RUN_EXPECTED_QTY=500000
DRY_RUN_ENTRY_PRICE=10300
DRY_RUN_ENTRY_QTY=10
DRY_RUN_STEP_DELAY=0.2
DRY_RUN_LOG_DIR=data/dry_run/logs
DRY_RUN_STATE_DIR=data/dry_run/state
DRY_RUN_DB_DIR=data/dry_run/db

# 미설정 시 KIS_MODE 기본값: REAL=0.20 / PAPER=1.1 (2026-04-20 유량 정책)
# KIS_RATE_INTERVAL_SEC=0.20
KIS_MAX_TRANSIENT_RETRIES=2
KIS_TRANSIENT_RETRY_BASE_SEC=1.0
KIS_TRANSIENT_RETRY_MAX_SEC=8.0
F1_GAP_MIN=0.025
F1_GAP_CORE_MAX=0.080
F1_GAP_HARD_MAX=0.100
F1_MIN_EXPECTED_AMOUNT=100000000
F1_HIGH_GAP_MIN_EXPECTED_AMOUNT=5000000000
F1_SELECTION_TOP_PCT=0.10
F1_MIN_CANDIDATES=10
F1_EXPECTED_QUOTE_CONCURRENCY=1
F1_MARKET_INTERVAL_SEC=3.0
F3_VI_CHECK_ENABLED=1
F3_VI_RELEASE_WAIT_SEC=130.0
F3_VI_RELEASE_POLL_SEC=2.0
F3_ENTRY_MAX_ATTEMPTS=2
F3_ENTRY_RETRY_DELAY_SEC=0.5
F3_ENTRY_RETRY_DEADLINE=09:11:00
F3_PRE_ORDER_QUIET_SEC=1.5
F3_PYRAMID_AT=09:10:40

# 조기·수동 진입 거래의 청산 후 가격 관측 종료시각
F4_POST_CLOSE_OBSERVE_UNTIL=09:10
TRAILING_SHADOW_ENABLED=1
TRAILING_SHADOW_BASELINE_TRAIL=0.015

# 0단계 계측: 체결된 PAPER 한 종목의 재생 가능한 가격 경로 durable 기록
STRATEGY_TICK_CAPTURE_ENABLED=1
STRATEGY_TICK_DIR=data/strategy_ticks
# 캡처 경로 완전성 전용 사후 REST 백업 스위치(09:35~15:14). 차트 보강용
# F4_POST_CLOSE_REST_BACKUP_ENABLED와 별개이며, 이 값을 0으로 두면 캡처는
# WS만으로 15:15까지의 경로를 채운다.
STRATEGY_TICK_REST_BACKUP_ENABLED=1
# 압축 후 일 용량 소프트 한도(MB). 초과해도 기록을 버리지 않고 경고만 남긴다.
STRATEGY_TICK_SOFT_LIMIT_MB=100
```

### 0단계 가격 경로 캡처(tick_capture)

- 체결된 PAPER 한 종목만 대상으로 진입(F3 체결 확정)부터 고정 **15:15 KST**까지 시간 단위
  gzip JSONL(`data/strategy_ticks/YYYYMMDD/{ticker}.{HH}.jsonl.gz`)로 기록하고 DB
  `price_path_manifests`에 최종화한다. 행 필드는 순번·거래소 시각(`source_ts`)·수신 시각·
  가격·수량·출처(ws/rest)·유효성이다.
- writer는 논블로킹이며 캡처·파일·DB 실패가 진입·청산·F5·복구 주문을 막거나 지연시키지
  않는다. writer는 F4 청산 모니터가 소유·취소하지 않는다(정상 청산이 기록을 끊지 않음).
- CLOSED 이후에는 가격만 기록하고 스탑·주문·VI 계산을 하지 않는다. 저우선
  (`REQUEST_PRIORITY_BACKGROUND`) REST 백업은 **09:35에 시작해 15:14에 멈춰** F5
  precheck/exec가 항상 우선한다. 이 백업은 차트 보강용
  `F4_POST_CLOSE_REST_BACKUP_ENABLED`(기본 0)를 우회하므로, 우회 자체를
  `STRATEGY_TICK_REST_BACKUP_ENABLED`로 명시해 운영자가 끈 설정을 캡처가 조용히
  되살리지 않게 한다.
- 15:15 이전 WS 단절은 재연결해도 `data_complete=0`/`missing_reason=WS_LOSS`로 남기고,
  프로세스 재시작은 truncate/중복 없이 이어쓰며 `RESTART_GAP`으로 표시한다. 거래소 시각
  역전은 `source_ts_reversals`, 실제 seq 빈틈은 `seq_gaps`, REST 보강 구간은
  `rest_backfill_ranges_json`으로 분리 기록한다.
- 재시작 복원 스캔(하루치 gzip 전체)은 워커 스레드에서 돈다. 이 경로가 F4 스탑 감시
  무장보다 앞서므로 이벤트 루프를 막으면 포지션이 무방비가 된다. writer·manifest는
  복원이 끝난 뒤에만 기록해 seq 중복을 막는다.
- `source_ts`가 없는 REST 표본은 거래소 시각 경계(`first_source_ts`/`last_source_ts`)를
  지우지 않는다. 사후 REST 백업이 마지막 행이 되는 실운영에서 경계가 NULL이 되면
  진입~15:15 커버리지 판정이 실제와 무관해진다.
- manifest `chunks_json`은 chunk별 `first_seq`/`last_seq`/`seq_count`만 담는다(틱마다
  순번을 넣으면 주문 경로와 같은 SQLite에 수 MB 쓰기가 생긴다).
- 압축 후 일 용량이 `STRATEGY_TICK_SOFT_LIMIT_MB`(기본 100MB)를 넘으면 기록을 버리지
  않고 `TICK_CAPTURE_SOFT_LIMIT_EXCEEDED` 경고에 실제 용량을 남긴다.
- `tick_capture`와 `f1_snapshot_selector`는 관측 전용이라 전략 지문
  (`_STRATEGY_FILES`)에 넣지 않는다. 넣으면 관측 코드 수정마다 새 `experiment_id`가
  열려 40거래일 paired 수집이 매번 초기화된다. 캡처 on/off는 `STRATEGY_TICK_` 환경
  스냅샷이 지문에 반영한다.
- 당일 분봉 읽기 전용 PoC는 `python scripts\kis_minute_bar_poc.py`(외부 호출 없음)이며,
  라이브 검증은 PAPER·09:35 이후·`--with-kis`로 수동 실행한다(≤60 실제 호출, 주문 경로 없음).

### DRY_RUN 실행 목적

- 실계좌/모의계좌 주문 없이 F1 -> F4 흐름을 확인한다.
- DRY_RUN 실행 시 로그, 상태, DB는 `data/dry_run/*` 경로를 사용한다.
- 외부 KIS 인증, 주문, WebSocket을 건너뛰므로 안전한 회귀 테스트에 사용한다.
- F4는 결정론적 합성 틱으로 청산한 뒤 종료하며, `F4_POST_CLOSE_OBSERVE_UNTIL`까지
  청산 후 WS/REST 가격 관측을 계속하지 않는다.

### KIS rate limit 운영 기준

- REST 호출은 `KIS_RATE_INTERVAL_SEC` 기준으로 전역 직렬화한다.
- REST 연결은 프로세스 수명 동안 공유 AsyncClient의 연결 풀을 재사용하고,
  정상 종료 시 `kis_rest.close_client()`로 닫는다.
- `LATENCY_HIGH`의 `latency_ms`/`network_ms`는 실제 HTTP 왕복시간이다.
  `rate_wait_ms`, `client_setup_ms`, `local_overhead_ms`, `total_ms`로 로컬 대기와
  상류 API 지연을 분리한다.
- F1 예상체결가 보강은 `F1_EXPECTED_QUOTE_CONCURRENCY`로 동시 작업 수를 제한한다.
- F1 KOSPI/KOSDAQ 랭킹 조회 사이에는 `F1_MARKET_INTERVAL_SEC` 간격을 둔다.
- F3 매수 주문 직전에는 `F3_PRE_ORDER_QUIET_SEC`만큼 대기해 직전 조회 호출과 주문 호출이 붙지 않게 한다.
- KOSDAQ 랭킹 조회가 KIS 응답 코드 `OPSQ2001` 등으로 실패할 수 있으므로, F1 로그의 `market`, `rt_cd`, `msg_cd`를 함께 확인한다.
- F1 진행·완료 로그의 `processed_count`, `eligible_count`, `skipped_count`,
  `quote_valid_count`, `quote_fallback_count`, `error_count`, `skip_reasons`를 함께 확인한다.
  `parsed_count`는 원본 응답 수가 아니라 적격 후보 수와 같은 의미다.

### F4 청산 후 관측과 EXITING 운영

- `F4_POST_CLOSE_OBSERVE_UNTIL`은 `HH:MM` 형식이다. 잘못된 값은 로깅 초기화 후
  `F4_OBSERVE_UNTIL_INVALID` WARN을 1회 남기고 기본 `09:10`을 사용한다.
- 이 값을 `tick_capture.CAPTURE_UNTIL`(15:20)보다 **늦게 두면 캡처가 그 뒤로
  다시 붙지 않는다.** 그 시각 이후에는 관측(차트용 가격 수집)만 이어지고 durable
  캡처는 붙지 않는다 — 붙이면 `_price_observation_active()`의 컷오프가 캡처
  활성 여부로 뒤집혀 최종화·재부착이 초당 한 번씩 반복된다. 이 값이 `15:30`으로
  설정돼 있던 동안 15:15~15:30 사이 약 900바퀴가 돌았고, 재부착이 디스크의 기존
  청크를 재시작으로 읽어 그날 캡처가 `RESTART_GAP`으로 남았다.
- **`price_path_manifests`의 `RESTART_GAP` 5행은 실제 재시작이 아니라 위 진동이다.**
  20260814/001820, 20260819/006660, 20260827/006340, 20260828/043200,
  20260831/443670 다섯 행이 모두 같은 지문을 남겼다 — `created_at` 09:01,
  `last_source_ts` 15:14:58~59, `reached_expected_close=1`, 그리고 `finalized_at`이
  `F4_POST_CLOSE_OBSERVE_UNTIL` 경계인 15:30:00 ±1초. 진짜 재시작은 이렇게 초
  단위로 정렬되지 않는다. **다섯 날 모두 틱 데이터는 창이 닫힐 때까지 온전하다**;
  손상된 것은 품질 플래그뿐이다. 현재 `data_complete`를 읽는 코드가 없어(호출부는
  upsert 자신과 테스트뿐) 정정하지 않고 두었다. 품질 게이팅을 실제로 붙일 때 아래
  한 줄로 정리한다.

  ```sql
  UPDATE price_path_manifests SET data_complete=1, missing_reason=NULL
   WHERE missing_reason='RESTART_GAP' AND reached_expected_close=1;
  ```

  **품질 플래그만 잃은 것이 아니다 — 진동은 단절 증거도 지운다.** 매니페스트의
  UNIQUE 키가 `(trade_date, ticker, experiment_id)`라 재부착이 같은 행을 계속
  upsert하고, 마지막 0.5초짜리 인스턴스의 카운터가 남는다. 20260831은 그날 WS가
  16회 끊겨 15:15:00에 `WS_LOSS`(`ws_disconnects=16`)로 최종화됐는데, 진동이 끝난
  뒤 남은 행은 `ws_disconnects=0`이다. 위 `UPDATE`는 `data_complete`만 되돌린다 —
  `ws_disconnects`·`source_ts_reversals`는 복구할 수 없고, 그날 로그의
  `TICK_CAPTURE_FINALIZED` 첫 행에서만 실제 값을 읽을 수 있다.
- `CAPTURE_UNTIL`은 2026-08-31부터 **15:20**이다. 그 전에는 F5 청산 시각(15:15)에서
  파생된 값이었으나, F5가 15:15인 것은 시장가 매도가 마감 동시호가에 걸리지 않게
  하려는 주문 제약이지 관측 제약이 아니다. 연속매매는 15:20에 끝난다. 따라서
  `price_path_manifests.data_complete=1`의 뜻이 그날을 기준으로 바뀐다 — 이전 행은
  "15:15까지 받았다", 이후 행은 "15:20까지 받았다"이다. 두 기간의 완전성 비율을
  직접 비교하지 않는다. 15:20~15:30 마감 동시호가 구간은 담지 않는다(연속 체결이
  없고, 그 구간 WS 프레임이 무엇인지 실측한 바 없다).
- **캡처 창 이전(2026-08-31 이하)의 실장 확인 절차.** 창을 15:20으로 옮긴 뒤 첫
  거래일에 아래를 돌려 재부착 진동이 실제로 멎었는지 본다. A가 진입하지 않은 날에도
  캡처는 붙으므로(관측 계층 중립화) 거래 유무와 무관하게 확인된다.

  ```powershell
  .\.venv\Scripts\python.exe -c @'
import json
for line in open('data/logs/20260901.jsonl', encoding='utf-8'):
    r = json.loads(line)
    if r['event'].startswith('TICK_CAPTURE_'):
        print(r['ts'][11:19], r['event'], r.get('reason', ''), r.get('data_complete', ''))
'@
  ```

  | 시각 | 통과 조건 |
  |---|---|
  | 15:15 | `TICK_CAPTURE_FINALIZED`가 **없다** — 창이 15:20으로 옮겨졌다 |
  | 15:20 | `TICK_CAPTURE_FINALIZED`가 **정확히 1회** |
  | 15:20 이후 | `TICK_CAPTURE_STARTED`가 **없다** — 재부착이 막혔다 |

  `reason`은 그날 WS 상태에 따라 `COMPLETE`(무단절) 또는 `WS_LOSS`(단절 있음)이며,
  둘 다 정상이다. 이 확인이 보는 것은 **최종화 횟수와 시각**이지 완전성 플래그가
  아니다. `RESTART_GAP`이 다시 나오면 진동이 남아 있다는 뜻이다.

  비교용으로, 진동이 있던 20260831의 같은 출력은 이렇게 시작한다 — 15:15에
  최종화된 뒤 곧바로 다시 시작하고, 그 왕복이 15:29:59까지 이어진다.

  ```
  15:15:00 TICK_CAPTURE_FINALIZED WS_LOSS 0
  15:15:00 TICK_CAPTURE_STARTED
  15:15:01 TICK_CAPTURE_FINALIZED RESTART_GAP 0
  ```
- 조기·수동 진입 거래가 이 시각 전에 CLOSED가 되면 WS/REST는 차트용 가격만 수집한다.
  CLOSED 중에는 스탑 계산, VI 처리, 매도 주문을 실행하지 않는다.
- 상태 파일의 `entry_at`이 손상되면 `F4_ENTRY_AT_INVALID` WARN을 값별 1회 남기고
  청산 후 관측을 중단한다.
- 부분체결·체결 미확인·F5 재시도 실패는 `EXITING`으로 남는다.
- 당일 EXITING 재시작은 `EXITING_REQUIRES_RECONCILIATION` CRIT 알림과 함께
  자동 진입·자동 재매도를 차단한다. KIS 주문/체결 내역, 실제 잔고, DB OPEN 거래를
  수동 대사한 뒤 상태를 정리한다.

### Web UI 메뉴/API 기준

- `자산` 메뉴는 KIS 잔고 조회 기반 계좌 스냅샷을 표시한다.
  - API: `/api/assets`
  - 항목: 총평가금액, 예수금, 주문가능금액, 보유종목 수, 주식평가금액, 평가손익
  - KIS 조회 성공 결과는 `asset_snapshots` 테이블에 저장한다.
  - `/api/assets`와 `/api/status`는 메모리 캐시가 비어 있으면 마지막 저장 스냅샷을 fallback으로 반환할 수 있으며, UI는 `captured_at` 기준 마지막 저장 시각을 표시한다.
- `주문` 메뉴는 주문 실행과 처리 결과를 표시한다.
  - 현재 원천: `orders` 테이블, 주문 관련 JSONL 이벤트 로그
  - API: `/api/orders`
  - 항목: 주문번호, 종목, 매수/매도, 주문수량, 주문가격, 체결수량, 상태, 주문 단계, 주문시각/체결시각
- 주문/체결 내역은 `/api/stream`의 로그 이벤트 수신 시 즉시 `/api/orders`를 재조회하고, 5초 폴링을 백업으로 둔다.
- `주문가능금액`은 자산 데이터이므로 `/api/assets`가 원천이다. 주문 메뉴에서는 주문 판단용 보조 참조값으로만 노출한다.
- KIS 잔고 조회 응답에 비문서 확장 필드 `ord_psbl_cash`가 없으면 UI의 추정 주문가능금액은
  `dnca_tot_amt`와 `prvs_rcdl_excc_amt`(가수도정산금액) 중 큰 값을 fallback으로 사용한다.
  이 값은 주문 가능 보장이 아니며, 종목별 정확한 주문가능수량/금액 판단은 F3의 매수가능조회 경로를
  우선한다.
- 보유 후 가격흐름은 `/api/status`의 `tick_history`, `minute_price_history`, `trade_marks`를 함께 사용한다.
  - 원시 tick은 최대 5,000개, 분 단위 마지막 가격은 별도 버퍼에 최대 180분 보관한다.
  - 보유 중에는 최근 20분 슬라이딩 창, 청산 후에는 진입부터 마지막 체결/tick까지 고정 범위를 표시한다.
  - SSE tick은 증분 추가하고 렌더링은 최소 150ms 간격으로 묶는다. 화면 폭을 넘으면 구간별 최솟값/최댓값 보존 방식으로 다운샘플링한다.
  - 차트는 컨테이너 폭 100%를 사용하며 시간 범위에 따라 1/2/5/10분 격자와 적응형 라벨 간격을 선택한다.
  - 당일 체결 주문은 매수/매도 마커로 표시하고, 마지막 가격은 가격 점·현재가·마지막 매도·마지막 매수 순으로 fallback한다.

### 테스트 명령

현재 검증 기준은 가상환경 파이썬을 명시해서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node tests\js\price_flow_checks.js
```

운영 보강 변경만 빠르게 확인할 때:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_f5_timeout.py tests\test_live.py tests\test_api_server.py tests\test_state_daily_reset.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src\modules\f5_timeout.py src\live.py src\api\server.py tests\test_f5_timeout.py tests\test_live.py tests\test_api_server.py
node --check docs\html\assets\app.js
node tests\js\price_flow_checks.js
```

### Telegram 알림 확인

- 알림 메시지는 `제목 -> 상황 -> 조치 -> 세부 -> 코드` 순서로 표시된다.
- `STALE_POSITION_DETECTED`처럼 조치가 필요한 이벤트는 이벤트 코드보다 사람이 읽는 제목을 우선한다.
- Telegram 전송에는 Markdown `parse_mode`를 사용하지 않는다. 이벤트 코드의 `_` 문자나 한글 문구가 파싱 오류를 만들지 않게 하기 위함이다.

---

## 16. KIS MCP Phase 1 공식 자료 확인 (2026-07-21)

### 결론

| 대상 | 확인 결과 | 도입 판정 |
|------|-----------|-----------|
| KIS Code Assistant MCP | API 명세 검색·공식 샘플코드 조회 전용. 실제 KIS 계좌 API는 호출하지 않음 | Phase 2 진행 |
| KIS Trading MCP | 시세·계좌 조회와 함께 주문·정정·취소 기능 노출. 주문 계열만 끄는 공식 설정 없음 | 도입 보류 |

두 도구 모두 한국투자증권의 공식 KIS Developers 포털과 공식 GitHub 조직에서 안내한다. 이 프로젝트에는
Code Assistant만 개발 도구 후보로 등록하며, Trading MCP와 KIS Quant Plugin은 주문 기능을 포함하므로
현재 운영 정책의 도입 대상에서 제외한다.

### 공식 소스와 버전

- KIS Developers 포털
  - [MCP 소개](https://apiportal.koreainvestment.com/tools-mcp)
  - [코딩도우미 MCP](https://apiportal.koreainvestment.com/tools-sample)
  - [트레이딩 MCP](https://apiportal.koreainvestment.com/tools-trading)
- 공식 저장소
  - [open-trading-api/MCP](https://github.com/koreainvestment/open-trading-api/tree/main/MCP)
  - [KIS Code Assistant MCP](https://github.com/koreainvestment/open-trading-api/tree/main/MCP/KIS%20Code%20Assistant%20MCP)
  - [KIS Trading MCP](https://github.com/koreainvestment/open-trading-api/tree/main/MCP/Kis%20Trading%20MCP)
- 확인일: 2026-07-21
- Code Assistant 버전
  - NPM `package.json`: `0.1.1`
  - 내부 `pyproject.toml` 및 HTTP health 응답 예시: `0.1.0`
  - 두 메타데이터가 일치하지 않으므로 설치 후에는 NPM 패키지 버전과 서버 보고 버전을 각각 기록한다.
- Trading MCP 버전: `pyproject.toml` 기준 `0.1.0`
- 공식 저장소는 샘플이 별도 공지 없이 갱신될 수 있다고 명시하므로, 설치 시 확인일과 실제 설치 버전을
  함께 남긴다.

### Code Assistant 설치·실행 기준

요구사항은 Node.js 18 이상, Python 3.12 이상, `uv`다. 공식 문서의 권장 stdio 실행은 다음과 같다.

```powershell
npx -y @koreainvestment/kis-code-assistant-mcp
```

소스 검토가 필요한 경우에만 별도 개발 도구 디렉터리에 클론해 실행한다.

```powershell
git clone https://github.com/koreainvestment/open-trading-api.git
Set-Location "open-trading-api/MCP/KIS Code Assistant MCP"
uv sync
uv run server.py --stdio
```

이 명령은 Phase 1에서 공식 절차만 확인한 것이며 아직 이 저장소에 설치하거나 등록하지 않았다.

### 제공 기능과 권한 경계

Code Assistant는 인증, 국내주식, 국내채권, 국내선물옵션, 해외주식, 해외선물옵션, ELW, ETF/ETN
카테고리별 API 검색 도구와 공식 GitHub 샘플코드 읽기 도구를 제공한다. 로컬 `data.csv` 기반 검색과
공식 GitHub 코드 조회만 수행하며, README도 실제 API 호출은 별도 구현이 필요하다고 명시한다.
따라서 KIS 앱 키·시크릿·계좌번호가 필요 없고 KIS REST 호출 한도를 소비하지 않는다.

Trading MCP는 166개 API를 대상으로 시세, 잔고, 주문·체결내역, 현물·신용·선물옵션 주문과
정정·취소를 제공한다. 현재 `server.py`는 국내주식 등 모든 상품군 Tool 클래스를 조건 없이 등록한다.
공식 README, 환경 변수, 서버 등록 코드에는 주문 계열만 제외하는 allowlist/denylist 또는 조회 전용
모드가 없다. MCP 접근 토큰은 서버 접속자를 인증할 뿐 KIS 주문 권한을 축소하지 않는다.

### 호출 제한 확인 결과

- Code Assistant: KIS Open API를 호출하지 않으므로 KIS 호출 쿼터와 무관하다.
- Trading MCP: 사용자 App Key/Secret으로 KIS Open API를 직접 호출하며 공식 README도 KIS API 호출
  제한 준수를 요구한다. 같은 자격 증명을 봇과 함께 쓰면 호출 충돌 위험이 있으므로 운영상 같은 쿼터로
  간주한다.
- 미확정: 제한 산정의 정확한 단위가 앱 키, 계좌 또는 사용자 중 무엇인지는 2026-07-21 현재 공개된
  공식 문서에서 확인하지 못했다. Trading MCP 재검토 전에 KIS Developers 공식 문의 채널에서 확인한다.

### Phase 1 결정

1. Phase 2에서는 Code Assistant만 격리된 개발 환경에 등록한다.
2. Code Assistant에는 KIS 자격 증명을 전달하지 않는다.
3. Trading MCP는 공식 조회 전용 모드 또는 신뢰할 수 있는 도구 allowlist가 제공될 때까지 설치하지 않는다.
4. 쿼터 산정 단위가 확인되기 전에도 09:00~09:11 KIS MCP 호출 금지 정책은 유지한다.

### Phase 2 Code Assistant 연결 (2026-07-21)

Codex의 프로젝트 전용 `.codex/config.toml`에 Code Assistant를 등록했다. 전역 Codex 설정은 변경하지
않았으며, 이 저장소가 신뢰 상태일 때만 설정이 로드된다.

```toml
[mcp_servers.kis-code-assistant]
command = "npx"
args = ["-y", "@koreainvestment/kis-code-assistant-mcp@0.1.1"]
enabled = true
required = false
startup_timeout_sec = 60.0
tool_timeout_sec = 60.0
default_tools_approval_mode = "approve"
enabled_tools = [
  "search_auth_api",
  "search_domestic_stock_api",
  "search_domestic_bond_api",
  "search_domestic_futureoption_api",
  "search_overseas_stock_api",
  "search_overseas_futureoption_api",
  "search_elw_api",
  "search_etfetn_api",
  "read_source_code",
]
```

`default_tools_approval_mode = "approve"`는 위 allowlist에 포함된 검색·소스 읽기 도구에만 적용된다.
KIS 앱 키, 앱 시크릿, 계좌번호와 환경 변수는 설정하지 않는다. Trading MCP는 등록하지 않는다.

설정 확인:

```powershell
codex mcp list
codex mcp get kis-code-assistant --json
```

실제 새 Codex 프로세스에서 `search_domestic_stock_api`로 “국내주식 잔고조회 API”를 검색해 MCP 응답
`inquire_balance`, `inquire_balance_rlz_pl`을 수신했다. 설정 변경 전에 실행 중이던 Codex 세션은 MCP
목록을 자동 갱신하지 않을 수 있으므로 새 세션을 열거나 Codex/IDE 확장을 재시작한다.

### Phase 4 PAPER 읽기 전용 사고 분석 (2026-07-21)

Code Assistant에는 계좌 조회 기능이 없고 Trading MCP는 주문 도구를 분리할 수 없어 사용하지 않는다.
실제 원장 대조는 기존 KIS REST 래퍼를 통해 잔고와 당일 미체결 GET만 호출한다.

```powershell
python scripts\kis_phase4_readonly_audit.py
```

도구의 안전 조건:

- `KIS_MODE=PAPER`만 허용
- 09:00~09:11 및 15:40 이전 실행 차단
- 잔고조회 성공 후에만 미체결조회 수행
- `stop_on_rate_limit=True`로 HTTP 429와 `EGW00201` 즉시 중단
- 응답 컨테이너·필수 필드 누락, 인증·권한 오류, `rt_cd != 0`에서 추가 호출 중단
- 계좌번호·종목·주문번호·금액·원문 응답을 출력하지 않음
- DB는 SQLite read-only URI로 열고 JSONL은 읽기만 하며 자동 보정하지 않음

2026-07-21 16:27 KST PAPER 재현에서 잔고와 미체결조회가 성공했고, 보유 0종목·미체결 0건을
DB 당일 주문 2건(pending 0건)과 JSONL 이벤트 215건에 대조해 불일치 0건(`MATCH`)을 확인했다.
상세 확인 순서와 기록 양식은 [KIS_INCIDENT_AUDIT.md](KIS_INCIDENT_AUDIT.md)를 따른다.

### Phase 5 과거 데이터 읽기 전용 PoC (2026-07-22)

로컬 F1 스냅샷과 Improve 메뉴의 DB 집계를 읽기 전용으로 비교한다. 기본 실행은 외부 API를 호출하지
않는다.

```powershell
python scripts\kis_phase5_historical_poc.py
```

장 종료 후 PAPER 공식 일봉으로 최대 3표본을 대조하려면 다음처럼 실행한다.

```powershell
python scripts\kis_phase5_historical_poc.py --with-kis --max-kis-samples 3
```

- `--with-kis`는 `KIS_MODE=PAPER`, 15:40 이후만 허용하고 09:00~09:11에는 차단한다.
- 호출 API는 `inquire-daily-itemchartprice` GET뿐이며 HTTP 429·`EGW00201`·응답 오류에서 즉시
  중단한다. 주문·정정·취소 경로는 없다.
- 스냅샷은 09:00:00~09:10:59 KST의 날짜별 최초 정상 파일만 선택하고 주말, 확인된 휴장,
  범위 밖 시각과 중복 파일은 집계에서 제외한다.
- 수정주가 플래그 설명이 공식 일봉 샘플 사이에서 일치하지 않으므로 엔드포인트와 플래그를 함께
  기록한다. 다른 일봉 API에 플래그 의미를 그대로 적용하지 않는다.
- 상세 데이터 정의, 537행 품질 검사, PAPER 3표본 결과와 Improve 비교는
  [KIS_HISTORICAL_POC.md](KIS_HISTORICAL_POC.md)에 기록했다.

### Phase 6 MCP 운영 안전성·비활성화 (2026-07-22)

#### 호출 시간과 조사 기록

- 09:00:00 이상 09:11:00 미만에는 Code Assistant를 포함한 KIS MCP와 개발용 KIS 조회를
  실행하지 않는다.
- 그 밖의 장중 호출은 봇을 먼저 중지했거나, 봇과 앱 키·호출 쿼터가 분리된 별도 PAPER 키를
  사용하는 경우에만 허용한다. 일반적인 명세 조사와 과거 데이터 조회는 15:40 이후를 기본으로 한다.
- REAL 계좌 조사는 별도 승인과 조회 전용 경로가 확인된 경우만 허용한다. 주문 도구가 함께 노출되는
  Trading MCP는 사용하지 않는다.
- 모든 MCP 조사는 시각(KST), 목적, 도구/버전, 계정 구분, 봇 상태, 자격 증명 분리 여부, 조회
  대상·TR/데이터 기간, 호출 횟수와 결과·중단 코드를 남긴다. 기록 양식은
  [KIS_INCIDENT_AUDIT.md](KIS_INCIDENT_AUDIT.md)를 사용한다.

#### 정적 안전 감사

다음 명령은 외부 API나 MCP를 호출하지 않고 프로젝트 설정과 문서, 런타임 의존성만 검사한다.

```powershell
python scripts\kis_phase6_safety_audit.py
```

검사 항목은 Code Assistant `0.1.1`만 등록됐는지, 주문·계좌·Trading 도구가 없는지, 설정에 KIS
자격 증명 표식이 없는지, `main.py`와 `src/`가 MCP를 참조하지 않는지, `.codex/`가 Git에서
제외됐는지, 운영·폐기 절차가 문서화됐는지다.

#### MCP 중지·등록 해제

1. 진행 중인 MCP 조회가 없는지 확인하고 Codex/IDE 세션을 종료한다. 세션 종료가 stdio MCP 자식
   프로세스를 정상 종료하는 기본 절차다.
2. 사용자 전역 등록으로 설치한 경우 다음 명령으로 제거한다.

```powershell
codex mcp remove kis-code-assistant
```

   현재처럼 `.codex/config.toml`에 둔 프로젝트 로컬 등록은 `codex mcp list`에는 보이지만
   `codex mcp remove`의 제거 대상이 아니다. 이 경우 해당 TOML의 `kis-code-assistant` 블록을
   제거한다. 파일에 다른 설정이 없다면 파일 자체를 제거해도 된다. 제거 후 새 프로세스에서 확인한다.

```powershell
Remove-Item -LiteralPath .codex\config.toml
codex mcp list
```

3. 비정상 종료로 프로세스가 남았을 때만 명령행을 먼저 확인하고, 정확히
   `@koreainvestment/kis-code-assistant-mcp`인 PID만 중지한다. 광범위한 `node.exe` 종료는 금지한다.

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match '@koreainvestment/kis-code-assistant-mcp' } |
  Select-Object ProcessId, ParentProcessId, Name, CommandLine

Stop-Process -Id <확인한_PID> -Force
```

4. 설정 제거 상태에서는 다음 감사와 봇 검증을 실행한다.

```powershell
python scripts\kis_phase6_safety_audit.py --require-config-absent
python -c "import main; print('START_PATH_IMPORT_OK')"
python -m pytest -q -p no:cacheprovider
```

#### 자격 증명 폐기·교체

- 현재 Code Assistant에는 KIS 앱 키·시크릿·계좌번호·토큰을 전달하지 않았으므로 폐기할 MCP 전용
  자격 증명이 없다. 등록 제거와 프로세스 종료만 수행한다.
- Trading MCP는 설치하지 않았고 별도 자격 증명도 발급하지 않았다.
- 향후 별도 PAPER 자격 증명을 발급했다면 KIS Developers에서 해당 앱 키를 폐기·재발급하고,
  사용자 전용 환경 변수/운영체제 비밀 저장소와 MCP 토큰 캐시에서 이전 값을 삭제한다.
- MCP가 봇 자격 증명을 공유한 사실이 확인되면 봇을 중지하고 공유 키를 폐기·교체한 뒤 `.env`와
  토큰 캐시를 갱신한다. 새 키 검증 전에는 봇을 재시작하지 않는다.
- 폐기 기록에는 자격 증명 식별용 별칭, 폐기 시각, 수행자, 영향받는 환경과 재발급 확인만 남기며
  키 원문은 기록하지 않는다.

#### Phase 6 비활성화 재현 결과

2026-07-22 KST에 프로젝트 설정을 임시 제거하고 실행 중인 Code Assistant stdio 프로세스 트리
1개를 정확한 명령행·PID로 확인해 종료했다. 잔존 프로세스는 0개였고 새 `codex mcp list`는 등록
없음을 반환했다. 설정 부재 상태에서 다음을 확인했다.

- `kis_phase6_safety_audit.py --require-config-absent`: `PASS`, 외부 호출 0회
- `import main` 및 `main.main` 시작 callable 확인: `START_PATH_IMPORT_OK`
- 전체 테스트: `440 passed`
- 런타임 `main.py`·`src/`의 MCP 참조: 0건

검증 뒤 개발용 프로젝트 설정은 원본 SHA-256이 같은 파일로 복원했으며 MCP 프로세스는 중지 상태로
두었다. 새 Codex/IDE 세션은 복원된 프로젝트 설정을 읽어 Code Assistant를 다시 시작할 수 있다.
`codex mcp remove kis-code-assistant`는 이 프로젝트 로컬 등록에 대해 “not found”를 반환했으므로,
프로젝트 등록 해제 절차는 `.codex/config.toml` 제거가 기준이다.

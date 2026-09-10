import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_logger: logging.Logger | None = None

EVENT_LABELS = {
    "DB_TRACK_MIGRATION_START": "트랙 마이그레이션 시작(DB Track Migration Start)",
    "DB_TRACK_MIGRATION_DONE": "트랙 마이그레이션 완료(DB Track Migration Done)",
    "DB_TRACK_MIGRATION_PREEXISTING_FK_VIOLATIONS": (
        "마이그레이션 이전부터 있던 FK 위반(Pre-existing FK Violations)"
    ),
    "DAILY_STATE_RESET": "새 거래일 상태 초기화(Daily State Reset)",
    "CATCHUP_START": "누락 작업 보완 시작(Catch-up Start)",
    "PROCESS_ALREADY_RUNNING": "이미 실행 중인 프로세스 감지(Process Already Running)",
    "STALE_PID_REPLACED": "오래된 PID 파일 제거(Stale PID Removed)",
    "STALE_STATE_DISCARDED": "지난 거래일 상태 파일 폐기(Stale State Discarded)",
    "STALE_BACKUP_FAILED": "지난 상태 증거 사본 실패(Stale Backup Failed)",
    "STALE_ACTIVE_RECONCILED": (
        "전일 활성 상태 자동 정리(Stale Active Reconciled)"
    ),
    "STALE_ACTIVE_ROLLOVER_BLOCKED": (
        "전일 활성 상태 일일 전환 차단(Stale Active Rollover Blocked)"
    ),
    "PROCESS_RESTART_DETECTED": "프로세스 재시작 감지(Process Restart Detected)",
    "STARTUP_RECOVERY_FAILED": "시작 시 포지션 대사 실패(Startup Recovery Failed)",
    "STARTUP_RECOVERY_FALLBACK_FAILED": (
        "시작 대사 상태 복원 실패(Startup Recovery Fallback Failed)"
    ),
    "STARTUP_RECOVERY_ALERT_FAILED": (
        "시작 대사 실패 알림 오류(Startup Recovery Alert Failed)"
    ),
    "STRATEGY_FINGERPRINT_LOCKED": (
        "프로세스 전략 지문 고정(Strategy Fingerprint Locked)"
    ),
    "BASELINE_EXPERIMENT_REGISTERED": (
        "기준선 실험 등록(Baseline Experiment Registered)"
    ),
    "BASELINE_EXPERIMENT_REGISTER_FAILED": (
        "기준선 실험 등록 실패(Baseline Experiment Register Failed)"
    ),
    "TIME_SYNC_OK": "시각 동기화 정상(Time Sync OK)",
    "TIME_SYNC_WARN": "시각 오차 경고(Time Sync Warning)",
    "TIME_SYNC_FALLBACK": "시각 동기화 서버 재시도(Time Sync Fallback)",
    "TIME_SYNC_ERROR": "시각 동기화 실패(Time Sync Error)",
    "TOKEN_REFRESHED": "KIS 토큰 갱신(Token Refreshed)",
    "TOKEN_LOADED_FROM_CACHE": "KIS 토큰 캐시 로드(Token Loaded From Cache)",
    "TOKEN_CACHE_WRITE_SKIPPED": "KIS 토큰 캐시 쓰기 생략(Token Cache Write Skipped)",
    "TOKEN_REFRESH_HTTP_ERR": "KIS 토큰 갱신 HTTP 오류(Token Refresh HTTP Error)",
    "TOKEN_REFRESH_ATTEMPT_FAIL": "KIS 토큰 갱신 재시도 실패(Token Refresh Attempt Failed)",
    "TOKEN_REFRESH_FAIL": "KIS 토큰 갱신 실패(Token Refresh Failed)",
    "TOKEN_EXPIRED": "KIS 토큰 만료(Token Expired)",
    "TOKEN_REVOKE_SKIP": "KIS 토큰 폐기 생략(Token Revoke Skipped)",
    "TOKEN_ISSUE_REFUSED_READONLY": "KIS 토큰 발급 거부(Token Issue Refused Readonly)",
    "TOKEN_REVOKE_REFUSED_DEV": "KIS 토큰 폐기 거부(Token Revoke Refused In Dev Tree)",
    "TOKEN_REVOKED": "KIS 토큰 폐기(Token Revoked)",
    "TOKEN_REVOKE_FAIL": "KIS 토큰 폐기 실패(Token Revoke Failed)",
    "TOKEN_REVOKE_ERR": "KIS 토큰 폐기 오류(Token Revoke Error)",
    "WS_KEY_REFRESHED": "웹소켓 키 갱신(WebSocket Key Refreshed)",
    "WS_KEY_HTTP_ERR": "웹소켓 키 HTTP 오류(WebSocket Key HTTP Error)",
    "WS_KEY_ATTEMPT_FAIL": "웹소켓 키 재시도 실패(WebSocket Key Attempt Failed)",
    "WS_KEY_REFRESH_FAIL": "웹소켓 키 갱신 실패(WebSocket Key Refresh Failed)",
    "LATENCY_HIGH": "API 지연 감지(High Latency)",
    "LATENCY_SUMMARY": "API 지연 집계(Latency Summary)",
    "RATE_LIMIT_HIT": "API 호출 제한 감지(Rate Limit Hit)",
    "RATE_LIMIT_RECOVERED": "API 호출 제한 복구(Rate Limit Recovered)",
    "TRANSIENT_ERROR_RETRY": "API 일시 오류 재시도(Transient Error Retry)",
    "KIS_REQUEST_FAILED": "KIS 요청 실패(KIS Request Failed)",
    "ASSET_SNAPSHOT_FAILED": "자산 조회 실패(Asset Snapshot Failed)",
    "ASSET_SNAPSHOT_SAVE_FAILED": "자산 스냅샷 저장 실패(Asset Snapshot Save Failed)",
    "ASSET_SNAPSHOT_LOAD_FAILED": "자산 스냅샷 로드 실패(Asset Snapshot Load Failed)",
    "API_ORDERS_FAILED": "주문 내역 조회 실패(API Orders Failed)",
    "API_HISTORY_FAILED": "거래 이력 조회 실패(API History Failed)",
    "API_STATS_FAILED": "통계 조회 실패(API Stats Failed)",
    "BALANCE_QUERY_ERROR": "잔고 조회 오류(Balance Query Error)",
    "BALANCE_QUERY_FAILED": "잔고 조회 실패·진입 차단(Balance Query Failed)",
    "ENTRY_DEADLINE_PASSED": "진입 마감 초과·주문 차단(Entry Deadline Passed)",
    "BALANCE_CASH_CHECK": "매수 가능 금액 확인(Balance Cash Check)",
    "BUYABLE_QTY_CHECK": "종목별 매수가능수량 확인(Buyable Quantity Check)",
    "BUYABLE_QTY_ERROR": "종목별 매수가능수량 조회 오류(Buyable Quantity Error)",
    "BUYABLE_QTY_QUERY_FAILED": "종목별 매수가능수량 조회 실패(Buyable Quantity Query Failed)",
    "GAP_RECHECK_UNAVAILABLE": "진입 전 갭 재검증 불가(Gap Recheck Unavailable)",
    "ENTRY_CANCEL_RELEASE_WAIT": "취소 후 증거금 해제 대기(Entry Cancel Release Wait)",
    "ENTRY_QTY_CLAMPED": "진입 주문 수량 조정(Entry Quantity Clamped)",
    "ENTRY_QTY_SIZED_AT_LIMIT": "지정가 기준 진입 수량 산정(Entry Quantity Sized at Limit)",
    "F1_DONE": "F1 필터 완료(F1 Done)",
    "F1_SKIPPED": "F1 필터 생략(F1 Skipped)",
    "F1_FETCH_START": "F1 API 조회중(F1 Fetching)",
    "F1_EXPECTED_QUOTE_START": "F1 예상체결 조회 시작(F1 Expected Quote Start)",
    "F1_EXPECTED_QUOTE_PROGRESS": "F1 예상체결 조회중(F1 Expected Quote Progress)",
    "F1_EXPECTED_QUOTE_DONE": "F1 예상체결 조회 완료(F1 Expected Quote Done)",
    "F1_API_ERROR": "F1 API 오류(F1 API Error)",
    "F1_FETCH_DONE": "F1 API 조회 완료(F1 Fetch Done)",
    "F1_MARKET_INTERVAL": "F1 시장 간 대기(F1 Market Interval)",
    "F1_FILTER_EMPTY": "F1 필터 결과 없음(F1 Filter Empty)",
    "F1_RETRY_WAIT": "F1 재시도 대기(F1 Retry Wait)",
    "F1_EXPECTED_QUOTE_ERROR": "F1 예상가 조회 오류(F1 Expected Quote Error)",
    "F1_CANDIDATE_PARSE_ERROR": "F1 후보 파싱 오류(F1 Candidate Parse Error)",
    "F1_EXPECTED_COMPARE": "F1 예상체결 비교(F1 Expected Compare)",
    "F1_SNAPSHOT_SAVED": "F1 후보 스냅샷 저장(F1 Snapshot Saved)",
    "F1_SNAPSHOT_SAVE_ERROR": "F1 후보 스냅샷 저장 오류(F1 Snapshot Save Error)",
    "F1_SNAPSHOT_ROTATE_ERROR": "F1 후보 스냅샷 정리 오류(F1 Snapshot Rotate Error)",
    "PAPER_FAST_PROBE_PREOPEN_START": (
        "PAPER Fast Path 장전 관측 시작(Paper Fast Probe Preopen Start)"
    ),
    "PAPER_FAST_PROBE_RANKING": "PAPER Fast Path 순위 관측(Paper Fast Probe Ranking)",
    "PAPER_FAST_PROBE_MULTI": "PAPER Fast Path 멀티시세 관측(Paper Fast Probe Multi)",
    "PAPER_FAST_PROBE_PREOPEN_DONE": (
        "PAPER Fast Path 장전 관측 완료(Paper Fast Probe Preopen Done)"
    ),
    "PAPER_FAST_PROBE_PREOPEN_SKIPPED": "PAPER Fast Path Preopen Skipped",
    "PAPER_FAST_PROBE_MARKET_CLOSED": (
        "PAPER Fast Path 휴장 감지(Paper Fast Probe Market Closed)"
    ),
    "PAPER_FAST_PROBE_OPEN_MULTI": (
        "PAPER Fast Path 개장 멀티시세 관측(Paper Fast Probe Open Multi)"
    ),
    "PAPER_FAST_PROBE_OPEN_DONE": "PAPER Fast Path 개장 관측 완료(Paper Fast Probe Open Done)",
    "PAPER_FAST_PROBE_OPEN_SKIPPED": (
        "PAPER Fast Path 개장 관측 생략(Paper Fast Probe Open Skipped)"
    ),
    "PAPER_FAST_PROBE_ERROR": "PAPER Fast Path 관측 오류(Paper Fast Probe Error)",
    "PAPER_FAST_SHADOW_COMPARE": "PAPER Fast Path 후보 비교(Paper Fast Shadow Compare)",
    "PAPER_FAST_SHADOW_COMPARE_ERROR": (
        "PAPER Fast Path 후보 비교 오류(Paper Fast Shadow Compare Error)"
    ),
    "PAPER_FAST_PATH_SELECTED": "PAPER Fast Path 진입 선택(Paper Fast Path Selected)",
    "PAPER_FAST_PATH_FALLBACK": "PAPER Fast Path 후보 부족 대체(Paper Fast Path Fallback)",
    "PAPER_FAST_PATH_MERGED": "PAPER Fast/Legacy 후보 병합(Paper Fast Path Merged)",
    "F1_FALLBACK_EMPTY": "F1 대체 조회 결과 없음(F1 Fallback Empty)",
    "ENTRY_PIPELINE_TIMING": "진입 파이프라인 소요시간(Entry Pipeline Timing)",
    "BALANCE_SNAPSHOT_READY": "개장 전 잔고 스냅샷 준비(Balance Snapshot Ready)",
    "BALANCE_SNAPSHOT_HIT": "잔고 스냅샷 사용(Balance Snapshot Hit)",
    "BALANCE_SNAPSHOT_MISS": "잔고 스냅샷 미사용(Balance Snapshot Miss)",
    "BALANCE_SNAPSHOT_ERROR": "잔고 스냅샷 준비 오류(Balance Snapshot Error)",
    "BALANCE_SNAPSHOT_SKIPPED": "잔고 스냅샷 준비 생략(Balance Snapshot Skipped)",
    "F3_FAST_RECHECK_USED": "F3 멀티시세 재검증 사용(F3 Fast Recheck Used)",
    "DEV_HELD_QUERY_FAILED": "개발 보유종목 조회 실패(Dev Held Query Failed)",
    "DEV_HELD_TICKERS_EXCLUDED": "개발 보유종목 제외(Dev Held Tickers Excluded)",
    "NO_TARGET": "대상 종목 없음(No Target)",
    "F2_SKIPPED": "F2 종목 잠금 생략(F2 Skipped)",
    "VI_FILTER_ALL_EXCLUDED": "VI 필터 전부 제외(VI Filter All Excluded)",
    "TARGET_LOCKED": "대상 종목 잠금(Target Locked)",
    "F3_SKIPPED": "F3 진입 생략(F3 Skipped)",
    "F3_RECHECK": "F3 진입 전 재검증(F3 Recheck)",
    "F3_RECHECK_BATCH_TIMING": "F3 재검증 묶음 소요시간(F3 Recheck Batch Timing)",
    "F3_FINAL_PICK": "F3 최종 종목 선정(F3 Final Pick)",
    "F3_ENTRY_BLOCKED": "F3 진입 차단(F3 Entry Blocked)",
    "F3_DEADLINE_PARSE_ERROR": "F3 마감시각 파싱 오류(F3 Deadline Parse Error)",
    "ENTRY_CASH_BASIS": "진입 수량 산정 기준 현금(Entry Cash Basis)",
    "ENTRY_ORDER_SENT": "진입 주문 전송(Entry Order Sent)",
    "ENTRY_PRE_ORDER_WAIT": "진입 주문 전 대기(Entry Pre-order Wait)",
    "ENTRY_RETRY_START": "진입 재시도 시작(Entry Retry Start)",
    "ENTRY_RETRY_SKIPPED": "진입 재시도 생략(Entry Retry Skipped)",
    "ENTRY_FILL_POLL_TIMEOUT": "진입 체결조회 시간초과(Entry Fill Poll Timeout)",
    "ENTRY_RETRY_EARLY_GAP_CHECK": "재진입 조기 갭 확인(Entry Retry Early Gap Check)",
    "ENTRY_BUDGET_EXCEEDED_SHADOW": "진입 시간예산 초과 관측(Entry Budget Exceeded Shadow)",
    "ENTRY_CANCEL_SENT": "진입 주문 취소 전송(Entry Cancel Sent)",
    "ENTRY_CANCEL_RETRY": "진입 주문 취소 재시도(Entry Cancel Retry)",
    "ENTRY_CANCEL_REJECTED_FILLED": "취소 거부 후 체결 확인(Entry Cancel Rejected But Filled)",
    "ENTRY_CANCEL_CONFIRM_ERROR": "진입 취소 최종 체결 확인 실패(Entry Cancel Confirm Error)",
    "ENTRY_CANCEL_UNCONFIRMED": "진입 주문 취소 확인 실패(Entry Cancel Unconfirmed)",
    "ENTRY_PENDING_PERSIST_ERROR": "진입 주문 복구정보 저장 실패(Entry Pending Persist Error)",
    "ENTRY_DB_DEGRADED": "진입 후 DB 감사기록 장애(Entry DB Degraded)",
    "ENTRY_PARTIAL_FILL": "진입 부분체결(Entry Partial Fill)",
    "ENTRY_FILL_RECONCILED": "진입 최종체결 대조(Entry Fill Reconciled)",
    "F3_FINAL_QUOTE": "F3 주문 직전 호가(F3 Final Quote)",
    "F3_FINAL_QUOTE_ERROR": "F3 주문 직전 호가 오류(F3 Final Quote Error)",
    "F3_RECHECK_QUOTE_FIELDS": "F3 재검증 원시 가격필드(F3 Recheck Quote Fields)",
    "ENTRY_PRICE_APPROVED": "진입 가격 상한 승인(Entry Price Approved)",
    "ENTRY_PRICE_BLOCKED": "진입 가격 상한 차단(Entry Price Blocked)",
    "ENTRY_QUOTE_MOVE_HIGH": "진입 직전 호가 급변(Entry Quote Move High)",
    "F3_ENV_REMOVED": "제거된 F3 설정 감지(F3 Removed Env Detected)",
    "F3_LIMIT_BUY_REQUIRED": "F3 지정가 매수 강제(F3 Limit Buy Required)",
    "FORCE_CATCHUP_BLOCKED": "강제 진입 캐치업 차단(Force Catch-up Blocked)",
    "PENDING_ENTRY_RECOVERED": "재시작 매수 주문 대조 완료(Pending Entry Recovered)",
    "PENDING_ENTRY_RECOVERY_FAILED": "재시작 매수 주문 대조 실패(Pending Entry Recovery Failed)",
    "GAP_CHANGED": "진입 전 갭 변동(Gap Changed)",
    "HIGH_GAP_AMOUNT_LOW": "고갭 유동성 부족 차단(High-Gap Liquidity Low)",
    "PENDING_ENTRY_LEGACY_AMOUNT_UNVERIFIED": (
        "구버전 매수복구 대금 미확인(Legacy Entry Amount Unverified)"
    ),
    "VI_ENTRY_BLOCKED": "진입 전 VI 차단(VI Entry Blocked)",
    "VI_ENTRY_WAIT_STARTED": "VI 해제 대기 시작(VI Release Wait Started)",
    "VI_ENTRY_RELEASED": "VI 해제 확인(VI Release Confirmed)",
    "VI_ENTRY_WAIT_TIMEOUT": "VI 해제 대기 종료(VI Release Wait Timeout)",
    "F3_VI_CHECK_ERROR": "F3 VI 조회 실패(F3 VI Check Error)",
    "INSUFFICIENT_BALANCE": "매수 가능 금액 부족(Insufficient Balance)",
    "TRADE_ALREADY_EXISTS": "당일 거래 이미 존재(Trade Already Exists)",
    "ENTRY_CANDIDATE_RETRY": "다음 후보로 진입 재시도(Entry Candidate Retry)",
    "ENTRY_CANDIDATE_RETRY_SKIPPED": "후보 재시도 마감 초과(Entry Candidate Retry Skipped)",
    "ENTRY_CANDIDATE_EXHAUSTED": "진입 후보 소진(Entry Candidate Exhausted)",
    "ENTRY_TERMINAL_PERSIST_ERROR": "종료 상태 저장 실패(Entry Terminal Persist Error)",
    "F3_RECHECK_QUOTE_RETRY": "F3 재검증 시세 재시도(F3 Recheck Quote Retry)",
    "F3_RECHECK_QUOTE_RECOVERED": "F3 재검증 시세 복구(F3 Recheck Quote Recovered)",
    "ENTRY_FAIL": "진입 실패(Entry Failed)",
    "MARKET_CLOSED": "휴장일 감지(Market Closed)",
    "MARKET_CLOSED_RESTORED": "휴장 상태 복원(Market Closed Restored)",
    "DAY_SKIP_RESTORED": "당일 스킵 상태 복원(Day Skip Restored)",
    "SLIPPAGE_GUARD": "슬리피지 가드 발동(Slippage Guard)",
    "ENTRY_EXECUTED": "진입 체결(Entry Executed)",
    "PYRAMID_TIMEOUT": "피라미딩 체결 시간 초과(Pyramid Timeout)",
    "PYRAMID_EXECUTED": "피라미딩 체결(Pyramid Executed)",
    "PYRAMID_SKIPPED": "피라미딩 생략(Pyramid Skipped)",
    "WS_CONNECTED": "웹소켓 연결(WebSocket Connected)",
    "WS_DISCONNECTED": "웹소켓 연결 끊김(WebSocket Disconnected)",
    "WS_STALE": "웹소켓 틱 지연(WebSocket Stale)",
    "WS_RECOVERED": "웹소켓 틱 복구(WebSocket Recovered)",
    "F4_HEARTBEAT": "F4 추적 생존 신호(F4 Heartbeat)",
    "F4_REST_BACKUP_START": "F4 REST 백업 시작(F4 REST Backup Start)",
    "F4_REST_BACKUP_ERROR": "F4 REST 백업 오류(F4 REST Backup Error)",
    "F4_FILL_UNCONFIRMED": "F4 체결 미확인(F4 Fill Unconfirmed)",
    "F4_CLOSE_PENDING": "F4 청산 확인 대기(F4 Close Pending)",
    "F4_CLOSE_ALREADY_IN_PROGRESS": "F4 청산 진행 중(F4 Close Already In Progress)",
    "F4_CLOSE_CANCEL_REQUESTED": "F4 청산 취소 요청 감지(F4 Close Cancel Requested)",
    "F4_CLOSE_TASK_CANCELLED": "F4 청산 태스크 취소(F4 Close Task Cancelled)",
    "F4_CLOSE_RECORD_ERROR": "F4 청산 기록 오류(F4 Close Record Error)",
    "F4_MONITOR_TASK_ERROR": "F4 모니터 태스크 오류(F4 Monitor Task Error)",
    "F4_RUN_FOREVER_ERROR": "F4 상주 루프 오류(F4 Resident Loop Error)",
    "VI_DETECTED": "VI 발동 감지(VI Detected)",
    "VI_RELEASED": "VI 해제 감지(VI Released)",
    "VI_CHECK_NEGATIVE": "VI 확인 결과 미발동(VI Check Negative)",
    "VI_CHECK_FAILED": "VI 확인 실패(VI Check Failed)",
    "VI_WATCH_ERROR": "VI 감시 오류(VI Watch Error)",
    "F4_STATE_PERSISTED": "F4 추적 상태 저장(F4 State Persisted)",
    "F4_STATE_PERSIST_ERROR": "F4 추적 상태 저장 오류(F4 State Persist Error)",
    "F4_POST_CLOSE_TRACKING_STOPPED": "매도 후 추적 종료(Post-close Tracking Stopped)",
    "F4_POST_CLOSE_TRACKING_STOP_REQUESTED": (
        "매도 후 추적 종료 요청(Post-close Tracking Stop Requested)"
    ),
    "F4_POST_CLOSE_TRACKING_STOP_PERSIST_ERROR": (
        "매도 후 추적 종료 저장 오류(Post-close Tracking Stop Persist Error)"
    ),
    "PAPER_FAST_SHADOW_PROGRESS": "PAPER Fast Shadow 검증 진행(PAPER Fast Shadow Progress)",
    "PAPER_FAST_SHADOW_PROGRESS_ERROR": (
        "PAPER Fast Shadow 검증 집계 오류(PAPER Fast Shadow Progress Error)"
    ),
    "F4_DB_PROGRESS_ERROR": "F4 DB 진행 상태 저장 오류(F4 DB Progress Error)",
    "F4_ENTRY_AT_INVALID": "F4 진입시각 오류(F4 Entry Time Invalid)",
    "TICK_SPIKE_DROPPED": "이상 틱 제외(Tick Spike Dropped)",
    "TICK_CAPTURE_STARTED": "가격경로 캡처 시작(Tick Capture Started)",
    "TICK_CAPTURE_START_ERROR": "가격경로 캡처 시작 오류(Tick Capture Start Error)",
    "TICK_CAPTURE_WRITE_ERROR": "가격경로 캡처 쓰기 오류(Tick Capture Write Error)",
    "TICK_CAPTURE_MANIFEST_ERROR": "가격경로 manifest 오류(Tick Capture Manifest Error)",
    "TICK_CAPTURE_FINALIZE_ERROR": "가격경로 캡처 종료 오류(Tick Capture Finalize Error)",
    "TICK_CAPTURE_FINALIZED": "가격경로 캡처 최종화(Tick Capture Finalized)",
    "TICK_CAPTURE_TARGET_SWITCHED": "가격경로 캡처 종목 전환(Tick Capture Target Switched)",
    "TICK_CAPTURE_SWITCH_DRAIN_TIMEOUT": (
        "가격경로 전환 마감 대기 초과(Tick Capture Switch Drain Timeout)"
    ),
    "TICK_CAPTURE_SWITCH_FINALIZE_SKIPPED": (
        "가격경로 전환 마감 생략(Tick Capture Switch Finalize Skipped)"
    ),
    "F4_SELL_ERROR": "F4 매도 오류(F4 Sell Error)",
    "F4_SELL_SUBMISSION_UNKNOWN": (
        "F4 매도 접수 여부 불명(F4 Sell Submission Unknown)"
    ),
    "EXIT_INTENT_PERSIST_FAILED": (
        "청산 의도 영속화 실패(Exit Intent Persist Failed)"
    ),
    "EXIT_ORDER_DB_DEGRADED": (
        "청산 주문 DB 감사기록 장애(Exit Order DB Degraded)"
    ),
    "EXIT_ORDER_EXCLUSION_QUERY_FAILED": (
        "이전 매도 주문 제외목록 조회 실패(Exit Order Exclusion Query Failed)"
    ),
    "EXIT_ORDER_RESPONSE_RECOVERED": (
        "유실된 매도 응답 대사 완료(Exit Order Response Recovered)"
    ),
    "EXIT_ORDER_SUBMISSION_UNKNOWN": (
        "매도 주문 접수 여부 불명(Exit Order Submission Unknown)"
    ),
    "EXIT_ORDER_RECONCILE_QUERY_FAILED": (
        "매도 주문 대사 조회 실패(Exit Order Reconcile Query Failed)"
    ),
    "EXIT_ORDER_RECOVERY_CLOSED": (
        "재시작 청산 체결 복구 완료(Exit Order Recovery Closed)"
    ),
    "EXIT_ORDER_RECOVERY_PENDING": (
        "재시작 청산 대기 유지(Exit Order Recovery Pending)"
    ),
    "TRAILING_STOP": "트레일링 스탑 청산(Trailing Stop)",
    "HARD_STOP": "하드 스탑 청산(Hard Stop)",
    "F5_PRECHECK": "F5 청산 사전 확인(F5 Precheck)",
    "F5_PRECHECK_FAIL": "F5 청산 사전 확인 실패(F5 Precheck Failed)",
    "TIMEOUT_CLOSE": "타임아웃 청산(Timeout Close)",
    "TIMEOUT_RETRY": "타임아웃 청산 재시도(Timeout Retry)",
    "TIMEOUT_ORDER_FAILED": "타임아웃 청산 주문 실패(Timeout Order Failed)",
    "TIMEOUT_ORDER_STATUS_UNKNOWN": (
        "타임아웃 매도 상태 불명(Timeout Order Status Unknown)"
    ),
    "TIMEOUT_ORDER_SUBMISSION_UNKNOWN": (
        "타임아웃 매도 접수 여부 불명(Timeout Order Submission Unknown)"
    ),
    "TIMEOUT_STATE_CLOSE_FAILED": "타임아웃 상태 종료 실패(Timeout State Close Failed)",
    "REAL_START_BLOCKED": "실전 시작 준비도 미달(Real Start Blocked)",
    "REAL_ENTRY_BLOCKED": "실전 매수 준비도 차단(Real Entry Blocked)",
    "REAL_SMOKE_BUY_AUTHORIZED": (
        "실전 스모크 매수 승인(Real Smoke Buy Authorized)"
    ),
    "REAL_READINESS_FAILED": "실전 준비도 계산 실패(Real Readiness Failed)",
    "NOTIFICATION_FAILED": "알림 전송 실패(Notification Failed)",
    "ORDER_SMOKE_BUY_START": "주문 테스트 매수 시작(Order Smoke Buy Start)",
    "ORDER_SMOKE_BUY_FILLED": "주문 테스트 매수 체결(Order Smoke Buy Filled)",
    "ORDER_SMOKE_SELL_START": "주문 테스트 매도 시작(Order Smoke Sell Start)",
    "ORDER_SMOKE_SELL_FILLED": "주문 테스트 매도 체결(Order Smoke Sell Filled)",
}


class _JsonLinesHandler(logging.Handler):
    def __init__(self, log_dir: str) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra: dict = getattr(record, "_extra", {})
            entry = {
                "ts": datetime.now(KST).isoformat(),
                "event": extra.get("event", record.getMessage()),
                "event_label": extra.get("event_label"),
                "level": extra.get("level", record.levelname),
                "ticker": extra.get("ticker"),
                **{
                    k: v
                    for k, v in extra.items()
                    if k not in ("event", "event_label", "ticker", "level")
                },
            }
            date_str = datetime.now(KST).strftime("%Y%m%d")
            log_file = self._log_dir / f"{date_str}.jsonl"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)


def setup(log_dir: str) -> logging.Logger:
    global _logger
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    _logger = logging.getLogger("stock")
    _logger.setLevel(logging.DEBUG)
    if not _logger.handlers:
        _logger.addHandler(_JsonLinesHandler(log_dir))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        _logger.addHandler(ch)
    return _logger


_LEVEL_TO_PYTHON = {
    "info": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "ERROR",
    "crit": "ERROR",
}
_LEVEL_NORMALIZED = {
    "debug": "info",
    "info": "info",
    "warn": "warn",
    "warning": "warn",
    "error": "error",
    "critical": "error",
    "crit": "error",
}


def normalize_level(level: str | None) -> str:
    key = str(level or "info").strip().lower()
    return _LEVEL_NORMALIZED.get(key, "info")


def event_label(event: str) -> str:
    return EVENT_LABELS.get(event, f"{event}({event})")


def _target_name_for(ticker: str | None) -> str | None:
    if not ticker:
        return None
    try:
        from src import state

        s = state.get()
        if ticker == s.target_ticker:
            return s.target_name
    except Exception:
        return None
    return None


def log(event: str, level: str = "info", ticker: str | None = None, **kwargs) -> None:
    logger = logging.getLogger("stock")
    normalized_level = normalize_level(level)
    py_level = _LEVEL_TO_PYTHON.get(normalized_level, "INFO")
    lvl = getattr(logging, py_level, logging.INFO)
    label = event_label(event)
    name = kwargs.pop("name", None) or _target_name_for(ticker)
    extra = {
        "event": event,
        "event_label": label,
        "ticker": ticker,
        "name": name,
        "level": normalized_level,
        **kwargs,
    }
    logger.log(lvl, label, extra={"_extra": extra})

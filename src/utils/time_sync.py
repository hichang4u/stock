import time

import ntplib

from src import live, notifier
from src.utils.logger import log

NTP_WARN_MS = 200.0
NTP_CRIT_MS = 500.0

# 같은 CRIT 알림을 다시 보내기까지의 최소 간격. 08:30 일일 점검(24시간 간격)은 시계가 틀린 채면
# 매일 알리고, 재시작이 잇따라 점검이 겹쳐도 한 번만 알린다. 벽시계가 틀린 상황을 다루므로
# time.monotonic()으로 잰다. 프로세스가 다시 뜨면 이 기억은 사라진다(재시작마다 한 번은 알린다).
# 정상(200ms 이하)으로 돌아오면 풀린다. WARN(200 ~ 500ms)은 풀지 않는다 — 오차가 500ms 언저리를
# 오갈 때 CRIT마다 다시 알리지 않게 한다.
CRIT_ALERT_COOLDOWN_SEC = 6 * 60 * 60.0
_crit_alerted_at: float | None = None


def _alert_crit(offset_ms: float, signed_ms: float) -> str:
    """CRIT 시각 오차를 텔레그램 알림 큐에 넣고, 로그에 적을 결과를 돌려준다.

    QUEUED(큐에 넣음) / SUPPRESSED(쿨다운 안이라 생략) / FILTERED(notifier가 거름) / FAILED(예외).
    큐에 넣기만 하므로 이벤트 루프를 막지 않는다. 전송 재시도와 실패 기록은 notifier 워커 몫이다.
    2026-11-13 08:30의 CRIT(1,953.6ms)은 로그만 남고 알림이 없었다
    (docs/CLOCK_DRIFT_FOLLOWUP_20261113.md).
    """
    global _crit_alerted_at
    now = time.monotonic()
    if _crit_alerted_at is not None and now - _crit_alerted_at < CRIT_ALERT_COOLDOWN_SEC:
        return "SUPPRESSED"
    direction = "늦다" if signed_ms > 0 else "빠르다"
    message = f"시스템 시각 오차 {offset_ms:.0f}ms(로컬 시계가 {direction}). 확인 필요."
    try:
        queued = notifier.send_nowait("TIME_SYNC_WARN", level="CRIT", message=message)
    except Exception as e:  # noqa: BLE001 — 알림 실패가 시각 점검을 흔들면 안 된다
        log("TIME_SYNC_ALERT_FAILED", level="WARN", error=repr(e))
        return "FAILED"
    if not queued:
        return "FILTERED"
    _crit_alerted_at = now
    return "QUEUED"


def check_ntp(servers: list[str]) -> float:
    """
    NTP 서버 목록을 순서대로 시도해 첫 번째 성공 서버의 오차(ms)를 반환한다.
    전체 실패 시 -1.0 반환.
    PRD §4: ±200ms 허용, 500ms 초과 시 CRIT 알림.

    `signed_offset_ms`는 NTP 서버 시각 − 로컬 시각이다. 양수면 로컬 시계가 늦다.
    """
    global _crit_alerted_at
    last_error: Exception | None = None

    for server in servers:
        try:
            resp = ntplib.NTPClient().request(server, version=3)
            signed_ms = resp.offset * 1000
            offset_ms = abs(signed_ms)

            live.ntp_offset_ms = round(offset_ms, 1)
            if offset_ms > NTP_CRIT_MS:
                live.ntp_level = "CRIT"
                alert = _alert_crit(offset_ms, signed_ms)
                log(
                    "TIME_SYNC_WARN", level="CRIT", ntp_server=server,
                    offset_ms=round(offset_ms, 1), signed_offset_ms=round(signed_ms, 1),
                    alert=alert,
                )
            elif offset_ms > NTP_WARN_MS:
                live.ntp_level = "WARN"
                log(
                    "TIME_SYNC_WARN", level="WARN", ntp_server=server,
                    offset_ms=round(offset_ms, 1), signed_offset_ms=round(signed_ms, 1),
                )
            else:
                live.ntp_level = "OK"
                _crit_alerted_at = None
                log(
                    "TIME_SYNC_OK", level="INFO", ntp_server=server,
                    offset_ms=round(offset_ms, 1), signed_offset_ms=round(signed_ms, 1),
                )

            return offset_ms
        except Exception as e:
            log("TIME_SYNC_FALLBACK", level="WARN", ntp_server=server, error=str(e))
            last_error = e

    live.ntp_offset_ms = -1.0
    live.ntp_level = "ERROR"
    log("TIME_SYNC_ERROR", level="CRIT", ntp_servers=servers, error=str(last_error))
    return -1.0

"""스케줄 시각 상수 — 단일 출처.

scheduler(잡 등록)·main(catchup)·api/server(UI 표시)가 공유한다.
apscheduler 의존이 없는 순수 모듈이므로 어디서든 안전하게 import 가능
(scheduler.py를 직접 import하면 apscheduler가 테스트 경로에 끌려온다).
"""

import os


def _hhmmss(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """`HH:MM:SS` env 오버라이드. 형식이 틀리면 기본값을 쓴다.

    개발 트리를 운영의 진입 창(09:00~09:11) 밖으로 밀어내기 위한 것이다.
    잘못된 값에 fail-closed 하면 그날 스케줄이 통째로 사라지므로,
    휴장 판정과 같은 fail-safe 원칙을 따른다 — 거래일을 잃는 쪽이 더 비싸다.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    parts = raw.split(":")
    if len(parts) != 3:
        return default
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return default
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return default
    return h, m, s


F1_H, F1_M, _F1_S = _hhmmss("SCHEDULE_F1", (9, 0, 0))
PAPER_FAST_PROBE_H, PAPER_FAST_PROBE_M, PAPER_FAST_PROBE_S = 8, 59, 45
BALANCE_PREFETCH_H, BALANCE_PREFETCH_M, BALANCE_PREFETCH_S = 8, 59, 50
F2_H, F2_M, _F2_S = _hhmmss("SCHEDULE_F2", (9, 10, 0))
F3_H, F3_M, F3_S = _hhmmss("SCHEDULE_F3", (9, 10, 10))
# F3_FILL_DEADLINE은 오버라이드하지 않는다 — 개발 트리 제외 규칙(f3_entry의
# _held_tickers_to_exclude)이 운영 기준선에 기대기 때문에, 이 값이 움직이면
# 그 규칙의 전제가 깨진다.
F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M = 9, 11
# 청산은 KRX 연속매매 구간(~15:20) 안에서 끝나야 한다. 15:20부터는 장마감
# 동시호가라 시장가 매도가 즉시 체결되지 않고 15:30 단일가로 모이므로,
# F5의 체결 폴링·잔량 재주문(최대 3회)이 전부 미체결로 끝난다.
# 15:15 실행은 동시호가 시작까지 5분 여유를 남긴다.
F5_PRECHECK_H, F5_PRECHECK_M, F5_PRECHECK_S = 15, 14, 50
F5_EXEC_H, F5_EXEC_M, F5_EXEC_S = 15, 15, 0

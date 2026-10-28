"""성과 비용 모델과 트랙 A 구간 — 성과 보고서와 대시보드 '개선' 메뉴가 같은 값을 쓴다.

`scripts/performance.py`·`scripts/performance_report.py`에 있던 것을 옮겼다(2026-10-28,
docs/IMPROVE_MENU_FOLLOWUP_20261028.md). 관측·보고 전용이라 전략 지문 대상이 아니다.
"""

from __future__ import annotations

# 개선 계획 §2 초기 PAPER 비용·체결 가정. 연구 상수이며 요율의 단정이 아니다.
BASE_ROUND_TRIP_COST_PCT = 0.18
HARD_STOP_SLIPPAGE_PCT = 0.30
TRAILING_SLIPPAGE_PCT = 0.15
TIMEOUT_SLIPPAGE_PCT = 0.20
SLIPPAGE_BY_REASON = {
    "GAP_HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "TRAILING": TRAILING_SLIPPAGE_PCT,
    "TIMEOUT": TIMEOUT_SLIPPAGE_PCT,
    "DATA_END": TIMEOUT_SLIPPAGE_PCT,
}


def apply_costs(gross_pct: float, exit_reason: str) -> dict:
    net_cost = gross_pct - BASE_ROUND_TRIP_COST_PCT
    slip = SLIPPAGE_BY_REASON.get(exit_reason, TIMEOUT_SLIPPAGE_PCT)
    return {"net_cost_pct": net_cost, "net_conservative_pct": net_cost - slip}


# 구간 경계와 그때 바뀐 조건(설명). 실제 복수 변경은 구간 안 지문 개수로 자동 검사한다.
TRACK_A_SEGMENTS: list[dict] = [
    {"name": "초기 운영", "start": "20260702", "end": "20260806",
     "changed": ["(PAPER 표시 전, 지문 기록 없음)"]},
    {"name": "PAPER 레거시", "start": "20260807", "end": "20260910",
     "changed": ["실행 모드·지문 기록 시작, 진입 안전장치 강화 묶음"]},
    {"name": "빠른 경로", "start": "20260911", "end": "20261001",
     "changed": ["빠른 경로 하이브리드 활성(PAPER_FAST_HYBRID=1). 0914~1001은 WS 파서 결함 기간"]},
    {"name": "파서 수정 후", "start": "20261002", "end": "20261027",
     "changed": ["WS 파서 헤더 분할 수정", "상한가 잠김 예외"]},
    # release/20261027-1(10/27 장 마감 후 승격). 진입 티켓 8.12절.
    {"name": "첫 체결 대기", "start": "20261028", "end": None,
     "changed": ["개장 직후 주문의 2초 체결 확인 창을 그 종목의 첫 체결부터 센다"
                 "(F3_FIRST_TRADE_WAIT_UNTIL_SEC=45)"]},
]


def current_segment(segments: list[dict] | None = None) -> dict:
    """끝이 열린(end=None) 마지막 구간. 없으면 마지막 구간."""
    segs = TRACK_A_SEGMENTS if segments is None else segments
    open_ended = [s for s in segs if s.get("end") is None]
    return (open_ended or segs)[-1]

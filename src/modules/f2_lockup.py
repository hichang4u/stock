"""F2. 타겟 락업 엔진 (08:58:00 ~ 08:59:30) — PRD §F2"""

from src import notifier, state
from src.utils.logger import log

F2_MAX_TARGET_CANDIDATES = 3


async def run(candidates: list[dict]) -> None:
    """
    복합 정렬 → 상위 종목 타겟 락업.

    F1을 통과한 강한 모멘텀 후보를 정적 VI 가격과 가깝다는 이유로 다시
    제거하지 않는다. 실제 VI 발동 여부와 주문 가능 시점은 F3가 확인한다.
    candidates: F1 통과 종목 리스트.
    """
    s = state.get()
    if s.day_skip or not candidates:
        log("F2_SKIPPED", level="WARN",
            reason="DAY_SKIP" if s.day_skip else "NO_CANDIDATES")
        return

    # ── 복합 정렬 (내림차순): 1순위 f1_score(없는 레거시 후보는 예상 체결대금 폴백) ──
    sorted_list = sorted(
        candidates,
        key=lambda c: (
            "f1_score" in c,
            c.get("f1_score", 0.0),
            c.get("expected_amount", 0.0),
            c.get("buy_sell_ratio", 0.0),
        ),
        reverse=True,
    )

    # ── 락업 ─────────────────────────────────────────────────────────
    locked_candidates = sorted_list[:F2_MAX_TARGET_CANDIDATES]
    target = locked_candidates[0]
    s.target_ticker = target["ticker"]
    s.target_name = target.get("name")
    s.target_candidates = locked_candidates
    # 관측 종목은 그날 처음 잠긴 1순위로 고정한다(state.observe_ticker 참고).
    if not s.observe_ticker:
        s.observe_ticker = s.target_ticker

    log(
        "TARGET_LOCKED", level="INFO", ticker=s.target_ticker, name=s.target_name,
        target_count=len(locked_candidates),
        target_tickers=[c.get("ticker") for c in locked_candidates],
        target_names=[c.get("name") for c in locked_candidates],
        gap_pct=round(target.get("gap_pct", 0.0) * 100, 2),
        expected_price=target.get("expected_price"),
        expected_amount=target.get("expected_amount"),
        buy_sell_ratio=target.get("buy_sell_ratio"),
    )
    await notifier.send(
        "TARGET_LOCKED", level="INFO",
        message=(
            f"타겟 확정: {s.target_ticker}, "
            f"예상갭 {target.get('gap_pct', 0.0)*100:.1f}%"
        ),
    )

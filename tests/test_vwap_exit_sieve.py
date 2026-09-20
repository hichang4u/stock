"""VWAP 청산 체 — simulate_exit만 검사한다. DB·틱 파일은 읽지 않는다."""

from datetime import datetime

from scripts.vwap_exit_sieve import simulate_exit

KST = "+09:00"
ENTRY_AT = datetime.fromisoformat(f"2026-09-18T09:00:05{KST}")


def _tick(ts: str, price: float, qty: float = 100.0) -> dict:
    return {"source_ts": f"2026-09-18T{ts}{KST}", "price": price, "qty": qty, "source": "ws"}


def test_reproduction_variant_matches_f4_trailing_rule():
    # entry 10000. 10250(스텝 2.5%) 도달 후 10050(=entry*(1+0.025-0.02)) 이탈에서 청산.
    ticks = [
        _tick("09:00:03", 10000.0, 100000),  # 시가 (진입 전, VWAP 누적에만)
        _tick("09:00:10", 10100.0),
        _tick("09:01:00", 10260.0),
        _tick("09:02:00", 10100.0),
        _tick("09:03:00", 10040.0),
        _tick("09:04:00", 9000.0),
    ]
    pnl, reason, at = simulate_exit(
        ticks, entry_price=10000.0, entry_at=ENTRY_AT,
        grace_min=0, use_trail=True, use_vwap=False,
    )
    assert (reason, at) == ("TRAIL", "09:03")
    assert round(pnl, 2) == 0.40


def test_hard_stop_before_trailing_is_active():
    ticks = [_tick("09:00:10", 10000.0), _tick("09:01:00", 9790.0), _tick("09:02:00", 8000.0)]
    pnl, reason, at = simulate_exit(
        ticks, entry_price=10000.0, entry_at=ENTRY_AT,
        grace_min=0, use_trail=True, use_vwap=False,
    )
    assert (reason, at) == ("HARD", "09:01")
    assert round(pnl, 2) == -2.10


def test_vwap_exit_uses_previous_minute_close_and_respects_grace():
    # VWAP ≈ 10000 (시가 물량이 지배). 09:01 마감가 9950 < VWAP 이지만 유예 5분 안이라 무시,
    # 09:07 마감가 9960 < VWAP → 09:08 첫 틱에서 청산.
    ticks = [
        _tick("09:00:03", 10000.0, 1_000_000),
        _tick("09:01:30", 9950.0),
        _tick("09:02:00", 10010.0),
        _tick("09:07:30", 9960.0),
        _tick("09:08:00", 9970.0),
        _tick("09:09:00", 12000.0),
    ]
    pnl, reason, at = simulate_exit(
        ticks, entry_price=10000.0, entry_at=ENTRY_AT,
        grace_min=5, use_trail=False, use_vwap=True,
    )
    assert (reason, at) == ("VWAP", "09:08")
    assert round(pnl, 2) == -0.30


def test_vwap_buffer_prevents_shallow_dip_exit():
    ticks = [
        _tick("09:00:03", 10000.0, 1_000_000),
        _tick("09:07:30", 9990.0),   # VWAP 대비 -0.1%: 버퍼 0.3% 안
        _tick("09:08:00", 10005.0),
        _tick("15:15:01", 10100.0),
    ]
    pnl, reason, at = simulate_exit(
        ticks, entry_price=10000.0, entry_at=ENTRY_AT,
        grace_min=5, use_trail=False, use_vwap=True, vwap_buffer=0.003,
    )
    assert (reason, at) == ("TIMEOUT", "15:15")
    assert round(pnl, 2) == 1.00


def test_end_of_capture_without_signal_is_eod():
    ticks = [_tick("09:00:10", 10000.0), _tick("09:30:00", 10100.0)]
    pnl, reason, at = simulate_exit(
        ticks, entry_price=10000.0, entry_at=ENTRY_AT,
        grace_min=5, use_trail=False, use_vwap=True,
    )
    assert (reason, at) == ("EOD", "09:30")
    assert round(pnl, 2) == 1.00

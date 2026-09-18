"""트레일 near-miss 진단 — 순수 함수만 검사한다. DB·틱 파일은 읽지 않는다."""

from datetime import datetime

from scripts.trail_near_miss import measure_episode, measure_post_hit, stop_lines

KST = "+09:00"


def _tick(ts: str, price: float) -> dict:
    return {"source_ts": f"2026-09-18T{ts}{KST}", "price": price, "source": "ws"}


def test_stop_lines_match_f4_formula():
    # 20260918 성호전자: entry 22002, step 5% → 1.5% 선 22772.07, 2.0% 선 22662.06
    base, rec, nxt = stop_lines(22002.0, 0.05)
    assert round(base, 2) == 22772.07
    assert round(rec, 2) == 22662.06
    assert round(nxt, 2) == 23652.15


def test_escape_reports_margin_above_recommended_line():
    ticks = [
        _tick("09:09:42", 22750.0),  # 1.5% 선 터치, 2.0% 선(22662) 위에서 멈춤
        _tick("09:10:00", 22900.0),
        _tick("09:11:59", 23700.0),  # 다음 스텝(23652) 도달
        _tick("09:12:00", 22000.0),  # 이후는 보지 않는다
    ]
    ep = measure_episode(
        ticks, entry_price=22002.0, highest_step=0.05,
        touch_at=datetime.fromisoformat(f"2026-09-18T09:09:42{KST}"),
    )
    assert ep["outcome"] == "ESCAPE"
    assert ep["low_after_touch"] == 22750.0
    assert round(ep["margin_to_recommended_pct"], 2) == 0.39
    assert ep["elapsed_sec"] == 137.0


def test_hit_stops_at_first_tick_through_recommended_line():
    ticks = [
        _tick("09:01:11", 10940.0),  # 1.5% 선 10962 터치, 2.0% 선 10907 위
        _tick("09:01:12", 10950.0),
        _tick("09:01:43", 10900.0),  # 2.0% 선 10907 이탈
        _tick("09:01:44", 12000.0),  # 이탈 뒤는 에피소드에 안 들어간다
    ]
    ep = measure_episode(
        ticks, entry_price=10853.0, highest_step=0.025,
        touch_at=datetime.fromisoformat(f"2026-09-18T09:01:11{KST}"),
    )
    assert ep["outcome"] == "HIT"
    assert ep["end_ts"].endswith("09:01:43+09:00")
    assert ep["low_after_touch"] == 10900.0
    assert ep["margin_to_recommended_pct"] < 0
    assert round(ep["bounce_over_baseline_pct"], 2) == round((10950 - 10961.53) / 10961.53 * 100, 2)
    assert ep["elapsed_sec"] == 32.0


def test_open_when_capture_ends_before_either_line():
    ticks = [_tick("09:09:42", 22750.0), _tick("09:09:43", 22800.0)]
    ep = measure_episode(
        ticks, entry_price=22002.0, highest_step=0.05,
        touch_at=datetime.fromisoformat(f"2026-09-18T09:09:42{KST}"),
    )
    assert ep["outcome"] == "OPEN"
    assert ep["end_ts"] is None and ep["elapsed_sec"] is None


def test_none_when_no_ticks_after_touch():
    ticks = [_tick("09:00:00", 22000.0)]
    assert measure_episode(
        ticks, entry_price=22002.0, highest_step=0.05,
        touch_at=datetime.fromisoformat(f"2026-09-18T09:09:42{KST}"),
    ) is None


def test_post_hit_window_excludes_ticks_outside_it():
    ticks = [
        _tick("09:01:43", 10900.0),  # 이탈 틱
        _tick("09:05:00", 10080.0),
        _tick("09:20:00", 11500.0),  # 다음 스텝(11396) 회복
        _tick("09:40:00", 20000.0),  # 30분 밖
    ]
    post = measure_post_hit(
        ticks, entry_price=10853.0, highest_step=0.025,
        hit_ts=f"2026-09-18T09:01:43{KST}",
    )
    assert post["reached_next_step"] is True
    assert round(post["high_pct_vs_line"], 2) == round((11500 - 10907.265) / 10907.265 * 100, 2)
    assert post["low_pct_vs_line"] < 0

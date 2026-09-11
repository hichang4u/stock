"""트랙 B 규칙 후보의 순수 함수 검증.

청산은 트랙 A와 같은 한 벌(하드스탑·스텝 트레일링·15:15)이어야 진입 시각만
비교된다. 봉은 내부 경로를 모르므로 고가·저가 순서를 양쪽으로 돌리고, 답이
갈리는 날은 AMBIGUOUS로 판정에서 뺀다 — STRATEGY_BACKTEST_20260820.md 관례.
"""

import pytest

from scripts import track_b_rules
from scripts.track_b_rules import (
    DEFAULT_PARAMS,
    HARD_STOP,
    RULES,
    STEP_SIZE,
    STEP_TRAIL,
    build_context,
    r1_high_reclaim,
    r2_vwap_reclaim,
    r3_indicator,
    r4_pullback,
    r5_pullback_breakout,
    resolve_exit,
    simulate_exit,
)


def _bar(time_: str, *, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "date": "20260820", "time": time_,
        "open": open_, "high": high, "low": low, "close": close, "volume": 1000.0,
    }


def test_hard_stop_before_trailing_activates():
    bars = [
        _bar("093600", open_=100, high=101, low=100, close=100),
        _bar("093700", open_=100, high=100, low=97.9, close=98),
    ]
    result = simulate_exit(bars, 0, 100.0, order="low_first")
    assert result["reason"] == "HARD_STOP"
    assert result["exit_price"] == pytest.approx(100.0 * (1 - HARD_STOP))
    assert result["pct"] == pytest.approx(-HARD_STOP * 100)


def test_trailing_stop_uses_highest_step_not_high_price():
    """스텝 +2.5% 도달 후 청산선은 진입가*(1+0.025-0.020)이다. 고가가 아니다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=104, low=103, close=103),
        _bar("093800", open_=103, high=103, low=100.4, close=100.4),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    assert result["reason"] == "TRAILING"
    assert result["exit_price"] == pytest.approx(100.0 * (1 + STEP_SIZE - STEP_TRAIL))
    assert result["exit_time"] == "093800"


def test_hard_stop_disarms_once_trailing_active():
    """A와 같다 — 트레일링이 켜지면 하드스탑은 더 이상 보지 않는다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=106, low=100, close=106),
        _bar("093800", open_=106, high=106, low=97, close=97),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    # 스텝 0.05 → 청산선 100*(1+0.05-0.02) = 103. 하드스탑 98이 아니다.
    assert result["reason"] == "TRAILING"
    assert result["exit_price"] == pytest.approx(103.0)


def test_timeout_closes_at_1515_close():
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("151500", open_=100, high=101, low=99.5, close=100.5),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    assert result["reason"] == "TIMEOUT"
    assert result["exit_price"] == pytest.approx(100.5)


def test_same_bar_touches_both_is_ambiguous():
    """같은 봉이 스텝과 손절에 모두 닿으면 순서에 따라 답이 갈린다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=104, low=97.5, close=98),
        _bar("151500", open_=98, high=98, low=98, close=98),
    ]
    resolved = resolve_exit(bars, 0, 100.0)
    assert resolved["ambiguous"] is True
    assert resolved["pct"] is None
    assert resolved["high_first"]["reason"] == "TRAILING"
    assert resolved["low_first"]["reason"] == "HARD_STOP"


def test_unambiguous_day_reports_single_pct():
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=100.5, low=99.8, close=100.2),
        _bar("151500", open_=100.2, high=100.3, low=100.1, close=100.3),
    ]
    resolved = resolve_exit(bars, 0, 100.0)
    assert resolved["ambiguous"] is False
    assert resolved["pct"] == pytest.approx(0.3)


def test_exit_measures_from_entry_bar():
    """진입 봉 이전의 저가로 손절 판정을 받으면 안 된다."""
    bars = [
        _bar("093500", open_=100, high=100, low=90, close=100),
        _bar("093600", open_=100, high=101, low=100, close=101),
        _bar("151500", open_=101, high=101, low=101, close=101),
    ]
    result = simulate_exit(bars, 1, 100.0, order="low_first")
    assert result["reason"] == "TIMEOUT"


def test_exit_constants_match_track_a():
    """A와 다른 값이 되면 '진입 시각만 다르다'는 전제가 깨진다."""
    from src.modules import f4_tracking

    assert STEP_SIZE == f4_tracking.STEP_SIZE
    assert STEP_TRAIL == f4_tracking.STEP_TRAIL
    assert HARD_STOP == f4_tracking.HARD_STOP_RATIO


def _series(closes: list[float], *, volumes: list[float] | None = None,
            start_min: int = 0) -> list[dict]:
    """09:00부터 1분씩. 고가·저가는 종가에 붙여 단순하게 둔다."""
    bars = []
    for i, c in enumerate(closes):
        minute = start_min + i
        hour, mm = 9 + minute // 60, minute % 60
        bars.append({
            "date": "20260820",
            "time": f"{hour:02d}{mm:02d}00",
            "open": c, "high": c, "low": c, "close": c,
            "volume": (volumes[i] if volumes else 1000.0),
        })
    return bars


def test_r1_fires_only_when_prior_high_is_reclaimed():
    bars = _series([100, 110, 105, 109, 111])
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r1_high_reclaim(bars, 3, ctx, DEFAULT_PARAMS) is False  # 109 < 110
    assert r1_high_reclaim(bars, 4, ctx, DEFAULT_PARAMS) is True   # 111 > 110


def test_r1_needs_no_parameters():
    """파라미터가 0개라는 것이 R1의 근거다. 값이 늘면 그 근거가 사라진다."""
    bars = _series([100, 110, 111])
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r1_high_reclaim(bars, 2, ctx, {}) is True


def test_r2_requires_crossing_and_volume_expansion():
    # VWAP(직전) 95.0 아래에서 90으로 닫혔다가 105로 올라선다.
    closes = [100, 98, 96, 94, 92, 90, 105]
    volumes = [1000] * 6 + [5000]
    bars = _series(closes, volumes=volumes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    # 마지막 봉에서 VWAP 위로 올라서고 거래량이 직전 5봉 평균을 넘는다.
    assert r2_vwap_reclaim(bars, 6, ctx, DEFAULT_PARAMS) is True


def test_r2_rejects_crossing_without_volume():
    closes = [100, 98, 96, 94, 92, 90, 105]
    volumes = [1000] * 7
    bars = _series(closes, volumes=volumes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r2_vwap_reclaim(bars, 6, ctx, DEFAULT_PARAMS) is False


def test_r3_waits_for_histogram_maturity():
    """히스토그램이 막 정의된 구간에서는 신호를 내지 않는다.

    전일 시드를 안 쓰므로 34번째 봉에서야 첫 값이 선다. 값 두세 개의 부호로
    판정하는 것이 v0의 결함이었다.
    """
    closes = [100 + (i % 7) - 3 for i in range(60)]
    bars = _series(closes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    first = ctx["first_hist_idx"]
    assert first is not None
    for i in range(first, first + DEFAULT_PARAMS["hist_maturity_bars"]):
        assert r3_indicator(bars, i, ctx, DEFAULT_PARAMS) is False


def test_rules_registry_is_closed_at_four_axes():
    """등록은 닫혀 있다. 축을 추가하면 그리드 서치가 된다 (스펙 §4.2).

    R4는 이 금지의 예외가 아니라 **새 사전 등록**이다. §4.2가 막는 것은 "결과를
    본 뒤 같은 표본에 축을 더하는 것"인데, R1~R3을 판정한 22거래일 표본은
    2026-09-03에 소멸했다(`68549c7`). R4는 그 표본에 얹히지 않는다 — 판정은
    백필로 새로 쌓는 표본에서만 하고, 관문은 돌리기 전에 선언한다.

    R5는 위 조건을 **충족하지 못한다** — R1~R4를 판정한 48거래일 표본(2026-07-01
    ~09-10)이 살아 있는 채로 2026-09-11에 추가됐다. 그래서 R5의 이 표본 결과는
    판정이 아니라 **체(sieve)** 로만 쓴다: 지면 닫히고, 이기면 H1처럼 사전 등록
    가설이 되어 등록 이후 표본에서만 판정한다. 관문은 돌리기 전에 선언했다.
    """
    assert sorted(RULES) == ["R1", "R2", "R3", "R4", "R5"]


def test_gap_block_suppresses_signals_after_missing_minutes():
    bars = _series([100, 101, 102])
    # 09:02 다음이 09:06 — 3분이 빠졌다.
    bars.append({"date": "20260820", "time": "090600", "open": 120,
                 "high": 120, "low": 120, "close": 120, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "090700", "open": 121,
                 "high": 121, "low": 121, "close": 121, "volume": 1000.0})
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert ctx["gap_block"][3] is True
    assert ctx["gap_block"][4] is True   # min_bars_after_gap=2
    assert ctx["gap_block"][2] is False


def test_r3_fires_when_conditions_met():
    """r3_indicator의 발화 경로를 고정한다.

    히스토그램이 성숙한 뒤, 종가 > SMA이고 히스토그램이 음→양으로 꺾인
    봉에서 True를 돌려야 한다. 조건이 실제로 성립하는 인덱스를 찾아
    검증한다.
    """
    # 하락 후 상승으로 꺾이는 계열로 MACD 히스토그램의 부호 전환을 자연스럽게 유도
    closes = [100 + (i % 7) - 3 for i in range(70)]
    bars = _series(closes)
    ctx = build_context(bars, DEFAULT_PARAMS)

    first = ctx["first_hist_idx"]
    assert first is not None
    maturity = DEFAULT_PARAMS["hist_maturity_bars"]

    # 성숙 구간 이후에서 조건을 만족하는 인덱스를 찾는다
    fire_idx = None
    for i in range(first + maturity, len(bars)):
        sma_val = ctx["sma"][i]
        hist_now = ctx["macd"][i]["hist"]
        hist_prev = ctx["macd"][i - 1]["hist"]

        if (sma_val is not None and hist_now is not None and hist_prev is not None and
            bars[i]["close"] > sma_val and hist_prev < 0 <= hist_now):
            fire_idx = i
            break

    # 조건을 만족하는 인덱스가 존재해야 함
    assert fire_idx is not None, "Could not find bar where r3_indicator conditions are met"

    # 그 인덱스에서 r3_indicator가 True를 돌려야 함
    assert r3_indicator(bars, fire_idx, ctx, DEFAULT_PARAMS) is True


def _seq_bars(closes, start_hhmm=900, volume=100.0):
    """분 단위로 이어지는 봉. close만 의미가 있다."""
    out = []
    hh, mm = divmod(start_hhmm, 100)
    for c in closes:
        out.append({
            "time": f"{hh:02d}{mm:02d}00",
            "open": float(c), "high": float(c), "low": float(c), "close": float(c),
            "volume": volume,
        })
        mm += 1
        if mm == 60:
            mm = 0
            hh += 1
    return out


def test_warmup_defines_macd_from_the_first_bar_of_the_day():
    """워밍업이 없으면 당일 초반 MACD는 None이다. 한 세션치를 붙이면 첫 봉부터 값이 선다."""
    from src import warmup as warmup_mod

    # 한 세션치 — 09:00부터 이어 붙이면 15:20까지 닿아 개장~마감을 덮는다.
    warm = _seq_bars([1000 + i for i in range(381)])
    day = _seq_bars([1060 + i for i in range(5)], start_hhmm=1000)

    assert warmup_mod.covers_session(warm)

    cold = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
    hot = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS, warmup=warm)

    assert cold["macd"][0]["macd"] is None
    assert hot["macd"][0]["macd"] is not None
    assert len(hot["macd"]) == len(day)
    assert len(hot["sma"]) == len(day)


def test_warmup_below_threshold_is_not_used_at_all():
    """임계 미만 워밍업은 조용히 섞이지 않는다 — 옛 모드와 완전히 같아야 한다(스펙 §4.3 판정).

    28봉 같은 부분 워밍업이 옛 모드도 새 모드도 아닌 제3의 값을 만드는 것이
    이 항목이 막으려는 버그다.
    """
    warm = _seq_bars([1000 + i for i in range(60)])   # 09:00~09:59 — 마감에 닿지 않는다
    day = _seq_bars([1060 + i for i in range(5)], start_hhmm=1000)

    cold = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
    hot = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS, warmup=warm)

    assert hot == cold


def test_warmup_does_not_leak_into_session_accumulators():
    """VWAP·당일 고가·봉 간격은 세션 값이다 — 워밍업이 섞이면 안 된다."""
    warm = _seq_bars([9999.0] * 60)          # 당일보다 훨씬 높은 전일 고가
    day = _seq_bars([100.0, 110.0, 120.0], start_hhmm=1000)

    cold = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
    hot = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS, warmup=warm)

    assert hot["vwap"] == cold["vwap"]
    assert hot["run_high"] == cold["run_high"]
    assert hot["gap_block"] == cold["gap_block"]


def test_warmup_none_reproduces_the_old_context_exactly():
    """--warmup-days 0 의 회귀선. 기존 문서의 숫자가 재현 가능해야 한다."""
    day = _seq_bars([100 + i for i in range(40)])

    assert (track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
            == track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS,
                                           warmup=[]))


# ---------------------------------------------------------------------------
# R4 눌림목 — 지지 도달·거래량 급감·방어 캔들·반등 확인 넷을 봉으로만 판정한다.
# 가이드의 4단계(호가창 대량 매수 유입)는 봉에 없다. 분봉 API가 OHLCV만 주므로
# 백테스트에 영영 들어올 수 없고, 규칙에 넣으면 실시간과 백테스트가 다른 것을
# 본다. 그래서 "반등 확인 봉"으로 옮겼다.
# ---------------------------------------------------------------------------


def _ohlcv(time_: str, *, open_: float, high: float, low: float,
           close: float, volume: float) -> dict:
    return {
        "date": "20260820", "time": time_,
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    }


def _pullback_series(*, support: dict | None = None,
                     confirm: dict | None = None,
                     lead_high: float = 120.0) -> list[dict]:
    """선행 급등 → 평탄 구간(SMA20 = 100) → 지지 캔들 → 반등 확인 봉.

    평탄하게 두는 이유는 SMA20을 100.0으로 고정해 지지 터치를 정확히 겨냥하기
    위해서다. 분은 건너뛰지 않는다 — 건너뛰면 gap_block이 신호를 막는다.
    """
    bars = [_ohlcv("093500", open_=100, high=lead_high, low=100,
                   close=100, volume=1000.0)]
    for i in range(1, 22):
        minute = 35 + i
        bars.append(_ohlcv(f"{9 + minute // 60:02d}{minute % 60:02d}00",
                           open_=100, high=100, low=100, close=100, volume=1000.0))
    bars.append(support or _ohlcv("095700", open_=100.2, high=100.3, low=99.5,
                                  close=100.1, volume=300.0))
    bars.append(confirm or _ohlcv("095800", open_=100.1, high=100.6, low=100.1,
                                  close=100.5, volume=1500.0))
    return bars


def test_r4_fires_on_support_touch_dry_volume_defense_and_confirmation():
    """네 조건이 모두 성립한 반등 확인 봉에서 발화한다."""
    bars = _pullback_series()
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is True
    # 지지 캔들 자체에서는 아직 발화하지 않는다 — 확인 봉을 기다린다.
    assert r4_pullback(bars, 22, ctx, DEFAULT_PARAMS) is False


def test_r4_requires_confirmation_bar_to_clear_support_high():
    """확인 봉 종가가 지지 캔들 고가를 넘지 못하면 반등이 아니다."""
    bars = _pullback_series(
        confirm=_ohlcv("095800", open_=100.1, high=100.29, low=100.0,
                       close=100.2, volume=1500.0),
    )
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is False


def test_r4_requires_volume_to_dry_up():
    """매도세 소진이 전제다. 거래량이 안 마르면 눌림목이 아니라 하락이다."""
    bars = _pullback_series(
        support=_ohlcv("095700", open_=100.2, high=100.3, low=99.5,
                       close=100.1, volume=900.0),
    )
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is False


def test_r4_requires_defensive_candle():
    """아래꼬리도 도지도 아닌 봉은 방어가 아니다."""
    bars = _pullback_series(
        support=_ohlcv("095700", open_=100.3, high=100.35, low=100.0,
                       close=100.05, volume=300.0),
    )
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is False


def test_r4_accepts_doji_as_defense():
    """몸통이 없는 십자 캔들도 방어로 친다 — 아래꼬리 조건과 OR다."""
    bars = _pullback_series(
        support=_ohlcv("095700", open_=100.0, high=100.4, low=99.9,
                       close=100.02, volume=300.0),
        confirm=_ohlcv("095800", open_=100.02, high=100.7, low=100.0,
                       close=100.6, volume=1500.0),
    )
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is True


def test_r4_requires_prior_run_up():
    """급등이 없었으면 눌림목도 없다. 그냥 흘러내리는 종목과 구분한다."""
    bars = _pullback_series(lead_high=100.0)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is False


def test_r4_requires_price_to_reach_support():
    """지지선 근처까지 내려오지 않았으면 판정하지 않는다 — 예측 매수 금지."""
    bars = _pullback_series(
        support=_ohlcv("095700", open_=101.5, high=101.6, low=101.0,
                       close=101.4, volume=300.0),
        confirm=_ohlcv("095800", open_=101.4, high=101.9, low=101.3,
                       close=101.8, volume=1500.0),
    )
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r4_pullback(bars, 23, ctx, DEFAULT_PARAMS) is False


# ── R5 눌림목+돌파 ─────────────────────────────────────────────────

def _pullback_breakout_series(*, breakout_close: float = 120.5,
                              pull_low: float = 99.8,
                              pull_volume: float = 300.0,
                              lead_high: float = 120.0,
                              slope: float = 0.0) -> list[dict]:
    """급등 봉(고점 120) → 평탄 구간 21봉(SMA20 = 100) → 눌림 5봉(지지 터치,
    거래량 급감) → 돌파 봉(인덱스 27).

    ``slope`` > 0 이면 평탄 구간이 완만히 오른다 — 평탄한 선은 자기 SMA 위에
    놓여 항상 "지지 터치"가 되므로, 터치가 없는 경우를 만들려면 SMA가 가격
    아래로 처지게 해야 한다.
    """
    bars = [_ohlcv("093500", open_=100, high=lead_high, low=100,
                   close=100, volume=1000.0)]
    for i in range(1, 22):
        minute = 35 + i
        px = 100 + slope * i
        bars.append(_ohlcv(f"{9 + minute // 60:02d}{minute % 60:02d}00",
                           open_=px, high=px, low=px, close=px, volume=1000.0))
    base = 100 + slope * 21
    for k in range(5):
        minute = 57 + k
        bars.append(_ohlcv(f"{9 + minute // 60:02d}{minute % 60:02d}00",
                           open_=base + 0.2, high=base + 0.4,
                           low=pull_low if k == 2 else base + 0.1,
                           close=base + 0.2, volume=pull_volume))
    bars.append(_ohlcv("100200", open_=base + 0.3, high=breakout_close + 0.2,
                       low=base + 0.2, close=breakout_close, volume=2000.0))
    return bars


def test_r5_fires_when_pullback_then_prior_high_is_broken():
    bars = _pullback_breakout_series()
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 27, ctx, DEFAULT_PARAMS) is True
    # 눌림 구간 자체에서는 발화하지 않는다 — 돌파 봉을 기다린다.
    assert r5_pullback_breakout(bars, 26, ctx, DEFAULT_PARAMS) is False


def test_r5_requires_close_above_prior_high_not_support_high():
    """R4와의 차이: 방어 캔들 고가(100.4)를 넘는 것으로는 부족하다."""
    bars = _pullback_breakout_series(breakout_close=101.0)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 27, ctx, DEFAULT_PARAMS) is False


def test_r5_requires_pullback_to_reach_support():
    """완만히 오르는 선(SMA20 ≈ 106)에서 눌림 저가 110 — 지지에 닿지 않았다."""
    bars = _pullback_breakout_series(slope=0.5, pull_low=110.0)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 27, ctx, DEFAULT_PARAMS) is False


def test_r5_requires_pullback_volume_to_dry_up():
    bars = _pullback_breakout_series(pull_volume=900.0)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 27, ctx, DEFAULT_PARAMS) is False


def test_r5_requires_prior_run_up():
    """급등이 없었으면 돌파할 고점도 없다 — R1 단독 돌파와 구분한다."""
    bars = _pullback_breakout_series(lead_high=101.0, breakout_close=101.5)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 27, ctx, DEFAULT_PARAMS) is False


def test_r5_does_not_fire_without_a_pullback_between_peak_and_breakout():
    """고점 바로 다음 봉이 고점을 넘는 것은 눌림목이 아니라 연속 상승이다."""
    bars = [_ohlcv("093500", open_=100, high=120, low=100, close=100, volume=1000.0),
            _ohlcv("093600", open_=100, high=121, low=100, close=120.5, volume=1000.0)]
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r5_pullback_breakout(bars, 1, ctx, DEFAULT_PARAMS) is False

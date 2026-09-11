"""트랙 B 규칙 후보 — 순수 함수만 둔다.

진입 축 다섯 개와 청산 한 벌이 들어 있다. I/O도 상태도 없어서 실시간 신호
엔진(2단계)과 백테스트가 같은 코드를 탈 수 있다.

청산은 트랙 A와 같다 — 하드스탑 -2.0%, 스텝 트레일링 +2.5%/-2.0%, 15:15.
B가 A와 다른 점을 진입 시각 하나로 줄여야 비교가 통제된다.
"""

from __future__ import annotations

import math

from src import indicators
from src import warmup as warmup_mod  # noqa: E402

# 트랙 A와 같은 값. f4_tracking.STEP_SIZE / STEP_TRAIL / HARD_STOP_RATIO 와 일치한다.
STEP_SIZE = 0.025
STEP_TRAIL = 0.020
HARD_STOP = 0.020
TIMEOUT_TIME = "151500"


def _step_of(pnl: float) -> float:
    return max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)


def simulate_exit(
    bars: list[dict],
    entry_idx: int,
    entry_price: float,
    *,
    order: str,
) -> dict:
    """진입 봉부터 청산까지 봉으로 훑는다.

    봉 안의 가격 경로는 모른다. ``order`` 로 고가·저가 중 어느 쪽을 먼저 본
    것으로 가정할지 정하고, 호출부가 양쪽을 돌려 답이 갈리는지 본다.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive: {entry_price}")
    highest_step = 0.0
    trailing_active = False

    # bars.index(bar) 를 쓰지 않는다 — 값이 같은 봉이 둘이면 앞의 것을 돌려준다.
    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        if bar["time"] >= TIMEOUT_TIME:
            return {
                "exit_idx": idx, "exit_time": bar["time"],
                "exit_price": bar["close"], "reason": "TIMEOUT",
                "pct": (bar["close"] / entry_price - 1) * 100,
            }
        prices = (
            [bar["high"], bar["low"]] if order == "high_first"
            else [bar["low"], bar["high"]]
        )
        for price in prices:
            step = _step_of(price / entry_price - 1)
            if step > highest_step:
                highest_step = step
            if highest_step >= STEP_SIZE:
                trailing_active = True

            if not trailing_active and price <= entry_price * (1 - HARD_STOP):
                stop = entry_price * (1 - HARD_STOP)
                return {
                    "exit_idx": idx, "exit_time": bar["time"],
                    "exit_price": stop, "reason": "HARD_STOP",
                    "pct": -HARD_STOP * 100,
                }
            if trailing_active:
                stop = entry_price * (1 + highest_step - STEP_TRAIL)
                if price <= stop:
                    return {
                        "exit_idx": idx, "exit_time": bar["time"],
                        "exit_price": stop, "reason": "TRAILING",
                        "pct": (stop / entry_price - 1) * 100,
                    }

    last = bars[-1]
    return {
        "exit_idx": len(bars) - 1, "exit_time": last["time"],
        "exit_price": last["close"], "reason": "DATA_END",
        "pct": (last["close"] / entry_price - 1) * 100,
    }


def resolve_exit(bars: list[dict], entry_idx: int, entry_price: float) -> dict:
    """양쪽 순서로 돌려 답이 갈리면 AMBIGUOUS.

    갈린 날을 한쪽 답으로 적으면 봉 내부를 안다고 주장하는 것이 된다. 판정에서
    빼되 몇 건이 빠졌는지는 항상 보고한다 — 많이 빠지면 결론 자체가 약하다.
    """
    high_first = simulate_exit(bars, entry_idx, entry_price, order="high_first")
    low_first = simulate_exit(bars, entry_idx, entry_price, order="low_first")
    ambiguous = (
        high_first["reason"] != low_first["reason"]
        or high_first["exit_time"] != low_first["exit_time"]
    )
    return {
        "ambiguous": ambiguous,
        "high_first": high_first,
        "low_first": low_first,
        "pct": None if ambiguous else high_first["pct"],
    }


DEFAULT_PARAMS = {
    "sma_period": 20,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "vol_window": 5,
    # 히스토그램이 정의된 뒤 이만큼 지나야 판정한다. v0가 값 2~3개로 부호를
    # 판정하던 것을 막는다.
    "hist_maturity_bars": 3,
    # 봉이 빠진 직후 이만큼은 신호를 내지 않는다 (그림자 스펙 §15.3).
    "min_bars_after_gap": 2,
    # R4 눌림목. 값은 돌리기 전에 고정하고 결과를 본 뒤 바꾸지 않는다 (설계 §5.6).
    "pullback_prior_run": 0.03,     # 눌리기 전 최소 상승폭
    "pullback_touch_pct": 0.002,    # 지지선 근접 허용 오차
    "pullback_vol_window": 5,
    "pullback_vol_dry": 0.6,        # 지지 캔들 거래량 / 직전 평균
    "pullback_tail_ratio": 0.5,     # 아래꼬리 / 봉 전체 범위
    "pullback_doji_body": 0.1,      # 몸통 / 봉 전체 범위
}


def _minutes(time_: str) -> int:
    return int(time_[:2]) * 60 + int(time_[2:4])


def build_context(
    bars: list[dict], params: dict, warmup: list[dict] | None = None
) -> dict:
    """하루치 파생 계열을 한 번만 계산한다.

    지표는 반드시 운영 코드(src.indicators)를 쓴다. 여기서 다시 구현하면
    백테스트와 실시간이 다른 값을 보게 된다.

    ``warmup``은 전 거래일 봉이다. **SMA·MACD에만 먹인다** — VWAP은 정의상
    세션 누적이고, ``run_high``는 "그날 직전까지의 고가"이며, ``gap_block``은
    15:30→09:00 경계를 거대한 봉 간격으로 읽는다. 셋 중 하나라도 전일을
    섞으면 R1·R2가 조용히 다른 규칙이 된다.
    """
    period = params.get("sma_period", DEFAULT_PARAMS["sma_period"])
    warmed_bars, offset = warmup_mod.combine(warmup_mod.usable(warmup or []), bars)

    macd_rows = indicators.macd(
        warmed_bars,
        params.get("macd_fast", DEFAULT_PARAMS["macd_fast"]),
        params.get("macd_slow", DEFAULT_PARAMS["macd_slow"]),
        params.get("macd_signal", DEFAULT_PARAMS["macd_signal"]),
    )[offset:]
    sma_rows = indicators.sma(warmed_bars, period)[offset:]

    run_high: list[float] = []
    vwap: list[float | None] = []  # 거래량 0인 선두 봉은 VWAP이 없다
    cum_pv = 0.0
    cum_v = 0.0
    highest = float("-inf")
    for bar in bars:
        run_high.append(highest)          # 직전 봉까지의 고가
        highest = max(highest, bar["high"])
        cum_pv += bar["close"] * bar["volume"]
        cum_v += bar["volume"]
        vwap.append(cum_pv / cum_v if cum_v > 0 else None)

    first_hist_idx = next(
        (i for i, r in enumerate(macd_rows) if r["hist"] is not None), None
    )

    block_for = params.get("min_bars_after_gap", DEFAULT_PARAMS["min_bars_after_gap"])
    gap_block = [False] * len(bars)
    remaining = 0
    for i, bar in enumerate(bars):
        if i > 0 and _minutes(bar["time"]) - _minutes(bars[i - 1]["time"]) > 1:
            remaining = block_for
        if remaining > 0:
            gap_block[i] = True
            remaining -= 1

    return {
        "sma": sma_rows,
        "macd": macd_rows,
        "vwap": vwap,
        "run_high": run_high,
        "first_hist_idx": first_hist_idx,
        "gap_block": gap_block,
    }


def r1_high_reclaim(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """확정 봉 종가가 그날 직전까지의 고가를 넘는다. 파라미터 0개.

    30분 고가의 49%가 09:00~09:02에 만들어진다. 그 고가를 되찾는 사건은 드물고
    상태 전환이 분명하다 — 설계 §2.3.
    """
    prior_high = ctx["run_high"][i]
    if prior_high == float("-inf"):
        return False
    return bars[i]["close"] > prior_high


def r2_vwap_reclaim(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """VWAP 아래에 있던 종가가 위로 올라서고, 그 봉 거래량이 직전 N봉 평균을 넘는다."""
    window = params.get("vol_window", DEFAULT_PARAMS["vol_window"])
    if i < 1 or i < window:
        return False
    vwap_now, vwap_prev = ctx["vwap"][i], ctx["vwap"][i - 1]
    if vwap_now is None or vwap_prev is None:
        return False
    if not (bars[i - 1]["close"] <= vwap_prev < bars[i]["close"]):
        return False
    prior = [b["volume"] for b in bars[i - window:i]]
    if not prior or sum(prior) <= 0:
        return False
    return bars[i]["volume"] > sum(prior) / len(prior)


def r3_indicator(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """v0 개정판 — 종가 > SMA + 히스토그램 음→양. 단 성숙 대기를 둔다.

    청산에 'SMA 이탈'이 없다는 점이 v0와 다르다. v0는 진입 조건과 청산 조건이
    같은 가격에서 무장해 3~6분 만에 끝났다 (설계 §2.1).
    """
    first = ctx["first_hist_idx"]
    maturity = params.get("hist_maturity_bars", DEFAULT_PARAMS["hist_maturity_bars"])
    if first is None or i < first + maturity or i < 1:
        return False
    sma_now = ctx["sma"][i]
    hist_now, hist_prev = ctx["macd"][i]["hist"], ctx["macd"][i - 1]["hist"]
    if sma_now is None or hist_now is None or hist_prev is None:
        return False
    return bars[i]["close"] > sma_now and hist_prev < 0 <= hist_now


def r4_pullback(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """급등 후 눌림에서 방어 캔들이 뜨고, 다음 봉이 그 고가를 넘는다.

    실전 가이드의 4단계 중 ①지지 도달 ②거래량 급감 ③방어 캔들은 봉으로
    판정하고, ④호가창 대량 매수 유입은 **봉으로 옮길 수 없다** — 분봉 API가
    OHLCV만 주므로 백테스트 표본에 영영 들어오지 않는다. 호가를 조건에 넣으면
    실시간과 백테스트가 서로 다른 것을 보게 된다. 그래서 "반등 확인 봉"으로
    옮겼다. 원본보다 한 박자 늦지만 재현 가능하고 미래를 보지 않는다.

    실시간 봉의 ``tick_derived``(체결강도·호가잔량·체결구분별 거래량)는 계속
    쌓이므로, 그림자가 돌기 시작하면 이 대체가 옳았는지 사후 대조할 수 있다.
    """
    if i < 1:
        return False
    support, confirm = bars[i - 1], bars[i]

    # ④ 반등 확인 — 확인 봉 종가가 지지 캔들 고가를 넘는다.
    if confirm["close"] <= support["high"]:
        return False

    # ① 선행 급등 — 오른 적이 없으면 눌림목이 아니라 그냥 하락이다.
    prior_high = ctx["run_high"][i - 1]
    first_open = bars[0]["open"]
    if prior_high == float("-inf") or first_open <= 0:
        return False
    run_up = params.get("pullback_prior_run", DEFAULT_PARAMS["pullback_prior_run"])
    if prior_high / first_open - 1 < run_up:
        return False

    # ① 지지 도달 — 닿기 전에는 판정하지 않는다 (예측 매수 금지).
    sma_now = ctx["sma"][i - 1]
    if sma_now is None:
        return False
    touch = params.get("pullback_touch_pct", DEFAULT_PARAMS["pullback_touch_pct"])
    if support["low"] > sma_now * (1 + touch):
        return False

    # ② 거래량 급감 — 던지려는 물량이 소진됐는가.
    window = params.get("pullback_vol_window", DEFAULT_PARAMS["pullback_vol_window"])
    if i - 1 < window:
        return False
    prior_vol = [b["volume"] for b in bars[i - 1 - window:i - 1]]
    if not prior_vol or sum(prior_vol) <= 0:
        return False
    dry = params.get("pullback_vol_dry", DEFAULT_PARAMS["pullback_vol_dry"])
    if support["volume"] > sum(prior_vol) / len(prior_vol) * dry:
        return False

    # ③ 방어 캔들 — 아래꼬리가 길거나(망치), 몸통이 없다(도지).
    span = support["high"] - support["low"]
    if span <= 0:
        return False
    body = abs(support["close"] - support["open"])
    lower_tail = min(support["open"], support["close"]) - support["low"]
    tail_ratio = params.get(
        "pullback_tail_ratio", DEFAULT_PARAMS["pullback_tail_ratio"]
    )
    doji_body = params.get("pullback_doji_body", DEFAULT_PARAMS["pullback_doji_body"])
    return lower_tail >= span * tail_ratio or body <= span * doji_body


def r5_pullback_breakout(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """눌림 뒤 돌파 — R4의 눌림(①지지 도달 ②거래량 급감)에 R1의 돌파를 잇는다.

    R4는 방어 캔들 고가만 넘으면 들어가고, R1은 눌림 없이 고점만 넘으면
    들어간다. 둘 다 졌다(2026-09-11 스윕). 이 규칙은 "눌렸다가 눌리기 전
    고점을 되찾는" 사건만 잡는다 — 조건이 교집합이라 진입은 더 늦고 더 드물다.
    방어 캔들 모양은 보지 않는다: 돌파 자체가 확인이라 중복이고, 조건이 늘수록
    표본만 준다. 파라미터는 R4 값을 그대로 쓰고 결과를 본 뒤 바꾸지 않는다.

    2026-09-11에 R1~R4 표본이 살아 있는 채로 추가됐으므로 그 표본에서의 결과는
    체로만 쓴다 (tests/test_track_b_rules.py 의 registry 테스트 참조).
    """
    if i < 2:
        return False
    confirm = bars[i]

    # 돌파 — 확정 봉 종가가 직전까지의 고점을 넘는다 (R1과 같은 정의).
    prior_high = ctx["run_high"][i]
    first_open = bars[0]["open"]
    if prior_high == float("-inf") or first_open <= 0:
        return False
    if confirm["close"] <= prior_high:
        return False

    # 선행 급등 — 되찾을 만한 고점이었는가.
    run_up = params.get("pullback_prior_run", DEFAULT_PARAMS["pullback_prior_run"])
    if prior_high / first_open - 1 < run_up:
        return False

    # 눌림 구간 — 고점 봉 다음부터 확정 봉 직전까지. 비어 있으면 연속 상승이다.
    peak = next(k for k in range(i) if bars[k]["high"] == prior_high)
    pull = range(peak + 1, i)
    if len(pull) == 0:
        return False

    # ① 지지 도달 — 눌림 구간 어느 봉의 저가가 SMA 근처까지 내려왔다.
    touch = params.get("pullback_touch_pct", DEFAULT_PARAMS["pullback_touch_pct"])
    touched = any(
        ctx["sma"][k] is not None and bars[k]["low"] <= ctx["sma"][k] * (1 + touch)
        for k in pull
    )
    if not touched:
        return False

    # ② 거래량 급감 — 돌파 직전 N봉 평균이 고점까지의 직전 N봉(고점 봉 포함, 그만큼
    #    없으면 있는 만큼) 평균에 비해 말랐다. 고점이 첫 봉인 날이 흔해서 N봉을
    #    강제하면 09:00 고점 종목이 통째로 빠진다.
    window = params.get("pullback_vol_window", DEFAULT_PARAMS["pullback_vol_window"])
    prior_vol = [b["volume"] for b in bars[max(0, peak + 1 - window):peak + 1]]
    if sum(prior_vol) <= 0:
        return False
    dry = params.get("pullback_vol_dry", DEFAULT_PARAMS["pullback_vol_dry"])
    # 마른 것은 돌파 직전 — 지지 부근 — 의 봉들이어야 한다. 고점부터 전부
    # 평균하면 긴 횡보가 급감을 가려 버린다.
    tail = list(pull)[-window:]
    tail_vol = sum(bars[k]["volume"] for k in tail) / len(tail)
    return tail_vol <= sum(prior_vol) / len(prior_vol) * dry


RULES = {
    "R1": r1_high_reclaim,
    "R2": r2_vwap_reclaim,
    "R3": r3_indicator,
    "R4": r4_pullback,
    "R5": r5_pullback_breakout,
}

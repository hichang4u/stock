"""H5 판정기의 규칙 층 — 유니버스·신호·선정·진입·청산·대조군.

스펙 §2(선정·매매 규칙), §3.3~3.4(대조군·수익 정의), §4.4(전 신호 이벤트 연구).
순수 함수다. 캐시를 직접 읽지 않고 Panel 을 받는다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from scripts.breakout_panel import HISTORY_ROWS, Panel, Series

HOLD_DAYS = 20
PRICE_FLOOR = 1_000
VALUE_FLOOR = 1_000_000_000
LIMIT_CHANGE_DATE = "20150615"
LIMIT_UP_SLACK = 0.995
LIMIT_DOWN_SLACK = 1.005
MATCHED_TOP_FRAC = 0.10
COST = 0.0035
SIGNAL_START = "20110101"
SIGNAL_TOLERANCE = 1e-9  # 수정가 곱셈의 부동소수 오차로 같은 값이 '초과'로 뒤집히지 않게

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
NO_NEXT_DAY = "NO_NEXT_DAY"
NO_ENTRY_ROW = "NO_ENTRY_ROW"
NO_ENTRY_SUSPENDED = "NO_ENTRY_SUSPENDED"
NO_ENTRY_LIMIT_UP = "NO_ENTRY_LIMIT_UP"


def price_limit(d: str) -> float:
    """가격제한폭 — 2015-06-15 에 ±15% → ±30% (스펙 §2.5)."""
    return 0.15 if d < LIMIT_CHANGE_DATE else 0.30


def universe_reason(panel: Panel, ticker: str, d: str) -> str | None:
    """스펙 §2.2. 통과면 None, 아니면 첫 탈락 사유."""
    s = panel.series.get(ticker)
    i = s.pos(d) if s is not None else -1
    if s is None or i < 0:
        return "NO_ROW"
    info = panel.base_info(ticker, d)
    if info is None:
        return "NO_BASE"
    secugrp, kind, sect, abbrv = info
    if secugrp != "주권":
        return "SECUGRP"
    if kind != "보통주":
        return "KIND"
    if "SPAC" in sect or "스팩" in abbrv or "스팩" in s.names[i]:
        return "SPAC"
    if s.open[i] <= 0:
        return "NOT_TRADED"
    if s.close[i] < PRICE_FLOOR:
        return "PRICE"
    if s.value[i] < VALUE_FLOOR:
        return "VALUE"
    if i < HISTORY_ROWS:
        return "HISTORY"
    return None


def is_signal(s: Series, i: int) -> bool:
    """스펙 §2.3 — 수정 종가 > 직전 250행 수정 종가 최고치 (엄격 초과)."""
    pm = s.prior_max[i]
    return not math.isnan(pm) and s.adj_close(i) > pm * (1 + SIGNAL_TOLERANCE)


@dataclass(frozen=True)
class Outcome:
    status: str
    entry_date: str = ""
    exit_date: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    exit_kind: str = ""
    limit_down_delay: bool = False

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE

    @property
    def gross(self) -> float:
        return self.exit_price / self.entry_price - 1.0


def simulate(panel: Panel, ticker: str, signal_date: str) -> Outcome:
    """신호일 D 에 산다고 할 때의 결과 — 스펙 §2.5 진입, §2.6 청산."""
    s = panel.series[ticker]
    ci = panel.cal_index[signal_date]
    if ci + 1 >= len(panel.calendar):
        return Outcome(NO_NEXT_DAY)
    entry_day = panel.calendar[ci + 1]
    ei = s.pos(entry_day)
    if ei < 0:
        return Outcome(NO_ENTRY_ROW)
    if s.open[ei] <= 0:
        return Outcome(NO_ENTRY_SUSPENDED)
    basis = s.basis(ei)
    if basis is not None and s.open[ei] >= basis * (1 + price_limit(entry_day)) * LIMIT_UP_SLACK:
        return Outcome(NO_ENTRY_LIMIT_UP)
    entry_price = s.adj_open(ei)

    last = len(s.dates) - 1
    vanished = s.dates[last] < panel.last_day.get(s.market, s.dates[last])
    limit_down_seen = False
    target = ci + 1 + HOLD_DAYS
    for k in range(target, len(panel.calendar)):
        day = panel.calendar[k]
        if vanished and day > s.dates[last]:
            break
        xi = s.pos(day)
        if xi < 0 or s.open[xi] <= 0:
            continue
        b = s.basis(xi)
        if b is not None and s.open[xi] <= b * (1 - price_limit(day)) * LIMIT_DOWN_SLACK:
            limit_down_seen = True
            continue
        kind = "SCHEDULED" if k == target else "DELAYED"
        return Outcome(COMPLETE, entry_day, day, entry_price, s.adj_open(xi), kind,
                       limit_down_seen)
    if vanished:
        return Outcome(COMPLETE, entry_day, s.dates[last], entry_price, s.adj_close(last),
                       "VANISHED", limit_down_seen)
    return Outcome(INCOMPLETE, entry_date=entry_day, entry_price=entry_price)


@dataclass(frozen=True)
class Trade:
    ticker: str
    signal_date: str
    outcome: Outcome
    matched_mean: float | None
    matched_n: int
    equal_mean: float | None
    equal_n: int


@dataclass
class RuleRun:
    trades: list[Trade] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    event_net: list[float] = field(default_factory=list)
    event_excess: list[float] = field(default_factory=list)
    universe_sizes: dict[str, list[int]] = field(default_factory=dict)


def _cached(panel: Panel, cache: dict[str, Outcome], ticker: str, d: str) -> Outcome:
    outcome = cache.get(ticker)
    if outcome is None:
        outcome = simulate(panel, ticker, d)
        cache[ticker] = outcome
    return outcome


def _complete_mean(outcomes: Iterable[Outcome]) -> tuple[float | None, int]:
    grosses = [o.gross for o in outcomes if o.complete]
    if not grosses:
        return None, 0
    return sum(grosses) / len(grosses), len(grosses)


def run_rules(panel: Panel) -> RuleRun:
    run = RuleRun()
    held_until: dict[str, str | None] = {}  # 종목 → 청산일. 미완료면 None(끝까지 보유)
    for ci, d in enumerate(panel.calendar):
        if d < SIGNAL_START:
            continue
        members: list[str] = []
        values: dict[str, int] = {}
        for t in panel.by_date.get(d, []):
            reason = universe_reason(panel, t, d)
            if reason is None:
                s = panel.series[t]
                members.append(t)
                values[t] = s.value[s.pos(d)]
            elif reason == "NO_BASE":
                run.counts["universe_no_base"] += 1
        run.universe_sizes.setdefault(d[:4], []).append(len(members))

        signals = [t for t in members if is_signal(panel.series[t], panel.series[t].pos(d))]
        if not signals:
            run.counts["no_signal_day"] += 1
            continue

        cache: dict[str, Outcome] = {}
        ranked = sorted(members, key=lambda t: (-values[t], t))
        top = ranked[: math.ceil(len(members) * MATCHED_TOP_FRAC)]
        signal_set = set(signals)
        matched_mean, matched_n = _complete_mean(
            _cached(panel, cache, t, d) for t in top if t not in signal_set)
        if matched_mean is not None:
            for t in signals:
                o = _cached(panel, cache, t, d)
                if o.complete:
                    run.event_net.append(o.gross - COST)
                    run.event_excess.append(o.gross - matched_mean)

        entry_day = panel.calendar[ci + 1] if ci + 1 < len(panel.calendar) else ""
        chosen: str | None = None
        for t in sorted(signals, key=lambda t: (-values[t], t)):
            if t in held_until:
                until = held_until[t]
                if until is None or until >= entry_day:
                    continue
            chosen = t
            break
        if chosen is None:
            run.counts["all_signals_held"] += 1
            continue

        o = _cached(panel, cache, chosen, d)
        if not (o.complete or o.status == INCOMPLETE):
            run.counts[o.status] += 1
            continue
        equal_mean, equal_n = _complete_mean(
            _cached(panel, cache, t, d) for t in members if t != chosen)
        run.trades.append(Trade(chosen, d, o, matched_mean, matched_n, equal_mean, equal_n))
        held_until[chosen] = o.exit_date if o.complete else None
        if o.status == INCOMPLETE:
            run.counts["incomplete"] += 1
        elif o.exit_kind == "VANISHED":
            run.counts["exit_vanished"] += 1
        if o.limit_down_delay:
            run.counts["exit_limit_down_delay"] += 1
    return run

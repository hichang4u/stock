"""H5 규칙 층 — 합성 시장만 쓴다. 2010년은 52주 준비, 2011년 상반기에 신호를 낸다."""

import math

from scripts.breakout_panel import build_panel
from scripts.breakout_rules import (
    COMPLETE,
    INCOMPLETE,
    NO_ENTRY_LIMIT_UP,
    NO_ENTRY_SUSPENDED,
    is_signal,
    price_limit,
    run_rules,
    simulate,
    universe_reason,
)
from scripts.krx_daily_fetch import weekdays

DAYS = weekdays("20100104", "20110630")
S = next(i for i, d in enumerate(DAYS) if d >= "20110101") + 5  # 신호일 인덱스
BIG = 2_000_000_000


def rec(ticker, d, *, o, c, chg, val, name):
    return {
        "BAS_DD": d, "ISU_CD": ticker, "ISU_NM": name,
        "TDD_OPNPRC": str(o), "TDD_HGPRC": str(max(o, c) if o else 0),
        "TDD_LWPRC": str(min(o, c) if o else 0), "TDD_CLSPRC": str(c),
        "CMPPREVDD_PRC": str(chg), "ACC_TRDVAL": str(val),
    }


def path(at=None):
    """기본 시가·종가 1,000원. at = {인덱스: (시가, 종가)}."""
    out = [(1000, 1000)] * len(DAYS)
    for i, oc in (at or {}).items():
        out[i] = oc
    return out


class Market:
    def __init__(self):
        self.rows: dict = {}
        self.base: list = []
        self.missing: set = set()

    def add(self, ticker, prices, *, market="stk", value=BIG, name="종목", secugrp="주권",
            kind="보통주", sect="", abbrv="종목", start=0, base=True):
        prev = None
        for d, (o, c) in zip(DAYS[start:], prices):
            chg = 0 if prev is None else c - prev
            self.rows.setdefault((market, d), []).append(
                rec(ticker, d, o=o, c=c, chg=chg, val=value, name=name))
            prev = c
        if base:
            self.base.append({"ISU_SRT_CD": ticker, "SECUGRP_NM": secugrp,
                              "KIND_STKCERT_TP_NM": kind, "SECT_TP_NM": sect,
                              "ISU_ABBRV": abbrv})
        return self

    def panel(self):
        # 두 시장 모두 매일 행이 있게 유니버스 밖의 채움 종목을 넣는다(외국주권)
        self.add("900001", path(), market="stk", secugrp="외국주권")
        self.add("900002", path(), market="ksq", secugrp="외국주권")
        table = {key: rows for key, rows in self.rows.items() if key not in self.missing}
        return build_panel(DAYS, lambda m, d: table.get((m, d)), [("stk", DAYS[0], self.base)])


def test_price_limit_switches_on_20150615():
    assert price_limit("20150612") == 0.15
    assert price_limit("20150615") == 0.30


def test_universe_reason_branches():
    m = Market()
    m.add("000010", path())
    m.add("000020", path(), secugrp="부동산투자회사")
    m.add("000030", path(), kind="구형우선주")
    m.add("000040", path(), sect="SPAC(소속부없음)")
    m.add("000050", path(), sect="관리종목(소속부없음)", abbrv="하나31호스팩")
    m.add("000060", path({S: (0, 1000)}))
    m.add("000070", path({S: (999, 999)}))
    m.add("000080", path(), value=999_999_999)
    m.add("000090", path()[S - 100:], start=S - 100)
    m.add("000100", path(), name="메리츠금융지주", abbrv="메리츠금융지주")
    m.add("000110", path(), base=False)
    p = m.panel()
    day = DAYS[S]

    got = {t: universe_reason(p, t, day) for t in
           ("000010", "000020", "000030", "000040", "000050", "000060", "000070",
            "000080", "000090", "000100", "000110", "999999")}
    assert got == {
        "000010": None, "000020": "SECUGRP", "000030": "KIND", "000040": "SPAC",
        "000050": "SPAC", "000060": "NOT_TRADED", "000070": "PRICE", "000080": "VALUE",
        "000090": "HISTORY", "000100": None, "000110": "NO_BASE", "999999": "NO_ROW",
    }


def test_signal_needs_a_strictly_higher_adjusted_close():
    p = Market().add("000010", path({S: (1000, 1100)})).add("000020", path()).panel()
    a, b = p.series["000010"], p.series["000020"]
    assert is_signal(a, S) is True
    assert is_signal(a, S + 1) is False
    assert is_signal(b, S) is False


def test_simulate_exits_at_the_twentieth_day_open():
    p = Market().add("000010", path({S: (1000, 1100), S + 21: (1210, 1210)})).panel()
    o = simulate(p, "000010", DAYS[S])
    assert (o.status, o.entry_date, o.exit_date, o.exit_kind) == (
        COMPLETE, DAYS[S + 1], DAYS[S + 21], "SCHEDULED")
    assert math.isclose(o.gross, 0.21)


def test_simulate_refuses_a_suspended_entry():
    p = Market().add("000010", path({S: (1000, 1100), S + 1: (0, 1100)})).panel()
    assert simulate(p, "000010", DAYS[S]).status == NO_ENTRY_SUSPENDED


def test_simulate_refuses_a_limit_up_open_with_the_pre_2015_limit():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 1: (1265, 1265)}))   # 1100 × 1.15
    m.add("000020", path({S: (1000, 1100), S + 1: (1250, 1250)}))   # 기준 1258.6 미만
    p = m.panel()
    assert simulate(p, "000010", DAYS[S]).status == NO_ENTRY_LIMIT_UP
    assert simulate(p, "000020", DAYS[S]).status == COMPLETE


def test_simulate_delays_the_exit_on_suspension_and_on_a_limit_down_open():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 21: (0, 1000)}))
    m.add("000020", path({S: (1000, 1100), S + 21: (850, 850)}))    # 1000 × 0.85 × 1.005 이하
    p = m.panel()
    a, b = simulate(p, "000010", DAYS[S]), simulate(p, "000020", DAYS[S])
    assert (a.exit_date, a.exit_kind, a.limit_down_delay) == (DAYS[S + 22], "DELAYED", False)
    assert (b.exit_date, b.exit_kind, b.limit_down_delay) == (DAYS[S + 22], "DELAYED", True)


def test_simulate_exits_a_vanished_stock_at_its_last_close():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 10: (500, 500)})[: S + 11])
    m.add("000020", path())
    o = simulate(m.panel(), "000010", DAYS[S])
    assert (o.status, o.exit_date, o.exit_kind) == (COMPLETE, DAYS[S + 10], "VANISHED")
    assert math.isclose(o.gross, -0.5)


def test_simulate_is_incomplete_past_the_end_of_data():
    p = Market().add("000010", path()).panel()
    assert simulate(p, "000010", DAYS[-5]).status == INCOMPLETE


def test_a_missing_market_file_is_not_a_vanishing():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), market="ksq")
    m.missing.add(("ksq", DAYS[S + 21]))
    o = simulate(m.panel(), "000010", DAYS[S])
    assert (o.exit_date, o.exit_kind) == (DAYS[S + 22], "DELAYED")


def test_run_rules_picks_the_top_value_signal_and_builds_both_controls():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), value=5_000_000_000)
    m.add("000020", path({S: (1000, 1100)}), value=3_000_000_000)
    # 청산일 시가만 움직이고 종가는 1,000원에 둔다 — 종가를 올리면 그날 새 신고가가 된다
    m.add("000030", path({S + 21: (1100, 1000)}), value=9_000_000_000)
    m.add("000040", path({S + 21: (900, 1000)}), value=2_000_000_000)
    run = run_rules(m.panel())

    assert [(t.ticker, t.signal_date) for t in run.trades] == [("000010", DAYS[S])]
    trade = run.trades[0]
    # 유니버스 4종목 → 상위 ⌈0.4⌉ = 1종목(000030), 신호가 아니다
    assert trade.matched_n == 1 and math.isclose(trade.matched_mean, 0.1)
    # 거래 종목만 뺀 3종목: 0, +0.1, −0.1
    assert trade.equal_n == 3 and math.isclose(trade.equal_mean, 0.0, abs_tol=1e-12)
    assert len(run.event_excess) == 2
    assert all(math.isclose(x, -0.1) for x in run.event_excess)


def test_run_rules_skips_a_held_stock_for_the_next_rank():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 5: (1000, 1200)}), value=5_000_000_000)
    m.add("000020", path({S + 5: (1000, 1100)}), value=3_000_000_000)
    run = run_rules(m.panel())
    assert [(t.ticker, t.signal_date) for t in run.trades] == [
        ("000010", DAYS[S]), ("000020", DAYS[S + 5])]


def test_run_rules_breaks_value_ties_by_code():
    m = Market()
    m.add("000020", path({S: (1000, 1100)}))
    m.add("000010", path({S: (1000, 1100)}))
    assert run_rules(m.panel()).trades[0].ticker == "000010"


def test_run_rules_does_not_substitute_when_the_top_signal_cannot_be_entered():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 1: (0, 1100)}), value=5_000_000_000)
    m.add("000020", path({S: (1000, 1100)}), value=3_000_000_000)
    run = run_rules(m.panel())
    assert run.trades == []
    assert run.counts[NO_ENTRY_SUSPENDED] == 1


def test_run_rules_keeps_a_trade_whose_matched_control_is_empty():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), value=5_000_000_000)
    run = run_rules(m.panel())
    assert run.trades[0].matched_mean is None and run.trades[0].matched_n == 0
    assert run.event_excess == []

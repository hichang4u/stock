"""H5 데이터 층 — 합성 행만 쓴다."""

import math

from scripts.breakout_panel import (
    KNOWN_KIND,
    KNOWN_SECUGRP,
    build_panel,
    load_panel,
    prior_max,
)
from scripts.krx_daily_fetch import cache_path, save_atomic


def rec(ticker, d, *, o, hi, lo, c, chg, val=2_000_000_000, name="종목"):
    return {
        "BAS_DD": d, "ISU_CD": ticker, "ISU_NM": name,
        "TDD_OPNPRC": str(o), "TDD_HGPRC": str(hi), "TDD_LWPRC": str(lo),
        "TDD_CLSPRC": str(c), "CMPPREVDD_PRC": str(chg), "ACC_TRDVAL": str(val),
    }


def base_rec(code, secugrp="주권", kind="보통주", sect="", abbrv="종목"):
    return {"ISU_CD": "KR7" + code + "000", "ISU_SRT_CD": code, "SECUGRP_NM": secugrp,
            "KIND_STKCERT_TP_NM": kind, "SECT_TP_NM": sect, "ISU_ABBRV": abbrv}


def panel_of(table, days=None, base=()):
    days = days or sorted({d for (_m, d) in table})
    return build_panel(days, lambda m, d: table.get((m, d)), list(base))


def test_prior_max_uses_only_the_previous_window():
    out = prior_max([1.0, 3.0, 2.0, 5.0, 4.0], 2)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert list(out)[2:] == [3.0, 3.0, 5.0]


def test_samsung_split_shape_gives_factor_one_fiftieth():
    table = {
        ("stk", "20180503"): [rec("005930", "20180503", o=0, hi=0, lo=0, c=2650000, chg=0)],
        ("stk", "20180504"): [rec("005930", "20180504", o=53000, hi=53900, lo=51300,
                                  c=51900, chg=-1100)],
        ("ksq", "20180503"): [rec("000250", "20180503", o=1, hi=1, lo=1, c=1, chg=0)],
        ("ksq", "20180504"): [rec("000250", "20180504", o=1, hi=1, lo=1, c=1, chg=0)],
    }
    s = panel_of(table).series["005930"]
    assert s.events == [("20180504", 0.02)]
    assert s.basis(1) == 53000
    assert math.isclose(s.adj_close(0), 53000.0)
    assert s.adj_close(1) == 51900.0


def test_liquidation_crash_is_a_loss_not_an_event():
    table = {
        ("stk", "20170222"): [rec("117930", "20170222", o=0, hi=0, lo=0, c=780, chg=0)],
        ("stk", "20170223"): [rec("117930", "20170223", o=420, hi=420, lo=300, c=310, chg=-470)],
        ("ksq", "20170222"): [rec("000250", "20170222", o=1, hi=1, lo=1, c=1, chg=0)],
        ("ksq", "20170223"): [rec("000250", "20170223", o=1, hi=1, lo=1, c=1, chg=0)],
    }
    s = panel_of(table).series["117930"]
    assert s.events == []
    assert list(s.mult) == [1.0, 1.0]


def _rising(ticker, days, *, merge_at=None):
    """종가가 날마다 1원씩 오르는 합성 종목. merge_at 행에서 10:1 병합(가격 10배)."""
    rows = []
    for i, d in enumerate(days):
        close = 1000 + i
        chg = 1
        if merge_at is not None and i >= merge_at:
            close = (1000 + i) * 10
            chg = 10 if i > merge_at else close - (1000 + i - 1) * 10
        rows.append(rec(ticker, d, o=close, hi=close, lo=close, c=close, chg=chg))
    return rows


def test_future_event_factors_do_not_change_earlier_signals():
    days = [f"D{i:04d}" for i in range(300)]
    plain = _rising("111110", days)
    merged = _rising("111110", days, merge_at=280)
    a = panel_of({("stk", d): [r] for d, r in zip(days, plain)} |
                 {("ksq", d): [] for d in days}).series["111110"]
    b = panel_of({("stk", d): [r] for d, r in zip(days, merged)} |
                 {("ksq", d): [] for d in days}).series["111110"]

    assert b.events and b.events[0][0] == "D0280"
    before = range(250, 280)
    assert [a.adj_close(i) > a.prior_max[i] for i in before] == [
        b.adj_close(i) > b.prior_max[i] for i in before
    ]
    assert b.adj_close(280) > b.prior_max[280]


def test_calendar_holiday_missing_and_unknown_days():
    row = [rec("005930", "x", o=1, hi=1, lo=1, c=1, chg=0)]
    table = {
        ("stk", "D1"): row, ("ksq", "D1"): row,        # 거래일
        ("stk", "D2"): [], ("ksq", "D2"): [],          # 휴장
        ("stk", "D3"): row,                            # 코스닥 파일 없음 → 결측
        # D4: 두 시장 모두 파일 없음 → 거래일인지 모름
        ("stk", "D5"): row, ("ksq", "D5"): [],         # 코스닥만 빈 목록 → 결측
    }
    p = panel_of(table, days=["D1", "D2", "D3", "D4", "D5"])

    assert p.calendar == ["D1", "D3", "D5"]
    assert p.missing == {"D3": frozenset({"ksq"}), "D4": frozenset({"stk", "ksq"}),
                         "D5": frozenset({"ksq"})}
    assert p.unknown_days == ["D4"]
    assert math.isclose(p.missing_ratio(), 3 / 5)
    assert p.last_day == {"stk": "D5", "ksq": "D1"}
    assert p.cal_index["D3"] == 1


def test_base_snapshot_is_the_latest_on_or_before_the_day():
    table = {("stk", "20170215"): [rec("005930", "20170215", o=1, hi=1, lo=1, c=1, chg=0)],
             ("ksq", "20170215"): []}
    base = [("stk", "20170201", [base_rec("005930")]),
            ("stk", "20170301", [base_rec("005930", sect="X"), base_rec("000660")])]
    p = panel_of(table, base=base)

    assert p.base_info("005930", "20170215") == ("주권", "보통주", "", "종목")
    assert p.base_info("005930", "20170131") is None
    assert p.base_info("000660", "20170215") is None
    assert p.base_info("005930", "20170302") == ("주권", "보통주", "X", "종목")
    assert p.categories["SECUGRP_NM"] == {"주권"}


def test_blank_change_counts_as_no_basis_and_factor_one():
    r0 = rec("005930", "D1", o=100, hi=100, lo=100, c=100, chg=0)
    r1 = rec("005930", "D2", o=200, hi=200, lo=200, c=200, chg=0)
    r1["CMPPREVDD_PRC"] = ""
    p = panel_of({("stk", "D1"): [r0], ("stk", "D2"): [r1], ("ksq", "D1"): [], ("ksq", "D2"): []})
    s = p.series["005930"]
    assert s.no_basis == 1 and s.events == [] and s.basis(1) is None


def test_pos_finds_dates_and_misses():
    table = {("stk", "D1"): [rec("005930", "D1", o=1, hi=1, lo=1, c=1, chg=0)], ("ksq", "D1"): []}
    s = panel_of(table).series["005930"]
    assert s.pos("D1") == 0 and s.pos("D0") == -1 and s.pos("D9") == -1


def test_load_panel_reads_the_cache_layout(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170201"),
                [rec("005930", "20170201", o=1, hi=1, lo=1, c=1, chg=0)])
    save_atomic(cache_path(tmp_path, "ksq", "20170201"), [])
    save_atomic(cache_path(tmp_path, "base/stk", "20170201"), [base_rec("005930")])

    p = load_panel(tmp_path, start="20170201", end="20170201")

    assert p.calendar == ["20170201"]
    assert p.base_info("005930", "20170201") == ("주권", "보통주", "", "종목")


def test_known_categories_match_the_spec():
    assert KNOWN_SECUGRP == {"주권", "부동산투자회사", "사회간접자본투융자회사", "투자회사",
                             "외국주권", "주식예탁증권"}
    assert KNOWN_KIND == {"보통주", "구형우선주", "신형우선주", "종류주권"}

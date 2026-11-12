"""H5 판정기의 데이터 층 — 캐시를 시장 달력과 종목별 열 배열로 올린다.

스펙 §2.7(시장 달력·결측), §3.1(기준가 역산·수정가), §5(데이터 규약).
약 900만 행을 행 객체로 들면 수 GB라, 종목마다 array 열로 담고 날짜·종목명 문자열은
intern 해서 공유한다. 원문은 날짜 하나씩 받아 소화한다. 성과는 여기서 계산하지 않는다.
"""

from __future__ import annotations

import bisect
import math
import sys
from array import array
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from scripts.krx_daily_fetch import (
    BASE_KINDS,
    DAILY_KINDS,
    END,
    START,
    cache_path,
    read_rows,
    weekdays,
)

HISTORY_ROWS = 250
NO_CHANGE = -(2**62)  # 전일대비가 빈 행의 표지

# 스펙 §5.3 "등록 시 확인된 범주값". 봉인(Task 7)이 이 밖의 값을 보면 멈춘다.
# "선박투자회사"·"주식예탁증서"는 2026-11-12 첫 판정 실행에서 봉인이 멈춘 값이다(§5.3 추가분).
# 알려진 값일 뿐 허용 목록이 아니다. 유니버스는 여전히 "주권"·"보통주"만 통과시킨다.
KNOWN_SECUGRP = frozenset(
    {
        "주권", "부동산투자회사", "사회간접자본투융자회사", "투자회사", "외국주권", "주식예탁증권",
        "선박투자회사", "주식예탁증서",
    }
)
KNOWN_KIND = frozenset({"보통주", "구형우선주", "신형우선주", "종류주권"})

BaseInfo = tuple[str, str, str, str]  # (증권그룹, 주식종류, 소속부, 약칭)


def to_int(text: object) -> int | None:
    s = str(text if text is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    return int(float(s))


def prior_max(values: list[float], window: int) -> array:
    """values[i] 직전 window 개의 최댓값. 이력이 window 개 미만이면 nan. O(n) 단조 덱."""
    out = array("d", [math.nan]) * len(values)
    dq: deque[int] = deque()
    for i, v in enumerate(values):
        while dq and dq[0] < i - window:
            dq.popleft()
        if i >= window:
            out[i] = values[dq[0]]
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
    return out


@dataclass
class Series:
    ticker: str
    market: str
    dates: list[str]
    names: list[str]
    open: array
    high: array
    low: array
    close: array
    change: array
    value: array
    mult: array = field(default_factory=lambda: array("d"))
    prior_max: array = field(default_factory=lambda: array("d"))
    events: list[tuple[str, float]] = field(default_factory=list)
    no_basis: int = 0

    def pos(self, d: str) -> int:
        i = bisect.bisect_left(self.dates, d)
        return i if i < len(self.dates) and self.dates[i] == d else -1

    def basis(self, i: int) -> int | None:
        """기준가 = 종가 − 전일대비 (스펙 §3.1). 전일대비가 비면 None."""
        change = self.change[i]
        return None if change == NO_CHANGE else self.close[i] - change

    def adj_open(self, i: int) -> float:
        return self.open[i] * self.mult[i]

    def adj_close(self, i: int) -> float:
        return self.close[i] * self.mult[i]


class _Acc:
    __slots__ = ("dates", "names", "open", "high", "low", "close", "change", "value", "market")

    def __init__(self) -> None:
        self.dates: list[str] = []
        self.names: list[str] = []
        self.open = array("q")
        self.high = array("q")
        self.low = array("q")
        self.close = array("q")
        self.change = array("q")
        self.value = array("q")
        self.market = ""

    def add(self, rec: dict, d: str, market: str) -> None:
        if self.dates and self.dates[-1] == d:
            return  # 같은 날 두 시장에 겹쳐 나오면 먼저 본 것만 쓴다
        self.dates.append(d)
        self.names.append(sys.intern(str(rec.get("ISU_NM") or "")))
        self.open.append(to_int(rec.get("TDD_OPNPRC")) or 0)
        self.high.append(to_int(rec.get("TDD_HGPRC")) or 0)
        self.low.append(to_int(rec.get("TDD_LWPRC")) or 0)
        self.close.append(to_int(rec.get("TDD_CLSPRC")) or 0)
        change = to_int(rec.get("CMPPREVDD_PRC"))
        self.change.append(NO_CHANGE if change is None else change)
        self.value.append(to_int(rec.get("ACC_TRDVAL")) or 0)
        self.market = market


def build_series(ticker: str, acc: _Acc) -> Series:
    s = Series(ticker, acc.market, acc.dates, acc.names, acc.open, acc.high, acc.low,
               acc.close, acc.change, acc.value)
    n = len(s.dates)
    factors = [1.0] * n
    for i in range(1, n):
        basis = s.basis(i)
        if basis is None:
            s.no_basis += 1
            continue
        prev_close = s.close[i - 1]
        if prev_close > 0 and basis != prev_close:
            factors[i] = basis / prev_close
            s.events.append((s.dates[i], factors[i]))
    mult = array("d", [1.0]) * n
    running = 1.0
    for i in range(n - 1, -1, -1):
        mult[i] = running
        running *= factors[i]
    s.mult = mult
    s.prior_max = prior_max([s.close[i] * mult[i] for i in range(n)], HISTORY_ROWS)
    return s


@dataclass
class Panel:
    calendar: list[str]
    series: dict[str, Series]
    by_date: dict[str, list[str]]
    missing: dict[str, frozenset[str]]
    unknown_days: list[str]
    last_day: dict[str, str]
    base_dates: list[str]
    base: dict[str, dict[str, BaseInfo]]
    categories: dict[str, set[str]]
    weekday_count: int
    cal_index: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.cal_index = {d: i for i, d in enumerate(self.calendar)}

    def base_info(self, ticker: str, d: str) -> BaseInfo | None:
        i = bisect.bisect_right(self.base_dates, d) - 1
        if i < 0:
            return None
        return self.base[self.base_dates[i]].get(ticker)

    def missing_ratio(self) -> float:
        return len(self.missing) / self.weekday_count if self.weekday_count else 1.0


def _base_snapshots(
    base: Iterable[tuple[str, str, list[dict]]],
) -> tuple[dict[str, dict[str, BaseInfo]], dict[str, set[str]]]:
    snapshots: dict[str, dict[str, BaseInfo]] = {}
    categories: dict[str, set[str]] = {
        "SECUGRP_NM": set(), "KIND_STKCERT_TP_NM": set(), "SECT_TP_NM": set(),
    }
    for _market, d, rows in base:
        snap = snapshots.setdefault(d, {})
        for rec in rows:
            code = str(rec.get("ISU_SRT_CD") or "")
            if not code:
                continue
            info: BaseInfo = (
                sys.intern(str(rec.get("SECUGRP_NM") or "")),
                sys.intern(str(rec.get("KIND_STKCERT_TP_NM") or "")),
                sys.intern(str(rec.get("SECT_TP_NM") or "")),
                sys.intern(str(rec.get("ISU_ABBRV") or "")),
            )
            snap[code] = info
            categories["SECUGRP_NM"].add(info[0])
            categories["KIND_STKCERT_TP_NM"].add(info[1])
            categories["SECT_TP_NM"].add(info[2])
    return snapshots, categories


def build_panel(
    days: list[str],
    get_daily: Callable[[str, str], list[dict] | None],
    base: Iterable[tuple[str, str, list[dict]]],
) -> Panel:
    """days: 평일 목록. get_daily(시장, 날짜) = 행 목록, 파일이 없으면 None.
    base: (시장, 스냅샷 날짜, 기본정보 행 목록)."""
    accs: dict[str, _Acc] = {}
    calendar: list[str] = []
    by_date: dict[str, list[str]] = {}
    missing: dict[str, frozenset[str]] = {}
    unknown: list[str] = []
    last_day: dict[str, str] = {}
    for raw_day in days:
        d = sys.intern(raw_day)
        got = {m: get_daily(m, d) for m in DAILY_KINDS}
        filled = [m for m in DAILY_KINDS if got[m]]
        if not filled:
            if all(got[m] is not None for m in DAILY_KINDS):
                continue  # 두 시장 모두 빈 목록 — 휴장
            missing[d] = frozenset(m for m in DAILY_KINDS if got[m] is None)
            unknown.append(d)  # 거래일인지 알 수 없다
            continue
        gaps = frozenset(m for m in DAILY_KINDS if not got[m])
        if gaps:
            missing[d] = gaps
        calendar.append(d)
        tickers: list[str] = []
        for m in filled:
            last_day[m] = d
            for rec in got[m] or []:
                ticker = sys.intern(str(rec.get("ISU_CD") or ""))
                if not ticker:
                    continue
                accs.setdefault(ticker, _Acc()).add(rec, d, m)
                tickers.append(ticker)
        by_date[d] = tickers
    series = {t: build_series(t, acc) for t, acc in accs.items()}
    snapshots, categories = _base_snapshots(base)
    return Panel(calendar, series, by_date, missing, unknown, last_day,
                 sorted(snapshots), snapshots, categories, len(days))


def load_panel(cache_dir: Path, start: str = START, end: str = END) -> Panel:
    def get_daily(market: str, d: str) -> list[dict] | None:
        path = cache_path(cache_dir, market, d)
        return read_rows(path) if path.exists() else None

    def iter_base() -> Iterator[tuple[str, str, list[dict]]]:
        for kind in BASE_KINDS:
            for path in sorted((cache_dir / kind).glob("*.json.gz")):
                yield kind.split("/")[1], path.name[:8], read_rows(path)

    return build_panel(weekdays(start, end), get_daily, iter_base())

"""Collect public C2 inputs for a source check during the 15:20–15:25 KST window."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as wall_time
from html import unescape
from itertools import count
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx

from scripts.krx_daily_fetch import BASE_URL, ENDPOINTS, ROOT, load_key
from src.market_calendar import is_late_open

KST = timezone(timedelta(hours=9))
INPUT_CUTOFF = wall_time(15, 20)
DEADLINE = wall_time(15, 25)
KIND = "https://kind.krx.co.kr"
KIND_DISCLOSURES = f"{KIND}/disclosure/todaydisclosure.do"
NAVER_CHART = "https://api.stock.naver.com/chart/domestic"
OUTPUT = ROOT / "data" / "c2" / "source_probe"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 5.0
MARKET_ACTION_PAGES = {
    "halt": ("tradinghaltissue", "searchTradingHaltIssueSub", "tradinghaltissue_sub"),
    "admin": ("adminissue", "searchAdminIssueSub", "adminissue_sub"),
    "caution_environment": ("hwangiissue", "searchHwangiIssueSub", "hwangiissue_sub"),
    "warning": ("investattentwarnrisky", "investattentwarnriskySub", "invstwarnisu_sub"),
    "risk": ("investattentwarnrisky", "investattentwarnriskySub", "invstriskisu_sub"),
}
AMENDMENTS = ("[기재정정]", "[첨부정정]", "[첨부추가]", "[변경등록]", "[정정]")
# C2 draft 925cad3 §3.2. Here this list only scopes the source check.
DISCLOSURE_TITLES = (
    "단일판매ㆍ공급계약체결",
    "단일판매·공급계약체결",
    "단일판매공급계약체결",
    "영업(잠정)실적",
    "매출액또는손익구조",
    "자기주식취득결정",
    "자기주식취득신탁계약체결결정",
    "주식소각결정",
    "무상증자결정",
    "유형자산취득결정",
    "타법인주식및출자증권취득결정",
    "신규시설투자",
    "특허권취득",
    "기술도입ㆍ이전ㆍ제휴계약체결",
    "기술이전계약",
    "투자판단관련주요경영사항",
)
PAGE_COUNTER = re.compile(r"전체\s*<em>([\d,]+)</em>건\s*:\s*<strong>(\d+)</strong>/(\d+)")
DISCLOSURE_ROW = re.compile(r'<tr id="parkman".*?</tr>', re.S)
PUBLICATION_MINUTE = re.compile(r'<td class="first txc">(\d\d:\d\d)</td>')
ACCEPTANCE_ID = re.compile(r"openDisclsViewer\('(\d{14})'")
CORP_CODE = re.compile(r"companysummary_open\('(\w+)'\)")
DISCLOSURE_TITLE = re.compile(r"openDisclsViewer\('\d{14}'[^>]*title='([^']*)'")


def kst_now() -> datetime:
    return datetime.now(KST)


def in_window(day: date, instant: datetime) -> bool:
    local = instant.astimezone(KST)
    return local.date() == day and INPUT_CUTOFF <= local.time() <= DEADLINE


def parse_kind_page(page: str) -> tuple[int, int, int, list[dict[str, str | None]]]:
    count = PAGE_COUNTER.search(page)
    if count is None:
        raise ValueError("KIND pagination missing")
    rows = []
    for row in DISCLOSURE_ROW.findall(page):
        fields = {
            "minute": PUBLICATION_MINUTE.search(row),
            "id": ACCEPTANCE_ID.search(row),
            "corp": CORP_CODE.search(row),
            "title": DISCLOSURE_TITLE.search(row),
        }
        if any(fields[name] is None for name in ("minute", "id", "title")):
            raise ValueError("KIND disclosure row incomplete")
        values = {
            name: unescape(match[1]).strip() if match else None for name, match in fields.items()
        }
        wall_time.fromisoformat(values["minute"] or "")
        rows.append(values)
    return int(count[1].replace(",", "")), int(count[2]), int(count[3]), rows


def is_candidate_disclosure(row: dict[str, str | None]) -> bool:
    minute = row["minute"]
    title = "".join((row["title"] or "").split())
    return (
        minute is not None
        and wall_time.fromisoformat(minute) < INPUT_CUTOFF
        and not title.startswith(AMENDMENTS)
        and any(keyword in title for keyword in DISCLOSURE_TITLES)
    )


def chart_rows(rows: Any, day: date, start: str) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("minute source did not return a list")
    prefix = day.strftime("%Y%m%d")
    valid = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid minute row")
        label = row.get("localDateTime", "")
        if not isinstance(label, str) or not re.fullmatch(r"\d{14}", label):
            raise ValueError("invalid minute timestamp")
        if not (prefix + start + "00" <= label <= prefix + "151900"):
            continue
        price = row.get("currentPrice")
        volume = row.get("accumulatedTradingVolume")
        if (
            not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not math.isfinite(price)
            or price <= 0
            or not isinstance(volume, (int, float))
            or isinstance(volume, bool)
            or not math.isfinite(volume)
            or volume < 0
        ):
            raise ValueError("invalid price or volume")
        valid.append(row)
    if len({row["localDateTime"] for row in valid}) != len(valid):
        raise ValueError("duplicate minute timestamp")
    return sorted(valid, key=lambda row: row["localDateTime"])


def missing_reference_minutes(rows: list[dict], day: date) -> list[str]:
    labels = {row["localDateTime"] for row in rows}
    date_prefix = day.strftime("%Y%m%d")
    return [minute for minute in ("144900", "151900") if date_prefix + minute not in labels]


class SourceProbe:
    def __init__(
        self,
        client: httpx.Client,
        day: date,
        historical: bool,
        clock: Callable[[], datetime] = kst_now,
    ):
        self.client = client
        self.day = day
        self.historical = historical
        self.clock = clock
        self.source_results: dict[str, dict] = {}
        self.issues: list[str] = []

    def request(
        self,
        label: str,
        url: str,
        *,
        form: dict | None = None,
        query: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response | None:
        started = self.clock()
        record: dict[str, Any] = {
            "source": url,
            "requested_at": started.isoformat(),
            "query": query or form or {},
        }
        self.source_results[label] = record
        deadline = datetime.combine(self.day, DEADLINE, KST)
        remaining = (deadline - started).total_seconds()
        if not self.historical and (
            not in_window(self.day, started) or remaining <= REQUEST_TIMEOUT
        ):
            self.issues.append(f"{label}: outside collection window")
            return None
        try:
            response = self.client.request(
                "POST" if form is not None else "GET",
                url,
                data=form,
                params=query,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            received = self.clock()
            record["received_at"] = received.isoformat()
            record["http_status"] = response.status_code
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.issues.append(f"{label}: {type(exc).__name__}")
            return None
        if not self.historical and not in_window(self.day, received):
            self.issues.append(f"{label}: response after deadline")
            return None
        return response

    def disclosures(self) -> list[dict[str, str | None]]:
        rows = []
        expected_count = expected_pages = None
        for page in count(1):
            response = self.request(
                f"kind_page_{page}",
                KIND_DISCLOSURES,
                form={
                    "method": "searchTodayDisclosureSub",
                    "currentPageSize": str(PAGE_SIZE),
                    "pageIndex": str(page),
                    "orderMode": "0",
                    "orderStat": "D",
                    "forward": "todaydisclosure_sub",
                    "chose": "S",
                    "todayFlag": "N",
                    "selDate": self.day.isoformat(),
                },
            )
            if response is None:
                break
            try:
                total, current, pages, found = parse_kind_page(response.text)
                if page == 1:
                    expected_count, expected_pages = total, pages
                if (total, current, pages) != (expected_count, page, expected_pages):
                    raise ValueError("KIND pagination changed during collection")
                if total and (not found or pages != math.ceil(total / PAGE_SIZE)):
                    raise ValueError("KIND pagination and row count disagree")
            except ValueError as exc:
                self.issues.append(str(exc))
                break
            rows.extend(found)
            if page >= pages:
                break
            time.sleep(0.3)
        if len(rows) != expected_count or len({row["id"] for row in rows}) != expected_count:
            self.issues.append("KIND disclosure list incomplete or duplicate")
        return rows

    def chart(self, kind: str, symbol: str, start: str) -> list[dict]:
        prefix = self.day.strftime("%Y%m%d")
        label = f"{kind}_{symbol}"
        response = self.request(
            label,
            f"{NAVER_CHART}/{kind}/{symbol}/minute",
            query={
                "startDateTime": prefix + start,
                "endDateTime": prefix + "1519",
            },
        )
        if response is None:
            return []
        try:
            rows = chart_rows(response.json(), self.day, start)
        except ValueError as exc:
            self.issues.append(f"{label}: {exc}")
            return []
        if missing_reference_minutes(rows, self.day):
            self.issues.append(f"{label}: 14:49 or 15:19 minute unavailable")
        return rows

    def reference(self, day: date) -> dict[str, dict[str, dict]]:
        key = load_key()
        output: dict[str, dict[str, dict]] = {}
        if not key:
            self.issues.append("KRX public market-data key unavailable")
            return output
        for kind in ("stk", "ksq", "base/stk", "base/ksq"):
            response = self.request(
                f"krx_{kind}",
                f"{BASE_URL}/{ENDPOINTS[kind]}",
                query={"basDd": day.strftime("%Y%m%d")},
                headers={"AUTH_KEY": key},
            )
            if response is None:
                continue
            try:
                rows = response.json()["OutBlock_1"]
                if not isinstance(rows, list) or not rows:
                    raise ValueError("KRX reference rows missing")
                by_code = {r.get("ISU_SRT_CD", r.get("ISU_CD")): r for r in rows}
                if None in by_code or len(by_code) != len(rows):
                    raise ValueError("KRX reference codes missing or duplicate")
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                self.issues.append(f"krx_{kind}: {type(exc).__name__}")
                continue
            output[kind] = by_code
        return output

    def market_action_pages(self) -> dict[str, str]:
        # Preserve responses for review. These pages do not certify the full point-in-time status.
        pages = {}
        for flag, (path, method, forward) in MARKET_ACTION_PAGES.items():
            form = {
                "method": method,
                "forward": forward,
                "pageIndex": "1",
                "currentPageSize": str(PAGE_SIZE),
            }
            if flag == "halt":
                form["marketType"] = "0"
            if flag in ("warning", "risk"):
                form.update(
                    menuIndex="2" if flag == "warning" else "3", searchFromDate=self.day.isoformat()
                )
            response = self.request(f"kind_{flag}", f"{KIND}/investwarn/{path}.do", form=form)
            if response is not None and response.text.strip():
                pages[flag] = response.text
            else:
                self.issues.append(f"kind_{flag}: missing response")
        return pages

    def collect(self, reference_date: date) -> dict:
        started = self.clock()
        disclosures = self.disclosures()
        indexes = {code: self.chart("index", code, "1445") for code in ("KOSPI", "KOSDAQ")}
        reference = self.reference(reference_date)
        master = reference.get("base/stk", {}) | reference.get("base/ksq", {})
        prices = reference.get("stk", {}) | reference.get("ksq", {})
        candidates = set()
        candidate_ids = []
        for disclosure in disclosures:
            if not is_candidate_disclosure(disclosure):
                continue
            candidate_ids.append(disclosure["id"])
            corp = disclosure["corp"] or ""
            code = corp + "0" if re.fullmatch(r"[0-9A-Z]{5}", corp) else ""
            if code not in master:
                self.issues.append(f"KIND {disclosure['id']}: no match in KRX security master")
                continue
            candidates.add(code)
        actions = self.market_action_pages() if not self.historical else {}
        stocks = {}
        for code in sorted(candidates):
            stocks[code] = {
                "security": master[code],
                "reference": prices.get(code),
                "minutes": self.chart("item", code, "0900"),
            }
            if code not in prices:
                self.issues.append(f"{code}: reference trading data unavailable")
            time.sleep(0.15)
        finished = self.clock()
        if not self.historical and not in_window(self.day, finished):
            self.issues.append("capture finished after deadline")
        return {
            "date": self.day.isoformat(),
            "reference_date": reference_date.isoformat(),
            "mode": "HISTORICAL_SOURCE_CHECK" if self.historical else "LIVE_SOURCE_CHECK",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "verdict": "INPUT_CHECK_ONLY",
            "issues": self.issues,
            "sources": self.source_results,
            "unverified_fields": ["complete point-in-time market actions", "KRX auction volumes"],
            "disclosures": disclosures,
            "candidate_disclosure_ids": candidate_ids,
            "index_minutes": indexes,
            "stock_minutes": stocks,
            "market_action_responses": actions,
        }

    def recheck(self, first: dict) -> dict:
        finished = datetime.fromisoformat(first["finished_at"])
        if (
            first.get("mode") != "LIVE_SOURCE_CHECK"
            or first.get("date") != self.day.isoformat()
            or finished.tzinfo is None
            or not in_window(self.day, finished)
        ):
            raise ValueError("recheck requires the same day's on-time live snapshot")

        jobs = [
            ("index", code, "1445", first["index_minutes"][code]) for code in ("KOSPI", "KOSDAQ")
        ]
        jobs += [
            ("item", code, "0900", stock["minutes"])
            for code, stock in sorted(first["stock_minutes"].items())
        ]
        if any(
            kind == "item" and not re.fullmatch(r"[0-9A-Z]{6}", code) for kind, code, _, _ in jobs
        ):
            raise ValueError("invalid symbol in first snapshot")

        started = self.clock()
        comparisons = {}
        for kind, code, start, raw_rows in jobs:
            seen = chart_rows(raw_rows, self.day, start)
            missing = missing_reference_minutes(seen, self.day)
            if not missing:
                continue
            later = self.chart(kind, code, start)
            still_missing = missing_reference_minutes(later, self.day)
            comparisons[f"{kind}_{code}"] = {
                "first_missing": missing,
                "recovered": [minute for minute in missing if minute not in still_missing],
                "still_missing": [minute for minute in missing if minute in still_missing],
                "minutes": later,
            }
            time.sleep(0.15)
        return {
            "date": self.day.isoformat(),
            "mode": "LIVE_MINUTE_RECHECK",
            "first_finished_at": first["finished_at"],
            "started_at": started.isoformat(),
            "finished_at": self.clock().isoformat(),
            "verdict": "INPUT_CHECK_ONLY",
            "comparisons": comparisons,
            "sources": self.source_results,
            "issues": self.issues,
            "unverified_fields": first["unverified_fields"],
        }


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A repeat or simultaneous invocation cannot overwrite observed data.
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        path.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--reference-date", type=date.fromisoformat, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--historical", action="store_true")
    mode.add_argument("--retry-missing", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    now = kst_now()
    if (
        args.reference_date >= args.date
        or args.date.weekday() >= 5
        or is_late_open(args.date.strftime("%Y%m%d"))
    ):
        parser.error("requires a regular weekday and an earlier reference trading date")
    if args.historical and args.date >= now.date():
        parser.error("historical checks require a past date")
    if not args.historical and not in_window(args.date, now):
        parser.error("live collection requires the requested date at 15:20–15:25 KST")
    suffix = "recheck" if args.retry_missing else ("historical" if args.historical else "live")
    path = args.out_dir / f"{args.date:%Y%m%d}-{suffix}.json"
    if path.exists():
        parser.error(f"snapshot already exists: {path}")
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}) as client:
        probe = SourceProbe(client, args.date, args.historical)
        if args.retry_missing:
            first_path = args.out_dir / f"{args.date:%Y%m%d}-live.json"
            try:
                first = json.loads(first_path.read_text(encoding="utf-8"))
                if first.get("reference_date") != args.reference_date.isoformat():
                    raise ValueError("reference trading date differs from the initial snapshot")
                snapshot = probe.recheck(first)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                parser.error(f"invalid or unavailable live snapshot: {exc}")
        else:
            snapshot = probe.collect(args.reference_date)
    write_snapshot(path, snapshot)
    print(
        json.dumps(
            {"path": str(path), "verdict": snapshot["verdict"], "issues": snapshot["issues"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

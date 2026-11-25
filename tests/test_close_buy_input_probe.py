from datetime import date, datetime

import httpx
import pytest

from scripts.close_buy_input_probe import (
    SourceProbe,
    chart_rows,
    in_window,
    is_candidate_disclosure,
    parse_kind_page,
    write_snapshot,
)

DAY = date(2026, 11, 25)


def instant(clock: str) -> datetime:
    return datetime.fromisoformat(f"{DAY.isoformat()}T{clock}+09:00")


def kind_row(minute: str, disclosure_id: str) -> str:
    return (
        f'<tr id="parkman"><td class="first txc">{minute}</td>'
        "<a onclick=\"companysummary_open('00593')\">삼성전자</a>"
        f"<a onclick=\"openDisclsViewer('{disclosure_id}')\" title='신규시설투자'>공시</a></tr>"
    )


def test_disclosures_use_displayed_minute_across_receipt_dates() -> None:
    html = (
        "전체 <em>2</em>건 : <strong>1</strong>/1"
        + kind_row("15:19", "20261124000746")
        + kind_row("15:20", "20261125000747")
    )
    total, page, pages, rows = parse_kind_page(html)

    assert (total, page, pages) == (2, 1, 1)
    assert [is_candidate_disclosure(row) for row in rows] == [True, False]
    with pytest.raises(ValueError, match="pagination missing"):
        parse_kind_page("<html>Service unavailable</html>")


def test_duplicate_disclosures_are_reported() -> None:
    row = kind_row("14:55", "20261125000746")
    html = "전체 <em>2</em>건 : <strong>1</strong>/1" + row + row
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))
    ) as client:
        probe = SourceProbe(client, DAY, False, clock=lambda: instant("15:20:03"))
        assert len(probe.disclosures()) == 2
    assert "KIND disclosure list incomplete or duplicate" in probe.issues


def test_chart_discards_future_and_other_day_minutes() -> None:
    rows = [
        {"localDateTime": t, "currentPrice": 10_000, "accumulatedTradingVolume": 1}
        for t in ("20261125144900", "20261124151900", "20261125151900", "20261125152000")
    ]

    assert chart_rows(rows, DAY, "1445") == [rows[0], rows[2]]
    with pytest.raises(ValueError, match="duplicate minute timestamp"):
        chart_rows([rows[0], rows[0]], DAY, "1445")


def test_no_request_outside_window_or_with_insufficient_time() -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        pytest.fail("network request attempted")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        times = ("15:19:59", "15:24:56", "15:25:01")
        for clock in times:
            probe = SourceProbe(client, DAY, False, clock=lambda: instant(clock))
            assert probe.request("index", "https://example.invalid") is None
            assert probe.issues == ["index: outside collection window"]
    assert in_window(DAY, instant("15:20:00"))


def test_response_after_window_is_recorded_but_not_used() -> None:
    instants = iter((instant("15:24:50"), instant("15:25:01")))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        probe = SourceProbe(client, DAY, False, clock=lambda: next(instants))
        assert probe.request("index", "https://example.invalid") is None

    assert probe.issues == ["index: response after deadline"]
    assert probe.source_results["index"]["received_at"].endswith("15:25:01+09:00")


def test_snapshot_cannot_overwrite_or_leave_partial(tmp_path) -> None:
    output = tmp_path / "20261125-live.json"
    write_snapshot(output, {"first": True})

    with pytest.raises(FileExistsError):
        write_snapshot(output, {"second": True})
    with pytest.raises(ValueError):
        write_snapshot(tmp_path / "bad.json", {"price": float("nan")})

    assert output.read_text(encoding="utf-8") == '{"first": true}\n'
    assert list(tmp_path.iterdir()) == [output]


def test_recheck_recovers_only_missing_minutes_without_changing_first_snapshot() -> None:
    def bar(minute: str) -> dict:
        return {
            "localDateTime": f"20261125{minute}00",
            "currentPrice": 10_000,
            "accumulatedTradingVolume": 1,
        }

    complete = [bar("1449"), bar("1519")]
    initial_rows = [bar("1449")]
    first = {
        "date": DAY.isoformat(),
        "mode": "LIVE_SOURCE_CHECK",
        "finished_at": instant("15:20:33").isoformat(),
        "index_minutes": {"KOSPI": complete, "KOSDAQ": complete},
        "stock_minutes": {"005930": {"minutes": initial_rows}, "000660": {"minutes": complete}},
        "unverified_fields": ["KRX auction volumes"],
    }
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.params["endDateTime"] == "202611251519"
        return httpx.Response(200, json=[*complete, bar("1520")])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        probe = SourceProbe(client, DAY, False, clock=lambda: instant("15:23:00"))
        result = probe.recheck(first)
        with pytest.raises(ValueError, match="on-time live snapshot"):
            probe.recheck({**first, "finished_at": instant("15:25:01").isoformat()})

    assert result["verdict"] == "INPUT_CHECK_ONLY"
    assert requests == ["/chart/domestic/item/005930/minute"]
    assert result["comparisons"]["item_005930"] == {
        "first_missing": ["151900"],
        "recovered": ["151900"],
        "still_missing": [],
        "minutes": complete,
    }
    assert first["stock_minutes"]["005930"]["minutes"] == [bar("1449")]

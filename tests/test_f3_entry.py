import asyncio
import os as _os
import time
from datetime import timedelta as _timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.modules.f3_entry as f3
from src import state

_REAL_FETCH_BUYABLE_QTY = f3._fetch_buyable_qty
_REAL_FETCH_ORDER_FILL_SNAPSHOT = f3._fetch_order_fill_snapshot
_REAL_FETCH_FINAL_ENTRY_QUOTE = f3._fetch_final_entry_quote


def _entry_quote(ask_price: float) -> f3.EntryQuote:
    return f3.EntryQuote(
        ask_price=ask_price,
        ask_qty=999_999,
        antc_price=0,
        fetched_monotonic=f3.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )


def _mock_final_quote(monkeypatch, ask_price: float) -> None:
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(side_effect=lambda _ticker: _entry_quote(ask_price)),
    )


def _buyable(
    qty: int = 999_999,
    amt: float = 999_999_999.0,
    ord_psbl_cash: float = 0.0,
) -> dict:
    """주문가능 조회 응답. 기본값은 수량·금액 어느 쪽도 제약이 아닌 상태다.

    ord_psbl_cash 기본값이 0인 것은 "브로커가 이 필드를 주지 않았다"는 뜻이고,
    그러면 수량 산정은 _fetch_available_cash 로 물러난다. 대부분의 테스트가
    현금을 그쪽으로 통제하므로 기본값을 0으로 두어야 각 테스트가 의도한 예산을
    그대로 쓴다. 실제 주문가능액으로 산정되는 경로는 이 값을 명시하는
    테스트에서 검증한다.
    """
    return {
        "nrcvb_buy_qty": qty,
        "nrcvb_buy_amt": amt,
        "max_buy_qty": qty,
        "max_buy_amt": amt,
        "ord_psbl_cash": ord_psbl_cash,
    }


def _reset_state() -> None:
    s = state.get()
    s.trading_date = "20260701"
    s.target_ticker = "006340"
    s.target_name = None
    s.target_candidates = None
    s.entry_price = None
    s.entry_qty = None
    s.remaining_qty = None
    s.high_price = None
    s.position_status = "IDLE"
    s.close_reason = None
    s.order_id = None
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0
    s.day_skip = False
    s.pending_entry = None


@pytest.fixture(autouse=True)
def reset_fill_poll_summary(monkeypatch):
    f3._last_fill_poll_summary = {}
    # 운영과 동일한 지정가 경로에서 각 테스트가 신선한 최종 호가를 받는다.
    monkeypatch.setattr(f3, "F3_PRE_ORDER_QUIET_SEC", 0)
    _mock_final_quote(monkeypatch, 10_310)
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        AsyncMock(return_value=None),
    )
    # 기존 차단/후보 전환 테스트는 대기 한도 0으로 고정한다. VI 해제 대기
    # 동작은 전용 테스트에서 양수로 덮어써 검증한다.
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable()))
    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(return_value=None))
    monkeypatch.setattr(f3.db, "record_entry_order_attempt", AsyncMock(return_value=1))
    # 마감 검사를 실제 시계와 분리 — 마감 동작 테스트는 개별적으로 False를 덮어쓴다
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: True)
    yield
    f3._last_fill_poll_summary = {}


def test_removed_env_var_is_warned_not_silently_ignored(monkeypatch):
    """.env에 남은 제거 설정은 조용히 무시되면 안 된다.

    F3_MAX_ENTRY_SLIPPAGE_RATIO는 이름이 바뀐 게 아니라 삭제된 안전장치라,
    운영자가 "아직 슬리피지 상한이 걸려 있다"고 오해할 위험이 크다.
    """
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setenv("F3_MAX_ENTRY_SLIPPAGE_RATIO", "0.005")

    f3._warn_removed_env_vars()

    removed = [kwargs for event, kwargs in events if event == "F3_ENV_REMOVED"]
    assert len(removed) == 1
    assert removed[0]["level"] == "WARN"
    assert removed[0]["removed_env"] == "F3_MAX_ENTRY_SLIPPAGE_RATIO"
    assert removed[0]["removed_value"] == "0.005"
    assert removed[0]["replacement_env"] == "F3_ASK_SLIPPAGE_RATIO"


def test_removed_env_var_warning_is_silent_when_unset(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.delenv("F3_MAX_ENTRY_SLIPPAGE_RATIO", raising=False)

    f3._warn_removed_env_vars()

    assert [event for event, _ in events if event == "F3_ENV_REMOVED"] == []


def test_market_buy_setting_cannot_disable_mandatory_limit_safety(monkeypatch):
    events = []
    monkeypatch.setenv("F3_LIMIT_BUY_ENABLED", "0")
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    f3._warn_removed_env_vars()

    warning = [kwargs for event, kwargs in events if event == "F3_LIMIT_BUY_REQUIRED"][-1]
    assert warning["configured_value"] == "0"
    assert warning["effective_value"] == "1"


def test_fill_gap_reaches_max_boundary():
    """체결가 갭이 정확히 상한(10%)이면 청산 대상이다 (>=, 초과가 아닌 이상)."""
    assert f3._fill_gap_reaches_max(f3.GAP_MAX_FILL) is True
    assert f3._fill_gap_reaches_max(f3.GAP_MAX_FILL - 1e-9) is False


def test_entry_limit_price_uses_final_ask_cap(monkeypatch):
    """7/30 위닉스 호가는 신선한 매도호가 +1% 상한을 사용한다."""
    monkeypatch.setattr(f3, "F3_ASK_SLIPPAGE_RATIO", 0.01)
    gap_cap = f3._strict_gap_cap(4_490, expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT)

    limit_price, ask_cap = f3._entry_limit_price(4_690, gap_cap)

    assert ask_cap == 4_735
    assert gap_cap == 4_935
    assert limit_price == 4_735

    limit_price, ask_cap = f3._entry_limit_price(4_900, gap_cap)

    assert ask_cap == 4_945
    assert limit_price == gap_cap


def test_final_quote_age_default_is_mode_specific():
    assert f3._default_final_quote_max_age_ms("PAPER") == 1_500
    assert f3._default_final_quote_max_age_ms("REAL") == 500
    assert f3._effective_final_quote_max_age_ms("PAPER", 500) == 1_500
    assert f3._effective_final_quote_max_age_ms("PAPER", 0) == 0
    assert f3._effective_final_quote_max_age_ms("REAL", 500) == 500


@pytest.mark.parametrize(
    ("prev_close", "expected_cap"),
    [
        (1_300, 1_429),
        (1_500, 1_649),
        (3_000, 3_295),
        (7_000, 7_690),
        (10_000, 10_990),
        (50_000, 54_900),
        (100_000, 109_900),
        (200_000, 219_500),
    ],
)
def test_strict_gap_cap_never_reaches_order_gap_boundary(
    prev_close,
    expected_cap,
):
    # 적격 고갭 유동성일 때만 10% 상한을 사용한다 (저대금/부재는 8% 전용 테스트).
    cap = f3._strict_gap_cap(prev_close, expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT)
    exact_boundary = Decimal(prev_close) * Decimal("1.10")

    assert cap == expected_cap
    assert Decimal(str(cap)) < exact_boundary
    assert Decimal(str(cap + f3._tick_size(cap))) >= exact_boundary


def test_strict_gap_cap_has_no_boundary_violations_across_valid_krx_ticks():
    violations = []
    price_bands = (
        range(1, 2_000, 1),
        range(2_000, 5_000, 5),
        range(5_000, 20_000, 10),
        range(20_000, 50_000, 50),
        range(50_000, 200_000, 100),
        range(200_000, 500_000, 500),
        range(500_000, 1_000_001, 1_000),
    )

    for band in price_bands:
        for prev_close in band:
            cap = f3._strict_gap_cap(prev_close)
            boundary = Decimal(prev_close) * Decimal("1.10")
            if Decimal(str(cap)) >= boundary:
                violations.append((prev_close, cap))

    assert violations == []


@pytest.mark.asyncio
async def test_send_buy_uses_gap_cap_limit_order(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3.kis_rest, "post", post)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")

    await f3._send_buy("006340", 48, "PAPER", limit_price=14_510)

    body = post.await_args.kwargs["body"]
    assert body["ORD_DVSN"] == "00"
    assert body["ORD_UNPR"] == "14510"
    assert body["ORD_QTY"] == "48"


@pytest.mark.asyncio
async def test_paper_rate_wait_keeps_final_quote_fresh_with_1500ms_guard(monkeypatch):
    """PAPER 1.1초 레이트리미터를 실제 전송 가드 순서로 통과해야 한다."""

    class Response:
        status_code = 200

        def json(self):
            return {
                "rt_cd": "0",
                "output": {
                    "ODNO": "0000000937",
                    "KRX_FWDG_ORD_ORGNO": "001",
                },
            }

    class Client:
        calls = 0
        is_closed = False

        def __init__(self, *args, **kwargs):
            pass

        async def request(self, *args, **kwargs):
            type(self).calls += 1
            return Response()

        async def aclose(self):
            self.is_closed = True

    quote = f3.EntryQuote(
        ask_price=14_500,
        ask_qty=100,
        antc_price=0,
        fetched_monotonic=f3.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    monkeypatch.setattr(f3, "F3_FINAL_QUOTE_MAX_AGE_MS", 1_500)
    monkeypatch.setattr(f3.kis_rest, "_RATE_INTERVAL", 1.1)
    monkeypatch.setattr(f3.kis_rest, "_last_call_at", f3.time.monotonic())
    monkeypatch.setattr(f3.kis_rest, "_client", None)
    monkeypatch.setattr(f3.kis_rest, "_client_factory", None)
    monkeypatch.setattr(f3.kis_rest.httpx, "AsyncClient", Client)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")

    response = await f3._send_buy(
        "006340",
        48,
        "PAPER",
        limit_price=14_510,
        send_guard=lambda: f3._quote_is_fresh(quote),
    )

    assert response["rt_cd"] == "0"
    assert Client.calls == 1
    assert 1_000 <= f3._quote_age_ms(quote) <= 1_500


@pytest.mark.asyncio
async def test_cancel_order_uses_official_limit_cancel_contract(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3.kis_rest, "post", post)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")

    await f3._cancel_order("0000000937", "001", "PAPER")

    body = post.await_args.kwargs["body"]
    assert body["ORD_DVSN"] == "00"
    assert body["RVSE_CNCL_DVSN_CD"] == "02"
    assert body["QTY_ALL_ORD_YN"] == "Y"
    assert body["ORD_QTY"] == "0"
    assert body["EXCG_ID_DVSN_CD"] == "KRX"


def test_qty_clamp_level_uses_configured_reduction_threshold(monkeypatch):
    monkeypatch.setattr(f3, "F3_QTY_CLAMP_WARN_PCT", 20.0)

    assert f3._qty_clamp_reduction_pct(657, 558) == pytest.approx(15.07)
    assert f3._qty_clamp_log_level(657, 558) == "INFO"
    assert f3._qty_clamp_log_level(9, 5) == "WARN"


def test_parse_deadline_logs_invalid_value(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = f3._parse_deadline("09:bad:08", (9, 0, 8))

    assert result == (9, 0, 8)
    assert events[0][0] == "F3_DEADLINE_PARSE_ERROR"
    assert events[0][1]["value"] == "09:bad:08"
    assert events[0][1]["default"] == "09:00:08"


@pytest.mark.asyncio
async def test_pre_order_quiet_wait_logs_and_sleeps(monkeypatch):
    events = []
    sleep = AsyncMock()

    monkeypatch.setattr(f3, "F3_PRE_ORDER_QUIET_SEC", 1.5)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f3._pre_order_quiet_wait("006340", 1, 2, 12900.0, 774)

    sleep.assert_awaited_once_with(1.5)
    assert events == [
        (
            "ENTRY_PRE_ORDER_WAIT",
            {
                "level": "INFO",
                "ticker": "006340",
                "phase": "ENTRY",
                "sleep_sec": 1.5,
                "order_price": 12900.0,
                "order_qty": 774,
                "entry_attempt": 1,
                "max_attempts": 2,
            },
        )
    ]


@pytest.mark.asyncio
async def test_fetch_available_cash_prefers_orderable_cash(monkeypatch):
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output2": [
                    {
                        "ord_psbl_cash": "1,234",
                        "dnca_tot_amt": "9,999",
                        "prvs_rcdl_excc_amt": "8,888",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert await f3._fetch_available_cash() == 1234.0
    assert events == [
        (
            "BALANCE_CASH_CHECK",
            {
                "level": "DEBUG",
                "cash": 1234.0,
                "cash_source": "ord_psbl_cash",
                "ord_psbl_cash": 1234.0,
                "ord_psbl_present": True,
                "dnca_tot_amt": 9999.0,
                "prvs_rcdl_excc_amt": 8888.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_fetch_available_cash_does_not_fall_back_when_orderable_cash_is_zero(monkeypatch):
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output2": [
                    {
                        "ord_psbl_cash": "0",
                        "dnca_tot_amt": "2,345",
                        "prvs_rcdl_excc_amt": "8,888",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 0.0


@pytest.mark.asyncio
async def test_fetch_available_cash_falls_back_when_orderable_cash_missing(monkeypatch):
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output2": [
                    {
                        "dnca_tot_amt": "3,456",
                        "prvs_rcdl_excc_amt": "2,345",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 3456.0


@pytest.mark.asyncio
async def test_fetch_available_cash_uses_settlement_amount_when_larger(monkeypatch):
    # 미결제 상태: 예수금(dnca)은 작지만 가수도정산금액(prvs)이 1차 예산에 더 가깝다.
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output2": [
                    {
                        "dnca_tot_amt": "120,543",
                        "prvs_rcdl_excc_amt": "8,865,465",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert await f3._fetch_available_cash() == 8_865_465.0
    assert events[0][1]["cash_source"] == "prvs_rcdl_excc_amt"


@pytest.mark.asyncio
async def test_fetch_available_cash_retries_rate_limit_then_succeeds(monkeypatch):
    """호출 제한 오류는 짧은 백오프 후 재시도해 정상 응답을 얻는다."""
    sleep = AsyncMock()
    get = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"},
            {
                "rt_cd": "0",
                "output2": [
                    {
                        "ord_psbl_cash": "5,000",
                        "dnca_tot_amt": "0",
                        "prvs_rcdl_excc_amt": "0",
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 5000.0
    assert get.await_count == 2
    sleep.assert_awaited_once_with(f3.BALANCE_QUERY_RETRY_DELAY_SEC)


@pytest.mark.asyncio
async def test_fetch_available_cash_returns_none_when_retries_exhausted(monkeypatch):
    """잔고 조회가 끝내 실패하면 현금 0이 아니라 None(조회 실패)을 반환한다."""
    sleep = AsyncMock()
    get = AsyncMock(
        return_value={
            "rt_cd": "1",
            "msg_cd": "EGW00201",
            "msg1": "초당 거래건수 초과",
        }
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() is None
    assert get.await_count == f3.BALANCE_QUERY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_fetch_available_cash_retries_exception_then_succeeds(monkeypatch):
    """kis_rest.get 예외(JSON 파싱 오류 등)도 오류 응답과 동일하게 재시도한다."""
    sleep = AsyncMock()
    get = AsyncMock(
        side_effect=[
            RuntimeError("json parse error"),
            {
                "rt_cd": "0",
                "output2": [
                    {
                        "ord_psbl_cash": "5,000",
                        "dnca_tot_amt": "0",
                        "prvs_rcdl_excc_amt": "0",
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 5000.0
    assert get.await_count == 2


@pytest.mark.asyncio
async def test_fetch_available_cash_returns_none_when_exceptions_exhausted(monkeypatch):
    """예외가 계속되면 전파하지 않고 None을 반환해 단일 경로도 BALANCE_QUERY_FAILED로 수렴한다."""
    sleep = AsyncMock()
    get = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() is None
    assert get.await_count == f3.BALANCE_QUERY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_fetch_buyable_qty_uses_market_order_psbl_api(monkeypatch):
    events = []
    get = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output": {
                "nrcvb_buy_qty": "12",
                "nrcvb_buy_amt": "123,000",
                "max_buy_qty": "15",
                "max_buy_amt": "150,000",
                "ord_psbl_cash": "200,000",
            },
        }
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = await _REAL_FETCH_BUYABLE_QTY("005930", "PAPER")

    assert result["query_failed"] is False
    assert result["nrcvb_buy_qty"] == 12
    assert result["nrcvb_buy_amt"] == 123000.0
    get.assert_awaited_once()
    assert get.await_args.args == ("/uapi/domestic-stock/v1/trading/inquire-psbl-order",)
    assert get.await_args.kwargs["tr_id"] == "VTTC8908R"
    assert get.await_args.kwargs["params"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "005930",
        "ORD_UNPR": "",
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "N",
        "OVRS_ICLD_YN": "N",
    }
    assert events[-1][0] == "BUYABLE_QTY_CHECK"


@pytest.mark.asyncio
async def test_fetch_buyable_qty_uses_real_tr_id(monkeypatch):
    get = AsyncMock(return_value={"rt_cd": "0", "output": {"nrcvb_buy_qty": "1"}})
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    await _REAL_FETCH_BUYABLE_QTY("005930", "REAL")

    assert get.await_args.kwargs["tr_id"] == "TTTC8908R"


@pytest.mark.asyncio
async def test_fetch_buyable_qty_error_returns_zero(monkeypatch):
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "ERR", "msg1": "no cash"}),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = await _REAL_FETCH_BUYABLE_QTY("005930", "PAPER")

    assert result["query_failed"] is True
    assert result["nrcvb_buy_qty"] == 0
    assert result["msg_cd"] == "ERR"
    assert events[-1][0] == "BUYABLE_QTY_ERROR"
    assert events[-1][1]["msg_cd"] == "ERR"


@pytest.mark.asyncio
async def test_entry_fail_logs_fill_poll_summary(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3,
        "_cancel_order",
        AsyncMock(return_value={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "CANCELED"}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value=None),
    )
    f3._last_fill_poll_summary = {
        "poll_attempts": 6,
        "poll_last_rt_cd": "0",
        "poll_last_msg_cd": "MCA00000",
        "poll_last_output_count": 0,
        "poll_last_matched": False,
    }

    await f3.run()

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "UNFILLED"
    assert entry_fail["order_id"] == "0000000937"
    assert entry_fail["poll_attempts"] == 6
    assert entry_fail["poll_last_matched"] is False


@pytest.mark.asyncio
async def test_price_unavailable_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(0.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "GAP_RECHECK_UNAVAILABLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_prev_close_zero_blocks_entry_with_warn_log(monkeypatch):
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 0.0)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "GAP_RECHECK_UNAVAILABLE"
    unavailable = [kwargs for event, kwargs in events if event == "GAP_RECHECK_UNAVAILABLE"][-1]
    assert unavailable["reason"] == "MISSING_PREV_CLOSE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_insufficient_balance_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "QTY_ZERO"
    assert state.get().day_skip is True
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_first_order_blocked_when_deadline_passed_before_balance(monkeypatch):
    """비강제 실행에서 마감(09:11)을 넘겼으면 잔고 조회 없이 최초 주문 전에 차단한다."""
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    fetch_cash = AsyncMock(return_value=1_000_000.0)
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch_cash)
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: False)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    fetch_cash.assert_not_awaited()
    assert state.get().day_skip is True
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "ENTRY_DEADLINE_PASSED" in blocked
    f3.db.record_skip.assert_awaited_once()
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_first_order_blocked_when_deadline_passed_with_picked(monkeypatch):
    """배치 경로에서 잔고 재시도로 지연된 경우에도 주문 직전 마감 검사가 최초 주문을 막는다."""
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()
    picked = {
        "ticker": "006340",
        "expected_price": 10310.0,
        "prev_close": 10000.0,
        "cash": 1_000_000.0,
        "total_amount": 100_000,
        "total_qty": 9,
    }

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: False)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(picked=picked)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_send_buy_blocked_when_deadline_passes_during_prep(monkeypatch):
    """초기 검사 통과 후 quiet wait·수량 조회 중 마감을 넘기면
    주문 직전 최종 검사가 전송을 막고 ENTERING을 IDLE로 되돌린다."""
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()

    # 잔고 조회 전·1차 주문 전 검사는 통과, _send_buy 직전 최종 검사에서 마감 초과
    deadline_checks = iter([True, True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "ENTRY_DEADLINE_PASSED" in blocked
    f3.db.record_skip.assert_awaited_once()
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]
    assert "stage=AT_ORDER" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_send_buy_blocked_when_deadline_passes_during_rest_rate_wait(monkeypatch):
    """A REST dispatch guard rejection must restore IDLE and use the deadline path."""
    events = []
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "1",
            "msg_cd": f3.kis_rest.SEND_GUARD_BLOCKED_MSG_CD,
            "msg1": "request blocked by send guard",
        }
    )
    _reset_state()

    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: True)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_awaited_once()
    assert callable(send_buy.await_args.kwargs["send_guard"])
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    event_names = [event for event, _kwargs in events]
    assert "ENTRY_ORDER_SENT" not in event_names
    assert "ENTRY_DEADLINE_PASSED" in event_names
    assert "stage=AT_HTTP_SEND" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_run_single_cash_none_blocks_with_balance_query_failed(monkeypatch):
    (
        "단일 진입 경로에서도 잔고 조회 실패(None)는 QTY_ZERO가 아니라 "
        "BALANCE_QUERY_FAILED로 차단한다."
    )
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "QTY_ZERO" not in blocked
    send_buy.assert_not_awaited()
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_order_rejected_sets_day_skip(monkeypatch):
    events = []
    notify = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "7",
                "msg_cd": "APBK1234",
                "msg1": "rejected",
                "output": {},
            }
        ),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "ORDER_REJECTED"
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"
    assert notify.await_args.kwargs["ticker"] == "006340"
    assert "rejected" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_empty_order_id_is_rejected_even_when_rt_cd_is_success(monkeypatch):
    events = []
    _reset_state()
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "missing ODNO",
                "output": {"ODNO": ""},
            }
        ),
    )
    poll_fill = AsyncMock()
    monkeypatch.setattr(f3, "_poll_fill", poll_fill)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    poll_fill.assert_not_awaited()
    rejected = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"]
    assert rejected[-1]["reason"] == "ORDER_REJECTED"


@pytest.mark.asyncio
async def test_market_closed_rejection_records_market_closed_skip(monkeypatch):
    events = []
    notify = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "1",
                "msg_cd": "40100000",
                "msg1": "모의투자 영업일이 아닙니다.",
                "output": {},
            }
        ),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert "MARKET_CLOSED" in [event for event, _ in events]
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "MARKET_CLOSED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "MARKET_CLOSED"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "MARKET_CLOSED"
    assert "휴장일" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_market_closed_rejection_stops_candidate_retry(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "1",
            "msg_cd": "40100000",
            "msg1": "모의투자 영업일이 아닙니다.",
            "output": {},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    # 휴장 거부는 다른 후보로 재시도해도 같은 결과 — 즉시 당일 중단해야 한다
    send_buy.assert_awaited_once()
    assert "ENTRY_CANDIDATE_RETRY" not in [event for event, _ in events]
    assert state.get().day_skip is True
    assert state.get().close_reason == "MARKET_CLOSED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "MARKET_CLOSED"
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "MARKET_CLOSED"


@pytest.mark.asyncio
async def test_state_collision_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()
    state.get().position_status = "HOLDING"

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run()

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "STATE_NOT_IDLE"
    assert blocked["position_status"] == "HOLDING"
    send_buy.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_open_trade_blocks_new_entry_and_restores_holding(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(
            return_value={
                "id": 77,
                "date": "20260708",
                "ticker": "005930",
                "name": "삼성전자",
                "entry_price": 75000.0,
                "entry_qty": 10,
                "status": "OPEN",
            }
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().target_ticker == "005930"
    assert state.get().target_name == "삼성전자"
    assert state.get().trade_id == 77
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "TRADE_ALREADY_EXISTS"
    assert blocked["existing_status"] == "OPEN"


@pytest.mark.asyncio
async def test_existing_open_trade_clears_stale_target_name_when_db_has_no_name(monkeypatch):
    _reset_state()
    state.get().target_name = "다른후보"

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(
            return_value={
                "id": 77,
                "date": "20260708",
                "ticker": "005930",
                "entry_price": 75000.0,
                "entry_qty": 10,
                "status": "OPEN",
            }
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", AsyncMock())

    await f3.run()

    assert state.get().target_ticker == "005930"
    assert state.get().target_name is None


@pytest.mark.asyncio
async def test_existing_closed_trade_blocks_new_entry_and_sets_day_skip(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(
            return_value={
                "id": 78,
                "date": "20260708",
                "ticker": "065770",
                "entry_price": 1869.0,
                "entry_qty": 327,
                "status": "CLOSED",
            }
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "TRADE_ALREADY_EXISTS"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "TRADE_ALREADY_EXISTS"
    assert blocked["existing_status"] == "CLOSED"


@pytest.mark.asyncio
async def test_full_cash_quantity_places_first_buy(monkeypatch):
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.args == ("006340", 9, "PAPER")
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("warn_threshold_pct", "should_warn"),
    [(1.5, False), (0.5, True)],
)
async def test_limit_entry_allows_rising_ask_within_gap_cap(
    monkeypatch,
    warn_threshold_pct,
    should_warn,
):
    """재검증 뒤 호가가 올라도 최종 갭 10% 미만이면 진입한다."""
    _reset_state()
    events = []
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    monkeypatch.setattr(f3, "F3_QUOTE_MOVE_WARN_PCT", warn_threshold_pct)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(return_value=(4_660.0, 4_490.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_available_cash",
        AsyncMock(return_value=1_000_000.0),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            return_value=f3.EntryQuote(
                ask_price=4_690,
                ask_qty=562,
                antc_price=0,
                fetched_monotonic=f3.time.monotonic(),
                rt_cd="0",
                msg_cd="MCA00000",
                msg1="OK",
            )
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value={"fill_price": 4_690, "fill_qty": 200}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.args[1] == 200
    assert send_buy.await_args.kwargs["limit_price"] == 4_735
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False
    assert "ENTRY_PRICE_BLOCKED" not in [event for event, _ in events]
    quote_move_warnings = [kwargs for event, kwargs in events if event == "ENTRY_QUOTE_MOVE_HIGH"]
    assert bool(quote_move_warnings) is should_warn
    if should_warn:
        assert quote_move_warnings[-1]["level"] == "WARN"
        assert quote_move_warnings[-1]["quote_move_pct"] == pytest.approx(
            0.644,
            abs=0.001,
        )
    approved = [kwargs for event, kwargs in events if event == "ENTRY_PRICE_APPROVED"][-1]
    assert approved["quote_move_pct"] == pytest.approx(0.644, abs=0.001)
    sized = [kwargs for event, kwargs in events if event == "ENTRY_QTY_SIZED_AT_LIMIT"][-1]
    assert sized["reason"] == "LIMIT_PRICE_BUDGET"
    assert sized["planned_qty"] == 203
    assert sized["order_qty"] == 200
    assert not [
        kwargs
        for event, kwargs in events
        if event == "ENTRY_QTY_CLAMPED" and kwargs.get("reason") == "LIMIT_PRICE_BUDGET"
    ]
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_single_submits_absolute_gap_cap_when_ask_cap_is_higher(monkeypatch):
    """호출부가 ask 상한뿐 아니라 절대 갭 상한을 실제 제출가에 적용한다."""
    _reset_state()
    # 최종 호가 9.13%는 고갭 구간이므로 적격 유동성 후보에서만 주문이 진행된다.
    state.get().target_candidates = [
        {"ticker": "006340", "expected_amount": f3.HIGH_GAP_MIN_EXPECTED_AMOUNT}
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(4_660.0, 4_490.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(return_value=_entry_quote(4_900)),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value={"fill_price": 4_935, "fill_qty": 192}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_buy.assert_awaited_once()
    submitted_limit = send_buy.await_args.kwargs["limit_price"]
    assert (
        submitted_limit
        == f3._strict_gap_cap(4_490, expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT)
        == 4_935
    )
    assert Decimal(str(submitted_limit)) < Decimal("4490") * Decimal("1.10")
    assert send_buy.await_args.args == ("006340", 192, "PAPER")


@pytest.mark.asyncio
async def test_limit_sizing_qty_zero_is_info_when_candidate_retry_allowed(
    monkeypatch,
):
    _reset_state()
    blocked = []
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(return_value=_buyable(qty=1, amt=1_000.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            return_value=f3.EntryQuote(
                ask_price=1_030,
                ask_qty=100,
                antc_price=0,
                fetched_monotonic=f3.time.monotonic(),
                rt_cd="0",
                msg_cd="MCA00000",
                msg1="OK",
            )
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_log_entry_blocked",
        lambda ticker, reason, **kwargs: blocked.append((ticker, reason, kwargs)),
    )
    picked = {
        "ticker": "006340",
        "expected_price": 1_000.0,
        "prev_close": 970.0,
        "cash": 1_000.0,
        "total_amount": 1_000,
        "total_qty": 1,
    }

    result = await f3._run_single(
        picked=picked,
        allow_candidate_retry=True,
    )

    assert result == "QTY_ZERO"
    assert blocked[-1][1] == "QTY_ZERO"
    assert blocked[-1][2]["level"] == "INFO"
    assert blocked[-1][2]["candidate_retry"] is True
    send_buy.assert_not_awaited()


@pytest.mark.asyncio
async def test_limit_sizing_qty_zero_alerts_operator_when_day_is_skipped(monkeypatch):
    """후보 교체가 불가하면 하루를 스킵하므로 로그·알림·스킵기록이 모두 남아야 한다.

    형제 경로(QTY_ZERO/BUYABLE_QTY_ZERO)와 동일한 관측 수준을 보장한다 —
    조용한 day_skip은 대시보드에도 텔레그램에도 흔적이 남지 않는다.
    """
    _reset_state()
    events = []
    blocked = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    record_skip = AsyncMock()
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(return_value=_buyable(qty=1, amt=1_000.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            return_value=f3.EntryQuote(
                ask_price=1_030,
                ask_qty=100,
                antc_price=0,
                fetched_monotonic=f3.time.monotonic(),
                rt_cd="0",
                msg_cd="MCA00000",
                msg1="OK",
            )
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", record_skip)
    monkeypatch.setattr(
        f3,
        "_log_entry_blocked",
        lambda ticker, reason, **kwargs: blocked.append((ticker, reason, kwargs)),
    )
    picked = {
        "ticker": "006340",
        "expected_price": 1_000.0,
        "prev_close": 970.0,
        "cash": 1_000.0,
        "total_amount": 1_000,
        "total_qty": 1,
    }

    result = await f3._run_single(
        picked=picked,
        allow_candidate_retry=False,
    )

    assert result is None
    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"

    assert blocked[-1][1] == "QTY_ZERO"
    assert blocked[-1][2]["level"] == "WARN"

    insufficient = [kwargs for event, kwargs in events if event == "INSUFFICIENT_BALANCE"][-1]
    assert insufficient["reason"] == "QTY_ZERO_AT_LIMIT"
    assert insufficient["level"] == "WARN"
    assert insufficient["limit_buyable_qty"] == 0

    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"

    record_skip.assert_awaited_once()
    assert "reason=QTY_ZERO_AT_LIMIT" in record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_limit_entry_blocks_final_ask_outside_gap_cap(monkeypatch):
    """최종 호가가 10% 갭 상한 이상이면 주문하지 않는다."""
    _reset_state()
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(return_value=(4_660.0, 4_490.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_available_cash",
        AsyncMock(return_value=1_000_000.0),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            return_value=f3.EntryQuote(
                ask_price=4_950,
                ask_qty=100,
                antc_price=0,
                fetched_monotonic=f3.time.monotonic(),
                rt_cd="0",
                msg_cd="MCA00000",
                msg1="OK",
            )
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_limit_entry_cancels_remainder_and_records_partial_fill(monkeypatch):
    _reset_state()
    update_fill = AsyncMock()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    partial = {
        "status": "PARTIAL",
        "order_qty": 65,
        "fill_qty": 19,
        "remaining_qty": 46,
        "fill_price": 14_500,
    }
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(return_value=(14_440.0, 13_730.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_available_cash",
        AsyncMock(return_value=1_000_000.0),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            return_value=f3.EntryQuote(
                ask_price=14_500,
                ask_qty=100,
                antc_price=0,
                fetched_monotonic=f3.time.monotonic(),
                rt_cd="0",
                msg_cd="MCA00000",
                msg1="OK",
            )
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=partial))
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("CANCELLED", partial)),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", update_fill)
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.kwargs["limit_price"] == 14_640
    assert state.get().position_status == "HOLDING"
    assert state.get().entry_qty == 19
    assert state.get().pending_entry is None
    assert update_fill.await_args.kwargs["status"] == "PARTIAL_FILL"


@pytest.mark.asyncio
async def test_pyramid_cancel_uncertain_reconciles_pending_in_same_session(monkeypatch):
    _reset_state()
    recover_pending = AsyncMock(return_value=False)
    monkeypatch.setattr(f3, "FIRST_RATIO", 0.7)
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(return_value=(1_000.0, 970.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_available_cash",
        AsyncMock(return_value=10_000.0),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(
            side_effect=[
                f3.EntryQuote(1_000, 100, 0, f3.time.monotonic(), "0", "", ""),
                f3.EntryQuote(1_006, 100, 0, f3.time.monotonic(), "0", "", ""),
            ]
        ),
    )
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output": {
                        "ODNO": "0000000937",
                        "KRX_FWDG_ORD_ORGNO": "001",
                    },
                },
                {
                    "rt_cd": "0",
                    "output": {
                        "ODNO": "0000000938",
                        "KRX_FWDG_ORD_ORGNO": "001",
                    },
                },
            ]
        ),
    )
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(
            side_effect=[
                {"fill_price": 1_000, "fill_qty": 6},
                None,
            ]
        ),
    )
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("UNCERTAIN", None)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_current_price",
        AsyncMock(return_value=1_006),
    )
    monkeypatch.setattr(f3, "recover_pending_entry", recover_pending)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    recover_pending.assert_awaited_once()
    assert state.get().pending_entry["phase"] == "PYRAMID"


@pytest.mark.asyncio
async def test_entry_records_trade_with_target_name(monkeypatch):
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    state.get().target_name = "대원전선"
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    open_trade = AsyncMock(return_value=1)

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", open_trade)
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert open_trade.await_args.kwargs.get("name") == "대원전선"


@pytest.mark.asyncio
async def test_filled_position_remains_holding_when_open_trade_db_fails(monkeypatch):
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    persist = AsyncMock()
    notify = AsyncMock()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "FILLED-NO-DB", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9}),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(
        f3.db,
        "open_trade",
        AsyncMock(side_effect=RuntimeError("sqlite unavailable")),
    )
    monkeypatch.setattr(f3.state, "persist", persist)

    await f3.run()

    assert state.get().position_status == "HOLDING"
    assert state.get().remaining_qty == 9
    assert state.get().trade_id == 0
    assert persist.await_count >= 2
    assert [call.args[0] for call in notify.await_args_list] == [
        "ENTRY_DB_DEGRADED",
        "ENTRY_EXECUTED",
    ]


@pytest.mark.asyncio
async def test_entry_qty_is_clamped_by_buyable_quantity(monkeypatch):
    events = []
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(return_value=_buyable(qty=5, amt=5_000.0, ord_psbl_cash=10_000.0)),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 5})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.args == ("006340", 5, "PAPER")
    clamped = [kwargs for event, kwargs in events if event == "ENTRY_QTY_CLAMPED"][-1]
    assert clamped["planned_qty"] == 9
    assert clamped["buyable_qty"] == 5
    assert clamped["order_qty"] == 5
    assert clamped["level"] == "WARN"
    assert clamped["reduction_pct"] == pytest.approx(44.44)
    assert clamped["warn_threshold_pct"] == 20.0
    # 산정과 지정가 클램프가 같은 ord_psbl_cash를 쓰므로 둘이 크게 벌어지지
    # 않는다. 지정가 예산이 크게 깎는 경우는 잔고 요약으로 물러난 경로에서만
    # 나오고, 그쪽은 test_entry_quantity_clamped_to_limit_price_budget이 본다.
    assert not [kwargs for event, kwargs in events if event == "ENTRY_QTY_SIZED_AT_LIMIT"]


@pytest.mark.asyncio
async def test_entry_blocks_when_buyable_quantity_is_zero(monkeypatch):
    events = []
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=0, amt=0.0)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "BUYABLE_QTY_ZERO"
    insufficient = [kwargs for event, kwargs in events if event == "INSUFFICIENT_BALANCE"][-1]
    assert insufficient["reason"] == "BUYABLE_QTY_ZERO"
    f3.db.record_skip.assert_awaited_once()
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_buyable_query_failure_retries_without_day_skip(monkeypatch):
    events = []
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(
            side_effect=[
                {"query_failed": True, "rt_cd": "1", "msg_cd": "ConnectTimeout", "msg1": "timeout"},
                _buyable(qty=9, amt=9_000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_count == 1
    assert state.get().day_skip is False
    assert state.get().position_status == "HOLDING"
    query_failed = [kwargs for event, kwargs in events if event == "BUYABLE_QTY_QUERY_FAILED"][-1]
    assert query_failed["entry_attempt"] == 1


@pytest.mark.asyncio
async def test_buyable_query_failure_reason_resets_after_successful_retry_unfilled(monkeypatch):
    events = []
    notify = AsyncMock()
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(
            side_effect=[
                {"query_failed": True, "rt_cd": "1", "msg_cd": "ConnectTimeout", "msg1": "timeout"},
                _buyable(qty=9, amt=9_000.0),
            ]
        ),
    )
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "UNFILLED"
    assert "reason=UNFILLED" in f3.db.record_skip.await_args.args[2]
    assert "UNFILLED" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_full_first_entry_skips_pyramid_wait_when_no_second_qty(monkeypatch):
    events = []
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    sleep_until = AsyncMock()
    fetch_current_price = AsyncMock(return_value=1000)

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", sleep_until)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", fetch_current_price)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    sleep_until.assert_not_awaited()
    fetch_current_price.assert_not_awaited()
    assert any(
        event == "PYRAMID_SKIPPED" and kwargs.get("reason") == "NO_SECOND_QTY"
        for event, kwargs in events
    )


@pytest.mark.asyncio
async def test_pyramid_fill_sends_executed_alert(monkeypatch):
    _reset_state()
    _mock_final_quote(monkeypatch, 1_000)
    notify = AsyncMock()

    monkeypatch.setattr(f3, "FIRST_RATIO", 0.7)
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "msg_cd": "MCA00000",
                    "msg1": "OK",
                    "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
                },
                {
                    "rt_cd": "0",
                    "msg_cd": "MCA00000",
                    "msg1": "OK",
                    "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
                },
            ]
        ),
    )
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(
            side_effect=[
                {"fill_price": 1000, "fill_qty": 7},
                {"fill_price": 1006, "fill_qty": 3},
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1006))
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "mark_pyramided", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    notify.assert_any_await(
        "PYRAMID_EXECUTED",
        level="INFO",
        message="추가 매수: 006340 3주 @ 1,006원",
        ticker="006340",
    )


@pytest.mark.asyncio
async def test_pick_final_entry_candidate_rechecks_quotes_concurrently(monkeypatch):
    _reset_state()
    state.get().target_ticker = "AAA001"
    state.get().target_candidates = [
        {"ticker": "AAA001", "name": "A", "expected_amount": 1_000_000.0},
        {"ticker": "BBB002", "name": "B", "expected_amount": 2_000_000.0},
    ]
    started: list[str] = []
    both_started = f3.asyncio.Event()

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        started.append(ticker)
        if len(started) >= 2:
            both_started.set()
        await both_started.wait()
        return (10310.0, 10000.0) if ticker == "AAA001" else (10400.0, 10000.0)

    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    picked = await f3.asyncio.wait_for(f3._pick_final_entry_candidate(state.get()), timeout=1)

    assert started == ["AAA001", "BBB002"]
    assert picked["ticker"] == "BBB002"
    assert picked["total_qty"] == 91


@pytest.mark.asyncio
async def test_fetch_expected_price_uses_fallback_prev_close_without_retry(monkeypatch):
    events = []
    get = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output": {
                "antc_cnpr": "10310",
                "stck_prpr": "10320",
                "stck_prdy_clpr": "0",
            },
        }
    )
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(
        f3,
        "log",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    result = await f3._fetch_expected_price("005930", fallback_prev_close=10000.0)

    assert result == (10310.0, 10000.0)
    get.assert_awaited_once()
    fields = [item for event, item in events if event == "F3_RECHECK_QUOTE_FIELDS"][-1]
    assert fields["antc_cnpr"] == 10310.0
    assert fields["stck_prpr"] == 10320.0
    assert fields["selected_source"] == "antc_cnpr"


@pytest.mark.asyncio
async def test_entry_rechecks_all_candidates_and_picks_one_before_order(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "GOOD02"},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10100.0, 10000.0),
                (10310.0, 10000.0),
                (10310.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    assert "F3_CANDIDATE_SNAPSHOT_MISSING" in [event for event, _ in events]


@pytest.mark.asyncio
async def test_entry_retries_next_candidate_when_order_rejected(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        side_effect=[
            {
                "rt_cd": "1",
                "msg_cd": "40070000",
                "msg1": "모의투자 주문처리가 안되었습니다(매매불가 종목)",
                "output": {},
            },
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
            },
        ]
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args_list[0].args == ("BAD001", 91, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    final_pick = [kwargs for event, kwargs in events if event == "F3_FINAL_PICK"][-1]
    assert final_pick["name"] == "Bad"
    assert "ENTRY_CANDIDATE_RETRY" in [event for event, _ in events]
    f3.db.record_skip.assert_not_awaited()
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"


@pytest.mark.asyncio
async def test_entry_retries_next_candidate_when_buyable_quantity_is_zero(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(
            side_effect=[
                {
                    "query_failed": False,
                    "nrcvb_buy_qty": 0,
                    "nrcvb_buy_amt": 2_598_826.0,
                    "max_buy_qty": 1,
                    "max_buy_amt": 4_806_726.0,
                    "ord_psbl_cash": 2_586_190.0,
                },
                _buyable(qty=91, amt=999_999_999.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_buy.assert_awaited_once()
    assert send_buy.await_args.args == ("GOOD02", 91, "PAPER")
    assert send_buy.await_args.kwargs["limit_price"] == 10_410
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    retry_events = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"]
    assert retry_events[-1]["ticker"] == "BAD001"
    assert retry_events[-1]["reason"] == "BUYABLE_QTY_ZERO"
    f3.db.record_skip.assert_not_awaited()
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"


@pytest.mark.asyncio
async def test_candidate_retry_reuses_ranked_candidates_and_refreshes_only_next(monkeypatch):
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 2_000_000.0},
        {"ticker": "THIRD3", "name": "Third", "expected_amount": 1_000_000.0},
    ]
    fetch_expected = AsyncMock(
        side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10500.0, 10000.0),
            (10400.0, 10000.0),
        ]
    )
    send_buy = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": "40070000", "msg1": "주문 거절", "output": {}},
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
            },
        ]
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert fetch_expected.await_count == 4
    assert [call.args[0] for call in fetch_expected.await_args_list] == [
        "BAD001",
        "GOOD02",
        "THIRD3",
        "GOOD02",
    ]
    assert send_buy.await_args_list[0].args == ("BAD001", 91, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
async def test_candidate_retry_skips_next_candidate_when_freshness_gap_changes(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "STALE2", "name": "Stale", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD03", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    fetch_expected = AsyncMock(
        side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
            (11000.0, 10000.0),
            (10400.0, 10000.0),
        ]
    )
    send_buy = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": "40070000", "msg1": "주문 거절", "output": {}},
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000939", "KRX_FWDG_ORD_ORGNO": "001"},
            },
        ]
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args_list[0].args == ("BAD001", 91, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD03", 91, "PAPER")
    assert state.get().target_ticker == "GOOD03"
    changed = [kwargs for event, kwargs in events if event == "GAP_CHANGED"][-1]
    assert changed["ticker"] == "STALE2"
    assert changed["freshness_check"] is True
    assert changed["level"] == "INFO"
    blocked = [
        kwargs
        for event, kwargs in events
        if event == "F3_ENTRY_BLOCKED" and kwargs.get("ticker") == "STALE2"
    ]
    assert blocked[-1]["level"] == "INFO"


@pytest.mark.asyncio
async def test_entry_recheck_uses_candidate_prev_close_when_quote_prev_close_missing(monkeypatch):
    _reset_state()
    state.get().target_ticker = "GOOD02"
    state.get().target_candidates = [
        {
            "ticker": "GOOD02",
            "prev_close": 10000.0,
            "gap_pct": 0.031,
            "expected_amount": 1_000_000.0,
        },
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 0.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert send_buy.await_args.args == ("GOOD02", 91, "PAPER")
    assert state.get().position_status == "HOLDING"
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_all_candidates_fail_recheck_skips_without_order(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001"},
        {"ticker": "BAD002"},
        {"ticker": "BAD003"},
    ]
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10100.0, 10000.0),
                (10150.0, 10000.0),
                (11000.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    notify = AsyncMock()
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().day_skip is True
    assert state.get().target_ticker is None
    assert state.get().position_status == "IDLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "GAP_CHANGED"
    assert notify.await_args.kwargs["ticker"] == "BAD001"
    candidate_blocks = [
        kwargs
        for event, kwargs in events
        if event == "F3_ENTRY_BLOCKED" and not kwargs.get("terminal")
    ]
    terminal_blocks = [
        kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED" and kwargs.get("terminal")
    ]
    assert candidate_blocks
    assert all(item["level"] == "INFO" for item in candidate_blocks)
    assert len(terminal_blocks) == 1
    assert terminal_blocks[0]["level"] == "WARN"


@pytest.mark.asyncio
async def test_entry_recheck_detects_gap_change_with_derived_prev_close(monkeypatch):
    """prev_close 미제공 시 스냅샷 가격으로 유도해야 갭 변동을 감지할 수 있다.

    라이브 가격으로 유도하면 재계산 갭 ≡ 스냅샷 갭이 되어
    GAP_CHANGED 가드가 무력화된다 (동어반복 버그).
    """
    _reset_state()
    state.get().target_ticker = "GOOD02"
    state.get().target_candidates = [
        # 스냅샷: 10310원 / 갭 3.1% → 유도 prev_close = 10000원
        {
            "ticker": "GOOD02",
            "gap_pct": 0.031,
            "expected_price": 10310.0,
            "expected_amount": 1_000_000.0,
        },
    ]
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 라이브 예상체결가 15% 급등, prev_close 미제공
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(11500.0, 0.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_candidate_retry_stops_at_deadline(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "1",
            "msg_cd": "40070000",
            "msg1": "주문 거절",
            "output": {},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    # 첫 주문 전·전송 직전 검사는 통과, 이후(후보 재시도 시점)는 마감 초과
    deadline_checks = iter([True, True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(return_value=_buyable(qty=92, amt=999_999_999.0)),
    )
    notify = AsyncMock()
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    # 마감시각 초과 → 첫 후보 거절 후 다음 후보를 시도하지 않는다
    send_buy.assert_awaited_once()
    assert state.get().day_skip is True
    skipped = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "DEADLINE_REACHED"
    f3.db.record_skip.assert_awaited_once()
    assert "CANDIDATE_RETRY_DEADLINE" in f3.db.record_skip.await_args.args[2]
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_entry_all_candidates_gap_recheck_unavailable_alerts_entry_fail(monkeypatch):
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001"},
        {"ticker": "BAD002"},
    ]
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (0.0, 10000.0),
                (0.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().day_skip is True
    assert state.get().target_ticker is None
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"
    assert notify.await_args.kwargs["ticker"] == "BAD001"
    assert "GAP_RECHECK_UNAVAILABLE" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_entry_does_not_wait_for_first_order_time(monkeypatch):
    _reset_state()
    sleep_until = AsyncMock()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", sleep_until)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_awaited_once()
    sleep_until.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_retries_after_unfilled_order(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "msg_cd": "MCA00000",
                    "msg1": "OK",
                    "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
                },
                {
                    "rt_cd": "0",
                    "msg_cd": "MCA00000",
                    "msg1": "OK",
                    "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
                },
            ]
        ),
    )
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10310, "fill_qty": 67}]),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    event_names = [event for event, _ in events]
    assert event_names.count("ENTRY_ORDER_SENT") == 2
    assert "ENTRY_RETRY_START" in event_names
    assert "ENTRY_EXECUTED" in event_names
    assert state.get().position_status == "HOLDING"
    current_tasks = [
        task
        for task in f3._entry_audit_tasks
        if task.get_loop() is asyncio.get_running_loop()
    ]
    if current_tasks:
        await asyncio.gather(*current_tasks)
    audit_calls = f3.db.record_entry_order_attempt.await_args_list
    assert {call.kwargs["kis_order_id"] for call in audit_calls} == {
        "0000000937",
        "0000000938",
    }
    assert [call.kwargs["status"] for call in audit_calls].count("PENDING") == 2
    assert {call.kwargs["status"] for call in audit_calls} >= {
        "CANCELLED",
        "PARTIAL_FILL",
    }


@pytest.mark.asyncio
async def test_entry_cancels_last_unfilled_attempt(monkeypatch):
    _reset_state()
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            side_effect=[
                {
                    "rt_cd": "0",
                    "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
                },
                {
                    "rt_cd": "0",
                    "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
                },
            ]
        ),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(side_effect=[None, None]))
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert cancel_order.await_count == 2
    assert cancel_order.await_args_list[-1].args[:3] == ("0000000938", "001", "PAPER")
    assert state.get().position_status == "IDLE"


@pytest.mark.asyncio
async def test_entry_fail_uses_last_run_attempt_when_retry_skipped(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    # 잔고 조회 전·첫 주문 전·전송 직전 검사는 통과, 2차 시도 시점은 마감 초과
    deadline_checks = iter([True, True, True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    event_names = [event for event, _ in events]
    assert "ENTRY_RETRY_SKIPPED" in event_names
    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["entry_attempt"] == 1
    assert entry_fail["max_attempts"] == 2
    assert "attempts=1" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_poll_fill_updates_summary_from_kis_response(monkeypatch):
    events = []
    deadline = f3.datetime.now(f3.KST) + f3.timedelta(seconds=30)
    get = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output1": [
                {
                    "odno": "0000000937",
                    "tot_ccld_qty": "67",
                    "tot_ccld_amt": "690770",
                }
            ],
        }
    )

    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        _REAL_FETCH_ORDER_FILL_SNAPSHOT,
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        get,
    )

    fill = await f3._poll_fill("0000000937", deadline=deadline, ticker="006340")

    assert fill == {"fill_price": 10310, "fill_qty": 67}
    assert f3._last_fill_poll_summary["poll_attempts"] == 1
    assert f3._last_fill_poll_summary["poll_last_matched"] is True
    assert f3._last_fill_poll_summary["poll_last_ccld_qty"] == 67
    assert f3._last_fill_poll_summary["poll_last_output_count"] == 1
    assert (
        get.await_args.kwargs["request_priority"]
        == f3.kis_rest.REQUEST_PRIORITY_ORDER_STATUS
    )
    assert not events


@pytest.mark.asyncio
async def test_final_entry_quote_uses_critical_priority(monkeypatch):
    get = AsyncMock(return_value={
        "rt_cd": "0",
        "output1": {"askp1": "10310", "askp_rsqn1": "100"},
        "output2": {"antc_cnpr": "10300"},
    })
    monkeypatch.setattr(f3.kis_rest, "get", get)

    quote = await _REAL_FETCH_FINAL_ENTRY_QUOTE("006340")

    assert quote is not None
    assert get.await_args.kwargs["request_priority"] == (
        f3.kis_rest.REQUEST_PRIORITY_CRITICAL
    )


@pytest.mark.asyncio
async def test_fetch_order_fill_snapshot_distinguishes_partial_fill(monkeypatch):
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        _REAL_FETCH_ORDER_FILL_SNAPSHOT,
    )
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "0000000937",
                        "ord_qty": "48",
                        "tot_ccld_qty": "17",
                        "rmn_qty": "31",
                        "avg_prvs": "14500",
                        "tot_ccld_amt": "246500",
                    }
                ],
            }
        ),
    )

    fill = await f3._fetch_order_fill_snapshot(
        "0000000937",
        ticker="006340",
        expected_qty=48,
    )

    assert fill == {
        "status": "PARTIAL",
        "order_qty": 48,
        "fill_qty": 17,
        "remaining_qty": 31,
        "fill_price": 14_500,
    }


@pytest.mark.asyncio
async def test_cancel_success_reconciles_known_partial_fill(monkeypatch):
    known_fill = {
        "status": "PARTIAL",
        "order_qty": 48,
        "fill_qty": 17,
        "remaining_qty": 31,
        "fill_price": 14_500,
    }
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        AsyncMock(
            return_value={
                **known_fill,
                "fill_qty": 19,
                "remaining_qty": 29,
            }
        ),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    outcome, reconciled = await f3._cancel_entry_order_confirmed(
        "0000000937",
        "001",
        "PAPER",
        "006340",
        1,
        1,
        expected_qty=48,
        known_fill=known_fill,
    )

    assert outcome == "CANCELLED"
    assert reconciled["fill_qty"] == 19
    assert reconciled["remaining_qty"] == 29


@pytest.mark.asyncio
async def test_recover_pending_entry_reuses_existing_order_record(monkeypatch):
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {
        "order_id": "0000000937",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 48,
        "limit_price": 14_510,
        "anchor_price": 14_440,
        "prev_close": 13_730,
    }
    fill = {
        "status": "FILLED",
        "order_qty": 48,
        "fill_qty": 48,
        "remaining_qty": 0,
        "fill_price": 14_500,
    }
    record_order = AsyncMock()
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        AsyncMock(return_value=fill),
    )
    monkeypatch.setattr(
        f3.db,
        "get_order_by_kis_id",
        AsyncMock(return_value={"id": 77, "trade_id": 1}),
    )
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(
            return_value={
                "id": 1,
                "ticker": "006340",
                "status": "OPEN",
            }
        ),
    )
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", record_order)
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    recovered = await f3.recover_pending_entry()
    current_tasks = [
        task
        for task in f3._entry_audit_tasks
        if task.get_loop() is asyncio.get_running_loop()
    ]
    if current_tasks:
        await asyncio.gather(*current_tasks)

    assert recovered is True
    record_order.assert_not_awaited()
    f3.db.update_order_fill.assert_awaited_once_with(
        77,
        14_500,
        48,
        None,
        status="FILLED",
    )
    assert state.get().position_status == "HOLDING"
    assert state.get().pending_entry is None


@pytest.mark.asyncio
async def test_recover_pending_entry_creates_cancelled_audit_after_initial_write_failure(
    monkeypatch,
):
    """ACK 직후 감사 INSERT 전에 죽은 경우도 상태 파일만으로 최종 행을 만든다."""
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {
        "date": "20260701",
        "order_id": "RECOVER-AUDIT-1",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 48,
        "limit_price": 14_510,
        "anchor_price": 14_440,
        "attempt": 1,
        "max_attempts": 2,
        "mode": "PAPER",
    }

    async def audit_write(**kwargs):
        if kwargs["status"] == "PENDING":
            raise RuntimeError("initial audit unavailable")
        return 91

    record_audit = AsyncMock(side_effect=audit_write)
    monkeypatch.setattr(f3.db, "record_entry_order_attempt", record_audit)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("CANCELLED", None)),
    )
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    recovered = await f3.recover_pending_entry()
    current_tasks = [
        task
        for task in f3._entry_audit_tasks
        if task.get_loop() is asyncio.get_running_loop()
    ]
    if current_tasks:
        await asyncio.gather(*current_tasks)

    assert recovered is True
    final = [
        call
        for call in record_audit.await_args_list
        if call.kwargs["status"] == "CANCELLED"
    ]
    assert len(final) == 1
    assert final[0].kwargs["date"] == "20260701"
    assert final[0].kwargs["kis_order_id"] == "RECOVER-AUDIT-1"
    assert state.get().pending_entry is None


@pytest.mark.asyncio
async def test_entry_audit_failure_is_swallowed_and_logged(monkeypatch):
    events = []
    audit = {
        "date": "20260701",
        "kis_order_id": "AUDIT-FAIL-1",
        "ticker": "006340",
        "qty": 1,
        "price": 14_510.0,
        "trigger_price": 14_440.0,
        "attempt": 1,
        "max_attempts": 2,
        "mode": "PAPER",
    }
    monkeypatch.setattr(
        f3.db,
        "record_entry_order_attempt",
        AsyncMock(side_effect=RuntimeError("sqlite unavailable")),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f3._upsert_entry_attempt_safe(audit, "UNCERTAIN")

    degraded = [kwargs for event, kwargs in events if event == "ENTRY_DB_DEGRADED"]
    assert degraded[-1]["attempt_status"] == "UNCERTAIN"
    assert "sqlite unavailable" in degraded[-1]["error"]


@pytest.mark.asyncio
async def test_entry_audit_timeout_is_bounded(monkeypatch):
    blocker = asyncio.Event()
    events = []

    async def stalled_audit(**_kwargs):
        await blocker.wait()

    monkeypatch.setattr(f3, "F3_ENTRY_AUDIT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(f3.db, "record_entry_order_attempt", stalled_audit)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    audit = {
        "date": "20260701",
        "kis_order_id": "AUDIT-SLOW-1",
        "ticker": "006340",
        "qty": 1,
        "price": 14_510.0,
        "trigger_price": 14_440.0,
        "attempt": 1,
        "max_attempts": 2,
        "mode": "PAPER",
    }

    await asyncio.wait_for(f3._upsert_entry_attempt_safe(audit, "PENDING"), 0.1)

    degraded = [kwargs for event, kwargs in events if event == "ENTRY_DB_DEGRADED"]
    assert degraded[-1]["attempt_status"] == "PENDING"
    assert "TimeoutError" in degraded[-1]["error"]


@pytest.mark.asyncio
async def test_entry_audit_tasks_are_drained_before_shutdown(monkeypatch):
    blocker = asyncio.Event()
    events = []
    task = asyncio.create_task(blocker.wait())
    f3._entry_audit_tasks.add(task)
    monkeypatch.setattr(f3, "F3_ENTRY_AUDIT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f3.drain_entry_audit_tasks()

    assert task.cancelled()
    assert task not in f3._entry_audit_tasks
    shutdown = [
        kwargs
        for event, kwargs in events
        if event == "ENTRY_DB_DEGRADED"
        and kwargs.get("phase") == "ENTRY_ATTEMPT_AUDIT_SHUTDOWN"
    ]
    assert shutdown[-1]["pending_count"] == 1


@pytest.mark.asyncio
async def test_initial_entry_audit_is_detached_from_live_reconciliation(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def stalled_audit(**_kwargs):
        started.set()
        await release.wait()
        return 1

    monkeypatch.setattr(f3, "F3_ENTRY_AUDIT_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(f3.db, "record_entry_order_attempt", stalled_audit)
    audit = {
        "date": "20260701",
        "kis_order_id": "AUDIT-DETACHED-1",
        "ticker": "006340",
        "qty": 1,
        "price": 14_510.0,
        "trigger_price": 14_440.0,
        "attempt": 1,
        "max_attempts": 2,
        "mode": "PAPER",
    }

    f3._start_entry_attempt_audit(audit)
    await asyncio.wait_for(started.wait(), 0.1)
    current_tasks = [
        task
        for task in f3._entry_audit_tasks
        if task.get_loop() is asyncio.get_running_loop()
    ]
    assert current_tasks and not current_tasks[-1].done()

    release.set()
    await asyncio.gather(*current_tasks)


@pytest.mark.asyncio
async def test_recover_pending_entry_api_error_preserves_pending_and_blocks(monkeypatch):
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {
        "order_id": "0000000937",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 48,
    }
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        AsyncMock(side_effect=RuntimeError("KIS unavailable")),
    )
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    recovered = await f3.recover_pending_entry()

    assert recovered is False
    assert state.get().day_skip is True
    assert state.get().position_status == "ENTERING"
    assert state.get().pending_entry["order_id"] == "0000000937"
    f3.notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_continues_when_audit_payload_cannot_be_built(monkeypatch):
    events = []
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {
        "order_id": "RECOVER-NO-AUDIT",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 8,
    }
    monkeypatch.setattr(f3, "_entry_audit_from_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("CANCELLED", None)),
    )
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    recovered = await f3.recover_pending_entry()

    assert recovered is True
    assert s.pending_entry is None
    degraded = [kwargs for event, kwargs in events if event == "ENTRY_DB_DEGRADED"]
    assert degraded[-1]["reason"] == "INVALID_PENDING_AUDIT_FIELDS"


@pytest.mark.asyncio
async def test_recover_cancelled_pyramid_keeps_existing_holding(monkeypatch):
    _reset_state()
    s = state.get()
    s.position_status = "HOLDING"
    s.entry_price = 14_400
    s.entry_qty = 40
    s.remaining_qty = 40
    s.trade_id = 1
    s.pending_entry = {
        "order_id": "0000000938",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 8,
        "phase": "PYRAMID",
    }
    monkeypatch.setattr(
        f3,
        "_fetch_order_fill_snapshot",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("CANCELLED", None)),
    )
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    recovered = await f3.recover_pending_entry()

    assert recovered is True
    assert s.position_status == "HOLDING"
    assert s.entry_qty == 40
    assert s.remaining_qty == 40
    assert s.pending_entry is None
    assert s.day_skip is False
    pyramid_audits = [
        call
        for call in f3.db.record_entry_order_attempt.await_args_list
        if call.kwargs.get("kis_order_id") == "0000000938"
    ]
    assert pyramid_audits
    assert {call.kwargs["order_phase"] for call in pyramid_audits} == {"PYRAMID_BUY"}
    assert "CANCELLED" in {call.kwargs["status"] for call in pyramid_audits}


@pytest.mark.asyncio
async def test_dry_run_entry_state_collision_records_skip(monkeypatch):
    _reset_state()
    state.get().position_status = "HOLDING"
    record_skip = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3.db, "record_skip", record_skip)

    await f3._run_dry_entry("006340")

    record_skip.assert_awaited_once()
    args = record_skip.await_args.args
    assert args[1] == "DRY_RUN_F3_SKIPPED"
    assert "STATE_NOT_IDLE" in args[2]


@pytest.mark.asyncio
async def test_recheck_batch_timeout_uses_completed_candidates(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "SLOW01"
    state.get().target_candidates = [
        {"ticker": "SLOW01", "name": "Slow", "expected_amount": 3_000_000.0},
        {"ticker": "FAST02", "name": "Fast", "expected_amount": 2_000_000.0},
    ]

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        if ticker == "SLOW01":
            await f3.asyncio.sleep(0.05)
        return (10400.0, 10000.0)

    monkeypatch.setattr(f3, "F3_RECHECK_BATCH_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert [item["ticker"] for item in ranked] == ["FAST02"]
    timeout = [kwargs for event, kwargs in events if event == "F3_RECHECK_BATCH_TIMEOUT"][-1]
    assert timeout["requested_count"] == 2
    assert timeout["completed_count"] == 1
    assert timeout["pending_tickers"] == ["SLOW01"]


@pytest.mark.asyncio
async def test_recheck_candidate_exception_blocks_only_that_candidate(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        if ticker == "BAD001":
            raise RuntimeError("quote down")
        return 10400.0, 10000.0

    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert [item["ticker"] for item in ranked] == ["GOOD02"]
    event_names = [event for event, _ in events]
    assert "F3_RECHECK_QUOTE_ERROR" in event_names
    assert "F3_RECHECK_BATCH_TIMING" in event_names
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert any(
        item["ticker"] == "BAD001" and item["reason"] == "GAP_RECHECK_UNAVAILABLE"
        for item in blocked
    )


@pytest.mark.asyncio
async def test_recheck_cash_exception_blocks_with_balance_query_failed(monkeypatch):
    """잔고 조회 예외는 현금 0(INSUFFICIENT_BALANCE)이 아니라 BALANCE_QUERY_FAILED로 차단한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "GOOD01"
    state.get().target_candidates = [
        {"ticker": "GOOD01", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10400.0, 10000.0)))
    monkeypatch.setattr(
        f3,
        "_fetch_available_cash",
        AsyncMock(side_effect=RuntimeError("balance down")),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert ranked is None
    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    assert "BALANCE_QUERY_ERROR" in [event for event, _ in events]
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "QTY_ZERO" not in blocked
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_recheck_cash_none_blocks_with_balance_query_failed(monkeypatch):
    """잔고 조회가 재시도 끝에 None을 반환하면 후보 재검증 없이 BALANCE_QUERY_FAILED로 차단한다."""
    _reset_state()
    state.get().target_ticker = "GOOD01"
    state.get().target_candidates = [
        {"ticker": "GOOD01", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10400.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert ranked is None
    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_slippage_guard_allows_fill_when_gap_stays_below_max(monkeypatch):
    """체결가가 expected_price보다 비싸도 갭 상한(10%) 안이면 보유를 유지한다."""
    _reset_state()
    _mock_final_quote(monkeypatch, 1_015)
    send_sell = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 재검증 갭 3.09% (1000/970). 체결 1015원 → expected 대비 +1.5%지만 갭 4.64% < 10%.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1015, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1015))
    monkeypatch.setattr(f3, "_send_sell", send_sell)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_sell.assert_not_awaited()
    f3.db.record_skip.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False
    assert state.get().close_reason is None


@pytest.mark.asyncio
async def test_slippage_guard_exits_when_fill_gap_exceeds_max(monkeypatch):
    """체결가 기준 갭이 상한(10%) 이상이면 즉시 청산하고 당일 스킵한다."""
    events = []
    _reset_state()
    # 최종 호가 9.90%는 고갭 구간 — 적격 유동성 후보에서만 주문이 진행된다.
    state.get().target_candidates = [
        {"ticker": "006340", "expected_amount": f3.HIGH_GAP_MIN_EXPECTED_AMOUNT}
    ]
    _mock_final_quote(monkeypatch, 1_066)
    from src.modules import f4_tracking

    async def close_now(_price, reason):
        await state.set_closed(reason)
        return True

    close_guard = AsyncMock(side_effect=close_now)

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    # 재검증 갭 6.19% 통과. 체결 1070원 → 갭 10.31%로 상한 초과.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1030.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1070, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1070))
    monkeypatch.setattr(f4_tracking, "close_now", close_guard)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    close_guard.assert_awaited_once_with(1070, "SLIPPAGE_GUARD")
    f3.db.open_trade.assert_awaited_once()
    assert state.get().position_status == "CLOSED"
    assert state.get().day_skip is True
    assert state.get().close_reason == "SLIPPAGE_GUARD"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "SLIPPAGE_GUARD"
    guard = [kwargs for event, kwargs in events if event == "SLIPPAGE_GUARD"][-1]
    assert guard["fill_gap_pct"] == pytest.approx(10.309, abs=0.001)
    assert guard["gap_max_pct"] == 10.0


@pytest.mark.asyncio
async def test_entry_recheck_blocks_gap_at_ten_percent(monkeypatch):
    """F1 고갭 범위 밖인 10% 이상은 주문 전에 차단한다."""
    _reset_state()
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(11000.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    notify = AsyncMock()
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_slippage_guard_allows_fill_just_below_ten_percent(monkeypatch):
    """강한 모멘텀 체결도 10% 미만이면(적격 유동성) 보유를 유지한다."""
    _reset_state()
    # 최종 호가·체결 갭 9.90%는 고갭 구간 — 적격 유동성 후보에서만 진입/보유한다.
    state.get().target_candidates = [
        {"ticker": "006340", "expected_amount": f3.HIGH_GAP_MIN_EXPECTED_AMOUNT}
    ]
    _mock_final_quote(monkeypatch, 1_066)
    send_sell = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 재검증 갭 6.19% 통과. 체결 1066원 → 갭 9.90%로 상한 바로 아래.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1030.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1066, "fill_qty": 9})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1066))
    monkeypatch.setattr(f3, "_send_sell", send_sell)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_sell.assert_not_awaited()
    f3.db.record_skip.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False
    assert state.get().close_reason is None


# ── 진입 전 VI 체크 ───────────────────────────────────────────────────

_VI_ACTIVE_INFO = {
    "vi_kind_code": "2",
    "cntg_vi_hour": "090032",
    "bsop_date": "20260720",
    "vi_prc": "1140",
    "vi_stnd_prc": "0",
    "vi_dprt": "0.00",
    "vi_count": "1",
}


@pytest.mark.parametrize(
    ("vi_price", "expected"),
    [(10_999, False), (11_000, True), (11_001, True), (0, False)],
)
def test_vi_price_gap_cap_boundary(vi_price, expected):
    vi_info = {**_VI_ACTIVE_INFO, "vi_prc": str(vi_price)}

    # 적격 유동성 후보의 VI 상한은 10% 경계다 (저대금 8% 경계는 별도 테스트).
    assert (
        f3._vi_price_reaches_gap_cap(vi_info, 10_000, f3.HIGH_GAP_MIN_EXPECTED_AMOUNT) is expected
    )


@pytest.mark.asyncio
async def test_known_active_vi_waits_for_confirmed_release(monkeypatch):
    events = []
    sleep = AsyncMock()
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 130.0)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_POLL_SEC", 2.0)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "_fetch_vi_active", fetch)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    released = await f3._wait_for_vi_release(
        "009830",
        dict(_VI_ACTIVE_INFO),
        entry_attempt=1,
    )

    assert released is True
    sleep.assert_awaited_once_with(2.0)
    fetch.assert_awaited_once_with("009830")
    assert [event for event, _ in events] == [
        "VI_ENTRY_WAIT_STARTED",
        "VI_ENTRY_RELEASED",
    ]


@pytest.mark.asyncio
async def test_vi_release_rechecks_fresh_quote_then_submits_capped_limit(monkeypatch):
    """VI 해제 뒤 과거 호가를 재사용하지 않고 신선 호가로 지정가를 만든다."""
    events = []
    call_order = []
    _reset_state()
    vi_responses = iter([dict(_VI_ACTIVE_INFO), None])

    async def fetch_vi(_ticker):
        call_order.append("vi_check")
        return next(vi_responses)

    async def wait_sleep(_seconds):
        call_order.append("vi_wait")

    async def fetch_final_quote(_ticker):
        call_order.append("final_quote")
        return _entry_quote(10_400)

    async def send_buy(*_args, **_kwargs):
        call_order.append("send_buy")
        return {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }

    send_buy_mock = AsyncMock(side_effect=send_buy)
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 130.0)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_POLL_SEC", 2.0)
    monkeypatch.setattr(f3.asyncio, "sleep", wait_sleep)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_vi_active", fetch_vi)
    monkeypatch.setattr(f3, "_fetch_final_entry_quote", fetch_final_quote)
    monkeypatch.setattr(f3, "_send_buy", send_buy_mock)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value={"fill_price": 10_400, "fill_qty": 90}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert call_order == ["vi_check", "vi_wait", "vi_check", "final_quote", "send_buy"]
    assert send_buy_mock.await_args.args == ("006340", 90, "PAPER")
    assert send_buy_mock.await_args.kwargs["limit_price"] == 10_500
    assert callable(send_buy_mock.await_args.kwargs["send_guard"])
    assert send_buy_mock.await_args.kwargs["send_guard"]() is True
    event_names = [event for event, _kwargs in events]
    assert event_names.index("VI_ENTRY_RELEASED") < event_names.index("ENTRY_PRICE_APPROVED")
    assert event_names.index("ENTRY_PRICE_APPROVED") < event_names.index("ENTRY_ORDER_SENT")
    assert state.get().position_status == "HOLDING"
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_vi_wait_does_not_mistake_query_error_for_release(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 130.0)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_POLL_SEC", 0.1)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        f3,
        "_fetch_vi_active",
        AsyncMock(side_effect=[RuntimeError("VI query failed"), None]),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    released = await f3._wait_for_vi_release(
        "009830",
        dict(_VI_ACTIVE_INFO),
        entry_attempt=1,
    )

    assert released is True
    assert any(event == "F3_VI_CHECK_ERROR" for event, _ in events)
    assert events[-1][0] == "VI_ENTRY_RELEASED"


@pytest.mark.asyncio
async def test_vi_wait_times_out_fail_closed(monkeypatch):
    events = []
    fetch = AsyncMock(return_value=dict(_VI_ACTIVE_INFO))
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 10.0)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_POLL_SEC", 0.1)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_vi_active", fetch)
    fake_time = MagicMock()
    fake_time.monotonic = MagicMock(side_effect=[0, 0, 11, 11])
    monkeypatch.setattr(f3, "time", fake_time)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    released = await f3._wait_for_vi_release(
        "009830",
        dict(_VI_ACTIVE_INFO),
        entry_attempt=1,
    )

    assert released is False
    fetch.assert_awaited_once_with("009830")
    assert events[-1][0] == "VI_ENTRY_WAIT_TIMEOUT"
    assert events[-1][1]["reason"] == "WAIT_LIMIT"


@pytest.mark.asyncio
async def test_single_candidate_at_static_vi_cap_is_blocked_without_wait(monkeypatch):
    """VI 발동가가 +10% 상한이면 해제 후 진입 불가이므로 즉시 차단한다."""
    events = []
    _reset_state()
    send_buy = AsyncMock()
    notify = AsyncMock()
    wait_for_release = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_wait_for_vi_release", wait_for_release)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    vi_at_cap = {**_VI_ACTIVE_INFO, "vi_prc": "11000"}
    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(return_value=vi_at_cap))

    await f3.run()

    send_buy.assert_not_awaited()
    wait_for_release.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "VI_ENTRY_BLOCKED"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "VI_ACTIVE"
    assert blocked[-1]["wait_skipped_reason"] == "VI_PRICE_AT_OR_ABOVE_GAP_CAP"


@pytest.mark.asyncio
async def test_entry_replaces_active_vi_candidate_without_waiting(monkeypatch):
    """대체 후보가 있으면 1순위 VI 해제를 기다리지 않고 즉시 2순위로 이동한다."""
    events = []
    call_order = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]

    async def quiet_wait(*_args, **_kwargs):
        call_order.append("quiet_wait")

    vi_responses = iter([dict(_VI_ACTIVE_INFO), None])

    async def fetch_vi(_ticker):
        call_order.append("vi_check")
        return next(vi_responses)

    async def submit_buy(*_args, **_kwargs):
        call_order.append("send_buy")
        return {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        }

    send_buy = AsyncMock(side_effect=submit_buy)

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "F3_VI_RELEASE_WAIT_SEC", 130.0)
    monkeypatch.setattr(f3, "F3_VI_RELEASE_POLL_SEC", 0.1)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", quiet_wait)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=91)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_vi_active", fetch_vi)

    await f3.run()

    send_buy.assert_awaited_once()
    assert send_buy.await_args.args == ("GOOD02", 91, "PAPER")
    assert send_buy.await_args.kwargs["limit_price"] == 10_410
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    assert not any(event == "VI_ENTRY_WAIT_STARTED" for event, _ in events)
    assert call_order == [
        "quiet_wait",
        "vi_check",
        "quiet_wait",
        "vi_check",
        "send_buy",
    ]
    retry = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"][-1]
    assert retry["ticker"] == "BAD001"
    blocked = [kwargs for event, kwargs in events if event == "VI_ENTRY_BLOCKED"][-1]
    assert blocked["wait_skipped_reason"] == "CANDIDATE_AVAILABLE"
    f3.db.record_skip.assert_not_awaited()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"


@pytest.mark.asyncio
async def test_force_mode_cannot_override_entry_deadline(monkeypatch):
    _reset_state()
    state.get().target_ticker = "009830"
    block = AsyncMock()
    fetch_expected = AsyncMock()
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: False)
    monkeypatch.setattr(f3, "_block_entry_deadline_passed", block)
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected)
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run()

    block.assert_awaited_once_with("009830", "BEFORE_RECHECK")
    fetch_expected.assert_not_awaited()
    send_buy.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_proceeds_when_vi_check_fails(monkeypatch):
    """VI 조회 실패는 진입을 막지 않는다(fail-open) — 관측 실패로 기회를 버리지 않는다."""
    events = []
    _reset_state()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=92)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 91})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10310))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(
        f3, "_fetch_vi_active", AsyncMock(side_effect=RuntimeError("VI status query failed"))
    )

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "HOLDING"
    errors = [kwargs for event, kwargs in events if event == "F3_VI_CHECK_ERROR"]
    assert errors and "VI status query failed" in errors[-1]["error"]
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_candidates_vi_active_records_vi_active(monkeypatch):
    """모든 후보가 VI로만 소진되면 최종 사유도 VI_ACTIVE로 기록한다.

    시작 복원(main._restore_daily_skip_from_db)은 VI/ENTRY_FAIL을 구분 복원하므로
    ENTRY_FAIL로 남기면 재시작 catch-up이 VI 해제가에 추격 진입할 수 있다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "BAD002", "name": "Bad2", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(
        f3,
        "_fetch_vi_active",
        AsyncMock(side_effect=[dict(_VI_ACTIVE_INFO), dict(_VI_ACTIVE_INFO)]),
    )

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    skipped = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_EXHAUSTED"]
    assert skipped and skipped[-1]["reason"] == "NO_REMAINING_CANDIDATE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    assert "NO_REMAINING_CANDIDATE" in f3.db.record_skip.await_args.args[2]
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_mixed_rejection_reasons_keep_entry_fail(monkeypatch):
    """소진 사유에 VI가 아닌 거절이 섞이면 최종 사유는 ENTRY_FAIL을 유지한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "BAD002", "name": "Bad2", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "1",
            "msg_cd": "APBK0919",
            "msg1": "주문 거부",
            "output": {},
        }
    )
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_status(ticker):
        return dict(_VI_ACTIVE_INFO) if ticker == "BAD001" else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run()

    assert state.get().day_skip is True
    assert state.get().close_reason == "ENTRY_FAIL"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "NO_REMAINING_CANDIDATE" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_entry_retry_rechecks_vi_before_second_order(monkeypatch):
    """1차 미체결 폴링·취소·대기 중 VI가 발동하면
    2차 지정가 주문을 보내지 않고 당일 VI_ACTIVE로 차단해야 한다."""
    events = []
    _reset_state()
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    notify = AsyncMock()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_after_cancel(ticker):
        # 1차 주문 취소 이후(= 재시도 직전)에만 VI 발동 상태를 돌려준다.
        return dict(_VI_ACTIVE_INFO) if cancel_order.await_count else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_after_cancel))

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "VI_ACTIVE"


@pytest.mark.asyncio
async def test_entry_retry_vi_recheck_moves_to_next_candidate(monkeypatch):
    """다후보 경로: 1순위 재시도 직전 VI가 감지되면 취소 상태를 정리하고
    다음 후보로 넘어가 정상 진입해야 한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10400, "fill_qty": 91}]),
    )
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    async def vi_status(ticker):
        # BAD001은 1차 취소 이후에만 VI 발동, GOOD02는 항상 정상.
        if ticker == "BAD001" and cancel_order.await_count:
            return dict(_VI_ACTIVE_INFO)
        return None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run()

    assert send_buy.await_count == 2
    assert send_buy.await_args_list[0].args[0] == "BAD001"
    assert send_buy.await_args_list[-1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    retry_events = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"]
    assert retry_events and retry_events[-1]["ticker"] == "BAD001"
    assert retry_events[-1]["reason"] == "VI_ACTIVE"
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_retry_vi_check_runs_right_before_send(monkeypatch):
    """재시도 VI 재확인은 quiet wait·매수가능수량 조회까지 끝난 뒤,
    _send_buy 직전에 수행해야 한다 — 그 사이 발동한 VI도 잡아야 한다."""
    events = []
    _reset_state()
    fetch_buyable = AsyncMock(return_value=_buyable())
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", fetch_buyable)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_status(ticker):
        # 2차 시도의 매수가능수량 조회가 끝난 시점부터 VI 발동 — 조회~주문
        # 사이 창에서 발동한 VI를 모델링한다.
        return dict(_VI_ACTIVE_INFO) if fetch_buyable.await_count >= 2 else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"


@pytest.mark.asyncio
async def test_cancel_rejected_because_filled_recovers_fill(monkeypatch):
    """취소 거부의 흔한 원인은 기체결 — 취소 실패 시 체결을 재확인해
    재주문 없이 HOLDING으로 전환해야 한다(중복 주문 방지)."""
    _reset_state()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10310, "fill_qty": 91}]),
    )
    monkeypatch.setattr(
        f3,
        "_cancel_order",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "취소 불가"}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "HOLDING"
    assert state.get().entry_price == 10310
    assert state.get().entry_qty == 91
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_unconfirmed_blocks_candidate_switch_and_idle(monkeypatch):
    """취소 성공을 확인하지 못하면 주문이 살아 있을 수 있다 —
    재주문·후보 전환·IDLE 전환 없이 중단하고 운영자에게 알린다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    notify = AsyncMock()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(
            side_effect=[
                (10310.0, 10000.0),
                (10400.0, 10000.0),
                (10400.0, 10000.0),
            ]
        ),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3,
        "_cancel_order",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "시스템 오류"}),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    # 살아 있을 수 있는 주문 옆에서 다른 후보로 갈아타면 안 된다.
    send_buy.assert_awaited_once()
    assert send_buy.await_args.args[0] == "BAD001"
    # IDLE로 전환하지 않는다 — 미체결 주문 생존 가능성이 남아 있다.
    assert state.get().position_status == "ENTERING"
    assert state.get().day_skip is True
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "CANCEL_UNCONFIRMED" in f3.db.record_skip.await_args.args[2]
    unconfirmed_alerts = [
        call for call in notify.await_args_list if call.args[0] == "ENTRY_CANCEL_UNCONFIRMED"
    ]
    assert unconfirmed_alerts and unconfirmed_alerts[-1].kwargs["level"] == "CRIT"


@pytest.mark.asyncio
async def test_available_cash_for_entry_uses_fresh_preopen_snapshot(monkeypatch):
    fetch = AsyncMock(return_value=2_000_000.0)
    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch)
    monkeypatch.setattr(f3, "_today", lambda: "20260729")
    monkeypatch.setattr(f3.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(f3, "BALANCE_SNAPSHOT_TTL_SEC", 90.0)
    f3._available_cash_snapshot = {
        "date": "20260729",
        "cash": 1_000_000.0,
        "created_monotonic": 50.0,
    }

    cash = await f3._available_cash_for_entry()

    assert cash == 1_000_000.0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_available_cash_for_entry_refreshes_expired_snapshot(monkeypatch):
    fetch = AsyncMock(return_value=2_000_000.0)
    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch)
    monkeypatch.setattr(f3, "_today", lambda: "20260729")
    monkeypatch.setattr(f3.time, "monotonic", lambda: 200.0)
    monkeypatch.setattr(f3, "BALANCE_SNAPSHOT_TTL_SEC", 90.0)
    f3._available_cash_snapshot = {
        "date": "20260729",
        "cash": 1_000_000.0,
        "created_monotonic": 50.0,
    }

    cash = await f3._available_cash_for_entry()

    assert cash == 2_000_000.0
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_available_cash_for_entry_uses_snapshot_outside_fast_hybrid(monkeypatch):
    fetch = AsyncMock(return_value=2_000_000.0)
    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: False)
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("BALANCE_SNAPSHOT_PREFETCH", "1")
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch)
    f3._available_cash_snapshot = {
        "date": f3._today(),
        "cash": 1_000_000.0,
        "created_monotonic": f3.time.monotonic(),
    }

    cash = await f3._available_cash_for_entry()

    assert cash == 1_000_000.0
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_available_cash_for_entry_ignores_snapshot_when_prefetch_disabled(monkeypatch):
    fetch = AsyncMock(return_value=2_000_000.0)
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("BALANCE_SNAPSHOT_PREFETCH", "0")
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch)
    f3._available_cash_snapshot = {
        "date": f3._today(),
        "cash": 1_000_000.0,
        "created_monotonic": f3.time.monotonic(),
    }

    cash = await f3._available_cash_for_entry()

    assert cash == 2_000_000.0
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_mode_blocks_all_hybrid_f3_shortcuts_without_monkeypatching_gate(
    monkeypatch,
):
    fetch = AsyncMock(return_value=1_000_000.0)
    fast = {
        "ticker": "005930",
        "expected_price": 10300.0,
        "prev_close": 10000.0,
        "fast_observed_monotonic": f3.time.monotonic(),
    }
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    monkeypatch.setenv("PAPER_FAST_HYBRID", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch)
    f3._available_cash_snapshot = {
        "date": f3._today(),
        "cash": 1_000_000.0,
        "created_monotonic": f3.time.monotonic(),
    }

    assert f3.paper_fast_probe.hybrid_enabled() is False
    assert await f3.prepare_available_cash_snapshot() is None
    assert f3._cached_available_cash() is None
    assert f3._fast_recheck_rows(["005930"], {"005930": fast}) is None
    fetch.assert_not_awaited()


def test_fast_recheck_rows_reuses_fresh_open_multi_snapshot(monkeypatch):
    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.time, "monotonic", lambda: 105.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    fast = [
        {
            "ticker": "005930",
            "name": "삼성전자",
            "expected_price": 227500.0,
            "prev_close": 220000.0,
            "fast_observed_monotonic": 100.0,
        }
    ]
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: fast)

    rows = f3._fast_recheck_rows(["005930"], {"005930": fast[0]})

    assert rows is not None
    assert rows[0]["expected_price"] == 227500.0
    assert rows[0]["prev_close"] == 220000.0


@pytest.mark.asyncio
async def test_rank_final_candidates_uses_fast_recheck_without_unbound_local(monkeypatch):
    events = []
    _reset_state()
    fast = {
        "ticker": "005930",
        "name": "Samsung",
        "expected_price": 227500.0,
        "prev_close": 220000.0,
        "expected_amount": 3_000_000.0,
        "fast_observed_monotonic": 100.0,
    }
    state.get().target_ticker = fast["ticker"]
    state.get().target_candidates = [fast]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: [fast])
    monkeypatch.setattr(f3.time, "monotonic", lambda: 105.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    monkeypatch.setattr(f3, "_available_cash_for_entry", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert ranked is not None
    assert ranked[0]["ticker"] == "005930"
    timing = [fields for event, fields in events if event == "F3_RECHECK_BATCH_TIMING"][-1]
    assert timing["requested_count"] == 1
    assert timing["completed_count"] == 1
    assert timing["source"] == "FAST_MULTI"


def test_fast_recheck_rows_rejects_stale_snapshot(monkeypatch):
    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.time, "monotonic", lambda: 120.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    fast = [
        {
            "ticker": "005930",
            "expected_price": 227500.0,
            "prev_close": 220000.0,
            "fast_observed_monotonic": 100.0,
        }
    ]
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: fast)

    assert f3._fast_recheck_rows(["005930"], {"005930": fast[0]}) is None


# ── Single-candidate fresh FAST_MULTI reuse (Option B; no final ranking) ──


def _open_boundary_price_resp(**out):
    """inquire-price response with all opening fields defaulting to 0."""
    base = {
        "antc_cnpr": "0",
        "stck_prpr": "0",
        "stck_prdy_clpr": "0",
        "stck_oprc": "0",
    }
    base.update({key: str(value) for key, value in out.items()})
    return {"rt_cd": "0", "output": base}


def _wire_successful_entry(monkeypatch, send_buy):
    _mock_final_quote(monkeypatch, 3_985)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    # first_qty = int(int(1_000_000*ALLOC_RATIO) / 3985) = 238. Fill exactly the
    # requested qty so the loop breaks into HOLDING (not the unmocked cancel path).
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 3985, "fill_qty": 238})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=3985))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())


@pytest.mark.asyncio
async def test_live_order_reconciliation_continues_without_audit_payload(monkeypatch):
    events = []
    _reset_state()
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "LIVE-NO-AUDIT", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    _wire_successful_entry(monkeypatch, send_buy)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(3985.0, 3865.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_entry_audit_from_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f3.run()

    assert state.get().position_status == "HOLDING"
    f3._poll_fill.assert_awaited_once()
    degraded = [kwargs for event, kwargs in events if event == "ENTRY_DB_DEGRADED"]
    assert degraded[-1]["reason"] == "INVALID_PENDING_AUDIT_FIELDS"


@pytest.mark.asyncio
async def test_run_single_candidate_reuses_fresh_fast_snapshot(monkeypatch):
    _reset_state()
    fast = {
        "ticker": "413630",
        "name": "FastCo",
        "expected_price": 3985.0,
        "prev_close": 3865.0,
        "expected_amount": 3_000_000.0,
        "fast_observed_monotonic": 100.0,
    }
    state.get().target_ticker = fast["ticker"]
    state.get().target_candidates = [fast]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: [fast])
    monkeypatch.setattr(f3.time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    monkeypatch.setattr(f3, "_available_cash_for_entry", AsyncMock(return_value=1_000_000.0))
    # If routing still short-circuited to _run_single(picked=None), this stale
    # single quote would be used and (gap 0%) block the candidate as GAP_CHANGED.
    single_quote = AsyncMock(return_value=(3865.0, 3865.0))
    monkeypatch.setattr(f3, "_fetch_expected_price", single_quote)
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    _wire_successful_entry(monkeypatch, send_buy)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    await f3.run()

    single_quote.assert_not_awaited()
    send_buy.assert_awaited()
    assert send_buy.await_args.args[0] == "413630"
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False


@pytest.mark.asyncio
async def test_run_single_candidate_gap_changed_sets_day_skip(monkeypatch):
    _reset_state()
    state.get().target_ticker = "413630"
    state.get().target_candidates = [{"ticker": "413630", "name": "FastCo"}]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: False)
    monkeypatch.setattr(f3, "_available_cash_for_entry", AsyncMock(return_value=1_000_000.0))
    # 0% gap -> below GAP_MIN_RECHECK -> GAP_CHANGED, terminal day-skip.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(3865.0, 3865.0)))
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    await f3.run()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_run_single_candidate_stale_fast_falls_back_to_single_quote(monkeypatch):
    _reset_state()
    fast = {
        "ticker": "413630",
        "name": "FastCo",
        "expected_price": 3985.0,
        "prev_close": 3865.0,
        "expected_amount": 3_000_000.0,
        "fast_observed_monotonic": 100.0,
    }
    state.get().target_ticker = fast["ticker"]
    state.get().target_candidates = [fast]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: [fast])
    # Snapshot observed at 100.0 but now 200.0 -> aged out (>15s) -> SINGLE_QUOTE.
    monkeypatch.setattr(f3.time, "monotonic", lambda: 200.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    monkeypatch.setattr(f3, "_available_cash_for_entry", AsyncMock(return_value=1_000_000.0))
    single_quote = AsyncMock(return_value=(3985.0, 3865.0))
    monkeypatch.setattr(f3, "_fetch_expected_price", single_quote)
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    _wire_successful_entry(monkeypatch, send_buy)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    await f3.run()

    single_quote.assert_awaited()
    send_buy.assert_awaited()
    assert send_buy.await_args.args[0] == "413630"
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False


@pytest.mark.asyncio
async def test_run_single_candidate_fresh_fast_skips_balance_on_existing_trade(monkeypatch):
    """Fresh FAST reuse must not query balance before the existing-trade check."""
    _reset_state()
    fast = {
        "ticker": "413630",
        "name": "FastCo",
        "expected_price": 3985.0,
        "prev_close": 3865.0,
        "expected_amount": 3_000_000.0,
        "fast_observed_monotonic": 100.0,
    }
    state.get().target_ticker = fast["ticker"]
    state.get().target_candidates = [fast]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: [fast])
    monkeypatch.setattr(f3.time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    cash_spy = AsyncMock(return_value=1_000_000.0)
    monkeypatch.setattr(f3, "_available_cash_for_entry", cash_spy)
    fetch_spy = AsyncMock(return_value=(3985.0, 3865.0))
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_spy)
    monkeypatch.setattr(
        f3,
        "_existing_trade_for_today",
        AsyncMock(
            return_value={
                "status": "OPEN",
                "id": 7,
                "ticker": "413630",
                "entry_price": 3900.0,
                "entry_qty": 10,
                "name": "FastCo",
            }
        ),
    )
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    await f3.run()

    cash_spy.assert_not_awaited()
    fetch_spy.assert_not_awaited()
    send_buy.assert_not_awaited()
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
async def test_run_single_candidate_fresh_fast_gap_rejected_skips_balance(monkeypatch):
    """A rejected FAST gap must block before any balance query."""
    _reset_state()
    # Fresh snapshot but 0% gap (below GAP_MIN_RECHECK) -> GAP_CHANGED.
    fast = {
        "ticker": "413630",
        "name": "FastCo",
        "expected_price": 3865.0,
        "prev_close": 3865.0,
        "expected_amount": 3_000_000.0,
        "fast_observed_monotonic": 100.0,
    }
    state.get().target_ticker = fast["ticker"]
    state.get().target_candidates = [fast]

    monkeypatch.setattr(f3.paper_fast_probe, "hybrid_enabled", lambda: True)
    monkeypatch.setattr(f3.paper_fast_probe, "get_open_candidates", lambda: [fast])
    monkeypatch.setattr(f3.time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(f3, "F3_FAST_RECHECK_MAX_AGE_SEC", 15.0)
    cash_spy = AsyncMock(return_value=1_000_000.0)
    monkeypatch.setattr(f3, "_available_cash_for_entry", cash_spy)
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    await f3.run()

    cash_spy.assert_not_awaited()
    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"


# ── Opening-transition stale classification + hard recheck budget ──


@pytest.mark.asyncio
async def test_fetch_expected_price_retries_opening_transition_then_recovers(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3, "F3_RECHECK_RETRY_DELAY_SEC", 0.0)
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    get = AsyncMock(
        side_effect=[
            _open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=0),
            _open_boundary_price_resp(
                antc_cnpr=3985, stck_prpr=3985, stck_prdy_clpr=3865, stck_oprc=3900
            ),
        ]
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    result = await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)

    assert result == (3985.0, 3865.0)
    assert get.await_count == 2


@pytest.mark.asyncio
async def test_fetch_expected_price_stale_exhaustion_returns_unavailable(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    # Positive delay so the retry sleep fires; sleep is mocked, so no real wait.
    monkeypatch.setattr(f3, "F3_RECHECK_RETRY_DELAY_SEC", 0.01)
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)
    sleep = AsyncMock()
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    get = AsyncMock(
        return_value=_open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=0)
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    expected, prev_close = await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)

    assert expected == 0.0
    assert prev_close == 3865.0
    assert get.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_fetch_expected_price_hard_budget_stops_before_max_attempts(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3, "F3_RECHECK_RETRY_DELAY_SEC", 0.5)
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    # Deterministic wall clock: budget deadline is set at 1000+5=1005; the clock
    # jumps past it before the 2nd attempt, so the loop must stop early.
    ticks = iter([1000.0, 1000.0, 1001.0, 1006.0, 1006.0, 1006.0, 1006.0])
    last = [1006.0]

    def fake_monotonic():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    monkeypatch.setattr(f3.time, "monotonic", fake_monotonic)
    get = AsyncMock(
        return_value=_open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=0)
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    expected, prev_close = await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)

    assert expected == 0.0
    assert prev_close == 3865.0
    assert get.await_count < 3


@pytest.mark.asyncio
async def test_fetch_expected_price_get_timeout_returns_unavailable(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise f3.asyncio.TimeoutError()

    monkeypatch.setattr(f3.asyncio, "wait_for", fake_wait_for)
    get = AsyncMock(
        return_value=_open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=0)
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    expected, prev_close = await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)

    assert expected == 0.0
    assert prev_close == 3865.0


@pytest.mark.asyncio
async def test_fetch_expected_price_propagates_cancelled_error(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise f3.asyncio.CancelledError()

    monkeypatch.setattr(f3.asyncio, "wait_for", fake_wait_for)
    get = AsyncMock(
        return_value=_open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=0)
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    with pytest.raises(f3.asyncio.CancelledError):
        await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)


@pytest.mark.asyncio
async def test_fetch_expected_price_accepts_legit_post_open_zero_gap(monkeypatch):
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3, "F3_RECHECK_TOTAL_BUDGET_SEC", 5.0)
    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock())
    # stck_oprc>0 with current==prev is real market data (0% gap): accept, no retry.
    get = AsyncMock(
        return_value=_open_boundary_price_resp(stck_prpr=3865, stck_prdy_clpr=3865, stck_oprc=3870)
    )
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    result = await f3._fetch_expected_price("413630", fallback_prev_close=3865.0)

    assert result == (3865.0, 3865.0)
    assert get.await_count == 1


# ── 회귀: 종료 상태 영속화 (2026-08-04 인시던트) ────────────────────────
@pytest.mark.asyncio
async def test_reset_to_idle_persisted_writes_idle_to_disk(monkeypatch):
    """안전한 종료 경로는 인메모리 IDLE을 디스크에도 durable하게 반영해야 한다."""
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {"order_id": "X", "ticker": "006340", "requested_qty": 10}
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    # 먼저 ENTERING을 디스크에 기록 (인시던트의 취소 후 persist 상황 재현)
    await f3.state.persist(_os.environ["STATE_DIR"], f3._today())

    ok = await f3._reset_to_idle_persisted("ENTRY_FAIL")

    assert ok is True
    assert state.get().position_status == "IDLE"
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["position_status"] == "IDLE"
    assert loaded["pending_entry"] is None


@pytest.mark.asyncio
async def test_reset_to_idle_persisted_failure_is_fail_closed(monkeypatch):
    """종료 상태 영속화가 실패하면 성공을 가장하지 않고 fail-closed 처리한다."""
    _reset_state()
    state.get().position_status = "ENTERING"
    events = []
    notes = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    async def _capture_send(*a, **k):
        notes.append((a, k))

    monkeypatch.setattr(f3.notifier, "send", AsyncMock(side_effect=_capture_send))
    monkeypatch.setattr(f3.state, "persist", AsyncMock(side_effect=RuntimeError("disk full")))

    ok = await f3._reset_to_idle_persisted("ENTRY_FAIL")

    assert ok is False
    # 인메모리는 IDLE 유지, 신규 진입은 차단, CRIT 기록/통지 — 디스크 ENTERING은
    # 재시작 시 fail-closed로 걸린다.
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    crit = [k for e, k in events if e == "ENTRY_TERMINAL_PERSIST_ERROR"]
    assert crit and crit[0]["level"] == "CRIT"
    # 기존 pending/취소 미확인 이벤트를 재사용하지 않는다
    assert "ENTRY_PENDING_PERSIST_ERROR" not in [e for e, _ in events]
    assert any(a and a[0] == "ENTRY_TERMINAL_PERSIST_ERROR" for a, _ in notes)


@pytest.mark.asyncio
async def test_cancelled_then_gap_change_persists_idle_to_disk(monkeypatch):
    """인시던트 경로: 주문 접수→미체결 취소→다음 시도 갭변동 거절 시
    디스크 상태가 ENTERING으로 남지 않고 IDLE로 durable하게 정정돼야 한다."""
    _reset_state()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))

    def _quote(ask):
        return f3.EntryQuote(
            ask_price=ask,
            ask_qty=100,
            antc_price=0,
            fetched_monotonic=f3.time.monotonic(),
            rt_cd="0",
            msg_cd="MCA00000",
            msg1="OK",
        )

    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(side_effect=[_quote(10_300), _quote(11_000)]),
    )
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000796", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("CANCELLED", None)),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    # state.persist는 실제 구현을 사용 (디스크 검증 목적)

    await f3.run()

    assert state.get().position_status == "IDLE"
    assert state.get().close_reason == "GAP_CHANGED"
    assert state.get().day_skip is True
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["position_status"] == "IDLE"
    assert loaded["pending_entry"] is None


# ── 회귀: 체결 폴링의 정밀 마감·적응형 대기 ──────────────────────────────
def _install_fake_clock(monkeypatch, start, query_cost_sec):
    clock = {"now": start}
    query_starts = []

    fake_dt = MagicMock()
    fake_dt.now.side_effect = lambda tz=None: clock["now"]
    monkeypatch.setattr(f3, "datetime", fake_dt)

    async def fake_snapshot(order_id, **kwargs):
        query_starts.append(clock["now"])
        clock["now"] = clock["now"] + _timedelta(seconds=query_cost_sec)
        return None

    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", fake_snapshot)

    async def fake_sleep(secs):
        clock["now"] = clock["now"] + _timedelta(seconds=secs)

    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock(side_effect=fake_sleep))
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    return clock, query_starts


@pytest.mark.asyncio
async def test_poll_fill_precise_deadline_allows_second_poll(monkeypatch):
    """2초 마감이 절삭/무조건 1초 대기 때문에 1회 폴링으로 굳어지면 안 된다."""
    start = f3.datetime.now(f3.KST)
    deadline = start + _timedelta(seconds=2.0)
    _clock, query_starts = _install_fake_clock(monkeypatch, start, query_cost_sec=0.3)

    fill = await f3._poll_fill("O1", deadline=deadline, ticker="006340", expected_qty=10)

    assert fill is None
    assert len(query_starts) >= 2
    assert all(t < deadline for t in query_starts)


@pytest.mark.asyncio
async def test_poll_fill_never_starts_query_at_or_after_deadline(monkeypatch):
    """마감에 도달/초과한 뒤에는 신규 체결조회를 시작하지 않는다 (노출창 미연장)."""
    start = f3.datetime.now(f3.KST)
    deadline = start + _timedelta(seconds=2.0)
    # 느린 조회(1.5초) — 두 번째 대기 후 마감을 넘어서므로 재조회 금지
    _clock, query_starts = _install_fake_clock(monkeypatch, start, query_cost_sec=1.5)

    fill = await f3._poll_fill("O1", deadline=deadline, ticker="006340", expected_qty=10)

    assert fill is None
    assert len(query_starts) == 1
    assert all(t < deadline for t in query_starts)
    assert f3._last_fill_poll_summary["poll_attempts"] == 1
    assert f3._last_fill_poll_summary["poll_deadline"] == deadline.strftime("%H:%M:%S")


@pytest.mark.asyncio
async def test_poll_fill_cancels_inflight_get_at_deadline(monkeypatch):
    """느린 GET 하나가 체결 폴링의 전체 마감을 연장하지 않는다."""
    deadline = f3.datetime.now(f3.KST) + _timedelta(seconds=0.02)
    events = []

    async def slow_snapshot(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", slow_snapshot)
    monkeypatch.setattr(f3, "log", lambda event, **fields: events.append((event, fields)))

    started = time.perf_counter()
    fill = await f3._poll_fill("O-SLOW", deadline=deadline, ticker="006340")
    elapsed = time.perf_counter() - started

    assert fill is None
    assert elapsed < 0.2
    assert f3._last_fill_poll_summary["poll_last_error"] == "DEADLINE_TIMEOUT"
    assert [event for event, _ in events].count("ENTRY_FILL_POLL_TIMEOUT") == 1


@pytest.mark.asyncio
async def test_poll_fill_returns_partial_on_timeout(monkeypatch):
    """마감 시 확인된 부분체결 요약/반환 시맨틱을 보존한다."""
    start = f3.datetime.now(f3.KST)
    deadline = start + _timedelta(seconds=2.0)
    clock = {"now": start}

    fake_dt = MagicMock()
    fake_dt.now.side_effect = lambda tz=None: clock["now"]
    monkeypatch.setattr(f3, "datetime", fake_dt)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    partial = {
        "status": "PARTIAL",
        "order_qty": 10,
        "fill_qty": 4,
        "remaining_qty": 6,
        "fill_price": 10_500,
    }

    async def fake_snapshot(order_id, **kwargs):
        clock["now"] = clock["now"] + _timedelta(seconds=0.3)
        return partial

    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", fake_snapshot)

    async def fake_sleep(secs):
        clock["now"] = clock["now"] + _timedelta(seconds=secs)

    monkeypatch.setattr(f3.asyncio, "sleep", AsyncMock(side_effect=fake_sleep))

    fill = await f3._poll_fill("O1", deadline=deadline, ticker="006340", expected_qty=10)

    assert fill == partial


# ── 회귀: 후보 소진 이벤트 분리 ──────────────────────────────────────────
def test_entry_candidate_exhausted_has_accurate_label():
    from src.utils.logger import event_label

    label = event_label("ENTRY_CANDIDATE_EXHAUSTED")
    assert label != "ENTRY_CANDIDATE_EXHAUSTED(ENTRY_CANDIDATE_EXHAUSTED)"
    assert "마감" not in label  # 소진은 마감초과와 구분돼야 한다


# ── 회귀 (correction round 1): 종료 close_reason 디스크/메모리 일치 ──────
def _limit_quote(ask, *, age_sec=0.0):
    return f3.EntryQuote(
        ask_price=ask,
        ask_qty=100,
        antc_price=0,
        fetched_monotonic=f3.time.monotonic() - age_sec,
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )


@pytest.mark.asyncio
async def test_retry_early_gap_guard_blocks_before_slow_checks(monkeypatch):
    _reset_state()
    state.get().position_status = "ENTERING"
    reject = AsyncMock(return_value="GAP_CHANGED")
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(return_value=_limit_quote(10_050)),
    )
    monkeypatch.setattr(f3, "_reject_final_entry_price", reject)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    reason = await f3._early_retry_gap_guard(
        "005930",
        expected_price=10_300,
        prev_close=10_000,
        allow_candidate_retry=True,
        entry_attempt=2,
    )

    assert reason == "GAP_CHANGED"
    reject.assert_awaited_once()
    assert reject.await_args.args[:2] == ("005930", "GAP_CHANGED")


@pytest.mark.asyncio
async def test_entry_total_budget_is_shadow_only(monkeypatch):
    events = []
    clock = iter([100.0, 146.0])
    pipeline = AsyncMock()
    monkeypatch.setattr(f3.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(f3, "F3_ENTRY_TOTAL_BUDGET_SEC", 45.0)
    monkeypatch.setattr(f3, "_run_pipeline", pipeline)
    monkeypatch.setattr(f3, "log", lambda event, **fields: events.append((event, fields)))

    await f3.run()

    pipeline.assert_awaited_once_with()
    shadow = [fields for event, fields in events if event == "ENTRY_BUDGET_EXCEEDED_SHADOW"]
    assert len(shadow) == 1
    assert shadow[0]["enforcement"] is False
    assert shadow[0]["elapsed_ms"] == 46_000


@pytest.mark.asyncio
async def test_buyable_qty_zero_persists_final_close_reason(monkeypatch):
    """BUYABLE_QTY_ZERO 비-후보재시도 종료: 디스크 close_reason이 최종 메모리
    값(INSUFFICIENT_BALANCE)과 일치해야 한다 (ENTRY_FAIL로 굳으면 안 된다)."""
    _reset_state()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(
            return_value={
                "nrcvb_buy_qty": 0,
                "nrcvb_buy_amt": 0.0,
                "max_buy_qty": 0,
                "max_buy_amt": 0.0,
                "ord_psbl_cash": 0.0,
            }
        ),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().position_status == "IDLE"
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["position_status"] == "IDLE"
    assert loaded["close_reason"] == state.get().close_reason


@pytest.mark.asyncio
async def test_qty_zero_at_limit_persists_final_close_reason(monkeypatch):
    """지정가 예산 기준 수량 0 비-후보재시도 종료도 디스크 close_reason이
    최종 메모리 값(INSUFFICIENT_BALANCE)과 일치해야 한다."""
    _reset_state()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(
            return_value={
                "nrcvb_buy_qty": 999_999,
                "nrcvb_buy_amt": 9.9e8,
                "max_buy_qty": 999_999,
                "max_buy_amt": 9.9e8,
                "ord_psbl_cash": 100.0,  # 지정가 기준 예산 부족 → limit_buyable_qty=0
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(return_value=_limit_quote(10_300))
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().position_status == "IDLE"
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["close_reason"] == state.get().close_reason


@pytest.mark.asyncio
async def test_final_quote_stale_persists_entry_fail_close_reason(monkeypatch):
    """FINAL_QUOTE_STALE 비-후보재시도 종료: 최종 메모리 close_reason은
    ENTRY_FAIL이며 디스크도 동일해야 한다 (FINAL_QUOTE_STALE로 굳으면 안 된다)."""
    _reset_state()
    events = []
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "F3_FINAL_QUOTE_MAX_AGE_MS", 1_500)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(return_value=_limit_quote(10_300, age_sec=100.0)),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    send_buy.assert_not_awaited()
    blocked = [kwargs for event, kwargs in events if event == "ENTRY_PRICE_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "FINAL_QUOTE_STALE"
    assert blocked[-1]["quote_age_ms"] >= 100_000
    assert state.get().position_status == "IDLE"
    assert state.get().close_reason == "ENTRY_FAIL"
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["close_reason"] == state.get().close_reason


@pytest.mark.asyncio
async def test_final_quote_gap_changed_persists_matching_close_reason(monkeypatch):
    """최종 호가 갭변동 비-후보재시도 종료: 디스크 close_reason이 메모리와 일치."""
    _reset_state()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    # 최종 호가 갭 10% → 주문 전 GAP_CHANGED
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(return_value=_limit_quote(11_000))
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert state.get().position_status == "IDLE"
    assert state.get().close_reason == "GAP_CHANGED"
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["close_reason"] == state.get().close_reason


# ── 회귀 (correction round 1): 취소 미확인 시 디스크 ENTERING+pending 유지 ──
@pytest.mark.asyncio
async def test_cancel_unconfirmed_keeps_entering_pending_on_disk(monkeypatch):
    """취소 미확인(UNCERTAIN)일 때 주문 접수 후 디스크에 기록된 ENTERING+pending
    식별자가 그대로 남아야 한다 — IDLE로 되돌리면 중복 포지션 위험."""
    _reset_state()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_300.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(return_value=_limit_quote(10_300))
    )
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000796", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3,
        "_cancel_entry_order_confirmed",
        AsyncMock(return_value=("UNCERTAIN", None)),
    )
    # 취소 미확인 → pending 복구 경로. 복구를 no-op로 두어 접수 시 디스크 상태를 검증.
    recover = AsyncMock(return_value=False)
    monkeypatch.setattr(f3, "recover_pending_entry", recover)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    recover.assert_awaited_once()
    assert state.get().position_status == "ENTERING"
    assert state.get().day_skip is True
    loaded = f3.state.load(_os.environ["STATE_DIR"])
    assert loaded is not None
    assert loaded["position_status"] == "ENTERING"
    assert loaded["pending_entry"] is not None
    assert loaded["pending_entry"]["order_id"] == "0000000796"


# ── 회귀 (correction round 1): 종료 재영속화 실패도 통지 ──────────────────
@pytest.mark.asyncio
async def test_persist_terminal_or_log_failure_notifies(monkeypatch):
    _reset_state()
    events = []
    notes = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    async def _capture_send(*a, **k):
        notes.append((a, k))

    monkeypatch.setattr(f3.notifier, "send", AsyncMock(side_effect=_capture_send))
    monkeypatch.setattr(f3.state, "persist", AsyncMock(side_effect=RuntimeError("disk full")))

    ok = await f3._persist_terminal_or_log("ENTRY_FAIL")

    assert ok is False
    crit = [k for e, k in events if e == "ENTRY_TERMINAL_PERSIST_ERROR"]
    assert crit and crit[0]["level"] == "CRIT"
    assert any(a and a[0] == "ENTRY_TERMINAL_PERSIST_ERROR" for a, _ in notes)


# ── F1 고갭 유동성 정책 하드닝 (F3 우회 차단) ────────────────────────────
import src.modules.f1_selector as _f1  # noqa: E402


def test_f3_gap_thresholds_are_sourced_from_f1():
    """F3 고갭 임계값은 F1에서 가져와 정책이 갈라지지 않는다 (값 비교, identity 아님)."""
    assert f3.GAP_HIGH_BAND == _f1.GAP_CORE_MAX
    assert f3.GAP_MAX_ORDER == _f1.GAP_HARD_MAX
    assert f3.GAP_MAX_FILL == _f1.GAP_HARD_MAX
    assert f3.HIGH_GAP_MIN_EXPECTED_AMOUNT == _f1.HIGH_GAP_MIN_EXPECTED_AMOUNT
    # 기본 정책값 회귀 방지
    assert f3.GAP_HIGH_BAND == pytest.approx(0.08)
    assert f3.GAP_MAX_ORDER == pytest.approx(0.10)
    assert f3.HIGH_GAP_MIN_EXPECTED_AMOUNT == pytest.approx(1_000_000_000)


def test_amount_qualifies_high_gap_fails_closed_on_missing_or_invalid():
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    assert f3._amount_qualifies_high_gap(thr) is True
    assert f3._amount_qualifies_high_gap(thr + 1) is True
    assert f3._amount_qualifies_high_gap(thr - 1) is False
    assert f3._amount_qualifies_high_gap(None) is False
    assert f3._amount_qualifies_high_gap(0) is False
    assert f3._amount_qualifies_high_gap(-1) is False
    assert f3._amount_qualifies_high_gap(float("nan")) is False
    # 무한대는 임계값보다 크더라도 유효 대금이 아니므로 자격 없음 (fail-closed).
    assert f3._amount_qualifies_high_gap(float("inf")) is False
    assert f3._amount_qualifies_high_gap(float("-inf")) is False
    # 비수치 문자열/타입은 무효로 취급한다.
    assert f3._amount_qualifies_high_gap("abc") is False
    assert f3._amount_qualifies_high_gap(True) is False


def test_candidate_expected_amount_normalizes_to_finite_float_or_none():
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    # 유효 수치는 float로 정규화
    got = f3._candidate_expected_amount({"expected_amount": thr})
    assert got == float(thr) and isinstance(got, float)
    # 부재/무효/무한/비수치는 None으로 정규화 (raw Any 반환 금지)
    assert f3._candidate_expected_amount({}) is None
    assert f3._candidate_expected_amount({"expected_amount": None}) is None
    assert f3._candidate_expected_amount({"expected_amount": float("inf")}) is None
    assert f3._candidate_expected_amount({"expected_amount": float("nan")}) is None
    assert f3._candidate_expected_amount({"expected_amount": "abc"}) is None
    assert f3._candidate_expected_amount(None) is None
    # F1과 동일하게 expected_amount가 0/None이면 avg_amount_5d로 폴백한다.
    assert f3._candidate_expected_amount(
        {"expected_amount": 0, "avg_amount_5d": thr}
    ) == float(thr)
    assert f3._candidate_expected_amount(
        {"expected_amount": None, "avg_amount_5d": thr}
    ) == float(thr)
    # 무한대 대금이 고갭 정책을 통과하지 못한다 (정규화→자격판정 일관성)
    assert f3._amount_qualifies_high_gap(
        f3._candidate_expected_amount({"expected_amount": float("inf")})
    ) is False


def test_evaluate_order_gap_policy_boundaries():
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    low = thr - 1
    # 하한 미만 차단
    assert f3._evaluate_order_gap(0.019, thr) == (False, "BELOW_MIN")
    # 코어 구간 허용 (대금 무관)
    assert f3._evaluate_order_gap(0.05, None) == (True, "OK")
    # 7.99% 저대금 허용
    assert f3._evaluate_order_gap(0.0799, low) == (True, "OK")
    # 8.00% 저대금 차단 (고갭 유동성 요건)
    assert f3._evaluate_order_gap(0.08, low) == (False, "HIGH_GAP_AMOUNT_LOW")
    # 8.00% 적격 대금 허용
    assert f3._evaluate_order_gap(0.08, thr) == (True, "OK")
    # 9.99% 적격 대금 허용
    assert f3._evaluate_order_gap(0.0999, thr) == (True, "OK")
    # 10% 차단
    assert f3._evaluate_order_gap(0.10, thr) == (False, "ABOVE_MAX")
    # 고갭 대금 부재/무효 fail-closed
    assert f3._evaluate_order_gap(0.085, None) == (False, "HIGH_GAP_AMOUNT_LOW")
    assert f3._evaluate_order_gap(0.085, 0) == (False, "HIGH_GAP_AMOUNT_LOW")


def test_evaluate_order_gap_invalid_gap_fails_closed():
    """비유한(NaN/±inf) 갭은 fail-open이 아니라 INVALID_GAP으로 차단한다."""
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    assert f3._evaluate_order_gap(float("nan"), thr) == (False, "INVALID_GAP")
    assert f3._evaluate_order_gap(float("inf"), thr) == (False, "INVALID_GAP")
    assert f3._evaluate_order_gap(float("-inf"), thr) == (False, "INVALID_GAP")
    # 대금이 없어도 NaN 갭은 INVALID_GAP(과열 고갭 사유보다 우선)로 차단한다.
    assert f3._evaluate_order_gap(float("nan"), None) == (False, "INVALID_GAP")


@pytest.mark.asyncio
async def test_initial_recheck_invalid_gap_blocks_no_order(monkeypatch):
    """운영 경로: 재검증 갭이 NaN이면 주문을 보내지 않고 차단한다."""
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _ok_amt())]
    send_buy = AsyncMock()
    monkeypatch.setattr(
        f3, "_fetch_expected_price", AsyncMock(return_value=(float("nan"), 10_000.0))
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True


def test_resolve_recovery_expected_amount_priority():
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    s = state.get()
    s.target_candidates = [{"ticker": "006340", "expected_amount": thr}]
    # 유효 pending(저대금) 우선 — 더 높은 후보 대금으로 대체되지 않는다.
    amt, src = f3._resolve_recovery_expected_amount(
        {"expected_amount": 100.0}, s, "006340"
    )
    assert (amt, src) == (100.0, "pending")
    # 무효 pending(NaN)은 후보로 폴백한다.
    amt, src = f3._resolve_recovery_expected_amount(
        {"expected_amount": float("nan")}, s, "006340"
    )
    assert (amt, src) == (float(thr), "candidates")
    # 무효 pending + 후보 없음 → 미확인
    s.target_candidates = []
    amt, src = f3._resolve_recovery_expected_amount(
        {"expected_amount": "abc"}, s, "006340"
    )
    assert (amt, src) == (None, "unavailable")
    # expected_amount 키 자체가 없는 구버전 pending은 현재 스키마 결측과 구분한다.
    amt, src = f3._resolve_recovery_expected_amount({}, s, "006340")
    assert (amt, src) == (None, "legacy_unavailable")


@pytest.mark.asyncio
async def test_recover_invalid_pending_amount_falls_back_to_candidate(monkeypatch):
    """복구: pending 대금이 무효(NaN)면 후보 대금으로 폴백 — 적격이면 청산 안 함."""
    close_now = AsyncMock()
    await _run_recovery(
        monkeypatch,
        _recovery_pending(expected_amount=float("nan")),
        10_850,
        close_now,
        candidates=[_high_gap_candidate("006340", _ok_amt())],
    )
    close_now.assert_not_awaited()


@pytest.mark.parametrize(
    ("prev_close", "expected_cap"),
    [
        (1_300, 1_429),
        (10_000, 10_990),
        (50_000, 54_900),
        (200_000, 219_500),
    ],
)
def test_strict_gap_cap_qualifying_amount_uses_10pct(prev_close, expected_cap):
    cap = f3._strict_gap_cap(prev_close, expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT)
    boundary = Decimal(prev_close) * Decimal("1.10")
    assert cap == expected_cap
    assert Decimal(str(cap)) < boundary
    assert Decimal(str(cap + f3._tick_size(cap))) >= boundary


@pytest.mark.parametrize("prev_close", [1_300, 10_000, 50_000, 200_000])
def test_strict_gap_cap_low_or_missing_amount_uses_8pct(prev_close):
    boundary_8 = Decimal(prev_close) * Decimal("1.08")
    for cap in (
        f3._strict_gap_cap(prev_close),
        f3._strict_gap_cap(prev_close, expected_amount=None),
        f3._strict_gap_cap(prev_close, expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT - 1),
    ):
        assert Decimal(str(cap)) < boundary_8
        assert Decimal(str(cap + f3._tick_size(cap))) >= boundary_8


def test_strict_gap_cap_no_boundary_violations_both_ceilings():
    price_bands = (
        range(1, 2_000, 1),
        range(2_000, 5_000, 5),
        range(5_000, 20_000, 10),
        range(20_000, 50_000, 50),
        range(50_000, 200_000, 100),
        range(200_000, 500_000, 500),
        range(500_000, 1_000_001, 1_000),
    )
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    violations = []
    for band in price_bands:
        for prev_close in band:
            cap10 = f3._strict_gap_cap(prev_close, expected_amount=thr)
            if Decimal(str(cap10)) >= Decimal(prev_close) * Decimal("1.10"):
                violations.append(("10", prev_close, cap10))
            cap8 = f3._strict_gap_cap(prev_close)
            if Decimal(str(cap8)) >= Decimal(prev_close) * Decimal("1.08"):
                violations.append(("8", prev_close, cap8))
    assert violations == []


def test_evaluate_post_fill_guard_reasons():
    thr = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT
    # 정상 코어 체결: 통과
    assert f3._evaluate_post_fill_guard(
        fill_price=10_300, submitted_order_price=10_400, prev_close=10_000, expected_amount=None
    ) == []
    # 체결 갭 >=10%: FILL_GAP
    assert "FILL_GAP" in f3._evaluate_post_fill_guard(
        fill_price=11_000, submitted_order_price=11_500, prev_close=10_000, expected_amount=thr
    )
    # 지정가 초과: LIMIT_PRICE
    assert "LIMIT_PRICE" in f3._evaluate_post_fill_guard(
        fill_price=10_500, submitted_order_price=10_400, prev_close=10_000, expected_amount=thr
    )
    # [8%,10%) 저대금: HIGH_GAP_AMOUNT_LOW
    assert f3._evaluate_post_fill_guard(
        fill_price=10_850, submitted_order_price=11_000, prev_close=10_000, expected_amount=thr - 1
    ) == ["HIGH_GAP_AMOUNT_LOW"]
    # [8%,10%) 대금 부재 fail-safe
    assert f3._evaluate_post_fill_guard(
        fill_price=10_850, submitted_order_price=11_000, prev_close=10_000, expected_amount=None
    ) == ["HIGH_GAP_AMOUNT_LOW"]
    # [8%,10%) 적격 대금: 통과
    assert f3._evaluate_post_fill_guard(
        fill_price=10_850, submitted_order_price=11_000, prev_close=10_000, expected_amount=thr
    ) == []
    # 8% 미만 대금 부재: 통과 (하위 호환)
    assert f3._evaluate_post_fill_guard(
        fill_price=10_790, submitted_order_price=11_000, prev_close=10_000, expected_amount=None
    ) == []


@pytest.mark.parametrize(
    ("fill_price", "submitted_order_price", "prev_close"),
    [
        (float("nan"), 11_000, 10_000),
        (float("inf"), 11_000, 10_000),
        (10_850, float("nan"), 10_000),
        (10_850, 11_000, float("nan")),
        (10_850, 11_000, 0),
    ],
)
def test_evaluate_post_fill_guard_invalid_prices_fail_closed(
    fill_price, submitted_order_price, prev_close
):
    assert f3._evaluate_post_fill_guard(
        fill_price=fill_price,
        submitted_order_price=submitted_order_price,
        prev_close=prev_close,
        expected_amount=f3.HIGH_GAP_MIN_EXPECTED_AMOUNT,
    ) == ["INVALID_FILL_DATA"]


# ── F1 고갭 유동성 우회 차단: 주문 게이트/체결 후/복구 통합 ────────────────
import src.modules.f4_tracking as _f4  # noqa: E402


def _high_gap_candidate(ticker, amount):
    return {"ticker": ticker, "name": ticker, "expected_amount": amount, "prev_close": 10_000}


def _low_amt():
    return f3.HIGH_GAP_MIN_EXPECTED_AMOUNT - 1


def _ok_amt():
    return f3.HIGH_GAP_MIN_EXPECTED_AMOUNT


def _ok_buy_resp():
    return {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    }


@pytest.mark.asyncio
async def test_initial_recheck_high_gap_low_amount_blocks_no_order(monkeypatch):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _low_amt())]
    events = []
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_850.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    blocked = [k for e, k in events if e == "F3_ENTRY_BLOCKED"]
    assert any(k.get("reason") == "HIGH_GAP_AMOUNT_LOW" for k in blocked)


@pytest.mark.asyncio
async def test_initial_recheck_high_gap_qualifying_amount_allows_order(monkeypatch):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _ok_amt())]
    send_buy = AsyncMock(return_value=_ok_buy_resp())
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_850.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    _mock_final_quote(monkeypatch, 10_800)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10_700, "fill_qty": 8})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10_700))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3._run_single()

    send_buy.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_ask_gate_high_gap_low_amount_blocks_no_order(monkeypatch):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _low_amt())]
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    _mock_final_quote(monkeypatch, 10_850)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True


@pytest.mark.asyncio
async def test_post_fill_first_buy_high_gap_low_amount_triggers_guard(monkeypatch):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _low_amt())]
    close_now = AsyncMock()
    monkeypatch.setattr(_f4, "close_now", close_now)
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    _mock_final_quote(monkeypatch, 10_300)
    monkeypatch.setattr(
        f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=8, amt=100_000.0))
    )
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value=_ok_buy_resp()))
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10_850, "fill_qty": 8})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10_850))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    await f3._run_single()

    close_now.assert_awaited_once()
    assert close_now.await_args.args[1] == "SLIPPAGE_GUARD"
    assert state.get().day_skip is True
    guard = [k for e, k in events if e == "SLIPPAGE_GUARD"]
    assert guard and "HIGH_GAP_AMOUNT_LOW" in guard[-1]["guard_reasons"]


def _recovery_pending(**over):
    base = {
        "order_id": "0000000937",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 8,
        "limit_price": 11_000,
        "anchor_price": 10_300,
        "prev_close": 10_000,
    }
    base.update(over)
    return base


async def _run_recovery(monkeypatch, pending, fill_price, close_now, candidates=None):
    _reset_state()
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = dict(pending)
    if candidates is not None:
        s.target_candidates = candidates
    fill = {
        "status": "FILLED",
        "order_qty": 8,
        "fill_qty": 8,
        "remaining_qty": 0,
        "fill_price": fill_price,
    }
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", AsyncMock(return_value=fill))
    monkeypatch.setattr(f3.db, "get_order_by_kis_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(return_value={"id": 1, "ticker": "006340", "status": "OPEN"}),
    )
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)
    monkeypatch.setattr(_f4, "close_now", close_now)
    recovered = await f3.recover_pending_entry()
    tasks = [t for t in f3._entry_audit_tasks if t.get_loop() is asyncio.get_running_loop()]
    if tasks:
        await asyncio.gather(*tasks)
    return recovered


@pytest.mark.asyncio
async def test_recover_high_gap_persisted_low_amount_guards(monkeypatch):
    close_now = AsyncMock()
    await _run_recovery(
        monkeypatch, _recovery_pending(expected_amount=_low_amt()), 10_850, close_now
    )
    close_now.assert_awaited_once()
    assert close_now.await_args.args[1] == "SLIPPAGE_GUARD"
    assert state.get().day_skip is True


@pytest.mark.asyncio
async def test_recover_high_gap_missing_amount_fail_safe_guards(monkeypatch):
    close_now = AsyncMock()
    await _run_recovery(
        monkeypatch,
        _recovery_pending(expected_amount=None),
        10_850,
        close_now,
    )
    close_now.assert_awaited_once()
    assert state.get().day_skip is True


@pytest.mark.asyncio
async def test_recover_legacy_high_gap_unknown_amount_does_not_force_close(monkeypatch):
    """구버전 pending의 스키마 결측만으로 과거 합법 체결을 강제 청산하지 않는다."""
    close_now = AsyncMock()
    await _run_recovery(monkeypatch, _recovery_pending(), 10_850, close_now)
    close_now.assert_not_awaited()
    assert state.get().day_skip is False


@pytest.mark.asyncio
async def test_recover_below_8pct_missing_amount_is_compatible(monkeypatch):
    close_now = AsyncMock()
    await _run_recovery(monkeypatch, _recovery_pending(), 10_300, close_now)
    close_now.assert_not_awaited()
    assert state.get().day_skip is False


@pytest.mark.asyncio
async def test_recover_high_gap_amount_resolved_from_candidates(monkeypatch):
    close_now = AsyncMock()
    await _run_recovery(
        monkeypatch,
        _recovery_pending(),
        10_850,
        close_now,
        candidates=[_high_gap_candidate("006340", _ok_amt())],
    )
    close_now.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_entry_persists_expected_amount(monkeypatch):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _ok_amt())]
    captured = {}

    async def _capture_pending(payload):
        captured.update(payload)
        state.get().pending_entry = dict(payload)

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3.state, "set_pending_entry", _capture_pending)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    _mock_final_quote(monkeypatch, 10_300)
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value=_ok_buy_resp()))
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10_300, "fill_qty": 8})
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10_300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3._run_single()

    assert captured.get("expected_amount") == _ok_amt()


def test_high_gap_amount_low_is_candidate_retryable():
    assert "HIGH_GAP_AMOUNT_LOW" in f3._CANDIDATE_RETRY_REASONS
    assert "HIGH_GAP_AMOUNT_LOW" in f3._EXPECTED_CANDIDATE_REJECTIONS


def test_high_gap_amount_low_has_dedicated_event_label():
    from src.utils import logger as _logger

    label = _logger.event_label("HIGH_GAP_AMOUNT_LOW")
    # 원시 중복 라벨("HIGH_GAP_AMOUNT_LOW(HIGH_GAP_AMOUNT_LOW)")로 떨어지면 안 된다.
    assert label != "HIGH_GAP_AMOUNT_LOW(HIGH_GAP_AMOUNT_LOW)"
    assert "HIGH_GAP_AMOUNT_LOW" in _logger.EVENT_LABELS


@pytest.mark.parametrize(
    ("vi_price", "amount", "expected"),
    [
        (10_799, None, False),
        (10_800, None, True),
        (10_999, None, True),
        (10_999, "OK", False),
        (11_000, "OK", True),
        (0, "OK", False),
    ],
)
def test_vi_price_gap_cap_is_amount_aware(vi_price, amount, expected):
    resolved = f3.HIGH_GAP_MIN_EXPECTED_AMOUNT if amount == "OK" else None
    vi_info = dict(_VI_ACTIVE_INFO)
    vi_info["vi_prc"] = str(vi_price)
    assert f3._vi_price_reaches_gap_cap(vi_info, 10_000, resolved) is expected


@pytest.mark.asyncio
async def test_early_retry_gap_guard_blocks_high_gap_low_amount(monkeypatch):
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(return_value=_entry_quote(10_850))
    )
    reject = AsyncMock(return_value="HIGH_GAP_AMOUNT_LOW")
    monkeypatch.setattr(f3, "_reject_final_entry_price", reject)

    result = await f3._early_retry_gap_guard(
        "006340",
        expected_price=10_300,
        prev_close=10_000,
        allow_candidate_retry=True,
        entry_attempt=2,
        expected_amount=_low_amt(),
    )

    assert result == "HIGH_GAP_AMOUNT_LOW"
    assert reject.await_args.args[1] == "HIGH_GAP_AMOUNT_LOW"


@pytest.mark.asyncio
async def test_early_retry_gap_guard_allows_high_gap_qualifying_amount(monkeypatch):
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(return_value=_entry_quote(10_850))
    )
    reject = AsyncMock()
    monkeypatch.setattr(f3, "_reject_final_entry_price", reject)

    result = await f3._early_retry_gap_guard(
        "006340",
        expected_price=10_300,
        prev_close=10_000,
        allow_candidate_retry=True,
        entry_attempt=2,
        expected_amount=_ok_amt(),
    )

    assert result is None
    reject.assert_not_awaited()


# ── 경로 테스트 (correction round 1): 게이트별 행위 검증 ──────────────────
@pytest.mark.asyncio
async def test_rank_excludes_high_gap_low_amount_and_selects_next(monkeypatch):
    """랭킹 재검증: 정확히 8% 저대금 후보를 제외하고 다음 유효 후보를 고른다."""
    _reset_state()
    s = state.get()
    s.target_ticker = "111111"
    s.target_candidates = [
        {"ticker": "111111", "name": "A", "expected_amount": _low_amt(), "prev_close": 10_000},
        {"ticker": "222222", "name": "B", "expected_amount": _ok_amt(), "prev_close": 10_000},
    ]
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))
    monkeypatch.setattr(f3, "_fast_recheck_rows", lambda *a, **k: None)
    monkeypatch.setattr(f3, "_available_cash_for_entry", AsyncMock(return_value=1_000_000.0))

    async def _expected(ticker, fallback_prev_close=0.0):
        # 111111: 정확히 8.0% (저대금 → 제외), 222222: 3.0% 코어 (선택)
        return (10_800.0, 10_000.0) if ticker == "111111" else (10_300.0, 10_000.0)

    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(side_effect=_expected))

    ranked = await f3._rank_final_entry_candidates(s)

    assert ranked is not None
    assert ranked[0]["ticker"] == "222222"
    assert all(row["ticker"] != "111111" for row in ranked)
    blocked = [k for e, k in events if e == "F3_ENTRY_BLOCKED" and k.get("ticker") == "111111"]
    assert blocked and blocked[-1]["reason"] == "HIGH_GAP_AMOUNT_LOW"
    assert blocked[-1]["expected_amount"] == _low_amt()


@pytest.mark.asyncio
async def test_pipeline_rotates_after_high_gap_low_amount_and_enters_next(monkeypatch):
    """파이프라인: 1번 후보의 고갭 저대금 거절 후 2번 후보 진입까지 이어진다."""
    _reset_state()
    s = state.get()
    first = {"ticker": "111111", "name": "A", "expected_amount": _low_amt()}
    second = {"ticker": "222222", "name": "B", "expected_amount": _ok_amt()}
    s.target_ticker = first["ticker"]
    s.target_name = first["name"]
    s.target_candidates = [first, second]
    ranked = [
        {
            "ticker": first["ticker"],
            "candidate": first,
            "candidate_rank": 1,
            "expected_price": 10_850.0,
            "prev_close": 10_000.0,
            "cash": 1_000_000.0,
            "total_amount": 950_000,
            "total_qty": 87,
        },
        {
            "ticker": second["ticker"],
            "candidate": second,
            "candidate_rank": 2,
            "expected_price": 10_300.0,
            "prev_close": 10_000.0,
            "cash": 1_000_000.0,
            "total_amount": 950_000,
            "total_qty": 92,
        },
    ]
    monkeypatch.setattr(f3, "_rank_final_entry_candidates", AsyncMock(return_value=ranked))
    monkeypatch.setattr(f3, "_refresh_entry_candidate", AsyncMock(return_value=ranked[1]))
    monkeypatch.setattr(f3, "_before_deadline", lambda *_: True)
    monkeypatch.setattr(f3, "log", lambda *a, **k: None)

    calls = []

    async def run_single(*, picked=None, allow_candidate_retry=False):
        calls.append((picked["ticker"], allow_candidate_retry))
        if picked["ticker"] == first["ticker"]:
            return "HIGH_GAP_AMOUNT_LOW"
        state.get().position_status = "HOLDING"
        return None

    monkeypatch.setattr(f3, "_run_single", run_single)

    await f3._run_pipeline()

    assert calls == [("111111", True), ("222222", True)]
    assert state.get().target_ticker == "222222"
    assert state.get().position_status == "HOLDING"
    f3._refresh_entry_candidate.assert_awaited_once_with(ranked[1])


@pytest.mark.asyncio
async def test_refresh_entry_candidate_rejects_crossing_to_high_gap_low_amount(monkeypatch):
    """후보 리프레시: >=8%로 넘어가며 적격 대금이 없으면 제외(None) + 증거 로그."""
    _reset_state()
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_850.0, 10_000.0)))
    picked = {
        "ticker": "006340",
        "candidate": {"ticker": "006340", "expected_amount": _low_amt(), "prev_close": 10_000},
        "candidate_rank": 2,
        "expected_price": 10_300.0,
        "prev_close": 10_000.0,
        "total_amount": 1_000_000,
        "cash": 1_000_000,
    }

    refreshed = await f3._refresh_entry_candidate(picked)

    assert refreshed is None
    blocked = [k for e, k in events if e == "F3_ENTRY_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "HIGH_GAP_AMOUNT_LOW"
    assert blocked[-1]["expected_amount"] == _low_amt()


@pytest.mark.asyncio
async def test_vi_release_then_fresh_quote_crossing_high_gap_low_amount_no_order(monkeypatch):
    """VI 해제 확인 후에도 최종 호가가 8.5% 저대금이면 주문을 보내지 않는다."""
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", _low_amt())]
    send_buy = AsyncMock()
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    # 초기 재검증은 코어(3.1%) 통과, VI 발동 후 해제 확인, 최종 호가 8.5% 크로싱
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(return_value=dict(_VI_ACTIVE_INFO)))
    monkeypatch.setattr(f3, "_wait_for_vi_release", AsyncMock(return_value=True))
    _mock_final_quote(monkeypatch, 10_850)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single()

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True


def _pyramid_common(monkeypatch, *, final_quotes, fills, amount, current_price):
    _reset_state()
    state.get().target_candidates = [_high_gap_candidate("006340", amount)]
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "FIRST_RATIO", 0.5)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10_310.0, 10_000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3, "_fetch_final_entry_quote", AsyncMock(side_effect=list(final_quotes))
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(side_effect=list(fills)))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=current_price))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "mark_pyramided", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())


@pytest.mark.asyncio
async def test_dormant_pyramid_no_second_buy_on_high_gap_low_amount_crossing(monkeypatch):
    """휴면 피라미딩: 2차 호가가 8.5% 저대금으로 크로싱하면 2차 매수를 보내지 않는다."""
    send_buy = AsyncMock(return_value=_ok_buy_resp())
    _pyramid_common(
        monkeypatch,
        final_quotes=[_entry_quote(10_300), _entry_quote(10_850)],
        fills=[{"fill_price": 10_300, "fill_qty": 999_999}],
        amount=_low_amt(),
        current_price=10_400,
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3._run_single()

    # 1차 매수만 전송, 2차(피라미딩)는 크로싱 차단으로 미전송
    assert send_buy.await_count == 1


@pytest.mark.asyncio
async def test_pyramid_abnormal_fill_high_gap_low_amount_triggers_guard(monkeypatch):
    """피라미딩 이상 체결이 8.5% 저대금이면 공유 체결후 평가기로 SLIPPAGE_GUARD."""
    close_now = AsyncMock()
    send_buy = AsyncMock(return_value=_ok_buy_resp())
    _pyramid_common(
        monkeypatch,
        # 2차 호가는 코어(3%)라 게이트 통과, 그러나 체결은 8.5% 이상 이상 체결
        final_quotes=[_entry_quote(10_300), _entry_quote(10_300)],
        fills=[
            {"fill_price": 10_300, "fill_qty": 999_999},
            {"fill_price": 10_850, "fill_qty": 999_999},
        ],
        amount=_low_amt(),
        current_price=10_400,
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(_f4, "close_now", close_now)
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    await f3._run_single()

    assert send_buy.await_count == 2
    close_now.assert_awaited_once()
    assert close_now.await_args.args[1] == "SLIPPAGE_GUARD"
    assert state.get().day_skip is True
    # 공유 체결후 평가기가 고갭 유동성 부족을 방어 사유로 명시해야 한다.
    guard = [k for e, k in events if e == "SLIPPAGE_GUARD" and k.get("phase") == "PYRAMID"]
    assert guard and "HIGH_GAP_AMOUNT_LOW" in guard[-1]["guard_reasons"]


@pytest.mark.asyncio
async def test_cancel_rejected_twice_confirms_fill_before_unconfirmed(monkeypatch):
    """취소가 두 번 다 거부되면 UNCERTAIN 직전에 체결을 한 번 더 확인한다.

    체결조회는 주문 직후 수 초간 빈 응답을 준다. 그 사이 취소는 기체결 때문에
    거부되므로, 짧은 폴링 창만 보고 UNCERTAIN을 내면 이미 잡힌 포지션을
    '취소 실패'로 오분류한다(20260902 004310).
    """
    filled = {
        "status": "FILLED",
        "order_qty": 216,
        "fill_qty": 216,
        "remaining_qty": 0,
        "fill_price": 8_625,
    }
    cancels: list[int] = []

    async def cancel(*_args, **_kwargs):
        cancels.append(1)
        return {
            "rt_cd": "1",
            "msg_cd": "40330000",
            "msg1": "모의투자 정정/취소할 수량이 없습니다.",
        }

    async def snapshot(*_args, **_kwargs):
        # 재시도 취소까지 거부된 뒤에야 체결조회가 따라잡는다.
        return filled if len(cancels) >= 2 else None

    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", 0.05)
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", snapshot)
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    outcome, reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447",
        "00950",
        "PAPER",
        "004310",
        1,
        2,
        expected_qty=216,
    )

    assert outcome == "FILLED"
    assert reconciled["fill_qty"] == 216
    assert reconciled["fill_price"] == 8_625
    assert not [e for e, _ in events if e == "ENTRY_CANCEL_UNCONFIRMED"]
    stages = [k.get("confirmed_after") for e, k in events if e == "ENTRY_CANCEL_REJECTED_FILLED"]
    assert stages == ["CANCEL_RETRY"]


@pytest.mark.asyncio
async def test_cancel_rejected_twice_without_fill_stays_unconfirmed(monkeypatch):
    """마지막 확인에도 체결이 없으면 UNCERTAIN을 유지한다(fail-closed)."""

    async def cancel(*_args, **_kwargs):
        return {"rt_cd": "1", "msg_cd": "40330000", "msg1": "취소할 수량이 없습니다."}

    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", 0.05)
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    snapshot = AsyncMock(return_value=None)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", snapshot)
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    outcome, reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447",
        "00950",
        "PAPER",
        "004310",
        1,
        2,
        expected_qty=216,
    )

    assert outcome == "UNCERTAIN"
    assert reconciled is None
    assert [e for e, _ in events if e == "ENTRY_CANCEL_UNCONFIRMED"]
    # 폴링 1회 + 포기 직전 최종 확인 1회. 최종 확인을 지우면 여기서 걸린다.
    assert snapshot.await_count >= 2


@pytest.mark.asyncio
async def test_cancel_rejected_twice_keeps_partial_fill_unconfirmed(monkeypatch):
    """부분체결만 확인되면 전량 체결이 아니므로 UNCERTAIN을 유지한다."""
    partial = {
        "status": "PARTIAL",
        "order_qty": 216,
        "fill_qty": 100,
        "remaining_qty": 116,
        "fill_price": 8_625,
    }
    cancels: list[int] = []

    async def cancel(*_args, **_kwargs):
        cancels.append(1)
        return {"rt_cd": "1", "msg_cd": "40330000", "msg1": "취소할 수량이 없습니다."}

    async def snapshot(*_args, **_kwargs):
        return partial if len(cancels) >= 2 else None

    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", 0.05)
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", snapshot)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    outcome, _reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447",
        "00950",
        "PAPER",
        "004310",
        1,
        2,
        expected_qty=216,
    )

    assert outcome == "UNCERTAIN"


@pytest.mark.asyncio
async def test_cancel_final_fill_check_failure_stays_unconfirmed(monkeypatch):
    """마지막 체결 확인이 실패해도 UNCERTAIN으로 떨어진다 — 예외를 올리지 않는다.

    이 지점은 취소 확인의 최종 안전장치다. 여기서 예외가 새면 호출부의
    day_skip·CRIT 알림·pending 복구가 통째로 건너뛰어지고, 체결된 포지션이
    ENTERING인 채 F4 추적 밖에 남는다.
    """

    async def cancel(*_args, **_kwargs):
        return {"rt_cd": "1", "msg_cd": "40330000", "msg1": "취소할 수량이 없습니다."}

    snapshot = AsyncMock(side_effect=RuntimeError("ccld query down"))
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", 0.05)
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", snapshot)
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    outcome, reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447", "00950", "PAPER", "004310", 1, 2, expected_qty=216
    )

    assert outcome == "UNCERTAIN"
    assert reconciled is None
    assert [e for e, _ in events if e == "ENTRY_CANCEL_CONFIRM_ERROR"]
    assert [e for e, _ in events if e == "ENTRY_CANCEL_UNCONFIRMED"]


@pytest.mark.asyncio
async def test_cancel_rejected_twice_rejects_zero_fill_price(monkeypatch):
    """수량이 맞아도 체결가가 0이면 FILLED로 보지 않는다.

    set_holding(0.0)은 F4의 스탑 계산을 통째로 망친다. 가격을 모르면
    UNCERTAIN으로 넘겨 recover_pending_entry의 INVALID_FILL_PRICE 검사에 맡긴다.
    """
    cancels: list[int] = []

    async def cancel(*_args, **_kwargs):
        cancels.append(1)
        return {"rt_cd": "1", "msg_cd": "40330000", "msg1": "취소할 수량이 없습니다."}

    async def snapshot(*_args, **_kwargs):
        if len(cancels) < 2:
            return None
        return {
            "status": "FILLED",
            "order_qty": 216,
            "fill_qty": 216,
            "remaining_qty": 0,
            "fill_price": 0.0,
        }

    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", 0.05)
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", snapshot)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    outcome, _reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447", "00950", "PAPER", "004310", 1, 2, expected_qty=216
    )

    assert outcome == "UNCERTAIN"


@pytest.mark.asyncio
async def test_cancel_rejected_then_filled_marks_confirmation_stage(monkeypatch):
    """첫 폴링에서 체결이 잡히면 confirmed_after=CANCEL_POLL로 남는다.

    ENTRY_CANCEL_REJECTED_FILLED는 이제 두 지점에서 나온다. 어느 쪽이 잡았는지
    로그만 보고 구분할 수 있어야 창 길이 조정의 효과를 사후에 측정할 수 있다.
    """
    filled = {
        "status": "FILLED",
        "order_qty": 216,
        "fill_qty": 216,
        "remaining_qty": 0,
        "fill_price": 8_625,
    }

    async def cancel(*_args, **_kwargs):
        return {"rt_cd": "1", "msg_cd": "40330000", "msg1": "취소할 수량이 없습니다."}

    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_fetch_order_fill_snapshot", AsyncMock(return_value=filled))
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **k: events.append((event, k)))

    outcome, _reconciled = await f3._cancel_entry_order_confirmed(
        "0000003447", "00950", "PAPER", "004310", 1, 2, expected_qty=216
    )

    assert outcome == "FILLED"
    stages = [k.get("confirmed_after") for e, k in events if e == "ENTRY_CANCEL_REJECTED_FILLED"]
    assert stages == ["CANCEL_POLL"]


def test_entry_cash_basis_prefers_orderable_cash():
    """수량 산정은 브로커가 답한 실제 주문가능액을 쓴다."""
    cash, basis = f3._entry_cash_basis(
        {"query_failed": False, "ord_psbl_cash": 9_412_327.0, "nrcvb_buy_qty": 873}
    )
    assert cash == 9_412_327.0
    assert basis == "ORD_PSBL_CASH"


def test_entry_cash_basis_falls_back_when_orderable_cash_absent():
    """0이거나 조회가 실패하면 잔고 요약 경로로 물러난다.

    값이 안 오는 계좌·모드에서 수량 0으로 막히면 그것이 회귀다.
    """
    assert f3._entry_cash_basis({"query_failed": False, "ord_psbl_cash": 0.0}) == (
        None,
        "BALANCE_SUMMARY",
    )
    assert f3._entry_cash_basis({"query_failed": True, "ord_psbl_cash": 5.0}) == (
        None,
        "BALANCE_SUMMARY",
    )
    assert f3._entry_cash_basis(None) == (None, "BALANCE_SUMMARY")


@pytest.mark.asyncio
async def test_run_single_sizes_from_orderable_cash_not_balance_summary(monkeypatch):
    """잔고 요약이 훨씬 큰 값을 불러도 수량은 실제 주문가능액을 따른다.

    dnca_tot_amt는 미결제 매수대금을 반영하지 않아 장부에만 남은 금액을 그대로
    부른다(20260709: 장부 10,064,148 / 실제 119,940). 그 값으로 수량을 잡으면
    주문이 거부된다.
    """
    _reset_state()
    events = []
    send_buy = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }
    )
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(4_660.0, 4_490.0)))
    # 장부값은 100배로 부풀려 둔다 — 이 값을 따라가면 테스트가 깨진다.
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=100_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(return_value=_buyable(ord_psbl_cash=1_000_000.0)),
    )
    monkeypatch.setattr(
        f3,
        "_fetch_final_entry_quote",
        AsyncMock(return_value=_entry_quote(4_690)),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 4_690, "fill_qty": 200})
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    # 1,000,000 × 0.95 / 4,660 = 203 계획, 지정가 예산으로 200 제출.
    sized = [k for e, k in events if e == "ENTRY_QTY_SIZED_AT_LIMIT"][-1]
    assert sized["planned_qty"] == 203
    assert send_buy.await_args.args[1] == 200
    basis = [k for e, k in events if e == "ENTRY_CASH_BASIS"][-1]
    assert basis["cash_basis"] == "ORD_PSBL_CASH"
    assert basis["cash"] == 1_000_000.0


@pytest.mark.asyncio
async def test_run_single_queries_buyable_qty_once_on_first_attempt(monkeypatch):
    """산정용 조회를 앞으로 당겨도 API 왕복이 늘지 않는다.

    09:01 창에서 왕복 한 번은 그대로 지연이다. 주문 루프는 이미 받아둔 결과를
    재사용해야 한다.
    """
    _reset_state()
    buyable = AsyncMock(
        return_value=_buyable(ord_psbl_cash=1_000_000.0)
    )
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(4_660.0, 4_490.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", buyable)
    monkeypatch.setattr(f3, "_fetch_final_entry_quote", AsyncMock(return_value=_entry_quote(4_690)))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 4_690, "fill_qty": 200})
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    assert buyable.await_count == 1

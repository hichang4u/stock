import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 테스트 수집 중에는 개발 머신의 .env가 os.environ을 오염시키지 않게 한다
# (conftest가 STOCK_SKIP_DOTENV=1을 설정). 운영/수동 실행에서는 정상 로드한다.
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(dotenv_path=ROOT / ".env", override=True)

from src import live, state  # noqa: E402
from src.api import auth, kis_ws  # noqa: E402
from src.modules import f3_entry, f4_tracking  # noqa: E402
from src.utils import logger  # noqa: E402
from src.utils.spike_filter import SpikeFilter  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
TICKER = os.getenv("PAPER_F4_TEST_TICKER", "006340")
REST_POLL_SEC = float(os.getenv("PAPER_F4_TEST_REST_POLL_SEC", "15"))
BUY_CASH_RATIO = float(os.getenv("PAPER_F4_TEST_BUY_CASH_RATIO", "1.0"))
_PSBL_TR = {"REAL": "TTTC8908R", "PAPER": "VTTC8908R"}


def now_kst() -> datetime:
    return datetime.now(KST)


def deadline_after(seconds: int) -> tuple[int, int, int]:
    d = now_kst() + timedelta(seconds=seconds)
    return d.hour, d.minute, d.second


def seconds_until_1100() -> float:
    now = now_kst()
    end = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if now >= end:
        return 0.0
    return (end - now).total_seconds()


async def sell_and_poll(ticker: str, qty: int, mode: str, label: str):
    sell_resp = await f4_tracking._send_sell(ticker, qty, mode)
    sell_id = sell_resp.get("output", {}).get("ODNO", "")
    print(
        f"{label}_SELL_SENT rt={sell_resp.get('rt_cd')} "
        f"msg={sell_resp.get('msg_cd')} id={sell_id} text={sell_resp.get('msg1')}",
        flush=True,
    )
    fill = None
    if sell_id:
        fill = await f4_tracking._poll_fill(sell_id, timeout_sec=30)
        print(f"{label}_SELL_FILL {fill}", flush=True)
    return sell_id, fill


def _as_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


async def fetch_orderable_qty(ticker: str, mode: str) -> tuple[int, dict]:
    resp = await f3_entry.kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        tr_id=_PSBL_TR[mode],
        params={
            "CANO": os.getenv("KIS_ACCT_NO", ""),
            "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
            "PDNO": ticker,
            "ORD_UNPR": "0",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        },
    )
    output = resp.get("output") or {}
    qty = max(
        _as_int(output.get("max_buy_qty")),
        _as_int(output.get("ord_psbl_qty")),
        _as_int(output.get("nrcvb_buy_qty")),
    )
    return qty, resp


async def main() -> None:
    mode = os.getenv("KIS_MODE", "PAPER")
    if mode != "PAPER":
        raise SystemExit(f"ABORT: KIS_MODE={mode}, PAPER only")
    if os.getenv("DRY_RUN", "0") == "1":
        raise SystemExit("ABORT: DRY_RUN=1, expected real PAPER API path")

    logger.setup(os.getenv("LOG_DIR", "data/logs"))
    await auth.load_or_refresh()

    remain = seconds_until_1100()
    if remain <= 0:
        raise SystemExit("ABORT: already past 11:00 KST")

    cash = await f3_entry._fetch_available_cash()
    if cash is None:
        raise SystemExit("ABORT: balance query failed (BALANCE_QUERY_FAILED)")
    price = await f3_entry._fetch_current_price(TICKER)
    orderable_qty, psbl_resp = await fetch_orderable_qty(TICKER, mode)
    theoretical_qty = int((cash * BUY_CASH_RATIO) // price) if price else 0
    qty = min(theoretical_qty, orderable_qty) if orderable_qty else theoretical_qty
    print(
        f"PRECHECK time={now_kst().isoformat()} ticker={TICKER} cash={cash} "
        f"current_price={price} buy_cash_ratio={BUY_CASH_RATIO} "
        f"theoretical_qty={theoretical_qty} orderable_qty={orderable_qty} qty={qty} "
        f"amount={qty * price if price else 0} psbl_rt={psbl_resp.get('rt_cd')} "
        f"psbl_msg={psbl_resp.get('msg_cd')} seconds_until_1100={remain:.1f}",
        flush=True,
    )
    if qty <= 0:
        raise SystemExit("NO_BUY_QTY")

    quiet_sec = getattr(f3_entry, "F3_PRE_ORDER_QUIET_SEC", 1.5)
    if quiet_sec > 0:
        print(f"PRE_ORDER_WAIT seconds={quiet_sec}", flush=True)
        await asyncio.sleep(quiet_sec)

    buy_resp = await f3_entry._send_buy(TICKER, qty, mode)
    buy_id = buy_resp.get("output", {}).get("ODNO", "")
    buy_org = buy_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
    print(
        f"BUY_SENT rt={buy_resp.get('rt_cd')} msg={buy_resp.get('msg_cd')} "
        f"id={buy_id} org={buy_org} text={buy_resp.get('msg1')}",
        flush=True,
    )
    if str(buy_resp.get("rt_cd", "")) != "0" or not buy_id:
        raise SystemExit("BUY_REJECTED")

    fill = await f3_entry._poll_fill(buy_id, deadline_after(30), ticker=TICKER)
    if not fill:
        cancel_resp = await f3_entry._cancel_order(buy_id, buy_org, mode)
        print(
            f"BUY_UNFILLED_CANCEL rt={cancel_resp.get('rt_cd')} "
            f"msg={cancel_resp.get('msg_cd')} text={cancel_resp.get('msg1')}",
            flush=True,
        )
        return

    fill_price = float(fill["fill_price"])
    fill_qty = int(fill["fill_qty"])
    print(
        f"BUY_FILLED price={fill_price} qty={fill_qty} amount={fill_price * fill_qty}",
        flush=True,
    )

    s = state.get()
    s.trading_date = now_kst().strftime("%Y%m%d")
    s.target_ticker = TICKER
    await state.set_holding(fill_price, fill_qty, buy_id)
    state.get().target_ticker = TICKER
    state.get().trade_id = 0

    spike_filter = SpikeFilter()
    ticks = []
    rest_prices = []
    stop_at = time.monotonic() + seconds_until_1100()
    print("MONITOR_START until=11:00:00", flush=True)

    async def on_tick(tick: dict) -> None:
        live.ws_connected = True
        price_value = float(tick["price"])
        live.push_tick(price_value)
        ticks.append(price_value)
        await f4_tracking._process_tick(price_value, spike_filter)
        if len(ticks) == 1 or len(ticks) % 50 == 0:
            print(
                f"WS_TICK count={len(ticks)} price={price_value} "
                f"status={state.get().position_status} reason={state.get().close_reason} "
                f"trailing={state.get().trailing_active} step={state.get().highest_step}",
                flush=True,
            )

    ws_task = asyncio.create_task(
        kis_ws.subscribe(
            TICKER,
            on_tick,
            stop_if=lambda: state.get().position_status != "HOLDING"
            or time.monotonic() >= stop_at,
        )
    )

    while time.monotonic() < stop_at and state.get().position_status == "HOLDING":
        await asyncio.sleep(REST_POLL_SEC)
        try:
            price = await f3_entry._fetch_current_price(TICKER)
            if price:
                price_value = float(price)
                rest_prices.append(price_value)
                live.push_tick(price_value)
                await f4_tracking._process_tick(price_value, spike_filter)
                print(
                    f"REST_PRICE count={len(rest_prices)} price={price_value} "
                    f"status={state.get().position_status} reason={state.get().close_reason} "
                    f"trailing={state.get().trailing_active} step={state.get().highest_step}",
                    flush=True,
                )
        except Exception as exc:
            print(f"REST_PRICE_ERROR {exc!r}", flush=True)

    try:
        await asyncio.wait_for(ws_task, timeout=10)
    except Exception as exc:
        ws_task.cancel()
        print(f"WS_END {type(exc).__name__}: {exc}", flush=True)

    closed_by_f4 = state.get().position_status == "CLOSED"
    cleanup_id = ""
    cleanup_fill = None
    if not closed_by_f4:
        cleanup_id, cleanup_fill = await sell_and_poll(TICKER, fill_qty, mode, "ELEVEN_CLEANUP")
        if cleanup_fill:
            await state.set_closed("ELEVEN_TEST_CLEANUP")
            state.get().remaining_qty = 0

    print(
        {
            "ticker": TICKER,
            "buy_id": buy_id,
            "buy_fill_price": fill_price,
            "buy_fill_qty": fill_qty,
            "ws_tick_count": len(ticks),
            "rest_price_count": len(rest_prices),
            "first_ws_price": ticks[0] if ticks else None,
            "last_ws_price": ticks[-1] if ticks else None,
            "last_rest_price": rest_prices[-1] if rest_prices else None,
            "closed_by_f4": closed_by_f4,
            "final_status": state.get().position_status,
            "close_reason": state.get().close_reason,
            "trailing_active": state.get().trailing_active,
            "highest_step": state.get().highest_step,
            "cleanup_sell_id": cleanup_id,
            "cleanup_fill": cleanup_fill,
        },
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

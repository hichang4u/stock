"""PAPER 1-share buy/sell smoke test.

Flow:
1. Select a target through F1/F2.
2. Buy exactly 1 share with the production gap-capped limit policy.
3. Poll fill.
4. Sell exactly the filled quantity at market.

This script refuses to run outside KIS_MODE=PAPER.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_tests._helper as h
from src import db, state
from src.api import auth
from src.modules import f1_filter, f2_lockup, f3_entry
from src.utils import logger
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")


def _deadline(seconds: int = 30) -> datetime:
    """_poll_fill이 요구하는 절대 마감. (시,분,초) 튜플을 주면 비교에서 터진다."""
    return datetime.now(KST) + timedelta(seconds=seconds)


def _smoke_buy_limit_or_reason(
    ticker: str,
    expected_price: float,
    prev_close: float,
    quote: "f3_entry.EntryQuote | None",
) -> tuple[float | None, str]:
    """PAPER 스모크 매수 지정가를 산출하거나 거부 사유를 반환한다.

    잠긴 상태 후보(state.target_candidates)의 예상 체결대금으로 F3 고갭 유동성
    정책을 적용한다. 고갭(>=8%) 저대금/부재는 fail-closed로 거부하고, 지정가
    상한도 동일한 대금 기준으로 결정한다. (limit_price 또는 None, 사유) 반환.
    """
    if expected_price <= 0 or prev_close <= 0 or quote is None:
        return None, "QUOTE_UNAVAILABLE"
    if not f3_entry._quote_is_fresh(quote):
        return None, "QUOTE_STALE"
    amount = f3_entry._candidate_expected_amount(
        f3_entry._candidate_for_ticker(state.get(), ticker)
    )
    fresh_gap = quote.ask_price / prev_close - 1
    allowed, reason = f3_entry._evaluate_order_gap(fresh_gap, amount)
    if not allowed:
        return None, reason
    limit_price, _ = f3_entry._entry_limit_price(
        quote.ask_price,
        f3_entry._strict_gap_cap(prev_close, expected_amount=amount),
    )
    return limit_price, "OK"


async def run() -> bool:
    h.header("PAPER 1주 왕복 주문 스모크 테스트")

    if h.mode() != "PAPER":
        h.fail("mode guard", f"KIS_MODE={h.mode()}")
        return False

    logger.setup(os.getenv("LOG_DIR", "data/logs"))
    db_path = Path(os.getenv("DB_DIR", "data/db")) / "trading.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await db.init(str(db_path))

    if not await auth.load_or_refresh():
        h.fail("token")
        await db.close()
        return False

    today = datetime.now(KST).strftime("%Y%m%d")
    await state.ensure_trading_day(today)

    candidates = await f1_filter.run()
    h.ok("f1", f"candidates={len(candidates)}")
    if not candidates:
        h.fail("target", "F1 후보 없음")
        await db.close()
        return False

    await f2_lockup.run(candidates)
    ticker = state.get().target_ticker
    if not ticker:
        h.fail("f2", "target_ticker 없음")
        await db.close()
        return False
    h.ok("f2", f"target={ticker}")

    expected_price, prev_close = await f3_entry._fetch_expected_price(ticker)
    quote = await f3_entry._fetch_final_entry_quote(ticker)
    limit_price, gate_reason = _smoke_buy_limit_or_reason(
        ticker, expected_price, prev_close, quote
    )
    # limit_price가 확정되면 quote는 헬퍼에서 None/무효가 아님이 보장되지만,
    # 타입 검사기 관점에서 send_guard의 quote를 EntryQuote로 좁히기 위해 명시한다.
    if limit_price is None or quote is None:
        h.fail("buy limit", f"신선한 허용 갭 매도호가 확정 실패 (reason={gate_reason})")
        await db.close()
        return False
    qty = 1
    log(
        "ORDER_SMOKE_BUY_START",
        level="INFO",
        ticker=ticker,
        order_qty=qty,
        expected_price=expected_price,
        limit_price=limit_price,
    )
    buy_resp = await f3_entry._send_buy(
        ticker,
        qty,
        "PAPER",
        limit_price=limit_price,
        send_guard=lambda: f3_entry._quote_is_fresh(quote),
    )
    buy_out = buy_resp.get("output", {})
    buy_order_id = buy_out.get("ODNO", "")
    buy_org_no = buy_out.get("KRX_FWDG_ORD_ORGNO", "")
    print(
        "  buy_resp:",
        {
            "rt_cd": buy_resp.get("rt_cd"),
            "msg_cd": buy_resp.get("msg_cd"),
            "msg1": buy_resp.get("msg1"),
            "order_id": buy_order_id,
            "org_no": buy_org_no,
        },
    )

    if not buy_order_id or buy_resp.get("rt_cd") not in (None, "0"):
        h.fail("buy order", buy_resp.get("msg1", "주문 실패"))
        await db.close()
        return False

    buy_fill = await f3_entry._poll_fill(buy_order_id, _deadline())
    if not buy_fill:
        h.fail("buy fill", "30초 내 체결 없음, 취소 시도")
        if buy_org_no:
            await f3_entry._cancel_order(buy_order_id, buy_org_no, "PAPER")
        await db.close()
        return False

    h.ok("buy fill", f"{buy_fill['fill_qty']}주 @ {buy_fill['fill_price']:,}")
    log(
        "ORDER_SMOKE_BUY_FILLED",
        level="INFO",
        ticker=ticker,
        order_id=buy_order_id,
        fill_qty=buy_fill["fill_qty"],
        fill_price=buy_fill["fill_price"],
    )

    sell_qty = int(buy_fill["fill_qty"])
    await asyncio.sleep(1.2)
    log("ORDER_SMOKE_SELL_START", level="INFO", ticker=ticker, order_qty=sell_qty)
    sell_resp: dict = {}
    sell_order_id = ""
    for attempt in range(1, 6):
        if attempt > 1:
            await asyncio.sleep(1.2)
        sell_resp = await f3_entry._send_sell(ticker, sell_qty, "PAPER")
        sell_out = sell_resp.get("output", {})
        sell_order_id = sell_out.get("ODNO", "")
        print(
            "  sell_resp:",
            {
                "attempt": attempt,
                "rt_cd": sell_resp.get("rt_cd"),
                "msg_cd": sell_resp.get("msg_cd"),
                "msg1": sell_resp.get("msg1"),
                "order_id": sell_order_id,
            },
        )
        if sell_order_id and sell_resp.get("rt_cd") in (None, "0"):
            break

    if not sell_order_id or sell_resp.get("rt_cd") not in (None, "0"):
        h.fail("sell order", sell_resp.get("msg1", "주문 실패"))
        await db.close()
        return False

    sell_fill = await f3_entry._poll_fill(sell_order_id, _deadline())
    if not sell_fill:
        h.fail("sell fill", "30초 내 체결 없음")
        await db.close()
        return False

    h.ok("sell fill", f"{sell_fill['fill_qty']}주 @ {sell_fill['fill_price']:,}")
    log(
        "ORDER_SMOKE_SELL_FILLED",
        level="INFO",
        ticker=ticker,
        order_id=sell_order_id,
        fill_qty=sell_fill["fill_qty"],
        fill_price=sell_fill["fill_price"],
    )

    await db.close()
    h.ok("round trip", f"{ticker} 1주 매수/매도 완료")
    return True


if __name__ == "__main__":
    ok = asyncio.run(run())
    raise SystemExit(0 if ok else 1)

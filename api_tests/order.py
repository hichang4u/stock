"""KIS 주문 테스트 — F3 정책 지정가 매수 → 체결 확인 → 매도 → 체결 확인.

실제 주문이 발생합니다. --confirm 플래그 또는 confirm=True 인자 필수.
REAL_SMOKE_TICKER는 실행 시점 갭이 F3 허용 범위(2% 이상 10% 미만)인
종목으로 명시한다. 최종 호가가 오래됐거나 범위를 벗어나면 주문하지 않는다.
  python api_tests/order.py --confirm
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER       = os.getenv("REAL_SMOKE_TICKER", "005930")
QTY          = 1
FILL_TIMEOUT = 60         # 초

_BUY_TR  = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


async def _place_order(
    mode: str,
    side: str,
    qty: int,
    *,
    limit_price: float | None = None,
    send_guard=None,
    allow_real_smoke_buy: bool = False,
) -> dict:
    from src.api import kis_rest

    tr_id = _BUY_TR[mode] if side == "BUY" else _SELL_TR[mode]
    is_buy = side == "BUY"
    if is_buy:
        if limit_price is None or limit_price <= 0:
            raise ValueError("REAL smoke BUY requires a positive F3-capped limit price")
        ord_unpr = str(int(limit_price))
    else:
        ord_unpr = "0"
    # send_guard가 콜러블이라 bool 전용 dict로 추론되면 안 된다.
    post_kwargs: dict[str, Any] = {
        "allow_real_smoke_buy": mode == "REAL" and is_buy and allow_real_smoke_buy,
    }
    if is_buy:
        post_kwargs["send_guard"] = send_guard
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=tr_id,
        # main()의 준비도 승인은 아직 획득할 수 없는 최초 REAL 스모크
        # 매수 1건에만 --confirm 승인을 전달한다. REAL 매도에는 게이트가 없다.
        body={
            "CANO":         h.acct_no(),
            "ACNT_PRDT_CD": h.acct_cd(),
            "PDNO":         TICKER,
            "ORD_DVSN":    "00" if is_buy else "01",
            "ORD_QTY":     str(qty),
            "ORD_UNPR":    ord_unpr,
        },
        **post_kwargs,
    )


async def _prepare_limit_buy(ticker: str) -> dict:
    """Build the same fresh-quote/absolute-gap-capped limit used by F3."""
    from src.modules import f3_entry

    expected_price, prev_close = await f3_entry._fetch_expected_price(ticker)
    quote = await f3_entry._fetch_final_entry_quote(ticker)
    if expected_price <= 0 or prev_close <= 0 or quote is None:
        raise RuntimeError("expected price, previous close, or final quote unavailable")
    if not f3_entry._quote_is_fresh(quote):
        raise RuntimeError("final quote is stale")
    fresh_gap = quote.ask_price / prev_close - 1
    # 수동 REAL 스모크는 F1 후보(예상 체결대금) 컨텍스트가 없으므로 fail-closed로
    # 판정한다. 고갭(>=8%) 구간은 유동성 미검증 상태에서 제출 전에 거부한다.
    gap_allowed, gap_reason = f3_entry._evaluate_order_gap(fresh_gap, None)
    if not gap_allowed:
        raise RuntimeError(
            f"final ask gap {fresh_gap * 100:.2f}% rejected by F3 policy "
            f"(reason={gap_reason})"
        )
    gap_cap = f3_entry._strict_gap_cap(prev_close, expected_amount=None)
    limit_price, ask_cap = f3_entry._entry_limit_price(quote.ask_price, gap_cap)
    if limit_price <= 0:
        raise RuntimeError("F3-capped limit price is not positive")
    return {
        "ticker": ticker,
        "expected_price": expected_price,
        "prev_close": prev_close,
        "quote": quote,
        "fresh_gap": fresh_gap,
        "ask_cap": ask_cap,
        "gap_cap": gap_cap,
        "limit_price": limit_price,
    }


async def _poll_fill(order_id: str, mode: str, timeout: int = FILL_TIMEOUT) -> dict | None:
    from src.api import kis_rest

    today = datetime.now().strftime("%Y%m%d")
    for elapsed in range(timeout):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO":             h.acct_no(),
                    "ACNT_PRDT_CD":     h.acct_cd(),
                    "INQR_STRT_DT":     today,
                    "INQR_END_DT":      today,
                    "SLL_BUY_DVSN_CD":  "00",
                    "INQR_DVSN":        "00",
                    "PDNO":             "",
                    "CCLD_DVSN":        "00",
                    "ORD_GNO_BRNO":     "",
                    "ODNO":             order_id,
                    "INQR_DVSN_1":      "",
                    "INQR_DVSN_3":      "00",
                    "EXCG_ID_DVSN_CD":  "KRX",
                    "CTX_AREA_FK100":   "",
                    "CTX_AREA_NK100":   "",
                },
            )
            for item in resp.get("output1", []):
                if item.get("odno") == order_id:
                    qty_filled = int(item.get("tot_ccld_qty") or 0)
                    amt = float(item.get("tot_ccld_amt") or 0)
                    if qty_filled > 0:
                        return {"qty": qty_filled, "price": round(amt / qty_filled)}
        except Exception as e:
            print(f"    poll[{elapsed}s] {repr(e)}")
        await asyncio.sleep(1)
    return None


async def run(confirm: bool = False) -> bool:
    mode = h.mode()
    h.header(f"2. Order (매수+매도)  {TICKER} {QTY}주  mode={mode}")

    if not confirm:
        print("  --confirm 플래그 없음. 주문 테스트 건너뜁니다.")
        print("  실행: python api_tests/order.py --confirm")
        return True   # skip ≠ fail

    h.setup_logging()

    from src.api import auth

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    try:
        buy_plan = await _prepare_limit_buy(TICKER)
    except Exception as exc:
        h.fail(
            "BUY 지정가 준비",
            f"{exc}. REAL_SMOKE_TICKER를 현재 F1 적격 종목으로 지정하세요.",
        )
        return False

    from src.modules import f3_entry

    print(
        f"\n  [BUY 지정가] {TICKER} {QTY}주 @ {buy_plan['limit_price']:,.0f}원 "
        f"(최종호가={buy_plan['quote'].ask_price:,.0f}, "
        f"갭={buy_plan['fresh_gap'] * 100:.2f}%)"
    )
    buy_resp = await _place_order(
        mode,
        "BUY",
        QTY,
        limit_price=buy_plan["limit_price"],
        send_guard=lambda: f3_entry._quote_is_fresh(buy_plan["quote"]),
        allow_real_smoke_buy=confirm,
    )
    buy_id   = buy_resp.get("output", {}).get("ODNO", "")
    buy_org_no = buy_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
    print(f"  msg_cd={buy_resp.get('msg_cd')}  {buy_resp.get('msg1','').strip()}")
    print(f"  order_id : {buy_id}")

    if buy_resp.get("msg_cd") == f3_entry.kis_rest.SEND_GUARD_BLOCKED_MSG_CD:
        h.fail(
            "BUY 전송 차단",
            "최종 호가 신선도 또는 진입 마감 가드가 HTTP 전송을 중단했습니다.",
        )
        return False
    if not buy_id:
        h.fail("BUY 주문", "order ID 없음")
        return False
    h.ok("BUY 주문 접수", buy_id)

    print(f"  체결 대기 (최대 {FILL_TIMEOUT}s)...")
    buy_fill = await _poll_fill(buy_id, mode)
    if buy_fill:
        h.ok("BUY 체결", f"{buy_fill['qty']}주 @ {buy_fill['price']:,}원")
    else:
        if not buy_org_no:
            h.fail(
                "BUY 미체결 취소",
                "원주문 조직번호 없음. MTS에서 즉시 주문·잔고를 확인하세요.",
            )
            return False
        cancel_outcome, late_fill = await f3_entry._cancel_entry_order_confirmed(
            buy_id,
            buy_org_no,
            mode,
            TICKER,
            1,
            1,
            expected_qty=QTY,
        )
        if late_fill:
            buy_fill = {
                "qty": int(late_fill.get("fill_qty") or 0),
                "price": round(float(late_fill.get("fill_price") or 0)),
            }
            h.ok(
                "BUY 취소 경쟁 중 체결",
                f"{buy_fill['qty']}주 @ {buy_fill['price']:,}원",
            )
        else:
            h.fail(
                "BUY 체결",
                f"{FILL_TIMEOUT}s 내 미체결, cancel_outcome={cancel_outcome}",
            )
            if cancel_outcome != "CANCELLED":
                print("  [!] 취소 미확인. MTS에서 즉시 주문·잔고를 확인하세요.")
            return False

    await asyncio.sleep(1)

    # SELL
    sell_qty = int(buy_fill["qty"])
    print(f"\n  [SELL] {TICKER} {sell_qty}주 시장가")
    sell_resp = await _place_order(mode, "SELL", sell_qty)
    sell_id   = sell_resp.get("output", {}).get("ODNO", "")
    print(f"  msg_cd={sell_resp.get('msg_cd')}  {sell_resp.get('msg1','').strip()}")
    print(f"  order_id : {sell_id}")

    if not sell_id:
        h.fail("SELL 주문", "order ID 없음")
        return False
    h.ok("SELL 주문 접수", sell_id)

    print(f"  체결 대기 (최대 {FILL_TIMEOUT}s)...")
    sell_fill = await _poll_fill(sell_id, mode)
    if sell_fill:
        h.ok("SELL 체결", f"{sell_fill['qty']}주 @ {sell_fill['price']:,}원")
        pnl     = sell_fill["price"] - buy_fill["price"]
        pnl_pct = pnl / buy_fill["price"] * 100
        print(f"\n  BUY  : {buy_fill['price']:,} KRW")
        print(f"  SELL : {sell_fill['price']:,} KRW")
        print(f"  P&L  : {pnl:+,} KRW ({pnl_pct:+.2f}%)")
    else:
        h.fail("SELL 체결", f"{FILL_TIMEOUT}s 내 미체결")
        return False

    return True


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    result = asyncio.run(run(confirm=confirm))
    raise SystemExit(0 if result else 1)

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 테스트 수집 중에는 개발 머신의 .env가 os.environ을 오염시키지 않게 한다
# (conftest가 STOCK_SKIP_DOTENV=1을 설정). 운영/수동 실행에서는 정상 로드한다.
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(dotenv_path=ROOT / ".env", override=True)

from src.api import auth, kis_rest  # noqa: E402
from src.api.status_logic import parse_asset_snapshot_response  # noqa: E402
from src.modules import f4_tracking  # noqa: E402
from src.utils import logger, tree_role  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


def _as_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _money(value) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _order_row_ticker(row: dict) -> str:
    return str(row.get("pdno") or row.get("PDNO") or "").strip()


def _is_sell_order(row: dict) -> bool:
    code = str(row.get("sll_buy_dvsn_cd") or "").strip()
    name = str(row.get("sll_buy_dvsn_cd_name") or "")
    return code == "01" or "매도" in name


def _pending_sell_qty_by_ticker(order_rows: list[dict]) -> dict[str, int]:
    pending: dict[str, int] = {}
    for row in order_rows:
        ticker = _order_row_ticker(row)
        if not ticker or not _is_sell_order(row):
            continue
        remain = _as_int(row.get("rmn_qty"))
        if remain > 0:
            pending[ticker] = pending.get(ticker, 0) + remain
    return pending


def _qty_to_sell(holding: dict, pending_sells: dict[str, int], *, force_held_qty: bool) -> int:
    ticker = str(holding.get("ticker") or "")
    qty = _as_int(holding.get("qty"))
    orderable = _as_int(holding.get("orderable_qty"))
    pending = pending_sells.get(ticker, 0)
    uncovered_qty = max(qty - pending, 0)
    if orderable > 0:
        return min(orderable, uncovered_qty)
    if force_held_qty:
        return uncovered_qty
    return 0


async def _fetch_holdings(mode: str) -> list[dict]:
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=_BAL_TR[mode],
        params=kis_rest.balance_inquiry_params(),
    )
    snapshot = parse_asset_snapshot_response(resp)
    return list(snapshot.get("holdings") or [])


async def _fetch_today_orders(mode: str) -> list[dict]:
    today = datetime.now(KST).strftime("%Y%m%d")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        tr_id=_CCLD_TR[mode],
        params={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "EXCG_ID_DVSN_CD": "KRX",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    if resp.get("rt_cd") not in (None, "0", 0):
        raise RuntimeError(f"KIS order inquiry failed: {resp.get('msg_cd')} {resp.get('msg1')}")
    return list(resp.get("output1") or [])


async def _close_one(holding: dict, mode: str, qty: int) -> dict:
    ticker = holding["ticker"]
    if qty <= 0:
        return {
            "ticker": ticker, "name": holding.get("name"), "qty": 0,
            "skipped": "NO_SELLABLE_QTY",
        }

    sell_resp = await f4_tracking._send_sell(ticker, qty, mode)
    order_id = (sell_resp.get("output") or {}).get("ODNO", "")
    result = {
        "ticker": ticker,
        "name": holding.get("name"),
        "qty": qty,
        "rt_cd": sell_resp.get("rt_cd"),
        "msg_cd": sell_resp.get("msg_cd"),
        "msg1": sell_resp.get("msg1"),
        "order_id": order_id,
        "fill": None,
    }
    if sell_resp.get("rt_cd") == "0" and order_id:
        result["fill"] = await f4_tracking._poll_fill(order_id, timeout_sec=30)
    return result


def _print_holdings(
    holdings: list[dict], pending_sells: dict[str, int], planned: dict[str, int]
) -> None:
    print("\n모의투자 보유 종목", flush=True)
    print("-" * 100, flush=True)
    for h in holdings:
        ticker = str(h.get("ticker") or "")
        qty = _as_int(h.get("qty"))
        orderable = _as_int(h.get("orderable_qty"))
        pending = pending_sells.get(ticker, 0)
        sell_qty = planned.get(ticker, 0)
        status = "주문대상" if sell_qty > 0 else "대기/확인"
        print(
            f"{ticker} {h.get('name') or ''} "
            f"보유 {qty:,}주 / 매도가능 {orderable:,}주 / 미체결매도 {pending:,}주 / "
            f"재청산주문 {sell_qty:,}주 / {status} / "
            f"현재가 {_money(h.get('current_price'))}원 / "
            f"평가 {_money(h.get('evaluation_amount'))}원",
            flush=True,
        )
    print("-" * 100, flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PAPER 계좌 보유 종목 전량 청산 보조 스크립트")
    parser.add_argument(
        "--force-held-qty",
        action="store_true",
        help=(
            "매도가능수량이 0이어도 오늘 미체결 매도수량을 제외한 "
            "남은 보유수량 재청산을 시도합니다."
        ),
    )
    return parser.parse_args()


async def main() -> int:
    tree_role.require_prod(ROOT)
    args = _parse_args()
    mode = os.getenv("KIS_MODE", "PAPER")
    if mode != "PAPER":
        print(f"ABORT: KIS_MODE={mode}. 이 스크립트는 PAPER 모의투자 전용입니다.", flush=True)
        return 2
    if os.getenv("DRY_RUN", "0") == "1":
        print("ABORT: DRY_RUN=1. 실제 PAPER API 주문 경로에서만 실행하세요.", flush=True)
        return 2

    logger.setup(os.getenv("LOG_DIR", "data/logs"))
    await auth.load_or_refresh()

    holdings = await _fetch_holdings(mode)
    order_rows = await _fetch_today_orders(mode)
    pending_sells = _pending_sell_qty_by_ticker(order_rows)
    planned = {
        str(h.get("ticker") or ""): _qty_to_sell(
            h, pending_sells, force_held_qty=args.force_held_qty
        )
        for h in holdings
    }
    targets = [h for h in holdings if planned.get(str(h.get("ticker") or ""), 0) > 0]

    if not holdings:
        print("청산할 모의투자 보유 종목이 없습니다.", flush=True)
        return 0

    _print_holdings(holdings, pending_sells, planned)
    if not targets:
        print("새로 주문할 수량이 없습니다.", flush=True)
        print("미체결 매도 주문이 있거나 매도가능수량이 0입니다.", flush=True)
        print(
            "매도가능수량 0이어도 남은 보유수량 재주문을 시도하려면 "
            "--force-held-qty 옵션을 붙이세요.",
            flush=True,
        )
        return 0

    if args.force_held_qty:
        print(
            "주의: --force-held-qty 활성화. 매도가능수량 0인 종목도 "
            "미체결 매도분을 제외하고 재주문을 시도합니다.",
            flush=True,
        )
    print("위 재청산주문 수량을 시장가 매도합니다.", flush=True)
    print("주문을 보내려면 YES 를 정확히 입력하세요.", flush=True)
    confirm = input("> ").strip()
    if confirm != "YES":
        print("취소했습니다. 주문을 보내지 않았습니다.", flush=True)
        return 1

    print(f"\n청산 주문 시작: {datetime.now(KST).isoformat()}", flush=True)
    results = []
    for holding in targets:
        ticker = str(holding.get("ticker") or "")
        result = await _close_one(holding, mode, planned[ticker])
        results.append(result)
        fill = result.get("fill") or {}
        print(
            f"{result.get('ticker')} {result.get('name') or ''} "
            f"qty={result.get('qty')} rt={result.get('rt_cd')} msg={result.get('msg_cd')} "
            f"order_id={result.get('order_id') or '-'} "
            f"fill_qty={fill.get('fill_qty', '-')} fill_price={fill.get('fill_price', '-')} "
            f"text={result.get('msg1') or ''}",
            flush=True,
        )

    failed = [r for r in results if r.get("rt_cd") != "0"]
    if failed:
        print("\n일부 주문이 실패했습니다. KIS 미체결/잔고를 확인하세요.", flush=True)
        return 3

    print("\n주문 전송 완료. KIS 미체결/체결/잔고를 반드시 확인하세요.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

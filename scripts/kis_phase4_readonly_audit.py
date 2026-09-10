"""Phase 4 PAPER 계좌 읽기 전용 사고 분석 감사.

잔고와 당일 미체결 주문만 조회하고, 원문 계좌 데이터나 주문 식별자는 출력하지 않는다.
주문·정정·취소 API는 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 테스트 수집 중에는 개발 머신의 .env가 os.environ을 오염시키지 않게 한다
# (conftest가 STOCK_SKIP_DOTENV=1을 설정). 운영/수동 실행에서는 정상 로드한다.
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from src.api import auth, kis_rest  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
BALANCE_TR = "VTTC8434R"
UNFILLED_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
UNFILLED_TR = "VTTC0081R"
RATE_LIMIT_CODES = {"EGW00201", "HTTP_429"}
REQUIRED_BALANCE_SUMMARY_FIELDS = {
    "dnca_tot_amt",
    "prvs_rcdl_excc_amt",
    "scts_evlu_amt",
    "tot_evlu_amt",
    "evlu_pfls_smtl_amt",
}


class AuditStop(RuntimeError):
    def __init__(self, reason: str, msg_cd: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.msg_cd = msg_cd


def _assert_safe_window(now: datetime) -> None:
    current = now.timetz().replace(tzinfo=None)
    if time(9, 0) <= current < time(9, 11):
        raise AuditStop("FORBIDDEN_0900_0911")
    if current < time(15, 40):
        raise AuditStop("AFTER_CLOSE_ONLY")


def _assert_success(resp: dict, stage: str) -> None:
    msg_cd = str(resp.get("msg_cd") or "")
    if msg_cd in RATE_LIMIT_CODES:
        raise AuditStop("RATE_LIMIT", msg_cd)
    if str(resp.get("rt_cd", "")) != "0":
        raise AuditStop(f"{stage}_FAILED", msg_cd or None)


def _unfilled_params(today: str) -> dict:
    return {
        "CANO": kis_rest.account_no(),
        "ACNT_PRDT_CD": kis_rest.account_cd(),
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "02",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_1": "",
        "INQR_DVSN_3": "00",
        "EXCG_ID_DVSN_CD": "KRX",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }


def _normalized_id(value: object) -> str:
    return str(value or "").strip()


def _read_db_orders(db_path: Path, iso_date: str) -> tuple[set[str], set[str], int]:
    if not db_path.exists():
        return set(), set(), 0
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT kis_order_id, status
               FROM orders
               WHERE substr(ordered_at, 1, 10)=?""",
            (iso_date,),
        ).fetchall()
    all_ids = {_normalized_id(row[0]) for row in rows if _normalized_id(row[0])}
    pending_ids = {
        _normalized_id(order_id)
        for order_id, status in rows
        if status in {"PENDING", "PARTIAL_FILL"} and _normalized_id(order_id)
    }
    return all_ids, pending_ids, len(rows)


def _read_log_order_ids(log_path: Path) -> tuple[set[str], int]:
    if not log_path.exists():
        return set(), 0
    ids: set[str] = set()
    event_count = 0
    with log_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            event_count += 1
            for key in ("kis_order_id", "odno", "order_id"):
                value = _normalized_id(event.get(key))
                if value:
                    ids.add(value)
    return ids, event_count


async def run() -> dict:
    now = datetime.now(KST)
    result: dict = {
        "checked_at": now.isoformat(timespec="seconds"),
        "mode": os.getenv("KIS_MODE", "PAPER"),
        "read_only": True,
        "calls": ["balance", "unfilled_orders"],
    }
    try:
        if result["mode"] != "PAPER":
            raise AuditStop("PAPER_ONLY")
        _assert_safe_window(now)
        if not kis_rest.account_no() or not kis_rest.account_cd():
            raise AuditStop("ACCOUNT_CONFIG_MISSING")
        if not await auth.load_or_refresh():
            raise AuditStop("AUTH_FAILED")

        balance = await kis_rest.get(
            BALANCE_PATH,
            tr_id=BALANCE_TR,
            params=kis_rest.balance_inquiry_params(),
            stop_on_rate_limit=True,
        )
        _assert_success(balance, "BALANCE")
        holdings = balance.get("output1")
        summaries = balance.get("output2")
        if not isinstance(holdings, list):
            raise AuditStop("BALANCE_OUTPUT1_MISSING")
        if not isinstance(summaries, list) or not summaries or not isinstance(summaries[0], dict):
            raise AuditStop("BALANCE_OUTPUT2_MISSING")
        missing_fields = sorted(REQUIRED_BALANCE_SUMMARY_FIELDS - summaries[0].keys())
        if missing_fields:
            raise AuditStop("BALANCE_FIELDS_MISSING")
        result["balance"] = {
            "status": "SUCCESS",
            "holdings_count": sum(
                1 for item in holdings if int(float(item.get("hldg_qty") or 0)) > 0
            ),
            "required_fields_present": True,
        }

        compact_date = now.strftime("%Y%m%d")
        unfilled_resp = await kis_rest.get(
            UNFILLED_PATH,
            tr_id=UNFILLED_TR,
            params=_unfilled_params(compact_date),
            stop_on_rate_limit=True,
        )
        _assert_success(unfilled_resp, "UNFILLED")
        unfilled_rows = unfilled_resp.get("output1")
        if not isinstance(unfilled_rows, list):
            raise AuditStop("UNFILLED_OUTPUT1_MISSING")
        kis_unfilled_ids = {
            _normalized_id(row.get("odno"))
            for row in unfilled_rows
            if _normalized_id(row.get("odno"))
            and int(float(row.get("rmn_qty") or 0)) > 0
        }

        iso_date = now.strftime("%Y-%m-%d")
        db_path = ROOT / os.getenv("DB_DIR", "data/db") / "trading.db"
        log_path = ROOT / os.getenv("LOG_DIR", "data/logs") / f"{compact_date}.jsonl"
        db_ids, db_pending_ids, db_order_count = _read_db_orders(db_path, iso_date)
        log_ids, log_event_count = _read_log_order_ids(log_path)
        missing_db = kis_unfilled_ids - db_ids
        missing_log = kis_unfilled_ids - log_ids
        stale_db_pending = db_pending_ids - kis_unfilled_ids
        verdict = (
            "MATCH"
            if not missing_db and not missing_log and not stale_db_pending
            else "REVIEW_REQUIRED"
        )
        result["comparison"] = {
            "status": verdict,
            "kis_unfilled_count": len(kis_unfilled_ids),
            "db_today_order_count": db_order_count,
            "db_pending_count": len(db_pending_ids),
            "jsonl_event_count": log_event_count,
            "jsonl_order_id_count": len(log_ids),
            "kis_unfilled_missing_db_count": len(missing_db),
            "kis_unfilled_missing_jsonl_count": len(missing_log),
            "db_pending_absent_kis_count": len(stale_db_pending),
        }
        result["status"] = "SUCCESS"
    except AuditStop as exc:
        result["status"] = "STOPPED"
        result["stop_reason"] = exc.reason
        if exc.msg_cd:
            result["msg_cd"] = exc.msg_cd
    except Exception as exc:  # 계좌 원문이나 예외 메시지는 출력하지 않는다.
        result["status"] = "STOPPED"
        result["stop_reason"] = "UNEXPECTED_ERROR"
        result["error_type"] = type(exc).__name__
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 PAPER 읽기 전용 사고 분석 감사")
    parser.parse_args()
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

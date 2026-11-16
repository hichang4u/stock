"""Pure status/F1 display helpers for the API and tests."""

import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.modules.f1_filter import GAP_MAX, GAP_MIN, select_liquidity_candidates

_KST = ZoneInfo("Asia/Seoul")


def latest_today_snapshot_path(snapshot_dir: Path, today: str) -> Path | None:
    if not snapshot_dir.exists():
        return None
    files = list(snapshot_dir.glob(f"{today}_*.jsonl"))
    if not files:
        return None
    return max(files, key=lambda p: p.name)


def _required_balance_float(source: dict, field: str) -> float:
    value = source.get(field)
    if value is None or value == "":
        raise RuntimeError(f"KIS balance response missing field {field}")
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"KIS balance response invalid field {field}={value!r}") from exc


def _optional_balance_float(source: dict, field: str) -> float | None:
    value = source.get(field)
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"KIS balance response invalid field {field}={value!r}") from exc


def _asset_holding_from_item(item: dict) -> dict | None:
    qty = int(_required_balance_float(item, "hldg_qty"))
    if qty <= 0:
        return None
    ticker = str(item.get("pdno") or "").strip()
    return {
        "ticker": ticker,
        "name": str(item.get("prdt_name") or "").strip(),
        "qty": qty,
        "orderable_qty": int(_optional_balance_float(item, "ord_psbl_qty") or 0),
        "current_price": _optional_balance_float(item, "prpr"),
        "avg_price": _optional_balance_float(item, "pchs_avg_pric"),
        "purchase_amount": _optional_balance_float(item, "pchs_amt"),
        "evaluation_amount": _optional_balance_float(item, "evlu_amt"),
        "pnl_amount": _optional_balance_float(item, "evlu_pfls_amt"),
        "pnl_pct": _optional_balance_float(item, "evlu_pfls_rt"),
    }


def parse_asset_snapshot_response(resp: dict) -> dict:
    rt_cd = resp.get("rt_cd")
    if rt_cd not in (None, "0", 0):
        raise RuntimeError(
            f"KIS balance error rt_cd={rt_cd} msg_cd={resp.get('msg_cd')} msg1={resp.get('msg1')}"
        )

    output2 = resp.get("output2")
    if not isinstance(output2, list) or not output2:
        raise RuntimeError("KIS balance response missing output2")
    output1 = resp.get("output1") or []
    if not isinstance(output1, list):
        raise RuntimeError("KIS balance response output1 is not a list")
    if any(not isinstance(item, dict) for item in output1):
        raise RuntimeError("KIS balance response output1 item is not an object")

    summary = output2[0]
    if not isinstance(summary, dict):
        raise RuntimeError("KIS balance response output2[0] is not an object")

    holdings = [h for item in output1 if (h := _asset_holding_from_item(item)) is not None]
    cash = _required_balance_float(summary, "dnca_tot_amt")
    buyable = _optional_balance_float(summary, "ord_psbl_cash")
    settlement = _optional_balance_float(summary, "prvs_rcdl_excc_amt")
    # ord_psbl_cash 부재 시 예수금과 가수도정산금액 중 큰 값을 추정치로 표시한다.
    if buyable is not None:
        buyable_cash, buyable_source = buyable, "ord_psbl_cash"
    elif settlement is not None and settlement > cash:
        buyable_cash, buyable_source = settlement, "prvs_rcdl_excc_amt"
    else:
        buyable_cash, buyable_source = cash, "dnca_tot_amt"
    stock_value = _required_balance_float(summary, "scts_evlu_amt")
    total = _required_balance_float(summary, "tot_evlu_amt")
    pnl = _required_balance_float(summary, "evlu_pfls_smtl_amt")
    return {
        "cash": cash,
        "buyable_cash": buyable_cash,
        "buyable_cash_source": buyable_source,
        "stock_value": stock_value,
        "total_asset": total,
        "pnl_amount": pnl,
        "holdings_count": len(holdings),
        "holdings": holdings,
        "source": "KIS",
    }


def f3_detail_from_event(event: dict | None) -> str:
    if not event:
        return "최종 1개 매수"
    if event.get("event") == "F3_FINAL_PICK":
        return f"{event.get('valid_count')} / {event.get('checked_count')} 재검증"

    reason = event.get("reason")
    labels = {
        "BELOW_MIN": "갭 하한 미달",
        "ABOVE_MAX": "갭 상한 초과",
        "GAP_CHANGED": "진입 전 갭 변동",
        "GAP_RECHECK_UNAVAILABLE": "진입 전 갭 재검증 불가",
        "VI_ACTIVE": "변동성완화장치(VI) 발동으로 진입 보류",
        "CANCEL_UNCONFIRMED": "진입 주문 취소 확인 실패",
        "BUYABLE_QTY_ZERO": "매수가능수량 0",
        "BUYABLE_QTY_QUERY_FAILED": "매수가능수량 조회 실패",
        "INSUFFICIENT_BALANCE": "주문가능금액 부족",
        "QTY_ZERO": "주문 수량 0",
        "NO_ENTRY_CANDIDATE": "진입 가능 후보 없음",
        "STATE_NOT_IDLE": "진입 가능 상태 아님",
    }
    return labels.get(str(reason), str(reason) if reason else "최종 1개 매수")


def pipeline_from_logs(logs: list[dict], position_status: str) -> dict:
    stage = 0
    failed = False

    if position_status == "ENTERING":
        return {"pipeline_stage": 2, "pipeline_failed": False}
    if position_status == "HOLDING":
        return {"pipeline_stage": 3, "pipeline_failed": False}
    if position_status == "EXITING":
        return {"pipeline_stage": 3, "pipeline_failed": True}
    if position_status == "CLOSED":
        return {"pipeline_stage": 4, "pipeline_failed": False}

    for entry in logs:
        event = entry.get("event")
        if event in {"F1_DONE", "F1_FILTER_EMPTY", "F1_SNAPSHOT_SAVED", "NO_TARGET"}:
            stage = max(stage, 1)
        elif event in {"TARGET_LOCKED", "F2_SKIPPED"}:
            stage = max(stage, 2)
        elif event in {
            "F3_RECHECK",
            "ENTRY_ORDER_SENT",
            "ENTRY_FILL_POLL_TIMEOUT",
            "ENTRY_CANCEL_SENT",
            "ENTRY_RETRY_START",
            "ENTRY_RETRY_SKIPPED",
            "ENTRY_FAIL",
            "GAP_RECHECK_UNAVAILABLE",
            "BUYABLE_QTY_QUERY_FAILED",
            "BUYABLE_QTY_ZERO",
            "F3_SKIPPED",
            "F3_ENTRY_BLOCKED",
            "GAP_CHANGED",
        }:
            stage = max(stage, 2)
        elif event in {"ENTRY_EXECUTED", "DRY_RUN_ENTRY_EXECUTED"}:
            stage = max(stage, 3)
            failed = False

        if event in {
            "ENTRY_FAIL",
            "F3_SKIPPED",
            "F3_ENTRY_BLOCKED",
            "GAP_CHANGED",
            "GAP_RECHECK_UNAVAILABLE",
            "BUYABLE_QTY_QUERY_FAILED",
            "BUYABLE_QTY_ZERO",
        }:
            failed = True

    return {"pipeline_stage": stage, "pipeline_failed": failed}


def f1_verdict(candidate: dict) -> str:
    reason = candidate.get("gap_reason")
    labels = {
        "CORE_GAP": "통과",
        "HIGH_GAP_ALLOWED": "고갭통과",
        "GAP_BELOW_2": "갭미달",
        "GAP_BELOW_CORE": "약한갭",
        "HIGH_GAP_AMOUNT_LOW": "대금부족",
        "HIGH_GAP_REJECTED": "고갭조건미달",
        "EXTREME_GAP_RISK": "초고갭",
        "GAP_TOO_HIGH": "갭과열",
        "NEGATIVE_GAP": "음수갭",
        # 로그 복구 경로 — 선정된 것은 확실하나 갭 수치를 복구하지 못한 경우
        "GAP_UNVERIFIED": "갭확인불가",
    }
    return labels.get(str(reason), "통과" if candidate.get("gap_allowed") else "제외")


def f1_allowed(candidate: dict) -> bool:
    if "gap_allowed" in candidate:
        return candidate.get("gap_allowed") is True
    gap = float(candidate.get("gap_pct") or 0)
    return GAP_MIN <= gap < GAP_MAX


def candidate_amount(candidate: dict) -> float:
    return float(candidate.get("expected_amount") or candidate.get("avg_amount_5d") or 0)


def sort_f1_candidates_for_display(rows: list[dict], selected: list[dict]) -> list[dict]:
    selected_tickers = {c.get("ticker") for c in selected}

    def key(candidate: dict) -> tuple:
        ticker = candidate.get("ticker")
        expected_gap = float(candidate.get("expected_api_gap_pct") or 0)
        ranking_gap = float(candidate.get("ranking_gap_pct") or 0)
        expected_valid = (
            float(candidate.get("expected_api_price") or 0) > 0
            and int(candidate.get("expected_api_qty") or 0) > 0
        )
        return (
            ticker in selected_tickers,
            f1_allowed(candidate),
            GAP_MIN <= expected_gap < GAP_MAX,
            GAP_MIN <= ranking_gap < GAP_MAX,
            expected_valid,
            candidate_amount(candidate),
            float(candidate.get("gap_pct") or 0),
        )

    return sorted(rows, key=key, reverse=True)


def f1_summary_from_rows(rows: list[dict]) -> dict:
    gap_pass = [c for c in rows if f1_allowed(c)]
    selected = select_liquidity_candidates(gap_pass)
    display_rows = sort_f1_candidates_for_display(rows, selected)

    expected_valid = [
        c for c in rows
        if float(c.get("expected_api_price") or 0) > 0
        and int(c.get("expected_api_qty") or 0) > 0
    ]
    ranking_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("ranking_gap_pct") or 0) < GAP_MAX
    ]
    expected_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("expected_api_gap_pct") or 0) < GAP_MAX
    ]

    return {
        "raw_count": len(rows),
        "expected_valid": len(expected_valid),
        "ranking_pass": len(ranking_pass),
        "expected_pass": len(expected_pass),
        "gap_pass": len(gap_pass),
        "core_gap": sum(1 for c in rows if c.get("gap_band") == "CORE_GAP"),
        "high_gap_allowed": sum(1 for c in rows if c.get("gap_reason") == "HIGH_GAP_ALLOWED"),
        "high_gap_rejected": sum(
            1
            for c in rows
            if c.get("gap_band") == "HIGH_GAP" and c.get("gap_allowed") is not True
        ),
        "extreme_gap": sum(1 for c in rows if c.get("gap_band") == "EXTREME_GAP"),
        "liquidity_pass": len(selected),
        "selected": selected[0] if selected else None,
        "selected_tickers": [c.get("ticker") for c in selected if c.get("ticker")],
        "candidates": [
            {**c, "verdict": f1_verdict(c)}
            for c in display_rows[:50]
        ],
    }


def _floor_minute_kst(value: datetime) -> datetime:
    """분 단위로 내린 KST 시각. tz 없는 값은 KST로 본다(live의 기록이 KST다)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=_KST)
    return aware.astimezone(_KST).replace(second=0, microsecond=0)


def _seed_close(row: dict) -> float | None:
    close = row.get("close")
    if isinstance(close, bool) or not isinstance(close, (int, float)):
        return None
    if not math.isfinite(close) or close <= 0:
        return None
    return float(close)


def price_seed_from_bars(
    bar_rows: list,
    *,
    trade_date: str,
    since: datetime | None,
    before: datetime | None,
    now: datetime,
) -> tuple[list[dict], dict]:
    """저장된 1분 봉에서 가격흐름 차트의 앞 구간을 메울 분 종가를 고른다(순수).

    메모리 분 이력(`live._MINUTE_HISTORY_MAX`분)에서 밀려난 구간만 메운다. 고르는 기준:

    - 창: `since`를 분으로 내린 시각 이상(진입 분 포함 — `live.minute_price_history`와 같은
      규칙), `before`를 분으로 내린 시각 미만. `before`는 메모리 분 이력의 첫 분이라 그 뒤는
      메모리 분과 틱이 맡는다(겹치는 분은 메모리 우선).
    - 끝난 분만: 봉의 분이 `now`까지 끝나지 않았으면 뺀다. 진행 중인 봉의 종가는 아직
      바뀌는 값이다.
    - 값: 종가가 유한한 양수인 봉만. 날짜가 다르거나 시각·종가를 못 읽는 행은 버리고
      `invalid`로 센다. 빠진 분을 앞뒤 값으로 채우지 않는다.
    - 같은 분이 두 번 있으면 뒤 행이 이긴다. 결과는 시각 오름차순이다.

    돌려주는 행의 `ts`는 분의 **시작** 시각이고(`minute_price_history`와 같은 표기) `price`는
    그 분의 종가다. 봉의 분은 거래소 체결시각 기준이고 메모리 분은 수신 시각 기준이라
    분 경계에 걸친 체결은 서로 다른 분에 들어갈 수 있다 — 그래서 겹치는 분은 합치지 않고
    메모리 분을 쓴다.

    `missing_minutes`는 고른 구간 안과 메모리 첫 분 사이에 봉이 없는 분의 수다. 체결이 없던
    분과 수집이 끊긴 분을 구분하지 못하므로 그대로 내보낸다.
    """
    lower = _floor_minute_kst(since) if since is not None else None
    upper = _floor_minute_kst(before) if before is not None else None
    now_kst = now if now.tzinfo is not None else now.replace(tzinfo=_KST)
    try:
        day = datetime.strptime(trade_date, "%Y%m%d").replace(tzinfo=_KST)
    except ValueError:
        return [], {"count": 0, "invalid": 0, "missing_minutes": 0, "first": None, "last": None}

    by_minute: dict[int, float] = {}
    invalid = 0
    for row in bar_rows if isinstance(bar_rows, list) else []:
        if not isinstance(row, dict):
            invalid += 1
            continue
        text = str(row.get("time") or "")
        row_date = row.get("date")
        close = _seed_close(row)
        if (
            len(text) < 4
            or not text[:4].isdigit()
            or int(text[:2]) > 23
            or int(text[2:4]) > 59
            or (row_date is not None and str(row_date) != trade_date)
            or close is None
        ):
            invalid += 1
            continue
        minute = int(text[:2]) * 60 + int(text[2:4])
        start = day + timedelta(minutes=minute)
        if lower is not None and start < lower:
            continue
        if upper is not None and start >= upper:
            continue
        if start + timedelta(minutes=1) > now_kst:
            continue
        by_minute[minute] = close

    minutes = sorted(by_minute)
    rows = [
        {"ts": (day + timedelta(minutes=m)).isoformat(), "price": by_minute[m]}
        for m in minutes
    ]
    missing = sum(b - a - 1 for a, b in zip(minutes, minutes[1:]))
    if minutes and upper is not None and upper.date() == day.date():
        missing += max(0, upper.hour * 60 + upper.minute - minutes[-1] - 1)
    return rows, {
        "count": len(rows),
        "invalid": invalid,
        "missing_minutes": missing,
        "first": rows[0]["ts"] if rows else None,
        "last": rows[-1]["ts"] if rows else None,
    }

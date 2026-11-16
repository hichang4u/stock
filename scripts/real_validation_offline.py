"""실전 검증 오프라인 분석 — 완료된 백업 스냅샷만 읽어 PAPER 기준선·위험 초안·비용 민감도를 낸다.

읽기 전용이다. 운영 DB·운영 프로세스·KIS를 건드리지 않고, 장 마감 뒤 만들어진 백업
(`D:\\Private\\stock_backups\\data_<날짜>_<시각>`)만 연다. DB는 `mode=ro&immutable=1`로 열어
파일을 만들지도 고치지도 않는다. 결과는 집계와 파생 수치뿐이고 계좌·현금 값은 쓰지 않는다.

    python scripts/real_validation_offline.py \\
        --backup D:\\Private\\stock_backups\\data_20261112_154651 --cutoff 20261112 \\
        --out data/real_validation_offline/20261113-1

가정(스트레스 손실률 격자, 수수료 범위, 세율 시나리오, 자본 예시)은 아래 상수로 고정돼 있고
손익을 계산하기 **전에** manifest에 먼저 기록된다. 결과를 보고 바꾸지 않는다.

이 분석이 아닌 것:
- REAL 실측이 아니다. 전부 PAPER 서버 기록이다.
- 위험 초안의 준수 검증이 아니다. 호가 잔량이 없어 유동성 한도를 계산하지 못하고, 스트레스
  손실률·절대 상한·자본이 미정이라 `sizing.allowed_order`를 온전히 돌릴 수 없다.
- 미래 수익성 검증이 아니다. 지문·경로가 섞인 과거 탐색 구간의 재계산이다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import perf_costs  # noqa: E402
from src.modules import cost_model  # noqa: E402
from src.real_validation import costs  # noqa: E402
from src.real_validation.costs import CostBasis, CostPolicyVersion, Fill  # noqa: E402
from src.real_validation.execution_profile import latency_summary  # noqa: E402

ANALYSIS_VERSION = "real-validation-offline-1"

# ── 손익을 보기 전에 고정한 가정 ──────────────────────────────────────

# coordinator 위험 초안(검토용 제안). 설정이 아니라 분석 시나리오다.
RISK_DRAFT = {
    "per_trade_loss_budget_ratio": 0.0010,
    "daily_stop_ratio": 0.0030,
    "max_drawdown_ratio": 0.0200,
    "consecutive_net_losses": 3,
    "alloc_cap_ratio": 0.05,
}
# 스트레스 손실률은 미정이다. 아래는 민감도 격자이며 승인값이 아니다.
STRESS_LOSS_GRID = (0.03, 0.04, 0.06)
# 손절 폭만 쓴 경우. 설계가 금지한 기준선이라 비교용으로만 낸다.
STRESS_STOP_ONLY_REFERENCE = 0.02
# 연속손실 중단 뒤 '원인 검토'에 걸리는 영업일. 관측할 수 없는 값이라 격자로 둔다.
REVIEW_DAYS_GRID = (1, 5)
# 금액 예시용 자본(원). 사용자 자본이 아니다.
CAPITAL_EXAMPLES = (10_000_000, 30_000_000, 100_000_000)

# 증권사 수수료(편도)는 미정이다. 민감도 범위.
FEE_PER_SIDE_GRID = (0.0, 0.00015, 0.0005, 0.0015)
# 매도 세금(증권거래세+농어촌특별세) 시나리오. 무엇이 확인됐는지는 manifest의 세율 확인 항목을 본다.
# - T020: 매도금액의 0.20%. 코스닥은 시행령 본문으로 확인, 코스피는 농특세 0.15%가 조건부.
# - T_STATUTE_LOW: 본문으로 확인한 증권거래세만(코스피 0.05%, 코스닥 0.20%). 시장을 모르면 0.05%.
#   코스피 농특세를 뺀 **하한**이다.
# - T015: 연구 상수(왕복 0.18%p)가 깔고 있던 이전 세율. 참고용이며 확인하지 않았다.
SELL_TAX_SCENARIOS: dict[str, float | None] = {
    "T020": 0.0020,
    "T_STATUTE_LOW": None,
    "T015": 0.0015,
}
STATUTE_STT = {"KOSPI": 0.0005, "KOSDAQ": 0.0020, "UNKNOWN": 0.0005}
# 위험 경로·버전별 표에 쓰는 기준 비용 시나리오(가정).
BASE_FEE_PER_SIDE = 0.00015
BASE_TAX_SCENARIO = "T020"

_ETF_NAME = re.compile(
    r"KODEX|TIGER|KBSTAR|ARIRANG|HANARO|KOSEF|KINDEX|ACE |SOL |PLUS |RISE |TIMEFOLIO|ETN"
    r"|레버리지|인버스"
)
_ORDER_ENDPOINT = "/order-cash"
_LATENCY_LOG_FLOOR_MS = 200  # kis_rest는 network_ms가 이 값을 넘을 때만 건별 기록을 남긴다


# ── 입력 ─────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """백업 DB를 읽기 전용·불변으로 연다. -wal/-shm도 만들지 않는다."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_log(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") and row.get("ts"):
            events.append(row)
    return events


def market_map(probe_dir: Path, cutoff: str) -> dict[str, str]:
    """빠른 경로 프로브 덤프의 시장별 순위·멀티시세 응답에서 종목 → 시장을 모은다."""
    markets: dict[str, str] = {}
    names = {"J": "KOSPI", "Q": "KOSDAQ"}
    for path in sorted(probe_dir.glob("2*.jsonl")):
        if not path.stem.isdigit() or path.stem > cutoff:
            continue
        for row in read_log(path):
            market = names.get(str(row.get("market")))
            output = (row.get("response") or {}).get("output")
            if market is None or not isinstance(output, list):
                continue
            for item in output:
                ticker = item.get("mksc_shrn_iscd") or item.get("inter_shrn_iscd")
                if ticker:
                    markets[str(ticker)] = market
    return markets


def load_trades(conn: sqlite3.Connection, cutoff: str) -> list[dict[str, Any]]:
    """컷오프까지의 트랙 A 청산 거래와 그 주문. 종목명은 ETF 여부 판단에만 쓰고 내보내지 않는다."""
    trades = []
    rows = conn.execute(
        """SELECT * FROM trades WHERE track='A' AND status='CLOSED' AND date<=? ORDER BY date""",
        (cutoff,),
    ).fetchall()
    for row in rows:
        trade = dict(row)
        trade["etf_like"] = bool(_ETF_NAME.search(str(trade.pop("name") or "").upper()))
        trade["orders"] = [
            {k: v for k, v in dict(o).items() if k != "name"}
            for o in conn.execute("SELECT * FROM orders WHERE trade_id=? ORDER BY id", (row["id"],))
        ]
        trades.append(trade)
    return trades


# ── 통계 ─────────────────────────────────────────────────────────────


def _ts(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(event["ts"]))


def _ms(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() * 1000


def summarize(values: Iterable[float | None], total: int) -> dict[str, Any]:
    """표본 수·p50/p95/max와 누락. `total`은 값이 있어야 했던 건수다."""
    present = [float(v) for v in values if v is not None]
    summary = latency_summary(present)
    summary["expected"] = total
    summary["missing"] = total - len(present)
    summary["missing_rate"] = None if total == 0 else round((total - len(present)) / total, 4)
    return summary


def perf_stats(pcts: Sequence[float]) -> dict[str, Any]:
    """거래당 손익률(%) 묶음의 요약. MDD는 청산 손익을 순서대로 더한 누적(%p)의 고점 대비 낙폭이다.

    보유 중 평가손실은 들어 있지 않다 — 거래 단위 기록에는 보유 중 저점이 없다.
    """
    if not pcts:
        return {"n": 0}
    equity = peak = worst = 0.0
    run = longest = 0
    for pct in pcts:
        equity += pct
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
        run = run + 1 if pct < 0 else 0
        longest = max(longest, run)
    return {
        "n": len(pcts),
        "mean_pct": round(statistics.fmean(pcts), 4),
        "median_pct": round(statistics.median(pcts), 4),
        "sd_pct": round(statistics.stdev(pcts), 4) if len(pcts) > 1 else None,
        "win_rate": round(sum(1 for p in pcts if p > 0) / len(pcts), 4),
        "sum_pct": round(sum(pcts), 4),
        "worst_pct": round(min(pcts), 4),
        "mdd_pctp_closed_trades": round(worst, 4),
        "max_consecutive_losses": longest,
    }


# ── 1. PAPER 지연 기준선 ─────────────────────────────────────────────


def _norm_order_id(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def entry_timing(events: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """하루 로그에서 진입 주문의 구간 시각을 뽑는다. 주문이 없던 날은 None.

    시각의 뜻을 섞지 않는다. 여기 나오는 차이는 전부 **같은 로컬 시계의 로그 기록 시각**끼리다.
    - 선정: `TARGET_LOCKED`를 남긴 시각
    - 접수: `ENTRY_ORDER_SENT`를 남긴 시각 — 주문 응답을 받은 **뒤**다. 전송 시각이 아니다
    - 전송(선로에 실린 시각): 접수 − `network_ms`. 주문 호출의 `LATENCY_HIGH`가 있을 때만 안다
    - 체결 통보 수신: `FILL_NOTICE_RECEIVED`를 남긴 시각
    - 체결 확인: `ENTRY_EXECUTED`를 남긴 시각
    거래소 시각(`exchange_time`, 초 단위)과 로컬 시각의 차는 계산하지 않는다 — 시계가 다르다.
    """
    sents = [(i, e) for i, e in enumerate(events) if e["event"] == "ENTRY_ORDER_SENT"]
    if not sents:
        return None
    pipeline = next((e for e in events if e["event"] == "ENTRY_PIPELINE_TIMING"), None)
    executed = next((e for e in events if e["event"] == "ENTRY_EXECUTED"), None)
    filled_id = None if executed is None else _norm_order_id(executed.get("order_id"))

    attempts: list[dict[str, Any]] = []
    for index, sent in sents:
        ack = _ts(sent)
        before = events[:index]
        target = next((e for e in reversed(before) if e["event"] == "TARGET_LOCKED"), None)
        approved = next((e for e in reversed(before) if e["event"] == "ENTRY_PRICE_APPROVED"), None)
        order_call = None
        for candidate in reversed(before[-6:]):
            is_order = candidate["event"] == "LATENCY_HIGH" and str(
                candidate.get("api_endpoint", "")
            ).endswith(_ORDER_ENDPOINT)
            if is_order and 0 <= _ms(ack, _ts(candidate)) <= 250:
                order_call = candidate
                break
        order_id = _norm_order_id(sent.get("order_id"))
        accepted = str(sent.get("rt_cd")) == "0"
        notices = [
            e for e in events[index:]
            if e["event"] == "FILL_NOTICE_RECEIVED"
            and str(e.get("side")) == "02"
            and order_id is not None
            and _norm_order_id(e.get("order_id")) == order_id
        ]
        first_trade = next(
            (
                e for e in events[index:]
                if e["event"] == "ENTRY_FIRST_TRADE_SEEN"
                and _norm_order_id(e.get("order_id")) == order_id
            ),
            None,
        )
        # 예전 로그의 LATENCY_HIGH에는 latency_ms 하나뿐이다. 대기와 통신이 섞였는지 알 수
        # 없으므로 분리 구간에는 넣지 않고 따로 둔다.
        split = order_call is not None and order_call.get("network_ms") is not None
        network = float(order_call["network_ms"]) if order_call and split else None
        rate_wait = float(order_call.get("rate_wait_ms") or 0) if order_call and split else None
        unsplit = None
        if order_call is not None and not split and order_call.get("latency_ms") is not None:
            unsplit = float(order_call["latency_ms"])
        # 정규장 개장 예정 시각(같은 로컬 시계의 09:00:00). 그날의 실제 첫 체결 시각이 아니다.
        nominal_open = ack.replace(hour=9, minute=0, second=0, microsecond=0)
        attempt: dict[str, Any] = {
            "attempt": sent.get("entry_attempt"),
            "nominal_open_to_select_ms": (
                None if target is None else _ms(_ts(target), nominal_open)
            ),
            "nominal_open_to_ack_ms": _ms(ack, nominal_open),
            "accepted": accepted,
            "filled": accepted and order_id is not None and order_id == filled_id,
            "select_to_ack_ms": None if target is None else _ms(ack, _ts(target)),
            "send_to_ack_network_ms": network,
            "send_rate_wait_ms": rate_wait,
            "send_latency_unsplit_ms": unsplit,
            "network_censored_below_floor": order_call is None,
            "select_to_wire_ms": (
                None if target is None or network is None else _ms(ack, _ts(target)) - network
            ),
            "quote_age_at_approval_ms": None if approved is None else approved.get("quote_age_ms"),
            "quote_age_at_ack_ms": None,
            "quote_age_at_wire_ms": None,
            "best_ask_amount": None,
            "order_amount": None,
            "first_trade_wait_ms": None if first_trade is None else first_trade.get("waited_ms"),
        }
        if approved is not None and approved.get("quote_age_ms") is not None:
            at_ack = float(approved["quote_age_ms"]) + _ms(ack, _ts(approved))
            attempt["quote_age_at_ack_ms"] = at_ack
            if network is not None:
                attempt["quote_age_at_wire_ms"] = at_ack - network
        if approved is not None and approved.get("ask_price") and approved.get("ask_qty"):
            attempt["best_ask_amount"] = float(approved["ask_price"]) * float(approved["ask_qty"])
        if sent.get("order_price") and sent.get("order_qty"):
            attempt["order_amount"] = float(sent["order_price"]) * float(sent["order_qty"])
        if attempt["filled"] and executed is not None:
            attempt["ack_to_confirm_ms"] = _ms(_ts(executed), ack)
            # 초기 로그는 이 값을 재지 않고 0으로 남겼다. 0은 측정값이 아니다.
            attempt["send_start_to_confirm_ms"] = executed.get("fill_latency_ms") or None
            attempt["partial_fill"] = bool(executed.get("partial_fill"))
            attempt["order_qty"] = sent.get("order_qty")
            attempt["confirmed_fill_qty"] = executed.get("fill_qty")
            attempt["confirmed_fill_price"] = executed.get("fill_price")
        if notices:
            qty = sum(float(n["qty"]) for n in notices)
            attempt["ack_to_first_notice_ms"] = _ms(_ts(notices[0]), ack)
            attempt["ack_to_last_notice_ms"] = _ms(_ts(notices[-1]), ack)
            attempt["notice_fills"] = len(notices)
            attempt["notice_qty"] = qty
            attempt["notice_vwap"] = sum(float(n["qty"]) * float(n["price"]) for n in notices) / qty
            attempt["notice_last_price"] = float(notices[-1]["price"])
        attempts.append(attempt)

    return {
        "path": None if pipeline is None else pipeline.get("selection_source"),
        "attempts": attempts,
        "entered": executed is not None,
    }


def sell_notice_fills(events: Sequence[dict[str, Any]], order_id: object) -> dict[str, Any] | None:
    """매도 주문의 체결 통보를 모아 가중평균가와 마지막 체결가를 낸다."""
    target = _norm_order_id(order_id)
    notices = [
        e for e in events
        if e["event"] == "FILL_NOTICE_RECEIVED"
        and str(e.get("side")) == "01"
        and target is not None
        and _norm_order_id(e.get("order_id")) == target
    ]
    if not notices:
        return None
    qty = sum(float(n["qty"]) for n in notices)
    return {
        "fills": len(notices),
        "qty": qty,
        "vwap": sum(float(n["qty"]) * float(n["price"]) for n in notices) / qty,
        "last_price": float(notices[-1]["price"]),
        "span_ms": _ms(_ts(notices[-1]), _ts(notices[0])),
    }


_ENTRY_METRICS = (
    "nominal_open_to_select_ms",
    "nominal_open_to_ack_ms",
    "select_to_ack_ms",
    "select_to_wire_ms",
    "send_to_ack_network_ms",
    "send_rate_wait_ms",
    "send_latency_unsplit_ms",
    "ack_to_first_notice_ms",
    "ack_to_last_notice_ms",
    "ack_to_confirm_ms",
    "send_start_to_confirm_ms",
    "first_trade_wait_ms",
    "quote_age_at_approval_ms",
    "quote_age_at_ack_ms",
    "quote_age_at_wire_ms",
)


def latency_report(days: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """진입일 묶음을 전체·경로별·지문별로 요약한다. 체결로 이어진 주문만 구간 통계에 넣는다."""

    def block(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        filled = [a for d in subset for a in d["timing"]["attempts"] if a["filled"]]
        all_attempts = [a for d in subset for a in d["timing"]["attempts"]]
        out: dict[str, Any] = {
            "order_days": len(subset),
            "entered_days": sum(1 for d in subset if d["timing"]["entered"]),
            "attempts": len(all_attempts),
            "accepted_attempts": sum(1 for a in all_attempts if a["accepted"]),
            "filled_attempts": len(filled),
            "network_split_logged": sum(
                1 for a in filled if a["send_to_ack_network_ms"] is not None
            ),
            "network_logged_without_split": sum(
                1 for a in filled if a["send_latency_unsplit_ms"] is not None
            ),
            "network_at_or_below_log_floor": sum(
                1 for a in filled if a["network_censored_below_floor"]
            ),
            "partial_fills": sum(1 for a in filled if a.get("partial_fill")),
            "metrics": {
                name: summarize((a.get(name) for a in filled), len(filled))
                for name in _ENTRY_METRICS
            },
        }
        return out

    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_fingerprint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for day in days:
        by_path[str(day["timing"]["path"] or "UNKNOWN")].append(day)
        by_fingerprint[str(day.get("fingerprint") or "UNKNOWN")].append(day)
    return {
        "all": block(days),
        "by_path": {key: block(value) for key, value in sorted(by_path.items())},
        "by_fingerprint": {key: block(value) for key, value in sorted(by_fingerprint.items())},
    }


def sell_report(trades: Sequence[dict[str, Any]], logs: dict[str, list[dict[str, Any]]]) -> dict:
    """매도 주문: DB의 로컬 지연과 트리거 대비 체결가, 통보 가중평균가와 DB 체결가의 차."""
    rows = []
    for trade in trades:
        for order in trade["orders"]:
            if order["order_type"] != "SELL" or not order.get("fill_qty"):
                continue
            row: dict[str, Any] = {
                "date": trade["date"],
                "reason": trade["close_reason"],
                # 0은 재지 않은 기록이다(수동 청산 등).
                "send_start_to_confirm_ms": order.get("fill_latency_ms") or None,
                "adverse_slip_vs_trigger_pct": None,
                "notice_vwap_vs_db_bp": None,
                "notice_fills": None,
            }
            if order.get("trigger_price") and order.get("fill_price"):
                # 양수 = 트리거보다 낮게 팔렸다(불리). 음수 = 트리거보다 높게 팔렸다.
                row["adverse_slip_vs_trigger_pct"] = (
                    1 - float(order["fill_price"]) / float(order["trigger_price"])
                ) * 100
            notice = sell_notice_fills(logs.get(trade["date"], []), order.get("kis_order_id"))
            if notice and order.get("fill_price"):
                row["notice_fills"] = notice["fills"]
                row["notice_vwap_vs_db_bp"] = (
                    notice["vwap"] / float(order["fill_price"]) - 1
                ) * 10_000
            rows.append(row)
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_reason[str(row["reason"])].append(row)

    def block(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return {
            "orders": len(subset),
            "send_start_to_confirm_ms": summarize(
                (r["send_start_to_confirm_ms"] for r in subset), len(subset)
            ),
            "adverse_slip_vs_trigger_pct": summarize(
                (r["adverse_slip_vs_trigger_pct"] for r in subset), len(subset)
            ),
            # 예전 행은 트리거가가 주문가로 소급 기록돼 체결가와 같다. 정확히 0인 값은 측정이
            # 아닐 수 있다.
            "adverse_slip_exactly_zero": sum(
                1 for r in subset if r["adverse_slip_vs_trigger_pct"] == 0
            ),
            "orders_filled_below_trigger_by_over_0.5pct": sum(
                1 for r in subset if (r["adverse_slip_vs_trigger_pct"] or 0) > 0.5
            ),
            "orders_with_notices": sum(1 for r in subset if r["notice_fills"]),
            "multi_fill_orders": sum(1 for r in subset if (r["notice_fills"] or 0) > 1),
            "notice_vwap_vs_db_bp": summarize(
                (r["notice_vwap_vs_db_bp"] for r in subset), len(subset)
            ),
        }

    return {"all": block(rows), "by_reason": {k: block(v) for k, v in sorted(by_reason.items())}}


# ── 3. 비용 ──────────────────────────────────────────────────────────


def tax_rate(trade: dict[str, Any], scenario: str) -> float:
    fixed = SELL_TAX_SCENARIOS[scenario]
    if fixed is not None:
        return fixed
    return STATUTE_STT[str(trade.get("market") or "UNKNOWN")]


def net_pct(trade: dict[str, Any], *, fee_per_side: float, sell_tax: float) -> dict[str, Any]:
    """DB 체결가로 실제 순손익을 다시 계산한다. 수수료·세금만 뺀다(`costs.realized_pnl`)."""
    qty = int(trade["exit_qty"] or trade["entry_qty"])
    version = CostPolicyVersion(
        effective_from="20260101",
        market="KRX",
        account_condition="SENSITIVITY",
        basis=CostBasis.SCENARIO_ASSUMPTION,
        buy_fee_rate=fee_per_side,
        sell_fee_rate=fee_per_side,
        sell_tax_rate=sell_tax,
    )
    return costs.realized_pnl(
        [Fill(qty, float(trade["entry_price"]))], [Fill(qty, float(trade["exit_price"]))], version
    )


def cost_report(trades: Sequence[dict[str, Any]], paths: dict[str, str | None]) -> dict[str, Any]:
    gross = [float(net_pct(t, fee_per_side=0.0, sell_tax=0.0)["gross_pct"]) for t in trades]
    grid = {}
    for tax_name, tax in SELL_TAX_SCENARIOS.items():
        for fee in FEE_PER_SIDE_GRID:
            pcts = [
                float(net_pct(t, fee_per_side=fee, sell_tax=tax_rate(t, tax_name))["net_pct"])
                for t in trades
            ]
            grid[f"{tax_name}_fee{fee * 100:.3f}pct_per_side"] = {
                "sell_tax": "by market" if tax is None else tax,
                "fee_per_side": fee,
                "mean_cost_pctp": round(statistics.fmean(g - p for g, p in zip(gross, pcts)), 4),
                **perf_stats(pcts),
            }
    research = [g - cost_model.BASE_ROUND_TRIP_COST_PCT for g in gross]
    # 기존 conservative 수치: 실제 체결 손익에서 사유별 슬리피지를 **한 번 더** 뺀 스트레스 값.
    conservative = [
        cost_model.pnl_breakdown(
            float(t["entry_price"]), float(t["exit_price"]), str(t["close_reason"])
        )["conservative_net_pct"]
        for t in trades
    ]
    base = [
        float(
            net_pct(
                t, fee_per_side=BASE_FEE_PER_SIDE, sell_tax=tax_rate(t, BASE_TAX_SCENARIO)
            )["net_pct"]
        )
        for t in trades
    ]

    def grouped(key_of: Any) -> dict[str, Any]:
        groups: dict[str, list[int]] = defaultdict(list)
        for index, trade in enumerate(trades):
            groups[str(key_of(trade))].append(index)
        return {
            key: {
                "first": trades[idx[0]]["date"],
                "last": trades[idx[-1]]["date"],
                "gross": perf_stats([gross[i] for i in idx]),
                "base_net": perf_stats([base[i] for i in idx]),
            }
            for key, idx in sorted(groups.items(), key=lambda kv: trades[kv[1][0]]["date"])
        }

    def segment_of(trade: dict[str, Any]) -> str:
        for segment in perf_costs.TRACK_A_SEGMENTS:
            if segment["start"] <= trade["date"] and (
                segment["end"] is None or trade["date"] <= segment["end"]
            ):
                return str(segment["name"])
        return "UNKNOWN"

    return {
        "trades": len(trades),
        "period": [trades[0]["date"], trades[-1]["date"]] if trades else None,
        "gross": perf_stats(gross),
        "grid": grid,
        "research_constant_round_trip_0.18": perf_stats(research),
        "legacy_conservative_extra_slippage": {
            "note": "실제 체결 손익에 사유별 슬리피지를 한 번 더 뺀 추가 불리 조정이다. "
            "실제 손익이 아니다",
            **perf_stats(conservative),
        },
        "base_assumption": {"fee_per_side": BASE_FEE_PER_SIDE, "sell_tax": BASE_TAX_SCENARIO},
        "base_net": perf_stats(base),
        "by_segment": grouped(segment_of),
        "by_fingerprint": grouped(lambda t: t.get("strategy_fingerprint") or "NONE"),
        "by_path": grouped(lambda t: paths.get(t["date"]) or "UNKNOWN"),
        "by_execution_mode": grouped(lambda t: t.get("execution_mode") or "UNLABELLED"),
        "by_market": grouped(lambda t: t.get("market") or "UNKNOWN"),
        "etf_like_trades": sum(1 for t in trades if t["etf_like"]),
        "db_pnl_pct_mismatch": sum(
            1 for t, g in zip(trades, gross) if abs(float(t["pnl_pct"]) - g) > 0.011
        ),
    }


# ── 2. 위험 초안 ─────────────────────────────────────────────────────


def position_fraction(stress: float) -> tuple[float, str]:
    """초안의 두 확정 필드로 정해지는 자본 대비 주문 비율과 결정 한도.

    현금·절대 상한·유동성 한도는 자료가 없어 넣지 못했다. 실제 허용 금액은 이보다 클 수 없다.
    """
    by_loss = RISK_DRAFT["per_trade_loss_budget_ratio"] / stress
    cap = RISK_DRAFT["alloc_cap_ratio"]
    return (by_loss, "loss_budget") if by_loss <= cap else (cap, "alloc_cap")


def _business_days_after(day: str, count: int) -> str:
    current: date = datetime.strptime(day, "%Y%m%d").date()
    while count > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count -= 1
    return current.strftime("%Y%m%d")


def risk_path(
    trades: Sequence[tuple[str, float]], *, stress: float, review_days: int | None
) -> dict[str, Any]:
    """거래(날짜, 순손익률 %)에 초안을 적용한 계좌 기준 경로.

    `review_days=None`이면 중단 없이 전부 거래한 정적 경로다. 값이 있으면 중단 정책 경로다:
    순손실 3연속이면 그만큼의 영업일 동안 진입을 건너뛰고, 고점 대비 낙폭이 한도에 닿으면
    거기서 끝낸다(재개 결정은 관측할 수 없다). **건너뛴 뒤의 시장 경로·후보 선택은 실제와
    달랐을 것이고 그 차이는 이 자료로 알 수 없다.**
    """
    fraction, binding = position_fraction(stress)
    budget = RISK_DRAFT["per_trade_loss_budget_ratio"]
    equity = peak = worst_dd = 0.0
    run = longest = 0
    taken = skipped = over_budget = daily_stop_days = halts = 0
    worst_trade = 0.0
    blocked_until: str | None = None
    drawdown_halt_on: str | None = None
    for day, pct in trades:
        if drawdown_halt_on is not None or (blocked_until is not None and day <= blocked_until):
            skipped += 1
            continue
        result = fraction * pct / 100
        taken += 1
        equity += result
        peak = max(peak, equity)
        worst_dd = max(worst_dd, peak - equity)
        worst_trade = min(worst_trade, result)
        if -result > budget + 1e-12:
            over_budget += 1
        if -result >= RISK_DRAFT["daily_stop_ratio"]:
            daily_stop_days += 1
        run = run + 1 if result < 0 else 0
        longest = max(longest, run)
        if review_days is None:
            continue
        if peak - equity >= RISK_DRAFT["max_drawdown_ratio"]:
            drawdown_halt_on = day
            halts += 1
        elif run >= RISK_DRAFT["consecutive_net_losses"]:
            blocked_until = _business_days_after(day, review_days)
            halts += 1
            run = 0
    return {
        "stress_loss_ratio": stress,
        "position_fraction_of_capital": round(fraction, 6),
        "binding": binding,
        "policy_applied": review_days is not None,
        "review_business_days": review_days,
        "trades_taken": taken,
        "trades_skipped_after_halt": skipped,
        "halts": halts,
        "drawdown_halt_on": drawdown_halt_on,
        "account_return_pct": round(equity * 100, 4),
        "account_mdd_pct_closed_trades": round(worst_dd * 100, 4),
        "worst_trade_account_pct": round(worst_trade * 100, 4),
        "worst_trade_vs_budget_multiple": round(-worst_trade / budget, 2),
        "trades_over_loss_budget": over_budget,
        "days_at_or_over_daily_stop": daily_stop_days,
        "max_consecutive_net_losses_taken": longest,
        "mdd_limit_breached": worst_dd >= RISK_DRAFT["max_drawdown_ratio"],
    }


def risk_report(
    trades: Sequence[dict[str, Any]], timing_days: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    series = [
        (
            t["date"],
            float(
                net_pct(
                    t, fee_per_side=BASE_FEE_PER_SIDE, sell_tax=tax_rate(t, BASE_TAX_SCENARIO)
                )["net_pct"]
            ),
        )
        for t in trades
    ]
    stresses = (STRESS_STOP_ONLY_REFERENCE, *STRESS_LOSS_GRID)
    static = [risk_path(series, stress=s, review_days=None) for s in stresses]
    policy = [
        risk_path(series, stress=s, review_days=days)
        for s in stresses
        for days in REVIEW_DAYS_GRID
    ]
    losses = sorted(-pct for _, pct in series if pct < 0)
    prices = [float(t["entry_price"]) for t in trades]
    best_ask = [
        a["best_ask_amount"]
        for d in timing_days
        for a in d["timing"]["attempts"]
        if a["filled"] and a["best_ask_amount"]
    ]
    examples = []
    for capital in CAPITAL_EXAMPLES:
        for stress in STRESS_LOSS_GRID:
            fraction, binding = position_fraction(stress)
            order_amount = capital * fraction
            examples.append(
                {
                    "example_capital_krw": capital,
                    "stress_loss_ratio": stress,
                    "binding": binding,
                    "loss_budget_krw": capital * RISK_DRAFT["per_trade_loss_budget_ratio"],
                    "daily_stop_krw": capital * RISK_DRAFT["daily_stop_ratio"],
                    "max_order_amount_krw": round(order_amount),
                    "past_entries_priced_above_order_amount": sum(
                        1 for p in prices if p > order_amount
                    ),
                    "past_entries_with_best_ask_observed": len(best_ask),
                    "of_those_order_exceeds_best_ask_amount": sum(
                        1 for amount in best_ask if order_amount > amount
                    ),
                }
            )
    return {
        "inputs": {
            "draft": RISK_DRAFT,
            "net_pct_assumption": {
                "fee_per_side": BASE_FEE_PER_SIDE,
                "sell_tax": BASE_TAX_SCENARIO,
            },
            "trades": len(series),
        },
        "full_allowed_order_possible": False,
        "missing_for_allowed_order": [
            "approved capital (absolute)",
            "stress loss ratio components",
            "absolute order cap",
            "order book depth within the limit price (only the best ask level is logged)",
            "participation ratio and expected traded amount per order",
            "account data as-of per order",
        ],
        "loss_size_percentiles_pct": {
            "n_losing_trades": len(losses),
            "p50": _rank(losses, 0.50),
            "p90": _rank(losses, 0.90),
            "p95": _rank(losses, 0.95),
            "max": losses[-1] if losses else None,
            "share_beyond_stress": {
                str(s): round(sum(1 for x in losses if x > s * 100) / len(series), 4)
                for s in stresses
            },
        },
        "static_all_trades": static,
        "halt_policy_paths": policy,
        "best_ask_amount_krw": summarize(best_ask, len(best_ask)),
        "amount_examples": examples,
        "caveats": [
            "정규화 결과는 자본 대비 비율이다. 금액 예시의 자본은 사용자 자본이 아니다",
            "과거 가격 경로에 주문 비율만 곱한 값이다. 주문 크기가 달라졌을 때의 체결·시장충격은 "
            "반영되지 않았다",
            "호가 잔량은 최우선 한 단계만 기록돼 있다. 전량 체결과 실제 한도 준수를 "
            "인정하지 않는다",
            "중단 정책 경로는 중단 뒤 거래를 뺀 것이다. 그 뒤의 시장 경로와 후보 선택은 관측되지 "
            "않았고, 정적 경로와의 차이는 생존·선택 편향을 포함한다",
            "주문 비율이 작아져 계좌 변동이 줄어드는 것은 수익성이 좋아진 것이 아니다. 거래당 "
            "손익률은 같다",
            "MDD는 청산 손익 기준이다. 보유 중 평가손실은 들어 있지 않다",
        ],
    }


def _rank(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    return round(values[max(1, math.ceil(q * len(values))) - 1], 4)


# ── 실행 ─────────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=False, timeout=30
    )
    return done.stdout.strip()


def build_manifest(backup: Path, cutoff: str, log_files: Sequence[Path], tax: dict) -> dict:
    db_path = backup / "db" / "trading.db"
    return {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_commit": _git("rev-parse", "HEAD"),
        "code_dirty_files": [line for line in _git("status", "--porcelain").splitlines() if line],
        "backup": str(backup),
        "cutoff_inclusive": cutoff,
        "cutoff_reason": "장 마감 뒤 온라인 백업으로 만든 완료 스냅샷만 쓴다. "
        "진행 중인 거래일은 제외한다",
        "db": {
            "path": str(db_path),
            "bytes": db_path.stat().st_size,
            "mtime": datetime.fromtimestamp(db_path.stat().st_mtime).isoformat(timespec="seconds"),
            "sha256": sha256_file(db_path),
            "opened": "mode=ro&immutable=1",
        },
        "logs": {
            "count": len(log_files),
            "first": log_files[0].name if log_files else None,
            "last": log_files[-1].name if log_files else None,
            "bytes": sum(p.stat().st_size for p in log_files),
            "sha256_of_file_hashes": hashlib.sha256(
                "".join(f"{p.name}:{sha256_file(p)}\n" for p in log_files).encode()
            ).hexdigest(),
        },
        "filters": {
            "trades": "track='A' AND status='CLOSED' AND date<=cutoff",
            "latency": "ENTRY_ORDER_SENT가 있는 날. 구간 통계는 체결로 이어진 주문만",
            "excluded": "트랙 B, 미청산, 컷오프 이후, watchdog 로그",
        },
        "assumptions_fixed_before_results": {
            "risk_draft": RISK_DRAFT,
            "stress_loss_grid": STRESS_LOSS_GRID,
            "stress_stop_only_reference": STRESS_STOP_ONLY_REFERENCE,
            "review_days_grid": REVIEW_DAYS_GRID,
            "capital_examples_krw": CAPITAL_EXAMPLES,
            "fee_per_side_grid": FEE_PER_SIDE_GRID,
            "sell_tax_scenarios": SELL_TAX_SCENARIOS,
            "statute_stt_by_market": STATUTE_STT,
            "base_cost_assumption": {
                "fee_per_side": BASE_FEE_PER_SIDE,
                "sell_tax": BASE_TAX_SCENARIO,
            },
            "research_round_trip_cost_pct": cost_model.BASE_ROUND_TRIP_COST_PCT,
        },
        "tax_rate_verification": tax,
        "not_done": [
            "REAL 서버 실측",
            "증권사 정산 대사",
            "잠금 미래 수익성 검증",
            "실제 판단부를 건 PAPER·REAL 재생",
        ],
    }


def run(backup: Path, cutoff: str, out: Path, tax: dict[str, Any]) -> dict[str, Any]:
    log_files = sorted(
        p for p in (backup / "logs").glob("2*.jsonl") if p.stem.isdigit() and p.stem <= cutoff
    )
    out.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(backup, cutoff, log_files, tax)
    # 가정을 먼저 적어 둔다. 결과는 그 뒤에 계산한다.
    _write(out / "manifest.json", manifest)

    conn = open_readonly(backup / "db" / "trading.db")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        trades = load_trades(conn, cutoff)
    finally:
        conn.close()
    markets = market_map(backup / "paper_fast_probe", cutoff)
    for trade in trades:
        trade["market"] = markets.get(str(trade["ticker"]))
    fingerprints = {t["date"]: t.get("strategy_fingerprint") for t in trades}

    logs: dict[str, list[dict[str, Any]]] = {}
    timing_days: list[dict[str, Any]] = []
    for path in log_files:
        events = read_log(path)
        logs[path.stem] = [e for e in events if e["event"] == "FILL_NOTICE_RECEIVED"]
        timing = entry_timing(events)
        if timing is not None:
            timing_days.append(
                {"date": path.stem, "fingerprint": fingerprints.get(path.stem), "timing": timing}
            )
    paths = {d["date"]: d["timing"]["path"] for d in timing_days}
    trade_dates = {t["date"] for t in trades}
    timing_dates = {d["date"] for d in timing_days}

    results = {
        "integrity_check": integrity,
        "coverage": {
            "closed_trades": len(trades),
            "trade_dates_without_order_log": sorted(trade_dates - timing_dates),
            "order_log_dates_without_closed_trade": sorted(timing_dates - trade_dates),
            "log_days": len(log_files),
            "order_days_in_logs": len(timing_days),
        },
        "latency_entry": latency_report(timing_days),
        "latency_sell": sell_report(trades, logs),
        "cost": cost_report(trades, paths),
        "risk": risk_report(trades, timing_days),
    }
    _write(out / "results.json", results)
    with (out / "entry_timing_days.jsonl").open("wb") as handle:
        for day in timing_days:
            handle.write((json.dumps(day, ensure_ascii=False) + "\n").encode("utf-8"))
    return results


def _write(path: Path, value: Any) -> None:
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--cutoff", required=True, help="YYYYMMDD, 포함")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--tax-note", type=Path, help="세율 확인 기록(JSON). 없으면 '미확인'으로 남긴다"
    )
    args = parser.parse_args(argv)
    tax: dict[str, Any] = {"status": "UNVERIFIED", "note": "세율 시나리오는 조건부다"}
    if args.tax_note:
        tax = json.loads(args.tax_note.read_bytes().decode("utf-8"))
    results = run(args.backup, args.cutoff, args.out, tax)
    print(json.dumps(results["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

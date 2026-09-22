"""PAPER-only observation probe for the proposed pre-open fast path.

The probe is deliberately read-only: it records public market-data responses
and never mutates trading state, selects the live target, or submits orders.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.api import kis_rest
from src.modules import f1_selector
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

RANKING_PATH = "/uapi/domestic-stock/v1/ranking/fluctuation"
RANKING_TR_ID = "FHPST01710000"
MULTI_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/intstock-multprice"
MULTI_PRICE_TR_ID = "FHKST11300006"

PREOPEN_MARKETS = (
    {"label": "J", "ranking_input": "0001"},
    {"label": "Q", "ranking_input": "1001"},
)

_prepared_tickers: list[str] = []
# prepare() 가 도는 동안 True. OPEN 프로브가 "PREOPEN 이 아직 안 끝났다"와 "PREOPEN 이
# 안 돌았다"를 구분하는 데 쓴다 — 전자는 기다리고 후자는 바로 포기한다.
_prepare_in_progress: bool = False
_prepared_candidates: list[dict] = []
_open_candidates: list[dict] = []
_last_open_quality: dict[str, Any] = {"ok": False, "reason": "NOT_RUN"}
_last_closed_verdict: dict[str, Any] = {"closed": False, "reason": "NOT_RUN"}

# 휴장 판정 기준. CTCA0903R(국내휴장일조회)이 모의투자 미지원이라 PAPER 는 평일
# 가드밖에 없고, 2026-08-17(광복절 대체공휴일)에 F1 이 60종목을 조회하고 주문까지
# 전송했다. 장전 멀티시세 응답에 이미 신호가 있어 추가 호출 없이 막을 수 있다.
#
# 관측된 분리 (2026-07-28~08-20, 개장 14일 / 휴장 1일):
#   hour_cls_code   개장 'B'          / 휴장 '0' (전 종목)
#   intr_antc_vol   개장 0~1종목만 0  / 휴장 전 종목 0
#   acml_vol 중앙값 개장 246~1,995    / 휴장 3,859,277 (전일 거래량이 그대로)
#
# 휴장 표본이 1일뿐이므로 세 조건을 모두 만족할 때만 휴장으로 본다. 기존 휴장
# 판정과 같은 fail-open 원칙이다 — 거래일을 놓치는 쪽이 더 큰 손실이다.
MARKET_CLOSED_HOUR_CLS = "0"
MARKET_CLOSED_MIN_ROWS = 10
MARKET_CLOSED_MIN_STALE_VOLUME = 100_000


def enabled() -> bool:
    """Return True only for an explicitly enabled PAPER session."""
    return (
        os.getenv("KIS_MODE", "PAPER").upper() == "PAPER"
        and os.getenv("PAPER_FAST_PROBE", "0") == "1"
        and os.getenv("DRY_RUN", "0") != "1"
    )


def shadow_enabled() -> bool:
    """Shadow comparison is PAPER-only and never changes trading state."""
    return enabled() and os.getenv("PAPER_FAST_SHADOW", "1") == "1"


def hybrid_enabled() -> bool:
    """Live PAPER fast path requires an explicit opt-in; REAL is always excluded."""
    return enabled() and os.getenv("PAPER_FAST_HYBRID", "0") == "1"


def _shadow_top_n() -> int:
    try:
        return max(1, min(30, int(os.getenv("PAPER_FAST_SHADOW_TOP_N", "30"))))
    except ValueError:
        return 30


def _shadow_required_days() -> int:
    try:
        return max(1, int(os.getenv("PAPER_FAST_SHADOW_REQUIRED_DAYS", "10")))
    except ValueError:
        return 10


def _shadow_scan_file_limit() -> int:
    try:
        return max(1, int(os.getenv("PAPER_FAST_SHADOW_SCAN_FILE_LIMIT", "32")))
    except ValueError:
        return 32


def _probe_dir() -> Path:
    return Path(os.getenv("PAPER_FAST_PROBE_DIR", "data/paper_fast_probe"))


def _probe_path(now: datetime | None = None) -> Path:
    current = now or datetime.now(KST)
    return _probe_dir() / f"{current.strftime('%Y%m%d')}.jsonl"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return int(_to_float(value))


def _rows(resp: dict, key: str = "output") -> list[dict]:
    value = resp.get(key) or []
    if isinstance(value, dict):
        return [value]
    return [row for row in value if isinstance(row, dict)]


def _public_response(resp: dict, key: str = "output") -> dict:
    """Keep only public response data; never persist headers or credentials."""
    rows = _rows(resp, key)
    return {
        "rt_cd": resp.get("rt_cd"),
        "msg_cd": resp.get("msg_cd"),
        "msg1": resp.get("msg1"),
        "output_count": len(rows),
        "output": rows,
    }


def _append_record(event: str, **fields: Any) -> None:
    path = _probe_path()
    record = {
        "ts": datetime.now(KST).isoformat(),
        "event": event,
        "mode": os.getenv("KIS_MODE", "PAPER"),
        **fields,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase=fields.get("phase"),
            reason="WRITE_FAILED",
            error=str(exc)[:300],
        )


def _ranking_params(ranking_input: str) -> dict:
    return {
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20171",
        "fid_input_iscd": ranking_input,
        "fid_rank_sort_cls_code": "0",
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": f"{f1_selector.GAP_MIN * 100:.1f}",
        "fid_rsfl_rate2": f"{f1_selector.GAP_HARD_MAX * 100:.1f}",
    }


def _valid_tickers(rows: list[dict]) -> list[str]:
    tickers: list[str] = []
    for row in rows:
        ticker = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "")
        if len(ticker) == 6 and ticker.isdigit() and ticker not in tickers:
            tickers.append(ticker)
        if len(tickers) >= 30:
            break
    return tickers


def _multi_params(tickers: list[str]) -> dict:
    params: dict[str, str] = {}
    for index, ticker in enumerate(tickers[:30], start=1):
        params[f"FID_COND_MRKT_DIV_CODE_{index}"] = "J"
        params[f"FID_INPUT_ISCD_{index}"] = ticker
    return params


def _estimate_timing_before_call() -> tuple[float, int]:
    started = time.monotonic()
    last_call = float(getattr(kis_rest, "_last_call_at", 0.0) or 0.0)
    interval = float(getattr(kis_rest, "_RATE_INTERVAL", 0.0) or 0.0)
    estimated_wait_ms = max(0, round((interval - (started - last_call)) * 1000))
    return started, estimated_wait_ms


async def _get_and_record(
    *,
    event: str,
    phase: str,
    market: str,
    path: str,
    tr_id: str,
    params: dict,
) -> dict:
    started, estimated_wait_ms = _estimate_timing_before_call()
    try:
        resp = await kis_rest.get(path, tr_id=tr_id, params=params)
    except Exception as exc:
        total_ms = max(0, round((time.monotonic() - started) * 1000))
        _append_record(
            event,
            phase=phase,
            market=market,
            path=path,
            tr_id=tr_id,
            request=params,
            timing={
                "total_ms": total_ms,
                "estimated_rate_wait_ms": estimated_wait_ms,
                "estimated_non_rate_ms": max(0, total_ms - estimated_wait_ms),
            },
            error={"type": type(exc).__name__, "message": str(exc)[:300]},
        )
        log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase=phase,
            market=market,
            path=path,
            reason=type(exc).__name__,
        )
        return {}

    total_ms = max(0, round((time.monotonic() - started) * 1000))
    response = _public_response(resp)
    _append_record(
        event,
        phase=phase,
        market=market,
        path=path,
        tr_id=tr_id,
        request=params,
        timing={
            "total_ms": total_ms,
            "estimated_rate_wait_ms": estimated_wait_ms,
            "estimated_non_rate_ms": max(0, total_ms - estimated_wait_ms),
        },
        response=response,
    )
    log(
        event,
        level="INFO" if str(resp.get("rt_cd", "")) == "0" else "WARN",
        phase=phase,
        market=market,
        output_count=response["output_count"],
        total_ms=total_ms,
        estimated_rate_wait_ms=estimated_wait_ms,
        rt_cd=resp.get("rt_cd"),
        msg_cd=resp.get("msg_cd"),
    )
    return resp


def _candidate_from_multi(
    row: dict,
    market: str,
    ranking_by_ticker: dict[str, dict],
) -> dict | None:
    ticker = str(row.get("inter_shrn_iscd") or "")
    if len(ticker) != 6 or not ticker.isdigit():
        return None

    ranking = ranking_by_ticker.get(ticker, {})
    name = str(row.get("inter_kor_isnm") or ranking.get("hts_kor_isnm") or "")
    if f1_selector.is_excluded_product(name):
        return None

    current_price = _to_float(row.get("inter2_prpr"))
    prev_close = _to_float(row.get("inter2_prdy_clpr") or row.get("inter2_sdpr"))
    expected_qty = _to_int(row.get("intr_antc_vol"))
    expected_gap_pct = (
        _to_float(row.get("intr_antc_cntg_prdy_ctrt"))
        if expected_qty > 0
        else _to_float(row.get("prdy_ctrt"))
    )
    expected_diff = _to_float(row.get("intr_antc_cntg_vrss"))
    derived_expected = (
        prev_close + expected_diff
        if prev_close > 0 and expected_diff
        else prev_close * (1 + expected_gap_pct / 100)
    )
    price = derived_expected if expected_qty > 0 and derived_expected > 0 else current_price
    gap_pct = expected_gap_pct
    qty = expected_qty if expected_qty > 0 else _to_int(row.get("acml_vol"))
    expected_amount = (
        price * qty
        if expected_qty > 0
        else _to_float(row.get("acml_tr_pbmn"))
    )
    ranking_price = _to_float(ranking.get("stck_prpr")) or price
    avg_amount_5d = _to_float(ranking.get("avrg_vol")) * ranking_price
    vi_gap = (
        ((prev_close * 1.10) - price) / price
        if price > 0 and prev_close > 0
        else None
    )
    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "expected_price": price,
        "prev_close": prev_close,
        "gap_pct": gap_pct / 100,
        "gap_source": (
            "multi.intr_antc_cntg_prdy_ctrt"
            if expected_qty > 0
            else "multi.prdy_ctrt"
        ),
        "avg_amount_5d": avg_amount_5d,
        "expected_amount": expected_amount,
        "expected_qty": qty,
        "vi_gap": vi_gap,
        "buy_sell_ratio": 0.0,
        "current_price": current_price,
        "ask_price": _to_float(row.get("inter2_askp")),
        "quote_hour_code": str(row.get("hour_cls_code") or ""),
    }


def _select_probe_tickers(
    ranking_rows: dict[str, list[dict]],
    multi_rows: dict[str, list[dict]],
    limit: int = 3,
) -> list[str]:
    candidates: list[dict] = []
    fallback: list[dict] = []
    for market in ("J", "Q"):
        ranking_by_ticker = {
            str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or ""): row
            for row in ranking_rows.get(market, [])
        }
        for row in multi_rows.get(market, []):
            candidate = _candidate_from_multi(row, market, ranking_by_ticker)
            if candidate is None:
                continue
            fallback.append(candidate)
            if candidate["expected_price"] > 0 and candidate["prev_close"] > 0:
                candidates.append(candidate)

    ranked = f1_selector.rank_candidates(candidates)
    selected = [str(candidate["ticker"]) for candidate in ranked[:limit]]
    if len(selected) >= limit:
        return selected

    fallback.sort(
        key=lambda item: (
            item.get("expected_amount", 0.0),
            item.get("gap_pct", 0.0),
        ),
        reverse=True,
    )
    for candidate in fallback:
        ticker = str(candidate["ticker"])
        if ticker not in selected:
            selected.append(ticker)
        if len(selected) >= limit:
            break
    return selected


async def prepare() -> list[str]:
    """Collect the 08:59 ranking and multi-price evidence."""
    global _prepared_tickers, _prepared_candidates, _last_closed_verdict
    if not enabled():
        return []

    _prepared_tickers = []
    _prepared_candidates = []
    _last_closed_verdict = {"closed": False, "reason": "NOT_RUN"}
    now = datetime.now(KST)
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= market_open:
        _append_record(
            "PAPER_FAST_PROBE_PREOPEN_SKIPPED",
            phase="PREOPEN",
            scheduled_before=market_open.isoformat(),
            actual_ts=now.isoformat(),
            reason="MARKET_OPEN",
        )
        log(
            "PAPER_FAST_PROBE_PREOPEN_SKIPPED",
            level="WARN",
            actual_ts=now.isoformat(),
            reason="MARKET_OPEN",
        )
        return []

    global _prepare_in_progress
    _prepare_in_progress = True
    try:
        return await _prepare_locked()
    finally:
        _prepare_in_progress = False


async def _prepare_locked() -> list[str]:
    global _prepared_tickers, _prepared_candidates, _last_closed_verdict
    started = time.monotonic()
    _append_record("PAPER_FAST_PROBE_PREOPEN_START", phase="PREOPEN")
    log("PAPER_FAST_PROBE_PREOPEN_START", level="INFO")

    ranking_rows: dict[str, list[dict]] = {}
    multi_rows: dict[str, list[dict]] = {}
    market_summaries: list[dict] = []

    for market_cfg in PREOPEN_MARKETS:
        market = market_cfg["label"]
        ranking_params = _ranking_params(market_cfg["ranking_input"])
        ranking_resp = await _get_and_record(
            event="PAPER_FAST_PROBE_RANKING",
            phase="PREOPEN",
            market=market,
            path=RANKING_PATH,
            tr_id=RANKING_TR_ID,
            params=ranking_params,
        )
        rows = _rows(ranking_resp)
        ranking_rows[market] = rows
        requested_tickers = _valid_tickers(rows)

        multi_resp: dict = {}
        if requested_tickers:
            multi_resp = await _get_and_record(
                event="PAPER_FAST_PROBE_MULTI",
                phase="PREOPEN",
                market=market,
                path=MULTI_PRICE_PATH,
                tr_id=MULTI_PRICE_TR_ID,
                params=_multi_params(requested_tickers),
            )
        returned_rows = _rows(multi_resp)
        multi_rows[market] = returned_rows
        returned_tickers = [
            str(row.get("inter_shrn_iscd") or "")
            for row in returned_rows
            if row.get("inter_shrn_iscd")
        ]
        market_summaries.append(
            {
                "market": market,
                "ranking_count": len(rows),
                "requested_count": len(requested_tickers),
                "returned_count": len(returned_rows),
                "requested_tickers": requested_tickers,
                "returned_tickers": returned_tickers,
                "missing_tickers": [
                    ticker for ticker in requested_tickers if ticker not in returned_tickers
                ],
                "unexpected_tickers": [
                    ticker for ticker in returned_tickers if ticker not in requested_tickers
                ],
            }
        )

    _prepared_tickers = _select_probe_tickers(
        ranking_rows,
        multi_rows,
        limit=_shadow_top_n(),
    )
    prepared_by_ticker: dict[str, dict] = {}
    for market in ("J", "Q"):
        ranking_by_ticker = {
            str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or ""): row
            for row in ranking_rows.get(market, [])
        }
        for row in multi_rows.get(market, []):
            candidate = _candidate_from_multi(row, market, ranking_by_ticker)
            if candidate is not None:
                prepared_by_ticker[str(candidate["ticker"])] = candidate
    _prepared_candidates = [
        prepared_by_ticker[ticker]
        for ticker in _prepared_tickers
        if ticker in prepared_by_ticker
    ]
    # 시장 전체를 한 표본으로 본다. 시장별로 따로 재면 한쪽 응답이 얇을 때
    # 표본 하한에 걸려 판정을 놓친다.
    _last_closed_verdict = evaluate_market_closed(
        [row for rows in multi_rows.values() for row in rows]
    )
    if _last_closed_verdict["closed"]:
        _append_record(
            "PAPER_FAST_PROBE_MARKET_CLOSED", phase="PREOPEN", **_last_closed_verdict
        )
        log("PAPER_FAST_PROBE_MARKET_CLOSED", level="WARN", **_last_closed_verdict)

    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    _append_record(
        "PAPER_FAST_PROBE_PREOPEN_DONE",
        phase="PREOPEN",
        elapsed_ms=elapsed_ms,
        selected_tickers=_prepared_tickers,
        markets=market_summaries,
        market_closed=_last_closed_verdict,
    )
    log(
        "PAPER_FAST_PROBE_PREOPEN_DONE",
        level="INFO",
        elapsed_ms=elapsed_ms,
        selected_tickers=_prepared_tickers,
        market_count=len(market_summaries),
    )
    return list(_prepared_tickers)


def _load_prepared_tickers() -> list[str]:
    path = _probe_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("event") != "PAPER_FAST_PROBE_PREOPEN_DONE":
            continue
        return [
            str(ticker)
            for ticker in record.get("selected_tickers", [])
            if len(str(ticker)) == 6 and str(ticker).isdigit()
        ][:_shadow_top_n()]
    return []


def load_persisted_open_candidates(
    now: datetime | None = None,
) -> tuple[Path | None, list[dict]]:
    """Rebuild today's last completed fast-path selection **for reporting only**.

    Replays the probe file's public multi-price response plus the ordered
    tickers that were accepted, so `/api/f1` can still show the selection after
    a restart without another KIS call.

    This deliberately does NOT repopulate the in-memory ``_open_candidates``:
    those rows carry ``fast_observed_monotonic``, which is meaningless across
    processes and would defeat F3's freshness gate.  Restart recovery on the
    trading side comes from the F1 snapshot (``save_candidate_snapshot``) and
    persisted state, not from here.
    """
    path = _probe_path(now)
    if not path.exists():
        return None, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, []

    ranking_by_ticker: dict[str, dict] = {}
    market_by_ticker: dict[str, str] = {}
    prepared_by_ticker: dict[str, dict] = {}
    pending_open_rows: list[dict] = []
    restored: list[dict] = []

    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue

        event = record.get("event")
        # 새 관측 사이클 시작 — 이전 사이클 잔재가 섞이지 않도록 전부 비운다.
        # restored까지 비워야 완주하지 못한 사이클이 직전 결과를 재사용하지 않는다.
        if event == "PAPER_FAST_PROBE_PREOPEN_START":
            ranking_by_ticker = {}
            market_by_ticker = {}
            prepared_by_ticker = {}
            pending_open_rows = []
            restored = []
            continue

        response_rows = _rows(record.get("response") or {})
        if event == "PAPER_FAST_PROBE_RANKING":
            market = str(record.get("market") or "J")
            for row in response_rows:
                ticker = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "")
                if ticker:
                    ranking_by_ticker[ticker] = row
                    market_by_ticker[ticker] = market
            continue

        if event == "PAPER_FAST_PROBE_MULTI" and record.get("phase") == "PREOPEN":
            market = str(record.get("market") or "J")
            for row in response_rows:
                candidate = _candidate_from_multi(row, market, ranking_by_ticker)
                if candidate is not None:
                    prepared_by_ticker[str(candidate["ticker"])] = candidate
            continue

        if event == "PAPER_FAST_PROBE_OPEN_MULTI":
            pending_open_rows = response_rows
            continue

        if event != "PAPER_FAST_PROBE_OPEN_DONE" or not pending_open_rows:
            continue

        parsed: list[dict] = []
        for row in pending_open_rows:
            ticker = str(row.get("inter_shrn_iscd") or "")
            prepared = prepared_by_ticker.get(ticker, {})
            candidate = _candidate_from_multi(
                row,
                str(prepared.get("market") or market_by_ticker.get(ticker) or "J"),
                ranking_by_ticker,
            )
            if candidate is None:
                continue
            if prepared:
                candidate["avg_amount_5d"] = prepared.get("avg_amount_5d", 0.0)
            candidate["gap_allowed"] = f1_selector.gap_allowed(candidate)
            candidate["gap_source"] = f"fast.{candidate.get('gap_source', 'multi')}"
            parsed.append(candidate)

        ranked_by_ticker = {
            str(candidate.get("ticker")): candidate
            for candidate in f1_selector.rank_candidates(parsed)
            if candidate.get("ticker")
        }
        parsed_by_ticker = {
            str(candidate.get("ticker")): candidate
            for candidate in parsed
            if candidate.get("ticker")
        }
        accepted_tickers = [
            str(ticker)
            for ticker in (record.get("shadow_tickers") or [])
            if ticker
        ]
        restored = [
            ranked_by_ticker.get(ticker) or parsed_by_ticker[ticker]
            for ticker in accepted_tickers
            if ticker in ranked_by_ticker or ticker in parsed_by_ticker
        ]
        # 소비한 개장 응답은 비운다 — 뒤따르는 OPEN_DONE이 같은 행을 다시 쓰지 않게.
        pending_open_rows = []

    return (path, restored) if restored else (None, [])


def evaluate_market_closed(rows: list[dict]) -> dict[str, Any]:
    """멀티시세 응답이 '오늘 세션이 없다'고 말하는지 판정한다.

    휴장일에는 동시호가가 돌지 않아 예상체결 필드가 비고, 현재가·거래량 자리에
    직전 거래일 값이 그대로 내려온다. 세 신호가 **모두** 일치할 때만 휴장으로
    본다. 하나라도 어긋나면 개장으로 취급한다(fail-open).

    ``any`` 로 판정하면 안 된다. 2026-08-13 개장일에도 ``hour_cls_code='0'`` 인
    종목이 1개 있었고(거래정지로 추정), 그걸 휴장으로 오인하면 정상 거래일을
    통째로 잃는다.
    """
    total = len(rows)
    hour_cls_codes = sorted({str(row.get("hour_cls_code") or "") for row in rows})
    expected_qty_zero = sum(1 for row in rows if _to_float(row.get("intr_antc_vol")) <= 0)
    volumes = sorted(_to_float(row.get("acml_vol")) for row in rows)
    median_volume = volumes[total // 2] if volumes else 0.0
    evidence = {
        "total_count": total,
        "hour_cls_codes": hour_cls_codes,
        "expected_qty_zero_count": expected_qty_zero,
        "median_acml_vol": median_volume,
    }

    if total < MARKET_CLOSED_MIN_ROWS:
        return {"closed": False, "reason": "SAMPLE_TOO_SMALL", **evidence}
    if hour_cls_codes != [MARKET_CLOSED_HOUR_CLS]:
        return {"closed": False, "reason": "HOUR_CLS_NOT_CLOSED", **evidence}
    if expected_qty_zero != total:
        return {"closed": False, "reason": "EXPECTED_QTY_PRESENT", **evidence}
    if median_volume < MARKET_CLOSED_MIN_STALE_VOLUME:
        # 거래량까지 0인 응답은 휴장이 아니라 응답 이상일 수 있다. 근거가 모자라면
        # 막지 않는다.
        return {"closed": False, "reason": "VOLUME_NOT_STALE", **evidence}
    return {"closed": True, "reason": "STALE_SESSION", **evidence}


def get_market_closed_verdict() -> dict[str, Any]:
    """장전 프로브가 마지막으로 낸 휴장 판정. 실행 전이면 NOT_RUN."""
    return dict(_last_closed_verdict)


def market_closed_detected() -> bool:
    return _last_closed_verdict.get("closed") is True


def get_open_candidates() -> list[dict]:
    return [dict(candidate) for candidate in _open_candidates]


def get_last_open_quality() -> dict[str, Any]:
    return dict(_last_open_quality)


def open_quality_ok() -> bool:
    return _last_open_quality.get("ok") is True


def merge_candidates(fast_candidates: list[dict], legacy_candidates: list[dict]) -> list[dict]:
    """Merge fallback results without losing fresher Fast candidates.

    Legacy rows establish the wider-universe baseline; duplicate Fast rows override them
    because their quote was observed at the open boundary. The common selector is applied
    once more so the merged result preserves the normal F1 ordering and safety floors.
    """
    by_ticker = {
        str(candidate.get("ticker")): dict(candidate)
        for candidate in legacy_candidates
        if candidate.get("ticker")
    }
    by_ticker.update({
        str(candidate.get("ticker")): dict(candidate)
        for candidate in fast_candidates
        if candidate.get("ticker")
    })
    return f1_selector.rank_candidates(list(by_ticker.values()))


def compare_with_legacy(legacy_candidates: list[dict]) -> dict:
    """Record fast/legacy selection parity without changing live candidates."""
    fast = get_open_candidates()
    fast_tickers = [str(c.get("ticker") or "") for c in fast if c.get("ticker")]
    legacy_tickers = [
        str(c.get("ticker") or "") for c in legacy_candidates if c.get("ticker")
    ]
    common = set(fast_tickers) & set(legacy_tickers)
    fast_by_ticker = {str(c.get("ticker")): c for c in fast}
    legacy_by_ticker = {str(c.get("ticker")): c for c in legacy_candidates}
    gap_deltas = [
        abs(
            float(fast_by_ticker[ticker].get("gap_pct") or 0.0)
            - float(legacy_by_ticker[ticker].get("gap_pct") or 0.0)
        )
        * 100
        for ticker in common
    ]
    fields: dict[str, Any] = {
        "fast_count": len(fast_tickers),
        "legacy_count": len(legacy_tickers),
        "fast_tickers": fast_tickers,
        "legacy_tickers": legacy_tickers,
        "top3_overlap_count": len(set(fast_tickers[:3]) & set(legacy_tickers[:3])),
        "rank1_match": bool(
            fast_tickers and legacy_tickers and fast_tickers[0] == legacy_tickers[0]
        ),
        "common_count": len(common),
        "max_gap_delta_pct_point": round(max(gap_deltas), 4) if gap_deltas else None,
    }
    if shadow_enabled():
        _append_record("PAPER_FAST_SHADOW_COMPARE", phase="SHADOW", **fields)
        log("PAPER_FAST_SHADOW_COMPARE", level="INFO", **fields)
    return fields


def _day_market_was_closed(lines: list[str]) -> bool:
    """하루치 프로브 기록이 '그날 세션이 없었다'고 말하는지.

    새 기록에는 장전 감지가 ``PAPER_FAST_PROBE_MARKET_CLOSED`` 로 남는다. 감지가
    붙기 전 기록에는 그 표식이 없으므로 개장 멀티시세 응답으로 다시 판정한다.
    """
    rows: list[dict] = []
    for line in lines:
        if '"PAPER_FAST_PROBE_MARKET_CLOSED"' in line:
            return True
        if '"PAPER_FAST_PROBE_OPEN_MULTI"' not in line:
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        rows.extend((record.get("response") or {}).get("output") or [])
    return bool(rows) and evaluate_market_closed(rows)["closed"]


def shadow_validation_summary() -> dict:
    """Summarize distinct, timely open-vs-legacy shadow comparison days.

    A day counts only when a complete open-boundary observation is followed by
    a legacy comparison within three minutes. This excludes manual/test reruns
    much later in the day and never promotes the fast path automatically.
    """
    observations: list[tuple[str, dict]] = []
    skipped_untimely_days: list[str] = []
    skipped_incomplete_days: list[str] = []
    skipped_closed_days: list[str] = []
    parse_error_lines = 0
    scan_file_limit = _shadow_scan_file_limit()
    directory = _probe_dir()
    paths: list[Path] = []
    try:
        if directory.exists():
            paths = sorted(directory.glob("*.jsonl"), reverse=True)[:scan_file_limit]
    except OSError:
        paths = []
    for path in reversed(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            skipped_incomplete_days.append(path.stem)
            continue
        # 휴장일은 검증일이 아니다. 감지 로직이 붙기 전에 기록된 날(2026-08-17)도
        # 파일에 남은 개장 멀티시세로 다시 판정해 계수에서 뺀다. 그러지 않으면
        # 10일 게이트가 실제 표본보다 빨리 찬다.
        if _day_market_was_closed(lines):
            skipped_closed_days.append(path.stem)
            continue

        completed_opens: list[datetime] = []
        compares: list[tuple[datetime, dict]] = []
        for line in lines:
            if (
                '"PAPER_FAST_PROBE_OPEN_DONE"' not in line
                and '"PAPER_FAST_SHADOW_COMPARE"' not in line
            ):
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise TypeError("record is not an object")
                recorded_at = datetime.fromisoformat(str(record.get("ts") or ""))
                if recorded_at.tzinfo is None:
                    recorded_at = recorded_at.replace(tzinfo=KST)
                else:
                    recorded_at = recorded_at.astimezone(KST)
            except (TypeError, ValueError):
                parse_error_lines += 1
                continue
            if record.get("event") == "PAPER_FAST_PROBE_OPEN_DONE":
                quality = record.get("quality") or {}
                if isinstance(quality, dict) and quality.get("ok") is True:
                    completed_opens.append(recorded_at)
                continue
            if record.get("event") == "PAPER_FAST_SHADOW_COMPARE":
                compares.append((recorded_at, record))

        timely_compares = [
            record
            for compared_at, record in compares
            if any(
                0.0 <= (compared_at - opened_at).total_seconds() <= 180.0
                for opened_at in completed_opens
            )
        ]
        if timely_compares:
            observations.append((path.stem, timely_compares[-1]))
        elif completed_opens and compares:
            skipped_untimely_days.append(path.stem)
        elif completed_opens or compares:
            skipped_incomplete_days.append(path.stem)

    required_days = _shadow_required_days()
    observed_days = len(observations)
    return {
        "observed_days": observed_days,
        "required_days": required_days,
        "remaining_days": max(0, required_days - observed_days),
        "validation_complete": observed_days >= required_days,
        "observed_dates": [date for date, _ in observations],
        "rank1_match_days": sum(
            1 for _, record in observations if record.get("rank1_match") is True
        ),
        "top3_overlap_total": sum(
            int(record.get("top3_overlap_count") or 0) for _, record in observations
        ),
        "scanned_files": len(paths),
        "scan_file_limit": scan_file_limit,
        "skipped_untimely_days": skipped_untimely_days,
        "skipped_incomplete_days": skipped_incomplete_days,
        "skipped_closed_days": skipped_closed_days,
        "parse_error_lines": parse_error_lines,
    }


def log_shadow_validation_progress() -> None:
    """Log bounded shadow progress after the entry pipeline; never raise."""
    if not shadow_enabled():
        return
    try:
        fields: dict[str, Any] = shadow_validation_summary()
        log("PAPER_FAST_SHADOW_PROGRESS", level="INFO", **fields)
    except Exception as exc:
        log(
            "PAPER_FAST_SHADOW_PROGRESS_ERROR",
            level="WARN",
            reason="UNHANDLED",
            error=repr(exc),
        )


async def _wait_for_prepare(target: datetime) -> None:
    """진행 중인 prepare() 가 끝나기를 예산 안에서 기다리고, 기다린 결과를 기록한다."""
    budget_ms = max(0, int(os.getenv("PAPER_FAST_PROBE_PREOPEN_WAIT_MS", "8000")))
    loop = asyncio.get_running_loop()
    started = loop.time()
    while _prepare_in_progress and (loop.time() - started) * 1000 < budget_ms:
        await asyncio.sleep(0.05)
    waited_ms = max(0, round((loop.time() - started) * 1000))
    completed = not _prepare_in_progress
    fields: dict[str, Any] = {
        "phase": "OPEN",
        "target_ts": target.isoformat(),
        "waited_ms": waited_ms,
        "budget_ms": budget_ms,
        "completed": completed,
        "prepared_count": len(_prepared_tickers),
    }
    _append_record("PAPER_FAST_PROBE_OPEN_WAITED_PREOPEN", **fields)
    log("PAPER_FAST_PROBE_OPEN_WAITED_PREOPEN", level="INFO" if completed else "WARN", **fields)


async def observe_open_boundary() -> list[dict]:
    """Observe and rank the prepared shortlist near 09:00."""
    global _prepared_tickers, _open_candidates, _last_open_quality
    _open_candidates = []
    _last_open_quality = {"ok": False, "reason": "NOT_RUN"}
    if not enabled():
        _last_open_quality = {"ok": False, "reason": "DISABLED"}
        return []

    now = datetime.now(KST)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    offset_ms = max(0, int(os.getenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")))
    target += timedelta(milliseconds=offset_ms)
    if now < target:
        await asyncio.sleep((target - now).total_seconds())
        now = datetime.now(KST)

    # 지각은 스케줄러가 여기 도착한 시각으로 잰다. 아래에서 PREOPEN 을 기다린 시간은
    # 지각이 아니라 별도 예산(PAPER_FAST_PROBE_PREOPEN_WAIT_MS)이다.
    lateness_ms = max(0, round((now - target).total_seconds() * 1000))
    max_lateness_ms = max(
        0,
        int(os.getenv("PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS", "2500")),
    )
    if lateness_ms <= max_lateness_ms and _prepare_in_progress and not _prepared_tickers:
        # 2026-09-22: 08:59:45 랭킹 호출 1건이 16.5초 걸려 PREOPEN 이 09:00:05 에 끝났는데
        # OPEN 은 09:00:00 에 NO_PREOPEN_TICKERS 로 포기했다 → 레거시 경로, 진입 09:02.
        # 5초만 기다렸으면 빠른 경로였다. 여기서 기다린다.
        await _wait_for_prepare(target)
    if lateness_ms > max_lateness_ms:
        _last_open_quality = {
            "ok": False,
            "reason": "TOO_LATE",
            "lateness_ms": lateness_ms,
        }
        _append_record(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            phase="OPEN",
            target_ts=target.isoformat(),
            actual_ts=now.isoformat(),
            lateness_ms=lateness_ms,
            max_lateness_ms=max_lateness_ms,
            reason="TOO_LATE",
        )
        log(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            level="WARN",
            lateness_ms=lateness_ms,
            max_lateness_ms=max_lateness_ms,
            reason="TOO_LATE",
        )
        return []

    tickers = list(_prepared_tickers) or _load_prepared_tickers()
    if not tickers:
        _last_open_quality = {"ok": False, "reason": "NO_PREOPEN_TICKERS"}
        _append_record(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            phase="OPEN",
            target_ts=target.isoformat(),
            actual_ts=now.isoformat(),
            lateness_ms=lateness_ms,
            reason="NO_PREOPEN_TICKERS",
        )
        log(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            level="WARN",
            lateness_ms=lateness_ms,
            reason="NO_PREOPEN_TICKERS",
        )
        return []

    resp = await _get_and_record(
        event="PAPER_FAST_PROBE_OPEN_MULTI",
        phase="OPEN",
        market="SHORTLIST",
        path=MULTI_PRICE_PATH,
        tr_id=MULTI_PRICE_TR_ID,
        params=_multi_params(tickers),
    )
    rows = _rows(resp)
    requested_tickers = list(dict.fromkeys(tickers[:30]))
    returned_tickers = [
        str(row.get("inter_shrn_iscd") or "")
        for row in rows
        if row.get("inter_shrn_iscd")
    ]
    returned_set = set(returned_tickers)
    requested_set = set(requested_tickers)
    missing_tickers = [ticker for ticker in requested_tickers if ticker not in returned_set]
    unexpected_tickers = [ticker for ticker in returned_tickers if ticker not in requested_set]
    valid_ask_tickers = {
        str(row.get("inter_shrn_iscd") or "")
        for row in rows
        if _to_float(row.get("inter2_askp")) > 0
    } & requested_set
    invalid_ask_tickers = [
        ticker for ticker in requested_tickers if ticker not in valid_ask_tickers
    ]
    response_ok = str(resp.get("rt_cd") or "") == "0"
    if not response_ok:
        quality_reason = "API_ERROR"
    elif missing_tickers:
        quality_reason = "MISSING_TICKERS"
    elif invalid_ask_tickers:
        quality_reason = "INVALID_ASK"
    elif unexpected_tickers or len(returned_tickers) != len(requested_tickers):
        quality_reason = "RESPONSE_MISMATCH"
    else:
        quality_reason = "COMPLETE"
    _last_open_quality = {
        "ok": quality_reason == "COMPLETE",
        "reason": quality_reason,
        "requested_count": len(requested_tickers),
        "returned_count": len(returned_tickers),
        "valid_ask_count": len(valid_ask_tickers),
        "missing_tickers": missing_tickers,
        "invalid_ask_tickers": invalid_ask_tickers,
        "unexpected_tickers": unexpected_tickers,
        "rt_cd": resp.get("rt_cd"),
        "msg_cd": resp.get("msg_cd"),
    }
    prepared_by_ticker = {
        str(candidate.get("ticker") or ""): candidate
        for candidate in _prepared_candidates
    }
    parsed: list[dict] = []
    observed_monotonic = time.monotonic()
    for row in rows:
        ticker = str(row.get("inter_shrn_iscd") or "")
        # 불완전 응답에서도 정상 코드 아래 유효 매도호가가 확인된 행만 fallback
        # 병합 후보로 보존한다. API 오류/호가 결손 행은 거래 후보로 승격하지 않는다.
        if not response_ok or ticker not in valid_ask_tickers:
            continue
        prepared = prepared_by_ticker.get(ticker, {})
        candidate = _candidate_from_multi(
            row,
            str(prepared.get("market") or "J"),
            {},
        )
        if candidate is None:
            continue
        if prepared:
            candidate["avg_amount_5d"] = prepared.get("avg_amount_5d", 0.0)
        candidate["gap_allowed"] = f1_selector.gap_allowed(candidate)
        candidate["fast_observed_monotonic"] = observed_monotonic
        candidate["gap_source"] = f"fast.{candidate.get('gap_source', 'multi')}"
        parsed.append(candidate)
    _open_candidates = f1_selector.rank_candidates(parsed)
    filter_stats = f1_selector.selection_stats(parsed)
    _append_record(
        "PAPER_FAST_PROBE_OPEN_DONE",
        phase="OPEN",
        target_ts=target.isoformat(),
        actual_ts=now.isoformat(),
        lateness_ms=lateness_ms,
        requested_tickers=requested_tickers,
        returned_tickers=returned_tickers,
        valid_ask_count=len(valid_ask_tickers),
        output_count=len(rows),
        shadow_candidate_count=len(_open_candidates),
        shadow_tickers=[candidate.get("ticker") for candidate in _open_candidates],
        quality=_last_open_quality,
        **filter_stats,
    )
    log(
        "PAPER_FAST_PROBE_OPEN_DONE",
        level="INFO" if rows else "WARN",
        lateness_ms=lateness_ms,
        requested_count=len(requested_tickers),
        output_count=len(rows),
        valid_ask_count=len(valid_ask_tickers),
        shadow_candidate_count=len(_open_candidates),
        quality_reason=quality_reason,
        **filter_stats,
    )
    return get_open_candidates()

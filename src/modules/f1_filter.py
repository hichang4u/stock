"""F1 premarket candidate filter (08:40 ~ 08:58)."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.modules import f1_selector, f1_snapshot_selector
from src.utils.logger import log
from src.utils.number import to_float as _to_float
from src.utils.number import to_int as _to_int

KST = ZoneInfo("Asia/Seoul")

GAP_MIN = f1_selector.GAP_MIN
GAP_MAX = f1_selector.GAP_CORE_MAX
HIGH_GAP_MAX = f1_selector.GAP_HARD_MAX
EXTREME_GAP_MAX = 0.150
HIGH_GAP_MIN_EXPECTED_AMOUNT = f1_selector.HIGH_GAP_MIN_EXPECTED_AMOUNT
MIN_EXPECTED_AMOUNT = f1_selector.MIN_EXPECTED_AMOUNT
LIQUIDITY_TOP_PCT = f1_selector.LIQUIDITY_TOP_PCT
F1_MIN_CANDIDATES = f1_selector.MIN_CANDIDATES

F1_DEADLINE_H = 9
F1_DEADLINE_M = 10
F1_RETRY_INTERVAL_SEC = int(os.getenv("F1_RETRY_INTERVAL_SEC", "5"))
F1_SNAPSHOT_DIR = os.getenv("F1_SNAPSHOT_DIR", "data/f1_snapshots")
# 회전 한도는 수집 창보다 넉넉히 커야 한다. 스냅샷 JSONL은 후보 원본의 유일한
# 사본이고, 계획은 최소 40거래일 + 잠금 10거래일을 요구하며 원시 데이터는 180일
# 보존이 원칙이다. 하루 여러 파일이 남을 수 있으므로 20개는 기준선 절반을 수집
# 도중에 지운다.
F1_SNAPSHOT_KEEP = int(os.getenv("F1_SNAPSHOT_KEEP", "400"))
# Keep the PAPER default at 1. A 2026-07-23 read-only benchmark found no speed
# benefit at 2 and observed one KIS rate-limit response; see docs/F1_SPEED_EXPERIMENT_20260723.md.
F1_EXPECTED_QUOTE_CONCURRENCY = int(os.getenv("F1_EXPECTED_QUOTE_CONCURRENCY", "1"))
F1_PROGRESS_LOG_EVERY = max(1, int(os.getenv("F1_PROGRESS_LOG_EVERY", "10")))
F1_MARKET_INTERVAL_SEC = float(os.getenv("F1_MARKET_INTERVAL_SEC", "3.0"))

# KIS ranking uses J+input market buckets, and expected-quote accepts J for both KOSPI/KOSDAQ.
_PREMARKET_MARKETS = (
    {"label": "J", "ranking_market": "J", "ranking_input": "0001", "quote_market": "J"},
    {"label": "Q", "ranking_market": "J", "ranking_input": "1001", "quote_market": "J"},
)

async def run(*, terminal_on_empty: bool = True) -> list[dict]:
    """
    Fetch premarket candidates, apply the gap/liquidity filters, and retry until
    the F2 deadline if the KIS premarket fields are not ready yet.
    """
    if os.getenv("DRY_RUN", "0") == "1":
        result = [_dry_run_candidate()]
        log("DRY_RUN_F1_DONE", level="WARN", total_candidates=len(result), passed=len(result))
        return result

    s = state.get()
    if s.day_skip:
        log(
            "F1_SKIPPED",
            level="WARN",
            reason=s.close_reason or "DAY_SKIP",
            trading_date=s.trading_date,
            position_status=s.position_status,
        )
        return []

    attempt = 0
    while True:
        attempt += 1
        raw_candidates = await _fetch_all_premarket()
        gap_filtered = _filter_by_gap(raw_candidates)
        result = select_liquidity_candidates(gap_filtered)
        empty_reason = "SELECTION_FILTER_EMPTY" if gap_filtered else "GAP_FILTER_EMPTY"

        if result:
            break

        log(
            "F1_FILTER_EMPTY",
            level="INFO",
            attempt=attempt,
            raw_count=len(raw_candidates),
            filter_count=len(gap_filtered),
            reason=empty_reason,
            **_gap_stats(raw_candidates),
        )

        if not _should_retry():
            if not terminal_on_empty:
                log(
                    "F1_FALLBACK_EMPTY",
                    level="WARN",
                    attempt=attempt,
                    raw_count=len(raw_candidates),
                    filter_count=len(gap_filtered),
                    reason=empty_reason,
                    **_gap_stats(raw_candidates),
                )
                return []
            log(
                "NO_TARGET",
                level="INFO",
                attempt=attempt,
                raw_count=len(raw_candidates),
                filter_count=len(gap_filtered),
                reason=empty_reason,
                **_gap_stats(raw_candidates),
            )
            await notifier.send(
                "NO_TARGET",
                level="INFO",
                message="당일 필터 통과 종목 없음. 거래 스킵.",
            )
            s.day_skip = True
            today = datetime.now(KST).strftime("%Y%m%d")
            await db.record_skip(
                today,
                "NO_TARGET",
                f"raw={len(raw_candidates)},gap_filtered={len(gap_filtered)},reason={empty_reason}",
            )
            return []

        sleep_sec = _retry_sleep_seconds()
        log(
            "F1_RETRY_WAIT",
            level="WARN",
            attempt=attempt,
            retry_after_sec=sleep_sec,
            raw_count=len(raw_candidates),
            deadline=f"{F1_DEADLINE_H:02d}:{F1_DEADLINE_M:02d}:00",
            reason=empty_reason,
        )
        await asyncio.sleep(sleep_sec)

    total = len(gap_filtered)

    log("F1_DONE", level="INFO", total_candidates=total, passed=len(result))
    return result


def _filter_by_gap(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if _is_gap_candidate(c)]


def _is_gap_candidate(candidate: dict) -> bool:
    return candidate.get("gap_allowed") is True


def select_liquidity_candidates(candidates: list[dict]) -> list[dict]:
    return f1_selector.select_candidates(candidates)


def _classify_gap_candidate(candidate: dict) -> dict:
    gap = candidate.get("gap_pct", 0.0)
    amount = candidate.get("expected_amount", 0.0)

    if gap < 0:
        return {"gap_band": "NEGATIVE_GAP", "gap_allowed": False, "gap_reason": "NEGATIVE_GAP"}
    if gap < 0.020:
        return {"gap_band": "LOW_GAP", "gap_allowed": False, "gap_reason": "GAP_BELOW_2"}
    if gap < GAP_MIN:
        return {"gap_band": "WEAK_GAP", "gap_allowed": False, "gap_reason": "GAP_BELOW_CORE"}
    if gap < GAP_MAX:
        return {"gap_band": "CORE_GAP", "gap_allowed": True, "gap_reason": "CORE_GAP"}
    if gap < HIGH_GAP_MAX:
        if f1_selector.high_gap_allowed(candidate):
            return {
                "gap_band": "HIGH_GAP",
                "gap_allowed": True,
                "gap_reason": "HIGH_GAP_ALLOWED",
            }
        if amount < HIGH_GAP_MIN_EXPECTED_AMOUNT:
            reason = "HIGH_GAP_AMOUNT_LOW"
        else:
            reason = "HIGH_GAP_REJECTED"
        return {"gap_band": "HIGH_GAP", "gap_allowed": False, "gap_reason": reason}
    if gap < EXTREME_GAP_MAX:
        return {"gap_band": "EXTREME_GAP", "gap_allowed": False, "gap_reason": "EXTREME_GAP_RISK"}
    return {"gap_band": "EXCLUDED_GAP", "gap_allowed": False, "gap_reason": "GAP_TOO_HIGH"}


def _gap_stats(candidates: list[dict]) -> dict:
    gaps = [c.get("gap_pct", 0.0) * 100 for c in candidates]
    if not gaps:
        return {"gap_min_pct": None, "gap_max_pct": None, "zero_gap_count": 0}
    return {
        "gap_min_pct": round(min(gaps), 3),
        "gap_max_pct": round(max(gaps), 3),
        "zero_gap_count": sum(1 for g in gaps if abs(g) < 0.0001),
    }


def _should_retry() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("F1_ENABLE_RETRY", "1") != "1":
        return False
    now = datetime.now(KST)
    deadline = now.replace(hour=F1_DEADLINE_H, minute=F1_DEADLINE_M, second=0, microsecond=0)
    return now < deadline


def _retry_sleep_seconds() -> int:
    now = datetime.now(KST)
    deadline = now.replace(hour=F1_DEADLINE_H, minute=F1_DEADLINE_M, second=0, microsecond=0)
    remaining = max(1, int((deadline - now).total_seconds()))
    return max(1, min(F1_RETRY_INTERVAL_SEC, remaining))


def _ranking_params(market_cfg: dict) -> dict:
    """FHPST01710000 질의 파라미터. 장중 감시 경로와 F1 본선이 같은 값을 쓴다.

    fid_rsfl_rate1/2 는 KIS 가 무시한다(거래대금 상위를 돌려준다). 등락률
    거르기는 클라이언트가 한다 — C1 스펙 2.1절의 정정 기록과 같은 이야기다.
    """
    return {
        "fid_cond_mrkt_div_code": market_cfg["ranking_market"],
        "fid_cond_scr_div_code": "20171",
        "fid_input_iscd": market_cfg["ranking_input"],
        "fid_rank_sort_cls_code": "0",
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": f"{GAP_MIN * 100:.1f}",
        "fid_rsfl_rate2": f"{HIGH_GAP_MAX * 100:.1f}",
    }


async def _fetch_all_premarket() -> list[dict]:
    """
    Fetch KOSPI/KOSDAQ fluctuation rankings and enrich each row with the
    quote API's expected execution price/volume when available.
    """
    results: list[dict] = []
    for index, market_cfg in enumerate(_PREMARKET_MARKETS):
        market = market_cfg["label"]
        if index > 0 and F1_MARKET_INTERVAL_SEC > 0:
            log("F1_MARKET_INTERVAL", level="INFO", market=market, sleep_sec=F1_MARKET_INTERVAL_SEC)
            await asyncio.sleep(F1_MARKET_INTERVAL_SEC)

        log(
            "F1_FETCH_START",
            level="INFO",
            market=market,
            ranking_input=market_cfg["ranking_input"],
            gap_min_pct=round(GAP_MIN * 100, 2),
            gap_max_pct=round(HIGH_GAP_MAX * 100, 2),
        )
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                tr_id="FHPST01710000",
                params=_ranking_params(market_cfg),
            )
        except Exception as e:
            log("F1_API_ERROR", level="WARN", market=market, error=repr(e))
            continue

        output = resp.get("output", [])
        log(
            "F1_EXPECTED_QUOTE_START",
            level="INFO",
            market=market,
            output_count=len(output),
            concurrency=max(1, F1_EXPECTED_QUOTE_CONCURRENCY),
        )
        candidates, parse_stats = await _parse_candidates_concurrently(
            output,
            market,
            market_cfg["quote_market"],
        )
        log(
            "F1_EXPECTED_QUOTE_DONE",
            level="INFO",
            market=market,
            output_count=len(output),
            parsed_count=len(candidates),
            **parse_stats,
        )
        parsed_count = len(candidates)
        zero_gap_count = sum(1 for c in candidates if abs(c.get("gap_pct", 0.0)) < 0.000001)
        results.extend(candidates)

        log(
            "F1_FETCH_DONE",
            level="INFO",
            market=market,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
            output_count=len(output),
            parsed_count=parsed_count,
            zero_gap_count=zero_gap_count,
        )

    _log_expected_comparison(results)
    _save_candidate_snapshot(results)
    return results


async def fetch_ranking_only_candidates() -> list[dict]:
    """랭킹 응답만으로 F1 후보를 만든다 — 종목별 예상체결 조회를 하지 않는다.

    빠른 경로가 이긴 날에도 레거시가 무엇을 골랐을지 기록하기 위한 감시용이다
    (docs/FAST_PATH_UNIVERSE_FOLLOWUP_20260929.md 8.5절의 E'). 후보에 필요한 값은
    전부 랭킹 행에 있다 — prdy_ctrt(갭), stck_prpr(가격), acml_vol/acml_tr_pbmn
    (수량·금액), avrg_vol(5일 평균). 예상체결 조회 60회는 장전 예상가로 값을
    다듬을 뿐이고 개장 후에는 그 값이 0이라 생략해도 결과가 같다.

    호출 2회, BACKGROUND 우선순위다. 체결·청산 경로(CRITICAL/PRICE)가 언제나
    먼저 나간다. 한쪽 시장이 실패해도 다른 쪽 결과는 남긴다 — 감시가 전부
    아니면 무가 되면 그날 표본을 통째로 잃는다.
    """
    results: list[dict] = []
    for index, market_cfg in enumerate(_PREMARKET_MARKETS):
        market = market_cfg["label"]
        if index > 0 and F1_MARKET_INTERVAL_SEC > 0:
            await asyncio.sleep(F1_MARKET_INTERVAL_SEC)
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                tr_id="FHPST01710000",
                params=_ranking_params(market_cfg),
                request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            )
        except Exception as exc:
            log("F1_RANKING_ONLY_ERROR", level="WARN", market=market, error=repr(exc))
            continue
        for item in resp.get("output") or []:
            if _candidate_skip_reason(item) is not None:
                continue
            try:
                candidate = await _parse_candidate(
                    item,
                    market,
                    market_cfg["quote_market"],
                    use_expected_quote=False,
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                log(
                    "F1_RANKING_ONLY_PARSE_ERROR",
                    level="WARN",
                    market=market,
                    error=repr(exc),
                )
                continue
            if candidate is not None:
                results.append(candidate)
    return results


async def _parse_candidates_concurrently(
    items: list[dict],
    market: str,
    quote_market: str = "J",
) -> tuple[list[dict], dict]:
    concurrency = max(1, F1_EXPECTED_QUOTE_CONCURRENCY)
    semaphore = asyncio.Semaphore(concurrency)
    total = len(items)
    completed = 0
    parsed_count = 0
    quote_valid_count = 0
    quote_fallback_count = 0
    error_count = 0
    skip_reasons: dict[str, int] = {}
    progress_every = max(1, F1_PROGRESS_LOG_EVERY)

    async def parse_one(item: dict) -> dict | None:
        nonlocal completed, parsed_count, quote_valid_count, quote_fallback_count, error_count
        skip_reason = _candidate_skip_reason(item)
        candidate = None
        if skip_reason is not None:
            skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1
        else:
            async with semaphore:
                try:
                    candidate = await _parse_candidate(item, market, quote_market)
                except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                    error_count += 1
                    log(
                        "F1_CANDIDATE_PARSE_ERROR",
                        level="WARN",
                        ticker=item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd"),
                        market=market,
                        reason=exc.__class__.__name__,
                        error=str(exc)[:200],
                    )
        completed += 1
        if candidate is not None:
            parsed_count += 1
            if candidate.get("gap_source") == "expected.antc_cnpr":
                quote_valid_count += 1
            else:
                quote_fallback_count += 1
        if total and (completed == total or completed % progress_every == 0):
            log(
                "F1_EXPECTED_QUOTE_PROGRESS",
                level="INFO",
                market=market,
                completed=completed,
                total=total,
                parsed_count=parsed_count,
                eligible_count=parsed_count,
                skipped_count=sum(skip_reasons.values()),
                quote_valid_count=quote_valid_count,
                quote_fallback_count=quote_fallback_count,
                error_count=error_count,
                skip_reasons=dict(sorted(skip_reasons.items())),
                progress_pct=round((completed / total) * 100, 1),
            )
        return candidate

    parsed = await asyncio.gather(*(parse_one(item) for item in items))
    candidates = [candidate for candidate in parsed if candidate is not None]
    return candidates, {
        "processed_count": total,
        "eligible_count": len(candidates),
        "skipped_count": sum(skip_reasons.values()),
        "quote_valid_count": quote_valid_count,
        "quote_fallback_count": quote_fallback_count,
        "error_count": error_count,
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }


async def _parse_candidate(
    item: dict,
    market: str = "J",
    quote_market: str = "J",
    *,
    use_expected_quote: bool = True,
) -> dict | None:
    ticker = item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd")
    name = item.get("hts_kor_isnm", "")
    if _candidate_skip_reason(item) is not None:
        return None
    ranking_gap_pct = _to_float(item.get("prdy_ctrt"))
    ranking_price = _to_float(item.get("stck_prpr"))

    prev_close = ranking_price / (1 + ranking_gap_pct / 100)
    expected_price = ranking_price
    expected_qty = _to_int(item.get("acml_vol"))
    expected_amount = _to_float(item.get("acml_tr_pbmn"))
    final_gap_pct = ranking_gap_pct
    gap_source = "ranking.prdy_ctrt"

    # 개장 뒤에는 antc_cnpr 이 0이라 이 조회가 값을 바꾸지 못한다. 감시 경로는
    # 종목당 1회씩 도는 이 호출을 통째로 건너뛰어 랭킹 2회로 끝낸다.
    expected_quote = (
        await _fetch_expected_quote(ticker, quote_market) if use_expected_quote else None
    )
    if (
        expected_quote
        and expected_quote["expected_price"] > 0
        and expected_quote["expected_qty"] > 0
    ):
        expected_price = expected_quote["expected_price"]
        expected_qty = expected_quote["expected_qty"]
        expected_amount = expected_quote["expected_amount"]
        final_gap_pct = expected_quote["expected_gap_pct"]
        if abs(expected_quote.get("prev_close", 0.0)) > 0.0001:
            prev_close = expected_quote["prev_close"]
        elif abs(final_gap_pct) > 0.0001:
            prev_close = expected_price / (1 + final_gap_pct / 100)
        gap_source = "expected.antc_cnpr"

    avrg_vol = _to_float(item.get("avrg_vol"))
    vi_gap = _calc_vi_gap(expected_price, prev_close)
    candidate = {
        "ticker": ticker,
        "name": name,
        "market": market,
        "expected_price": expected_price,
        "prev_close": prev_close,
        "gap_pct": final_gap_pct / 100,
        "gap_source": gap_source,
        "avg_amount_5d": avrg_vol * ranking_price,
        "expected_amount": expected_amount,
        "expected_qty": expected_qty,
        "ranking_gap_pct": ranking_gap_pct / 100,
        "ranking_price": ranking_price,
        "ranking_qty": _to_int(item.get("acml_vol")),
        "ranking_amount": _to_float(item.get("acml_tr_pbmn")),
        "expected_api_gap_pct": (
            expected_quote["expected_gap_pct"] / 100 if expected_quote else None
        ),
        "expected_api_price": expected_quote["expected_price"] if expected_quote else None,
        "expected_api_qty": expected_quote["expected_qty"] if expected_quote else None,
        "expected_api_amount": expected_quote["expected_amount"] if expected_quote else None,
        "vi_gap": vi_gap,
        "buy_sell_ratio": 0.0,
    }
    candidate.update(_classify_gap_candidate(candidate))
    return candidate


def _candidate_skip_reason(item: dict) -> str | None:
    ticker = item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd")
    if not ticker or len(ticker) != 6 or not ticker.isdigit():
        return "INVALID_TICKER"

    name = str(item.get("hts_kor_isnm") or "")
    if f1_selector.is_excluded_product(name):
        return "EXCLUDED_PRODUCT"

    if _to_float(item.get("stck_prpr")) <= 0:
        return "INVALID_RANKING_PRICE"
    return None


async def _fetch_expected_quote(ticker: str, market: str = "J") -> dict | None:
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker},
        )
    except Exception as e:
        log("F1_EXPECTED_QUOTE_ERROR", level="WARN", ticker=ticker, market=market, error=repr(e))
        return None

    out = resp.get("output2", {})
    expected = _to_float(out.get("antc_cnpr"))
    qty = _to_int(out.get("antc_vol"))
    gap_pct = _to_float(out.get("antc_cntg_prdy_ctrt"))
    diff = _to_float(out.get("antc_cntg_vrss"))

    if expected <= 0:
        return None

    prev_close = expected - diff if diff else 0.0
    return {
        "expected_price": expected,
        "expected_qty": qty,
        "expected_amount": expected * qty,
        "expected_gap_pct": gap_pct,
        "prev_close": prev_close,
        "rt_cd": resp.get("rt_cd"),
        "msg_cd": resp.get("msg_cd"),
        "msg1": resp.get("msg1"),
    }


def _log_expected_comparison(candidates: list[dict]) -> None:
    ranking_pass = sum(1 for c in candidates if GAP_MIN <= c.get("ranking_gap_pct", 0.0) < GAP_MAX)
    expected_pass = sum(
        1
        for c in candidates
        if GAP_MIN <= (c.get("expected_api_gap_pct") or 0.0) < GAP_MAX
    )
    final_pass = sum(1 for c in candidates if _is_gap_candidate(c))
    expected_valid = sum(1 for c in candidates if (c.get("expected_api_price") or 0) > 0)
    gap_source_expected = sum(1 for c in candidates if c.get("gap_source") == "expected.antc_cnpr")
    mismatch = sum(
        1
        for c in candidates
        if (GAP_MIN <= c.get("ranking_gap_pct", 0.0) < GAP_MAX)
        != (GAP_MIN <= (c.get("expected_api_gap_pct") or 0.0) < GAP_MAX)
    )
    log(
        "F1_EXPECTED_COMPARE",
        level="INFO",
        total=len(candidates),
        ranking_pass=ranking_pass,
        expected_valid=expected_valid,
        expected_pass=expected_pass,
        final_pass=final_pass,
        expected_source_count=gap_source_expected,
        mismatch_count=mismatch,
        core_gap_count=sum(1 for c in candidates if c.get("gap_band") == "CORE_GAP"),
        high_gap_allowed_count=sum(
            1 for c in candidates if c.get("gap_reason") == "HIGH_GAP_ALLOWED"
        ),
    )


def write_candidate_snapshot(
    snapshot_dir: Path, candidates: list[dict], now: datetime
) -> Path:
    """Write the JSONL atomically then the completion sidecar.

    The full snapshot is written to a temp file and renamed into place, so a
    partially written ``.jsonl`` never appears. Only after that success is the
    completion sidecar written (also tmp→rename), making the sidecar's presence
    atomic evidence that the snapshot finished. The shared selector requires
    this evidence before treating a file as normal.
    """
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{now.strftime('%Y%m%d_%H%M%S')}.jsonl"
    tmp = snapshot_dir / f"{path.name}.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        for candidate in candidates:
            f.write(json.dumps(candidate, ensure_ascii=False) + "\n")
    tmp.replace(path)
    f1_snapshot_selector.write_completion_sidecar(path)
    return path


def _save_candidate_snapshot(candidates: list[dict]) -> None:
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("F1_SAVE_SNAPSHOT", "1") != "1":
        return
    if not candidates:
        return

    try:
        snapshot_dir = Path(F1_SNAPSHOT_DIR)
        path = write_candidate_snapshot(snapshot_dir, candidates, datetime.now(KST))
        _rotate_candidate_snapshots(snapshot_dir, keep=F1_SNAPSHOT_KEEP)
        log("F1_SNAPSHOT_SAVED", level="INFO", path=str(path), count=len(candidates))
    except Exception as e:
        log("F1_SNAPSHOT_SAVE_ERROR", level="WARN", error=repr(e))


def save_candidate_snapshot(candidates: list[dict]) -> None:
    """Persist candidates from alternate F1 sources using the standard format."""
    _save_candidate_snapshot(candidates)


def _rotate_candidate_snapshots(snapshot_dir: Path, keep: int = F1_SNAPSHOT_KEEP) -> None:
    if keep <= 0:
        return
    files = sorted(
        snapshot_dir.glob("*.jsonl"),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            f1_snapshot_selector.sidecar_path(old).unlink(missing_ok=True)
        except OSError as e:
            log("F1_SNAPSHOT_ROTATE_ERROR", level="WARN", path=str(old), error=repr(e))


def _calc_vi_gap(expected_price: float, prev_close: float) -> float | None:
    if expected_price <= 0 or prev_close <= 0:
        return None
    # Assumes the standard +/-10% static VI band. Some KOSDAQ names can use
    # +/-15%; model that explicitly before using this helper for those cases.
    static_vi_upper = prev_close * 1.10
    return (static_vi_upper - expected_price) / expected_price


def _is_common_stock_candidate(ticker: str | None, name: str = "") -> bool:
    """Exclude ETF/ETN/leveraged/inverse products from F1 candidates."""
    if not ticker or len(ticker) != 6 or not ticker.isdigit():
        return False

    return not f1_selector.is_excluded_product(name)


def _dry_run_candidate() -> dict:
    prev_close = float(os.getenv("DRY_RUN_PREV_CLOSE", "10000"))
    expected_price = float(os.getenv("DRY_RUN_EXPECTED_PRICE", "10300"))
    expected_qty = int(os.getenv("DRY_RUN_EXPECTED_QTY", "500000"))
    gap_pct = (expected_price / prev_close) - 1
    candidate = {
        "ticker": os.getenv("DRY_RUN_TICKER", "005930"),
        "name": "DRY RUN",
        "market": "J",
        "expected_price": expected_price,
        "prev_close": prev_close,
        "gap_pct": gap_pct,
        "gap_source": "dry_run",
        "avg_amount_5d": expected_price * expected_qty,
        "expected_amount": expected_price * expected_qty,
        "expected_qty": expected_qty,
        "ranking_gap_pct": gap_pct,
        "ranking_price": expected_price,
        "ranking_qty": expected_qty,
        "ranking_amount": expected_price * expected_qty,
        "expected_api_gap_pct": gap_pct,
        "expected_api_price": expected_price,
        "expected_api_qty": expected_qty,
        "expected_api_amount": expected_price * expected_qty,
        "vi_gap": ((prev_close * 1.10) - expected_price) / expected_price,
        "buy_sell_ratio": 2.0,
    }
    candidate.update(_classify_gap_candidate(candidate))
    return candidate

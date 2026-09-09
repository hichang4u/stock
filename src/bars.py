"""트랙 B 봉 집계 — 틱 스트림에서 1분 OHLCV를 만든다.

`src/modules/`가 아니라 최상위에 두는 이유는 CODING_GUIDELINES §2의
"modules/ 코드는 api/를 직접 import하지 않는다" 규칙 때문이다. live.py와
같은 층위의 관측 인프라로 본다.

첫 틱이 그 (날짜, 종목)의 계열을 시작하고, 봉이 닫힐 때마다 파일로
write-through 하며, 날짜나 종목이 바뀌면 이전 계열을 마감한다(설계 §3.1).

기동 배선(`install()` + `start()`)은 main.py가 아니라 `src/api/server.py`의
lifespan 훅에 있다 — main.py는 _STRATEGY_FILES에 있어 수정하면 트랙 A의
지문이 돌지만 server.py는 목록 밖이고, main.py가 uvicorn을 loop="none"으로
봇의 이벤트 루프 안에서 돌리므로 그 훅은 틱을 처리하는 것과 같은 루프에서
실행된다.
"""

import asyncio
import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import live, state, warmup
from src.api import kis_minute_bars
from src.modules import tick_capture
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")

_BARS_DIR = Path(os.getenv("TRACK_B_BARS_DIR", "data/bars"))

_DRAIN_INTERVAL_SEC = 1.0
_supervisor: asyncio.Task | None = None

# 모스펙 §11.1이 확정한 H0STCNT0 필드 배치
_IDX_CTTR = 18            # 체결강도
_IDX_CCLD_DVSN = 21       # 체결구분 (코드 의미는 해석하지 않는다)
_IDX_ASKP1 = 10           # 최우선 매도호가
_IDX_BIDP1 = 11           # 최우선 매수호가
_IDX_TOTAL_ASKP = 38      # 총 매도호가잔량
_IDX_TOTAL_BIDP = 39      # 총 매수호가잔량

_queue: deque[dict] = deque()
_series: dict[tuple[str, str], dict[str, dict]] = {}
_filters: dict[str, SpikeFilter] = {}
_dirty: set[tuple[str, str]] = set()
# 아직 봉이 열리지 않은 분에서 걸러진 스파이크 수. (날짜, 종목, 분) → 개수.
# 그 분의 첫 정상 틱이 봉을 열 때 옮겨 담는다.
_pending_spikes: dict[tuple[str, str, str], int] = {}
# 지금 정정이 막혀 있는 (날짜, 종목) → 사유. 에피소드가 시작·종료할 때만
# 로그를 남기려고 둔다. 매분 남기면 하루 수백 줄이 된다.
_correction_blocked: dict[tuple[str, str], str] = {}
# 지금 틱이 들어오고 있는 (날짜, 종목). 바뀌면 이전 계열을 마감한다.
_active: tuple[str, str] | None = None


def bars_path(date: str, ticker: str) -> Path:
    return _BARS_DIR / f"{date}_{ticker}.json"


def reset() -> None:
    """테스트 전용 — 모든 인메모리 상태를 비운다."""
    global _active
    stop()
    for task in _workers.values():
        task.cancel()
    _workers.clear()
    _queue.clear()
    _series.clear()
    _filters.clear()
    _dirty.clear()
    _pending_spikes.clear()
    _correction_blocked.clear()
    _active = None


def install() -> None:
    """틱 스트림에 자신을 등록한다. 여러 번 불러도 한 번만 붙는다."""
    tick_capture.register_tick_listener(on_tick)


def start() -> None:
    """드레인 수퍼바이저를 띄운다. 여러 번 불러도 태스크는 하나다.

    `install()`만으로는 봉이 하나도 생기지 않는다 — on_tick은 큐에 넣기만
    하고, 큐를 비우는 `drain()`을 주기적으로 부르는 주체가 있어야 한다.
    ensure_worker와 마찬가지로 실행 중인 루프가 없으면 조용히 돌아간다.
    """
    global _supervisor
    if _supervisor is not None and not _supervisor.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _supervisor = loop.create_task(_supervise(), name="track_b_bars_supervisor")


def stop() -> None:
    """수퍼바이저를 취소하고 핸들을 비운다. 돌고 있지 않아도 안전하다."""
    global _supervisor
    task = _supervisor
    _supervisor = None
    if task is not None:
        task.cancel()


async def _supervise() -> None:
    """_DRAIN_INTERVAL_SEC마다 drain()을 돈다. 어떤 예외에도 죽지 않는다.

    수퍼바이저가 죽으면 봉 집계 전체가 소리 없이 멈춘다. 그래서 drain()의
    예외는 CRIT으로 남기고 루프를 계속한다 — 취소만 예외로 통과시킨다.
    """
    while True:
        await asyncio.sleep(_DRAIN_INTERVAL_SEC)
        try:
            drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 수퍼바이저는 절대 죽지 않는다
            log("TRACK_B_DRAIN_ERROR", level="CRIT", error=repr(exc))


def on_tick(tick: dict) -> None:
    """논블로킹. 어떤 예외도 전파하지 않는다(주문 경로 격리).

    A의 손절 판정 경로에서 트랙 B가 하는 일은 이 append 하나뿐이다.
    봉 확정·정정·지표는 전부 drain()에서, 별도 태스크로 돈다.
    """
    try:
        _queue.append(tick)
    except Exception:  # noqa: BLE001 — 관측 실패가 호출부를 흔들면 안 된다
        pass


def _spike_filter_for(ticker: str) -> SpikeFilter:
    """트랙 B 전용 인스턴스. A의 필터를 공유하면 내부 상태가 오염된다."""
    flt = _filters.get(ticker)
    if flt is None:
        flt = SpikeFilter()
        _filters[ticker] = flt
    return flt


def _minute_of(tick: dict) -> datetime | None:
    raw_ts = tick.get("source_ts") if tick.get("valid") else None
    for candidate in (raw_ts, tick.get("received_at")):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(str(candidate)).astimezone(KST)
        except (TypeError, ValueError):
            continue
    return None


def _float_at(raw, index: int) -> float | None:
    try:
        return float(raw[index])
    except (TypeError, ValueError, IndexError):
        return None


def _new_bar(when: datetime, price: float) -> dict:
    return {
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "open": price, "high": price, "low": price, "close": price,
        "volume": 0.0,
        "confirmed": False,
        "tick_count": 0,
        "spike_dropped": 0,
        "tick_derived": _new_derived(),
    }


def _new_derived() -> dict:
    return {
        "cttr": None, "askp1": None, "bidp1": None,
        "total_askp_rsqn": None, "total_bidp_rsqn": None,
        "vol_by_ccld": {},
        # 분봉 API는 OHLCV만 준다. 틱 파생값은 정정 대상이 아니다.
        "corrected": False,
    }


def _apply(bar: dict, tick: dict, price: float) -> None:
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["close"] = price
    bar["tick_count"] += 1

    qty = tick.get("qty")
    volume = float(qty) if isinstance(qty, (int, float)) else 0.0
    bar["volume"] += volume

    raw = tick.get("raw")
    if not isinstance(raw, list):
        return
    derived = bar["tick_derived"]
    if derived is None:
        # 정정이 만든 봉은 파생값이 없다(_merge_official). 그 분에 틱이 실제로
        # 도착하면 그때 구조를 만든다 — 여기서 대입만 하면 TypeError로 죽고,
        # 그 위의 OHLCV 갱신은 이미 끝나 파생값만 조용히 사라진다.
        derived = bar["tick_derived"] = _new_derived()
    for key, index in (
        ("cttr", _IDX_CTTR), ("askp1", _IDX_ASKP1), ("bidp1", _IDX_BIDP1),
        ("total_askp_rsqn", _IDX_TOTAL_ASKP), ("total_bidp_rsqn", _IDX_TOTAL_BIDP),
    ):
        value = _float_at(raw, index)
        if value is not None:
            derived[key] = value        # 봉 구간 마지막 값
    try:
        code = str(raw[_IDX_CCLD_DVSN]).strip()
    except IndexError:
        code = ""
    if code:
        derived["vol_by_ccld"][code] = derived["vol_by_ccld"].get(code, 0.0) + volume


def drain() -> None:
    """큐를 비우며 봉을 갱신하고, 바뀐 계열을 디스크에 쓴다."""
    while _queue:
        tick = _queue.popleft()
        try:
            _consume(tick)
        except Exception as exc:  # noqa: BLE001 — 개별 틱 실패 격리
            log("TRACK_B_BAR_TICK_ERROR", level="WARN", error=repr(exc))
    _flush()


def _consume(tick: dict) -> None:
    ticker = tick.get("ticker")
    if not ticker:
        return
    when = _minute_of(tick)
    if when is None:
        return
    try:
        price = float(tick["price"])
    except (KeyError, TypeError, ValueError):
        return
    if price <= 0:
        return

    key = (when.strftime("%Y%m%d"), str(ticker))
    minute = when.strftime("%H%M%S")[:4] + "00"
    _close_previous(key)
    minutes = _series.setdefault(key, {})
    bar = minutes.get(minute)

    if not _spike_filter_for(str(ticker)).is_valid(price, str(ticker)):
        if bar is not None:
            bar["spike_dropped"] += 1
        else:
            # 분의 첫 틱이 스파이크면 아직 봉이 없다. 그 가격으로 봉을 열면
            # 걸러낸 값이 시가가 된다 — 카운터만 맡아 두었다가 그 분의 첫
            # 정상 틱이 봉을 열 때 옮긴다.
            _pending_spikes[(key[0], key[1], minute)] = (
                _pending_spikes.get((key[0], key[1], minute), 0) + 1
            )
        _dirty.add(key)
        return

    if bar is None:
        bar = _new_bar(when.replace(second=0, microsecond=0), price)
        bar["spike_dropped"] = _pending_spikes.pop((key[0], key[1], minute), 0)
        minutes[minute] = bar
    _apply(bar, tick, price)
    ensure_worker(key[0], key[1])
    _dirty.add(key)


def _close_previous(key: tuple[str, str]) -> None:
    """활성 (날짜, 종목)이 바뀌면 이전 계열을 마감한다 (설계 §3.1).

    마지막으로 한 번 더 디스크에 쓴 뒤 메모리에서 지운다. /api/bars가 파일
    폴백을 갖고 있어 마감한 날도 계속 읽힌다(source="file"). 나가는 종목의
    스파이크 필터도 함께 버린다 — 어제 종가를 들고 있으면 오늘 시초의 큰
    갭이 스파이크로 걸러진다. B가 보는 것이 바로 그 큰 시초 갭이다.

    워커도 같이 취소한다. 두고 가면 1분마다 분봉 API를 부르며 방금 지운
    키를 다시 채우고, 스스로 멈추지도 못한다 — 유휴 판정이 전역 _queue를
    보는데 거기에는 새 종목의 틱이 계속 들어오기 때문이다. 2026-08-28에
    실제로 이렇게 됐다: 09:01:18에 버린 041190이 장중 내내 정정을 돌며
    트랙 B의 REST 사용량을 두 배로 만들었다.
    """
    global _active
    previous = _active
    _active = key
    if previous is None or previous == key:
        return
    _dirty.add(previous)
    _flush()
    _series.pop(previous, None)
    _filters.pop(previous[1], None)
    for pending in [k for k in _pending_spikes if (k[0], k[1]) == previous]:
        _pending_spikes.pop(pending, None)
    task = _workers.pop(previous, None)
    if task is not None:
        task.cancel()
    log("TRACK_B_SERIES_CLOSED", level="INFO", date=previous[0], ticker=previous[1])


def series(date: str, ticker: str) -> list[dict]:
    minutes = _series.get((date, ticker), {})
    return [minutes[m] for m in sorted(minutes)]


def _flush() -> None:
    while _dirty:
        date, ticker = _dirty.pop()
        rows = series(date, ticker)
        if not rows:
            continue
        path = bars_path(date, ticker)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 — 쓰기 실패가 집계를 멈추면 안 된다
            log(
                "TRACK_B_BAR_WRITE_ERROR", level="WARN",
                ticker=ticker, date=date, error=repr(exc),
            )


_CORRECT_INTERVAL_SEC = 60.0
_IDLE_STOP_SEC = 600.0
_workers: dict[tuple[str, str], asyncio.Task] = {}

def _is_complete_session(rows: list[dict]) -> bool:
    """전 거래일 세션으로 봐도 되는가 — 워밍업과 같은 판정을 쓴다.

    한때 이 자리에 별도의 봉 수 문턱이 있었다. "다시 받을 가치가 있는가"와
    "데운 셈 칠 것인가"가 다른 질문이라고 봤기 때문인데, 두 답이 갈리면
    재요청해도 끝내 못 쓰는 세션이 생긴다. ``covers_session``은 개장~마감을
    덮었는지로 판정하므로 무거래 분이 섞인 정상 세션도 한 번만 받는다.
    """
    return warmup.covers_session(rows)


def correction_block_reason(
    now: datetime, *, a_holding: bool, ws_stale: bool
) -> str | None:
    """분봉 API 호출을 막는 이유. 막지 않으면 None.

    두 창에서 막는다. 09:00~09:11은 A의 진입 창이고, A가 보유 중인데 WS가
    끊긴 구간은 A의 REST 백업이 PAPER 초당 1건 예산을 쓰고 있는 때다.

    둘이 겹치면 금지창을 먼저 답한다 — 그쪽이 먼저 막는다.
    """
    if kis_minute_bars.in_forbidden_window(now):
        return "ENTRY_WINDOW"
    if a_holding and ws_stale:
        return "A_HOLDING_WS_DOWN"
    return None


def should_correct(now: datetime, *, a_holding: bool, ws_stale: bool) -> bool:
    """분봉 API를 지금 호출해도 되는가."""
    return correction_block_reason(
        now, a_holding=a_holding, ws_stale=ws_stale
    ) is None


def _note_correction_block(key: tuple[str, str], reason: str) -> None:
    """A 보유 + WS 끊김으로 막힌 에피소드의 시작을 한 번만 남긴다.

    금지창은 남기지 않는다 — 매일 09:00~09:11에 걸리는 설계대로의 동작이라
    소음이다. 남길 값어치가 있는 것은 다른 쪽이다: 08-27 실장 이후 8거래일에서
    한 번도 성립하지 않았고(A가 매번 09:11 전에 청산했다), 그래서 이 분기가
    실제로 도는 것을 아직 아무도 본 적이 없다. 전체 39건 중 13건은 09:11
    이후까지 보유했으므로 죽은 분기는 아니다.
    """
    if reason == "ENTRY_WINDOW":
        return
    if _correction_blocked.get(key) == reason:
        return
    _correction_blocked[key] = reason
    log("TRACK_B_CORRECTION_BLOCKED", date=key[0], ticker=key[1], reason=reason)


def _clear_correction_block(key: tuple[str, str]) -> None:
    reason = _correction_blocked.pop(key, None)
    if reason:
        log("TRACK_B_CORRECTION_RESUMED", date=key[0], ticker=key[1], reason=reason)


async def ensure_warmup(
    date: str, ticker: str, prev_date: str, now: datetime | None = None
) -> bool:
    """전 거래일 봉을 디스크에 확보한다. 지표 워밍업이 이 파일을 읽는다.

    금지창(09:00~09:11)에는 부르지 않는다 — A의 진입 창을 지키는 가드가
    워밍업보다 우선한다. 트랙 B의 판정이 빨라도 09:35이라 지연 로드가 판정을
    늦추지 않는다(스펙 §6.2).

    ``date``는 워밍업을 요청한 당일 거래일(로깅용), ``prev_date``는 그 워밍업이
    가져오는 전 거래일이다 — 읽고 쓰는 파일은 ``prev_date`` 것이다. 빈 응답은
    파일로 남기지 않는다 — 남기면 다음 호출이 '이미 있다'고 보고 영영 다시
    받지 않는다.

    파일이 있다는 사실만으로 확보로 치지 않는다. ``data/bars/``는 실시간
    레코더도 같이 쓰는 저장소라, 트랙 B가 09:01에 마감한 종목의 20봉짜리
    스텁을 여기 남겨 두는 일이 흔하다(``_close_previous``). 그 쌍은 하필
    어제 추적했던, 오늘 다시 나올 가능성이 가장 높은 종목이다 — 존재만 보면
    바로 그 쌍이 영영 데워지지 않는다. 완결성은 ``_is_complete_session``
    하나가 판정한다.
    """
    now = now or datetime.now(KST)
    path = bars_path(prev_date, ticker)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = []
        if _is_complete_session(existing):
            return True
        log("TRACK_B_WARMUP_PARTIAL_REFETCH", level="INFO",
            date=date, ticker=ticker, prev_date=prev_date, bars=len(existing))
    if kis_minute_bars.in_forbidden_window(now):
        return False
    try:
        rows = await kis_minute_bars.fetch_session(prev_date, ticker)
    except Exception as exc:  # noqa: BLE001 — 워밍업 실패는 판정을 막지 않는다
        log("TRACK_B_WARMUP_FAILED", level="WARN",
            date=date, ticker=ticker, prev_date=prev_date, error=repr(exc))
        return False
    if not rows:
        log("TRACK_B_WARMUP_EMPTY", level="INFO",
            date=date, ticker=ticker, prev_date=prev_date)
        return False
    if not _is_complete_session(rows):
        log("TRACK_B_WARMUP_TRUNCATED", level="WARN",
            date=date, ticker=ticker, prev_date=prev_date, bars=len(rows))
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — 쓰기 실패도 판정을 막지 않는다
        log("TRACK_B_WARMUP_FAILED", level="WARN",
            date=date, ticker=ticker, prev_date=prev_date, error=repr(exc))
        return False
    log("TRACK_B_WARMUP_READY", level="INFO",
        date=date, ticker=ticker, prev_date=prev_date, bars=len(rows))
    return True


def _merge_official(bar: dict | None, official: dict) -> dict:
    """공식 분봉으로 OHLCV를 대체한다. 틱 파생값과 카운터는 보존한다."""
    if bar is None:
        bar = {
            "date": official["date"], "time": official["time"],
            "tick_count": 0, "spike_dropped": 0,
            # 분봉 API는 틱 파생값을 주지 않는다. 없음을 없음으로 남긴다.
            "tick_derived": None,
        }
    bar["open"] = official["open"]
    bar["high"] = official["high"]
    bar["low"] = official["low"]
    bar["close"] = official["close"]
    bar["volume"] = official["volume"]
    bar["confirmed"] = True
    return bar


def _in_progress_minute(when: datetime, date: str) -> str | None:
    """평가 시각 기준으로 아직 끝나지 않은 분. 없으면 None.

    분봉 API는 진행 중인 분도 그때까지의 부분 OHLCV로 내려준다. 그것을
    병합하면 (1) 확정 표시가 거짓이 되고 (2) 그 분의 남은 틱이 공식 거래량
    위에 다시 더해져 이중 계상이 된다. 장 마지막 분은 다음 폴링이 없어
    영구히 틀린 채로 남는다. 그래서 아예 병합하지 않는다 — 부분 공식 봉보다
    미확정이라고 정직하게 말하는 틱 집계 봉이 낫다.
    """
    if when.strftime("%Y%m%d") != date:
        return None
    return when.strftime("%H%M") + "00"


async def correct_once(
    date: str,
    ticker: str,
    *,
    now: datetime | None = None,
) -> int:
    """공식 분봉 한 페이지로 당일 봉을 정정한다. 정정한 봉 수를 돌려준다."""
    when = now or datetime.now(KST)
    a_holding = state.get().position_status == "HOLDING"
    blocked = correction_block_reason(
        when, a_holding=a_holding, ws_stale=not live.ws_connected
    )
    if blocked:
        _note_correction_block((date, ticker), blocked)
        return 0
    _clear_correction_block((date, ticker))

    try:
        response = await kis_minute_bars.fetch_minute_bars(ticker)
        official, issues = kis_minute_bars.parse_minute_bars(response)
    except kis_minute_bars.MinuteBarError as exc:
        log("TRACK_B_CORRECTION_FAILED", level="WARN", ticker=ticker, error=repr(exc))
        return 0
    except Exception as exc:  # noqa: BLE001 — 정정 실패가 집계를 멈추면 안 된다
        log("TRACK_B_CORRECTION_ERROR", level="WARN", ticker=ticker, error=repr(exc))
        return 0

    minutes = _series.setdefault((date, ticker), {})
    in_progress = _in_progress_minute(when, date)
    corrected = 0
    for row in official:
        if row["date"] != date:
            continue
        minute = row["time"][:4] + "00"
        if minute == in_progress:
            continue
        minutes[minute] = _merge_official(minutes.get(minute), {**row, "time": minute})
        corrected += 1

    if corrected:
        _dirty.add((date, ticker))
        _flush()
        log(
            "TRACK_B_BARS_CORRECTED", level="INFO",
            ticker=ticker, date=date, corrected=corrected, issues=issues,
        )
    return corrected


async def worker(date: str, ticker: str) -> None:
    """1분 주기로 봉을 확정하고 정정한다. 틱이 끊기면 스스로 끝난다."""
    idle = 0.0
    try:
        while idle < _IDLE_STOP_SEC:
            await asyncio.sleep(_CORRECT_INTERVAL_SEC)
            pending = len(_queue)
            drain()
            idle = 0.0 if pending else idle + _CORRECT_INTERVAL_SEC
            await correct_once(date, ticker)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log("TRACK_B_WORKER_DEAD", level="CRIT", ticker=ticker, error=repr(exc))
    finally:
        # 마지막 배출도 실패할 수 있다. 워커 종료 경로에서 예외를 올리면
        # 태스크가 unretrieved exception으로 남는다.
        try:
            drain()
        except Exception:  # noqa: BLE001
            pass
        _workers.pop((date, ticker), None)


def ensure_worker(date: str, ticker: str) -> None:
    """실행 중인 이벤트 루프가 있으면 워커를 지연 생성한다.

    main.py에 기동 배선을 넣지 않기 위해서다 — main.py는 _STRATEGY_FILES에
    있어 수정하면 트랙 A의 전략 지문이 돈다.
    """
    key = (date, ticker)
    task = _workers.get(key)
    if task is not None and not task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _workers[key] = loop.create_task(worker(date, ticker), name=f"track_b_bars_{ticker}")


async def restore_day(
    date: str,
    ticker: str,
    *,
    now: datetime | None = None,
) -> int:
    """장중 재시작 후 당일 봉을 공식 분봉으로 복원한다.

    봉은 인메모리 누적이라 재시작하면 그날 봉이 사라진다. MACD가 26봉을
    요구하므로(모스펙 §7.1) 복원이 없으면 재시작한 날의 B는 26분간 눈이 먼다.

    복원된 봉의 틱 파생값은 None이다 — 분봉 API가 주지 않는다. 이 값을 쓰는
    규칙을 나중에 만들면 그때 복원 구간을 신호 대상에서 제외해야 한다.
    """
    when = now or datetime.now(KST)
    a_holding = state.get().position_status == "HOLDING"
    deferred = correction_block_reason(
        when, a_holding=a_holding, ws_stale=not live.ws_connected
    )
    if deferred:
        log("TRACK_B_RESTORE_DEFERRED", level="INFO", ticker=ticker, date=date,
            reason=deferred)
        return 0

    try:
        official, issues = await kis_minute_bars.fetch_day_bars(ticker)
    except kis_minute_bars.MinuteBarError as exc:
        log("TRACK_B_RESTORE_FAILED", level="WARN", ticker=ticker, error=repr(exc))
        return 0
    except Exception as exc:  # noqa: BLE001
        log("TRACK_B_RESTORE_ERROR", level="WARN", ticker=ticker, error=repr(exc))
        return 0

    minutes = _series.setdefault((date, ticker), {})
    in_progress = _in_progress_minute(when, date)
    restored = 0
    for row in official:
        if row["date"] != date:
            continue
        minute = row["time"][:4] + "00"
        if minute == in_progress:
            continue
        minutes[minute] = _merge_official(minutes.get(minute), {**row, "time": minute})
        restored += 1

    if restored:
        _dirty.add((date, ticker))
        _flush()
        log(
            "TRACK_B_BARS_RESTORED", level="INFO",
            ticker=ticker, date=date, restored=restored, issues=issues,
        )
    return restored

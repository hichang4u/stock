"""선택·체결된 PAPER 한 종목의 재생 가능한 가격 경로를 15:20까지 durable 기록.

설계 원칙:
- 진입 체결(F3)이 확정되면 즉시 시작하고, F4는 idempotent하게 attach/resume한다.
- ``enqueue``는 논블로킹이며 예외를 절대 전파하지 않는다 — 주문·청산·F5·복구 경로를
  막거나 지연시키지 않는다. 파일/DB 실패도 마찬가지다.
- writer는 F4 청산 모니터 태스크가 소유·취소하지 않는다(이 모듈이 자체 소유).
- 시간 단위 gzip chunk로 회전하고, 재시작 시 기존 chunk를 truncate 없이 이어쓰며
  seq를 이어받아 중복·건너뜀이 없다.
- CLOSED 이후에는 가격만 기록한다(스탑·주문·VI 계산 없음 — 호출부 책임).
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import hashlib
import json
import os
from collections import deque
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from src import db
from src.schedule_times import F5_EXEC_H, F5_EXEC_M
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

WRITER_VERSION = "tick-writer-1"
# tick-schema-2: 미해석 WS 원시 필드 `raw` 추가. tick-schema-1 행에는 없다.
SCHEMA_VERSION = "tick-schema-2"

STRATEGY_TICK_DIR = os.getenv("STRATEGY_TICK_DIR", "data/strategy_ticks")
_ENABLED = os.getenv("STRATEGY_TICK_CAPTURE_ENABLED", "1") == "1"
_DRAIN_INTERVAL_SEC = 0.5


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 계획 §"가격 경로 용량과 보존": 초기 소프트 한도는 압축 후 일 100MB. 초과해도
# 기록을 버리지 않고 경고와 실제 용량을 남긴다(단계 A 종료 시 재산정).
SOFT_LIMIT_MB = max(0.0, _env_float("STRATEGY_TICK_SOFT_LIMIT_MB", 100.0))

# 캡처 종료는 연속매매가 끝나는 15:20이다. F5(15:15)에서 파생시키지 않는다 —
# F5가 15:15인 것은 시장가 매도가 마감 동시호가에 걸리지 않게 하려는 주문
# 제약이지(schedule_times.py) 관측 제약이 아니고, 관측 계층은 이미 트랙 A의
# 포지션과 분리돼 있다(모스펙 §3.3). 15:20 이후 동시호가 구간은 연속 체결이
# 없어 담지 않는다 — 그 구간 프레임이 무엇인지 실측한 바 없다.
CAPTURE_UNTIL = (15, 20)
CAPTURE_BACKUP_START = (9, 35)
# 백업은 09:35 이후에만 시작하고 15:14에 멈춘다 — F5 precheck 15:14:50 ·
# exec 15:15:00이 유량에서 항상 우선이다. 15:15~15:20은 WS로만 받는다.
CAPTURE_BACKUP_STOP = (F5_EXEC_H, F5_EXEC_M - 1)  # (15, 14)

# 캡처 경로 완전성 전용 스위치. F4_POST_CLOSE_REST_BACKUP_ENABLED(기본 0)는 차트
# 보강용 일반 사후 폴링 스위치이므로, 캡처가 그 설정을 조용히 뒤집지 않도록
# 별도 이름으로 분리한다. STRATEGY_TICK_ 접두사라 전략 지문 환경 스냅샷에 잡힌다.
REST_BACKUP_ENABLED = os.getenv("STRATEGY_TICK_REST_BACKUP_ENABLED", "1") == "1"

_VALID_REASONS = {
    "COMPLETE",
    "MANUAL_STOP",
    "PROCESS_SHUTDOWN",
    "WS_LOSS",
    "INCOMPLETE_BEFORE_1515",
    "WRITER_ERROR",
    "TARGET_SWITCHED",
}


def _hour_of(received_at: str) -> str:
    try:
        return datetime.fromisoformat(received_at).astimezone(KST).strftime("%H")
    except (TypeError, ValueError):
        return str(received_at)[11:13] or "00"


class TickCapture:
    """단일 종목 가격 경로 writer. 인스턴스는 하나의 (거래일,종목)만 담당한다."""

    def __init__(
        self,
        *,
        trade_date: str,
        ticker: str,
        trade_id: int | None,
        experiment_id: str | None,
        entry_at: str | None,
        base_dir: str | Path = STRATEGY_TICK_DIR,
        prior_finalize: Awaitable[object] | None = None,
    ) -> None:
        self.trade_date = trade_date
        self.ticker = ticker
        self.trade_id = trade_id
        self.experiment_id = experiment_id
        self.entry_at = entry_at
        self.dir = Path(base_dir) / trade_date
        self._queue: deque[dict] = deque()
        self._task: asyncio.Task | None = None
        self._initial_task: asyncio.Task | None = None
        self._resume_task: asyncio.Task | None = None
        self._resumed = False
        # gzip.open(..., "at")은 GzipFile을 감싼 텍스트 핸들을 낸다.
        self._fh: dict[str, TextIO] = {}
        self._seq = 0
        self._rows_written = 0
        self._write_errors = 0
        self._source_ts_reversals = 0
        self._ws_disconnects = 0
        self._ws_loss_before_close = False
        self._prior_interruption = False
        self._rest_backfill: list[dict] = []
        self._first_source_ts: str | None = None
        self._last_source_ts: str | None = None
        self._first_received_at: str | None = None
        self._last_received_at: str | None = None
        # 역전 검출용 마지막 정렬 기준 시각(거래소 시각 우선, 없으면 수신 시각).
        self._last_order_ts: str | None = None
        self._closed = False
        # 같은 (거래일,종목)의 이전 인스턴스가 마감 중이면 그 flush를 기다린 뒤에만
        # 복원 스캔을 돈다. 버퍼가 디스크에 없는 상태로 스캔하면 seq가 1부터 다시
        # 매겨져 같은 chunk에 중복 행이 생긴다.
        self._prior_finalize = prior_finalize

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        """배경 드레인·복원·초기 manifest를 띄운다(논블로킹).

        기존 chunk 복원은 하루치 gzip 전체를 훑으므로 절대 호출자 스레드에서 하지
        않는다. 장중 재시작에서 이 경로가 F4 스탑 감시 무장보다 앞서기 때문에,
        블로킹하면 포지션이 무방비인 채로 이벤트 루프가 멈춘다.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self._resume_task = asyncio.create_task(
                self._resume_off_loop(), name=f"tick_capture_resume_{self.ticker}"
            )
            self._task = asyncio.create_task(
                self._writer_loop(), name=f"tick_capture_{self.ticker}"
            )
            # 하드 크래시가 증거를 남기도록 초기 불완전 manifest를 비동기로 기록한다.
            self._initial_task = asyncio.create_task(
                self._safe_write_initial_manifest(),
                name=f"tick_capture_init_{self.ticker}",
            )
        except RuntimeError:
            # 실행 중 이벤트 루프가 없으면(비정상 컨텍스트) 배경 태스크 없이도
            # enqueue/finalize가 동작해야 하므로 복원만 그 자리에서 수행한다.
            self._resume_from_existing()
            self._resumed = True
            self._task = None

    async def _resume_off_loop(self) -> None:
        """복원 스캔을 워커 스레드로 넘긴다. 실패해도 캡처를 멈추지 않는다.

        같은 종목의 선행 마감이 남아 있으면 먼저 기다린다. gzip 버퍼는 handle을
        닫을 때 비워지므로, 기다리지 않으면 빈 파일을 읽고 seq를 0부터 다시 센다.
        """
        try:
            await self._await_prior_finalize()
            await asyncio.to_thread(self._resume_from_existing)
        except Exception as exc:  # noqa: BLE001 — 복원 실패가 캡처를 막지 않는다
            log(
                "TICK_CAPTURE_RESUME_ERROR",
                level="WARN",
                ticker=self.ticker,
                error=repr(exc),
            )
        finally:
            self._resumed = True

    async def _await_prior_finalize(self) -> None:
        """선행 인스턴스의 마감을 기다린다. 실패·취소는 복원을 막지 않는다."""
        prior = self._prior_finalize
        if prior is None:
            return
        self._prior_finalize = None
        try:
            await asyncio.shield(prior)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — 선행 마감 실패는 그쪽에서 이미 기록
            pass

    async def _await_resume(self) -> None:
        """복원이 끝날 때까지 기다린다(취소돼도 복원 자체는 완주시킨다).

        seq·행수 복원 전에 드레인하면 seq가 겹쳐 중복 행이 생기므로, writer 루프와
        manifest 기록은 모두 이 지점을 통과한 뒤에만 진행한다.
        """
        task = self._resume_task
        if task is None or self._resumed:
            return
        try:
            await asyncio.shield(task)
        except Exception:  # noqa: BLE001 — 복원 실패는 _resume_off_loop이 이미 기록
            pass

    def _resume_from_existing(self) -> None:
        """chunk 행에서 전체 상태(시각 경계·행수·역전·REST 구간·seq)를 복원한다.

        truncate/중복 없이 seq를 이어받고, 이전 세션이 있었으면 프로세스 중단을
        표시한다(다운타임 동안의 시각 공백은 완전 커버가 아니므로 불완전 처리).
        전체 행을 메모리에 담지 않고 한 번만 순회한다.
        """
        for row in self._iter_chunk_rows():
            if not self._prior_interruption:
                self._prior_interruption = True
            self._rows_written += 1
            seq = 0
            try:
                seq = int(row.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > self._seq:
                self._seq = seq
            source_ts = row.get("source_ts")
            received_at = row.get("received_at")
            order_ts = source_ts or received_at
            if (
                self._last_order_ts is not None
                and order_ts is not None
                and str(order_ts) < str(self._last_order_ts)
            ):
                self._source_ts_reversals += 1
            if order_ts is not None:
                self._last_order_ts = str(order_ts)
            if row.get("source") == "rest":
                self._track_rest_backfill(received_at)
            self._note_timestamps(source_ts, received_at)

    def _iter_chunk_rows(self):
        """chunk 행을 파일명(=시간) 순서로 스트리밍한다. 행은 append 순 = seq 순."""
        for gzp in sorted(self.dir.glob(f"{self.ticker}.*.jsonl.gz")):
            try:
                with gzip.open(gzp, "rt", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

    def mark_ws_disconnect(self) -> None:
        """캡처 창(15:20) 이전 WS 단절을 실제 증거로 기록한다(재연결해도 불완전)."""
        self._ws_disconnects += 1
        self._ws_loss_before_close = True

    def _track_rest_backfill(self, received_at: str | None) -> None:
        """연속된 REST 표본 구간을 [start,end] received_at 범위로 누적한다."""
        if not received_at:
            return
        if self._rest_backfill and self._rest_backfill[-1].get("_open"):
            self._rest_backfill[-1]["end"] = received_at
        else:
            self._rest_backfill.append(
                {"start": received_at, "end": received_at, "_open": True}
            )

    def enqueue(self, tick: dict) -> None:
        """논블로킹. 어떤 예외도 전파하지 않는다(주문 경로 격리)."""
        try:
            self._queue.append(dict(tick))
        except Exception:  # noqa: BLE001 — 캡처 실패가 호출부를 흔들면 안 된다
            pass

    async def _writer_loop(self) -> None:
        try:
            # 복원이 seq를 이어받기 전에 쓰면 번호가 겹친다.
            await self._await_resume()
            while not self._closed:
                await asyncio.sleep(_DRAIN_INTERVAL_SEC)
                self._drain()
        except asyncio.CancelledError:
            raise

    def _drain(self) -> None:
        while self._queue:
            row = self._queue.popleft()
            try:
                self._write_row(row)
            except Exception as exc:  # noqa: BLE001 — 개별 쓰기 실패 격리
                self._write_errors += 1
                log(
                    "TICK_CAPTURE_WRITE_ERROR",
                    level="WARN",
                    ticker=self.ticker,
                    error=repr(exc),
                )

    def _fh_for(self, hour: str) -> TextIO:
        fh = self._fh.get(hour)
        if fh is None:
            path = self.dir / f"{self.ticker}.{hour}.jsonl.gz"
            fh = gzip.open(path, "at", encoding="utf-8")  # append: no truncation
            self._fh[hour] = fh
        return fh

    def _write_row(self, tick: dict) -> None:
        self._seq += 1
        source_ts = tick.get("source_ts")
        received_at = tick.get("received_at") or source_ts or ""
        source = tick.get("source")
        # 정렬 기준은 거래소 시각 우선, 없으면(REST 등) 수신 시각. 역전은 seq가
        # 아니라 source_ts_reversals로 별도 집계한다(seq는 항상 연속).
        order_ts = source_ts or received_at
        if (
            self._last_order_ts is not None
            and order_ts
            and str(order_ts) < str(self._last_order_ts)
        ):
            self._source_ts_reversals += 1
        if order_ts:
            self._last_order_ts = str(order_ts)

        if source == "rest":
            self._track_rest_backfill(received_at)
        elif self._rest_backfill and self._rest_backfill[-1].get("_open"):
            self._rest_backfill[-1]["_open"] = False  # ws 재개 → 백필 구간 종료

        row = {
            "seq": self._seq,
            "source_ts": source_ts,
            "received_at": received_at,
            "price": tick.get("price"),
            "qty": tick.get("qty"),
            "source": source,
            "valid": tick.get("valid"),
            # 미해석 WS 원시 필드. 공식 명세로 인덱스가 확인되면 과거분까지
            # 소급 해석할 수 있다. REST 백업 틱에는 없으므로 None.
            "raw": tick.get("raw"),
        }
        fh = self._fh_for(_hour_of(str(received_at)))
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._rows_written += 1
        self._note_timestamps(source_ts, received_at)

    def _note_timestamps(self, source_ts, received_at) -> None:
        """거래소 시각과 수신 시각의 경계를 각각 독립적으로 갱신한다.

        REST 표본은 거래소 시각이 없어 ``source_ts=None``으로 들어온다. 이를 그대로
        덮어쓰면 장 마감 후 REST 백업이 마지막 행이 되는 대부분의 거래에서
        ``last_source_ts``가 NULL이 되어, 진입~15:20 커버리지 판정이 실제와 무관하게
        결측으로 보인다. 따라서 None은 경계를 지우지 않는다.
        """
        if source_ts is not None:
            if self._first_source_ts is None:
                self._first_source_ts = source_ts
            self._last_source_ts = source_ts
        if received_at:
            if self._first_received_at is None:
                self._first_received_at = received_at
            self._last_received_at = received_at

    def _rest_backfill_json(self) -> str:
        ranges = [
            {"start": r["start"], "end": r["end"]} for r in self._rest_backfill
        ]
        return json.dumps(ranges, ensure_ascii=False)

    def _seq_gaps(self, chunks: list[dict]) -> int:
        """관측된 seq 범위에서 비어 있는 번호 수(연속이면 0). 역전과는 무관하다.

        chunk마다 전체 seq 목록을 들고 있으면 manifest 한 행이 수 MB가 되어 주문
        경로와 같은 SQLite에 큰 쓰기를 만든다. 경계(first/last)와 개수만으로 같은
        값을 얻는다.
        """
        firsts = [c["first_seq"] for c in chunks if c.get("first_seq")]
        lasts = [c["last_seq"] for c in chunks if c.get("last_seq")]
        counted = sum(int(c.get("seq_count") or 0) for c in chunks)
        if not firsts or not lasts or counted <= 0:
            return 0
        span = max(lasts) - min(firsts) + 1
        return max(0, span - counted)

    async def _safe_write_initial_manifest(self) -> None:
        try:
            await self.write_initial_manifest()
        except Exception as exc:  # noqa: BLE001 — 초기 manifest 실패가 캡처를 막지 않는다
            log(
                "TICK_CAPTURE_MANIFEST_ERROR",
                level="WARN",
                ticker=self.ticker,
                error=repr(exc),
            )

    async def write_initial_manifest(self) -> dict:
        """하드 크래시 대비 증거로 초기 불완전 manifest(IN_PROGRESS)를 남긴다."""
        await self._await_resume()
        chunks = await asyncio.to_thread(self._chunk_metadata)
        return await db.upsert_price_path_manifest(
            trade_date=self.trade_date,
            ticker=self.ticker,
            trade_id=self.trade_id,
            experiment_id=self.experiment_id,
            chunks_json=json.dumps(chunks, ensure_ascii=False),
            first_source_ts=self._first_source_ts,
            last_source_ts=self._last_source_ts,
            first_received_at=self._first_received_at,
            last_received_at=self._last_received_at,
            seq_gaps=self._seq_gaps(chunks),
            source_ts_reversals=self._source_ts_reversals,
            ws_disconnects=self._ws_disconnects,
            rest_backfill_ranges_json=self._rest_backfill_json(),
            reached_expected_close=0,
            data_complete=0,
            missing_reason="IN_PROGRESS",
            writer_version=WRITER_VERSION,
            schema_version=SCHEMA_VERSION,
            content_hash=None,
            finalized_at=None,
        )

    async def finalize(self, reason: str, *, reached_expected_close: bool) -> dict:
        """루프를 멈추고 남은 큐를 비운 뒤 chunk 해시와 manifest를 확정한다."""
        if reason not in _VALID_REASONS:
            reason = "INCOMPLETE_BEFORE_1515"
        self._closed = True
        # 초기 manifest 태스크가 최종 manifest를 덮어쓰지 않도록 먼저 정리한다.
        if self._initial_task is not None and not self._initial_task.done():
            self._initial_task.cancel()
            try:
                await self._initial_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 복원이 끝나기 전에 드레인하면 seq가 겹치므로 반드시 먼저 기다린다.
        await self._await_resume()
        # 권위 있는 최종 드레인.
        self._drain()
        for fh in self._fh.values():
            try:
                fh.close()
            except OSError:
                pass
        self._fh.clear()

        chunks = await asyncio.to_thread(self._chunk_metadata)
        self._warn_if_over_soft_limit()
        content_hash = hashlib.sha256(
            "".join(c["sha256"] for c in chunks).encode("utf-8")
        ).hexdigest()

        # 리터럴 ``INCOMPLETE_BEFORE_1515``는 캡처 창이 15:20으로 옮겨진 뒤에도
        # 그대로 둔다. 뜻은 "창이 닫히기 전에 끝났다"이며, 이름을 바꾸면 같은
        # 사유가 DB에 두 문자열로 남아 과거 행과 대조가 끊긴다.
        explicit_incomplete = {
            "MANUAL_STOP",
            "PROCESS_SHUTDOWN",
            "WS_LOSS",
            "INCOMPLETE_BEFORE_1515",
            "TARGET_SWITCHED",
        }
        if self._write_errors > 0:
            data_complete = 0
            missing_reason: str | None = "WRITER_ERROR"
        elif reason in explicit_incomplete:
            data_complete = 0
            missing_reason = reason
        elif self._ws_loss_before_close:
            # 캡처 창(15:20) 이전 WS 단절은 재연결해도 불완전으로 남긴다.
            data_complete = 0
            missing_reason = "WS_LOSS"
        elif self._prior_interruption:
            # 재시작 다운타임 동안의 시각 공백은 완전 커버가 아니다.
            data_complete = 0
            missing_reason = "RESTART_GAP"
        elif reason == "COMPLETE" and reached_expected_close:
            data_complete = 1
            missing_reason = None
        else:
            data_complete = 0
            missing_reason = "INCOMPLETE_BEFORE_1515"

        now = datetime.now(KST).isoformat()
        try:
            manifest = await db.upsert_price_path_manifest(
                trade_date=self.trade_date,
                ticker=self.ticker,
                trade_id=self.trade_id,
                experiment_id=self.experiment_id,
                chunks_json=json.dumps(chunks, ensure_ascii=False),
                first_source_ts=self._first_source_ts,
                last_source_ts=self._last_source_ts,
                first_received_at=self._first_received_at,
                last_received_at=self._last_received_at,
                seq_gaps=self._seq_gaps(chunks),
                source_ts_reversals=self._source_ts_reversals,
                ws_disconnects=self._ws_disconnects,
                rest_backfill_ranges_json=self._rest_backfill_json(),
                reached_expected_close=1 if reached_expected_close else 0,
                data_complete=data_complete,
                missing_reason=missing_reason,
                writer_version=WRITER_VERSION,
                schema_version=SCHEMA_VERSION,
                content_hash=content_hash,
                finalized_at=now,
            )
        except Exception as exc:  # noqa: BLE001 — manifest 실패가 종료를 막지 않는다
            log(
                "TICK_CAPTURE_MANIFEST_ERROR",
                level="WARN",
                ticker=self.ticker,
                error=repr(exc),
            )
            return {}
        log(
            "TICK_CAPTURE_FINALIZED",
            level="INFO",
            ticker=self.ticker,
            reason=missing_reason or "COMPLETE",
            data_complete=data_complete,
            rows=self._rows_written,
            source_ts_reversals=self._source_ts_reversals,
            ws_disconnects=self._ws_disconnects,
        )
        return manifest

    def _chunk_metadata(self) -> list[dict]:
        chunks: list[dict] = []
        for gzp in sorted(self.dir.glob(f"{self.ticker}.*.jsonl.gz")):
            try:
                raw = gzp.read_bytes()
            except OSError:
                continue
            rows = 0
            uncompressed = 0
            first_seq: int | None = None
            last_seq: int | None = None
            seq_count = 0
            try:
                with gzip.open(gzp, "rt", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        rows += 1
                        uncompressed += len(line.encode("utf-8"))
                        try:
                            seq = int(json.loads(line).get("seq") or 0)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
                        if seq <= 0:
                            continue
                        seq_count += 1
                        if first_seq is None or seq < first_seq:
                            first_seq = seq
                        if last_seq is None or seq > last_seq:
                            last_seq = seq
            except OSError:
                pass
            chunks.append({
                "path": str(gzp),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_compressed": len(raw),
                "size_uncompressed": uncompressed,
                "rows": rows,
                "first_seq": first_seq,
                "last_seq": last_seq,
                "seq_count": seq_count,
            })
        return chunks

    def _day_compressed_bytes(self) -> int:
        """거래일 디렉터리 전체의 압축 후 용량(모든 종목 chunk 합)."""
        total = 0
        for gzp in self.dir.glob("*.jsonl.gz"):
            try:
                total += gzp.stat().st_size
            except OSError:
                continue
        return total

    def _warn_if_over_soft_limit(self) -> None:
        """소프트 한도 초과를 경고로만 남긴다 — 기록은 절대 버리지 않는다."""
        if SOFT_LIMIT_MB <= 0:
            return
        try:
            total = self._day_compressed_bytes()
        except Exception:  # noqa: BLE001 — 용량 집계 실패가 최종화를 막지 않는다
            return
        limit_bytes = SOFT_LIMIT_MB * 1024 * 1024
        if total <= limit_bytes:
            return
        log(
            "TICK_CAPTURE_SOFT_LIMIT_EXCEEDED",
            level="WARN",
            trade_date=self.trade_date,
            ticker=self.ticker,
            compressed_bytes=total,
            compressed_mb=round(total / (1024 * 1024), 3),
            soft_limit_mb=SOFT_LIMIT_MB,
        )


# ── 모듈 싱글턴 배선 (F3/F4/main) ────────────────────────────────────────

_capture: TickCapture | None = None


def _capture_allowed() -> bool:
    # 테스트 중에는 프로덕션 싱글턴이 배경 writer 태스크를 만들지 않는다
    # (전용 테스트는 TickCapture 클래스를 직접 구동한다). f1_filter 스냅샷과 동일 규약.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return _ENABLED and os.getenv("KIS_MODE", "PAPER") == "PAPER"


def is_active() -> bool:
    return _capture is not None


def active_ticker() -> str | None:
    return _capture.ticker if _capture is not None else None


_switch_finalizers: set[asyncio.Task] = set()
# 같은 종목으로 되돌아오는 전환(A→B→A)에서 새 인스턴스가 기다릴 선행 마감.
_pending_switch_by_ticker: dict[str, asyncio.Task] = {}
# 전환 마감 대기 상한. 종료 경로가 여기서 멈추면 DB close와 PID 해제가 함께 막혀
# 다음 기동이 스테일 PID에 걸린다. 미완성 manifest가 멈춘 종료보다 낫다.
SWITCH_DRAIN_TIMEOUT_SEC = 10.0


def _finalize_detached(cap: TickCapture, reason: str) -> None:
    """전환으로 떼어낸 캡처를 배경에서 마감한다. 진입 경로를 블로킹하지 않는다."""

    async def _run() -> None:
        try:
            await cap.finalize(reason, reached_expected_close=False)
        except Exception as exc:  # noqa: BLE001 — 마감 실패가 새 캡처를 막지 않는다
            log(
                "TICK_CAPTURE_FINALIZE_ERROR",
                level="WARN",
                ticker=cap.ticker,
                error=repr(exc),
            )

    try:
        task = asyncio.create_task(_run(), name=f"tick_capture_switch_{cap.ticker}")
    except RuntimeError:
        # 실행 중 루프가 없으면 시작 시 남긴 불완전 manifest가 증거로 남는다.
        log(
            "TICK_CAPTURE_SWITCH_FINALIZE_SKIPPED",
            level="WARN",
            ticker=cap.ticker,
            reason=reason,
        )
        return
    _switch_finalizers.add(task)
    _pending_switch_by_ticker[cap.ticker] = task
    task.add_done_callback(_switch_finalizers.discard)
    task.add_done_callback(functools.partial(_forget_pending_switch, cap.ticker))


def _forget_pending_switch(ticker: str, task: asyncio.Task) -> None:
    """더 최근 전환이 자리를 차지했으면 건드리지 않는다."""
    if _pending_switch_by_ticker.get(ticker) is task:
        _pending_switch_by_ticker.pop(ticker, None)


async def drain_switch_finalizers() -> None:
    """전환으로 예약된 마감을 모두 기다린다(프로세스 종료 정리용)."""
    while _switch_finalizers:
        await asyncio.gather(*list(_switch_finalizers), return_exceptions=True)


def start(
    trade_date: str,
    ticker: str,
    trade_id: int | None,
    experiment_id: str | None,
    entry_at: str | None,
) -> bool:
    """진입 체결 확정 직후 호출. 논블로킹·가드; 실패해도 진입을 막지 않는다."""
    global _capture
    if not _capture_allowed() or not ticker:
        return False
    try:
        if _capture is not None and _capture.ticker == ticker:
            # idempotent — 같은 종목을 재시작하면 seq와 chunk가 끊긴다.
            return True
        if _capture is not None:
            # F1이 잠근 종목과 F3 최종 선정·실제 체결 종목은 갈릴 수 있다. 낡은
            # 캡처를 붙들고 있으면 enqueue 종목 필터가 실제 매매 종목의 틱을
            # 전량 버리고, F4의 active_ticker 가드가 백업·finalize까지 막는다.
            previous = _capture
            _capture = None
            log(
                "TICK_CAPTURE_TARGET_SWITCHED",
                level="WARN",
                ticker=ticker,
                previous_ticker=previous.ticker,
                trade_id=trade_id,
            )
            _finalize_detached(previous, "TARGET_SWITCHED")
        capture = TickCapture(
            trade_date=trade_date,
            ticker=ticker,
            trade_id=trade_id,
            experiment_id=experiment_id,
            entry_at=entry_at,
            base_dir=STRATEGY_TICK_DIR,
            prior_finalize=_pending_switch_by_ticker.get(ticker),
        )
        capture.start()
        # start()가 실패하면 배경 태스크 없는 인스턴스가 싱글턴에 남아 enqueue만
        # 쌓인다. 기동에 성공한 뒤에만 배선한다.
        _capture = capture
        log("TICK_CAPTURE_STARTED", level="INFO", ticker=ticker, trade_id=trade_id)
        return True
    except Exception as exc:  # noqa: BLE001 — 캡처 시작 실패가 진입을 막지 않는다
        log("TICK_CAPTURE_START_ERROR", level="WARN", ticker=ticker, error=repr(exc))
        return False


def attach_or_resume(
    trade_date: str,
    ticker: str,
    trade_id: int | None,
    experiment_id: str | None,
    entry_at: str | None,
) -> bool:
    """F4가 idempotent하게 붙거나 재시작 후 이어쓴다."""
    return start(trade_date, ticker, trade_id, experiment_id, entry_at)


# 관측 팬아웃 — 트랙 B의 봉 집계기가 여기 붙는다. live.push_tick은 가격과
# 종목만 받아 OHLCV를 만들 수 없으므로(거래량 없음) 팬아웃은 여기에 둔다.
_tick_listeners: list = []


def register_tick_listener(fn) -> None:
    """틱 스트림 구독자를 등록한다. 중복 등록은 무시한다."""
    if fn not in _tick_listeners:
        _tick_listeners.append(fn)


def clear_tick_listeners() -> None:
    """테스트 전용 — 등록된 구독자를 모두 제거한다."""
    _tick_listeners.clear()


def enqueue(tick: dict) -> None:
    """논블로킹. 활성 캡처가 있고 종목이 일치할 때만 적재한다.

    구독자 호출을 캡처 활성 검사보다 **앞에** 둔다 — 캡처가 붙지 않은
    순간에도 트랙 B는 봉을 만들어야 한다.
    """
    for fn in _tick_listeners:
        try:
            fn(tick)
        except Exception:  # noqa: BLE001 — 구독자 실패가 캡처·주문 경로를 흔들면 안 된다
            pass
    cap = _capture
    if cap is not None and tick.get("ticker") in (None, cap.ticker):
        cap.enqueue(tick)


def mark_ws_disconnect() -> None:
    """활성 캡처에 WS 단절 증거를 기록한다(모듈 배선용 래퍼)."""
    cap = _capture
    if cap is not None:
        cap.mark_ws_disconnect()


async def finalize(reason: str, *, reached_expected_close: bool) -> None:
    """활성 캡처를 종료·최종화한다. 실패해도 예외를 올리지 않는다.

    종목 전환으로 떼어낸 캡처의 마감까지 함께 기다린다. 종료 경로는 이 함수
    하나만 부르고 곧바로 DB를 닫으므로, 여기서 기다리지 않으면 전환된 캡처의
    manifest가 IN_PROGRESS로 남는다.
    """
    global _capture
    cap = _capture
    _capture = None
    try:
        if cap is not None:
            await cap.finalize(reason, reached_expected_close=reached_expected_close)
    except Exception as exc:  # noqa: BLE001
        log(
            "TICK_CAPTURE_FINALIZE_ERROR",
            level="WARN",
            ticker=cap.ticker if cap is not None else None,
            error=repr(exc),
        )
    try:
        await asyncio.wait_for(
            drain_switch_finalizers(), timeout=SWITCH_DRAIN_TIMEOUT_SEC
        )
    except Exception as exc:  # noqa: BLE001 — 종료가 여기서 막히면 안 된다
        log("TICK_CAPTURE_SWITCH_DRAIN_TIMEOUT", level="WARN", error=repr(exc))

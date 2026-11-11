"""승인 증거·사전등록·위험 중단·예산 예약의 영속 저장소.

`data/db/trading.db`와 **다른 파일**이다. `src/db.py`는 전략 지문 대상이라 스키마를
거기에 넣으면 20건 카운터가 리셋된다. 설계 §5는 저장 위치·스키마를 DB 구현 설계에서
확정한다고 했으므로, 이 파일의 위치는 잠정이며 호출자가 경로를 명시해야 한다.

네 테이블 모두 추가 전용이다. UPDATE·DELETE는 트리거가 막는다 — 과거 판정을 덮어쓰거나
중단 기록을 지워서 자격을 되살릴 수 없다. 해제·재개는 새 행으로만 남긴다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_TABLES = ("registrations", "evidence", "halt_events", "reservation_events")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registrations (
    experiment_id TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    spec_json     TEXT NOT NULL,
    spec_hash     TEXT NOT NULL,
    locked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    verdict      TEXT NOT NULL CHECK (verdict IN (
                     'NOT_EVALUATED','COLLECTING','INSUFFICIENT','FAIL','PASS'
                 )),
    refs_json    TEXT NOT NULL,
    data_hash    TEXT,
    evaluated_at TEXT NOT NULL,
    expires_at   TEXT,
    reasons_json TEXT NOT NULL,
    detail_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halt_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event         TEXT NOT NULL CHECK (event IN ('HALT','RESUME')),
    halt_id       INTEGER,
    kind          TEXT NOT NULL,
    reason        TEXT NOT NULL,
    policy_hash   TEXT,
    trading_day   TEXT NOT NULL,
    at            TEXT NOT NULL,
    decision_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservation_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event           TEXT NOT NULL CHECK (event IN ('RESERVE','RELEASE')),
    client_order_id TEXT NOT NULL,
    amount          REAL NOT NULL,
    loss_budget     REAL NOT NULL,
    reason          TEXT NOT NULL,
    at              TEXT NOT NULL,
    UNIQUE (client_order_id, event)
);
"""


class StoreUnavailable(Exception):
    """저장소를 읽을 수 없다. 호출자는 이를 '중단 아님'이 아니라 '확인 불가'로 다룬다."""


def _immutability_triggers() -> str:
    statements = []
    for table in _TABLES:
        for action in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    return "\n".join(statements)


class Store:
    """추가 전용 SQLite 저장소. 도메인 규칙은 각 모듈이 갖고 여기는 행만 다룬다."""

    def __init__(self, conn: sqlite3.Connection, *, read_only: bool) -> None:
        self._conn = conn
        self.read_only = read_only

    @classmethod
    def open(cls, path: str | Path, *, create: bool = False) -> Store:
        """`create=False`면 읽기 전용으로 연다. 파일이 없으면 만들지 않고 실패한다.

        읽는 쪽(준비도·대시보드)이 빈 저장소를 만들어 놓으면 '중단 기록 없음'으로
        읽히므로, 생성은 기록하는 쪽만 한다.
        """
        target = Path(path)
        try:
            if create:
                target.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(target), isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(_SCHEMA + _immutability_triggers())
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                if not target.is_file():
                    raise StoreUnavailable(f"store not found: {target}")
                conn = sqlite3.connect(
                    f"{target.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
                )
                conn.row_factory = sqlite3.Row
            store = cls(conn, read_only=not create)
            store._check_schema()
            return store
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"store unreadable: {exc!r}") from exc

    def _check_schema(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or str(row["value"]) != str(SCHEMA_VERSION):
            found = None if row is None else row["value"]
            raise StoreUnavailable(f"unexpected store schema_version: {found!r}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """쓰기 잠금을 먼저 잡는다 — 예산 확인과 예약 기록 사이에 다른 기록이 끼지 못한다."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # ── 사전등록 ──────────────────────────────────────────────────────

    def insert_registration(
        self, experiment_id: str, kind: str, spec: dict[str, Any], spec_hash: str, locked_at: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO registrations (experiment_id, kind, spec_json, spec_hash, locked_at)
               VALUES (?, ?, ?, ?, ?)""",
            (experiment_id, kind, _dump(spec), spec_hash, locked_at),
        )

    def registration(self, experiment_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT * FROM registrations WHERE experiment_id=?", (experiment_id,)
        )
        return _decode(rows[0], spec_json="spec") if rows else None

    def registrations(self, kind: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM registrations WHERE kind=? ORDER BY rowid", (kind,)
        )
        return [_decode(row, spec_json="spec") for row in rows]

    # ── 증거 ─────────────────────────────────────────────────────────

    def insert_evidence(self, record: dict[str, Any]) -> int:
        cur = self._conn.execute(
            """INSERT INTO evidence
                   (kind, verdict, refs_json, data_hash, evaluated_at, expires_at,
                    reasons_json, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["kind"],
                record["verdict"],
                _dump(record["refs"]),
                record.get("data_hash"),
                record["evaluated_at"],
                record.get("expires_at"),
                _dump(list(record.get("reasons") or [])),
                _dump(record.get("detail") or {}),
            ),
        )
        return int(cur.lastrowid or 0)

    def evidence(self, kind: str) -> list[dict[str, Any]]:
        """기록 순서 그대로. 마지막 행이 그 종류의 현재 증거다."""
        rows = self._rows("SELECT * FROM evidence WHERE kind=? ORDER BY id", (kind,))
        return [
            _decode(row, refs_json="refs", reasons_json="reasons", detail_json="detail")
            for row in rows
        ]

    # ── 위험 중단 ─────────────────────────────────────────────────────

    def insert_halt_event(
        self,
        *,
        event: str,
        halt_id: int | None,
        kind: str,
        reason: str,
        policy_hash: str | None,
        trading_day: str,
        at: str,
        decision: dict[str, Any] | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO halt_events
                   (event, halt_id, kind, reason, policy_hash, trading_day, at, decision_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event, halt_id, kind, reason, policy_hash, trading_day, at, _dump(decision or {})),
        )
        return int(cur.lastrowid or 0)

    def halt_events(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM halt_events ORDER BY id")
        return [_decode(row, decision_json="decision") for row in rows]

    # ── 예산 예약 ─────────────────────────────────────────────────────

    def insert_reservation_event(
        self,
        *,
        event: str,
        client_order_id: str,
        amount: float,
        loss_budget: float,
        reason: str,
        at: str,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO reservation_events
                   (event, client_order_id, amount, loss_budget, reason, at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event, client_order_id, amount, loss_budget, reason, at),
        )
        return int(cur.lastrowid or 0)

    def reservation_events(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM reservation_events ORDER BY id")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _decode(row: dict[str, Any], **columns: str) -> dict[str, Any]:
    decoded = dict(row)
    for column, key in columns.items():
        decoded[key] = json.loads(decoded.pop(column))
    return decoded

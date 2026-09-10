"""날짜별 최초 정상 F1 스냅샷 선택기 + 원자적 완료 증거 (공통 함수).

phase5 PoC·분봉 PoC·향후 수집기가 한 규칙을 공유한다. 백테스트는 날짜별
09:00:00~09:10:59 KST의 최초 **정상** 스냅샷 하나만 대표로 사용한다. "정상"은
완료 증거(원자적 사이드카 또는 결정론적 로그 매칭), JSON·필수 필드 무결성, 파일 내
중복 종목 없음, 최소 universe coverage, 창 시각을 모두 만족한 파일이다.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

SELECTOR_VERSION = "f1-selector-1"
# 사이드카는 "쓰기가 끝까지 성공했다"는 사실만 증명하며 그 사실은 판정 규칙 버전과
# 무관하다. 정확히 일치하는 버전만 받으면 규칙을 한 번 다듬는 순간 과거 사이드카가
# 전부 SIDECAR_INVALID가 되어 분석 가능 거래일이 0으로 떨어진다. 따라서 알려진
# 버전 집합으로 받아들이고, 버전 자체는 provenance로만 기록한다.
KNOWN_SELECTOR_VERSIONS = frozenset({"f1-selector-1"})
CAPTURE_START = time(9, 0)
CAPTURE_END = time(9, 11)  # 09:00:00 <= t < 09:11:00 → 09:10:59 포함
SIDECAR_SUFFIX = ".done.json"

# 코드로 고정한 최소 universe coverage. 계획 §2는 "정상" 판정을 코드 규칙으로 먼저
# 고정하도록 요구한다 — 호출자마다 다른 값을 넘기면 같은 selector_version에서도
# 정상 거래일 집합이 달라져 재현성 계약이 깨진다.
MIN_UNIVERSE_COVERAGE = 10

SNAPSHOT_NAME_RE = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{6})\.jsonl$")

# 실제 스냅샷 행이 담는 필드 집합과 일치시켜 전량 탈락을 방지한다.
REQUIRED_SNAPSHOT_FIELDS = frozenset({
    "ticker",
    "expected_price",
    "prev_close",
    "gap_pct",
    "expected_amount",
    "vi_gap",
    "gap_allowed",
    "gap_band",
})

# 결정론적 거절 사유(평가 순서대로 우선).
REJECTION_REASONS = (
    "INVALID_FILENAME",
    "OUTSIDE_WINDOW",
    "WEEKEND",
    "MARKET_CLOSED",
    "HASH_MISMATCH",
    "SIDECAR_INVALID",
    "NO_COMPLETION_EVIDENCE",
    "INVALID_JSON",
    "MISSING_REQUIRED_FIELD",
    "DUPLICATE_TICKER",
    "BELOW_MIN_COVERAGE",
    "DUPLICATE_DATE",
)


def snapshot_hash(path: Path) -> str:
    """스냅샷 파일 바이트의 SHA-256 hex."""
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def _row_count(path: Path) -> int:
    return sum(
        1 for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    )


def parse_snapshot_time(path: Path) -> datetime | None:
    match = SNAPSHOT_NAME_RE.match(Path(path).name)
    if not match:
        return None
    try:
        naive = datetime.strptime(
            match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
    return naive.replace(tzinfo=KST)


def sidecar_path(snapshot_path: Path) -> Path:
    return Path(str(snapshot_path) + SIDECAR_SUFFIX)


def write_completion_sidecar(snapshot_path: Path) -> Path:
    """전체 JSONL 쓰기가 끝난 뒤에만 원자적으로 완료 증거를 기록한다.

    임시파일 → rename으로 부분 사이드카를 남기지 않는다. 사이드카가 존재한다는
    사실 자체가 "스냅샷 쓰기가 끝까지 성공했다"는 원자적 증거다.
    """
    snapshot_path = Path(snapshot_path)
    payload = {
        "snapshot_path": snapshot_path.name,
        "sha256": snapshot_hash(snapshot_path),
        "count": _row_count(snapshot_path),
        "completed_at": datetime.now(KST).isoformat(),
        "selector_version": SELECTOR_VERSION,
    }
    dst = sidecar_path(snapshot_path)
    tmp = Path(str(dst) + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(dst)
    return dst


def _log_completion(snapshot_path: Path, compact_date: str, log_dir: Path) -> bool:
    """결정론적 로그 폴백.

    같은 날짜 로그에서 이 파일의 ``F1_SNAPSHOT_SAVED`` 이벤트가 있고, 그 직후
    (다른 ``F1_SNAPSHOT_SAVED``가 끼지 않은 채) ``F1_DONE``이 오면 완료로 본다.
    ts 오름차순, 동률은 파일 내 등장 순서로 안정 정렬한다.
    """
    log_file = Path(log_dir) / f"{compact_date}.jsonl"
    if not log_file.exists():
        return False
    name = Path(snapshot_path).name
    seq: list[tuple[str, str, bool, int]] = []  # (event, ts, matches_file, order)
    for order, line in enumerate(log_file.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        event = row.get("event")
        if event not in ("F1_SNAPSHOT_SAVED", "F1_DONE"):
            continue
        ts = str(row.get("ts") or "")
        matches = event == "F1_SNAPSHOT_SAVED" and (
            Path(str(row.get("path") or "")).name == name
        )
        seq.append((event, ts, matches, order))
    seq.sort(key=lambda item: (item[1], item[3]))
    for i, (event, _ts, matches, _order) in enumerate(seq):
        if event == "F1_SNAPSHOT_SAVED" and matches:
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            if nxt is not None and nxt[0] == "F1_DONE":
                return True
    return False


def completion_evidence(
    snapshot_path: Path, *, log_dir: Path | None = None
) -> tuple[bool, str | None]:
    """(ok, reason). 사이드카 우선, 없으면 결정론적 로그 폴백."""
    snapshot_path = Path(snapshot_path)
    sc = sidecar_path(snapshot_path)
    if sc.exists():
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except (TypeError, json.JSONDecodeError):
            return False, "SIDECAR_INVALID"
        if not isinstance(data, dict):
            return False, "SIDECAR_INVALID"
        # sha256 무결성이 우선 — 내용 변조/부분 재작성을 구체 사유로 구분한다.
        if str(data.get("sha256")) != snapshot_hash(snapshot_path):
            return False, "HASH_MISMATCH"
        if str(data.get("snapshot_path")) != snapshot_path.name:
            return False, "SIDECAR_INVALID"
        if str(data.get("selector_version")) not in KNOWN_SELECTOR_VERSIONS:
            return False, "SIDECAR_INVALID"
        try:
            count = data.get("count")
            if count is None or int(count) != _row_count(snapshot_path):
                return False, "SIDECAR_INVALID"
        except (TypeError, ValueError):
            return False, "SIDECAR_INVALID"
        completed_at = data.get("completed_at")
        try:
            parsed = datetime.fromisoformat(str(completed_at))
        except (TypeError, ValueError):
            return False, "SIDECAR_INVALID"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return False, "SIDECAR_INVALID"  # 오프셋 없는 시각은 신규 데이터로 불허
        return True, None
    if log_dir is not None:
        compact_date = Path(snapshot_path).name[:8]
        if _log_completion(snapshot_path, compact_date, Path(log_dir)):
            return True, None
    return False, "NO_COMPLETION_EVIDENCE"


def evaluate_snapshot(
    path: Path,
    *,
    market_closed_dates: set[str],
    min_coverage: int = MIN_UNIVERSE_COVERAGE,
    log_dir: Path | None = None,
) -> dict:
    """파일 하나의 정상 여부를 판정한다. ``reason``이 None이면 파일-레벨 통과."""
    path = Path(path)
    result: dict = {"path": path, "date": None, "reason": None, "hash": None, "count": 0}

    captured_at = parse_snapshot_time(path)
    if captured_at is None:
        result["reason"] = "INVALID_FILENAME"
        return result
    compact_date = captured_at.strftime("%Y%m%d")
    result["date"] = compact_date

    local_time = captured_at.timetz().replace(tzinfo=None)
    if not (CAPTURE_START <= local_time < CAPTURE_END):
        result["reason"] = "OUTSIDE_WINDOW"
        return result
    if captured_at.weekday() >= 5:
        result["reason"] = "WEEKEND"
        return result
    if compact_date in market_closed_dates:
        result["reason"] = "MARKET_CLOSED"
        return result

    ok, reason = completion_evidence(path, log_dir=log_dir)
    if not ok:
        result["reason"] = reason
        return result

    seen: set[str] = set()
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            result["reason"] = "INVALID_JSON"
            return result
        count += 1
        if any(
            row.get(field) is None or row.get(field) == ""
            for field in REQUIRED_SNAPSHOT_FIELDS
        ):
            result["reason"] = "MISSING_REQUIRED_FIELD"
            return result
        ticker = str(row.get("ticker") or "")
        if ticker in seen:
            result["reason"] = "DUPLICATE_TICKER"
            return result
        seen.add(ticker)

    if count < min_coverage:
        result["reason"] = "BELOW_MIN_COVERAGE"
        return result

    result["count"] = count
    result["hash"] = snapshot_hash(path)
    return result


def select_earliest_normal_snapshots(
    paths: list[Path],
    *,
    market_closed_dates: set[str],
    min_coverage: int = MIN_UNIVERSE_COVERAGE,
    log_dir: Path | None = None,
) -> tuple[dict[str, Path], dict]:
    """날짜별 최초 정상 스냅샷 하나를 선택하고 결정론적 report를 반환한다."""
    reasons = {reason: 0 for reason in REJECTION_REASONS}
    selected: dict[str, Path] = {}
    selected_meta: dict[str, dict] = {}

    for path in sorted(paths, key=lambda p: Path(p).name):
        ev = evaluate_snapshot(
            path,
            market_closed_dates=market_closed_dates,
            min_coverage=min_coverage,
            log_dir=log_dir,
        )
        if ev["reason"] is not None:
            reasons[ev["reason"]] += 1
            continue
        compact_date = ev["date"]
        if compact_date in selected:
            reasons["DUPLICATE_DATE"] += 1
            continue
        selected[compact_date] = Path(path)
        selected_meta[compact_date] = {
            "date": compact_date,
            "path": str(path),
            "hash": ev["hash"],
            "count": ev["count"],
        }

    report = {
        "selector_version": SELECTOR_VERSION,
        "min_coverage": min_coverage,
        "total_files": len(paths),
        "selected_count": len(selected),
        "reasons": reasons,
        "selected": [selected_meta[key] for key in sorted(selected_meta)],
    }
    return selected, report

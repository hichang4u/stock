import json
from pathlib import Path

from scripts.replay_universe import build_document, restore_universes


def _write_log(directory: Path, date: str, events: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    (directory / f"{date}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _locked(tickers) -> dict:
    return {"event": "TARGET_LOCKED", "target_tickers": tickers}


def test_takes_the_last_nonempty_lock_of_the_day(tmp_path):
    """F2가 재시도하면 종목이 바뀐다. 실제 진입을 시도한 조합은 마지막 것이다."""
    _write_log(tmp_path, "20260910", [
        _locked(["111111", "222222"]),
        {"event": "F1_DONE", "passed": 10},
        _locked(["333333", "444444", "555555"]),
    ])

    assert restore_universes([tmp_path]) == {"20260910": ["333333", "444444", "555555"]}


def test_skips_empty_and_missing_ticker_lists(tmp_path):
    """필드가 없던 초기 로그(2026-06-29)와 빈 목록 이벤트를 건너뛴다."""
    _write_log(tmp_path, "20260629", [{"event": "TARGET_LOCKED"}])
    _write_log(tmp_path, "20260630", [_locked([])])
    _write_log(tmp_path, "20260701", [_locked([]), _locked(["777777"])])

    assert restore_universes([tmp_path]) == {"20260701": ["777777"]}


def test_ignores_unparseable_lines(tmp_path):
    """로그가 잘린 채 끝날 수 있다. 그 줄만 버리고 나머지는 살린다."""
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "20260910.jsonl").write_text(
        json.dumps(_locked(["111111"])) + "\n{ 잘린 줄\n", encoding="utf-8"
    )

    assert restore_universes([directory]) == {"20260910": ["111111"]}


def test_later_directory_wins_for_the_same_date(tmp_path):
    """운영과 개발 보관본에 같은 날짜가 있으면 나중에 준 디렉터리를 쓴다."""
    old, new = tmp_path / "old", tmp_path / "new"
    _write_log(old, "20260910", [_locked(["111111"])])
    _write_log(new, "20260910", [_locked(["999999"])])

    assert restore_universes([old, new]) == {"20260910": ["999999"]}


def test_document_records_provenance(tmp_path):
    """언제 어디서 만들었는지 남는다 — 재생성 가능한 산출물임을 드러낸다."""
    doc = build_document({"20260910": ["111111"]}, [tmp_path])

    assert doc["days"] == {"20260910": [{"rank": 1, "ticker": "111111"}]}
    assert doc["source_dirs"] == [str(tmp_path)]
    assert doc["day_count"] == 1
    assert doc["pair_count"] == 1
    assert doc["generated_at"].endswith("+09:00")

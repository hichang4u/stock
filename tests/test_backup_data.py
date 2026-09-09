"""data/ 백업의 회전·중복 방지·정합성 검증.

20260903에 `git worktree remove` 가 정션을 따라가 f1_snapshots와 backtest_bars를
통째로 지웠고 어디에도 남지 않았다. 이 스크립트는 그 사고의 대응이므로, 조용히
안 도는 것이 가장 나쁜 실패다. 테스트는 임시 디렉터리에서만 돈다 — 실제
data/ 나 ../stock_backups 를 건드리지 않는다.
"""

import sqlite3
from pathlib import Path

from scripts import backup_data


def _make_source(root: Path) -> Path:
    """백업 대상 흉내 — 일반 파일 하나, DB 하나, 그리고 WAL 부산물."""
    src = root / "data"
    (src / "f1_snapshots").mkdir(parents=True)
    (src / "f1_snapshots" / "20260909_090138.jsonl").write_text("{}", encoding="utf-8")
    db = src / "db" / "trading.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT)")
    con.execute("INSERT INTO trades (ticker) VALUES ('005930')")
    con.commit()
    con.close()
    (src / "db" / "trading.db-wal").write_text("noise", encoding="utf-8")
    (src / "db" / "trading.db-shm").write_text("noise", encoding="utf-8")
    return src


def test_has_backup_for_detects_the_same_day(tmp_path):
    (tmp_path / "data_20260909_153000").mkdir()
    assert backup_data.has_backup_for(tmp_path, "20260909") is True
    assert backup_data.has_backup_for(tmp_path, "20260910") is False


def test_has_backup_for_ignores_files_and_foreign_names(tmp_path):
    """디렉터리만 백업으로 친다. 이름이 비슷한 파일에 속으면 그날 백업을 건너뛴다."""
    (tmp_path / "data_20260909_153000").write_text("x", encoding="utf-8")
    (tmp_path / "notes_20260909").mkdir()
    assert backup_data.has_backup_for(tmp_path, "20260909") is False


def test_has_backup_for_on_missing_dest_root(tmp_path):
    """백업 루트가 아직 없으면 '오늘 백업 없음'이다 — 예외를 던지지 않는다."""
    assert backup_data.has_backup_for(tmp_path / "없음", "20260909") is False


def test_skip_if_today_does_not_create_a_second_backup(tmp_path):
    src = _make_source(tmp_path)
    dest = tmp_path / "backups"
    argv = ["--dest", str(dest), "--source", str(src), "--skip-if-today"]

    assert backup_data.main(argv) == 0
    first = sorted(p.name for p in dest.glob("data_*"))
    assert len(first) == 1

    assert backup_data.main(argv) == 0
    assert sorted(p.name for p in dest.glob("data_*")) == first


def test_backup_skips_wal_and_keeps_the_db_readable(tmp_path):
    src = _make_source(tmp_path)
    dest_root = tmp_path / "backups"
    dest = backup_data.run(dest_root, keep=10, source=src)

    assert (dest / "f1_snapshots" / "20260909_090138.jsonl").exists()
    assert not (dest / "db" / "trading.db-wal").exists()
    assert not (dest / "db" / "trading.db-shm").exists()

    con = sqlite3.connect(f"file:{dest / 'db' / 'trading.db'}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT ticker FROM trades").fetchone()[0] == "005930"
    finally:
        con.close()
    assert backup_data.verify(dest) is True


def test_rotation_keeps_the_newest_and_drops_the_rest(tmp_path):
    src = _make_source(tmp_path)
    dest_root = tmp_path / "backups"
    for stamp in ("data_20260901_150000", "data_20260902_150000",
                  "data_20260903_150000"):
        (dest_root / stamp).mkdir(parents=True)

    backup_data.run(dest_root, keep=2, source=src)

    left = sorted(p.name for p in dest_root.glob("data_*"))
    assert len(left) == 2
    assert "data_20260901_150000" not in left
    assert "data_20260902_150000" not in left

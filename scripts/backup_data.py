"""data/ 를 저장소 바깥으로 백업한다 — 읽기 전용, 되돌릴 수 없는 손실 대비.

data/ 의 대부분은 재현이 불가능하다. f1_snapshots는 09:00의 실시간 예상체결
상태라 그 시각에만 존재하고, 로그와 틱 캡처도 마찬가지다. 20260903에
`git worktree remove` 가 정션을 따라가 f1_snapshots와 backtest_bars를 통째로
지웠고 휴지통·섀도복사본 어디에도 남지 않았다. 그래서 백업 위치는 저장소
바깥이다 — 저장소를 대상으로 한 작업이 백업까지 같이 지우면 안 된다.

SQLite는 파일 복사로 뜨지 않는다. 봇이 WAL 모드로 열어둔 채 돌기 때문에
어중간한 시점의 복사본은 깨진 DB가 된다. 온라인 백업 API를 쓴다.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# 저장소 바깥. .worktrees 안이나 data/ 밑에 두면 이번 사고를 그대로 반복한다.
DEST_ROOT = ROOT.parent / "stock_backups"
KEEP = 10


def _backup_sqlite(src: Path, dst: Path) -> None:
    """열려 있는 DB도 일관된 스냅샷으로 뜬다."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def has_backup_for(dest_root: Path, date_str: str) -> bool:
    """그날 날짜의 백업 디렉터리가 이미 있는가.

    백필은 여러 번 돌려도 안전하도록 만들어졌다. 그때마다 백업을 뜨면 KEEP개
    회전이 하루 만에 다 돌아 "10일치"가 "오늘 10개"가 된다. 그래서 하루 한 번만
    뜬다.

    디렉터리만 백업으로 친다 — 이름이 비슷한 파일에 속으면 그날 백업을 통째로
    건너뛴다.
    """
    if not dest_root.is_dir():
        return False
    return any(
        p.is_dir() and p.name.startswith(f"data_{date_str}_")
        for p in dest_root.glob(f"data_{date_str}_*")
    )


def run(dest_root: Path = DEST_ROOT, keep: int = KEEP,
        source: Path = DATA) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_root / f"data_{stamp}"
    dest.mkdir(parents=True, exist_ok=False)

    copied = dbs = 0
    for src in sorted(source.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(source)
        out = dest / rel
        # -wal/-shm은 뜨지 않는다. 온라인 백업이 이미 그 내용을 반영한 본체를 만든다.
        if src.suffix in (".db-wal", ".db-shm") or src.name.endswith(("-wal", "-shm")):
            continue
        if src.suffix == ".db":
            _backup_sqlite(src, out)
            dbs += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        copied += 1

    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"백업 완료: {dest}")
    print(f"  일반 파일 {copied}개, DB {dbs}개, 합계 {size / 1024 / 1024:.1f} MB")

    olds = sorted(
        (p for p in dest_root.glob("data_*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in olds[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        print(f"  오래된 백업 제거: {old.name}")
    return dest


def verify(dest: Path) -> bool:
    """백업이 실제로 읽히는지 확인한다. 못 읽는 백업은 백업이 아니다."""
    ok = True
    for db in sorted(dest.rglob("*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    print(f"  무결성 실패: {db}")
                    ok = False
                    continue
                tables = [
                    r[0]
                    for r in con.execute(
                        "select name from sqlite_master where type='table'"
                    )
                ]
                counts = {
                    t: con.execute(f"select count(*) from {t}").fetchone()[0]
                    for t in tables
                    if not t.startswith("sqlite_")
                }
                print(f"  검증 {db.relative_to(dest)}: {counts}")
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 — 검증 실패는 조용히 넘기면 안 된다
            print(f"  검증 오류 {db}: {exc!r}")
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data/ 백업")
    parser.add_argument("--dest", default=str(DEST_ROOT))
    parser.add_argument("--keep", type=int, default=KEEP)
    parser.add_argument("--source", default=str(DATA))
    parser.add_argument(
        "--skip-if-today", action="store_true",
        help="오늘 날짜의 백업이 이미 있으면 뜨지 않는다 (백필 런처가 쓴다)",
    )
    args = parser.parse_args(argv)

    dest_root = Path(args.dest)
    today = datetime.now().strftime("%Y%m%d")
    if args.skip_if_today and has_backup_for(dest_root, today):
        print(f"오늘({today}) 백업이 이미 있습니다. 건너뜁니다: {dest_root}")
        return 0

    dest = run(dest_root, args.keep, Path(args.source))
    return 0 if verify(dest) else 1


if __name__ == "__main__":
    sys.exit(main())

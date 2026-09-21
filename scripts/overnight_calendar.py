"""C1 오버나이트 거래일 달력 — D+1을 파일 존재가 아니라 실제 거래일로 정한다.

``overnight_screen.py``가 프로브 통과 실행마다 ``data/overnight/calendar.json``을
005930 일봉으로 갱신한다(스펙 §3.4 정정, 2026-09-21). ``track_b_backfill.needed_pairs``와
``overnight_sieve``는 이 달력에서 D+1을 구한다 — 후보 파일이나 분봉 파일의 존재로 D+1을
추정하면, D+1에 후보 파일도 분봉도 없는 날(기계·API가 오후 내내 죽은 날) D+2가 조용히
D+1로 둔갑해 이틀 보유가 사전 등록 표본에 섞인다. 달력이 아직 없는 초기에는 각
소비자가 기존 휴리스틱으로 대체한다.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_calendar(root: Path) -> list[str] | None:
    """``root/data/overnight/calendar.json``을 읽는다. 없거나 읽을 수 없으면 None."""
    path = root / "data" / "overnight" / "calendar.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return [str(d) for d in data]


def next_trading_date(calendar: list[str], date: str) -> str | None:
    """``calendar``에서 ``date``보다 큰 첫 날짜(다음 거래일). 없으면 None."""
    later = sorted(d for d in calendar if d > date)
    return later[0] if later else None

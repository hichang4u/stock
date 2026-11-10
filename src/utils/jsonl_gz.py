"""틱 캡처 `*.jsonl.gz`를 gzip 멤버 단위로 복구하며 읽는다.

쓰는 쪽(`src/modules/tick_capture.py`)은 `gzip.open(path, "at")`으로 이어 쓰므로,
재시작할 때마다 파일 뒤에 새 멤버가 붙는다. 앞 멤버가 끝 표시 없이 끊겼으면(전원 차단·강제
종료) 파이썬 `gzip`은 그 멤버를 풀다가 뒤 멤버 바이트까지 압축 데이터로 읽어 `zlib.error`로
멈추고, 뒤 멤버를 통째로 잃는다(2026-11-09 09시 파일: 26,368행 가운데 24,919행.
docs/TICK_GZIP_CORRUPTION_FOLLOWUP_20261109.md).

여기서는 먼저 멤버 시작 위치를 모두 찾고, 멤버마다 자기 구간만 따로 푼다. 끊긴 멤버는
마지막으로 온전한 줄까지 쓰고 다음 멤버로 넘어간다. 멤버 시작은 gzip 머리(`1f 8b 08`)이고
첫 줄이 JSON으로 읽혀야 인정한다. 압축 데이터 안에 우연히 같은 바이트가 나와도 멤버로 보지
않기 위해서다.

읽기 전용이다. 파일을 고치지 않는다.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

_MAGIC = b"\x1f\x8b\x08"
_CHUNK = 1 << 16
_GZIP_WBITS = 16 + zlib.MAX_WBITS


def _first_line_is_json(data: bytes, start: int) -> bool:
    """`start`에서 gzip 멤버가 풀리고 첫 줄이 JSON이면 참. 빈 멤버(머리·꼬리만)도 참."""
    d = zlib.decompressobj(_GZIP_WBITS)
    out = b""
    i = start
    try:
        while i < len(data) and not d.eof and b"\n" not in out:
            out += d.decompress(data[i:i + _CHUNK])
            i += _CHUNK
    except zlib.error:
        return False
    if b"\n" in out:
        line = out.split(b"\n", 1)[0]
    elif d.eof:
        if not out:
            return True
        line = out
    else:
        return False  # 첫 줄도 다 쓰지 못하고 끊긴 멤버 — 건질 줄이 없다
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def member_starts(data: bytes) -> list[int]:
    """멤버 시작 위치. 0은 늘 넣는다(머리가 깨졌으면 그 멤버가 아무것도 내지 않을 뿐이다)."""
    starts = [0]
    i = data.find(_MAGIC, 1)
    while i != -1:
        if _first_line_is_json(data, i):
            starts.append(i)
        i = data.find(_MAGIC, i + 1)
    return starts


class GzipLines:
    """줄(끝의 개행 없음)을 차례로 낸다. 다 돈 뒤 `truncated`·`damaged_at`을 본다.

    - `truncated`: 파일 끝부분을 끝까지 풀지 못했다. 장중 기록 중인 파일이 늘 이렇다.
    - `damaged_at`: 끝 표시 없이 끊겼는데 뒤에 다른 멤버가 있었던 멤버의 시작 바이트.
      장중 기록 중에는 생기지 않는다. 끊긴 지점 뒤의 줄 일부를 잃었다는 뜻이다.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.truncated = False
        self.damaged_at: list[int] = []

    def __iter__(self) -> Iterator[str]:
        data = self.path.read_bytes()
        if not data:
            return
        starts = member_starts(data)
        bounds = list(zip(starts, starts[1:] + [len(data)], strict=True))
        for k, (start, end) in enumerate(bounds):
            is_last = k == len(bounds) - 1
            clean = yield from self._member(data, start, end)
            if clean:
                continue
            if is_last:
                self.truncated = True
            else:
                self.damaged_at.append(start)

    @staticmethod
    def _member(data: bytes, start: int, end: int) -> Generator[str, None, bool]:
        """[start, end)를 한 멤버로 푼다. 끝 표시까지 풀리고 남는 바이트가 없으면 참을 돌려준다."""
        d = zlib.decompressobj(_GZIP_WBITS)
        buf = b""
        i = start
        broken = False
        while i < end and not d.eof:
            piece = data[i:min(i + _CHUNK, end)]
            i += _CHUNK
            snap = d.copy()
            try:
                buf += d.decompress(piece)
            except zlib.error:
                # 예외가 나면 그 호출의 출력이 통째로 사라진다. 깨진 바이트 직전까지 살리려고
                # 직전 상태에서 한 바이트씩 다시 넣는다(멤버마다 한 번, 최대 64KB).
                d = snap
                for b in range(len(piece)):
                    try:
                        buf += d.decompress(piece[b:b + 1])
                    except zlib.error:
                        break
                broken = True
            if b"\n" in buf:
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    yield line.decode("utf-8", "replace")
            if broken:
                return False  # 마지막의 덜 쓴 줄(buf)은 버린다
        if not d.eof:
            return False
        if buf:
            yield buf.decode("utf-8", "replace")
        return not d.unused_data


@dataclass
class JsonlGz:
    rows: list[dict] = field(default_factory=list)
    truncated: bool = False
    damaged_at: list[int] = field(default_factory=list)


def read_jsonl_gz(path: str | Path) -> JsonlGz:
    """JSON 객체 줄만 모은다. 빈 줄·JSON이 아닌 줄·객체가 아닌 값은 건너뛴다."""
    lines = GzipLines(path)
    out = JsonlGz()
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.rows.append(row)
    out.truncated = lines.truncated
    out.damaged_at = lines.damaged_at
    return out

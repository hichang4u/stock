"""틱 캡처 jsonl.gz 복구 읽기 — 끝이 잘린 멤버와 그 뒤에 이어 쓴 멤버.

2026-11-09 09시 파일: 보유 중 전원 차단으로 첫 멤버가 끝 표시 없이 끊겼고, 재시작 뒤 같은
파일에 둘째 멤버가 붙었다. 파이썬 `gzip`은 첫 멤버를 풀다가 둘째 멤버 바이트까지 압축
데이터로 읽어 `zlib.error`로 멈췄다(docs/TICK_GZIP_CORRUPTION_FOLLOWUP_20261109.md).
"""

import gzip
import json
import zlib
from pathlib import Path

from src.utils.jsonl_gz import GzipLines, read_jsonl_gz


def rows(prefix: str, n: int) -> list[dict]:
    return [{"seq": i + 1, "tag": prefix, "pad": "x" * 40} for i in range(n)]


def body(items: list[dict]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in items).encode("utf-8")


def complete_member(items: list[dict]) -> bytes:
    return gzip.compress(body(items))


def unfinished_member(items: list[dict]) -> bytes:
    """닫지 못한 GzipFile이 디스크에 남긴 모양.

    머리 + 동기 플러시까지의 압축 데이터. 끝 표시와 꼬리(CRC·길이)가 없다.
    """
    c = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return c.compress(body(items)) + c.flush(zlib.Z_SYNC_FLUSH)


def power_loss_file(path: Path, a: list[dict], b: list[dict]) -> Path:
    """2026-11-09 모양: 끊긴 멤버 뒤에 재시작이 이어 쓴 완전한 멤버."""
    path.write_bytes(unfinished_member(a) + complete_member(b))
    return path


def test_plain_member_reads_all_rows(tmp_path):
    a = rows("a", 30)
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(complete_member(a))
    r = read_jsonl_gz(p)
    assert r.rows == a
    assert not r.truncated and r.damaged_at == []


def test_members_closed_normally_are_all_read(tmp_path):
    a, b = rows("a", 5), rows("b", 7)
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(complete_member(a) + complete_member(b))
    r = read_jsonl_gz(p)
    assert r.rows == a + b
    assert not r.truncated and r.damaged_at == []


def test_file_still_being_written_is_truncated_not_damaged(tmp_path):
    a = rows("a", 20)
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(unfinished_member(a))
    r = read_jsonl_gz(p)
    assert r.rows == a
    assert r.truncated and r.damaged_at == []


def test_member_after_power_loss_is_recovered(tmp_path):
    """python gzip은 여기서 zlib.error로 멈추고 둘째 멤버를 잃는다."""
    a, b = rows("a", 40), rows("b", 60)
    p = power_loss_file(tmp_path / "x.jsonl.gz", a, b)
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            list(fh)
        stdlib_failed = False
    except (zlib.error, EOFError, OSError):
        stdlib_failed = True
    assert stdlib_failed

    r = read_jsonl_gz(p)
    assert r.rows == a + b
    assert r.damaged_at == [0]
    assert not r.truncated


def test_member_cut_mid_block_keeps_complete_lines_only(tmp_path):
    a, b = rows("a", 200), rows("b", 50)
    cut = unfinished_member(a)
    cut = cut[: len(cut) * 2 // 3]
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(cut + complete_member(b))
    lines = list(GzipLines(p))
    parsed = [json.loads(line) for line in lines if line.strip()]  # 깨진 줄이 섞이면 여기서 터진다
    got_a = [r for r in parsed if r["tag"] == "a"]
    assert 0 < len(got_a) < len(a) and got_a == a[: len(got_a)]
    assert [r for r in parsed if r["tag"] == "b"] == b


def test_fake_gzip_header_in_damaged_bytes_is_not_a_member(tmp_path):
    a, b = rows("a", 10), rows("b", 10)
    junk = b"\x1f\x8b\x08\x00" + bytes(range(256)) * 4
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(unfinished_member(a) + junk + complete_member(b))
    r = read_jsonl_gz(p)
    assert r.rows == a + b
    assert r.damaged_at == [0]


def test_zero_filled_tail_after_power_loss_is_truncated(tmp_path):
    a = rows("a", 10)
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(unfinished_member(a) + b"\x00" * 4096)
    r = read_jsonl_gz(p)
    assert r.rows == a
    assert r.truncated and r.damaged_at == []


def test_last_line_without_newline_in_closed_member(tmp_path):
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(gzip.compress(b'{"seq": 1}\n{"seq": 2}'))
    assert read_jsonl_gz(p).rows == [{"seq": 1}, {"seq": 2}]


def test_empty_file_and_blank_or_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl.gz"
    p.write_bytes(b"")
    r = read_jsonl_gz(p)
    assert r.rows == [] and not r.truncated
    p.write_bytes(gzip.compress(b'{"seq": 1}\n\nnot json\n[1, 2]\n{"seq": 2}\n'))
    assert read_jsonl_gz(p).rows == [{"seq": 1}, {"seq": 2}]

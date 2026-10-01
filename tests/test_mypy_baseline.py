"""mypy baseline 재동결 — 저장소 규칙(`* text=auto eol=lf`)대로 LF로 써야 한다.

`write_text`는 Windows에서 `\\n`을 CRLF로 바꿔 저장한다. 2026-10-01 재동결 때
mypy-baseline.txt 105줄이 전부 CRLF로 바뀌어 손으로 되돌렸다.
"""

from collections import Counter

from scripts import mypy_baseline


def test_write_baseline_uses_lf_line_endings(tmp_path, monkeypatch):
    path = tmp_path / "mypy-baseline.txt"
    monkeypatch.setattr(mypy_baseline, "BASELINE_PATH", path)

    mypy_baseline.write_baseline(Counter({"src/a.py: msg  [code]": 2}))

    data = path.read_bytes()
    assert b"\r\n" not in data
    assert data.endswith(b"src/a.py: msg  [code]\nsrc/a.py: msg  [code]\n")


def test_written_baseline_reads_back_the_same_counts(tmp_path, monkeypatch):
    path = tmp_path / "mypy-baseline.txt"
    monkeypatch.setattr(mypy_baseline, "BASELINE_PATH", path)
    counts = Counter({"src/a.py: msg  [code]": 2, "src/b.py: 다른 메시지  [x]": 1})

    mypy_baseline.write_baseline(counts)

    assert mypy_baseline.load_baseline() == counts

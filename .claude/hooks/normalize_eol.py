r"""파일을 쓴 뒤 줄끝을 저장소 규약(.gitattributes)에 맞춘다.

`.gitattributes`가 `* text=auto eol=lf`라 워킹카피도 LF가 맞다(`.bat`/`.cmd`만
CRLF). 그런데 파이썬 `write_text`·`open(...,"w")`는 Windows에서 `\n`을 CRLF로
바꿔 저장하므로, 문서를 고칠 때마다 조용히 CRLF가 섞인다. 2026-09-29에 두 번
연속 그렇게 저장해 되돌렸다. CLAUDE.md에 규칙을 적어 뒀지만 그건 권고이고,
이 훅은 결정적이다.

PostToolUse 훅이라 파일이 이미 쓰인 뒤 돌아간다. 실패해도 절대 막지 않는다 —
줄끝 정리는 편의이지 안전장치가 아니다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# .gitattributes 의 `* text=auto eol=lf` 가 적용되는 텍스트 파일.
# `.bat`/`.cmd` 는 CRLF 로 두라고 명시돼 있으므로 뺀다.
LF_SUFFIXES = {
    ".py", ".md", ".ps1", ".json", ".txt", ".yml", ".yaml",
    ".cfg", ".ini", ".toml", ".jsonl", ".html", ".css", ".js",
}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    raw_path = ((payload.get("tool_input") or {}).get("file_path") or "").strip()
    if not raw_path:
        return 0

    path = Path(raw_path)
    if path.suffix.lower() not in LF_SUFFIXES:
        return 0

    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\r\n" not in data:
        return 0

    # BOM 은 건드리지 않는다 — .ps1 은 BOM 을 유지해야 한다.
    try:
        path.write_bytes(data.replace(b"\r\n", b"\n"))
    except OSError:
        return 0

    print(f"[eol] {path.name}: CRLF -> LF (.gitattributes 규약)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

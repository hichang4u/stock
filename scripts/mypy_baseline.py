"""mypy 결과를 baseline과 대조해 '새로 생긴' 오류만 실패로 본다.

이 저장소는 타입 검사를 받은 적이 없어 전략 파일에 오류가 100건 남아 있다.
고치려면 파일 내용이 바뀌고, 그러면 strategy_fingerprint()가 리셋되면서 REAL
전환에 필요한 '깨끗한 PAPER 청산 20건'을 처음부터 다시 쌓아야 한다. 그 비용은
전략을 실제로 손보는 시점에 함께 치르는 게 맞다.

그래서 지금 있는 오류는 동결하고, 그 뒤로 새로 생기는 것만 막는다.

줄 번호는 baseline에 넣지 않는다 — 무관한 줄 하나만 밀려도 전부 어긋난다.
파일·오류코드·메시지 조합의 개수로만 비교하므로, 같은 오류가 늘면 잡히고
줄어들면 baseline을 조일 수 있다고 알려준다.

    python scripts/mypy_baseline.py            # 대조 (새 오류 있으면 exit 1)
    python scripts/mypy_baseline.py --write    # baseline 재생성
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "mypy-baseline.txt"

# "path:12: error: 메시지  [code]" 에서 줄 번호만 떼어낸다.
_ERROR_RE = re.compile(r"^(?P<file>.+?):\d+: error: (?P<message>.*)$")


def run_mypy(python: str) -> str:
    proc = subprocess.run(
        [python, "-m", "mypy", "."],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"mypy 실행 실패 (exit {proc.returncode}):\n{proc.stderr or proc.stdout}")
    return proc.stdout


def parse(output: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in output.splitlines():
        match = _ERROR_RE.match(line.strip())
        if match:
            entry = f"{match.group('file').replace(chr(92), '/')}: {match.group('message')}"
            counts[entry] += 1
    return counts


def load_baseline() -> Counter[str]:
    if not BASELINE_PATH.exists():
        return Counter()
    counts: Counter[str] = Counter()
    for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            counts[stripped] += 1
    return counts


def write_baseline(counts: Counter[str]) -> None:
    lines = [
        "# mypy baseline — scripts/mypy_baseline.py 가 생성한다. 직접 고치지 말 것.",
        "# 줄 번호는 일부러 뺐다. 여기 있는 오류는 이미 알려진 것이고,",
        "# 새로 생기는 오류만 preflight 에서 막는다.",
        f"# 총 {sum(counts.values())}건",
        "",
    ]
    for entry in sorted(counts):
        lines.extend([entry] * counts[entry])
    BASELINE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="baseline을 현재 상태로 재생성")
    parser.add_argument("--python", default=sys.executable, help="mypy를 돌릴 인터프리터")
    args = parser.parse_args(argv)

    current = parse(run_mypy(args.python))

    if args.write:
        write_baseline(current)
        print(f"baseline 기록: {sum(current.values())}건 → {BASELINE_PATH.name}")
        return 0

    baseline = load_baseline()
    added = current - baseline
    removed = baseline - current

    if removed:
        print(f"baseline 대비 {sum(removed.values())}건이 사라졌습니다. --write 로 조이세요:")
        for entry in sorted(removed):
            print(f"  - {entry}")

    if added:
        print(f"\n새로 생긴 mypy 오류 {sum(added.values())}건:")
        for entry in sorted(added):
            print(f"  + {entry}")
        return 1

    print(f"새 mypy 오류 없음 (baseline {sum(baseline.values())}건 동결 중)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

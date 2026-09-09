"""프로세스 워치독 — Windows Task Scheduler에서 1분 간격으로 호출 (PRD §6-7).

main.py 프로세스 사망 감지 시 자동 재시작. 재기동 창은 08:00~10:00이다.

**생존은 PID 파일의 락으로 판정한다. 읽어서 판정하지 않는다.** main.py는
`msvcrt.LK_NBLCK`로 PID 파일의 0번 바이트를 잠근 채 돌기 때문에(main.py
`_try_lock_pid_file`), 살아 있는 동안 그 파일을 읽으면 PermissionError가 난다.
예전 구현은 그 실패를 '사망'으로 읽었다 — 봇이 건강할 때마다 재기동을 시도했고,
새 인스턴스는 같은 락에 막혀 PROCESS_ALREADY_RUNNING(CRIT)으로 즉시 죽었다.
1분 간격이면 자정부터 10:01까지 그 짓을 600번 한다.

PID 파일은 쓰지 않는다. main.py가 락을 잡은 뒤 스스로 쓴다.
"""

import msvcrt
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
PID_FILE = ROOT / "main.pid"
MAIN_SCRIPT = ROOT / "main.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# 재기동 창. 상한만 있으면 새벽 3시에도 봇을 띄운다.
# 하한은 main.py가 스스로 도는 시각(08:29 전후)보다 조금 앞에 둔다.
RESTART_WINDOW_START = (8, 0)
RESTART_WINDOW_END = (10, 1)


def is_bot_alive(pid_path: Path = PID_FILE) -> bool:
    """main.py가 PID 파일을 잠그고 있는가.

    잠글 수 있으면 아무도 안 잡고 있다는 뜻이므로 죽은 것이다. 잡아본 락은
    반드시 푼다 — 남겨두면 다음 기동이 자기 자신에게 막힌다.
    """
    if not pid_path.exists():
        return False
    try:
        handle = open(pid_path, "a+", encoding="utf-8")
    except OSError:
        # 열지도 못하는 파일이면 남이 쥐고 있다고 본다. 함부로 띄우지 않는다.
        return True
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        handle.close()


def should_restart_at(now: datetime) -> bool:
    return RESTART_WINDOW_START <= (now.hour, now.minute) < RESTART_WINDOW_END


def _spawn() -> int:
    python_exe = str(PYTHON) if PYTHON.exists() else sys.executable
    proc = subprocess.Popen(
        [python_exe, str(MAIN_SCRIPT)],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return proc.pid


def main(
    now: datetime | None = None,
    pid_path: Path = PID_FILE,
    spawn=None,
) -> int:
    now = now or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if is_bot_alive(pid_path):
        print(f"[{stamp}] 프로세스 정상 실행 중 (PID 파일 잠김)")
        return 0

    if not should_restart_at(now):
        print(f"[{stamp}] 재기동 창 밖 — 띄우지 않음")
        return 0

    print(f"[{stamp}] 프로세스 사망 감지 — 재시작")
    pid = (spawn or _spawn)()
    print(f"[{stamp}] 재시작 요청 완료 (PID={pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

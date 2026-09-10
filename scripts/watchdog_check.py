"""프로세스 워치독 — Windows Task Scheduler에서 1분 간격으로 호출 (PRD §6-7).

main.py 프로세스 사망 감지 시 자동 재시작. 재기동 창은 08:00~10:00이다.

**생존은 PID 파일의 락으로 판정한다. 읽어서 판정하지 않는다.** main.py는
`msvcrt.LK_NBLCK`로 PID 파일의 0번 바이트를 잠근 채 돌기 때문에(main.py
`_try_lock_pid_file`), 살아 있는 동안 그 파일을 읽으면 PermissionError가 난다.
예전 구현은 그 실패를 '사망'으로 읽었다 — 봇이 건강할 때마다 재기동을 시도했고,
새 인스턴스는 같은 락에 막혀 PROCESS_ALREADY_RUNNING(CRIT)으로 즉시 죽었다.
1분 간격이면 자정부터 10:01까지 그 짓을 600번 한다.

PID 파일은 쓰지 않는다. main.py가 락을 잡은 뒤 스스로 쓴다.

작업 스케줄러는 stdout을 버리므로 판단을 data/logs/watchdog_<날짜>.log에도
남긴다. 재기동 창 안에서만 남긴다 — 1분 간격이라 하루 종일 남기면 1,440줄이
쌓이는데, 확인할 값어치가 있는 구간은 창 안이다.
"""

import msvcrt
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(ROOT))

from src.utils import tree_role  # noqa: E402

PID_FILE = ROOT / "main.pid"
MAIN_SCRIPT = ROOT / "main.py"
LOG_DIR = ROOT / "data" / "logs"
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


def write_log(line: str, now: datetime, log_dir: Path) -> None:
    """로그 실패가 재기동을 막아서는 안 된다 — 본말이 전도된다."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"watchdog_{now.strftime('%Y%m%d')}.log"
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


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


def _default_release_check() -> tree_role.ReleaseState:
    return tree_role.check_release_state(ROOT, tree_role.read_role(ROOT))


def main(
    now: datetime | None = None,
    pid_path: Path = PID_FILE,
    spawn=None,
    log_dir: Path = LOG_DIR,
    release_check=None,
) -> int:
    now = now or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    in_window = should_restart_at(now)

    def say(text: str) -> None:
        line = f"[{stamp}] {text}"
        print(line)
        if in_window:
            write_log(line, now, log_dir)

    if is_bot_alive(pid_path):
        say("프로세스 정상 실행 중 (PID 파일 잠김)")
        return 0

    if not in_window:
        print(f"[{stamp}] 재기동 창 밖 — 띄우지 않음")
        return 0

    # 워치독은 main.py를 직접 띄운다(런처를 거치지 않는다). 그래서 릴리스
    # 상태 검사가 여기에도 있어야 한다 — 런처에만 두면 운영의 주 경로가
    # 검사를 통과하지 않는다.
    state = (release_check or _default_release_check)()
    if not state.ok:
        say(f"기동 거부 — {state.reason}: {state.detail}")
        return 0

    say("프로세스 사망 감지 — 재시작")
    pid = (spawn or _spawn)()
    say(f"재시작 요청 완료 (PID={pid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

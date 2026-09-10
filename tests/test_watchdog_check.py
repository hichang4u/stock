"""워치독의 생존 판정과 재기동 창.

1분마다 돌면서 프로세스를 띄우는 코드인데 그동안 테스트가 없었다. 판정이
뒤집히면 건강한 봇 위에 매분 새 인스턴스를 띄우려 든다 — 실제로 그랬다.
"""

import msvcrt
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import watchdog_check
from src.utils.tree_role import ReleaseState

KST = ZoneInfo("Asia/Seoul")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=KST)


def _ok_release_check():
    """이 트리가 dev 든 prod 든 상관없이 항상 통과하는 검사.

    release_check 를 넘기지 않으면 _default_release_check() 가 실제
    트리의 .stock-role 과 git 상태를 읽는다. 재기동 로직만 보는
    테스트가 그 상태에 좌우되면 안 되므로(이 트리가 나중에 prod 로
    바뀌면 이유 없이 깨진다) 여기서 명시적으로 주입한다.
    """
    return ReleaseState(True, "AT_RELEASE_TAG", "release/20260910-1")


def test_alive_when_the_pid_file_is_locked(tmp_path):
    """main.py 는 PID 파일의 0번 바이트를 잠근 채 돈다. 잠겨 있으면 살아 있다.

    이 판정이 이 스크립트의 핵심이다. 예전처럼 파일을 '읽어서' 판정하면 락
    때문에 PermissionError 가 나고, 그것을 사망으로 읽어 건강한 봇 위에 새
    인스턴스를 띄우려 든다.
    """
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    handle = open(pid_file, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        assert watchdog_check.is_bot_alive(pid_file) is True
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def test_not_alive_when_the_pid_file_is_unlocked(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    assert watchdog_check.is_bot_alive(pid_file) is False


def test_alive_check_leaves_no_lock_behind(tmp_path):
    """판정이 락을 남기면 다음 기동이 자기 자신에게 막힌다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    assert watchdog_check.is_bot_alive(pid_file) is False
    assert watchdog_check.is_bot_alive(pid_file) is False
    handle = open(pid_file, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def test_not_alive_when_the_pid_file_is_missing(tmp_path):
    assert watchdog_check.is_bot_alive(tmp_path / "없음.pid") is False


def test_restart_window_has_both_bounds():
    """상한만 있으면 새벽 3시에도 봇을 띄운다 — 예전 동작이 그랬다."""
    assert watchdog_check.should_restart_at(_at(3, 0)) is False
    assert watchdog_check.should_restart_at(_at(7, 59)) is False
    assert watchdog_check.should_restart_at(_at(8, 0)) is True
    assert watchdog_check.should_restart_at(_at(9, 30)) is True
    assert watchdog_check.should_restart_at(_at(10, 0)) is True
    assert watchdog_check.should_restart_at(_at(10, 1)) is False
    assert watchdog_check.should_restart_at(_at(15, 0)) is False


def test_main_does_not_spawn_while_the_bot_is_alive(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    calls = []
    handle = open(pid_file, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        rc = watchdog_check.main(
            now=_at(9, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999,
            log_dir=tmp_path, release_check=_ok_release_check,
        )
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()
    assert rc == 0
    assert calls == []


def test_main_spawns_when_dead_inside_the_window(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    calls = []
    rc = watchdog_check.main(
        now=_at(9, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path, release_check=_ok_release_check,
    )
    assert rc == 0
    assert calls == [1]


def test_main_does_not_spawn_outside_the_window(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    calls = []
    rc = watchdog_check.main(
        now=_at(3, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path, release_check=_ok_release_check,
    )
    assert rc == 0
    assert calls == []


def test_main_does_not_write_the_pid_file(tmp_path):
    """PID 는 main.py 가 락을 잡은 뒤 스스로 쓴다. 워치독이 쓰면 남의 것을 덮는다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    watchdog_check.main(
        now=_at(9, 0), pid_path=pid_file, spawn=lambda: 999, log_dir=tmp_path,
        release_check=_ok_release_check,
    )
    assert pid_file.read_text(encoding="utf-8").strip() == "4242"


# ---------------------------------------------------------------------------
# 로그 — 작업 스케줄러는 stdout 을 버린다. 판정이 뒤집혀 있던 것이 오늘의
# 버그였으므로, 고쳐진 판정이 실제로 무엇을 봤는지 확인할 수단이 있어야 한다.
# ---------------------------------------------------------------------------


def test_logs_the_healthy_case_inside_the_restart_window(tmp_path):
    """창 안에서는 정상이어도 남긴다 — 판정이 맞는지 아침에 확인해야 한다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    log_dir = tmp_path / "logs"
    handle = open(pid_file, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        watchdog_check.main(
            now=_at(9, 0), pid_path=pid_file, log_dir=log_dir, spawn=lambda: 999,
            release_check=_ok_release_check,
        )
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()

    written = (log_dir / "watchdog_20260910.log").read_text(encoding="utf-8")
    assert "정상 실행 중" in written


def test_does_not_log_outside_the_restart_window(tmp_path):
    """1분마다 도는 코드다. 창 밖까지 남기면 하루 1,440줄이 쌓인다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    log_dir = tmp_path / "logs"
    watchdog_check.main(
        now=_at(3, 0), pid_path=pid_file, log_dir=log_dir, spawn=lambda: 999,
        release_check=_ok_release_check,
    )
    assert not log_dir.exists() or not list(log_dir.glob("*.log"))


def test_logs_both_lines_when_it_restarts(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    log_dir = tmp_path / "logs"
    watchdog_check.main(
        now=_at(9, 0), pid_path=pid_file, log_dir=log_dir, spawn=lambda: 999,
        release_check=_ok_release_check,
    )
    written = (log_dir / "watchdog_20260910.log").read_text(encoding="utf-8")
    assert "사망 감지" in written
    assert "PID=999" in written


def test_log_appends_instead_of_overwriting(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    log_dir = tmp_path / "logs"
    for _ in range(2):
        watchdog_check.main(
            now=_at(9, 0), pid_path=pid_file, log_dir=log_dir, spawn=lambda: 999,
            release_check=_ok_release_check,
        )
    lines = (log_dir / "watchdog_20260910.log").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(lines) == 4


def test_a_log_failure_does_not_stop_the_restart(tmp_path):
    """로그를 못 써서 봇을 못 살리면 본말이 전도된다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    blocked = tmp_path / "logs"
    blocked.write_text("디렉터리가 아니라 파일이다", encoding="utf-8")

    calls = []
    rc = watchdog_check.main(
        now=_at(9, 0), pid_path=pid_file, log_dir=blocked,
        spawn=lambda: calls.append(1) or 999,
        release_check=_ok_release_check,
    )
    assert rc == 0
    assert calls == [1]


def test_main_writes_no_log_outside_the_given_dir(tmp_path):
    """로그 디렉터리를 주입하면 그 밖에는 한 줄도 쓰지 않는다.

    주입을 빠뜨린 테스트가 운영 워치독 로그에 PID=999를 남긴 적이 있다
    (2026-09-10). 실제 경로로 새는 것을 이 테스트가 막는다.
    """
    pid_file = tmp_path / "main.pid"
    log_dir = tmp_path / "logs"
    other = tmp_path / "운영로그"
    other.mkdir()

    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: 999,
        log_dir=log_dir,
        release_check=_ok_release_check,
    )

    assert list(log_dir.glob("watchdog_*.log")), "주입한 디렉터리에 로그가 없다"
    assert not list(other.iterdir()), "주입하지 않은 디렉터리에 썼다"


# ---------------------------------------------------------------------------
# 릴리스 상태 — 워치독은 main.py를 직접 띄운다(런처를 거치지 않는다). 더티한
# 운영 트리를 되살리면 트리 분리가 무의미해지므로, 재기동 직전에 검사한다.
# ---------------------------------------------------------------------------


def test_main_does_not_spawn_when_the_release_state_is_bad(tmp_path):
    """더티한 운영 트리를 워치독이 되살리면 분리가 무의미해진다."""
    pid_file = tmp_path / "main.pid"
    calls = []
    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
        release_check=lambda: ReleaseState(False, "TREE_DIRTY", "파일.txt"),
    )
    assert calls == [], "릴리스 상태가 나쁜데 프로세스를 띄웠다"
    logged = (tmp_path / f"watchdog_{_at(9, 0).strftime('%Y%m%d')}.log").read_text("utf-8")
    assert "TREE_DIRTY" in logged, "거부 사유가 로그에 없다"


def test_main_spawns_when_the_release_state_is_good(tmp_path):
    pid_file = tmp_path / "main.pid"
    calls = []
    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
        release_check=lambda: ReleaseState(True, "AT_RELEASE_TAG", "release/20260911-1"),
    )
    assert calls == [1]


def test_main_does_not_spawn_when_the_release_check_raises(tmp_path):
    """검사 자체가 터져도 무소음으로 죽으면 안 된다.

    check_release_state는 자기 git 호출의 OSError/SubprocessError만
    잡는다. 그 밖의 예외(예: 한글 로케일 윈도우에서 git 출력이
    UnicodeDecodeError를 내는 경우)가 새면 say() 호출 전에 죽어서 작업
    스케줄러가 버리는 stdout에만 남고, 워치독 로그에는 아무 흔적도
    없이 재기동 창이 그냥 닫힌다. 그래서 예외도 띄우지 않는 결정으로
    바꾸고 로그에 남겨야 한다 — "크래시하지 않는다"만 확인하는 테스트로는
    이 부분(로그에 남는다)을 놓친다.
    """
    pid_file = tmp_path / "main.pid"
    calls = []

    def _raising_release_check():
        raise RuntimeError("git blew up")

    rc = watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
        release_check=_raising_release_check,
    )
    assert rc == 0
    assert calls == [], "검사가 예외를 던졌는데 프로세스를 띄웠다"
    logged = (tmp_path / f"watchdog_{_at(9, 0).strftime('%Y%m%d')}.log").read_text("utf-8")
    assert "git blew up" in logged, "검사 실패의 흔적이 로그에 없다"


def test_default_release_check_consults_the_real_tree_role(tmp_path):
    """release_check를 안 넘기면 실제 tree_role 검사로 이어져야 한다.

    위의 다른 재기동 테스트들은 hermetic하려고 release_check를 명시
    주입한다. 그래서 기본값 배선(_default_release_check) 자체를 보는
    테스트가 따로 있어야, 그 배선이 끊겨도 나머지 테스트들이 알아채지
    못하는 사각지대가 생기지 않는다.
    """
    state = watchdog_check._default_release_check()
    assert state.reason in {
        "ROLE_MISSING",
        "ROLE_UNKNOWN",
        "DEV",
        "GIT_FAILED",
        "TREE_DIRTY",
        "NOT_AT_RELEASE_TAG",
        "AT_RELEASE_TAG",
    }

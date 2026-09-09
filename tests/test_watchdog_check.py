"""워치독의 생존 판정과 재기동 창.

1분마다 돌면서 프로세스를 띄우는 코드인데 그동안 테스트가 없었다. 판정이
뒤집히면 건강한 봇 위에 매분 새 인스턴스를 띄우려 든다 — 실제로 그랬다.
"""

import msvcrt
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import watchdog_check

KST = ZoneInfo("Asia/Seoul")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=KST)


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
            now=_at(9, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999
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
        now=_at(9, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999
    )
    assert rc == 0
    assert calls == [1]


def test_main_does_not_spawn_outside_the_window(tmp_path):
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    calls = []
    rc = watchdog_check.main(
        now=_at(3, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999
    )
    assert rc == 0
    assert calls == []


def test_main_does_not_write_the_pid_file(tmp_path):
    """PID 는 main.py 가 락을 잡은 뒤 스스로 쓴다. 워치독이 쓰면 남의 것을 덮는다."""
    pid_file = tmp_path / "main.pid"
    pid_file.write_text("4242", encoding="utf-8")
    watchdog_check.main(now=_at(9, 0), pid_path=pid_file, spawn=lambda: 999)
    assert pid_file.read_text(encoding="utf-8").strip() == "4242"

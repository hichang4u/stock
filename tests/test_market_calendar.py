from datetime import datetime

from src import market_calendar, release

# conftest의 no_late_open_days가 테스트마다 목록을 비우므로, 실제 목록은 import 시점에 잡아 둔다.
REAL_LATE_OPEN_DAYS = dict(market_calendar.LATE_OPEN_DAYS)


def test_late_open_days_are_valid_weekdays():
    assert REAL_LATE_OPEN_DAYS
    for date, reason in REAL_LATE_OPEN_DAYS.items():
        day = datetime.strptime(date, "%Y%m%d")
        assert day.strftime("%Y%m%d") == date
        assert day.weekday() < 5, date
        assert reason.strip(), date


def test_known_late_open_days(monkeypatch):
    monkeypatch.setattr(market_calendar, "LATE_OPEN_DAYS", REAL_LATE_OPEN_DAYS)
    assert market_calendar.is_late_open("20261119")
    assert market_calendar.late_open_reason("20261119")
    assert not market_calendar.is_late_open("20261120")
    assert market_calendar.late_open_reason("20261120") is None


def test_calendar_is_not_part_of_the_strategy_fingerprint():
    """날짜를 더해도 지문이 바뀌지 않게 한다. 진입을 멈추는 규칙은 main.py(지문 대상)에 있다."""
    assert "src/market_calendar.py" not in release._STRATEGY_FILES
    assert "main.py" in release._STRATEGY_FILES

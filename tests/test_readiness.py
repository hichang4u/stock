"""실전투자 준비도 점수와 REAL AND 게이트 테스트."""

from datetime import datetime, timedelta

import pytest

from src import db, readiness, release
from src.real_validation import gate as real_gate
from tests import real_validation_support as support


@pytest.fixture
async def mem(monkeypatch):
    await db.init(":memory:")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    yield
    await db.close()


def _safe_env(monkeypatch):
    values = {
        "F3_ALLOC_RATIO": "0.20",
        "FIRST_RATIO": "1.00",
        "F2_RETRY_F1_ON_FAIL": "0",
        "PAPER_FAST_PROBE": "0",
        "PAPER_FAST_HYBRID": "0",
        "F3_FINAL_QUOTE_MAX_AGE_MS": "500",
        "F4_REST_BACKUP_ENABLED": "1",
        "F4_REST_ONLY_WHEN_WS_STALE": "1",
        "DRY_RUN": "0",
        "FORCE_CATCHUP": "0",
        "REAL_CONFIG_VERIFIED": "1",
        "REAL_SMOKE_TEST_APPROVED": "1",
        "REAL_ALERT_TEST_APPROVED": "1",
        "REAL_OPERATOR_DRILL_APPROVED": "1",
        "REAL_READINESS_MIN_PAPER_TRADES": "20",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


async def test_new_strategy_fingerprint_starts_with_zero_paper_evidence(mem, monkeypatch):
    _safe_env(monkeypatch)

    report = await readiness.calculate()

    assert report["groups"][0]["earned"] == 30
    assert report["groups"][1]["earned"] == 0
    assert report["clean_paper_trades"] == 0
    assert report["percent"] == 70
    assert report["eligible_for_real"] is False


def _freeze_one_hour_after(monkeypatch, evaluated_at: str) -> None:
    """합성 증거의 평가 시각 직후로 준비도의 시계를 고정한다."""
    monkeypatch.setattr(
        readiness, "_now", lambda: datetime.fromisoformat(evaluated_at) + timedelta(hours=1)
    )


async def _twenty_clean_trades(exit_price: float = 10_100.0, pnl_pct: float = 1.0) -> None:
    for day in range(1, 21):
        date = f"202609{day:02d}"
        trade_id = await db.open_trade(date, "005930", 10_000.0, 10, "삼성전자")
        await db.close_trade(
            trade_id,
            exit_price,
            "TRAILING" if pnl_pct > 0 else "HARD_STOP",
            pnl_pct,
            0.0,
            exit_qty=10,
            high_price=10_200.0,
        )


async def test_twenty_clean_same_version_paper_trades_reach_100_but_real_stays_blocked(
    mem, monkeypatch
):
    """점수 100은 운영 안전성일 뿐이다. 수익성·경로·위험 증거가 없으면 REAL은 차단이다."""
    _safe_env(monkeypatch)
    monkeypatch.delenv(real_gate.ENV_DIR, raising=False)
    await _twenty_clean_trades()

    report = await readiness.calculate()

    assert report["clean_paper_trades"] == 20
    assert report["percent"] == 100
    assert report["operational_ready"] is True
    assert report["eligible_for_real"] is False
    assert report["blockers"] == [
        f"{name}=NOT_EVALUATED: REAL_VALIDATION_DIR_UNSET"
        for name in real_gate.COMPONENTS[1:]
    ]
    assert report["real_gate"]["components"]["operational_ready"]["passed"] is True


async def test_twenty_clean_exits_that_all_lost_do_not_qualify_for_real(mem, monkeypatch):
    """설계 §5: 정상 청산 20회가 전부 손실이면 차단."""
    _safe_env(monkeypatch)
    monkeypatch.delenv(real_gate.ENV_DIR, raising=False)
    await _twenty_clean_trades(exit_price=9_800.0, pnl_pct=-2.0)

    report = await readiness.calculate()

    assert report["clean_paper_trades"] == 20
    assert report["percent"] == 100
    assert report["operational_ready"] is True
    assert report["eligible_for_real"] is False
    assert report["real_gate"]["components"]["profitability_passed"]["verdict"] == (
        "NOT_EVALUATED"
    )


async def test_complete_evidence_is_still_blocked_until_pre_buy_enforcement_is_wired(
    mem, monkeypatch, tmp_path
):
    _safe_env(monkeypatch)
    monkeypatch.setattr(readiness, "strategy_fingerprint", lambda: support.FINGERPRINT)
    monkeypatch.setattr(release, "strategy_fingerprint", lambda: support.FINGERPRINT)
    _freeze_one_hour_after(monkeypatch, support.passing_directory(tmp_path / "rv"))
    monkeypatch.setenv(real_gate.ENV_DIR, str(tmp_path / "rv"))
    await _twenty_clean_trades()

    report = await readiness.calculate()

    assert report["percent"] == 100
    assert report["eligible_for_real"] is False
    assert report["blockers"] == [
        "risk_policy_valid=NOT_EVALUATED: RUNTIME_PRE_BUY_ENFORCEMENT_NOT_WIRED"
    ]


async def test_real_opens_only_when_all_six_conditions_pass(mem, monkeypatch, tmp_path):
    """합성 증거 + 집행 연결을 가정한 경우에만 열린다. 실제 승인 증거는 아직 없다."""
    _safe_env(monkeypatch)
    monkeypatch.setattr(readiness, "strategy_fingerprint", lambda: support.FINGERPRINT)
    monkeypatch.setattr(release, "strategy_fingerprint", lambda: support.FINGERPRINT)
    monkeypatch.setattr(real_gate, "PRE_BUY_ENFORCEMENT_WIRED", True)
    evaluated_at = support.passing_directory(tmp_path / "rv")
    _freeze_one_hour_after(monkeypatch, evaluated_at)
    monkeypatch.setenv(real_gate.ENV_DIR, str(tmp_path / "rv"))

    below_100 = await readiness.calculate()
    await _twenty_clean_trades()
    full = await readiness.calculate()
    # 같은 증거라도 유효기간(합성 30일)이 지나면 다시 닫힌다.
    monkeypatch.setattr(
        readiness, "_now", lambda: datetime.fromisoformat(evaluated_at) + timedelta(days=31)
    )
    expired = await readiness.calculate()

    assert expired["percent"] == 100
    assert expired["eligible_for_real"] is False
    assert below_100["percent"] == 70
    assert below_100["eligible_for_real"] is False
    assert below_100["real_gate"]["blockers"] == [
        "operational_ready=FAIL: 현재 코드 지문 PAPER 정상 청산 20회"
    ]
    assert full["percent"] == 100
    assert full["blockers"] == []
    assert full["eligible_for_real"] is True


def test_readiness_fallback_names_the_same_six_conditions_as_the_gate():
    assert readiness.REAL_GATE_COMPONENTS == real_gate.COMPONENTS


async def test_broken_gate_package_blocks_real_without_breaking_readiness(mem, monkeypatch):
    """게이트 쪽 결함은 REAL을 막을 뿐, 준비도 계산(과 PAPER 기동)을 죽이지 않는다."""
    _safe_env(monkeypatch)
    await _twenty_clean_trades()

    def boom(**_kwargs):
        raise RuntimeError("gate package is broken")

    monkeypatch.setattr(real_gate, "evaluate_from_environment", boom)

    report = await readiness.calculate()

    assert report["percent"] == 100
    assert report["operational_ready"] is True
    assert report["eligible_for_real"] is False
    assert report["real_gate"]["components"]["operational_ready"]["passed"] is True
    assert report["blockers"] == [
        f"{name}=NOT_EVALUATED: REAL_GATE_UNAVAILABLE" for name in real_gate.COMPONENTS[1:]
    ]


async def test_gate_failure_inside_readiness_blocks_instead_of_raising(mem, monkeypatch, tmp_path):
    _safe_env(monkeypatch)
    (tmp_path / "risk_policy.json").write_bytes(b"{broken")
    monkeypatch.setenv(real_gate.ENV_DIR, str(tmp_path))
    await _twenty_clean_trades()

    report = await readiness.calculate()

    assert report["percent"] == 100
    assert report["eligible_for_real"] is False
    assert any("RISK_POLICY_EVALUATION_ERROR" in blocker for blocker in report["blockers"])


async def test_changed_fingerprint_resets_paper_evidence_to_zero(mem, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setattr(release, "strategy_fingerprint", lambda: "old-version")
    for day in range(1, 21):
        trade_id = await db.open_trade(
            f"202610{day:02d}", "005930", 10_000.0, 10, "삼성전자"
        )
        await db.close_trade(
            trade_id,
            10_100.0,
            "TRAILING",
            1.0,
            0.0,
            exit_qty=10,
            high_price=10_200.0,
        )
    monkeypatch.setattr(readiness, "strategy_fingerprint", lambda: "new-version")

    report = await readiness.calculate()

    assert report["strategy_fingerprint"] == "new-version"
    assert report["clean_paper_trades"] == 0
    assert report["groups"][1]["earned"] == 0
    assert report["eligible_for_real"] is False


async def test_first_ratio_env_cannot_disagree_with_runtime_gate(mem, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setenv("FIRST_RATIO", "0.7")

    report = await readiness.calculate()

    check = next(
        check
        for check in report["groups"][2]["checks"]
        if check["key"] == "pyramiding_disabled"
    )
    assert check["passed"] is True
    assert check["detail"] == "runtime FIRST_RATIO=1"

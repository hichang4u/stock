"""현재 자동매매 로직 버전의 재현 가능한 지문."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# 주문 판단·상태 복구에 실제로 참여하는 파일만 넣는다. 관측 전용 모듈
# (tick_capture, f1_snapshot_selector 등)은 제외한다 — 관측 코드 수정이 지문을
# 바꾸면 새 experiment_id가 열려 40거래일 paired 수집이 매번 0부터 다시 시작한다.
# 관측 동작이 기준선 적격성에 영향을 주는 부분은 STRATEGY_TICK_ 환경 스냅샷이 잡는다.
#
# 이 지문은 저장소 파일의 바이트와 환경변수만 본다 — 설치된 패키지 버전은 보지
# 않는다. 그래서 kis_rest.py·kis_ws.py를 외부 라이브러리로 바꾸면 pip 업그레이드
# 한 번이 매매 동작을 바꾸면서 지문은 그대로 두어, 다른 코드로 만든 결과가 한
# 표본에 섞인다. 직접 구현을 유지하는 결정적 이유이고 전문은
# docs/CODING_GUIDELINES.md §1-1에 있다.
_STRATEGY_FILES = (
    "main.py",
    "src/state.py",
    "src/live.py",
    "src/db.py",
    "src/api/kis_rest.py",
    "src/api/kis_ws.py",
    "src/api/kis_notice.py",
    "src/modules/f1_filter.py",
    "src/modules/f1_selector.py",
    "src/modules/f2_lockup.py",
    "src/modules/f3_entry.py",
    "src/modules/f4_tracking.py",
    "src/modules/f5_timeout.py",
    "src/modules/exit_recovery.py",
    "src/modules/paper_fast_probe.py",
    "src/modules/vi_watch.py",
    "src/schedule_times.py",
    "src/scheduler.py",
    "src/utils/number.py",
    "src/utils/spike_filter.py",
)

# 비밀키·계좌·파일 경로는 제외하고, 선택·진입·청산 및 그 실행 결과에 영향을
# 주는 환경설정만 지문에 포함한다. 코드 기본값 변경은 _STRATEGY_FILES 해시가,
# 배포별 override 변경은 이 환경 스냅샷이 포착한다.
_STRATEGY_ENV_PREFIXES = (
    "F1_",
    "F2_",
    "F3_",
    "F4_",
    "F5_",
    "PAPER_FAST_",
    "TRAILING_SHADOW_",
    "STRATEGY_TICK_",
    "VI_",
    "BALANCE_SNAPSHOT_",
    "EXIT_RECONCILE_",
    "KIS_RATE_",
    "KIS_MAX_TRANSIENT_",
    "KIS_TRANSIENT_",
    "KIS_LOW_PRIORITY_",
)
_STRATEGY_ENV_NAMES = {
    "FORCE_CATCHUP",
}


def _strategy_environment() -> dict[str, str]:
    """현재 프로세스에 실제 로드된 비밀값 제외 전략 override를 반환한다."""
    return {
        name: value.strip()
        for name, value in sorted(os.environ.items())
        if name in _STRATEGY_ENV_NAMES
        or any(name.startswith(prefix) for prefix in _STRATEGY_ENV_PREFIXES)
    }


@lru_cache(maxsize=1)
def strategy_fingerprint() -> str:
    """주문 판단·상태 복구 코드나 유효 환경설정이 바뀌는 12자리 SHA-256."""
    digest = hashlib.sha256()
    for relative in _STRATEGY_FILES:
        path = _ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        # 목록에 선언된 전략 파일을 읽지 못한 상태에서 별도의 유효 지문을
        # 만들면 준비도 실적이 잘못 이어질 수 있으므로 기동을 fail-closed한다.
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(b"strategy-environment\0")
    digest.update(
        json.dumps(
            _strategy_environment(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\0")
    return digest.hexdigest()[:12]

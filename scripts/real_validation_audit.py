"""실전 검증 증거 저장소 감사 — 읽기 전용.

`REAL_VALIDATION_DIR` 하나를 받아 저장소·정책 파일·증거를 대조하고 JSON으로 낸다.
아무것도 쓰지 않는다(저장소는 `mode=ro`). 게이트가 매번 하지 않는 무거운 확인 — 수익성
판정을 저장된 입력 원본으로 **다시 계산**하는 것 — 을 여기서 한다. REAL 기동 전 점검용이다.

    python scripts/real_validation_audit.py --dir <디렉터리> --fingerprint <전략 지문>

이 감사가 보는 것:
- 저장소 무결성(SQLite integrity_check)과 증거 해시 사슬
- 증거마다: 잠긴 등록과의 결합, 입력 원본 해시, 판정 코드 해시가 현재와 같은지, 실행 환경
- 수익성 증거: 저장된 입력으로 재계산한 판정이 기록과 같은지
- 정책 파일의 유효성과 해시, 활성 중단, 끝나지 않은 주문 예약
- 주어진 지문·시각으로 본 게이트 판정

이 감사가 **보지 못하는 것**(결과의 `not_covered`에도 적힌다):
- 저장된 입력 원본이 실제 거래 DB와 같은지. 입력까지 지어낸 증거는 여기서 걸리지 않는다
- 저장소 전체가 통째로 바뀌었는지. 판정 때 git에 남긴 사슬 머리 해시와 사람이 대조해야 한다
- 파이썬 인터프리터·표준 라이브러리·운영체제 바이너리. 실행 환경은 기록만 하고, 달라졌을 때
  결과가 같은지는 재계산으로 간접 확인한다
- 판정 파일이 아닌 배선 코드(`store.py`·`gate.py`·`evidence.py`)의 변경
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.real_validation import (  # noqa: E402
    evidence,
    execution_profile,
    gate,
    profitability,
    risk_halt,
    sizing,
)
from src.real_validation.core import Finding, Verdict, canonical_hash  # noqa: E402
from src.real_validation.costs import CostPolicy  # noqa: E402
from src.real_validation.risk_policy import RiskPolicy  # noqa: E402
from src.real_validation.store import Store, StoreUnavailable  # noqa: E402

NOT_COVERED = (
    "저장된 입력 원본과 실제 거래 DB의 일치 여부",
    "저장소 전체 교체 — git에 남긴 사슬 머리 해시와의 대조는 사람이 한다",
    "파이썬 인터프리터·표준 라이브러리·운영체제 바이너리(실행 환경은 기록만 한다)",
    "배선 코드(store.py, gate.py, evidence.py)와 주문 직전 계산 코드의 변경",
    "실제 PASS 증거 — 지금까지 실제 판정은 한 건도 없다",
)

_PACKAGE = Path(evidence.__file__).resolve().parent


def in_repo_imports(kind: str) -> list[str]:
    """그 종류의 판정 파일이 임포트하는 저장소 안 모듈. 판정 코드 해시가 덮는 범위를 보여준다."""
    found: set[str] = set()
    for name in evidence.RULE_FILES[kind]:
        tree = ast.parse((_PACKAGE / name).read_bytes().decode("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src"):
                for alias in node.names:
                    candidate = f"{node.module}.{alias.name}"
                    path = ROOT.joinpath(*candidate.split("."))
                    is_module = path.with_suffix(".py").is_file() or path.is_dir()
                    found.add(candidate if is_module else str(node.module))
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("src"))
    return sorted(found)


def code_scope() -> dict[str, Any]:
    scope: dict[str, Any] = {}
    for kind, files in evidence.RULE_FILES.items():
        hashed = {f"src.real_validation.{Path(name).stem}" for name in files}
        imports = in_repo_imports(kind)
        scope[kind] = {
            "hashed_files": list(files),
            "rules_code_hash": evidence.rules_code_hash(kind),
            "in_repo_imports": imports,
            "imported_but_not_hashed": [m for m in imports if m not in hashed],
        }
    return scope


def _file_state(directory: Path) -> dict[str, Any]:
    state: dict[str, Any] = {}
    loaders: dict[str, Any] = {
        gate.RISK_POLICY_FILE: lambda raw: (
            RiskPolicy.from_dict(raw).policy_hash(), RiskPolicy.from_dict(raw).problems()
        ),
        gate.COST_POLICY_FILE: lambda raw: (
            CostPolicy.from_dict(raw).policy_hash(), CostPolicy.from_dict(raw).problems()
        ),
        gate.EXECUTION_PROFILE_FILE: lambda raw: (
            execution_profile.ExecutionProfile.from_dict(raw).profile_hash(),
            execution_profile.ExecutionProfile.from_dict(raw).problems(),
        ),
        gate.FILL_MODEL_FILE: lambda raw: (canonical_hash(raw), []),
    }
    for name, load in loaders.items():
        path = directory / name
        if not path.is_file():
            state[name] = {"present": False}
            continue
        try:
            digest, problems = load(json.loads(path.read_bytes().decode("utf-8")))
            state[name] = {"present": True, "hash": digest, "problems": problems}
        except Exception as exc:
            state[name] = {"present": True, "error": repr(exc)}
    return state


def _integrity(store_path: Path) -> str:
    conn = sqlite3.connect(f"{store_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _finding(finding: Finding) -> dict[str, Any]:
    return {"verdict": finding.verdict.value, "reasons": list(finding.reasons)}


def audit(directory: Path, *, fingerprint: str, now: datetime) -> dict[str, Any]:
    """디렉터리 하나를 감사한다. `ok`는 아래 확인이 전부 깨끗할 때만 True다."""
    report: dict[str, Any] = {
        "directory": str(directory),
        "audited_at": now.isoformat(),
        "runtime": evidence.runtime_info(),
        "evaluation_rule_version": evidence.EVALUATION_RULE_VERSION,
        "pre_buy_enforcement_wired": gate.PRE_BUY_ENFORCEMENT_WIRED,
        "code_scope": code_scope(),
        "files": _file_state(directory),
        "not_covered": list(NOT_COVERED),
    }
    problems: list[str] = []
    store_path = directory / gate.STORE_FILE
    try:
        report["integrity_check"] = _integrity(store_path) if store_path.is_file() else "MISSING"
        with Store.open(store_path) as store:
            chain = evidence.verify_chain(store)
            rows = store.evidence()
            evidence_report = []
            for row in rows:
                experiment_id = str(row["refs"].get("experiment_id") or "")
                registration = store.registration(experiment_id)
                not_before = None
                if row["kind"] == evidence.KIND_PROFITABILITY and registration is not None:
                    not_before = registration["spec"].get("judgment_date")
                binding = evidence.check_binding(row, registration, not_before=not_before)
                current_rules = evidence.rules_code_hash(row["kind"])
                item: dict[str, Any] = {
                    "id": row["id"],
                    "kind": row["kind"],
                    "verdict": row["verdict"],
                    "experiment_id": experiment_id,
                    "evaluated_at": row["evaluated_at"],
                    "expires_at": row["expires_at"],
                    "row_hash": row["row_hash"],
                    "binding": _finding(binding),
                    "rules_code_current": row["refs"].get("rules_code_hash") == current_rules,
                    "recorded_runtime": row["detail"].get("runtime"),
                    "runtime_same": row["detail"].get("runtime") == report["runtime"],
                    "rejudged_from": row["detail"].get("rejudged_from"),
                }
                if row["kind"] == evidence.KIND_PROFITABILITY:
                    item["recomputed"] = _finding(profitability.audit(store, row["id"]))
                    if item["recomputed"]["verdict"] == Verdict.FAIL.value:
                        problems.append(f"evidence {row['id']}: recomputed verdict differs")
                if not binding.passed:
                    problems.append(f"evidence {row['id']}: {','.join(binding.reasons)}")
                evidence_report.append(item)
            report["chain"] = {"problems": chain, "head": evidence.chain_head(store)}
            report["evidence"] = evidence_report
            report["registrations"] = [
                {
                    "experiment_id": r["experiment_id"],
                    "kind": r["kind"],
                    "locked_at": r["locked_at"],
                    "spec_hash_matches_content": canonical_hash(r["spec"]) == r["spec_hash"],
                }
                for kind in (profitability.REGISTRATION_KIND, execution_profile.REGISTRATION_KIND)
                for r in store.registrations(kind)
            ]
            report["active_halts"] = [
                {k: h[k] for k in ("id", "kind", "reason", "trading_day", "at")}
                for h in risk_halt.active_halts(store)
            ]
            report["unsettled_orders"] = {
                order_id: {
                    "pending_amount": state.pending_amount,
                    "exposure_amount": state.exposure_amount,
                    "loss_in_use": state.loss_in_use,
                    "acked": state.acked_at is not None,
                }
                for order_id, state in sizing.orders(store).items()
                if not state.settled
            }
            problems.extend(chain)
            problems.extend(
                f"registration {r['experiment_id']}: spec altered"
                for r in report["registrations"]
                if not r["spec_hash_matches_content"]
            )
    except (StoreUnavailable, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        report["store_error"] = repr(exc)
        problems.append(f"store unreadable: {exc!r}")
    if report.get("integrity_check") not in ("ok",):
        problems.append(f"integrity_check: {report.get('integrity_check')}")

    decision = gate.evaluate_from_environment(
        strategy_fingerprint=fingerprint,
        operational_ready=Finding(Verdict.NOT_EVALUATED, ("NOT_PART_OF_THIS_AUDIT",)),
        now=now,
        env={gate.ENV_DIR: str(directory)},
    )
    report["gate"] = decision.as_dict()
    report["exits_permitted_whatever_the_gate_says"] = {
        action.value: gate.permits(decision, action)
        for action in (
            risk_halt.Action.PROTECTIVE_SELL, risk_halt.Action.CANCEL, risk_halt.Action.RECONCILE
        )
    }
    report["problems"] = problems
    report["ok"] = not problems
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--now", help="오프셋 있는 ISO 시각. 없으면 현재")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    report = audit(args.dir, fingerprint=args.fingerprint, now=now)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

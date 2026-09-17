"""H2 재료 공시 라벨 — 순수 함수만 검사한다. 네트워크는 부르지 않는다.

라벨 정의는 docs/superpowers/specs/2026-09-17-h2-catalyst-disclosure-hypothesis.md §2.
"""

from scripts.catalyst_label import (
    classify_report,
    flow_from_investor_rows,
    label_pair,
    label_window,
)


# ── §2.1 보고서명 → MATERIAL / OTHER ─────────────────────────────────────


def test_supply_contract_is_material():
    assert classify_report("단일판매ㆍ공급계약체결") == "MATERIAL"


def test_correction_prefix_is_stripped_before_matching():
    """정정공시는 원본 유형을 따른다."""
    assert classify_report("[기재정정]영업(잠정)실적(공정공시)") == "MATERIAL"
    assert classify_report("[첨부정정]주주총회소집결의") == "OTHER"


def test_voluntary_other_business_matters_is_other():
    """특허·임상이 '기타경영사항(자율공시)'로 오면 본문을 열지 않으므로 OTHER다."""
    assert classify_report("기타경영사항(자율공시)") == "OTHER"


def test_convertible_bond_is_other():
    """전환사채는 화이트리스트에 없다 — 호재·악재가 섞여 있어 넣지 않았다."""
    assert classify_report("전환사채권발행결정") == "OTHER"


# ── §2.1 쌍 라벨 ──────────────────────────────────────────────────────────


def test_pair_with_no_disclosure_is_none():
    assert label_pair([]) == {"label": "NONE", "matched_report_nm": [], "report_nm": [], "disclosure_count": 0}


def test_pair_is_material_if_any_report_matches():
    result = label_pair(["주주총회소집결의", "무상증자결정", "임원ㆍ주요주주특정증권등소유상황보고서"])
    assert result["label"] == "MATERIAL"
    assert result["matched_report_nm"] == ["무상증자결정"]
    assert result["disclosure_count"] == 3


def test_pair_with_only_routine_reports_is_other():
    assert label_pair(["주주총회소집결의"])["label"] == "OTHER"


# ── §2.1 시간 창: 직전 거래일 ~ D의 전날(달력일) ──────────────────────────


TRADING_DATES = ["20260904", "20260908", "20260909", "20260910", "20260911", "20260914"]


def test_window_on_a_weekday_is_the_previous_trading_day_only():
    assert label_window("20260909", TRADING_DATES) == ("20260908", "20260908")


def test_window_on_monday_spans_friday_through_sunday():
    assert label_window("20260914", TRADING_DATES) == ("20260911", "20260913")


def test_window_after_a_holiday_spans_the_holiday():
    """9/7 휴장: 9/8의 창은 직전 거래일 9/4부터 9/7까지다."""
    assert label_window("20260908", TRADING_DATES) == ("20260904", "20260907")


def test_window_without_a_previous_trading_day_is_none():
    assert label_window("20260904", TRADING_DATES) is None


# ── §2.2 수급 라벨 (보고용) ───────────────────────────────────────────────


INVESTOR_ROWS = [
    {"stck_bsop_date": "20260910", "frgn_ntby_qty": "-1000", "orgn_ntby_qty": "300"},
    {"stck_bsop_date": "20260909", "frgn_ntby_qty": "500", "orgn_ntby_qty": "-200"},
]


def test_flow_is_positive_when_foreign_plus_institution_net_buy_is_positive():
    assert flow_from_investor_rows(INVESTOR_ROWS, "20260909") == (True, "KIS")


def test_flow_is_negative_when_the_sum_is_not_positive():
    assert flow_from_investor_rows(INVESTOR_ROWS, "20260910") == (False, "KIS")


def test_flow_outside_the_api_window_is_null_not_zero():
    """소급 범위 밖은 null로 남기고 사유를 적는다 — 0으로 채우지 않는다."""
    assert flow_from_investor_rows(INVESTOR_ROWS, "20260801") == (None, "OUT_OF_RANGE")


# ── corp_code 조회: 우선주는 보통주의 corp_code를 쓴다 ──────────────────────


def test_preferred_share_falls_back_to_the_common_stock_corp_code():
    from scripts.catalyst_label import corp_code_for

    mapping = {"005930": "00126380"}
    assert corp_code_for(mapping, "005935") == "00126380"
    assert corp_code_for(mapping, "005930") == "00126380"
    assert corp_code_for(mapping, "999999") is None


def test_pair_label_keeps_every_report_name_for_the_record():
    result = label_pair(["주주총회소집결의", "무상증자결정"])
    assert result["report_nm"] == ["주주총회소집결의", "무상증자결정"]

"""KIS 실시간 체결통보 — 복호화·해석·주문별 장부.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md
"""

import asyncio
import json
from base64 import b64encode

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from src.api import kis_notice
from src.api.kis_notice import FIELDS, Notice, decrypt, parse, split_records

KEY = "0123456789abcdef0123456789abcdef"   # 32바이트 → AES256
IV = "abcdef9876543210"


def _encrypt(text: str) -> str:
    cipher = AES.new(KEY.encode(), AES.MODE_CBC, IV.encode())
    return b64encode(cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))).decode()


def _fields(order="0000001226", qty="295", price="23500", cntg="2", rfus="0", hms="090008"):
    f = [""] * len(FIELDS)
    values = {"ODER_NO": order, "CNTG_QTY": qty, "CNTG_UNPR": price, "CNTG_YN": cntg,
              "RFUS_YN": rfus, "STCK_SHRN_ISCD": "348340", "STCK_CNTG_HOUR": hms,
              "SELN_BYOV_CLS": "02"}
    for name, value in values.items():
        f[FIELDS.index(name)] = value
    return f


@pytest.fixture(autouse=True)
def _clean_book():
    kis_notice.reset()
    yield
    kis_notice.reset()


def test_fields_follow_the_official_26_columns():
    assert len(FIELDS) == 26
    assert FIELDS[2] == "ODER_NO" and FIELDS[13] == "CNTG_YN"


def test_decrypt_round_trips_an_encrypted_frame():
    text = "^".join(_fields())
    assert decrypt(KEY, IV, _encrypt(text)) == text


def test_parse_reads_a_fill_and_strips_leading_zeros():
    n = parse(_fields())
    assert n == Notice(order_id="1226", ticker="348340", qty=295, price=23500.0,
                       hms="090008", side="02")


def test_parse_ignores_acceptance_and_rejection_notices():
    assert parse(_fields(cntg="1")) is None
    assert parse(_fields(rfus="1")) is None
    assert parse(_fields(qty="0")) is None
    assert parse(["short"]) is None


def test_split_records_uses_the_header_count():
    two = _fields(qty="100") + _fields(qty="195")
    records = split_records("^".join(two), 2)
    assert [r[FIELDS.index("CNTG_QTY")] for r in records] == ["100", "195"]
    assert len(split_records("^".join(_fields()), None)) == 1


def test_book_accumulates_partial_fills_and_signals_at_full():
    event = kis_notice.expect("0000001226", 295)
    kis_notice.record(Notice("1226", "348340", 100, 23500.0, "090008", "02"))
    assert not event.is_set()
    assert kis_notice.fill("0000001226")["status"] == "PARTIAL"
    kis_notice.record(Notice("1226", "348340", 195, 23400.0, "090008", "02"))
    assert event.is_set()
    got = kis_notice.fill("1226")
    assert got["status"] == "FILLED" and got["fill_qty"] == 295 and got["remaining_qty"] == 0
    # 조회 경로(f3_entry `_query_order_fill_snapshot`)와 같은 반올림이다. 23,433.9 → 23,434.
    assert got["fill_price"] == 23434


def test_book_price_matches_the_query_rounding_on_a_real_split_fill():
    """2026-11-05 035720: 통보 4건 평균 30,450.4원, DB 체결가 30,450원."""
    kis_notice.expect("1836", 229)
    for qty, price in ((100, 30450.0), (79, 30450.0), (40, 30450.0), (10, 30459.0)):
        kis_notice.record(Notice("1836", "035720", qty, price, "090007", "02"))
    assert kis_notice.fill("1836")["fill_price"] == 30450


def test_notice_arriving_before_registration_is_kept():
    kis_notice.record(Notice("1226", "348340", 295, 23500.0, "090008", "02"))
    event = kis_notice.expect("0000001226", 295)
    assert event.is_set() and kis_notice.fill("1226")["fill_qty"] == 295


def test_unknown_order_has_no_fill_or_event():
    assert kis_notice.fill("999") is None and kis_notice.event_for("999") is None


def test_latency_is_measured_from_registration_to_first_fill():
    kis_notice.expect("1226", 295)
    assert kis_notice.latency_ms("1226") is None
    kis_notice.record(Notice("1226", "348340", 295, 23500.0, "090008", "02"))
    assert kis_notice.latency_ms("1226") >= 0


def test_enabled_needs_hts_id_and_the_switch(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "someone")
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    assert kis_notice.enabled() is True
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "0")
    assert kis_notice.enabled() is False
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    monkeypatch.setenv("KIS_HTS_ID", " ")
    assert kis_notice.enabled() is False


@pytest.mark.asyncio
async def test_event_can_be_awaited():
    event = kis_notice.expect("1226", 10)
    asyncio.get_running_loop().call_soon(
        kis_notice.record, Notice("1226", "348340", 10, 1.0, "090008", "02"))
    await asyncio.wait_for(event.wait(), timeout=1)


def _ack(tr_id="H0STCNI9", rt_cd="0"):
    out = {"key": KEY, "iv": IV} if rt_cd == "0" else {}
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": "x", "encrypt": "Y"},
                       "body": {"rt_cd": rt_cd, "msg_cd": "OPSP0000",
                                "msg1": "SUBSCRIBE SUCCESS", "output": out}})


def test_handle_message_subscribes_then_books_an_encrypted_fill(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: logs.append(e))
    assert kis_notice.handle_message(_ack()) is None
    assert kis_notice.healthy() is True
    kis_notice.expect("0000001226", 295)
    assert kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields()))) is None
    assert kis_notice.fill("1226")["fill_qty"] == 295
    assert "FILL_NOTICE_SUBSCRIBED" in logs and "FILL_NOTICE_RECEIVED" in logs


def test_rejected_subscription_disables_for_the_day(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "hts-user")
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    assert kis_notice.should_subscribe() is True
    assert kis_notice.handle_message(_ack(rt_cd="1")) == "DISABLE"
    assert kis_notice.healthy() is False and kis_notice.should_subscribe() is False


def test_quote_traffic_and_pingpong_are_not_notices(monkeypatch):
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    assert kis_notice.handle_message(json.dumps({"header": {"tr_id": "PINGPONG"}})) == "PONG"
    assert kis_notice.handle_message(_ack(tr_id="H0STCNT0")) is None
    assert kis_notice.healthy() is False
    assert kis_notice.handle_message("0|H0STCNT0|001|005930^091015^10300") is None


def test_encrypted_frame_without_a_key_is_not_booked(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: logs.append(e))
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields())))
    assert kis_notice.fill("1226") is None and "FILL_NOTICE_UNDECRYPTABLE" in logs


def test_connection_lost_forgets_the_key(monkeypatch):
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    kis_notice.handle_message(_ack())
    kis_notice.connection_lost()
    assert kis_notice.healthy() is False
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields())))
    assert kis_notice.fill("1226") is None


def test_subscribe_request_uses_hts_id_and_mode(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "hts-user")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    body = json.loads(kis_notice.subscribe_request("quote-key"))
    assert body["header"]["approval_key"] == "quote-key"
    assert body["body"]["input"] == {"tr_id": "H0STCNI9", "tr_key": "hts-user"}
    monkeypatch.setenv("KIS_MODE", "REAL")
    assert kis_notice.tr_id() == "H0STCNI0"


# ── 진단 로그 (docs/FILL_NOTICE_FOLLOWUP_20261007.md 5절 가, 6.4절) ──


def _capture(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: logs.append((e, k)))
    return logs


def _frames(logs, event="FILL_NOTICE_FRAME"):
    return [k for e, k in logs if e == event]


def test_every_notice_record_logs_its_shape_without_account_fields(monkeypatch):
    logs = _capture(monkeypatch)
    kis_notice.handle_message(_ack())
    accept = _fields(cntg="1")
    fill = _fields()
    for f in (accept, fill):
        f[FIELDS.index("CUST_ID")] = "HTS-SECRET"
        f[FIELDS.index("ACNT_NO")] = "ACCT-SECRET"
        f[FIELDS.index("ACNT_NAME")] = "NAME-SECRET"
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(accept)))
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(fill)))
    frames = _frames(logs)
    assert [(f["cntg_yn"], f["booked"], f["reason"]) for f in frames] == [
        ("1", False, "NOT_FILL"), ("2", True, "FILL")]
    assert frames[0]["field_count"] == 26 and frames[0]["encrypted"] is True
    assert frames[0]["header_count"] == 1 and frames[0]["order_id"] == "1226"
    assert "SECRET" not in repr(logs)


def test_short_notice_record_is_logged_but_not_booked(monkeypatch):
    logs = _capture(monkeypatch)
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_fields()[:20]))
    (frame,) = _frames(logs)
    assert frame["field_count"] == 20 and frame["reason"] == "SHORT"
    assert frame["booked"] is False and kis_notice.fill("1226") is None


def test_unknown_tr_frame_is_logged_once_with_its_tr_id(monkeypatch):
    logs = _capture(monkeypatch)
    kis_notice.handle_message("1|K0STCNI9|001|abcd")
    kis_notice.handle_message("1|K0STCNI9|001|abcd")
    kis_notice.handle_message("0|H0STCNT0|001|005930^091015^10300")
    (unknown,) = _frames(logs, "WS_FRAME_UNKNOWN_TR")
    assert unknown["tr_id"] == "K0STCNI9" and unknown["encrypted"] is True
    assert unknown["header_count"] == "001" and unknown["occurrences"] == 1
    assert _frames(logs) == []


def _short(fields: list[str]) -> list[str]:
    # 2026-10-12 모의 통보는 23필드였다. 앞쪽 위치는 공식과 같고 뒤가 빠진 모양.
    return fields[:23]


def test_paper_23_field_fill_is_booked_and_keeps_its_value_log(monkeypatch):
    """모의 통보는 23필드다. 위치는 10/14 ~ 11/05 체결 주문 28건과 대조해 맞았다
    (docs/FILL_NOTICE_FOLLOWUP_20261007.md 8.9절)."""
    logs = _capture(monkeypatch)
    kis_notice.expect("0000001226", 36)
    fill = _fields(qty="36", price="26750", hms="090100")
    fill[FIELDS.index("SELN_BYOV_CLS")] = "01"
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_short(fill)))
    (frame,) = _frames(logs)
    assert frame["field_count"] == 23
    assert frame["reason"] == "FILL" and frame["booked"] is True
    assert (frame["side"], frame["ticker"], frame["cntg_qty"], frame["cntg_unpr"],
            frame["exchange_time"]) == ("01", fill[FIELDS.index("STCK_SHRN_ISCD")],
                                        "36", "26750", "090100")
    assert kis_notice.fill("1226")["fill_qty"] == 36
    assert kis_notice.event_for("1226").is_set()


def test_paper_23_field_acceptance_is_not_a_fill(monkeypatch):
    """접수 통보의 수량·단가 자리에는 주문값이 있다(8.6절).

    체결로 쌓으면 미체결 주문을 체결로 본다.
    """
    logs = _capture(monkeypatch)
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_short(_fields(cntg="1"))))
    (frame,) = _frames(logs)
    assert frame["reason"] == "NOT_FILL" and frame["booked"] is False
    assert kis_notice.fill("1226") is None


@pytest.mark.parametrize(("count", "reason"), [(22, "SHORT"), (24, "SHORT"), (25, "SHORT"),
                                               (27, "LONG")])
def test_other_field_counts_are_not_booked(monkeypatch, count, reason):
    """형식이 또 바뀌면 위치를 믿을 수 없다. 조회 확인으로 돌아가고 사유를 남긴다."""
    logs = _capture(monkeypatch)
    kis_notice.expect("1226", 295)
    fields = (_fields() + ["X"])[:count]
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(fields))
    (frame,) = _frames(logs)
    assert frame["field_count"] == count
    assert frame["reason"] == reason and frame["booked"] is False
    assert kis_notice.fill("1226") is None


@pytest.mark.parametrize("ticker", ["", "홍길동", "ACCOUNT-NAME"])
def test_fill_with_a_ticker_that_is_not_a_code_is_not_booked(monkeypatch, ticker):
    """종목 자리에 코드가 아닌 값이 있으면 위치가 밀린 레코드다.

    체결로 쌓지 않고 값도 남기지 않는다.
    """
    logs = _capture(monkeypatch)
    kis_notice.expect("1226", 295)
    rec = _fields()
    rec[FIELDS.index("STCK_SHRN_ISCD")] = ticker
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_short(rec)))
    (frame,) = _frames(logs)
    assert frame["reason"] == "BAD_VALUE" and frame["booked"] is False
    assert kis_notice.fill("1226") is None
    assert "FILL_NOTICE_RECEIVED" not in [e for e, _ in logs]
    if ticker:
        assert ticker not in repr(logs)


def test_value_fields_are_withheld_when_the_record_looks_misaligned(monkeypatch):
    """체결 여부가 1·2가 아니면 위치를 믿을 수 없다. 그 자리에 계좌 필드가 올 수 있다."""
    logs = _capture(monkeypatch)
    rec = _fields(cntg="X")
    rec[FIELDS.index("CNTG_QTY")] = "ACCT-SECRET"
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_short(rec)))
    (frame,) = _frames(logs)
    assert frame["cntg_qty"] is None and frame["ticker"] is None
    assert "SECRET" not in repr(logs)


def test_value_fields_that_do_not_look_like_codes_are_masked(monkeypatch):
    """숫자·영문 대문자 코드가 아니면 길이만 남긴다(계좌명이 끼어들어도 새지 않게)."""
    logs = _capture(monkeypatch)
    rec = _fields()
    rec[FIELDS.index("STCK_SHRN_ISCD")] = "홍길동"
    kis_notice.handle_message("0|H0STCNI9|001|" + "^".join(_short(rec)))
    (frame,) = _frames(logs)
    assert frame["ticker"] == "<len=3>"
    assert "홍길동" not in repr(logs)

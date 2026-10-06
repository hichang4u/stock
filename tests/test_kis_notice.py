"""KIS 실시간 체결통보 — 복호화·해석·주문별 장부.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md
"""

import asyncio
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
    assert got["fill_price"] == pytest.approx((100 * 23500 + 195 * 23400) / 295)


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

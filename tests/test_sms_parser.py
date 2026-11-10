from datetime import datetime

import pytest

from transaction_ml.ingestion.sms_parser import balance_gap, parse_sms

# Real IDFC FIRST wording; names, RRNs and account numbers are made up.
DEBIT = ("Your A/c XX1234 debited by Rs. 196.00 on 06/11/26; RAVI KUMAR credited. RRN 100000000001. "
         "Available balance Rs. 747.90. Team IDFC FIRST Bank")
CREDIT = ("Your A/C XXXXX001234 is credited with INR 100.00 on 06/11/26 22:42. "
          "Your new balance is INR 847.90. Team IDFC FIRST Bank")
SEQUENCE = [
    DEBIT,
    CREDIT,
    "Your A/c XX1234 debited by Rs. 70.00 on 07/11/26; Miss ANITA  DEVI credited. RRN 100000000002. Available balance Rs. 777.90. Team IDFC FIRST Bank",
    "Your A/c XX1234 debited by Rs. 50.00 on 07/11/26; DMRC credited. RRN 100000000003. Available balance Rs. 727.90. Team IDFC FIRST Bank",
    "Your A/c XX1234 debited by Rs. 13.00 on 07/11/26; MOHAN LAL credited. RRN 100000000004. Available balance Rs. 714.90. Team IDFC FIRST Bank",
    "Your A/c XX1234 debited by Rs. 220.00 on 07/11/26; SURESH TEA STALL credited. RRN 100000000005. Available balance Rs. 494.90. Team IDFC FIRST Bank",
    "Your A/C XXXXX001234 is credited with INR 160.00 on 08/11/26 20:26. Your new balance is INR 654.90. Team IDFC FIRST Bank",
]


def test_debit():
    s = parse_sms(DEBIT, received_at=datetime(2026, 11, 6, 13, 5, 42))
    assert (s.direction, s.amount, s.payee, s.rrn, s.balance, s.account_last4) == \
        ("debit", -196.0, "RAVI KUMAR", "100000000001", 747.90, "1234")
    assert s.id == "rrn:100000000001"
    assert s.timestamp == datetime(2026, 11, 6, 13, 5)  # time taken from arrival


def test_debit_without_arrival_time_falls_back_to_midnight():
    assert parse_sms(DEBIT).timestamp == datetime(2026, 11, 6)
    # arrival on a different day (e.g. forwarded late) must not shift the date
    assert parse_sms(DEBIT, received_at=datetime(2026, 11, 9, 8, 0)).timestamp == datetime(2026, 11, 6)


def test_credit_uses_its_own_time():
    s = parse_sms(CREDIT, received_at=datetime(2026, 11, 7, 9, 0))
    assert (s.direction, s.amount, s.payee, s.balance, s.timestamp) == \
        ("credit", 100.0, None, 847.90, datetime(2026, 11, 6, 22, 42))
    assert s.id.startswith("cr:") and s.id == parse_sms(CREDIT).id  # stable


def test_whitespace_and_line_breaks_are_normalised():
    s = parse_sms(SEQUENCE[2].replace("; ", ";\n"))
    assert s.payee == "Miss ANITA DEVI"


def test_large_amounts_with_commas():
    s = parse_sms(DEBIT.replace("Rs. 196.00", "Rs. 12,500.00").replace("Rs. 747.90", "Rs. 1,02,747.90"))
    assert (s.amount, s.balance) == (-12500.0, 102747.90)


@pytest.mark.parametrize("text", [
    "123456 is your OTP for transaction of Rs 500. Do not share. IDFC FIRST Bank",
    "Get a pre-approved loan of Rs. 2,00,000! Team IDFC FIRST Bank",
    "",
])
def test_non_transaction_sms_ignored(text):
    assert parse_sms(text) is None


def test_balance_chain_detects_missed_sms():
    parsed = [parse_sms(t) for t in SEQUENCE]
    gaps = [balance_gap(prev.balance, cur) for prev, cur in zip(parsed, parsed[1:])]
    assert gaps == [0, 0, 0, 0, 0, 0]
    # drop one SMS -> the next one reveals the missing amount
    assert balance_gap(parsed[3].balance, parsed[5]) == -13.0  # an unrecorded ₹13 debit

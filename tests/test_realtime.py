"""SMS ingestion, credits, user-editable categories, v2 migration and RRN dedupe."""
import json
import sqlite3
from datetime import datetime

import pytest

from conftest import SECRET, txn
from test_sms_parser import CREDIT, DEBIT
from transaction_ml.models.core import FRIENDS, MONEY_RECEIVED, UNCATEGORIZED, UPIData
from transaction_ml.system import LEGACY_DEFAULT_SECRET, TransactionEngine, derive_fernet_key

AT = datetime(2026, 11, 6, 13, 5)


def sms(payee, rrn, amount, balance, day=6):
    return (f"Your A/c XX1234 debited by Rs. {amount:.2f} on {day:02d}/11/26; {payee} credited. "
            f"RRN {rrn}. Available balance Rs. {balance:.2f}. Team IDFC FIRST Bank")


# ---- SMS ------------------------------------------------------------------------

def test_sms_debit_from_unknown_payee_needs_review(engine):
    r = engine.ingest_sms(DEBIT, AT)
    assert (r.status, r.transaction.category, r.needs_review) == ("added", UNCATEGORIZED, True)
    assert (r.transaction.source, r.transaction.balance, r.transaction.date) == ("sms", 747.90, AT)
    stored = engine.storage.get_transaction(r.transaction.id)
    assert (stored.merchant_name, stored.balance, stored.source) == ("RAVI KUMAR", 747.90, "sms")


def test_sms_credit_is_money_received_without_asking(engine):
    r = engine.ingest_sms(CREDIT, AT)
    assert (r.transaction.category, r.needs_review) == (MONEY_RECEIVED, False)


def test_same_sms_twice_is_a_duplicate(engine):
    engine.ingest_sms(DEBIT, AT)
    assert engine.ingest_sms(DEBIT, AT).status == "duplicate"
    assert len(engine.storage.get_all_transactions()) == 1


def test_unrecognised_sms_is_kept_for_later(engine):
    assert engine.ingest_sms("Your OTP is 1234", AT).status == "ignored"
    assert engine.storage.get_unparsed_sms() == [(AT.isoformat(), "Your OTP is 1234")]


def test_learned_payee_is_categorised_silently_next_time(engine):
    first = engine.ingest_sms(sms("Miss ANITA  DEVI", "100000000001", 70, 930), AT).transaction
    engine.correct(first, "mess food")
    nxt = engine.ingest_sms(sms("ANITA DEVI", "100000000002", 70, 860, day=7), AT)
    assert (nxt.transaction.category, nxt.needs_review) == ("mess food", False)


def test_balance_gap_reports_missed_sms(engine):
    assert engine.ingest_sms(sms("A", "100000000001", 100, 900), AT).balance_gap == 0
    assert engine.ingest_sms(sms("B", "100000000002", 50, 850), AT).balance_gap == 0
    # a ₹30 debit SMS never arrived
    assert engine.ingest_sms(sms("C", "100000000004", 20, 800), AT).balance_gap == -30.0


def test_sms_and_statement_copies_of_a_payment_dedupe(engine, write_csv):
    from transaction_ml.ingestion.excel_parser import parse_bank_statement
    engine.ingest_sms(sms("RAVI KUMAR", "412345678901", 196, 747.90), AT)
    path = write_csv([["Date", "Narration", "Withdrawal Amt", "Deposit Amt"],
                      ["06/11/2026", "UPI/DR/412345678901/RAVI KUMAR/IDFB/ravi@ybl/UPI", "196", ""],
                      ["07/11/2026", "UPI/DR/499999999999/SOMEONE ELSE/IDFB/x@ybl/UPI", "10", ""]])
    added = engine.ingest(parse_bank_statement(path))
    assert [t.id for t in added] == ["rrn:499999999999"]


def test_credits_never_become_training_data(engine):
    for day, (m, c) in enumerate([("SWIGGY", "outside food"), ("DMRC", "travel")], 1):
        t = txn(m, day)
        engine.ingest([t])
        engine.correct(t, c)
    refund = txn("SWIGGY", 5, amount=+120)
    engine.ingest([refund])
    engine.correct(refund, FRIENDS)  # e.g. a friend paying back
    assert engine.predict(txn("SWIGGY", 9)).category == "outside food"


# ---- categories ------------------------------------------------------------------

def test_defaults_are_the_users_categories(engine):
    assert [n for n, _ in engine.categories()] == [
        "travel", "outside food", "mess food", "personal items", "health food", "phone",
        "entertainment", FRIENDS, MONEY_RECEIVED]


def test_add_rename_merge_delete(engine):
    t1, t2 = txn("DMRC", 1), txn("CHAI WALA", 2)
    engine.ingest([t1, t2])
    engine.correct(t1, "travel")
    engine.correct(t2, "outside food")

    engine.add_category("snacks")
    engine.rename_category("travel", "commute")
    assert engine.storage.get_transaction(t1.id).category == "commute"
    assert engine.predict(txn("DMRC", 9)).category == "commute"  # memory follows the rename

    engine.rename_category("outside food", "snacks")  # merge
    assert "outside food" not in dict(engine.categories())
    assert engine.storage.get_transaction(t2.id).category == "snacks"

    engine.delete_category("commute")
    back = engine.storage.get_transaction(t1.id)
    assert (back.category, back.verified) == (UNCATEGORIZED, False)
    assert engine.predict(txn("DMRC", 9)).source != "merchant_memory"


@pytest.mark.parametrize("op", [
    lambda e: e.add_category("travel"),
    lambda e: e.add_category(UNCATEGORIZED),
    lambda e: e.add_category("x", kind="weird"),
    lambda e: e.rename_category("nope", "x"),
    lambda e: e.rename_category(MONEY_RECEIVED, "x"),
    lambda e: e.delete_category(MONEY_RECEIVED),
    lambda e: e.correct(txn("X"), "Food & Dining"),
])
def test_invalid_category_operations(engine, op):
    with pytest.raises(ValueError):
        op(engine)


# ---- migration of the original DB ---------------------------------------------

def test_v1_db_migrates_to_users_categories(tmp_path):
    """A DB from the statement-upload era: old 17 categories, md5 ids, plaintext UPI."""
    path = tmp_path / "old.db"
    fernet_key = derive_fernet_key(LEGACY_DEFAULT_SECRET)
    from cryptography.fernet import Fernet
    f = Fernet(fernet_key)
    enc = lambda s: f.encrypt(s.encode())
    upi = json.dumps({"type": "DR", "txn_id": "657000000001", "merchant_or_person": "ZOMATO",
                      "bank": "HDFC", "handle_or_gateway": "z@hdfc", "note": ""})
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, encrypted_merchant BLOB, encrypted_amount BLOB, "
                     "date TEXT, encrypted_notes BLOB, account_id TEXT, category TEXT, subcategory TEXT, splits TEXT, upi_data TEXT)")
        conn.execute("CREATE TABLE merchant_memory (normalized_merchant TEXT PRIMARY KEY, category TEXT, subcategory TEXT, streak INTEGER)")
        rows = [("md5a", enc("ZOMATO"), enc("-300.0"), "2026-03-01T00:00:00", None, None, "Utilities", None, None, upi),
                ("md5b", enc("NEFT ACME"), enc("5000.0"), "2026-03-02T00:00:00", None, None, "Private", None, None, None)]
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO merchant_memory VALUES ('zomato', 'Utilities', NULL, 3)")

    engine = TransactionEngine(str(path), LEGACY_DEFAULT_SECRET)
    assert (tmp_path / "old.db.bak-v0").exists()
    by_id = {t.id: t for t in engine.storage.get_all_transactions()}
    assert set(by_id) == {"rrn:657000000001", "md5b"}
    assert by_id["rrn:657000000001"].category == UNCATEGORIZED
    assert by_id["md5b"].category == MONEY_RECEIVED
    assert engine.storage.load_merchant_memory() == {}

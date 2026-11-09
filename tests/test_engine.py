import base64
import sqlite3

import pytest

from conftest import SECRET, txn
from transaction_ml.models.core import UNCATEGORIZED, ModelConfig
from transaction_ml.system import LEGACY_DEFAULT_SECRET, TransactionEngine, derive_fernet_key

TRAINING = [
    ("SWIGGY", "Food & Dining"), ("ZOMATO", "Food & Dining"),
    ("UBER INDIA", "Transport"), ("OLA CABS", "Transport"),
    ("AIRTEL PREPAID", "Utilities"), ("NETFLIX", "Entertainment"),
    ("AMAZON", "Shopping"), ("APOLLO PHARMACY", "Health & Wellness"),
]


def train(engine):
    for day, (merchant, category) in enumerate(TRAINING, start=1):
        t = txn(merchant, day)
        engine.ingest([t])
        engine.correct(t, category)


def test_untrained_engine_returns_uncategorized(engine):
    pred = engine.predict(txn("ANYTHING"))
    assert (pred.category, pred.source) == (UNCATEGORIZED, "fallback")


def test_one_correction_activates_merchant_memory(engine):
    engine.correct(txn("NETFLIX", 1), "Entertainment")
    pred = engine.predict(txn("Netflix", 2))  # normalised match
    assert (pred.category, pred.source, pred.confidence) == ("Entertainment", "merchant_memory", 1.0)


def test_conflicting_correction_resets_memory_streak(db_path):
    engine = TransactionEngine(db_path, SECRET, ModelConfig(memory_activation_threshold=2))
    engine.correct(txn("CAFE", 1), "Food & Dining")
    assert engine.predict(txn("CAFE", 9)).source != "merchant_memory"
    engine.correct(txn("CAFE", 2), "Food & Dining")
    assert engine.predict(txn("CAFE", 9)).source == "merchant_memory"
    engine.correct(txn("CAFE", 3), "Business")
    assert engine.predict(txn("CAFE", 9)).source != "merchant_memory"


def test_model_generalises_to_unseen_merchant_variants(engine):
    train(engine)
    for variant, expected in [("SWIGGY INSTAMART", "Food & Dining"), ("UBER TRIP", "Transport"),
                              ("AIRTEL BROADBAND", "Utilities"), ("AMAZON RETAIL", "Shopping")]:
        pred = engine.predict(txn(variant, 20))
        assert (pred.category, pred.source) == (expected, "model"), variant


def test_unrelated_merchants_stay_uncategorized(engine):
    train(engine)
    for name in ["RAHUL SHARMA", "IMPS 99887 MOHAN", "DECATHLON"]:
        assert engine.predict(txn(name, 20)).category == UNCATEGORIZED, name


def test_learning_survives_restart(engine, db_path):
    # Regression: labels used to get shuffled after restart (sorted vs canonical class order).
    train(engine)
    before = [engine.predict(txn(v, 20)).category for v in ("SWIGGY INSTAMART", "UBER TRIP", "NETFLIX")]
    restarted = TransactionEngine(db_path, SECRET)
    after = [restarted.predict(txn(v, 20)).category for v in ("SWIGGY INSTAMART", "UBER TRIP", "NETFLIX")]
    assert before == after == ["Food & Dining", "Transport", "Entertainment"]
    assert restarted.predict(txn("NETFLIX", 20)).source == "merchant_memory"


def test_reingest_does_not_overwrite_corrections(engine):
    t = txn("MYSTERY SHOP")
    engine.ingest([t])
    engine.correct(t, "Business")
    assert engine.ingest([txn("MYSTERY SHOP")]) == 0
    assert engine.storage.get_transaction(t.id).category == "Business"


def test_correction_reclassifies_pending_rows(engine):
    rows = [txn("SWIGGY", d) for d in range(1, 5)]
    engine.ingest(rows)
    assert all(engine.storage.get_transaction(t.id).category == UNCATEGORIZED for t in rows)
    changed = engine.correct(rows[0], "Food & Dining")
    assert changed == 3
    assert {engine.storage.get_transaction(t.id).category for t in rows} == {"Food & Dining"}
    assert [engine.storage.get_transaction(t.id).verified for t in rows] == [True, False, False, False]


def test_unknown_category_rejected(engine):
    with pytest.raises(ValueError):
        engine.correct(txn("X"), "Groceries")


def test_sensitive_fields_are_encrypted_at_rest(engine, db_path):
    from transaction_ml.models.core import UPIData
    t = txn("SECRET SHOP", amount=-1234.5)
    t.notes = "birthday gift"
    t.upi = UPIData("DR", "1", "SECRET SHOP", "HDFC", "secret@okhdfc", "gift")
    engine.ingest([t])
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute("SELECT encrypted_merchant, encrypted_amount, encrypted_notes, upi_data FROM transactions").fetchone()
    assert all(isinstance(c, bytes) and b"SECRET" not in c and b"1234" not in c and b"birthday" not in c for c in raw)
    back = engine.storage.get_transaction(t.id)
    assert (back.merchant_name, back.amount, back.notes, back.upi) == ("SECRET SHOP", -1234.5, "birthday gift", t.upi)


def test_legacy_plaintext_upi_rows_still_load(engine, db_path):
    import json
    engine.ingest([txn("X")])
    legacy = {"type": "DR", "txn_id": "1", "merchant_or_person": "X", "bank": "B", "handle_or_gateway": "x@b", "note": ""}
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE transactions SET upi_data = ?", (json.dumps(legacy),))
    assert engine.storage.get_transaction(txn("X").id).upi.handle_or_gateway == "x@b"


def test_legacy_key_derivation_is_unchanged():
    # Must match the original implementation or existing DBs become unreadable.
    assert derive_fernet_key(LEGACY_DEFAULT_SECRET) == base64.urlsafe_b64encode(LEGACY_DEFAULT_SECRET.encode())
    proper = base64.urlsafe_b64encode(b"k" * 32).decode()
    assert derive_fernet_key(proper) == proper.encode()


def test_legacy_db_is_migrated_with_backup(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # schema from the original commit
        conn.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, encrypted_merchant BLOB, encrypted_amount BLOB, "
                     "date TEXT, encrypted_notes BLOB, account_id TEXT, category TEXT, subcategory TEXT, splits TEXT)")
    engine = TransactionEngine(str(path), SECRET)
    assert (tmp_path / "old.db.bak-pre-verified").exists()
    engine.ingest([txn("X")])
    assert engine.storage.get_transaction(txn("X").id).verified is False


def test_shared_upi_gateway_does_not_make_strangers_look_familiar(engine):
    from transaction_ml.models.core import UPIData
    train(engine)
    stranger = txn("RAHUL SHARMA", 20)
    stranger.upi = UPIData("DR", "1", "RAHUL SHARMA", "HDFC", "rahul@okhdfcbank", "UPI")
    assert engine.predict(stranger).category == UNCATEGORIZED

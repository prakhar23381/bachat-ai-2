import base64
import binascii
import functools
import os
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from transaction_ml.engine.rules import MerchantMemory
from transaction_ml.features.extractor import normalize_merchant_name
from transaction_ml.features.extractor import FeatureExtractor
from transaction_ml.models.classifiers import CategoryClassifier
from transaction_ml.ingestion.sms_parser import parse_sms
from transaction_ml.models.core import (
    EXPENSE, INCOME, MONEY_RECEIVED, PROTECTED_CATEGORIES, SPLIT, UNCATEGORIZED,
    ModelConfig, Prediction, Transaction,
)
from transaction_ml.storage.db import EncryptedStorage

# The original code shipped this hardcoded key. Kept only so DBs created with it
# can still be opened; set BACHAT_KEY to override.
LEGACY_DEFAULT_SECRET = "01234567890123456789012345678901"


def load_secret() -> str:
    secret = os.environ.get("BACHAT_KEY")
    if not secret:
        warnings.warn("BACHAT_KEY not set; using the public legacy default key. Your DB is effectively unencrypted.")
        return LEGACY_DEFAULT_SECRET
    return secret


def derive_fernet_key(secret: str) -> bytes:
    """
    Accepts either a proper Fernet key (urlsafe-base64 of 32 bytes) or any string,
    which is truncated/padded to 32 bytes. Must stay byte-for-byte compatible with
    the original implementation or existing DBs become unreadable.
    """
    try:
        if len(base64.urlsafe_b64decode(secret)) == 32:
            return secret.encode("utf-8")
    except (binascii.Error, ValueError):
        pass
    return base64.urlsafe_b64encode(secret.encode("utf-8")[:32].ljust(32, b"0"))


def _locked(method):
    """The API thread pool and the Telegram bot thread share one engine."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


MAX_CATEGORY_LENGTH = 24  # Telegram button payloads are limited to 64 bytes


@dataclass
class SmsResult:
    status: str                            # "added" | "duplicate" | "ignored"
    transaction: Optional[Transaction] = None
    balance_gap: float = 0.0               # != 0: an SMS was probably missed (signed amount)

    @property
    def needs_review(self) -> bool:
        return self.status == "added" and self.transaction.category == UNCATEGORIZED


class TransactionEngine:
    """
    Categorisation pipeline, in priority order:
      0. Credits (money in) are always "Money received".
      1. Merchant memory: exact payee match from previous user corrections.
      2. Logistic regression on hashed text + amount/time features, retrained from
         every user-verified debit. Only trusted when the payee name looks like one
         already verified and the probability is high enough.
      3. Otherwise "Uncategorized" = needs the user's answer.

    Only verified debits are training data; unverified rows are re-predicted after
    every correction or category change.
    """

    def __init__(self, db_path: str, secret: str, config: Optional[ModelConfig] = None):
        self._lock = threading.RLock()
        self.config = config or ModelConfig()
        self.storage = EncryptedStorage(db_path, derive_fernet_key(secret))
        self.features = FeatureExtractor()
        self.classifier = CategoryClassifier()
        self.memory = MerchantMemory(self.config, self.storage.load_merchant_memory())
        self.retrain()

    # ---- categories -----------------------------------------------------------

    def categories(self) -> List[Tuple[str, str]]:
        return self.storage.get_categories()

    def _require(self, name: str, *, exists: bool):
        if (name in self.storage.category_names()) != exists:
            raise ValueError(f"Category {name!r} {'does not exist' if exists else 'already exists'}")

    @_locked
    def add_category(self, name: str, kind: str = EXPENSE):
        name = name.strip()
        if not name or name == UNCATEGORIZED or len(name) > MAX_CATEGORY_LENGTH or "|" in name:
            raise ValueError(f"Category names must be 1-{MAX_CATEGORY_LENGTH} characters, without '|'")
        if kind not in (EXPENSE, INCOME, SPLIT):
            raise ValueError(f"Unknown kind {kind!r}")
        self._require(name, exists=False)
        self.storage.add_category(name, kind)

    @_locked
    def rename_category(self, old: str, new: str):
        """Renaming onto an existing category merges the two."""
        new = new.strip()
        self._require(old, exists=True)
        if old in PROTECTED_CATEGORIES or not new or new == UNCATEGORIZED or len(new) > MAX_CATEGORY_LENGTH or "|" in new:
            raise ValueError(f"Cannot rename {old!r} to {new!r}")
        if new in self.storage.category_names():
            self.storage.delete_category(old)
        else:
            self.storage.rename_category(old, new)
        self.storage.recategorize(old, new)
        self._reload()

    @_locked
    def delete_category(self, name: str):
        """Its transactions go back to Uncategorized (and get re-predicted)."""
        self._require(name, exists=True)
        if name in PROTECTED_CATEGORIES:
            raise ValueError(f"{name!r} cannot be deleted")
        self.storage.delete_category(name)
        self.storage.recategorize(name, UNCATEGORIZED, unverify=True)
        self._reload()

    def _reload(self):
        self.memory = MerchantMemory(self.config, self.storage.load_merchant_memory())
        self.retrain()
        self.reclassify_unverified()

    # ---- prediction -----------------------------------------------------------

    def predict_many(self, transactions: List[Transaction]) -> List[Prediction]:
        """Pure prediction: no side effects."""
        if not transactions:
            return []
        model_out = self.classifier.predict(
            self.features.transform(transactions), self.features.anchor_vectors(transactions)
        )
        preds = []
        for t, result in zip(transactions, model_out):
            rule = self.memory.predict(t)
            if t.amount > 0:
                preds.append(Prediction(t.id, MONEY_RECEIVED, 1.0, "rule"))
            elif rule:
                preds.append(Prediction(t.id, rule["category"], 1.0, "merchant_memory", rule.get("subcategory")))
            elif result is None:
                preds.append(Prediction(t.id, UNCATEGORIZED, 0.0, "fallback"))
            else:
                category, confidence, probs, similarity = result
                if confidence < self.config.min_confidence or similarity < self.config.min_similarity:
                    category = UNCATEGORIZED
                preds.append(Prediction(t.id, category, confidence, "model", all_probabilities=probs))
        return preds

    def predict(self, transaction: Transaction) -> Prediction:
        return self.predict_many([transaction])[0]

    # ---- writes -----------------------------------------------------------------

    @_locked
    def ingest(self, transactions: List[Transaction]) -> List[Transaction]:
        """
        Classify and store new transactions; returns the ones actually added.
        Existing ids are skipped, so re-imports never overwrite corrections.
        """
        seen = self.storage.existing_ids([t.id for t in transactions])
        new = []
        for t in transactions:
            if t.id not in seen:
                new.append(t)
                seen.add(t.id)
        for t, pred in zip(new, self.predict_many(new)):
            t.category, t.subcategory = pred.category, pred.subcategory
        self.storage.insert_transactions(new)
        return new

    @_locked
    def ingest_sms(self, text: str, received_at: Optional[datetime] = None) -> SmsResult:
        sms = parse_sms(text, received_at)
        if sms is None:
            self.storage.save_unparsed_sms(text, received_at)
            return SmsResult("ignored")

        t = Transaction(
            id=sms.id,
            merchant_name=sms.payee or "Incoming transfer",
            amount=sms.amount,
            date=sms.timestamp,
            account_id=sms.account_last4,
            source="sms",
            balance=sms.balance,
        )
        if not self.ingest([t]):
            return SmsResult("duplicate", self.storage.get_transaction(t.id))

        gap = 0.0
        last = self.storage.get_meta("last_balance")
        if last is not None:
            gap = round(sms.balance - (float(last) + sms.amount), 2)
        self.storage.set_meta("last_balance", str(sms.balance))
        return SmsResult("added", t, gap)

    @_locked
    def merge_statement(self, transactions: List[Transaction]) -> Tuple[int, int]:
        """
        Statement import: add rows not seen yet, and enrich rows already captured by SMS
        with what only the statement has (the user's UPI note and the payee's UPI ID).
        Unverified enriched rows are re-predicted. Returns (added, enriched).
        """
        existing = {t.id: t for t in self.storage.get_all_transactions()}
        enriched = []
        for t in transactions:
            old = existing.get(t.id)
            if old and old.upi is None and t.upi is not None:
                old.upi = t.upi
                old.notes = old.notes or (t.upi.note or None)
                enriched.append(old)
        self.storage.insert_transactions(enriched)
        added = self.ingest([t for t in transactions if t.id not in existing])
        if enriched:
            self.reclassify_unverified()
        return len(added), len(enriched)

    def category_from_note(self, note: str) -> Optional[str]:
        """
        A note that names a category ("mess", "travel", "health food") is taken as
        the user's answer. A category matches if all its words appear in the note,
        or if its first word does and no other category shares that first word.
        """
        words = set(normalize_merchant_name(note).split())
        names = [n for n, _ in self.categories()]
        firsts = Counter(n.split()[0].lower() for n in names)
        hits = [n for n in names
                if set(n.lower().split()) <= words
                or (n.split()[0].lower() in words and firsts[n.split()[0].lower()] == 1)]
        return hits[0] if len(hits) == 1 else None

    @_locked
    def apply_note(self, transaction: Transaction, note: str) -> Tuple[Prediction, bool]:
        """
        Attach the user's note. Returns (prediction, decided): decided=True means the
        note named a category and it was applied as a correction; otherwise the
        prediction (possibly Uncategorized) is a suggestion to confirm.
        """
        transaction.notes = note.strip()
        named = self.category_from_note(note)
        if named:
            self.correct(transaction, named)
            return Prediction(transaction.id, named, 1.0, "note"), True
        pred = self.predict(transaction)
        if not transaction.verified:
            transaction.category = pred.category
        self.storage.insert_transaction(transaction)
        return pred, False

    def pending_payees(self) -> List[Tuple[Transaction, int]]:
        """Uncategorized debits grouped by payee, most frequent first: (latest txn, count)."""
        groups: Dict[str, List[Transaction]] = defaultdict(list)
        for t in self.storage.get_all_transactions():
            if t.category == UNCATEGORIZED and t.amount < 0:
                groups[MerchantMemory.key(t)].append(t)
        ranked = sorted(groups.values(), key=lambda g: (-len(g), -max(t.date for t in g).timestamp()))
        return [(max(g, key=lambda t: t.date), len(g)) for g in ranked]

    def day_summary(self, day: datetime) -> Dict:
        rows = [t for t in self.storage.get_all_transactions() if t.date.date() == day.date()]
        spent = defaultdict(float)
        for t in rows:
            if t.amount < 0:
                spent[t.category or UNCATEGORIZED] += -t.amount
        return {
            "date": day.date().isoformat(),
            "count": len(rows),
            "spent_total": round(sum(spent.values()), 2),
            "spent_by_category": dict(sorted(spent.items(), key=lambda kv: -kv[1])),
            "received_total": round(sum(t.amount for t in rows if t.amount > 0), 2),
            "pending": sum(1 for t in rows if t.category == UNCATEGORIZED),
        }

    @_locked
    def correct(self, transaction: Transaction, category: str, subcategory: Optional[str] = None) -> int:
        """
        Apply a user correction, retrain, and re-predict every unverified row.
        Returns how many other rows changed category as a result.
        """
        if category != UNCATEGORIZED:
            self._require(category, exists=True)

        transaction.category, transaction.subcategory = category, subcategory
        transaction.verified = category != UNCATEGORIZED
        self.storage.insert_transaction(transaction)

        if transaction.verified and transaction.amount < 0:
            record = self.memory.update(transaction, category, subcategory)
            self.storage.save_merchant_memory(MerchantMemory.key(transaction), record)

        self.retrain()
        return self.reclassify_unverified()

    @_locked
    def retrain(self):
        verified = [t for t in self.storage.get_all_transactions() if t.verified and t.amount < 0]
        self.classifier.fit(
            self.features.transform(verified) if verified else None,
            [t.category for t in verified],
            self.features.anchor_vectors(verified) if verified else None,
        )

    @_locked
    def reclassify_unverified(self) -> int:
        pending = [t for t in self.storage.get_all_transactions() if not t.verified]
        changed = []
        for t, pred in zip(pending, self.predict_many(pending)):
            if (pred.category, pred.subcategory) != (t.category, t.subcategory):
                t.category, t.subcategory = pred.category, pred.subcategory
                changed.append(t)
        self.storage.insert_transactions(changed)
        return len(changed)

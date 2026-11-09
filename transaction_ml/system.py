import base64
import binascii
import os
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from transaction_ml.engine.rules import MerchantMemory
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

    def add_category(self, name: str, kind: str = EXPENSE):
        name = name.strip()
        if not name or name == UNCATEGORIZED:
            raise ValueError("Invalid category name")
        if kind not in (EXPENSE, INCOME, SPLIT):
            raise ValueError(f"Unknown kind {kind!r}")
        self._require(name, exists=False)
        self.storage.add_category(name, kind)

    def rename_category(self, old: str, new: str):
        """Renaming onto an existing category merges the two."""
        new = new.strip()
        self._require(old, exists=True)
        if old in PROTECTED_CATEGORIES or not new or new == UNCATEGORIZED:
            raise ValueError(f"Cannot rename {old!r} to {new!r}")
        if new in self.storage.category_names():
            self.storage.delete_category(old)
        else:
            self.storage.rename_category(old, new)
        self.storage.recategorize(old, new)
        self._reload()

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
            self.features.transform(transactions), self.features.merchant_vectors(transactions)
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

    def retrain(self):
        verified = [t for t in self.storage.get_all_transactions() if t.verified and t.amount < 0]
        self.classifier.fit(
            self.features.transform(verified) if verified else None,
            [t.category for t in verified],
            self.features.merchant_vectors(verified) if verified else None,
        )

    def reclassify_unverified(self) -> int:
        pending = [t for t in self.storage.get_all_transactions() if not t.verified]
        changed = []
        for t, pred in zip(pending, self.predict_many(pending)):
            if (pred.category, pred.subcategory) != (t.category, t.subcategory):
                t.category, t.subcategory = pred.category, pred.subcategory
                changed.append(t)
        self.storage.insert_transactions(changed)
        return len(changed)

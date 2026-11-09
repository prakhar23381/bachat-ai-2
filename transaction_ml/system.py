import base64
import binascii
import os
import warnings
from typing import List, Optional

from transaction_ml.engine.rules import MerchantMemory
from transaction_ml.features.extractor import FeatureExtractor
from transaction_ml.models.classifiers import CategoryClassifier
from transaction_ml.models.core import CANONICAL_CATEGORIES, UNCATEGORIZED, ModelConfig, Prediction, Transaction
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


class TransactionEngine:
    """
    Categorisation pipeline, in priority order:
      1. Merchant memory: exact merchant match from previous user corrections.
      2. Logistic regression on hashed text + amount/date features, retrained from
         every user-verified transaction. Only trusted when the merchant name looks
         like one already verified and the probability is high enough.
      3. Otherwise "Uncategorized", which puts the row in the Active Learning queue.

    Only verified rows (user corrections) are ever used as training data, and
    unverified rows are re-predicted after every correction.
    """

    def __init__(self, db_path: str, secret: str, config: Optional[ModelConfig] = None):
        self.config = config or ModelConfig()
        self.storage = EncryptedStorage(db_path, derive_fernet_key(secret))
        self.features = FeatureExtractor()
        self.classifier = CategoryClassifier()
        self.memory = MerchantMemory(self.config, self.storage.load_merchant_memory())
        self.retrain()

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
            if rule:
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

    def ingest(self, transactions: List[Transaction]) -> int:
        """
        Classify and store new transactions. Rows whose id already exists are
        skipped, so re-uploading a statement never overwrites corrections.
        Returns the number of newly stored rows.
        """
        existing = self.storage.existing_ids([t.id for t in transactions])
        new, seen = [], set(existing)
        for t in transactions:
            if t.id not in seen:
                new.append(t)
                seen.add(t.id)
        for t, pred in zip(new, self.predict_many(new)):
            t.category, t.subcategory = pred.category, pred.subcategory
        self.storage.insert_transactions(new)
        return len(new)

    def correct(self, transaction: Transaction, category: str, subcategory: Optional[str] = None) -> int:
        """
        Apply a user correction, retrain, and re-predict every unverified row.
        Returns how many other rows changed category as a result.
        """
        if category not in CANONICAL_CATEGORIES:
            raise ValueError(f"Unknown category: {category!r}")

        transaction.category, transaction.subcategory = category, subcategory
        transaction.verified = category != UNCATEGORIZED
        self.storage.insert_transaction(transaction)

        if transaction.verified:
            record = self.memory.update(transaction, category, subcategory)
            self.storage.save_merchant_memory(MerchantMemory.key(transaction), record)

        self.retrain()
        return self.reclassify_unverified()

    def retrain(self):
        verified = [t for t in self.storage.get_all_transactions() if t.verified]
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

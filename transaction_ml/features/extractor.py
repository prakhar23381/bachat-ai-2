import re
from typing import List

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import HashingVectorizer

from transaction_ml.models.core import Transaction

TEXT_FEATURES = 10_000
# log(amount) reaches ~11 while each hashed text feature is a fraction of 1 (L2-norm),
# so unscaled the amount dominates the model. 0.1 keeps it as a weak signal.
AMOUNT_SCALE = 0.1


_HONORIFIC = re.compile(r"^(mr|mrs|miss|ms|shri|smt|dr) ")


def normalize_merchant_name(name: str) -> str:
    """Lowercase, strip punctuation/honorifics, collapse whitespace. Also the merchant-memory key."""
    if not name:
        return ""
    name = re.sub(r"[^a-z0-9\s]", "", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    return _HONORIFIC.sub("", name)


class FeatureExtractor:
    """Stateless: hashed word + char n-grams of the text, plus amount/date features."""

    def __init__(self):
        self.word_vectorizer = HashingVectorizer(
            analyzer="word", ngram_range=(1, 2), n_features=TEXT_FEATURES // 2,
            stop_words="english", norm="l2",
        )
        self.char_vectorizer = HashingVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), n_features=TEXT_FEATURES // 2, norm="l2",
        )

    @staticmethod
    def _text(t: Transaction) -> str:
        merchant = normalize_merchant_name(t.merchant_name)
        notes = normalize_merchant_name(t.notes or "")
        if not t.upi:
            return f"{merchant} {notes}".strip()
        bank = normalize_merchant_name(t.upi.bank)
        gateway = normalize_merchant_name(t.upi.handle_or_gateway)
        note = normalize_merchant_name(t.upi.note)
        # merchant and UPI note repeated to up-weight them
        return f"{merchant} {merchant} {notes} {bank} {gateway} {note} {note}".strip()

    @staticmethod
    def _numeric(t: Transaction) -> List[float]:
        amount = float(t.amount)
        dt = t.date
        return [
            AMOUNT_SCALE * np.sign(amount) * np.log1p(abs(amount)),
            1.0 if dt.weekday() >= 5 else 0.0,
            np.sin(2 * np.pi * dt.hour / 24.0),
            np.cos(2 * np.pi * dt.hour / 24.0),
            np.sin(2 * np.pi * dt.day / 31.0),
            np.cos(2 * np.pi * dt.day / 31.0),
        ]

    def transform(self, transactions: List[Transaction]) -> csr_matrix:
        texts = [self._text(t) for t in transactions]
        numeric = csr_matrix(np.array([self._numeric(t) for t in transactions], dtype=np.float32))
        return hstack(
            [self.word_vectorizer.transform(texts), self.char_vectorizer.transform(texts), numeric],
            format="csr",
        )

    def merchant_vectors(self, transactions: List[Transaction]) -> csr_matrix:
        """L2-normalised char n-grams of the merchant name alone (for similarity checks)."""
        return self.char_vectorizer.transform([normalize_merchant_name(t.merchant_name) for t in transactions])

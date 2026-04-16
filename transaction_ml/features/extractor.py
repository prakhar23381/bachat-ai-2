import numpy as np
import re
from typing import List, Dict, Any
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import FeatureUnion
from sklearn.feature_extraction.text import HashingVectorizer
from scipy.sparse import hstack, csr_matrix

from transaction_ml.models.core import Transaction

def normalize_merchant_name(name: str) -> str:
    """Basic normalization for merchant names."""
    if not name:
        return ""
    name = name.lower()
    # Remove special characters, keep alphanumeric and spaces
    name = re.sub(r'[^a-z0-9\s]', '', name)
    # Condense whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name

class TransactionTextTransformer(BaseEstimator, TransformerMixin):
    """
    Extracts text from transactions and applies Hashing vectorization.
    Combines merchant name and notes.
    """
    def __init__(self, max_features: int = 10000):
        self.max_features = max_features
        # Word n-grams
        self.word_vectorizer = HashingVectorizer(
            analyzer='word',
            ngram_range=(1, 2),
            n_features=self.max_features // 2,
            stop_words='english',
            norm='l2'
        )
        # Char n-grams
        self.char_vectorizer = HashingVectorizer(
            analyzer='char_wb',
            ngram_range=(3, 5),
            n_features=self.max_features // 2,
            norm='l2'
        )

    def _get_text(self, transactions: List[Transaction]) -> List[str]:
        texts = []
        for t in transactions:
            m_name = normalize_merchant_name(t.merchant_name)
            notes_text = normalize_merchant_name(t.notes) if t.notes else ""
            
            if t.upi:
                bank = normalize_merchant_name(t.upi.bank)
                gateway = normalize_merchant_name(t.upi.handle_or_gateway)
                upi_note = normalize_merchant_name(t.upi.note)
                # Repeat m_name and upi_note to artificially boost their TF-IDF/Hashing weights
                texts.append(f"{m_name} {m_name} {notes_text} {bank} {gateway} {upi_note} {upi_note}".strip())
            else:
                texts.append(f"{m_name} {notes_text}".strip())
        return texts

    def fit(self, X: List[Transaction], y=None):
        return self

    def transform(self, X: List[Transaction]) -> csr_matrix:
        texts = self._get_text(X)
        word_features = self.word_vectorizer.transform(texts)
        char_features = self.char_vectorizer.transform(texts)
        return hstack([word_features, char_features], format='csr')

class TransactionStructuredTransformer(BaseEstimator, TransformerMixin):
    """
    Extracts numeric and time-based features from transactions.
    """
    def fit(self, X: List[Transaction], y=None):
        return self

    def transform(self, X: List[Transaction]) -> np.ndarray:
        features = []
        for t in X:
            # 1. Amount processing (log-scaled)
            # Use log1p to handle 0 or small amounts gracefully
            amount = float(t.amount)
            sign = 1.0 if amount >= 0 else -1.0
            log_amount = sign * np.log1p(abs(amount))
            
            # 2. Time features
            dt = t.date
            hour = dt.hour
            is_weekend = 1.0 if dt.weekday() >= 5 else 0.0
            day_of_month = dt.day

            # We can encode hour as sine/cosine to capture cyclic nature (optional but good practice)
            hour_sin = np.sin(2 * np.pi * hour / 24.0)
            hour_cos = np.cos(2 * np.pi * hour / 24.0)

            # Same for day of month (approx 31 days)
            day_sin = np.sin(2 * np.pi * day_of_month / 31.0)
            day_cos = np.cos(2 * np.pi * day_of_month / 31.0)

            features.append([
                log_amount,
                is_weekend,
                hour_sin,
                hour_cos,
                day_sin,
                day_cos
            ])
            
        return np.array(features, dtype=np.float32)

class FeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Complete pipeline combining Text and Structured features.
    """
    def __init__(self, max_text_features: int = 10000):
        self.text_transformer = TransactionTextTransformer(max_features=max_text_features)
        self.struct_transformer = TransactionStructuredTransformer()
        
    def fit(self, X: List[Transaction], y=None):
        self.text_transformer.fit(X, y)
        self.struct_transformer.fit(X, y)
        return self
        
    def transform(self, X: List[Transaction]) -> csr_matrix:
        text_features = self.text_transformer.transform(X)
        struct_features = self.struct_transformer.transform(X)
        # Convert struct features to sparse and concatenate
        struct_sparse = csr_matrix(struct_features)
        return hstack([text_features, struct_sparse], format='csr')

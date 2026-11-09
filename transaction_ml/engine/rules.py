from typing import Dict, Optional

from transaction_ml.features.extractor import normalize_merchant_name
from transaction_ml.models.core import ModelConfig, Transaction


class MerchantMemory:
    """
    Exact-match merchant -> category rules learned from user corrections.
    Once a merchant has `memory_activation_threshold` consistent corrections,
    the rule bypasses the model.
    """

    def __init__(self, config: ModelConfig, records: Optional[Dict[str, Dict]] = None):
        self.config = config
        # normalized_merchant -> {"category", "subcategory", "streak"}
        self.records: Dict[str, Dict] = records or {}

    @staticmethod
    def key(transaction: Transaction) -> str:
        return normalize_merchant_name(transaction.merchant_name)

    def predict(self, transaction: Transaction) -> Optional[Dict]:
        record = self.records.get(self.key(transaction))
        if record and record["streak"] >= self.config.memory_activation_threshold:
            return record
        return None

    def update(self, transaction: Transaction, category: str, subcategory: Optional[str] = None) -> Dict:
        """Record a user correction. A conflicting correction resets the streak."""
        key = self.key(transaction)
        record = self.records.get(key)
        if record and record["category"] == category and record["subcategory"] == subcategory:
            record["streak"] += 1
        else:
            record = {"category": category, "subcategory": subcategory, "streak": 1}
            self.records[key] = record
        return record

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict

# Fixed set of top-level categories. The model can only ever predict one of these.
CANONICAL_CATEGORIES = [
    "Food & Dining",
    "Shopping",
    "Transport",
    "Housing",
    "Utilities",
    "Health & Wellness",
    "Entertainment",
    "Travel",
    "Education",
    "Personal Care",
    "Income",
    "Transfers",
    "Fees & Charges",
    "Business",
    "Shared / Receivables",  # split bills and IOUs
    "Private",               # hidden in the UI until "vault" is unlocked
    "Uncategorized",
]

UNCATEGORIZED = "Uncategorized"


@dataclass
class UPIData:
    type: str  # 'DR' or 'CR'
    txn_id: str
    merchant_or_person: str
    bank: str
    handle_or_gateway: str
    note: str


@dataclass
class Transaction:
    id: str
    merchant_name: str
    amount: float          # negative = money out, positive = money in
    date: datetime
    notes: Optional[str] = None
    account_id: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    splits: Optional[List[Dict]] = None   # [{"amount": float, "category": str}, ...]
    upi: Optional[UPIData] = None
    verified: bool = False  # True once the user has confirmed/corrected the category


@dataclass
class Prediction:
    transaction_id: str
    category: str
    confidence: float
    source: str  # "merchant_memory" | "model" | "fallback"
    subcategory: Optional[str] = None
    all_probabilities: Dict[str, float] = field(default_factory=dict)


@dataclass
class ModelConfig:
    # Model predictions are returned as Uncategorized (so they land in the Active
    # Learning queue instead of silently guessing) unless BOTH hold:
    #   - the model's probability for its top category >= min_confidence
    #   - the text resembles some verified transaction: cosine similarity of char
    #     n-grams >= min_similarity. Without this, totally unfamiliar merchants
    #     get assigned whichever category has the most examples.
    # Tuned on synthetic data (tests/test_engine.py); revisit on real statements.
    min_confidence: float = 0.3
    min_similarity: float = 0.3

    # How many consistent user corrections of the same merchant before the
    # merchant -> category rule bypasses the model entirely.
    memory_activation_threshold: int = 1

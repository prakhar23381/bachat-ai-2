import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict

# Category kinds: "expense" counts towards spending, "income" is money in,
# "split" is money paid on behalf of / to friends (not your own spending).
EXPENSE, INCOME, SPLIT = "expense", "income", "split"

UNCATEGORIZED = "Uncategorized"     # system: "needs a label"; not stored in the categories table
MONEY_RECEIVED = "Money received"   # system: every credit lands here
FRIENDS = "Friends / splits"

# Seeded into a new DB; editable afterwards (add / rename / merge / delete).
DEFAULT_CATEGORIES = [
    ("travel", EXPENSE),
    ("outside food", EXPENSE),
    ("mess food", EXPENSE),
    ("personal items", EXPENSE),
    ("health food", EXPENSE),
    ("phone", EXPENSE),
    ("entertainment", EXPENSE),
    (FRIENDS, SPLIT),
    (MONEY_RECEIVED, INCOME),
]
PROTECTED_CATEGORIES = {MONEY_RECEIVED}


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
    source: str = "statement"         # "sms" | "statement" | "manual"
    balance: Optional[float] = None   # account balance after this txn (from SMS)


@dataclass
class Prediction:
    transaction_id: str
    category: str
    confidence: float
    source: str  # "rule" | "merchant_memory" | "model" | "fallback"
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


def rrn_id_for(t: Transaction) -> Optional[str]:
    """UPI payments carry a 12-digit reference (RRN) in both SMS and statements."""
    ref = t.upi.txn_id if t.upi else None
    return f"rrn:{ref}" if ref and re.fullmatch(r"\d{12}", ref) else None

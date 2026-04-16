from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict

# Standard set of canonical categories (Level 1)
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
    "Shared / Receivables", # For split bills and IOUs
    "Private",              # For hidden/sensitive expenses
    "Uncategorized"
]

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
    """Represents a single financial transaction."""
    id: str
    merchant_name: str
    amount: float
    date: datetime
    notes: Optional[str] = None
    account_id: Optional[str] = None
    # For labeled data (either by user or ground truth in training)
    category: Optional[str] = None
    subcategory: Optional[str] = None
    
    # Support for shared bills (e.g. McDonald's: Part Food, Part Shared Receivable)
    splits: Optional[List[Dict]] = None
    
    # Optional destructured UPI metadata
    upi: Optional[UPIData] = None
    
    def __post_init__(self):
        if self.category and self.category not in CANONICAL_CATEGORIES:
            # Allow fallback or initialization checks, but primarily we enforce canonical parents
            pass

@dataclass
class Prediction:
    """Output prediction for a transaction."""
    transaction_id: str
    category: str
    confidence: float
    source: str  # "global_model", "user_model", "merchant_memory"
    subcategory: Optional[str] = None
    all_probabilities: Dict[str, float] = field(default_factory=dict)

@dataclass
class CategoryDef:
    """Definition for categories and user-defined subcategories."""
    name: str
    parent: Optional[str] = None # If None, it's a canonical category
    is_active: bool = True       # Subcategories need minimum evidence to become active
    is_hidden: bool = False      # Hide from UI without biometric auth (e.g. Private -> Alcohol)

@dataclass
class ModelConfig:
    """Global configuration for the ML system."""
    gold_confidence_threshold: float = 0.98
    silver_confidence_threshold: float = 0.93
    bronze_confidence_threshold: float = 0.70
    
    # Blending weights
    base_user_weight: float = 0.3
    base_global_weight: float = 0.7
    
    # Merchant memory
    memory_activation_threshold: int = 3  # N consecutive identical predictions
    
    # Subcategory
    subcat_activation_evidence: int = 10
    max_subcats_per_parent: int = 7

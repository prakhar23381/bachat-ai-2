from typing import Dict, List, Optional
from transaction_ml.models.core import ModelConfig, Transaction, CategoryDef
from transaction_ml.features.extractor import normalize_merchant_name

class MerchantMemory:
    """
    Stores and activates strict merchant -> category mappings.
    Bypasses the model once a pattern is established.
    """
    def __init__(self, config: ModelConfig):
        self.config = config
        # Mapping normalized_merchant -> {category: str, subcategory: str, streak: int}
        self.memory: Dict[str, Dict] = {}
        
    def predict(self, transaction: Transaction) -> Optional[Dict[str, str]]:
        """
        Returns {'category': cat, 'subcategory': subcat} if memory is active, else None.
        """
        norm_name = normalize_merchant_name(transaction.merchant_name)
        if norm_name in self.memory:
            record = self.memory[norm_name]
            if record['streak'] >= self.config.memory_activation_threshold:
                return {
                    'category': record['category'],
                    'subcategory': record['subcategory']
                }
        return None

    def update(self, transaction: Transaction, confirmed_category: str, confirmed_subcategory: Optional[str] = None):
        """
        Updates the streak for a merchant based on a user confirmation or gold/silver prediction.
        """
        norm_name = normalize_merchant_name(transaction.merchant_name)
        if norm_name not in self.memory:
            self.memory[norm_name] = {'category': confirmed_category, 'subcategory': confirmed_subcategory, 'streak': 1}
        else:
            record = self.memory[norm_name]
            # If the mapping matches the history, increment streak
            if record['category'] == confirmed_category and record['subcategory'] == confirmed_subcategory:
                record['streak'] += 1
            else:
                # Reset streak if the pattern changes
                self.memory[norm_name] = {'category': confirmed_category, 'subcategory': confirmed_subcategory, 'streak': 1}

class CategoryManager:
    """
    Manages custom subcategories aligned to canonical parents.
    """
    def __init__(self, config: ModelConfig):
        self.config = config
        self.subcategories: Dict[str, CategoryDef] = {}
        # Tracks how many transactions have been assigned to this subcategory
        self.evidence_counts: Dict[str, int] = {}
        
    def register_subcategory(self, name: str, parent: str):
        """Creates a new subcategory definition, initially inactive."""
        # Check parent limits
        active_for_parent = sum(
            1 for sub in self.subcategories.values() 
            if sub.parent == parent and sub.is_active
        )
        if active_for_parent >= self.config.max_subcats_per_parent:
            raise ValueError(f"Max active subcategories ({self.config.max_subcats_per_parent}) reached for parent '{parent}'.")
            
        if name not in self.subcategories:
            self.subcategories[name] = CategoryDef(name=name, parent=parent, is_active=False)
            self.evidence_counts[name] = 0

    def add_evidence(self, subcategory_name: str):
        """Adds a count to a subcategory. Activates it if threshold is met."""
        if subcategory_name in self.subcategories:
            self.evidence_counts[subcategory_name] += 1
            if self.evidence_counts[subcategory_name] >= self.config.subcat_activation_evidence:
                self.subcategories[subcategory_name].is_active = True

class LearningEngine:
    """
    Implements the semi-supervised learning triage logic (Gold, Silver, Bronze).
    """
    def __init__(self, config: ModelConfig):
        self.config = config
        
    def triage(self, confidence: float, is_user_correction: bool) -> str:
        """
        Returns 'gold', 'silver', 'bronze', or 'ignore'.
        """
        if is_user_correction:
            return 'gold'
            
        if confidence >= self.config.gold_confidence_threshold:
            # We treat extreme model confidence as 'silver' (low-weight learning) or maybe gold?
            # The prompt says: "High-confidence predictions (>0.93) -> low-weight learning".
            # "Gold: User corrections".
            pass # fallback to silver logic below
            
        if confidence >= self.config.silver_confidence_threshold:
            return 'silver'
            
        if confidence >= self.config.bronze_confidence_threshold:
            return 'bronze'
            
        return 'ignore' # Below bronze, requires user intervention

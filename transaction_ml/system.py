import base64
from typing import List, Tuple, Dict, Any, Optional
import numpy as np

from transaction_ml.models.core import Transaction, ModelConfig, Prediction, CANONICAL_CATEGORIES
from transaction_ml.features.extractor import FeatureExtractor
from transaction_ml.models.classifiers import GlobalClassifier, UserClassifier, ModelBlender
from transaction_ml.engine.rules import MerchantMemory, CategoryManager, LearningEngine
from transaction_ml.storage.db import EncryptedStorage

class TransactionEngine:
    """
    Main orchestration class for the Transaction ML System.
    """
    def __init__(self, db_path: str, encryption_key_base64: str, config: Optional[ModelConfig] = None):
        self.config = config or ModelConfig()
        
        # We need bytes for fernet
        # Expecting a base64 encoded string from environment/keychain
        try:
            key_bytes = base64.urlsafe_b64decode(encryption_key_base64)
            if len(key_bytes) != 32:
                # If it's a raw 32 byte string, just encode it
                key_bytes = base64.urlsafe_b64encode(encryption_key_base64.encode('utf-8')[:32].ljust(32, b'0'))
            else:
                key_bytes = encryption_key_base64.encode('utf-8')
        except Exception: # Fallback for easy testing
            key_bytes = base64.urlsafe_b64encode(encryption_key_base64.encode('utf-8')[:32].ljust(32, b'0'))
            
        self.storage = EncryptedStorage(db_path, key_bytes)
        
        self.feature_extractor = FeatureExtractor()
        
        # Load or init global model (in a real app, this loads joblib)
        self.global_clf = GlobalClassifier()
        
        # Load or init user model
        self.user_clf = UserClassifier()
        
        saved_weights = self.storage.load_model_weights('user_clf')
        if saved_weights:
            coef, intercept, classes = saved_weights
            self.user_clf.initialize_from_global(coef, intercept, classes)
            
        self.blender = ModelBlender(self.global_clf, self.user_clf, self.config)
        
        self.memory = MerchantMemory(self.config)
        self.category_manager = CategoryManager(self.config)
        self.learning_engine = LearningEngine(self.config)

    def bootstrap(self, X_train: List[Transaction], y_train: List[str]):
        """
        Mock initial training for the Global model and feature extractor.
        In production, the Global Model and FeatureExtractor are shipped pre-trained.
        """
        self.feature_extractor.fit(X_train)
        
        # Only fit the model on the very first transaction to simply initialize the weight matrices
        X_feats = self.feature_extractor.transform([X_train[0]])
        
        # Use partial_fit to enforce the full canonical category list
        self.global_clf.model.partial_fit(X_feats, [y_train[0]], classes=np.array(CANONICAL_CATEGORIES))
        
        # Initialize the user model weights identically to global
        self.user_clf.initialize_from_global(
            self.global_clf.model.coef_, 
            self.global_clf.model.intercept_,
            self.global_clf.model.classes_
        )

    def predict(self, transaction: Transaction, store: bool = True) -> Prediction:
        """
        Classifies a transaction, possibly bypassing the model via Merchant Memory.
        Optionally triggers semi-supervised learning if confidence is high.
        """
        # 1. Check Merchant Memory (Bypass)
        memory_result = self.memory.predict(transaction)
        if memory_result:
            pred = Prediction(
                transaction_id=transaction.id,
                category=memory_result['category'],
                subcategory=memory_result.get('subcategory'),
                confidence=1.0,
                source='merchant_memory'
            )
            if store:
                transaction.category = pred.category
                transaction.subcategory = pred.subcategory
                self.storage.insert_transaction(transaction)
            return pred
            
        # 2. Extract Features
        try:
            X_feat = self.feature_extractor.transform([transaction])
        except ValueError:
            # Not fitted yet
            pred = Prediction(
                transaction_id=transaction.id,
                category="Uncategorized",
                confidence=0.0,
                source='fallback'
            )
            return pred

        # 3. Model Blending
        category, confidence, all_probs = self.blender.predict(X_feat)
        pred = Prediction(
            transaction_id=transaction.id,
            category=category,
            confidence=confidence,
            source='model_blend',
            all_probabilities=all_probs
        )

        # 4. Semi-supervised Triage
        triage_action = self.learning_engine.triage(confidence, is_user_correction=False)
        if store and triage_action in ['silver', 'gold']:
            # Learn from high confidence automatically
            self.user_clf.partial_fit(X_feat, [category])
            self.memory.update(transaction, category)

        # 5. Store
        if store:
            transaction.category = pred.category
            self.storage.insert_transaction(transaction)

        return pred

    def correct(self, transaction: Transaction, correct_category: str, correct_subcategory: Optional[str] = None):
        """
        User provides the ground-truth correction. Triggers Gold-tier learning.
        """
        transaction.category = correct_category
        transaction.subcategory = correct_subcategory
        
        # Enforce canonical parents if needed
        # (Assuming correct_category is valid)
        
        # Update Subcategories
        if correct_subcategory:
            self.category_manager.register_subcategory(correct_subcategory, correct_category)
            self.category_manager.add_evidence(correct_subcategory)
            
        # Update Memory
        self.memory.update(transaction, correct_category, correct_subcategory)
        
        # Update Model
        X_feat = self.feature_extractor.transform([transaction])
        self.user_clf.partial_fit(X_feat, [correct_category])
        
        # Save Weights to DB
        if hasattr(self.user_clf.model, 'coef_'):
             self.storage.save_model_weights(
                 'user_clf',
                 self.user_clf.model.coef_,
                 self.user_clf.model.intercept_,
                 self.user_clf.model.classes_
             )
        
        # Store Transaction to DB
        self.storage.insert_transaction(transaction)

    def active_learning_cold_start(self, transactions: List[Transaction], max_queries: int = 20) -> List[Tuple[Transaction, Prediction]]:
        """
        Processes historical transactions, returning the ones with lowest confidence
        so the user can label them to jump-start the personalization model.
        """
        results = []
        for t in transactions:
            pred = self.predict(t, store=False)
            results.append((t, pred))
            
        # Sort by lowest confidence first, avoiding memory bypasses which have conf 1.0
        results.sort(key=lambda x: x[1].confidence)
        
        return results[:max_queries]

    def trigger_drift_control(self):
        """
        Periodically call this to prevent user model corruption by 
        reanchoring it towards the global baseline.
        """
        self.blender.apply_drift_control(anchor_weight=0.1) # pulls 10% towards global

import numpy as np
from sklearn.linear_model import SGDClassifier
from typing import List, Dict, Optional, Tuple
from transaction_ml.models.core import CANONICAL_CATEGORIES, ModelConfig

class GlobalClassifier:
    """
    The frozen global model trained offline on large public/synthetic datasets.
    """
    def __init__(self, model: Optional[SGDClassifier] = None):
        if model is None:
            # For development, initialize a blank one that must be fitted
            self.model = SGDClassifier(loss='log_loss', penalty='l2', alpha=1e-4)
        else:
            self.model = model
            
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not hasattr(self.model, "classes_") or self.model.classes_ is None:
            n_samples = X.shape[0] if len(X.shape) > 1 else 1
            n_classes = len(CANONICAL_CATEGORIES)
            return np.ones((n_samples, n_classes)) / n_classes
        return self.model.predict_proba(X)
        
    def get_classes(self) -> np.ndarray:
        if not hasattr(self.model, "classes_") or self.model.classes_ is None:
            return np.array(CANONICAL_CATEGORIES)
        return self.model.classes_
        
    def get_weights(self) -> Tuple[np.ndarray, np.ndarray]:
        if not hasattr(self.model, "coef_"):
            n_classes = len(CANONICAL_CATEGORIES)
            # Default zero weights fallback
            return np.zeros((n_classes, 1)), np.zeros((n_classes,))
        return self.model.coef_, self.model.intercept_


class UserClassifier:
    """
    Personalization layer that learns incrementally via partial_fit.
    """
    def __init__(self, classes: List[str] = CANONICAL_CATEGORIES):
        self.classes = np.array(classes)
        # Using optimal learning rate or adaptive (e.g. 'adaptive', eta0=0.1)
        self.model = SGDClassifier(loss='log_loss', penalty='l2', alpha=1e-4, 
                                   learning_rate='adaptive', eta0=0.1)
        self._is_initialized = False

    def initialize_from_global(self, global_coef: np.ndarray, global_intercept: np.ndarray, global_classes: np.ndarray):
        """Warm start the user model with global weights."""
        self.model.coef_ = np.copy(global_coef)
        self.model.intercept_ = np.copy(global_intercept)
        self.model.classes_ = np.copy(global_classes)
        self.classes = np.copy(global_classes)
        self._is_initialized = True

    def partial_fit(self, X: np.ndarray, y: List[str]):
        """Incrementally update the model with new user data."""
        if hasattr(self.model, "classes_") and self.model.classes_ is not None:
            self.model.partial_fit(X, y)
        else:
            self.model.partial_fit(X, y, classes=self.classes)
        self._is_initialized = True
        
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self._is_initialized:
            # If completely untrained and not initialized, return uniform probabilities
            n_samples = X.shape[0] if len(X.shape) > 1 else 1
            n_classes = len(self.classes)
            return np.ones((n_samples, n_classes)) / n_classes
        return self.model.predict_proba(X)

    def get_weights(self) -> Tuple[np.ndarray, np.ndarray]:
        return self.model.coef_, self.model.intercept_
        
    def set_weights(self, coef: np.ndarray, intercept: np.ndarray):
        self.model.coef_ = np.copy(coef)
        self.model.intercept_ = np.copy(intercept)


class ModelBlender:
    """
    Combines probabilities from global and user models and handles drift control.
    """
    def __init__(self, global_clf: GlobalClassifier, user_clf: UserClassifier, config: ModelConfig):
        self.global_clf = global_clf
        self.user_clf = user_clf
        self.config = config
        
    def predict(self, X: np.ndarray) -> Tuple[str, float, Dict[str, float]]:
        """
        Returns (predicted_category, confidence, all_probabilities)
        using the weighted blend of both models.
        """
        global_probs = self.global_clf.predict_proba(X)
        user_probs = self.user_clf.predict_proba(X)
        
        # Combine probabilities
        blended_probs = (self.config.base_global_weight * global_probs) + \
                        (self.config.base_user_weight * user_probs)
        
        classes = self.global_clf.get_classes()
        
        # We assume X is a single instance for engine prediction
        probs = blended_probs[0]
        
        # Find best prediction
        best_idx = np.argmax(probs)
        best_class = classes[best_idx]
        confidence = probs[best_idx]
        
        all_probs_dict = {classes[i]: probs[i] for i in range(len(classes))}
        
        return best_class, float(confidence), all_probs_dict
        
    def apply_drift_control(self, anchor_weight: float = 0.5):
        """
        Re-anchors the user model weights towards the global model to prevent drift.
        user_param = (anchor_weight * global_param) + ((1 - anchor_weight) * user_param)
        """
        g_coef, g_inter = self.global_clf.get_weights()
        u_coef, u_inter = self.user_clf.get_weights()
        
        new_coef = (anchor_weight * g_coef) + ((1.0 - anchor_weight) * u_coef)
        new_inter = (anchor_weight * g_inter) + ((1.0 - anchor_weight) * u_inter)
        
        self.user_clf.set_weights(new_coef, new_inter)

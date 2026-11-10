from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.linear_model import LogisticRegression


class CategoryClassifier:
    """
    Multinomial logistic regression retrained from scratch on every user-verified
    transaction. With personal-scale data (hundreds to low thousands of rows) a
    full refit takes well under a second, so no weights are persisted: the model
    is derived state, rebuilt from the DB at startup.
    """

    def __init__(self, C: float = 100.0):
        self.C = C
        self.model: Optional[LogisticRegression] = None
        self._train_anchors = None  # [payee-name vectors, note vectors] of the training rows

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    def fit(self, X, y: List[str], anchors):
        # Logistic regression needs at least two classes to learn anything.
        if len(set(y)) < 2:
            self.model = None
            return
        self.model = LogisticRegression(C=self.C, max_iter=2000)
        self.model.fit(X, y)
        self._train_anchors = anchors

    def predict(self, X, anchors) -> List[Optional[Tuple[str, float, Dict[str, float], float]]]:
        """
        Per row: (category, confidence, {category: prob}, similarity) or None if untrained.
        `similarity` is the best cosine similarity of this row's payee name to any training
        payee name, or of its note to any training note.
        """
        if not self.is_trained:
            return [None] * X.shape[0]
        classes = self.model.classes_
        similarity = np.max(
            [(a @ t.T).max(axis=1).toarray().ravel() for a, t in zip(anchors, self._train_anchors)], axis=0
        )
        out = []
        for probs, sim in zip(self.model.predict_proba(X), similarity):
            best = int(np.argmax(probs))
            out.append((str(classes[best]), float(probs[best]), {str(c): float(p) for c, p in zip(classes, probs)}, float(sim)))
        return out

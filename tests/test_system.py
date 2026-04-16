import pytest
import os
import tempfile
import base64
from datetime import datetime
import numpy as np

from transaction_ml.models.core import Transaction, CANONICAL_CATEGORIES, ModelConfig
from transaction_ml.system import TransactionEngine

@pytest.fixture
def temp_engine():
    # Use a temporary database
    fd, path = tempfile.mkstemp()
    # 32-byte key base64 encoded
    key = base64.urlsafe_b64encode(b"01234567890123456789012345678901").decode()
    
    # Fast configuration for testing
    config = ModelConfig(
        memory_activation_threshold=2, 
        subcat_activation_evidence=2,
        base_global_weight=0.5,
        base_user_weight=0.5
    )
    
    engine = TransactionEngine(db_path=path, encryption_key_base64=key, config=config)
    yield engine
    
    os.close(fd)
    os.remove(path)


def test_bootstrap_and_predict(temp_engine):
    # Create some mock transactions to bootstrap the global model
    # Normally this happens offline
    t1 = Transaction(id="1", merchant_name="Uber", amount=15.0, date=datetime.now())
    t2 = Transaction(id="2", merchant_name="Whole Foods", amount=45.0, date=datetime.now())
    t3 = Transaction(id="3", merchant_name="Shell Oil", amount=30.0, date=datetime.now())
    
    X_train = [t1, t2, t3]
    y_train = ["Transport", "Food & Dining", "Transport"]
    
    temp_engine.bootstrap(X_train, y_train)
    
    # Now predict a new transaction
    t_new = Transaction(id="4", merchant_name="Uber Trip", amount=20.0, date=datetime.now())
    pred = temp_engine.predict(t_new, store=False)
    
    assert pred.category in CANONICAL_CATEGORIES
    assert pred.confidence > 0.0

def test_merchant_memory(temp_engine):
    # Setup
    t1 = Transaction(id="1", merchant_name="Uber", amount=15.0, date=datetime.now())
    t2 = Transaction(id="2", merchant_name="Whole Foods", amount=45.0, date=datetime.now())
    temp_engine.bootstrap([t1, t2], ["Transport", "Food & Dining"])
    
    # User corrects "Uber" to "Transport" / "Rideshare" consistently
    t_m1 = Transaction(id="m1", merchant_name="Netflix", amount=15.0, date=datetime.now())
    t_m2 = Transaction(id="m2", merchant_name="Netflix", amount=15.0, date=datetime.now())
    t_m3 = Transaction(id="m3", merchant_name="Netflix", amount=15.0, date=datetime.now())

    # Correction 1
    temp_engine.correct(t_m1, "Entertainment", "Streaming")
    
    # Predict normally -> memory should NOT activate yet (threshold = 2)
    pred_2 = temp_engine.predict(t_m2, store=False)
    assert pred_2.source != 'merchant_memory'
    
    # Correction 2
    temp_engine.correct(t_m2, "Entertainment", "Streaming")
    
    # Predict normally -> memory SHOULD activate now
    pred_3 = temp_engine.predict(t_m3, store=False)
    assert pred_3.source == 'merchant_memory'
    assert pred_3.category == "Entertainment"
    assert pred_3.subcategory == "Streaming"
    assert pred_3.confidence == 1.0


def test_drift_control(temp_engine):
    t1 = Transaction(id="1", merchant_name="Uber", amount=15.0, date=datetime.now())
    temp_engine.bootstrap([t1], ["Transport"])
    
    g_weights, g_intercept = temp_engine.global_clf.get_weights()
    u_weights, u_intercept = temp_engine.user_clf.get_weights()
    
    # Initially they should be identical
    np.testing.assert_array_equal(g_weights, u_weights)
    
    # User makes a massive correction causing weights to diverge
    t_m1 = Transaction(id="m1", merchant_name="Starbucks", amount=5.0, date=datetime.now())
    for _ in range(10): 
        temp_engine.correct(t_m1, "Food & Dining")
        
    u_weights_after, _ = temp_engine.user_clf.get_weights()
    
    # Now they diverge
    assert not np.array_equal(g_weights, u_weights_after)
    
    # Trigger drift control
    temp_engine.trigger_drift_control()
    
    u_weights_drifted, _ = temp_engine.user_clf.get_weights()
    
    # Drifted weights should not equal the extreme local ones anymore
    assert not np.array_equal(u_weights_after, u_weights_drifted)
    
    # But they shouldn't perfectly equal the global ones either (anchoring is only 10%)
    assert not np.array_equal(g_weights, u_weights_drifted)


def test_encrypted_storage_retrieval(temp_engine):
    t1 = Transaction(
        id="t_store_1", 
        merchant_name="Secret Shop", 
        amount=100.50, 
        date=datetime.now(), 
        notes="Birthday gift",
        category="Shopping"
    )
    
    temp_engine.storage.insert_transaction(t1)
    
    retrieved = temp_engine.storage.get_all_transactions()
    
    assert len(retrieved) == 1
    t_ret = retrieved[0]
    
    assert t_ret.id == "t_store_1"
    assert t_ret.merchant_name == "Secret Shop"
    assert t_ret.amount == 100.50
    assert t_ret.notes == "Birthday gift"

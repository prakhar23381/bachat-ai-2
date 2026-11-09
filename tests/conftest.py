import csv
from datetime import datetime

import pytest

from transaction_ml.models.core import ModelConfig, Transaction
from transaction_ml.system import TransactionEngine

SECRET = "test-secret"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def engine(db_path):
    return TransactionEngine(db_path, SECRET, ModelConfig())


@pytest.fixture
def write_csv(tmp_path):
    def _write(rows, name="statement.csv"):
        path = tmp_path / name
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        return str(path)
    return _write


def txn(merchant, day=1, amount=-100.0, id=None):
    return Transaction(id=id or f"{merchant}-{day}-{amount}", merchant_name=merchant,
                       amount=amount, date=datetime(2026, 3, day))

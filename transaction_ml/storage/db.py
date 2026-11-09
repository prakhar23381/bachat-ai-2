import json
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set

from cryptography.fernet import Fernet

from transaction_ml.models.core import Transaction, UPIData

_TXN_COLUMNS = "id, encrypted_merchant, encrypted_amount, date, encrypted_notes, account_id, category, subcategory, splits, upi_data, verified"


class EncryptedStorage:
    """
    SQLite persistence. Merchant name, amount, notes and UPI metadata are
    Fernet-encrypted; date, category and splits are plaintext.
    """

    def __init__(self, db_path: str, fernet_key: bytes):
        self.db_path = db_path
        self.fernet = Fernet(fernet_key)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _encrypt(self, text: Optional[str]) -> Optional[bytes]:
        return self.fernet.encrypt(text.encode("utf-8")) if text else None

    def _decrypt(self, token: Optional[bytes]) -> Optional[str]:
        return self.fernet.decrypt(token).decode("utf-8") if token else None

    def _init_db(self):
        is_new = not os.path.exists(self.db_path) or os.path.getsize(self.db_path) == 0
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    encrypted_merchant BLOB,
                    encrypted_amount BLOB,
                    date TEXT,
                    encrypted_notes BLOB,
                    account_id TEXT,
                    category TEXT,
                    subcategory TEXT,
                    splits TEXT,
                    upi_data TEXT,
                    verified INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS merchant_memory (
                    normalized_merchant TEXT PRIMARY KEY,
                    category TEXT,
                    subcategory TEXT,
                    streak INTEGER
                )
            """)
            columns = [c[1] for c in conn.execute("PRAGMA table_info(transactions)")]
        self._migrate(columns, is_new)

    def _migrate(self, columns: List[str], is_new: bool):
        if "upi_data" not in columns:
            with self._connect() as conn:
                conn.execute("ALTER TABLE transactions ADD COLUMN upi_data TEXT")
        if "verified" not in columns:
            # Pre-cleanup DBs: existing categories were never user-verified (and many
            # were mislabelled by an old bug), so they will be re-predicted on the next
            # correction. Keep a copy first.
            if not is_new:
                shutil.copy2(self.db_path, self.db_path + ".bak-pre-verified")
            with self._connect() as conn:
                conn.execute("ALTER TABLE transactions ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")

    # ---- transactions -------------------------------------------------------

    def _row(self, t: Transaction) -> tuple:
        return (
            t.id,
            self._encrypt(t.merchant_name),
            self._encrypt(str(t.amount)),
            t.date.isoformat(),
            self._encrypt(t.notes),
            t.account_id,
            t.category,
            t.subcategory,
            json.dumps(t.splits) if t.splits else None,
            self._encrypt(json.dumps(t.upi.__dict__)) if t.upi else None,
            int(t.verified),
        )

    def insert_transactions(self, transactions: List[Transaction]):
        """Insert or overwrite by id, in a single DB transaction."""
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO transactions ({_TXN_COLUMNS}) VALUES ({', '.join('?' * 11)})",
                [self._row(t) for t in transactions],
            )

    def insert_transaction(self, t: Transaction):
        self.insert_transactions([t])

    def _load_upi(self, value) -> Optional[UPIData]:
        if not value:
            return None
        # Rows written before the cleanup hold plaintext JSON (str); newer rows are encrypted (bytes).
        raw = value if isinstance(value, str) else self._decrypt(value)
        return UPIData(**json.loads(raw))

    def _row_to_transaction(self, row) -> Transaction:
        date = datetime.fromisoformat(row[3])
        if date.tzinfo:  # all dates are naive local time; mixing breaks sorting
            date = date.astimezone().replace(tzinfo=None)
        return Transaction(
            id=row[0],
            merchant_name=self._decrypt(row[1]),
            amount=float(self._decrypt(row[2])),
            date=date,
            notes=self._decrypt(row[4]),
            account_id=row[5],
            category=row[6],
            subcategory=row[7],
            splits=json.loads(row[8]) if row[8] else None,
            upi=self._load_upi(row[9]),
            verified=bool(row[10]),
        )

    def get_all_transactions(self) -> List[Transaction]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT {_TXN_COLUMNS} FROM transactions").fetchall()
        return [self._row_to_transaction(r) for r in rows]

    def get_transaction(self, txn_id: str) -> Optional[Transaction]:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {_TXN_COLUMNS} FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        return self._row_to_transaction(row) if row else None

    def existing_ids(self, ids: List[str]) -> Set[str]:
        if not ids:
            return set()
        with self._connect() as conn:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT id FROM transactions WHERE id IN ({placeholders})", ids).fetchall()
        return {r[0] for r in rows}

    # ---- merchant memory ----------------------------------------------------

    def load_merchant_memory(self) -> Dict[str, Dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT normalized_merchant, category, subcategory, streak FROM merchant_memory").fetchall()
        return {r[0]: {"category": r[1], "subcategory": r[2], "streak": r[3]} for r in rows}

    def save_merchant_memory(self, key: str, record: Dict):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO merchant_memory (normalized_merchant, category, subcategory, streak) VALUES (?, ?, ?, ?)",
                (key, record["category"], record["subcategory"], record["streak"]),
            )

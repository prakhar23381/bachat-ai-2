import json
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from cryptography.fernet import Fernet

from transaction_ml.models.core import (
    DEFAULT_CATEGORIES, MONEY_RECEIVED, UNCATEGORIZED, Transaction, UPIData, rrn_id_for,
)

SCHEMA_VERSION = 2

_TXN_COLUMNS = (
    "id, encrypted_merchant, encrypted_amount, date, encrypted_notes, account_id, category, "
    "subcategory, splits, upi_data, verified, source, encrypted_balance"
)

_SCHEMA = """
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
    upi_data BLOB,
    verified INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'statement',
    encrypted_balance BLOB
);
CREATE TABLE IF NOT EXISTS merchant_memory (
    normalized_merchant TEXT PRIMARY KEY,
    category TEXT,
    subcategory TEXT,
    streak INTEGER
);
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS unparsed_sms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT,
    encrypted_text BLOB
);
"""

# Columns added after the first release, for ALTER TABLE on old DBs.
_ADDED_COLUMNS = {
    "upi_data": "BLOB",
    "verified": "INTEGER NOT NULL DEFAULT 0",
    "source": "TEXT NOT NULL DEFAULT 'statement'",
    "encrypted_balance": "BLOB",
}


class EncryptedStorage:
    """
    SQLite persistence. Merchant, amount, notes, balance, UPI metadata and raw
    unparsed SMS are Fernet-encrypted; dates, categories and splits are plaintext.
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

    # ---- schema / migrations ------------------------------------------------

    def _init_db(self):
        is_new = not os.path.exists(self.db_path) or os.path.getsize(self.db_path) == 0
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not is_new and version < SCHEMA_VERSION:
            shutil.copy2(self.db_path, f"{self.db_path}.bak-v{version}")

        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {c[1] for c in conn.execute("PRAGMA table_info(transactions)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {decl}")
            if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO categories (name, kind, position) VALUES (?, ?, ?)",
                    [(n, k, i) for i, (n, k) in enumerate(DEFAULT_CATEGORIES)],
                )

        if not is_new and version < 2:
            self._migrate_to_v2()
        with self._connect() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_to_v2(self):
        """
        v2 replaced the old 17 hardcoded categories with the user's own list.
        Old labels are unreliable (an old bug shuffled them), so:
          credits -> Money received; debits with a category not in the new list -> Uncategorized,
          and everything is unverified. Transactions with a UPI reference are re-keyed
          to "rrn:<ref>" so SMS and statement copies of the same payment dedupe.
        """
        valid = set(self.category_names())
        rows = self.get_all_transactions()
        with self._connect() as conn:
            conn.execute("DELETE FROM merchant_memory WHERE category NOT IN (%s)" % ",".join("?" * len(valid)), list(valid))
            for t in rows:
                old_id = t.id
                if t.amount > 0:
                    t.category = MONEY_RECEIVED
                elif t.category not in valid:
                    t.category, t.verified = UNCATEGORIZED, False
                rrn_id = rrn_id_for(t)
                if rrn_id and rrn_id != old_id:
                    conn.execute("DELETE FROM transactions WHERE id = ?", (old_id,))
                    t.id = rrn_id
                conn.execute(f"INSERT OR REPLACE INTO transactions ({_TXN_COLUMNS}) VALUES ({_PLACEHOLDERS})", self._row(t))

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
            t.source,
            self._encrypt(str(t.balance)) if t.balance is not None else None,
        )

    def insert_transactions(self, transactions: List[Transaction]):
        """Insert or overwrite by id, in a single DB transaction."""
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO transactions ({_TXN_COLUMNS}) VALUES ({_PLACEHOLDERS})",
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
        balance = self._decrypt(row[12])
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
            source=row[11],
            balance=float(balance) if balance is not None else None,
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

    def recategorize(self, old: str, new: str, unverify: bool = False):
        with self._connect() as conn:
            conn.execute(
                "UPDATE transactions SET category = ?, verified = CASE WHEN ? THEN 0 ELSE verified END WHERE category = ?",
                (new, int(unverify), old),
            )
            if unverify:
                conn.execute("DELETE FROM merchant_memory WHERE category = ?", (old,))
            else:
                conn.execute("UPDATE merchant_memory SET category = ? WHERE category = ?", (new, old))

    # ---- categories ----------------------------------------------------------

    def get_categories(self) -> List[Tuple[str, str]]:
        with self._connect() as conn:
            return conn.execute("SELECT name, kind FROM categories ORDER BY position").fetchall()

    def category_names(self) -> List[str]:
        return [n for n, _ in self.get_categories()]

    def add_category(self, name: str, kind: str):
        with self._connect() as conn:
            pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories").fetchone()[0]
            conn.execute("INSERT INTO categories (name, kind, position) VALUES (?, ?, ?)", (name, kind, pos))

    def rename_category(self, old: str, new: str):
        with self._connect() as conn:
            conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old))

    def delete_category(self, name: str):
        with self._connect() as conn:
            conn.execute("DELETE FROM categories WHERE name = ?", (name,))

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

    # ---- misc -----------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def save_unparsed_sms(self, text: str, received_at: Optional[datetime]):
        """Keep SMS the parser didn't recognise, to improve the parser later."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO unparsed_sms (received_at, encrypted_text) VALUES (?, ?)",
                (received_at.isoformat() if received_at else None, self._encrypt(text)),
            )

    def get_unparsed_sms(self) -> List[Tuple[Optional[str], str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT received_at, encrypted_text FROM unparsed_sms ORDER BY id").fetchall()
        return [(r[0], self._decrypt(r[1])) for r in rows]


_PLACEHOLDERS = ", ".join("?" * len(_TXN_COLUMNS.split(",")))


import sqlite3
import json
import numpy as np
import io
from typing import List, Optional, Dict, Tuple
from datetime import datetime
from cryptography.fernet import Fernet
from transaction_ml.models.core import Transaction, CategoryDef, UPIData

class EncryptedStorage:
    """
    Handles SQLite operations with field-level encryption for sensitive data.
    """
    def __init__(self, db_path: str, encryption_key: bytes):
        """
        encryption_key must be a valid 32-url-safe-base64-encoded bytes (Fernet key).
        """
        self.db_path = db_path
        self.fernet = Fernet(encryption_key)
        self._init_db()

    def _encrypt(self, text: Optional[str]) -> Optional[bytes]:
        if not text:
            return None
        return self.fernet.encrypt(text.encode('utf-8'))

    def _decrypt(self, token: Optional[bytes]) -> Optional[str]:
        if not token:
            return None
        return self.fernet.decrypt(token).decode('utf-8')

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Transactions table
            cursor.execute('''
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
                    upi_data TEXT
                )
            ''')

            # Migration: Ensure upi_data column exists if db was created prior to upi_data addition
            cursor.execute("PRAGMA table_info(transactions)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'upi_data' not in columns:
                cursor.execute('ALTER TABLE transactions ADD COLUMN upi_data TEXT')

            # Subcategories table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subcategories (
                    name TEXT PRIMARY KEY,
                    parent TEXT,
                    is_active INTEGER,
                    evidence_count INTEGER
                )
            ''')
            
            # Merchant memory table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS merchant_memory (
                    normalized_merchant TEXT PRIMARY KEY,
                    category TEXT,
                    subcategory TEXT,
                    streak INTEGER
                )
            ''')
            
            # Model weights table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS model_weights (
                    model_id TEXT PRIMARY KEY,
                    coef_bytes BLOB,
                    intercept_bytes BLOB,
                    classes_json TEXT,
                    updated_at TEXT
                )
            ''')
            conn.commit()

    def insert_transaction(self, t: Transaction):
        enc_merchant = self._encrypt(t.merchant_name)
        enc_amount = self._encrypt(str(t.amount))
        enc_notes = self._encrypt(t.notes)
        
        splits_json = json.dumps(t.splits) if t.splits else None
        upi_json = json.dumps(t.upi.__dict__) if t.upi else None
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO transactions 
                (id, encrypted_merchant, encrypted_amount, date, encrypted_notes, account_id, category, subcategory, splits, upi_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (t.id, enc_merchant, enc_amount, t.date.isoformat(), enc_notes, t.account_id, t.category, t.subcategory, splits_json, upi_json))

    def get_all_transactions(self) -> List[Transaction]:
        transactions = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, encrypted_merchant, encrypted_amount, date, encrypted_notes, account_id, category, subcategory, splits, upi_data FROM transactions')
            
            for row in cursor.fetchall():
                merchant_name = self._decrypt(row[1])
                amount = float(self._decrypt(row[2]))
                notes = self._decrypt(row[4]) if row[4] else None
                splits = json.loads(row[8]) if row[8] else None
                upi = UPIData(**json.loads(row[9])) if row[9] else None
                
                transactions.append(Transaction(
                    id=row[0],
                    merchant_name=merchant_name,
                    amount=amount,
                    date=datetime.fromisoformat(row[3]),
                    notes=notes,
                    account_id=row[5],
                    category=row[6],
                    subcategory=row[7],
                    splits=splits,
                    upi=upi
                ))
        return transactions

    def save_model_weights(self, model_id: str, coef: np.ndarray, intercept: np.ndarray, classes: np.ndarray):
        # Serialize numpy arrays
        coef_out = io.BytesIO()
        np.save(coef_out, coef)
        
        inter_out = io.BytesIO()
        np.save(inter_out, intercept)
        
        classes_json = json.dumps(classes.tolist())
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO model_weights 
                (model_id, coef_bytes, intercept_bytes, classes_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (model_id, coef_out.getvalue(), inter_out.getvalue(), classes_json, datetime.now().isoformat()))

    def load_model_weights(self, model_id: str) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT coef_bytes, intercept_bytes, classes_json FROM model_weights WHERE model_id = ?', (model_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            coef_in = io.BytesIO(row[0])
            coef = np.load(coef_in)
            
            inter_in = io.BytesIO(row[1])
            intercept = np.load(inter_in)
            
            classes = np.array(json.loads(row[2]))
            
            return coef, intercept, classes

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import os
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import hashlib

from transaction_ml.system import TransactionEngine
from transaction_ml.models.core import CANONICAL_CATEGORIES, Transaction
from transaction_ml.ingestion.excel_parser import parse_bank_statement

app = FastAPI(title="Transaction ML Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Since it's a local tool
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Engine initialization
DB_PATH = "transactions.db"
KEY = "01234567890123456789012345678901"
engine = TransactionEngine(db_path=DB_PATH, encryption_key_base64=KEY)

class TransactionResponse(BaseModel):
    id: str
    merchant_name: str
    amount: float
    date: str
    category: Optional[str]
    subcategory: Optional[str]
    
    @classmethod
    def from_domain(cls, t):
        return cls(
            id=t.id,
            merchant_name=t.merchant_name,
            amount=t.amount,
            date=t.date.isoformat(),
            category=t.category,
            subcategory=t.subcategory
        )

class CorrectionRequest(BaseModel):
    category: str
    subcategory: Optional[str] = None

@app.get("/api/categories")
def get_categories():
    return {"categories": CANONICAL_CATEGORIES}

@app.get("/api/transactions", response_model=List[TransactionResponse])
def get_transactions():
    transactions = engine.storage.get_all_transactions()
    # Sort by date descending
    transactions.sort(key=lambda x: x.date, reverse=True)
    return [TransactionResponse.from_domain(t) for t in transactions]

@app.post("/api/upload")
async def upload_statement(file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.csv', '.xls')):
        raise HTTPException(status_code=400, detail="Only Excel or CSV files are supported.")
        
    # Save uploaded file to temp path
    fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(file.filename)[1])
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(await file.read())
            
        # Parse it
        try:
            transactions = parse_bank_statement(temp_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")
            
        # Optional: Bootstrap if blank
        if not engine.user_clf._is_initialized and len(transactions) > 0:
            engine.bootstrap(transactions, ["Uncategorized"] * len(transactions)) # Bootstraps on 1st element
            
        # Batch Predict and Store
        for t in transactions:
            # We predict and store in the DB
            engine.predict(t, store=True)
            
        return {"status": "success", "count": len(transactions)}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@app.post("/api/transactions/{txn_id}/correct")
def correct_transaction(txn_id: str, payload: CorrectionRequest):
    # Retrieve transaction from DB
    transactions = engine.storage.get_all_transactions()
    target = next((t for t in transactions if t.id == txn_id), None)
    
    if not target:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    engine.correct(target, payload.category, payload.subcategory)
    return {"status": "success", "transaction": TransactionResponse.from_domain(target)}

class SplitRequest(BaseModel):
    splits: List[dict]

@app.post("/api/transactions/{txn_id}/split")
def split_transaction(txn_id: str, payload: SplitRequest):
    transactions = engine.storage.get_all_transactions()
    target = next((t for t in transactions if t.id == txn_id), None)
    
    if not target:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    # Ideally, we calculate and attach the splits array 
    # For now, we just mock the payload update to DB
    target.splits = payload.splits
    engine.storage.insert_transaction(target)
    return {"status": "success", "splits": target.splits}

class ManualTransactionRequest(BaseModel):
    merchant_name: str
    amount: float
    date: str
    notes: Optional[str] = None

@app.post("/api/transactions/manual")
def add_manual_transaction(payload: ManualTransactionRequest):
    try:
        date_val = datetime.fromisoformat(payload.date.replace("Z", "+00:00"))
    except ValueError:
        date_val = datetime.now()
        
    h = hashlib.md5(f"{date_val.isoformat()}_{payload.merchant_name}_{payload.amount}".encode()).hexdigest()
    
    t = Transaction(
        id=h,
        merchant_name=payload.merchant_name,
        amount=payload.amount,
        date=date_val,
        notes=payload.notes,
        category="Uncategorized"
    )
    
    pred = engine.predict(t, store=True)
    return {"status": "success", "transaction": TransactionResponse.from_domain(t)}

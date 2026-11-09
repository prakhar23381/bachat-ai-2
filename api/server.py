import hashlib
import os
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from transaction_ml.ingestion.excel_parser import parse_bank_statement
from transaction_ml.models.core import CANONICAL_CATEGORIES, Transaction
from transaction_ml.system import TransactionEngine, load_secret

app = FastAPI(title="Bachat AI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("BACHAT_DB", "transactions.db")
engine = TransactionEngine(db_path=DB_PATH, secret=load_secret())


class TransactionResponse(BaseModel):
    id: str
    merchant_name: str
    amount: float
    date: str
    notes: Optional[str] = None
    category: Optional[str]
    subcategory: Optional[str]
    splits: Optional[List[Dict]] = None
    upi: Optional[Dict] = None
    verified: bool = False

    @classmethod
    def from_domain(cls, t: Transaction) -> "TransactionResponse":
        return cls(
            id=t.id,
            merchant_name=t.merchant_name,
            amount=t.amount,
            date=t.date.isoformat(),
            notes=t.notes,
            category=t.category,
            subcategory=t.subcategory,
            splits=t.splits,
            upi=t.upi.__dict__ if t.upi else None,
            verified=t.verified,
        )


class CorrectionRequest(BaseModel):
    category: str
    subcategory: Optional[str] = None


class Split(BaseModel):
    amount: float
    category: str


class SplitRequest(BaseModel):
    splits: List[Split]


class ManualTransactionRequest(BaseModel):
    merchant_name: str
    amount: float
    date: str
    notes: Optional[str] = None


def _get_or_404(txn_id: str) -> Transaction:
    t = engine.storage.get_transaction(txn_id)
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return t


@app.get("/api/categories")
def get_categories():
    return {"categories": CANONICAL_CATEGORIES}


@app.get("/api/transactions", response_model=List[TransactionResponse])
def get_transactions():
    transactions = sorted(engine.storage.get_all_transactions(), key=lambda t: t.date, reverse=True)
    return [TransactionResponse.from_domain(t) for t in transactions]


@app.post("/api/upload")
async def upload_statement(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in (".xlsx", ".csv"):
        raise HTTPException(status_code=400, detail="Only .xlsx or .csv statements are supported.")

    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(await file.read())
        try:
            transactions = parse_bank_statement(temp_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse file: {e}")
        added = engine.ingest(transactions)
        return {"status": "success", "parsed": len(transactions), "added": added}
    finally:
        os.remove(temp_path)


@app.post("/api/transactions/{txn_id}/correct")
def correct_transaction(txn_id: str, payload: CorrectionRequest):
    target = _get_or_404(txn_id)
    try:
        reclassified = engine.correct(target, payload.category, payload.subcategory)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "transaction": TransactionResponse.from_domain(target), "reclassified": reclassified}


@app.post("/api/transactions/{txn_id}/split")
def split_transaction(txn_id: str, payload: SplitRequest):
    target = _get_or_404(txn_id)
    splits = [s.model_dump() for s in payload.splits]
    if any(s["category"] not in CANONICAL_CATEGORIES for s in splits):
        raise HTTPException(status_code=400, detail="Unknown category in splits")
    if abs(sum(s["amount"] for s in splits) - target.amount) > 0.01:
        raise HTTPException(status_code=400, detail="Split amounts must add up to the transaction amount")
    target.splits = splits
    engine.storage.insert_transaction(target)
    return {"status": "success", "splits": target.splits}


@app.post("/api/transactions/manual")
def add_manual_transaction(payload: ManualTransactionRequest):
    try:
        date_val = datetime.fromisoformat(payload.date.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be ISO-8601")
    if date_val.tzinfo:  # store naive local time, like statement rows
        date_val = date_val.astimezone().replace(tzinfo=None)

    t = Transaction(
        id=hashlib.md5(f"{date_val.isoformat()}_{payload.merchant_name}_{payload.amount}".encode()).hexdigest(),
        merchant_name=payload.merchant_name,
        amount=payload.amount,
        date=date_val,
        notes=payload.notes,
    )
    engine.ingest([t])
    return {"status": "success", "transaction": TransactionResponse.from_domain(t)}

import hashlib
import hmac
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from transaction_ml.ingestion.excel_parser import parse_bank_statement
from transaction_ml.models.core import EXPENSE, Transaction
from transaction_ml.system import TransactionEngine, load_secret

DB_PATH = os.environ.get("BACHAT_DB", "transactions.db")
API_TOKEN = os.environ.get("BACHAT_API_TOKEN", "")
if len(API_TOKEN) < 16:
    raise RuntimeError("Set BACHAT_API_TOKEN to a random string of at least 16 characters.")

engine = TransactionEngine(db_path=DB_PATH, secret=load_secret())


def require_token(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), API_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


app = FastAPI(title="Bachat AI")
api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _naive_local(value: str) -> datetime:
    """Parse ISO-8601; timezone-aware values are converted to naive local time."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Not an ISO-8601 date: {value!r}")
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


# ---- models -------------------------------------------------------------------

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
    source: str = "statement"

    @classmethod
    def from_domain(cls, t: Transaction) -> "TransactionResponse":
        return cls(
            id=t.id, merchant_name=t.merchant_name, amount=t.amount, date=t.date.isoformat(),
            notes=t.notes, category=t.category, subcategory=t.subcategory, splits=t.splits,
            upi=t.upi.__dict__ if t.upi else None, verified=t.verified, source=t.source,
        )


class SmsRequest(BaseModel):
    text: str
    received_at: Optional[str] = None   # ISO-8601; the iOS Shortcut sends "Current Date"


class CorrectionRequest(BaseModel):
    category: str
    subcategory: Optional[str] = None


class CategoryRequest(BaseModel):
    name: str
    kind: str = EXPENSE


class RenameRequest(BaseModel):
    new_name: str


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


def _value_error_to_400(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- capture -----------------------------------------------------------------

@api.post("/sms")
def receive_sms(payload: SmsRequest):
    """Called by the iPhone Shortcut for every bank SMS."""
    received_at = _naive_local(payload.received_at) if payload.received_at else datetime.now()
    result = engine.ingest_sms(payload.text, received_at)
    return {
        "status": result.status,
        "transaction": TransactionResponse.from_domain(result.transaction) if result.transaction else None,
        "needs_review": result.needs_review,
        "balance_gap": result.balance_gap,
    }


@api.post("/upload")
async def upload_statement(file: UploadFile = File(...)):
    """Statement import, for backfilling past months or catching missed SMS."""
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
        return {"status": "success", "parsed": len(transactions), "added": len(added)}
    finally:
        os.remove(temp_path)


@api.post("/transactions/manual")
def add_manual_transaction(payload: ManualTransactionRequest):
    date_val = _naive_local(payload.date)
    t = Transaction(
        id=hashlib.md5(f"{date_val.isoformat()}_{payload.merchant_name}_{payload.amount}".encode()).hexdigest(),
        merchant_name=payload.merchant_name, amount=payload.amount, date=date_val,
        notes=payload.notes, source="manual",
    )
    engine.ingest([t])
    return {"status": "success", "transaction": TransactionResponse.from_domain(t)}


# ---- transactions --------------------------------------------------------------

@api.get("/transactions", response_model=List[TransactionResponse])
def get_transactions():
    transactions = sorted(engine.storage.get_all_transactions(), key=lambda t: t.date, reverse=True)
    return [TransactionResponse.from_domain(t) for t in transactions]


@api.post("/transactions/{txn_id}/correct")
def correct_transaction(txn_id: str, payload: CorrectionRequest):
    target = _get_or_404(txn_id)
    reclassified = _value_error_to_400(engine.correct, target, payload.category, payload.subcategory)
    return {"status": "success", "transaction": TransactionResponse.from_domain(target), "reclassified": reclassified}


@api.post("/transactions/{txn_id}/split")
def split_transaction(txn_id: str, payload: SplitRequest):
    target = _get_or_404(txn_id)
    splits = [s.model_dump() for s in payload.splits]
    valid = set(engine.storage.category_names())
    if any(s["category"] not in valid for s in splits):
        raise HTTPException(status_code=400, detail="Unknown category in splits")
    if abs(sum(s["amount"] for s in splits) - target.amount) > 0.01:
        raise HTTPException(status_code=400, detail="Split amounts must add up to the transaction amount")
    target.splits = splits
    engine.storage.insert_transaction(target)
    return {"status": "success", "splits": target.splits}


# ---- categories ----------------------------------------------------------------

@api.get("/categories")
def get_categories():
    return {"categories": [{"name": n, "kind": k} for n, k in engine.categories()]}


@api.post("/categories")
def add_category(payload: CategoryRequest):
    _value_error_to_400(engine.add_category, payload.name, payload.kind)
    return get_categories()


@api.patch("/categories/{name}")
def rename_category(name: str, payload: RenameRequest):
    _value_error_to_400(engine.rename_category, name, payload.new_name)
    return get_categories()


@api.delete("/categories/{name}")
def delete_category(name: str):
    _value_error_to_400(engine.delete_category, name)
    return get_categories()


app.include_router(api)

# Serve the built dashboard (npm run build) from the same origin in production.
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="dashboard")

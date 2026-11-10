import csv
import hashlib
import re
from collections import Counter
from typing import List, Optional, Set

import pandas as pd

from transaction_ml.ingestion.upi_parser import parse_upi_string
from transaction_ml.models.core import Transaction, rrn_id_for

NARRATION_WORDS = {"particulars", "narration", "description", "details", "remarks"}
DEBIT_WORDS = {"debit", "withdrawal", "withdrawals", "dr"}
CREDIT_WORDS = {"credit", "deposit", "deposits", "cr"}
AMOUNT_WORDS = {"amount", "amt"}


def _tokens(header: str) -> Set[str]:
    # Whole-word matching: substring matching made "description" look like a credit column.
    return set(re.findall(r"[a-z]+", header.lower()))


def _read_raw(filepath: str) -> pd.DataFrame:
    if filepath.lower().endswith(".csv"):
        # Bank CSVs often start with preamble lines that have fewer fields than the
        # table, which breaks pd.read_csv, so pad the rows by hand.
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        width = max((len(r) for r in rows), default=0)
        return pd.DataFrame([r + [None] * (width - len(r)) for r in rows])
    return pd.read_excel(filepath, engine="openpyxl", header=None)


def find_header_row_index(df: pd.DataFrame) -> int:
    """First row that mentions a date and a narration-like column."""
    for idx, row in df.iterrows():
        words = _tokens(" ".join(str(v) for v in row.values))
        if ("date" in words or "dt" in words) and words & NARRATION_WORDS:
            return idx
    return 0


def clean_amount(val) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r"[^0-9.\-]", "", str(val))  # strips commas, currency symbols, "Cr"/"Dr"
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _first(headers: List[str], predicate) -> Optional[str]:
    return next((h for h in headers if predicate(_tokens(h))), None)


def parse_bank_statement(filepath: str) -> List[Transaction]:
    """Parse an .xlsx or .csv bank statement by detecting its columns from header names."""
    df_raw = _read_raw(filepath)
    header_idx = find_header_row_index(df_raw)
    headers = [str(c).strip().lower() for c in df_raw.iloc[header_idx]]
    df = df_raw.iloc[header_idx + 1:].copy()
    df.columns = headers
    df = df.dropna(how="all")

    date_col = _first(headers, lambda w: ("date" in w or "dt" in w) and "value" not in w) \
        or _first(headers, lambda w: "date" in w or "dt" in w)
    merchant_col = _first(headers, lambda w: bool(w & NARRATION_WORDS))
    debit_col = _first(headers, lambda w: bool(w & DEBIT_WORDS) and not w & CREDIT_WORDS)
    credit_col = _first(headers, lambda w: bool(w & CREDIT_WORDS) and not w & DEBIT_WORDS)
    drcr_col = _first(headers, lambda w: bool(w & DEBIT_WORDS) and bool(w & CREDIT_WORDS))
    amount_col = _first(headers, lambda w: bool(w & AMOUNT_WORDS) and not w & (DEBIT_WORDS | CREDIT_WORDS))

    if not date_col or not merchant_col:
        raise ValueError(f"Could not detect Date or Narration columns. Headers found: {headers}")
    if not (debit_col and credit_col) and not amount_col:
        raise ValueError(f"Could not detect amount columns. Headers found: {headers}")

    transactions = []
    seen = Counter()
    for _, row in df.iterrows():
        date_val = pd.to_datetime(row[date_col], errors="coerce", dayfirst=True)
        if pd.isna(date_val):
            continue  # footer / summary rows

        raw_merchant = str(row[merchant_col]).strip()
        if raw_merchant.lower() in ("nan", "none", ""):
            continue

        if debit_col and credit_col:
            credit = clean_amount(row[credit_col])
            amount = credit if credit > 0 else -abs(clean_amount(row[debit_col]))
        else:
            amount = clean_amount(row[amount_col])
            if drcr_col:
                is_credit = "cr" in str(row[drcr_col]).lower()
                amount = abs(amount) if is_credit else -abs(amount)

        upi = parse_upi_string(raw_merchant)

        # Deterministic id so re-uploading the same statement is idempotent.
        # Repeats of an identical row (same day, text, amount) get a suffix.
        base = f"{date_val.isoformat()}_{raw_merchant}_{amount}"
        n = seen[base]
        seen[base] += 1
        txn_id = hashlib.md5((base if n == 0 else f"{base}_{n}").encode()).hexdigest()

        t = Transaction(
            id=txn_id,
            merchant_name=upi.merchant_or_person if upi else raw_merchant,
            amount=amount,
            date=date_val.to_pydatetime(),
            upi=upi,
            source="statement",
        )
        # UPI rows are keyed by their reference number so they dedupe against SMS.
        t.id = rrn_id_for(t) or t.id
        transactions.append(t)
    return transactions

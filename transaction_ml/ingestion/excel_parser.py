import pandas as pd
import numpy as np
import hashlib
from typing import List
from transaction_ml.models.core import Transaction
from transaction_ml.ingestion.upi_parser import parse_upi_string

def find_header_row_index(df: pd.DataFrame) -> int:
    """Finds the index of the row most likely to be the actual column headers."""
    # We look for a row containing 'date' and ('particulars' or 'description' or 'narration')
    for idx, row in df.iterrows():
        row_str = " ".join([str(val).lower() for val in row.values])
        if 'date' in row_str and pd.notna(row_str):
            if any(k in row_str for k in ['particulars', 'narration', 'description', 'details']):
                return idx
    return 0 # Fallback to 0

def clean_amount(val) -> float:
    if pd.isna(val) or val == '' or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).replace(',', '').strip()
    try:
        return float(val_str)
    except:
        return 0.0

def parse_bank_statement(filepath: str) -> List[Transaction]:
    """
    Parses an Excel or CSV file dynamically using heuristic column naming.
    """
    if filepath.endswith('.csv'):
        df_raw = pd.read_csv(filepath, header=None)
    else:
        df_raw = pd.read_excel(filepath, engine='openpyxl', header=None)
        
    header_idx = find_header_row_index(df_raw)
    
    # Re-read or re-construct with correct headers
    df = df_raw.iloc[header_idx+1:].copy()
    headers = [str(col).strip().lower() for col in df_raw.iloc[header_idx]]
    df.columns = headers
    
    # Drop completely empty rows
    df = df.dropna(how='all')
    
    # Identify key columns by sub-string matching
    date_col = next((c for c in headers if 'date' in c and 'value' not in c), None) # Prefer 'Transaction Date' over 'Value Date'
    if not date_col:
        date_col = next((c for c in headers if 'date' in c), None)
        
    merchant_col = next((c for c in headers if any(k in c for k in ['particulars', 'narration', 'description'])), None)
    
    debit_col = next((c for c in headers if 'debit' in c or 'withdrawal' in c or 'dr' in c), None)
    credit_col = next((c for c in headers if 'credit' in c or 'deposit' in c or 'cr' in c), None)
    amount_col = next((c for c in headers if 'amount' in c and 'dr' not in c and 'cr' not in c), None)

    if not date_col or not merchant_col:
        raise ValueError(f"Could not autodetect Date or Merchant columns. Headers found: {headers}")
        
    transactions = []
    
    for _, row in df.iterrows():
        # Parse Date
        date_val = pd.to_datetime(row[date_col], errors='coerce')
        if pd.isna(date_val): 
            continue # Skip non-transaction rows (like Footers)
            
        raw_merchant = str(row[merchant_col]).strip()
        if raw_merchant.lower() in ['nan', 'none', '']: continue
        
        # Extract metadata from generic UPI strings
        upi_data = parse_upi_string(raw_merchant)
        merchant_name = upi_data.merchant_or_person if upi_data else raw_merchant
        
        # Calculate amount
        amount = 0.0
        if debit_col and credit_col:
            d = clean_amount(row[debit_col])
            c = clean_amount(row[credit_col])
            if c > 0:
                amount = c
            else:
                amount = -d
        elif amount_col:
            amount = clean_amount(row[amount_col])
            
        # Create a stable deterministic ID
        h = hashlib.md5(f"{date_val.isoformat()}_{raw_merchant}_{amount}".encode()).hexdigest()
        
        t = Transaction(
            id=h,
            merchant_name=merchant_name,
            amount=amount,
            date=date_val,
            category=None,
            subcategory=None,
            upi=upi_data
        )
        transactions.append(t)
        
    return transactions

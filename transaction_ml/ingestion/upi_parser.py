import re
from typing import Optional
from transaction_ml.models.core import UPIData

def parse_upi_string(text: str) -> Optional[UPIData]:
    """
    Parses standard Indian UPI transaction string:
    UPI/<TYPE>/<TXN_ID>/<MERCHANT_OR_PERSON>/<BANK>/<HANDLE_OR_GATEWAY>/<NOTE>
    """
    if not text:
        return None
        
    pattern = r"UPI\/(DR|CR)\/([a-zA-Z0-9]+)\/([^\/]+)\/([^\/]+)\/([^\/]+)(?:\/(.*))?"
    match = re.search(pattern, text, re.IGNORECASE)
    
    if not match:
        return None
        
    type_code = match.group(1).upper()
    txn_id = match.group(2)
    merchant = match.group(3)
    bank = match.group(4)
    gateway = match.group(5)
    note = match.group(6) or ""
    
    return UPIData(
        type=type_code,
        txn_id=txn_id,
        merchant_or_person=merchant,
        bank=bank,
        handle_or_gateway=gateway,
        note=note
    )

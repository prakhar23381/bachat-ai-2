"""
Parser for IDFC FIRST Bank transaction SMS.

Known formats (Nov 2026):
  Debit : "Your A/c XX1234 debited by Rs. 196.00 on 06/11/26; JOHN DOE credited.
           RRN 123456789012. Available balance Rs. 747.90. Team IDFC FIRST Bank"
  Credit: "Your A/C XXXXX001234 is credited with INR 100.00 on 06/11/26 22:42.
           Your new balance is INR 847.90. Team IDFC FIRST Bank"

Notes:
  - Debit SMS carry no time and no UPI ID, only the payee's registered name
    (often a person, even for shops). Time comes from when the SMS arrived.
  - Credit SMS carry a time but no sender.
  - Every SMS carries the resulting balance, so a missed SMS shows up as a
    balance mismatch (see `balance_gap`).
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_AMOUNT = r"([\d,]+(?:\.\d+)?)"

DEBIT_RE = re.compile(
    r"A/c\s+(?P<account>\w+)\s+debited\s+by\s+Rs\.?\s*" + _AMOUNT.replace("(", "(?P<amount>", 1)
    + r"\s+on\s+(?P<date>\d{2}/\d{2}/\d{2});\s*(?P<payee>.+?)\s+credited\.\s*"
    r"RRN\s+(?P<rrn>\d+)\.\s*Available\s+balance\s+Rs\.?\s*" + _AMOUNT.replace("(", "(?P<balance>-?", 1),
    re.IGNORECASE,
)
CREDIT_RE = re.compile(
    r"A/C\s+(?P<account>\w+)\s+is\s+credited\s+with\s+INR\s*" + _AMOUNT.replace("(", "(?P<amount>", 1)
    + r"\s+on\s+(?P<date>\d{2}/\d{2}/\d{2})(?:\s+(?P<time>\d{1,2}:\d{2}))?\.\s*"
    r"Your\s+new\s+balance\s+is\s+INR\s*" + _AMOUNT.replace("(", "(?P<balance>-?", 1),
    re.IGNORECASE,
)


@dataclass
class BankSMS:
    id: str                    # stable: "rrn:<RRN>" for debits, hash for credits
    direction: str             # "debit" | "credit"
    amount: float              # signed: negative = money out
    timestamp: datetime
    account_last4: str
    balance: float             # balance after this transaction
    payee: Optional[str]       # debits only
    rrn: Optional[str]         # debits only


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _when(date: str, time: Optional[str], received_at: Optional[datetime]) -> datetime:
    day = datetime.strptime(date, "%d/%m/%y")
    if time:
        h, m = map(int, time.split(":"))
        return day.replace(hour=h, minute=m)
    # No time in the SMS: use arrival time if it is the same day (normally within seconds).
    if received_at and received_at.date() == day.date():
        return received_at.replace(second=0, microsecond=0, tzinfo=None)
    return day


def parse_sms(text: str, received_at: Optional[datetime] = None) -> Optional[BankSMS]:
    """Returns None for anything that isn't a recognised transaction SMS (OTPs, offers...)."""
    text = " ".join(text.split())  # SMS line breaks / double spaces

    m = DEBIT_RE.search(text)
    if m:
        return BankSMS(
            id=f"rrn:{m['rrn']}",
            direction="debit",
            amount=-_num(m["amount"]),
            timestamp=_when(m["date"], None, received_at),
            account_last4=m["account"][-4:],
            balance=_num(m["balance"]),
            payee=m["payee"].strip(),
            rrn=m["rrn"],
        )

    m = CREDIT_RE.search(text)
    if m:
        ts = _when(m["date"], m["time"], received_at)
        amount, balance = _num(m["amount"]), _num(m["balance"])
        key = f"{m['account'][-4:]}|{ts.isoformat()}|{amount}|{balance}"
        return BankSMS(
            id="cr:" + hashlib.sha256(key.encode()).hexdigest()[:16],
            direction="credit",
            amount=amount,
            timestamp=ts,
            account_last4=m["account"][-4:],
            balance=balance,
            payee=None,
            rrn=None,
        )
    return None


def balance_gap(previous_balance: float, sms: BankSMS) -> float:
    """
    Signed total of transactions missing between two consecutive SMS:
    0 = nothing missed, negative = unrecorded debit(s), positive = unrecorded credit(s).
    """
    return round(sms.balance - (previous_balance + sms.amount), 2)

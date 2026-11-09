"""
Terminal alternative to the web UI: import a statement, then label the most
frequent uncategorized merchants interactively.

    BACHAT_KEY=... python train_from_excel.py --file statement.xlsx
"""
import argparse
import os
from collections import defaultdict

from transaction_ml.ingestion.excel_parser import parse_bank_statement
from transaction_ml.models.core import CANONICAL_CATEGORIES, UNCATEGORIZED
from transaction_ml.system import TransactionEngine, load_secret

MAX_QUESTIONS = 10


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="Path to the .xlsx/.csv bank statement")
    parser.add_argument("--db", default=os.environ.get("BACHAT_DB", "transactions.db"), help="SQLite DB path")
    args = parser.parse_args()

    transactions = parse_bank_statement(args.file)
    engine = TransactionEngine(db_path=args.db, secret=load_secret())
    added = engine.ingest(transactions)
    print(f"[*] Parsed {len(transactions)} transactions, {added} new.")

    pending = defaultdict(list)
    for t in engine.storage.get_all_transactions():
        if t.category == UNCATEGORIZED:
            pending[t.merchant_name].append(t)
    if not pending:
        print("[*] Nothing uncategorized.")
        return

    print(f"[*] {sum(map(len, pending.values()))} uncategorized rows across {len(pending)} merchants.")
    for i, cat in enumerate(CANONICAL_CATEGORIES):
        print(f"  {i:>2}  {cat}")

    by_frequency = sorted(pending.items(), key=lambda kv: len(kv[1]), reverse=True)
    for merchant, rows in by_frequency[:MAX_QUESTIONS]:
        current = engine.storage.get_transaction(rows[0].id)
        if current.category != UNCATEGORIZED:
            continue  # filled in by an earlier answer
        ans = input(f"\n{merchant!r} ({len(rows)} txns, e.g. {rows[0].amount:.2f}) -> category # [s=skip, q=quit]: ").strip().lower()
        if ans == "q":
            break
        if not ans.isdigit() or int(ans) >= len(CANONICAL_CATEGORIES):
            continue
        changed = engine.correct(current, CANONICAL_CATEGORIES[int(ans)])
        print(f"   learned; {changed} other rows updated")

    left = sum(t.category == UNCATEGORIZED for t in engine.storage.get_all_transactions())
    print(f"\n[*] Done. {left} rows still uncategorized.")


if __name__ == "__main__":
    main()

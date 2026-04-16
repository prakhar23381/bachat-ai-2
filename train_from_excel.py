import argparse
import os
from collections import defaultdict
from transaction_ml.ingestion.excel_parser import parse_bank_statement
from transaction_ml.system import TransactionEngine
from transaction_ml.models.core import CANONICAL_CATEGORIES

def main():
    parser = argparse.ArgumentParser(description="Ingest and train the ML engine from a bank statement.")
    parser.add_argument("--file", type=str, required=True, help="Path to the Excel/CSV bank statement")
    parser.add_argument("--db", type=str, default="transactions.db", help="Path to local encrypted SQLite DB")
    parser.add_argument("--key", type=str, default="01234567890123456789012345678901", help="32-byte Encryption Key")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.file):
        print(f"Error: File not found at {args.file}")
        return
        
    print(f"[*] Parsing statement: {args.file}")
    try:
        transactions = parse_bank_statement(args.file)
    except Exception as e:
        print(f"Error parsing file: {e}")
        return
        
    print(f"[*] Found {len(transactions)} valid transactions.")
    
    engine = TransactionEngine(db_path=args.db, encryption_key_base64=args.key)
    
    # Check if this is the first time running (bootstrap needed?)
    # We can bootstrap with dummy data if weights are untouched, or just rely on partial_fit
    # For now, let's bootstrap with the first few transactions just to initialize dimensions
    if not engine.user_clf._is_initialized:
        print("[*] Blank engine detected. Bootstrapping Global Model layout...")
        # Feed all transactions but label them 'Uncategorized' to init dimensions without bias
        engine.bootstrap(transactions, ["Uncategorized"] * len(transactions))
    
    # Active Labeling Flow (Cold Start)
    print("\n" + "="*50)
    print("      COLD START ACTIVE LEARNING FLOW      ")
    print("="*50)
    print("Let's categorize your top recurring uncategorized merchants to fast-track the engine.")
    
    # Group by merchant name to find highest yield
    merchant_counts = defaultdict(list)
    for t in transactions:
        merchant_counts[t.merchant_name].append(t)
        
    # Sort by frequency descending
    sorted_merchants = sorted(merchant_counts.items(), key=lambda x: len(x[1]), reverse=True)
    
    labeled_count = 0
    categories_str = ", ".join([f"[{i}] {c}" for i, c in enumerate(CANONICAL_CATEGORIES)])
    
    for merchant_name, txn_list in sorted_merchants:
        if labeled_count >= 10:  # Ask for top 10 merchants only
            break
            
        print(f"\n? Found {len(txn_list)} transactions for: '{merchant_name}'")
        print(f"  Example amt: {txn_list[0].amount}")
        
        # Check if the engine already knows it
        pred = engine.predict(txn_list[0], store=False)
        if pred.category != 'Uncategorized' and (pred.confidence > 0.95 or pred.source == 'merchant_memory'):
            print(f"  -> Engine already maps this to '{pred.category}' with high confidence. Skipping.")
            continue
            
        print("Available Categories:")
        for i, cat in enumerate(CANONICAL_CATEGORIES):
            print(f"  {i:>2} - {cat}")
            
        ans = input(f"Enter Category ID (or 's' to skip): ").strip().lower()
        if ans == 's' or not ans.isdigit():
            continue
            
        try:
            cat_idx = int(ans)
            selected_cat = CANONICAL_CATEGORIES[cat_idx]
        except (ValueError, IndexError):
            print("Invalid selection. Skipping.")
            continue
            
        print(f"  -> Mapping '{merchant_name}' to {selected_cat}...")
        
        # We only need to formally correct one or two to trigger memory and model heavily
        # But we'll correct the first one to trigger the heavy update
        engine.correct(txn_list[0], selected_cat)
        labeled_count += 1
        
    print("\n[*] Training Complete. The system will now auto-predict the remaining items intelligently.")
    
    # Auto predict the rest to store them
    print("[*] Running full batch prediction and storing securely...")
    for t in transactions:
        engine.predict(t, store=True)
        
    print("[*] Done! Data is securely encrypted in your local SQLite Database.")

if __name__ == "__main__":
    main()

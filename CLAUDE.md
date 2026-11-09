# Bachat AI: project context

Personal finance tracker. Takes a monthly bank statement (.xlsx/.csv, Indian banks),
stores transactions locally in an encrypted SQLite DB, and auto-categorises them
from the user's own correction history (merchant, UPI metadata, amount, date).
Single user, runs only on localhost. No data leaves the machine.

Repo: https://github.com/prakhar23381/bachat-ai-2 (public, so **never commit statements or `*.db`**).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
export BACHAT_KEY='<long random secret>'   # see "Encryption" below
./start.sh                                  # API :8000 + Vite UI :5173
.venv/bin/python -m pytest                  # 34 tests, ~2s (pytest.ini sets pythonpath)
cd frontend && npm run lint && npm run build
PYTHONPATH=. .venv/bin/python train_from_excel.py --file statement.xlsx   # terminal labelling flow
```

Env vars: `BACHAT_KEY` (encryption secret), `BACHAT_DB` (DB path, default `transactions.db` in CWD).

## How categorisation works (`transaction_ml/system.py`)

Every prediction goes through, in order:

1. **Merchant memory** (`engine/rules.py`): exact match on the normalised merchant name
   against user corrections. One correction is enough (`memory_activation_threshold=1`);
   a conflicting correction resets it. Confidence 1.0.
2. **Model** (`models/classifiers.py`): multinomial `LogisticRegression(C=100)` **retrained
   from scratch** on all `verified` rows at startup and after every correction (~100 ms for
   ~400 rows). No weights are persisted; the model is derived from the DB.
   A prediction is only accepted if **both** hold:
   - top probability >= `min_confidence` (0.3)
   - char n-gram cosine similarity between the merchant name and the nearest verified
     merchant >= `min_similarity` (0.3). Without this gate, unfamiliar merchants
     get the most frequent class.
3. Otherwise **Uncategorized**, so the row shows up in the UI's Active Learning queue.

`verified` = the user set this row's category. Only verified rows are training data.
After every correction, **all unverified rows are re-predicted** (`reclassify_unverified`).
Re-uploading a statement skips existing ids (`ingest`), so corrections are never overwritten.

Thresholds were tuned on synthetic data (see `tests/test_engine.py`) and should be revisited
with real statements.

## Files and folders

```
api/                    FastAPI HTTP layer
  server.py             All endpoints. Builds one global TransactionEngine at import time.
transaction_ml/         Core library (no web code)
  system.py             TransactionEngine (predict / ingest / correct / retrain / reclassify),
                        key loading (BACHAT_KEY) and derivation
  models/core.py        Dataclasses: Transaction, UPIData, Prediction, ModelConfig; CANONICAL_CATEGORIES
  models/classifiers.py CategoryClassifier: LogisticRegression plus nearest-merchant similarity
  features/extractor.py FeatureExtractor: hashed word 1-2grams + char 3-5grams (10k cols)
                        + 6 numeric (scaled log amount, weekend, hour sin/cos, day sin/cos).
                        Also normalize_merchant_name(), the merchant-memory key.
  engine/rules.py       MerchantMemory: merchant -> category rules with a streak counter
  ingestion/excel_parser.py  Bank-statement parser: finds the header row and detects date /
                        narration / debit / credit / amount / Dr-Cr columns by whole-word
                        header matching; day-first dates; deterministic md5 ids
  ingestion/upi_parser.py    Regex for "UPI/DR|CR/<txn>/<name>/<bank>/<vpa>/<note>" narrations
  storage/db.py         EncryptedStorage: SQLite, Fernet-encrypted merchant/amount/notes/UPI,
                        merchant_memory table, schema migrations
frontend/               React 19 + Vite 8 + TypeScript single-page UI
  src/App.tsx           The whole UI in one component: upload, pie chart by category,
                        month/year filter, transaction list with inline re-categorise,
                        bill split modal, manual-entry modal, "vault" toggle for Private,
                        Active Learning panel (uncategorized merchants, most frequent first)
  src/index.css         Dark glassmorphism theme (all styles live here)
  src/main.tsx          React entry
  API base URL hardcoded in App.tsx: http://localhost:8000/api
tests/                  pytest
  conftest.py           Fixtures: temp-DB engine, CSV writer, txn() helper
  test_ingestion.py     Parser regressions (SBI/HDFC/ICICI header shapes, dates, ids, Dr/Cr)
  test_engine.py        Memory, model generalisation, unknown-merchant gating, restart
                        persistence, re-ingest safety, encryption at rest, legacy key and migration
  test_api.py           Endpoint behaviour via TestClient (reloads api.server per test)
train_from_excel.py     CLI: import a statement, then label top uncategorized merchants in the terminal
start.sh                Runs uvicorn (background) + `npm run dev`
pytest.ini              pythonpath=. so tests import transaction_ml without PYTHONPATH
transactions.db         (gitignored) the user's real data. Do not delete or commit it.
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/categories` | `CANONICAL_CATEGORIES` |
| GET | `/api/transactions` | All rows, newest first; includes `upi`, `splits`, `verified` |
| POST | `/api/upload` | multipart `file` (.xlsx/.csv) → `{parsed, added}` |
| POST | `/api/transactions/{id}/correct` | `{category, subcategory?}` → `{transaction, reclassified}` |
| POST | `/api/transactions/{id}/split` | `{splits:[{amount, category}]}`; amounts must sum to the txn amount |
| POST | `/api/transactions/manual` | `{merchant_name, amount, date (ISO, local), notes?}` |

## Data and encryption

- Amount sign convention: negative = debit/expense, positive = credit/income.
- All dates are stored **naive local time**. Mixing tz-aware and naive datetimes breaks sorting.
- Encrypted columns: merchant, amount, notes, `upi_data` (encrypted since the cleanup; legacy rows
  hold plaintext JSON, which is still readable and gets encrypted when the row is rewritten).
  **Plaintext**: date, category, subcategory, splits. Merchant-memory keys (normalised merchant
  names) are also plaintext.
- Key: `derive_fernet_key(BACHAT_KEY)`. If unset, falls back to `LEGACY_DEFAULT_SECRET`, the
  key hardcoded in the original public code (so effectively no protection). The derivation
  **must stay byte-compatible** or existing DBs become unreadable (covered by a test).
  There is no re-key tool yet.
- Transaction id = md5(`{date.isoformat()}_{raw narration}_{amount}`), with a `_n` suffix for
  identical repeats within one file. Changing the parser's date/amount output changes ids,
  so a re-upload would create duplicates.
- Schema migrations live in `EncryptedStorage._migrate`. Adding `verified` copies the DB to
  `<db>.bak-pre-verified` first. Old DBs may still contain unused `subcategories` /
  `model_weights` tables from the original design; they're harmless.

## Known limitations / next steps (priority order)

1. **Real data is long-tail**: the owner's DB had 383 rows across 279 distinct merchants, mostly
   UPI person-to-person payments. Memory and the model only help with repeats, so expect lots
   of manual labelling. Ideas: rules on UPI VPA handles (e.g. `@ybl` personal vs merchant
   VPAs), recipient-name rules, amount-pattern rules for rent/SIPs.
2. Only one UPI narration format is parsed (`UPI/DR/...`). HDFC uses `UPI-NAME-VPA-...`.
3. `.xls` is unsupported (would need `xlrd`); only `.xlsx`/`.csv`.
4. No duplicate detection across banks/accounts (`account_id` exists but is never set).
5. Subcategories are stored but there's no UI or API to set them (the frontend always sends `null`).
6. Merchant-memory keys are plaintext merchant names; no key rotation tool.
7. Frontend is one 500-line component with heavy inline styles; split it when adding features.
8. Every request decrypts the whole DB (fine at personal scale, O(n) per correction).

## History

Originally generated by an older Gemini model (Mar–Apr 2026). The Nov 2026 cleanup (branch
`cleanup`) fixed: credit column misdetected when headers contain "Description"; month/day
swapped dates; category labels shuffled after every server restart (sklearn sorts
`classes_`, the code assumed canonical order), which corrupted most stored categories in the
original DB; a "global model" fit on one row labelled Uncategorized that dominated every
prediction; incremental SGD that learned nothing from corrections; merchant memory not
persisted; re-upload wiping corrections; duplicate same-day rows merged; API omitting
`upi`/`splits`; tz-aware manual dates crashing the list endpoint; upload only possible on an
empty DB.

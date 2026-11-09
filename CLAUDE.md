# Bachat AI: project context

Real-time personal finance tracker for one user (the owner). Every IDFC FIRST Bank SMS
is forwarded from the owner's iPhone (iOS Shortcuts automation) to a small always-on
server, which categorises the transaction immediately. It asks the owner on Telegram
only when it isn't sure, and learns from each answer. A web dashboard shows history and
charts. Statement upload (.xlsx/.csv) is kept for backfilling past months.

Repo: https://github.com/prakhar23381/bachat-ai-2 (public, so **never commit statements,
`*.db`, real SMS text, or real payee names**; test fixtures use made-up names and RRNs).

## Status / roadmap

| Phase | Scope | State |
|---|---|---|
| 1 | User's categories (editable), SMS parser and `/api/sms`, token auth, dashboard serving, migration of old data | **done** (branch `realtime`) |
| 2 | Telegram bot: ask only when unsure (inline category buttons); daily summary; missed-SMS (balance gap) alerts | todo |
| 3 | Deploy on a free cloud VM (Oracle Always Free / GCP e2-micro) with HTTPS; iOS Shortcut setup guide | todo |
| 4 | Splitwise: "Split" button creates an expense (only your share counts as spending); poll `get_expenses` for bills others paid; match settle-ups so nothing is double counted | todo. Personal API key comes from https://secure.splitwise.com/apps (laptop browser); may need Splitwise Pro (unverified) |
| 5 | Dashboard rework: drop the upload-first flow; Splitwise balances view; split up the 500-line App.tsx | todo |

The owner's decisions behind this design are recorded in Claude's project memory
(`bachat-redesign-decisions`).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
export BACHAT_KEY='<long random secret>'          # DB encryption secret
export BACHAT_API_TOKEN='<random, 16+ chars>'     # bearer token for every /api call
./start.sh                                        # API :8000 + Vite UI :5173 (proxies /api)
.venv/bin/python -m pytest                        # 65 tests, ~3s
cd frontend && npm run lint && npm run build      # build -> frontend/dist, served by FastAPI at /
```

Env vars: `BACHAT_KEY`, `BACHAT_API_TOKEN` (the server refuses to start without it),
`BACHAT_DB` (default `transactions.db` in CWD).

## How a transaction is categorised (`transaction_ml/system.py`)

1. **Credit** (amount > 0): always "Money received" (`source="rule"`); never asks. IDFC credit SMS don't say who sent the money.
2. **Merchant memory** (`engine/rules.py`): exact match on the normalised payee name
   (lowercased, punctuation and honorifics like "Miss" stripped) from the user's corrections.
   One correction is enough.
3. **Model** (`models/classifiers.py`): `LogisticRegression(C=100)` retrained from scratch on
   all **verified debits**, at startup and after every correction (~100 ms for ~400 rows).
   No weights are persisted. A prediction is accepted only if the probability is
   >= `min_confidence` (0.3) **and** the char n-gram similarity of the payee name to the nearest
   verified payee is >= `min_similarity` (0.3); otherwise the result is Uncategorized.
4. **Uncategorized** = `needs_review`: the Telegram bot (phase 2) and the dashboard's Active
   Learning panel ask the user, most frequent payee first.

`verified` = the user set this row's category. After every correction or category change,
**all unverified rows are re-predicted**. Re-imports skip existing ids, so corrections survive.

IDFC debit SMS contain only the payee's bank-registered name (often a person, even for shops),
with no UPI ID and no time. So the arrival time sent by the Shortcut is the timestamp, and
time of day and amount are real signals (`features/extractor.py`).

## Categories

Stored in the `categories` table, seeded from `DEFAULT_CATEGORIES` (models/core.py):
travel, outside food, mess food, personal items, health food, phone, entertainment
(kind `expense`), Friends / splits (`split`), Money received (`income`, protected).
"Uncategorized" is a system state, not a stored category. Rename onto an existing name = merge.
Delete = its rows become Uncategorized and unverified, and get re-predicted. Only `expense`
categories count in the spending chart.

## Files and folders

```
api/server.py           FastAPI. /api router requires `Authorization: Bearer $BACHAT_API_TOKEN`.
                        Serves frontend/dist at / when built (same origin, no CORS).
transaction_ml/
  system.py             TransactionEngine: predict / ingest / ingest_sms / correct /
                        categories / retrain / reclassify; SmsResult; key loading/derivation
  models/core.py        Transaction, UPIData, Prediction, ModelConfig, category constants,
                        rrn_id_for()
  models/classifiers.py CategoryClassifier (LogisticRegression + nearest-payee similarity)
  features/extractor.py Hashed word/char n-grams + scaled amount, weekend, hour, day features;
                        normalize_merchant_name() is the merchant-memory key
  engine/rules.py       MerchantMemory
  ingestion/sms_parser.py   IDFC FIRST SMS -> BankSMS; balance_gap() for missed-SMS detection
  ingestion/excel_parser.py Statement parser (header-word detection, day-first dates)
  ingestion/upi_parser.py   "UPI/DR|CR/<ref>/<name>/<bank>/<vpa>/<note>" statement narrations
  storage/db.py         EncryptedStorage: SQLite + Fernet; versioned migrations
                        (PRAGMA user_version, currently 2; backup <db>.bak-v<N> before migrating)
frontend/src/App.tsx    Whole dashboard: token gate, chart, filters, transaction list with
                        inline re-categorise, split/manual modals, Active Learning, category editor
tests/                  test_sms_parser, test_realtime (SMS/categories/migration/dedupe),
                        test_engine, test_ingestion, test_api; conftest has the fixtures
start.sh                Local dev: uvicorn + vite
```

## API (all under /api, all need the bearer token)

| Method | Path | Notes |
|---|---|---|
| POST | `/sms` | `{text, received_at?}` (ISO). Returns `{status: added/duplicate/ignored, transaction, needs_review, balance_gap}`. Unrecognised SMS are stored encrypted in `unparsed_sms` |
| POST | `/upload` | statement backfill → `{parsed, added}` |
| POST | `/transactions/manual` | `{merchant_name, amount, date, notes?}` |
| GET | `/transactions` | newest first; includes `verified`, `source`, `upi`, `splits` |
| POST | `/transactions/{id}/correct` | `{category}` → `{transaction, reclassified}` |
| POST | `/transactions/{id}/split` | local split record (to be replaced by Splitwise in phase 4) |
| GET/POST | `/categories` | list / add `{name, kind}` |
| PATCH/DELETE | `/categories/{name}` | rename-or-merge `{new_name}` / delete |

## Data and encryption

- Amounts: negative = money out. Dates: naive local time everywhere.
- Ids: `rrn:<12-digit RRN>` for UPI payments (from SMS, or the UPI ref in a statement
  narration), so the SMS and statement copies of one payment dedupe. That the SMS RRN equals
  the statement UPI ref is **assumed, not yet verified** against a real IDFC statement.
  Credit SMS have no RRN: id = hash(account, time, amount, balance).
- Encrypted: merchant/payee, amount, notes, balance, UPI metadata, unparsed SMS. Plaintext:
  dates, categories, splits, merchant-memory keys (normalised payee names).
- `derive_fernet_key` must stay byte-compatible (tested); without `BACHAT_KEY` the legacy key
  from the original public code is used, which is effectively no encryption.
- The `meta` table holds `last_balance` (for gap detection).
- Migration to v2 (from the statement-era DB): credits → Money received; debits with old
  categories → Uncategorized/unverified; UPI rows re-keyed to `rrn:`; stale merchant memory
  dropped. On the owner's real DB this left 336 debits (246 payees) to label.

## Known limitations

- Only IDFC FIRST SMS formats seen so far (UPI debit, credit). Card spends, ATM, NEFT/IMPS and
  autopay SMS are unknown; they land in `unparsed_sms`, so check there and extend `sms_parser.py`.
- Balance-gap detection assumes SMS arrive in order and that only one account is tracked.
- Every request decrypts the whole DB (fine at personal scale).
- Frontend: one large component, inline styles; not yet visually reviewed after phase 1.

## History

- Mar–Apr 2026: original statement-upload app generated by an older Gemini model.
- Nov 2026 `cleanup` (PR #1): fixed 7 confirmed bugs (label shuffle after restart, credit
  column misdetection, month/day swap, untrainable model, etc.), replaced SGD with retrained LR.
- Nov 2026 `realtime`: redesign to SMS → server → Telegram, the user's own categories, auth.

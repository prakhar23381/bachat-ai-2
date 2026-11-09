# Bachat AI

A local, private personal-finance tracker. Upload your monthly bank statement (.xlsx or .csv),
and it categorises every transaction based on how you've categorised similar ones before.
Everything runs on your machine; amounts and merchant names are encrypted in a local SQLite file.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Pick a long random secret and keep it safe. Losing it means losing the data.
export BACHAT_KEY='change-me-to-something-long-and-random'

./start.sh        # API on :8000, UI on http://localhost:5173
```

## Use it

1. Upload a statement. Rows it can't place confidently land in **Active Learning**, sorted by how
   often each merchant appears.
2. Label them. Each label is applied right away to that merchant's other rows, and the model
   retrains so similar merchant names get categorised too.
3. Click any category badge to fix it. ✓ marks rows you've confirmed; anything else may be
   re-predicted as the model learns.
4. Upload next month's statement the same way (header button). Already-imported rows are skipped.

Supported statement layouts: separate Debit/Credit (or Withdrawal/Deposit) columns, or one
Amount column with an optional Dr/Cr column. Dates are read day-first (DD/MM/YYYY).

## Develop

```bash
.venv/bin/python -m pytest
cd frontend && npm run lint && npm run build
```

See [CLAUDE.md](CLAUDE.md) for architecture, a file-by-file guide, and known limitations.

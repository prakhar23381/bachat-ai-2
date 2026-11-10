# Bachat AI

A private, real-time spending tracker. Every bank SMS is forwarded from the phone to your own
server, which categorises the transaction immediately into your own categories. It asks
you only when it doesn't recognise the payee, and learns from every answer.

> **Going live:** follow [docs/SETUP.md](docs/SETUP.md) (Telegram bot, free VM, iPhone Shortcut).
> Done: SMS ingestion (IDFC FIRST Bank), learning categoriser, editable categories, Telegram bot
> (your reply = the UPI note), dashboard, deploy scripts. Next: Splitwise sync.
> Architecture and roadmap: [CLAUDE.md](CLAUDE.md).

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export BACHAT_KEY='long-random-secret'        # encrypts the DB; losing it loses the data
export BACHAT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

./start.sh        # dashboard on http://localhost:5173, sign in with BACHAT_API_TOKEN
```

Send an SMS the way the iPhone Shortcut will:

```bash
curl -H "Authorization: Bearer $BACHAT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text": "Your A/c XX1234 debited by Rs. 50.00 on 07/11/26; DMRC credited. RRN 100000000003. Available balance Rs. 950.00. Team IDFC FIRST Bank"}' \
  http://localhost:8000/api/sms
```

## Develop

```bash
.venv/bin/python -m pytest
cd frontend && npm run lint && npm run build
```

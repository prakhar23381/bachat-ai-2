# Going live: phone → server → Telegram

About an hour, once. At the end, every IDFC FIRST payment you make shows up within seconds.
Known payees are filed silently, and you get a Telegram question for anything new.

## 1. Create your Telegram bot (5 min)

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and username.
2. BotFather replies with a **bot token** (`123456:ABC...`). Keep it private; it goes into the
   server config in step 4, not into any chat or git.
3. Open your new bot and send it any message (e.g. `hi`), so it can find your chat id later.

## 2. Create a free VM (20 min)

**Oracle Cloud Always Free** (recommended: free indefinitely, Mumbai/Hyderabad regions):

1. Sign up at cloud.oracle.com. A card is needed for verification; Always Free resources are not charged.
2. *Compute → Instances → Create instance*:
   - Image: **Canonical Ubuntu 24.04** (needs Python 3.12; 22.04 will not work)
   - Shape: `VM.Standard.E2.1.Micro` (Always Free) or an Ampere A1 with 1 OCPU / 6 GB
   - Add your SSH public key (`cat ~/.ssh/id_ed25519.pub` on your Mac)
3. Open ports 80 and 443: *Instance → Subnet → Default security list → Add ingress rules*,
   source `0.0.0.0/0`, TCP, destination ports `80` and `443`.
   (The setup script opens them in the VM's own firewall too.)
4. Note the instance's **public IP**.

*Alternative:* Google Cloud `e2-micro` in us-west1/us-central1/us-east1 is also free;
the steps are the same (open 80/443 in the VPC firewall).

## 3. Install (10 min)

```bash
ssh ubuntu@<public-ip>
git clone -b realtime https://github.com/prakhar23381/bachat-ai-2.git
cd bachat-ai-2 && ./deploy/setup.sh
```

It prints your URL, e.g. `https://152-67-1-2.sslip.io`. sslip.io maps the IP to a hostname
so HTTPS works without buying a domain.

**Back up `BACHAT_KEY`** from `/etc/bachat.env` (password manager). Without it the database
cannot be decrypted.

## 4. Connect the bot

```bash
sudo nano /etc/bachat.env      # set BACHAT_TELEGRAM_TOKEN=<token from BotFather>, leave CHAT_ID empty
cd /opt/bachat && sudo env $(sudo grep ^BACHAT_TELEGRAM_TOKEN= /etc/bachat.env) PYTHONPATH=. \
  .venv/bin/python -m api.telegram_bot whoami
#   -> prints your chat id, e.g. 512345678
sudo nano /etc/bachat.env      # set BACHAT_TELEGRAM_CHAT_ID=512345678
sudo systemctl restart bachat
```

Send `/help` to your bot; it should answer. The bot ignores everyone except that chat id.

## 5. Bring your old transactions (optional)

Your current local DB uses the old built-in key. Re-encrypt it with the server's key on your
**Mac**, then copy it up:

```bash
cd ~/"Documents/Code/Bachat AI - 17 March 2026"
NEW_KEY='<BACHAT_KEY from the server>' .venv/bin/python scripts/rekey.py transactions.db server.db
scp server.db ubuntu@<public-ip>:/tmp/
ssh ubuntu@<public-ip> 'sudo systemctl stop bachat && sudo mv /tmp/server.db /opt/bachat/data/transactions.db \
  && sudo chown bachat:bachat /opt/bachat/data/transactions.db && sudo systemctl start bachat'
rm server.db
```

Then send `/label` to the bot to start sorting the old payees, most frequent first.

## 6. The iPhone Shortcut (10 min)

Shortcuts app → **Automation** → **+** → **Message**:

1. **Message Contains:** `IDFC FIRST Bank`. Every transaction SMS ends with "Team IDFC FIRST Bank".
   Leave Sender empty.
2. Choose **Run Immediately** and turn **Notify When Run** off. Tap **Next** → **New Blank Automation**.
3. Add the action **Get Contents of URL**:
   - URL: `https://<your-host>/api/sms`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <BACHAT_API_TOKEN>`
   - Request Body: **JSON**, with two Text fields:
     - `text` → the **Shortcut Input** variable (tap it and choose **Content**, the SMS body)
     - `received_at` → the **Current Date** variable, tap it → Date Format **ISO 8601**, include time
4. Save.

Test without spending: the dashboard at `https://<your-host>` (sign in with the API token)
should load. Then make a small UPI payment; within a few seconds the bot should ask about it.

If the phone is offline when an SMS arrives, the Shortcut can't deliver it. The next SMS
reveals the missing amount through the balance, and the bot warns you.

## Day to day

- **Bot asks** → reply with what it was for (`chai`, `mess dinner`, `auto to metro`). If the
  note names a category, it's filed; otherwise tap the suggested category or pick one.
- **Known payees** → nothing to do; the 9 pm summary lists the day's spending.
- **Monthly statement** → download it as **Excel** from IDFC net banking or the app and upload
  it on the dashboard. That fills in your UPI notes and payees' UPI IDs for anything you didn't
  annotate. (The statement IDFC emails is a password-protected PDF, which isn't supported yet.)
- **Update the server** after new code: `cd bachat-ai-2 && ./deploy/update.sh`.
- **Logs:** `journalctl -u bachat -f`.

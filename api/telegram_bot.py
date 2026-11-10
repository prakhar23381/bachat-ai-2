"""
Telegram bot: the day-to-day interface.

- New debit from an unknown payee -> asks "what for?". Reply with your note (it is
  stored as the note and used to categorise), or tap a category.
- Known payees are categorised silently and show up in the daily summary.
- Balance doesn't add up -> warns that an SMS was probably missed.
- /label works through old uncategorized payees, most frequent first.
- /summary shows today; /help lists commands.

Only the owner's chat (BACHAT_TELEGRAM_CHAT_ID) is ever answered.
To find your chat id: message the bot once, then run `python -m api.telegram_bot whoami`.
"""
import html
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from transaction_ml.models.core import MONEY_RECEIVED, UNCATEGORIZED, Prediction, Transaction
from transaction_ml.system import SmsResult, TransactionEngine

log = logging.getLogger("bachat.bot")

HELP = (
    "I ask about payments I can't place. Reply with what it was for (your note), "
    "or tap a category.\n\n"
    "/label: next uncategorized payee (old ones too)\n"
    "/summary: today so far\n"
    "/help: this message"
)


def rupees(x: float) -> str:
    return f"₹{abs(x):,.0f}" if abs(x) == int(abs(x)) else f"₹{abs(x):,.2f}"


class TelegramBot:
    def __init__(self, token: str, chat_id: int, engine: TransactionEngine,
                 summary_hour: int = 21, client: Optional[httpx.Client] = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = int(chat_id)
        self.engine = engine
        self.summary_hour = summary_hour
        self.http = client or httpx.Client(timeout=45)
        self._stop = threading.Event()

    # ---- Telegram API ------------------------------------------------------------

    def call(self, method: str, **params):
        res = self.http.post(self.base + method, json=params)
        body = res.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]

    def send(self, text: str, keyboard: Optional[List[List[Dict]]] = None) -> int:
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)["message_id"]

    def edit(self, message_id: int, text: str):
        try:
            self.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text, parse_mode="HTML")
        except RuntimeError as e:  # e.g. message too old / unchanged; not worth failing over
            log.warning("%s", e)

    # ---- outgoing -------------------------------------------------------------------

    def _describe(self, t: Transaction) -> str:
        when = t.date.strftime("%d %b, %H:%M") if (t.date.hour or t.date.minute) else t.date.strftime("%d %b")
        return f"<b>{rupees(t.amount)}</b> to <b>{html.escape(t.merchant_name)}</b> · {when}"

    def _keyboard(self, t: Transaction, suggestion: Optional[str] = None) -> List[List[Dict]]:
        names = [n for n, _ in self.engine.categories() if n != MONEY_RECEIVED]
        if suggestion in names:
            names.remove(suggestion)
            names.insert(0, suggestion)
        buttons = [{"text": ("✓ " if n == suggestion else "") + n, "callback_data": f"c|{n}|{t.id}"} for n in names]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{"text": "Skip", "callback_data": f"s||{t.id}"}])
        return rows

    def ask(self, t: Transaction, extra: str = "", suggestion: Optional[str] = None):
        text = f"{self._describe(t)}{extra}\nWhat was it for? Reply with a note, or pick:"
        message_id = self.send(text, self._keyboard(t, suggestion))
        self.engine.storage.add_question(message_id, t.id)

    def on_sms(self, result: SmsResult):
        """Called after every /api/sms request."""
        try:
            if result.status != "added":
                return
            if result.needs_review:
                self.ask(result.transaction)
            if result.balance_gap:
                direction = "left your account" if result.balance_gap < 0 else "came into your account"
                self.send(f"⚠️ {rupees(result.balance_gap)} {direction} without an SMS reaching me "
                          f"(noticed with the {rupees(result.transaction.amount)} payment to "
                          f"{html.escape(result.transaction.merchant_name)}). Check the IDFC app.")
        except Exception:
            log.exception("Telegram notification failed")

    def send_summary(self, day: Optional[datetime] = None, quiet_if_empty: bool = False):
        s = self.engine.day_summary(day or datetime.now())
        if s["count"] == 0:
            if not quiet_if_empty:
                self.send("Nothing recorded today.")
            return
        lines = [f"<b>Today: {rupees(s['spent_total'])} spent</b>"]
        lines += [f"• {html.escape(c)}: {rupees(v)}" for c, v in s["spent_by_category"].items()]
        if s["received_total"]:
            lines.append(f"Received: {rupees(s['received_total'])}")
        if s["pending"]:
            lines.append(f"\n{s['pending']} still need a category: /label")
        self.send("\n".join(lines))

    # ---- incoming ---------------------------------------------------------------------

    def handle_update(self, update: Dict):
        if "callback_query" in update:
            self._on_button(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _owner(self, chat: Dict) -> bool:
        return chat.get("id") == self.chat_id

    def _on_button(self, q: Dict):
        msg = q.get("message", {})
        if not self._owner(msg.get("chat", {})):
            return
        self.call("answerCallbackQuery", callback_query_id=q["id"])
        action, category, txn_id = q.get("data", "||").split("|", 2)
        t = self.engine.storage.get_transaction(txn_id)
        if not t:
            return self.edit(msg["message_id"], "That transaction no longer exists.")
        if action == "s":
            self.engine.storage.mark_answered(txn_id)
            return self.edit(msg["message_id"], f"{self._describe(t)}\nSkipped (still Uncategorized).")
        try:
            others = self.engine.correct(t, category)
        except ValueError as e:
            return self.send(html.escape(str(e)))
        self._close(t, category, others)

    def _close(self, t: Transaction, category: str, others: int):
        note = f" · “{html.escape(t.notes)}”" if t.notes else ""
        text = f"{self._describe(t)}{note}\n✓ <b>{html.escape(category)}</b>"
        if others:
            text += f" (+{others} similar updated)"
        for message_id in self.engine.storage.questions_for(t.id):
            self.edit(message_id, text)
        self.engine.storage.mark_answered(t.id)

    def _on_message(self, m: Dict):
        if not self._owner(m.get("chat", {})):
            return
        text = (m.get("text") or "").strip()
        if not text:
            return
        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            return self.send(HELP)
        if command == "/summary":
            return self.send_summary()
        if command == "/label":
            return self._label_next()
        self._on_note(m, text)

    def _label_next(self):
        pending = self.engine.pending_payees()
        if not pending:
            return self.send("Nothing left to label. 🎉")
        t, count = pending[0]
        extra = f"\n({count} payments to this payee · {len(pending)} payees left)" if count > 1 else f"\n({len(pending)} payees left)"
        self.ask(t, extra)

    def _on_note(self, m: Dict, note: str):
        reply_to = m.get("reply_to_message", {}).get("message_id")
        txn_id = self.engine.storage.question_txn(reply_to) if reply_to else None
        if txn_id is None:
            latest = self.engine.storage.latest_open_question()
            txn_id = latest[1] if latest else None
        t = self.engine.storage.get_transaction(txn_id) if txn_id else None
        if t is None:
            return self.send("No open question to attach that note to. /label shows the next one.")

        pred, decided = self.engine.apply_note(t, note)
        if decided:
            return self._close(t, pred.category, 0)
        suggestion = pred.category if pred.category != UNCATEGORIZED else None
        prompt = f"\nNote saved. Looks like <b>{html.escape(suggestion)}</b>?" if suggestion else "\nNote saved. Which category?"
        message_id = self.send(f"{self._describe(t)} · “{html.escape(t.notes)}”{prompt}", self._keyboard(t, suggestion))
        self.engine.storage.add_question(message_id, t.id)

    # ---- background loops -------------------------------------------------------------

    def poll_forever(self):
        offset = int(self.engine.storage.get_meta("tg_offset") or 0)
        while not self._stop.is_set():
            try:
                updates = self.call("getUpdates", offset=offset, timeout=30,
                                    allowed_updates=["message", "callback_query"])
                for u in updates:
                    offset = u["update_id"] + 1
                    self.engine.storage.set_meta("tg_offset", str(offset))
                    try:
                        self.handle_update(u)
                    except Exception:
                        log.exception("Failed to handle update %s", u.get("update_id"))
            except Exception:
                log.exception("Polling failed; retrying")
                self._stop.wait(5)

    def summary_forever(self):
        while not self._stop.wait(60):
            now = datetime.now()
            today = now.date().isoformat()
            if now.hour >= self.summary_hour and self.engine.storage.get_meta("last_summary") != today:
                try:
                    self.send_summary(now, quiet_if_empty=True)
                    self.engine.storage.set_meta("last_summary", today)
                except Exception:
                    log.exception("Daily summary failed")

    def start(self):
        for target in (self.poll_forever, self.summary_forever):
            threading.Thread(target=target, daemon=True, name=f"bot-{target.__name__}").start()

    def stop(self):
        self._stop.set()


def bot_from_env(engine: TransactionEngine) -> Optional[TelegramBot]:
    token = os.environ.get("BACHAT_TELEGRAM_TOKEN")
    chat_id = os.environ.get("BACHAT_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram bot disabled: set BACHAT_TELEGRAM_TOKEN and BACHAT_TELEGRAM_CHAT_ID")
        return None
    return TelegramBot(token, int(chat_id), engine, summary_hour=int(os.environ.get("BACHAT_SUMMARY_HOUR", 21)))


if __name__ == "__main__" and sys.argv[1:] == ["whoami"]:
    # Prints the chat id of whoever messaged the bot recently (run before the server starts polling).
    token = os.environ["BACHAT_TELEGRAM_TOKEN"]
    res = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in res.get("result", []) if "message" in u}
    print("\n".join(f"{cid}  {name}" for cid, name in chats.items()) or "No messages yet: send your bot any message first.")

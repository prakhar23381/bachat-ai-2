"""Bot behaviour against a fake Telegram API (httpx.MockTransport)."""
import json
from datetime import datetime

import httpx
import pytest

from api.telegram_bot import TelegramBot
from conftest import txn
from test_realtime import sms
from transaction_ml.models.core import UNCATEGORIZED, UPIData

OWNER = 4242
AT = datetime(2026, 11, 7, 13, 5)


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.next_id = [], [], 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({**body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "editMessageText":
            self.edits.append(body)
        return httpx.Response(200, json={"ok": True, "result": True})

    def buttons(self, i=-1):
        return [b["text"] for row in self.sent[i]["reply_markup"]["inline_keyboard"] for b in row]


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def bot(engine, tg):
    return TelegramBot("TOKEN", OWNER, engine, client=httpx.Client(transport=httpx.MockTransport(tg)))


def message(text, chat=OWNER, reply_to=None):
    m = {"message_id": 1, "chat": {"id": chat}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": m}


def button(data, message_id, chat=OWNER):
    return {"update_id": 2, "callback_query": {"id": "q", "data": data, "message": {"message_id": message_id, "chat": {"id": chat}}}}


def receive(engine, bot, payee="RAMESH TEA STALL", rrn="100000000001", amount=220, balance=780):
    result = engine.ingest_sms(sms(payee, rrn, amount, balance, day=7), AT)
    bot.on_sms(result)
    return result.transaction


def test_unknown_payee_triggers_a_question(engine, bot, tg):
    receive(engine, bot)
    assert len(tg.sent) == 1 and "RAMESH TEA STALL" in tg.sent[0]["text"] and "₹220" in tg.sent[0]["text"]
    assert "mess food" in tg.buttons() and "Money received" not in tg.buttons() and "Skip" in tg.buttons()


def test_known_payee_and_credits_are_silent(engine, bot, tg):
    t = receive(engine, bot)
    engine.correct(t, "outside food")
    receive(engine, bot, rrn="100000000002", balance=560)
    credit = engine.ingest_sms("Your A/C XXXXX001234 is credited with INR 100.00 on 07/11/26 22:42. "
                               "Your new balance is INR 660.00. Team IDFC FIRST Bank", AT)
    bot.on_sms(credit)
    assert len(tg.sent) == 1  # only the very first question


def test_button_answer_categorises_and_closes_question(engine, bot, tg):
    t = receive(engine, bot)
    bot.handle_update(button(f"c|outside food|{t.id}", tg.sent[0]["message_id"]))
    stored = engine.storage.get_transaction(t.id)
    assert (stored.category, stored.verified) == ("outside food", True)
    assert "✓ <b>outside food</b>" in tg.edits[-1]["text"]
    assert engine.storage.latest_open_question() is None


def test_note_naming_a_category_decides_it(engine, bot, tg):
    t = receive(engine, bot)
    bot.handle_update(message("mess dinner", reply_to=tg.sent[0]["message_id"]))
    stored = engine.storage.get_transaction(t.id)
    assert (stored.category, stored.notes, stored.verified) == ("mess food", "mess dinner", True)
    assert "“mess dinner”" in tg.edits[-1]["text"]


def test_free_text_note_is_saved_and_asks_to_confirm(engine, bot, tg):
    t = receive(engine, bot)
    bot.handle_update(message("chai and samosa"))  # not a reply: goes to the latest open question
    stored = engine.storage.get_transaction(t.id)
    assert (stored.notes, stored.category) == ("chai and samosa", UNCATEGORIZED)
    assert "Note saved" in tg.sent[-1]["text"]
    bot.handle_update(button(f"c|outside food|{t.id}", tg.sent[-1]["message_id"]))
    assert engine.storage.get_transaction(t.id).category == "outside food"


def test_notes_teach_the_model_for_new_payees(engine, bot, tg):
    # Same kind of purchase from different vendors, described the same way
    for i, (payee, note, cat) in enumerate([("RAMESH TEA STALL", "chai", "outside food"),
                                             ("VIJAY KUMAR", "chai samosa", "outside food"),
                                             ("DMRC", "metro", "travel"), ("AUTO WALA", "auto to metro", "travel")]):
        t = receive(engine, bot, payee=payee, rrn=f"10000000001{i}", balance=1000 - i)
        bot.handle_update(message(note))
        bot.handle_update(button(f"c|{cat}|{t.id}", tg.sent[-1]["message_id"]))
    new = receive(engine, bot, payee="MOHAN LAL", rrn="100000000099", balance=900)
    bot.handle_update(message("chai"))
    assert "Looks like <b>outside food</b>" in tg.sent[-1]["text"]
    assert tg.buttons()[0] == "✓ outside food"
    assert engine.storage.get_transaction(new.id).verified is False  # only a suggestion until tapped


def test_skip_leaves_it_uncategorized(engine, bot, tg):
    t = receive(engine, bot)
    bot.handle_update(button(f"s||{t.id}", tg.sent[0]["message_id"]))
    assert engine.storage.get_transaction(t.id).category == UNCATEGORIZED
    assert "Skipped" in tg.edits[-1]["text"]


def test_missed_sms_warning(engine, bot, tg):
    receive(engine, bot, rrn="100000000001", amount=100, balance=900)
    receive(engine, bot, rrn="100000000002", amount=50, balance=820)  # ₹30 unaccounted for
    assert any("₹30 left your account" in m["text"] for m in tg.sent)


def test_label_walks_most_frequent_payee_first(engine, bot, tg):
    engine.ingest([txn("ONE OFF", 1), txn("REGULAR", 2), txn("REGULAR", 3), txn("REGULAR", 4)])
    bot.handle_update(message("/label"))
    assert "REGULAR" in tg.sent[-1]["text"] and "3 payments" in tg.sent[-1]["text"]
    regular = engine.pending_payees()[0][0]
    bot.handle_update(button(f"c|travel|{regular.id}", tg.sent[-1]["message_id"]))
    assert "(+2 similar updated)" in tg.edits[-1]["text"]
    bot.handle_update(message("/label"))
    assert "ONE OFF" in tg.sent[-1]["text"]


def test_daily_summary(engine, bot, tg):
    t = receive(engine, bot, amount=220, balance=780)
    engine.correct(t, "outside food")
    receive(engine, bot, payee="SOMEONE NEW", rrn="100000000002", amount=50, balance=730)
    bot.send_summary(AT)
    text = tg.sent[-1]["text"]
    assert "₹270 spent" in text and "outside food: ₹220" in text and "1 still need a category" in text


def test_strangers_are_ignored(engine, bot, tg):
    t = receive(engine, bot)
    bot.handle_update(message("/summary", chat=999))
    bot.handle_update(button(f"c|travel|{t.id}", tg.sent[0]["message_id"], chat=999))
    assert len(tg.sent) == 1 and engine.storage.get_transaction(t.id).category == UNCATEGORIZED


def test_statement_adds_note_and_upi_id_to_sms_rows(engine, write_csv):
    from transaction_ml.ingestion.excel_parser import parse_bank_statement
    t = engine.ingest_sms(sms("RAMESH TEA STALL", "412345678901", 220, 780, day=7), AT).transaction
    path = write_csv([["Date", "Narration", "Withdrawal Amt", "Deposit Amt"],
                      ["07/11/2026", "UPI/DR/412345678901/RAMESH TEA STALL/IDFB/ramesh@ybl/evening chai", "220", ""]])
    assert engine.merge_statement(parse_bank_statement(path)) == (0, 1)
    stored = engine.storage.get_transaction(t.id)
    assert (stored.notes, stored.upi.handle_or_gateway) == ("evening chai", "ramesh@ybl")

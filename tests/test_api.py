import importlib

import pytest
from fastapi.testclient import TestClient

STATEMENT = (
    "Date,Narration,Withdrawal Amt,Deposit Amt\n"
    "01/03/2026,UPI/DR/1/SWIGGY/HDFC/swiggy@hdfc/lunch,300,\n"
    "02/03/2026,UPI/DR/2/SWIGGY/HDFC/swiggy@hdfc/dinner,450,\n"
    "03/03/2026,ACME SALARY,,85000\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BACHAT_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("BACHAT_KEY", "api-test-secret")
    import api.server
    importlib.reload(api.server)  # engine is created at import time
    return TestClient(api.server.app)


def upload(client, body=STATEMENT, name="s.csv"):
    return client.post("/api/upload", files={"file": (name, body.encode())})


def test_upload_list_and_reupload_is_idempotent(client):
    assert upload(client).json() == {"status": "success", "parsed": 3, "added": 3}
    assert upload(client).json()["added"] == 0
    rows = client.get("/api/transactions").json()
    assert [r["merchant_name"] for r in rows] == ["ACME SALARY", "SWIGGY", "SWIGGY"]  # newest first
    assert rows[1]["upi"]["note"] == "dinner"
    assert rows[0]["upi"] is None


def test_correct_propagates_to_same_merchant(client):
    upload(client)
    swiggy = [r for r in client.get("/api/transactions").json() if r["merchant_name"] == "SWIGGY"]
    res = client.post(f"/api/transactions/{swiggy[0]['id']}/correct", json={"category": "Food & Dining"})
    assert res.status_code == 200 and res.json()["reclassified"] == 1
    rows = {r["id"]: r for r in client.get("/api/transactions").json()}
    assert rows[swiggy[1]["id"]]["category"] == "Food & Dining"
    assert (rows[swiggy[0]["id"]]["verified"], rows[swiggy[1]["id"]]["verified"]) == (True, False)


def test_correct_rejects_bad_input(client):
    upload(client)
    tid = client.get("/api/transactions").json()[0]["id"]
    assert client.post(f"/api/transactions/{tid}/correct", json={"category": "Nope"}).status_code == 400
    assert client.post("/api/transactions/missing/correct", json={"category": "Income"}).status_code == 404


def test_split_validates_and_is_returned(client):
    upload(client)
    row = next(r for r in client.get("/api/transactions").json() if r["amount"] == -450)
    bad = {"splits": [{"amount": -100, "category": "Food & Dining"}]}
    assert client.post(f"/api/transactions/{row['id']}/split", json=bad).status_code == 400
    good = {"splits": [{"amount": -225, "category": "Food & Dining"}, {"amount": -225, "category": "Shared / Receivables"}]}
    assert client.post(f"/api/transactions/{row['id']}/split", json=good).status_code == 200
    saved = next(r for r in client.get("/api/transactions").json() if r["id"] == row["id"])
    assert saved["splits"] == good["splits"]


def test_manual_entry_mixes_with_statement_rows(client):
    upload(client)
    res = client.post("/api/transactions/manual", json={"merchant_name": "Chai", "amount": -20, "date": "2026-03-04T10:00:00Z"})
    assert res.status_code == 200
    # Regression: a tz-aware manual date used to break sorting against naive statement dates.
    assert client.get("/api/transactions").json()[0]["merchant_name"] == "Chai"


def test_rejects_unsupported_files(client):
    assert upload(client, "junk", "s.xls").status_code == 400
    assert upload(client, "a,b\n1,2\n", "s.csv").status_code == 400

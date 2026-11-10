import importlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-0123456789"

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
    monkeypatch.setenv("BACHAT_API_TOKEN", TOKEN)
    import api.server
    importlib.reload(api.server)  # engine is created at import time
    return TestClient(api.server.app, headers={"Authorization": f"Bearer {TOKEN}"})


def upload(client, body=STATEMENT, name="s.csv"):
    return client.post("/api/upload", files={"file": (name, body.encode())})


def test_upload_list_and_reupload_is_idempotent(client):
    assert upload(client).json() == {"status": "success", "parsed": 3, "added": 3, "enriched": 0}
    assert upload(client).json()["added"] == 0
    rows = client.get("/api/transactions").json()
    assert [r["merchant_name"] for r in rows] == ["ACME SALARY", "SWIGGY", "SWIGGY"]  # newest first
    assert rows[1]["upi"]["note"] == "dinner"
    assert rows[0]["upi"] is None


def test_correct_propagates_to_same_merchant(client):
    upload(client)
    swiggy = [r for r in client.get("/api/transactions").json() if r["merchant_name"] == "SWIGGY"]
    res = client.post(f"/api/transactions/{swiggy[0]['id']}/correct", json={"category": "outside food"})
    assert res.status_code == 200 and res.json()["reclassified"] == 1
    rows = {r["id"]: r for r in client.get("/api/transactions").json()}
    assert rows[swiggy[1]["id"]]["category"] == "outside food"
    assert (rows[swiggy[0]["id"]]["verified"], rows[swiggy[1]["id"]]["verified"]) == (True, False)


def test_correct_rejects_bad_input(client):
    upload(client)
    tid = client.get("/api/transactions").json()[0]["id"]
    assert client.post(f"/api/transactions/{tid}/correct", json={"category": "Nope"}).status_code == 400
    assert client.post("/api/transactions/missing/correct", json={"category": "travel"}).status_code == 404


def test_split_validates_and_is_returned(client):
    upload(client)
    row = next(r for r in client.get("/api/transactions").json() if r["amount"] == -450)
    bad = {"splits": [{"amount": -100, "category": "outside food"}]}
    assert client.post(f"/api/transactions/{row['id']}/split", json=bad).status_code == 400
    good = {"splits": [{"amount": -225, "category": "outside food"}, {"amount": -225, "category": "Friends / splits"}]}
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


SMS = ("Your A/c XX1234 debited by Rs. 196.00 on 06/11/26; RAVI KUMAR credited. RRN 100000000001. "
       "Available balance Rs. 747.90. Team IDFC FIRST Bank")


def test_every_endpoint_requires_the_token(client):
    for method, path in [("get", "/api/transactions"), ("get", "/api/categories"), ("post", "/api/sms")]:
        assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong-token-000000"}).status_code == 401
        assert getattr(client, method)(path, headers={"Authorization": ""}).status_code == 401


def test_server_refuses_to_start_without_a_token(monkeypatch, tmp_path):
    monkeypatch.setenv("BACHAT_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("BACHAT_API_TOKEN", "short")
    import api.server
    with pytest.raises(RuntimeError):
        importlib.reload(api.server)


def test_sms_endpoint_flow(client):
    res = client.post("/api/sms", json={"text": SMS, "received_at": "2026-11-06T13:05:42+05:30"}).json()
    assert (res["status"], res["needs_review"], res["balance_gap"]) == ("added", True, 0.0)
    assert res["transaction"]["merchant_name"] == "RAVI KUMAR"
    assert res["transaction"]["date"].startswith("2026-11-06")
    client.post(f"/api/transactions/{res['transaction']['id']}/correct", json={"category": "mess food"})
    again = client.post("/api/sms", json={"text": SMS}).json()
    assert (again["status"], again["transaction"]["category"]) == ("duplicate", "mess food")
    assert client.post("/api/sms", json={"text": "Your OTP is 1234"}).json()["status"] == "ignored"


def test_category_endpoints(client):
    names = lambda r: [c["name"] for c in r.json()["categories"]]
    assert "mess food" in names(client.get("/api/categories"))
    assert "snacks" in names(client.post("/api/categories", json={"name": "snacks"}))
    assert "chai" in names(client.patch("/api/categories/snacks", json={"new_name": "chai"}))
    assert "chai" not in names(client.delete("/api/categories/chai"))
    assert client.delete("/api/categories/Money received").status_code == 400
    assert client.post("/api/categories", json={"name": "travel"}).status_code == 400

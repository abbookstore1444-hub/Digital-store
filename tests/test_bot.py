"""Smoke tests for the security fixes and the screenshot-reminder buttons.
Run with: pip install -r requirements.txt -r requirements-dev.txt && pytest
No network calls: everything that would talk to Meta, QPay or Google is stubbed."""

import asyncio
import hashlib
import hmac
import json
import os

# Products are read from the environment at import time.
os.environ.setdefault("PRODUCT_1_KEYWORDS", "no1")
os.environ.setdefault("PRODUCT_1_AMOUNT", "15000")
os.environ.setdefault("PRODUCT_1_DESCRIPTION", "Test ebook")
os.environ.setdefault("PRODUCT_1_DRIVE_LINK", "https://drive.google.com/file/d/ABC/view")

import pytest
from fastapi.testclient import TestClient

import qpay_digital_products_bot as bot

client = TestClient(bot.app)
APP_SECRET = "app-secret"
ADMIN_SECRET = "admin-secret"


@pytest.fixture(autouse=True)
def secrets_and_stubs(monkeypatch):
    monkeypatch.setattr(bot, "META_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(bot, "TEST_ENDPOINT_SECRET", ADMIN_SECRET)
    sent = []

    async def fake_send(recipient, message):
        sent.append((recipient, message))

    monkeypatch.setattr(bot, "send_meta_message", fake_send)
    bot.AWAITING_PAYMENT_SCREENSHOT.clear()
    bot.PROCESSED_EVENT_IDS.clear()
    return sent


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_health():
    assert client.get("/").json()["status"] == "ok"


@pytest.mark.parametrize("method,path", [
    ("post", "/test-deliver?psid=1"),
    ("post", "/test-menu?psid=1"),
    ("post", "/test-category-menu?psid=1"),
    ("post", "/setup-messenger-profile"),
    ("get", "/payment-status/x"),
])
def test_admin_endpoints_need_secret(method, path, monkeypatch):
    assert getattr(client, method)(path).status_code == 403
    sep = "&" if "?" in path else "?"
    assert getattr(client, method)(f"{path}{sep}secret=wrong").status_code == 403
    # Disabled entirely when no secret is configured, even with an empty one passed.
    monkeypatch.setattr(bot, "TEST_ENDPOINT_SECRET", "")
    assert getattr(client, method)(f"{path}{sep}secret=").status_code == 403


def test_create_invoice_needs_secret():
    r = client.post("/create-invoice", json={"amount": 100, "description": "x"})
    assert r.status_code == 403


def test_admin_endpoint_works_with_secret(secrets_and_stubs):
    r = client.post(f"/test-deliver?psid=123&secret={ADMIN_SECRET}")
    assert r.status_code == 200
    assert any("drive.google.com" in m.get("text", "") for _, m in secrets_and_stubs)


def test_webhook_rejects_unsigned_and_bad_signature():
    body = json.dumps({"object": "page", "entry": []}).encode()
    assert client.post("/meta-webhook", content=body).status_code == 403
    assert client.post("/meta-webhook", content=body,
                       headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403


def test_webhook_rejected_when_app_secret_missing(monkeypatch):
    monkeypatch.setattr(bot, "META_APP_SECRET", "")
    body = b'{"object":"page","entry":[]}'
    assert client.post("/meta-webhook", content=body,
                       headers={"X-Hub-Signature-256": sign(body)}).status_code == 503


def test_webhook_accepts_valid_signature():
    body = json.dumps({"object": "page", "entry": []}).encode()
    r = client.post("/meta-webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200


def test_summarize_webhook_hides_text_and_names():
    data = {"entry": [{
        "messaging": [{"sender": {"id": "42"}, "message": {"mid": "m1", "text": "my secret address"}}],
        "changes": [{"field": "feed", "value": {"item": "comment", "verb": "add", "comment_id": "c1",
                                                 "message": "hello", "from": {"id": "9", "name": "Bat"}}}],
    }]}
    line = bot.summarize_webhook(data)
    assert "messaging:text sender=42" in line and "comment=c1" in line
    assert "secret address" not in line and "Bat" not in line and "hello" not in line


def test_screenshot_reminder_has_cancel_and_menu_buttons(secrets_and_stubs):
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = "order-1"
    event = {"sender": {"id": "555"}, "timestamp": 1, "message": {"mid": "m2", "text": "paid?"}}
    asyncio.run(bot.handle_messaging_event(event))
    _, message = secrets_and_stubs[-1]
    payloads = [q["payload"] for q in message.get("quick_replies", [])]
    assert payloads == ["CANCEL_ORDER", "VIEW_MENU"]
    assert bot.AWAITING_PAYMENT_SCREENSHOT["555"] == "order-1"  # still waiting


def test_cancel_quick_reply_clears_pending_order(secrets_and_stubs, monkeypatch):
    monkeypatch.setattr(bot, "mark_order_cancelled", lambda order_id: None)
    bot.AWAITING_PAYMENT_SCREENSHOT["556"] = "order-2"
    event = {"sender": {"id": "556"}, "timestamp": 2,
             "message": {"mid": "m3", "text": "x", "quick_reply": {"payload": "CANCEL_ORDER"}}}
    asyncio.run(bot.handle_messaging_event(event))
    assert "556" not in bot.AWAITING_PAYMENT_SCREENSHOT

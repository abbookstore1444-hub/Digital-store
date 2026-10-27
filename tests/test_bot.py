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

# The fixture below stubs send_meta_message; keep the real one for the token test.
SEND_META_MESSAGE = bot.send_meta_message
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


def test_manual_buy_shows_cancel_with_bank_details(secrets_and_stubs, monkeypatch):
    monkeypatch.setattr(bot, "PAYMENT_MODE", "manual")
    monkeypatch.setattr(bot, "log_new_order", lambda *a, **k: None)
    monkeypatch.setattr(bot, "COMMENTER_NAMES", {"557": "Test"})
    event = {"sender": {"id": "557"}, "timestamp": 3, "postback": {"payload": "QPAY_PAY_1"}}
    asyncio.run(bot.handle_messaging_event(event))
    _, message = secrets_and_stubs[-1]
    assert [q["payload"] for q in message.get("quick_replies", [])] == ["CANCEL_ORDER", "VIEW_MENU"]
    assert "557" in bot.AWAITING_PAYMENT_SCREENSHOT


def test_failed_send_error_does_not_contain_token(monkeypatch):
    monkeypatch.setattr(bot, "META_PAGE_ACCESS_TOKEN", "tok123")
    monkeypatch.setattr(bot, "REPLY_DELAY_SECONDS", 0)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return bot.httpx.Response(400, json={"error": {"message": "bad"}})

    real_client = bot.httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=bot.httpx.MockTransport(handler)))
    with pytest.raises(bot.GraphAPIError) as err:
        asyncio.run(SEND_META_MESSAGE({"id": "1"}, {"text": "hi"}))
    assert "access_token=tok123" in seen["url"]  # sent the documented way
    assert "tok123" not in str(err.value) and "/me/messages" in str(err.value)
    assert "400" in str(err.value) and "bad" in str(err.value)


def test_httpx_does_not_log_request_urls():
    import logging
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_cancel_quick_reply_clears_pending_order(secrets_and_stubs, monkeypatch):
    monkeypatch.setattr(bot, "mark_order_cancelled", lambda order_id: None)
    bot.AWAITING_PAYMENT_SCREENSHOT["556"] = "order-2"
    event = {"sender": {"id": "556"}, "timestamp": 2,
             "message": {"mid": "m3", "text": "x", "quick_reply": {"payload": "CANCEL_ORDER"}}}
    asyncio.run(bot.handle_messaging_event(event))
    assert "556" not in bot.AWAITING_PAYMENT_SCREENSHOT


# --- Gemini screenshot check, Gemini answers and Telegram owner notifications ---

PRODUCT = next(p for p in bot.PRODUCTS if p.index == 1)  # 15000₮
ORDER = "555-1-abc123"
GOOD_RECEIPT = {
    "is_payment_receipt": True, "transfer_successful": True, "amount": 15000,
    "recipient_account": "5123456789", "recipient_name": "Test Shop",
    "transfer_date": "2026-10-27", "transaction_reference": "TX1", "looks_edited": False,
}
NOW = bot.datetime(2026, 10, 27, 12, tzinfo=bot.timezone.utc)


@pytest.fixture
def bank(monkeypatch):
    monkeypatch.setattr(bot, "BANK_ACCOUNT_NUMBER", "5123456789")
    monkeypatch.setattr(bot, "BANK_ACCOUNT_NAME", "")
    bot.USED_RECEIPTS.clear()


@pytest.fixture
def owner(monkeypatch, bank):
    """Telegram + Gemini configured; records what reaches the owner and what gets delivered."""
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(bot, "TELEGRAM_ADMIN_CHAT_ID", "99")
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "gemini-key")
    rec = {"notify": [], "delivered": [], "telegram": [], "menu": []}

    async def fake_notify(text, photo=None, buttons=None):
        rec["notify"].append((text, photo, buttons))

    async def fake_fulfill(order_id):
        rec["delivered"].append(order_id)

    async def fake_download(url):
        return b"image-bytes", "image/jpeg"

    async def fake_telegram(method, data, files=None):
        rec["telegram"].append((method, data))

    async def fake_menu(psid, *a):
        rec["menu"].append(psid)

    monkeypatch.setattr(bot, "notify_owner", fake_notify)
    monkeypatch.setattr(bot, "fulfill_manual_order", fake_fulfill)
    monkeypatch.setattr(bot, "download_image", fake_download)
    monkeypatch.setattr(bot, "telegram_call", fake_telegram)
    monkeypatch.setattr(bot, "send_category_menu", fake_menu)

    async def fake_contact(psid):
        return "Болор Цэцэг", f"https://inbox/{psid}"

    monkeypatch.setattr(bot, "customer_contact", fake_contact)
    monkeypatch.setitem(bot.INVOICES, ORDER, {
        "invoice_id": "MANUAL", "status": "PENDING", "psid": "555", "product_index": 1, "email": None,
    })
    return rec


def gemini_returns(monkeypatch, result):
    async def fake_gemini(parts, schema):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(bot, "gemini_json", fake_gemini)


def photo_event(mid="p1"):
    return {"sender": {"id": "555"}, "timestamp": 10,
            "message": {"mid": mid, "attachments": [{"type": "image", "payload": {"url": "https://cdn/x.jpg"}}]}}


def test_judge_receipt_accepts_the_right_payment(bank):
    assert bot.judge_receipt(GOOD_RECEIPT, PRODUCT, "img:1", now=NOW) == (True, "OK")


@pytest.mark.parametrize("change", [
    {"amount": 10000},
    {"amount": None},
    {"recipient_account": "5999999999"},
    {"is_payment_receipt": False},
    {"transfer_successful": False},
    {"looks_edited": True},
    {"recipient_account": None, "recipient_name": None},
    {"transfer_date": "2026-10-01"},
])
def test_judge_receipt_rejects_doubtful_screenshots(bank, change):
    accepted, reason = bot.judge_receipt({**GOOD_RECEIPT, **change}, PRODUCT, "img:1", now=NOW)
    assert not accepted and reason


def test_judge_receipt_rejects_a_reused_receipt(bank):
    bot.USED_RECEIPTS.add("ref:TX1")
    assert not bot.judge_receipt(GOOD_RECEIPT, PRODUCT, "img:new", now=NOW)[0]


def test_judge_receipt_needs_bank_details(bank, monkeypatch):
    monkeypatch.setattr(bot, "BANK_ACCOUNT_NUMBER", "")
    assert not bot.judge_receipt(GOOD_RECEIPT, PRODUCT, "img:1", now=NOW)[0]


@pytest.mark.parametrize("seen,ok", [
    ("5123456789", True), ("5123 4567 89", True), ("MN12 0005 0051 2345 6789", True),
    ("****6789", True), ("5123****6789", True), ("****1111", False), ("9999****6789", False),
    ("", False), (None, False),
])
def test_account_matches(seen, ok):
    assert bot.account_matches(seen, "5123456789") is ok


def test_good_screenshot_is_delivered_without_the_owner(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, {**GOOD_RECEIPT, "transfer_date": None})
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    asyncio.run(bot.handle_messaging_event(photo_event()))
    assert owner["delivered"] == [ORDER] and owner["notify"] == []
    assert "ref:TX1" in bot.USED_RECEIPTS


def test_doubtful_screenshot_goes_to_the_owner(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, {**GOOD_RECEIPT, "amount": 5000, "transfer_date": None})
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    asyncio.run(bot.handle_messaging_event(photo_event()))
    assert owner["delivered"] == []
    text, photo, buttons = owner["notify"][0]
    assert "5000" in text and photo == (b"image-bytes", "image/jpeg")
    assert "Болор Цэцэг" in text and "https://inbox/555" in text
    assert [b["callback_data"] for b in buttons["inline_keyboard"][0]] == [f"pay:ok:{ORDER}", f"pay:no:{ORDER}"]
    assert secrets_and_stubs[-1][1]["text"] == bot.PAYMENT_UNDER_REVIEW_TEXT


def test_gemini_failure_sends_screenshot_to_the_owner(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, bot.GeminiError("all busy"))
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    asyncio.run(bot.handle_messaging_event(photo_event()))
    assert owner["delivered"] == [] and len(owner["notify"]) == 1


def test_without_telegram_any_photo_still_delivers(secrets_and_stubs, owner, monkeypatch):
    monkeypatch.setattr(bot, "TELEGRAM_ADMIN_CHAT_ID", "")
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    asyncio.run(bot.handle_messaging_event(photo_event()))
    assert owner["delivered"] == [ORDER]


def telegram_post(body, secret=None):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret if secret is not None else bot.telegram_webhook_secret()}
    return client.post("/telegram-webhook", json=body, headers=headers)


def callback(data, chat_id=99):
    return {"callback_query": {"id": "cb1", "data": data,
                               "message": {"message_id": 5, "chat": {"id": chat_id}, "caption": "x", "photo": [{}]}}}


def test_telegram_webhook_needs_the_secret(owner):
    assert telegram_post(callback(f"pay:ok:{ORDER}"), secret="wrong").status_code == 403
    assert owner["delivered"] == []


def test_owner_approve_delivers(owner):
    assert telegram_post(callback(f"pay:ok:{ORDER}")).status_code == 200
    assert owner["delivered"] == [ORDER]
    assert [m for m, _ in owner["telegram"]] == ["answerCallbackQuery", "editMessageCaption"]
    assert owner["telegram"][1][1]["reply_markup"] == {"inline_keyboard": []}


def test_owner_reject_asks_the_customer_again(secrets_and_stubs, owner):
    telegram_post(callback(f"pay:no:{ORDER}"))
    assert owner["delivered"] == []
    assert secrets_and_stubs[-1][1]["text"] == bot.PAYMENT_REJECTED_TEXT
    assert bot.AWAITING_PAYMENT_SCREENSHOT["555"] == ORDER


def test_buttons_from_another_chat_do_nothing(owner):
    telegram_post(callback(f"pay:ok:{ORDER}", chat_id=7))
    assert owner["delivered"] == []


def test_question_answered_by_gemini_then_menu(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, {"reply": "Сайн байна уу!", "needs_owner": False, "product_index": None})
    asyncio.run(bot.process_text_messages("700", ["сайн уу"]))
    assert secrets_and_stubs[-1][1]["text"] == "Сайн байна уу!"
    assert owner["menu"] == ["700"] and owner["notify"] == []


def test_question_gemini_cannot_answer_reaches_the_owner(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, {"reply": "Таны хүсэлтийг хүлээж авлаа.", "needs_owner": True, "product_index": None})
    asyncio.run(bot.process_text_messages("700", ["мөнгөө буцааж авмаар байна"]))
    text = owner["notify"][0][0]
    assert "мөнгөө буцааж авмаар байна" in text
    assert "Болор Цэцэг" in text and "https://inbox/700" in text
    assert secrets_and_stubs[-1][1]["text"] == "Таны хүсэлтийг хүлээж авлаа."


def test_gemini_is_told_not_to_promise_the_owner(secrets_and_stubs, owner, monkeypatch):
    seen = {}

    async def fake_gemini(parts, schema):
        seen["prompt"] = parts[0]["text"]
        return {"reply": "OK", "needs_owner": False, "product_index": None}

    monkeypatch.setattr(bot, "gemini_json", fake_gemini)
    asyncio.run(bot.process_text_messages("700", ["hi"]))
    assert "Never say that the owner" in seen["prompt"] and "owner will reply soon" not in seen["prompt"]
    assert "NO refunds" in seen["prompt"] and "ONLY when the customer asks" in seen["prompt"]
    assert "төлбөрийг буцаан олгох боломжгүй байна" in seen["prompt"]


def test_customer_contact_reads_name_from_the_conversation(monkeypatch):
    monkeypatch.setattr(bot, "PAGE_ID", "1304")
    monkeypatch.setattr(bot, "COMMENTER_NAMES", {})

    def handler(request):
        assert request.url.path.endswith("/me/conversations")
        assert request.url.params["user_id"] == "555"
        return bot.httpx.Response(200, json={"data": [{"link": "/x/inbox/1", "participants": {"data": [
            {"id": "1304", "name": "Уншлагын өртөө"}, {"id": "555", "name": "Сарнай Дорж"}]}}]})

    real_client = bot.httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=bot.httpx.MockTransport(handler)))
    name, link = asyncio.run(bot.customer_contact("555"))
    assert name == "Сарнай Дорж"
    assert "asset_id=1304" in link and "selected_item_id=555" in link


def test_question_about_a_product_shows_its_buy_button(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, {"reply": "Тийм ээ.", "needs_owner": False, "product_index": 1})
    asyncio.run(bot.process_text_messages("700", ["test ebook авмаар байна"]))
    buttons = secrets_and_stubs[-1][1]["attachment"]["payload"]["buttons"]
    assert buttons[0]["payload"] == PRODUCT.payload and owner["menu"] == []


def test_question_during_payment_gets_a_gemini_answer(secrets_and_stubs, owner, monkeypatch):
    seen = {}

    async def fake_gemini(parts, schema):
        seen["prompt"] = parts[0]["text"]
        return {"reply": "Excel-ийн томъёо заадаг. Төлбөрөө хийгээд зургаа илгээнэ үү.", "needs_owner": False, "product_index": 1}

    monkeypatch.setattr(bot, "gemini_json", fake_gemini)
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    event = {"sender": {"id": "555"}, "timestamp": 11, "message": {"mid": "q1", "text": "энэ ном юу заадаг вэ"}}
    asyncio.run(bot.handle_messaging_event(event))
    _, message = secrets_and_stubs[-1]
    assert message["text"].startswith("Excel-ийн томъёо")
    assert [q["payload"] for q in message["quick_replies"]] == ["CANCEL_ORDER", "VIEW_MENU"]
    assert "waiting for a screenshot" in seen["prompt"] and "Test ebook" in seen["prompt"]
    assert "цахим гарын авлага" in seen["prompt"] and "хэвлэмэл ном биш" in seen["prompt"]
    assert bot.AWAITING_PAYMENT_SCREENSHOT["555"] == ORDER and owner["menu"] == []


def test_question_during_payment_falls_back_to_the_reminder(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, bot.GeminiError("busy"))
    bot.AWAITING_PAYMENT_SCREENSHOT["555"] = ORDER
    event = {"sender": {"id": "555"}, "timestamp": 12, "message": {"mid": "q2", "text": "hello"}}
    asyncio.run(bot.handle_messaging_event(event))
    assert secrets_and_stubs[-1][1]["text"] == bot.AWAITING_SCREENSHOT_REMINDER_TEXT
    assert bot.AWAITING_PAYMENT_SCREENSHOT["555"] == ORDER


def test_gemini_failure_falls_back_to_the_fixed_reply(secrets_and_stubs, owner, monkeypatch):
    gemini_returns(monkeypatch, bot.GeminiError("busy"))
    asyncio.run(bot.process_text_messages("700", ["hi"]))
    assert secrets_and_stubs[-1][1]["text"] == bot.FALLBACK_REPLY_TEXT


def test_error_logs_reach_the_owner_once(owner):
    handler = next(h for h in bot.logger.handlers if isinstance(h, bot.TelegramErrorHandler))
    handler.last_sent.clear()

    async def run():
        bot.logger.error("Delivery failed for order X")
        bot.logger.error("Delivery failed for order X")
        bot.logger.warning("just a warning")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(owner["notify"]) == 1 and "Delivery failed" in owner["notify"][0][0]

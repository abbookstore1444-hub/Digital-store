"""
QPay + Meta Messenger Bot -- DIGITAL PRODUCTS EDITION
------------------------------------------------------
Same core idea as the original QPay Messenger bot (comment a keyword -> get
a Pay button -> pay via QPay -> get your thing), but "your thing" here is a
downloadable file (PDF / ebook) stored in Google Drive, instead of a
Facebook Group invite or a video.

DELIVERY: after a customer pays, they get the file THREE ways:
    1. A Google Drive link, sent immediately in Messenger.
    2. The actual file, sent as a Messenger attachment (best-effort --
       Facebook fetches it directly from Drive, so this costs Render
       nothing regardless of file size).
    3. The bot then asks the customer for their email address. Once they
       reply with one, it emails them the Drive link via plain SMTP. By
       default the email does NOT attach the actual file -- attaching
       would mean downloading it onto Render first, using bandwidth and
       memory there. Set EMAIL_ATTACH_FILES=true if you want the email to
       include the real file as an attachment instead of just a link.

WHY GOOGLE DRIVE, NO API NEEDED: each product's file just needs to be
shared as "Anyone with the link can view" in Google Drive. You paste that
share link into an environment variable -- no Google API credentials,
no OAuth. This keeps things simple, but does mean anyone who gets the link
can access the file (same trade-off as the group-link approach in the
original bot).

CAVEAT ON LARGE FILES: Google Drive shows a "can't scan for viruses"
confirmation page for files it can't quickly scan (common above ~25-100MB,
depending on file type). This bot handles the small-file case (direct
download works) and the confirm-token case (handles Drive's confirmation
redirect). If a file is too large for either, delivery automatically falls
back to just sending the link instead of the actual bytes -- nothing
breaks, the customer just gets a link instead of an attachment.

Configure products via environment variables on Render:

    PRODUCT_1_KEYWORDS=no.1,no1
    PRODUCT_1_AMOUNT=15000
    PRODUCT_1_DESCRIPTION=My Ebook
    PRODUCT_1_DRIVE_LINK=https://drive.google.com/file/d/XXXXXXXXXXXX/view
    PRODUCT_1_FILENAME=my-ebook.pdf

    PRODUCT_2_KEYWORDS=no.2,no2
    PRODUCT_2_AMOUNT=20000
    PRODUCT_2_DESCRIPTION=Another Ebook
    PRODUCT_2_DRIVE_LINK=https://drive.google.com/file/d/YYYYYYYYYYYY/view
    PRODUCT_2_FILENAME=another-ebook.pdf

    ...no limit, just keep numbering with no gaps.

Email sending uses plain SMTP (works with Gmail app passwords, SendGrid,
Mailgun's SMTP endpoint, etc.) -- configure with:

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USERNAME=you@yourbusiness.com
    SMTP_PASSWORD=your-app-password
    SMTP_FROM_EMAIL=you@yourbusiness.com
    SMTP_FROM_NAME=Your Business Name

If SMTP isn't configured, the bot just skips the email step (Messenger +
Drive link delivery still work fine on their own).
"""

import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

from qpay_client.v2 import AsyncQPayClient, QPaySettings
from qpay_client.v2.enums import ObjectType
from qpay_client.v2.schemas import (
    InvoiceCreateSimpleRequest,
    Offset,
    PaymentCheckRequest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qpay_digital_bot")

app = FastAPI(title="QPay Digital Products Messenger Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# CONFIGURATION -- all set as Environment Variables on Render.
# ---------------------------------------------------------------------------

QPAY_ENV = os.environ.get("QPAY_ENV", "sandbox")
CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL", "https://example.com")

# --- Meta / Messenger settings ---
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

TEST_ENDPOINT_SECRET = os.environ.get("TEST_ENDPOINT_SECRET", "")

COMMENT_REPLY_TEXT = os.environ.get(
    "COMMENT_REPLY_TEXT",
    "Танд мэдээллийг чатаар илгээлээ! \U0001F4E9",
)

# Sent right after payment, asking the customer for their email so we can
# also send the file there. Customize freely.
ASK_EMAIL_TEXT = os.environ.get(
    "ASK_EMAIL_TEXT",
    "\U0001F4E7 Файлыг мөн имэйлээр авахыг хүсвэл имэйл хаягаа энд бичээд илгээнэ үү.",
)
EMAIL_SENT_TEXT = os.environ.get(
    "EMAIL_SENT_TEXT",
    "\u2705 Баярлалаа! Файлыг таны имэйл хаягт илгээлээ.",
)
EMAIL_INVALID_TEXT = os.environ.get(
    "EMAIL_INVALID_TEXT",
    "Уучлаарай, имэйл хаяг зөв бичигдээгүй байна. Дахин оролдоно уу.",
)

BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "This business")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")

# Google Sheets logging (optional).
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# --- Email delivery settings (plain SMTP) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", BUSINESS_NAME)

# Whether to actually download the file from Drive and attach it to the
# email. Defaults to OFF -- emails just include the Drive link instead,
# which costs zero Render bandwidth/memory. Set to "true" to attach the
# real file to emails (Messenger delivery already sends the actual file
# separately either way, at no cost to Render, since Facebook fetches it
# directly from Drive).
EMAIL_ATTACH_FILES = os.environ.get("EMAIL_ATTACH_FILES", "false").lower() == "true"

# Max file size we'll try to download+attach to an email, if
# EMAIL_ATTACH_FILES is enabled. Keeps memory usage sane on a small Render
# instance.
MAX_ATTACHMENT_MB = float(os.environ.get("MAX_ATTACHMENT_MB", "20"))


@dataclass
class Product:
    index: int
    keywords: list[str]
    amount: float
    description: str
    drive_link: str = ""
    filename: str = "file.pdf"

    @property
    def payload(self) -> str:
        return f"QPAY_PAY_{self.index}"

    @property
    def drive_file_id(self) -> str | None:
        return extract_drive_file_id(self.drive_link)


def extract_drive_file_id(drive_link: str) -> str | None:
    """Pulls the file ID out of a Google Drive share link, e.g.
    'https://drive.google.com/file/d/ABC123/view?usp=sharing' -> 'ABC123',
    or 'https://drive.google.com/open?id=ABC123' -> 'ABC123'."""
    if not drive_link:
        return None
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", drive_link)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", drive_link)
    if match:
        return match.group(1)
    return None


def build_drive_direct_link(file_id: str) -> str:
    """A direct-download style link. Works without a confirmation page for
    smaller files; larger files need the confirm-token flow handled in
    download_drive_file below."""
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def load_products() -> list[Product]:
    """Reads PRODUCT_1_..., PRODUCT_2_..., etc. Stops at the first missing
    number, so products must be numbered without gaps starting from 1."""
    products: list[Product] = []
    i = 1
    while True:
        keywords_raw = os.environ.get(f"PRODUCT_{i}_KEYWORDS")
        if not keywords_raw:
            break
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
        amount = float(os.environ.get(f"PRODUCT_{i}_AMOUNT", "0"))
        description = os.environ.get(f"PRODUCT_{i}_DESCRIPTION", f"Product {i}")
        drive_link = os.environ.get(f"PRODUCT_{i}_DRIVE_LINK", "")
        filename = os.environ.get(f"PRODUCT_{i}_FILENAME", f"product_{i}.pdf")
        products.append(Product(i, keywords, amount, description, drive_link, filename))
        i += 1
    return products


PRODUCTS = load_products()


def get_qpay_settings() -> QPaySettings:
    if QPAY_ENV == "production":
        return QPaySettings.production(
            username=os.environ["QPAY_USERNAME"],
            password=os.environ["QPAY_PASSWORD"],
            invoice_code=os.environ["QPAY_INVOICE_CODE"],
        )
    return QPaySettings.sandbox()


def get_orders_sheet():
    if not GOOGLE_SHEETS_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        return None
    creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1


def log_new_order(order_id: str, amount: float, description: str, customer_name: str = "") -> None:
    sheet = get_orders_sheet()
    if not sheet:
        return
    sheet.append_row([
        order_id, customer_name, description, amount, "PENDING",
        datetime.now(timezone.utc).isoformat(),
    ])


def mark_order_paid(order_id: str) -> None:
    sheet = get_orders_sheet()
    if not sheet:
        return
    try:
        cell = sheet.find(order_id)
        sheet.update_cell(cell.row, 5, "PAID")
    except Exception:
        pass


def update_order_email(order_id: str, email: str) -> None:
    """Best-effort: adds the customer's email into a 6th column, if the
    sheet is configured and the order row can be found."""
    sheet = get_orders_sheet()
    if not sheet:
        return
    try:
        cell = sheet.find(order_id)
        sheet.update_cell(cell.row, 6, email)
    except Exception:
        pass


async def get_customer_name(psid: str) -> str:
    if not META_PAGE_ACCESS_TOKEN:
        return ""
    url = f"{GRAPH_API_BASE}/{psid}"
    params = {"fields": "first_name,last_name", "access_token": META_PAGE_ACCESS_TOKEN}
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, params=params)
            data = resp.json()
        return f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    except Exception:
        return ""


# In-memory "databases". Reset on restart -- fine for testing, swap for a
# real database (e.g. Postgres) once this is handling real volume.
INVOICES: dict[str, dict] = {}
# Tracks which PSIDs we're currently waiting on an email reply from, and
# which order that email is for.
AWAITING_EMAIL: dict[str, str] = {}

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# META / MESSENGER HELPERS
# ---------------------------------------------------------------------------

def verify_meta_signature(raw_body: bytes, signature_header: str) -> None:
    if not META_APP_SECRET:
        return
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing signature")
    expected = hmac.new(META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("sha256=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Invalid signature")


async def send_meta_message(recipient: dict, message: dict) -> None:
    url = f"{GRAPH_API_BASE}/me/messages"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    payload = {"recipient": recipient, "message": message, "messaging_type": "RESPONSE"}
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params, json=payload)
        logger.info("Meta Send API response: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()


async def send_pay_button(recipient: dict, product: Product) -> None:
    await send_meta_message(
        recipient,
        {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": f"{product.description} -- {product.amount:.0f}\u20ae",
                    "buttons": [
                        {"type": "postback", "title": "Qpay-ээр төлөх", "payload": product.payload}
                    ],
                },
            }
        },
    )


async def send_file_via_messenger(psid: str, product: Product) -> bool:
    """Attempts to send the actual file as a Messenger attachment, by
    pointing Meta at the Drive direct-download link (Meta fetches it
    server-side). Returns True if Meta accepted it, False otherwise (e.g.
    file too large, or Drive served a confirmation page instead of the
    file). Failure here is not fatal -- the Drive link is sent separately
    regardless."""
    file_id = product.drive_file_id
    if not file_id:
        return False
    direct_link = build_drive_direct_link(file_id)
    try:
        await send_meta_message(
            {"id": psid},
            {
                "attachment": {
                    "type": "file",
                    "payload": {"url": direct_link, "is_reusable": False},
                }
            },
        )
        return True
    except Exception as e:
        logger.warning("Failed to send file attachment via Messenger for psid=%s: %s", psid, e)
        return False


async def like_comment(comment_id: str) -> None:
    url = f"{GRAPH_API_BASE}/{comment_id}/likes"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params)
        resp.raise_for_status()


async def reply_to_comment(comment_id: str, message: str) -> None:
    url = f"{GRAPH_API_BASE}/{comment_id}/comments"
    params = {"access_token": META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, params=params, json={"message": message})
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# GOOGLE DRIVE FILE DOWNLOAD (for email attachments) -- no API key needed,
# just fetches the public share link. Handles Drive's "can't scan this
# file for viruses" confirmation-page redirect for larger files.
# ---------------------------------------------------------------------------

async def download_drive_file(file_id: str) -> bytes | None:
    """Downloads a publicly-shared Drive file's bytes. Returns None if it's
    too large, or if Drive won't serve it directly (e.g. permissions)."""
    direct_link = build_drive_direct_link(file_id)
    max_bytes = int(MAX_ATTACHMENT_MB * 1024 * 1024)

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        resp = await http_client.get(direct_link)
        content_type = resp.headers.get("content-type", "")

        # Large files: Drive returns an HTML confirmation page with a
        # "confirm" token embedded, instead of the file itself.
        if "text/html" in content_type:
            match = re.search(r'confirm=([0-9A-Za-z_-]+)', resp.text)
            if not match:
                logger.warning("Drive served HTML with no confirm token for file_id=%s", file_id)
                return None
            confirm_token = match.group(1)
            confirm_link = f"{direct_link}&confirm={confirm_token}"
            resp = await http_client.get(confirm_link)
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                logger.warning("Drive still serving HTML after confirm token for file_id=%s", file_id)
                return None

        if len(resp.content) > max_bytes:
            logger.info(
                "Drive file_id=%s is %.1fMB, over the %.1fMB attachment limit -- skipping attachment",
                file_id, len(resp.content) / (1024 * 1024), MAX_ATTACHMENT_MB,
            )
            return None

        return resp.content


# ---------------------------------------------------------------------------
# EMAIL DELIVERY (plain SMTP)
# ---------------------------------------------------------------------------

def _send_email_sync(to_email: str, subject: str, body_text: str, attachment: tuple[str, bytes] | None) -> None:
    """Blocking SMTP send -- call via asyncio.to_thread from async code."""
    msg = MIMEMultipart()
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    if attachment:
        filename, file_bytes = attachment
        part = MIMEApplication(file_bytes, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM_EMAIL, [to_email], msg.as_string())


async def send_product_email(to_email: str, product: Product) -> bool:
    """Emails the customer their product. Attaches the actual file if it
    can be downloaded within the size limit, otherwise just includes the
    Drive link in the email body. Returns True on success."""
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        logger.warning("SMTP not configured -- skipping email delivery")
        return False

    file_id = product.drive_file_id
    attachment = None
    if EMAIL_ATTACH_FILES and file_id:
        file_bytes = await download_drive_file(file_id)
        if file_bytes:
            attachment = (product.filename, file_bytes)

    body = f"Худалдан авалтад баярлалаа!\n\n{product.description}\n\n"
    if attachment:
        body += "Файл энэ имэйлд хавсаргагдсан байгаа.\n"
    if product.drive_link:
        body += f"\nDrive-с татах холбоос: {product.drive_link}\n"

    try:
        import asyncio
        await asyncio.to_thread(
            _send_email_sync, to_email, product.description, body, attachment
        )
        return True
    except Exception as e:
        logger.warning("Failed to send email to %s: %s", to_email, e)
        return False


# ---------------------------------------------------------------------------
# INVOICE CREATION
# ---------------------------------------------------------------------------

async def create_qpay_invoice(
    order_id: str, amount: float, description: str, customer_name: str = "",
    psid: str = "", product_index: int | None = None,
) -> dict:
    settings = get_qpay_settings()
    async with AsyncQPayClient(settings=settings) as client:
        invoice = await client.invoice_create(
            InvoiceCreateSimpleRequest(
                sender_invoice_no=order_id,
                invoice_receiver_code="terminal",
                invoice_description=description,
                amount=Decimal(str(amount)),
                callback_url=f"{CALLBACK_BASE_URL}/qpay-callback?order_id={order_id}",
            )
        )

    INVOICES[order_id] = {
        "invoice_id": invoice.invoice_id,
        "status": "PENDING",
        "psid": psid or order_id,
        "product_index": product_index,
        "email": None,
    }
    log_new_order(order_id, amount, description, customer_name)

    return {
        "invoice_id": invoice.invoice_id,
        "qr_text": invoice.qr_text,
        "qr_image_base64": invoice.qr_image,
        "qpay_short_url": getattr(invoice, "qPay_shortUrl", None),
    }


async def deliver_digital_product(order_id: str) -> None:
    """Called once QPay confirms payment. Sends the Drive link + attempts
    a Messenger file attachment immediately, then asks for an email to
    also send it there."""
    record = INVOICES.get(order_id)
    if not record:
        return

    psid = record.get("psid", order_id)
    product = next((p for p in PRODUCTS if p.index == record.get("product_index")), None)
    if not product:
        logger.warning("No matching product for order_id=%s -- can't deliver", order_id)
        return

    text = f"\U0001F389 Төлбөр төлөгдлөө! {product.description}\n\n"
    if product.drive_link:
        text += f"Татах холбоос: {product.drive_link}"
    await send_meta_message({"id": psid}, {"text": text})

    await send_file_via_messenger(psid, product)

    # Ask for email so we can send a copy there too.
    await send_meta_message({"id": psid}, {"text": ASK_EMAIL_TEXT})
    AWAITING_EMAIL[psid] = order_id


# ---------------------------------------------------------------------------
# 1) META WEBHOOK VERIFICATION
# ---------------------------------------------------------------------------
@app.get("/meta-webhook")
async def verify_meta_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == META_VERIFY_TOKEN:
        return PlainTextResponse(content=params.get("hub.challenge") or "")
    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# 2) META WEBHOOK EVENTS (comments + button clicks + email replies)
# ---------------------------------------------------------------------------
@app.post("/meta-webhook")
async def receive_meta_webhook(request: Request):
    raw_body = await request.body()
    verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256", ""))

    data = json.loads(raw_body)
    logger.info("Incoming webhook payload: %s", json.dumps(data))

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "feed":
                await handle_feed_change(change.get("value", {}))
        for messaging_event in entry.get("messaging", []):
            await handle_messaging_event(messaging_event)

    return {"status": "ok"}


def find_matching_product(comment_text: str) -> Product | None:
    for product in PRODUCTS:
        if product.keywords and any(kw in comment_text for kw in product.keywords):
            return product
    return None


async def handle_feed_change(value: dict) -> None:
    if value.get("item") != "comment" or value.get("verb") != "add":
        return

    comment_text = (value.get("message") or "").lower()
    comment_id = value.get("comment_id")
    if not comment_id:
        return

    product = find_matching_product(comment_text)
    if not product:
        return

    try:
        await like_comment(comment_id)
    except Exception as e:
        logger.warning("Failed to like comment %s: %s", comment_id, e)
    try:
        await reply_to_comment(comment_id, COMMENT_REPLY_TEXT)
    except Exception as e:
        logger.warning("Failed to reply to comment %s: %s", comment_id, e)

    await send_pay_button({"comment_id": comment_id}, product)


async def handle_messaging_event(event: dict) -> None:
    sender_id = event.get("sender", {}).get("id")
    if not sender_id:
        return

    postback = event.get("postback")
    if postback:
        payload = postback.get("payload", "")
        if not payload.startswith("QPAY_PAY_"):
            return
        try:
            product_index = int(payload.replace("QPAY_PAY_", "", 1))
        except ValueError:
            return
        product = next((p for p in PRODUCTS if p.index == product_index), None)
        if not product:
            return

        customer_name = await get_customer_name(sender_id)
        order_id = f"{sender_id}-{product_index}-{uuid.uuid4().hex[:6]}"

        invoice = await create_qpay_invoice(
            order_id=order_id, amount=product.amount, description=product.description,
            customer_name=customer_name, psid=sender_id, product_index=product_index,
        )
        link = invoice.get("qpay_short_url") or invoice.get("qr_text")
        await send_meta_message(
            {"id": sender_id},
            {"text": f"Qpay-ээр төлөх бол энд дарна уу: {link}\nХэрэв алдаа заасан тохиолдолд 1. Дэлгэцний буланд байрлах \u00b0\u00b0\u00b0 дарж 2. Open in external browser гэж дарна уу."},
        )
        return

    # Not a postback -- check if we're waiting on an email reply from this
    # person for a completed order.
    message = event.get("message", {})
    text = (message.get("text") or "").strip()
    if not text or sender_id not in AWAITING_EMAIL:
        return

    order_id = AWAITING_EMAIL[sender_id]
    record = INVOICES.get(order_id)
    if not record:
        del AWAITING_EMAIL[sender_id]
        return

    if not EMAIL_REGEX.match(text):
        await send_meta_message({"id": sender_id}, {"text": EMAIL_INVALID_TEXT})
        return  # keep waiting -- don't clear AWAITING_EMAIL

    product = next((p for p in PRODUCTS if p.index == record.get("product_index")), None)
    if not product:
        del AWAITING_EMAIL[sender_id]
        return

    sent = await send_product_email(text, product)
    record["email"] = text
    update_order_email(order_id, text)
    del AWAITING_EMAIL[sender_id]

    if sent:
        await send_meta_message({"id": sender_id}, {"text": EMAIL_SENT_TEXT})
    else:
        await send_meta_message(
            {"id": sender_id},
            {"text": "Имэйл илгээхэд алдаа гарлаа. Гэхдээ Messenger-т илгээсэн холбоосоор файлаа авах боломжтой."},
        )


# ---------------------------------------------------------------------------
# 3) QPAY CALLBACK
# ---------------------------------------------------------------------------
@app.post("/qpay-callback")
async def qpay_callback(order_id: str):
    record = INVOICES.get(order_id)
    if not record:
        return "SUCCESS"

    settings = get_qpay_settings()
    async with AsyncQPayClient(settings=settings) as client:
        result = await client.payment_check(
            PaymentCheckRequest(
                object_type=ObjectType.invoice,
                object_id=record["invoice_id"],
                offset=Offset(page_number=1, page_limit=100),
            )
        )

    if result.count > 0 and record["status"] != "PAID":
        record["status"] = "PAID"
        mark_order_paid(order_id)
        await deliver_digital_product(order_id)

    return "SUCCESS"


# ---------------------------------------------------------------------------
# 4) MANUAL TESTING ENDPOINTS
# ---------------------------------------------------------------------------
class CreateInvoiceRequest(BaseModel):
    order_id: str | None = None
    amount: float
    description: str


class CreateInvoiceResponse(BaseModel):
    invoice_id: str
    qr_text: str
    qr_image_base64: str
    qpay_short_url: str | None = None


@app.post("/create-invoice", response_model=CreateInvoiceResponse)
async def create_invoice(payload: CreateInvoiceRequest):
    order_id = payload.order_id or f"AUTO-{uuid.uuid4().hex[:12]}"
    invoice = await create_qpay_invoice(order_id, payload.amount, payload.description)
    return CreateInvoiceResponse(**invoice)


@app.get("/payment-status/{order_id}")
async def payment_status(order_id: str):
    record = INVOICES.get(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown order_id")
    return {"order_id": order_id, "status": record["status"]}


@app.post("/test-deliver")
async def test_deliver(psid: str, product_index: int = 1, secret: str = ""):
    """TEST-ONLY: simulates a completed payment and runs the full delivery
    flow (Messenger link + attachment attempt + email prompt) for a given
    PSID, without needing a real QPay payment."""
    if TEST_ENDPOINT_SECRET and secret != TEST_ENDPOINT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing secret")

    product = next((p for p in PRODUCTS if p.index == product_index), None)
    if not product:
        raise HTTPException(status_code=404, detail=f"No product with index {product_index}")

    order_id = f"TEST-{psid}-{product_index}-{uuid.uuid4().hex[:6]}"
    INVOICES[order_id] = {
        "invoice_id": "TEST", "status": "PAID", "psid": psid,
        "product_index": product_index, "email": None,
    }
    await deliver_digital_product(order_id)
    return {"status": "sent", "order_id": order_id}


@app.get("/")
async def health_check():
    return {"status": "ok", "qpay_env": QPAY_ENV, "products_configured": len(PRODUCTS)}


# ---------------------------------------------------------------------------
# 5) PRIVACY POLICY
# ---------------------------------------------------------------------------
PRIVACY_POLICY_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Privacy Policy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
          max-width: 700px; margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.6; }}
  h1 {{ font-size: 1.6em; }}
  h2 {{ font-size: 1.15em; margin-top: 1.6em; }}
  footer {{ margin-top: 3em; color: #666; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p>This policy explains how {BUSINESS_NAME} ("we", "us") handles information
when you interact with our Facebook Page and Messenger bot.</p>

<h2>What we collect</h2>
<p>When you comment on our posts or message our Page, we receive your
Facebook name and a unique Messenger ID (PSID) from Meta. If you choose to
provide it, we also collect your email address, solely so we can send you
a copy of the digital product you purchased. We do not receive your
password or friends list. When you pay for an order, we also process the
order amount and description, and a payment status from our payment
provider, QPay.</p>

<h2>How we use it</h2>
<p>We use this information to respond to your comments and messages,
generate a payment request for orders you initiate, confirm when a
payment has been completed, and deliver the digital product you purchased
via Messenger and/or email.</p>

<h2>Who we share it with</h2>
<p>Order amount and a reference ID are shared with QPay (our payment
processor) solely to generate and verify payment. Your email address, if
provided, is used only to send you your purchase and is passed to our
email delivery provider solely for that purpose. We do not sell or share
your information with advertisers or any other third party.</p>

<h2>How long we keep it</h2>
<p>We retain order, payment, and email records only as long as needed to
fulfill your order and for basic bookkeeping. You may ask us to delete
your data at any time using the contact details below.</p>

<h2>Your choices</h2>
<p>You can stop messaging our Page at any time, and you are never required
to provide your email address to receive your purchase via Messenger. To
request access to or deletion of any data we hold about you, contact us
using the details below.</p>

<h2>Contact</h2>
<p>{"Email: " + CONTACT_EMAIL if CONTACT_EMAIL else "Contact us via Facebook Messenger through our Page."}</p>

<footer>Last updated: this page reflects the current version of our bot's
data handling as described above.</footer>
</body>
</html>
"""


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return PRIVACY_POLICY_HTML

import imaplib
import smtplib
import email
import os
import json
import logging
import time
from email.message import EmailMessage
from email.header import decode_header
from datetime import datetime
import requests  # for Ollama HTTP API (local)
from email.utils import parseaddr

import json
import os
from dotenv import load_dotenv

load_dotenv()

with open("config.json") as f:
    CONFIG = json.load(f)


# Load secrets from .env
CONFIG.update({
    "imap_host": os.getenv("IMAP_HOST"),
    "imap_port": int(os.getenv("IMAP_PORT")),
    "imap_use_ssl": os.getenv("IMAP_USE_SSL", "true").lower() == "true",

    "email_address": os.getenv("EMAIL_ADDRESS"),
    "email_password": os.getenv("EMAIL_PASSWORD"),

    "smtp_host": os.getenv("SMTP_HOST"),
    "smtp_port": int(os.getenv("SMTP_PORT")),
    "smtp_use_tls": os.getenv("SMTP_USE_TLS", "true").lower() == "true",

    "smtp_user": os.getenv("SMTP_USER"),
    "smtp_password": os.getenv("SMTP_PASSWORD"),
})
    
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Persistence: track already-processed mail IDs ───────────────────────────

def load_processed_ids() -> set:
    path = CONFIG["processed_ids_file"]
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids: set):
    with open(CONFIG["processed_ids_file"], "w") as f:
        json.dump(list(ids), f)


# ─── IMAP helpers ────────────────────────────────────────────────────────────

def decode_str(value: str) -> str:
    """Decode encoded email header strings."""
    parts = decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def fetch_emails_with_attachments(processed_ids: set) -> list[dict]:
    """
    Connect via IMAP, fetch UNSEEN mails that have attachments,
    skip blocked domains, return list of mail dicts.
    """
    results = []

    log.info("Connecting to IMAP server %s…", CONFIG["imap_host"])
    if CONFIG["imap_use_ssl"]:
        conn = imaplib.IMAP4_SSL(CONFIG["imap_host"], CONFIG["imap_port"])
    else:
        conn = imaplib.IMAP4(CONFIG["imap_host"], CONFIG["imap_port"])

    conn.login(CONFIG["email_address"], CONFIG["email_password"])
    conn.select(CONFIG["mailbox"])

    # Search for UNSEEN mails only
    status, data = conn.search(None, "UNSEEN")
    if status != "OK" or not data[0]:
        log.info("No new unseen emails.")
        conn.logout()
        return results

    # Get latest 10 unread emails
    mail_ids = data[0].split()[::-1][:10]
    log.info("Found %d unseen email(s).", len(mail_ids))

    for mid in mail_ids:
        mid_str = mid.decode()
        if mid_str in processed_ids:
            continue

        status, msg_data = conn.fetch(mid, "(RFC822)")
        if status != "OK":
            continue

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # ── Sender check ──────────────────────────────────────────────────
        from_raw = msg.get("From", "")
        from_decoded = decode_str(from_raw)

        sender_name = parseaddr(from_decoded)[0]

        sender_email = email.utils.parseaddr(from_decoded)[1].lower()
        sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""

        allowed_domains = [d.lower() for d in CONFIG["allowed_domains"]]

        if sender_domain not in allowed_domains:
            log.info(
                "Skipping mail from unauthorized domain: %s",
                sender_email,
            )
            processed_ids.add(mid_str)
            continue

        # ── Attachment check ──────────────────────────────────────────────
        attachments = []
        body_text = ""

        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            content_type = part.get_content_type()

            if "attachment" in content_disposition:
                filename = part.get_filename()
                if filename:
                    filename = decode_str(filename)
                    attachments.append({
                        "filename": filename,
                        "content_type": content_type,
                        "size": len(part.get_payload(decode=True) or b""),
                    })
            elif content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    body_text += payload.decode("utf-8", errors="replace")

        if not attachments:
            log.info("Mail from %s has no attachments – skipping.", sender_email)
            continue

        subject = decode_str(msg.get("Subject", "(no subject)"))
        date = msg.get("Date", "")

        log.info(
            "Processing mail: FROM=%s | SUBJECT=%s | ATTACHMENTS=%d",
            sender_email, subject, len(attachments),
        )

        results.append({
            "id": mid_str,
            "from": sender_email,
            "from_raw": from_decoded,
            "from_name": sender_name,
            "subject": subject,
            "date": date,
            "body": body_text.strip(),
            "attachments": attachments,
        })

        if CONFIG["mark_seen"]:
            conn.store(mid, "+FLAGS", "\\Seen")

        processed_ids.add(mid_str)

    conn.logout()
    return results


# ─── Local LLM (Ollama) ──────────────────────────────────────────────────────
def generate_acknowledgement(mail: dict) -> str:
    """Generate a static acknowledgement email."""

    attachment_list = ", ".join(
        attachment["filename"] for attachment in mail["attachments"]
    )

    return f"""Dear {mail["from_name"]},

Greetings from {CONFIG["company_name"]}.

Thank you for your interest in {CONFIG["company_name"]}.

We have successfully received your profile along with the following attachment(s):

{attachment_list}

You are invited to attend the walk-in interview for the {CONFIG["internship"]}.

Interview Details

Date: {CONFIG["walkin_date"]}

Time: {CONFIG["walkin_time"]}

Location:
{CONFIG["walkin_location"]}

Please carry the following documents while attending the interview:

• Aadhaar Card

• Resume (1 printed copy)

If you have any questions, feel free to reply to this email.

We look forward to meeting you.

Best Regards,

HR Team
{CONFIG["company_name"]}
"""

# ─── SMTP: send acknowledgement ──────────────────────────────────────────────

def send_acknowledgement(mail: dict, ack_body: str):
    """Send acknowledgement reply via SMTP."""
    msg = EmailMessage()
    msg["From"] = CONFIG["email_address"]
    msg["To"] = mail["from"]
    msg["Subject"] = CONFIG["reply_subject"].format(
    internship=CONFIG["internship"]
    )
    msg["In-Reply-To"] = mail.get("message_id", "")
    msg.set_content(ack_body)

    log.info("Sending acknowledgement to %s…", mail["from"])

    try:
        if CONFIG["smtp_use_tls"]:
            with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
                server.ehlo()
                server.starttls()
                server.login(CONFIG["smtp_user"], CONFIG["smtp_password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
                server.send_message(msg)

        log.info("Acknowledgement sent to %s ", mail["from"])

    except Exception as exc:
        log.error("Failed to send acknowledgement to %s: %s", mail["from"], exc)


# ─── Main loop ───────────────────────────────────────────────────────────────

def run():
    processed_ids = load_processed_ids()
    log.info(
    "Mail Agent started. Allowed Domains: %s",
    CONFIG["allowed_domains"]
)

    while True:
        try:
            mails = fetch_emails_with_attachments(processed_ids)

            for mail in mails:
                ack_body = generate_acknowledgement(mail)
                log.info("--- Acknowledgement Draft ---\n%s\n---", ack_body)
                send_acknowledgement(mail, ack_body)

            save_processed_ids(processed_ids)

        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)

        if CONFIG["poll_interval_seconds"] <= 0:
            log.info("Single-run mode. Exiting.")
            break

        log.info("Sleeping %ds before next check…", CONFIG["poll_interval_seconds"])
        time.sleep(CONFIG["poll_interval_seconds"])


if __name__ == "__main__":
    run()

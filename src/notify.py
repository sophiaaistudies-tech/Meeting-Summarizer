"""
Usage notifications.

Sends a short plain-text email to ADMIN_EMAIL every time someone runs a summary,
so you know the page is being used, by whom, and whether it worked.

Notifications never interrupt the job: if sending one fails, it is logged and the
summary still goes out.

Set ADMIN_EMAIL in .env. Leave it empty to switch notifications off.
"""

import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()


def notify_admin(subject: str, fields: dict) -> bool:
    """
    Send one notification. Returns True if it went out, False otherwise.

    fields is rendered as aligned "label: value" lines, in the order given.
    """
    if not ADMIN_EMAIL:
        return False
    if not (EMAIL_SENDER and EMAIL_PASSWORD):
        print("[Notify] EMAIL_SENDER or EMAIL_PASSWORD missing, skipping notification.")
        return False

    width = max((len(k) for k in fields), default=0)
    body = "\n".join(f"{k.ljust(width)}  {v}" for k, v in fields.items())
    body += f"\n\nSent {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ADMIN_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[Notify] Sent to {ADMIN_EMAIL}")
        return True
    except Exception as e:
        # A failed notification must never fail the summary itself
        print(f"[Notify] Failed: {type(e).__name__}: {e}")
        return False

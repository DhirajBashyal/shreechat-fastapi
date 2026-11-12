"""Outgoing email.

With SMTP_HOST set, mail is sent over SMTP. Without it (local development),
the email is written to the log instead so links can be copied from there.
Outside local development an unset SMTP_HOST is an error, so secrets such as
reset links are never logged in production.
"""

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger()


def _send_smtp(msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST:
        if settings.is_local:
            logger.info(
                "Email (dev mode, not sent)",
                extra={"to": to, "subject": subject, "body": body},
            )
            return
        logger.error("SMTP_HOST is not configured; email not sent")
        return

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        await asyncio.to_thread(_send_smtp, msg)
    except (OSError, smtplib.SMTPException) as e:
        logger.error("Failed to send email", extra={"error": str(e)})

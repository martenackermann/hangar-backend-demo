"""Mailer implementations: SMTP (default → Mailpit), memory (tests), console (logs).

In demo mode every mailer refuses recipients outside ``example.com`` (SPEC §12).
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import make_msgid

import aiosmtplib
import structlog

from app.core.config import get_settings
from app.core.gateways.protocols import MailerError, OutgoingMail, SentMail

log = structlog.get_logger(__name__)

DEMO_DOMAIN = "example.com"


def check_recipients(mail: OutgoingMail) -> list[str]:
    recipients = [*mail.to, *mail.cc]
    if not recipients:
        raise MailerError("The mail has no recipients.")
    if get_settings().demo_mode:
        foreign = [r for r in recipients if not r.lower().endswith("@" + DEMO_DOMAIN)]
        if foreign:
            raise MailerError(
                f"Demo mode only sends to @{DEMO_DOMAIN} addresses: {', '.join(foreign)}"
            )
    return recipients


def build_message(mail: OutgoingMail, message_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = mail.sender
    message["To"] = ", ".join(mail.to)
    if mail.cc:
        message["Cc"] = ", ".join(mail.cc)
    if mail.reply_to:
        message["Reply-To"] = mail.reply_to
    message["Subject"] = mail.subject
    message["Message-ID"] = message_id
    for name, value in mail.headers.items():
        message[name] = value
    message.set_content(mail.text)
    message.add_alternative(mail.html, subtype="html")
    for attachment in mail.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


class SmtpMailer:
    async def send(self, mail: OutgoingMail) -> SentMail:
        recipients = check_recipients(mail)
        settings = get_settings()
        message_id = make_msgid(domain="hangar.example.com")
        message = build_message(mail, message_id)
        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password.get_secret_value()
                if settings.smtp_password
                else None,
                start_tls=settings.smtp_starttls,
                timeout=15,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise MailerError(f"SMTP delivery failed: {exc}") from exc
        return SentMail(message_id=message_id, accepted=recipients)


class MemoryMailer:
    """Collects mails in :attr:`outbox` (tests, demos without SMTP)."""

    def __init__(self) -> None:
        self.outbox: list[OutgoingMail] = []

    async def send(self, mail: OutgoingMail) -> SentMail:
        recipients = check_recipients(mail)
        self.outbox.append(mail)
        return SentMail(message_id=make_msgid(domain="hangar.example.com"), accepted=recipients)


class ConsoleMailer:
    async def send(self, mail: OutgoingMail) -> SentMail:
        recipients = check_recipients(mail)
        log.info("mail_console", to=mail.to, cc=mail.cc, subject=mail.subject, headers=mail.headers)
        return SentMail(message_id=make_msgid(domain="hangar.example.com"), accepted=recipients)

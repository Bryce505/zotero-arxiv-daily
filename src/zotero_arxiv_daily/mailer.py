"""Deliver the weekly digest to the team.

Everyone goes in Bcc so recipients cannot see each other's addresses, and the
attachment set is capped: a dozen open-access PDFs will blow past an SMTP
server's message limit, so only what fits is attached and the rest stays in
the repository archive.
"""

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from loguru import logger

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_subtype: str


def select_attachments(paths: list[str], max_total_bytes: int = MAX_ATTACHMENT_BYTES) -> list[Attachment]:
    """Read *paths* in order, keeping what fits under the ceiling."""
    chosen: list[Attachment] = []
    used = 0
    for path in paths:
        if not os.path.exists(path):
            logger.warning(f"Attachment {path} is missing; skipping")
            continue
        size = os.path.getsize(path)
        if used + size > max_total_bytes:
            logger.info(
                f"Skipping attachment {os.path.basename(path)} ({size} bytes): would exceed the size ceiling"
            )
            continue
        with open(path, "rb") as handle:
            content = handle.read()
        subtype = os.path.splitext(path)[1].lstrip(".").lower() or "octet-stream"
        chosen.append(Attachment(filename=os.path.basename(path), content=content, mime_subtype=subtype))
        used += size
    return chosen


def _media_type_for(subtype: str) -> tuple[str, str]:
    if subtype == "pdf":
        return "application", "pdf"
    if subtype in {"html", "htm"}:
        return "text", "html"
    if subtype == "md":
        return "text", "markdown"
    return "application", "octet-stream"


def build_message(
    subject: str,
    html: str,
    sender: str,
    recipients: list[str],
    attachments: list[Attachment],
) -> EmailMessage:
    """Build the digest message with every recipient in Bcc.

    ``smtplib.send_message`` strips Bcc before transmitting, so no recipient
    ever sees the others.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender  # the sender is the only visible recipient
    msg["Bcc"] = ", ".join(recipients)
    msg.set_content("此邮件为 HTML 格式，请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(html, subtype="html")

    for attachment in attachments:
        maintype, subtype = _media_type_for(attachment.mime_subtype)
        msg.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return msg


def send_digest(config, subject: str, html: str, attachments: list[Attachment]) -> None:
    """Send the digest over SMTP, preferring STARTTLS and falling back to SSL."""
    settings = config.email
    recipients = [str(r).strip() for r in (settings.get("recipients") or []) if str(r).strip()]
    if not recipients:
        raise ValueError("email.recipients is empty: no recipients to send the digest to")

    sender = settings.sender
    msg = build_message(subject, html, sender, recipients, attachments)

    try:
        server = smtplib.SMTP(settings.smtp_server, settings.smtp_port)
        server.starttls()
    except Exception as exc:  # noqa: BLE001 - many providers are SSL-only on 465
        logger.debug(f"STARTTLS unavailable ({exc}); falling back to SSL")
        server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port)

    server.login(sender, settings.sender_password)
    server.send_message(msg, from_addr=sender, to_addrs=recipients)
    server.quit()
    logger.info(f"Digest sent to {len(recipients)} recipients with {len(attachments)} attachments")

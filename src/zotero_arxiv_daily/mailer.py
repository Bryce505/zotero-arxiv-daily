"""Deliver the weekly digest to the team.

Everyone goes in Bcc so recipients cannot see each other's addresses, and the
attachment set is capped: a dozen open-access PDFs will blow past an SMTP
server's message limit, so only what fits is attached and the rest stays in
the repository archive.
"""

import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from loguru import logger

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
SMTP_TIMEOUT_SECONDS = 60
_ADDRESS_SEPARATOR_RE = re.compile(r"[,;\s]+")


def _safe_get(config, key: str):
    """Read *key*, treating an unresolvable interpolation as absent.

    ``receiver`` interpolates a secret the weekly workflow does not export,
    and an unset environment variable must not abort delivery at the last
    step.
    """
    try:
        return config.get(key)
    except Exception as exc:  # noqa: BLE001 - OmegaConf interpolation failure
        logger.debug(f"email.{key} could not be resolved ({exc}); treating it as unset")
        return None


def resolve_recipients(email_config) -> list[str]:
    """Normalise the configured recipients into a list of addresses.

    Accepts a YAML list or a single delimited string, because a GitHub secret
    can only hold a string.  Falls back to the single ``receiver`` so an
    existing daily-digest configuration keeps working untouched.
    """
    raw = _safe_get(email_config, "recipients")
    if isinstance(raw, str):
        candidates = _ADDRESS_SEPARATOR_RE.split(raw)
    elif raw:
        candidates = [str(r) for r in raw]
    else:
        candidates = []

    recipients = [c.strip() for c in candidates if c and c.strip()]
    if not recipients:
        fallback = _safe_get(email_config, "receiver")
        if fallback:
            recipients = [str(fallback).strip()]
    return recipients


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
    recipients = resolve_recipients(settings)
    if not recipients:
        raise ValueError("email.recipients is empty: no recipients to send the digest to")

    sender = settings.sender
    msg = build_message(subject, html, sender, recipients, attachments)

    # Without a timeout an SSL-only port (465 is the shipped default) leaves
    # the plaintext greeting read blocking forever instead of falling through.
    server = None
    try:
        server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
        server.starttls()
    except Exception as exc:  # noqa: BLE001 - many providers are SSL-only on 465
        logger.debug(f"STARTTLS unavailable ({exc}); falling back to SSL")
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001 - the socket is already unusable
                pass
        server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)

    server.login(sender, settings.sender_password)
    server.send_message(msg, from_addr=sender, to_addrs=recipients)
    server.quit()
    logger.info(f"Digest sent to {len(recipients)} recipients with {len(attachments)} attachments")

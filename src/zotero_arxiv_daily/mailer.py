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
# Short on purpose: an SSL-only port (465 is the shipped default) has to time
# out on the plaintext greeting for the SSL fallback below to be reached.
SMTP_CONNECT_TIMEOUT_SECONDS = 60
# The same 60s applied to the DATA write is what killed run 35361805170: with
# ~19MB of attachments queued the provider stopped draining the socket,
# sendall() timed out, and smtplib re-raised it as "Server not connected".
# A message body is not a handshake; it gets minutes.
SMTP_DATA_TIMEOUT_SECONDS = 600
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


def _encoded_size(raw_bytes: int) -> int:
    """Base64 size of *raw_bytes*: SMTP limits apply to the encoded message."""
    return (raw_bytes + 2) // 3 * 4


def select_attachments(paths: list[str], max_total_bytes: int = MAX_ATTACHMENT_BYTES) -> list[Attachment]:
    """Read *paths* in order, keeping what fits under the ceiling.

    The ceiling is measured on the base64-encoded size, roughly 4/3 of the
    bytes on disk, because that is what the provider counts.
    """
    chosen: list[Attachment] = []
    used = 0
    for path in paths:
        if not os.path.exists(path):
            logger.warning(f"Attachment {path} is missing; skipping")
            continue
        size = _encoded_size(os.path.getsize(path))
        if used + size > max_total_bytes:
            logger.info(
                f"Skipping attachment {os.path.basename(path)} ({size} bytes encoded): "
                "would exceed the size ceiling"
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


def _connect(settings):
    """Open an authenticated session, preferring STARTTLS and falling back to SSL."""
    server = None
    try:
        server = smtplib.SMTP(
            settings.smtp_server, settings.smtp_port, timeout=SMTP_CONNECT_TIMEOUT_SECONDS
        )
        server.starttls()
    except Exception as exc:  # noqa: BLE001 - many providers are SSL-only on 465
        logger.debug(f"STARTTLS unavailable ({exc}); falling back to SSL")
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001 - the socket is already unusable
                pass
        server = smtplib.SMTP_SSL(
            settings.smtp_server, settings.smtp_port, timeout=SMTP_CONNECT_TIMEOUT_SECONDS
        )

    # Connected: the greeting-timeout reason is spent, so give the transfer room.
    sock = getattr(server, "sock", None)
    if sock is not None:
        sock.settimeout(SMTP_DATA_TIMEOUT_SECONDS)
    return server


def _deliver(settings, sender: str, recipients: list[str], msg: EmailMessage) -> None:
    server = _connect(settings)
    try:
        server.login(sender, settings.sender_password)
        server.send_message(msg, from_addr=sender, to_addrs=recipients)
    except Exception:
        try:
            server.close()
        except Exception:  # noqa: BLE001 - the socket is already unusable
            pass
        raise
    # The message is accepted by now; a provider that drops the connection
    # instead of answering QUIT has still delivered it, and treating that as a
    # failure would send the whole digest a second time.
    try:
        server.quit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"QUIT after a successful send failed ({exc}); ignoring")


def send_digest(config, subject: str, html: str, attachments: list[Attachment]) -> None:
    """Send the digest over SMTP, dropping the attachments rather than the digest.

    The attachments are a convenience — every PDF is also archived in the
    repository and linked from the report — so a message too large for the
    provider to swallow must not cost the week's email.
    """
    settings = config.email
    recipients = resolve_recipients(settings)
    if not recipients:
        raise ValueError("email.recipients is empty: no recipients to send the digest to")

    sender = settings.sender
    try:
        _deliver(settings, sender, recipients, build_message(subject, html, sender, recipients, attachments))
    except (smtplib.SMTPException, OSError) as exc:
        if not attachments:
            raise
        logger.warning(
            f"Delivery with {len(attachments)} attachments failed ({exc!r}); "
            "retrying without them"
        )
        _deliver(settings, sender, recipients, build_message(subject, html, sender, recipients, []))
        logger.info(f"Digest sent to {len(recipients)} recipients with no attachments")
        return
    logger.info(f"Digest sent to {len(recipients)} recipients with {len(attachments)} attachments")

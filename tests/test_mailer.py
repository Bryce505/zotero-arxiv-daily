"""Multi-recipient delivery with Bcc and size-guarded attachments."""

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.mailer import (
    Attachment,
    build_message,
    select_attachments,
    send_digest,
)

RECIPIENTS = ["a@example.org", "b@example.org", "c@example.org"]


def test_no_recipient_appears_in_a_visible_header():
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, [])
    assert msg["To"] == "me@example.org"
    for header in ("To", "Cc"):
        value = msg[header] or ""
        assert not any(r in value for r in RECIPIENTS)


def test_every_recipient_is_carried_in_bcc():
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, [])
    assert msg.get_all("Bcc") == [", ".join(RECIPIENTS)]


def test_the_subject_and_html_body_survive():
    msg = build_message(
        "CMC 文献周报 2026-08-W3（共 18 篇）", "<p>正文</p>", "me@example.org", RECIPIENTS, []
    )
    assert "2026-08-W3" in msg["Subject"]
    assert "正文" in msg.get_body(preferencelist=("html",)).get_content()


def test_a_plain_text_alternative_is_present_for_text_only_clients():
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, [])
    assert msg.get_body(preferencelist=("plain",)) is not None


def test_attachments_are_attached_with_their_filenames():
    attachments = [Attachment(filename="report.html", content=b"<html></html>", mime_subtype="html")]
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, attachments)
    assert [part.get_filename() for part in msg.iter_attachments()] == ["report.html"]


def test_a_pdf_attachment_keeps_its_media_type():
    attachments = [Attachment(filename="p.pdf", content=b"%PDF-1.7", mime_subtype="pdf")]
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, attachments)
    assert next(msg.iter_attachments()).get_content_type() == "application/pdf"


def test_select_attachments_stops_at_the_size_ceiling(tmp_path):
    paths = []
    for i in range(4):
        path = tmp_path / f"{i}.pdf"
        path.write_bytes(b"x" * 900)  # 1200 bytes once base64-encoded
        paths.append(str(path))
    assert len(select_attachments(paths, max_total_bytes=2500)) == 2


def test_select_attachments_keeps_the_given_order(tmp_path):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"a" * 10)
    second.write_bytes(b"b" * 10)
    chosen = select_attachments([str(first), str(second)], max_total_bytes=10_000)
    assert [c.filename for c in chosen] == ["first.pdf", "second.pdf"]


def test_select_attachments_skips_a_single_oversized_file(tmp_path):
    big, small = tmp_path / "big.pdf", tmp_path / "small.pdf"
    big.write_bytes(b"x" * 5000)
    small.write_bytes(b"y" * 100)
    chosen = select_attachments([str(big), str(small)], max_total_bytes=1000)
    assert [c.filename for c in chosen] == ["small.pdf"]


def test_select_attachments_ignores_missing_files():
    assert select_attachments(["/nonexistent/a.pdf"], max_total_bytes=10_000) == []


def make_config(recipients=RECIPIENTS):
    return OmegaConf.create(
        {
            "email": {
                "sender": "me@example.org",
                "sender_password": "pw",
                "smtp_server": "smtp.example.org",
                "smtp_port": 587,
                "recipients": recipients,
            }
        }
    )


def test_send_digest_delivers_to_every_recipient(monkeypatch):
    sent = {}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            sent["server"] = (server, port)

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["to_addrs"] = to_addrs

        def quit(self):
            sent["quit"] = True

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(), "S", "<p>hi</p>", [])
    assert sorted(sent["to_addrs"]) == sorted(RECIPIENTS)
    assert sent["quit"] is True


def test_send_digest_falls_back_to_ssl_when_starttls_is_unavailable(monkeypatch):
    used = {}

    class NoTLS:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            raise OSError("STARTTLS not offered")

    class StubSSL:
        def __init__(self, server, port, timeout=None):
            used["ssl"] = True

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            used["to_addrs"] = to_addrs

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", NoTLS)
    monkeypatch.setattr(smtplib, "SMTP_SSL", StubSSL)
    send_digest(make_config(), "S", "<p>hi</p>", [])
    assert used["ssl"] is True
    assert sorted(used["to_addrs"]) == sorted(RECIPIENTS)


def test_send_digest_refuses_an_empty_recipient_list():
    with pytest.raises(ValueError, match="no recipients"):
        send_digest(make_config(recipients=[]), "S", "<p>hi</p>", [])


def test_send_digest_ignores_blank_recipient_entries(monkeypatch):
    sent = {}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["to_addrs"] = to_addrs

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(recipients=["a@example.org", "  ", ""]), "S", "<p>hi</p>", [])
    assert sent["to_addrs"] == ["a@example.org"]


def test_a_comma_separated_string_is_accepted_as_the_recipient_list(monkeypatch):
    """A GitHub secret holds a string, not a YAML list."""
    sent = {}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["to_addrs"] = to_addrs

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    config = OmegaConf.create(
        {
            "email": {
                "sender": "me@example.org",
                "sender_password": "pw",
                "smtp_server": "s",
                "smtp_port": 587,
                "recipients": "a@example.org, b@example.org;c@example.org",
            }
        }
    )
    send_digest(config, "S", "<p>hi</p>", [])
    assert sent["to_addrs"] == ["a@example.org", "b@example.org", "c@example.org"]


def test_recipients_fall_back_to_the_single_receiver(monkeypatch):
    """An existing single-recipient setup keeps working untouched."""
    sent = {}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["to_addrs"] = to_addrs

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    config = OmegaConf.create(
        {
            "email": {
                "sender": "me@example.org",
                "sender_password": "pw",
                "smtp_server": "s",
                "smtp_port": 587,
                "recipients": None,
                "receiver": "solo@example.org",
            }
        }
    )
    send_digest(config, "S", "<p>hi</p>", [])
    assert sent["to_addrs"] == ["solo@example.org"]


def test_the_configured_recipients_are_declared_in_base_config():
    """send_digest reads email.recipients, so base.yaml must document it."""
    from hydra import compose, initialize_config_dir
    from hydra.core.global_hydra import GlobalHydra
    from pathlib import Path

    GlobalHydra.instance().clear()
    config_dir = str(Path(__file__).resolve().parent.parent / "config")
    with initialize_config_dir(config_dir=config_dir, version_base=None):
        cfg = compose(
            config_name="default",
            overrides=[
                "zotero.user_id=1",
                "zotero.api_key=k",
                "email.sender=a@b.c",
                "email.receiver=a@b.c",
                "email.smtp_server=s",
                "email.smtp_port=465",
                "email.sender_password=p",
                "llm.api.key=k",
                "llm.api.base_url=u",
                "llm.generation_kwargs.model=m",
                "executor.source=[arxiv]",
            ],
        )
    assert "recipients" in cfg.email


def test_the_smtp_connection_carries_a_timeout(monkeypatch):
    """Port 465 is SSL-only; an untimed SMTP greeting read hangs forever."""
    seen = {}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            seen["timeout"] = timeout

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            pass

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(), "S", "<p>hi</p>", [])
    assert seen["timeout"] is not None and seen["timeout"] > 0


def test_a_failed_starttls_connection_is_closed_before_falling_back(monkeypatch):
    """Leaving the half-open socket behind leaks a connection per run."""
    closed = {"value": False}

    class NoTLS:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            raise OSError("STARTTLS not offered")

        def close(self):
            closed["value"] = True

    class StubSSL:
        def __init__(self, server, port, timeout=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            pass

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", NoTLS)
    monkeypatch.setattr(smtplib, "SMTP_SSL", StubSSL)
    send_digest(make_config(), "S", "<p>hi</p>", [])
    assert closed["value"] is True


def test_the_attachment_ceiling_accounts_for_base64_inflation(tmp_path):
    """SMTP limits apply to the encoded message, which is ~4/3 of the raw bytes."""
    path = tmp_path / "big.pdf"
    path.write_bytes(b"x" * 900)
    # 900 raw bytes encode to ~1200; a 1000-byte ceiling must reject it.
    assert select_attachments([str(path)], max_total_bytes=1000) == []


def test_an_attachment_that_still_fits_once_encoded_is_kept(tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(b"x" * 600)
    assert [a.filename for a in select_attachments([str(path)], max_total_bytes=1000)] == ["ok.pdf"]


def test_the_data_write_gets_a_longer_timeout_than_the_greeting(monkeypatch):
    """Run 35361805170: a ~19MB body stalled past the 60s connect timeout.

    smtplib re-raises a timed-out sock.sendall() as SMTPServerDisconnected
    ('Server not connected'), so the 60s that exists only to make an SSL-only
    port fall through must not still be in force during the DATA write.
    """
    import smtplib

    from zotero_arxiv_daily.mailer import SMTP_CONNECT_TIMEOUT_SECONDS, SMTP_DATA_TIMEOUT_SECONDS

    timeouts = []

    class StubSock:
        def settimeout(self, value):
            timeouts.append(value)

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            timeouts.append(timeout)
            self.sock = StubSock()

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            timeouts.append("send")

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(), "S", "<p>hi</p>", [])
    assert timeouts == [SMTP_CONNECT_TIMEOUT_SECONDS, SMTP_DATA_TIMEOUT_SECONDS, "send"]
    assert SMTP_DATA_TIMEOUT_SECONDS > SMTP_CONNECT_TIMEOUT_SECONDS


def test_a_message_the_provider_will_not_swallow_is_retried_without_attachments(monkeypatch):
    """The PDFs are also archived in the repo; the digest itself is not."""
    import smtplib

    from zotero_arxiv_daily.mailer import Attachment

    sent = []

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            payload = msg.get_payload()
            attached = [p for p in payload if p.get_filename()] if isinstance(payload, list) else []
            sent.append(len(attached))
            if attached:
                raise smtplib.SMTPServerDisconnected("Server not connected")

        def close(self):
            pass

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(), "S", "<p>hi</p>", [Attachment("a.pdf", b"x", "pdf")])
    assert sent == [1, 0]


def test_a_bare_message_that_fails_still_raises(monkeypatch):
    """Without attachments to drop there is no second chance to hide."""
    import smtplib

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            raise smtplib.SMTPServerDisconnected("Server not connected")

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        send_digest(make_config(), "S", "<p>hi</p>", [])


def test_a_dropped_connection_at_quit_does_not_resend_the_digest(monkeypatch):
    """163/QQ hang up instead of answering QUIT; the mail has already gone."""
    import smtplib

    from zotero_arxiv_daily.mailer import Attachment

    sends = {"count": 0}

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sends["count"] += 1

        def close(self):
            pass

        def quit(self):
            raise smtplib.SMTPServerDisconnected("Server not connected")

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    send_digest(make_config(), "S", "<p>hi</p>", [Attachment("a.pdf", b"x", "pdf")])
    assert sends["count"] == 1

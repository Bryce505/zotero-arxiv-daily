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
        path.write_bytes(b"x" * 1000)
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
        def __init__(self, server, port):
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
        def __init__(self, server, port):
            pass

        def starttls(self):
            raise OSError("STARTTLS not offered")

    class StubSSL:
        def __init__(self, server, port):
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
        def __init__(self, server, port):
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

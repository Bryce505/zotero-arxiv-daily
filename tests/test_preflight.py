"""Preflight: prove the environment works before spending a full run on it.

The weekly pipeline does its expensive work — Zotero, clustering, four
searches, full-text, extraction — before it ever touches SMTP. A credential
or connectivity problem therefore surfaces at the very end, after twenty-odd
minutes. Preflight probes every boundary cheaply and reports a verdict.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.preflight import (
    CheckResult,
    check_llm,
    check_recipients,
    check_smtp,
    check_sources,
    check_zotero,
    format_report,
    run_preflight,
)
from zotero_arxiv_daily.protocol import CorpusPaper


def make_config(**overrides):
    base = {
        "zotero": {"user_id": "1", "api_key": "k", "include_path": ["文献", "文献/**"], "ignore_path": None},
        "email": {
            "sender": "me@corp.com",
            "sender_password": "pw",
            "smtp_server": "smtp.example.org",
            "smtp_port": 587,
            "recipients": "a@corp.com, b@corp.com",
        },
        "llm": {
            "api": {"key": "sk", "base_url": "https://api.example.org"},
            "generation_kwargs": {"model": "deepseek-v4-flash"},
            "language": "中文",
        },
        "search": {"sources": ["pubmed", "crossref"]},
        "source": {"pubmed": {}, "crossref": {}},
    }
    config = OmegaConf.create(base)
    for path, value in overrides.items():
        OmegaConf.update(config, path, value, merge=True)
    return config


def corpus(n=5, path="文献/表征"):
    return [
        CorpusPaper(title=f"P{i}", abstract="a", added_date=datetime(2026, 1, 1), paths=[path])
        for i in range(n)
    ]


# --------------------------------------------------------------------- Zotero

def test_zotero_check_reports_the_matched_corpus_size(monkeypatch):
    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight._fetch_corpus", lambda config: (corpus(40), corpus(37))
    )
    result = check_zotero(make_config())
    assert result.ok
    assert "37" in result.detail and "40" in result.detail


def test_zotero_check_fails_when_the_filter_matches_nothing(monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.preflight._fetch_corpus", lambda config: (corpus(40), []))
    result = check_zotero(make_config())
    assert not result.ok
    assert "include_path" in result.detail


def test_zotero_check_warns_when_the_filter_drops_most_of_the_library(monkeypatch):
    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight._fetch_corpus", lambda config: (corpus(100), corpus(5))
    )
    result = check_zotero(make_config())
    assert result.ok
    assert result.warning


def test_zotero_check_reports_a_credential_failure(monkeypatch):
    def boom(config):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr("zotero_arxiv_daily.preflight._fetch_corpus", boom)
    result = check_zotero(make_config())
    assert not result.ok
    assert "403" in result.detail


# ------------------------------------------------------------------------ LLM

def test_llm_check_passes_on_a_reply(monkeypatch):
    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.OpenAI",
        lambda **kw: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **k: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                    )
                )
            )
        ),
    )
    result = check_llm(make_config())
    assert result.ok
    assert "deepseek-v4-flash" in result.detail


def test_llm_check_reports_the_failure(monkeypatch):
    def boom(**kw):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.OpenAI",
        lambda **kw: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=boom))
        ),
    )
    result = check_llm(make_config())
    assert not result.ok
    assert "401" in result.detail


def test_llm_check_flags_an_english_default(monkeypatch):
    """The most common misconfiguration: the digest comes back in English."""
    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.OpenAI",
        lambda **kw: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **k: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                    )
                )
            )
        ),
    )
    result = check_llm(make_config(**{"llm.language": "English"}))
    assert result.warning
    assert "language" in result.detail


# -------------------------------------------------------------------- sources

def test_each_configured_source_is_probed(monkeypatch):
    probed = []

    class StubRetriever:
        name = "stub"

        def __init__(self, config):
            pass

        def search(self, query, start, end, limit):
            probed.append((query, limit))
            return [object()]

    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.get_query_retriever_cls", lambda name: StubRetriever
    )
    results = check_sources(make_config())
    assert [r.name for r in results] == ["pubmed", "crossref"]
    assert all(r.ok for r in results)
    assert all(limit <= 3 for _, limit in probed), "the probe must stay cheap"


def test_a_source_returning_nothing_is_a_warning_not_a_failure(monkeypatch):
    class EmptyRetriever:
        name = "stub"

        def __init__(self, config):
            pass

        def search(self, query, start, end, limit):
            return []

    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.get_query_retriever_cls", lambda name: EmptyRetriever
    )
    results = check_sources(make_config())
    assert all(r.ok for r in results)
    assert all(r.warning for r in results)


def test_an_unreachable_source_fails(monkeypatch):
    class BrokenRetriever:
        name = "stub"

        def __init__(self, config):
            pass

        def search(self, query, start, end, limit):
            raise ConnectionError("network unreachable")

    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.get_query_retriever_cls", lambda name: BrokenRetriever
    )
    results = check_sources(make_config())
    assert not any(r.ok for r in results)


# ----------------------------------------------------------------------- SMTP

def test_smtp_check_logs_in_without_sending(monkeypatch):
    actions = []

    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            actions.append("connect")

        def starttls(self):
            actions.append("starttls")

        def login(self, user, password):
            actions.append("login")

        def send_message(self, *a, **k):  # pragma: no cover
            raise AssertionError("preflight must never send mail")

        def quit(self):
            actions.append("quit")

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    result = check_smtp(make_config())
    assert result.ok
    assert actions == ["connect", "starttls", "login", "quit"]


def test_smtp_check_reports_a_rejected_login(monkeypatch):
    class StubSMTP:
        def __init__(self, server, port, timeout=None):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            raise RuntimeError("535 authentication failed")

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    result = check_smtp(make_config())
    assert not result.ok
    assert "535" in result.detail


# ----------------------------------------------------------------- recipients

def test_recipient_check_lists_them():
    result = check_recipients(make_config())
    assert result.ok
    assert "2" in result.detail


def test_recipient_check_fails_when_none_resolve():
    config = make_config()
    config.email.recipients = None
    result = check_recipients(config)
    assert not result.ok
    assert "RECIPIENTS" in result.detail


# ------------------------------------------------------------------- reporting

def test_the_report_marks_pass_fail_and_warn():
    results = [
        CheckResult(name="zotero", ok=True, detail="111 papers"),
        CheckResult(name="llm", ok=True, detail="slow", warning=True),
        CheckResult(name="smtp", ok=False, detail="535 rejected"),
    ]
    report = format_report(results)
    assert "zotero" in report and "llm" in report and "smtp" in report
    assert "535 rejected" in report


def test_the_report_ends_with_a_verdict():
    passing = format_report([CheckResult(name="a", ok=True, detail="d")])
    failing = format_report([CheckResult(name="a", ok=False, detail="d")])
    assert "PASS" in passing.upper()
    assert "FAIL" in failing.upper()


def test_run_preflight_returns_false_when_any_check_fails(monkeypatch):
    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.check_zotero",
        lambda c: CheckResult(name="zotero", ok=False, detail="nope"),
    )
    for name in ("check_llm", "check_smtp", "check_recipients"):
        monkeypatch.setattr(
            f"zotero_arxiv_daily.preflight.{name}",
            lambda c, _n=name: CheckResult(name=_n, ok=True, detail="fine"),
        )
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
    ok, results = run_preflight(make_config())
    assert ok is False
    assert any(not r.ok for r in results)


def test_run_preflight_returns_true_when_everything_passes(monkeypatch):
    for name in ("check_zotero", "check_llm", "check_smtp", "check_recipients"):
        monkeypatch.setattr(
            f"zotero_arxiv_daily.preflight.{name}",
            lambda c, _n=name: CheckResult(name=_n, ok=True, detail="fine"),
        )
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
    ok, _ = run_preflight(make_config())
    assert ok is True


def test_a_warning_alone_does_not_fail_preflight(monkeypatch):
    for name in ("check_zotero", "check_llm", "check_smtp", "check_recipients"):
        monkeypatch.setattr(
            f"zotero_arxiv_daily.preflight.{name}",
            lambda c, _n=name: CheckResult(name=_n, ok=True, detail="d", warning=True),
        )
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
    ok, _ = run_preflight(make_config())
    assert ok is True

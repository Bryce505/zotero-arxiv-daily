"""Preflight: prove the environment works before spending a full run on it.

The weekly pipeline does its expensive work — Zotero, clustering, four
searches, full-text, extraction — before it ever touches SMTP. A credential
or connectivity problem therefore surfaces at the very end, after twenty-odd
minutes. Preflight probes every boundary cheaply and reports a verdict.
"""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.preflight import (
    CheckResult,
    check_embedding,
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


def test_the_probe_uses_the_same_query_form_each_source_gets_in_production(monkeypatch):
    """A probe that bypasses the production query selection gives a false green.

    The first live run passed preflight on a two-word probe and then returned
    0 candidates from Europe PMC and OpenAlex, because those two AND their
    terms and production hands them a different query form than Crossref.
    """
    probed = {}

    def retriever_for(name):
        class Stub:
            def __init__(self, config):
                pass

            def search(self, query, start, end, limit):
                probed[name] = query
                return []

        return Stub

    monkeypatch.setattr(
        "zotero_arxiv_daily.preflight.get_query_retriever_cls", retriever_for
    )
    config = make_config()
    config.search.sources = ["pubmed", "europepmc", "openalex", "crossref"]

    check_sources(config)

    assert " OR " in probed["europepmc"], "a conjunctive source needs an OR'd probe"
    assert probed["openalex"] == probed["europepmc"]
    assert probed["pubmed"] != probed["crossref"], "PubMed takes its boolean form"
    assert " OR " not in probed["crossref"], "Crossref takes the natural-language form"


class TestCheckEmbedding:
    """The embedding backend is the one boundary preflight never touched.

    A wrong EMBEDDING_API_KEY composes fine — interpolation succeeds — and only
    surfaces at the rerank step, about ten minutes into the weekly run, after
    clustering and query distillation have already spent LLM calls.
    """

    @staticmethod
    def _config(reranker: str = "api"):
        return make_config(**{"executor.reranker": reranker})

    def _patch(self, monkeypatch, embed):
        class StubReranker:
            def __init__(self, config):
                pass

        StubReranker.embed = staticmethod(embed)
        monkeypatch.setattr(
            "zotero_arxiv_daily.preflight.get_reranker_cls", lambda name: StubReranker
        )

    def test_an_api_reranker_returning_a_vector_passes(self, monkeypatch):
        self._patch(monkeypatch, lambda texts: np.ones((len(texts), 1024)))
        result = check_embedding(self._config())
        assert result.ok and not result.warning
        assert "1024" in result.detail, "the operator should see the vector width"

    def test_an_api_reranker_that_raises_fails(self, monkeypatch):
        def boom(texts):
            raise RuntimeError("401 Unauthorized")

        self._patch(monkeypatch, boom)
        result = check_embedding(self._config())
        assert not result.ok
        assert "401 Unauthorized" in result.detail

    def test_an_api_reranker_returning_nothing_fails(self, monkeypatch):
        self._patch(monkeypatch, lambda texts: np.empty((0, 0)))
        result = check_embedding(self._config())
        assert not result.ok

    def test_the_local_reranker_is_not_probed(self, monkeypatch):
        """Loading the local model took 5.5 minutes on the runner.

        Preflight's whole value is being cheap, and a local model has no
        credential that could be wrong, so there is nothing worth paying for.
        """
        calls = []
        self._patch(monkeypatch, lambda texts: calls.append(texts) or np.ones((1, 8)))
        result = check_embedding(self._config("local"))
        assert result.ok
        assert calls == [], "the local model must not be downloaded by preflight"

    def test_run_preflight_includes_the_embedding_check(self, monkeypatch):
        self._patch(monkeypatch, lambda texts: np.ones((len(texts), 1024)))
        monkeypatch.setattr(
            "zotero_arxiv_daily.preflight.check_zotero",
            lambda c: CheckResult(name="zotero", ok=True, detail=""),
        )
        monkeypatch.setattr(
            "zotero_arxiv_daily.preflight.check_llm",
            lambda c: CheckResult(name="llm", ok=True, detail=""),
        )
        monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
        monkeypatch.setattr(
            "zotero_arxiv_daily.preflight.check_smtp",
            lambda c: CheckResult(name="smtp", ok=True, detail=""),
        )
        _, results = run_preflight(self._config())
        assert "embedding" in [r.name for r in results]


# ------------------------------------------------------------- report config

from zotero_arxiv_daily.preflight import check_report_config


def _ok(name: str) -> CheckResult:
    return CheckResult(name=name, ok=True, detail="stub")


def report_config(**overrides):
    report = {
        "min_relevance": 55,
        "min_score": 60,
        "triage_pool": 60,
        "triage_batch": 8,
        "journals": {"bonus": 10, "allow": ["mAbs", "Separations"]},
        "industry": {"bonus": 8, "names": ["Amgen"]},
        "fields": [
            {"key": "background", "label": "背景", "instruction": "i"},
            {"key": "method", "label": "方法", "instruction": "i", "kind": "list", "max_items": 5},
        ],
    }
    report.update(overrides)
    return OmegaConf.create({"report": report})


def test_a_well_formed_report_config_passes():
    result = check_report_config(report_config())
    assert result.ok
    assert "2 journals" in result.detail
    assert "1 companies" in result.detail


def test_a_journal_list_that_is_not_a_list_fails():
    # What a mis-indented YAML edit actually produces.
    result = check_report_config(report_config(journals={"bonus": 10, "allow": "mAbs"}))
    assert not result.ok
    assert "allow" in result.detail


def test_an_unknown_field_kind_fails():
    fields = [{"key": "method", "label": "方法", "instruction": "i", "kind": "bullets"}]
    result = check_report_config(report_config(fields=fields))
    assert not result.ok
    assert "bullets" in result.detail


def test_a_negative_threshold_fails():
    assert not check_report_config(report_config(min_relevance=-1)).ok


def test_a_zero_triage_batch_fails():
    assert not check_report_config(report_config(triage_batch=0)).ok


def test_zeroed_thresholds_are_allowed():
    # Setting both to 0 is the documented way to disable the gate.
    assert check_report_config(report_config(min_relevance=0, min_score=0)).ok


def test_empty_name_lists_are_allowed():
    result = check_report_config(report_config(journals={"bonus": 10, "allow": []}))
    assert result.ok


def test_a_duplicate_journal_warns_without_failing():
    # Hand-maintaining 63 lines makes a paste duplicate near certain, and a
    # duplicate is harmless — bonuses do not stack — so it must not block a run.
    config = report_config(journals={"bonus": 10, "allow": ["mAbs", "The MAbs"]})
    result = check_report_config(config)
    assert result.ok
    assert result.warning
    assert "mabs" in result.detail.lower()


def test_report_config_is_part_of_the_preflight_run(config, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_zotero", lambda c: _ok("zotero"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_llm", lambda c: _ok("llm"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_embedding", lambda c: _ok("embedding"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_recipients", lambda c: _ok("recipients"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_smtp", lambda c: _ok("smtp"))
    _, results = run_preflight(config)
    assert any(r.name == "report-config" for r in results)

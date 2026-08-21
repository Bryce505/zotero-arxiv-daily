"""The optional monthly synthesis layer."""

import os
from datetime import date
from types import SimpleNamespace

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.monthly import MonthlyExecutor, collect_month_reports, synthesise


def seed_reports(root, names):
    os.makedirs(os.path.join(root, "reports", "2026"), exist_ok=True)
    for name in names:
        with open(os.path.join(root, "reports", "2026", name), "w", encoding="utf-8") as handle:
            handle.write(f"# {name}\n\n正文 {name}")


def test_collect_finds_every_week_of_the_month(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-08-W2.md", "2026-08-W3.md"])
    found = collect_month_reports(str(tmp_path), 2026, 8)
    assert [name for name, _ in found] == ["2026-08-W1", "2026-08-W2", "2026-08-W3"]


def test_collect_ignores_other_months(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-07-W4.md"])
    assert [name for name, _ in collect_month_reports(str(tmp_path), 2026, 8)] == ["2026-08-W1"]


def test_collect_ignores_a_previous_monthly_synthesis(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-08-monthly.md"])
    assert [name for name, _ in collect_month_reports(str(tmp_path), 2026, 8)] == ["2026-08-W1"]


def test_collect_returns_the_report_bodies(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md"])
    _, body = collect_month_reports(str(tmp_path), 2026, 8)[0]
    assert "正文 2026-08-W1.md" in body


def test_collect_on_an_empty_month_is_empty(tmp_path):
    assert collect_month_reports(str(tmp_path), 2026, 8) == []


def stub_client(payload="## 月度综述\n\n本月主题演化…"):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
                )
            )
        )
    )


def test_synthesise_returns_the_model_output():
    result = synthesise([("2026-08-W1", "body")], stub_client(), {"generation_kwargs": {"model": "m"}})
    assert "月度综述" in result


def test_synthesise_degrades_to_empty_on_failure():
    def boom(**kw):
        raise RuntimeError("rate limited")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    assert synthesise([("2026-08-W1", "body")], client, {"generation_kwargs": {"model": "m"}}) == ""


def make_config(tmp_path):
    return OmegaConf.create(
        {
            "llm": {
                "api": {"key": "k", "base_url": "http://localhost/v1"},
                "generation_kwargs": {"model": "m"},
                "language": "中文",
            },
            "report": {"output_dir": str(tmp_path)},
            "git": {"enabled": False, "user_name": "b", "user_email": "b@e.org", "branch": ""},
            "email": {
                "sender": "me@example.org",
                "sender_password": "pw",
                "smtp_server": "s",
                "smtp_port": 587,
                "recipients": ["t@example.org"],
            },
        }
    )


@pytest.fixture()
def stub_monthly(monkeypatch):
    sent = []
    monkeypatch.setattr("zotero_arxiv_daily.monthly.OpenAI", lambda **kw: stub_client())
    monkeypatch.setattr("zotero_arxiv_daily.monthly.send_digest", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr("zotero_arxiv_daily.monthly.git_commit_paths", lambda *a, **kw: True)
    return sent


def test_monthly_run_writes_a_synthesis(tmp_path, stub_monthly):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-08-W2.md"])
    path = MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31))
    assert path is not None
    assert (tmp_path / "reports" / "2026" / "2026-08-monthly.md").exists()


def test_monthly_run_sends_the_synthesis(tmp_path, stub_monthly):
    seed_reports(tmp_path, ["2026-08-W1.md"])
    MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31))
    assert len(stub_monthly) == 1
    _, subject, _, _ = stub_monthly[0]
    assert "2026-08" in subject


def test_monthly_run_is_a_no_op_without_weekly_reports(tmp_path, stub_monthly):
    assert MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31)) is None
    assert stub_monthly == []


def test_monthly_run_skips_delivery_when_synthesis_is_empty(tmp_path, monkeypatch):
    sent = []
    seed_reports(tmp_path, ["2026-08-W1.md"])

    def boom(**kw):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(
        "zotero_arxiv_daily.monthly.OpenAI",
        lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom))),
    )
    monkeypatch.setattr("zotero_arxiv_daily.monthly.send_digest", lambda *a, **kw: sent.append(a))
    assert MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31)) is None
    assert sent == []


def test_model_output_is_escaped_before_it_reaches_the_email_body(tmp_path, monkeypatch):
    """Every other rendering path escapes; this one must not be the exception."""
    sent = []
    seed_reports(tmp_path, ["2026-08-W1.md"])
    monkeypatch.setattr(
        "zotero_arxiv_daily.monthly.OpenAI",
        lambda **kw: stub_client("综述 <script>alert(1)</script> 结束"),
    )
    monkeypatch.setattr("zotero_arxiv_daily.monthly.send_digest", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr("zotero_arxiv_daily.monthly.git_commit_paths", lambda *a, **kw: True)
    MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31))
    _, _, html, _ = sent[0]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_synthesis_is_committed_from_the_output_root(tmp_path, monkeypatch):
    """git add must run where the artefacts were written."""
    calls = []
    seed_reports(tmp_path, ["2026-08-W1.md"])
    monkeypatch.setattr("zotero_arxiv_daily.monthly.OpenAI", lambda **kw: stub_client())
    monkeypatch.setattr("zotero_arxiv_daily.monthly.send_digest", lambda *a, **kw: None)
    monkeypatch.setattr(
        "zotero_arxiv_daily.monthly.git_commit_paths",
        lambda paths, message, config, cwd=".": calls.append((paths, cwd)) or True,
    )
    MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31))
    paths, cwd = calls[0]
    assert cwd == str(tmp_path)
    assert all(not p.startswith("/") for p in paths)


def test_the_synthesis_anchor_covers_a_fifth_friday(tmp_path, stub_monthly):
    """October 2026's fifth Friday is the 30th; a run on the 28th misses it."""
    from zotero_arxiv_daily.monthly import synthesis_anchor

    # Fired on 2026-11-01, the anchor must land inside October.
    anchor = synthesis_anchor(date(2026, 11, 1))
    assert anchor.year == 2026 and anchor.month == 10
    assert anchor.day >= 30

    seed_reports(tmp_path, ["2026-10-W5.md"])
    path = MonthlyExecutor(make_config(tmp_path)).run(anchor=anchor)
    assert path is not None
    assert (tmp_path / "reports" / "2026" / "2026-10-monthly.md").exists()

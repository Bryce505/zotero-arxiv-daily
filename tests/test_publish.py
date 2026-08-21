"""Writing digest artefacts and committing them back to the repository."""

import os
import subprocess

from omegaconf import OmegaConf

from zotero_arxiv_daily.publish import git_commit_paths, write_text


def make_config(enabled=True):
    return OmegaConf.create(
        {"git": {"enabled": enabled, "user_name": "digest bot", "user_email": "bot@example.org", "branch": ""}}
    )


def test_write_text_creates_missing_directories(tmp_path):
    path = str(tmp_path / "reports" / "2026" / "2026-08-W3.md")
    write_text(path, "# hi")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "# hi"


def test_write_text_returns_the_path(tmp_path):
    path = str(tmp_path / "a.md")
    assert write_text(path, "x") == path


def test_write_text_overwrites_an_existing_file(tmp_path):
    path = str(tmp_path / "a.md")
    write_text(path, "first")
    write_text(path, "second")
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "second"


def test_write_text_round_trips_unicode(tmp_path):
    path = str(tmp_path / "a.md")
    write_text(path, "电荷异质性")
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "电荷异质性"


def _init_repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "seed@example.org"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "seed"], cwd=repo, check=True)
    with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as handle:
        handle.write("seed")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def test_commit_records_a_new_file(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    assert git_commit_paths(["reports/a.md"], "docs: add report", make_config(), cwd=repo) is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "docs: add report" in log


def test_commit_is_skipped_when_nothing_changed(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_commit_paths(["seed.txt"], "docs: nothing", make_config(), cwd=repo) is False
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "docs: nothing" not in log


def test_commit_is_skipped_when_disabled(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    assert git_commit_paths(["reports/a.md"], "docs: add", make_config(enabled=False), cwd=repo) is False


def test_commit_is_skipped_for_an_empty_path_list(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_commit_paths([], "docs: add", make_config(), cwd=repo) is False


def test_commit_uses_the_configured_identity(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    git_commit_paths(["reports/a.md"], "docs: add report", make_config(), cwd=repo)
    author = subprocess.run(
        ["git", "log", "-1", "--pretty=%an <%ae>"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert author == "digest bot <bot@example.org>"


def test_commit_failure_is_reported_not_raised(tmp_path):
    not_a_repo = str(tmp_path / "plain")
    os.makedirs(not_a_repo)
    write_text(os.path.join(not_a_repo, "a.md"), "x")
    assert git_commit_paths(["a.md"], "docs: add", make_config(), cwd=not_a_repo) is False

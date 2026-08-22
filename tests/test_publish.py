"""Writing digest artefacts and committing them back to the repository."""

import os
import subprocess

from omegaconf import OmegaConf

from zotero_arxiv_daily.publish import git_commit_paths, git_push_artefacts, write_text


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


def test_a_missing_path_does_not_sink_the_whole_commit(tmp_path):
    """git add is all-or-nothing: one bad pathspec stages nothing at all.

    The digest and the seen-DOI state must still be archived when an
    optional artefact (a cache that degraded, a gitignored library) is
    absent.
    """
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    assert git_commit_paths(
        ["reports/a.md", "state/never_written.json"], "docs: add report", make_config(), cwd=repo
    ) is True
    tracked = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "reports/a.md" in tracked


def test_a_gitignored_path_does_not_sink_the_whole_commit(tmp_path):
    repo = _init_repo(tmp_path)
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as handle:
        handle.write("library/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ignore"], cwd=repo, check=True)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    write_text(os.path.join(repo, "library", "x.pdf"), "pdf")
    assert git_commit_paths(
        ["reports/a.md", "library"], "docs: add report", make_config(), cwd=repo
    ) is True


def test_a_commit_of_only_missing_paths_is_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_commit_paths(["nope/a.md"], "docs: add", make_config(), cwd=repo) is False


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _clone_pair(tmp_path):
    """A bare remote plus two independent clones of it.

    Reproduces the runner's situation: the digest job holds a checkout for the
    length of the run while anything else may push to the same branch.
    """
    remote = str(tmp_path / "remote.git")
    _run(["git", "init", "-q", "--bare", "-b", "main", remote], str(tmp_path))
    seed = _init_repo(tmp_path)
    _run(["git", "branch", "-M", "main"], seed)
    _run(["git", "remote", "add", "origin", remote], seed)
    _run(["git", "push", "-q", "-u", "origin", "main"], seed)

    clones = []
    for name in ("runner", "other"):
        path = str(tmp_path / name)
        _run(["git", "clone", "-q", remote, path], str(tmp_path))
        _run(["git", "config", "user.email", f"{name}@example.org"], path)
        _run(["git", "config", "user.name", name], path)
        clones.append(path)
    return remote, clones[0], clones[1]


def _commit_file(repo, name, body="x"):
    write_text(os.path.join(repo, name), body)
    _run(["git", "add", "--", name], repo)
    _run(["git", "commit", "-q", "-m", f"add {name}"], repo)


def _remote_log(remote):
    return subprocess.run(
        ["git", "log", "--oneline", "main"], cwd=remote, capture_output=True, text=True
    ).stdout


def test_push_lands_the_digest_commit_on_the_remote(tmp_path):
    remote, runner, _ = _clone_pair(tmp_path)
    _commit_file(runner, "digest.md")

    assert git_push_artefacts(make_config(), cwd=runner) is True
    assert "add digest.md" in _remote_log(remote)


def test_push_rebases_onto_a_concurrent_push_instead_of_losing_the_digest(tmp_path):
    """The demonstrated data-loss path.

    A digest run holds its checkout for the length of the run. When anything
    else pushes to the branch meanwhile, a plain push is rejected — and with
    `git push || echo` the workflow swallowed it, reported success, and the
    ephemeral runner took the only copy of the report and the seen-DOI state
    with it.
    """
    remote, runner, other = _clone_pair(tmp_path)
    _commit_file(runner, "digest.md")
    _commit_file(other, "unrelated.md")
    _run(["git", "push", "-q", "origin", "main"], other)

    assert git_push_artefacts(make_config(), cwd=runner) is True

    log = _remote_log(remote)
    assert "add digest.md" in log, "the digest commit must survive the race"
    assert "add unrelated.md" in log, "the concurrent commit must not be clobbered"


def test_push_reports_failure_when_it_cannot_land(tmp_path):
    """Failure must be visible: silence is what made the loss undetectable."""
    _, runner, _ = _clone_pair(tmp_path)
    _commit_file(runner, "digest.md")
    _run(["git", "remote", "set-url", "origin", str(tmp_path / "gone.git")], runner)

    assert git_push_artefacts(make_config(), cwd=runner, attempts=1) is False


def test_nothing_to_push_is_success(tmp_path):
    _, runner, _ = _clone_pair(tmp_path)
    assert git_push_artefacts(make_config(), cwd=runner) is True


def test_push_is_skipped_when_git_is_disabled(tmp_path):
    remote, runner, _ = _clone_pair(tmp_path)
    _commit_file(runner, "digest.md")

    assert git_push_artefacts(make_config(enabled=False), cwd=runner) is True
    assert "add digest.md" not in _remote_log(remote)

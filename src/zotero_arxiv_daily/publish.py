"""Write digest artefacts and commit them back to the repository.

Committing from Actions needs ``permissions: contents: write`` on the job and
an author identity on the runner.  A run that produced no change must not
commit: ``git commit`` exits non-zero on an empty index and would fail the
whole workflow.
"""

import os
import subprocess

from loguru import logger


def write_text(path: str, content: str) -> str:
    """Write *content* to *path*, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def git_commit_paths(paths: list[str], message: str, config, cwd: str = ".") -> bool:
    """Stage *paths* and commit them. Returns whether a commit was made."""
    settings = config.git
    if not settings.get("enabled", True):
        logger.info("git.enabled is false; leaving artefacts uncommitted")
        return False
    if not paths:
        return False

    _run(["git", "config", "user.name", str(settings.get("user_name") or "zotero-cmc-weekly")], cwd)
    _run(["git", "config", "user.email", str(settings.get("user_email") or "actions@github.com")], cwd)

    add = _run(["git", "add", "--", *paths], cwd)
    if add.returncode != 0:
        logger.warning(f"git add failed: {add.stderr.strip()}")
        return False

    staged = _run(["git", "diff", "--cached", "--name-only"], cwd)
    if not staged.stdout.strip():
        logger.info("No artefact changes to commit")
        return False

    commit = _run(["git", "commit", "-m", message], cwd)
    if commit.returncode != 0:
        logger.warning(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
        return False

    logger.info(f"Committed {len(staged.stdout.strip().splitlines())} artefact files")
    return True

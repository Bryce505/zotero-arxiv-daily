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

    # git add is all-or-nothing: one absent or ignored pathspec aborts the
    # whole call and stages nothing, which would lose the report and the
    # seen-DOI state along with the optional artefact.
    added = 0
    for path in paths:
        result = _run(["git", "add", "--", path], cwd)
        if result.returncode != 0:
            logger.info(f"Not archiving {path}: {result.stderr.strip() or 'nothing to add'}")
            continue
        added += 1
    if added == 0:
        logger.warning("None of the artefact paths could be staged")
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


def _current_branch(cwd: str) -> str:
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return result.stdout.strip()


def git_push_artefacts(config, cwd: str = ".", attempts: int = 5) -> bool:
    """Push the digest commit, rebasing past anything that landed meanwhile.

    The digest job holds its checkout for the length of the run — measured at
    an hour — and anything else pushing to the branch in that window makes a
    plain push fail. The previous ``git push || echo "nothing to push"``
    swallowed that failure and reported success, so the ephemeral runner was
    reclaimed carrying the only copy of the report, the seen-DOI state and
    the caches. Returning False here is what makes the loss visible.
    """
    if not config.git.get("enabled", True):
        logger.info("git.enabled is false; leaving artefacts unpushed")
        return True

    branch = _current_branch(cwd)
    if not branch or branch == "HEAD":
        logger.warning("Not on a branch; refusing to push a detached HEAD")
        return False

    for attempt in range(1, attempts + 1):
        ahead = _run(["git", "log", "--oneline", f"origin/{branch}..HEAD"], cwd)
        if ahead.returncode == 0 and not ahead.stdout.strip():
            logger.info("No artefact commit to push")
            return True

        push = _run(["git", "push", "origin", f"HEAD:{branch}"], cwd)
        if push.returncode == 0:
            logger.info(f"Pushed the digest to {branch} on attempt {attempt}")
            return True

        detail = (push.stderr or push.stdout).strip().splitlines()
        logger.warning(f"Push rejected on attempt {attempt}: {detail[-1] if detail else 'unknown'}")
        if attempt == attempts:
            break

        fetch = _run(["git", "fetch", "origin", branch], cwd)
        if fetch.returncode != 0:
            logger.warning(f"Cannot reach origin: {fetch.stderr.strip()}")
            continue
        # --autostash, because the checkout is always dirty here: weekly.yml
        # writes CUSTOM_CONFIG into config/custom.yaml, a tracked file, before
        # the pipeline starts. Without it `git rebase` refuses outright ("You
        # have unstaged changes"), which made this whole recovery path dead
        # code in the one environment it exists for — run 33573304939 mailed
        # its digest and then lost the archive and the seen-DOI state exactly
        # that way.
        rebase = _run(["git", "rebase", "--autostash", f"origin/{branch}"], cwd)
        if rebase.returncode != 0:
            _run(["git", "rebase", "--abort"], cwd)
            logger.error(f"Cannot rebase onto origin/{branch}: {rebase.stderr.strip()}")
            return False

    logger.error(
        f"Could not push the digest to {branch} after {attempts} attempts; "
        "the archive and the seen-DOI state would be lost with this runner"
    )
    return False

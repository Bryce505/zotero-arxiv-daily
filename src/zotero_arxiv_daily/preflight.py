"""Prove the environment works before spending a full run on it.

The weekly pipeline does all its expensive work — Zotero, clustering, four
searches, full-text retrieval, extraction — before it ever touches SMTP.  A
wrong credential or an unreachable host therefore surfaces at the very end,
after twenty-odd minutes of runner time.

Preflight probes every boundary cheaply, sends no mail, writes nothing, and
exits non-zero if anything is broken, so a first run tells you what is wrong
in about a minute.
"""

import smtplib
import sys
from dataclasses import dataclass
from datetime import date, timedelta

import dotenv
import hydra
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from zotero_arxiv_daily.executor import Executor
from zotero_arxiv_daily.mailer import SMTP_TIMEOUT_SECONDS, resolve_recipients
from zotero_arxiv_daily.retriever import get_query_retriever_cls

_PROBE_LIMIT = 2
_THIN_FILTER_RATIO = 0.2


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _fetch_corpus(config: DictConfig):
    """Return ``(all_papers, matched_papers)`` from the live Zotero library."""
    executor = Executor.__new__(Executor)
    executor.config = config
    executor.include_path_patterns = list(config.zotero.include_path or []) or None
    executor.ignore_path_patterns = list(config.zotero.ignore_path or []) or None
    everything = executor.fetch_zotero_corpus()
    return everything, executor.filter_corpus(list(everything))


def check_zotero(config: DictConfig) -> CheckResult:
    try:
        everything, matched = _fetch_corpus(config)
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        return CheckResult(name="zotero", ok=False, detail=f"could not read the library: {exc}")

    if not matched:
        return CheckResult(
            name="zotero",
            ok=False,
            detail=(
                f"{len(everything)} papers have abstracts but include_path "
                f"{list(config.zotero.include_path or [])} matched none of them"
            ),
        )

    thin = len(matched) < len(everything) * _THIN_FILTER_RATIO
    return CheckResult(
        name="zotero",
        ok=True,
        detail=f"{len(matched)} of {len(everything)} papers matched include_path",
        warning=thin,
    )


def check_llm(config: DictConfig) -> CheckResult:
    model = config.llm.generation_kwargs.get("model")
    try:
        client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
        client.chat.completions.create(
            messages=[{"role": "user", "content": "reply with the single word ok"}],
            **config.llm.generation_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="llm", ok=False, detail=f"{model} did not answer: {exc}")

    language = str(config.llm.get("language") or "")
    if language.lower() == "english":
        return CheckResult(
            name="llm",
            ok=True,
            warning=True,
            detail=(
                f"{model} answered, but llm.language is English: the digest fields "
                "will be written in English. Set language: 中文 for a Chinese digest."
            ),
        )
    return CheckResult(name="llm", ok=True, detail=f"{model} answered ({language or 'default'})")


def check_sources(config: DictConfig) -> list[CheckResult]:
    end = date.today()
    start = end - timedelta(days=30)
    results = []
    for name in config.search.sources:
        try:
            retriever = get_query_retriever_cls(name)(config)
            found = retriever.search("monoclonal antibody", start, end, _PROBE_LIMIT)
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name=name, ok=False, detail=f"unreachable: {exc}"))
            continue
        if not found:
            results.append(
                CheckResult(
                    name=name,
                    ok=True,
                    warning=True,
                    detail="reachable but the probe query returned nothing",
                )
            )
        else:
            results.append(CheckResult(name=name, ok=True, detail=f"{len(found)} probe results"))
    return results


def check_smtp(config: DictConfig) -> CheckResult:
    settings = config.email
    server = None
    try:
        try:
            server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
            server.starttls()
        except Exception:  # noqa: BLE001 - many providers are SSL-only on 465
            if server is not None:
                try:
                    server.close()
                except Exception:  # noqa: BLE001
                    pass
            server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
        server.login(settings.sender, settings.sender_password)
        server.quit()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="smtp", ok=False, detail=f"login rejected: {exc}")
    return CheckResult(name="smtp", ok=True, detail=f"{settings.smtp_server} accepted the login")


def check_recipients(config: DictConfig) -> CheckResult:
    recipients = resolve_recipients(config.email)
    if not recipients:
        return CheckResult(
            name="recipients",
            ok=False,
            detail="no recipients resolved; set the RECIPIENTS secret (comma separated)",
        )
    return CheckResult(name="recipients", ok=True, detail=f"{len(recipients)} recipients, all Bcc")


def run_preflight(config: DictConfig) -> tuple[bool, list[CheckResult]]:
    """Run every check. Returns ``(everything_passed, results)``."""
    results = [check_zotero(config), check_llm(config)]
    results.extend(check_sources(config))
    results.extend([check_recipients(config), check_smtp(config)])
    return all(r.ok for r in results), results


def format_report(results: list[CheckResult]) -> str:
    lines = ["", "Preflight", "─" * 60]
    for result in results:
        mark = "FAIL" if not result.ok else ("WARN" if result.warning else " OK ")
        lines.append(f"[{mark}] {result.name:<12} {result.detail}")
    lines.append("─" * 60)
    failures = [r for r in results if not r.ok]
    warnings = [r for r in results if r.ok and r.warning]
    if failures:
        lines.append(f"FAIL — {len(failures)} check(s) must be fixed before the weekly run")
    elif warnings:
        lines.append(f"PASS with {len(warnings)} warning(s) — the run will work, but read them")
    else:
        lines.append("PASS — every check succeeded")
    return "\n".join(lines)


dotenv.load_dotenv()


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig) -> None:
    ok, results = run_preflight(config)
    logger.info(format_report(results))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

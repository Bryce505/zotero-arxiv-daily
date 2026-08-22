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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

import dotenv
import hydra
import numpy as np
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from zotero_arxiv_daily.executor import Executor
from zotero_arxiv_daily.mailer import SMTP_TIMEOUT_SECONDS, _safe_get, resolve_recipients
from zotero_arxiv_daily.reranker import get_reranker_cls
from zotero_arxiv_daily.retriever import get_query_retriever_cls
from zotero_arxiv_daily.search.profile import QueryProfile, query_for_source

_PROBE_LIMIT = 2
_THIN_FILTER_RATIO = 0.2

# Routed through ``query_for_source`` so each source is probed with the query
# form it will actually receive.  A probe that skips that step passes while
# production returns nothing, which is exactly what happened on the first run.
_PROBE_PROFILE = QueryProfile(
    cluster="preflight probe",
    mesh_terms=["Antibodies, Monoclonal"],
    free_terms=["monoclonal antibody", "mass spectrometry"],
    pubmed_query='"monoclonal antibody"[tiab]',
    plain_query="monoclonal antibody characterisation by mass spectrometry",
)


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


# Short, domain-shaped, and cheap: one vector proves the credential, the base
# URL and the model name all resolve.
_EMBEDDING_PROBE = "capillary isoelectric focusing of monoclonal antibody charge variants"


def check_embedding(config: DictConfig) -> CheckResult:
    """Probe the embedding backend — the one boundary nothing else here touches.

    A wrong key composes fine and only fails at the rerank step, ten minutes
    into the weekly run, after clustering and distillation have already spent
    LLM calls.

    Only the API reranker is probed. It is the one holding a credential that
    can be wrong, and it answers in about a second; the local model has no
    credential, and downloading it cost 5.5 minutes on the runner, which would
    defeat the point of a preflight that finishes in under a minute.
    """
    executor = _safe_get(config, "executor") or {}
    name = str(_safe_get(executor, "reranker") or "")
    if name != "api":
        return CheckResult(
            name="embedding",
            ok=True,
            detail=f"{name or 'default'} reranker; no credential to probe",
        )
    try:
        vectors = get_reranker_cls(name)(config).embed([_EMBEDDING_PROBE])
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        return CheckResult(name="embedding", ok=False, detail=f"embedding call failed: {exc}")

    array = np.asarray(vectors)
    if array.size == 0:
        return CheckResult(
            name="embedding", ok=False, detail="the embedding API returned no vector"
        )
    api = _safe_get(config, "reranker") or {}
    model = _safe_get(_safe_get(api, "api") or {}, "model") or "the embedding model"
    return CheckResult(
        name="embedding", ok=True, detail=f"{model} returned a {array.shape[-1]}-dim vector"
    )


def check_sources(config: DictConfig) -> list[CheckResult]:
    end = date.today()
    start = end - timedelta(days=30)
    results = []
    for name in config.search.sources:
        try:
            retriever = get_query_retriever_cls(name)(config)
            query = query_for_source(_PROBE_PROFILE, name)
            found = retriever.search(query, start, end, _PROBE_LIMIT)
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


_VALID_FIELD_KINDS = frozenset({"text", "list"})
_NON_NEGATIVE_KEYS = ("min_relevance", "min_score", "triage_pool")


def _name_list(block, key: str, problems: list[str]) -> list[str]:
    """Read one curated name list, complaining if the YAML edit went wrong."""
    raw = _safe_get(block, key)
    if raw is None:
        return []
    # A dict/DictConfig is iterable too (over its keys), which would silently
    # turn a mis-edited mapping into a name list built from those keys.
    if isinstance(raw, (str, Mapping)) or not hasattr(raw, "__iter__"):
        problems.append(f"{key} must be a list, got {type(raw).__name__}")
        return []
    return [str(n) for n in raw]


def check_report_config(config: DictConfig) -> CheckResult:
    """Validate the parts of ``report`` the operator hand-edits.

    Every other check probes a remote boundary.  This one probes the file the
    operator most recently touched: the curated lists and report fields moved
    into ``config/base.yaml`` precisely because they are edited by hand, and a
    mis-indented list is otherwise discovered on a Friday, mid-send.
    """
    from .affiliation import normalize

    report = _safe_get(config, "report") or {}
    problems: list[str] = []
    warnings: list[str] = []

    journals_block = _safe_get(report, "journals") or {}
    industry_block = _safe_get(report, "industry") or {}
    journals = _name_list(journals_block, "allow", problems)
    companies = _name_list(industry_block, "names", problems)

    for label, names in (("journals", journals), ("companies", companies)):
        seen: dict[str, str] = {}
        for name in names:
            key = normalize(name)
            if key and key in seen:
                warnings.append(f"{label}: {name!r} duplicates {seen[key]!r}")
            seen[key] = name

    for key in _NON_NEGATIVE_KEYS:
        value = _safe_get(report, key)
        if value is not None and (not isinstance(value, int) or value < 0):
            problems.append(f"{key} must be a non-negative integer, got {value!r}")
    for label, block in (("journals.bonus", journals_block), ("industry.bonus", industry_block)):
        value = _safe_get(block, "bonus")
        if value is not None and (not isinstance(value, int) or value < 0):
            problems.append(f"{label} must be a non-negative integer, got {value!r}")
    batch = _safe_get(report, "triage_batch")
    if batch is not None and (not isinstance(batch, int) or batch < 1):
        problems.append(f"triage_batch must be at least 1, got {batch!r}")

    kinds = {"text": 0, "list": 0}
    for field in _safe_get(report, "fields") or []:
        kind = str(_safe_get(field, "kind") or "text")
        if kind not in _VALID_FIELD_KINDS:
            problems.append(f"field {_safe_get(field, 'key')!r} has unknown kind {kind!r}")
        else:
            kinds[kind] += 1

    if problems:
        return CheckResult(name="report-config", ok=False, detail="; ".join(problems))
    detail = (
        f"{len(journals)} journals, {len(companies)} companies, "
        f"{kinds['text'] + kinds['list']} fields ({kinds['text']} text / {kinds['list']} list)"
    )
    if warnings:
        return CheckResult(
            name="report-config", ok=True, warning=True, detail=f"{detail}; {'; '.join(warnings)}"
        )
    return CheckResult(name="report-config", ok=True, detail=detail)


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
    results = [check_report_config(config), check_zotero(config), check_llm(config)]
    results.extend(check_sources(config))
    results.extend([check_embedding(config), check_recipients(config), check_smtp(config)])
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

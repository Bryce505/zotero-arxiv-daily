"""An optional second search line: whatever topic the operator names.

The rest of the digest is derived from the Zotero library — themes are
clustered out of it, queries are distilled from those themes, and relevance
is judged against biologics CMC.  That is the right default and a poor fit
for "I am also watching X this quarter", where X may be adjacent to the
library or outside it entirely.

So this line takes a topic (plus optional background) straight from the
operator, has the model turn it into the same four query forms every other
source consumes, searches the same window, and judges what comes back
against *that* topic rather than against CMC.  An empty topic switches the
whole line off: no prompt, no request, no section in the report.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date

from loguru import logger

from ..dedup import dedup_papers, drop_seen, normalize_doi
from ..protocol import Paper
from ..scoring import score_papers
from ..triage import triage_for_topic
from .profile import QueryProfile, query_for_source

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_BACKFILL_OVERSAMPLE = 3


@dataclass(frozen=True)
class FocusSettings:
    topic: str
    background: str
    min_papers: int
    max_papers: int
    min_relevance: int


@dataclass
class FocusResult:
    """One rendered section: the topic, a sentence about it, its papers."""

    topic: str
    summary: str
    papers: list[Paper] = field(default_factory=list)


def focus_settings(config) -> FocusSettings | None:
    """Read ``search.focus``, or None when no topic was given.

    None means the feature is off, and callers must take it as "do nothing at
    all" rather than "search with an empty query" — an empty topic is how an
    operator who does not want this line says so, and it must not cost them
    an LLM call.
    """
    search = config.get("search") or {}
    block = search.get("focus") or {}
    topic = str(block.get("topic") or "").strip()
    if not topic:
        return None
    return FocusSettings(
        topic=topic,
        background=str(block.get("background") or "").strip(),
        min_papers=int(block.get("min_papers") or 0),
        max_papers=int(block.get("max_papers") or 0) or 8,
        min_relevance=int(block.get("min_relevance") or 0),
    )


def build_focus_profile(topic: str, background: str | None, client, llm_params: dict) -> tuple[QueryProfile, str]:
    """Turn the operator's topic into query forms plus a one-line summary.

    Reuses ``QueryProfile`` rather than inventing a shape: every source
    already knows how to read one, so the focus line gets PubMed's boolean
    form and Europe PMC/OpenAlex's OR'd terms for free.  A failure degrades
    to searching the raw topic text — losing the summary is cosmetic, losing
    the search would be the whole feature.
    """
    context = f"\n用户补充的背景：{background}" if background else ""
    prompt = (
        f"用户希望在文献周报里追踪这个主题：{topic}{context}\n\n"
        "请先用一句话（不超过 40 字）概括这个主题在关注什么，再为它生成检索式，"
        "用于在 PubMed / Crossref / OpenAlex 上找该主题的文献。只输出 JSON：\n"
        '{"summary":"一句话概括","mesh_terms":["..."],"free_terms":["..."],'
        '"pubmed_query":"带 [MeSH] 与 [tiab] 限定的布尔式","plain_query":"英文自然语言检索词"}'
    )
    try:
        response = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "你是一位医药文献检索专家，精通 PubMed 检索式构造，只输出 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        match = _JSON_BLOCK_RE.search(response.choices[0].message.content or "")
        if match is None:
            raise ValueError("no JSON object found in the response")
        data = json.loads(match.group(0))
        return (
            QueryProfile(
                cluster=topic,
                mesh_terms=[str(t) for t in data.get("mesh_terms", [])],
                free_terms=[str(t) for t in data.get("free_terms", [])],
                pubmed_query=str(data.get("pubmed_query", "")),
                plain_query=str(data.get("plain_query", "")) or topic,
            ),
            str(data.get("summary", "")).strip(),
        )
    except Exception as exc:  # noqa: BLE001 - the focus line must degrade, not fail
        logger.warning(f"Could not distil a query profile for {topic!r} ({exc}); searching the topic as given")
        return QueryProfile(cluster=topic, mesh_terms=[], free_terms=[], pubmed_query="", plain_query=topic), ""


def _keep_relevant(papers: list[Paper], config, client, settings: FocusSettings) -> list[Paper]:
    """Judge against the operator's topic, then apply the topic floor."""
    if not papers:
        return []
    triage_for_topic(
        papers,
        client,
        config.llm,
        settings.topic,
        settings.background,
        int((config.get("report") or {}).get("triage_batch", 8)),
    )
    # Scoring is reused only for the badge line and the ordering: the journal
    # and company bonuses mean the same thing here as anywhere else, while the
    # CMC thresholds in `passing_papers` do not, so the floor applied is the
    # topic's own.
    score_papers(papers, config)
    return [p for p in papers if p.scoring and p.scoring.relevance >= settings.min_relevance]


def collect_focus_papers(
    config,
    client,
    retriever_for,
    start: date,
    end: date,
    exclude_dois: set[str] | frozenset[str] = frozenset(),
) -> FocusResult | None:
    """Search the operator's topic and return its section, or None.

    None covers every "there is nothing to show" case — no topic configured,
    nothing retrieved, nothing clearing the topic floor — so the renderers
    never have to decide whether an empty section is worth a heading.
    """
    settings = focus_settings(config)
    if settings is None:
        return None

    profile, summary = build_focus_profile(settings.topic, settings.background, client, config.llm)
    limit = int(config.search.get("per_cluster_limit", 25))
    found: list[Paper] = []
    for source in config.search.sources:
        query = query_for_source(profile, source)
        papers = retriever_for(source).search(query, start, end, limit)
        logger.info(f"{source}/焦点主题「{settings.topic}」: {len(papers)} candidates")
        found.extend(papers)

    excluded = set(exclude_dois)
    candidates = drop_seen(dedup_papers(found), excluded)
    kept = _keep_relevant(candidates, config, client, settings)
    excluded |= {d for d in (normalize_doi(p.doi) for p in kept) if d}

    logger.info(
        f"Focus topic {settings.topic!r}: {len(candidates)} candidates -> "
        f"{len(kept)} cleared relevance {settings.min_relevance}"
    )

    if len(kept) < settings.min_papers:
        # Same reasoning as the library-wide backfill: a quiet week on a niche
        # topic is better served by an established paper than by nothing.
        shortfall = settings.min_papers - len(kept)
        classics = retriever_for("openalex").search_highly_cited(
            profile.plain_query, max(1, shortfall * _BACKFILL_OVERSAMPLE)
        )
        for paper in classics:
            paper.is_backfill = True
        fetched = len(classics)
        classics = drop_seen(dedup_papers(classics), excluded)
        topped_up = _keep_relevant(classics, config, client, settings)[:shortfall]
        kept.extend(topped_up)
        # Without this the section can come back empty with nothing saying
        # whether OpenAlex had nothing, or had plenty and none of it was on
        # topic — the same blind spot the library-wide backfill had.
        logger.info(
            f"Focus top-up for {settings.topic!r}: {fetched} fetched from OpenAlex -> "
            f"{len(classics)} after exclude/dedup -> {len(topped_up)} kept (needed {shortfall})"
        )

    if not kept:
        logger.warning(
            f"Focus topic {settings.topic!r} produced no papers this week; section omitted. "
            "Nothing retrieved cleared the topic relevance floor "
            f"({settings.min_relevance}); lower search.focus.min_relevance, or widen the topic, "
            "if this repeats"
        )
        return None

    kept.sort(key=lambda p: -(p.scoring.rank_score if p.scoring else 0))
    return FocusResult(topic=settings.topic, summary=summary, papers=kept[: settings.max_papers])

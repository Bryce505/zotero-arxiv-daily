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

from ..dedup import dedup_papers, drop_seen, normalize_doi, title_key
from ..protocol import Paper
from ..scoring import score_papers
from ..triage import triage_for_topic
from .profile import _CONJUNCTIVE_SOURCES, or_join

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_BACKFILL_OVERSAMPLE = 3
_MIN_CLASSICS_FETCHED = 25
# The focus line is not a week's news. The operator named a subject, and the
# answer is whatever the literature holds on it — run 33573304939 found no
# paper on its topic in seven days, and widening to ninety only filled the
# section with adjacent-method work. Every source is asked for its best
# matches across the whole record instead; the topic gate, not the calendar,
# decides what survives.
_ALL_TIME_START = date(1900, 1, 1)


class _SeenSet:
    """DOIs and titles already retrieved, so a later rung does not re-judge them.

    Re-judging costs an LLM call and can hand the same paper two different
    verdicts inside one digest, which reads as a coin flip to whoever compares
    the sections.
    """

    def __init__(self, dois):
        self._dois = set(dois)
        self._titles: set[str] = set()

    def unseen(self, papers: list[Paper]) -> list[Paper]:
        return [
            p for p in papers
            if (normalize_doi(p.doi) or "") not in self._dois and title_key(p.title) not in self._titles
        ]

    def remember(self, papers: list[Paper]) -> None:
        for paper in papers:
            doi = normalize_doi(paper.doi)
            if doi:
                self._dois.add(doi)
            self._titles.add(title_key(paper.title))


@dataclass(frozen=True)
class FocusSettings:
    topic: str
    background: str
    min_papers: int
    max_papers: int
    min_relevance: int


@dataclass(frozen=True)
class FocusProfile:
    """Query forms for a topic that names a subject.

    Deliberately not ``QueryProfile``. A library theme is a spread of related
    concepts, so OR-ing its terms is right: any of them marks a paper as
    on-theme. A focus topic names a *subject*, and OR-ing that subject in
    with the terms describing the angle makes the subject optional — which
    is exactly what happened. Europe PMC was asked for

        ("ulinastatin" OR "urinary trypsin inhibitor" OR "enzyme inhibition"
         OR "kinetic parameters" OR "inhibitory kinetics")

    and duly returned enzyme-kinetics papers about other enzymes entirely.
    Here the subject group is ANDed with the aspect group, so a paper that
    only matches the angle cannot come back.
    """

    topic: str
    subject_terms: list[str]
    aspect_terms: list[str]
    pubmed_query: str
    plain_query: str

    def query_for(self, source: str) -> str:
        """Return the query form *source* can actually answer."""
        if source == "pubmed":
            return self.pubmed_query or self.plain_query
        if source in _CONJUNCTIVE_SOURCES:
            subject = or_join(self.subject_terms) or or_join([self.plain_query])
            aspect = or_join(self.aspect_terms)
            if not subject:
                return self.plain_query
            return f"({subject}) AND ({aspect})" if aspect else subject
        return self.plain_query


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


def build_focus_profile(topic: str, background: str | None, client, llm_params: dict) -> tuple[FocusProfile, str]:
    """Turn the operator's topic into query forms plus a one-line summary.

    The model is asked to split the topic in two: the terms that identify the
    *subject* (including its English name and the synonyms the literature
    actually uses — a Chinese topic will not match anything otherwise) and
    the terms that describe the *angle*. Only that split lets the subject be
    required rather than merely one option among many.

    A failure degrades to searching the raw topic text: losing the summary is
    cosmetic, losing the search would be the whole feature.
    """
    context = f"\n用户补充的背景：{background}" if background else ""
    prompt = (
        f"用户希望在文献周报里追踪这个主题：{topic}{context}\n\n"
        "请先用一句话（不超过 40 字）概括这个主题在关注什么，再把它拆成两组检索词：\n"
        "- subject_terms：**主题点名的那个研究对象**（药物/分子/蛋白/材料/技术）的英文名称，"
        "以及文献里实际使用的同义词、别名、缩写。这一组是检索时的必要条件，必须填；"
        "中文名不要直接写进去，要给出英文检索词。\n"
        "- aspect_terms：主题关注的**切面**（如动力学、稳定性、纯化、临床疗效）的英文检索词，"
        "不要把研究对象重复写进这一组；主题没有明确切面时给空数组。\n"
        "pubmed_query 请把两组用 AND 连起来（对象组必须出现），只有对象组时就只写对象组。\n\n"
        "只输出 JSON：\n"
        '{"summary":"一句话概括","subject_terms":["..."],"aspect_terms":["..."],'
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
        subject = [str(t).strip() for t in data.get("subject_terms", []) if str(t).strip()]
        profile = FocusProfile(
            topic=topic,
            # Falling back to the topic itself keeps the subject required even
            # when the model forgets to name it.
            subject_terms=subject or [topic],
            aspect_terms=[str(t).strip() for t in data.get("aspect_terms", []) if str(t).strip()],
            pubmed_query=str(data.get("pubmed_query", "")),
            plain_query=str(data.get("plain_query", "")) or topic,
        )
        logger.info(
            f"Focus topic {topic!r}: subject {profile.subject_terms}, aspect {profile.aspect_terms}"
        )
        return profile, str(data.get("summary", "")).strip()
    except Exception as exc:  # noqa: BLE001 - the focus line must degrade, not fail
        logger.warning(f"Could not distil a query profile for {topic!r} ({exc}); searching the topic as given")
        return FocusProfile(topic=topic, subject_terms=[topic], aspect_terms=[], pubmed_query="", plain_query=topic), ""


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
    seen = _SeenSet(exclude_dois)
    kept: list[Paper] = []

    def take(papers: list[Paper], rung: str, wanted: int) -> None:
        """Judge one rung's haul and keep what clears the topic floor."""
        fetched = len(papers)
        fresh = seen.unseen(dedup_papers(papers))
        seen.remember(fresh)
        passed = _keep_relevant(fresh, config, client, settings)[:wanted]
        kept.extend(passed)
        logger.info(
            f"Focus {rung} for {settings.topic!r}: {fetched} fetched -> {len(fresh)} new -> "
            f"{len(passed)} cleared relevance {settings.min_relevance} (wanted {wanted})"
        )

    found: list[Paper] = []
    for source in config.search.sources:
        papers = retriever_for(source).search(
            profile.query_for(source), _ALL_TIME_START, end, limit
        )
        logger.info(f"{source}/焦点主题「{settings.topic}」: {len(papers)} candidates")
        found.extend(papers)
    take(found, "search", settings.max_papers)

    if len(kept) < settings.min_papers:
        # A second pass ranked by citations rather than by each source's own
        # relevance ordering, which surfaces the established work on a topic
        # that a relevance-ranked list can bury.
        shortfall = settings.min_papers - len(kept)
        classics = retriever_for("openalex").search_highly_cited(
            profile.plain_query, max(_MIN_CLASSICS_FETCHED, shortfall * _BACKFILL_OVERSAMPLE)
        )
        for paper in classics:
            paper.is_backfill = True
        take(classics, "classics", shortfall)

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

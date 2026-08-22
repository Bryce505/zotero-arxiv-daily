"""Group the Zotero corpus into themes, and route candidates to them.

The Zotero collection tree mixes methodology themes with project codenames
(KJ103, BJ044), so it cannot be used as a topic label directly.  Instead an
LLM reads the whole corpus once and proposes a handful of themes.  The result
is cached against a corpus fingerprint, so the call happens only when the
library has materially changed.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass

import numpy as np
from loguru import logger

from ..protocol import CorpusPaper, Paper

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_MAX_PROMPT_TITLES = 300  # beyond this the response outgrows max_tokens
_MIN_CACHE_COVERAGE = 0.85


@dataclass
class ThemeCluster:
    name: str
    description: str
    members: list[int]


def corpus_fingerprint(corpus: list[CorpusPaper]) -> str:
    """A stable digest of the corpus, insensitive to ordering."""
    titles = sorted(c.title for c in corpus)
    return hashlib.sha256("\n".join(titles).encode("utf-8")).hexdigest()[:16]


def _sample_indices(size: int, max_titles: int) -> list[int]:
    """Evenly spaced indices spanning the corpus, at most *max_titles* of them."""
    if size <= max_titles:
        return list(range(size))
    stride = size / max_titles
    return sorted({min(int(i * stride), size - 1) for i in range(max_titles)})


def _build_prompt(corpus: list[CorpusPaper], n_clusters: int, sample: list[int]) -> str:
    listing = "\n".join(f"[{local}] {corpus[real].title}" for local, real in enumerate(sample))
    return (
        f"下面是一位生物制药 CMC 分析科学家的文献库，共 {len(sample)} 篇。\n"
        f"请按**分析方法学主题**把它们聚成 {n_clusters} 个簇。\n"
        "注意：库中的分类含项目代号（如 KJ103、BJ044），请忽略项目归属，只按方法学主题聚类。\n"
        "每篇必须且只能归入一个簇。只输出 JSON，不要输出其他内容：\n"
        '{"clusters":[{"name":"簇名","description":"一句话描述","members":[0,3,7]}]}\n\n'
        f"{listing}"
    )


def _parse_clusters(payload: str, corpus_size: int) -> list[ThemeCluster]:
    match = _JSON_BLOCK_RE.search(payload)
    if match is None:
        raise ValueError("no JSON object found in the response")
    data = json.loads(match.group(0))
    clusters = []
    for raw in data["clusters"]:
        members = [int(i) for i in raw.get("members", []) if 0 <= int(i) < corpus_size]
        clusters.append(
            ThemeCluster(
                name=str(raw["name"]),
                description=str(raw.get("description", "")),
                members=members,
            )
        )
    if not clusters:
        raise ValueError("the response contained no clusters")
    return clusters


def _absorb_unassigned(clusters: list[ThemeCluster], corpus_size: int) -> list[ThemeCluster]:
    """Put every corpus paper the model forgot into the largest cluster."""
    covered = {i for c in clusters for i in c.members}
    missing = [i for i in range(corpus_size) if i not in covered]
    if missing:
        logger.warning(
            f"{len(missing)} corpus papers were left unclustered; folding them into the largest cluster"
        )
        largest = max(clusters, key=lambda c: len(c.members))
        largest.members = sorted(largest.members + missing)
    return clusters


def _single_cluster(corpus: list[CorpusPaper]) -> list[ThemeCluster]:
    return [
        ThemeCluster(name="全部", description="未能聚类，全部归为一簇", members=list(range(len(corpus))))
    ]


def _cluster_corpus_strict(
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
    n_clusters: int,
    max_titles: int = _MAX_PROMPT_TITLES,
) -> list[ThemeCluster]:
    """Group *corpus* into themes, raising if the model cannot be parsed.

    A corpus larger than *max_titles* is sampled for the prompt: asking for
    membership of every paper at once outgrows the response token budget, and
    the failure is silent (the digest collapses to one theme).  Papers outside
    the sample are folded into the largest cluster, so a library well beyond
    this size wants a different assignment strategy — see the plan doc.
    """
    sample = _sample_indices(len(corpus), max_titles)
    if len(sample) < len(corpus):
        logger.warning(
            f"Corpus of {len(corpus)} papers exceeds the {max_titles}-title prompt cap; "
            f"clustering on a {len(sample)}-paper sample and folding the rest into the largest theme"
        )
    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": "你是一位生物制药 CMC 分析领域的文献主题归纳专家，只输出 JSON。",
            },
            {"role": "user", "content": _build_prompt(corpus, n_clusters, sample)},
        ],
        **llm_params.get("generation_kwargs", {}),
    )
    clusters = _parse_clusters(response.choices[0].message.content, len(sample))
    for cluster in clusters:
        cluster.members = [sample[i] for i in cluster.members]
    return _absorb_unassigned(clusters, len(corpus))


def cluster_corpus(
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
    n_clusters: int = 5,
    max_titles: int = _MAX_PROMPT_TITLES,
) -> list[ThemeCluster]:
    """Ask the LLM to group *corpus* into themes; never raises."""
    try:
        return _cluster_corpus_strict(corpus, client, llm_params, n_clusters, max_titles)
    except Exception as exc:  # noqa: BLE001 - clustering must never break the run
        logger.warning(f"Corpus clustering failed ({exc}); falling back to a single cluster")
        return _single_cluster(corpus)


def _to_cache(clusters: list[ThemeCluster], corpus: list[CorpusPaper]) -> list[dict]:
    """Serialise membership by title, so it survives a corpus reordering."""
    return [
        {
            "name": c.name,
            "description": c.description,
            "member_titles": [corpus[i].title for i in c.members if i < len(corpus)],
        }
        for c in clusters
    ]


def _cache_coverage(cached: list[dict], corpus: list[CorpusPaper]) -> float:
    """Fraction of the current corpus the cached membership already covers."""
    if not corpus or not cached:
        return 0.0
    known = {t for raw in cached for t in raw.get("member_titles", [])}
    return sum(1 for c in corpus if c.title in known) / len(corpus)


def _from_cache(cached: list[dict], corpus: list[CorpusPaper]) -> list[ThemeCluster]:
    """Resolve cached titles back to positions in *corpus* as fetched."""
    index_of: dict[str, int] = {}
    for i, paper in enumerate(corpus):
        index_of.setdefault(paper.title, i)

    clusters = []
    for raw in cached:
        members = [index_of[t] for t in raw["member_titles"] if t in index_of]
        clusters.append(
            ThemeCluster(name=raw["name"], description=raw.get("description", ""), members=members)
        )
    if not clusters:
        raise ValueError("the cache held no clusters")
    return _absorb_unassigned(clusters, len(corpus))


def load_or_build_clusters(
    path: str,
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
    n_clusters: int = 5,
) -> list[ThemeCluster]:
    """Return cached clusters when the corpus is unchanged, else rebuild."""
    fingerprint = corpus_fingerprint(corpus)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                cached = json.load(handle)
            coverage = _cache_coverage(cached.get("clusters", []), corpus)
            if coverage >= _MIN_CACHE_COVERAGE:
                logger.info(
                    f"Reusing cached theme clusters from {path} "
                    f"({coverage:.0%} of the corpus already assigned)"
                )
                return _from_cache(cached["clusters"], corpus)
            logger.info(f"Cluster cache covers only {coverage:.0%} of the corpus; re-clustering")
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break the run
            logger.warning(f"Ignoring unreadable cluster cache {path}: {exc}")

    try:
        clusters = _cluster_corpus_strict(corpus, client, llm_params, n_clusters)
    except Exception as exc:  # noqa: BLE001 - degrade for this run only
        # Never cache the fallback: the cache is committed and keyed only on
        # the corpus, so one transient API error would collapse the digest to
        # a single theme for good.
        logger.warning(f"Corpus clustering failed ({exc}); using a single cluster for this run only")
        return _single_cluster(corpus)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"fingerprint": fingerprint, "clusters": _to_cache(clusters, corpus)},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"Built {len(clusters)} theme clusters and cached them to {path}")
    return clusters


_DEFAULT_DESCRIPTION_WEIGHT = 0.6


def assign_clusters(
    candidates: list[Paper],
    sim: np.ndarray,
    clusters: list[ThemeCluster],
    desc_sim: np.ndarray | None = None,
    description_weight: float = _DEFAULT_DESCRIPTION_WEIGHT,
) -> None:
    """Route each candidate to the theme its combined signal points at.

    Two signals, blended.  The *mean* similarity over a cluster's corpus
    members is the original signal — using the mean rather than the max so a
    single accidental spike cannot outweigh a consistently close theme — but
    it is diffuse: it averages over every paper the theme happens to
    contain, so a candidate that merely shares surface vocabulary with those
    papers can outscore one that actually matches the theme.  ``desc_sim``,
    the candidate's similarity to each cluster's own one-sentence
    description, is a sharper, deliberately topical signal that corrects
    exactly that case.  It is optional — a caller that cannot produce it
    passes ``None`` and gets the original corpus-only routing.

    ``desc_sim`` must have one column per entry of *clusters* in that same
    order, including any empty ones — its columns are not pre-filtered to
    match ``populated`` below.
    """
    populated = [(i, c) for i, c in enumerate(clusters) if c.members]
    if not populated:
        return
    for row_i, (row, paper) in enumerate(zip(sim, candidates)):
        corpus_scores = [float(np.mean(row[c.members])) for _, c in populated]
        if desc_sim is None:
            combined = corpus_scores
        else:
            combined = [
                (1 - description_weight) * corpus_scores[j] + description_weight * float(desc_sim[row_i, idx])
                for j, (idx, _) in enumerate(populated)
            ]
        paper.cluster = populated[int(np.argmax(combined))][1].name

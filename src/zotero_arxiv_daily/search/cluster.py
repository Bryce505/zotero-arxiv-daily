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


@dataclass
class ThemeCluster:
    name: str
    description: str
    members: list[int]


def corpus_fingerprint(corpus: list[CorpusPaper]) -> str:
    """A stable digest of the corpus, insensitive to ordering."""
    titles = sorted(c.title for c in corpus)
    return hashlib.sha256("\n".join(titles).encode("utf-8")).hexdigest()[:16]


def _build_prompt(corpus: list[CorpusPaper], n_clusters: int) -> str:
    listing = "\n".join(f"[{i}] {c.title}" for i, c in enumerate(corpus))
    return (
        f"下面是一位生物制药 CMC 分析科学家的文献库，共 {len(corpus)} 篇。\n"
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


def cluster_corpus(
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
    n_clusters: int = 5,
) -> list[ThemeCluster]:
    """Ask the LLM to group *corpus* into themes; never raises."""
    try:
        response = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "你是一位生物制药 CMC 分析领域的文献主题归纳专家，只输出 JSON。",
                },
                {"role": "user", "content": _build_prompt(corpus, n_clusters)},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        clusters = _parse_clusters(response.choices[0].message.content, len(corpus))
    except Exception as exc:  # noqa: BLE001 - clustering must never break the run
        logger.warning(f"Corpus clustering failed ({exc}); falling back to a single cluster")
        return [
            ThemeCluster(name="全部", description="未能聚类，全部归为一簇", members=list(range(len(corpus))))
        ]
    return _absorb_unassigned(clusters, len(corpus))


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
            if cached.get("fingerprint") == fingerprint:
                logger.info(f"Reusing cached theme clusters from {path}")
                return _from_cache(cached["clusters"], corpus)
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break the run
            logger.warning(f"Ignoring unreadable cluster cache {path}: {exc}")

    clusters = cluster_corpus(corpus, client, llm_params, n_clusters)
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


def assign_clusters(candidates: list[Paper], sim: np.ndarray, clusters: list[ThemeCluster]) -> None:
    """Route each candidate to the cluster its corpus members sit closest to.

    Uses the *mean* similarity over a cluster's members rather than the max,
    so a single accidental spike cannot outweigh a consistently close theme.
    """
    populated = [c for c in clusters if c.members]
    if not populated:
        return
    for row, paper in zip(sim, candidates):
        means = [float(np.mean(row[c.members])) for c in populated]
        paper.cluster = populated[int(np.argmax(means))].name

"""Turn each theme cluster into the query forms the sources need.

Two waves, following the literature-search methodology: a natural-language
query for the relevance-ranked sources (Crossref, OpenAlex, Europe PMC), and a
quoted boolean query with MeSH terms for PubMed.
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass

from loguru import logger

from ..protocol import CorpusPaper
from .cluster import ThemeCluster

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_SAMPLE_TITLES = 25


@dataclass
class QueryProfile:
    cluster: str
    mesh_terms: list[str]
    free_terms: list[str]
    pubmed_query: str
    plain_query: str


def _cluster_fingerprint(clusters: list[ThemeCluster]) -> str:
    names = "\n".join(sorted(c.name for c in clusters))
    return hashlib.sha256(names.encode("utf-8")).hexdigest()[:16]


def distill_profile(
    cluster: ThemeCluster,
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
) -> QueryProfile:
    """Derive the query forms for one cluster; never raises."""
    titles = [corpus[i].title for i in cluster.members[:_SAMPLE_TITLES] if i < len(corpus)]
    listing = "\n".join(f"- {t}" for t in titles)
    prompt = (
        f"下面是一位生物制药 CMC 分析科学家文献库中「{cluster.name}」主题的代表性文献标题。\n"
        f"主题描述：{cluster.description}\n\n{listing}\n\n"
        "请为这个主题生成检索式，用于在 PubMed / Crossref / OpenAlex 上找同主题的新发表文献。只输出 JSON：\n"
        '{"mesh_terms":["..."],"free_terms":["..."],'
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
        match = _JSON_BLOCK_RE.search(response.choices[0].message.content)
        if match is None:
            raise ValueError("no JSON object found in the response")
        data = json.loads(match.group(0))
        return QueryProfile(
            cluster=cluster.name,
            mesh_terms=[str(t) for t in data.get("mesh_terms", [])],
            free_terms=[str(t) for t in data.get("free_terms", [])],
            pubmed_query=str(data.get("pubmed_query", "")),
            plain_query=str(data.get("plain_query", "")) or cluster.name,
        )
    except Exception as exc:  # noqa: BLE001 - distillation must never break the run
        logger.warning(f"Query distillation failed for cluster {cluster.name} ({exc}); using the cluster name")
        plain = f"{cluster.name} {cluster.description}".strip()
        return QueryProfile(cluster=cluster.name, mesh_terms=[], free_terms=[], pubmed_query="", plain_query=plain)


def load_or_build_profiles(
    path: str,
    clusters: list[ThemeCluster],
    corpus: list[CorpusPaper],
    client,
    llm_params: dict,
) -> list[QueryProfile]:
    """Return cached profiles when the cluster set is unchanged, else rebuild."""
    fingerprint = _cluster_fingerprint(clusters)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                cached = json.load(handle)
            if cached.get("fingerprint") == fingerprint:
                logger.info(f"Reusing cached query profiles from {path}")
                return [QueryProfile(**p) for p in cached["profiles"]]
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break the run
            logger.warning(f"Ignoring unreadable profile cache {path}: {exc}")

    profiles = [distill_profile(c, corpus, client, llm_params) for c in clusters]
    if any(not p.pubmed_query for p in profiles):
        # A cached empty pubmed_query would silently disable PubMed for that
        # theme forever, and the cache is committed. Degrade for this run only.
        logger.warning("At least one query profile degraded; not caching this round")
        return profiles

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"fingerprint": fingerprint, "profiles": [asdict(p) for p in profiles]},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"Distilled {len(profiles)} query profiles and cached them to {path}")
    return profiles

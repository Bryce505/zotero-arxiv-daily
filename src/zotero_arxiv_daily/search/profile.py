"""Turn each theme cluster into the query forms the sources need.

Three forms, because the sources do not read a query the same way: a quoted
boolean query with MeSH terms for PubMed, a natural-language query for
Crossref's relevance ranking, and the free terms OR'd together for Europe PMC
and OpenAlex, which AND the words of a query and answer a long one with
nothing at all.  See ``query_for_source``.
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


# Europe PMC and OpenAlex AND the terms of a query together, so the long
# natural-language query that suits Crossref's relevance ranking asks them for
# a record containing all twenty-odd words and they return nothing at all.
# Measured on the first live run: Crossref 65 hits, these two 0 across every
# cluster.  They get the free terms OR'd instead.
_CONJUNCTIVE_SOURCES = frozenset({"europepmc", "openalex"})
_MAX_OR_TERMS = 12


def or_join(terms: list[str]) -> str:
    """Quote and OR *terms* into one boolean clause."""
    quoted = [f'"{t.strip()}"' for t in terms if t and t.strip()]
    return " OR ".join(quoted[:_MAX_OR_TERMS])


def query_for_source(profile: QueryProfile, source: str) -> str:
    """Return the query form *source* can actually answer.

    Falls back to the natural-language query whenever the preferred form is
    missing, so a degraded profile still searches rather than searching for an
    empty string.
    """
    if source == "pubmed":
        return profile.pubmed_query or profile.plain_query
    if source in _CONJUNCTIVE_SOURCES:
        return or_join(profile.free_terms) or profile.plain_query
    return profile.plain_query


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


def alternate_queries(
    profiles: list[QueryProfile],
    tried: dict[str, list[str]],
    client,
    llm_params: dict,
) -> dict[str, str]:
    """Return one *differently worded* query per theme; never raises.

    Backfill's second and third rounds exist because re-running a query that
    already came back short returns the same papers again.  What moves the
    needle is a different angle on the same theme — a synonym set, the
    adjacent technique, the broader or narrower concept — which is the same
    move a systematic review makes when a search under-covers a subtopic.

    ``tried`` maps a theme to every query already spent on it, and goes into
    the prompt verbatim so the model can avoid repeating itself.  Anything it
    returns that names an unknown theme, or repeats a query already tried, is
    dropped: a repeat would burn a round for nothing.  A failure returns an
    empty mapping, which the caller reads as "no more rounds" — degrading to
    the single-round behaviour backfill had before rounds existed.
    """
    if not profiles:
        return {}
    known = {p.cluster for p in profiles}
    listing = "\n".join(
        f"- {p.cluster}：{'；'.join(tried.get(p.cluster) or []) or '（尚未检索）'}" for p in profiles
    )
    prompt = (
        "下面是一位生物制药 CMC 分析科学家文献库的几个主题，以及每个主题已经用过的检索式。\n"
        "这些检索式返回的文献不够，请为每个主题另出一条**换过用词**的英文检索式：\n"
        "改用同义词、相邻方法学、上位或下位概念，覆盖同一主题的不同表述，"
        "不要与已用过的检索式雷同，也不要偏离该主题。\n\n"
        f"{listing}\n\n"
        '只输出 JSON 对象，键是上面列出的主题名称，值是新的检索式：{"主题名":"new query"}'
    )
    try:
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "你是一位医药文献检索专家，只输出 JSON 对象。"},
                {"role": "user", "content": prompt},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        match = _JSON_BLOCK_RE.search(response.choices[0].message.content or "")
        if match is None:
            raise ValueError("no JSON object found in the response")
        data = json.loads(match.group(0))
    except Exception as exc:  # noqa: BLE001 - a retry must never cost the digest
        logger.warning(f"Could not generate alternate backfill queries ({exc}); stopping after this round")
        return {}

    fresh: dict[str, str] = {}
    for cluster, query in (data or {}).items():
        name, text = str(cluster).strip(), str(query or "").strip()
        if name in known and text and text not in (tried.get(name) or []):
            fresh[name] = text
    return fresh

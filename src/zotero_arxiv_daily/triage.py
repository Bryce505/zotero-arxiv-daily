"""Judge how relevant a candidate is to biologics CMC analysis, cheaply.

The digest used to have a ranking and a quota but no relevance floor, so
five themes times five slots had to be filled from the tail of the candidate
list — which is how a paper on sodium-ion battery anodes and one on H+/H2
collision cross-sections reached a CMC reading list.

Embedding similarity cannot close that hole: it answers "does this look like
the Zotero library", and a library full of chromatography methods scores any
separation paper highly whatever the analyte is.  So a small LLM pass reads
the title and abstract and answers the question that actually matters.

It reads abstracts, not full text, and goes out in batches, which keeps it
roughly an order of magnitude cheaper than the extraction pass it gates.
"""

import json
import re
from dataclasses import dataclass, field

from loguru import logger
from tqdm import tqdm

from .protocol import Paper
from .utils import truncate_for_prompt

_JSON_ARRAY_RE = re.compile(r"\[.*\]", flags=re.DOTALL)
_MAX_ABSTRACT_TOKENS = 400
_BATCH_ATTEMPTS = 2

_RUBRIC = """你是一位生物制药 CMC 分析领域的资深专家。请判断每篇文献与「生物药 CMC 分析」的相关程度。

生物药指：重组蛋白、单克隆抗体、双特异性抗体、多抗、抗体偶联药物（ADC）、融合蛋白、疫苗、病毒载体（AAV 等）、细胞与基因治疗产品。

评分标准：
- 80-100：研究对象本身就是上述生物药，讨论其表征、纯化、质量控制、稳定性或工艺。
- 55-79：研究对象不是生物药，但方法学可直接迁移到生物药表征。例如完整蛋白 top-down 测序、
  糖基化位点分析、天然质谱、电荷变异体分离、聚集体分析、宿主细胞蛋白检测。
- 20-54：仅有名词或技术重合，迁移需要重新开发。例如小分子药物的色谱方法、临床诊断试剂、
  兽医检测、环境或食品分析。
- 0-19：与生物大分子表征没有任何联系。例如电池材料、等离子体物理、地质、天文。

以下五类是必须给低分的典型：钠离子电池电极、等离子体碰撞截面、小分子仿制药的 RP-HPLC 方法学、
兽医用 ELISA 或胶体金试纸、临床诊断生物标志物（如牙周病 CRP）。它们即使用到色谱或质谱，
也与生物药 CMC 无关。

reason 用一句话说清「为什么值得读」或「为什么无关」，不超过 40 字。
modalities 只填上面列出的生物药类型，没有就填空数组。"""

_OUTPUT_SPEC = """只输出 JSON 数组，每篇一项，键名完全一致：
[{"index": 1, "relevance": 0-100 的整数, "reason": "一句话", "modalities": ["ADC"]}]"""


@dataclass
class TriageResult:
    relevance: int
    reason: str
    modalities: list[str] = field(default_factory=list)


def _build_prompt(batch: list[Paper]) -> str:
    entries = "\n\n".join(
        f"[{i}] 标题：{paper.title}\n摘要：{truncate_for_prompt(paper.abstract or '', _MAX_ABSTRACT_TOKENS)}"
        for i, paper in enumerate(batch, 1)
    )
    return f"{_RUBRIC}\n\n{_OUTPUT_SPEC}\n\n待判定文献（共 {len(batch)} 篇）：\n\n{entries}"


def _parse_rows(content: str, count: int) -> dict[int, TriageResult]:
    """Turn one response into {1-based index: TriageResult}; raises if unusable."""
    match = _JSON_ARRAY_RE.search(content or "")
    if match is None:
        raise ValueError("no JSON array found in the response")
    results: dict[int, TriageResult] = {}
    for row in json.loads(match.group(0)):
        try:
            index = int(row["index"])
            relevance = int(row["relevance"])
        except (KeyError, TypeError, ValueError):
            # One unusable row must not discard the rest of the batch.
            logger.debug(f"Discarding an unparseable triage row: {row!r}")
            continue
        if not 1 <= index <= count:
            logger.debug(f"Discarding a triage row indexed {index}, outside 1..{count}")
            continue
        modalities = row.get("modalities") or []
        results[index] = TriageResult(
            relevance=max(0, min(100, relevance)),
            reason=str(row.get("reason") or ""),
            modalities=[str(m) for m in modalities if str(m).strip()],
        )
    return results


def _triage_batch(batch: list[Paper], client, llm_params: dict) -> dict[int, TriageResult]:
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": "你只输出 JSON 数组，不输出任何解释。"},
            {"role": "user", "content": _build_prompt(batch)},
        ],
        **llm_params.get("generation_kwargs", {}),
    )
    return _parse_rows(response.choices[0].message.content, len(batch))


def _assign(batch: list[Paper], results: dict[int, TriageResult]) -> None:
    for index, paper in enumerate(batch, 1):
        paper.triage = results.get(index)


def triage_papers(papers: list[Paper], client, llm_params: dict, batch_size: int = 8) -> None:
    """Fill ``paper.triage`` for every paper; never raises.

    A paper left with ``triage is None`` has not been judged, and the gate
    treats that as "did not pass" rather than waving it through — letting an
    unjudged paper past would turn the gate into decoration on exactly the
    run where the LLM is misbehaving.
    """
    batch_size = max(1, int(batch_size))
    batches = [papers[i:i + batch_size] for i in range(0, len(papers), batch_size)]
    for batch in tqdm(batches, desc="Triaging candidates"):
        for attempt in range(1, _BATCH_ATTEMPTS + 1):
            try:
                _assign(batch, _triage_batch(batch, client, llm_params))
                break
            except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the run
                logger.warning(f"Triage batch attempt {attempt}/{_BATCH_ATTEMPTS} failed: {exc}")
        else:
            logger.warning(f"Falling back to one triage call per paper for {len(batch)} papers")
            for paper in batch:
                try:
                    paper.triage = _triage_batch([paper], client, llm_params).get(1)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Triage failed for {paper.title!r}: {exc}")
                    paper.triage = None

    unjudged = sum(1 for p in papers if p.triage is None)
    if unjudged:
        logger.warning(f"{unjudged}/{len(papers)} candidates left unjudged; they will not be delivered")

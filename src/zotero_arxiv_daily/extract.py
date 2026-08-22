"""Turn a paper into the digest's structured fields.

Which fields exist is a configuration question, not a code question: the
report's ``fields`` list drives both the prompt and the rendered output, so
adding "洞见" or dropping "方法" is a YAML edit.
"""

import json
import re
from dataclasses import dataclass

from loguru import logger
from tqdm import tqdm

from .protocol import Paper
from .utils import truncate_for_prompt

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)
_MAX_SOURCE_TOKENS = 6000


@dataclass
class FieldSpec:
    key: str
    label: str
    instruction: str
    kind: str = "text"      # "text" | "list"
    max_items: int = 0      # only meaningful for "list"; 0 means no limit


@dataclass(frozen=True)
class ListItem:
    point: str
    detail: str


def load_field_specs(config) -> list[FieldSpec]:
    """Read the configured report fields."""
    specs = []
    for raw in config.report.fields:
        label = str(raw["label"])
        specs.append(
            FieldSpec(
                key=str(raw["key"]),
                label=label,
                instruction=str(raw.get("instruction") or label),
                kind=str(raw.get("kind") or "text"),
                max_items=int(raw.get("max_items") or 0),
            )
        )
    return specs


def normalize_list_value(raw, max_items: int) -> list[ListItem]:
    """Coerce whatever the model returned into a list of point/detail items.

    Models are inconsistent about this shape — sometimes objects, sometimes
    bare strings, sometimes one long string — and a digest is worth more
    than a schema argument, so every shape is accepted.
    """
    if not raw:
        return []
    rows = raw if isinstance(raw, list) else [raw]
    items = []
    for row in rows:
        if isinstance(row, dict):
            point = str(row.get("point") or "").strip()
            detail = str(row.get("detail") or "").strip()
        else:
            point, detail = "", str(row).strip()
        if point or detail:
            items.append(ListItem(point=point, detail=detail))
    return items[:max_items] if max_items > 0 else items


def _modality_note(paper: Paper) -> str:
    """Tell the extractor which biologic the gate said this paper touches."""
    modalities = list(getattr(paper.triage, "modalities", None) or [])
    if modalities:
        return (
            f"本文经判定与以下生物药类型相关：{'、'.join(modalities)}。"
            "洞见必须围绕这些类型展开，说明该方法或发现可以怎样用在这类产品的 CMC 分析上。\n\n"
        )
    if paper.triage is not None:
        return (
            "本文的研究对象不是生物药，但方法学被判定可迁移到生物药表征。"
            "洞见要讲清迁移到重组蛋白、抗体、双抗、ADC 等产品上的具体路径与前提限制。\n\n"
        )
    return ""


def _schema_fragment(spec: FieldSpec) -> str:
    if spec.kind == "list":
        limit = f"，最多 {spec.max_items} 条" if spec.max_items else ""
        return f'"{spec.key}":[{{"point":"关键词","detail":"{spec.instruction}{limit}"}}]'
    return f'"{spec.key}":"{spec.instruction}"'


def _build_prompt(paper: Paper, fields: list[FieldSpec], language: str) -> str:
    body = paper.full_text or paper.abstract or ""
    schema = ",".join(_schema_fragment(f) for f in fields)
    labels = "\n".join(
        f"- {f.key}（{f.label}）：{f.instruction}"
        + ("　【本字段输出数组，每项含 point 与 detail】" if f.kind == "list" else "")
        for f in fields
    )
    return (
        f"请阅读下面这篇文献，并用{language}逐项作答。\n\n"
        f"{_modality_note(paper)}"
        f"需要提取的字段：\n{labels}\n\n"
        f"只输出 JSON，键名必须与上面完全一致：\n{{{schema}}}\n\n"
        f"标题：{paper.title}\n\n"
        f"正文：\n{truncate_for_prompt(body, _MAX_SOURCE_TOKENS)}"
    )


def _empty_value(spec: FieldSpec):
    return [] if spec.kind == "list" else ""


def extract_paper(paper: Paper, client, llm_params: dict, fields: list[FieldSpec]) -> dict:
    """Extract the configured fields for one paper; never raises."""
    language = llm_params.get("language", "中文")
    empty = {f.key: _empty_value(f) for f in fields}
    try:
        response = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": f"你是一位生物制药 CMC 分析领域的文献分析专家，用{language}作答，只输出 JSON。",
                },
                {"role": "user", "content": _build_prompt(paper, fields, language)},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        match = _JSON_BLOCK_RE.search(response.choices[0].message.content)
        if match is None:
            raise ValueError("no JSON object found in the response")
        data = json.loads(match.group(0))
    except Exception as exc:  # noqa: BLE001 - a bad extraction must not lose the paper
        logger.warning(f"Extraction failed for {paper.title!r}: {exc}")
        return empty
    return {
        f.key: (
            normalize_list_value(data.get(f.key), f.max_items)
            if f.kind == "list"
            else str(data.get(f.key, "") or "")
        )
        for f in fields
    }


def extract_all(papers: list[Paper], client, llm_params: dict, fields: list[FieldSpec]) -> None:
    """Populate ``paper.extraction`` for every paper, in place."""
    for paper in tqdm(papers, desc="Extracting fields"):
        paper.extraction = extract_paper(paper, client, llm_params, fields)

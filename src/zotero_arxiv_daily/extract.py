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
            )
        )
    return specs


def _build_prompt(paper: Paper, fields: list[FieldSpec], language: str) -> str:
    body = paper.full_text or paper.abstract or ""
    schema = ",".join(f'"{f.key}":"{f.instruction}"' for f in fields)
    labels = "\n".join(f"- {f.key}（{f.label}）：{f.instruction}" for f in fields)
    return (
        f"请阅读下面这篇文献，并用{language}逐项作答。\n\n"
        f"需要提取的字段：\n{labels}\n\n"
        f"只输出 JSON，键名必须与上面完全一致，值为字符串：\n{{{schema}}}\n\n"
        f"标题：{paper.title}\n\n"
        f"正文：\n{truncate_for_prompt(body, _MAX_SOURCE_TOKENS)}"
    )


def extract_paper(paper: Paper, client, llm_params: dict, fields: list[FieldSpec]) -> dict[str, str]:
    """Extract the configured fields for one paper; never raises."""
    language = llm_params.get("language", "中文")
    empty = {f.key: "" for f in fields}
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
    return {f.key: str(data.get(f.key, "") or "") for f in fields}


def extract_all(papers: list[Paper], client, llm_params: dict, fields: list[FieldSpec]) -> None:
    """Populate ``paper.extraction`` for every paper, in place."""
    for paper in tqdm(papers, desc="Extracting fields"):
        paper.extraction = extract_paper(paper, client, llm_params, fields)

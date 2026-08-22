# 周报相关性闸门与结构化改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在周报流水线的「打分」与「配额」之间插入一道相关性闸门，让不相关文献根本进不了周报；同时把报告字段做成有类型的（文本 / 有序列表），并给每篇标注相关度与推荐理由。

**Architecture:** 采用检索领域标准的 retrieve → rerank → read 漏斗。新增一个只读标题+摘要的廉价 LLM 分诊阶段，产出 0–100 相关度、一句话推荐理由与命中的生物药类型；随后按「相关度硬下限」与「综合分（相关度 + 期刊加分 + 企业加分）」两道闸门筛选；配额只在过闸文献中分配，凑不满则由既有的高被引经典补位承接。

**Tech Stack:** Python ≥3.13 · Hydra + OmegaConf · openai SDK（指向 DeepSeek 兼容端点）· numpy · pytest（纯 stub，无 Docker）

**Spec:** `docs/superpowers/specs/2026-08-22-digest-relevance-and-structure-design.md`

## Global Constraints

- Python `requires-python = ">=3.13"`；**不新增任何依赖**
- 测试禁用 `unittest.mock`；一律 `pytest monkeypatch` + `SimpleNamespace` + `tests/canned_responses.py`（沿用 `tests/conftest.py` 既有约定）
- 本地跑测试用 `.venv/bin/pytest`，**不要用 `uv run`**——沙箱内 `download.pytorch.org` 被出口策略拒绝，`uv sync` 会失败
- 既有 421 项测试必须保持通过。`tests/test_protocol.py` 中 3 项 tiktoken 用例在沙箱内失败（`openaipublic.blob.core.windows.net` 被出口策略拒绝），属**既有状态**，与本改动无关，不要试图修
- 一个部件失败不得拖垮整轮运行：所有 LLM 调用、所有外部源解析都必须 `try/except` 并降级，绝不向上抛
- 长名单与报告字段一律落在 `config/base.yaml`；**不要**放进 `config/custom.yaml`（CI 会用 `CUSTOM_CONFIG` 整个覆写它，且 OmegaConf 对列表是整体替换）
- 提交信息用英文，正文说清「为什么」；**任何提交产物中不得出现模型标识**
- 全部工作在分支 `claude/pharma-literature-automation-kyerjk` 上进行

**与 spec 的一处命名偏差（有意）：** spec §6.3 写作 `is_industry(paper, names) -> str | None`。`is_` 前缀暗示返回布尔，与实际返回命中的公司名不符。本计划统一用 `match_industry`，与 `match_journal` 对称。Task 1 完成时**顺手把 spec 那一行改过来**，避免两份文档长期不一致。

---

## File Structure

**新增**

| 文件 | 职责 |
| --- | --- |
| `src/zotero_arxiv_daily/affiliation.py` | 名称归一化与名单匹配。纯函数，只依赖 `protocol` |
| `src/zotero_arxiv_daily/triage.py` | LLM 相关性分诊：批量协议、解析、降级 |
| `src/zotero_arxiv_daily/scoring.py` | 综合分计算与两道闸门。纯函数 |
| `tests/test_affiliation.py` · `tests/test_triage.py` · `tests/test_scoring.py` | 对应测试 |

**修改**

| 文件 | 改动 |
| --- | --- |
| `protocol.py` | `Paper` 增 `institutions` / `company_institutions` / `triage` / `scoring` |
| `retriever/{openalex,pubmed,europepmc,crossref}_retriever.py` | 填充作者单位 |
| `extract.py` | `FieldSpec.kind/max_items`；list 值归一化；提示词携带分诊结论 |
| `report.py` | 三个渲染器支持有序列表与徽标行 |
| `weekly.py` | 插入分诊与闸门阶段（位于配额之前） |
| `backfill.py` | 补位候选走同一道闸 |
| `preflight.py` | 新增 `check_report_config` |
| `config/base.yaml` | 新增 `report` 下的闸门与名单键；重写 `fields` |
| `README.md` · `docs/cmc-weekly-setup.md` | 重写 / 更新 |

**依赖顺序**：Task 1–3 互不依赖，可并行；Task 4 依赖 1 与 3；Task 5–6 依赖 3；Task 7 依赖全部；Task 8–9 收尾。

---

## Task 1: 名单匹配 `affiliation.py`

**Files:**
- Create: `src/zotero_arxiv_daily/affiliation.py`
- Test: `tests/test_affiliation.py`
- Modify: `docs/superpowers/specs/2026-08-22-digest-relevance-and-structure-design.md`（§6.3 的 `is_industry` 改名为 `match_industry`）

**Interfaces:**
- Consumes: 无
- Produces:
  - `normalize(text: str) -> str`
  - `match_name(text: str, names: list[str]) -> str | None`
  - `match_journal(journal: str | None, names) -> str | None`
  - `match_industry(institutions: list[str], company_institutions: list[str], names) -> str | None`

`match_industry` 接收两个列表而非整个 `Paper`，让本模块不依赖 `protocol`，也让测试不必构造 `Paper`。

- [ ] **Step 1: 写失败的测试**

`tests/test_affiliation.py`：

```python
"""Journal and company name matching."""

from zotero_arxiv_daily.affiliation import (
    match_industry,
    match_journal,
    match_name,
    normalize,
)

JOURNALS = [
    "Molecular and Cellular Proteomics",
    "Biotechnology and Bioengineering",
    "Journal of Biological Chemistry",
    "mAbs",
    "Analytical Chemistry",
]


def test_normalize_lowercases_and_strips_punctuation():
    assert normalize("Molecular & cellular proteomics : MCP") == "molecular cellular proteomics mcp"


def test_normalize_drops_the_and_and():
    assert normalize("The Journal of Biological Chemistry") == "journal of biological chemistry"
    assert normalize("Biotechnology and Bioengineering") == "biotechnology bioengineering"


def test_normalize_keeps_of():
    assert normalize("Journal of Chromatography A") == "journal of chromatography a"


# The three rows of spec 6.2: each of these silently failed to match before
# `the`/`and` were dropped, and each is a journal the maintainer reads.
def test_ampersand_title_matches_the_and_form():
    assert match_journal("Molecular & cellular proteomics : MCP", JOURNALS) == (
        "Molecular and Cellular Proteomics"
    )


def test_and_in_journal_matches_and_in_entry():
    assert match_journal("Biotechnology and Bioengineering", JOURNALS) == (
        "Biotechnology and Bioengineering"
    )


def test_leading_the_does_not_block_the_match():
    assert match_journal("The Journal of biological chemistry", JOURNALS) == (
        "Journal of Biological Chemistry"
    )


def test_case_difference_does_not_block_the_match():
    assert match_journal("MAbs", JOURNALS) == "mAbs"


def test_short_entry_does_not_match_inside_a_longer_word():
    assert match_journal("Journal of Mabsorption Studies", JOURNALS) is None


def test_word_sequence_must_be_contiguous():
    assert match_journal("Journal of Pharmaceutical and Biomedical Analysis", ["Journal of Pharmaceutical Analysis"]) is None


def test_missing_journal_is_not_an_error():
    assert match_journal(None, JOURNALS) is None
    assert match_journal("", JOURNALS) is None


def test_empty_name_list_matches_nothing():
    assert match_journal("Analytical Chemistry", []) is None


def test_match_name_returns_the_list_entry_not_the_text():
    # The badge shows the curated name, not whatever the source happened to print.
    assert match_name("analytical chemistry letters", ["Analytical Chemistry"]) == "Analytical Chemistry"


def test_industry_matches_a_named_company():
    assert match_industry(["Amgen Inc., Thousand Oaks, CA"], [], ["Amgen", "Pfizer"]) == "Amgen"


def test_industry_falls_back_to_a_source_flagged_company():
    # OpenAlex says type == "company" for a firm nobody put on the list.
    assert match_industry(["Genentech"], ["Genentech"], ["Amgen"]) == "Genentech"


def test_named_company_wins_over_the_source_flag():
    assert match_industry(["Amgen", "Genentech"], ["Genentech"], ["Amgen"]) == "Amgen"


def test_academic_affiliation_is_not_industry():
    assert match_industry(["Tsinghua University"], [], ["Amgen"]) is None


def test_institutions_are_matched_one_by_one():
    # Joining them would let "Amgen Pfizer" match across a boundary that does
    # not exist in any single affiliation.
    assert match_industry(["Amgen Research", "Pfizer Ltd"], [], ["Amgen Pfizer"]) is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_affiliation.py -q
```

预期：`ModuleNotFoundError: No module named 'zotero_arxiv_daily.affiliation'`

- [ ] **Step 3: 写最小实现**

`src/zotero_arxiv_daily/affiliation.py`：

```python
"""Match journal titles and author affiliations against curated name lists.

Journal titles arrive in wildly different shapes: ``Molecular & cellular
proteomics : MCP`` from PubMed, ``Molecular and Cellular Proteomics`` from a
hand-written list, ``The Journal of biological chemistry`` with a leading
article.  Mapping punctuation to spaces is not enough — ``&`` leaves a gap
where the list entry writes ``and``, so the two sides land one token apart
and never match.  Dropping ``the`` and ``and`` from both sides closes that
gap without loosening anything else.

``of`` is deliberately kept: it appears on both sides of every ``Journal of
X`` title, so dropping it would only widen the false-positive surface.
"""

import re

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
# Dropped from both sides, so this can only widen a match, never create a
# cross-boundary one.
_DROPPED = frozenset({"the", "and"})


def normalize(text: str) -> str:
    """Lowercase *text*, drop punctuation and the articles that vary by source."""
    tokens = _NON_ALNUM_RE.sub(" ", (text or "").lower()).split()
    return " ".join(t for t in tokens if t not in _DROPPED)


def match_name(text: str, names: list[str]) -> str | None:
    """Return the first entry of *names* occurring as a whole word sequence."""
    haystack = f" {normalize(text)} "
    if haystack == "  ":
        return None
    for name in names or []:
        needle = normalize(name)
        if needle and f" {needle} " in haystack:
            return name
    return None


def match_journal(journal: str | None, names: list[str]) -> str | None:
    """Return the curated journal name this *journal* string matches."""
    return match_name(journal or "", names)


def match_industry(
    institutions: list[str],
    company_institutions: list[str],
    names: list[str],
) -> str | None:
    """Return the company behind a paper, or None when it looks academic.

    Two independent signals: a name the operator curated, or an institution
    the retrieval source itself flagged as a company.  The curated list wins
    so the badge shows the name the operator recognises.
    """
    for institution in institutions or []:
        hit = match_name(institution, names)
        if hit:
            return hit
    return next(iter(company_institutions or []), None)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_affiliation.py -q
```

预期：17 passed

- [ ] **Step 5: 同步 spec 的命名**

把 spec §6.3 的 `def is_industry(paper: Paper, names: list[str]) -> str | None:` 改成：

```python
def match_industry(
    institutions: list[str],
    company_institutions: list[str],
    names: list[str],
) -> str | None:
```

并在该段补一句：接收两个列表而非整个 `Paper`，使本模块不反向依赖 `protocol`。

- [ ] **Step 6: 提交**

```bash
git add src/zotero_arxiv_daily/affiliation.py tests/test_affiliation.py docs/superpowers/specs/
git commit -m "feat: match journals and companies against curated name lists

Mapping punctuation to spaces is not enough for journal titles. PubMed
prints 'Molecular & cellular proteomics : MCP' while a hand-written list
says 'Molecular and Cellular Proteomics'; the ampersand normalises to
nothing and the entry keeps 'and', so the two land one token apart and
never match. The same silently broke 'Biotechnology and Bioengineering'
and every title carrying a leading 'The'. Dropping both articles from
both sides closes the gap, and those three cases are now regression
tests rather than a footnote."
```

---

## Task 2: 作者单位进入 `Paper`

四个查询式检索源**目前一个都没有取作者单位**。`Paper.affiliations` 存在，但只有 `generate_affiliations()` 一条从全文提取的路径，而全文大概率拿不到（首期 25 篇里 24 篇没有全文）。企业加分要落地，单位必须从检索源元数据直接来。

**Files:**
- Modify: `src/zotero_arxiv_daily/protocol.py`（`Paper` 加两个字段）
- Modify: `src/zotero_arxiv_daily/retriever/openalex_retriever.py`
- Modify: `src/zotero_arxiv_daily/retriever/pubmed_retriever.py`
- Modify: `src/zotero_arxiv_daily/retriever/europepmc_retriever.py`
- Modify: `src/zotero_arxiv_daily/retriever/crossref_retriever.py`
- Test: `tests/retriever/test_institutions.py`

**Interfaces:**
- Consumes: 无
- Produces: `Paper.institutions: list[str]`、`Paper.company_institutions: list[str]`（后者是前者的子集，仅 OpenAlex 能填）

- [ ] **Step 1: 写失败的测试**

`tests/retriever/test_institutions.py`：

```python
"""Author affiliations lifted from each source's own metadata."""

from xml.etree import ElementTree

from zotero_arxiv_daily.retriever.crossref_retriever import CrossrefRetriever
from zotero_arxiv_daily.retriever.europepmc_retriever import EuropepmcRetriever
from zotero_arxiv_daily.retriever.openalex_retriever import OpenalexRetriever
from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever

OPENALEX_WORK = {
    "title": "A paper",
    "doi": "https://doi.org/10.1000/x",
    "abstract_inverted_index": {"An": [0], "abstract": [1]},
    "publication_date": "2026-08-18",
    "authorships": [
        {
            "author": {"display_name": "A Researcher"},
            "institutions": [
                {"display_name": "Amgen Inc.", "type": "company"},
                {"display_name": "Stanford University", "type": "education"},
            ],
        }
    ],
}


def test_openalex_records_every_institution(config):
    paper = OpenalexRetriever(config)._to_paper(OPENALEX_WORK, is_backfill=False)
    assert paper.institutions == ["Amgen Inc.", "Stanford University"]


def test_openalex_flags_only_the_companies(config):
    paper = OpenalexRetriever(config)._to_paper(OPENALEX_WORK, is_backfill=False)
    assert paper.company_institutions == ["Amgen Inc."]


def test_openalex_survives_a_work_with_no_institutions(config):
    work = dict(OPENALEX_WORK, authorships=[{"author": {"display_name": "A"}}])
    paper = OpenalexRetriever(config)._to_paper(work, is_backfill=False)
    assert paper.institutions == []
    assert paper.company_institutions == []


def test_openalex_deduplicates_repeated_institutions(config):
    work = dict(
        OPENALEX_WORK,
        authorships=[OPENALEX_WORK["authorships"][0], OPENALEX_WORK["authorships"][0]],
    )
    paper = OpenalexRetriever(config)._to_paper(work, is_backfill=False)
    assert paper.institutions == ["Amgen Inc.", "Stanford University"]


PUBMED_XML = """
<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
  <ArticleTitle>A paper</ArticleTitle>
  <Abstract><AbstractText>An abstract.</AbstractText></Abstract>
  <Journal><Title>mAbs</Title></Journal>
  <AuthorList>
    <Author><LastName>Doe</LastName><ForeName>Jane</ForeName>
      <AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>
    </Author>
    <Author><LastName>Roe</LastName><ForeName>Ann</ForeName>
      <AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>
    </Author>
  </AuthorList>
</Article></MedlineCitation></PubmedArticle>
"""


def test_pubmed_records_affiliations_once(config):
    article = ElementTree.fromstring(PUBMED_XML)
    paper = PubmedRetriever(config)._article_to_paper(article)
    assert paper.institutions == ["Pfizer Inc., New York, NY."]


def test_pubmed_survives_an_article_with_no_affiliation(config):
    stripped = PUBMED_XML.replace(
        "<AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>", ""
    )
    paper = PubmedRetriever(config)._article_to_paper(ElementTree.fromstring(stripped))
    assert paper.institutions == []


def test_europepmc_records_the_affiliation(config):
    item = {
        "title": "A paper",
        "abstractText": "An abstract.",
        "authorString": "Doe J",
        "id": "1",
        "journalTitle": "mAbs",
        "affiliation": "Lonza AG, Basel, Switzerland",
    }
    assert EuropepmcRetriever(config)._to_paper(item).institutions == ["Lonza AG, Basel, Switzerland"]


def test_europepmc_survives_a_missing_affiliation(config):
    item = {"title": "A paper", "abstractText": "An abstract.", "authorString": "Doe J", "id": "1"}
    assert EuropepmcRetriever(config)._to_paper(item).institutions == []


def test_crossref_records_author_affiliations(config):
    item = {
        "title": ["A paper"],
        "abstract": "<jats:p>An abstract.</jats:p>",
        "DOI": "10.1000/x",
        "author": [{"family": "Doe", "given": "Jane", "affiliation": [{"name": "Amgen Inc."}]}],
        "container-title": ["mAbs"],
    }
    assert CrossrefRetriever(config)._to_paper(item).institutions == ["Amgen Inc."]


def test_crossref_survives_authors_with_no_affiliation(config):
    item = {
        "title": ["A paper"],
        "abstract": "<jats:p>An abstract.</jats:p>",
        "DOI": "10.1000/x",
        "author": [{"family": "Doe", "given": "Jane"}],
        "container-title": ["mAbs"],
    }
    assert CrossrefRetriever(config)._to_paper(item).institutions == []


def test_no_source_populates_company_institutions_except_openalex(config):
    item = {"title": "A paper", "abstractText": "An abstract.", "authorString": "Doe J", "id": "1",
            "affiliation": "Lonza AG"}
    # Only OpenAlex reports an institution *type*; the others cannot know.
    assert EuropepmcRetriever(config)._to_paper(item).company_institutions == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/retriever/test_institutions.py -q
```

预期：`AttributeError: 'Paper' object has no attribute 'institutions'`

- [ ] **Step 3: 给 `Paper` 加字段**

`src/zotero_arxiv_daily/protocol.py`——在 `cited_by_count` 一行之后加：

```python
    # Affiliations as the retrieval source itself reported them.  Distinct
    # from `affiliations`, which the LLM extracts from full text and which is
    # empty for nearly every paper because the full text is paywalled.
    institutions: list[str] = field(default_factory=list)
    # The subset OpenAlex flagged `type == "company"`.  No other source
    # reports an institution type, so for them this stays empty.
    company_institutions: list[str] = field(default_factory=list)
```

同一文件顶部的 import 改为：

```python
from dataclasses import dataclass, field
```

- [ ] **Step 4: 四个检索器各自填充**

`openalex_retriever.py`——在 `_to_paper` 里 `return Paper(` 之前插入：

```python
        institutions: list[str] = []
        companies: list[str] = []
        for authorship in work.get("authorships") or []:
            for institution in authorship.get("institutions") or []:
                name = (institution.get("display_name") or "").strip()
                if not name or name in institutions:
                    continue
                institutions.append(name)
                if institution.get("type") == "company":
                    companies.append(name)
```

并在 `Paper(...)` 参数里加 `institutions=institutions, company_institutions=companies,`。

`pubmed_retriever.py`——在 `_article_to_paper` 的 `return Paper(` 之前插入：

```python
        institutions: list[str] = []
        for node in article.findall(".//AffiliationInfo/Affiliation"):
            name = (node.text or "").strip()
            if name and name not in institutions:
                institutions.append(name)
```

并在 `Paper(...)` 参数里加 `institutions=institutions,`。

`europepmc_retriever.py`——在 `_to_paper` 的 `return Paper(` 之前插入：

```python
        affiliation = (item.get("affiliation") or "").strip()
```

并在 `Paper(...)` 参数里加 `institutions=[affiliation] if affiliation else [],`。

`crossref_retriever.py`——在 `_to_paper` 的 `return Paper(` 之前插入：

```python
        institutions: list[str] = []
        for author in item.get("author") or []:
            for affiliation in author.get("affiliation") or []:
                name = (affiliation.get("name") or "").strip()
                if name and name not in institutions:
                    institutions.append(name)
```

并在 `Paper(...)` 参数里加 `institutions=institutions,`。

同一文件的 `search()` 里，`select` 参数追加 `affiliation`：

```python
            "select": "DOI,title,abstract,author,container-title,created",
```
改成
```python
            # Crossref omits any field not named here, affiliation included.
            "select": "DOI,title,abstract,author,container-title,created,affiliation",
```

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/pytest tests/retriever/ tests/test_protocol_journal_fields.py -q
```

预期：新增 11 项全部 PASS，既有检索器测试无回归

- [ ] **Step 6: 提交**

```bash
git add src/zotero_arxiv_daily/protocol.py src/zotero_arxiv_daily/retriever/ tests/retriever/test_institutions.py
git commit -m "feat: lift author affiliations from each source's metadata

Preferring industry research needs to know who wrote the paper, and none
of the four query sources was reading that. The one existing path pulls
affiliations out of full text with an LLM, which is no path at all: 24 of
the first digest's 25 papers had no full text to read.

OpenAlex is the only source that reports an institution *type*, so the
companies it flags are kept separately from the raw list. That gives the
scorer a second, curation-free way to recognise industry work without
guessing from the affiliation string, where 'Institute of Pharmaceuticals'
would read as a company."
```

---

## Task 3: 相关性分诊 `triage.py`

只读标题与摘要的廉价 LLM 调用，产出 0–100 相关度、一句话推荐理由、命中的生物药类型。批量发送以压住调用次数。

**Files:**
- Create: `src/zotero_arxiv_daily/triage.py`
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `Paper`（`title` / `abstract`）
- Produces:
  - `TriageResult(relevance: int, reason: str, modalities: list[str])`
  - `triage_papers(papers: list[Paper], client, llm_params: dict, batch_size: int = 8) -> None`（就地写 `paper.triage`，从不抛异常）

- [ ] **Step 1: 写失败的测试**

`tests/test_triage.py`：

```python
"""LLM relevance triage: batching, parsing, and degradation."""

import json
from types import SimpleNamespace

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.triage import TriageResult, triage_papers

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}


def make_paper(title: str) -> Paper:
    return Paper(source="pubmed", title=title, authors=[], abstract=f"Abstract of {title}.", url="u")


def stub_client(responses: list, recorder: list | None = None) -> SimpleNamespace:
    """Return a client whose Nth call yields responses[N], cycling on the last."""
    calls = {"n": 0}

    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        payload = responses[index]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def payload(*rows: dict) -> str:
    return json.dumps(list(rows), ensure_ascii=False)


ROW1 = {"index": 1, "relevance": 88, "reason": "ADC 载药分布表征", "modalities": ["ADC"]}
ROW2 = {"index": 2, "relevance": 12, "reason": "锂电池负极，与生物药无关", "modalities": []}


def test_triage_fills_every_paper_in_the_batch():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW1, ROW2)]), LLM_PARAMS)
    assert papers[0].triage == TriageResult(relevance=88, reason="ADC 载药分布表征", modalities=["ADC"])
    assert papers[1].triage.relevance == 12


def test_rows_are_matched_by_index_not_by_order():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW2, ROW1)]), LLM_PARAMS)
    assert papers[0].triage.relevance == 88
    assert papers[1].triage.relevance == 12


def test_a_paper_the_model_skipped_stays_unjudged():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW1)]), LLM_PARAMS)
    assert papers[0].triage.relevance == 88
    assert papers[1].triage is None


def test_an_out_of_range_index_is_discarded():
    papers = [make_paper("ADC paper")]
    triage_papers(papers, stub_client([payload({"index": 7, "relevance": 90, "reason": "x"})]), LLM_PARAMS)
    assert papers[0].triage is None


def test_relevance_is_clamped_into_range():
    papers = [make_paper("A"), make_paper("B")]
    rows = payload({"index": 1, "relevance": 150, "reason": "r"}, {"index": 2, "relevance": -5, "reason": "r"})
    triage_papers(papers, stub_client([rows]), LLM_PARAMS)
    assert papers[0].triage.relevance == 100
    assert papers[1].triage.relevance == 0


def test_a_row_without_a_usable_relevance_is_discarded():
    papers = [make_paper("A")]
    triage_papers(papers, stub_client([payload({"index": 1, "relevance": "很高", "reason": "r"})]), LLM_PARAMS)
    assert papers[0].triage is None


def test_missing_modalities_becomes_an_empty_list():
    papers = [make_paper("A")]
    triage_papers(papers, stub_client([payload({"index": 1, "relevance": 70, "reason": "r"})]), LLM_PARAMS)
    assert papers[0].triage.modalities == []


def test_a_malformed_batch_is_retried_once():
    papers = [make_paper("A")]
    client = stub_client(["not json at all", payload({"index": 1, "relevance": 70, "reason": "r"})])
    triage_papers(papers, client, LLM_PARAMS)
    assert papers[0].triage.relevance == 70


def test_a_batch_that_keeps_failing_degrades_to_one_call_per_paper():
    papers = [make_paper("A"), make_paper("B")]
    calls: list = []
    # Both batch attempts blow up; the per-paper retries then succeed.
    client = stub_client(
        [RuntimeError("boom"), RuntimeError("boom"), payload({"index": 1, "relevance": 61, "reason": "r"})],
        recorder=calls,
    )
    triage_papers(papers, client, LLM_PARAMS)
    assert papers[0].triage.relevance == 61
    assert papers[1].triage.relevance == 61
    assert len(calls) == 4  # two batch attempts, then one call per paper


def test_a_total_llm_outage_leaves_papers_unjudged_without_raising():
    papers = [make_paper("A"), make_paper("B")]
    triage_papers(papers, stub_client([RuntimeError("down")]), LLM_PARAMS)
    assert [p.triage for p in papers] == [None, None]


def test_papers_are_sent_in_batches_of_the_configured_size():
    papers = [make_paper(f"P{i}") for i in range(10)]
    calls: list = []
    triage_papers(papers, stub_client([payload()], recorder=calls), LLM_PARAMS, batch_size=4)
    assert len(calls) == 3  # 4 + 4 + 2


def test_an_empty_paper_list_makes_no_calls():
    calls: list = []
    triage_papers([], stub_client([payload()], recorder=calls), LLM_PARAMS)
    assert calls == []


def test_the_prompt_carries_the_counter_examples_that_slipped_through():
    # These five are the actual off-topic papers the first digest shipped.
    # Softening the rubric until they pass again should break a test.
    calls: list = []
    triage_papers([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    for counter_example in ("电池", "等离子体", "小分子", "兽医", "临床诊断"):
        assert counter_example in prompt


def test_the_prompt_carries_the_paper_title_and_abstract():
    calls: list = []
    triage_papers([make_paper("Cetuximab ADC")], stub_client([payload()], recorder=calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    assert "Cetuximab ADC" in prompt
    assert "Abstract of Cetuximab ADC." in prompt
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_triage.py -q
```

预期：`ModuleNotFoundError: No module named 'zotero_arxiv_daily.triage'`

- [ ] **Step 3: 写实现**

`src/zotero_arxiv_daily/triage.py`：

```python
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
```

- [ ] **Step 4: 给 `Paper` 加 `triage` 字段**

`src/zotero_arxiv_daily/protocol.py`——在 Task 2 加的两个字段之后：

```python
    triage: Optional["TriageResult"] = None
```

用字符串前向引用，并在文件顶部加 `TYPE_CHECKING` 导入，避免 `protocol` ↔ `triage` 循环依赖：

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .triage import TriageResult
```

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_triage.py -q
```

预期：14 passed

- [ ] **Step 6: 提交**

```bash
git add src/zotero_arxiv_daily/triage.py src/zotero_arxiv_daily/protocol.py tests/test_triage.py
git commit -m "feat: judge candidate relevance before spending an extraction on it

Embedding similarity answers 'does this look like the Zotero library',
which is not the same question as 'is this about biologics'. A library
full of chromatography methods scores any separation paper highly no
matter what the analyte is, so the first digest shipped a sodium-ion
battery study and a plasma-physics one.

Triage reads title and abstract only and goes out in batches, so gating
25 extractions costs about eight short calls. The rubric names the five
kinds of paper that actually slipped through, and a test asserts they
stay named — softening the rubric until they pass again should fail CI.

A paper left unjudged is treated as not passing rather than waved
through: waving it through would disable the gate on exactly the run
where the model is misbehaving."
```

---

## Task 4: 综合分与双闸门 `scoring.py`

**Files:**
- Create: `src/zotero_arxiv_daily/scoring.py`
- Modify: `src/zotero_arxiv_daily/protocol.py`（`Paper.scoring`）
- Modify: `config/base.yaml`（`report` 下新增闸门与两份名单）
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `TriageResult`（Task 3）、`match_journal` / `match_industry`（Task 1）
- Produces:
  - `ScoreBreakdown(relevance: int, journal_hit: str | None, industry_hit: str | None, rank_score: int)`
  - `score_papers(papers: list[Paper], config) -> None`（就地写 `paper.scoring`）
  - `passing_papers(papers: list[Paper], config) -> list[Paper]`（过闸的，按 `rank_score` 降序）

- [ ] **Step 1: 写失败的测试**

`tests/test_scoring.py`：

```python
"""Composite scoring and the two gates."""

from omegaconf import OmegaConf

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.scoring import passing_papers, score_papers
from zotero_arxiv_daily.triage import TriageResult


def make_config(**overrides):
    report = {
        "min_relevance": 55,
        "min_score": 60,
        "journals": {"bonus": 10, "allow": ["mAbs", "Separations"]},
        "industry": {"bonus": 8, "names": ["Amgen"]},
    }
    report.update(overrides)
    return OmegaConf.create({"report": report})


def make_paper(relevance=None, journal=None, institutions=None, companies=None) -> Paper:
    paper = Paper(source="pubmed", title="A paper", authors=[], abstract="a", url="u", journal=journal)
    paper.institutions = institutions or []
    paper.company_institutions = companies or []
    if relevance is not None:
        paper.triage = TriageResult(relevance=relevance, reason="r", modalities=[])
    return paper


def test_rank_score_is_relevance_when_nothing_matches():
    paper = make_paper(relevance=70, journal="Poultry Science")
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 70
    assert paper.scoring.journal_hit is None
    assert paper.scoring.industry_hit is None


def test_a_listed_journal_adds_its_bonus():
    paper = make_paper(relevance=70, journal="Separations")
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 80
    assert paper.scoring.journal_hit == "Separations"


def test_a_listed_company_adds_its_bonus():
    paper = make_paper(relevance=70, institutions=["Amgen Inc., Thousand Oaks"])
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 78
    assert paper.scoring.industry_hit == "Amgen"


def test_both_bonuses_stack():
    paper = make_paper(relevance=70, journal="mAbs", institutions=["Amgen"])
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 88


def test_an_unjudged_paper_gets_no_breakdown():
    paper = make_paper(relevance=None, journal="mAbs")
    score_papers([paper], make_config())
    assert paper.scoring is None


def test_an_unjudged_paper_never_passes():
    papers = [make_paper(relevance=None, journal="mAbs")]
    score_papers(papers, make_config())
    assert passing_papers(papers, make_config()) == []


def test_a_paper_at_both_thresholds_passes():
    papers = [make_paper(relevance=60, journal="Poultry Science")]
    score_papers(papers, make_config())
    assert len(passing_papers(papers, make_config())) == 1


def test_a_paper_below_the_composite_line_is_dropped():
    papers = [make_paper(relevance=59, journal="Poultry Science")]
    score_papers(papers, make_config())
    assert passing_papers(papers, make_config()) == []


# The reason min_relevance exists at all: without it a 42-point paper —
# squarely in the rubric's "only nouns overlap" band — reaches 60 on
# bonuses alone and lands in the digest.
def test_bonuses_cannot_lift_a_paper_below_the_relevance_floor():
    papers = [make_paper(relevance=42, journal="mAbs", institutions=["Amgen"])]
    score_papers(papers, make_config())
    assert papers[0].scoring.rank_score == 60  # would clear min_score on its own
    assert passing_papers(papers, make_config()) == []


def test_zeroed_thresholds_pass_everything_that_was_judged():
    papers = [make_paper(relevance=5, journal="Poultry Science")]
    config = make_config(min_relevance=0, min_score=0)
    score_papers(papers, config)
    assert len(passing_papers(papers, config)) == 1


def test_survivors_come_back_best_first():
    papers = [make_paper(relevance=60), make_paper(relevance=90), make_paper(relevance=75)]
    config = make_config()
    score_papers(papers, config)
    assert [p.scoring.rank_score for p in passing_papers(papers, config)] == [90, 75, 60]


def test_an_empty_journal_list_costs_no_bonus():
    config = make_config(journals={"bonus": 10, "allow": []})
    paper = make_paper(relevance=70, journal="mAbs")
    score_papers([paper], config)
    assert paper.scoring.rank_score == 70


def test_a_missing_industry_block_is_not_an_error():
    config = OmegaConf.create({"report": {"min_relevance": 55, "min_score": 60}})
    paper = make_paper(relevance=70, journal="mAbs", institutions=["Amgen"])
    score_papers([paper], config)
    assert paper.scoring.rank_score == 70


def test_a_source_flagged_company_earns_the_bonus_without_being_listed():
    paper = make_paper(relevance=70, companies=["Genentech"], institutions=["Genentech"])
    score_papers([paper], make_config())
    assert paper.scoring.industry_hit == "Genentech"
    assert paper.scoring.rank_score == 78
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_scoring.py -q
```

预期：`ModuleNotFoundError: No module named 'zotero_arxiv_daily.scoring'`

- [ ] **Step 3: 写实现**

`src/zotero_arxiv_daily/scoring.py`：

```python
"""Turn a triage verdict plus two curated lists into one comparable number.

Two gates, not one.  The composite line (`min_score`) is where the journal
and company bonuses do their work — they are meant to break ties between
papers that already qualify.  The relevance floor (`min_relevance`) is the
line no bonus can cross, because without it a 42-point paper — squarely in
the rubric's "only the nouns overlap" band — reaches 60 on bonuses alone and
lands in the digest.  Bonuses should promote among the qualified, never
promote the unqualified.
"""

from dataclasses import dataclass

from loguru import logger
from omegaconf import DictConfig

from .affiliation import match_industry, match_journal
from .protocol import Paper

_DEFAULT_MIN_RELEVANCE = 55
_DEFAULT_MIN_SCORE = 60


@dataclass
class ScoreBreakdown:
    relevance: int
    journal_hit: str | None
    industry_hit: str | None
    rank_score: int


def _block(config, key: str) -> dict:
    """Read one optional sub-block of ``report`` as a plain dict."""
    report = config.get("report") or {}
    value = report.get(key) or {}
    return {"bonus": int(value.get("bonus") or 0), "names": list(value.get("allow") or value.get("names") or [])}


def score_papers(papers: list[Paper], config: DictConfig) -> None:
    """Fill ``paper.scoring`` for every judged paper, in place."""
    journals = _block(config, "journals")
    industry = _block(config, "industry")
    for paper in papers:
        if paper.triage is None:
            paper.scoring = None
            continue
        journal_hit = match_journal(paper.journal, journals["names"])
        industry_hit = match_industry(paper.institutions, paper.company_institutions, industry["names"])
        paper.scoring = ScoreBreakdown(
            relevance=paper.triage.relevance,
            journal_hit=journal_hit,
            industry_hit=industry_hit,
            rank_score=paper.triage.relevance
            + (journals["bonus"] if journal_hit else 0)
            + (industry["bonus"] if industry_hit else 0),
        )


def passing_papers(papers: list[Paper], config: DictConfig) -> list[Paper]:
    """Return the papers clearing both gates, best first."""
    report = config.get("report") or {}
    min_relevance = int(report.get("min_relevance", _DEFAULT_MIN_RELEVANCE))
    min_score = int(report.get("min_score", _DEFAULT_MIN_SCORE))

    survivors, unjudged, below_floor, below_line = [], 0, 0, 0
    for paper in papers:
        if paper.scoring is None:
            unjudged += 1
        elif paper.scoring.relevance < min_relevance:
            below_floor += 1
        elif paper.scoring.rank_score < min_score:
            below_line += 1
        else:
            survivors.append(paper)

    logger.info(
        f"Relevance gate: {len(survivors)}/{len(papers)} passed "
        f"({unjudged} unjudged, {below_floor} below relevance {min_relevance}, "
        f"{below_line} below score {min_score})"
    )
    return sorted(survivors, key=lambda p: -p.scoring.rank_score)
```

- [ ] **Step 4: 给 `Paper` 加 `scoring` 字段**

`src/zotero_arxiv_daily/protocol.py`——紧跟 `triage` 之后：

```python
    scoring: Optional["ScoreBreakdown"] = None
```

`TYPE_CHECKING` 块补一行 `from .scoring import ScoreBreakdown`。

- [ ] **Step 5: 配置落到 `config/base.yaml`**

在 `report:` 一节 `min_per_cluster: 1` 之后、`attach_pdfs:` 之前插入：

```yaml
  # —— 相关性闸门 ——
  min_relevance: 55        # 分诊给出的原始相关度下限。期刊/企业加分不能突破这条线。
  min_score: 60            # 综合分下限 = 相关度 + 期刊加分 + 企业加分。
  triage_pool: 60          # 送去分诊的候选上限，按嵌入相似度取前 N。候选不足则全送。
  triage_batch: 8          # 每次分诊调用处理几篇。调小更稳，调大更省。
  journals:
    bonus: 10              # 期刊命中下面名单时加多少分。
    allow: [ ... ]         # 见下
  industry:
    bonus: 8               # 作者单位命中下面名单时加多少分。
    names: [ ... ]         # 见下
```

`allow` 的 63 项与 `names` 的 52 项，**从 spec §11.2 与 §11.3 逐字复制**，保留其中的分类注释行。

- [ ] **Step 6: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_scoring.py tests/test_config_wiring.py -q
```

预期：新增 14 项 PASS，配置装配测试无回归

- [ ] **Step 7: 提交**

```bash
git add src/zotero_arxiv_daily/scoring.py src/zotero_arxiv_daily/protocol.py config/base.yaml tests/test_scoring.py
git commit -m "feat: gate candidates on relevance, with journal and company bonuses

Two gates rather than one. The composite line is where the bonuses do
their work, breaking ties between papers that already qualify. The
relevance floor is the line no bonus can cross.

Without that floor a 42-point paper — squarely in the rubric's 'only the
nouns overlap' band — reaches the composite line of 60 on a journal
bonus plus a company bonus and lands in the digest. Bonuses should
promote among the qualified, never promote the unqualified, and a test
pins that distinction.

The two curated lists live in config/base.yaml rather than in
CUSTOM_CONFIG: OmegaConf merges dicts key by key but replaces lists
wholesale, so overriding either list through the GitHub variable would
silently drop every entry the operator did not re-paste."
```

---

## Task 5: 有类型的报告字段

字段目前是无类型字符串，渲染是 `**{label}：** {value}` 一行到底。模型面对「方法」这种天然多要素的字段只能整段搬摘要——即使用者反馈的「大段堆砌」。

**Files:**
- Modify: `src/zotero_arxiv_daily/extract.py`
- Modify: `config/base.yaml`（重写 `report.fields`）
- Test: `tests/test_extract.py`（扩充）

**Interfaces:**
- Consumes: `TriageResult`（Task 3）
- Produces:
  - `FieldSpec(key, label, instruction, kind="text", max_items=0)`
  - `ListItem(point: str, detail: str)`
  - `normalize_list_value(raw, max_items: int) -> list[ListItem]`
  - `paper.extraction` 的值类型变为 `str | list[ListItem]`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_extract.py` 末尾：

```python
from zotero_arxiv_daily.extract import ListItem, normalize_list_value
from zotero_arxiv_daily.triage import TriageResult

LIST_FIELDS = [
    FieldSpec(key="background", label="背景", instruction="研究背景"),
    FieldSpec(key="method", label="方法", instruction="拆成 3-5 条", kind="list", max_items=3),
]


def test_field_specs_default_to_plain_text():
    cfg = OmegaConf.create({"report": {"fields": [{"key": "background", "label": "背景"}]}})
    spec = load_field_specs(cfg)[0]
    assert spec.kind == "text"
    assert spec.max_items == 0


def test_field_specs_read_kind_and_max_items():
    cfg = OmegaConf.create(
        {"report": {"fields": [{"key": "method", "label": "方法", "kind": "list", "max_items": 4}]}}
    )
    spec = load_field_specs(cfg)[0]
    assert spec.kind == "list"
    assert spec.max_items == 4


def test_list_value_keeps_point_and_detail():
    raw = [{"point": "柱系统", "detail": "C8 反相柱，变性条件"}]
    assert normalize_list_value(raw, 0) == [ListItem(point="柱系统", detail="C8 反相柱，变性条件")]


def test_a_list_of_bare_strings_becomes_detail_only_items():
    assert normalize_list_value(["C8 反相柱", "SEC-3000 柱"], 0) == [
        ListItem(point="", detail="C8 反相柱"),
        ListItem(point="", detail="SEC-3000 柱"),
    ]


def test_a_plain_string_becomes_one_item():
    assert normalize_list_value("C8 反相柱，变性条件", 0) == [ListItem(point="", detail="C8 反相柱，变性条件")]


def test_a_missing_list_value_is_empty():
    assert normalize_list_value(None, 0) == []
    assert normalize_list_value("", 0) == []
    assert normalize_list_value([], 0) == []


def test_items_without_any_text_are_dropped():
    assert normalize_list_value([{"point": "", "detail": ""}, {"point": "柱", "detail": ""}], 0) == [
        ListItem(point="柱", detail="")
    ]


def test_max_items_truncates():
    raw = [{"point": f"P{i}", "detail": "d"} for i in range(6)]
    assert len(normalize_list_value(raw, 3)) == 3


def test_zero_max_items_means_no_limit():
    raw = [{"point": f"P{i}", "detail": "d"} for i in range(6)]
    assert len(normalize_list_value(raw, 0)) == 6


def test_extraction_returns_list_items_for_a_list_field():
    payload = json.dumps(
        {"background": "抗体电荷异质性", "method": [{"point": "柱", "detail": "C8"}]}, ensure_ascii=False
    )
    result = extract_paper(make_paper(), stub_client(payload), LLM_PARAMS, LIST_FIELDS)
    assert result["background"] == "抗体电荷异质性"
    assert result["method"] == [ListItem(point="柱", detail="C8")]


def test_a_failed_extraction_returns_the_right_empty_shape():
    def boom(**kwargs):
        raise RuntimeError("down")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    result = extract_paper(make_paper(), client, LLM_PARAMS, LIST_FIELDS)
    assert result == {"background": "", "method": []}


def test_the_prompt_names_the_modalities_triage_found():
    calls: list = []
    paper = make_paper()
    paper.triage = TriageResult(relevance=88, reason="r", modalities=["ADC", "单抗"])
    extract_paper(paper, stub_client(PAYLOAD, calls), LLM_PARAMS, LIST_FIELDS)
    prompt = str(calls[0]["messages"])
    assert "ADC" in prompt
    assert "单抗" in prompt


def test_a_paper_with_no_modalities_still_gets_a_usable_prompt():
    # Method-transfer papers pass the gate with modalities == [].  Inventing
    # a modality for them would put words in the model's mouth.
    calls: list = []
    paper = make_paper()
    paper.triage = TriageResult(relevance=60, reason="r", modalities=[])
    extract_paper(paper, stub_client(PAYLOAD, calls), LLM_PARAMS, LIST_FIELDS)
    prompt = str(calls[0]["messages"])
    assert "迁移" in prompt


def test_an_untriaged_paper_extracts_without_raising():
    result = extract_paper(make_paper(), stub_client(PAYLOAD), LLM_PARAMS, LIST_FIELDS)
    assert result["background"] == "单抗电荷异质性"


def test_the_prompt_asks_for_an_array_only_for_list_fields():
    calls: list = []
    extract_paper(make_paper(), stub_client(PAYLOAD, calls), LLM_PARAMS, LIST_FIELDS)
    prompt = str(calls[0]["messages"])
    assert '"method":[' in prompt.replace(" ", "")
    assert '"background":"' in prompt.replace(" ", "")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_extract.py -q
```

预期：`ImportError: cannot import name 'ListItem'`

- [ ] **Step 3: 改 `extract.py`**

顶部无需新增 import。**不要**写 `from .triage import TriageResult`：`triage.py` 在运行时导入 `protocol`，加这一行会形成真实的循环导入。`extract.py` 只读 `paper.triage.modalities` 这个属性，不需要那个类型。

替换 `FieldSpec` 与 `load_field_specs`：

```python
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
```

`_build_prompt` 替换为：

```python
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
```

`extract_paper` 里两处 `empty` 与返回值改为按 `kind` 分派：

```python
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
```

- [ ] **Step 4: 重写 `config/base.yaml` 的 `report.fields`**

整段 `fields:` 替换为 spec §11.4 的五个字段定义，**逐字复制**（含 `kind`、`max_items` 与字数区间）。

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_extract.py tests/test_config_wiring.py -q
```

预期：新增 15 项 PASS

- [ ] **Step 6: 提交**

```bash
git add src/zotero_arxiv_daily/extract.py config/base.yaml tests/test_extract.py
git commit -m "feat: give report fields a type, and tell the extractor what it is reading

Every field was an untyped string rendered as one long line, so a model
asked for 'method' had nowhere to put five separate facts and pasted the
abstract instead. Fields now declare kind: text or list, and a list field
comes back as point/detail pairs the renderers can lay out.

Model output for a list field is accepted in whatever shape it arrives —
objects, bare strings, or one long string — because a digest is worth
more than winning a schema argument.

The extraction prompt now carries the modalities triage identified, so
the insight field argues about the ADC or the bispecific actually at
hand. The first digest's insights drifted into 'RP-HPLC remains an
efficient and reliable technique' precisely because nothing told the
model which product the reader cares about."
```

---

## Task 6: 渲染有序列表与徽标行

**Files:**
- Modify: `src/zotero_arxiv_daily/report.py`
- Test: `tests/test_report.py`（扩充）

**Interfaces:**
- Consumes: `ListItem` / `FieldSpec`（Task 5）、`TriageResult`（Task 3）、`ScoreBreakdown`（Task 4）
- Produces: 三个渲染器对 `kind == "list"` 字段输出有序列表；每篇多一行徽标与推荐理由

**注意**：`_email_pick()` 现在取「第一个非空字段」做导语并直接 `escape()`，字段变成列表后会抛 `TypeError`。改为用 `paper.triage.reason` 做导语——推荐理由本来就是干这个的。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_report.py` 末尾：

```python
from zotero_arxiv_daily.extract import ListItem
from zotero_arxiv_daily.scoring import ScoreBreakdown
from zotero_arxiv_daily.triage import TriageResult

LIST_SPECS = [
    FieldSpec(key="background", label="背景", instruction="i"),
    FieldSpec(key="method", label="方法", instruction="i", kind="list"),
]


def judged_paper(**kw) -> Paper:
    paper = Paper(
        source="pubmed",
        title="ADC 表征",
        authors=["Doe Jane"],
        abstract="a",
        url="https://example.org/1",
        doi="10.1000/adc",
        journal="Separations",
        score=1.0,
        cluster="色谱",
    )
    paper.triage = TriageResult(relevance=82, reason="首次把 iCIEF 用于 AAV 衣壳", modalities=["ADC"])
    paper.scoring = ScoreBreakdown(relevance=82, journal_hit="Separations", industry_hit="Amgen", rank_score=100)
    paper.extraction = {
        "background": "抗体电荷异质性长期靠 IEX 分析",
        "method": [ListItem(point="柱系统", detail="C8 反相柱"), ListItem(point="", detail="二极管阵列检测")],
    }
    for key, value in kw.items():
        setattr(paper, key, value)
    return paper


def one_paper_digest(paper: Paper) -> Digest:
    return build_digest([paper], [], date(2026, 8, 21), top_n=1)


def test_markdown_numbers_a_list_field():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "1. **柱系统** — C8 反相柱" in md


def test_markdown_omits_the_dash_when_there_is_no_keyword():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "2. 二极管阵列检测" in md


def test_markdown_still_renders_a_text_field_inline():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "**背景：** 抗体电荷异质性长期靠 IEX 分析" in md


def test_markdown_shows_relevance_and_both_badges():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "相关度 82" in md
    assert "核心期刊" in md
    assert "企业研究（Amgen）" in md


def test_markdown_shows_the_recommendation_reason():
    md = render_markdown(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "**推荐理由：** 首次把 iCIEF 用于 AAV 衣壳" in md


def test_a_paper_without_a_journal_hit_shows_no_journal_badge():
    paper = judged_paper()
    paper.scoring = ScoreBreakdown(relevance=70, journal_hit=None, industry_hit=None, rank_score=70)
    md = render_markdown(one_paper_digest(paper), LIST_SPECS)
    assert "相关度 70" in md
    assert "核心期刊" not in md
    assert "企业研究" not in md


def test_an_unjudged_paper_renders_without_a_badge_line():
    paper = judged_paper()
    paper.triage = None
    paper.scoring = None
    md = render_markdown(one_paper_digest(paper), LIST_SPECS)
    assert "相关度" not in md
    assert "推荐理由" not in md
    assert "抗体电荷异质性长期靠 IEX 分析" in md


def test_web_html_renders_a_list_field_as_an_ordered_list():
    html = render_web_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "<ol" in html
    assert "<strong>柱系统</strong>" in html
    assert "C8 反相柱" in html


def test_web_html_escapes_list_item_text():
    paper = judged_paper()
    paper.extraction = {"background": "", "method": [ListItem(point="<b>x</b>", detail="a & b")]}
    html = render_web_html(one_paper_digest(paper), LIST_SPECS)
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "a &amp; b" in html


def test_web_html_shows_the_badges():
    html = render_web_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "相关度 82" in html
    assert "企业研究（Amgen）" in html


def test_email_uses_the_recommendation_reason_as_the_teaser():
    # The old teaser took the first non-empty field and escaped it, which
    # raises TypeError now that a field can be a list.
    html = render_email_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "首次把 iCIEF 用于 AAV 衣壳" in html


def test_email_renders_without_a_triage_verdict():
    paper = judged_paper()
    paper.triage = None
    paper.scoring = None
    html = render_email_html(one_paper_digest(paper), LIST_SPECS)
    assert "ADC 表征" in html


def test_email_shows_relevance_in_the_cluster_rows():
    html = render_email_html(one_paper_digest(judged_paper()), LIST_SPECS)
    assert "82" in html


def test_email_still_fits_its_byte_budget():
    papers = [judged_paper() for _ in range(200)]
    digest = build_digest(papers, [], date(2026, 8, 21), top_n=3)
    html = render_email_html(digest, LIST_SPECS, max_bytes=20000)
    assert len(html.encode("utf-8")) <= 20000
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_report.py -q
```

预期：`ImportError` 或断言失败

- [ ] **Step 3: 改 `report.py`**

顶部 import 补 `from .extract import FieldSpec, ListItem`。

在 `_byline` 之后插入共用的徽标与列表逻辑：

```python
def _badges(paper: Paper) -> str:
    """Relevance plus the two reasons this paper outranked its neighbours."""
    scoring = paper.scoring
    if scoring is None:
        return ""
    bits = [f"相关度 {scoring.relevance}"]
    if scoring.journal_hit:
        bits.append("核心期刊")
    if scoring.industry_hit:
        bits.append(f"企业研究（{scoring.industry_hit}）")
    return " · ".join(bits)


def _reason(paper: Paper) -> str:
    return (paper.triage.reason if paper.triage else "") or ""


def _as_items(value) -> list[ListItem]:
    """Tolerate an extraction written before fields had types."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, ListItem)]
    return []
```

`_markdown_entry` 替换为：

```python
def _markdown_field(spec: FieldSpec, value) -> list[str]:
    if spec.kind == "list":
        items = _as_items(value)
        if not items:
            return []
        lines = [f"**{spec.label}：**", ""]
        for n, item in enumerate(items, 1):
            lines.append(f"{n}. **{item.point}** — {item.detail}" if item.point else f"{n}. {item.detail}")
        return lines + [""]
    return [f"**{spec.label}：** {value}", ""] if value else []


def _markdown_entry(paper: Paper, fields: list[FieldSpec], extra: str = "") -> list[str]:
    link = paper.doi_url or paper.url
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    lines = [f"### {paper.title}", "", meta, ""]
    badges = _badges(paper)
    if badges:
        lines += [badges, ""]
    lines += [f"DOI: <{link}>", ""]
    reason = _reason(paper)
    if reason:
        lines += [f"**推荐理由：** {reason}", ""]
    for spec in fields:
        lines += _markdown_field(spec, (paper.extraction or {}).get(spec.key, ""))
    return lines
```

`_html_card` 里的字段循环替换为：

```python
    badges = _badges(paper)
    if badges:
        rows.append(f'<p class="meta">{escape(badges)}</p>')
    reason = _reason(paper)
    if reason:
        rows.append(f'<div class="field"><b>推荐理由</b>：{escape(reason)}</div>')
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if spec.kind == "list":
            items = _as_items(value)
            if not items:
                continue
            points = "".join(
                f"<li><strong>{escape(i.point)}</strong> — {escape(i.detail)}</li>"
                if i.point
                else f"<li>{escape(i.detail)}</li>"
                for i in items
            )
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b><ol class="points">{points}</ol></div>')
        elif value:
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b>：{escape(value)}</div>')
```

`_WEB_CSS` 末尾补一行：

```css
.points{margin:.35rem 0 0;padding-left:1.4rem}.points li{margin:.25rem 0}
```

`_email_pick` 的 `first_field` 三行替换为：

```python
    # The teaser is the recommendation reason: it is one sentence by
    # construction, and a list-valued field cannot be escaped as a string.
    teaser = _reason(paper)
    body = (
        f'<p style="margin:2px 0 0;font-size:13px;color:#3C4642">{escape(teaser)}</p>'
        if teaser
        else ""
    )
```
并删掉 `extraction = paper.extraction or {}` 与 `first_field = ...` 两行。

`_email_list` 的 `<span>` 一行改为把相关度带上：

```python
        badge = f" · 相关度 {paper.scoring.relevance}" if paper.scoring else ""
        rows.append(
            '<tr><td style="padding:4px 0;border-bottom:1px solid #F0EEE8">'
            f'<a href="{link}" style="color:#0E5E5A;font-size:13px;text-decoration:none">'
            f"{escape(paper.title)}</a>"
            f'<span style="color:#5C6660;font-size:11px"> · {escape(_byline(paper))}{escape(badge)}</span>'
            "</td></tr>"
        )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_report.py tests/test_mailer.py -q
```

预期：新增 14 项 PASS，既有报告测试无回归

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/report.py tests/test_report.py
git commit -m "feat: render typed fields as ordered lists, with a relevance badge

A list-valued field renders as a numbered list in all three renderers,
each item leading with its keyword. Every paper gains a line showing its
relevance score and why it outranked its neighbours, plus the one-line
recommendation reason from triage.

The email teaser now uses that reason instead of the first non-empty
field. That was not a style preference: the old code escaped the field
value as a string and would raise TypeError the moment the first field
was a list."
```

---

## Task 7: 接入流水线

把闸门插进 `weekly.py`，位置在**配额之前**——这是整个改造的要害。配额只在过闸文献中分配，某主题当周只有 1 篇合格就只出 1 篇，不再从候选队尾捞数凑满。

**Files:**
- Modify: `src/zotero_arxiv_daily/weekly.py`
- Modify: `src/zotero_arxiv_daily/backfill.py`
- Test: `tests/test_weekly.py`（扩充 + 改 fixture）、`tests/test_backfill.py`（扩充）

**Interfaces:**
- Consumes: `triage_papers`（Task 3）、`score_papers` / `passing_papers`（Task 4）
- Produces: `WeeklyExecutor._gate(papers: list[Paper]) -> list[Paper]`；`backfill_papers(..., gate=None)`

**先改 fixture，否则既有测试会全红。** `tests/test_weekly.py` 的 LLM 桩在头三次调用后一律返回 `{"background": ...}`。分诊拿到它解析不出 JSON 数组，重试、逐篇降级全部失败，于是所有候选 `triage is None`，一篇都过不了闸，周报为空——十几个既有测试跟着挂。

- [ ] **Step 1: 让测试 fixture 认识分诊调用**

`tests/test_weekly.py` 的 `weekly_config` fixture 里，`config.report` 补齐新键：

```python
        config.report = OmegaConf.create(
            {
                "min_papers": 4,
                "max_papers": 6,
                "top_picks": 2,
                "min_per_cluster": 1,
                "attach_pdfs": 0,
                "output_dir": str(tmp_path),
                "min_relevance": 55,
                "min_score": 60,
                "triage_pool": 60,
                "triage_batch": 8,
                "journals": {"bonus": 10, "allow": ["Journal of Chromatography A"]},
                "industry": {"bonus": 8, "names": ["Amgen"]},
                "fields": [{"key": "background", "label": "背景", "instruction": "研究背景"}],
            }
        )
```

`stubbed` fixture 里，`state` 加一个可调的相关度，并让 `create()` 认得分诊请求。**关键是不要在测试里整体替换这个桩**——它还负责供应聚类与检索式蒸馏的三份 payload，换掉就会让聚类解析失败，测试因为无关的原因变红。

```python
    # A number, or one number per candidate.  Tests dial this instead of
    # replacing the stub, which also feeds clustering and profile distillation.
    state = {"sent": [], "committed": [], "pushed": False, "relevance": 90}
```

`create()` 改为：

```python
    def create(**kwargs):
        request = str(kwargs.get("messages", []))
        # Triage asks for a JSON array and says how many papers are in the
        # batch; answer every index so the gate has something to work with.
        if '"relevance"' in request:
            count = int(re.search(r"共 (\d+) 篇", request).group(1))
            setting = state["relevance"]
            rows = [
                {
                    "index": i,
                    "relevance": setting[i - 1] if isinstance(setting, list) else setting,
                    "reason": f"理由 {i}",
                    "modalities": ["ADC"],
                }
                for i in range(1, count + 1)
            ]
            content = json.dumps(rows, ensure_ascii=False)
        else:
            try:
                content = next(payloads)
            except StopIteration:
                content = '{"background":"抽取出的背景"}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
```

文件顶部补 `import json` 与 `import re`。

- [ ] **Step 2: 写失败的测试**

追加到 `tests/test_weekly.py` 末尾：

```python
def test_every_delivered_paper_cleared_the_gate(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.scoring is not None
            assert paper.scoring.relevance >= weekly_config.report.min_relevance


def test_an_irrelevant_candidate_never_reaches_the_digest(weekly_config, stubbed):
    # 10 is the rubric's "no connection to biologics" band — where the
    # sodium-ion battery paper belongs.
    stubbed["relevance"] = 10
    assert WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21)) is None
    assert stubbed["sent"] == []


def test_the_quota_is_allocated_only_among_survivors(weekly_config, stubbed):
    # Three candidates, one qualifying. The quota is six slots across two
    # clusters; the old code filled the rest from the tail of the list.
    stubbed["relevance"] = [90, 10, 10]
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert sum(len(papers) for _, papers in digest.clusters) == 1


def test_a_journal_on_the_list_earns_its_bonus_end_to_end(weekly_config, stubbed):
    # make_candidate() sets journal="J Chromatogr A"; the fixture lists
    # "Journal of Chromatography A", which must NOT match an abbreviation.
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    paper = digest.clusters[0][1][0]
    assert paper.scoring.journal_hit is None
    assert paper.scoring.rank_score == 90


def test_only_the_configured_pool_size_is_triaged(weekly_config, stubbed, monkeypatch):
    from zotero_arxiv_daily.triage import triage_papers as real_triage

    with open_dict(weekly_config):
        weekly_config.report.triage_pool = 2

    seen: list[int] = []

    def counting(papers, client, llm_params, batch_size=8):
        seen.append(len(papers))
        return real_triage(papers, client, llm_params, batch_size)

    monkeypatch.setattr("zotero_arxiv_daily.weekly.triage_papers", counting)
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert seen[0] == 2
```

追加到 `tests/test_backfill.py` 末尾：

```python
def test_backfill_candidates_pass_through_the_gate():
    profiles = [QueryProfile(cluster="c", mesh_terms=[], free_terms=[], pubmed_query="q", plain_query="p")]

    class Retriever:
        def search_highly_cited(self, query, limit):
            return [
                _paper(doi="10.1/keep", cited_by_count=500),
                _paper(doi="10.1/drop", cited_by_count=900),
            ]

    def gate(papers):
        return [p for p in papers if p.doi == "10.1/keep"]

    # Without the gate the 900-citation paper would win. Highly cited is not
    # the same as relevant — that is how a 2005 virology paper got in.
    chosen = backfill_papers(profiles, Retriever(), needed=2, exclude_dois=set(), gate=gate)
    assert [p.doi for p in chosen] == ["10.1/keep"]


def test_backfill_without_a_gate_keeps_everything():
    profiles = [QueryProfile(cluster="c", mesh_terms=[], free_terms=[], pubmed_query="q", plain_query="p")]

    class Retriever:
        def search_highly_cited(self, query, limit):
            return [_paper(doi="10.1/a", cited_by_count=5)]

    assert len(backfill_papers(profiles, Retriever(), needed=1, exclude_dois=set())) == 1
```

`_paper()` 是该文件已有的构造辅助；若签名不接受 `doi` / `cited_by_count`，按现有实现调整调用。

- [ ] **Step 3: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_weekly.py tests/test_backfill.py -q
```

预期：新增用例 FAIL（`AttributeError` 或断言不成立）

- [ ] **Step 4: 改 `backfill.py`**

签名与过滤逻辑：

```python
def backfill_papers(
    profiles: list[QueryProfile],
    retriever,
    needed: int,
    exclude_dois: set[str],
    gate=None,
) -> list[Paper]:
    """Return up to *needed* highly-cited papers across *profiles*.

    *gate* filters the oversampled pool before the citation sort.  Sorting by
    citations without it is how a 2005 virology paper reached a CMC digest:
    highly cited is not the same as relevant.
    """
```

`pool = dedup_papers(pool)` 之后插入：

```python
    if gate is not None:
        pool = gate(pool)
```

- [ ] **Step 5: 改 `weekly.py`**

import 补：

```python
from zotero_arxiv_daily.scoring import passing_papers, score_papers
from zotero_arxiv_daily.triage import triage_papers
```

`WeeklyExecutor` 加方法：

```python
    def _gate(self, papers):
        """Triage, score, and keep only what clears both thresholds."""
        if not papers:
            return []
        triage_papers(
            papers,
            self.openai_client,
            self.config.llm,
            int(self.config.report.get("triage_batch", 8)),
        )
        score_papers(papers, self.config)
        return passing_papers(papers, self.config)
```

`run()` 里 `chosen = []` 那一段替换为：

```python
        chosen = []
        if candidates:
            self._score_and_assign(candidates, corpus, clusters)
            candidates.sort(key=lambda p: -(p.score or 0.0))
            # Triage is the cost ceiling: only the most similar candidates are
            # worth an LLM call, and everything below is the least similar of
            # an already-filtered pool.
            pool = candidates[: int(self.config.report.get("triage_pool", 60))]
            eligible = self._gate(pool)
            quota = allocate_quota(
                {c.name: len(c.members) for c in clusters},
                int(self.config.report.max_papers),
                int(self.config.report.min_per_cluster),
            )
            # Quota is allocated among survivors only.  Allocating it over all
            # candidates is what forced five themes times five slots to be
            # filled from the tail of the list.
            chosen = take_by_quota(eligible, quota)
```

`backfill_papers(...)` 调用补 `gate=self._gate,`。

- [ ] **Step 6: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_weekly.py tests/test_backfill.py tests/test_monthly.py -q
```

预期：全部 PASS，既有周报编排测试无回归

- [ ] **Step 7: 跑全量回归**

```bash
.venv/bin/pytest -q 2>&1 | tail -20
```

预期：仅 `tests/test_protocol.py` 的 3 项 tiktoken 用例失败（既有状态）

- [ ] **Step 8: 提交**

```bash
git add src/zotero_arxiv_daily/weekly.py src/zotero_arxiv_daily/backfill.py tests/test_weekly.py tests/test_backfill.py
git commit -m "feat: gate candidates before allocating the per-theme quota

This is the change the rest of the work exists to enable. The quota is now
allocated among papers that cleared the relevance gate, so a theme with
one qualifying paper that week contributes one paper. Previously five
themes times five slots had to be filled whatever the candidate list
held, which is how a sodium-ion battery study and a plasma-physics one
reached a CMC reading list.

Backfill takes the same gate. Sorting oversampled candidates by citation
count alone is how a 2005 virology paper padded the first digest: highly
cited is not the same as relevant.

Triage runs on the top slice of candidates by embedding similarity rather
than all of them, which bounds the cost at roughly eight short calls."
```

---

## Task 8: 预检校验配置本身

现有 6 项检查全部在探测外部边界，没有一项看配置。名单和字段现在放在 `config/base.yaml` 由使用者手工编辑，缩进打错会在周五发信那一刻才炸——这类错误必须在预检就红。

**Files:**
- Modify: `src/zotero_arxiv_daily/preflight.py`
- Test: `tests/test_preflight.py`（扩充）

**Interfaces:**
- Consumes: 无
- Produces: `check_report_config(config: DictConfig) -> CheckResult`，并入 `run_preflight`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_preflight.py` 末尾：

```python
from zotero_arxiv_daily.preflight import check_report_config


def report_config(**overrides):
    report = {
        "min_relevance": 55,
        "min_score": 60,
        "triage_pool": 60,
        "triage_batch": 8,
        "journals": {"bonus": 10, "allow": ["mAbs", "Separations"]},
        "industry": {"bonus": 8, "names": ["Amgen"]},
        "fields": [
            {"key": "background", "label": "背景", "instruction": "i"},
            {"key": "method", "label": "方法", "instruction": "i", "kind": "list", "max_items": 5},
        ],
    }
    report.update(overrides)
    return OmegaConf.create({"report": report})


def test_a_well_formed_report_config_passes():
    result = check_report_config(report_config())
    assert result.ok
    assert "2 journals" in result.detail
    assert "1 companies" in result.detail


def test_a_journal_list_that_is_not_a_list_fails():
    # What a mis-indented YAML edit actually produces.
    result = check_report_config(report_config(journals={"bonus": 10, "allow": "mAbs"}))
    assert not result.ok
    assert "allow" in result.detail


def test_an_unknown_field_kind_fails():
    fields = [{"key": "method", "label": "方法", "instruction": "i", "kind": "bullets"}]
    result = check_report_config(report_config(fields=fields))
    assert not result.ok
    assert "bullets" in result.detail


def test_a_negative_threshold_fails():
    assert not check_report_config(report_config(min_relevance=-1)).ok


def test_a_zero_triage_batch_fails():
    assert not check_report_config(report_config(triage_batch=0)).ok


def test_zeroed_thresholds_are_allowed():
    # Setting both to 0 is the documented way to disable the gate.
    assert check_report_config(report_config(min_relevance=0, min_score=0)).ok


def test_empty_name_lists_are_allowed():
    result = check_report_config(report_config(journals={"bonus": 10, "allow": []}))
    assert result.ok


def test_a_duplicate_journal_warns_without_failing():
    # Hand-maintaining 63 lines makes a paste duplicate near certain, and a
    # duplicate is harmless — bonuses do not stack — so it must not block a run.
    config = report_config(journals={"bonus": 10, "allow": ["mAbs", "The MAbs"]})
    result = check_report_config(config)
    assert result.ok
    assert result.warning
    assert "mabs" in result.detail.lower()


def test_report_config_is_part_of_the_preflight_run(config, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_zotero", lambda c: _ok("zotero"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_llm", lambda c: _ok("llm"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_sources", lambda c: [])
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_embedding", lambda c: _ok("embedding"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_recipients", lambda c: _ok("recipients"))
    monkeypatch.setattr("zotero_arxiv_daily.preflight.check_smtp", lambda c: _ok("smtp"))
    _, results = run_preflight(config)
    assert any(r.name == "report-config" for r in results)
```

若 `tests/test_preflight.py` 里还没有 `_ok` 辅助，加上：

```python
def _ok(name: str) -> CheckResult:
    return CheckResult(name=name, ok=True, detail="stub")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_preflight.py -q
```

预期：`ImportError: cannot import name 'check_report_config'`

- [ ] **Step 3: 写实现**

`src/zotero_arxiv_daily/preflight.py`——在 `check_smtp` 之后插入：

```python
_VALID_FIELD_KINDS = frozenset({"text", "list"})
_NON_NEGATIVE_KEYS = ("min_relevance", "min_score", "triage_pool")


def _name_list(block, key: str, problems: list[str]) -> list[str]:
    """Read one curated name list, complaining if the YAML edit went wrong."""
    raw = _safe_get(block, key)
    if raw is None:
        return []
    if isinstance(raw, str) or not hasattr(raw, "__iter__"):
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

    journals = _name_list(_safe_get(report, "journals") or {}, "allow", problems)
    companies = _name_list(_safe_get(report, "industry") or {}, "names", problems)

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
```

`run_preflight` 改为：

```python
    results = [check_report_config(config), check_zotero(config), check_llm(config)]
```

配置校验放第一位：它不需要网络，最快，且它挂了后面的检查也没有意义。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_preflight.py -q
```

预期：新增 9 项 PASS

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/preflight.py tests/test_preflight.py
git commit -m "feat: validate the hand-edited report config in preflight

Every other preflight check probes a remote boundary. This one probes the
file the operator most recently touched. The curated journal and company
lists live in config/base.yaml precisely because they are edited by hand,
and a mis-indented list otherwise surfaces on a Friday, mid-send.

Duplicate entries warn rather than fail: maintaining 63 lines by hand
makes a paste duplicate near certain, and a duplicate is harmless because
bonuses do not stack. Blocking a run over one would be worse than the
problem."
```

---

## Task 9: 重写 README

现有 README（214 行）仍是上游 zotero-arxiv-daily 的说明——讲的是每天推送 arXiv 论文，与本仓库实际做的事已经完全脱节。目标读者：**fork 之后照着配好参数就能跑通的新手**。

**Files:**
- Rewrite: `README.md`
- Modify: `docs/cmc-weekly-setup.md`（`CUSTOM_CONFIG` 样例）
- Test: `tests/test_setup_doc.py`（扩充）

**Interfaces:**
- Consumes: 全部前置任务的配置键
- Produces: 无代码接口

- [ ] **Step 1: 写守卫测试**

追加到 `tests/test_setup_doc.py` 末尾：

```python
README = REPO / "README.md"


def test_the_readme_documents_every_new_report_key():
    text = README.read_text(encoding="utf-8")
    for key in ("min_relevance", "min_score", "triage_pool", "triage_batch"):
        assert key in text, f"README never mentions {key}"


def test_the_readme_explains_where_each_list_lives():
    # The list-replacement trap is the one that silently loses half a
    # curated list, so the README must name both files.
    text = README.read_text(encoding="utf-8")
    assert "config/base.yaml" in text
    assert "CUSTOM_CONFIG" in text


def test_the_readme_no_longer_claims_to_be_the_upstream_arxiv_tool():
    text = README.read_text(encoding="utf-8")
    assert "Recommend new arxiv papers of your interest daily" not in text


def test_the_readme_walks_through_paper_selection():
    text = README.read_text(encoding="utf-8")
    for stage in ("分诊", "闸门", "配额", "补位"):
        assert stage in text


def test_every_field_instruction_still_carries_a_word_budget(composed):
    # Word budgets are the only mechanism limiting field length — truncating
    # would cut a sentence in half — so an edit that drops one must fail here.
    from zotero_arxiv_daily.extract import load_field_specs

    for spec in load_field_specs(composed):
        assert re.search(r"\d+\s*[-–]\s*\d+\s*字", spec.instruction), (
            f"field {spec.key!r} lost its word budget"
        )


def test_the_list_fields_are_the_ones_the_design_settled_on(composed):
    from zotero_arxiv_daily.extract import load_field_specs

    kinds = {s.key: s.kind for s in load_field_specs(composed)}
    assert kinds == {
        "background": "text",
        "gap": "text",
        "method": "list",
        "conclusion": "list",
        "insight": "list",
    }
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/pytest tests/test_setup_doc.py -q
```

预期：4 项新用例 FAIL

- [ ] **Step 3: 重写 README**

按 spec §14 的十二节大纲写。硬性要求：

1. **第 5 节「文章是怎么选出来的」**必须包含 spec §14.1 那张阶段表（阶段 / 淘汰了什么 / 由哪个参数控制），并用首期真实数据走一个例子：
   - 钠离子电池那篇在**分诊**被判 0–19 分，被 `min_relevance: 55` 拦下
   - ADC 那篇相关度高，且 *Separations* 命中期刊名单再 +10，所以排第一
   还要给一张调参对照表：「周报太杂 / 太薄 / 某方向总漏」各自该动哪个参数、往哪个方向动。

2. **第 6 节「配置在哪里、怎么改」**必须讲清三件事（spec §14.2）：
   - 两层配置的分工，以及仓库里 `config/custom.yaml` 在 CI 中为何无效
   - **列表整体替换的陷阱**，附 spec §11.0 那段实测代码
   - 两条修改路径的操作步骤，各自跑一次 preflight

3. **第 9 节「预检」**列出 7 项检查（`report-config` 排第一）各自验证什么。

4. 保留上游署名与 LICENSE 链接——这是 fork，不是原创。

- [ ] **Step 4: 更新 `docs/cmc-weekly-setup.md`**

`CUSTOM_CONFIG` 样例保持现状（新键都在 `base.yaml`，不进 Variables），但补一段说明：名单和字段改哪个文件、为什么不放这里。同时把预检输出样例更新为 7 项。

- [ ] **Step 5: 跑测试确认通过**

```bash
.venv/bin/pytest tests/test_setup_doc.py -q
```

预期：全部 PASS，含既有的秘密表与工作流守卫

- [ ] **Step 6: 跑全量回归并提交**

```bash
.venv/bin/pytest -q 2>&1 | tail -20
git add README.md docs/cmc-weekly-setup.md tests/test_setup_doc.py
git commit -m "docs: rewrite the README around what this repository actually does

The README still described the upstream tool: daily arXiv recommendations
from a Zotero library. This fork sends a weekly CMC literature digest
built from four journal sources, gated on relevance, and archived back
into the repository.

Two sections are new because they answer the questions the maintainer
actually hit. One walks a paper through every stage that could drop it
and names the parameter controlling each, using the first digest's real
outcomes. The other explains which file each setting belongs in, and
demonstrates the list-replacement behaviour that silently discards any
curated list overridden through CUSTOM_CONFIG."
```

- [ ] **Step 7: 推送**

```bash
git push -u origin claude/pharma-literature-automation-kyerjk
```

---

## 完成判据

- [ ] `.venv/bin/pytest -q` 仅剩 `tests/test_protocol.py` 的 3 项 tiktoken 失败（既有状态）
- [ ] `config/base.yaml` 含 63 本期刊、52 家企业、5 个带 `kind` 的字段
- [ ] preflight 输出 7 行，`report-config` 在第一行
- [ ] README 中「文章是怎么选出来的」一节能让人自己判断某篇为什么进/没进周报
- [ ] 分支已推送，**未创建 PR**（未获授权）

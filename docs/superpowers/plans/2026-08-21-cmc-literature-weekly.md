# CMC 文献周报流水线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 zotero-arxiv-daily 之上构建一条每周五自动运行的 CMC 文献周报流水线：以 Zotero 语料派生检索式，多源查询式检索期刊文献，按主题簇配额排序，抓取 OA 全文，LLM 结构化抽取，渲染三层产物入库并群发邮件。

**Architecture:** 保留上游「firehose 检索器 + 加权相似度重排」的骨架不动，并行新增一套「查询式检索器」注册表与一个 `weekly` 编排入口。语料经 LLM 聚成 4–6 个主题簇并蒸馏出逐簇布尔检索式（带指纹缓存，月度刷新）；候选按对簇内语料的平均相似度归簇，各簇按 sqrt 配额取数，避免全局 Top-N 被大簇吃光。产物同一份数据渲染三次（仓库 md / 仓库 HTML / 邮件正文 HTML），PDF 仅作邮件附件。

**Tech Stack:** Python ≥3.13 · Hydra + OmegaConf · pyzotero · requests · openai(SDK，指向 DeepSeek) · pymupdf4llm · numpy · pytest（纯 stub，无 Docker）

**Spec:** `docs/cmc-literature-weekly-plan.md`（可行性分析与设计定稿，含发现 1–12 与已确认配置 8.1–8.7）

## Global Constraints

- Python `requires-python = ">=3.13"`；不新增任何重量级依赖（Actions 运行时预算）
- 配置一律走 Hydra 组合：`config/base.yaml` 填默认与文档，`config/custom.yaml` 用 `${oc.env:VAR}` 注入
- 测试禁用 `unittest.mock`；一律 `pytest monkeypatch` + `SimpleNamespace` + `tests/canned_responses.py`（沿用 `tests/conftest.py` 既有约定）
- 新代码**不得**在运行时硬依赖 `tiktoken` 联网下载编码表——截断走 `truncate_for_prompt()`（tiktoken 优先，失败降级按字符数）
- Zotero 语料过滤固定为 `include_path: ["文献", "文献/**"]`（两条缺一不可）
- 周命名：**该周周五所在月份 + 该月第几个周五**，如 `2026-08-W3`；覆盖期 = 周五往前 6 天至该周五（含两端）
- 产物路径：`reports/<YYYY>/<YYYY-MM-WN>.md` 与 `.html`；PDF 落 `library/<YYYY>/<YYYY-MM-WN>/`
- 仓库私有：**周报正文只放 DOI 链接**，不放仓库内 PDF 链接；PDF 经邮件附件交付
- 收件人 ≤10 人，**一律 Bcc**；邮件正文 HTML 压在 **102KB** 内（Gmail 截断阈值）；附件合计 ≤ **20MB**
- 每周 15–25 篇；不足 15 篇时用 OpenAlex `cited_by_count` 高引补位，报告中**单列并标注「经典补位」**
- 定时：`0 12 * * 5` UTC（= 北京时间周五 20:00）
- 上游既有行为不得回归：`BaseReranker.rerank`、`BaseRetriever` 及四个预印本检索器的公开行为保持不变

---

## File Structure

**新增（`src/zotero_arxiv_daily/` 下）：**

| 文件 | 职责 |
| --- | --- |
| `weeknum.py` | 周锚点、周标签、覆盖期窗口。纯函数，无依赖 |
| `dedup.py` | DOI 归一化、跨源去重、跨周 seen 状态读写 |
| `quota.py` | 按簇 sqrt 配额分配 + 按可用量再分配 |
| `search/__init__.py` | 子包导出 |
| `search/cluster.py` | LLM 语料主题聚类 + 指纹缓存；候选归簇 |
| `search/profile.py` | 逐簇检索式蒸馏（MeSH + 自由词 + 布尔式）+ 缓存 |
| `retriever/query_base.py` | 查询式检索器基类与独立注册表（与 firehose 注册表并存） |
| `retriever/pubmed_retriever.py` | PubMed E-utilities esearch+efetch |
| `retriever/europepmc_retriever.py` | Europe PMC REST |
| `retriever/crossref_retriever.py` | Crossref REST（polite pool） |
| `retriever/openalex_retriever.py` | OpenAlex works（兼作补位数据源） |
| `backfill.py` | 高引经典补位 |
| `fulltext/__init__.py` | 子包导出 |
| `fulltext/resolver.py` | OA 全文阶梯：Unpaywall → Europe PMC → 预印本 → 出版商 |
| `extract.py` | 字段由 YAML 驱动的 LLM 结构化抽取 |
| `report.py` | 三层渲染：markdown / 网页 HTML / 邮件 HTML |
| `mailer.py` | 多收件人 Bcc + 附件 + 大小护栏 |
| `publish.py` | 产物写盘与 git 提交 |
| `weekly.py` | 周报编排入口（Hydra main） |
| `monthly.py` | B 层：月度综述（可选） |

**修改：**

| 文件 | 改动 |
| --- | --- |
| `protocol.py` | `Paper` 扩展期刊文献字段；新增 `ExtractedField` |
| `reranker/base.py` | 抽出 `time_decay_weights()` 与 `similarity_matrix()`；`rerank` 行为不变 |
| `utils.py` | 新增 `truncate_for_prompt()`、`http_get_with_retry()`；`send_email` 保持不动（新通道走 `mailer.py`） |
| `config/base.yaml` | 新增 `search:` `fulltext:` `report:` `git:` `weekly:` 五段 |
| `.github/workflows/weekly.yml` | 新增周五定时 workflow |
| `.github/workflows/monthly.yml` | 新增月度 workflow（B 层） |

**设计偏离（相对 spec §5，需知会）：** spec 写「`retriever/base.py` 0 改动，作为新检索器基类」。实际 `BaseRetriever.retrieve_papers()` 是无参 firehose 签名，塞不进「按检索式 + 日期窗口 + 上限」的查询语义。因此改为**新增平行基类 `retriever/query_base.py` 与独立注册表**，`retriever/base.py` 真正 0 改动、四个预印本检索器 0 改动。这比扭曲既有接口更安全。

---

## Task 1: 周命名与覆盖期窗口

**Files:**
- Create: `src/zotero_arxiv_daily/weeknum.py`
- Test: `tests/test_weeknum.py`

**Interfaces:**
- Consumes: 无
- Produces: `anchor_friday(d: date) -> date`、`week_label(d: date) -> str`、`week_window(d: date) -> tuple[date, date]`、`report_paths(d: date) -> tuple[str, str]`、`library_dir(d: date) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_weeknum.py
"""Tests for week anchoring and labelling."""

from datetime import date

from zotero_arxiv_daily.weeknum import (
    anchor_friday,
    library_dir,
    report_paths,
    week_label,
    week_window,
)


def test_anchor_on_a_friday_returns_that_friday():
    assert anchor_friday(date(2026, 8, 21)) == date(2026, 8, 21)


def test_anchor_on_a_saturday_returns_previous_day():
    assert anchor_friday(date(2026, 8, 22)) == date(2026, 8, 21)


def test_anchor_on_a_thursday_walks_back_six_days():
    assert anchor_friday(date(2026, 8, 20)) == date(2026, 8, 14)


def test_label_counts_fridays_within_the_month():
    assert week_label(date(2026, 8, 7)) == "2026-08-W1"
    assert week_label(date(2026, 8, 14)) == "2026-08-W2"
    assert week_label(date(2026, 8, 21)) == "2026-08-W3"
    assert week_label(date(2026, 8, 28)) == "2026-08-W4"


def test_label_uses_the_month_the_friday_falls_in():
    # 2026-10-02 is a Friday: the week spans September but the label is October W1.
    assert week_label(date(2026, 10, 2)) == "2026-10-W1"


def test_window_is_the_seven_days_ending_on_the_friday():
    assert week_window(date(2026, 8, 21)) == (date(2026, 8, 15), date(2026, 8, 21))


def test_window_may_cross_a_month_boundary():
    assert week_window(date(2026, 10, 2)) == (date(2026, 9, 26), date(2026, 10, 2))


def test_report_paths_are_year_foldered():
    md, html = report_paths(date(2026, 8, 21))
    assert md == "reports/2026/2026-08-W3.md"
    assert html == "reports/2026/2026-08-W3.html"


def test_library_dir_is_year_foldered():
    assert library_dir(date(2026, 8, 21)) == "library/2026/2026-08-W3"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_weeknum.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.weeknum'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/weeknum.py
"""Week anchoring for the weekly digest.

A digest week is named after the Friday it closes on: the month that Friday
falls in, plus which Friday of that month it is.  ``2026-08-21`` is the third
Friday of August 2026, so its label is ``2026-08-W3`` and it covers
``2026-08-15`` through ``2026-08-21`` inclusive.
"""

from datetime import date, timedelta

FRIDAY = 4  # date.weekday(): Monday is 0


def anchor_friday(d: date) -> date:
    """Return the most recent Friday on or before *d*."""
    return d - timedelta(days=(d.weekday() - FRIDAY) % 7)


def week_label(d: date) -> str:
    """Return the ``YYYY-MM-WN`` label for the week *d* falls in."""
    friday = anchor_friday(d)
    ordinal = (friday.day - 1) // 7 + 1
    return f"{friday.year}-{friday.month:02d}-W{ordinal}"


def week_window(d: date) -> tuple[date, date]:
    """Return the inclusive ``(start, end)`` dates covered by *d*'s week."""
    friday = anchor_friday(d)
    return friday - timedelta(days=6), friday


def report_paths(d: date) -> tuple[str, str]:
    """Return the ``(markdown, html)`` repository paths for *d*'s digest."""
    friday = anchor_friday(d)
    label = week_label(d)
    return (
        f"reports/{friday.year}/{label}.md",
        f"reports/{friday.year}/{label}.html",
    )


def library_dir(d: date) -> str:
    """Return the repository directory holding *d*'s downloaded PDFs."""
    friday = anchor_friday(d)
    return f"library/{friday.year}/{week_label(d)}"
```

`(friday.day - 1) // 7 + 1` 成立是因为同一月内的周五恰好每 7 天一个：1–7 号的周五是 W1，8–14 号是 W2，以此类推。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_weeknum.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/weeknum.py tests/test_weeknum.py
git commit -m "feat: add week anchoring and digest path naming"
```

---

## Task 2: Paper 模型扩展

**Files:**
- Modify: `src/zotero_arxiv_daily/protocol.py`
- Test: `tests/test_protocol_journal_fields.py`

**Interfaces:**
- Consumes: 无
- Produces: `Paper` 新增字段 `doi: Optional[str]`、`journal: Optional[str]`、`pub_date: Optional[date]`、`pdf_path: Optional[str]`、`oa_status: str = "unknown"`、`extraction: Optional[dict[str, str]]`、`cluster: Optional[str]`、`is_backfill: bool = False`、`cited_by_count: Optional[int]`；新增属性 `Paper.doi_url -> Optional[str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_protocol_journal_fields.py
"""Journal-literature fields added to Paper for the weekly digest."""

from datetime import date

from zotero_arxiv_daily.protocol import Paper


def make_paper(**kw) -> Paper:
    base = dict(
        source="pubmed",
        title="A paper",
        authors=["Smith, J."],
        abstract="An abstract.",
        url="https://example.org/1",
    )
    base.update(kw)
    return Paper(**base)


def test_journal_fields_default_to_empty():
    paper = make_paper()
    assert paper.doi is None
    assert paper.journal is None
    assert paper.pub_date is None
    assert paper.pdf_path is None
    assert paper.oa_status == "unknown"
    assert paper.extraction is None
    assert paper.cluster is None
    assert paper.is_backfill is False
    assert paper.cited_by_count is None


def test_journal_fields_round_trip():
    paper = make_paper(
        doi="10.1016/j.chroma.2026.01.001",
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
        cited_by_count=42,
        is_backfill=True,
    )
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.cited_by_count == 42
    assert paper.is_backfill is True


def test_doi_url_builds_a_resolver_link():
    paper = make_paper(doi="10.1016/j.chroma.2026.01.001")
    assert paper.doi_url == "https://doi.org/10.1016/j.chroma.2026.01.001"


def test_doi_url_is_none_without_a_doi():
    assert make_paper().doi_url is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_protocol_journal_fields.py -q`
Expected: FAIL — `TypeError: Paper.__init__() got an unexpected keyword argument 'doi'`

- [ ] **Step 3: 最小实现**

在 `src/zotero_arxiv_daily/protocol.py` 顶部的 import 区把 `from datetime import datetime` 改为：

```python
from datetime import date, datetime
```

然后在 `Paper` 的 `score: Optional[float] = None` 之后追加字段与属性：

```python
    score: Optional[float] = None
    doi: Optional[str] = None
    journal: Optional[str] = None
    pub_date: Optional[date] = None
    pdf_path: Optional[str] = None
    oa_status: str = "unknown"
    extraction: Optional[dict[str, str]] = None
    cluster: Optional[str] = None
    is_backfill: bool = False
    cited_by_count: Optional[int] = None

    @property
    def doi_url(self) -> Optional[str]:
        """Return the doi.org resolver link, or None when the DOI is unknown."""
        return f"https://doi.org/{self.doi}" if self.doi else None
```

所有新字段都有默认值，因此既有的四个预印本检索器构造 `Paper` 的方式不受影响。

- [ ] **Step 4: 跑测试确认通过，并确认无回归**

Run: `uv run --no-sync pytest tests/test_protocol_journal_fields.py tests/retriever -q`
Expected: PASS — 4 passed + 既有检索器测试全绿

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/protocol.py tests/test_protocol_journal_fields.py
git commit -m "feat: extend Paper with journal-literature fields"
```

---

## Task 3: DOI 归一化与跨源、跨周去重

**Files:**
- Create: `src/zotero_arxiv_daily/dedup.py`
- Test: `tests/test_dedup.py`

**Interfaces:**
- Consumes: `Paper`（Task 2）
- Produces: `normalize_doi(raw: str | None) -> str | None`、`title_key(title: str) -> str`、`dedup_papers(papers: list[Paper]) -> list[Paper]`、`drop_seen(papers: list[Paper], seen: set[str]) -> list[Paper]`、`load_seen(path: str) -> set[str]`、`save_seen(path: str, seen: set[str]) -> None`、`corpus_doi_set(corpus_items: list[dict]) -> set[str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dedup.py
"""DOI normalisation and cross-source / cross-week de-duplication."""

import json

from zotero_arxiv_daily.dedup import (
    dedup_papers,
    drop_seen,
    load_seen,
    normalize_doi,
    save_seen,
    title_key,
)
from zotero_arxiv_daily.protocol import Paper


def make_paper(title="A paper", doi=None, source="pubmed") -> Paper:
    return Paper(
        source=source,
        title=title,
        authors=[],
        abstract="abs",
        url="https://example.org/1",
        doi=doi,
    )


def test_normalize_strips_resolver_prefix_and_lowercases():
    assert normalize_doi("https://doi.org/10.1016/J.Chroma.2026.01.001") == "10.1016/j.chroma.2026.01.001"


def test_normalize_strips_doi_scheme_and_whitespace():
    assert normalize_doi("  doi:10.1021/acs.analchem.6b00001 ") == "10.1021/acs.analchem.6b00001"


def test_normalize_rejects_non_dois():
    assert normalize_doi(None) is None
    assert normalize_doi("") is None
    assert normalize_doi("not-a-doi") is None


def test_title_key_ignores_case_punctuation_and_spacing():
    assert title_key("Charge  Variants: A Review!") == title_key("charge variants a review")


def test_dedup_keeps_the_first_paper_per_doi():
    papers = [
        make_paper(title="First", doi="10.1000/x", source="pubmed"),
        make_paper(title="Second", doi="https://doi.org/10.1000/X", source="crossref"),
    ]
    result = dedup_papers(papers)
    assert len(result) == 1
    assert result[0].title == "First"


def test_dedup_falls_back_to_title_for_papers_without_a_doi():
    papers = [
        make_paper(title="Charge Variants: A Review", doi=None),
        make_paper(title="charge variants a review", doi=None),
    ]
    assert len(dedup_papers(papers)) == 1


def test_dedup_keeps_distinct_papers():
    papers = [make_paper(title="A", doi="10.1000/a"), make_paper(title="B", doi="10.1000/b")]
    assert len(dedup_papers(papers)) == 2


def test_a_doi_bearing_paper_never_collapses_into_a_different_doi():
    papers = [make_paper(title="Same Title", doi="10.1000/a"), make_paper(title="Same Title", doi="10.1000/b")]
    assert len(dedup_papers(papers)) == 2


def test_drop_seen_removes_papers_whose_doi_was_already_sent():
    papers = [make_paper(doi="10.1000/a"), make_paper(doi="10.1000/b")]
    assert [p.doi for p in drop_seen(papers, {"10.1000/a"})] == ["10.1000/b"]


def test_drop_seen_keeps_papers_without_a_doi():
    papers = [make_paper(doi=None)]
    assert len(drop_seen(papers, {"10.1000/a"})) == 1


def test_seen_state_round_trips(tmp_path):
    path = str(tmp_path / "seen.json")
    save_seen(path, {"10.1000/b", "10.1000/a"})
    assert load_seen(path) == {"10.1000/a", "10.1000/b"}


def test_load_seen_on_a_missing_file_is_empty():
    assert load_seen("/nonexistent/seen.json") == set()


def test_saved_seen_state_is_sorted_for_stable_diffs(tmp_path):
    path = str(tmp_path / "seen.json")
    save_seen(path, {"10.1000/c", "10.1000/a", "10.1000/b"})
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == ["10.1000/a", "10.1000/b", "10.1000/c"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_dedup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.dedup'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/dedup.py
"""De-duplication across sources and across weeks.

The same paper routinely surfaces from PubMed, Europe PMC, Crossref and
OpenAlex at once, so candidates are collapsed on a normalised DOI.  Papers
with no DOI — mostly preprints — fall back to a normalised title.  A
``seen_dois`` state file carries the de-duplication across weeks so a paper is
never recommended twice.
"""

import json
import os
import re
from typing import Iterable

from .protocol import Paper

_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
_TITLE_NOISE_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_doi(raw: str | None) -> str | None:
    """Return the bare lowercase DOI, or None when *raw* holds no DOI."""
    if not raw:
        return None
    match = _DOI_RE.search(raw.strip())
    return match.group(0).lower().rstrip(".") if match else None


def title_key(title: str) -> str:
    """Return a comparison key that ignores case, punctuation and spacing."""
    cleaned = _TITLE_NOISE_RE.sub(" ", title.lower())
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def dedup_papers(papers: list[Paper]) -> list[Paper]:
    """Collapse duplicates, keeping the first occurrence of each paper.

    Papers carrying a DOI are keyed on it; only DOI-less papers fall back to
    their title, so two genuinely different papers that share a title are
    never merged.
    """
    seen_dois: set[str] = set()
    seen_titles: set[str] = set()
    kept: list[Paper] = []
    for paper in papers:
        doi = normalize_doi(paper.doi)
        if doi is not None:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)
        else:
            key = title_key(paper.title)
            if key in seen_titles:
                continue
            seen_titles.add(key)
        kept.append(paper)
    return kept


def drop_seen(papers: list[Paper], seen: set[str]) -> list[Paper]:
    """Drop papers whose DOI appears in *seen*; keep every DOI-less paper."""
    return [p for p in papers if (normalize_doi(p.doi) or "") not in seen]


def load_seen(path: str) -> set[str]:
    """Load the set of already-delivered DOIs; missing file means empty."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        return set(json.load(handle))


def save_seen(path: str, seen: set[str]) -> None:
    """Write *seen* sorted, so week-over-week diffs stay readable."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sorted(seen), handle, indent=1, ensure_ascii=False)


def corpus_doi_set(corpus_items: Iterable[dict]) -> set[str]:
    """Collect normalised DOIs out of raw Zotero item dicts."""
    dois = set()
    for item in corpus_items:
        doi = normalize_doi(item.get("data", {}).get("DOI"))
        if doi:
            dois.add(doi)
    return dois
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_dedup.py -q`
Expected: PASS — 13 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/dedup.py tests/test_dedup.py
git commit -m "feat: add DOI normalisation and cross-source de-duplication"
```

---

## Task 4: 按簇配额分配

**Files:**
- Create: `src/zotero_arxiv_daily/quota.py`
- Test: `tests/test_quota.py`

**Interfaces:**
- Consumes: `Paper`（Task 2）
- Produces: `allocate_quota(cluster_sizes: dict[str, int], total: int, min_per_cluster: int = 1) -> dict[str, int]`、`take_by_quota(ranked: list[Paper], quota: dict[str, int]) -> list[Paper]`

发现 12 的落点：`rerank` 把相似度对**全语料**求和，语料分布失衡 28 倍时全局 Top-N 会被大簇吃光。配额按簇语料量**开方**分配（压缩失衡），并给每簇留下限。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_quota.py
"""Per-cluster quota allocation (spec finding 12)."""

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.quota import allocate_quota, take_by_quota


def make_paper(title: str, cluster: str, score: float) -> Paper:
    return Paper(
        source="pubmed",
        title=title,
        authors=[],
        abstract="abs",
        url="https://example.org/" + title,
        score=score,
        cluster=cluster,
    )


def test_quota_sums_to_the_requested_total():
    quota = allocate_quota({"a": 100, "b": 25, "c": 4}, total=18)
    assert sum(quota.values()) == 18


def test_quota_compresses_imbalance_by_square_root():
    # 100:25:4 in raw size is 10:5:2 under sqrt, so the smallest cluster
    # keeps a meaningful share instead of being crowded out.
    quota = allocate_quota({"a": 100, "b": 25, "c": 4}, total=17)
    assert quota == {"a": 10, "b": 5, "c": 2}


def test_every_cluster_gets_at_least_the_floor():
    quota = allocate_quota({"big": 400, "tiny": 1}, total=20, min_per_cluster=2)
    assert quota["tiny"] >= 2
    assert sum(quota.values()) == 20


def test_total_below_the_combined_floor_spreads_one_each():
    quota = allocate_quota({"a": 9, "b": 4, "c": 1}, total=2, min_per_cluster=1)
    assert sum(quota.values()) == 2
    assert set(quota) == {"a", "b", "c"}
    assert quota["a"] == 1


def test_no_clusters_yields_no_quota():
    assert allocate_quota({}, total=15) == {}


def test_take_by_quota_picks_the_best_of_each_cluster():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("a2", "a", 8.0),
        make_paper("b1", "b", 7.0),
        make_paper("a3", "a", 6.0),
        make_paper("b2", "b", 5.0),
    ]
    taken = take_by_quota(ranked, {"a": 2, "b": 1})
    assert [p.title for p in taken] == ["a1", "a2", "b1"]


def test_take_by_quota_redistributes_an_underfilled_cluster():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("a2", "a", 8.0),
        make_paper("a3", "a", 7.0),
        make_paper("b1", "b", 6.0),
    ]
    # b is owed 3 but only has 1; the surplus goes to a by score order.
    taken = take_by_quota(ranked, {"a": 1, "b": 3})
    assert [p.title for p in taken] == ["a1", "a2", "a3", "b1"]


def test_take_by_quota_returns_papers_in_descending_score():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("b1", "b", 8.5),
        make_paper("a2", "a", 8.0),
    ]
    taken = take_by_quota(ranked, {"a": 2, "b": 1})
    assert [p.score for p in taken] == [9.0, 8.5, 8.0]


def test_take_by_quota_ignores_clusters_with_no_quota():
    ranked = [make_paper("a1", "a", 9.0), make_paper("z1", "z", 8.0)]
    taken = take_by_quota(ranked, {"a": 1})
    assert [p.title for p in taken] == ["a1"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_quota.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.quota'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/quota.py
"""Per-cluster quota allocation.

``BaseReranker.rerank`` sums similarity across the whole corpus, so a global
top-N is dominated by whichever theme the user happens to have collected most
of.  With a 28x spread between the largest and smallest cluster that crowds
the small themes out entirely.  Quotas are therefore allocated on the *square
root* of each cluster's corpus size, which compresses the imbalance, and every
cluster keeps a floor.
"""

import math

from .protocol import Paper


def allocate_quota(
    cluster_sizes: dict[str, int],
    total: int,
    min_per_cluster: int = 1,
) -> dict[str, int]:
    """Split *total* slots across clusters by sqrt of their corpus size."""
    if not cluster_sizes or total <= 0:
        return {}

    names = sorted(cluster_sizes)
    weights = {name: math.sqrt(max(cluster_sizes[name], 0)) or 1.0 for name in names}

    # Not enough slots to honour the floor: hand out one each, best-weighted
    # clusters first, so nothing is silently dropped to zero for everyone.
    if total < min_per_cluster * len(names):
        quota = {name: 0 for name in names}
        order = sorted(names, key=lambda n: (-weights[n], n))
        for name in order[:total]:
            quota[name] = 1
        return quota

    quota = {name: min_per_cluster for name in names}
    remaining = total - min_per_cluster * len(names)
    if remaining:
        weight_sum = sum(weights.values())
        exact = {name: remaining * weights[name] / weight_sum for name in names}
        floors = {name: int(exact[name]) for name in names}
        for name in names:
            quota[name] += floors[name]
        # Largest-remainder method for the leftover, ties broken by name so
        # the allocation is deterministic run to run.
        leftover = remaining - sum(floors.values())
        by_remainder = sorted(names, key=lambda n: (-(exact[n] - floors[n]), n))
        for name in by_remainder[:leftover]:
            quota[name] += 1
    return quota


def take_by_quota(ranked: list[Paper], quota: dict[str, int]) -> list[Paper]:
    """Take each cluster's quota from *ranked*, then redistribute shortfalls.

    *ranked* must already be sorted best-first.  Clusters holding fewer
    candidates than they are owed release the surplus to the remaining papers
    in score order, so the digest still reaches its target length.
    """
    remaining = dict(quota)
    taken: list[Paper] = []
    overflow: list[Paper] = []
    for paper in ranked:
        cluster = paper.cluster
        if cluster not in remaining:
            continue
        if remaining[cluster] > 0:
            remaining[cluster] -= 1
            taken.append(paper)
        else:
            overflow.append(paper)

    shortfall = sum(remaining.values())
    if shortfall > 0:
        taken.extend(overflow[:shortfall])

    return sorted(taken, key=lambda p: (p.score is None, -(p.score or 0.0)))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_quota.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/quota.py tests/test_quota.py
git commit -m "feat: allocate digest slots per theme cluster by sqrt quota"
```

---

## Task 5: Reranker 抽出可复用的打分零件

**Files:**
- Modify: `src/zotero_arxiv_daily/reranker/base.py`
- Test: `tests/reranker/test_base_reranker.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: 模块级 `time_decay_weights(n: int) -> np.ndarray`；`BaseReranker.similarity_matrix(candidates, corpus) -> np.ndarray`；`BaseReranker.rerank` 公开行为**不变**

候选要归簇，就得拿到 `candidates × corpus` 的相似度矩阵；而簇成员索引指向的是**原始语料顺序**，`rerank` 内部却按 `added_date` 重排过。因此把两件零件抽出来，让编排层自己按稳定顺序算一次，既复用又不动既有行为。

- [ ] **Step 1: 写失败测试（追加到既有文件末尾）**

```python
# tests/reranker/test_base_reranker.py  —— 追加

def test_time_decay_weights_sum_to_one():
    import numpy as np

    from zotero_arxiv_daily.reranker.base import time_decay_weights

    weights = time_decay_weights(120)
    assert weights.shape == (120,)
    assert np.isclose(weights.sum(), 1.0)


def test_time_decay_weights_favour_recent_entries():
    from zotero_arxiv_daily.reranker.base import time_decay_weights

    weights = time_decay_weights(120)
    assert weights[0] > weights[-1]


def test_time_decay_weights_handle_a_single_entry():
    import numpy as np

    from zotero_arxiv_daily.reranker.base import time_decay_weights

    assert np.isclose(time_decay_weights(1).sum(), 1.0)


def test_similarity_matrix_preserves_the_given_corpus_order(config):
    from zotero_arxiv_daily.reranker.base import BaseReranker
    import numpy as np

    class StubReranker(BaseReranker):
        def get_similarity_score(self, s1, s2):
            return np.array([[float(len(a) + len(b)) for b in s2] for a in s1])

    candidates = [_make_paper("cand", "xx")]
    corpus = [_make_corpus_paper("c1", "a"), _make_corpus_paper("c2", "bbb")]
    matrix = StubReranker(config).similarity_matrix(candidates, corpus)

    assert matrix.shape == (1, 2)
    # column order follows the corpus argument, not any internal sort
    assert matrix[0][0] < matrix[0][1]
```

测试用到的 `_make_paper` / `_make_corpus_paper` 若既有文件中不存在，在文件顶部加：

```python
from datetime import datetime

from zotero_arxiv_daily.protocol import CorpusPaper, Paper


def _make_paper(title: str, abstract: str) -> Paper:
    return Paper(source="stub", title=title, authors=[], abstract=abstract, url="https://e.org/" + title)


def _make_corpus_paper(title: str, abstract: str) -> CorpusPaper:
    return CorpusPaper(title=title, abstract=abstract, added_date=datetime(2026, 1, 1), paths=["文献"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/reranker/test_base_reranker.py -q`
Expected: FAIL — `ImportError: cannot import name 'time_decay_weights'`

- [ ] **Step 3: 最小实现**

把 `src/zotero_arxiv_daily/reranker/base.py` 的 `rerank` 改写为下面这样（新增两处，`rerank` 的算法保持逐字等价）：

```python
def time_decay_weights(n: int) -> np.ndarray:
    """Normalised weights that favour recently added corpus entries.

    Expects the corpus to already be sorted newest-first.
    """
    weights = 1 / (1 + np.log10(np.arange(n) + 1))
    return weights / weights.sum()


class BaseReranker(ABC):
    def __init__(self, config:DictConfig):
        self.config = config

    def similarity_matrix(self, candidates:list[Paper], corpus:list[CorpusPaper]) -> np.ndarray:
        """Return the [n_candidate, n_corpus] similarity matrix.

        Columns follow the order of *corpus* as given, so callers holding
        index-based cluster membership can rely on it.
        """
        sim = self.get_similarity_score([c.abstract for c in candidates], [c.abstract for c in corpus])
        assert sim.shape == (len(candidates), len(corpus))
        return sim

    def rerank(self, candidates:list[Paper], corpus:list[CorpusPaper]) -> list[Paper]:
        corpus = sorted(corpus,key=lambda x: x.added_date,reverse=True)
        sim = self.similarity_matrix(candidates, corpus)
        scores = (sim * time_decay_weights(len(corpus))).sum(axis=1) * 10 # [n_candidate]
        for s,c in zip(scores,candidates):
            c.score = s
        candidates = sorted(candidates,key=lambda x: x.score,reverse=True)
        return candidates
```

- [ ] **Step 4: 跑测试确认通过，且既有 reranker 测试无回归**

Run: `uv run --no-sync pytest tests/reranker -q`
Expected: PASS — 新增 4 条 + 既有全绿（`test_local_reranker.py` 被 slow 标记跳过）

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/reranker/base.py tests/reranker/test_base_reranker.py
git commit -m "refactor: expose reusable similarity matrix and decay weights"
```

---

## Task 6: 语料主题聚类与候选归簇

**Files:**
- Create: `src/zotero_arxiv_daily/search/__init__.py`
- Create: `src/zotero_arxiv_daily/search/cluster.py`
- Test: `tests/search/__init__.py`
- Test: `tests/search/test_cluster.py`

**Interfaces:**
- Consumes: `CorpusPaper`、`Paper`、`truncate_for_prompt`（Task 13 前置，见下方 Step 3 的就地实现说明）
- Produces: `ThemeCluster`（dataclass: `name: str`、`description: str`、`members: list[int]`）、`corpus_fingerprint(corpus) -> str`、`cluster_corpus(corpus, client, llm_params, n_clusters=5) -> list[ThemeCluster]`、`load_or_build_clusters(path, corpus, client, llm_params, n_clusters) -> list[ThemeCluster]`、`assign_clusters(candidates, sim, clusters) -> None`

按**主题簇**而非 Zotero 分类树蒸馏检索式（发现 3）：分类树混了项目代号（KJ103、BJ044）与方法学主题，直接当标签会把「同一个项目下的不同方法」错误绑在一起。

- [ ] **Step 1: 写失败测试**

```python
# tests/search/__init__.py  —— 空文件
```

```python
# tests/search/test_cluster.py
"""LLM corpus clustering, fingerprint caching, and candidate assignment."""

import json
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.search.cluster import (
    ThemeCluster,
    assign_clusters,
    cluster_corpus,
    corpus_fingerprint,
    load_or_build_clusters,
)


def make_corpus(n: int = 4) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Paper {i}",
            abstract=f"Abstract {i}",
            added_date=datetime(2026, 1, i + 1),
            paths=["文献/表征"],
        )
        for i in range(n)
    ]


def stub_client(payload: str) -> SimpleNamespace:
    """A minimal stand-in for the OpenAI client returning a fixed body."""
    message = SimpleNamespace(content=payload)
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice])
    completions = SimpleNamespace(create=lambda **kw: response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}

VALID_PAYLOAD = json.dumps(
    {
        "clusters": [
            {"name": "电荷异质性", "description": "电荷变异体分析", "members": [0, 1]},
            {"name": "宿主细胞蛋白", "description": "HCP 残留检测", "members": [2, 3]},
        ]
    },
    ensure_ascii=False,
)


def test_cluster_corpus_parses_the_llm_payload():
    clusters = cluster_corpus(make_corpus(), stub_client(VALID_PAYLOAD), LLM_PARAMS)
    assert [c.name for c in clusters] == ["电荷异质性", "宿主细胞蛋白"]
    assert clusters[0].members == [0, 1]


def test_cluster_corpus_tolerates_a_fenced_payload():
    fenced = f"```json\n{VALID_PAYLOAD}\n```"
    clusters = cluster_corpus(make_corpus(), stub_client(fenced), LLM_PARAMS)
    assert len(clusters) == 2


def test_cluster_corpus_drops_out_of_range_members():
    payload = json.dumps({"clusters": [{"name": "x", "description": "d", "members": [0, 99]}]})
    clusters = cluster_corpus(make_corpus(), stub_client(payload), LLM_PARAMS)
    assert clusters[0].members == [0]


def test_cluster_corpus_falls_back_to_one_cluster_on_bad_json():
    clusters = cluster_corpus(make_corpus(), stub_client("not json at all"), LLM_PARAMS)
    assert len(clusters) == 1
    assert clusters[0].members == [0, 1, 2, 3]


def test_every_corpus_paper_lands_in_some_cluster():
    payload = json.dumps({"clusters": [{"name": "x", "description": "d", "members": [0, 1]}]})
    clusters = cluster_corpus(make_corpus(), stub_client(payload), LLM_PARAMS)
    covered = {i for c in clusters for i in c.members}
    assert covered == {0, 1, 2, 3}


def test_fingerprint_is_stable_across_reordering():
    corpus = make_corpus()
    assert corpus_fingerprint(corpus) == corpus_fingerprint(list(reversed(corpus)))


def test_fingerprint_changes_when_the_corpus_changes():
    before = corpus_fingerprint(make_corpus(4))
    assert before != corpus_fingerprint(make_corpus(5))


def test_cached_clusters_are_reused_without_calling_the_llm(tmp_path):
    path = str(tmp_path / "clusters.json")
    corpus = make_corpus()
    first = load_or_build_clusters(path, corpus, stub_client(VALID_PAYLOAD), LLM_PARAMS)

    def explode(**kw):
        raise AssertionError("the LLM must not be called when the cache is warm")

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    second = load_or_build_clusters(path, corpus, cold, LLM_PARAMS)
    assert [c.name for c in second] == [c.name for c in first]


def test_cache_is_rebuilt_when_the_corpus_fingerprint_moves(tmp_path):
    path = str(tmp_path / "clusters.json")
    load_or_build_clusters(path, make_corpus(4), stub_client(VALID_PAYLOAD), LLM_PARAMS)
    other = json.dumps({"clusters": [{"name": "新主题", "description": "d", "members": [0]}]}, ensure_ascii=False)
    rebuilt = load_or_build_clusters(path, make_corpus(5), stub_client(other), LLM_PARAMS)
    assert rebuilt[0].name == "新主题"


def test_candidates_are_assigned_to_their_closest_cluster():
    candidates = [
        Paper(source="s", title="c0", authors=[], abstract="a", url="u0"),
        Paper(source="s", title="c1", authors=[], abstract="a", url="u1"),
    ]
    clusters = [
        ThemeCluster(name="alpha", description="", members=[0, 1]),
        ThemeCluster(name="beta", description="", members=[2, 3]),
    ]
    # c0 is closest to the alpha columns, c1 to the beta columns.
    sim = np.array([[0.9, 0.8, 0.1, 0.2], [0.1, 0.2, 0.9, 0.7]])
    assign_clusters(candidates, sim, clusters)
    assert candidates[0].cluster == "alpha"
    assert candidates[1].cluster == "beta"


def test_assignment_uses_the_mean_not_the_max_similarity():
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    clusters = [
        ThemeCluster(name="one_hit_wonder", description="", members=[0, 1]),
        ThemeCluster(name="consistently_close", description="", members=[2, 3]),
    ]
    # A single 0.95 spike loses to a pair that is uniformly 0.7.
    sim = np.array([[0.95, 0.05, 0.7, 0.7]])
    assign_clusters(candidates, sim, clusters)
    assert candidates[0].cluster == "consistently_close"


def test_assignment_skips_empty_clusters():
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    clusters = [ThemeCluster(name="empty", description="", members=[]), ThemeCluster(name="real", description="", members=[0])]
    assign_clusters(candidates, np.array([[0.5]]), clusters)
    assert candidates[0].cluster == "real"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/search/test_cluster.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.search'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/search/__init__.py
"""Corpus-derived search: theme clustering and query distillation."""
```

```python
# src/zotero_arxiv_daily/search/cluster.py
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
from dataclasses import asdict, dataclass

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
    lines = [f"[{i}] {c.title}" for i, c in enumerate(corpus)]
    listing = "\n".join(lines)
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
        logger.warning(f"{len(missing)} corpus papers were left unclustered; folding them into the largest cluster")
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
    prompt = _build_prompt(corpus, n_clusters)
    try:
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "你是一位生物制药 CMC 分析领域的文献主题归纳专家，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        clusters = _parse_clusters(response.choices[0].message.content, len(corpus))
    except Exception as exc:  # noqa: BLE001 - clustering must never break the run
        logger.warning(f"Corpus clustering failed ({exc}); falling back to a single cluster")
        return [ThemeCluster(name="全部", description="未能聚类，全部归为一簇", members=list(range(len(corpus))))]
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
                return [ThemeCluster(**c) for c in cached["clusters"]]
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break the run
            logger.warning(f"Ignoring unreadable cluster cache {path}: {exc}")

    clusters = cluster_corpus(corpus, client, llm_params, n_clusters)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"fingerprint": fingerprint, "clusters": [asdict(c) for c in clusters]},
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/search/test_cluster.py -q`
Expected: PASS — 12 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/search tests/search
git commit -m "feat: cluster the Zotero corpus into themes and route candidates"
```

---

## Task 7: 逐簇检索式蒸馏

**Files:**
- Create: `src/zotero_arxiv_daily/search/profile.py`
- Test: `tests/search/test_profile.py`

**Interfaces:**
- Consumes: `ThemeCluster`（Task 6）、`CorpusPaper`
- Produces: `QueryProfile`（dataclass: `cluster: str`、`mesh_terms: list[str]`、`free_terms: list[str]`、`pubmed_query: str`、`plain_query: str`）、`distill_profile(cluster, corpus, client, llm_params) -> QueryProfile`、`load_or_build_profiles(path, clusters, corpus, client, llm_params) -> list[QueryProfile]`

`literature-search` skill 的多波检索方法在这里的落点：Wave 1 自然语言（`plain_query`，喂 Crossref / OpenAlex / Europe PMC），Wave 2 布尔式加引号（`pubmed_query`，喂 PubMed）。

- [ ] **Step 1: 写失败测试**

```python
# tests/search/test_profile.py
"""Per-cluster query distillation."""

import json
from datetime import datetime
from types import SimpleNamespace

from zotero_arxiv_daily.protocol import CorpusPaper
from zotero_arxiv_daily.search.cluster import ThemeCluster
from zotero_arxiv_daily.search.profile import (
    QueryProfile,
    distill_profile,
    load_or_build_profiles,
)

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}}

PAYLOAD = json.dumps(
    {
        "mesh_terms": ["Antibodies, Monoclonal", "Chromatography, Ion Exchange"],
        "free_terms": ["charge variant", "cIEF"],
        "pubmed_query": '("Antibodies, Monoclonal"[MeSH] OR "charge variant"[tiab])',
        "plain_query": "monoclonal antibody charge variant characterisation",
    },
    ensure_ascii=False,
)


def stub_client(payload: str) -> SimpleNamespace:
    message = SimpleNamespace(content=payload)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))


def make_corpus() -> list[CorpusPaper]:
    return [
        CorpusPaper(title=f"Paper {i}", abstract=f"Abstract {i}", added_date=datetime(2026, 1, 1), paths=["文献"])
        for i in range(3)
    ]


def test_distill_parses_all_four_query_forms():
    cluster = ThemeCluster(name="电荷异质性", description="d", members=[0, 1])
    profile = distill_profile(cluster, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)
    assert profile.cluster == "电荷异质性"
    assert "Antibodies, Monoclonal" in profile.mesh_terms
    assert "cIEF" in profile.free_terms
    assert "[MeSH]" in profile.pubmed_query
    assert profile.plain_query.startswith("monoclonal antibody")


def test_distill_falls_back_to_the_cluster_name_on_bad_json():
    cluster = ThemeCluster(name="电荷异质性", description="电荷变异体", members=[0])
    profile = distill_profile(cluster, make_corpus(), stub_client("garbage"), LLM_PARAMS)
    assert profile.cluster == "电荷异质性"
    assert profile.plain_query == "电荷异质性 电荷变异体"
    assert profile.pubmed_query == ""


def test_a_profile_without_a_pubmed_query_is_not_usable_for_pubmed():
    profile = QueryProfile(cluster="x", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="x")
    assert profile.pubmed_query == ""
    assert profile.plain_query


def test_profiles_are_cached_per_cluster_set(tmp_path):
    path = str(tmp_path / "profiles.json")
    clusters = [ThemeCluster(name="a", description="d", members=[0])]
    first = load_or_build_profiles(path, clusters, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)

    def explode(**kw):
        raise AssertionError("the LLM must not be called when the cache is warm")

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    second = load_or_build_profiles(path, clusters, make_corpus(), cold, LLM_PARAMS)
    assert second[0].pubmed_query == first[0].pubmed_query


def test_profile_cache_is_rebuilt_when_the_cluster_names_change(tmp_path):
    path = str(tmp_path / "profiles.json")
    load_or_build_profiles(path, [ThemeCluster(name="a", description="d", members=[0])], make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)
    other = json.dumps({"mesh_terms": [], "free_terms": [], "pubmed_query": "NEW", "plain_query": "new"})
    rebuilt = load_or_build_profiles(path, [ThemeCluster(name="b", description="d", members=[0])], make_corpus(), stub_client(other), LLM_PARAMS)
    assert rebuilt[0].pubmed_query == "NEW"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/search/test_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.search.profile'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/search/profile.py
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
                {"role": "system", "content": "你是一位医药文献检索专家，精通 PubMed 检索式构造，只输出 JSON。"},
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/search -q`
Expected: PASS — 17 passed（Task 6 的 12 条 + 本任务 5 条）

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/search/profile.py tests/search/test_profile.py
git commit -m "feat: distill per-cluster query profiles for the journal sources"
```

---

## Task 8: 查询式检索器基类 + PubMed

**Files:**
- Create: `src/zotero_arxiv_daily/retriever/query_base.py`
- Create: `src/zotero_arxiv_daily/retriever/pubmed_retriever.py`
- Modify: `src/zotero_arxiv_daily/utils.py`（新增 `http_get_with_retry`）
- Modify: `src/zotero_arxiv_daily/retriever/__init__.py`（导出新注册表）
- Test: `tests/retriever/test_query_base.py`
- Test: `tests/retriever/test_pubmed_retriever.py`

**Interfaces:**
- Consumes: `Paper`（Task 2）
- Produces: `http_get_with_retry(url, *, params=None, headers=None, retries=4, backoff=2.0, timeout=30) -> requests.Response`；`BaseQueryRetriever`（`search(query: str, start: date, end: date, limit: int) -> list[Paper]`）；`register_query_retriever(name)`；`get_query_retriever_cls(name)`；`registered_query_retrievers`；`PubmedRetriever`

- [ ] **Step 1: 写失败测试**

```python
# tests/retriever/test_query_base.py
"""The query-style retriever registry, parallel to the firehose one."""

from datetime import date

import pytest

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.retriever.query_base import (
    BaseQueryRetriever,
    get_query_retriever_cls,
    register_query_retriever,
)


def test_registering_makes_a_retriever_findable_by_name(config):
    @register_query_retriever("stub_source")
    class StubRetriever(BaseQueryRetriever):
        def search(self, query, start, end, limit):
            return [Paper(source=self.name, title=query, authors=[], abstract="a", url="u")]

    cls = get_query_retriever_cls("stub_source")
    assert cls is StubRetriever
    assert cls.name == "stub_source"
    papers = cls(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 10)
    assert papers[0].source == "stub_source"


def test_unknown_query_retriever_raises():
    with pytest.raises(ValueError, match="not found"):
        get_query_retriever_cls("no_such_source")


def test_the_firehose_registry_is_untouched():
    from zotero_arxiv_daily.retriever.base import registered_retrievers
    from zotero_arxiv_daily.retriever.query_base import registered_query_retrievers

    assert registered_query_retrievers is not registered_retrievers
    assert "arxiv" in registered_retrievers
    assert "arxiv" not in registered_query_retrievers
```

```python
# tests/retriever/test_pubmed_retriever.py
"""PubMed E-utilities retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever

ESEARCH_JSON = {"esearchresult": {"idlist": ["40000001", "40000002"]}}

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000001</PMID>
   <Article>
    <ArticleTitle>Charge variant analysis of a monoclonal antibody</ArticleTitle>
    <Abstract><AbstractText>We describe a cIEF method.</AbstractText></Abstract>
    <AuthorList>
     <Author><LastName>Smith</LastName><ForeName>J</ForeName></Author>
     <Author><LastName>Doe</LastName><ForeName>A</ForeName></Author>
    </AuthorList>
    <Journal><Title>J Chromatogr A</Title></Journal>
   </Article>
  </MedlineCitation>
  <PubmedData>
   <History><PubMedPubDate PubStatus="pubmed"><Year>2026</Year><Month>8</Month><Day>18</Day></PubMedPubDate></History>
   <ArticleIdList><ArticleId IdType="doi">10.1016/j.chroma.2026.01.001</ArticleId></ArticleIdList>
  </PubmedData>
 </PubmedArticle>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000002</PMID>
   <Article>
    <ArticleTitle>A paper with no abstract</ArticleTitle>
    <AuthorList><Author><LastName>Lee</LastName><ForeName>K</ForeName></Author></AuthorList>
    <Journal><Title>Anal Chem</Title></Journal>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture()
def mock_pubmed(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        if "esearch" in url:
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: ESEARCH_JSON,
                text="",
            )
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text=EFETCH_XML)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_pubmed_search_parses_articles(config, mock_pubmed):
    papers = PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # the abstract-less record is dropped
    paper = papers[0]
    assert paper.title == "Charge variant analysis of a monoclonal antibody"
    assert paper.doi == "10.1016/j.chroma.2026.01.001"
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.source == "pubmed"


def test_pubmed_sends_the_date_window_and_limit(config, mock_pubmed):
    PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_pubmed[0]
    assert params["mindate"] == "2026/08/15"
    assert params["maxdate"] == "2026/08/21"
    assert params["retmax"] == 20
    assert params["datetype"] == "edat"


def test_pubmed_returns_nothing_for_an_empty_query(config, mock_pubmed):
    assert PubmedRetriever(config).search("", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_pubmed == []


def test_pubmed_returns_nothing_when_no_ids_match(config, monkeypatch):
    def _patched(url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"esearchresult": {"idlist": []}},
            text="",
        )

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20) == []


def test_pubmed_passes_the_api_key_when_configured(config, mock_pubmed):
    from omegaconf import open_dict

    with open_dict(config.source):
        config.source.pubmed = {"api_key": "secret-key", "tool": "t", "email": "e@example.org"}
    PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_pubmed[0]
    assert params["api_key"] == "secret-key"


def test_pubmed_survives_a_malformed_xml_body(config, monkeypatch):
    def _patched(url, **kwargs):
        if "esearch" in url:
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: ESEARCH_JSON, text="")
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text="<not-xml")

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/retriever/test_query_base.py tests/retriever/test_pubmed_retriever.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.retriever.query_base'`

- [ ] **Step 3: 最小实现**

先在 `src/zotero_arxiv_daily/utils.py` 顶部 import 区加入：

```python
from time import sleep
import requests
```

并在文件末尾追加：

```python
def http_get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    retries: int = 4,
    backoff: float = 2.0,
    timeout: int = 30,
):
    """GET with exponential backoff.

    Every journal source here is a free public API with rate limits and
    occasional 5xx; a single failed request must never take down the weekly
    run, so callers get a raised exception only after the last attempt.
    """
    delay = backoff
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised on the last attempt
            if attempt == retries - 1:
                raise
            logger.warning(f"GET {url} failed ({exc}); retrying in {delay:.0f}s")
            sleep(delay)
            delay *= 2
```

```python
# src/zotero_arxiv_daily/retriever/query_base.py
"""Query-style retrieval, parallel to the firehose retrievers.

``BaseRetriever`` models "everything posted today in a category" and its
``retrieve_papers()`` takes no arguments.  Journal literature needs the
opposite shape — a boolean query bounded by a date window — so it gets its own
base class and its own registry rather than bending the existing interface.
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import Type

from omegaconf import DictConfig

from ..protocol import Paper


class BaseQueryRetriever(ABC):
    name: str

    def __init__(self, config: DictConfig):
        self.config = config
        self.retriever_config = getattr(config.source, self.name, None)

    def _setting(self, key: str, default=None):
        """Read a per-source setting, tolerating an absent config block."""
        if self.retriever_config is None:
            return default
        value = self.retriever_config.get(key, default)
        return default if value is None else value

    @abstractmethod
    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        """Return papers matching *query* published between *start* and *end*."""


registered_query_retrievers: dict[str, Type[BaseQueryRetriever]] = {}


def register_query_retriever(name: str):
    def decorator(cls):
        registered_query_retrievers[name] = cls
        cls.name = name
        return cls
    return decorator


def get_query_retriever_cls(name: str) -> Type[BaseQueryRetriever]:
    if name not in registered_query_retrievers:
        raise ValueError(f"Query retriever {name} not found")
    return registered_query_retrievers[name]
```

```python
# src/zotero_arxiv_daily/retriever/pubmed_retriever.py
"""PubMed retrieval over E-utilities (esearch then efetch).

An NCBI API key lifts the rate limit from 3 to 10 requests/second; it is
optional and the retriever works without one.
"""

from datetime import date
from xml.etree import ElementTree

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@register_query_retriever("pubmed")
class PubmedRetriever(BaseQueryRetriever):

    def _common_params(self) -> dict:
        params = {"db": "pubmed", "tool": self._setting("tool", "zotero-cmc-weekly")}
        email = self._setting("email")
        if email:
            params["email"] = email
        api_key = self._setting("api_key")
        if api_key:
            params["api_key"] = api_key
        return params

    def _esearch(self, query: str, start: date, end: date, limit: int) -> list[str]:
        params = self._common_params() | {
            "term": query,
            "retmode": "json",
            "retmax": limit,
            "datetype": "edat",
            "mindate": start.strftime("%Y/%m/%d"),
            "maxdate": end.strftime("%Y/%m/%d"),
        }
        response = http_get_with_retry(_ESEARCH, params=params)
        return list(response.json().get("esearchresult", {}).get("idlist", []))

    def _efetch(self, pmids: list[str]) -> str:
        params = self._common_params() | {"id": ",".join(pmids), "retmode": "xml"}
        return http_get_with_retry(_EFETCH, params=params).text

    @staticmethod
    def _article_to_paper(article: ElementTree.Element) -> Paper | None:
        title = "".join(article.itertext()) if article.find(".//ArticleTitle") is None else None
        title_node = article.find(".//ArticleTitle")
        if title_node is None:
            return None
        title = "".join(title_node.itertext()).strip()

        abstract_nodes = article.findall(".//Abstract/AbstractText")
        abstract = " ".join("".join(n.itertext()).strip() for n in abstract_nodes).strip()
        if not abstract:
            return None  # the reranker scores on abstracts; a paper without one is unusable

        authors = []
        for author in article.findall(".//AuthorList/Author"):
            last = author.findtext("LastName") or ""
            fore = author.findtext("ForeName") or ""
            name = f"{last} {fore}".strip()
            if name:
                authors.append(name)

        doi = None
        for article_id in article.findall(".//ArticleIdList/ArticleId"):
            if article_id.get("IdType") == "doi":
                doi = (article_id.text or "").strip() or None

        pub_date = None
        node = article.find('.//PubMedPubDate[@PubStatus="pubmed"]')
        if node is not None:
            try:
                pub_date = date(
                    int(node.findtext("Year")),
                    int(node.findtext("Month")),
                    int(node.findtext("Day")),
                )
            except (TypeError, ValueError):
                pub_date = None

        pmid = article.findtext(".//PMID") or ""
        return Paper(
            source="pubmed",
            title=title,
            authors=authors,
            abstract=abstract,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            doi=doi,
            journal=article.findtext(".//Journal/Title"),
            pub_date=pub_date,
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        try:
            pmids = self._esearch(query, start, end, limit)
            if not pmids:
                return []
            xml = self._efetch(pmids)
            root = ElementTree.fromstring(xml)
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"PubMed search failed for {query!r}: {exc}")
            return []

        papers = []
        for article in root.findall(".//PubmedArticle"):
            try:
                paper = self._article_to_paper(article)
            except Exception as exc:  # noqa: BLE001 - skip the bad record, keep the good ones
                logger.warning(f"Skipping an unparseable PubMed record: {exc}")
                continue
            if paper is not None:
                papers.append(paper)
        return papers
```

在 `src/zotero_arxiv_daily/retriever/__init__.py` 末尾追加：

```python
from .query_base import get_query_retriever_cls, registered_query_retrievers  # noqa: E402,F401
from . import pubmed_retriever  # noqa: E402,F401
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/retriever -q`
Expected: PASS — 新增 9 条 + 既有检索器测试全绿

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/retriever src/zotero_arxiv_daily/utils.py tests/retriever
git commit -m "feat: add query-style retriever registry and PubMed source"
```

---

## Task 9: Europe PMC 与 Crossref 检索器

**Files:**
- Create: `src/zotero_arxiv_daily/retriever/europepmc_retriever.py`
- Create: `src/zotero_arxiv_daily/retriever/crossref_retriever.py`
- Modify: `src/zotero_arxiv_daily/retriever/__init__.py`
- Test: `tests/retriever/test_europepmc_retriever.py`
- Test: `tests/retriever/test_crossref_retriever.py`

**Interfaces:**
- Consumes: `BaseQueryRetriever`、`http_get_with_retry`（Task 8）
- Produces: `EuropepmcRetriever`、`CrossrefRetriever`（均实现 `search`）

Europe PMC 免 key，且直接告诉你哪些有 OA 全文（`isOpenAccess` / `hasPDF`），是全文阶梯的重要输入。Crossref 走 polite pool，需在 UA 里带 mailto。

- [ ] **Step 1: 写失败测试**

```python
# tests/retriever/test_europepmc_retriever.py
"""Europe PMC REST retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.europepmc_retriever import EuropepmcRetriever

RESPONSE = {
    "resultList": {
        "result": [
            {
                "id": "40000001",
                "doi": "10.1016/j.chroma.2026.01.001",
                "title": "Host cell protein quantitation by LC-MS",
                "abstractText": "A validated HCP assay.",
                "authorString": "Smith J, Doe A",
                "journalTitle": "J Chromatogr A",
                "firstPublicationDate": "2026-08-18",
                "isOpenAccess": "Y",
                "pmcid": "PMC1234567",
            },
            {
                "id": "40000002",
                "title": "No abstract here",
                "authorString": "Lee K",
                "firstPublicationDate": "2026-08-19",
                "isOpenAccess": "N",
            },
        ]
    }
}


@pytest.fixture()
def mock_epmc(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_europepmc_parses_results(config, mock_epmc):
    papers = EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # abstract-less record dropped
    paper = papers[0]
    assert paper.title == "Host cell protein quantitation by LC-MS"
    assert paper.doi == "10.1016/j.chroma.2026.01.001"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.oa_status == "open"


def test_europepmc_marks_closed_access(config, monkeypatch):
    closed = {"resultList": {"result": [dict(RESPONSE["resultList"]["result"][0], isOpenAccess="N")]}}
    monkeypatch.setattr(requests, "get", lambda url, **kw: SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: closed))
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    papers = EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert papers[0].oa_status == "closed"


def test_europepmc_embeds_the_date_window_in_the_query(config, mock_epmc):
    EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_epmc[0]
    assert "FIRST_PDATE:[2026-08-15 TO 2026-08-21]" in params["query"]
    assert params["pageSize"] == 20


def test_europepmc_returns_nothing_for_an_empty_query(config, mock_epmc):
    assert EuropepmcRetriever(config).search("  ", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_epmc == []


def test_europepmc_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20) == []
```

```python
# tests/retriever/test_crossref_retriever.py
"""Crossref REST retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.crossref_retriever import CrossrefRetriever

RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1021/acs.analchem.6b00001",
                "title": ["Size variant analysis by SEC-MALS"],
                "abstract": "<jats:p>We report a SEC-MALS method.</jats:p>",
                "author": [{"family": "Smith", "given": "J"}, {"family": "Doe", "given": "A"}],
                "container-title": ["Anal Chem"],
                "created": {"date-parts": [[2026, 8, 18]]},
            },
            {
                "DOI": "10.1021/acs.analchem.6b00002",
                "title": ["No abstract"],
                "author": [],
                "container-title": ["Anal Chem"],
                "created": {"date-parts": [[2026, 8, 19]]},
            },
        ]
    }
}


@pytest.fixture()
def mock_crossref(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {}), kwargs.get("headers", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_crossref_parses_items_and_strips_jats(config, mock_crossref):
    papers = CrossrefRetriever(config).search("SEC-MALS", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "Size variant analysis by SEC-MALS"
    assert paper.abstract == "We report a SEC-MALS method."
    assert paper.journal == "Anal Chem"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]


def test_crossref_sends_the_date_filter_and_polite_header(config, mock_crossref):
    from omegaconf import open_dict

    with open_dict(config.source):
        config.source.crossref = {"mailto": "someone@example.org"}
    CrossrefRetriever(config).search("SEC-MALS", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params, headers = mock_crossref[0]
    assert "from-created-date:2026-08-15" in params["filter"]
    assert "until-created-date:2026-08-21" in params["filter"]
    assert params["rows"] == 20
    assert "someone@example.org" in headers["User-Agent"]


def test_crossref_returns_nothing_for_an_empty_query(config, mock_crossref):
    assert CrossrefRetriever(config).search("", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_crossref == []


def test_crossref_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert CrossrefRetriever(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 20) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/retriever/test_europepmc_retriever.py tests/retriever/test_crossref_retriever.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.retriever.europepmc_retriever'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/retriever/europepmc_retriever.py
"""Europe PMC retrieval.

No API key required, and the response says outright whether a record has open
full text — which feeds straight into the full-text ladder.
"""

from datetime import date, datetime

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@register_query_retriever("europepmc")
class EuropepmcRetriever(BaseQueryRetriever):

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        except ValueError:
            return None

    def _to_paper(self, item: dict) -> Paper | None:
        abstract = (item.get("abstractText") or "").strip()
        if not abstract:
            return None
        authors = [a.strip() for a in (item.get("authorString") or "").split(",") if a.strip()]
        pmcid = item.get("pmcid")
        return Paper(
            source="europepmc",
            title=(item.get("title") or "").strip().rstrip("."),
            authors=authors,
            abstract=abstract,
            url=f"https://europepmc.org/article/MED/{item.get('id')}",
            doi=item.get("doi"),
            journal=item.get("journalTitle"),
            pub_date=self._parse_date(item.get("firstPublicationDate")),
            oa_status="open" if item.get("isOpenAccess") == "Y" else "closed",
            pdf_url=(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                if pmcid and item.get("isOpenAccess") == "Y"
                else None
            ),
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        windowed = f"({query}) AND (FIRST_PDATE:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}])"
        params = {"query": windowed, "format": "json", "pageSize": limit, "resultType": "core"}
        try:
            payload = http_get_with_retry(_SEARCH, params=params).json()
            items = payload.get("resultList", {}).get("result", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"Europe PMC search failed for {query!r}: {exc}")
            return []
        papers = []
        for item in items:
            paper = self._to_paper(item)
            if paper is not None:
                papers.append(paper)
        return papers
```

```python
# src/zotero_arxiv_daily/retriever/crossref_retriever.py
"""Crossref retrieval.

Crossref's polite pool wants a contact address in the User-Agent; supplying
one buys better latency and fewer throttles.  Abstracts arrive as JATS XML
fragments and are flattened to plain text.
"""

import re
from datetime import date

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_WORKS = "https://api.crossref.org/works"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@register_query_retriever("crossref")
class CrossrefRetriever(BaseQueryRetriever):

    @staticmethod
    def _strip_jats(raw: str | None) -> str:
        if not raw:
            return ""
        return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()

    @staticmethod
    def _parse_date(item: dict) -> date | None:
        parts = (item.get("created") or {}).get("date-parts") or []
        if not parts or not parts[0]:
            return None
        values = (list(parts[0]) + [1, 1])[:3]
        try:
            return date(int(values[0]), int(values[1]), int(values[2]))
        except (TypeError, ValueError):
            return None

    def _to_paper(self, item: dict) -> Paper | None:
        abstract = self._strip_jats(item.get("abstract"))
        if not abstract:
            return None
        titles = item.get("title") or []
        containers = item.get("container-title") or []
        authors = [
            f"{a.get('family', '')} {a.get('given', '')}".strip()
            for a in item.get("author") or []
            if a.get("family") or a.get("given")
        ]
        doi = item.get("DOI")
        return Paper(
            source="crossref",
            title=(titles[0] if titles else "").strip(),
            authors=authors,
            abstract=abstract,
            url=f"https://doi.org/{doi}" if doi else "",
            doi=doi,
            journal=containers[0] if containers else None,
            pub_date=self._parse_date(item),
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        params = {
            "query.bibliographic": query,
            "rows": limit,
            "filter": (
                f"from-created-date:{start:%Y-%m-%d},"
                f"until-created-date:{end:%Y-%m-%d},"
                "type:journal-article"
            ),
            "select": "DOI,title,abstract,author,container-title,created",
        }
        mailto = self._setting("mailto")
        agent = "zotero-cmc-weekly/1.0"
        headers = {"User-Agent": f"{agent} (mailto:{mailto})" if mailto else agent}
        try:
            payload = http_get_with_retry(_WORKS, params=params, headers=headers).json()
            items = payload.get("message", {}).get("items", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"Crossref search failed for {query!r}: {exc}")
            return []
        papers = []
        for item in items:
            paper = self._to_paper(item)
            if paper is not None:
                papers.append(paper)
        return papers
```

在 `src/zotero_arxiv_daily/retriever/__init__.py` 的 import 行补上两个模块：

```python
from . import pubmed_retriever, europepmc_retriever, crossref_retriever  # noqa: E402,F401
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/retriever -q`
Expected: PASS — 新增 9 条 + 既有全绿

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/retriever tests/retriever
git commit -m "feat: add Europe PMC and Crossref query retrievers"
```

---

## Task 10: OpenAlex 检索器与高引补位

**Files:**
- Create: `src/zotero_arxiv_daily/retriever/openalex_retriever.py`
- Create: `src/zotero_arxiv_daily/backfill.py`
- Modify: `src/zotero_arxiv_daily/retriever/__init__.py`
- Test: `tests/retriever/test_openalex_retriever.py`
- Test: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `BaseQueryRetriever`、`Paper`、`QueryProfile`（Task 7）、`dedup_papers` / `drop_seen`（Task 3）
- Produces: `OpenalexRetriever.search(...)`、`OpenalexRetriever.search_highly_cited(query, limit) -> list[Paper]`、`backfill_papers(profiles, retriever, needed, exclude_dois) -> list[Paper]`

OpenAlex 的 `abstract_inverted_index` 要还原成正文；`cited_by_count` 是补位排序的依据（8.5 节）。

- [ ] **Step 1: 写失败测试**

```python
# tests/retriever/test_openalex_retriever.py
"""OpenAlex retrieval, including the highly-cited backfill query."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.openalex_retriever import OpenalexRetriever, invert_abstract

WORK = {
    "doi": "https://doi.org/10.1016/j.chroma.2026.01.001",
    "title": "Charge heterogeneity of therapeutic proteins",
    "abstract_inverted_index": {"A": [0], "cIEF": [1], "method": [2]},
    "authorships": [{"author": {"display_name": "J Smith"}}, {"author": {"display_name": "A Doe"}}],
    "primary_location": {"source": {"display_name": "J Chromatogr A"}},
    "publication_date": "2026-08-18",
    "cited_by_count": 137,
    "open_access": {"is_oa": True, "oa_url": "https://example.org/paper.pdf"},
}

RESPONSE = {"results": [WORK, dict(WORK, abstract_inverted_index=None, doi="https://doi.org/10.1000/no-abstract")]}


@pytest.fixture()
def mock_openalex(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_invert_abstract_restores_word_order():
    assert invert_abstract({"the": [0, 2], "cat": [1]}) == "the cat the"


def test_invert_abstract_handles_a_missing_index():
    assert invert_abstract(None) == ""


def test_openalex_parses_works(config, mock_openalex):
    papers = OpenalexRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # abstract-less work dropped
    paper = papers[0]
    assert paper.doi == "10.1016/j.chroma.2026.01.001"  # resolver prefix stripped
    assert paper.abstract == "A cIEF method"
    assert paper.cited_by_count == 137
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.oa_status == "open"
    assert paper.pdf_url == "https://example.org/paper.pdf"


def test_openalex_sends_the_publication_date_filter(config, mock_openalex):
    OpenalexRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_openalex[0]
    assert "from_publication_date:2026-08-15" in params["filter"]
    assert "to_publication_date:2026-08-21" in params["filter"]


def test_highly_cited_sorts_by_citation_count(config, mock_openalex):
    papers = OpenalexRetriever(config).search_highly_cited("cIEF", 5)
    _, params = mock_openalex[0]
    assert params["sort"] == "cited_by_count:desc"
    assert "from_publication_date" not in params.get("filter", "")
    assert papers[0].is_backfill is True


def test_openalex_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert OpenalexRetriever(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert OpenalexRetriever(config).search_highly_cited("q", 5) == []
```

```python
# tests/test_backfill.py
"""Highly-cited backfill when the week is thin (spec 8.5)."""

from zotero_arxiv_daily.backfill import backfill_papers
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.search.profile import QueryProfile


class StubRetriever:
    """Returns a fixed pool, recording how it was asked."""

    def __init__(self, pool):
        self.pool = pool
        self.calls = []

    def search_highly_cited(self, query, limit):
        self.calls.append((query, limit))
        return list(self.pool)


def make_paper(doi: str, cited: int) -> Paper:
    return Paper(
        source="openalex",
        title=f"Paper {doi}",
        authors=[],
        abstract="abs",
        url="u",
        doi=doi,
        cited_by_count=cited,
        is_backfill=True,
    )


PROFILES = [
    QueryProfile(cluster="a", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="charge variant"),
    QueryProfile(cluster="b", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="host cell protein"),
]


def test_backfill_returns_at_most_what_is_needed():
    pool = [make_paper(f"10.1000/{i}", 100 - i) for i in range(10)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=3, exclude_dois=set())
    assert len(result) == 3


def test_backfill_orders_by_citation_count():
    pool = [make_paper("10.1000/low", 5), make_paper("10.1000/high", 500), make_paper("10.1000/mid", 50)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=3, exclude_dois=set())
    assert [p.cited_by_count for p in result] == [500, 50, 5]


def test_backfill_excludes_dois_already_in_the_digest_or_library():
    pool = [make_paper("10.1000/a", 100), make_paper("10.1000/b", 90)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=5, exclude_dois={"10.1000/a"})
    assert [p.doi for p in result] == ["10.1000/b"]


def test_backfill_deduplicates_across_clusters():
    pool = [make_paper("10.1000/same", 100)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=5, exclude_dois=set())
    assert len(result) == 1


def test_backfill_tags_each_paper_with_its_cluster():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    result = backfill_papers(PROFILES[:1], retriever, needed=1, exclude_dois=set())
    assert result[0].cluster == "a"
    assert result[0].is_backfill is True


def test_backfill_does_nothing_when_nothing_is_needed():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    assert backfill_papers(PROFILES, retriever, needed=0, exclude_dois=set()) == []
    assert retriever.calls == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/retriever/test_openalex_retriever.py tests/test_backfill.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.retriever.openalex_retriever'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/retriever/openalex_retriever.py
"""OpenAlex retrieval.

Doubles as the backfill source: ``cited_by_count`` lets a thin week be topped
up with the field's established papers rather than padding with weak matches.
Abstracts arrive as an inverted index and must be reassembled.
"""

from datetime import date, datetime

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_WORKS = "https://api.openalex.org/works"


def invert_abstract(index: dict[str, list[int]] | None) -> str:
    """Rebuild plain text from OpenAlex's inverted abstract index."""
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        positions.extend((spot, word) for spot in spots)
    return " ".join(word for _, word in sorted(positions))


@register_query_retriever("openalex")
class OpenalexRetriever(BaseQueryRetriever):

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        except ValueError:
            return None

    def _to_paper(self, work: dict, *, is_backfill: bool = False) -> Paper | None:
        abstract = invert_abstract(work.get("abstract_inverted_index"))
        if not abstract:
            return None
        raw_doi = work.get("doi") or ""
        doi = raw_doi.replace("https://doi.org/", "").strip() or None
        source_block = (work.get("primary_location") or {}).get("source") or {}
        oa = work.get("open_access") or {}
        return Paper(
            source="openalex",
            title=(work.get("title") or "").strip(),
            authors=[
                (a.get("author") or {}).get("display_name", "")
                for a in work.get("authorships") or []
                if (a.get("author") or {}).get("display_name")
            ],
            abstract=abstract,
            url=f"https://doi.org/{doi}" if doi else work.get("id", ""),
            doi=doi,
            journal=source_block.get("display_name"),
            pub_date=self._parse_date(work.get("publication_date")),
            cited_by_count=work.get("cited_by_count"),
            oa_status="open" if oa.get("is_oa") else "closed",
            pdf_url=oa.get("oa_url"),
            is_backfill=is_backfill,
        )

    def _query(self, params: dict, *, is_backfill: bool) -> list[Paper]:
        mailto = self._setting("mailto")
        if mailto:
            params = params | {"mailto": mailto}
        try:
            results = http_get_with_retry(_WORKS, params=params).json().get("results", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"OpenAlex query failed: {exc}")
            return []
        papers = []
        for work in results:
            paper = self._to_paper(work, is_backfill=is_backfill)
            if paper is not None:
                papers.append(paper)
        return papers

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        params = {
            "search": query,
            "per-page": min(limit, 200),
            "filter": (
                f"from_publication_date:{start:%Y-%m-%d},"
                f"to_publication_date:{end:%Y-%m-%d},"
                "type:article"
            ),
        }
        return self._query(params, is_backfill=False)

    def search_highly_cited(self, query: str, limit: int) -> list[Paper]:
        """Return the most-cited papers matching *query*, any publication date."""
        if not query.strip():
            return []
        params = {
            "search": query,
            "per-page": min(limit, 200),
            "sort": "cited_by_count:desc",
            "filter": "type:article",
        }
        return self._query(params, is_backfill=True)
```

```python
# src/zotero_arxiv_daily/backfill.py
"""Top up a thin week with established, highly-cited work.

A week that yields fewer than the configured minimum is padded from OpenAlex
by citation count rather than by loosening the relevance bar — the digest
would rather show a known classic than a weak new match.  Backfilled papers
are tagged so the report can list them separately (spec 8.5).
"""

from loguru import logger

from .dedup import dedup_papers, normalize_doi
from .protocol import Paper
from .search.profile import QueryProfile

_OVERSAMPLE = 3


def backfill_papers(
    profiles: list[QueryProfile],
    retriever,
    needed: int,
    exclude_dois: set[str],
) -> list[Paper]:
    """Return up to *needed* highly-cited papers across *profiles*."""
    if needed <= 0 or not profiles:
        return []

    per_cluster = max(1, needed * _OVERSAMPLE // len(profiles))
    pool: list[Paper] = []
    for profile in profiles:
        found = retriever.search_highly_cited(profile.plain_query, per_cluster)
        for paper in found:
            paper.cluster = profile.cluster
            paper.is_backfill = True
        pool.extend(found)

    pool = [p for p in pool if (normalize_doi(p.doi) or "") not in exclude_dois]
    pool = dedup_papers(pool)
    pool.sort(key=lambda p: p.cited_by_count or 0, reverse=True)
    chosen = pool[:needed]
    logger.info(f"Backfilled {len(chosen)} highly-cited papers (needed {needed})")
    return chosen
```

在 `src/zotero_arxiv_daily/retriever/__init__.py` 的 import 行补上 openalex：

```python
from . import pubmed_retriever, europepmc_retriever, crossref_retriever, openalex_retriever  # noqa: E402,F401
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/retriever tests/test_backfill.py -q`
Expected: PASS — 新增 12 条 + 既有全绿

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/retriever src/zotero_arxiv_daily/backfill.py tests/
git commit -m "feat: add OpenAlex retriever and highly-cited backfill"
```

---

## Task 11: OA 全文获取阶梯

**Files:**
- Create: `src/zotero_arxiv_daily/fulltext/__init__.py`
- Create: `src/zotero_arxiv_daily/fulltext/resolver.py`
- Test: `tests/fulltext/__init__.py`
- Test: `tests/fulltext/test_resolver.py`

**Interfaces:**
- Consumes: `Paper`、`http_get_with_retry`（Task 8）、`extract_markdown_from_pdf`（既有）
- Produces: `FullTextResult`（dataclass: `pdf_bytes: bytes | None`、`oa_status: str`、`source: str | None`）、`resolve_pdf(paper, cfg) -> FullTextResult`、`download_fulltext(papers, cfg, out_dir) -> None`

阶梯顺序：**已知 OA 直链 → Unpaywall → Europe PMC OA → 预印本**。命中即停，未命中标 `closed` 并降级为仅摘要（发现 6）。**不做**上图代理自动下载（发现 4）。

- [ ] **Step 1: 写失败测试**

```python
# tests/fulltext/__init__.py  —— 空文件
```

```python
# tests/fulltext/test_resolver.py
"""The open-access full-text ladder."""

from types import SimpleNamespace

import pytest
import requests
from omegaconf import OmegaConf

from zotero_arxiv_daily.fulltext.resolver import download_fulltext, resolve_pdf
from zotero_arxiv_daily.protocol import Paper

PDF_BYTES = b"%PDF-1.7\nfake pdf body"


def make_paper(**kw) -> Paper:
    base = dict(source="pubmed", title="A paper", authors=[], abstract="abs", url="https://example.org/1")
    base.update(kw)
    return Paper(**base)


@pytest.fixture()
def cfg():
    return OmegaConf.create({"fulltext": {"enabled": True, "unpaywall_email": "someone@example.org", "max_bytes": 20_000_000}})


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)


def pdf_response(body=PDF_BYTES):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        content=body,
        headers={"Content-Type": "application/pdf"},
    )


def test_a_known_oa_pdf_url_is_used_directly(cfg, monkeypatch, no_sleep):
    seen = []

    def _patched(url, **kw):
        seen.append(url)
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(pdf_url="https://example.org/direct.pdf", oa_status="open"), cfg)
    assert result.pdf_bytes == PDF_BYTES
    assert result.source == "direct"
    assert seen == ["https://example.org/direct.pdf"]


def test_unpaywall_is_consulted_when_there_is_no_direct_url(cfg, monkeypatch, no_sleep):
    def _patched(url, **kw):
        if "unpaywall" in url:
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"best_oa_location": {"url_for_pdf": "https://oa.example.org/p.pdf"}},
            )
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1000/a"), cfg)
    assert result.pdf_bytes == PDF_BYTES
    assert result.source == "unpaywall"
    assert result.oa_status == "open"


def test_unpaywall_is_skipped_without_a_contact_email(monkeypatch, no_sleep):
    cfg = OmegaConf.create({"fulltext": {"enabled": True, "unpaywall_email": None, "max_bytes": 1000}})
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append(url) or pdf_response())
    resolve_pdf(make_paper(doi="10.1000/a"), cfg)
    assert not any("unpaywall" in c for c in calls)


def test_a_closed_paper_yields_no_bytes(cfg, monkeypatch, no_sleep):
    def _patched(url, **kw):
        if "unpaywall" in url:
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"best_oa_location": None})
        raise requests.HTTPError("403")

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1000/a"), cfg)
    assert result.pdf_bytes is None
    assert result.oa_status == "closed"


def test_a_non_pdf_body_is_rejected(cfg, monkeypatch, no_sleep):
    html = SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        content=b"<html>paywall</html>",
        headers={"Content-Type": "text/html"},
    )
    monkeypatch.setattr(requests, "get", lambda url, **kw: html)
    result = resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg)
    assert result.pdf_bytes is None


def test_an_oversized_pdf_is_rejected(monkeypatch, no_sleep):
    cfg = OmegaConf.create({"fulltext": {"enabled": True, "unpaywall_email": None, "max_bytes": 5}})
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    result = resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg)
    assert result.pdf_bytes is None


def test_disabling_fulltext_short_circuits_the_ladder(monkeypatch, no_sleep):
    cfg = OmegaConf.create({"fulltext": {"enabled": False, "unpaywall_email": None, "max_bytes": 1000}})
    monkeypatch.setattr(requests, "get", lambda url, **kw: pytest.fail("no request may be made"))
    assert resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg).pdf_bytes is None


def test_download_writes_pdfs_and_records_the_path(cfg, tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr(
        "zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf",
        lambda path: "# extracted markdown",
    )
    papers = [make_paper(doi="10.1016/j.chroma.2026.01.001", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is not None
    assert papers[0].pdf_path.endswith(".pdf")
    assert papers[0].full_text == "# extracted markdown"
    assert papers[0].oa_status == "open"


def test_download_sanitises_the_doi_into_a_filename(cfg, tmp_path, monkeypatch, no_sleep):
    import os

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    papers = [make_paper(doi="10.1016/j.chroma.2026.01.001", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    assert "/" not in name
    assert name == "10.1016_j.chroma.2026.01.001.pdf"


def test_download_leaves_closed_papers_abstract_only(cfg, tmp_path, monkeypatch, no_sleep):
    def _patched(url, **kw):
        raise requests.HTTPError("403")

    monkeypatch.setattr(requests, "get", _patched)
    papers = [make_paper(doi="10.1000/closed", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is None
    assert papers[0].full_text is None
    assert papers[0].oa_status == "closed"


def test_a_failed_extraction_still_keeps_the_pdf(cfg, tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())

    def _boom(path):
        raise RuntimeError("pymupdf exploded")

    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", _boom)
    papers = [make_paper(doi="10.1000/x", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is not None
    assert papers[0].full_text is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/fulltext -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.fulltext'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/fulltext/__init__.py
"""Open-access full-text acquisition."""
```

```python
# src/zotero_arxiv_daily/fulltext/resolver.py
"""Fetch open-access full text, in descending order of reliability.

The ladder is: a URL the source already handed us, then Unpaywall's best OA
location, then Europe PMC's OA service.  The first hit wins.  Anything that
stays behind a paywall is left abstract-only and flagged, so the report can
list it under "needs manual retrieval" — publisher-proxy automation is
deliberately out of scope (spec finding 4).
"""

import os
import re
from dataclasses import dataclass

from loguru import logger

from ..protocol import Paper
from ..utils import extract_markdown_from_pdf, http_get_with_retry

_UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
_EPMC_PDF = "https://europepmc.org/api/fulltextRepo?pprId={doi}&type=FILE&fileName=main.pdf"
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_PDF_MAGIC = b"%PDF"


@dataclass
class FullTextResult:
    pdf_bytes: bytes | None
    oa_status: str
    source: str | None = None


def _fetch_pdf(url: str, max_bytes: int) -> bytes | None:
    """GET *url* and return the body only if it really is a PDF of sane size."""
    try:
        response = http_get_with_retry(url, retries=2, timeout=60)
    except Exception as exc:  # noqa: BLE001 - a paywall is an ordinary outcome here
        logger.debug(f"Full-text fetch failed for {url}: {exc}")
        return None
    body = response.content or b""
    content_type = response.headers.get("Content-Type", "")
    if not body.startswith(_PDF_MAGIC) and "pdf" not in content_type.lower():
        logger.debug(f"{url} did not return a PDF (Content-Type: {content_type!r})")
        return None
    if len(body) > max_bytes:
        logger.debug(f"{url} returned {len(body)} bytes, over the {max_bytes} limit")
        return None
    return body


def _unpaywall_pdf_url(doi: str, email: str) -> str | None:
    try:
        payload = http_get_with_retry(_UNPAYWALL.format(doi=doi), params={"email": email}, retries=2).json()
    except Exception as exc:  # noqa: BLE001 - Unpaywall is best-effort
        logger.debug(f"Unpaywall lookup failed for {doi}: {exc}")
        return None
    location = payload.get("best_oa_location") or {}
    return location.get("url_for_pdf") or location.get("url")


def resolve_pdf(paper: Paper, config) -> FullTextResult:
    """Walk the OA ladder for *paper*, stopping at the first real PDF."""
    settings = config.fulltext
    if not settings.get("enabled", True):
        return FullTextResult(pdf_bytes=None, oa_status=paper.oa_status)

    max_bytes = int(settings.get("max_bytes") or 20_000_000)

    if paper.pdf_url:
        body = _fetch_pdf(paper.pdf_url, max_bytes)
        if body:
            return FullTextResult(pdf_bytes=body, oa_status="open", source="direct")

    email = settings.get("unpaywall_email")
    if paper.doi and email:
        url = _unpaywall_pdf_url(paper.doi, email)
        if url:
            body = _fetch_pdf(url, max_bytes)
            if body:
                return FullTextResult(pdf_bytes=body, oa_status="open", source="unpaywall")

    if paper.doi:
        body = _fetch_pdf(_EPMC_PDF.format(doi=paper.doi), max_bytes)
        if body:
            return FullTextResult(pdf_bytes=body, oa_status="open", source="europepmc")

    return FullTextResult(pdf_bytes=None, oa_status="closed")


def _filename_for(paper: Paper, index: int) -> str:
    stem = paper.doi or f"paper-{index}"
    return _UNSAFE_NAME_RE.sub("_", stem) + ".pdf"


def download_fulltext(papers: list[Paper], config, out_dir: str) -> None:
    """Resolve, save and text-extract full text for *papers*, in place."""
    os.makedirs(out_dir, exist_ok=True)
    hits = 0
    for index, paper in enumerate(papers):
        result = resolve_pdf(paper, config)
        paper.oa_status = result.oa_status
        if result.pdf_bytes is None:
            continue
        path = os.path.join(out_dir, _filename_for(paper, index))
        with open(path, "wb") as handle:
            handle.write(result.pdf_bytes)
        paper.pdf_path = path
        hits += 1
        try:
            paper.full_text = extract_markdown_from_pdf(path)
        except Exception as exc:  # noqa: BLE001 - keep the PDF even if extraction fails
            logger.warning(f"Failed to extract markdown from {path}: {exc}")
            paper.full_text = None
    logger.info(f"Full text resolved for {hits}/{len(papers)} papers")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/fulltext -q`
Expected: PASS — 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/fulltext tests/fulltext
git commit -m "feat: add the open-access full-text ladder"
```

---

## Task 12: 字段可配置的结构化抽取

**Files:**
- Modify: `src/zotero_arxiv_daily/utils.py`（新增 `truncate_for_prompt`）
- Create: `src/zotero_arxiv_daily/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `Paper`
- Produces: `truncate_for_prompt(text: str, max_tokens: int) -> str`；`FieldSpec`（dataclass: `key`、`label`、`instruction`）；`load_field_specs(config) -> list[FieldSpec]`；`extract_paper(paper, client, llm_params, fields) -> dict[str, str]`；`extract_all(papers, client, llm_params, fields) -> None`

这是「输出内容可定制化」的落点：增删字段只改 `config/base.yaml` 的 `report.fields`，不动一行代码。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_extract.py
"""Config-driven structured extraction."""

import json
from types import SimpleNamespace

from omegaconf import OmegaConf

from zotero_arxiv_daily.extract import (
    FieldSpec,
    extract_all,
    extract_paper,
    load_field_specs,
)
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.utils import truncate_for_prompt

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}

FIELDS = [
    FieldSpec(key="background", label="背景", instruction="研究背景"),
    FieldSpec(key="gap", label="待解决的问题", instruction="尚未解决的问题"),
    FieldSpec(key="method", label="方法", instruction="所用方法"),
]

PAYLOAD = json.dumps({"background": "单抗电荷异质性", "gap": "缺乏快速方法", "method": "cIEF"}, ensure_ascii=False)


def stub_client(payload: str, recorder: list | None = None) -> SimpleNamespace:
    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_paper(**kw) -> Paper:
    base = dict(source="pubmed", title="A paper", authors=[], abstract="An abstract.", url="u")
    base.update(kw)
    return Paper(**base)


def test_truncate_returns_short_text_unchanged():
    assert truncate_for_prompt("short", 100) == "short"


def test_truncate_shortens_long_text_without_network_access():
    long_text = "word " * 5000
    result = truncate_for_prompt(long_text, 50)
    assert len(result) < len(long_text)


def test_truncate_handles_empty_text():
    assert truncate_for_prompt("", 100) == ""


def test_extract_returns_every_configured_field():
    result = extract_paper(make_paper(), stub_client(PAYLOAD), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert result["method"] == "cIEF"


def test_extract_fills_missing_fields_rather_than_omitting_them():
    partial = json.dumps({"background": "只有背景"}, ensure_ascii=False)
    result = extract_paper(make_paper(), stub_client(partial), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert result["gap"] == ""


def test_extract_degrades_to_empty_fields_on_bad_json():
    result = extract_paper(make_paper(), stub_client("not json"), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert all(v == "" for v in result.values())


def test_extract_prefers_full_text_when_available():
    recorder = []
    paper = make_paper(full_text="FULL TEXT BODY")
    extract_paper(paper, stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "FULL TEXT BODY" in prompt


def test_extract_falls_back_to_the_abstract_without_full_text():
    recorder = []
    extract_paper(make_paper(), stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "An abstract." in prompt


def test_extract_asks_for_exactly_the_configured_field_keys():
    recorder = []
    extract_paper(make_paper(), stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    for field in FIELDS:
        assert field.key in prompt
        assert field.label in prompt


def test_extract_all_writes_onto_every_paper():
    papers = [make_paper(title="a"), make_paper(title="b")]
    extract_all(papers, stub_client(PAYLOAD), LLM_PARAMS, FIELDS)
    assert all(p.extraction is not None for p in papers)
    assert papers[0].extraction["method"] == "cIEF"


def test_one_failing_paper_does_not_abort_the_batch():
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=PAYLOAD))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    papers = [make_paper(title="a"), make_paper(title="b")]
    extract_all(papers, client, LLM_PARAMS, FIELDS)
    assert papers[0].extraction == {"background": "", "gap": "", "method": ""}
    assert papers[1].extraction["method"] == "cIEF"


def test_field_specs_come_from_config():
    config = OmegaConf.create(
        {"report": {"fields": [{"key": "insight", "label": "洞见", "instruction": "对我的启发"}]}}
    )
    specs = load_field_specs(config)
    assert specs == [FieldSpec(key="insight", label="洞见", instruction="对我的启发")]


def test_field_specs_tolerate_a_missing_instruction():
    config = OmegaConf.create({"report": {"fields": [{"key": "insight", "label": "洞见"}]}})
    assert load_field_specs(config)[0].instruction == "洞见"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_extract.py -q`
Expected: FAIL — `ImportError: cannot import name 'truncate_for_prompt'`

- [ ] **Step 3: 最小实现**

在 `src/zotero_arxiv_daily/utils.py` 末尾追加：

```python
_CHARS_PER_TOKEN = 4  # conservative for mixed Chinese/English text


def truncate_for_prompt(text: str, max_tokens: int) -> str:
    """Trim *text* to roughly *max_tokens* tokens.

    Prefers a real tokenizer, but falls back to a character estimate when
    tiktoken cannot reach its encoding files — the weekly run must not depend
    on a model-vocabulary download succeeding at runtime.
    """
    if not text:
        return text
    try:
        import tiktoken

        enc = tiktoken.encoding_for_model("gpt-4o")
        return enc.decode(enc.encode(text)[:max_tokens])
    except Exception as exc:  # noqa: BLE001 - offline or blocked; estimate instead
        logger.debug(f"tiktoken unavailable ({exc}); truncating by character count")
        return text[: max_tokens * _CHARS_PER_TOKEN]
```

```python
# src/zotero_arxiv_daily/extract.py
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_extract.py -q`
Expected: PASS — 14 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/extract.py src/zotero_arxiv_daily/utils.py tests/test_extract.py
git commit -m "feat: add config-driven structured field extraction"
```

---

## Task 13: 三层渲染

**Files:**
- Create: `src/zotero_arxiv_daily/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `Paper`、`FieldSpec`（Task 12）、`week_label` / `week_window`（Task 1）
- Produces: `Digest`（dataclass: `label`、`start`、`end`、`clusters: list[tuple[str, list[Paper]]]`、`backfill: list[Paper]`、`top_picks: list[Paper]`、`needs_manual: list[Paper]`）、`build_digest(papers, backfill, anchor, top_n) -> Digest`、`render_markdown(digest, fields) -> str`、`render_web_html(digest, fields) -> str`、`render_email_html(digest, fields, max_bytes=102_000) -> str`

发现 11 的落点。三份产物同源不同形：md 归档、网页 HTML 样式不受限、**邮件 HTML 必须 table 布局 + 内联样式**（Gmail 剥 `@font-face`、CSS 变量不生效、Outlook 走 Word 引擎无 flex/grid），且压在 102KB 内否则 Gmail 截断。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report.py
"""Three-tier rendering: markdown, web HTML, email HTML."""

from datetime import date

from zotero_arxiv_daily.extract import FieldSpec
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.report import (
    build_digest,
    render_email_html,
    render_markdown,
    render_web_html,
)

FIELDS = [
    FieldSpec(key="background", label="背景", instruction="i"),
    FieldSpec(key="insight", label="洞见", instruction="i"),
]


def make_paper(title, cluster, score, **kw) -> Paper:
    base = dict(
        source="pubmed",
        title=title,
        authors=["Smith J"],
        abstract="abs",
        url="https://example.org/" + title,
        score=score,
        cluster=cluster,
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
        doi="10.1000/" + title,
        extraction={"background": f"{title} 背景", "insight": f"{title} 洞见"},
    )
    base.update(kw)
    return Paper(**base)


def sample_digest():
    papers = [
        make_paper("alpha", "电荷异质性", 9.0),
        make_paper("beta", "电荷异质性", 8.0),
        make_paper("gamma", "宿主细胞蛋白", 7.0, oa_status="closed"),
    ]
    backfill = [make_paper("classic", "电荷异质性", 6.0, is_backfill=True, cited_by_count=900)]
    return build_digest(papers, backfill, date(2026, 8, 21), top_n=2)


def test_digest_carries_the_week_label_and_window():
    digest = sample_digest()
    assert digest.label == "2026-08-W3"
    assert digest.start == date(2026, 8, 15)
    assert digest.end == date(2026, 8, 21)


def test_digest_groups_papers_by_cluster():
    digest = sample_digest()
    assert [name for name, _ in digest.clusters] == ["电荷异质性", "宿主细胞蛋白"]
    assert [p.title for p in digest.clusters[0][1]] == ["alpha", "beta"]


def test_digest_top_picks_are_the_highest_scoring_new_papers():
    digest = sample_digest()
    assert [p.title for p in digest.top_picks] == ["alpha", "beta"]


def test_backfill_is_kept_out_of_the_cluster_sections():
    digest = sample_digest()
    clustered = [p.title for _, papers in digest.clusters for p in papers]
    assert "classic" not in clustered
    assert [p.title for p in digest.backfill] == ["classic"]


def test_closed_access_papers_are_listed_for_manual_retrieval():
    digest = sample_digest()
    assert [p.title for p in digest.needs_manual] == ["gamma"]


def test_markdown_contains_the_label_window_and_every_paper():
    text = render_markdown(sample_digest(), FIELDS)
    assert "2026-08-W3" in text
    assert "2026-08-15" in text and "2026-08-21" in text
    for title in ("alpha", "beta", "gamma", "classic"):
        assert title in text


def test_markdown_renders_each_configured_field_label():
    text = render_markdown(sample_digest(), FIELDS)
    assert "背景" in text
    assert "洞见" in text
    assert "alpha 洞见" in text


def test_markdown_links_dois_not_repository_paths():
    text = render_markdown(sample_digest(), FIELDS)
    assert "https://doi.org/10.1000/alpha" in text
    assert "library/" not in text


def test_markdown_labels_the_backfill_section():
    text = render_markdown(sample_digest(), FIELDS)
    assert "经典补位" in text


def test_web_html_is_a_standalone_document():
    html = render_web_html(sample_digest(), FIELDS)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html.rstrip()
    assert "prefers-color-scheme" in html


def test_web_html_escapes_markup_in_paper_content():
    papers = [make_paper("x<script>alert(1)</script>", "c", 1.0)]
    html = render_web_html(build_digest(papers, [], date(2026, 8, 21), top_n=1), FIELDS)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_email_html_uses_table_layout_only():
    html = render_email_html(sample_digest(), FIELDS)
    assert "<table" in html
    assert "display:flex" not in html.replace(" ", "")
    assert "display:grid" not in html.replace(" ", "")
    assert "var(--" not in html
    assert "@font-face" not in html


def test_email_html_leads_with_the_top_picks():
    html = render_email_html(sample_digest(), FIELDS)
    assert html.index("alpha") < html.index("gamma")
    assert "优先" in html


def test_email_html_stays_under_the_gmail_clip_threshold():
    papers = [make_paper(f"paper{i}", "c", float(i)) for i in range(200)]
    html = render_email_html(build_digest(papers, [], date(2026, 8, 21), top_n=3), FIELDS)
    assert len(html.encode("utf-8")) <= 102_000


def test_email_html_says_so_when_it_had_to_truncate():
    papers = [make_paper(f"paper{i}", "c", float(i)) for i in range(200)]
    html = render_email_html(build_digest(papers, [], date(2026, 8, 21), top_n=3), FIELDS, max_bytes=4000)
    assert "完整周报见附件" in html
    assert len(html.encode("utf-8")) <= 4000


def test_an_empty_digest_still_renders():
    digest = build_digest([], [], date(2026, 8, 21), top_n=3)
    assert "2026-08-W3" in render_markdown(digest, FIELDS)
    assert "2026-08-W3" in render_email_html(digest, FIELDS)
    assert "2026-08-W3" in render_web_html(digest, FIELDS)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.report'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/report.py
"""Render one digest three ways.

Email HTML and web HTML are not the same medium.  Gmail strips @font-face and
ignores CSS variables, Outlook renders through Word (no flexbox, no grid), and
Gmail clips a message past ~102KB.  So the email body is a table-based,
inline-styled summary that leads with the papers worth reading first, while
the archived web page is free to use the full stylesheet.
"""

from dataclasses import dataclass, field
from datetime import date
from html import escape

from .extract import FieldSpec
from .protocol import Paper
from .weeknum import week_label, week_window

EMAIL_MAX_BYTES = 102_000
_TRUNCATION_NOTE = "完整周报见附件。"


@dataclass
class Digest:
    label: str
    start: date
    end: date
    clusters: list[tuple[str, list[Paper]]] = field(default_factory=list)
    backfill: list[Paper] = field(default_factory=list)
    top_picks: list[Paper] = field(default_factory=list)
    needs_manual: list[Paper] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(papers) for _, papers in self.clusters) + len(self.backfill)


def build_digest(papers: list[Paper], backfill: list[Paper], anchor: date, top_n: int = 3) -> Digest:
    """Group *papers* into the shape the renderers consume."""
    start, end = week_window(anchor)
    fresh = [p for p in papers if not p.is_backfill]

    grouped: dict[str, list[Paper]] = {}
    for paper in fresh:
        grouped.setdefault(paper.cluster or "未分类", []).append(paper)
    for bucket in grouped.values():
        bucket.sort(key=lambda p: -(p.score or 0.0))

    ordered = sorted(grouped.items(), key=lambda kv: (-max((p.score or 0.0) for p in kv[1]), kv[0]))

    return Digest(
        label=week_label(anchor),
        start=start,
        end=end,
        clusters=ordered,
        backfill=sorted(backfill, key=lambda p: -(p.cited_by_count or 0)),
        top_picks=sorted(fresh, key=lambda p: -(p.score or 0.0))[:top_n],
        needs_manual=[p for p in fresh if p.oa_status != "open"],
    )


def _byline(paper: Paper) -> str:
    bits = []
    if paper.journal:
        bits.append(paper.journal)
    if paper.pub_date:
        bits.append(paper.pub_date.isoformat())
    if paper.authors:
        bits.append(paper.authors[0] + (" et al." if len(paper.authors) > 1 else ""))
    return " · ".join(bits)


# --------------------------------------------------------------------------- markdown

def render_markdown(digest: Digest, fields: list[FieldSpec]) -> str:
    lines = [
        f"# CMC 文献周报 {digest.label}",
        "",
        f"**覆盖期：** {digest.start.isoformat()} ~ {digest.end.isoformat()}　**共 {digest.total} 篇**",
        "",
    ]

    if digest.top_picks:
        lines += ["## 本周优先读", ""]
        for paper in digest.top_picks:
            link = paper.doi_url or paper.url
            lines.append(f"- [{paper.title}]({link})　{_byline(paper)}")
        lines.append("")

    for name, papers in digest.clusters:
        lines += [f"## {name}（{len(papers)} 篇）", ""]
        for paper in papers:
            lines += _markdown_entry(paper, fields)

    if digest.backfill:
        lines += ["## 经典补位", "", "> 本周新文献不足，以下为依据文献库检索到的高引经典文献。", ""]
        for paper in digest.backfill:
            lines += _markdown_entry(paper, fields, extra=f"被引 {paper.cited_by_count or 0}")

    if digest.needs_manual:
        lines += ["## 需人工取全文", ""]
        for paper in digest.needs_manual:
            lines.append(f"- [{paper.title}]({paper.doi_url or paper.url})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _markdown_entry(paper: Paper, fields: list[FieldSpec], extra: str = "") -> list[str]:
    link = paper.doi_url or paper.url
    head = f"### {paper.title}"
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    lines = [head, "", f"{meta}", "", f"DOI: <{link}>", ""]
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if value:
            lines += [f"**{spec.label}：** {value}", ""]
    return lines


# --------------------------------------------------------------------------- web html

_WEB_CSS = """
:root{--bg:#FBFAF7;--fg:#1F2421;--muted:#5C6660;--rule:#E2E0D8;--accent:#0E5E5A;--card:#FFFFFF}
:root:not([data-theme="light"]){}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14181A;--fg:#E8E6E0;--muted:#9AA5A0;--rule:#2A3033;--accent:#5FB8B2;--card:#1B2124}}
:root[data-theme="dark"]{--bg:#14181A;--fg:#E8E6E0;--muted:#9AA5A0;--rule:#2A3033;--accent:#5FB8B2;--card:#1B2124}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
 font-family:"Noto Sans SC","Helvetica Neue",Arial,sans-serif;line-height:1.7}
main{max-width:52rem;margin:0 auto}
h1{font-size:1.9rem;margin:0 0 .3rem}
h2{font-size:1.25rem;margin:2.5rem 0 .75rem;padding-bottom:.35rem;border-bottom:2px solid var(--accent)}
h3{font-size:1.05rem;margin:1.6rem 0 .3rem}
.meta{color:var(--muted);font-size:.86rem}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:1rem 1.15rem;margin:.9rem 0}
.field{margin:.5rem 0}
.field b{color:var(--accent)}
a{color:var(--accent)}
.pill{display:inline-block;font-size:.75rem;padding:.1rem .5rem;border:1px solid var(--rule);border-radius:999px;color:var(--muted)}
""".strip()


def render_web_html(digest: Digest, fields: list[FieldSpec]) -> str:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>CMC 文献周报 {escape(digest.label)}</title>",
        f"<style>{_WEB_CSS}</style></head><body><main>",
        f"<h1>CMC 文献周报 {escape(digest.label)}</h1>",
        f'<p class="meta">覆盖期 {digest.start.isoformat()} ~ {digest.end.isoformat()}　共 {digest.total} 篇</p>',
    ]

    if digest.top_picks:
        parts.append("<h2>本周优先读</h2><ol>")
        for paper in digest.top_picks:
            link = escape(paper.doi_url or paper.url)
            parts.append(f'<li><a href="{link}">{escape(paper.title)}</a> <span class="meta">{escape(_byline(paper))}</span></li>')
        parts.append("</ol>")

    for name, papers in digest.clusters:
        parts.append(f"<h2>{escape(name)}<span class='pill'>{len(papers)} 篇</span></h2>")
        parts.extend(_html_card(p, fields) for p in papers)

    if digest.backfill:
        parts.append("<h2>经典补位</h2><p class='meta'>本周新文献不足，以下为依据文献库检索到的高引经典文献。</p>")
        parts.extend(_html_card(p, fields, extra=f"被引 {p.cited_by_count or 0}") for p in digest.backfill)

    if digest.needs_manual:
        parts.append("<h2>需人工取全文</h2><ul>")
        for paper in digest.needs_manual:
            parts.append(f'<li><a href="{escape(paper.doi_url or paper.url)}">{escape(paper.title)}</a></li>')
        parts.append("</ul>")

    parts.append("</main></body></html>")
    return "\n".join(parts)


def _html_card(paper: Paper, fields: list[FieldSpec], extra: str = "") -> str:
    link = escape(paper.doi_url or paper.url)
    meta = _byline(paper)
    if extra:
        meta = f"{meta} · {extra}" if meta else extra
    rows = [f'<div class="card"><h3><a href="{link}">{escape(paper.title)}</a></h3>',
            f'<p class="meta">{escape(meta)}</p>']
    for spec in fields:
        value = (paper.extraction or {}).get(spec.key, "")
        if value:
            rows.append(f'<div class="field"><b>{escape(spec.label)}</b>：{escape(value)}</div>')
    rows.append("</div>")
    return "".join(rows)


# --------------------------------------------------------------------------- email html

_EMAIL_WRAP_OPEN = (
    '<div style="margin:0;padding:16px;background:#FBFAF7;'
    'font-family:-apple-system,BlinkMacSystemFont,\'Helvetica Neue\',Arial,sans-serif;color:#1F2421">'
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
    'style="max-width:640px;margin:0 auto;background:#FFFFFF;border:1px solid #E2E0D8;border-radius:8px">'
    '<tr><td style="padding:20px 22px">'
)
_EMAIL_WRAP_CLOSE = "</td></tr></table></div>"


def render_email_html(digest: Digest, fields: list[FieldSpec], max_bytes: int = EMAIL_MAX_BYTES) -> str:
    """Render the email body, trimming sections until it fits *max_bytes*.

    Table layout and inline styles only: Gmail drops @font-face and CSS
    variables, and Outlook has no flexbox or grid.
    """
    header = (
        f'<h1 style="margin:0 0 4px;font-size:20px">CMC 文献周报 {escape(digest.label)}</h1>'
        f'<p style="margin:0 0 16px;color:#5C6660;font-size:13px">'
        f"覆盖期 {digest.start.isoformat()} ~ {digest.end.isoformat()}　共 {digest.total} 篇</p>"
    )

    blocks: list[str] = []
    if digest.top_picks:
        blocks.append(_email_heading("本周优先读"))
        blocks.extend(_email_pick(p, fields) for p in digest.top_picks)

    for name, papers in digest.clusters:
        blocks.append(_email_heading(f"{escape(name)}（{len(papers)} 篇）"))
        blocks.append(_email_list(papers))

    if digest.backfill:
        blocks.append(_email_heading("经典补位"))
        blocks.append(_email_list(digest.backfill))

    footer = (
        f'<p style="margin:18px 0 0;color:#5C6660;font-size:12px">'
        f"{_TRUNCATION_NOTE}</p>"
    )

    def assemble(kept: list[str], note: str = "") -> str:
        return _EMAIL_WRAP_OPEN + header + "".join(kept) + note + footer + _EMAIL_WRAP_CLOSE

    html = assemble(blocks)
    if len(html.encode("utf-8")) <= max_bytes:
        return html

    note = f'<p style="margin:14px 0 0;color:#94372C;font-size:12px">内容较多，正文已截断。{_TRUNCATION_NOTE}</p>'
    kept: list[str] = []
    for block in blocks:
        candidate = assemble(kept + [block], note)
        if len(candidate.encode("utf-8")) > max_bytes:
            break
        kept.append(block)
    return assemble(kept, note)


def _email_heading(text: str) -> str:
    return (
        f'<p style="margin:18px 0 6px;font-size:15px;font-weight:700;'
        f'color:#0E5E5A;border-bottom:2px solid #0E5E5A;padding-bottom:4px">{text}</p>'
    )


def _email_pick(paper: Paper, fields: list[FieldSpec]) -> str:
    link = escape(paper.doi_url or paper.url)
    first_field = next(((paper.extraction or {}).get(f.key, "") for f in fields if (paper.extraction or {}).get(f.key)), "")
    body = (
        f'<p style="margin:2px 0 0;font-size:13px;color:#3C4642">{escape(first_field)}</p>'
        if first_field
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:8px 0;border-left:3px solid #0E5E5A"><tr><td style="padding:2px 0 2px 10px">'
        f'<a href="{link}" style="color:#0E5E5A;font-weight:600;font-size:14px;text-decoration:none">{escape(paper.title)}</a>'
        f'<p style="margin:2px 0 0;color:#5C6660;font-size:12px">{escape(_byline(paper))}</p>'
        f"{body}</td></tr></table>"
    )


def _email_list(papers: list[Paper]) -> str:
    rows = []
    for paper in papers:
        link = escape(paper.doi_url or paper.url)
        rows.append(
            '<tr><td style="padding:4px 0;border-bottom:1px solid #F0EEE8">'
            f'<a href="{link}" style="color:#0E5E5A;font-size:13px;text-decoration:none">{escape(paper.title)}</a>'
            f'<span style="color:#5C6660;font-size:11px"> · {escape(_byline(paper))}</span>'
            "</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + "".join(rows)
        + "</table>"
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_report.py -q`
Expected: PASS — 16 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/report.py tests/test_report.py
git commit -m "feat: render the digest as markdown, web HTML and email HTML"
```

---

## Task 14: 多收件人邮件与附件

**Files:**
- Create: `src/zotero_arxiv_daily/mailer.py`
- Test: `tests/test_mailer.py`

**Interfaces:**
- Consumes: 无（只依赖标准库 smtplib / email）
- Produces: `Attachment`（dataclass: `filename: str`、`content: bytes`、`mime_subtype: str`）、`select_attachments(paths, max_total_bytes) -> list[Attachment]`、`build_message(subject, html, sender, recipients, attachments) -> EmailMessage`、`send_digest(config, subject, html, attachments) -> None`

既有 `utils.send_email` 是单收件人、无附件、主题写死 `Daily arXiv`，**不改它**（daily 流程还在用），周报走这个新通道。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mailer.py
"""Multi-recipient delivery with Bcc and size-guarded attachments."""

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.mailer import (
    Attachment,
    build_message,
    select_attachments,
    send_digest,
)

RECIPIENTS = ["a@example.org", "b@example.org", "c@example.org"]


def test_recipients_are_bcc_so_they_cannot_see_each_other():
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, [])
    assert msg["Bcc"] is None or "a@example.org" not in (msg["To"] or "")
    assert msg["To"] == "me@example.org"


def test_every_recipient_is_returned_for_the_envelope():
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, [])
    assert msg.get_all("Bcc") == [", ".join(RECIPIENTS)]


def test_the_subject_and_html_body_survive():
    msg = build_message("CMC 文献周报 2026-08-W3（共 18 篇）", "<p>正文</p>", "me@example.org", RECIPIENTS, [])
    assert "2026-08-W3" in msg["Subject"]
    body = msg.get_body(preferencelist=("html"))
    assert "正文" in body.get_content()


def test_attachments_are_attached_with_their_filenames():
    attachments = [Attachment(filename="report.html", content=b"<html></html>", mime_subtype="html")]
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, attachments)
    names = [part.get_filename() for part in msg.iter_attachments()]
    assert names == ["report.html"]


def test_a_pdf_attachment_keeps_its_media_type():
    attachments = [Attachment(filename="p.pdf", content=b"%PDF-1.7", mime_subtype="pdf")]
    msg = build_message("S", "<p>hi</p>", "me@example.org", RECIPIENTS, attachments)
    part = next(msg.iter_attachments())
    assert part.get_content_type() == "application/pdf"


def test_select_attachments_stops_at_the_size_ceiling(tmp_path):
    paths = []
    for i in range(4):
        path = tmp_path / f"{i}.pdf"
        path.write_bytes(b"x" * 1000)
        paths.append(str(path))
    chosen = select_attachments(paths, max_total_bytes=2500)
    assert len(chosen) == 2


def test_select_attachments_keeps_the_given_order(tmp_path):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"a" * 10)
    second.write_bytes(b"b" * 10)
    chosen = select_attachments([str(first), str(second)], max_total_bytes=10_000)
    assert [c.filename for c in chosen] == ["first.pdf", "second.pdf"]


def test_select_attachments_skips_a_single_oversized_file(tmp_path):
    big, small = tmp_path / "big.pdf", tmp_path / "small.pdf"
    big.write_bytes(b"x" * 5000)
    small.write_bytes(b"y" * 100)
    chosen = select_attachments([str(big), str(small)], max_total_bytes=1000)
    assert [c.filename for c in chosen] == ["small.pdf"]


def test_select_attachments_ignores_missing_files(tmp_path):
    assert select_attachments(["/nonexistent/a.pdf"], max_total_bytes=10_000) == []


def test_send_digest_delivers_to_every_recipient(monkeypatch):
    sent = {}

    class StubSMTP:
        def __init__(self, server, port):
            sent["server"] = (server, port)

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["to_addrs"] = to_addrs

        def quit(self):
            sent["quit"] = True

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", StubSMTP)
    config = OmegaConf.create(
        {
            "email": {
                "sender": "me@example.org",
                "sender_password": "pw",
                "smtp_server": "smtp.example.org",
                "smtp_port": 587,
                "recipients": RECIPIENTS,
            }
        }
    )
    send_digest(config, "S", "<p>hi</p>", [])
    assert sorted(sent["to_addrs"]) == sorted(RECIPIENTS)
    assert sent["quit"] is True


def test_send_digest_refuses_an_empty_recipient_list():
    config = OmegaConf.create(
        {"email": {"sender": "me@example.org", "sender_password": "pw", "smtp_server": "s", "smtp_port": 587, "recipients": []}}
    )
    with pytest.raises(ValueError, match="no recipients"):
        send_digest(config, "S", "<p>hi</p>", [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_mailer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.mailer'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/mailer.py
"""Deliver the weekly digest to the team.

Everyone goes in Bcc so recipients cannot see each other's addresses, and the
attachment set is capped: a dozen open-access PDFs will blow past an SMTP
server's message limit, so only what fits is attached and the rest stays in
the repository archive.
"""

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from loguru import logger

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_subtype: str


def select_attachments(paths: list[str], max_total_bytes: int = MAX_ATTACHMENT_BYTES) -> list[Attachment]:
    """Read *paths* in order, keeping what fits under the ceiling."""
    chosen: list[Attachment] = []
    used = 0
    for path in paths:
        if not os.path.exists(path):
            logger.warning(f"Attachment {path} is missing; skipping")
            continue
        size = os.path.getsize(path)
        if used + size > max_total_bytes:
            logger.info(f"Skipping attachment {os.path.basename(path)} ({size} bytes): would exceed the size ceiling")
            continue
        with open(path, "rb") as handle:
            content = handle.read()
        subtype = os.path.splitext(path)[1].lstrip(".").lower() or "octet-stream"
        chosen.append(Attachment(filename=os.path.basename(path), content=content, mime_subtype=subtype))
        used += size
    return chosen


def _maintype_for(subtype: str) -> tuple[str, str]:
    if subtype == "pdf":
        return "application", "pdf"
    if subtype in {"html", "htm"}:
        return "text", "html"
    if subtype == "md":
        return "text", "markdown"
    return "application", "octet-stream"


def build_message(
    subject: str,
    html: str,
    sender: str,
    recipients: list[str],
    attachments: list[Attachment],
) -> EmailMessage:
    """Build the digest message with every recipient in Bcc."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender  # the sender is the only visible recipient
    msg["Bcc"] = ", ".join(recipients)
    msg.set_content("此邮件为 HTML 格式，请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(html, subtype="html")

    for attachment in attachments:
        maintype, subtype = _maintype_for(attachment.mime_subtype)
        msg.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return msg


def send_digest(config, subject: str, html: str, attachments: list[Attachment]) -> None:
    """Send the digest over SMTP, preferring STARTTLS and falling back to SSL."""
    settings = config.email
    recipients = [str(r).strip() for r in (settings.get("recipients") or []) if str(r).strip()]
    if not recipients:
        raise ValueError("email.recipients is empty: no recipients to send the digest to")

    sender = settings.sender
    msg = build_message(subject, html, sender, recipients, attachments)

    try:
        server = smtplib.SMTP(settings.smtp_server, settings.smtp_port)
        server.starttls()
    except Exception as exc:  # noqa: BLE001 - many providers are SSL-only on 465
        logger.debug(f"STARTTLS unavailable ({exc}); falling back to SSL")
        server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port)

    server.login(sender, settings.sender_password)
    server.send_message(msg, from_addr=sender, to_addrs=recipients)
    server.quit()
    logger.info(f"Digest sent to {len(recipients)} recipients with {len(attachments)} attachments")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_mailer.py -q`
Expected: PASS — 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/mailer.py tests/test_mailer.py
git commit -m "feat: add multi-recipient digest delivery with attachments"
```

---

## Task 15: 产物写盘与提交入库

**Files:**
- Create: `src/zotero_arxiv_daily/publish.py`
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: 无
- Produces: `write_text(path, content) -> str`、`git_commit_paths(paths, message, config) -> bool`

Actions 里提交需要 `permissions: contents: write`，且要配 `user.name` / `user.email`。**无改动时不提交**（否则 `git commit` 返回非零把整个 run 判失败）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_publish.py
"""Writing digest artefacts and committing them back to the repository."""

import os
import subprocess

from omegaconf import OmegaConf

from zotero_arxiv_daily.publish import git_commit_paths, write_text


def make_config(enabled=True):
    return OmegaConf.create(
        {"git": {"enabled": enabled, "user_name": "digest bot", "user_email": "bot@example.org", "branch": ""}}
    )


def test_write_text_creates_missing_directories(tmp_path):
    path = str(tmp_path / "reports" / "2026" / "2026-08-W3.md")
    write_text(path, "# hi")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "# hi"


def test_write_text_returns_the_path(tmp_path):
    path = str(tmp_path / "a.md")
    assert write_text(path, "x") == path


def test_write_text_overwrites_an_existing_file(tmp_path):
    path = str(tmp_path / "a.md")
    write_text(path, "first")
    write_text(path, "second")
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == "second"


def _init_repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "seed@example.org"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "seed"], cwd=repo, check=True)
    with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as handle:
        handle.write("seed")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def test_commit_records_a_new_file(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    assert git_commit_paths(["reports/a.md"], "docs: add report", make_config(), cwd=repo) is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "docs: add report" in log


def test_commit_is_skipped_when_nothing_changed(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_commit_paths(["seed.txt"], "docs: nothing", make_config(), cwd=repo) is False
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "docs: nothing" not in log


def test_commit_is_skipped_when_disabled(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    assert git_commit_paths(["reports/a.md"], "docs: add", make_config(enabled=False), cwd=repo) is False


def test_commit_uses_the_configured_identity(tmp_path):
    repo = _init_repo(tmp_path)
    write_text(os.path.join(repo, "reports", "a.md"), "# report")
    git_commit_paths(["reports/a.md"], "docs: add report", make_config(), cwd=repo)
    author = subprocess.run(
        ["git", "log", "-1", "--pretty=%an <%ae>"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert author == "digest bot <bot@example.org>"


def test_commit_failure_is_reported_not_raised(tmp_path):
    not_a_repo = str(tmp_path / "plain")
    os.makedirs(not_a_repo)
    write_text(os.path.join(not_a_repo, "a.md"), "x")
    assert git_commit_paths(["a.md"], "docs: add", make_config(), cwd=not_a_repo) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_publish.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.publish'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/publish.py
"""Write digest artefacts and commit them back to the repository.

Committing from Actions needs ``permissions: contents: write`` on the job and
an author identity on the runner.  A run that produced no change must not
commit: ``git commit`` exits non-zero on an empty index and would fail the
whole workflow.
"""

import os
import subprocess

from loguru import logger


def write_text(path: str, content: str) -> str:
    """Write *content* to *path*, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def git_commit_paths(paths: list[str], message: str, config, cwd: str = ".") -> bool:
    """Stage *paths* and commit them. Returns whether a commit was made."""
    settings = config.git
    if not settings.get("enabled", True):
        logger.info("git.enabled is false; leaving artefacts uncommitted")
        return False
    if not paths:
        return False

    _run(["git", "config", "user.name", str(settings.get("user_name") or "zotero-cmc-weekly")], cwd)
    _run(["git", "config", "user.email", str(settings.get("user_email") or "actions@github.com")], cwd)

    add = _run(["git", "add", "--", *paths], cwd)
    if add.returncode != 0:
        logger.warning(f"git add failed: {add.stderr.strip()}")
        return False

    staged = _run(["git", "diff", "--cached", "--name-only"], cwd)
    if not staged.stdout.strip():
        logger.info("No artefact changes to commit")
        return False

    commit = _run(["git", "commit", "-m", message], cwd)
    if commit.returncode != 0:
        logger.warning(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
        return False

    logger.info(f"Committed {len(staged.stdout.strip().splitlines())} artefact files")
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_publish.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/publish.py tests/test_publish.py
git commit -m "feat: write and commit digest artefacts"
```

---

## Task 16: 周报编排、配置与 workflow

**Files:**
- Create: `src/zotero_arxiv_daily/weekly.py`
- Create: `.github/workflows/weekly.yml`
- Modify: `config/base.yaml`
- Test: `tests/test_weekly.py`

**Interfaces:**
- Consumes: 前十五个任务的全部产物
- Produces: `WeeklyExecutor`（`run(anchor: date | None = None) -> Digest | None`）、`main()`（Hydra 入口）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_weekly.py
"""End-to-end orchestration of the weekly digest, with every I/O stubbed."""

from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from omegaconf import OmegaConf, open_dict

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.weekly import WeeklyExecutor


def make_corpus(n=6) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Corpus {i}",
            abstract=f"Corpus abstract {i}",
            added_date=datetime(2026, 1, i + 1),
            paths=["文献/表征"],
        )
        for i in range(n)
    ]


def make_candidate(i: int) -> Paper:
    return Paper(
        source="pubmed",
        title=f"Candidate {i}",
        authors=["Smith J"],
        abstract=f"Candidate abstract {i}",
        url=f"https://example.org/{i}",
        doi=f"10.1000/{i}",
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
    )


@pytest.fixture()
def weekly_config(config, tmp_path):
    with open_dict(config):
        config.zotero.include_path = ["文献", "文献/**"]
        config.search = OmegaConf.create(
            {
                "sources": ["pubmed"],
                "n_clusters": 2,
                "per_cluster_limit": 25,
                "cluster_cache": str(tmp_path / "clusters.json"),
                "profile_cache": str(tmp_path / "profiles.json"),
                "seen_state": str(tmp_path / "seen.json"),
            }
        )
        config.fulltext = OmegaConf.create({"enabled": False, "unpaywall_email": None, "max_bytes": 1000})
        config.report = OmegaConf.create(
            {
                "min_papers": 4,
                "max_papers": 6,
                "top_picks": 2,
                "min_per_cluster": 1,
                "attach_pdfs": 0,
                "output_dir": str(tmp_path),
                "fields": [{"key": "background", "label": "背景", "instruction": "研究背景"}],
            }
        )
        config.git = OmegaConf.create({"enabled": False, "user_name": "b", "user_email": "b@e.org", "branch": ""})
        config.email.recipients = ["team@example.org"]
    return config


@pytest.fixture()
def stubbed(monkeypatch, weekly_config):
    """Stub every network boundary the weekly run touches."""
    state = {"sent": [], "committed": []}

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.WeeklyExecutor.fetch_zotero_corpus",
        lambda self: make_corpus(),
    )

    payloads = iter(
        [
            '{"clusters":[{"name":"电荷","description":"d","members":[0,1,2]},'
            '{"name":"HCP","description":"d","members":[3,4,5]}]}',
            '{"mesh_terms":[],"free_terms":[],"pubmed_query":"q1","plain_query":"p1"}',
            '{"mesh_terms":[],"free_terms":[],"pubmed_query":"q2","plain_query":"p2"}',
        ]
    )

    def create(**kwargs):
        try:
            content = next(payloads)
        except StopIteration:
            content = '{"background":"抽取出的背景"}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.OpenAI",
        lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )

    class StubQueryRetriever:
        name = "pubmed"

        def __init__(self, config):
            self.config = config

        def search(self, query, start, end, limit):
            return [make_candidate(i) for i in range(3)]

        def search_highly_cited(self, query, limit):
            return []

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.get_query_retriever_cls",
        lambda name: StubQueryRetriever,
    )

    class StubReranker:
        def __init__(self, config):
            pass

        def similarity_matrix(self, candidates, corpus):
            rng = np.linspace(0.1, 0.9, len(candidates) * len(corpus))
            return rng.reshape(len(candidates), len(corpus))

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: StubReranker)
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.send_digest",
        lambda config, subject, html, attachments: state["sent"].append((subject, html, attachments)),
    )
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.git_commit_paths",
        lambda paths, message, config, cwd=".": state["committed"].append(paths) or True,
    )
    return state


def test_weekly_run_produces_a_digest(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert digest is not None
    assert digest.label == "2026-08-W3"


def test_weekly_run_sends_one_email_with_the_label_in_the_subject(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert len(stubbed["sent"]) == 1
    subject, html, _ = stubbed["sent"][0]
    assert "2026-08-W3" in subject
    assert "CMC" in subject
    assert "2026-08-W3" in html


def test_weekly_run_writes_both_report_files(weekly_config, stubbed, tmp_path):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert (tmp_path / "reports" / "2026" / "2026-08-W3.md").exists()
    assert (tmp_path / "reports" / "2026" / "2026-08-W3.html").exists()


def test_weekly_run_records_delivered_dois_for_next_week(weekly_config, stubbed, tmp_path):
    import json

    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    with open(tmp_path / "seen.json", encoding="utf-8") as handle:
        assert "10.1000/0" in json.load(handle)


def test_papers_delivered_last_week_are_not_delivered_again(weekly_config, stubbed, tmp_path):
    executor = WeeklyExecutor(weekly_config)
    executor.run(anchor=date(2026, 8, 21))
    second = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 28))
    assert second is None or all(p.doi != "10.1000/0" for _, papers in second.clusters for p in papers)


def test_every_candidate_is_assigned_a_cluster(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.cluster


def test_every_delivered_paper_carries_its_extracted_fields(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.extraction is not None
            assert "background" in paper.extraction


def test_an_empty_corpus_aborts_before_any_search(weekly_config, stubbed, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.weekly.WeeklyExecutor.fetch_zotero_corpus", lambda self: [])
    assert WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21)) is None
    assert stubbed["sent"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_weekly.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.weekly'`

- [ ] **Step 3: 最小实现**

在 `config/base.yaml` 末尾追加五段：

```yaml
search:
  sources: ['pubmed','europepmc','crossref','openalex'] # Query-style sources used by the weekly digest.
  n_clusters: 5           # How many themes to group the Zotero corpus into.
  per_cluster_limit: 25   # Max candidates fetched per cluster per source.
  cluster_cache: state/theme_clusters.json  # Rebuilt only when the corpus changes.
  profile_cache: state/query_profiles.json  # Rebuilt only when the cluster set changes.
  seen_state: state/seen_dois.json          # DOIs already delivered, for cross-week de-duplication.

fulltext:
  enabled: true            # Whether to attempt open-access full-text retrieval.
  unpaywall_email: null    # Contact address required by the Unpaywall API. Example: you@example.org
  max_bytes: 20000000      # Reject any single PDF larger than this.

report:
  min_papers: 15           # Below this, top up with highly-cited backfill.
  max_papers: 25           # Hard ceiling on the digest length.
  top_picks: 3             # Size of the "read these first" section.
  min_per_cluster: 1       # Floor so a small theme is never crowded out.
  attach_pdfs: 5           # How many PDFs to attach to the email; the rest stay archived.
  output_dir: '.'          # Root the artefacts are written under.
  fields:                  # Drives both the extraction prompt and the rendered output.
    - {key: background,  label: 背景,       instruction: 这项研究的背景与动机}
    - {key: gap,         label: 待解决的问题, instruction: 本文试图解决的、此前尚未解决的问题}
    - {key: method,      label: 方法,       instruction: 采用的分析方法、样品与关键参数}
    - {key: conclusion,  label: 结论,       instruction: 主要结果与结论}
    - {key: insight,     label: 洞见,       instruction: 对生物制药 CMC 分析实践的启发}

git:
  enabled: true            # Whether to commit the artefacts back to the repository.
  user_name: zotero-cmc-weekly
  user_email: actions@github.com
  branch: ''               # Leave empty to commit onto the checked-out branch.
```

```python
# src/zotero_arxiv_daily/weekly.py
"""The weekly CMC digest pipeline.

Nine stages: fetch the Zotero corpus, cluster it into themes, distil a query
per theme, search the journal sources over the week's date window, collapse
duplicates, score and take a per-theme quota, top up a thin week, resolve open
-access full text, extract the configured fields, then render, archive and
send.
"""

from datetime import date, datetime

import hydra
import numpy as np
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from .backfill import backfill_papers
from .dedup import dedup_papers, drop_seen, load_seen, normalize_doi, save_seen
from .executor import Executor
from .extract import extract_all, load_field_specs
from .fulltext.resolver import download_fulltext
from .mailer import select_attachments, send_digest
from .publish import git_commit_paths, write_text
from .quota import allocate_quota, take_by_quota
from .report import build_digest, render_email_html, render_markdown, render_web_html
from .reranker import get_reranker_cls
from .reranker.base import time_decay_weights
from .retriever import get_query_retriever_cls
from .search.cluster import assign_clusters, load_or_build_clusters
from .search.profile import load_or_build_profiles
from .weeknum import library_dir, report_paths, week_label, week_window


class WeeklyExecutor(Executor):
    """Reuses the Zotero corpus plumbing, replaces everything downstream."""

    def __init__(self, config: DictConfig):
        # Deliberately not calling Executor.__init__: the firehose retrievers
        # and the eager reranker construction there do not apply here.
        self.config = config
        self.include_path_patterns = list(config.zotero.include_path or []) or None
        self.ignore_path_patterns = list(config.zotero.ignore_path or []) or None
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
        self.reranker = get_reranker_cls(config.executor.reranker)(config)

    def _search_all(self, profiles, start: date, end: date):
        limit = int(self.config.search.per_cluster_limit)
        candidates = []
        for source in self.config.search.sources:
            retriever = get_query_retriever_cls(source)(self.config)
            for profile in profiles:
                query = profile.pubmed_query if source == "pubmed" and profile.pubmed_query else profile.plain_query
                found = retriever.search(query, start, end, limit)
                logger.info(f"{source}/{profile.cluster}: {len(found)} candidates")
                candidates.extend(found)
        return candidates

    def _score(self, candidates, corpus, clusters):
        """Score candidates and route them to a theme in one embedding pass."""
        ordered = sorted(corpus, key=lambda c: c.added_date, reverse=True)
        order = {id(c): i for i, c in enumerate(corpus)}
        remap = [order[id(c)] for c in ordered]

        sim_sorted = self.reranker.similarity_matrix(candidates, ordered)
        scores = (sim_sorted * time_decay_weights(len(ordered))).sum(axis=1) * 10
        for score, paper in zip(scores, candidates):
            paper.score = float(score)

        # Cluster membership indexes the original corpus order, so undo the sort.
        sim_original = np.empty_like(sim_sorted)
        sim_original[:, remap] = sim_sorted
        assign_clusters(candidates, sim_original, clusters)

    def run(self, anchor: date | None = None):
        anchor = anchor or datetime.now().date()
        label = week_label(anchor)
        start, end = week_window(anchor)
        logger.info(f"Building digest {label} covering {start} to {end}")

        corpus = self.filter_corpus(self.fetch_zotero_corpus())
        if not corpus:
            logger.error(f"No Zotero papers matched. Check your settings:\n{self.config.zotero}")
            return None

        clusters = load_or_build_clusters(
            self.config.search.cluster_cache,
            corpus,
            self.openai_client,
            self.config.llm,
            int(self.config.search.n_clusters),
        )
        profiles = load_or_build_profiles(
            self.config.search.profile_cache,
            clusters,
            corpus,
            self.openai_client,
            self.config.llm,
        )

        seen = load_seen(self.config.search.seen_state)
        candidates = drop_seen(dedup_papers(self._search_all(profiles, start, end)), seen)
        logger.info(f"{len(candidates)} candidates after de-duplication")

        chosen = []
        if candidates:
            self._score(candidates, corpus, clusters)
            candidates.sort(key=lambda p: -(p.score or 0.0))
            sizes = {c.name: len(c.members) for c in clusters}
            quota = allocate_quota(
                sizes,
                int(self.config.report.max_papers),
                int(self.config.report.min_per_cluster),
            )
            chosen = take_by_quota(candidates, quota)

        shortfall = int(self.config.report.min_papers) - len(chosen)
        exclude = seen | {normalize_doi(p.doi) or "" for p in chosen}
        backfill = []
        if shortfall > 0:
            backfill = backfill_papers(
                profiles,
                get_query_retriever_cls("openalex")(self.config),
                shortfall,
                exclude,
            )

        delivered = chosen + backfill
        if not delivered:
            logger.warning("No papers to deliver this week")
            return None

        pdf_dir = f"{self.config.report.output_dir}/{library_dir(anchor)}"
        download_fulltext(delivered, self.config, pdf_dir)

        fields = load_field_specs(self.config)
        extract_all(delivered, self.openai_client, self.config.llm, fields)

        digest = build_digest(chosen, backfill, anchor, int(self.config.report.top_picks))
        md_rel, html_rel = report_paths(anchor)
        root = self.config.report.output_dir
        md_path = write_text(f"{root}/{md_rel}", render_markdown(digest, fields))
        html_path = write_text(f"{root}/{html_rel}", render_web_html(digest, fields))

        save_seen(
            self.config.search.seen_state,
            seen | {d for d in (normalize_doi(p.doi) for p in delivered) if d},
        )

        git_commit_paths(
            [md_rel, html_rel, self.config.search.seen_state],
            f"docs: add CMC literature digest {label}",
            self.config,
        )

        attach_paths = [html_path] + [
            p.pdf_path for p in digest.top_picks if p.pdf_path
        ][: int(self.config.report.attach_pdfs)]
        send_digest(
            self.config,
            f"CMC 文献周报 {label}（共 {digest.total} 篇）",
            render_email_html(digest, fields),
            select_attachments(attach_paths),
        )
        logger.info(f"Digest {label} delivered: {digest.total} papers, archived at {md_path}")
        return digest


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig) -> None:
    WeeklyExecutor(config).run()


if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/weekly.yml
name: CMC literature weekly digest

on:
  workflow_dispatch:
  schedule:
    # 12:00 UTC on Friday = 20:00 Asia/Shanghai
    - cron: '0 12 * * 5'

permissions:
  contents: write

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Sync dependencies
        run: uv sync

      - name: Build and send the digest
        env:
          ZOTERO_ID: ${{ secrets.ZOTERO_ID }}
          ZOTERO_KEY: ${{ secrets.ZOTERO_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          OPENAI_API_BASE: ${{ secrets.OPENAI_API_BASE }}
          NCBI_API_KEY: ${{ secrets.NCBI_API_KEY }}
          CONTACT_EMAIL: ${{ secrets.CONTACT_EMAIL }}
          SENDER: ${{ secrets.SENDER }}
          SENDER_PASSWORD: ${{ secrets.SENDER_PASSWORD }}
          RECIPIENTS: ${{ secrets.RECIPIENTS }}
          CUSTOM_CONFIG: ${{ vars.CUSTOM_CONFIG }}
        run: |
          printf "%b\n" "$CUSTOM_CONFIG" > config/custom.yaml
          uv run src/zotero_arxiv_daily/weekly.py

      - name: Push the archived digest
        run: |
          git push || echo "nothing to push"
```

- [ ] **Step 4: 跑测试确认通过，并跑全量确认无回归**

Run: `uv run --no-sync pytest tests/test_weekly.py -q && uv run --no-sync pytest -q`
Expected: `tests/test_weekly.py` 8 passed；全量除 `tests/test_protocol.py` 三条已知的沙箱受限用例外全绿

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/weekly.py config/base.yaml .github/workflows/weekly.yml tests/test_weekly.py
git commit -m "feat: orchestrate the weekly CMC literature digest"
```

---

## Task 17: 月度综述层（B 层，可选）

**Files:**
- Create: `src/zotero_arxiv_daily/monthly.py`
- Create: `.github/workflows/monthly.yml`
- Test: `tests/test_monthly.py`

**Interfaces:**
- Consumes: 已归档的周报 markdown
- Produces: `collect_month_reports(root, year, month) -> list[tuple[str, str]]`、`synthesise(reports, client, llm_params) -> str`、`MonthlyExecutor.run(anchor=None) -> str | None`

这是路线 B 的落点，也是 `literature-search` skill 完整验证式流程唯一真正用得上的地方：输入当月四份周报，产出跨篇归纳与主题演化。**失败不影响周报**——两条 workflow 相互独立。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_monthly.py
"""The optional monthly synthesis layer."""

from datetime import date
from types import SimpleNamespace

import pytest
from omegaconf import OmegaConf

from zotero_arxiv_daily.monthly import MonthlyExecutor, collect_month_reports, synthesise


def seed_reports(root, names):
    import os

    os.makedirs(os.path.join(root, "reports", "2026"), exist_ok=True)
    for name in names:
        with open(os.path.join(root, "reports", "2026", name), "w", encoding="utf-8") as handle:
            handle.write(f"# {name}\n\n正文 {name}")


def test_collect_finds_every_week_of_the_month(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-08-W2.md", "2026-08-W3.md"])
    found = collect_month_reports(str(tmp_path), 2026, 8)
    assert [name for name, _ in found] == ["2026-08-W1", "2026-08-W2", "2026-08-W3"]


def test_collect_ignores_other_months(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-07-W4.md"])
    assert [name for name, _ in collect_month_reports(str(tmp_path), 2026, 8)] == ["2026-08-W1"]


def test_collect_returns_the_report_bodies(tmp_path):
    seed_reports(tmp_path, ["2026-08-W1.md"])
    _, body = collect_month_reports(str(tmp_path), 2026, 8)[0]
    assert "正文 2026-08-W1.md" in body


def test_collect_on_an_empty_month_is_empty(tmp_path):
    assert collect_month_reports(str(tmp_path), 2026, 8) == []


def stub_client(payload="## 月度综述\n\n本月主题演化…"):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])
            )
        )
    )


def test_synthesise_returns_the_model_output():
    result = synthesise([("2026-08-W1", "body")], stub_client(), {"generation_kwargs": {"model": "m"}})
    assert "月度综述" in result


def test_synthesise_degrades_to_empty_on_failure():
    def boom(**kw):
        raise RuntimeError("rate limited")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    assert synthesise([("2026-08-W1", "body")], client, {"generation_kwargs": {"model": "m"}}) == ""


def make_config(tmp_path):
    return OmegaConf.create(
        {
            "llm": {"api": {"key": "k", "base_url": "http://localhost/v1"}, "generation_kwargs": {"model": "m"}, "language": "中文"},
            "report": {"output_dir": str(tmp_path)},
            "git": {"enabled": False, "user_name": "b", "user_email": "b@e.org", "branch": ""},
            "email": {"sender": "me@example.org", "sender_password": "pw", "smtp_server": "s", "smtp_port": 587, "recipients": ["t@example.org"]},
        }
    )


@pytest.fixture()
def stub_monthly(monkeypatch):
    sent = []
    monkeypatch.setattr("zotero_arxiv_daily.monthly.OpenAI", lambda **kw: stub_client())
    monkeypatch.setattr("zotero_arxiv_daily.monthly.send_digest", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr("zotero_arxiv_daily.monthly.git_commit_paths", lambda *a, **kw: True)
    return sent


def test_monthly_run_writes_a_synthesis(tmp_path, stub_monthly):
    seed_reports(tmp_path, ["2026-08-W1.md", "2026-08-W2.md"])
    path = MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31))
    assert path is not None
    assert (tmp_path / "reports" / "2026" / "2026-08-monthly.md").exists()


def test_monthly_run_is_a_no_op_without_weekly_reports(tmp_path, stub_monthly):
    assert MonthlyExecutor(make_config(tmp_path)).run(anchor=date(2026, 8, 31)) is None
    assert stub_monthly == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest tests/test_monthly.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotero_arxiv_daily.monthly'`

- [ ] **Step 3: 最小实现**

```python
# src/zotero_arxiv_daily/monthly.py
"""Optional monthly synthesis over the month's weekly digests.

Route B in the spec: an agent pass that reads what already shipped and looks
for the cross-cutting story — which themes grew, which questions recurred,
what a month of reading adds up to.  It is deliberately a separate workflow:
if it fails, the weekly digest is untouched.
"""

import os
import re
from datetime import date, datetime

import hydra
from loguru import logger
from omegaconf import DictConfig
from openai import OpenAI

from .mailer import select_attachments, send_digest
from .publish import git_commit_paths, write_text
from .utils import truncate_for_prompt

_WEEK_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-W\d+\.md$")
_MAX_REPORT_TOKENS = 4000


def collect_month_reports(root: str, year: int, month: int) -> list[tuple[str, str]]:
    """Return ``(label, body)`` for every weekly digest of *year*-*month*."""
    folder = os.path.join(root, "reports", str(year))
    if not os.path.isdir(folder):
        return []
    found = []
    for name in sorted(os.listdir(folder)):
        match = _WEEK_FILE_RE.match(name)
        if not match or (int(match.group(1)), int(match.group(2))) != (year, month):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            found.append((name[: -len(".md")], handle.read()))
    return found


def synthesise(reports: list[tuple[str, str]], client, llm_params: dict) -> str:
    """Ask the model for the month's cross-cutting story; never raises."""
    language = llm_params.get("language", "中文")
    body = "\n\n---\n\n".join(
        f"## {label}\n{truncate_for_prompt(text, _MAX_REPORT_TOKENS)}" for label, text in reports
    )
    prompt = (
        f"下面是本月{len(reports)}份 CMC 文献周报。请用{language}写一份月度综述，包含：\n"
        "1. 本月主题分布与相较上月的演化\n"
        "2. 跨周反复出现的科学问题或技术路线\n"
        "3. 对生物制药 CMC 分析实践最有价值的 3–5 篇及理由\n"
        "4. 值得关注但本月证据尚不充分的方向\n\n"
        "输出 markdown，不要重复罗列每篇文献。\n\n"
        f"{body}"
    )
    try:
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": f"你是一位生物制药 CMC 分析领域的资深研究员，用{language}撰写综述。"},
                {"role": "user", "content": prompt},
            ],
            **llm_params.get("generation_kwargs", {}),
        )
        return response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - the monthly layer must never break anything
        logger.warning(f"Monthly synthesis failed: {exc}")
        return ""


class MonthlyExecutor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)

    def run(self, anchor: date | None = None) -> str | None:
        anchor = anchor or datetime.now().date()
        root = self.config.report.output_dir
        reports = collect_month_reports(root, anchor.year, anchor.month)
        if not reports:
            logger.info(f"No weekly digests found for {anchor:%Y-%m}; nothing to synthesise")
            return None

        text = synthesise(reports, self.client, self.config.llm)
        if not text.strip():
            logger.warning("Monthly synthesis produced no content; skipping delivery")
            return None

        label = f"{anchor:%Y-%m}"
        rel = f"reports/{anchor.year}/{label}-monthly.md"
        path = write_text(f"{root}/{rel}", f"# CMC 文献月度综述 {label}\n\n{text}\n")
        git_commit_paths([rel], f"docs: add CMC literature monthly synthesis {label}", self.config)

        send_digest(
            self.config,
            f"CMC 文献月度综述 {label}",
            f'<div style="font-family:-apple-system,Arial,sans-serif;white-space:pre-wrap">{text}</div>',
            select_attachments([path]),
        )
        logger.info(f"Monthly synthesis {label} delivered, archived at {path}")
        return path


@hydra.main(version_base=None, config_path="../../config", config_name="default")
def main(config: DictConfig) -> None:
    MonthlyExecutor(config).run()


if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/monthly.yml
name: CMC literature monthly synthesis

on:
  workflow_dispatch:
  schedule:
    # 13:00 UTC on the 28th = 21:00 Asia/Shanghai, after the month's last Friday digest
    - cron: '0 13 28 * *'

permissions:
  contents: write

jobs:
  synthesis:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Sync dependencies
        run: uv sync

      - name: Build and send the monthly synthesis
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          OPENAI_API_BASE: ${{ secrets.OPENAI_API_BASE }}
          ZOTERO_ID: ${{ secrets.ZOTERO_ID }}
          ZOTERO_KEY: ${{ secrets.ZOTERO_KEY }}
          SENDER: ${{ secrets.SENDER }}
          SENDER_PASSWORD: ${{ secrets.SENDER_PASSWORD }}
          RECIPIENTS: ${{ secrets.RECIPIENTS }}
          CUSTOM_CONFIG: ${{ vars.CUSTOM_CONFIG }}
        run: |
          printf "%b\n" "$CUSTOM_CONFIG" > config/custom.yaml
          uv run src/zotero_arxiv_daily/monthly.py

      - name: Push the archived synthesis
        run: |
          git push || echo "nothing to push"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run --no-sync pytest tests/test_monthly.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/zotero_arxiv_daily/monthly.py .github/workflows/monthly.yml tests/test_monthly.py
git commit -m "feat: add the optional monthly synthesis layer"
```

---

## 收尾验证

全部任务完成后，按 `superpowers:verification-before-completion` 执行：

- [ ] 跑全量测试：`uv run --no-sync pytest -q`，记录实际通过 / 失败数，**不得**以「应该没问题」代替
- [ ] 逐条核对 Global Constraints，列出未满足项
- [ ] 逐条核对 spec `docs/cmc-literature-weekly-plan.md` §4 的九个阶段，指出每阶段落在哪个模块
- [ ] 跑 `superpowers:requesting-code-review`（本仓库对应 `/code-review`）
- [ ] 已知环境限制如实写明：`tests/test_protocol.py` 三条用例依赖 `openaipublic.blob.core.windows.net`（tiktoken 编码表），该域名被本沙箱出网策略 403 拒绝；这三条在 GitHub Actions 中可正常通过

## Self-Review

**1. Spec 覆盖核对（§4 九阶段 → 任务）**

| 阶段 | 落点 |
| --- | --- |
| ① 拉取并过滤 Zotero 语料 | Task 16 复用 `Executor.fetch_zotero_corpus` / `filter_corpus`，0 改动 |
| ② 主题聚类 + 蒸馏检索式（带缓存） | Task 6、Task 7 |
| ③ 多源检索（日期窗口） | Task 8、9、10 |
| ④ DOI 归一去重 | Task 3 |
| ⑤ 按主题簇分别排序、各取配额 | Task 4 + Task 5 + Task 16 `_score` |
| ⑤b 高引补位 | Task 10 `backfill.py` |
| ⑥ 全文获取阶梯 | Task 11 |
| ⑦ LLM 结构化抽取 | Task 12 |
| ⑧ 三层渲染 · 入库 · 群发 | Task 13、14、15、16 |
| P5 月度综述层 | Task 17 |

**2. 占位符扫描**：已逐条检查，无 TBD / "类似 Task N" / "补充适当的错误处理" 一类空话；每个 Step 3 都含可直接落盘的完整代码。

**3. 类型一致性核对**：

- `Paper.cluster`（Task 2 定义）被 Task 4 `take_by_quota`、Task 6 `assign_clusters`、Task 13 `build_digest` 一致读写
- `FieldSpec`（Task 12）在 Task 13 三个渲染函数中签名一致
- `QueryProfile.plain_query` / `.pubmed_query`（Task 7）被 Task 10 `backfill_papers` 与 Task 16 `_search_all` 一致消费
- `BaseQueryRetriever.search(query, start, end, limit)`（Task 8）四个检索器签名一致
- `git_commit_paths(paths, message, config, cwd=".")`（Task 15）在 Task 16、17 调用处一致

**4. 已知缺口（有意为之，不在本计划范围）**：

- P4 邮件源摄取（Google Scholar / 知网 / X-MOL IMAP）——spec 标注为可选，且需要用户先开一个专用邮箱与 IMAP 凭据
- 上图代理自动下载——spec 发现 4 明确不建议做，本计划不实现

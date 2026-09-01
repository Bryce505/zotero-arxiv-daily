# 2026-08-W4 测试周报复检：重复文献与经典补位缺失 设计与修复记录

**日期：** 2026-09-01
**状态：** 已实现（回溯记录，非预先评审）
**上游：** `docs/superpowers/specs/2026-08-22-digest-relevance-and-structure-design.md`（相关性闸门与经典补位的原始设计）
**关联提交：** `47c69ad`/`0f1e043`（标题/链接错配，本轮之前）
**关联运行：** GitHub Actions run [33517443909](https://github.com/Bryce505/zotero-arxiv-daily/actions/runs/33517443909)（`workflow_dispatch`，head `0f1e043`，2026-09-01 14:06–14:21 UTC，成功）

---

## 0. 说明：为什么这份文档是回溯写的，以及一次更正

本仓库的既有约定是先写 spec/plan 再动代码。这一轮改动体量小，且用户是拿着一份实测输出直接指出具体症状，诊断与修复是同一次会话内连续完成的。按约定补一份 spec，把诊断证据、根因与取舍记录下来——但没有先出一版「待评审」再等确认，这点与既有流程不同，如实说明。

**更正**：本文档最初的版本（同日更早提交）在 §5 写「没有那次运行的日志，无法确定这次补位为空的具体原因」。这是错的——GitHub Actions 保留了该次 `workflow_dispatch` 运行的完整任务日志，用户追问后去读了，§1.2/§1.3 是读日志后改写的实际结论，不再是推测。

---

## 1. 问题：实测诊断

测试素材：分支 `claude/literature-report-link-mismatch-cnss4y` 上手动触发的 workflow run 33517443909，产出 `2026-08-W4` 周报（用户以附件 `b2e96330-202608W4_2.html` 提供，随后该次运行自己也把结果提交进了仓库，见 `4c97f13`）。

### 1.1 重复文献

「蛋白质结构、功能与质谱表征」分区内 #4、#5 是同一篇综述，标题逐字相同（仅末尾句号之差），却各自带一条**不同的** DOI：

| # | 标题 | 作者署名 | DOI | 期刊字段 |
| --- | --- | --- | --- | --- |
| 4 | Protein persulfidation in plants: a central regulator of multiple signaling pathways | Yang D et al. | `10.1007/s00299-026-03954-y` | *(空)* |
| 5 | Protein persulfidation in plants: a central regulator of multiple signaling pathways. | Yang Di et al. | `10.1016/j.plaphy.2023.107900` | Plant cell reports |

DOI 前缀 `10.1007/s00299-*` 正是 Springer 期刊 *Plant Cell Reports* 的注册前缀，与 #5 自己标出的期刊名吻合；`10.1016/j.plaphy.*` 是 Elsevier *Plant Physiology and Biochemistry* 的前缀，对不上。仓库里 `state/seen_dois.json` 独立验证了这一点——两个 DOI 都被分别记了一遍，说明去重没有把它们识别为同一篇文献。这与上一轮修复的根因同构（`47c69ad`）：某个来源（大概率 PubMed）给一条标题、期刊都对的记录挂错了 DOI；这次污染的是**去重阶段**，不是全文抓取阶段（日志里这次运行 `Full text resolved for 0/5 papers`，这 5 篇本来就没抓到全文，`47c69ad` 修的那条路径这次根本没被触发）。

### 1.2 数量不足（5 篇）——日志给出的确切原因

日志（任务 `digest`，步骤 *Build and send the digest*）显示完整链路：

```
201 candidates after de-duplication (103 library DOIs and 98 previously delivered DOIs excluded)
...
55 candidate(s) read as relevant to biologics CMC in general but not judged to fit any current theme; excluded
55/60 candidates left unjudged; they will not be delivered
Relevance gate: 5/60 passed (55 unjudged, 0 below relevance 55, 0 below score 60)
```

关键数字：`0 below relevance 55, 0 below score 60`。也就是说，这周被拦下的 55 篇候选，**没有一篇是因为相关度或综合分不达标**——全部是被 `_apply_theme_verdicts`（PR #2 / `69f237e`，本轮之前已合并的"按 LLM 判定的主题归属校正候选簇"功能）判定为「和生物药 CMC 总体相关，但不属于本周语料聚出的任何一个具体主题」而排除。5 个主题里只有「色谱电泳纯度与含量分析」「蛋白质结构、功能与质谱表征」拿到候选，其余 3 个（宿主细胞蛋白 HCP 分析、免疫分析与酶学检测、电荷异质性与电泳分离分析）颗粒无收——不是没检索到候选（日志显示这几个主题在 pubmed/europepmc/openalex 上都拿到了 18–25 篇），是**候选到了、分诊也判了"相关"，但没有一篇被判定属于该主题本身**。

### 1.3 经典补位为什么也是 0——同一道闸，同样的死法

```
13 candidate(s) read as relevant to biologics CMC in general but not judged to fit any current theme; excluded
13/13 candidates left unjudged; they will not be delivered
Relevance gate: 0/13 passed (13 unjudged, 0 below relevance 55, 0 below score 60)
Backfilled 0 highly-cited papers (needed 10)
```

OpenAlex **确实返回了候选**（13 篇去重/排除后进入闸门），不是网络或限流问题（日志里 OpenAlex 请求偶有 429 但都在重试后成功）。补位这 13 篇被拦下的原因和 §1.2 一模一样：13/13 全部因为"不属于本周任何一个具体主题"被排除，同样 0 篇因相关度/综合分不达标。

`backfill_papers()` 复用 `weekly.py::_gate()`（`gate=self._gate`），这是 2026-08-22 spec §10 的有意设计（"补位文献同样需通过相关性判定"，为的是拦住无关的高引论文，如首期那篇 2005 年病毒学论文）。但 `_gate()` 现在做的判定比那份 spec 写的时候更严——PR #2 加上了"必须属于本周语料聚出的某个具体主题"这一层，而不只是"和生物药 CMC 相关"。这层加码对**新文献**是合理的（不然一篇泛泛相关的文章会被硬塞进最接近的主题）；但对**经典补位**的语义正好相反——OpenAlex 按被引数取来的经典文献，本来就是"这个领域公认重要"而不是"精确对应本周语料自动聚出的 5 个窄主题之一"，用同一把尺子量，补位候选比新文献更难通过。本次 13/13 全军覆没就是这个张力的直接后果，不是网络问题，也不是阈值配错。

**这是本文档目前唯一没有直接改代码的发现**——因为它是一个真实的设计取舍问题，不是明确的 bug，见 §5。

### 1.4 新发现：Crossref 本次运行请求全部失败（400）

日志里能看到，本次运行 5 个主题各自的 Crossref 查询**无一例外**在重试 3 次后失败：

```
GET https://api.crossref.org/works?...&select=DOI%2Ctitle%2Cabstract%2Cauthor%2Ccontainer-title%2Ccreated%2Caffiliation
  failed (400 Client Error: Bad Request ...)
crossref/宿主细胞蛋白（HCP）分析: 0 candidates
crossref/蛋白质结构、功能与质谱表征: 0 candidates
crossref/电荷异质性与电泳分离分析: 0 candidates
crossref/色谱电泳纯度与含量分析: 0 candidates
crossref/免疫分析与酶学检测: 0 candidates
```

根因在 `crossref_retriever.py::search()` 的请求参数：

```python
"select": "DOI,title,abstract,author,container-title,created,affiliation",
```

`affiliation` 不是 Crossref `/works` 的顶层可选字段——它嵌在 `author[].affiliation` 里（`_to_paper()` 自己的解析代码 `for affiliation in author.get("affiliation") or []` 读的正是这个嵌套字段）。把它当成顶层字段名塞进 `select`，Crossref 直接拒绝整个请求。这个字段是 2026-08-22 那轮改动（Task 2，为了给企业加分取作者单位）加进去的，从那以后 Crossref 这个源大概率**每周都在无声地贡献 0 候选**——本次 201 个去重后候选全部来自 pubmed / europepmc / openalex 三个源。

这个和 §1.1/§1.2/§1.3 是三个独立问题，只是恰好在同一次运行里一起暴露：Crossref 掉线让候选总量变薄，主题匹配闸把变薄后的候选进一步砍到 5 篇，去重策略又把其中一篇变成了两条。

### 1.5 min_papers / max_papers

用户问能否设两个独立变量控制篇数上下限。`config/base.yaml` **已经**是这样（`min_papers: 15` 补位触发线，`max_papers: 25` 配额硬上限），`custom.yaml` 未覆盖。此项无需新增代码，只需把语义讲清楚。

---

## 2. 根因

1. **去重策略**在「两侧都带 DOI 但不同」时拒绝按标题合并（详见下方 §3.1），前提假设「不同 DOI 就是不同文献」不成立。
2. **Crossref `select` 参数带了一个无效字段名**，导致该源整轮请求全部 400，静默贡献 0 候选，从 2026-08-22 引入至今。
3. **主题匹配闸（PR #2）无差别套用在经典补位上**，语义错配：补位候选是"该领域公认重要"，闸门要求的是"精确匹配本周语料聚出的某个具体主题"，两者本就不是一回事，标准定得越严补位越难触发（详见 §1.3、§5）。

---

## 3. 方案

### 3.1 去重：标题精确匹配即合并，不再要求 DOI 兼容

`dedup_papers()` 简化为：DOI 精确匹配优先；未命中则按标题精确匹配（忽略大小写/标点/空白），无论两侧是否都带 DOI、DOI 是否不同。保留的身份（含 DOI）以先出现的记录为准；后到的记录只贡献 `_MERGEABLE_FIELDS` 里对方缺的空位。测试：`test_same_title_collapses_even_when_both_sides_carry_different_dois`、`test_regression_persulfidation_duplicate_collapses_and_fills_the_journal_gap`（原样复现本次实测数据）、`test_dedup_keeps_distinct_papers_that_do_not_share_a_title`（确认不影响标题不同的正常路径）。

### 3.2 补位掉数可观测

`backfill_papers()` 记录取数管线三个阶段的计数（`fetched` → `after_dedup` → `gated`），`len(chosen) < needed` 时升级为 `logger.warning`。`weekly.py::WeeklyExecutor.run()` 在 `delivered` 低于 `min_papers` 时打一条 WARNING，报出新文献/补位的构成。不改变行为，只是把 §1.2/§1.3 这类症状从"要翻 Actions 日志才看得出"变成"运行结束就有一行摘要"。

### 3.3 Crossref `select` 参数去掉无效字段

删掉 `select` 里的 `,affiliation`；`author` 本身已经带嵌套的 affiliation 数据，`_to_paper()` 的解析代码从一开始读的就是这个嵌套字段，从未真的依赖过顶层 `affiliation`。新增回归测试 `test_crossref_select_does_not_include_the_invalid_affiliation_field`，直接断言 `select` 里没有这个字段、且 `author` 还在。

### 3.4 配置注释澄清

`config/base.yaml` 的 `report.min_papers`/`max_papers` 补充注释，明确两者是独立控制项，并指向补位是尽力而为、掉线时的 WARNING 会说明原因。

---

## 4. 验证

```bash
.venv/bin/python -m pytest tests/test_dedup.py tests/test_backfill.py tests/test_weekly.py \
  tests/test_config_wiring.py tests/retriever/test_crossref_retriever.py -q
# 92 passed

.venv/bin/python -m pytest tests/ -q --continue-on-collection-errors
# 582 passed, 3 failed（均为 tests/test_protocol.py 的 tiktoken 联网用例，
# 出口策略拒绝 openaipublic.blob.core.windows.net，属既有沙箱限制，与本次改动无关）
```

（沙箱内 `uv sync` 因 `download.pytorch.org` 被出口策略拒绝而无法完成；本轮验证复用 Python 3.13 + pip 安装除 `torch`/`sentence-transformers` 外全部运行依赖的方式跑测试，覆盖 `-m "not slow"` 默认套件。）

---

## 5. 遗留问题：经典补位该不该用同一道主题匹配闸

§1.3 是一个真实的设计取舍，不是本文档单方面改掉的 bug——`_gate()` 复用是 2026-08-22 spec 的明确设计，主题匹配是 PR #2 的明确设计，两者各自成立，叠加后的效果（补位几乎不可能通过）大概率不是任何一次改动时预期的结果。可选方向，留给后续讨论/决定：

1. **补位改用更松的判定**：`_apply_theme_verdicts` 已经在区分「与生物药 CMC 总体相关」和「属于某个具体主题」——补位可以只要求前者（`triage.modalities`/一般相关性），不要求精确落在当周聚出的窄主题里，命中哪个主题就归哪个主题展示。
2. **维持现状**：接受"语料主题窄 + 当周新文献也薄"的周会经常补不满 15 篇，靠 §3.2 的新日志尽早发现，人工判断是否需要调整 `n_clusters`（当前 5）或主题描述让分诊更容易判"属于"。
3. **两者结合的其它方案**（例如给补位单独配一条更低的主题匹配阈值，而不是全豁免）。

本文档不替用户做这个决定，只把证据和取舍摆清楚。

---

## 6. 变更文件清单

| 文件 | 改动 |
| --- | --- |
| `src/zotero_arxiv_daily/dedup.py` | 标题精确匹配即合并，不再要求 DOI 兼容；更新文档字符串 |
| `src/zotero_arxiv_daily/backfill.py` | 取数/去重/过闸三段计数；掉数时升级为 WARNING |
| `src/zotero_arxiv_daily/weekly.py` | `delivered` 低于 `min_papers` 时打 WARNING |
| `src/zotero_arxiv_daily/retriever/crossref_retriever.py` | `select` 去掉无效的 `affiliation` 字段 |
| `config/base.yaml` | 澄清 `min_papers`/`max_papers` 注释 |
| `tests/test_dedup.py` | 新增/改写 3 项，覆盖策略变更与本次回归场景 |
| `tests/retriever/test_crossref_retriever.py` | 新增 1 项，锁定 `select` 字段列表 |

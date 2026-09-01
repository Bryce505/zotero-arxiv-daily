# 2026-08-W4 测试周报复检：重复文献与经典补位缺失 设计与修复记录

**日期：** 2026-09-01
**状态：** 已实现（回溯记录，非预先评审）
**上游：** `docs/superpowers/specs/2026-08-22-digest-relevance-and-structure-design.md`（相关性闸门与经典补位的原始设计）
**关联提交：** `47c69ad`/`0f1e043`（标题/链接错配，本轮之前）

---

## 0. 说明：为什么这份文档是回溯写的

本仓库的既有约定是先写 spec/plan 再动代码。这一轮改动体量小（两处策略/可见性修复），且用户是拿着一份实测输出直接指出具体症状，诊断与修复是同一次会话内连续完成的。按约定补一份 spec，把诊断证据、根因与取舍记录下来，供以后复查——但没有先出一版「待评审」再等确认，这点与既有流程不同，如实说明。

---

## 1. 问题：实测诊断

测试素材：在分支 `claude/literature-report-link-mismatch-cnss4y` 上生成的 `2026-08-W4` 周报（`b2e96330-202608W4_2.html`，未提交进仓库，用户以附件形式提供）。

### 1.1 重复文献

「蛋白质结构、功能与质谱表征」分区内 #4、#5 是同一篇综述，标题逐字相同（仅末尾句号之差），却各自带一条**不同的** DOI：

| # | 标题 | 作者署名 | DOI | 期刊字段 |
| --- | --- | --- | --- | --- |
| 4 | Protein persulfidation in plants: a central regulator of multiple signaling pathways | Yang D et al. | `10.1007/s00299-026-03954-y` | *(空)* |
| 5 | Protein persulfidation in plants: a central regulator of multiple signaling pathways. | Yang Di et al. | `10.1016/j.plaphy.2023.107900` | Plant cell reports |

两条线索指向 #5 的 DOI 是错的、#4 的 DOI 是对的：

1. DOI 前缀 `10.1007/s00299-*` 正是 Springer 期刊 *Plant Cell Reports* 的注册前缀，与 #5 自己标出的期刊名「Plant cell reports」吻合；`10.1016/j.plaphy.*` 是 Elsevier *Plant Physiology and Biochemistry* 的前缀，与「Plant cell reports」对不上。
2. 这与上一轮修复的根因同构（见 `47c69ad`）：某个来源（大概率 PubMed）给一条本身标题/期刊都对的记录挂错了 DOI。上一轮修的是「错配 DOI 拉错全文」，这次是同一类脏数据在**去重阶段**造成的次生问题。

### 1.2 数量不足且经典补位未触发

该次周报共 5 篇（2 主题：色谱电泳纯度与含量分析 1 篇、蛋白质结构/功能/质谱表征 4 篇，其中 1 篇是上述重复），页面上**没有「经典补位」分区**——`digest.backfill` 为空列表，而 `report.min_papers = 15`，缺口达 10 篇，理论上必须触发 `backfill_papers()`。

排查 `weekly.py` / `backfill.py` 未发现补位触发条件本身有 bug（`shortfall = min_papers - len(chosen)`，`shortfall > 0` 即调用，逻辑与既有 14 项 `test_backfill.py` 用例一致）。补位在拿到候选后要过**同一道相关性闸**（spec 2026-08-22 §10，"经典补位走同一道闸"，即 `min_relevance=55` / `min_score=60`），这是有意设计，不能放松。

**结论：** 没有那次运行的日志，无法确定这次补位为空是 (a) OpenAlex 请求失败/限流、(b) 候选本身相关度不够、还是 (c) 测试环境用的桩 LLM 让分诊结果整体偏低——`backfill.py` 原来对「补位取到的篇数低于所需」只打 `INFO` 级日志，且不区分是取数阶段还是过闸阶段掉的，这本身就是一个可观测性缺口，见 §3.2。

### 1.3 min_papers / max_papers

用户问能否设两个独立变量控制篇数上下限。`config/base.yaml` **已经**是这样：

```yaml
report:
  min_papers: 15           # 低于此数（且新文献不够）时用经典补位补足
  max_papers: 25           # 配额硬上限（仅统计新文献，不含补位）
```

`custom.yaml` 未覆盖这两项，Hydra 合并后生效值就是 15/25，与用户预期的「15–25」区间一致。此项无需新增代码，只需把语义讲清楚（见 §3.3）。

---

## 2. 根因

### 2.1 去重策略在「两侧都带 DOI 但不同」时拒绝合并

`dedup.py::dedup_papers()` 原逻辑：候选先按 DOI 精确匹配；未命中时按标题回退匹配，但**只有当至少一侧没有 DOI 时**才允许合并（`other is None or doi is None or other == doi`）。

这是刻意设计（注释原文：「两个真正不同但标题相同的文献不应被误合并」），但前提假设是「两条记录都带 DOI 时，不同 DOI 就意味着不同文献」——1.1 的实测证明这个假设不成立：一个来源可以在标题、摘要都对的情况下单独把 DOI 挂错。拒绝合并的代价是把同一篇文献原样展示两次，且更差的是**两份都可能不完整**（#4 缺期刊、#5 的 DOI 不可信）。

### 2.2 补位掉数没有区分阶段、日志级别不够

`backfill_papers()` 只在最后打一行 `logger.info(f"Backfilled {len(chosen)} highly-cited papers (needed {needed})")`，缺口多大都是 INFO，且不说明缺口发生在「OpenAlex 没返回」「exclude/dedup 筛掉」还是「相关性闸拦下」哪一步。`weekly.py::run()` 里更是完全没有针对`len(delivered) < min_papers`的收尾判断——周报照常发出，运行日志上只有篇数，没有任何信号提示这是一次「补位失败」而非「本周本来就这么薄」。

---

## 3. 方案

### 3.1 去重：标题精确匹配即合并，不再要求 DOI 兼容

`dedup_papers()` 简化为：DOI 精确匹配优先；未命中则按标题精确匹配（忽略大小写/标点/空白），**无论两侧是否都带 DOI、DOI 是否不同**。保留的身份（含 DOI）以先出现的记录为准；后到的记录只贡献 `_MERGEABLE_FIELDS`（`pdf_url`/`journal`/`pub_date`/`cited_by_count`/`full_text`）里对方缺的空位，以及在先出现记录本身没有 DOI 时才补上 DOI。

**取舍**：逐字、多词的标题在两篇真正不同的文献之间偶然重合的概率极低；比起放任一篇文献在周报里出现两次（已经发生），这个风险可以接受。`tests/test_dedup.py` 新增：

- `test_same_title_collapses_even_when_both_sides_carry_different_dois`：策略变更的直接单测。
- `test_regression_persulfidation_duplicate_collapses_and_fills_the_journal_gap`：原样复现本次实测的两条记录，断言合并后只剩一条、DOI 取 `10.1007/s00299-026-03954-y`、期刊补齐为 `Plant cell reports`。
- `test_dedup_keeps_distinct_papers_that_do_not_share_a_title`：确认策略变更不影响「标题不同」的正常去重路径。

### 3.2 补位掉数可观测

`backfill_papers()`：记录取数管线三个阶段的计数（`fetched` → `after_dedup` → `gated`），当 `len(chosen) < needed` 时升级为 `logger.warning`，把三个数字都打出来，一眼能看出缺口卡在哪一步；达标时维持原来的 `INFO`。

`weekly.py::WeeklyExecutor.run()`：`delivered` 组装完之后，若 `len(delivered) < min_papers`，打一条 WARNING，报出「新文献数 + 补位数」的构成。不改变行为（该发的周报照常发），只是让「这周补位没兜住」从「翻日志才看得出」变成「运行结束就有一行摘要」。

### 3.3 配置注释澄清

`config/base.yaml` 的 `report.min_papers`/`max_papers` 补充注释，明确两者是独立控制项（补位触发线 / 配额硬上限），并指向补位是尽力而为、掉线时新的 WARNING 会说明原因，不再需要新增配置键。

---

## 4. 验证

```bash
.venv/bin/python -m pytest tests/test_dedup.py tests/test_backfill.py tests/test_weekly.py tests/test_config_wiring.py -q
# 85 passed

.venv/bin/python -m pytest tests/ -q --continue-on-collection-errors
# 581 passed, 3 failed（均为 tests/test_protocol.py 的 tiktoken 联网用例，
# 出口策略拒绝 openaipublic.blob.core.windows.net，属既有沙箱限制，与本次改动无关）
```

（沙箱内 `uv sync` 因 `download.pytorch.org` 被出口策略拒绝而无法完成；本轮验证复用了 Python 3.13 + pip 安装除 `torch`/`sentence-transformers` 外全部运行依赖的方式跑测试，覆盖到的正是 `-m "not slow"` 默认套件。）

---

## 5. 遗留问题

- **本次测试周报「5 篇且补位为空」的具体原因未定位**：没有那次运行的日志（是否配置了 `CONTACT_EMAIL`/OpenAlex mailto、当时用的是真实 LLM 还是桩服务、OpenAlex 请求是否命中限流等），只能确认代码路径本身没有逻辑 bug。§3.2 的可观测性改动是为了让**下一次**出现同样症状时可以直接从日志定位，而不是重新走一遍代码审查。
- **去重策略变更的边界情况**：如果未来出现「标题逐字相同但确实是两篇不同文献」的真实案例（例如同一系列丛书不同分册使用完全相同的标题），会被合并为一条。目前认为概率低于「同一文献因脏 DOI 重复出现」，如后续观测到反例需要重新评估。

---

## 6. 变更文件清单

| 文件 | 改动 |
| --- | --- |
| `src/zotero_arxiv_daily/dedup.py` | 标题精确匹配即合并，不再要求 DOI 兼容；更新文档字符串 |
| `src/zotero_arxiv_daily/backfill.py` | 取数/去重/过闸三段计数；掉数时升级为 WARNING |
| `src/zotero_arxiv_daily/weekly.py` | `delivered` 低于 `min_papers` 时打 WARNING |
| `config/base.yaml` | 澄清 `min_papers`/`max_papers` 注释 |
| `tests/test_dedup.py` | 新增/改写 3 项，覆盖策略变更与本次回归场景 |

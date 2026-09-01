# 补位放宽 / 多轮检索 / 特定主题检索 设计定稿

**日期：** 2026-09-01
**状态：** 待实现
**上游：**
- `docs/superpowers/specs/2026-08-22-digest-relevance-and-structure-design.md`（相关性闸门与经典补位原始设计）
- `docs/superpowers/specs/2026-09-01-w4-test-digest-duplicate-and-thin-week.md`（§5 留下的开放问题，本文档给出决定）
**参考：** `imbad0202/academic-research-skills` 的 `deep-research` 技能（见 §6）

---

## 1. 背景：为什么现在改

run [33517443909](https://github.com/Bryce505/zotero-arxiv-daily/actions/runs/33517443909) 的日志给出确切数字：

```
Relevance gate: 5/60 passed (55 unjudged, 0 below relevance 55, 0 below score 60)   # 新文献
Relevance gate: 0/13 passed (13 unjudged, 0 below relevance 55, 0 below score 60)   # 经典补位
Backfilled 0 highly-cited papers (needed 10)
```

两处都不是相关度/综合分不达标（各 0 篇），全部是主题匹配闸（`_apply_theme_verdicts`，PR #2）判定「与生物药 CMC 总体相关，但不属于本周语料聚出的任何一个具体主题」。

补位复用同一道闸是 2026-08-22 spec §10 的设计；主题匹配是 PR #2 的设计。各自成立，叠加后语义错配：**经典补位取的是"该领域公认重要"的高被引文献，闸门问的却是"是否精确落在本周语料自动聚出的窄主题里"**——后者对补位比对新文献更难通过，13/13 全灭就是结果。

用户就此拍板：补位改用更松的判定；补位仍不够时多轮换检索式重试；另外新增一条用户自定主题的检索线。

---

## 2. 目标与非目标

### 目标

1. **补位放宽**：补位候选只要求「与生物药 CMC 总体相关」，不要求落进当周窄主题；LLM 给出明确主题归属时仍按该主题展示，判「无」则沿用嵌入相似度给的临时归属。
2. **多轮补位检索**：一轮补不满时重新生成检索式再检索，总轮数 ≤ 3。
3. **特定主题检索（新功能）**：由变量传入「主题（必填）+ 背景（选填）」，LLM 消化后生成检索式，检索该主题文献并单列一节汇总进周报；变量为空则整条线不启用、不产生任何 LLM 或网络调用。
4. 三项都必须在只有摘要、且任一外部源挂掉时仍能降级运行，不拖垮整轮。

### 非目标

- 不改新文献（fresh candidates）的主题匹配闸——PR #2 对新文献的收紧是有效的，本次不动。
- 不引入新依赖，不改上游 firehose 检索器与 `BaseReranker` 的公开行为。
- 特定主题一节不参与「本周优先读」评选（见 §5.4 理由）。

---

## 3. 补位放宽

### 3.1 接口

```python
# triage.py
def triage_papers(papers, client, llm_params, batch_size=8, themes=None,
                  require_theme_fit=True) -> None
def _apply_theme_verdicts(papers, themes, require_theme_fit=True) -> None

# weekly.py
def _gate(self, papers, require_theme_fit=True) -> list[Paper]
```

补位调用点传 `gate=lambda papers: self._gate(papers, require_theme_fit=False)`。

### 3.2 `require_theme_fit=False` 时的三种判决

| LLM 的 `cluster` 判决 | 严格模式（新文献，不变） | 放宽模式（补位） |
| --- | --- | --- |
| 某个真实主题名 | 覆盖嵌入给的临时归属 | **同样覆盖**（更准的归属，白拿） |
| `"无"` | `paper.triage = None`，淘汰 | **保留**，`paper.cluster` 维持嵌入给的临时值，按该主题展示 |
| 缺失/无法解析 | 原样保留 | 原样保留 |

放宽只影响"是否淘汰"，不影响相关度与综合分两道数值闸——一篇被判 30 分的补位候选照样进不来。

### 3.3 日志

放宽模式下把被保留的"无主题归属"篇数打成 INFO（`N candidate(s) kept without a theme fit`），与严格模式的 `... excluded` 对称，便于事后核对补位到底放进来了什么。

---

## 4. 多轮补位检索

### 4.1 轮次协议

```
round 1: 用 profile.plain_query（与现状一致）
  ↓ 仍不足 needed 且 round < max_rounds
round 2..3: requery() 生成"换一批词"的检索式 → 重新检索
```

- 上限 `_MAX_ROUNDS = 3`（含第一轮）。
- 每轮结束即判定：已凑够 `needed` 立即停；`requery` 缺省或返回空则停。
- 跨轮累计 `seen`（DOI + 标题键）：**包含被闸门淘汰的候选**，避免第二轮把同一批文献再过一次 LLM 闸（纯浪费预算）。

### 4.2 `requery` 契约

```python
# search/profile.py
def alternate_queries(profiles, tried: dict[str, list[str]], client, llm_params) -> dict[str, str]:
    """每个主题给一条"换过词"的新检索式；失败返回 {}，绝不抛异常。"""
```

提示词要求：给出与已试过的检索式**用词不同**的表达（同义词、相邻方法学、上位/下位概念），仍限定在该主题描述范围内；输出 `{"主题名": "检索式"}` 的 JSON。解析失败或调用失败 → 记 WARNING 返回 `{}`，退化为"只跑第一轮"，与现状等价。

注入点沿用既有风格：`backfill_papers(..., requery=None)` 由 `weekly.py` 传入闭包，测试用 stub 注入，`backfill.py` 本身不认识 LLM 客户端。

### 4.3 日志

每轮一行 INFO（`Backfill round k/3: fetched → after exclude/dedup → passed gate → running total`），总量仍不足时保留既有 WARNING。

---

## 5. 特定主题检索

### 5.1 配置契约

```yaml
search:
  focus:
    topic: ${oc.env:FOCUS_TOPIC,null}          # 关注主题；留空 = 整条线不启用
    background: ${oc.env:FOCUS_BACKGROUND,null} # 可选背景，帮助生成更贴切的检索式
    min_papers: 3        # 少于这么多时用高被引经典补足
    max_papers: 8        # 这一节最多收录几篇
    min_relevance: 60    # 与该主题的相关度下限（LLM 判定，0-100）
```

`topic` 为空（null / 空串 / 纯空白）时：不建 profile、不发 LLM 请求、不检索、报告里没有这一节。这是"空值即关闭"的硬约束，测试要盯死。

### 5.2 流水线

```
topic (+background)
  ↓ LLM 消化 → QueryProfile（mesh/free/pubmed/plain 四种形态）+ 一句话主题简述
按 search.sources 逐源检索（与周报同一个日期窗口）
  ↓ dedup + 排除库内/往期/本期已选 DOI
主题相关度分诊（**独立评分细则：对齐用户主题，不是生物药 CMC 细则**）
  ↓ score_papers（期刊/企业加分照常，用于徽标与排序）
  ↓ relevance >= focus.min_relevance
不足 focus.min_papers → OpenAlex 高被引兜底（不限日期）→ 同一套分诊与门槛
  ↓ 按 rank_score 降序，截到 focus.max_papers
```

**为什么单独一套分诊细则**：用户的主题可能整体落在生物药 CMC 之外（例如"AI 在制药工艺放大中的应用"）。套用 CMC 细则会把用户明确点名要的方向判成 20-54 分全部淘汰——那这个功能就没意义了。所以这条线判的是「与用户给的主题相关吗」，`reason` 同样一句话，直接当作该文献的推荐理由渲染。

### 5.3 呈现

markdown / 网页 / 邮件三份渲染一致，位置在库内主题之后、经典补位之前：

```markdown
## 特定主题：{topic}（N 篇）

> {一句话主题简述}

### {文献标题}
（其余格式与其他主题完全一致：徽标行、推荐理由、结构化字段）
```

网页版同样进目录与编号体系（锚点 `#p-N`），`需人工取全文` 一节把这些文献一并计入，`digest.total` 计入。

### 5.4 不进「本周优先读」

top picks 现在是按 `rank_score` 在库内主题文献里挑。特定主题文献的相关度是拿**另一把尺子**（用户主题）量出来的，两者不可比；混排会让"相关度 92"在同一份报告里指两件事。这一节自带标题与简述，本身就是显眼位置，不需要再挤进优先读。

---

## 6. 从 `deep-research` 借来的东西

参考 `imbad0202/academic-research-skills` 的 `deep-research` 技能（v3.9.x），有用的是它的**检索方法学**，不是它的 13-agent 编排（那套是为产出完整研究报告设计的，与本仓库"每周自动跑一次、无人值守"的定位不匹配）：

| 借鉴点 | 出处 | 落到本设计哪里 |
| --- | --- | --- |
| 检索式 = 主词 + 同义词 + 相关词 + 布尔组合，四要素分开写 | `bibliography_agent.md` §Search Strategy Framework Step 1 | §5.2 复用既有 `QueryProfile` 的四形态（mesh/free/pubmed/plain），特定主题走同一结构 |
| `uncovered_topics` → `search-fills-gap`：覆盖不足就换角度补检索，而不是重复同一条式子 | `bibliography_agent.md` §Step 2 | §4「换词重试」而不是「重试同一条式子」 |
| 零命中要显式上报，不能静默继续 | `bibliography_agent.md` §Zero-hit and provenance reporting | §4.3 每轮日志 + 既有 shortfall WARNING |
| 两遍筛选：先标题摘要、后全文 | `bibliography_agent.md` §Step 4 | 既有 triage（标题+摘要）→ extract（全文）已是这个结构，不改 |
| **检索回来的内容是数据，不是指令** | `bibliography_agent.md` §Core Principles | §7 安全性 |

明确不借：多 agent 编排、PRISMA 流程图、证据分级 I–VII、devil's advocate 检查点。周报是无人值守的定时任务，这些需要人在环的环节没有落点。

---

## 7. 安全性：用户输入与检索结果都当数据

- `FOCUS_TOPIC` / `FOCUS_BACKGROUND` 是用户自己填的，进提示词是正常用法；但它们只用于**生成检索式与判定相关度**，不允许改变输出结构或其它阶段的行为。
- 检索回来的标题/摘要/全文一律当数据。既有 `extract.py` / `triage.py` 的提示词已经是"读下面这篇文献并作答"的形态，本次新增的两个提示词沿用同样口径，不引入"按文中指示执行"这类措辞。

---

## 8. 错误处理与降级

| 情形 | 行为 |
| --- | --- |
| `focus.topic` 为空 | 整条线不启用；无 LLM、无网络调用；报告无此节 |
| 主题消化 LLM 调用失败 | WARNING，退化为"用 topic 原文当 plain_query，其余形态留空"，仍然检索 |
| 特定主题检索零命中 | WARNING，该节不渲染（不留空标题） |
| 主题分诊失败 | 该篇 `triage=None` → 不计入（沿用既有"未判定即不投递"口径） |
| `alternate_queries` 失败 | WARNING，返回 `{}`，补位退化为单轮 |
| 补位仍不足 `needed` | 既有 WARNING（含三段计数），周报照发 |
| OpenAlex/任一源挂掉 | 既有 `try/except` 降级，返回空列表 |

---

## 9. 变更文件清单

**新增**

| 文件 | 职责 |
| --- | --- |
| `src/zotero_arxiv_daily/search/focus.py` | 特定主题：配置读取、主题消化、检索编排、结果结构 |
| `tests/search/test_focus.py` | 对应测试 |

**修改**

| 文件 | 改动 |
| --- | --- |
| `triage.py` | `require_theme_fit` 开关；新增面向用户主题的分诊 `triage_for_topic` |
| `search/profile.py` | 新增 `alternate_queries()` |
| `backfill.py` | 多轮循环 + `requery` 注入 + 跨轮 seen |
| `weekly.py` | `_gate(require_theme_fit)`；补位传放宽闸与 requery；接入特定主题；`delivered` 汇总 |
| `report.py` | `Digest.focus`；三个渲染器 + 目录 + 编号 + 需人工取全文 + total |
| `config/base.yaml` | `search.focus.*` 五个键 |
| `tests/test_triage.py` · `tests/test_backfill.py` · `tests/test_report.py` · `tests/test_weekly.py` | 对应用例 |

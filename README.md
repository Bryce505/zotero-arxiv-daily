# CMC 生物药文献周报（zotero-arxiv-daily fork）

本仓库 fork 自 [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)。上游项目按 Zotero 文献库每天推荐新的 arXiv 论文；这份 fork 保留了那条日报管线，并在此之上新增了一条独立的**周报管线**：每周自动检索 PubMed / Europe PMC / Crossref / OpenAlex 四个来源的新文献，用 LLM 判定与生物药 CMC（化学、生产与控制）分析的相关性，通过闸门与配额选出一批稿子，逐篇生成结构化摘要，最后邮件发送并归档回本仓库。全部计算跑在 GitHub Actions 免费额度内。

目标读者：**fork 之后要照着配好参数、把周报跑起来的人。** 想直接开始部署，跳到[第 4 节](#4-快速开始)；想弄懂某篇文章为什么进/没进周报，看[第 5 节](#5-文章是怎么选出来的)。

---

## 1. 这个项目做什么

一句话：每周把和你 Zotero 库相关、且经过相关性判定确实是生物药 CMC 方向的新文献选出来，生成中文结构化摘要，发邮件、存进仓库。

下面是首期周报（`reports/2026/2026-08-W3.md`）「本周优先读」第一条的真实节选：

```markdown
## 本周优先读

- [Comparative Orthogonal RP-UHPLC and SEC-UHPLC Methods for Quantitative
  Analysis and Integrity Assessment of Cetuximab-Based Antibody–Drug
  Conjugates](https://doi.org/10.3390/separations13080236)
  Separations · 2026-08-18 · Cuffaro Doretta et al.

### Comparative Orthogonal RP-UHPLC and SEC-UHPLC Methods for Quantitative
    Analysis and Integrity Assessment of Cetuximab-Based Antibody–Drug
    Conjugates

Separations · 2026-08-18 · Cuffaro Doretta et al.
DOI: <https://doi.org/10.3390/separations13080236>

**背景：** 抗体药物偶联物（ADC）结构复杂，其定量表征常受载荷相关干扰和分子
异质性影响，需要开发可靠的分析方法。

**待解决的问题：** 此前尚缺乏将反相与体积排阻超高效液相色谱联用、同时对ADC
进行定量和完整性评估的互补性方法，尤其是针对西妥昔单抗（Cetuximab）类ADC。
```

这一期是在相关性闸门上线**之前**跑的，所以「方法/结论/洞见」还是整段文字，标题下也没有相关度徽标行——现在的管线会把这三个字段拆成有序列表，并在标题下加一行形如 `相关度 82 · 核心期刊` 的徽标（见第 5、7 节）。之所以仍然用这一期举例，是因为它就是催生本次改造的原始证据：同一期里混进了一篇讲钠离子电池负极材料的论文，与生物药毫无关系——第 5 节会具体讲清楚新闸门怎么把这类稿子拦下。

## 2. 与上游 zotero-arxiv-daily 的关系

**保留了什么：**

- 日报管线（`main.py` / `main.yml`）原样保留：按 Zotero 库嵌入相似度，每天推荐新发布的 arXiv / bioRxiv / medRxiv / chemRxiv 论文，逻辑与上游一致。
- 检索器与重排器的插件式注册表机制（`@register_retriever` / `get_retriever_cls`，`@register_reranker` / `get_reranker_cls`）沿用，新增来源仍然是「写一个类 + 加一行装饰器」。
- Hydra 组合 `config/base.yaml` + `config/custom.yaml` 的配置模式沿用，但两层的分工发生了实质变化——这是本次改造踩过坑之后才讲清楚的，见第 6 节。
- AGPLv3 许可证与上游依赖（`pyzotero`、`arxiv.py`、`sentence-transformers`）不变。

**新增了什么（这是本仓库存在的理由）：**

- **周报管线**（`weekly.py` / `weekly.yml`）：聚类 Zotero 库主题 → LLM 蒸馏检索式 → 查询 4 个学术检索源 → **相关性分诊 + 综合分闸门**（本次改造的核心）→ 主题配额 → 经典补位 → 抓取开放获取全文 → 结构化抽取 → 渲染三份产物 → 邮件 + 归档进仓库。
- 一套新的查询式检索器注册表（`@register_query_retriever` / `get_query_retriever_cls`），服务周报用到的 `pubmed` / `europepmc` / `crossref` / `openalex`——它们与日报用的 `arxiv` / `biorxiv` / `medrxiv` / `chemrxiv` 是两套不同的检索接口，见第 3 节。
- **月度综述**（`monthly.py` / `monthly.yml`）：每月读当月所有周报，产出跨篇归纳，独立运行、挂了不影响周报。
- **预检**（`preflight.py` / `preflight.yml`）：不发邮件、不写文件、不提交的 7 项检查，抢在正式跑之前把配置和外部依赖的问题暴露出来，见第 9 节。
- **相关性分诊与综合分闸门**：LLM 给每篇候选打 0–100 的相关度分，命中期刊/企业名单再加分，两道阈值都过了才进周报，见第 5 节。
- **报告字段类型系统**：每个字段声明是 `text`（一段带因果链的叙述）还是 `list`（拆成「关键词 + 说明」的有序列表），不再是一整段无结构文字，见第 7 节。
- **单位/期刊名单匹配**（`affiliation.py`）：从四个检索源的元数据里提取作者单位，命中期刊或企业名单则加分并在徽标行标注。

## 3. 架构

两条流水线彼此独立，可以只启用其中一条：

**日报（`main.py`，继承自上游）：**

```
Zotero 库 ──┐
            ├─ 嵌入相似度打分（加权：新收录的库内文献权重更高）──▶ 排序 ──▶ LLM 生成 TLDR ──▶ 邮件
arXiv/bioRxiv/medRxiv/chemRxiv 当日新论文 ──┘
```

**周报（`weekly.py`，本次新增，见第 5 节展开）：**

```
Zotero 库主题聚类 + 检索式蒸馏
  ↓
四源检索（pubmed / europepmc / crossref / openalex）× 5 个主题
  ↓ 去重 + 去已读
嵌入打分 + 主题归类
  ↓ 取嵌入分最高的 report.triage_pool 篇
LLM 分诊（相关度 0–100 + 推荐理由 + 命中的生物药类型）
  ↓ 综合分 = 相关度 + 期刊加分 + 企业加分
双闸门：相关度 ≥ min_relevance 且 综合分 ≥ min_score
  ↓ 按主题配额，只在过闸文献里分
选定 ──▶ 不足 min_papers 则经典补位（同样过闸）
  ↓
抓取开放获取全文 → 结构化抽取（携带分诊结论）→ 渲染 → 邮件 + 归档进仓库
```

「主题归类」不是纯粹的向量最近邻：候选文献落进哪个簇，由「和簇内语料成员的平均相似度」与「和簇的一句话主题描述的相似度」加权得出，后者权重更高（默认 0.6，`search.cluster_assignment_description_weight`）——语料均值是弥散信号，容易被词面重合带偏；主题描述是更精确的锚点，见第 5 节的实例。

**三套插件式注册表：**

| 注册表 | 装饰器 | 查找函数 | 服务谁 | 现有实现 |
| --- | --- | --- | --- | --- |
| retriever | `@register_retriever(name)` | `get_retriever_cls()` | 日报 `executor.source` | `arxiv` `biorxiv` `medrxiv` `chemrxiv` |
| query retriever | `@register_query_retriever(name)` | `get_query_retriever_cls()` | 周报 `search.sources` | `pubmed` `europepmc` `crossref` `openalex` |
| reranker | `@register_reranker(name)` | `get_reranker_cls()` | 两条管线共用的 `executor.reranker` | `local`（sentence-transformers）`api`（OpenAI 兼容 embedding） |

数据类：`Paper` 与 `CorpusPaper` 定义在 `src/zotero_arxiv_daily/protocol.py`；`Paper` 上挂着调 LLM 的 `generate_tldr()` / `generate_affiliations()`，新增的分诊结论存在 `paper.triage`（`TriageResult`：`relevance` / `reason` / `modalities`），综合分存在 `paper.scoring`。

## 4. 快速开始

1. **Fork（并 Star）本仓库。** 建议保持私有——周报正文会带着你 Zotero 库的主题信息，且抓下来的 PDF 会提交进仓库。
2. **配置 Secrets**（仓库 Settings → Secrets and variables → Actions → **Secrets** 标签）。两条管线都要用到 `ZOTERO_ID`、`ZOTERO_KEY`、`SENDER`、`SENDER_PASSWORD`、`OPENAI_API_KEY`、`OPENAI_API_BASE`；只想跑周报还需要额外几个（完整清单、去哪申请见第 8 节和 [`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md) 第 2 节）。
3. **配置 `CUSTOM_CONFIG` 变量**（同一页面 → **Variables** 标签）。这是运行时真正生效的配置来源，具体怎么写、和 `config/base.yaml` 怎么分工，见第 6 节；周报专用的完整样例见 [`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md) 第 1 节，可以直接复制粘贴。
4. **跑一次预检**：Actions → **CMC weekly preflight** → Run workflow。全绿再进行下一步；有 `FAIL` 先按提示修（第 9 节讲每一项验证什么）。
5. **手动触发一次正式跑**：日报是 **Send emails daily**，周报是 **CMC literature weekly digest**。之后日报每天 22:00 UTC、周报每周五 12:00 UTC 自动跑一次；月度综述（**CMC literature monthly synthesis**）每月 1 号 13:00 UTC 跑一次，可选，禁用不影响周报。

首次跑周报会比之后每周都慢——主题聚类、检索式蒸馏、语料向量三个缓存都是冷的，之后会命中缓存。首跑该看什么日志、常见的坑，[`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md) 第 4、5 节有实测记录，不在这里重复。

## 5. 文章是怎么选出来的

新手最常见的困惑是「这篇为什么进了周报、那篇为什么没有」。选文其实是一段两段式漏斗：先检索出一个大候选池，用嵌入相似度粗排，再用 LLM 逐篇判定「是不是真的和生物药相关」，最后按综合分和主题配额选定、不够再补位。下表逐级列出每一步淘汰了什么、由哪个参数控制：

| 阶段 | 淘汰了什么 | 由哪个参数控制 |
| --- | --- | --- |
| 主题聚类 + 检索式蒸馏 | —（决定去检索什么，不淘汰候选） | `search.n_clusters` |
| 多源检索 | —（决定候选池有多大） | `search.sources`、`search.per_cluster_limit` |
| 去重 / 去已读 | 重复 DOI、库里已有的、往期已投递过的 | `search.seen_state` |
| 嵌入打分 | —（只排序，决定谁先送分诊，不淘汰） | `executor.reranker` |
| 送分诊 | 嵌入相似度最低的一批（这是成本上限，不是质量判断） | `report.triage_pool` |
| **相关性分诊** | **与生物药无关、或迁移价值很低的文献** | `report.triage_batch` |
| **相关度硬下限** | **仅名词/技术表面重合、真要迁移得重新开发的（20–54 分档）** | `report.min_relevance` |
| **综合分闸门** | **相关度过了、但综合分仍不够优的** | `report.min_score`、`report.journals.bonus`、`report.industry.bonus` |
| 主题配额 | 单个主题候选过度集中时，超出名额的部分 | `report.max_papers`、`report.min_per_cluster` |
| 经典补位 | —（不淘汰，只补数量，补位候选同样要过闸） | `report.min_papers` |

### 用首期真实数据走一遍

第 1 节引用的首期周报（`reports/2026/2026-08-W3.md`，2026-08-14 ~ 2026-08-21，共 25 篇）是在这套闸门上线**之前**跑的，选文只靠嵌入相似度排序和主题配额——这正是催生本次改造的原始证据。用它举两个具体例子：

**钠离子电池那篇会在哪一步被拦下。** 那一期混进了一篇《The First Electrochemical Cycle: State-of-Charge Dependent Formation and Evolution of the Solid Electrolyte Interphase on Hard Carbon Anodes in Sodium-Ion Batteries》（*Small Methods*，2026-08-14，Schäfer David et al.，DOI `10.1002/smtd.70958`），讲的是钠离子电池硬碳负极固态电解质界面（SEI）的形成过程，与生物药 CMC 没有任何关系。它当初能混进来，是因为「多模式原位表征」一类的词面特征让嵌入相似度误判成了「和你的色谱/质谱文献库像」。分诊评分细则里，`0–19`（无关）档明确把「电池材料」列为必须打低分的反例，这篇会落在这一档；`report.min_relevance: 55` 直接拦下它——期刊加分、企业加分都救不了，这条硬下限就是为这类稿子设的。

**ADC 那篇为什么排在第一。** 同一期「本周优先读」第一条就是第 1 节引用的那篇 ADC 分析方法论文（*Separations*，DOI `10.3390/separations13080236`）。研究对象本身是抗体药物偶联物（ADC），落在分诊细则 `80–100`（高）档；同时它发在 *Separations*——这本刊在 `report.journals.allow` 名单里，命中后综合分再加 `report.journals.bonus`（默认 10 分）。相关度本身就高，又叠加了期刊命中，两项一起让它在综合分排序里稳居第一，配额分配时自然优先拿到名额。

**壳聚糖酶那篇为什么被分进了「宿主细胞蛋白分析」。** 这是闸门上线**之后**实测发现的第二个问题：一篇讲疫苗效力检测（用壳聚糖酶消除壳聚糖佐剂对效价测定的干扰）的论文，正确通过了闸门（相关度 92，命中企业加分），但被归到了「宿主细胞蛋白（HCP）分析」这个簇下——内容跟 HCP 毫无关系。根因是当时候选归簇纯靠候选文献和簇内语料成员的**平均**相似度决定，而这个簇里恰好聚了不少讲蛋白质定量分析方法的文献，词面上的方法学重合把它带偏了。现在候选归簇同时看簇的**一句话主题描述**（「宿主细胞蛋白残留检测与定量分析」之类，聚类时 LLM 生成、以前生成了但没用上），且这个信号权重更高（默认 0.6）——这篇论文的摘要跟「疫苗」「佐剂」「效力检测」相关的簇描述会更接近，归簇结果理应随之改善。

### 调参对照表

| 症状 | 先动这个参数 | 往哪个方向 |
| --- | --- | --- |
| 周报太杂（进了不少沾边但不该进的） | `report.min_relevance` / `report.min_score` | 调高。同时查一下是不是有覆盖全学科的大刊（如 *PLoS ONE*、*Nature Communications*）在 `report.journals.allow` 里把不太相关的稿子用加分顶了上来——从名单里单独移除即可，不影响机制 |
| 周报太薄（总凑不满 `min_papers`，全靠补位） | `report.min_relevance` / `report.min_score` | 调低（不建议低于 55——那是分诊细则「中」档的下沿，再低就进了「低」档）；也可以调高 `report.triage_pool`，让更多候选有机会被分诊看到，而不是在嵌入排序阶段就被挡在门外 |
| 某个方向总是漏（比如某个主题总凑不满配额） | `report.min_per_cluster`、`search.per_cluster_limit` | 调高，给这个主题在配额分配里留保底名额、在检索阶段留更大的候选池；同时检查 `report.journals.allow` / `report.industry.names` 是不是漏收了这个方向常发的期刊或企业 |

`min_relevance: 0` 且 `min_score: 0` 等价于关掉闸门，退回旧行为，便于对照排查是不是闸门本身的问题。

## 6. 配置在哪里、怎么改

配置由 Hydra 在运行时合并，顺序见 `config/default.yaml`：

```yaml
defaults:
  - base
  - custom
```

先加载 `config/base.yaml`，再用 `config/custom.yaml` 覆盖。两个文件的分工：

| 文件 | 在哪 | 放什么 |
| --- | --- | --- |
| `config/base.yaml` | 提交在 git 里 | 默认值、长名单（期刊/企业）、报告字段定义 |
| `config/custom.yaml` | **每次 workflow 运行时被整份覆写** | 引用 secrets 的项、与运行环境相关的设置 |

**仓库里那份 `config/custom.yaml` 在 CI 中完全不生效。** `main.yml`、`weekly.yml`、`preflight.yml`（`monthly.yml` 同理）里都有这一行：

```bash
printf "%b\n" "$CUSTOM_CONFIG" > config/custom.yaml
```

`$CUSTOM_CONFIG` 来自仓库 Settings → Secrets and variables → Actions → **Variables** 里的 `CUSTOM_CONFIG` 变量：运行时先把它写进 `config/custom.yaml`，**整份覆盖掉仓库里那份文件的内容**，再启动 Hydra。也就是说，在网页上直接编辑仓库里的 `config/custom.yaml` 文件本身（它现在只是本地跑和读测试默认值时会用到的示例）对线上任何一次运行都没有影响——真正生效的是 `CUSTOM_CONFIG` 这个 Variable。

**列表整体替换的陷阱。** OmegaConf 合并两层配置时，字典是逐键合并的，但列表是**整体替换**，不是拼接。实测：

```python
from omegaconf import OmegaConf

base   = {"report": {"journals": {"bonus": 10, "allow": ["mAbs", "Analytical Chemistry", "Separations"]}}}
custom = {"report": {"journals": {"allow": ["Nature"]}}}
OmegaConf.merge(base, custom).report.journals
# → {"bonus": 10, "allow": ["Nature"]}     ← 三本刊全没了，bonus 还在
```

`bonus` 是标量，逐键合并后保留下来；`allow` 是列表，`custom` 里的 `["Nature"]` 把 `base` 里的三本期刊**整个顶替掉**，不是加进去，而且**不会报错、没有任何提示**。这意味着：`report.journals.allow`（63 本期刊）、`report.industry.names`（52 家企业）、`report.fields`（5 个报告字段）这三份长内容**必须写在 `config/base.yaml` 里，绝不能放进 `CUSTOM_CONFIG`**——放进 `CUSTOM_CONFIG` 之后，任何一次「只想加一本期刊」的修改，只要没把其余 62 本重新粘贴一遍，就会静默丢掉它们，下一次周报直接用一份缺了 62 本刊的名单跑，而你不会收到任何警告。`min_relevance`、`min_score` 这几个单个数值没有这个问题，两处都能改，但放 `base.yaml` 能顺带获得 git 的修改历史，所以默认也放在这里。

**两条修改路径：**

1. **改名单 / 改报告字段** → 打开仓库网页 → `config/base.yaml` → 铅笔图标编辑 → 改完 commit。网页编辑器认得 YAML，缩进错了会标红提示——这层保护是 Variables 的纯文本框没有的。
2. **改密钥 / 改收件人** → 仓库 Settings → Secrets and variables → Actions → **Variables** 标签 → 编辑 `CUSTOM_CONFIG`。

**两条路径改完都要跑一次 preflight**（Actions → CMC weekly preflight → Run workflow）——改名单/字段会被 `report-config` 检查校验（第 9 节第 1 项），比跑一次完整周报便宜得多。

**改 Variables 不会影响正在跑的 job。** `${{ vars.CUSTOM_CONFIG }}` 在 job 启动那一刻就解析完毕，之后再改 Variables，那次 job 用的还是启动时的值；改 `config/base.yaml` 同理——workflow checkout 的是触发那一刻的 commit，晚一步的提交要等下一次触发才生效。

## 7. 配置详解

配置按功能分成几个顶层键，逐节的详细注释就写在 `config/base.yaml` 里，这里给的是导航：

| 顶层键 | 管哪条管线 | 大致内容 |
| --- | --- | --- |
| `zotero` | 两条都用 | `user_id` / `api_key`；`include_path` / `ignore_path` 两组 glob，筛选纳入哪些 Zotero 分类 |
| `source` | 日报（`arxiv`/`biorxiv`/`medrxiv`/`chemrxiv`）+ 周报四个查询源的凭据（`pubmed.api_key`/`email`，`crossref.mailto`，`openalex.mailto`，`europepmc` 无需凭据） | 各来源的类目筛选或联系方式 |
| `email` | 两条都用 | 发信账号、SMTP；`recipients` 是周报专用的多收件人列表，缺省回退到 `receiver` |
| `llm` | 两条都用 | API key/base_url、生成参数、`language`（周报默认中文摘要要显式设成 `中文`） |
| `reranker` | 两条共用 `executor.reranker` 选择 `local`/`api`；`vector_cache` 是周报专用的语料向量缓存路径 | 本地模型或 embedding API 的参数 |
| `executor` | 日报为主 | `source` 选用哪些日报检索器、`reranker` 选择、`max_paper_num` 等 |
| `search` | 周报专用 | `sources`（四个查询源）、`n_clusters`、`per_cluster_limit`、`cluster_assignment_description_weight`（候选归簇时主题描述信号的权重，见第 3 节）、三个缓存文件路径 |
| `fulltext` | 周报专用 | 是否尝试抓开放获取全文、`unpaywall_email`、单文件大小上限 |
| `report` | 周报专用，**新手最常改的部分** | 数量控制（`min_papers`/`max_papers`/`top_picks`/`min_per_cluster`）、闸门（`min_relevance`/`min_score`/`triage_pool`/`triage_batch`，见第 5 节）、`journals`/`industry` 两份加分名单（见第 6 节的列表陷阱）、`fields` 报告字段定义（见下） |
| `git` | 周报/月度综述 | 是否把产物提交回仓库、提交用的 user.name/email |

`report.fields` 每一项都是 `key`/`label`/`kind`/`instruction`，`kind: list` 时再加 `max_items`：

| `key` | `kind` | 说明 |
| --- | --- | --- |
| `background` | `text` | 120–180 字的因果链叙述：原来怎么做、遇到什么坎、为什么现在必须迈过去 |
| `gap` | `text` | 60–100 字，只讲此前尚未解决的那一个核心问题 |
| `method` | `list`（≤5 条） | 每条「关键词 + 30–60 字说明」，含关键参数 |
| `conclusion` | `list`（≤5 条） | 每条「一句话结论 + 支撑数据（回收率/纯度/R²/LOD-LOQ 等）」 |
| `insight` | `list`（≤3 条） | 每条必须落在分诊判定命中的生物药类型（ADC/单抗/双抗/疫苗载体等）上，说明具体怎么用 |

字数限制只走提示词预算，不做硬截断——截断会把句子拦腰砍断，比超字数更难看。想加字段直接改 `config/base.yaml`，不用碰代码，见第 12 节。

## 8. 环境变量与 Secrets 清单

| 名称 | 哪条管线需要 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ZOTERO_ID` | 两条都用 | 是 | Zotero 账号的 User ID（不是用户名），[这里](https://www.zotero.org/settings/security)查 |
| `ZOTERO_KEY` | 两条都用 | 是 | 有读权限的 Zotero API key，同上页面申请 |
| `SENDER` | 两条都用 | 是 | 发信邮箱账号 |
| `SENDER_PASSWORD` | 两条都用 | 是 | 发信邮箱的 SMTP 授权码，通常不是登录密码，找邮箱服务商要 |
| `RECEIVER` | 日报 | 日报必填 | 日报的单一收件地址 |
| `OPENAI_API_KEY` | 两条都用 | 是 | OpenAI 兼容 LLM 服务的 key |
| `OPENAI_API_BASE` | 两条都用 | 是 | 对应的 API base URL |
| `RECIPIENTS` | 周报 | 周报必填 | 周报的多收件人列表，逗号/分号分隔，全部走 Bcc 互相隐藏；不设则回退到 `receiver` |
| `EMBEDDING_API_KEY` | 周报（`reranker: api` 时） | 用 API embedding 就必填 | embedding 服务的 key。配置里引用了它却没建这个 secret，run 会在配置组装阶段直接崩 |
| `NCBI_API_KEY` | 周报（可选） | 否 | PubMed E-utilities 限速从 3 提到 10 req/s，自助秒批 |
| `CONTACT_EMAIL` | 周报（强烈建议） | 否 | 一个值喂四处：PubMed、Crossref 与 OpenAlex 的 polite pool，以及 **Unpaywall**——缺失时 Unpaywall 这一级会被整个跳过，全文命中率大幅下降 |
| `DEBUG` | 两条都用（可选） | 否 | 调试模式开关 |

周报专用的几个（`RECIPIENTS`、`EMBEDDING_API_KEY`、`NCBI_API_KEY`、`CONTACT_EMAIL`）以及完整的 `CUSTOM_CONFIG` 样例，[`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md) 第 1、2 节有可以直接复制的版本和踩坑记录，这里不重复。

## 9. 预检

`preflight.py` 依次跑 7 项检查，不发邮件、不写文件、不提交，探测每个边界要多便宜就多便宜：

| 顺序 | 检查名 | 验证什么 |
| --- | --- | --- |
| 1 | `report-config` | `report.journals.allow` / `report.industry.names` 能否解析成列表；每个 `report.fields` 的 `kind` 是否是 `text`/`list`；`min_relevance`/`min_score`/`triage_pool`/`triage_batch` 是否为非负整数（`triage_batch` 还要求 ≥1）；名单里归一化后的重复条目（只警告，不阻断）。不碰网络，跑得最快，所以排第一——名单和字段是手工编辑的部分，缩进错了不该等到周五发信才炸 |
| 2 | `zotero` | 能否用 `ZOTERO_ID`/`ZOTERO_KEY` 读到库，`include_path` 实际命中几篇 |
| 3 | `llm` | LLM API 能否正常应答，回答语言是否符合 `llm.language` |
| 4 | `sources` | 逐一探测 `search.sources` 里配置的每个查询式源（当前是 `pubmed`/`europepmc`/`crossref`/`openalex`，各占一行输出），用 `query_for_source()` 生成**和周报正式检索同一条路径**的查询去探测——避免探测用短查询、正式跑用长句导致「预检全绿、正式跑却 0 篇」的盲区 |
| 5 | `embedding` | embedding key 是否真的能调通并返回向量，而不只是配置解析成功；返回向量的维度 |
| 6 | `recipients` | 能否解析出至少一个收件人 |
| 7 | `smtp` | 能否用给定账号密码登录 SMTP 服务器 |

`report-config` 检查的实际输出形如：

```
[ OK ] report-config 63 journals, 52 companies, 5 fields (2 text / 3 list)
```

有 `FAIL` 就不要跑正式周报，先按提示修；`WARN` 不阻塞运行，但值得看一眼。完整实测输出样例见 [`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md) 第 3 节。

## 10. 产物

```
reports/2026/2026-08-W3.md      周报 markdown（归档进仓库）
reports/2026/2026-08-W3.html    周报网页版（归档，左侧目录+编号，见下）
library/2026/2026-08-W3/*.pdf   当周抓到的开放获取全文（见下的命名规则）
state/theme_clusters.json       主题聚类缓存
state/query_profiles.json       检索式蒸馏缓存
state/seen_dois.json            跨周去重记录
state/corpus_vectors.npz        语料 embedding 缓存（配置了 reranker.vector_cache 才有）
```

**网页版带左侧目录。** 每篇文献按阅读顺序（本周优先读 → 各主题簇 → 经典补位）编号 `#1`、`#2`……同一篇文献在多处出现（比如既是优先读又在自己的主题簇里）复用同一个号，不会重复编号。左侧是吸顶的侧栏目录，按分节列出全部文献，点击跳转到正文对应位置；窄屏（<860px）会自动收起成顶部横条。这个设计权衡过用带书签的 PDF 代替 HTML——同样内容 PDF 比 HTML 大出 60 倍以上（未优化字体嵌入时甚至到 4MB+），周报要按周提交进仓库、逐周累积，所以留在了 HTML。邮件正文不受影响，仍是线性滚动的摘要式布局。

**PDF 文件名是「年份-第一作者-标题」，不是 DOI。** 例如 `2026-Zhang_Wei-Charge_Variant_Analysis_of_Monoclonal_Antibodies-a3f9c2.pdf`，末尾一段短哈希（多数取自 DOI）防止同作者同年、标题前 80 字重合的两篇文献互相覆盖，也让同一篇文献重跑时文件名保持不变。缺年份/作者时分别退化成 `unknown`；标题、作者、发表日期都拿不到的极端情况退回 `paper-{序号}.pdf`。这个命名只影响新下载的文件，`library/` 目录里已归档的旧文件（DOI 命名）不会被重命名。

**为什么要提交回仓库：** GitHub Actions 的 runner 是一次性的，任务结束即销毁。三个缓存、去重记录、周报本身如果只留在 runner 本地，下一次运行就凭空消失——聚类和检索式蒸馏得重新跑一遍 LLM，去重记录归零会导致往期文献重复推送。`git.enabled: true` 时，`weekly.py` 在发信之后把这些路径 commit 并 push 回触发它的分支；push 失败会让整个 run 直接失败（不会被静默吞掉，见第 11 节），因为 runner 销毁后仓库里的这份 commit 是唯一副本。

邮件正文是表格布局的摘要式 HTML，压在 Gmail 的 102KB 截断阈值内；附件是完整的周报 HTML，再加最多 `report.attach_pdfs`（默认 5）篇优先读的 PDF。**周报正文只放 DOI 链接，不放仓库内的 PDF 路径**——仓库是私有的，收件人不一定是 collaborator，点开会打不开，完整 PDF 要去仓库的 `library/` 目录拿。

## 11. 排错

| 现象 | 先查什么 |
| --- | --- |
| 周报为空 / 没收到邮件 | 先看 preflight 是否全绿；`recipients` 解析不出人会直接导致无收件人；`zotero` 匹配 0 篇多半是 `include_path` 的 glob 写错——两段常常缺一不可（如 `["文献", "文献/**"]`），只写后一段不匹配根分类本身 |
| 周报太杂 / 太薄 / 某个方向总漏 | 第 5 节的调参对照表——这三类症状几乎总是 `report.min_relevance` / `report.min_score` / `report.min_per_cluster` 之一 |
| 摘要全是英文 | `llm.language` 没设成 `中文`，默认值是 `English` |
| 全文命中率很低 | 先确认 `CONTACT_EMAIL` 配了没有——缺失会让 Unpaywall 那一级被整个跳过；即便配置了，命中率仍取决于该领域期刊的开放获取比例，这是周报末尾「需人工取全文」清单存在的原因，不必期待很高命中率 |
| 某个检索源整周返回 0 篇 | 大概率不是源挂了，是检索式构造有问题。先看 preflight 里这个源是不是也是 0：预检非 0、正式跑 0，多半是候选去重或 `per_cluster_limit` 的问题；预检也是 0，直接去看 `query_for_source()` 里这个源的分支 |
| 邮件被截断 / 附件缺失 | 邮件正文有 Gmail 102KB 阈值；附件按 base64 编码后有总大小上限。完整版本始终能从 `reports/` 里的归档文件拿到，不受邮件截断影响 |
| Action 跑完显示成功，但周报没进仓库 | 检查 push 那一步的日志——`git_push_artefacts` 推送失败会直接抛异常让整个 run 失败，不会静默吞掉；如果 run 本身显示红色，先看这一步 |
| 改了 `CUSTOM_CONFIG` 或 `config/base.yaml` 没生效 | 先确认改的是真正生效的那一份（见第 6 节）；改完跑一次 preflight，不要直接跑整条周报去验证 |

## 12. 本地开发

```bash
# 安装依赖
uv sync

# 跑测试（默认跳过标了 slow 的用例——那些需要下载 sentence-transformers 模型）
uv run pytest

# 连 slow 的一起跑
uv run pytest -m ""

# 跑单个测试
uv run pytest tests/test_utils.py::TestGlobMatch -v

# 带覆盖率
uv run pytest --cov=src/zotero_arxiv_daily --cov-report=term-missing
```

没有配置 linter / formatter。

**加一个新的日报检索源**（比如另一个 arXiv 镜像）：在 `src/zotero_arxiv_daily/retriever/` 下新建文件，继承 `BaseRetriever`，实现 `_retrieve_raw_papers()` 和 `convert_to_paper()`，用 `@register_retriever("你的名字")` 装饰，在 `retriever/__init__.py` 里 import 一下让装饰器执行，最后在 `executor.source` 里加上这个名字。

**加一个新的周报查询源**：同理，但继承 `BaseQueryRetriever`，用 `@register_query_retriever("你的名字")` 装饰，在 `retriever/__init__.py` 里 import。加进 `search.sources` 才会被真正用到，加进去之后会被预检自动纳入探测（第 9 节第 4 项）。

**加一个新的 reranker**：继承 `BaseReranker`，实现相似度计算，用 `@register_reranker("你的名字")` 装饰，在 `reranker/__init__.py` 里 import。`executor.reranker` 这一个键日报和周报共用，选哪个两条管线都受影响。

**给报告加一个新字段**：直接改 `config/base.yaml` 的 `report.fields`，加一项 `key`/`label`/`kind`/`instruction`（`kind: list` 时再加 `max_items`）。不需要碰代码——抽取提示词和三个渲染器（markdown / 网页 HTML / 邮件 HTML）都是从这份列表动态生成的。`instruction` 里必须带一个形如「120–180 字」的字数区间，`tests/test_setup_doc.py` 里有一条守卫测试会在漏写字数区间时失败。

---

## 许可与致谢

本仓库 fork 自 [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)，原始项目按 Zotero 库每日推荐 arXiv 论文。许可证为 GNU Affero General Public License v3（AGPLv3），这份 fork 延用同一许可，完整条款见 [`LICENSE`](./LICENSE)。

日报管线继续依赖上游选用的 [pyzotero](https://github.com/urschrei/pyzotero)、[arxiv](https://github.com/lukasschwab/arxiv.py)、[sentence-transformers](https://github.com/UKPLab/sentence-transformers)。周报管线新增依赖 PubMed E-utilities、Europe PMC、Crossref、OpenAlex 四个检索 API，以及一个 OpenAI 兼容的 LLM / embedding 服务（见第 8 节的 Secrets 清单）。

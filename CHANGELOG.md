# 开发历史

这份文档记录**本仓库相对上游 [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily) 新增的部分**是怎么一步步长出来的：加了什么功能、对应改了哪些脚本文件。上游那部分的完整逐条历史（两百多次提交、多个贡献者的 PR）留在 `git log` 里，这里不重复搬运，只在「起点」一节交代背景。

按时间顺序分成五个阶段，每阶段先说做了什么、为什么，再给一张「功能 → 文件」对照表，方便直接定位到该改哪个脚本。

---

## 0. 起点：fork 自上游日报工具

上游 zotero-arxiv-daily 是一个每日推荐工具：按 Zotero 库的嵌入相似度，从 arXiv/bioRxiv/medRxiv/chemRxiv 当天新论文里挑出最相关的一批，LLM 生成英文 TLDR，发邮件。这部分逻辑原样保留在 `main.py` / `executor.py` / `construct_email.py` / `retriever/{arxiv,biorxiv,medrxiv,chemrxiv}_retriever.py`，日报工作流 `main.yml` 也没动过——完整沿革见 `git log`。

Fork 之后、动手写周报管线之前，先做了两件准备工作：

| 提交 | 做了什么 |
| --- | --- |
| `67f8b9a` Add CLAUDE.md | 给 Claude Code 写项目说明，指导后续所有开发 |
| `64d2c7e` / `10d69f8` | 把测试套件按 1:1 模块映射重写，改用纯 Python stub（不需要 Docker / 真实网络），覆盖率提到 86% |

## 1. 周报管线：从 0 到能跑通

**做了什么。** 从零搭一条独立于日报的周报管线——不再是「今天新发的论文里哪篇最像我的库」，而是「主动去检索式地问四个学术数据库：有没有新文章配得上我关心的这几个主题」。核心链路：Zotero 语料聚类 → 每个主题蒸馏检索式 → 查询 PubMed/Europe PMC/Crossref/OpenAlex → 去重 → 嵌入打分 + 主题归类 → 按配额取稿 → 不足则经典补位 → 抓开放获取全文 → 结构化抽取 → 渲染三种产物 → 多收件人邮件 + 提交归档。

规划先行：`4bf3762`～`55d3eab` 这几个 `docs:` 提交记录了可行性分析、技术选型确认（embedding 模型怎么选、Semantic Scholar 要不要 key、IMAP 摄入路径和 X-MOL 聚合器评估过但没有采用）、TDD 实施计划——这条管线是先写测试、后写实现搭起来的。

功能实现 + 5 轮 code review 修复 + 预检 + embedding 缓存，落在这些文件里：

| 功能 | 主要文件 |
| --- | --- |
| 周数锚定与产物路径命名（如 `2026-08-W3`） | `weeknum.py` |
| `Paper` 扩展出期刊类字段（journal、doi 等） | `protocol.py` |
| DOI 归一化 + 跨源/跨周去重 | `dedup.py` |
| 按主题 sqrt 配额分配名额 | `quota.py` |
| 相似度矩阵与时间衰减权重独立成可复用组件 | `reranker/base.py` |
| LLM 把 Zotero 语料聚成主题簇，候选归簇 | `search/cluster.py` |
| 每个主题蒸馏出三种检索式 | `search/profile.py` |
| 周报检索器基类 + PubMed | `retriever/query_base.py`、`retriever/pubmed_retriever.py` |
| Europe PMC / Crossref 检索器 | `retriever/europepmc_retriever.py`、`retriever/crossref_retriever.py` |
| OpenAlex 检索器 + 经典补位 | `retriever/openalex_retriever.py`、`backfill.py` |
| 开放获取全文阶梯式抓取 | `fulltext/resolver.py` |
| 按配置抽取结构化字段 | `extract.py` |
| 渲染 markdown / 网页 HTML / 邮件 HTML 三种产物 | `report.py` |
| 多收件人（Bcc）投递 + 附件 | `mailer.py` |
| 产物写盘 + commit + push 回仓库 | `publish.py` |
| 整条管线编排（`WeeklyExecutor`） | `weekly.py` |
| 月度综述（独立于周报，可选） | `monthly.py` |
| 预检：跑之前探测每个外部依赖 | `preflight.py` |
| 语料 embedding 跨周缓存 | `reranker/vector_cache.py` |
| 配套测试 | `tests/` 下新增约 40 个测试文件，含 `test_weekly.py`（472 行）、`test_mailer.py`（365 行） |

（`8931e8d..bc27564` 区间：57 个文件、+6188/-26 行）

## 2. 相关性闸门、结构化字段、预检加固

**做了什么。** 第一阶段跑出的周报暴露了一个真实问题——纯靠嵌入相似度排序 + 配额选稿，会把和生物药毫无关系的论文（比如钠离子电池负极材料）选进来，因为它们和语料库有词面上的偶然重合。这一阶段加了一道独立于嵌入相似度的 LLM 相关性判定，两道数值闸门都过了才能进周报；同时把报告字段从一整段无结构文字改成有类型的字段（text/list），并补上单位/期刊匹配加分和预检覆盖。

| 功能 | 主要文件 |
| --- | --- |
| 期刊/企业名单匹配 | `affiliation.py` |
| 从检索源元数据提取作者单位 | `retriever/openalex_retriever.py`、`retriever/crossref_retriever.py` 等 |
| LLM 相关性分诊（0–100 分 + 理由 + 命中的生物药类型） | `triage.py` |
| 相关度 + 期刊/企业加分 → 综合分，双闸门判定 | `scoring.py` |
| 报告字段加类型（text/list），指导抽取与渲染 | `protocol.py`、`extract.py` |
| 字段渲染成有序列表 + 相关度徽标 | `report.py` |
| 候选先过闸门、再分配主题配额 | `quota.py`、`weekly.py` |
| 预检新增：校验手改的 report 配置、探测 embedding 后端 | `preflight.py` |
| 产物写盘/推送逻辑补强（rebase 后失败即报错） | `publish.py` |
| 配套测试 | `test_triage.py`、`test_scoring.py`、`test_preflight.py`（554 行）、`test_affiliation.py` 等 |

（`bc27564..a0f56be` 区间：32 个文件、+2949/-53 行）

## 3. 今天：PDF 命名、侧栏目录、归簇加权

**做了什么。** 三处真实运行后发现的可用性问题，一次会话内完成——细节和真实数据例子见 README 第 5、10 节。

| 功能 | 主要文件 |
| --- | --- |
| PDF 文件名从 DOI 改成「年份-作者-标题-哈希」 | `fulltext/resolver.py` |
| 候选归簇同时看簇的一句话主题描述，不再纯靠语料均值最近邻 | `search/cluster.py`、`weekly.py`、`config/base.yaml`（新增 `cluster_assignment_description_weight`） |
| 网页版加左侧目录 + 全文编号（`#1`、`#2`……同一篇文献复用同一个号） | `report.py` |

（`191c079..1e7eb55` 区间：10 个文件、+663/-25 行，含 4 个测试文件）

## 4. 分诊加一道主题归属判定，堵住归簇误分类的漏洞

**做了什么。** 上一阶段「归簇加权」修复上线后的第一次自然重跑（`reports/2026/2026-08-W3.md`）暴露出它不够用：闸门和描述加权都生效，同一期周报里仍有四篇文献被塞进了明显不对的主题——HCP 簇下混进了抗体工程和细胞培养文献，色谱簇下混进了 Zotero 库里完全没有的疫苗文献，等等（细节见 README 第 6、12 节的更新）。根因是嵌入相似度只能回答「这篇和生物药 CMC 整体像不像」，回答不了「这篇具体属于库里哪个主题」——这是两个不同粒度的问题，调权重解决不了。

修复挪到了分诊阶段：LLM 判定相关度分的同时，现在还会看到库里当前的全部主题名称和一句话描述，对每篇文献单独判断具体属于哪一个、或者都不属于；「都不属于」的文献即使相关度再高也被剔除，判定属于某个主题则覆盖归簇阶段给出的初始归类。`themes` 参数不传时（默认）prompt、输出 schema、行为都和之前完全一致，所有既有测试原样通过。

| 功能 | 主要文件 |
| --- | --- |
| 分诊 prompt 附带主题列表，输出加 `cluster` 字段并按「已知主题名 / 无 / 无法识别」三态解析 | `triage.py` |
| `_gate()` 把 `self._clusters`（run() 里紧跟聚类结果之后存下）转成主题名→描述的字典传给分诊，新旧候选和经典补位共用同一道校验 | `weekly.py` |
| 配套测试：prompt 是否带主题列表、三态解析、覆盖归簇结果、按索引而非顺序匹配、`_gate()` 传给分诊的主题字典是否和配额分配用的一致、端到端剔除与重新归类 | `test_triage.py`、`test_weekly.py` |
| 记录真实验证结果、更新验证边界的诚实标注 | `README.md`（第 6、12 节） |

（`d485e9b..a18bf2e` 区间：6 个文件、+280/-19 行，含 2 个测试文件）

---

## 5. 实测暴露的四个 bug，补位放宽，以及「特定主题」这条新检索线

这一阶段全部由真实周报的实测反馈驱动，`reports/2026/2026-08-W4` 那几版是现场证据。

**先修的四个 bug，都是「看上去在工作、其实没有」的类型：**

1. **标题和链接对不上。** 全文阶梯按 `paper.doi` 去 Unpaywall / Europe PMC 抓 PDF，DOI 在上游就挂错时抓回来的是**另一篇论文**，抽取出的「背景/方法/结论」照着错的全文写，链接也指向那一篇。加了一道标题—全文重合度校验：对不上就丢弃全文并退回「需人工取全文」，若这次抓取是靠 DOI 查到的，连 DOI 一并作废，链接回退到来源自己的 URL。
2. **同一篇文献出现两次。** 去重原来要求「两侧 DOI 相同或至少一侧没有 DOI」才肯按标题合并；而来源给一条正确记录挂错 DOI 时，两条记录 DOI 不同、标题逐字相同，于是各留一份。改成标题精确匹配即合并。
3. **Crossref 整轮 0 篇。** `select` 参数里带了 `affiliation`——它不是 Crossref 顶层字段（真正的位置是 `author[].affiliation`），Crossref 因此拒绝**每一个**请求（400），这个源从 2026-08-22 起就一直在无声地贡献 0 篇。删掉该字段后单轮回到 59 篇。
4. **邮件发出去了、归档全丢。** 推送被拒时的 rebase 兜底在这个工作流里从未生效过：workflow 先把 `CUSTOM_CONFIG` 写进受版控的 `config/custom.yaml`，工作区全程是脏的，`git rebase` 直接拒绝执行。改用 `--autostash`。

**经典补位放宽 + 多轮重试。** 实测日志显示补位候选 13/13 全部因为「不属于当周任何一个具体主题」被剔除，因相关度或综合分不达标的是 0 篇——高被引经典本来就不是按当周语料聚出的窄主题写的。补位改为不要求主题归属（两道数值闸照常），并在一轮补不满时让 LLM 换一批用词重新检索，最多 3 轮。改完后补位从 0/10 变成 14/14。

**新增「特定主题」检索线。** `FOCUS_TOPIC` 填一个你自己关心的方向，AI 消化成检索式、检索、判定、单列一节汇总，留空则整条线不启用。这条线绕了三次运行才走通，坑都记在 README 第 6 节：检索式最初把对象名和通用切面词 OR 在一起，导致药名可有可无、三次运行全部跑偏；现在拆成 `subject_terms AND aspect_terms`，对象是必要条件。相关度也换了一套细则——问的是「与你给的主题相关吗」，且主题点名了对象时，研究别的对象的文献即使方法邻近也不放行。

| 功能 | 主要文件 |
| --- | --- |
| 全文与标题重合度校验，不匹配则丢弃全文并作废可疑 DOI | `fulltext/resolver.py` |
| 标题精确匹配即合并，不再要求 DOI 兼容 | `dedup.py` |
| Crossref `select` 去掉无效的 `affiliation` 字段 | `retriever/crossref_retriever.py` |
| 推送冲突时 `rebase --autostash`，脏工作区不再阻断兜底 | `publish.py` |
| 补位放宽主题归属（`require_theme_fit`）、多轮换检索式、三段计数日志 | `triage.py`、`backfill.py`、`search/profile.py`、`weekly.py` |
| 特定主题：配置读取、主题消化、对象组 AND 切面组、不限日期检索、结果结构 | `search/focus.py`（新增） |
| 面向用户主题的独立分诊细则 | `triage.py` |
| 「特定主题」分区的三份渲染 + 目录 + 编号 + 计入总数 | `report.py` |
| `search.focus.*` 五个配置键、`FOCUS_TOPIC`/`FOCUS_BACKGROUND` 工作流注入，以及「可选变量也必须被工作流导出」的守卫测试 | `config/base.yaml`、`.github/workflows/weekly.yml`、`test_setup_doc.py` |
| 设计定稿与逐任务实施计划（含三次实测的修订记录） | `docs/superpowers/specs/`、`docs/superpowers/plans/` |

---

## 参考

- 完整逐条提交历史：`git log`（当前 201 次提交，含上游继承的部分）
- 每次架构/需求决策的详细讨论：[`docs/cmc-literature-weekly-plan.md`](docs/cmc-literature-weekly-plan.md)
- 部署与首跑实测记录：[`docs/cmc-weekly-setup.md`](docs/cmc-weekly-setup.md)
- 项目背景、功能全貌、项目结构、各配置项的作用与调参方法：[`README.md`](README.md)

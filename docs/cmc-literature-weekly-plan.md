# Zotero 驱动的 CMC 文献周报流水线 — 可行性分析与实现路线

> 面向生物制药 CMC 分析（理化分析 / 结构表征 / 活性分析）的文献自动追踪系统。
> 目标：每周五 20:00（北京时间）自动产出结构化周报，入库 GitHub，并群发组员。
>
> 在线版：https://claude.ai/code/artifact/f45d7d63-9e9d-4213-9d02-74e40ad543b8

---

## 1. 结论

**可行。以 Zotero-arXiv-Daily 为主干扩展，literature-search skill 的方法论固化成代码，不需要引入第三个框架。**

现有仓库已经把最难的两块做完了：**Zotero 语料获取**和**基于语料的加权相似度重排**。需要补的是「查询式检索」这一类新检索器、OA 全文获取阶梯、结构化抽取、周报渲染与入库、多人邮件。插件式的 `register_retriever` 体系意味着这些都是**加法而非改写**。代码复用率约 70%。

三处需要调整原设想：

1. **Google Drive 那一层可以整个去掉**（见发现 1）
2. **上图代理自动下载不建议做**，但「读 DOI 页面信息」不该是保底，应该是默认策略（见发现 4）
3. **Google Scholar 和知网进不了自动化**，有等效替代（见发现 5）

---

## 2. 九个关键发现

### 发现 1 — Google Drive 这一层可以完全去掉 【大幅简化】

Zotero 免费账户的 300MB 限制**只针对附件文件存储**；题录元数据（标题、摘要、分类、标签、笔记）的同步是**无限量且免费**的。而整条推荐流水线只用到 `title` / `abstractNote` / `dateAdded` / `collections` —— 全是元数据。

只要 Zotero 客户端保持「同步数据开启 + 文件同步关闭」（这正是用 Drive 存 PDF 的人的标准配置），`pyzotero` 就能拿到完整语料。**不需要 Drive OAuth、不需要 service account、不需要解析 zotero.sqlite。** 现有的 `executor.py:fetch_zotero_corpus` 一行都不用改。

兜底：万一整个同步都关了又不想开，可以从 Drive 读 `zotero.sqlite`，或定期导出 CSV/BibTeX 进仓库。但这是明显更脆弱的路径，不推荐。

### 发现 2 — 现有仓库是「当日新品推送」模型，不是「按主题检索」模型 【必须新增】

`BaseRetriever._retrieve_raw_papers()` 的语义是「把今天该源新发布的全部抓下来」，再靠 reranker 挑。这对 arXiv / bioRxiv 成立 —— 它们有分类、有每日 feed、量级几十到几百篇。

但对期刊文献**不成立**：没有「我领域今天的全部 PubMed」这种小体量 feed。而 CMC 分析的主战场恰恰是期刊（*Anal. Chem.*、*J. Pharm. Sci.*、*mAbs*、*JPBA*、*Anal. Biochem.*、*Electrophoresis*），不是预印本。

所以要补一类**查询式检索器**：带日期窗口按检索式查。好消息是 `BaseRetriever` 的契约（`_retrieve_raw_papers` + `convert_to_paper`）完全够用，只是前者内部换成按 profile 查询 —— 纯粹的插件加法。

### 发现 3 — 检索式应当从 Zotero 语料自动派生 【两个项目的缝合点】

Zotero 分类（表征 / 活性 / 理化）本身就是主题标签。做法：按 collection path 分组语料，每组取 30–50 篇标题+摘要，让 DeepSeek 蒸馏出一组检索式 —— MeSH 主题词 + 自由词 + 布尔短语式，正好对应 literature-search skill 的 Wave 1 / Wave 2 分波检索思路。

结果缓存进 `config/query_profiles.yaml`，**每月刷新一次而不是每周重算**：省 token，也让周与周之间的召回口径稳定可比。检索回来的候选最后仍然过现有 reranker 对全语料打分 —— **查询式检索保召回，embedding 重排保精度**。这是整套设计里最关键的一环。

### 发现 4 — 上图代理自动下载：不建议做 【不建议】

三个彼此独立的原因，任何一个单独成立都足以否掉：

- **许可层面。** ScienceDirect 的使用条款明文禁止用 robot、crawler、脚本批量下载，只允许通过其 TDM API 做文本挖掘。触发风控后封禁落在**机构账号**上，会连累全馆读者，本人的借书证也会被停。
- **技术层面。** GitHub 托管 runner 是境外机房 IP，上图代理有地域与 IP 风控，加上 OAuth 会话流转和可能的验证码，成功率很低 —— 大概率还没碰到许可问题就先失败了。
- **版权层面。** 把出版商 PDF 存进公司相关的 GitHub 仓库并群发组员，超出了个人研究使用的范围，对企业是实打实的版权敞口。

**能拿到大部分价值的替代方案：** OA 优先阶梯（见发现 6）覆盖能自动拿的部分；拿不到全文的用「结构化摘要 + Crossref 元数据 + PubMed MeSH」，这已经足够 AI 产出**筛选级**的背景/待解决问题/方法/结论，报告里明确标注 `全文未获取（仅摘要）`。

真正需要精读的那 3–5 篇，报告里直接给**上图代理深链**，人工点开手动下。**自动化做分诊，人做五次下载** —— 这不是妥协，这是正确的分工。

另外可以留一个 `--mode local`：同一套代码在自己电脑上跑，复用浏览器已登录的会话做个人用途取全文，GitHub Actions 那条线保持干净。

### 发现 5 — Google Scholar 与知网进不了自动化，但有等效替代 【调整源清单】

**Google Scholar** 无公开 API，数据中心 IP 几次请求就吃 CAPTCHA，靠代理池维持既不稳也不合规。替代：**OpenAlex**（免注册免 key，10 万次/天，带引用数和概念标签）+ **Semantic Scholar API**（免费，有 key 档）。两者合起来覆盖 Scholar 的大部分能力，而且是结构化的。

**知网** 无公开 API、强反爬、许可禁止抓取。建议中文文献仍走知网自带订阅人工看，并在周报里**明确标注这是已知盲区** —— 诚实标注盲区比假装覆盖了更有价值。好在 CMC 分析的方法学前沿基本在英文期刊。

### 发现 6 — OA 全文阶梯是自动取全文的正确形态 【直接可用】

按顺序尝试，命中即停：

1. **Unpaywall** — `GET api.unpaywall.org/v2/{DOI}?email=…`，读 `best_oa_location.url_for_pdf`。免费，10 万次/天。
2. **Europe PMC** — 免注册免 key，600 万+ 篇 OA 全文 XML，是这条链里性价比最高的一环。
3. **PMC** / **bioRxiv · medRxiv · chemRxiv**（后三个仓库里已经接好了）
4. **出版商 OA** — MDPI、Frontiers、Springer OA、ACS AuthorChoice

命中就走现成的 `utils.extract_markdown_from_pdf`（pymupdf4llm + pymupdf-layout，仓库里已装好）转 markdown。没命中就降级到摘要模式，并在报告里标出来。

### 发现 7 — MinerU 不进 Actions，用 pymupdf4llm 【工具选型】

仓库的 `pyproject.toml` 里已经有 `pymupdf4llm` + `pymupdf-layout`，`utils.extract_markdown_from_pdf` 直接可用，冷启动几秒。

MinerU 解析质量更好（尤其表格），但模型体积大、需要显著的下载和计算时间，塞进每周的 Actions 预算不划算。**建议：默认 pymupdf4llm；** 如果发现 CMC 文献里的方法学表格（色谱条件、质谱参数）提取质量确实不够，再对那少数几篇走 MinerU 托管 API，或放到本地模式里跑。

### 发现 8 — 邮件通道要改三处，仓库私有化有连带影响 【需改造】

现有 `utils.py:send_email` 是单收件人、纯 HTML、无附件、主题写死 `Daily arXiv`。要改：

- 多收件人：`sendmail(sender, [列表])`，组员邮箱建议放 **Bcc** 互相隐藏
- 改 `MIMEMultipart` 挂 PDF 附件
- **大小护栏**：多数 SMTP 上限 20–25MB，超了就只发链接不发附件

**连带约束：** 存 PDF 就必须把仓库设为私有。私有仓 Actions 有 2000 分钟/月免费额度，周跑一次约 10–20 分钟即约 80 分钟/月，够用。**但私有仓里的 PDF 链接，组员必须是仓库 collaborator 才打得开** —— 要么把组员加为协作者，要么邮件直接发附件、报告里只放 DOI 链接。这个取舍要先定。

### 发现 9 — 周命名与定时的两个坑 【实现细节】

**周命名。** 「2026年08月W1」是月内周序，跨月那周归属有歧义。建议把规则写死为「**该周周五所在月份 + 该月第几个周五**」，文件名 `reports/2026/2026-08-W3.md`，并在报告头部写明确区间 `覆盖期：2026-08-15 ~ 2026-08-21`。歧义消灭在文件头里。

**定时。** GitHub cron 是 UTC：周五 20:00 北京时间 = 周五 12:00 UTC → `0 12 * * 5`。另外定时任务在高峰期会延迟几分钟到一小时（对周报无所谓），且仓库 60 天无活动会被自动停用 —— 仓库里已有的 `keep-alive.yml` 正是处理这个的，**保留它**。

---

## 3. 三条路线对比

| 决策维度 | **A · 扩展 Zotero-arXiv-Daily** | B · Agent 驱动 | C · 现成工具拼装 |
| --- | --- | --- | --- |
| 形态 | 在现有插件体系上加检索器与流水线阶段 | 在 Actions 里跑 agent，自主检索并撰写 | n8n / Dify / RSS 聚合器编排 |
| Zotero 语料相似度排序 | **原生具备**，0 改动 | 需自行实现 | 基本做不到——这是核心需求 |
| 代码复用率 | **约 70%** | 约 20%（仅配置与邮件） | 0 |
| 结果确定性 | 高，同输入同输出，可回归测试 | 低，每次检索路径都不同 | 高，但能力上限低 |
| 周成本 | 几毛钱（仅抽取阶段调 LLM） | 数元至数十元，随篇数放大 | 托管费 + 无 LLM 成本优势 |
| 综述与洞见质量 | 好（单篇结构化抽取） | **更好**（跨篇归纳、主题演化） | 差（基本是转发） |
| 入库 GitHub / 可版本化 | 天然满足 | 满足 | 需额外打通 |
| 调试与排障 | 日志清晰，pytest 可覆盖 | 难，失败原因常不可复现 | 可视化但黑盒 |
| **结论** | **作主干** | 作**月度综述增强层**，不做周报主干 | 不推荐 |

**最优路线：A 为主干 + B 作可选月度层。**

周报要的是**稳定、便宜、可比** —— 同样的检索口径每周跑，才能看出趋势，A 完全胜任。而「本月这个方向整体在往哪走」这类跨篇归纳恰恰是 agent 的强项，适合每月一次单独跑，输入就是当月四份周报。两者不冲突，也不必同时上：**B 是 P4，不是 P0。**

---

## 4. 推荐架构

八个阶段，每周五 12:00 UTC 触发。

```
① 拉取并过滤 Zotero 语料                                     [现有代码 0 改动]
   pyzotero 拉全库元数据 → include_path glob 选中 理化/表征/活性
   fetch_zotero_corpus + filter_corpus
        ↓
② 蒸馏检索式（每月一次，带缓存）                                    [新增]
   按 collection 分组 → DeepSeek 提炼 MeSH 词 + 自由词 + 布尔式
   → config/query_profiles.yaml
        ↓
③ 多源检索（日期窗口 = 上周五 ~ 本周五）                      [4 个新检索器]
   新增：PubMed E-utilities · Europe PMC · Crossref · OpenAlex/S2
   复用：bioRxiv · medRxiv · chemRxiv · arXiv
        ↓
④ DOI 归一去重                                                    [新增]
   同一篇会同时出现在多个源；标题相似度兜底无 DOI 的预印本
   候选量级约 200–600 篇
        ↓
⑤ 对全语料加权余弦重排                                    [现有代码 0 改动]
   BaseReranker.rerank —— 较新入库的 Zotero 文献权重更高
   取 Top-N（建议 15–25）
        ↓
⑥ 全文获取阶梯                                                    [新增]
   Unpaywall → Europe PMC OA → 预印本 → 出版商 OA，命中即停
   命中 → extract_markdown_from_pdf 转 md，PDF 落 library/
   未命中 → 降级摘要模式并打标
        ↓
⑦ LLM 结构化抽取                                                  [新增]
   字段由 report.fields 配置驱动：
   标题 / DOI / 摘要 / 背景 / 待解决问题 / 方法 / 结论 / 洞见
   增删字段只改 YAML，不动代码 —— 这就是「输出内容可定制化」
        ↓
⑧ 渲染 · 入库 · 群发                                        [新增 + 改造]
   渲染 reports/2026/2026-08-W3.md，按 理化/表征/活性 分三段
   git commit & push 入库
   SMTP 多收件人发 HTML 正文 + PDF 附件（带大小护栏）
```

---

## 5. 代码复用清单

| 组件 | 位置 | 处置 |
| --- | --- | --- |
| Hydra 配置组合 | `config/{base,default,custom}.yaml` | 复用，增加 `search:` `fulltext:` `report:` `git:` 四段 |
| Zotero 语料拉取 | `executor.py:fetch_zotero_corpus` | **0 改动** |
| 分类路径过滤 | `executor.py:filter_corpus` · `utils.glob_match` | **0 改动** |
| 加权相似度重排 | `reranker/base.py:rerank` | **0 改动** |
| 向量化后端 | `reranker/{local,api}.py` | 0 改动。建议用 `api` 省 runner 时间（见确认项 5） |
| 检索器插件机制 | `retriever/base.py` | 0 改动，作为新检索器基类 |
| 预印本检索器 ×4 | `retriever/*_retriever.py` | **0 改动**，直接纳入源清单 |
| PDF → markdown | `utils.py:extract_markdown_from_pdf` | **0 改动** |
| 子进程硬超时护栏 | `arxiv_retriever.py:_run_with_hard_timeout` | 上提到 `utils.py` 供全文获取阶梯复用 |
| LLM 调用范式 | `protocol.py:generate_tldr` | 作为结构化抽取的模板（含 tiktoken 截断、异常降级） |
| Paper 数据类 | `protocol.py:Paper` | **扩展字段**：doi / journal / pub_date / pdf_path / oa_status / extraction |
| 流水线编排 | `executor.py:Executor.run` | **扩展**：插入阶段 ④⑥⑦⑧ |
| 邮件发送 | `utils.py:send_email` | **改造**：多收件人 + Bcc + 附件 + 大小护栏 |
| 邮件 HTML | `construct_email.py` | 改造：补 DOI / 期刊 / 结构化字段 / 仓库链接 |
| 定时 workflow | `.github/workflows/` | 新增 `weekly.yml`，`0 12 * * 5` + `permissions: contents: write` |
| 防停用 | `.github/workflows/keep-alive.yml` | **保留**（防 60 天无活动被停） |
| 测试范式 | `tests/` 纯 Python stub | 沿用，新检索器照 `tests/retriever/` 的写法加 |

**需要新增的模块：**

- `search/profile.py` — 检索式蒸馏
- `retriever/{pubmed,europepmc,crossref,openalex}_retriever.py`
- `dedup.py` — DOI 归一去重
- `fulltext/resolver.py` — OA 全文阶梯
- `extract.py` — 结构化抽取
- `report.py` — 周报渲染与周命名
- `publish.py` — 提交入库

---

## 6. 可行性矩阵

「可行性」指在 GitHub Actions 无人值守环境下稳定跑通的把握。

| 能力 | 可行性 | 主要障碍 | 结论 |
| --- | --- | --- | --- |
| Zotero 语料（云 API） | **高** | 需确认数据同步已开启 | 采用 |
| Zotero 语料（Drive sqlite） | 中 | OAuth + 解析 + 易随版本失效 | 仅作兜底 |
| PubMed 检索 | **高** | 建议申请 API key 提速至 10 req/s | 采用 |
| Europe PMC 检索 + OA 全文 | **高** | 无（免注册免 key） | 采用，优先级最高 |
| Crossref 检索 | **高** | 带 mailto 进 polite pool | 采用 |
| OpenAlex / Semantic Scholar | **高** | OpenAlex 免 key 10万/天；S2 需 key | 采用 |
| bioRxiv / medRxiv / chemRxiv / arXiv | **高** | 已实现并有测试 | 采用 |
| OA 全文 PDF（Unpaywall 阶梯） | **高** | 覆盖率非 100%，需降级路径 | 采用 |
| Google Scholar | 低 | 无 API，数据中心 IP 必遭 CAPTCHA | **放弃**，用 OpenAlex + S2 替代 |
| 知网 CNKI | 低 | 无 API + 强反爬 + 许可禁止 | **放弃**，周报中标注盲区 |
| 上图代理自动下载 | 低 | 许可禁止 + 机构账号封禁风险 + 境外 IP 风控 + 再分发版权敞口 | **不建议**；改为报告内深链人工取，可选本地模式 |
| 出版商 TDM API | 中 | 需机构授权，通常绑 IP | 有正规授权时可做 |
| MinerU 解析 | 中 | 模型体积超 Actions 时间预算 | 默认 pymupdf4llm；MinerU 走托管 API 或本地 |
| 周报入库 GitHub | **高** | 需 `contents: write` | 采用 |
| PDF 入库 GitHub | 中 | 须私有仓；组员需协作者权限；仅限 OA | 有条件采用 |
| 多人邮件 + 附件 | **高** | SMTP 20–25MB 上限 | 采用 + 大小护栏 |
| 周五 20:00 定时 | **高** | cron 为 UTC；高峰延迟；60 天停用 | `0 12 * * 5` + keep-alive |

---

## 7. 分阶段落地

每一阶段都能独立交付可用的东西，后一阶段依赖前一阶段。

### P0 — 不下载 PDF 的完整闭环（约 1 周，解决 80% 痛点）

- 接通 Zotero 云端语料，确认 `include_path` 选中三个分类树
- 实现检索式蒸馏 + 四个查询式检索器 + DOI 去重
- 复用现有 reranker 排序，取 Top-N
- LLM 仅基于摘要做结构化抽取
- 渲染周报 markdown，提交入库，发单人邮件自测

### P1 — 全文获取（约 3–4 天）

- OA 阶梯：Unpaywall → Europe PMC → 预印本 → 出版商 OA
- PDF 落 `library/2026/2026-08-W3/`，报告内链仓库路径
- 命中全文的走全文抽取，未命中标注「仅摘要」
- 报告末尾附「需人工取全文」清单 + 上图代理深链

### P2 — 群发（约 1–2 天）

- 多收件人 + Bcc 隐藏组员邮箱
- MIMEMultipart 挂 PDF 附件 + 20MB 护栏
- 主题模板化：`CMC 文献周报 2026-08-W3（共 18 篇）`

### P3 — 可定制化与分组（约 2–3 天）

- 抽取字段由 `report.fields` 驱动，增删字段只改 YAML
- 周报按理化 / 表征 / 活性分三段，各段独立排序
- 加「本周值得优先读的三篇」置顶区

### P4 — 月度综述层（可选，按需）

- 每月跑一次 agent，输入当月四份周报，产出跨篇归纳与主题演化
- 这一层才用得上 literature-search skill 的完整验证式流程

---

## 8. 需要确认的事项

前两项直接决定 P0 能不能开工。

1. **Zotero 客户端的「同步数据」开着吗？** 设置 → 同步。文件同步可以关（省 300MB 配额），但数据同步必须开，否则云端没有元数据可拉。这一项决定发现 1 是否成立。
2. **理化 / 表征 / 活性三个分类在 Zotero 里的完整路径是什么？** 需要形如 `文献/表征/糖基化` 的完整层级名，用来写 `include_path` 的 glob 模式。
3. **仓库设为私有，还是组员只收邮件附件？** 存 PDF 必须私有；私有仓的 PDF 链接要求组员是 collaborator。若不想加协作者，报告里就只放 DOI 链接、PDF 走邮件附件。
4. **组员邮箱有几个？需要互相隐藏吗？** 影响用 To 还是 Bcc。超过五六个建议一律 Bcc。
5. **向量化用哪家？** DeepSeek 官方 API 以对话补全为主，embedding 接口长期缺位（官方仓库至今挂着相关 open issue），请以 api.deepseek.com 当前文档为准。若确无，两个现成选项：仓库自带的 `local` reranker（sentence-transformers，runner 上多花一两分钟下模型），或硅基流动 / 智谱的 embedding API 走现成的 `api` reranker。生成仍用 DeepSeek，两者互不影响。
6. **每周期望多少篇？** 建议 15–25。低于 15 容易漏，高于 25 就没人读得完，也拉高 LLM 成本。
7. **周命名规则按「周五所在月份 + 该月第几个周五」可以吗？** 见发现 9。定下来之后文件名和报告头的日期区间就唯一确定了。

---

## 附：核查说明

本文结论基于对 `Bryce505/zotero-arxiv-daily` 全部源码的通读、`literature-search` skill 的实现，以及对各文献源 API 现状的核查。

涉及第三方服务配额与条款的部分（Zotero 存储策略、Unpaywall 与 Europe PMC 接口、Elsevier 文本挖掘政策、GitHub Actions 额度）均以公开文档为准，请在实施前复核当前版本 —— 这类政策变动不通知。上图代理与知网部分的判定是基于其服务条款与技术风控现状，**未做实测**。

参考来源：

- [Zotero 存储与同步（Rice University LibGuides）](https://libguides.rice.edu/c.php?g=1001613&p=7252828)
- [Unpaywall API 与 OpenAlex Locations](https://help.openalex.org/data/locations/)
- [Europe PMC RESTful Web Service](https://europepmc.org/RestfulWebService)
- [Elsevier 文本与数据挖掘政策](https://www.elsevier.com/about/policies-and-standards/text-and-data-mining)
- [Elsevier TDM 常见问题](https://www.elsevier.com/about/policies-and-standards/text-and-data-mining/faq)

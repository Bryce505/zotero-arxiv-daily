# CMC 文献周报 — 部署与首跑

给已经跑通日报流程的人。周报是**另一条独立管线**（`weekly.yml`），与 `main.yml` 互不干扰，可以并存。

---

## 1. 直接粘贴进 `CUSTOM_CONFIG`

仓库 → Settings → Secrets and variables → Actions → **Variables** 标签 → `CUSTOM_CONFIG`。

```yaml
zotero:
  user_id: ${oc.env:ZOTERO_ID}
  api_key: ${oc.env:ZOTERO_KEY}
  include_path: ["文献", "文献/**"]

email:
  sender: ${oc.env:SENDER}
  receiver: ${oc.env:SENDER}
  smtp_server: smtp.gmail.com
  smtp_port: 465
  sender_password: ${oc.env:SENDER_PASSWORD}

llm:
  api:
    key: ${oc.env:OPENAI_API_KEY}
    base_url: ${oc.env:OPENAI_API_BASE}
  generation_kwargs:
    model: deepseek-v4-flash
  language: 中文

reranker:
  vector_cache: state/corpus_vectors.npz
  api:
    key: ${oc.env:EMBEDDING_API_KEY}
    base_url: https://api.siliconflow.cn/v1
    model: BAAI/bge-m3
    batch_size: 32

executor:
  reranker: api
  debug: ${oc.env:DEBUG,null}
  source: ['biorxiv']
  max_paper_num: 15
  send_empty: true

source:
  biorxiv:
    category: ["biochemistry", "bioengineering", "molecular biology"]
```

### 每一段为什么这么写

| 配置 | 原因 |
| --- | --- |
| `include_path: ["文献", "文献/**"]` | 两条缺一不可。`文献/**` 不匹配「文献」这个根分类本身。冒烟测试实测覆盖 111/112 篇 |
| `receiver: ${oc.env:SENDER}` | 原来指向 `RECEIVER`，而周报 workflow 不导出这个 secret。指向 `SENDER` 让日报流程照常，周报走 `RECIPIENTS` |
| `smtp_port: 465` | **别改成 587。** STARTTLS 失败后的 SSL 回退用的是**同一个端口**，465 走「STARTTLS 失败 → SSL 回退」这条已在生产验证的路；587 一旦 STARTTLS 出问题，回退到 587 的 SSL 是死路 |
| **`language: 中文`** | **最容易踩的坑。** 默认是 `English`，不写这行，你的 背景/待解决的问题/方法/结论/洞见 会全部输出英文 |
| **`executor.reranker: api`** | **首跑最大的教训。** 本地 reranker 在 GitHub 免费 runner 上耗时极不稳定：同样的语料，一次 69 秒，另一次 31 分钟（run 32494449956 对 32495539309），差 27 倍。这是共享 runner 的 CPU 争用，改不了 |
| `model: BAAI/bge-m3` | 多语言模型。你库里有中文文献，原来的 `jina-...-nano` 对中文的表现从未验证过 |
| `vector_cache` | 缓存语料向量，跨周复用。走 API 后省的是调用次数而非 CPU 时间 |
| `executor.source` / `source.biorxiv` | 只给日报流程 `main.yml` 用。周报走 `search.sources`，不读这两项 |

> `search`、`fulltext`、`report`、`git` 四段**不用写**——`base.yaml` 里已有可用默认值，且 `NCBI_API_KEY`、`CONTACT_EMAIL`、`RECIPIENTS` 会自动注入。

**`report` 段尤其不要往这里搬。** `report.journals.allow`（63 本期刊）、`report.industry.names`（52 家企业）、`report.fields`（5 个报告字段）这三份长内容只应该改 `config/base.yaml`，不要写进 `CUSTOM_CONFIG`。原因是 OmegaConf 合并两层配置时，字典逐键合并，但**列表是整体替换**：`CUSTOM_CONFIG` 里哪怕只写 `report.journals.allow: ["Nature"]` 这一行，也会把 `base.yaml` 里那 63 本期刊全部顶替掉，`bonus` 之类的标量倒是会保留，而且不会有任何报错提示——下一周的周报会用一份只剩一本刊的名单默默跑完。`min_relevance`/`min_score` 这类单个数值两处都能改，没有这个问题。完整的实测演示和两条修改路径的操作步骤见 [`README.md`](../README.md) 第 6 节「配置在哪里、怎么改」。

---

## 2. Secrets 清单

Settings → Secrets and variables → Actions → **Secrets**：

| 名称 | 你已有 | 说明 |
| --- | --- | --- |
| `ZOTERO_ID` | ✅ | |
| `ZOTERO_KEY` | ✅ | |
| `OPENAI_API_KEY` | ✅ | 指向 DeepSeek。名字是协议名，不是厂商名 |
| `OPENAI_API_BASE` | ✅ | |
| `NCBI_API_KEY` | ✅ | PubMed 限速 3→10 req/s |
| `CONTACT_EMAIL` | ✅ | 一个值喂四处：PubMed、Crossref 与 OpenAlex 的 polite pool、**以及 Unpaywall**。首跑时这条是空的，Unpaywall 整级被跳过，全文命中率因此只有 1/25 |
| `FOCUS_TOPIC` | 可选 | 想在周报里单独追一个方向时填，例如「连续制造在单抗原液生产中的应用」。**不填就是不启用**——不会多花一次 LLM 调用，也不会多出一节 |
| `FOCUS_BACKGROUND` | 可选 | 给上面那个主题补一句背景（你在做什么、关心哪一面），模型据此生成更贴切的检索式 |
| `SENDER` | ✅ | |
| `SENDER_PASSWORD` | ✅ | Gmail 应用专用密码 |
| `RECIPIENTS` | ✅ | 组员邮箱，逗号分隔。全部走 Bcc 互相隐藏 |
| **`EMBEDDING_API_KEY`** | ❌ **需新建** | 硅基流动等 embedding 服务的 key（https://cloud.siliconflow.cn/ ）。四个 workflow 都已导出它；**配置里引用了它却不建这个 secret，run 会在配置组装阶段直接崩** |

`RECIPIENTS` 填法（一行，逗号或分号分隔都行）。域名不限，QQ、公司邮箱、Gmail 混填都可以——收件地址与发信认证无关：

```
zhang@qq.com, li@yourcompany.com, wang@outlook.com
```

> 企业邮件网关（Proofpoint、Mimecast、Defender）常默认剥离 `.html` 附件。首跑实测两个收件人都完整收到了正文与附件，但换新域名时值得先单发一个地址验证。

---

## 3. 先跑预检（约 1 分钟）

**别直接跑周报。** 周报要先做完 Zotero、聚类、四源检索、全文抓取、抽取，二十多分钟之后才碰 SMTP——一个密码错了，你要等到最后一步才知道。

预检把每个边界都便宜地探一遍：**不发邮件、不写文件、不提交**。

1. Actions → **CMC weekly preflight** → **Run workflow**

（三个新 workflow 实测都是 `active` 状态，不需要手动启用。若你的环境显示被禁用，先点 Enable。）

**实测输出**（2026-08-22，run 32548936193，耗时 51 秒；`report-config` 检查是相关性闸门改造之后才加的第 7 项，这次实测跑的时候还没有，下面按当前 `config/base.yaml` 的真实内容把它补在第一行，其余 9 行是原始实测结果）：

```
Preflight
────────────────────────────────────────────────────────────
[ OK ] report-config 63 journals, 52 companies, 5 fields (2 text / 3 list)
[ OK ] zotero       111 of 112 papers matched include_path
[ OK ] llm          deepseek-v4-flash answered (中文)
[ OK ] pubmed       2 probe results
[ OK ] europepmc    2 probe results
[ OK ] crossref     2 probe results
[ OK ] openalex     2 probe results
[ OK ] embedding    BAAI/bge-m3 returned a 1024-dim vector
[ OK ] recipients   2 recipients, all Bcc
[ OK ] smtp         smtp.gmail.com accepted the login
────────────────────────────────────────────────────────────
PASS — every check succeeded
```

这次实测确认了四个查询式检索器在**真实 API** 上都能正确解析响应。整个探测耗时 26 秒。

> **`embedding` 一行证明的是你的 embedding key 真的能调通**，不只是配置解析成功。一个写错的 `EMBEDDING_API_KEY` 插值一样成功、配置一样组装得起来，其余每一项都会报绿，然后在周报跑到重排那步才崩——那已经是十分钟之后，聚类和检索式蒸馏的 LLM 调用都花掉了。

> **预检探测用的检索式，与周报实际下发的是同一套。** 首跑时不是这样——预检用两个词的短查询，周报下发的是十几个词的长句，结果 Europe PMC 与 OpenAlex 在预检全绿的情况下于正式跑中返回 0 篇。现在探测走 `query_for_source()` 同一条路径，这个盲区已封死。

有 `FAIL` 就不要跑周报，先按提示修。`WARN` 不阻塞运行，但值得看一眼——比如上面那条 `llm` 警告，意味着你的周报会全出英文。

**改了任何 secret 或 `CUSTOM_CONFIG` 之后，重跑一次预检**，比跑一次完整周报便宜得多。

---

## 4. 跑周报

预检全绿之后：

1. Actions → **CMC literature weekly digest** → **Run workflow** 手动触发一次

首跑会比之后每周都慢，因为三个缓存都是冷的：主题聚类、检索式蒸馏、语料向量。第二周起这三项都会命中缓存。

### 首跑该看什么

日志里这几行是关键信号：

```
Built N theme clusters and cached them to .../state/theme_clusters.json
Distilled N query profiles ...
pubmed/<簇名>: N candidates          ← 每个源 × 每个簇一行
N candidates after de-duplication (X library DOIs and Y previously delivered DOIs excluded)
Full text resolved for N/M papers    ← OA 命中率
Digest sent to N recipients with M attachments
```

**每个源都该有产出。** 首跑时 Europe PMC 与 OpenAlex 在全部五个簇上都返回 0 篇，而 Crossref 返回 65 篇——同一条检索式。原因是这两个源把查询里的词**隐式 AND** 起来，要求一篇文献同时命中十几个词；Crossref 的 `query.bibliographic` 是相关性排序，永远返回最佳匹配。现在 `query_for_source()` 按源下发不同形式：PubMed 拿布尔式，Crossref 拿自然语言句，Europe PMC 与 OpenAlex 拿 `free_terms` 的 OR 拼接。**如果某个源又整列为 0，先怀疑检索式形式，不是源挂了。**

**最该核对的是聚类结果。** 打开 `state/theme_clusters.json`，看那几个簇名是否符合你对自己文献库的直觉。这一步决定了后面所有检索式的方向——聚错了，整周的检索都会偏。不满意就删掉这个文件重跑，或者手工改簇名与描述（`member_titles` 按标题匹配，改名不影响归属）。

---

## 5. 首跑实测与已知限制

**完整管线已端到端跑通**（run 32495539309，2026-08-21）：25 篇送达 2 个收件人、2 个附件，归档提交 `5479ffc` 并推回 main。实测耗时 **53.7 分钟**，分段如下：

| 阶段 | 实测 |
| --- | --- |
| Zotero 取数 + 过滤 | 6 秒（112 篇，111 篇匹配） |
| 主题聚类（LLM） | 2 分钟 |
| 检索式蒸馏（LLM） | 3.5 分钟 |
| 四源检索 | 26 秒 |
| **语料 embedding（本地）** | **31 分钟** |
| **候选 embedding（本地）** | **11.8 分钟** |
| 全文抓取 25 篇 | 1 分钟 |
| 逐篇结构化抽取 | 3.3 分钟（≈7 秒/篇） |

**本地 reranker 的耗时不可预测，这是换 embedding API 的真正理由。** 同样的语料，被取消的那次跑（run 32494449956）只用了 **69 秒**，这次用了 **31 分钟**——差 27 倍。原因是 GitHub 免费 runner 的 CPU 争用，不是代码问题，也无法通过优化代码解决。§1 的配置已默认改用 API。

**主题聚类质量良好。** 五个簇分别是 HCP 分析(19)、蛋白质结构与质谱表征(20)、电荷异质性与电泳分离(25)、色谱电泳纯度与含量(23)、免疫分析与酶学检测(24)，分布均衡且直接对应 CMC 三大块业务，没有出现塌缩。

**选题存在轻微漂移。** 首跑混进了绿蝇幼虫氨基酸 CE-MS、抗 dsDNA 临床免疫检测等方法学沾边但非 CMC 的文献。这是相似度排序的固有特性，可通过收紧 `search.per_cluster_limit` 或在 profile 里加排除词缓解。

**OA 全文命中率取决于 `CONTACT_EMAIL`。** 首跑该 secret 为空，Unpaywall 整级阶梯被跳过，25 篇只解析出 1 篇全文。补上后应有明显改善——但仍受限于本领域期刊的开放获取比例，不要期待高命中率。周报末尾的「需人工取全文」清单就是为此设计的。

**语料超过约 400 篇时聚类会退化。** 现在的做法是把整个文献库标题塞进一个 prompt 并要求返回每篇的归属。超过 300 篇会自动改为采样，未采样的篇目全部并入最大簇——语义上很粗糙。你现在 112 篇，按每年增长 100 篇算，大约三年后需要换一套归属策略（用 embedding 相似度而非 LLM 直接分配）。届时日志里会有明确告警。

**中英混合语料的 embedding 质量仍需人工核对。** 你的库里有中文文献（如「全柱成像毛细管等电聚焦电泳分析尤瑞克林电荷异质性方法的建立与应用」）。改用多语言的 `BAAI/bge-m3` 后风险降低，但仍建议在下次跑完后核对：那几篇中文文献被分到了哪个簇、排序是否合理。

**上图代理未接入，且不建议接。** 理由见方案文档发现 4：许可条款禁止机器人访问、封禁会落到机构账号影响全馆读者、Actions runner 是境外数据中心 IP 会触发地域限制、把出版商 PDF 存进仓库并群发已超出个人使用范围。

---

## 6. 产物落在哪

```
reports/2026/2026-08-W3.md      周报 markdown（归档）
reports/2026/2026-08-W3.html    周报网页版（归档 + 邮件附件）
library/2026/2026-08-W3/*.pdf   本周抓到的 OA 全文
state/theme_clusters.json       主题簇缓存
state/query_profiles.json       检索式缓存
state/seen_dois.json            跨周去重
state/corpus_vectors.npz        语料向量缓存
```

邮件正文是摘要式 HTML（table 布局，压在 Gmail 的 102KB 截断阈值内），附件是周报 HTML + 最多 5 篇优先读的 PDF（合计按 base64 编码后计不超过 20MB）。

**周报正文只放 DOI 链接**，不放仓库内 PDF 路径——仓库私有，组员不是 collaborator，点了打不开。

---

## 7. 月度综述（可选）

`monthly.yml` 每月 1 号 21:00（北京时间）跑一次，读当月所有周报，产出跨篇归纳。它是独立 workflow，**挂了不影响周报**。同样需要手动启用一次。

不想要就在 Actions 里禁用它，周报不受影响。

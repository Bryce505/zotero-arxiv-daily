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
  smtp_port: 587
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

executor:
  reranker: local
  debug: ${oc.env:DEBUG,null}
  source: ['biorxiv']

source:
  biorxiv:
    category: ["biochemistry"]
```

### 每一段为什么这么写

| 配置 | 原因 |
| --- | --- |
| `include_path: ["文献", "文献/**"]` | 两条缺一不可。`文献/**` 不匹配「文献」这个根分类本身。冒烟测试实测覆盖 111/112 篇 |
| `receiver: ${oc.env:SENDER}` | 原来指向 `RECEIVER`，而周报 workflow 不导出这个 secret。指向 `SENDER` 让日报流程照常，周报走 `RECIPIENTS` |
| `smtp_gmail / 587` | 原模板是 QQ 邮箱的 `smtp.qq.com:465`。你用 Gmail，587 走 STARTTLS |
| **`language: 中文`** | **最容易踩的坑。** 默认是 `English`，不写这行，你的 背景/待解决的问题/方法/结论/洞见 会全部输出英文 |
| `vector_cache` | 缓存语料向量。每周省掉约 7 分钟固定开销（实测 3.9 秒/篇 × 111 篇） |
| `executor.source` / `source.biorxiv` | 只给日报流程 `main.yml` 用。周报走 `search.sources`，不读这两项 |

> `search`、`fulltext`、`report`、`git` 四段**不用写**——`base.yaml` 里已有可用默认值，且 `NCBI_API_KEY`、`CONTACT_EMAIL`、`RECIPIENTS` 会自动注入。

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
| `CONTACT_EMAIL` | ✅ | 一个值喂四处：PubMed、Crossref 与 OpenAlex 的 polite pool、**以及 Unpaywall**。不填 Unpaywall 那级阶梯直接跳过 |
| `SENDER` | ✅ | |
| `SENDER_PASSWORD` | ✅ | Gmail 应用专用密码 |
| **`RECIPIENTS`** | ❌ **需新建** | 组员邮箱，逗号分隔。全部走 Bcc 互相隐藏 |

`RECIPIENTS` 填法（一行，逗号或分号分隔都行）：

```
zhang@corp.com, li@corp.com, wang@corp.com
```

---

## 3. 先跑预检（约 1 分钟）

**别直接跑周报。** 周报要先做完 Zotero、聚类、四源检索、全文抓取、抽取，二十多分钟之后才碰 SMTP——一个密码错了，你要等到最后一步才知道。

预检把每个边界都便宜地探一遍：**不发邮件、不写文件、不提交**。

1. Actions → **CMC weekly preflight** → 若有 "This workflow was disabled" 横幅先点 **Enable workflow**
2. **Run workflow**

输出长这样：

```
Preflight
────────────────────────────────────────────────────────────
[ OK ] zotero       111 of 112 papers matched include_path
[WARN] llm          deepseek-v4-flash answered, but llm.language is English: ...
[ OK ] pubmed       2 probe results
[ OK ] europepmc    2 probe results
[WARN] crossref     reachable but the probe query returned nothing
[ OK ] openalex     2 probe results
[FAIL] recipients   no recipients resolved; set the RECIPIENTS secret (comma separated)
[ OK ] smtp         smtp.gmail.com accepted the login
────────────────────────────────────────────────────────────
FAIL — 1 check(s) must be fixed before the weekly run
```

有 `FAIL` 就不要跑周报，先按提示修。`WARN` 不阻塞运行，但值得看一眼——比如上面那条 `llm` 警告，意味着你的周报会全出英文。

**改了任何 secret 或 `CUSTOM_CONFIG` 之后，重跑一次预检**，比跑一次完整周报便宜得多。

---

## 4. 跑周报

预检全绿之后：

1. Actions → **CMC literature weekly digest** → 若被禁用先 **Enable workflow**（fork 仓库里新建的定时任务默认是 `disabled_fork`）
2. **Run workflow** 手动触发一次

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

**最该核对的是聚类结果。** 打开 `state/theme_clusters.json`，看那几个簇名是否符合你对自己文献库的直觉。这一步决定了后面所有检索式的方向——聚错了，整周的检索都会偏。不满意就删掉这个文件重跑，或者手工改簇名与描述（`member_titles` 按标题匹配，改名不影响归属）。

---

## 5. 已知限制

**没有真实数据跑过。** 全部 399 条测试用的都是桩数据。预检（§3）能在真跑前查出配置与连通性问题，但查不出「推荐质量好不好」。第一次真跑一定会暴露我预料不到的东西——那是正常的，不是失败。

**语料超过约 400 篇时聚类会退化。** 现在的做法是把整个文献库标题塞进一个 prompt 并要求返回每篇的归属。超过 300 篇会自动改为采样，未采样的篇目全部并入最大簇——语义上很粗糙。你现在 112 篇，按每年增长 100 篇算，大约三年后需要换一套归属策略（用 embedding 相似度而非 LLM 直接分配）。届时日志里会有明确告警。

**重排仍是大头。** 语料向量缓存只砍掉固定的那 7 分钟；候选文献每周都是新的，缓存不了。候选 200 篇约 13 分钟，600 篇约 39 分钟。真要解决得换 embedding API（硅基流动 bge-m3、智谱等），仓库已有 `api` reranker，**改配置即可，不用写代码**：

```yaml
executor:
  reranker: api
reranker:
  api:
    key: ${oc.env:EMBEDDING_API_KEY}
    base_url: https://api.siliconflow.cn/v1
    model: BAAI/bge-m3
  vector_cache: state/corpus_vectors.npz
```

**中英混合语料的 embedding 质量未验证。** 你的库里有中文文献（如「全柱成像毛细管等电聚焦电泳分析尤瑞克林电荷异质性方法的建立与应用」）。当前 `jina-embeddings-v5-text-nano-retrieval` 对中文的处理强弱会直接影响相似度计算，需要在首跑后人工核对：那几篇中文文献被分到了哪个簇、排序是否合理。

**上图代理未接入，且不建议接。** 理由见方案文档发现 4：许可条款禁止机器人访问、封禁会落到机构账号影响全馆读者、Actions runner 是境外数据中心 IP 会触发地域限制、把出版商 PDF 存进仓库并群发已超出个人使用范围。周报末尾的「需人工取全文」清单就是为此设计的兜底。

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

# 周报相关性闸门与结构化改造 设计定稿

**日期：** 2026-08-22
**状态：** 待评审
**上游 spec：** `docs/cmc-literature-weekly-plan.md`（原始可行性分析，发现 1–12）

---

## 1. 问题：实测诊断

首期周报 `reports/2026/2026-08-W3.md` 交付 25 篇，逐条核对后**明显跑题 16 篇**，真正落在生物药 CMC 上的只有 3 篇：

| # | 标题（节选） | 期刊 | 判定 |
| --- | --- | --- | --- |
| 1 | Cetuximab-Based ADC 的 RP-UHPLC / SEC-UHPLC 正交方法 | Separations | ✅ 命中 |
| 7 | Fab 治疗性蛋白轻链杂质的多模式层析去除 | Biotechnology Progress | ✅ 命中 |
| 16 | AAV 衣壳 VP 电荷异质性 iCIEF–Western | — | ✅ 命中 |
| 19 | 钠离子电池硬碳负极固体电解质界面的形成与演化 | — | ❌ **完全无关** |
| 20 | H⁺+H₂ 与 D⁺+D₂ 碰撞的电离与电荷交换截面 | Plasma Physics and Controlled Fusion | ❌ **完全无关** |
| 5 | 氯吡格雷硫酸氢盐的 RP-HPLC 方法开发 | Advanced International Journal for Research | ❌ 小分子仿制药 |
| 21,22,24 | PPRV 胶体金试纸 / 鸽 PPMV-1 阻断 ELISA / 羊源产气荚膜梭菌 IgG 点印迹 | Animals, Poultry science 等 | ❌ 兽医诊断 |
| 25 | 血清 CRP 作为牙周病生物标志物 | Dentistry 3000 | ❌ 临床检验 |

### 1.1 根因一：管线里没有相关性闸门

`weekly.py` 的选文逻辑是：

```python
quota = allocate_quota({c.name: len(c.members) for c in clusters}, 25, 1)
chosen = take_by_quota(candidates, quota)
```

只有**排序**（嵌入相似度）和**配额**（5 主题 × 5 篇 = 必须凑满 25），**没有任何下限**。某主题当周只有 1 篇合格文献时，剩余 4 个名额照样从候选队尾捞——捞出了等离子体物理和钠离子电池。

嵌入相似度回答的是「像不像你的 Zotero 库」，不回答「这是不是生物药」。语料里大量色谱/质谱/电泳方法学文献，使得任何一篇讲分离分析的论文都能拿到不低的相似度，无论研究对象是抗体还是柴油。

### 1.2 根因二：几乎没拿到全文

周报「需人工取全文」列出 **24 篇**——25 篇新文献中 24 篇 `oa_status != "open"`，LLM 全程只读了摘要。

直接原因：该次运行时 `CONTACT_EMAIL` 未配置，而 `fulltext/resolver.py` 的 Unpaywall 一级**需要邮箱，缺失则整级跳过**。该变量已于 2026-08-22 补入 Repository secrets，此项会自然好转。

**本设计不建立在「全文一定拿得到」的假设上**：所有新增能力必须在只有摘要时也成立。全文是增量收益，不是前提。

### 1.3 根因三：字段是无类型字符串

`FieldSpec` 只有 `key/label/instruction` 三个字符串字段，抽取结果是 `dict[str, str]`。渲染是 `**{label}：** {value}` 一行到底。模型面对「方法」这种天然多要素的字段，只能把摘要整段搬进来——即用户反馈的「大段堆砌」。

---

## 2. 目标与非目标

### 目标

1. **相关性闸门**：不相关文献不进周报，而不是进了周报再靠洞见强行圆回生物药
2. **显示相关度与推荐理由**：每篇给出 0–100 的相关度与一句话推荐理由
3. **正文结构化**：指定字段拆成有序列表，每条为「关键词 + 说明」
4. **背景有叙事**：因果链完整，字数受控
5. **洞见锚定生物药**：围绕该文实际命中的生物药类型（重组蛋白/抗体/双抗/多抗/ADC/疫苗载体）展开
6. **期刊偏好**：可配置期刊名单，名单内加分
7. **企业单位偏好**：可配置企业名单，命中加分
8. **经典补位走同一道闸**：补位文献同样需通过相关性判定
9. **重写 README**：新手读完能理解架构、功能，并完成配置跑通

### 非目标

- 不做出版商代理/机构 VPN 抓全文（沿用上游 finding 4 的结论）
- 不做硬字数截断（会把句子拦腰砍断）
- 不改上游 firehose 检索器与 `BaseReranker.rerank` 的公开行为
- 不引入新的重量级依赖

---

## 3. 架构：两段式漏斗

选定路线为检索领域标准的 **retrieve → rerank → read**。

```
检索(4 源 × 5 主题)  ──────────────────────  ~200–500 篇
  ↓ dedup + drop_seen
嵌入打分 + 主题归类   ──────────────────────  ~150 篇
  ↓ 按嵌入分取前 triage_pool
【新】LLM 分诊（只读标题+摘要，批量）        ~60 篇
  ↓ 综合分 = 相关度 + 期刊加分 + 企业加分
【新】双闸门：相关度 ≥ min_relevance 且 综合分 ≥ min_score   ~20–30 篇
  ↓ 按主题配额（只在过闸文献中分配）
选定 ────────────────────────────────────  ≤ max_papers
  ↓ 不足 min_papers
【新】经典补位（同样经过分诊）
  ↓
取 OA 全文 → 结构化抽取（携带分诊结论）→ 渲染三份产物 → 发信/归档
```

### 3.1 为什么分诊与抽取分开

| 方案 | 判决 |
| --- | --- |
| 只改提示词 | 提示词决定不了「哪 25 篇进周报」。电池论文再怎么写还是电池 |
| 抽取时顺便判相关性 | 抽取是贵调用（6000 token 输入 + 全文下载）。要留淘汰余量得超额抽取约 2 倍，一半算力白烧 |
| **两段式分诊 → 抽取** | **选定。** 抽取预算只花在真会进周报的文献上；分诊天然产出相关度与推荐理由 |

**成本**：分诊按 8 篇一批、60 篇候选 ≈ 8 次短调用（输入仅标题+摘要，输出为小 JSON 数组）。相较现有 25 次全文抽取可忽略。当前运行时开销由嵌入主导，分诊不改变数量级。

### 3.2 闸门放在配额之前

这是本次改动的核心。配额只在**过闸文献**内分配，主题当周只有 1 篇合格就只出 1 篇。凑不满 `min_papers` 由经典补位承接——三项需求在此互锁。

---

## 4. 数据结构变更

### 4.1 新增 `TriageResult`（`triage.py`）

```python
@dataclass
class TriageResult:
    relevance: int              # 0-100，LLM 依评分细则给出
    reason: str                 # 推荐理由，一句话
    modalities: list[str]       # 命中的生物药类型，如 ["ADC", "单抗"]
```

### 4.2 `Paper` 新增字段（`protocol.py`）

```python
triage: Optional[TriageResult] = None   # 分诊结论；None 表示未判定
institutions: list[str] = field(default_factory=list)         # 检索源元数据给出的全部作者单位
company_institutions: list[str] = field(default_factory=list) # 其中被检索源明确标记为公司的子集
```

`affiliations` 字段保持不变（上游 LLM 提取路径专用），新增 `institutions` 承载**检索源直接给出的**单位，两者语义不同，不合并。

`company_institutions` 目前只有 OpenAlex 能填（它是唯一给出 `institutions[].type` 的源），其余源留空。它是 `institutions` 的子集，不重复计数。

### 4.3 `FieldSpec` 增加类型（`extract.py`）

```python
@dataclass
class FieldSpec:
    key: str
    label: str
    instruction: str
    kind: str = "text"        # "text" | "list"
    max_items: int = 0        # 仅 list 有意义，0 表示不限
```

`kind: list` 的字段，抽取 JSON 值为 `[{"point": "关键词", "detail": "说明"}]`。

---

## 5. 分诊模块 `src/zotero_arxiv_daily/triage.py`

### 5.1 接口

```python
def triage_papers(
    papers: list[Paper],
    client,
    llm_params: dict,
    batch_size: int = 8,
) -> None:
    """就地填充每篇的 paper.triage；从不抛异常。"""
```

### 5.2 评分细则（写进提示词）

用户已选定「中等」档：**研究对象是生物药，或方法学可直接迁移到生物药表征**。

| 分档 | 含义 | 参考分 |
| --- | --- | --- |
| 高 | 研究对象本身是重组蛋白/单抗/双抗/多抗/ADC/融合蛋白/疫苗/病毒载体/细胞基因治疗产品 | 80–100 |
| 中 | 研究对象不是生物药，但方法学可直接用于生物药表征（完整蛋白 top-down、糖基化位点分析、天然质谱、电荷变异体分离、聚集体分析、宿主细胞蛋白检测等） | 55–79 |
| 低 | 仅名词或技术重合，迁移需要重新开发（小分子药物色谱方法、临床诊断试剂、兽医检测、环境/食品分析） | 20–54 |
| 无关 | 与生物大分子表征无任何联系（材料、能源、物理、地学等） | 0–19 |

提示词中**显式列出反例**（钠离子电池、等离子体碰撞截面、氯吡格雷 RP-HPLC、兽用 ELISA、牙周病 CRP），因为这些正是首期实际漏进来的类型。

### 5.3 批量协议

一次调用处理 `batch_size` 篇，输入为编号列表（序号、标题、摘要，摘要按 `truncate_for_prompt` 截断），输出为 JSON 数组：

```json
[{"index": 1, "relevance": 82, "reason": "...", "modalities": ["ADC"]}, ...]
```

按 `index` 回填。返回项数不足或 index 越界时，缺失的那几篇留 `triage = None`。

### 5.4 降级策略

沿用代码库既有原则「一个部件挂掉不能拖垮整轮运行」：

1. 批调用失败 → 重试一次
2. 仍失败 → 该批降级为逐篇单独调用
3. 单篇仍失败 → `paper.triage = None`，WARNING 记录

**`triage is None` 的文献视为未过闸**，不进周报，缺口由经典补位承接。日志给出未判定篇数。理由：LLM 整体不可用时抽取同样会失败，周报本就无法交付；让缺失静默降级为「按嵌入分放行」会把闸门变成摆设。

---

## 6. 综合分与名单匹配

### 6.1 综合分

```
rank_score = relevance + journal_bonus + industry_bonus
```

- `journal_bonus`：期刊命中名单则 `report.journals.bonus`（默认 10），否则 0
- `industry_bonus`：单位命中则 `report.industry.bonus`（默认 8），否则 0

**排序用 `rank_score`。** 加分等价于「名单内门槛更低、名单外门槛更高」，正是选定的「强加分」语义。

闸门是**两道**，都要过：

```
relevance  >= report.min_relevance     # 原始相关度硬下限，加分救不了
rank_score >= report.min_score         # 综合择优线，加分在这里生效
```

第二道下限单独存在的理由：分诊细则里「低」档是 20–54 分（仅名词重合），若只有一道 `min_score: 60`，一篇原始 42 分的低档文献靠期刊 +10、企业 +8 就能过闸——加分本该在够格的文献之间择优，不该把不够格的抬进来。`min_relevance: 55` 正好卡在「中」档下沿。

两个阈值与综合分的计算都放在 `scoring.py`，与分诊解耦，便于单测。

**显示用 `relevance` 原始分**，加分只作为徽标呈现：

```
相关度 82 · 核心期刊 · 企业研究（Amgen）
```

理由：`rank_score` 可能超过 100，显示出来无法解释；`relevance` 是有语义的评分。

### 6.2 名单匹配 `src/zotero_arxiv_daily/affiliation.py`

期刊名混乱（`Molecular & cellular proteomics : MCP`、`mAbs` vs `MAbs`、`J Pharm Sci`），需要归一化：

```python
def normalize(text: str) -> str:
    """小写、非字母数字转空格、折叠空白。"""

def match_name(text: str, names: list[str]) -> str | None:
    """名单项以完整词序列出现在单条 text 中则返回该项，否则 None。"""
```

匹配用**空格填充后的子串包含**（`f" {entry} " in f" {text} "`），避免 `mabs` 误命中 `mabsorption` 一类。返回命中的名单项本身，使徽标能显示「企业研究（Amgen）」。

不支持 glob：词序列包含已足够，多一层语法只增加配置出错面。

### 6.3 企业判定的两条触发路径

```python
def is_industry(paper: Paper, names: list[str]) -> str | None:
```

1. `paper.institutions` 中任一项命中企业名单 → 返回命中的公司名
2. OpenAlex 明确标注 `institutions[].type == "company"` → 返回该机构 `display_name`

第二条白捡 Genentech、Lonza、三星生物等未写进名单的公司，且不依赖任何字符串启发式（不做「含 Inc/Ltd 即公司」这类猜测——`Institute of Pharmaceuticals` 会误判）。

实现上，OpenAlex retriever 把 `type == "company"` 的机构名同时写入 `paper.institutions` 与 `paper.company_institutions`。`affiliation.py` 只读这两个列表，不反向依赖任何检索器。

匹配**逐条单位**进行，不把单位拼成一个长串再匹配——拼接会让名单项跨越两个单位的边界误命中。

---

## 7. 检索源单位提取

**现状：四个查询式检索源都没有取作者单位。** `Paper.affiliations` 仅由 `generate_affiliations()` 从全文提取，而全文大概率拿不到。

| 源 | 字段 | 覆盖度 |
| --- | --- | --- |
| OpenAlex | `authorships[].institutions[].display_name` / `.type`、`raw_affiliation_strings` | 高 |
| PubMed | `.//AffiliationInfo/Affiliation` | 高 |
| Europe PMC | `affiliation`（通常仅第一作者） | 中 |
| Crossref | `author[].affiliation[].name` | 低，需把 `affiliation` 加进 `select` |

四个 retriever 各自填充 `paper.institutions`，去重保序。取不到就是空列表——**没有单位信息不是错误**，只是拿不到企业加分。

---

## 8. 字段类型系统与渲染

### 8.1 抽取侧

`kind: list` 的字段在 JSON schema 中声明为数组。归一化必须容错，**绝不抛异常**：

| 模型实际返回 | 归一化结果 |
| --- | --- |
| `[{"point": "柱系统", "detail": "C8 反相柱…"}]` | 原样 |
| `["C8 反相柱，变性条件", "SEC-3000 柱"]` | `point=""`，`detail=` 原字符串 |
| `"C8 反相柱，变性条件"`（纯字符串） | 单条，`point=""` |
| `null` / 缺键 | 空列表，该字段不渲染 |

`max_items > 0` 时截断到前 N 条。

### 8.2 渲染侧（三个渲染器各一份）

**markdown**

```markdown
**方法：**

1. **柱系统** — C8 反相柱，变性条件；SEC-3000 柱，天然条件
2. **检测** — 二极管阵列检测器
```

**网页 HTML**：`<ol><li><strong>关键词</strong> — 说明</li></ol>`

**邮件 HTML**：同结构但内联样式（Gmail 剥离 `@font-face` 与 CSS 变量，Outlook 走 Word 引擎），沿用 `report.py` 既有约定。

### 8.3 徽标行与推荐理由

每篇标题下方增加一行（三个渲染器一致）：

```
相关度 82 · 核心期刊 · 企业研究（Amgen）

**推荐理由：** 首次把 iCIEF–Western 用于 AAV 衣壳 VP 蛋白的电荷异质性表征，直接对应库内电荷变异体分析主题。
```

`triage is None` 时整行省略（只可能出现在人工构造的场景，正常管线里未过闸的文献根本不会进入渲染）。

---

## 9. 洞见锚定生物药

分诊已判定该文挂在哪些生物药类型上（`triage.modalities`）。**把这个结论回传给抽取提示词**：

> 本文经判定与以下生物药类型相关：ADC、单抗。请围绕这些类型展开洞见，说明该方法/发现可以怎样用在这类产品的 CMC 分析上。

首期周报里那句「RP-HPLC 仍是药物质量控制中的高效可靠手段」是氯吡格雷仿制药论文被硬凑出来的空话——新管线里它综合分不过闸，压根不会进入抽取。

`modalities` 为空但过了闸（方法学迁移类）时，回传语改为提示可迁移性，不硬塞一个不存在的类型。

---

## 10. 经典补位

现状：`backfill_papers()` 按 OpenAlex `cited_by_count` 排序取数，**不判相关性**——首期补进一篇 2005 年的病毒学论文。

改为：超额取候选（沿用 `_OVERSAMPLE = 3`）→ 走同一套 `triage_papers` → 过闸的按 `cited_by_count` 降序取前 `needed` 篇。

期刊/企业加分同样适用。渲染时保留既有「经典补位」分区与被引数标注。

---

## 11. 配置契约

```yaml
report:
  min_papers: 15
  max_papers: 25
  top_picks: 3
  min_per_cluster: 1

  # —— 新增 ——
  min_relevance: 55        # 原始相关度硬下限；加分不能突破这条线
  min_score: 60            # 综合分下限（相关度 + 期刊加分 + 企业加分）
  triage_pool: 60          # 送去分诊的候选上限（按嵌入分数取前 N；候选不足则全送）
  triage_batch: 8          # 每次分诊调用处理几篇

  journals:
    bonus: 10              # 命中名单的加分
    allow: [ ... ]         # 见 11.1

  industry:
    bonus: 8               # 命中企业的加分
    names: [ ... ]         # 见 11.2
```

`journals.allow` 或 `industry.names` 为空/缺省时，对应加分恒为 0——名单是可选增强，不是运行前提。
`min_relevance: 0` 且 `min_score: 0` 等价于关闭闸门，退回旧行为，便于对照排查。

排在 `triage_pool` 之外的候选（嵌入分最低的那批）直接淘汰，不做分诊。这是成本上限，不是质量判断——它们本就是相似度最低的一批。

### 11.1 期刊名单（默认值，可自行增删）

```yaml
allow:
  # 生物药与制剂
  - mAbs
  - Antibody Therapeutics
  - Antibodies
  - BioDrugs
  - Biologicals
  - Bioconjugate Chemistry
  - Molecular Pharmaceutics
  - Journal of Pharmaceutical Sciences
  - Journal of Pharmaceutical and Biomedical Analysis
  - European Journal of Pharmaceutics and Biopharmaceutics
  - International Journal of Pharmaceutics
  - Pharmaceutical Research
  - Pharmaceutics
  - AAPS Journal
  - AAPS PharmSciTech
  - PDA Journal of Pharmaceutical Science and Technology
  # 生物工艺与生物技术
  - Biotechnology and Bioengineering
  - Biotechnology Progress
  - Biotechnology Journal
  - Journal of Biotechnology
  - New Biotechnology
  - Nature Biotechnology
  - Frontiers in Bioengineering and Biotechnology
  # 分析化学、色谱与质谱
  - Analytical Chemistry
  - Analytica Chimica Acta
  - Analytical and Bioanalytical Chemistry
  - Analyst
  - Journal of Chromatography A
  - Journal of Chromatography B
  - Journal of Separation Science
  - Separations
  - Electrophoresis
  - Journal of the American Society for Mass Spectrometry
  - Journal of Mass Spectrometry
  - Bioanalysis
  # 蛋白质、糖生物学与组学
  - Journal of Proteome Research
  - Molecular and Cellular Proteomics
  - Proteomics
  - Protein Science
  - Protein Expression and Purification
  - Glycobiology
  - Journal of Biological Chemistry
  # 免疫与疫苗
  - Journal of Immunological Methods
  - Vaccine
  - Vaccines
  - Human Vaccines and Immunotherapeutics
  # 综合
  - Nature Communications
```

**说明**：`Molecular and Cellular Proteomics` 与 `Human Vaccines and Immunotherapeutics` 写成 `and` 形式——归一化会把 `&` 转成空格，两种写法都能命中。首期最好的那篇 ADC 论文发在 MDPI 的 *Separations*，已收入名单；这正是选「强加分」而非「硬过滤」的现实理由：任何手写名单都会漏。

### 11.2 企业名单（默认值，可自行增删）

```yaml
names:
  # 跨国生物制药
  - Amgen
  - Merck
  - MSD
  - Pfizer
  - Bristol Myers Squibb
  - Genentech
  - Roche
  - Novartis
  - Sanofi
  - AstraZeneca
  - GlaxoSmithKline
  - GSK
  - Johnson and Johnson
  - Janssen
  - Eli Lilly
  - AbbVie
  - Biogen
  - Regeneron
  - Moderna
  - BioNTech
  - Takeda
  - Bayer
  - Boehringer Ingelheim
  - Novo Nordisk
  - CSL Behring
  - Grifols
  - Daiichi Sankyo
  - Astellas
  - Alexion
  - Vertex
  - Gilead
  - Seagen
  - Alnylam
  - Ionis
  # CDMO 与上游供应商
  - Lonza
  - Samsung Biologics
  - WuXi Biologics
  - Catalent
  - Thermo Fisher
  - Sartorius
  - Cytiva
  - Repligen
  # 国内生物药企
  - Hengrui
  - Innovent
  - BeiGene
  - Henlius
  - Junshi
  - RemeGen
  - Akeso
  - Zai Lab
  - CanSino
  - Sunshine Guojian
```

国内企业按检索源实际给出的英文名收录（`WuXi Biologics`、`BeiGene` 等），中文名不进名单——PubMed/OpenAlex 的单位字符串均为英文。

### 11.3 报告字段（重写）

```yaml
fields:
  - key: background
    label: 背景
    kind: text
    instruction: >-
      用 120–180 字讲一个完整的小故事：这个领域原本靠什么手段做、遇到了什么具体的坎、
      为什么这道坎现在必须迈过去。要有因果链，一句接一句推进，不要罗列名词，
      不要用「本研究」开头。

  - key: gap
    label: 待解决的问题
    kind: text
    instruction: >-
      用 60–100 字说清此前尚未解决的那一个核心问题。只讲问题本身，不讲本文怎么解决。

  - key: method
    label: 方法
    kind: list
    max_items: 5
    instruction: >-
      拆成 3–5 条。每条 point 是 2–6 字的关键词（如「柱系统」「检测方式」「验证项」），
      detail 是 30–60 字的具体说明，含关键参数。不要把整段摘要塞进一条。

  - key: conclusion
    label: 结论
    kind: list
    max_items: 5
    instruction: >-
      拆成 2–5 条。每条 point 是该条结论的一句话概括，detail 是支撑它的关键数据
      （回收率、纯度、线性 R²、LOD/LOQ 等），30–60 字。

  - key: insight
    label: 洞见
    kind: list
    max_items: 3
    instruction: >-
      拆成 2–3 条，每条必须落在给定的生物药类型上。point 是应用场景
      （如「ADC 载药分布表征」「双抗错配体检测」），detail 说明具体怎么用、
      能替代或补强现有哪一步、有什么前提限制，40–80 字。
      禁止写「该方法高效可靠」这类不含信息的套话。
```

**字数限制只走提示词预算，不做硬截断**——截断会把句子拦腰砍断，比超字数更难看。测试断言每条 instruction 都带字数区间，防止后续编辑时丢失。

---

## 12. 错误处理与降级

| 情形 | 行为 |
| --- | --- |
| 分诊批调用失败 | 重试一次 → 降级逐篇 → 仍失败则 `triage = None` |
| `triage is None` | 视为未过闸，不进周报；WARNING 记录篇数 |
| 相关度达标但综合分不足 | 正常淘汰，INFO 记录，便于事后调阈值 |
| 过闸文献少于 `min_papers` | 触发经典补位（补位同样过闸） |
| 过闸文献为 0 且补位为 0 | 沿用既有逻辑：`send_empty` 决定是否发空信 |
| 期刊/企业名单为空 | 对应加分恒为 0，不报错 |
| 检索源未给出单位 | `institutions = []`，拿不到企业加分，不报错 |
| list 字段返回形状异常 | 按 8.1 归一化，绝不抛异常 |

---

## 13. 测试策略

沿用既有约定：禁用 `unittest.mock`，一律 `pytest monkeypatch` + `SimpleNamespace` + `tests/canned_responses.py`。

**新增测试文件**

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_triage.py` | 批量协议解析、index 回填、乱序/缺项、重试与逐篇降级、全失败置 None |
| `tests/test_affiliation.py` | 归一化、词序列匹配、`mabs` 不误命中、`&` 与 `and` 等价、企业两条触发路径 |
| `tests/test_scoring.py` | 综合分计算；两道闸门各自生效；**加分不能把低于 `min_relevance` 的文献抬进来** |

**扩充既有测试**

- `tests/test_extract.py`：list 字段四种异常形状的归一化、`max_items` 截断、分诊结论进入提示词
- `tests/test_report.py`：三个渲染器的有序列表与徽标行、`triage is None` 时省略徽标
- `tests/test_weekly.py`：闸门位于配额之前（构造一个主题只有 1 篇过闸的场景，断言不会凑数）
- `tests/test_backfill.py`：补位候选经过分诊、未过闸的不进结果
- `tests/test_setup_doc.py`：新增配置键在文档中有对应说明

**回归底线**：既有 421 项测试全部保持通过（`tests/test_protocol.py` 中 3 项 tiktoken 用例因沙箱内 `openaipublic.blob.core.windows.net` 被出口策略拒绝而失败，与本次改动无关，属既有状态）。

---

## 14. README 重写大纲

现有 README 仍是上游 zotero-arxiv-daily 的说明，与本仓库实际能力已经脱节。目标读者：**新手 clone/fork 后照着配好参数就能跑通**。

```
1. 这个项目做什么          一句话 + 一张周报截图式样例
2. 它和上游 zotero-arxiv-daily 的关系   保留了什么、新增了什么
3. 架构                    两条流水线（daily firehose / weekly digest）的图与阶段说明
                           插件式注册表：retriever / query retriever / reranker
4. 快速开始                fork → 配 Secrets/Variables → 跑预检 → 手动触发一次
5. 配置详解                逐节讲 config：zotero / source / search / reranker /
                           llm / report（含新增闸门与两份名单）/ fulltext / email / git
                           每个参数：作用、默认值、怎么调、调错了会怎样
6. 环境变量与 Secrets 清单  哪些必填、哪些可选、去哪里申请
7. 预检                    preflight 逐项检查什么，输出怎么读
8. 产物                    reports/ library/ state/ 各是什么、为什么要提交回仓库
9. 排错                    空结果、相关度过低/过高、邮件被截断、推送失败
10. 本地开发                uv sync / pytest / 加一个新检索源要动哪几处
```

配置详解一节必须与 `config/base.yaml` 的注释保持一致——`tests/test_setup_doc.py` 已有守卫比对工作流导出的环境变量与文档中的 `${oc.env:...}`，本次扩充到新增配置键。

---

## 15. 变更文件清单

**新增**

| 文件 | 职责 |
| --- | --- |
| `src/zotero_arxiv_daily/triage.py` | LLM 相关性分诊：批量协议、降级、结果回填 |
| `src/zotero_arxiv_daily/affiliation.py` | 名称归一化与名单匹配（期刊 + 企业） |
| `src/zotero_arxiv_daily/scoring.py` | 综合分计算与闸门筛选 |
| `tests/test_triage.py`、`tests/test_affiliation.py`、`tests/test_scoring.py` | 对应测试 |

**修改**

| 文件 | 改动 |
| --- | --- |
| `protocol.py` | `Paper` 增 `triage`、`institutions` |
| `extract.py` | `FieldSpec.kind/max_items`；list 归一化；提示词携带分诊结论 |
| `report.py` | 三个渲染器支持有序列表与徽标行 |
| `weekly.py` | 插入分诊与闸门阶段，位于配额之前 |
| `backfill.py` | 补位候选走同一道闸 |
| `retriever/{openalex,pubmed,europepmc,crossref}_retriever.py` | 填充 `institutions` |
| `config/base.yaml` | 新增 `report.min_score/triage_pool/triage_batch/journals/industry`；重写 `fields` |
| `docs/cmc-weekly-setup.md` | 更新 `CUSTOM_CONFIG` 样例 |
| `README.md` | 按第 14 节重写 |

---

## 16. 风险与取舍

| 风险 | 缓解 |
| --- | --- |
| 闸门过严导致周报太薄 | 两个阈值都是配置项；经典补位承接缺口；首次运行后按实际分布调 |
| 加分把低档文献抬进周报 | `min_relevance` 是加分无法突破的硬下限 |
| 分诊 LLM 判定不稳 | 评分细则含分档参考分与**实际漏进来过的反例**；批量输出小 JSON，解析失败逐篇重试 |
| 期刊名单漏收 | 选定「强加分」而非硬过滤，漏收只导致排名靠后，不会静默丢失 |
| 企业加分让学术界好文章被压 | 加分仅 8 分（相关度满分 100），是微调不是重排；且只在过闸文献间生效 |
| 分诊增加运行时长 | 8 次短调用，相对 25 次全文抽取可忽略；运行时开销仍由嵌入主导 |
| 全文仍然拿不到 | 所有新增能力在只有摘要时同样成立；全文是增量收益 |
| 首期 25 个 DOI 因推送丢失会重复投递 | 与本设计无关的既有问题，单独处理 |

### 明确不做

- 硬字数截断
- 期刊名单支持 glob 语法
- 从单位字符串启发式判断「是否公司」（`Institute of Pharmaceuticals` 会误判）
- 出版商代理抓全文

# 补位放宽 / 多轮检索 / 特定主题检索 Implementation Plan

> **For agentic workers:** 按任务顺序实现，每个任务先写失败的测试再写实现，单任务单提交。步骤用 `- [ ]` 勾选跟踪。

**Goal:** (1) 经典补位改用放宽的主题判定；(2) 补位不足时换检索式多轮重试（≤3 轮）；(3) 新增「特定主题」检索线：变量传入主题+可选背景，LLM 生成检索式，结果单列一节进周报。

**Spec:** `docs/superpowers/specs/2026-09-01-backfill-rounds-and-focus-topic-design.md`

**Tech Stack:** Python ≥3.13 · Hydra + OmegaConf · openai SDK · pytest（纯 stub，无 Docker）

## Global Constraints

- 不新增依赖
- 测试禁用 `unittest.mock`；一律 `pytest monkeypatch` + `SimpleNamespace`
- 沙箱内 `uv sync` 不可用（`download.pytorch.org` 被出口策略拒绝）；用 Python 3.13 venv + pip 装除 `torch`/`sentence-transformers` 外的依赖跑 `-m "not slow"` 套件
- `tests/test_protocol.py` 的 3 项 tiktoken 联网用例在沙箱内失败，属既有状态，不要试图修
- 任一 LLM/外部源失败都必须降级，不得向上抛
- 提交信息用英文，说清「为什么」；产物中不得出现模型标识
- 分支：`claude/literature-report-link-mismatch-cnss4y`

---

## Task 1: 补位放宽主题判定

**Files:** `triage.py`、`weekly.py`、`tests/test_triage.py`、`tests/test_weekly.py`

**Interfaces:**
- `triage_papers(..., require_theme_fit: bool = True)`
- `_apply_theme_verdicts(papers, themes, require_theme_fit=True)`
- `WeeklyExecutor._gate(papers, require_theme_fit=True)`

- [x] Step 1: 写失败的测试（放宽模式保留「无」判决、仍应用真实主题更正、默认严格不变、补位调用点传放宽）
- [x] Step 2: 跑测试确认失败
- [x] Step 3: 实现
- [x] Step 4: 跑测试确认通过
- [x] Step 5: 提交

## Task 2: 多轮补位检索

**Files:** `backfill.py`、`search/profile.py`、`weekly.py`、`tests/test_backfill.py`、`tests/search/test_profile.py`

**Interfaces:**
- `backfill_papers(profiles, retriever, needed, exclude_dois, gate=None, requery=None, max_rounds=3)`
- `alternate_queries(profiles, tried: dict[str, list[str]], client, llm_params) -> dict[str, str]`

- [ ] Step 1: 写失败的测试（第二轮补足、轮数上限、requery 缺省=单轮、跨轮去重不重复过闸、requery 失败即停、凑够即停）
- [ ] Step 2: 跑测试确认失败
- [ ] Step 3: 实现
- [ ] Step 4: 跑测试确认通过
- [ ] Step 5: 提交

## Task 3: 特定主题检索

**Files:** `search/focus.py`(新)、`triage.py`、`config/base.yaml`、`tests/search/test_focus.py`(新)

**Interfaces:**
- `focus_settings(config) -> FocusSettings | None`（topic 为空返回 None）
- `build_focus_profile(topic, background, client, llm_params) -> tuple[QueryProfile, str]`
- `triage_for_topic(papers, client, llm_params, topic, background, batch_size)`
- `FocusResult(topic, summary, papers)`

- [ ] Step 1: 写失败的测试（空 topic 零调用、profile 构建与降级、主题分诊与阈值、兜底补足、上限截断）
- [ ] Step 2: 跑测试确认失败
- [ ] Step 3: 实现
- [ ] Step 4: 跑测试确认通过
- [ ] Step 5: 提交

## Task 4: 渲染与编排接入

**Files:** `report.py`、`weekly.py`、`tests/test_report.py`、`tests/test_weekly.py`

- [ ] Step 1: 写失败的测试（三个渲染器的特定主题分区、目录/编号、total、需人工取全文、无 focus 时不渲染；weekly 端到端接入）
- [ ] Step 2: 跑测试确认失败
- [ ] Step 3: 实现
- [ ] Step 4: 跑测试确认通过
- [ ] Step 5: 提交

## Task 5: 文档与收尾

**Files:** `README.md`、`docs/cmc-weekly-setup.md`、spec 状态更新

- [ ] Step 1: README 增加特定主题一节与新配置键说明
- [ ] Step 2: setup 文档补 `FOCUS_TOPIC` / `FOCUS_BACKGROUND` 两个变量
- [ ] Step 3: 跑全量测试
- [ ] Step 4: 提交并推送

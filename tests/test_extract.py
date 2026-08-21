"""Config-driven structured extraction."""

import json
from types import SimpleNamespace

from omegaconf import OmegaConf

from zotero_arxiv_daily.extract import (
    FieldSpec,
    extract_all,
    extract_paper,
    load_field_specs,
)
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.utils import truncate_for_prompt

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}

FIELDS = [
    FieldSpec(key="background", label="背景", instruction="研究背景"),
    FieldSpec(key="gap", label="待解决的问题", instruction="尚未解决的问题"),
    FieldSpec(key="method", label="方法", instruction="所用方法"),
]

PAYLOAD = json.dumps(
    {"background": "单抗电荷异质性", "gap": "缺乏快速方法", "method": "cIEF"}, ensure_ascii=False
)


def stub_client(payload: str, recorder: list | None = None) -> SimpleNamespace:
    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_paper(**kw) -> Paper:
    base = dict(source="pubmed", title="A paper", authors=[], abstract="An abstract.", url="u")
    base.update(kw)
    return Paper(**base)


def test_truncate_returns_short_text_unchanged():
    assert truncate_for_prompt("short", 100) == "short"


def test_truncate_shortens_long_text():
    long_text = "word " * 5000
    result = truncate_for_prompt(long_text, 50)
    assert len(result) < len(long_text)


def test_truncate_handles_empty_text():
    assert truncate_for_prompt("", 100) == ""


def test_truncate_survives_a_tokenizer_that_cannot_load(monkeypatch):
    """The weekly run must not depend on a vocabulary download succeeding."""
    import tiktoken

    def _boom(model):
        raise RuntimeError("encoding files unreachable")

    monkeypatch.setattr(tiktoken, "encoding_for_model", _boom)
    result = truncate_for_prompt("word " * 5000, 50)
    assert result
    assert len(result) < 25000


def test_extract_returns_every_configured_field():
    result = extract_paper(make_paper(), stub_client(PAYLOAD), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert result["method"] == "cIEF"


def test_extract_fills_missing_fields_rather_than_omitting_them():
    partial = json.dumps({"background": "只有背景"}, ensure_ascii=False)
    result = extract_paper(make_paper(), stub_client(partial), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert result["gap"] == ""


def test_extract_degrades_to_empty_fields_on_bad_json():
    result = extract_paper(make_paper(), stub_client("not json"), LLM_PARAMS, FIELDS)
    assert set(result) == {"background", "gap", "method"}
    assert all(v == "" for v in result.values())


def test_extract_prefers_full_text_when_available():
    recorder = []
    paper = make_paper(full_text="FULL TEXT BODY")
    extract_paper(paper, stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "FULL TEXT BODY" in prompt


def test_extract_falls_back_to_the_abstract_without_full_text():
    recorder = []
    extract_paper(make_paper(), stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "An abstract." in prompt


def test_extract_asks_for_exactly_the_configured_field_keys():
    recorder = []
    extract_paper(make_paper(), stub_client(PAYLOAD, recorder), LLM_PARAMS, FIELDS)
    prompt = recorder[0]["messages"][-1]["content"]
    for field in FIELDS:
        assert field.key in prompt
        assert field.label in prompt


def test_extract_all_writes_onto_every_paper():
    papers = [make_paper(title="a"), make_paper(title="b")]
    extract_all(papers, stub_client(PAYLOAD), LLM_PARAMS, FIELDS)
    assert all(p.extraction is not None for p in papers)
    assert papers[0].extraction["method"] == "cIEF"


def test_one_failing_paper_does_not_abort_the_batch():
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=PAYLOAD))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    papers = [make_paper(title="a"), make_paper(title="b")]
    extract_all(papers, client, LLM_PARAMS, FIELDS)
    assert papers[0].extraction == {"background": "", "gap": "", "method": ""}
    assert papers[1].extraction["method"] == "cIEF"


def test_field_specs_come_from_config():
    config = OmegaConf.create(
        {"report": {"fields": [{"key": "insight", "label": "洞见", "instruction": "对我的启发"}]}}
    )
    assert load_field_specs(config) == [FieldSpec(key="insight", label="洞见", instruction="对我的启发")]


def test_field_specs_tolerate_a_missing_instruction():
    config = OmegaConf.create({"report": {"fields": [{"key": "insight", "label": "洞见"}]}})
    assert load_field_specs(config)[0].instruction == "洞见"


def test_adding_a_field_in_config_changes_the_prompt_alone():
    """Customising the output is a YAML edit, not a code change."""
    config = OmegaConf.create(
        {
            "report": {
                "fields": [
                    {"key": "background", "label": "背景", "instruction": "研究背景"},
                    {"key": "regulatory", "label": "法规关联", "instruction": "与 ICH 指南的关联"},
                ]
            }
        }
    )
    recorder = []
    specs = load_field_specs(config)
    extract_paper(make_paper(), stub_client(PAYLOAD, recorder), LLM_PARAMS, specs)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "regulatory" in prompt
    assert "法规关联" in prompt

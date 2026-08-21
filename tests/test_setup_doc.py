"""The setup guide hands the operator a config to paste; it must stay valid.

If the config schema moves and the doc does not, the operator pastes something
that composes wrong — and only finds out when a scheduled run fails.
"""

import os
import re
import shutil
from pathlib import Path

import pytest
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "cmc-weekly-setup.md"

# Exactly what .github/workflows/weekly.yml exports.
WORKFLOW_ENV = {
    "ZOTERO_ID": "000",
    "ZOTERO_KEY": "zk",
    "OPENAI_API_KEY": "sk",
    "OPENAI_API_BASE": "https://api.deepseek.com",
    "NCBI_API_KEY": "ncbi",
    "CONTACT_EMAIL": "me@corp.com",
    "SENDER": "me@corp.com",
    "SENDER_PASSWORD": "pw",
    "RECIPIENTS": "zhang@corp.com, li@corp.com, wang@corp.com",
}


def documented_custom_config() -> str:
    match = re.search(r"```yaml\n(zotero:.*?)```", DOC.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert match, "the setup guide no longer contains a CUSTOM_CONFIG block"
    return match.group(1)


@pytest.fixture()
def composed(tmp_path, monkeypatch):
    for name, value in WORKFLOW_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RECEIVER", raising=False)

    for name in ("base.yaml", "default.yaml"):
        shutil.copy(REPO / "config" / name, tmp_path / name)
    (tmp_path / "custom.yaml").write_text(documented_custom_config(), encoding="utf-8")

    GlobalHydra.instance().clear()
    with initialize_config_dir(config_dir=str(tmp_path), version_base=None):
        return compose(config_name="default")


def test_the_documented_config_composes(composed):
    assert composed is not None


def test_it_selects_the_whole_literature_tree(composed):
    assert list(composed.zotero.include_path) == ["文献", "文献/**"]


def test_it_sets_the_output_language(composed):
    """The default is English; a Chinese digest needs this set explicitly."""
    assert composed.llm.language == "中文"


def test_the_team_recipients_resolve_without_a_receiver_secret(composed):
    from zotero_arxiv_daily.mailer import resolve_recipients

    assert resolve_recipients(composed.email) == [
        "zhang@corp.com",
        "li@corp.com",
        "wang@corp.com",
    ]


def test_the_contact_email_reaches_every_place_that_needs_it(composed):
    assert composed.source.pubmed.email == "me@corp.com"
    assert composed.source.crossref.mailto == "me@corp.com"
    assert composed.source.openalex.mailto == "me@corp.com"
    assert composed.fulltext.unpaywall_email == "me@corp.com"


def test_every_searched_source_has_a_config_block(composed):
    for source in composed.search.sources:
        assert source in composed.source


def test_the_report_fields_are_the_ones_the_guide_promises(composed):
    from zotero_arxiv_daily.extract import load_field_specs

    assert [f.label for f in load_field_specs(composed)] == [
        "背景",
        "待解决的问题",
        "方法",
        "结论",
        "洞见",
    ]


def test_the_secrets_table_lists_every_secret_the_workflow_exports():
    doc = DOC.read_text(encoding="utf-8")
    workflow = (REPO / ".github" / "workflows" / "weekly.yml").read_text(encoding="utf-8")
    exported = set(re.findall(r"^\s+([A-Z_]+): \$\{\{ secrets\.", workflow, flags=re.M))
    for name in exported:
        assert name in doc, f"{name} is exported by weekly.yml but absent from the setup guide"

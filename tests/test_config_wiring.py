"""Every secret the workflows export must reach the code that needs it."""

from pathlib import Path

import pytest
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra

CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")
BASE_OVERRIDES = [
    "zotero.user_id=1",
    "zotero.api_key=k",
    "email.sender=a@b.c",
    "email.receiver=a@b.c",
    "email.smtp_server=s",
    "email.smtp_port=465",
    "email.sender_password=p",
    "llm.api.key=k",
    "llm.api.base_url=u",
    "llm.generation_kwargs.model=m",
    "executor.source=[arxiv]",
]


def compose_config():
    GlobalHydra.instance().clear()
    with initialize_config_dir(config_dir=CONFIG_DIR, version_base=None):
        return compose(config_name="default", overrides=BASE_OVERRIDES)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "ncbi-secret")
    monkeypatch.setenv("CONTACT_EMAIL", "contact@example.org")


def test_ncbi_api_key_reaches_the_pubmed_retriever(env):
    assert compose_config().source.pubmed.api_key == "ncbi-secret"


def test_contact_email_reaches_pubmed(env):
    assert compose_config().source.pubmed.email == "contact@example.org"


def test_contact_email_reaches_the_crossref_polite_pool(env):
    assert compose_config().source.crossref.mailto == "contact@example.org"


def test_contact_email_reaches_openalex(env):
    assert compose_config().source.openalex.mailto == "contact@example.org"


def test_contact_email_enables_unpaywall(env):
    assert compose_config().fulltext.unpaywall_email == "contact@example.org"


def test_the_config_still_composes_without_any_of_those_secrets(monkeypatch):
    for name in ("NCBI_API_KEY", "CONTACT_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    cfg = compose_config()
    assert cfg.source.pubmed.api_key is None
    assert cfg.fulltext.unpaywall_email is None


def test_every_query_source_named_in_search_has_a_config_block(env):
    cfg = compose_config()
    for source in cfg.search.sources:
        assert source in cfg.source, f"{source} is searched but has no config block"

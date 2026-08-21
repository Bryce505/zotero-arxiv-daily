"""The query-style retriever registry, parallel to the firehose one."""

from datetime import date

import pytest

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.retriever.query_base import (
    BaseQueryRetriever,
    get_query_retriever_cls,
    register_query_retriever,
)


def test_registering_makes_a_retriever_findable_by_name(config):
    @register_query_retriever("stub_source")
    class StubRetriever(BaseQueryRetriever):
        def search(self, query, start, end, limit):
            return [Paper(source=self.name, title=query, authors=[], abstract="a", url="u")]

    cls = get_query_retriever_cls("stub_source")
    assert cls is StubRetriever
    assert cls.name == "stub_source"
    papers = cls(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 10)
    assert papers[0].source == "stub_source"


def test_unknown_query_retriever_raises():
    with pytest.raises(ValueError, match="not found"):
        get_query_retriever_cls("no_such_source")


def test_a_retriever_tolerates_an_absent_config_block(config):
    @register_query_retriever("unconfigured_source")
    class Unconfigured(BaseQueryRetriever):
        def search(self, query, start, end, limit):
            return []

    retriever = Unconfigured(config)
    assert retriever._setting("api_key") is None
    assert retriever._setting("tool", "fallback") == "fallback"


def test_the_firehose_registry_is_untouched():
    from zotero_arxiv_daily.retriever.base import registered_retrievers
    from zotero_arxiv_daily.retriever.query_base import registered_query_retrievers

    assert registered_query_retrievers is not registered_retrievers
    assert "arxiv" in registered_retrievers
    assert "arxiv" not in registered_query_retrievers

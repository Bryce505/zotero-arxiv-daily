"""Query-style retrieval, parallel to the firehose retrievers.

``BaseRetriever`` models "everything posted today in a category" and its
``retrieve_papers()`` takes no arguments.  Journal literature needs the
opposite shape — a boolean query bounded by a date window — so it gets its own
base class and its own registry rather than bending the existing interface.
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import Type

from omegaconf import DictConfig

from ..protocol import Paper


class BaseQueryRetriever(ABC):
    name: str

    def __init__(self, config: DictConfig):
        self.config = config
        self.retriever_config = getattr(config.source, self.name, None)

    def _setting(self, key: str, default=None):
        """Read a per-source setting, tolerating an absent config block."""
        if self.retriever_config is None:
            return default
        value = self.retriever_config.get(key, default)
        return default if value is None else value

    @abstractmethod
    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        """Return papers matching *query* published between *start* and *end*."""


registered_query_retrievers: dict[str, Type[BaseQueryRetriever]] = {}


def register_query_retriever(name: str):
    def decorator(cls):
        registered_query_retrievers[name] = cls
        cls.name = name
        return cls
    return decorator


def get_query_retriever_cls(name: str) -> Type[BaseQueryRetriever]:
    if name not in registered_query_retrievers:
        raise ValueError(f"Query retriever {name} not found")
    return registered_query_retrievers[name]

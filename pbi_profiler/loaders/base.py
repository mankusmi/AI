"""Abstract interfaces implemented by each model-loading backend."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..model import Model


class ModelLoader(ABC):
    """Loads a `Model` (schema/metadata) from some source."""

    @abstractmethod
    def load(self) -> Model:
        ...


class QueryExecutor(ABC):
    """Runs a DAX query against a live model and returns rows as dicts.

    Only backends with an actual connection to model data (currently the
    live REST loader) can provide this; the data profiler is skipped when
    no executor is available (e.g. profiling a local TMDL/BIM file with no
    live data behind it).
    """

    @abstractmethod
    def run_dax(self, query: str) -> list[dict[str, Any]]:
        ...


class LoadedModel:
    """Bundle returned by a loader: the schema plus an optional data executor."""

    def __init__(self, model: Model, executor: Optional[QueryExecutor] = None):
        self.model = model
        self.executor = executor

from __future__ import annotations

from abc import ABC, abstractmethod

from ..report_model import ReportModel


class ReportLoader(ABC):
    """Loads a `ReportModel` (pages + visuals) from some report artifact."""

    @abstractmethod
    def load(self) -> ReportModel:
        ...

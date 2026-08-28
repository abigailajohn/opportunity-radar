from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.extraction import OpenAIFactualExtractionProvider, SemanticOpportunityExtractor
from opportunity_radar.fetching import HttpPageFetcher
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor


class ProviderMode(StrEnum):
    DETERMINISTIC = "deterministic"
    OPENAI = "openai"


@dataclass(frozen=True)
class ProviderBundle:
    mode: ProviderMode
    fetcher: PageFetcher
    extractor: OpportunityExtractor
    assessor: SemanticAssessor


def provider_mode_from_environment(explicit_mode: str | None = None) -> ProviderMode:
    value = explicit_mode or os.getenv("OPPORTUNITY_RADAR_MODE", ProviderMode.DETERMINISTIC.value)
    try:
        return ProviderMode(value.casefold())
    except ValueError as exc:
        raise ValueError("OPPORTUNITY_RADAR_MODE must be 'deterministic' or 'openai'") from exc


def build_provider_bundle(
    mode: ProviderMode,
    *,
    http_client: Any | None = None,
    openai_provider: Any | None = None,
) -> ProviderBundle:
    fetcher = HttpPageFetcher(client=http_client)
    assessor = DeterministicSemanticAssessor()
    if mode is ProviderMode.DETERMINISTIC:
        extractor: OpportunityExtractor = DeterministicOpportunityExtractor()
    else:
        provider = openai_provider or OpenAIFactualExtractionProvider.from_environment()
        extractor = SemanticOpportunityExtractor(provider)
    return ProviderBundle(mode, fetcher, extractor, assessor)

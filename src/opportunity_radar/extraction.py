from __future__ import annotations

import os
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin

from pydantic import BaseModel, Field, HttpUrl, ValidationError

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.models import (
    ApplicationRequirements,
    ConfidenceLevel,
    EligibilityRequirements,
    FundingDetails,
    Opportunity,
    ParticipationMode,
    SourceEvidence,
)
from opportunity_radar.normalization import derive_status


DEFAULT_SEMANTIC_INPUT_LIMIT = 50_000
DEFAULT_MINIMUM_CLEANED_CHARACTERS = 50


class ExtractionFailureKind(StrEnum):
    CONFIGURATION = "configuration"
    PROVIDER = "provider_failure"
    INVALID_STRUCTURED_RESPONSE = "invalid_structured_response"
    SCHEMA_VALIDATION = "schema_validation"
    INSUFFICIENT_CONTENT = "insufficient_cleaned_content"
    UNGROUNDED_OUTPUT = "ungrounded_output"


class ExtractionError(RuntimeError):
    def __init__(self, kind: ExtractionFailureKind, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class ExtractionConfigurationError(ExtractionError):
    def __init__(self, message: str) -> None:
        super().__init__(ExtractionFailureKind.CONFIGURATION, message)


class ExtractionProviderError(ExtractionError):
    def __init__(self, message: str) -> None:
        super().__init__(ExtractionFailureKind.PROVIDER, message)


class InvalidStructuredResponseError(ExtractionError):
    def __init__(self, message: str) -> None:
        super().__init__(ExtractionFailureKind.INVALID_STRUCTURED_RESPONSE, message)


class ExtractionSchemaError(ExtractionError):
    def __init__(self, message: str) -> None:
        super().__init__(ExtractionFailureKind.SCHEMA_VALIDATION, message)


class InsufficientCleanedContentError(ExtractionError):
    def __init__(self, character_count: int) -> None:
        super().__init__(
            ExtractionFailureKind.INSUFFICIENT_CONTENT,
            f"cleaned page content is insufficient ({character_count} characters)",
        )


class UngroundedExtractionError(ExtractionError):
    def __init__(self, message: str) -> None:
        super().__init__(ExtractionFailureKind.UNGROUNDED_OUTPUT, message)


class ExtractedEvidence(BaseModel):
    field: str
    value: str
    source_url: str | None = None
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    evidence_text: str


class OpportunityExtractionResult(BaseModel):
    title: str
    organization: str | None = None
    category: str = "Unknown"
    subcategories: list[str] = Field(default_factory=list)
    official_url: str | None = None
    application_url: str | None = None
    country: str | None = None
    city: str | None = None
    region: str | None = None
    participation_mode: ParticipationMode = ParticipationMode.UNKNOWN
    geographic_restrictions: list[str] = Field(default_factory=list)
    opening_date: date | None = None
    deadline: datetime | None = None
    deadline_timezone: str | None = None
    program_start_date: date | None = None
    program_end_date: date | None = None
    rolling_application: bool | None = None
    confirmed_accepting_applications: bool = False
    confirmed_opening_soon: bool = False
    confirmed_future_cycle: bool = False
    summary: str | None = None
    eligibility: EligibilityRequirements = Field(default_factory=EligibilityRequirements)
    funding: FundingDetails = Field(default_factory=FundingDetails)
    application: ApplicationRequirements = Field(default_factory=ApplicationRequirements)
    evidence: list[ExtractedEvidence] = Field(default_factory=list)
    extraction_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class FactualExtractionProvider(Protocol):
    def extract_facts(self, cleaned_text: str, *, page_url: str) -> OpportunityExtractionResult: ...


class FakeFactualExtractionProvider:
    def __init__(self, result: OpportunityExtractionResult | dict[str, Any] | Exception) -> None:
        self.result = result
        self.received_text: str | None = None
        self.received_page_url: str | None = None

    def extract_facts(self, cleaned_text: str, *, page_url: str) -> OpportunityExtractionResult:
        self.received_text = cleaned_text
        self.received_page_url = page_url
        if isinstance(self.result, Exception):
            raise self.result
        try:
            return OpportunityExtractionResult.model_validate(self.result)
        except ValidationError as exc:
            raise InvalidStructuredResponseError(str(exc)) from exc


EXTRACTION_INSTRUCTIONS = """
Extract factual opportunity information only from the supplied page text and its explicit links.
Return Unknown/null when a fact is absent or uncertain. Do not use outside knowledge or historical cycles.
Do not infer visa support from relocation language, international eligibility from missing restrictions,
funding from promotional language, or exact dates from patterns. Do not make personalized eligibility,
matching, priority, why-you, or recommended-action judgments. Evidence snippets must be short exact text
from the supplied page. Only return official/application URLs explicitly present in the page input.
Set eligibility.requirements_complete to true only for a reasonably self-contained eligibility section,
false when criteria are partial/ambiguous or deferred elsewhere, and null when completeness is uncertain.
Status is derived by deterministic code; only report the three explicit availability signals in the schema.
""".strip()


class OpenAIFactualExtractionProvider:
    def __init__(self, *, client: Any, model: str) -> None:
        if not model.strip():
            raise ExtractionConfigurationError("OPENAI_MODEL is required")
        self.client = client
        self.model = model

    @classmethod
    def from_environment(cls) -> OpenAIFactualExtractionProvider:
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")
        if not api_key:
            raise ExtractionConfigurationError("OPENAI_API_KEY is required")
        if not model:
            raise ExtractionConfigurationError("OPENAI_MODEL is required")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ExtractionConfigurationError("official OpenAI Python SDK is not installed") from exc
        return cls(client=OpenAI(api_key=api_key), model=model)

    def extract_facts(self, cleaned_text: str, *, page_url: str) -> OpportunityExtractionResult:
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=EXTRACTION_INSTRUCTIONS,
                input=f"Source URL: {page_url}\n\nSupplied page text:\n{cleaned_text}",
                text_format=OpportunityExtractionResult,
                store=False,
            )
        except Exception as exc:
            raise ExtractionProviderError(str(exc) or "OpenAI Responses API request failed") from exc
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise InvalidStructuredResponseError("OpenAI response did not contain parsed structured output")
        try:
            return OpportunityExtractionResult.model_validate(parsed)
        except ValidationError as exc:
            raise InvalidStructuredResponseError(str(exc)) from exc


_URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _explicit_page_urls(page: FetchedPage, semantic_text: str) -> set[str]:
    urls = {canonical_url(page.requested_url), canonical_url(page.final_url)}
    urls.update(canonical_url(match) for match in _URL_PATTERN.findall(semantic_text))
    return urls


def _grounded_url(value: str | None, *, page: FetchedPage, allowed_urls: set[str]) -> HttpUrl | None:
    if value is None:
        return None
    normalized = urljoin(page.final_url, value)
    if canonical_url(normalized) not in allowed_urls:
        raise UngroundedExtractionError(f"extracted URL was not present on supplied page: {normalized}")
    try:
        return HttpUrl(normalized)
    except ValidationError as exc:
        raise ExtractionSchemaError(f"invalid extracted URL: {normalized}") from exc


class SemanticOpportunityExtractor:
    def __init__(
        self,
        provider: FactualExtractionProvider,
        *,
        semantic_input_limit: int = DEFAULT_SEMANTIC_INPUT_LIMIT,
        minimum_cleaned_characters: int = DEFAULT_MINIMUM_CLEANED_CHARACTERS,
    ) -> None:
        if semantic_input_limit <= 0 or minimum_cleaned_characters <= 0:
            raise ValueError("semantic input limits must be positive")
        self.provider = provider
        self.semantic_input_limit = semantic_input_limit
        self.minimum_cleaned_characters = minimum_cleaned_characters

    def extract(self, page: FetchedPage) -> Opportunity:
        cleaned = page.cleaned_text.strip()
        if len(cleaned) < self.minimum_cleaned_characters:
            raise InsufficientCleanedContentError(len(cleaned))
        truncated = len(cleaned) > self.semantic_input_limit
        semantic_text = cleaned[: self.semantic_input_limit]
        try:
            result = self.provider.extract_facts(semantic_text, page_url=page.final_url)
        except ExtractionError:
            raise
        except ValidationError as exc:
            raise InvalidStructuredResponseError(str(exc)) from exc
        except Exception as exc:
            raise ExtractionProviderError(str(exc) or "semantic extraction provider failed") from exc

        allowed_urls = _explicit_page_urls(page, semantic_text)
        evidence: list[SourceEvidence] = []
        normalized_source = _normalized_text(semantic_text)
        for item in result.evidence:
            snippet = _normalized_text(item.evidence_text)
            if not snippet or snippet not in normalized_source:
                raise UngroundedExtractionError(
                    f"evidence for {item.field!r} is not grounded in supplied page text"
                )
            source_url = _grounded_url(
                item.source_url or page.final_url,
                page=page,
                allowed_urls=allowed_urls,
            )
            if source_url is None:  # pragma: no cover - fallback URL is always present
                raise ExtractionSchemaError("evidence source URL is missing")
            evidence.append(
                SourceEvidence(
                    field=item.field,
                    value=item.value,
                    source_url=source_url,
                    confidence=item.confidence,
                    evidence_text=item.evidence_text.strip(),
                )
            )

        try:
            return Opportunity(
                title=result.title.strip(),
                organization=result.organization,
                category=result.category or "Unknown",
                subcategories=result.subcategories,
                source_url=page.requested_url,
                official_url=_grounded_url(result.official_url, page=page, allowed_urls=allowed_urls),
                application_url=_grounded_url(result.application_url, page=page, allowed_urls=allowed_urls),
                status=derive_status(
                    deadline=result.deadline,
                    as_of=page.fetched_at,
                    confirmed_accepting=result.confirmed_accepting_applications,
                    confirmed_future_cycle=result.confirmed_future_cycle,
                    confirmed_opening_soon=result.confirmed_opening_soon,
                ),
                country=result.country,
                city=result.city,
                region=result.region,
                participation_mode=result.participation_mode,
                geographic_restrictions=result.geographic_restrictions,
                opening_date=result.opening_date,
                deadline=result.deadline,
                deadline_timezone=result.deadline_timezone,
                program_start_date=result.program_start_date,
                program_end_date=result.program_end_date,
                rolling_application=result.rolling_application,
                summary=result.summary,
                eligibility=result.eligibility,
                funding=result.funding,
                application=result.application,
                evidence=evidence,
                discovered_at=page.fetched_at,
                last_verified_at=page.fetched_at,
                extraction_confidence=result.extraction_confidence,
                semantic_input_truncated=truncated,
                semantic_input_character_count=len(cleaned),
                semantic_input_limit=self.semantic_input_limit,
            )
        except ValidationError as exc:
            raise ExtractionSchemaError(str(exc)) from exc

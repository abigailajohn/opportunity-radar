from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from opportunity_radar.extraction import (
    ExtractionConfigurationError,
    ExtractionProviderError,
    ExtractionSchemaError,
    FakeFactualExtractionProvider,
    InsufficientCleanedContentError,
    InvalidStructuredResponseError,
    OpenAIFactualExtractionProvider,
    OpportunityExtractionResult,
    SemanticOpportunityExtractor,
    UngroundedExtractionError,
)
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.models import (
    ApplicationRequirements,
    ConfidenceLevel,
    CoverageStatus,
    EligibilityRequirements,
    FundingComponent,
    FundingDetails,
    OpportunityStatus,
)
from opportunity_radar.pipeline import run_pipeline
from opportunity_radar.providers import FakePageFetcher, FakeSemanticAssessor
from conftest import make_judgment


FETCHED_AT = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


def make_page(*, url: str = "https://example.test/programme", cleaned_text: str | None = None) -> FetchedPage:
    text = cleaned_text or (
        "Security Fellowship\nApplicants must be undergraduate students.\n"
        "Applications close September 30, 2026.\n"
        "Apply now (https://example.test/apply)\n"
        "Flights are covered. No application fee is charged."
    )
    return FetchedPage(
        requested_url=url,
        final_url=url,
        raw_html=f"<html><body>{text}</body></html>",
        cleaned_text=text,
        fetched_at=FETCHED_AT,
        byte_count=len(text.encode()),
        character_count=len(text),
    )


def complete_result(**updates) -> OpportunityExtractionResult:
    data = {
        "title": "Security Fellowship",
        "organization": "Example Foundation",
        "category": "Fellowship",
        "application_url": "https://example.test/apply",
        "deadline": datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc),
        "confirmed_accepting_applications": True,
        "eligibility": EligibilityRequirements(
            requirements_complete=True,
            student_required=True,
            undergraduate_eligible=True,
            raw_text="Applicants must be undergraduate students.",
        ),
        "funding": FundingDetails(
            flights=FundingComponent(status=CoverageStatus.COVERED),
        ),
        "application": ApplicationRequirements(application_fee=False),
        "evidence": [
            {
                "field": "eligibility",
                "value": "Undergraduate students",
                "evidence_text": "Applicants must be undergraduate students.",
            },
            {
                "field": "deadline",
                "value": "2026-09-30T23:59:00Z",
                "evidence_text": "Applications close September 30, 2026.",
            },
            {
                "field": "funding.flights",
                "value": "covered",
                "evidence_text": "Flights are covered.",
            },
            {
                "field": "application.application_fee",
                "value": "false",
                "evidence_text": "No application fee is charged.",
            },
            {
                "field": "application_url",
                "value": "https://example.test/apply",
                "source_url": "https://example.test/apply",
                "evidence_text": "Apply now",
            },
        ],
        "extraction_confidence": ConfidenceLevel.HIGH,
    }
    data.update(updates)
    return OpportunityExtractionResult.model_validate(data)


def test_complete_opportunity_extraction() -> None:
    page = make_page()
    opportunity = SemanticOpportunityExtractor(
        FakeFactualExtractionProvider(complete_result())
    ).extract(page)
    assert opportunity.title == "Security Fellowship"
    assert opportunity.status is OpportunityStatus.OPEN
    assert str(opportunity.application_url) == "https://example.test/apply"
    assert opportunity.eligibility.requirements_complete is True
    assert opportunity.funding.flights.status is CoverageStatus.COVERED
    assert opportunity.discovered_at == FETCHED_AT
    assert not opportunity.semantic_input_truncated


def test_missing_optional_fields_and_unknown_category_are_valid() -> None:
    result = OpportunityExtractionResult(title="Unclassified opportunity", category="Unknown")
    opportunity = SemanticOpportunityExtractor(
        FakeFactualExtractionProvider(result)
    ).extract(make_page())
    assert opportunity.category == "Unknown"
    assert opportunity.organization is None
    assert opportunity.deadline is None
    assert opportunity.status is OpportunityStatus.UNKNOWN


@pytest.mark.parametrize("complete", [True, False, None])
def test_requirements_complete_round_trips(complete: bool | None) -> None:
    result = OpportunityExtractionResult(
        title="Programme",
        eligibility=EligibilityRequirements(requirements_complete=complete),
    )
    opportunity = SemanticOpportunityExtractor(
        FakeFactualExtractionProvider(result)
    ).extract(make_page())
    assert opportunity.eligibility.requirements_complete is complete


def test_required_evidence_validation_rejects_known_fact_without_evidence() -> None:
    result = complete_result(evidence=[])
    with pytest.raises(ExtractionSchemaError, match="SourceEvidence"):
        SemanticOpportunityExtractor(FakeFactualExtractionProvider(result)).extract(make_page())


def test_ungrounded_url_and_evidence_are_rejected() -> None:
    with pytest.raises(UngroundedExtractionError, match="URL"):
        SemanticOpportunityExtractor(
            FakeFactualExtractionProvider(
                OpportunityExtractionResult(
                    title="Programme",
                    official_url="https://hallucinated.test/programme",
                )
            )
        ).extract(make_page())

    with pytest.raises(UngroundedExtractionError, match="evidence"):
        SemanticOpportunityExtractor(
            FakeFactualExtractionProvider(
                OpportunityExtractionResult(
                    title="Programme",
                    evidence=[
                        {
                            "field": "summary",
                            "value": "invented",
                            "evidence_text": "This sentence does not exist on the page.",
                        }
                    ],
                )
            )
        ).extract(make_page())


def test_malformed_semantic_output_is_typed() -> None:
    provider = FakeFactualExtractionProvider({"organization": "Missing title"})
    with pytest.raises(InvalidStructuredResponseError):
        SemanticOpportunityExtractor(provider).extract(make_page())


def test_oversized_cleaned_text_is_truncated_and_recorded() -> None:
    page = make_page(cleaned_text="A" * 120)
    provider = FakeFactualExtractionProvider(OpportunityExtractionResult(title="Programme"))
    opportunity = SemanticOpportunityExtractor(
        provider,
        semantic_input_limit=100,
        minimum_cleaned_characters=1,
    ).extract(page)
    assert provider.received_text == "A" * 100
    assert opportunity.semantic_input_truncated
    assert opportunity.semantic_input_character_count == 120
    assert opportunity.semantic_input_limit == 100


def test_insufficient_cleaned_content_is_typed() -> None:
    with pytest.raises(InsufficientCleanedContentError):
        SemanticOpportunityExtractor(
            FakeFactualExtractionProvider(OpportunityExtractionResult(title="Programme"))
        ).extract(make_page(cleaned_text="short"))


@pytest.mark.parametrize(
    ("deadline", "accepting", "opening", "future", "expected"),
    [
        (FETCHED_AT - timedelta(days=1), True, False, False, OpportunityStatus.CLOSED),
        (FETCHED_AT + timedelta(days=5), True, False, False, OpportunityStatus.CLOSING_SOON),
        (FETCHED_AT + timedelta(days=20), True, False, False, OpportunityStatus.OPEN),
        (None, False, True, False, OpportunityStatus.OPENING_SOON),
        (None, False, False, True, OpportunityStatus.FUTURE_CYCLE),
        (None, False, False, False, OpportunityStatus.UNKNOWN),
    ],
)
def test_deterministic_status_post_processing(deadline, accepting, opening, future, expected) -> None:
    result = OpportunityExtractionResult(
        title="Programme",
        deadline=deadline,
        confirmed_accepting_applications=accepting,
        confirmed_opening_soon=opening,
        confirmed_future_cycle=future,
        evidence=(
            [{"field": "deadline", "value": deadline.isoformat(), "evidence_text": "Applications close"}]
            if deadline
            else []
        ),
    )
    page = make_page(cleaned_text="Applications close. This page contains enough factual text for extraction.")
    opportunity = SemanticOpportunityExtractor(
        FakeFactualExtractionProvider(result)
    ).extract(page)
    assert opportunity.status is expected


class MappingProvider:
    def __init__(self, results):
        self.results = results

    def extract_facts(self, cleaned_text: str, *, page_url: str):
        del cleaned_text
        result = self.results[page_url]
        if isinstance(result, Exception):
            raise result
        return result


def test_provider_failure_isolated_as_extract_stage(profile) -> None:
    good_url = "https://example.test/good"
    bad_url = "https://example.test/bad"
    pages = {good_url: make_page(url=good_url), bad_url: make_page(url=bad_url)}
    provider = MappingProvider(
        {
            good_url: OpportunityExtractionResult(title="Good programme"),
            bad_url: ExtractionProviderError("provider unavailable"),
        }
    )
    extractor = SemanticOpportunityExtractor(provider)
    result = run_pipeline(
        [bad_url, good_url],
        profile,
        FakePageFetcher(pages),
        extractor,
        FakeSemanticAssessor({good_url: make_judgment()}),
        as_of=date(2026, 8, 27),
    )
    assert len(result.opportunities) == 1
    assert len(result.assessments) == 1
    assert result.failures[0].stage.value == "extract"
    assert "provider unavailable" in result.failures[0].reason


def test_openai_environment_configuration_is_required(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ExtractionConfigurationError, match="OPENAI_API_KEY"):
        OpenAIFactualExtractionProvider.from_environment()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    with pytest.raises(ExtractionConfigurationError, match="OPENAI_MODEL"):
        OpenAIFactualExtractionProvider.from_environment()


def test_openai_provider_uses_responses_parse_without_live_call() -> None:
    captured = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=OpportunityExtractionResult(title="Programme"))

    provider = OpenAIFactualExtractionProvider(
        client=SimpleNamespace(responses=FakeResponses()),
        model="test-model",
    )
    result = provider.extract_facts("Page text", page_url="https://example.test/page")
    assert result.title == "Programme"
    assert captured["model"] == "test-model"
    assert captured["text_format"] is OpportunityExtractionResult
    assert captured["store"] is False


def test_openai_api_and_invalid_response_failures_are_typed() -> None:
    class FailingResponses:
        def parse(self, **kwargs):
            del kwargs
            raise RuntimeError("network unavailable")

    provider = OpenAIFactualExtractionProvider(
        client=SimpleNamespace(responses=FailingResponses()),
        model="test-model",
    )
    with pytest.raises(ExtractionProviderError, match="network unavailable"):
        provider.extract_facts("Page text", page_url="https://example.test/page")

    provider = OpenAIFactualExtractionProvider(
        client=SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: SimpleNamespace(output_parsed=None))),
        model="test-model",
    )
    with pytest.raises(InvalidStructuredResponseError):
        provider.extract_facts("Page text", page_url="https://example.test/page")

from datetime import date
import json

import pytest

from opportunity_radar.models import EligibilityRequirements, Opportunity, OpportunityAssessment, ProcessingFailure
from opportunity_radar.pipeline import InputValidationError, load_url_file, run_pipeline, write_outputs
from opportunity_radar.providers import (
    FakeOpportunityExtractor,
    FakePageFetcher,
    FakeSemanticAssessor,
)
from opportunity_radar.fetching import FetchedPage
from conftest import make_judgment, make_opportunity


def test_url_file_contract(tmp_path) -> None:
    path = tmp_path / "urls.txt"
    urls = [f"https://example.com/{index}" for index in range(9)] + ["\n", "https://example.com/9"]
    path.write_text("\n".join(urls), encoding="utf-8")
    assert len(load_url_file(path)) == 10
    path.write_text("\n".join(urls[:-1]), encoding="utf-8")
    with pytest.raises(InputValidationError, match="exactly 10"):
        load_url_file(path)
    path.write_text("\n".join([*urls[:-1], "ftp://example.com"]), encoding="utf-8")
    with pytest.raises(InputValidationError, match="HTTP/HTTPS"):
        load_url_file(path)


def test_pipeline_isolates_errors_deduplicates_and_serializes(tmp_path, profile) -> None:
    urls = [f"https://input.example/{index}" for index in range(10)]
    pages = {
        url: FetchedPage(url, f"https://page.example/{index}", f"page {index}")
        for index, url in enumerate(urls)
    }
    pages[urls[9]] = RuntimeError("blocked")
    opportunities = {}
    judgments = {}
    for index in range(9):
        final_url = f"https://page.example/{index}"
        opportunity = make_opportunity(
            title="Duplicate Fellowship 2026" if index < 2 else f"Programme {index} 2026",
            url=final_url,
            eligibility=EligibilityRequirements(student_required=True, undergraduate_eligible=True),
        )
        opportunities[final_url] = opportunity
        judgments[str(opportunity.source_url)] = make_judgment()
    opportunities["https://page.example/8"] = ValueError("cannot parse")
    result = run_pipeline(
        urls,
        profile,
        FakePageFetcher(pages),
        FakeOpportunityExtractor(opportunities),
        FakeSemanticAssessor(judgments),
        as_of=date(2026, 8, 27),
    )
    assert result.fetched_count == 9
    assert len(result.opportunities) == 7
    assert len(result.assessments) == 7
    assert {failure.stage.value for failure in result.failures} == {"fetch", "extract"}
    write_outputs(result, tmp_path)
    raw_opportunities = json.loads((tmp_path / "opportunities.json").read_text(encoding="utf-8"))
    raw_assessments = json.loads((tmp_path / "assessments.json").read_text(encoding="utf-8"))
    raw_failures = json.loads((tmp_path / "failures.json").read_text(encoding="utf-8"))
    assert len([Opportunity.model_validate(item) for item in raw_opportunities]) == 7
    assert len([OpportunityAssessment.model_validate(item) for item in raw_assessments]) == 7
    assert len([ProcessingFailure.model_validate(item) for item in raw_failures]) == 2
    assert (tmp_path / "digest.md").read_text(encoding="utf-8").startswith("# Opportunity Radar Digest")


def test_semantic_failure_is_per_url(profile) -> None:
    urls = ["https://input.example/one"]
    page = FetchedPage(urls[0], "https://page.example/one", "page")
    opportunity = make_opportunity(url=page.final_url)
    result = run_pipeline(
        urls,
        profile,
        FakePageFetcher({urls[0]: page}),
        FakeOpportunityExtractor({page.final_url: opportunity}),
        FakeSemanticAssessor({str(opportunity.source_url): RuntimeError("semantic unavailable")}),
        as_of=date(2026, 8, 27),
    )
    assert not result.assessments
    assert result.failures[0].stage.value == "evaluate"
    assert "semantic unavailable" in result.failures[0].reason

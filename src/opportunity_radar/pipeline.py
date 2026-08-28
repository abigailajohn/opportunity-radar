from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter, ValidationError

from opportunity_radar.deduplication import deduplicate
from opportunity_radar.digest import DigestResult, render_digest
from opportunity_radar.evaluation import evaluate_opportunity
from opportunity_radar.models import (
    Opportunity,
    OpportunityAssessment,
    OpportunityProfile,
    ProcessingFailure,
    ProcessingStage,
)
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor


class InputValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PipelineResult:
    input_count: int
    fetched_count: int
    opportunities: list[Opportunity]
    assessments: list[OpportunityAssessment]
    failures: list[ProcessingFailure]
    digest: DigestResult


def load_url_file(path: str | Path) -> list[str]:
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 10:
        raise InputValidationError(f"expected exactly 10 nonblank URLs, found {len(lines)}")
    adapter = TypeAdapter(HttpUrl)
    validated: list[str] = []
    for line_number, value in enumerate(lines, start=1):
        try:
            url = adapter.validate_python(value)
        except ValidationError as exc:
            raise InputValidationError(f"line {line_number} is not a valid HTTP/HTTPS URL") from exc
        if url.scheme not in {"http", "https"}:
            raise InputValidationError(f"line {line_number} is not a valid HTTP/HTTPS URL")
        validated.append(value)
    return validated


def run_pipeline(
    urls: list[str],
    profile: OpportunityProfile,
    fetcher: PageFetcher,
    extractor: OpportunityExtractor,
    assessor: SemanticAssessor,
    *,
    as_of: date,
    opportunity_transform: Callable[[Opportunity], Opportunity] | None = None,
) -> PipelineResult:
    extracted: list[Opportunity] = []
    failures: list[ProcessingFailure] = []
    fetched_count = 0
    for url in urls:
        try:
            page = fetcher.fetch(url)
            fetched_count += 1
        except Exception as exc:
            failures.append(ProcessingFailure(url=url, stage=ProcessingStage.FETCH, reason=str(exc)))
            continue
        try:
            opportunity = extractor.extract(page)
            extracted.append(opportunity_transform(opportunity) if opportunity_transform else opportunity)
        except Exception as exc:
            failures.append(ProcessingFailure(url=url, stage=ProcessingStage.EXTRACT, reason=str(exc)))

    opportunities = deduplicate(extracted)
    assessments: list[OpportunityAssessment] = []
    for opportunity in opportunities:
        try:
            assessments.append(
                evaluate_opportunity(opportunity, profile, assessor, as_of=as_of)
            )
        except Exception as exc:
            failures.append(
                ProcessingFailure(
                    url=str(opportunity.source_url),
                    stage=ProcessingStage.EVALUATE,
                    reason=str(exc),
                )
            )
    digest = render_digest(opportunities, assessments)
    return PipelineResult(len(urls), fetched_count, opportunities, assessments, failures, digest)


def write_outputs(result: PipelineResult, output_dir: str | Path) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {
        "opportunities.json": json.dumps(
            [item.model_dump(mode="json") for item in result.opportunities], indent=2
        ),
        "assessments.json": json.dumps(
            [item.model_dump(mode="json") for item in result.assessments], indent=2
        ),
        "failures.json": json.dumps(
            [item.model_dump(mode="json") for item in result.failures], indent=2
        ),
        "digest.md": result.digest.markdown,
    }
    for filename, content in payloads.items():
        try:
            (destination / filename).write_text(content + "\n", encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not serialize {filename}: {exc}") from exc

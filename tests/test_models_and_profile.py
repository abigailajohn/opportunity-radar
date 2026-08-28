from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opportunity_radar.models import Opportunity


def test_profile_loads_structured_values_and_date_precedence(profile) -> None:
    assert profile.identity.age.current == 21
    assert profile.education.year_2_start_date.isoformat() == "2026-10-26"
    assert profile.experience[0].organization == "Klas"


def test_opportunity_defaults_unknown_without_personalized_fields() -> None:
    now = datetime.now(timezone.utc)
    opportunity = Opportunity(
        title="Uncategorized programme",
        source_url="https://example.com/item",
        discovered_at=now,
        last_verified_at=now,
    )
    data = opportunity.model_dump(mode="json")
    assert data["category"] == "Unknown"
    assert data["funding"]["visa_support"]["status"] == "unknown"
    assert "why_you" not in data
    assert "priority_band" not in data


def test_known_application_url_requires_evidence() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="application URL"):
        Opportunity(
            title="Programme",
            source_url="https://example.com/item",
            application_url="https://example.com/apply",
            discovered_at=now,
            last_verified_at=now,
        )

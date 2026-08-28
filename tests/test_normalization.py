from datetime import datetime, timedelta, timezone

from opportunity_radar.models import OpportunityStatus
from opportunity_radar.normalization import derive_status


def test_status_rules() -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert derive_status(deadline=now - timedelta(seconds=1), as_of=now, confirmed_accepting=True) is OpportunityStatus.CLOSED
    assert derive_status(deadline=now + timedelta(days=7), as_of=now, confirmed_accepting=True) is OpportunityStatus.CLOSING_SOON
    assert derive_status(deadline=now + timedelta(days=8), as_of=now, confirmed_accepting=True) is OpportunityStatus.OPEN
    assert derive_status(deadline=None, as_of=now, confirmed_opening_soon=True) is OpportunityStatus.OPENING_SOON
    assert derive_status(deadline=None, as_of=now, confirmed_future_cycle=True) is OpportunityStatus.FUTURE_CYCLE
    assert derive_status(deadline=None, as_of=now) is OpportunityStatus.UNKNOWN

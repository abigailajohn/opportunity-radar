from datetime import date, datetime

from opportunity_radar.models import OpportunityStatus


def derive_status(
    *,
    deadline: datetime | None,
    as_of: datetime,
    opening_date: date | None = None,
    rolling_application: bool = False,
    confirmed_accepting: bool = False,
    confirmed_future_cycle: bool = False,
    confirmed_opening_soon: bool = False,
) -> OpportunityStatus:
    if deadline is not None:
        normalized_deadline = deadline
        normalized_as_of = as_of
        if deadline.tzinfo is None and as_of.tzinfo is not None:
            normalized_as_of = as_of.replace(tzinfo=None)
        elif deadline.tzinfo is not None and as_of.tzinfo is None:
            normalized_as_of = as_of.replace(tzinfo=deadline.tzinfo)
        if normalized_deadline < normalized_as_of:
            return OpportunityStatus.CLOSED
        if (normalized_deadline - normalized_as_of).total_seconds() <= 7 * 86400:
            return OpportunityStatus.CLOSING_SOON
        if opening_date is not None:
            if opening_date > normalized_as_of.date():
                return OpportunityStatus.FUTURE_CYCLE if opening_date.year > normalized_as_of.year else OpportunityStatus.OPENING_SOON
            return OpportunityStatus.OPEN
    if opening_date is not None:
        if opening_date > as_of.date():
            return OpportunityStatus.FUTURE_CYCLE if opening_date.year > as_of.year else OpportunityStatus.OPENING_SOON
        if rolling_application or confirmed_accepting:
            return OpportunityStatus.OPEN
    if rolling_application:
        return OpportunityStatus.OPEN
    if confirmed_accepting:
        return OpportunityStatus.OPEN
    if confirmed_opening_soon:
        return OpportunityStatus.OPENING_SOON
    if confirmed_future_cycle:
        return OpportunityStatus.FUTURE_CYCLE
    return OpportunityStatus.UNKNOWN

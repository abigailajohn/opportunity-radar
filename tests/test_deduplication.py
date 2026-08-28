from opportunity_radar.deduplication import deduplicate
from conftest import make_opportunity


def test_same_programme_cycle_deduplicates_and_retains_source() -> None:
    first = make_opportunity(url="https://example.com/programme?utm_source=newsletter")
    second = make_opportunity(url="https://partner.example.org/programme")
    result = deduplicate([first, second])
    assert len(result) == 1
    assert any(item.field == "duplicate_source_url" for item in result[0].evidence)


def test_different_cycles_are_not_duplicates() -> None:
    first = make_opportunity(title="Security Fellowship 2026")
    second = make_opportunity(title="Security Fellowship 2027", url="https://example.com/2027")
    assert len(deduplicate([first, second])) == 2

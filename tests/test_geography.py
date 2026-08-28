import pytest

from opportunity_radar.geography import location_matches_restrictions


@pytest.mark.parametrize(
    ("country", "scope"),
    [
        ("Nigeria", "Africa"),
        ("Mauritius", "Africa"),
        ("France", "Europe"),
        ("France", "EU"),
        ("France", "EMEA"),
        ("Nigeria", "EMEA"),
        ("Singapore", "APAC"),
    ],
)
def test_country_satisfies_region(country, scope) -> None:
    assert location_matches_restrictions([country], [scope])


@pytest.mark.parametrize("scope", ["Global", "Worldwide", "International", "Remote"])
def test_open_scopes_match(scope) -> None:
    assert location_matches_restrictions(["Mauritius"], [scope])


def test_unrelated_country_does_not_match_and_emea_is_not_eu() -> None:
    assert not location_matches_restrictions(["Mauritius"], ["Canada"])
    assert not location_matches_restrictions(["Nigeria"], ["EU"])

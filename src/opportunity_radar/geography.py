from __future__ import annotations


OPEN_SCOPES = frozenset({"global", "worldwide", "international", "remote"})

AFRICA = frozenset(
    {
        "algeria", "angola", "benin", "botswana", "burkina faso", "burundi",
        "cabo verde", "cameroon", "central african republic", "chad", "comoros",
        "democratic republic of the congo", "djibouti", "egypt", "equatorial guinea",
        "eritrea", "eswatini", "ethiopia", "gabon", "gambia", "ghana", "guinea",
        "guinea bissau", "ivory coast", "kenya", "lesotho", "liberia", "libya",
        "madagascar", "malawi", "mali", "mauritania", "mauritius", "morocco",
        "mozambique", "namibia", "niger", "nigeria", "republic of the congo",
        "rwanda", "sao tome and principe", "senegal", "seychelles", "sierra leone",
        "somalia", "south africa", "south sudan", "sudan", "tanzania", "togo",
        "tunisia", "uganda", "zambia", "zimbabwe",
    }
)
EU = frozenset(
    {
        "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "denmark",
        "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
        "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands",
        "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
    }
)
EUROPE = EU | frozenset(
    {
        "albania", "andorra", "belarus", "bosnia and herzegovina", "iceland",
        "kosovo", "liechtenstein", "moldova", "monaco", "montenegro", "north macedonia",
        "norway", "san marino", "serbia", "switzerland", "ukraine", "united kingdom",
        "vatican city",
    }
)
MIDDLE_EAST = frozenset(
    {
        "bahrain", "iran", "iraq", "israel", "jordan", "kuwait", "lebanon", "oman",
        "palestine", "qatar", "saudi arabia", "syria", "turkey", "united arab emirates",
        "yemen",
    }
)
APAC = frozenset(
    {
        "australia", "bangladesh", "bhutan", "brunei", "cambodia", "china", "fiji",
        "india", "indonesia", "japan", "laos", "malaysia", "maldives", "mongolia",
        "myanmar", "nepal", "new zealand", "pakistan", "papua new guinea", "philippines",
        "singapore", "south korea", "sri lanka", "taiwan", "thailand", "timor leste",
        "vietnam",
    }
)
NORTH_AMERICA = frozenset({"canada", "mexico", "united states", "united states of america"})

REGION_COUNTRIES = {
    "africa": AFRICA,
    "mea": AFRICA | MIDDLE_EAST,
    "emea": EUROPE | AFRICA | MIDDLE_EAST,
    "europe": EUROPE,
    "eu": EU,
    "apac": APAC,
    "north america": NORTH_AMERICA,
}

ALIASES = {
    "u s": "united states",
    "u s a": "united states",
    "usa": "united states",
    "uk": "united kingdom",
    "cote d ivoire": "ivory coast",
    "nigerian": "nigeria",
    "mauritian": "mauritius",
    "french": "france",
}


def normalize_geography(value: str) -> str:
    normalized = " ".join("".join(character if character.isalnum() else " " for character in value.casefold()).split())
    return ALIASES.get(normalized, normalized)


def location_matches_restrictions(locations: list[str], restrictions: list[str]) -> bool:
    normalized_restrictions = {normalize_geography(value) for value in restrictions}
    if normalized_restrictions.intersection(OPEN_SCOPES):
        return True
    normalized_locations = {normalize_geography(value) for value in locations}
    for restriction in normalized_restrictions:
        if restriction in normalized_locations:
            return True
        countries = REGION_COUNTRIES.get(restriction)
        if countries and normalized_locations.intersection(countries):
            return True
    return False

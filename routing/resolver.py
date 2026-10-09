"""Offline `City, ST` resolution; `normalize` is shared with the station ETL."""

import re
import unicodedata
from dataclasses import dataclass

_ABBR = {"st": "saint", "mt": "mount", "ft": "fort"}
ALIASES = {"new york city": "new york", "nyc": "new york", "washington dc": "washington"}
_COUNTRY = {"USA", "US", "UNITED STATES"}
_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington state": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_ABBRS = set(_STATE_NAMES.values())


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _state(text: str) -> str | None:
    """`TX`, `tx 75201`, `Texas`, `D.C.` -> state code (a trailing ZIP code is ignored)."""
    words = [w.replace(".", "") for w in text.strip().split() if not w[:1].isdigit()]
    if len(words) == 1 and words[0].upper() in _ABBRS:
        return words[0].upper()
    key = " ".join(words).lower()
    return _STATE_NAMES.get(key) or ("WA" if key == "washington" else None)


def normalize(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'", "").replace("&", " and "))
    return " ".join(_ABBR.get(w, w) for w in s.split())


def parse_place(text: str) -> tuple[str, str, bool] | None:
    """Return (normalised city, STATE, had_street_part) for text ending in a city and a state."""
    parts = [p.strip() for p in _ascii(text).split(",") if p.strip()]
    if parts and parts[-1].upper() in _COUNTRY:
        parts.pop()
    if len(parts) >= 2:  # "City, ST", "Dallas, Texas", or an address ending in "..., City, ST"
        city, state, street = parts[-2], _state(parts[-1]), len(parts) > 2
    elif len(parts) == 1:  # "Dallas TX", "Dallas Texas"
        words = parts[0].split()
        city = state = None
        street = False
        for n in (3, 2, 1):  # longest state name first ("New Mexico", "North Carolina")
            if len(words) > n and (state := _state(" ".join(words[-n:]))):
                city = " ".join(words[:-n])
                break
    else:
        return None
    if not city or not state:
        return None
    city = normalize(city)
    return ALIASES.get(city, city), state, street


@dataclass(frozen=True, slots=True)
class Resolved:
    """A start/finish point and how precisely it was located."""

    lat: float
    lng: float
    precision: str  # "coordinates" (as given) or "city_centroid" (looked up from text)
    matched: str | None = None
    note: str | None = None


class Places:
    def __init__(self, table: dict[str, tuple[float, float]]):
        self.table = table

    def resolve(self, text: str) -> Resolved | None:
        parsed = parse_place(text)
        if parsed is None:
            return None
        city, state, street = parsed
        hit = self.table.get(f"{state}|{city}")
        if hit is None:
            return None
        matched = f"{city.title()}, {state}"
        note = (
            f"Street address ignored: located at the centre of {matched} (city-centroid precision)"
            if street
            else None
        )
        return Resolved(hit[0], hit[1], "city_centroid", matched, note)

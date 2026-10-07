"""Offline `City, ST` resolution; `normalize` is shared with the station ETL."""

import re

_ABBR = {"st": "saint", "mt": "mount", "ft": "fort"}
ALIASES = {"new york city": "new york", "nyc": "new york", "washington dc": "washington"}
_COUNTRY = {"USA", "US", "UNITED STATES"}


def normalize(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'", "").replace("&", " and "))
    return " ".join(_ABBR.get(w, w) for w in s.split())


def parse_place(text: str) -> tuple[str, str] | None:
    """Return (normalised city, STATE) for `City, ST` input, else None."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts and parts[-1].upper() in _COUNTRY:
        parts.pop()
    if len(parts) != 2:
        return None
    state = parts[1].split()[0].upper() if parts[1] else ""
    if len(state) != 2 or not state.isalpha():
        return None
    city = normalize(parts[0])
    return ALIASES.get(city, city), state


class Places:
    def __init__(self, table: dict[str, tuple[float, float]]):
        self.table = table

    def resolve(self, text: str) -> tuple[float, float] | None:
        parsed = parse_place(text)
        if parsed is None:
            return None
        hit = self.table.get(f"{parsed[1]}|{parsed[0]}")
        return (hit[0], hit[1]) if hit else None

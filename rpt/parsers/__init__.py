"""Per-location parser registry."""
from __future__ import annotations

from .base import ParseResult, Parser
from . import calatagan, generic, pasig, quezon_city, rizal, san_juan

# Dropdown order requested by the user.
LOCATIONS: list[str] = [
    "Quezon City",
    "San Juan City",
    "Mandaluyong City",
    "Calatagan",
    "Pasig City",
    "Rizal",
    "Pampanga",
]

_PARSERS: dict[str, Parser] = {
    "Quezon City": quezon_city.QuezonCityParser(),
    "San Juan City": san_juan.SanJuanParser(),
    "Calatagan": calatagan.CalataganParser(),
    "Pasig City": pasig.PasigParser(),
    "Rizal": rizal.RizalParser(),
    "Mandaluyong City": generic.GenericParser("Mandaluyong City"),
    "Pampanga": generic.GenericParser("Pampanga"),
}


def get_parser(location: str) -> Parser:
    try:
        return _PARSERS[location]
    except KeyError:
        raise ValueError(f"Unknown location: {location!r}")


__all__ = ["LOCATIONS", "get_parser", "Parser", "ParseResult"]

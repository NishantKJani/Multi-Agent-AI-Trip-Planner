"""Mock catalog tools and tool schemas. Live flight/hotel handlers are bound per agent."""
from mock_data import CATALOG


def search_flights(destination: str, max_price: float = 1e9) -> list[dict]:
    rows = CATALOG.get(destination, {}).get("flights", [])
    return [dict(airline=a, price=p, stops=s) for a, p, s in rows if p <= max_price]


def search_hotels(destination: str, max_nightly: float = 1e9) -> list[dict]:
    rows = CATALOG.get(destination, {}).get("hotels", [])
    return [dict(name=n, area=a, nightly_price=p, rating=r) for n, a, p, r in rows if p <= max_nightly]


def search_activities(destination: str, interest: str = "") -> list[dict]:
    rows = CATALOG.get(destination, {}).get("activities", [])
    return [dict(name=n, cost=c, tag=t) for n, c, t in rows if not interest or t == interest]


TOOL_SPECS = {
    "search_flights": {
        "name": "search_flights", "description": "List flight options for a destination, optionally under a max price.",
        "input_schema": {"type": "object", "properties": {"destination": {"type": "string"}, "max_price": {"type": "number"}}, "required": ["destination"]}},
    "search_hotels": {
        "name": "search_hotels", "description": "List hotels for a destination, optionally under a max nightly price.",
        "input_schema": {"type": "object", "properties": {"destination": {"type": "string"}, "max_nightly": {"type": "number"}}, "required": ["destination"]}},
    "search_activities": {
        "name": "search_activities", "description": "List activities for a destination, optionally filtered by interest tag (culture, food, art, tech, nature, nightlife).",
        "input_schema": {"type": "object", "properties": {"destination": {"type": "string"}, "interest": {"type": "string"}}, "required": ["destination"]}},
}
HANDLERS = {"search_flights": search_flights, "search_hotels": search_hotels, "search_activities": search_activities}

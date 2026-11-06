"""Dated SerpAPI searches. Offers stay isolated to one agent and one planning run."""
import json
import math
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from pydantic import BaseModel
from schemas import ActivityOffer, Constraints, FlightPlan, HotelPlan


def activities_search_url(destination: str) -> str:
    """Google search page with the full list of things to do (the 'View more activities' target)."""
    return "https://www.google.com/search?" + urlencode({"q": f"top things to do in {destination}", "hl": "en", "gl": "us"})


class SearchError(RuntimeError):
    pass


def _price(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value > 0:
            return float(value)
    return None


def top_offers(offers: list[dict], selected: BaseModel, cost, rank, cap: float, n: int = 3) -> list[dict]:
    """The agent's pick first, then the best alternatives from the same search results.
    Alternatives within the budget cap are preferred; `rank` orders them best-first."""
    chosen = selected.model_dump(mode="json", exclude={"rationale"})
    rest = [o for o in offers if o != chosen]
    affordable = [o for o in rest if cost(o) <= cap]
    over_cap = [o for o in rest if cost(o) > cap]
    return [chosen, *(sorted(affordable, key=rank) + sorted(over_cap, key=rank))[: n - 1]]


class LiveSearch:
    def __init__(self, api_key: str, constraints: Constraints, trace, agent: str):
        if not api_key.strip():
            raise ValueError("A SerpAPI key is required for live searches.")
        if constraints.departure_date is None or constraints.return_date is None:
            raise ValueError("Live searches require a departure date and a return date.")
        self.api_key = api_key
        self.constraints = constraints
        self.trace = trace
        self.agent = agent
        self.offers: list[dict] | None = None
        self.searched = False
        self.returned: list[dict] = []

    def _request(self, engine: str, **params) -> dict:
        query = {"engine": engine, "api_key": self.api_key, "hl": "en", "gl": "us", **params}
        self.trace.log(self.agent, "live_search", f"{engine} search")
        try:
            with urlopen("https://serpapi.com/search.json?" + urlencode(query), timeout=45) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise SearchError(f"SerpAPI HTTP {exc.code}; check credentials, quota and search inputs.") from None
        except (URLError, TimeoutError, OSError):
            raise SearchError("SerpAPI connection failed or timed out.") from None
        except (ValueError, UnicodeError):
            raise SearchError("SerpAPI returned invalid JSON.") from None
        if not isinstance(data, dict):
            raise SearchError("SerpAPI returned an unexpected response shape.")
        metadata = data.get("search_metadata", {})
        if not isinstance(metadata, dict):
            raise SearchError("SerpAPI returned invalid search metadata.")
        if data.get("error") or metadata.get("status") == "Error":
            # Provider messages may contain a request URL and API key.
            raise SearchError("SerpAPI search failed; check credentials, quota and search inputs.")
        return data

    def _source(self, data: dict, key: str, fallback: str) -> str:
        url = data.get("search_metadata", {}).get(key)
        if not isinstance(url, str) or not url.startswith("https://www.google.com/"):
            return fallback
        return url

    def search_flights(self, destination: str, max_price: float = 1e9) -> list[dict]:
        self._check_destination(destination, self.constraints.arrival_airport)
        if self.offers is None:
            c = self.constraints
            assert c.departure_date is not None and c.return_date is not None
            data = self._request("google_flights", departure_id=c.departure_airport,
                                 arrival_id=c.arrival_airport, type=1, travel_class=1, currency="USD", adults=1,
                                 outbound_date=c.departure_date.isoformat(),
                                 return_date=c.return_date.isoformat())
            source = self._source(data, "google_flights_url", "https://www.google.com/travel/flights?" + urlencode({
                "q": f"{c.departure_airport} to {c.arrival_airport} {c.departure_date} return {c.return_date}"}))
            fetched = datetime.now(timezone.utc)
            offers = []
            rows = self._rows(data, "best_flights") + self._rows(data, "other_flights")
            for row in rows:
                price = _price(row.get("price"))
                legs = row.get("flights", [])
                if not isinstance(legs, list) or not all(isinstance(leg, dict) for leg in legs):
                    raise SearchError("SerpAPI returned invalid flight segments.")
                airlines = list(dict.fromkeys(leg.get("airline") for leg in legs if leg.get("airline")))
                if price is None or not legs or not airlines:
                    continue
                offer = FlightPlan(airline=" / ".join(airlines), price=price, stops=len(legs) - 1,
                                   rationale="", source_url=source, fetched_at=fetched)
                offers.append(offer.model_dump(mode="json", exclude={"rationale"}))
            self._save(offers, len(rows))
        return self._filter("price", max_price)

    def search_hotels(self, destination: str, max_nightly: float = 1e9) -> list[dict]:
        self._check_destination(destination)
        if self.offers is None:
            c = self.constraints
            assert c.departure_date is not None and c.return_date is not None
            data = self._request("google_hotels", q=f"hotels in {c.destination}", currency="USD", adults=1,
                                 check_in_date=c.departure_date.isoformat(),
                                 check_out_date=c.return_date.isoformat())
            # SerpAPI's google_hotels_url omits the dates, so build a dated link instead.
            source = "https://www.google.com/travel/search?" + urlencode({
                "q": f"hotels in {c.destination} {c.departure_date} to {c.return_date}", "hl": "en", "gl": "us"})
            fetched = datetime.now(timezone.utc)
            offers = []
            rows = self._rows(data, "properties")
            for row in rows:
                if row.get("type") != "hotel":
                    continue
                total = _price(row.get("total_rate", {}).get("extracted_lowest"))
                nightly = _price(row.get("rate_per_night", {}).get("extracted_lowest"))
                if not row.get("name") or (total is None and nightly is None):
                    continue
                # Prefer the whole-stay quote over a rounded nightly display rate.
                if total is not None:
                    nightly = round(total / c.nights, 2)
                offer = HotelPlan(name=row["name"], area=c.destination, nightly_price=nightly,
                                  total_price=total, rating=row.get("overall_rating"),
                                  rationale="", source_url=source, fetched_at=fetched)
                offers.append(offer.model_dump(mode="json", exclude={"rationale"}))
            self._save(offers, len(rows))
        return self._filter("nightly_price", max_nightly)

    def search_activities(self, destination: str) -> list[dict]:
        """Top things to do from Google Maps results (no prices). Returns a trimmed view for the model."""
        self._check_destination(destination)
        if self.offers is None:
            c = self.constraints
            interests = f"{', '.join(c.interests)} " if c.interests else ""
            data = self._request("google_maps", type="search", q=f"top {interests}things to do in {c.destination}")
            fetched = datetime.now(timezone.utc)
            rows = self._rows(data, "local_results")
            offers, seen = [], set()
            for row in rows:
                title = row.get("title")
                if not isinstance(title, str) or not title.strip() or title.casefold() in seen:
                    continue
                seen.add(title.casefold())
                link = {"api": 1, "query": f"{title} {c.destination}"}
                if row.get("place_id"):
                    link["query_place_id"] = row["place_id"]
                rating, reviews = row.get("rating"), row.get("reviews")
                description = row.get("description")
                offer = ActivityOffer(
                    name=title.strip(), category=row.get("type") if isinstance(row.get("type"), str) else None,
                    rating=rating if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 5 else None,
                    reviews=reviews if isinstance(reviews, int) and not isinstance(reviews, bool) and reviews >= 0 else None,
                    address=row.get("address") if isinstance(row.get("address"), str) else None,
                    description=description[:200] if isinstance(description, str) else None,
                    source_url="https://www.google.com/maps/search/?" + urlencode(link), fetched_at=fetched)
                offers.append(offer.model_dump(mode="json"))
            self.trace.log(self.agent, "search_results", f"{len(offers)} places; {len(rows) - len(offers)} rows excluded")
            if not offers:
                raise SearchError("No activities returned for this destination.")
            self.offers = offers[:20]
        self.searched = True
        self.returned = list(self.offers)
        return [{k: o[k] for k in ("name", "category", "rating", "reviews", "address", "description")} for o in self.offers]

    def resolve_activities(self, names: list[str]) -> list[dict]:
        """Map the agent's chosen names back to the exact offers that the search returned."""
        if not self.searched:
            raise SearchError(f"{self.agent} must call its live search tool before submitting.")
        by_name = {o["name"].casefold(): o for o in self.returned}
        picked: dict[str, dict] = {}
        for name in names:
            offer = by_name.get(name.strip().casefold())
            if offer is None:
                raise SearchError(f"{self.agent} chose an activity that live search did not return: {name!r}")
            picked.setdefault(offer["name"].casefold(), offer)
        return list(picked.values())

    def validate_picks(self, picks: BaseModel) -> BaseModel:
        self.resolve_activities(picks.names)
        return picks

    def _check_destination(self, destination: str, *aliases: str):
        allowed = {self.constraints.destination.casefold(), *(a.casefold() for a in aliases)}
        if destination.strip().casefold() not in allowed:
            raise SearchError("The agent must search the requested destination.")

    def _rows(self, data: dict, key: str) -> list[dict]:
        rows = data.get(key, [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SearchError(f"SerpAPI returned invalid {key} results.")
        return rows

    def _save(self, offers: list[dict], row_count: int):
        self.trace.log(self.agent, "search_results", f"{len(offers)} priced offers; {row_count - len(offers)} incomplete or non-hotel rows excluded")
        if not offers:
            raise SearchError(f"No priced {self.agent} offers returned for these dates. Try different dates or airports.")
        self.offers = offers

    def _filter(self, field: str, cap: float) -> list[dict]:
        assert self.offers is not None
        self.searched = True
        def cost(offer):
            if field == "nightly_price" and offer.get("total_price") is not None:
                return offer["total_price"] / self.constraints.nights
            return offer[field]

        fitting = [o for o in self.offers if cost(o) <= cap]
        if not fitting:
            self.trace.log(self.agent, "no_offers_under_cap", f"cap=${cap:.2f}; returning cheapest available offers")
        returned = fitting or sorted(self.offers, key=cost)[:5]
        self.returned.extend(returned)
        return returned

    def validate_selection(self, result: BaseModel) -> BaseModel:
        if not self.searched:
            raise SearchError(f"{self.agent} must call its live search tool before submitting.")
        selected = result.model_dump(mode="json", exclude={"rationale"})
        if not any(selected == offer for offer in self.returned):
            raise SearchError(f"{self.agent} submitted an offer or price not returned by live search.")
        return result

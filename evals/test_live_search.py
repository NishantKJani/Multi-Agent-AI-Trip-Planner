import asyncio
import json
import re
import threading
import unittest
from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from agents import Ctx, flights_agent
from budget import review
from live_search import LiveSearch, SearchError, activities_search_url, top_offers
from orchestrator import plan_trip, parse_request
from schemas import Constraints, FlightPlan, HotelPlan, ItineraryPlan
from tracing import Trace


def constraints(**changes):
    start = date.today() + timedelta(days=30)
    return Constraints(**{"destination": "Tokyo", "days": 4, "budget": 2000,
                          "departure_date": start, "return_date": start + timedelta(days=3),
                          **changes})


def trip_payload(**changes):
    start = date.today() + timedelta(days=30)
    payload = {"destination": "tokyo", "departure_airport": "sea", "arrival_airport": "HND", "budget": 2000,
               "departure_date": start.isoformat(), "return_date": (start + timedelta(days=3)).isoformat(),
               "interests": ["food"]}
    return {**payload, **changes}


FLIGHTS = {"search_metadata": {"status": "Success", "google_flights_url": "https://www.google.com/travel/flights"},
           "best_flights": [{"price": 650, "flights": [{"airline": "ANA"}]},
                            {"price": None, "flights": [{"airline": "Missing price"}]}],
           "other_flights": [{"price": 450, "flights": [{"airline": "United"}, {"airline": "ANA"}]}]}
HOTELS = {"search_metadata": {"status": "Success", "google_hotels_url": "https://www.google.com/travel/search?q=hotels"},
          "properties": [{"type": "hotel", "name": "Live Hotel", "overall_rating": 4.7,
                          "rate_per_night": {"extracted_lowest": 101},
                          "total_rate": {"extracted_lowest": 300}},
                         {"type": "hotel", "name": "Unrated Hotel",
                          "rate_per_night": {"extracted_lowest": 70}},
                         {"type": "hotel", "name": "Unpriced Hotel"},
                         {"type": "vacation rental", "name": "Villa",
                          "rate_per_night": {"extracted_lowest": 50}}]}


MAPS = {"search_metadata": {"status": "Success"},
        "local_results": [
            {"title": "Fountains of Bellagio", "place_id": "PID1", "rating": 4.8, "reviews": 61617,
             "type": "Tourist attraction", "address": "3600 S Las Vegas Blvd", "description": "x" * 300},
            {"title": "Welcome Sign", "place_id": "PID2", "rating": 9.9, "reviews": -5, "type": "Landmark"},
            {"title": "fountains of bellagio", "place_id": "DUP"},
            {"title": "   "}, {"rating": 4.0},
            {"title": "Red Rock Canyon", "rating": 4.8, "reviews": 25947}]}


def response(data):
    return BytesIO(json.dumps(data).encode())


class FakeMessages:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else trip_payload()

    def create(self, *, tools, messages, **kwargs):
        names = [tool["name"] for tool in tools]
        if len(messages) == 1:
            if "search_flights" in names or "search_hotels" in names:
                name = names[0]
                cap = re.search(r"(?:Price cap|Max nightly price): \$(\d+(?:\.\d+)?)", messages[0]["content"])
                arguments = {"destination": "Tokyo"}
                if cap:
                    arguments["max_price" if name == "search_flights" else "max_nightly"] = float(cap[1])
                block = SimpleNamespace(type="tool_use", name=name, id="search",
                                        input=arguments)
            elif "search_activities" in names:
                block = SimpleNamespace(type="tool_use", name="search_activities", id="search",
                                        input={"destination": "Tokyo"})
            else:
                block = SimpleNamespace(type="tool_use", name="submit_result", id="submit",
                                        input=self.payload)
        else:
            rows = json.loads(messages[-1]["content"][0]["content"])
            if "search_activities" in names:
                result = {"names": [rows[1]["name"].upper(), rows[0]["name"]], "rationale": "Top picks."}
            else:
                result = {**rows[0], "rationale": "Selected a returned offer."}
            block = SimpleNamespace(type="tool_use", name="submit_result", id="submit", input=result)
        return SimpleNamespace(content=[block])


class LiveSearchTests(unittest.TestCase):
    def setUp(self):
        self.c = constraints()
        self.trace = Trace()

    def search(self, agent="flights"):
        return LiveSearch("test-private-key", self.c, self.trace, agent)

    @patch("live_search.urlopen")
    def test_flights_parameters_normalization_cache_and_cap(self, request):
        request.return_value = response(FLIGHTS)
        search = self.search()
        options = search.search_flights("Tokyo", 500)
        self.assertEqual(options[0]["price"], 450)
        self.assertEqual(options[0]["stops"], 1)
        self.assertEqual(options[0]["airline"], "United / ANA")
        self.assertTrue(options[0]["fetched_at"])
        self.assertEqual(options[0]["source_url"], "https://www.google.com/travel/flights")
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["type"], ["1"])
        self.assertEqual(query["departure_id"], ["SEA"])
        self.assertEqual(query["arrival_id"], ["HND"])
        self.assertEqual(query["outbound_date"], [self.c.departure_date.isoformat()])
        self.assertEqual(query["return_date"], [self.c.return_date.isoformat()])
        self.assertEqual(query["currency"], ["USD"])
        self.assertEqual(query["adults"], ["1"])
        self.assertEqual(search.search_flights("Tokyo", 1)[0]["price"], 450)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("test-private-key", str(self.trace.events))

    @patch("live_search.urlopen")
    def test_hotel_total_rate_and_missing_rating(self, request):
        request.return_value = response(HOTELS)
        options = self.search("hotels").search_hotels("Tokyo")
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0]["nightly_price"], 100)
        self.assertEqual(options[0]["total_price"], 300)
        self.assertIsNone(options[1]["rating"])
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["engine"], ["google_hotels"])
        self.assertEqual(query["check_in_date"], [self.c.departure_date.isoformat()])     # same dates as the flight
        self.assertEqual(query["check_out_date"], [self.c.return_date.isoformat()])
        self.assertEqual(query["adults"], ["1"])
        hotel = HotelPlan(**options[0], rationale="Test")
        hotel.nightly_price = 101
        report = review(self.c, FlightPlan(airline="ANA", price=650, stops=0, rationale="Test"),
                        hotel, ItineraryPlan(days=[]))
        self.assertEqual(report.costs["hotels"], 300)

    @patch("live_search.urlopen")
    def test_grounding_rejects_changed_price_and_unreturned_offer(self, request):
        request.return_value = response(FLIGHTS)
        search = self.search()
        option = search.search_flights("Tokyo", 500)[0]
        valid = FlightPlan(**option, rationale="Test")
        self.assertEqual(search.validate_selection(valid), valid)
        with self.assertRaises(SearchError):
            search.validate_selection(valid.model_copy(update={"price": 1}))
        with self.assertRaises(SearchError):
            search.validate_selection(FlightPlan(**search.offers[0], rationale="Not exposed under cap"))
        search.searched = False
        with self.assertRaises(SearchError):
            search.validate_selection(valid)

    @patch("live_search.urlopen")
    def test_empty_and_unpriced_results_do_not_use_mock(self, request):
        for data in ({}, {"best_flights": [{"price": "650", "flights": [{"airline": "ANA"}]}]}):
            request.return_value = response(data)
            with self.assertRaisesRegex(SearchError, "No priced"):
                self.search().search_flights("Tokyo")

    @patch("live_search.urlopen")
    def test_errors_are_explicit_and_redacted(self, request):
        errors = [HTTPError("https://example.com/?api_key=test-private-key", 401, "Unauthorized", {}, None),
                  URLError("test-private-key"), TimeoutError("test-private-key")]
        for error in errors:
            request.side_effect = error
            with self.assertRaises(SearchError) as caught:
                self.search().search_flights("Tokyo")
            self.assertNotIn("test-private-key", str(caught.exception))
        request.side_effect = None
        request.return_value = response({"error": "Invalid test-private-key"})
        with self.assertRaises(SearchError) as caught:
            self.search().search_flights("Tokyo")
        self.assertNotIn("test-private-key", str(caught.exception))

    @patch("live_search.urlopen")
    def test_invalid_json_and_response_shapes(self, request):
        for data in (b"not json", b"[]", b'{"best_flights":{}}', b'{"search_metadata":[]}'):
            request.return_value = BytesIO(data)
            with self.assertRaises(SearchError):
                self.search().search_flights("Tokyo")

    def test_top_offers_selected_first_then_affordable_alternatives(self):
        offers = [{"price": p} for p in (500, 100, 300, 200, 400)]
        selected = FlightPlan(airline="A", price=300, stops=0, rationale="x")
        offers[2] = {**selected.model_dump(mode="json", exclude={"rationale"})}
        cost = lambda o: o["price"]
        picked = top_offers(offers, selected, cost, lambda o: o["price"], cap=250)
        self.assertEqual([o["price"] for o in picked], [300, 100, 200])
        picked = top_offers(offers, selected, cost, lambda o: o["price"], cap=50)   # nothing affordable
        self.assertEqual([o["price"] for o in picked], [300, 100, 200])
        self.assertEqual(len(top_offers([offers[2]], selected, cost, cost, cap=1)), 1)   # only the pick exists

    @patch("live_search.urlopen")
    def test_google_links_use_provider_url_or_dated_fallbacks(self, request):
        flights = {**FLIGHTS, "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?tfs=abc"}}
        request.return_value = response(flights)
        self.assertEqual(self.search().search_flights("Tokyo")[0]["source_url"], "https://www.google.com/travel/flights?tfs=abc")
        request.return_value = response({**HOTELS, "search_metadata": {"google_hotels_url": "https://evil.example/"}})
        url = self.search("hotels").search_hotels("Tokyo")[0]["source_url"]
        self.assertTrue(url.startswith("https://www.google.com/travel/search?"))
        self.assertIn(self.c.departure_date.isoformat(), url)
        self.assertIn(self.c.return_date.isoformat(), url)

    def test_required_live_inputs_and_destination(self):
        with self.assertRaises(ValueError):
            LiveSearch("", self.c, self.trace, "flights")
        with self.assertRaises(ValueError):
            LiveSearch("test", Constraints(destination="Tokyo", days=4, budget=2000), self.trace, "flights")
        with self.assertRaises(SearchError):
            self.search().search_flights("Paris")
        with patch("live_search.urlopen") as request:
            request.return_value = response(FLIGHTS)
            self.assertTrue(self.search().search_flights("hnd"))     # arrival airport code is accepted for flights
        with self.assertRaises(SearchError):
            self.search("hotels").search_hotels("HND")               # hotels need the city
        for changes in ({"return_date": None}, {"departure_airport": "Seattle"},
                        {"return_date": self.c.departure_date}, {"arrival_airport": "SEA"},
                        {"days": 5}, {"departure_date": date.today() - timedelta(days=1), "days": 5}):
            with self.assertRaises(ValueError):
                constraints(**changes)

    @patch("live_search.urlopen")
    def test_live_parent_delegates_in_parallel_and_returns_grounded_results(self, request):
        barrier = threading.Barrier(3)        # flights, hotels and activities must all be in flight together

        def provider(url, **kwargs):
            barrier.wait(timeout=5)
            return response(FLIGHTS if "google_flights" in url else MAPS if "google_maps" in url else HOTELS)

        request.side_effect = provider
        ctx = Ctx(mode="live", trace=self.trace, client=SimpleNamespace(messages=FakeMessages()),
                  search_api_key="test-private-key")
        result = asyncio.run(plan_trip(self.c, ctx))
        self.assertTrue(result.report.within_budget)
        self.assertEqual(result.flight.airline, "ANA")
        self.assertEqual(result.hotel.name, "Live Hotel")
        self.assertEqual(result.report.total, 950)               # activities have no price: $0
        self.assertEqual([o["price"] for o in result.flight_options], [650, 450])   # selected first, then alternative
        self.assertEqual([o["name"] for o in result.hotel_options], ["Live Hotel", "Unrated Hotel"])
        self.assertTrue(all(o["source_url"].startswith("https://www.google.com/") for o in result.flight_options))
        self.assertEqual([a.name for a in result.itinerary.activities], ["Welcome Sign", "Fountains of Bellagio"])
        self.assertIn("top+things+to+do+in+Tokyo", str(result.itinerary.more_url))
        self.assertEqual(request.call_count, 3)
        self.assertEqual({e["agent"] for e in self.trace.events if e["event"] == "done"},
                         {"flights", "hotels", "itinerary"})

    @patch("live_search.urlopen")
    def test_activity_search_failure_keeps_flights_and_hotels(self, request):
        def provider(url, **kwargs):
            if "google_maps" in url:
                raise HTTPError(url, 500, "boom", {}, None)
            return response(FLIGHTS if "google_flights" in url else HOTELS)

        request.side_effect = provider
        ctx = Ctx(mode="live", trace=self.trace, client=SimpleNamespace(messages=FakeMessages()),
                  search_api_key="test-private-key")
        result = asyncio.run(plan_trip(self.c, ctx))
        self.assertEqual(result.itinerary.activities, [])
        self.assertIn("top+things+to+do", str(result.itinerary.more_url))
        self.assertTrue(any("activity search failed" in n for n in result.notes))
        self.assertEqual(result.hotel.name, "Live Hotel")
        self.assertTrue(result.report.within_budget)

    @patch("live_search.urlopen")
    def test_activities_normalization_grounding_and_query(self, request):
        request.return_value = response(MAPS)
        search = self.search("itinerary")
        with self.assertRaises(SearchError):
            search.resolve_activities(["Fountains of Bellagio"])        # must search first
        rows = search.search_activities("Tokyo")
        self.assertEqual([r["name"] for r in rows], ["Fountains of Bellagio", "Welcome Sign", "Red Rock Canyon"])
        self.assertNotIn("source_url", rows[0])                          # the model sees a trimmed view
        self.assertEqual(len(rows[0]["description"]), 200)
        self.assertIsNone(rows[1]["rating"])                             # out-of-range rating dropped
        self.assertIsNone(rows[1]["reviews"])
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["engine"], ["google_maps"])
        self.assertEqual(query["q"], ["top things to do in Tokyo"])
        self.assertNotIn("currency", query)
        request.return_value = response(MAPS)
        with_interests = LiveSearch("test-private-key", constraints(interests=["food", "art"]), self.trace, "itinerary")
        with_interests.search_activities("Tokyo")
        self.assertEqual(parse_qs(urlparse(request.call_args.args[0]).query)["q"], ["top food, art things to do in Tokyo"])
        request.return_value = response(MAPS)
        picked = search.resolve_activities(["red rock canyon", "FOUNTAINS OF BELLAGIO", "Red Rock Canyon"])
        self.assertEqual([p["name"] for p in picked], ["Red Rock Canyon", "Fountains of Bellagio"])   # deduped, ordered
        self.assertIn("query_place_id=PID1", picked[1]["source_url"])
        self.assertTrue(picked[0]["source_url"].startswith("https://www.google.com/maps/search/?"))
        with self.assertRaises(SearchError):
            search.resolve_activities(["Invented Place"])
        request.return_value = response({"local_results": []})
        with self.assertRaises(SearchError):
            self.search("itinerary").search_activities("Tokyo")
        self.assertEqual(activities_search_url("Las Vegas"),
                         "https://www.google.com/search?q=top+things+to+do+in+Las+Vegas&hl=en&gl=us")

    @patch("live_search.urlopen")
    def test_agent_reuses_quotes_but_must_search_again_on_revision(self, request):
        request.return_value = response(FLIGHTS)
        ctx = Ctx(mode="live", trace=self.trace, client=SimpleNamespace(messages=FakeMessages()),
                  search_api_key="test-private-key")
        flights_agent(self.c, 1000, ctx)
        revised = flights_agent(self.c, 500, ctx, "Cut price")
        self.assertEqual(revised.price, 450)
        self.assertEqual(request.call_count, 1)

    def test_coordinator_extracts_one_set_of_dates(self):
        result = parse_request("any text", SimpleNamespace(messages=FakeMessages()), self.trace)
        self.assertEqual(result.destination, "Tokyo")
        self.assertEqual((result.departure_airport, result.arrival_airport), ("SEA", "HND"))
        self.assertEqual((result.departure_date, result.return_date), (self.c.departure_date, self.c.return_date))
        self.assertEqual((result.days, result.nights), (4, 3))

    def test_coordinator_reports_missing_and_inconsistent_details(self):
        client = SimpleNamespace(messages=FakeMessages(trip_payload(budget=None, arrival_airport=None, return_date=None)))
        with self.assertRaisesRegex(ValueError, "Arrival airport.*Return date.*Budget"):
            parse_request("any text", client, self.trace)
        client = SimpleNamespace(messages=FakeMessages(trip_payload(arrival_airport="SEA")))
        with self.assertRaisesRegex(ValueError, "airports must differ"):
            parse_request("any text", client, self.trace)

    def test_invalid_iterations_and_missing_live_credentials(self):
        for iterations in (0, 5):
            with self.assertRaises(ValueError):
                asyncio.run(plan_trip(self.c, Ctx(mode="mock", trace=self.trace), iterations))
        with self.assertRaises(ValueError):
            asyncio.run(plan_trip(self.c, Ctx(mode="live", trace=self.trace)))


class AppTests(unittest.TestCase):
    def test_mock_ui_and_result_persistence(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.success), 1)
        self.assertEqual(len(app.subheader), 3)
        self.assertEqual(len(app.dataframe), 3)         # flights, hotels, trace
        self.assertEqual(len(app.get("link_button")), 0)    # mock data has no Google link
        self.assertFalse(any(m.value.count("$") > 1 and "\\$" not in m.value for m in app.markdown))
        app.slider[0].set_value(2).run()
        self.assertEqual(len(app.success), 1)

    def test_live_ui_has_only_the_description_box(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        no_secrets = patch("streamlit.runtime.secrets.Secrets.__getitem__", side_effect=KeyError)
        with no_secrets, patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "SERPAPI_KEY": ""}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
            app.radio[0].set_value("Live (Claude)").run()
            self.assertIn("ANTHROPIC_API_KEY", app.info[0].value)
            app.text_input[0].set_value("test-anthropic-key")
            app.text_input[1].set_value("test-serp-key")
            app.run()
            self.assertEqual((len(app.selectbox), len(app.date_input), len(app.text_area)), (0, 0, 1))
            self.assertTrue(any("flight departure + hotel check-in" in m.value for m in app.markdown))
            with patch("orchestrator.parse_request") as parse:
                parse.side_effect = ValueError("Please add the missing details to your description: Budget.")
                app.button[0].click().run()
                self.assertEqual(len(app.error), 1)
                app.text_area[0].set_value("some trip").run()
                app.button[0].click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertIn("Budget", app.error[0].value)

    def test_live_ui_uses_configured_keys_without_text_boxes(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-a", "SERPAPI_KEY": "test-s"}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
            app.radio[0].set_value("Live (Claude)").run()
            self.assertEqual(len(app.text_input), 0)
            self.assertEqual(len(app.text_area), 1)
            self.assertEqual(len(app.info), 1)    # only the live-offers notice, no key prompt


    def test_live_results_show_three_tables_and_google_buttons(self):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path
        from orchestrator import TripResult
        from schemas import ActivitiesPlan, ActivityOffer, BudgetReport
        c = constraints()
        flight = FlightPlan(airline="ANA", price=650, stops=0, rationale="Pick costs $650 and $1,000.",
                            source_url="https://www.google.com/travel/flights?tfs=x")
        hotel = HotelPlan(name="Live Hotel", area="Tokyo", nightly_price=100, total_price=300, rating=4.7,
                          rationale="ok", source_url="https://www.google.com/travel/search?q=h")
        plan = ActivitiesPlan(activities=[ActivityOffer(name="Place A", category="Museum", rating=4.5, reviews=10,
                                                        source_url="https://www.google.com/maps/search/?api=1&query=a")],
                              rationale="Great $5 picks.", more_url="https://www.google.com/search?q=things")
        report = review(c, flight, hotel, plan)
        result = TripResult(c, flight, hotel, plan, report, 1, [], [flight.model_dump(mode="json")],
                            [hotel.model_dump(mode="json")])

        async def fake_plan(*args, **kwargs):
            return result

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "a", "SERPAPI_KEY": "s"}), \
                patch("orchestrator.parse_request", return_value=c), patch("orchestrator.plan_trip", fake_plan):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
            app.radio[0].set_value("Live (Claude)").run()
            app.text_area[0].set_value("some trip").run()
            app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.dataframe), 4)                  # flights, hotels, activities, trace
        labels = [b.proto.label for b in app.get("link_button")]
        self.assertEqual(labels, ["View more flight results on Google", "View more hotel results on Google",
                                  "View more activities on Google"])
        self.assertEqual(app.subheader[2].value, "🗺️ Top things to do in Tokyo")
        self.assertTrue(any("not included in the budget" in c.value for c in app.caption))


if __name__ == "__main__":
    unittest.main()

"""Coordinator: parse -> parallel sub-agents -> budget review -> targeted re-runs (max N loops)."""
import asyncio
from dataclasses import dataclass, field
from datetime import date
import anthropic
from pydantic import ValidationError
from schemas import (Constraints, FlightPlan, HotelPlan, ItineraryPlan, BudgetReport, TripRequest,
                     ActivitiesPlan)
from mock_data import CATALOG
from agents import AGENTS, Ctx
from budget import initial_caps, review
from llm import run_agent
from live_search import LiveSearch, top_offers, activities_search_url

COORD_MODEL = "claude-sonnet-5-5"


@dataclass
class TripResult:
    constraints: Constraints
    flight: FlightPlan
    hotel: HotelPlan
    itinerary: ItineraryPlan
    report: BudgetReport
    iterations: int
    notes: list[str]
    flight_options: list[dict] = field(default_factory=list)   # top 3 live offers, selected first
    hotel_options: list[dict] = field(default_factory=list)


REQUIRED_DETAILS = {
    "destination": "Destination (hotel city)",
    "departure_airport": "Departure airport",
    "arrival_airport": "Arrival airport",
    "departure_date": "Departure date",
    "return_date": "Return date",
    "budget": "Budget",
}


def parse_request(text: str, client: anthropic.Anthropic, trace) -> Constraints:
    """Live-mode only: turn free text into validated constraints, or say exactly what is missing."""
    trace.log("coordinator", "parse_request", text)
    system = (f"Extract trip details from the user's request. Today is {date.today().isoformat()}. "
              "Dates must be YYYY-MM-DD; if the year is omitted use the next upcoming occurrence. "
              "The departure date is both the flight date and hotel check-in; the return date is both the return flight and hotel check-out. "
              "Airports must be 3-letter IATA codes (convert an airport or city name to its main code). "
              "NEVER guess: leave any field the user did not state as null. "
              "Interests, if mentioned, must be from: culture, food, art, tech, nature, nightlife.")
    req = run_agent(client, COORD_MODEL, system, text, [], {}, TripRequest, trace, "coordinator")
    missing = [label for key, label in REQUIRED_DETAILS.items() if not getattr(req, key)]
    if missing:
        raise ValueError("Please add the missing details to your description: " + ", ".join(missing) + ".")
    airports = {k: getattr(req, k).strip().upper() for k in ("departure_airport", "arrival_airport")}
    destination = next((k for k in CATALOG if k.casefold() == req.destination.strip().casefold()), req.destination.strip())
    try:
        return Constraints(destination=destination, origin=airports["departure_airport"], budget=req.budget,
                           days=(req.return_date - req.departure_date).days + 1, interests=req.interests,
                           departure_date=req.departure_date, return_date=req.return_date, **airports)
    except ValidationError as e:
        raise ValueError("; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())) from None


async def _run_with_retry(name, fn, *args, retries=2):
    for attempt in range(retries + 1):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:                         # network error, bad schema, etc.
            if attempt == retries:
                raise
            args[2].trace.log(name, "retry", f"attempt {attempt + 1} failed ({type(e).__name__}); retrying")
            await asyncio.sleep(0.5 * (attempt + 1))


async def plan_trip(c: Constraints, ctx: Ctx, max_iters: int = 3) -> TripResult:
    if not 1 <= max_iters <= 4:
        raise ValueError("Max revision loops must be between 1 and 4.")
    ctx.searches.clear()
    if ctx.mode == "live":
        if ctx.client is None:
            raise ValueError("Live mode requires an Anthropic client.")
        for name in ("flights", "hotels"):
            ctx.searches[name] = LiveSearch(ctx.search_api_key or "", c, ctx.trace, name)
    trace, caps, feedback, results, notes = ctx.trace, initial_caps(c), {}, {}, []
    best_results, best_report = None, None
    targets = list(AGENTS)
    for it in range(1, max_iters + 1):
        trace.log("coordinator", "dispatch", f"iter {it}: {targets} (parallel)")
        outs = await asyncio.gather(*[
            _run_with_retry(n, AGENTS[n], c, caps[n], ctx, feedback.get(n)) for n in targets
        ], return_exceptions=True)
        for n, o in zip(targets, outs):
            if isinstance(o, Exception):
                trace.log(n, "failed", repr(o))
                if n not in results and n == "itinerary" and ctx.mode == "live":
                    # Activities are a nice-to-have: keep flights and hotels working without them.
                    results[n] = ActivitiesPlan(more_url=activities_search_url(c.destination))
                    notes.append("Live activity search failed; use the 'View more activities' link instead.")
                    continue
                if n not in results:
                    raise RuntimeError(f"{n} agent failed with no prior result: {o}")
                notes.append(f"{n} agent failed on iteration {it}; kept previous result.")
            else:
                results[n] = o
                trace.log(n, "done", o.model_dump_json())
        report = review(c, results["flights"], results["hotels"], results["itinerary"])
        if best_report is None or report.total < best_report.total:
            best_results, best_report = results.copy(), report
        trace.log("budget", "review", f"total ${report.total:.0f} / ${report.budget:.0f} ok={report.within_budget}")
        if report.within_budget:
            break
        feedback = {f.agent: f.instruction for f in report.feedback}
        for f in report.feedback:
            caps[f.agent] = f.max_cost
        targets = list(feedback)                       # re-run ONLY the agents asked to revise
        trace.log("coordinator", "revise", f"re-running {targets}")
    else:
        results, report = best_results, best_report
        notes.append(f"Could not get under budget after {max_iters} iterations; showing the cheapest plan found (${report.total:.0f} vs ${report.budget:.0f}).")
    flight_options, hotel_options = [], []
    if ctx.mode == "live":
        flight_options = top_offers(ctx.searches["flights"].offers, results["flights"], lambda o: o["price"],
                                    lambda o: (o["price"], o["stops"]), caps["flights"])
        hotel_cost = lambda o: o["total_price"] if o.get("total_price") is not None else o["nightly_price"] * c.nights
        hotel_options = top_offers(ctx.searches["hotels"].offers, results["hotels"], hotel_cost,
                                   lambda o: (-(o["rating"] or 0), hotel_cost(o)), caps["hotels"])
    return TripResult(c, results["flights"], results["hotels"], results["itinerary"], report, it, notes,
                      flight_options, hotel_options)

"""Budget agent. Deliberately code, not an LLM: LLMs shouldn't do arithmetic.
It validates totals and emits targeted, numeric revision requests."""
from schemas import Constraints, FlightPlan, HotelPlan, ItineraryPlan, BudgetReport, Feedback

SHARES = {"flights": 0.35, "hotels": 0.40, "itinerary": 0.25}   # "fair share" used to decide who must cut
OPTIMISTIC = {"flights": 0.50, "hotels": 0.55, "itinerary": 0.35}  # first-pass caps: let agents aim high


def initial_caps(c: Constraints) -> dict[str, float]:
    return {k: c.budget * v for k, v in OPTIMISTIC.items()}


def review(c: Constraints, flight: FlightPlan, hotel: HotelPlan, itin: ItineraryPlan) -> BudgetReport:
    hotel_cost = hotel.total_price if hotel.total_price is not None else hotel.nightly_price * c.nights
    costs = {"flights": flight.price, "hotels": hotel_cost, "itinerary": itin.total}
    total = sum(costs.values())
    if total <= c.budget:
        return BudgetReport(costs=costs, total=total, budget=c.budget, within_budget=True)
    over = total - c.budget
    excess = {k: max(0.0, costs[k] - c.budget * SHARES[k]) for k in costs}
    tot_excess = sum(excess.values()) or 1.0
    fb = []
    for k, e in excess.items():
        if e > 0:
            new_cap = max(0.0, costs[k] - over * e / tot_excess)
            fb.append(Feedback(agent=k, max_cost=new_cap,
                               instruction=f"{k} cost ${costs[k]:.0f}; trip is ${over:.0f} over budget. Find a cheaper option under ${new_cap:.0f}."))
    return BudgetReport(costs=costs, total=total, budget=c.budget, within_budget=False, feedback=fb)

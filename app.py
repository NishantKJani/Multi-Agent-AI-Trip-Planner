import asyncio
import os
import anthropic
import pandas as pd
import streamlit as st
from agents import Ctx
from orchestrator import plan_trip, parse_request
from schemas import ActivitiesPlan, Constraints
from tracing import Trace

st.set_page_config(page_title="Trip Planner Agents", page_icon="🧭", layout="wide")
st.title("🧭 Multi-Agent Trip Planner")
st.caption("Coordinator → Flights / Hotels / Itinerary (parallel) → Budget agent → targeted revisions")


def esc(text: str) -> str:
    """Streamlit markdown treats $...$ as math; escape dollar signs in model/catalog text."""
    return text.replace("$", "\\$")


def configured_key(name: str) -> str | None:
    """Key from an environment variable or .streamlit/secrets.toml; never from source code."""
    if os.environ.get(name):
        return os.environ[name].strip()
    try:
        return str(st.secrets[name]).strip() or None
    except (KeyError, FileNotFoundError):
        return None


with st.sidebar:
    mode = st.radio("Mode", ["Mock (no API key)", "Live (Claude)"])
    live = mode.startswith("Live")
    api_key = configured_key("ANTHROPIC_API_KEY") if live else None
    search_api_key = configured_key("SERPAPI_KEY") if live else None
    if live and not api_key:
        api_key = st.text_input("Anthropic API key", type="password", help="Kept in this session's memory only. Never written to disk.")
    if live and not search_api_key:
        search_api_key = st.text_input("SerpAPI key", type="password", help="Used for Google Flights and Google Hotels searches. Never written to disk.")
    if live and api_key and search_api_key:
        st.caption("API keys loaded.")
    max_iters = st.slider("Max revision loops", 1, 4, 3)

client = anthropic.Anthropic(api_key=api_key) if live and api_key else None
if live and (not api_key or not search_api_key):
    st.info("Set ANTHROPIC_API_KEY and SERPAPI_KEY (environment variables or .streamlit/secrets.toml), or enter them in the sidebar.")
    st.stop()

if live:
    st.info("Live flight, hotel and activity results for one adult, economy, one room, USD. Prices and availability may change; taxes, baggage, meals and transfers may be extra. "
            "Activities come from Google Maps results and have no price data.")
    st.markdown("**Please include in your description:**\n"
                "- Destination (hotel city)\n"
                "- Departure airport and arrival airport (e.g. SEA → HND)\n"
                "- Departure date (flight departure + hotel check-in)\n"
                "- Return date (return flight + hotel check-out)\n"
                "- Budget (USD)")
    text = st.text_area("Describe your trip", height=120,
                        placeholder="Hotel in Tokyo, fly SEA to HND. Depart 2026-12-06, return 2026-12-09. "
                                    "Budget $2000. I like food and culture.")
    go = st.button("Plan trip", type="primary")
else:
    col = st.columns(4)
    dest = col[0].selectbox("Destination", ["Tokyo", "Paris", "Lisbon"])
    days = col[1].number_input("Days", 2, 14, 4)
    budget = col[2].number_input("Budget (USD)", 300, 10000, 2000, step=100)
    interests = col[3].multiselect("Interests", ["culture", "food", "art", "tech", "nature", "nightlife"], ["food", "culture"])
    go = st.button("Plan trip", type="primary")

if go:
    trace = Trace()
    ctx = Ctx(mode="live" if live else "mock", trace=trace, client=client, search_api_key=search_api_key)
    st.session_state.pop("trip_result", None)
    try:
        with st.spinner("Agents working..."):
            if live:
                if not text.strip():
                    raise ValueError("Please describe your trip.")
                c = parse_request(text, client, trace)
            else:
                c = Constraints(destination=dest, days=days, budget=budget, interests=interests)
            r = asyncio.run(plan_trip(c, ctx, max_iters))
            st.session_state["trip_result"] = (mode, r, trace)
    except Exception as e:
        st.error(f"Something failed: {e}")
        with st.expander("Agent trace", expanded=True):
            st.dataframe(pd.DataFrame(trace.events), use_container_width=True)
        st.stop()

if "trip_result" in st.session_state and st.session_state["trip_result"][0] == mode:
    _, r, trace = st.session_state["trip_result"]
    st.caption(f"Last generated plan: {r.constraints.destination}, {r.constraints.days} days")
    (st.success if r.report.within_budget else st.warning)(
        f"USD {r.report.total:,.0f} of USD {r.report.budget:,.0f} budget · {r.iterations} iteration(s)")
    for n in r.notes:
        st.warning(esc(n))

    nights = r.constraints.nights

    def pick_column(options):
        return ["⭐ Selected"] + ["Alternative"] * (len(options) - 1)

    flight_options = r.flight_options or [r.flight.model_dump(mode="json")]
    st.subheader(f"✈️ Flights · ${r.flight.price:,.0f} selected")
    st.dataframe(pd.DataFrame({
        "Pick": pick_column(flight_options),
        "Airline": [o["airline"] for o in flight_options],
        "Stops (outbound)": [o["stops"] for o in flight_options],
        "Round trip": [o["price"] for o in flight_options]}),
        hide_index=True, width="stretch", column_config={"Round trip": st.column_config.NumberColumn(format="$%d")})
    st.caption(esc(r.flight.rationale))
    if r.flight.source_url:
        st.link_button("View more flight results on Google", str(r.flight.source_url))
        st.caption((f"Retrieved {r.flight.fetched_at:%Y-%m-%d %H:%M UTC}. " if r.flight.fetched_at else "")
                   + "Indicative fares, not confirmed bookings.")

    hotel_options = r.hotel_options or [r.hotel.model_dump(mode="json")]
    total_of = lambda o: o["total_price"] if o.get("total_price") is not None else o["nightly_price"] * nights
    st.subheader(f"🏨 Hotels · ${r.report.costs['hotels']:,.0f} selected")
    st.dataframe(pd.DataFrame({
        "Pick": pick_column(hotel_options),
        "Hotel": [o["name"] for o in hotel_options],
        "Rating": [o["rating"] for o in hotel_options],
        "Per night": [total_of(o) / nights for o in hotel_options],
        f"Total ({nights} nights)": [total_of(o) for o in hotel_options]}),
        hide_index=True, width="stretch",
        column_config={"Rating": st.column_config.NumberColumn(format="%.1f ★"),
                       "Per night": st.column_config.NumberColumn(format="$%.0f"),
                       f"Total ({nights} nights)": st.column_config.NumberColumn(format="$%d")})
    st.caption(esc(r.hotel.rationale))
    if r.hotel.source_url:
        st.link_button("View more hotel results on Google", str(r.hotel.source_url))
        st.caption((f"Retrieved {r.hotel.fetched_at:%Y-%m-%d %H:%M UTC}. " if r.hotel.fetched_at else "")
                   + "Rates are indicative, not confirmed bookings.")

    if isinstance(r.itinerary, ActivitiesPlan):
        st.subheader(f"🗺️ Top things to do in {r.constraints.destination}")
        if r.itinerary.activities:
            acts = r.itinerary.activities
            st.dataframe(pd.DataFrame({
                "Activity": [a.name for a in acts],
                "Map": [str(a.source_url) if a.source_url else None for a in acts],
                "Type": [a.category for a in acts],
                "Rating": [a.rating for a in acts],
                "Reviews": [a.reviews for a in acts],
                "Address": [a.address for a in acts]}),
                hide_index=True, width="stretch",
                column_config={"Rating": st.column_config.NumberColumn(format="%.1f ★"),
                               "Reviews": st.column_config.NumberColumn(format="%d"),
                               "Map": st.column_config.LinkColumn(display_text="Open in Maps")})
            st.caption(esc(r.itinerary.rationale))
        if r.itinerary.more_url:
            st.link_button("View more activities on Google", str(r.itinerary.more_url))
        st.caption("Activity prices aren't available and are not included in the budget total.")
    else:
        st.subheader(f"🗺️ Activities · ${r.itinerary.total:,.0f}")
        for day in r.itinerary.days:
            st.write(f"**Day {day.day}:** " + esc(", ".join(f"{x.name} (${x.cost:.0f})" for x in day.activities) or "Free day"))

    with st.expander("🔍 Agent trace", expanded=True):
        st.dataframe(pd.DataFrame(trace.events), use_container_width=True)

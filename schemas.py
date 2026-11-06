"""Structured contracts between the coordinator and sub-agents."""
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, model_validator


class Constraints(BaseModel):
    destination: str
    origin: str = "Seattle"
    days: int = Field(ge=2, le=14)
    budget: float = Field(gt=0)
    interests: list[str] = []
    departure_airport: str = Field(default="SEA", pattern=r"^[A-Z]{3}$")
    arrival_airport: str = Field(default="HND", pattern=r"^[A-Z]{3}$")
    departure_date: date | None = None   # flight departure and hotel check-in
    return_date: date | None = None      # return flight and hotel check-out

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.departure_date is None) != (self.return_date is None):
            raise ValueError("Provide both a departure date and a return date.")
        if self.departure_date is not None and self.return_date is not None:
            if self.departure_date < date.today():
                raise ValueError("The departure date cannot be in the past.")
            if self.return_date <= self.departure_date:
                raise ValueError("The return date must be after the departure date.")
            if self.days != self.nights + 1:
                raise ValueError("Trip days must equal nights + 1.")
            if self.departure_airport == self.arrival_airport:
                raise ValueError("Departure and arrival airports must differ.")
        return self

    @property
    def nights(self) -> int:
        if self.departure_date and self.return_date:
            return (self.return_date - self.departure_date).days
        return self.days - 1


class TripRequest(BaseModel):
    """What the coordinator extracts from the user's text. Every field is optional so
    missing details can be reported back instead of guessed."""
    destination: str | None = Field(default=None, description="Hotel city")
    departure_airport: str | None = Field(default=None, description="3-letter IATA code")
    arrival_airport: str | None = Field(default=None, description="3-letter IATA code")
    departure_date: date | None = Field(default=None, description="Flight departure and hotel check-in, YYYY-MM-DD")
    return_date: date | None = Field(default=None, description="Return flight and hotel check-out, YYYY-MM-DD")
    budget: float | None = Field(default=None, description="Total trip budget in USD")
    interests: list[str] = []


class FlightPlan(BaseModel):
    airline: str
    price: float = Field(ge=0, allow_inf_nan=False, description="Round-trip price in USD")
    stops: int = Field(ge=0)
    rationale: str
    source_url: HttpUrl | None = None
    fetched_at: datetime | None = None


class HotelPlan(BaseModel):
    name: str
    area: str
    nightly_price: float = Field(ge=0, allow_inf_nan=False)
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    rationale: str
    total_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_url: HttpUrl | None = None
    fetched_at: datetime | None = None


class Activity(BaseModel):
    name: str
    cost: float


class ItineraryDay(BaseModel):
    day: int
    activities: list[Activity]


class ItineraryPlan(BaseModel):
    days: list[ItineraryDay]

    @property
    def total(self) -> float:
        return sum(a.cost for d in self.days for a in d.activities)


class ActivityOffer(BaseModel):
    """A real place returned by a live Google Maps search. No price data is available."""
    name: str
    category: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    reviews: int | None = Field(default=None, ge=0)
    address: str | None = None
    description: str | None = None
    source_url: HttpUrl | None = None
    fetched_at: datetime | None = None


class ActivityPicks(BaseModel):
    """What the activities agent submits: exact names only; the details are looked up from the search results."""
    names: list[str] = Field(min_length=1, max_length=3, description="Exact names from the search_activities results")
    rationale: str


class ActivitiesPlan(ItineraryPlan):
    """Live-mode itinerary result: top things to do. Prices are unknown, so it adds $0 to the budget total."""
    days: list[ItineraryDay] = []
    activities: list[ActivityOffer] = []
    rationale: str = ""
    more_url: HttpUrl | None = None


class Feedback(BaseModel):
    agent: Literal["flights", "hotels", "itinerary"]
    max_cost: float
    instruction: str


class BudgetReport(BaseModel):
    costs: dict[str, float]
    total: float
    budget: float
    within_budget: bool
    feedback: list[Feedback] = []

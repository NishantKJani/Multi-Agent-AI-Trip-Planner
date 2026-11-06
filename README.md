# Multi-Agent AI Trip Planner

> A coordinator LLM agent delegates flight, hotel and activity research to parallel sub-agents that use **live Google Flights / Hotels / Maps data**, then a deterministic budget agent iterates with them until the plan fits your budget. Every offer shown in the results (price, flight, hotel, place) is verified against real search results.

## Summary

**What it does.** You describe a trip in one sentence ("Hotel in Las Vegas. Fly SEA to Las Vegas. Departure 11/11/2026, return 11/18/2026. Budget of $2000."). The app returns a priced plan: the best flight, hotel and things to do, the top 3 options for flights and hotels with links to the full Google results, and a trace of exactly what each agent did.

**Why it is interesting.** It implements the patterns that make LLM applications dependable rather than just impressive:

- a **parent agent that delegates** to isolated, specialised **sub-agents** running **in parallel**
- **tool calling** against real APIs, with **structured, schema-validated outputs**
- **grounding**: an agent cannot report a price, hotel, flight or place that the search tool did not actually return
- a **feedback loop** where a deterministic budget check sends *targeted* revision requests to only the agents that must cut costs, with bounded iterations
- **failure handling**, retries, graceful degradation, and full **tracing**
- **automated tests** (20 unit/integration/UI tests plus 5 budget evals) that run without paid API calls

**Example run (live data, Nov 2026).** SEA → Las Vegas, Nov 11–18, $2,000 budget: nonstop Southwest flight ($622) + a 7-night hotel (~$600–$650) + 3 top activities, about **$1,220–$1,270 total in one pass (~7–9 s)**. Live prices change; the point is that every price in the tables comes from a search result, not from the model.

**Tech:** Python · Anthropic Claude (tool use) · Pydantic v2 · asyncio · SerpAPI (Google Flights, Hotels, Maps) · Streamlit · unittest

## How one request flows

1. **`app.py`** builds a shared `Ctx` (mode, trace, Anthropic client, SerpAPI key) and calls the coordinator.
2. **`parse_request`** (`orchestrator.py`) makes one Claude call that must return a `TripRequest`. Anything the user did not state is left `null`, and the code reports exactly which details are missing instead of guessing.
3. The validated **`Constraints`** (`schemas.py`) are the contract handed to every sub-agent.
4. **`plan_trip`** looks up the sub-agents in the `AGENTS` registry and starts them together with `asyncio.gather`; each runs in a worker thread so their network calls overlap.
5. Each sub-agent runs its own **tool-use loop** (`run_agent` in `llm.py`): Claude calls the agent's one search tool, receives real results, then must call `submit_result` with output matching a Pydantic schema.
6. **Grounding validators** (`live_search.py`) reject any submission that does not match what the tool returned; a rejection raises, and the parent retries the agent.
7. **`budget.review`** recomputes the total from line items. If over budget it returns per-agent feedback with numeric caps, and only those agents are re-run.
8. The loop ends when the plan fits or the iteration limit is hit (the cheapest plan found is returned with an honest note). The UI renders the result and the trace.

## Engineering concepts and the checks that implement them

Each row is something implemented and tested in this repo, not just described.

| Concept | What it protects against | Where |
|---|---|---|
| Input validation with Pydantic | Garbage or ambiguous user input reaching the agents | `Constraints`, `TripRequest` in `schemas.py` |
| Structured outputs | Free-text answers that cannot be parsed or checked | `submit_result` tool in `llm.py` |
| Grounding | Hallucinated prices, hotels, flights, places | `validate_selection`, `resolve_activities` in `live_search.py` |
| Context isolation | One agent's data or mistakes leaking into another | `Ctx`, per-agent `LiveSearch` in `agents.py` |
| Parallel delegation | Slow serial calls | `plan_trip` in `orchestrator.py` |
| Deterministic budget agent | LLM arithmetic errors | `budget.py` |
| Iterative revision with bounded loops | Over-budget plans; infinite loops | `plan_trip`, `budget.review` |
| Failure handling | One flaky API call killing the whole plan | `_run_with_retry`, activity fallback |
| Tracing | "Black box" agent behaviour | `tracing.py`, trace panel in the UI |
| Secret and prompt hygiene | Key leakage; prompt injection via search data | `configured_key`, `_request`, system prompts |

### 1. Input validation (Pydantic)
Two layers, so a bad request fails early with a clear message instead of producing a bad plan.

- **Extraction layer, `TripRequest`:** every field is optional and the prompt says *never guess*. `parse_request` then lists exactly what is missing (e.g. "Please add the missing details: Return date, Budget.") rather than letting the model invent a date or airport.
- **Contract layer, `Constraints`:**
  - `days` must be 2–14; `budget` must be > 0
  - airport codes must match `^[A-Z]{3}$` (IATA)
  - the departure date cannot be in the past; the return date must be after the departure date
  - **cross-field rule:** `days == nights + 1`, where `nights` is derived from the dates
  - departure and arrival airports must differ
  - both dates or neither
- **Output models** validate what agents return: prices are `>= 0` and not NaN/inf, ratings are `0..5`, links must be valid `HttpUrl`s.
- Pydantic errors are translated into plain-English messages for the user (`parse_request`).

### 2. Structured outputs through a tool
`run_agent` gives Claude two kinds of tools: the agent's search tool and a built-in **`submit_result`** whose input schema is the Pydantic model's JSON schema (`model_json_schema()`). The agent is not "done" until it submits, and the submission is validated with `model_validate`. If the model never submits, the loop stops after `max_turns=6` and raises, so there is no runaway loop.

### 3. Grounding: the model cannot invent offers
This is the most important reliability check in the project.

- **Flights and hotels:** `validate_selection` requires the submitted offer (every field except the free-text rationale) to **exactly equal** an offer that the search tool returned to *this* agent in *this* run. A changed price, an invented airline or a made-up hotel is rejected.
- **Activities:** the model submits only **names**. `resolve_activities` looks each name up (case-insensitive, de-duplicated) in the real Google Maps results and raises for anything unknown, so details (rating, address, map link) always come from the data, never the model.
- The agent must **call its search tool before submitting** (`searched` flag), and the tool refuses to search a destination other than the requested one.
- **Provider data is checked too:** malformed JSON, wrong response shapes, missing/invalid prices and incomplete rows are rejected or dropped; an empty result raises instead of silently falling back to mock data.
- The UI shows the source link and retrieval time of each live offer. (The model's free-text rationale is authored by the model and is not validated; the tables are.)

### 4. Context isolation
Each sub-agent receives only the shared constraints, **its own cap** and optional revision feedback. It has its own system prompt, its own single tool, and its own `LiveSearch` instance (no shared mutable search state between threads; the only shared object is the thread-safe `Trace`). The activities agent even sees a *trimmed* view of results. Smaller context means fewer chances to confuse or leak.

### 5. Parallel delegation
`plan_trip` starts all targeted agents with `asyncio.gather(..., return_exceptions=True)`; each blocking agent call runs via `asyncio.to_thread`. `return_exceptions=True` means one failing agent does not cancel the others. A test asserts all three searches are in flight **at the same time** (a 3-party barrier).

### 6. Deterministic budget agent
Arithmetic is done in plain code (`budget.py`), never by an LLM. The total is recomputed from line items (flight price + hotel whole-stay rate + activities), so a model cannot claim a plan "fits" when it does not.

### 7. Iterative revision until the plan fits (bounded)
- **Round 1 uses optimistic caps** (`OPTIMISTIC`: flights 50%, hotels 55%, activities 35% of budget) so agents aim high first.
- If the total exceeds the budget, `review` computes each agent's excess over its **fair share** (`SHARES`: 35/40/25%) and issues **targeted feedback**: a new numeric cap = current cost minus the overage allocated in proportion to that agent's excess, plus a plain-language instruction.
- **Only the agents that must cut are re-run** (`targets = list(feedback)`), receiving the instruction in their prompt.
- The loop **terminates** when the plan is within budget, or after `max_iters` (1–4, validated). It tracks the **best plan so far** and, if the budget cannot be met, returns the cheapest one with an explicit note rather than hiding the failure. A mock-mode eval ("impossible": 7 days in Tokyo for $500) verifies that unmeetable budgets are flagged, not faked.
- Re-runs reuse cached search results within a run (no repeat billing) but the agent must still call its tool again, so grounding still applies on every revision.

### 8. Failure handling and graceful degradation
- `_run_with_retry`: up to 2 retries with increasing back-off for network errors, schema failures and grounding rejections.
- If an agent fails on a later round, the **previous result is kept** and a note is shown.
- If the **activity search fails entirely**, flights and hotels still work; the UI shows a note and the Google "View more activities" link.
- Provider errors are explicit and **redacted** (no API key in error text).

### 9. Tracing and observability
A thread-safe `Trace` records every dispatch, search, tool call, result, retry, budget review and revision with timestamps. The UI shows it as a table, so you can see which agent did what, when, and why a revision happened.

### 10. Security and prompt hygiene
- API keys come from environment variables or a git-ignored `.streamlit/secrets.toml`; they are never in source, prompts or traces.
- Search results are treated as **untrusted data** in the system prompts. More importantly, even if injected text fooled the model, the grounding validators would still reject any price or place that was not in the real results (defense in depth).
- Text rendered in the UI is escaped so `$` in model output cannot be misread as math.

## Tests and evals

```powershell
.\.venv-windows\Scripts\python.exe -m unittest discover -s evals -p "test_*.py"   # 20 tests
.\.venv-windows\Scripts\python.exe -m evals.run_evals                              # 5 budget scenarios
```

- **20 unit/integration/UI tests** (`evals/test_live_search.py`) use recorded-shape provider fixtures and a fake Claude tool loop, so they need **no API keys and make no paid calls**. They cover: request parameters and result normalization, caching, hotel whole-stay pricing, **grounding rejection** of changed prices / unreturned offers / invented places, empty and malformed provider responses, redacted errors, **parallel delegation**, **activity-failure fallback**, reuse-vs-re-search on revision, missing/inconsistent trip details, invalid iteration counts, missing credentials, top-3 ranking, Google links, and the Streamlit UI (key loading, results tables, "View more" buttons).
- **5 mock-mode budget evals** (`evals/run_evals.py`): easy, tight, very tight, one that requires a revision loop, and one impossible budget that must be flagged.
- **Live verification** was done by hand with real keys (trace shows `live_search`, `tool_call`, `done`); the live path is not part of the automated suite because it costs money and prices change.

## Run it

```powershell
python -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-windows\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

macOS/Linux: `python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m streamlit run app.py`

- **Mock mode (default):** no keys needed. Deterministic agents over a small demo catalog (Tokyo, Paris, Lisbon). Good for trying the architecture.
- **Live mode:** needs your own [Anthropic](https://console.anthropic.com/) and [SerpAPI](https://serpapi.com/) keys. Set `ANTHROPIC_API_KEY` and `SERPAPI_KEY` as environment variables, **or** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` (git-ignored) and fill it in. If neither is set, password fields appear in the sidebar (kept in session memory only). Models: coordinator `claude-sonnet-5-5`, sub-agents `claude-haiku-5-5` (constants in `orchestrator.py` / `agents.py`).

**What to type in live mode** (one text box): destination (hotel city), departure and arrival airports, departure date (flight departure + hotel check-in), return date (return flight + hotel check-out), and budget in USD. Optionally interests (culture, food, art, tech, nature, nightlife). Example:

```
Hotel in Las Vegas. Fly SEA to Las Vegas. Departure 11/11/2026, return 11/18/2026. Budget of $2000.
```

**What you get:** top-3 flights and hotels (the agent's pick first, then the best alternatives from the same search) with a "View more results on Google" button each; top activities with per-place Maps links and a "View more activities on Google" button; a budget summary; and the agent trace.

## Project layout

| File | Role |
|---|---|
| `app.py` | Streamlit UI, key loading, result rendering |
| `orchestrator.py` | Coordinator: `parse_request`, parallel dispatch, retry, revision loop (`plan_trip`) |
| `agents.py` | The three sub-agents (mock and live variants), shared `Ctx` |
| `llm.py` | Generic tool-use loop (`run_agent`) with `submit_result` and result validation hook |
| `live_search.py` | SerpAPI client, normalization, caching, grounding validators, top-3 ranking |
| `budget.py` | Deterministic budget agent: caps, shares, targeted feedback |
| `schemas.py` | Pydantic contracts between parent and sub-agents |
| `tools.py`, `mock_data.py` | Mock catalog tools and data |
| `tracing.py` | Thread-safe event trace |
| `evals/` | Unit/integration/UI tests and budget evals |

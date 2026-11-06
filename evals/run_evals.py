"""Run: python -m evals.run_evals   (mock mode; add live later by passing a client)"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agents import Ctx
from orchestrator import plan_trip
from schemas import Constraints
from tracing import Trace

CASES = [
    ("revision",    Constraints(destination="Tokyo", days=3, budget=1200, interests=["food", "culture"]), True),
    ("easy",        Constraints(destination="Tokyo", days=4, budget=3000, interests=["food"]), True),
    ("tight",       Constraints(destination="Paris", days=4, budget=1500, interests=["art"]), True),
    ("very tight",  Constraints(destination="Lisbon", days=3, budget=1000, interests=["food", "nature"]), True),
    ("impossible",  Constraints(destination="Tokyo", days=7, budget=500, interests=["food"]), False),
]

passed = 0
for name, c, should_fit in CASES:
    r = asyncio.run(plan_trip(c, Ctx(mode="mock", trace=Trace())))
    ok = r.report.within_budget == should_fit and (should_fit or r.notes)   # impossible case must be flagged, not hidden
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:11} total=${r.report.total:.0f}/{c.budget:.0f} iters={r.iterations}")
print(f"{passed}/{len(CASES)} passed")

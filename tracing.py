"""Tiny thread-safe tracer: shows which agent did what, and when."""
import threading, time


class Trace:
    def __init__(self):
        self.events, self._lock, self._t0 = [], threading.Lock(), time.time()

    def log(self, agent: str, event: str, detail: str = ""):
        with self._lock:
            self.events.append({"t(s)": round(time.time() - self._t0, 2), "agent": agent, "event": event, "detail": detail[:200]})

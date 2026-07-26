"""单次遍历的性能、效果和效率指标。"""

import json
import os
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone


class RunMetrics:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_monotonic = time.monotonic()
        self.counters = Counter()
        self.context = {}
        self.timings = defaultdict(
            lambda: {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0}
        )

    def inc(self, name: str, amount: int = 1):
        self.counters[name] += amount

    def set_context(self, **values):
        self.context.update(values)

    def observe(self, name: str, seconds: float):
        metric = self.timings[name]
        metric["count"] += 1
        metric["total_seconds"] += seconds
        metric["max_seconds"] = max(metric["max_seconds"], seconds)

    @contextmanager
    def timed(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - started)

    def snapshot(self) -> dict:
        elapsed = max(time.monotonic() - self.started_monotonic, 1e-9)
        counters = dict(self.counters)
        actions = counters.get("actions_attempted", 0)
        transitions = counters.get("transitions_detected", 0)
        capture_attempts = counters.get("capture_attempts", 0)
        captures = counters.get("screenshots_saved", 0)
        duplicates = counters.get("image_duplicates_rejected", 0)
        explored = counters.get("states_explored", 0)

        timings = {}
        for name, metric in self.timings.items():
            count = metric["count"]
            timings[name] = {
                **metric,
                "average_seconds": (
                    metric["total_seconds"] / count if count else 0.0
                ),
            }

        return {
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "context": self.context,
            "counters": counters,
            "timings": timings,
            "rates": {
                "screenshots_per_hour": captures * 3600 / elapsed,
                "transition_rate_per_action": (
                    transitions / actions if actions else 0.0
                ),
                "capture_success_rate": (
                    captures / capture_attempts if capture_attempts else 0.0
                ),
                "duplicate_rejection_rate": (
                    duplicates / capture_attempts if capture_attempts else 0.0
                ),
                "screenshots_per_explored_state": (
                    captures / explored if explored else 0.0
                ),
                "actions_per_saved_screenshot": (
                    actions / captures if captures else 0.0
                ),
            },
        }

    def write(self, filepath: str):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        temporary = f"{filepath}.tmp"
        with open(temporary, "w", encoding="utf-8") as output:
            json.dump(self.snapshot(), output, ensure_ascii=False, indent=2)
        os.replace(temporary, filepath)

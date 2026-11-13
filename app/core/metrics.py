"""Minimal Prometheus metrics (text exposition format), no extra dependency.

docs/ai/AI_ARCHITECTURE.md §8: ai_runs_total{status}, ai_handoff_total{reason},
ai_run_latency_seconds, ai_tokens_total{model}. Values are per process: with
several Gunicorn workers, scrape each or aggregate (switch to prometheus_client
multiprocess mode when that matters).
"""

import threading
from collections import defaultdict

_LOCK = threading.Lock()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    return (
        "{"
        + ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
        + "}"
    )


class Counter:
    def __init__(self, name: str, help_text: str) -> None:
        self.name, self.help = name, help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with _LOCK:
            self._values[tuple(sorted(labels.items()))] += amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, value in sorted(self._values.items()):
            lines.append(f"{self.name}{_labels(dict(key))} {value}")
        return lines


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...]) -> None:
        self.name, self.help, self.buckets = name, help_text, buckets
        self._counts = [0] * len(buckets)
        self._sum = 0.0
        self._total = 0

    def observe(self, value: float) -> None:
        with _LOCK:
            self._sum += value
            self._total += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self._counts[i] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for bound, count in zip(self.buckets, self._counts, strict=True):
            lines.append(f'{self.name}_bucket{{le="{bound}"}} {count}')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self._total}')
        lines.append(f"{self.name}_sum {self._sum}")
        lines.append(f"{self.name}_count {self._total}")
        return lines


AI_RUNS = Counter("ai_runs_total", "AI agent runs by final status")
AI_HANDOFFS = Counter("ai_handoff_total", "AI handoffs to humans by reason")
AI_RUN_LATENCY = Histogram(
    "ai_run_latency_seconds", "AI run duration", (0.5, 1, 2, 5, 10, 20, 30, 45, 60)
)
AI_TOKENS = Counter("ai_tokens_total", "LLM tokens used by the AI agent, by model")

REGISTRY = (AI_RUNS, AI_HANDOFFS, AI_RUN_LATENCY, AI_TOKENS)


def render() -> str:
    return "\n".join(line for metric in REGISTRY for line in metric.render()) + "\n"

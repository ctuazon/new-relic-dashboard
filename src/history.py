"""Rolling statistics used to derive a default anomaly threshold per monitored series."""
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


@dataclass
class Reading:
    timestamp: float
    value: float


class SeriesHistory:
    def __init__(self, maxlen: int = 1000):
        self._readings: Deque[Reading] = deque(maxlen=maxlen)

    def add(self, timestamp: float, value: float) -> None:
        self._readings.append(Reading(timestamp, value))

    def __len__(self) -> int:
        return len(self._readings)

    @property
    def values(self):
        return [r.value for r in self._readings]

    def mean(self) -> Optional[float]:
        vals = self.values
        return statistics.fmean(vals) if vals else None

    def stdev(self) -> float:
        vals = self.values
        if len(vals) < 2:
            return 0.0
        return statistics.pstdev(vals)

    def auto_threshold(self, multiplier: float, min_samples: int, fallback: float) -> float:
        """Default anomaly threshold: mean + multiplier * stdev of this series' own history,
        so far. Falls back to a flat percentage until enough samples have been collected."""
        if len(self._readings) < min_samples:
            return fallback
        mean = self.mean() or 0.0
        return mean + multiplier * self.stdev()

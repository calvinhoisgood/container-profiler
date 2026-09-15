"""Agent self-telemetry for sampling health.

The profiler should be able to explain the quality of its own measurements.
This module tracks scheduling lag, collection latency, overruns, skipped ticks,
and collector failures without depending on Qt, Docker, or hardware backends.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import statistics
import threading
from typing import Deque, Optional


@dataclass(frozen=True, slots=True)
class SamplingHealthSnapshot:
    cycles: int
    successful_cycles: int
    failed_cycles: int
    skipped_ticks: int
    overrun_cycles: int
    collection_latency_ms_mean: Optional[float]
    collection_latency_ms_p95: Optional[float]
    collection_latency_ms_max: Optional[float]
    scheduling_lag_ms_mean: Optional[float]
    scheduling_lag_ms_p95: Optional[float]
    scheduling_lag_ms_max: Optional[float]
    last_collection_latency_ms: Optional[float]
    last_scheduling_lag_ms: Optional[float]
    last_queue_depth: Optional[int]
    max_queue_depth: Optional[int]

    @property
    def success_ratio(self) -> Optional[float]:
        return None if self.cycles == 0 else self.successful_cycles / self.cycles

    @property
    def overrun_ratio(self) -> Optional[float]:
        return None if self.cycles == 0 else self.overrun_cycles / self.cycles


class AgentSelfTelemetry:
    """Thread-safe rolling health metrics for the sampler itself."""

    def __init__(self, *, interval_s: float, window_size: int = 1024) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        if window_size <= 0:
            raise ValueError("window_size must be > 0")
        self.interval_s = float(interval_s)
        self._latencies_ms: Deque[float] = deque(maxlen=window_size)
        self._lags_ms: Deque[float] = deque(maxlen=window_size)
        self._queue_depths: Deque[int] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self._cycles = 0
        self._successful = 0
        self._failed = 0
        self._skipped_ticks = 0
        self._overruns = 0

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * percentile
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[lower]
        fraction = rank - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    def record_cycle(
        self,
        *,
        scheduled_at: float,
        started_at: float,
        finished_at: float,
        success: bool,
        queue_depth: int | None = None,
    ) -> None:
        if finished_at < started_at:
            raise ValueError("finished_at must be >= started_at")
        if queue_depth is not None and queue_depth < 0:
            raise ValueError("queue_depth must be >= 0")

        lag_ms = max(0.0, (started_at - scheduled_at) * 1000.0)
        latency_ms = (finished_at - started_at) * 1000.0
        overrun = finished_at > scheduled_at + self.interval_s

        with self._lock:
            self._cycles += 1
            self._successful += int(success)
            self._failed += int(not success)
            self._overruns += int(overrun)
            self._latencies_ms.append(latency_ms)
            self._lags_ms.append(lag_ms)
            if queue_depth is not None:
                self._queue_depths.append(int(queue_depth))

    def record_skipped_ticks(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        with self._lock:
            self._skipped_ticks += count

    def snapshot(self) -> SamplingHealthSnapshot:
        with self._lock:
            latencies = list(self._latencies_ms)
            lags = list(self._lags_ms)
            queue_depths = list(self._queue_depths)
            cycles = self._cycles
            successful = self._successful
            failed = self._failed
            skipped = self._skipped_ticks
            overruns = self._overruns

        return SamplingHealthSnapshot(
            cycles=cycles,
            successful_cycles=successful,
            failed_cycles=failed,
            skipped_ticks=skipped,
            overrun_cycles=overruns,
            collection_latency_ms_mean=statistics.fmean(latencies) if latencies else None,
            collection_latency_ms_p95=self._percentile(latencies, 0.95),
            collection_latency_ms_max=max(latencies) if latencies else None,
            scheduling_lag_ms_mean=statistics.fmean(lags) if lags else None,
            scheduling_lag_ms_p95=self._percentile(lags, 0.95),
            scheduling_lag_ms_max=max(lags) if lags else None,
            last_collection_latency_ms=latencies[-1] if latencies else None,
            last_scheduling_lag_ms=lags[-1] if lags else None,
            last_queue_depth=queue_depths[-1] if queue_depths else None,
            max_queue_depth=max(queue_depths) if queue_depths else None,
        )


def advance_fixed_rate_deadline(
    current_deadline: float, finished_at: float, interval_s: float
) -> tuple[float, int]:
    """Advance a fixed-rate deadline without a catch-up burst.

    Returns ``(next_deadline, skipped_ticks)``. If scheduled ticks elapsed while
    collection was running, those ticks are skipped and reported rather than
    executing back-to-back samples.
    """
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    next_deadline = current_deadline + interval_s
    if finished_at < next_deadline:
        return next_deadline, 0
    skipped = math.floor((finished_at - next_deadline) / interval_s) + 1
    return next_deadline + skipped * interval_s, skipped

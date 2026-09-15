"""Shared data models for profiler backends and UI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True, frozen=True)
class ContainerInfo:
    """A concise snapshot of container identity, lifecycle state, and tags.

    Network/runtime fields come directly from Docker inspect metadata and feed
    Datadog-style Autodiscovery template resolution without an extra daemon
    round-trip from the GUI.
    """

    id: str
    name: str
    status: str
    image: str
    created: str
    labels: tuple[tuple[str, str], ...] = ()
    tags: tuple[str, ...] = ()
    env: Optional[str] = None
    service: Optional[str] = None
    version: Optional[str] = None
    primary_host: Optional[str] = None
    network_hosts: tuple[tuple[str, str], ...] = ()
    exposed_ports: tuple[int, ...] = ()
    hostname: Optional[str] = None
    pid: Optional[int] = None


@dataclass(slots=True, frozen=True)
class ContainerStats:
    """One Docker resource sample.

    Network byte counters are cumulative values from Docker. Network rates are
    derived locally from consecutive samples and may be ``None`` on the first
    sample for a container.
    """

    timestamp: float
    cpu_percent: float
    memory_mb: float
    memory_limit_mb: float
    memory_percent: float
    network_rx_bytes: int
    network_tx_bytes: int
    network_rx_bps: Optional[float] = None
    network_tx_bps: Optional[float] = None
    pids: Optional[int] = None


@dataclass(slots=True, frozen=True)
class PowerStats:
    """One host power/GPU sample. ``None`` means unavailable, not zero."""

    cpu_power_w: Optional[float] = None
    gpu_power_w: Optional[float] = None
    gpu_util_percent: Optional[float] = None
    gpu_memory_mb: Optional[float] = None
    gpu_memory_total_mb: Optional[float] = None
    gpu_temp_c: Optional[float] = None

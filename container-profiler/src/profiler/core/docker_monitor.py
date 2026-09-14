"""Docker container discovery and resource sampling.

The module deliberately keeps the Docker SDK import lazy so the rest of the
project (and unit tests) can be imported on machines without Docker installed.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from .discovery import DockerEventWatcher, extract_container_metadata
from .models import ContainerInfo, ContainerStats

ClientFactory = Callable[[], Any]
Clock = Callable[[], float]


class DockerMonitor:
    """Small, testable wrapper around the Docker SDK."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        client_factory: ClientFactory | None = None,
        monotonic_clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
    ) -> None:
        self.client: Any | None = client
        self._client_factory = client_factory or self._default_client_factory
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._network_baselines: dict[str, tuple[float, int, int]] = {}
        self.last_error: str | None = None

        if self.client is None:
            self._connect()

    @staticmethod
    def _default_client_factory() -> Any:
        import docker

        return docker.from_env()

    def _set_error(self, exc: BaseException | str) -> None:
        self.last_error = str(exc)

    def _connect(self) -> bool:
        try:
            client = self._client_factory()
            client.ping()
            self.client = client
            self.last_error = None
            return True
        except Exception as exc:
            self.client = None
            self._set_error(exc)
            return False

    def is_connected(self) -> bool:
        if self.client is None:
            return False
        try:
            self.client.ping()
            self.last_error = None
            return True
        except Exception as exc:
            self._set_error(exc)
            return False

    def _ensure_client(self) -> bool:
        return self.is_connected() or self._connect()

    def list_containers(self, all: bool = True) -> list[ContainerInfo]:
        """Return containers visible to the active Docker context with normalized tags."""
        if not self._ensure_client():
            return []

        try:
            containers = self.client.containers.list(all=all)
            result: list[ContainerInfo] = []
            for container in containers:
                image_tags = getattr(getattr(container, "image", None), "tags", None) or []
                attrs = getattr(container, "attrs", {}) or {}
                config = attrs.get("Config", {}) or {}
                configured_image = config.get("Image")
                image = str(image_tags[0] if image_tags else configured_image or "unknown")
                name = str(getattr(container, "name", "unknown"))
                metadata = extract_container_metadata(
                    config.get("Labels") or attrs.get("Labels") or {},
                    container_name=name,
                    image=image,
                )
                result.append(
                    ContainerInfo(
                        id=str(getattr(container, "short_id", None) or container.id),
                        name=name,
                        status=str(getattr(container, "status", "unknown")),
                        image=image,
                        created=str(attrs.get("Created", "")),
                        labels=metadata.labels,
                        tags=metadata.tags,
                        env=metadata.env,
                        service=metadata.service,
                        version=metadata.version,
                    )
                )
            self.last_error = None
            return result
        except Exception as exc:
            self._set_error(exc)
            return []

    def create_event_watcher(self) -> DockerEventWatcher | None:
        """Create a cancellable local Docker container-event watcher.

        The Docker SDK returns a cancellable stream for ``client.events``. The
        watcher keeps that blocking detail out of the GUI and normalizes event
        payloads before consumers see them.
        """
        if not self._ensure_client():
            return None

        def stream_factory():
            return self.client.events(decode=True, filters={"type": "container"})

        return DockerEventWatcher(stream_factory)

    @staticmethod
    def _cpu_percent(stats: dict[str, Any]) -> float:
        current = stats.get("cpu_stats", {}) or {}
        previous = stats.get("precpu_stats", {}) or {}
        current_usage = current.get("cpu_usage", {}) or {}
        previous_usage = previous.get("cpu_usage", {}) or {}

        cpu_delta = float(current_usage.get("total_usage", 0) or 0) - float(
            previous_usage.get("total_usage", 0) or 0
        )
        system_delta = float(current.get("system_cpu_usage", 0) or 0) - float(
            previous.get("system_cpu_usage", 0) or 0
        )

        online_cpus = current.get("online_cpus")
        if not online_cpus:
            online_cpus = len(current_usage.get("percpu_usage") or []) or 1

        if cpu_delta <= 0 or system_delta <= 0:
            return 0.0
        return max(0.0, (cpu_delta / system_delta) * float(online_cpus) * 100.0)

    @staticmethod
    def _memory_values(stats: dict[str, Any]) -> tuple[float, float, float]:
        memory = stats.get("memory_stats", {}) or {}
        raw_usage = float(memory.get("usage", memory.get("privateworkingset", 0)) or 0)
        limit = float(memory.get("limit", 0) or 0)
        details = memory.get("stats", {}) or {}

        reclaimable = details.get("inactive_file")
        if reclaimable is None:
            reclaimable = details.get("total_inactive_file")
        if reclaimable is None:
            reclaimable = 0

        usage = max(0.0, raw_usage - float(reclaimable or 0))
        percent = (usage / limit * 100.0) if limit > 0 else 0.0
        mib = 1024.0 * 1024.0
        return usage / mib, limit / mib, percent

    @staticmethod
    def _network_totals(stats: dict[str, Any]) -> tuple[int, int]:
        networks = stats.get("networks", {}) or {}
        rx_bytes = sum(int(values.get("rx_bytes", 0) or 0) for values in networks.values())
        tx_bytes = sum(int(values.get("tx_bytes", 0) or 0) for values in networks.values())
        return rx_bytes, tx_bytes

    def _network_rates(
        self, container_id: str, now: float, rx_bytes: int, tx_bytes: int
    ) -> tuple[Optional[float], Optional[float]]:
        previous = self._network_baselines.get(container_id)
        self._network_baselines[container_id] = (now, rx_bytes, tx_bytes)
        if previous is None:
            return None, None

        previous_time, previous_rx, previous_tx = previous
        elapsed = now - previous_time
        if elapsed <= 0:
            return None, None

        rx_delta = max(0, rx_bytes - previous_rx)
        tx_delta = max(0, tx_bytes - previous_tx)
        return rx_delta / elapsed, tx_delta / elapsed

    def reset_container_baseline(self, container_id: str) -> None:
        self._network_baselines.pop(container_id, None)

    def get_stats(self, container_id: str) -> Optional[ContainerStats]:
        """Collect one non-streaming Docker stats snapshot."""
        if not self._ensure_client():
            return None

        try:
            container = self.client.containers.get(container_id)
            raw = container.stats(stream=False)
            now_mono = self._monotonic_clock()
            rx_bytes, tx_bytes = self._network_totals(raw)
            rx_bps, tx_bps = self._network_rates(container_id, now_mono, rx_bytes, tx_bytes)
            memory_mb, memory_limit_mb, memory_percent = self._memory_values(raw)
            pids = (raw.get("pids_stats", {}) or {}).get("current")

            result = ContainerStats(
                timestamp=self._wall_clock(),
                cpu_percent=round(self._cpu_percent(raw), 2),
                memory_mb=round(memory_mb, 2),
                memory_limit_mb=round(memory_limit_mb, 2),
                memory_percent=round(memory_percent, 2),
                network_rx_bytes=rx_bytes,
                network_tx_bytes=tx_bytes,
                network_rx_bps=round(rx_bps, 2) if rx_bps is not None else None,
                network_tx_bps=round(tx_bps, 2) if tx_bps is not None else None,
                pids=int(pids) if pids is not None else None,
            )
            self.last_error = None
            return result
        except Exception as exc:
            self._set_error(exc)
            return None

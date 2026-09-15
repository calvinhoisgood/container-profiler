"""Docker inspect metadata used by Datadog-style Autodiscovery templates.

This module is intentionally Docker-SDK agnostic: it accepts the plain mapping
returned by ``container.attrs`` / ``docker inspect`` so parsing can be tested
without a daemon.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

_PORT_KEY_RE = re.compile(r"^(\d+)/(?:tcp|udp|sctp)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ContainerTemplateContext:
    """Network/runtime values available to an Autodiscovery template."""

    primary_host: str | None
    network_hosts: tuple[tuple[str, str], ...] = ()
    ports: tuple[int, ...] = ()
    hostname: str | None = None
    pid: int | None = None

    def host_for_network(self, name: str) -> str | None:
        """Return a named-network IP, falling back to the primary host.

        Datadog documents ``%%host_<NETWORK>%%`` as falling back to
        ``%%host%%`` when the named network cannot be found.
        """
        mapping = dict(self.network_hosts)
        return mapping.get(name) or self.primary_host


def _port_from_key(value: Any) -> int | None:
    match = _PORT_KEY_RE.match(str(value))
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def extract_container_template_context(
    attrs: Mapping[str, Any] | None,
) -> ContainerTemplateContext:
    """Extract network IPs, exposed ports, hostname and PID from inspect data.

    ``NetworkSettings.Networks`` is the canonical per-network location in
    current Docker Engine APIs. IPv4 is preferred for each network with global
    IPv6 used as a fallback. The bridge network is preferred as the generic
    host when present; otherwise network names are ordered deterministically.
    """
    attrs = attrs or {}
    network_settings = attrs.get("NetworkSettings") or {}
    if not isinstance(network_settings, Mapping):
        network_settings = {}

    raw_networks = network_settings.get("Networks") or {}
    networks: list[tuple[str, str]] = []
    if isinstance(raw_networks, Mapping):
        for raw_name, raw_values in raw_networks.items():
            if not isinstance(raw_values, Mapping):
                continue
            address = str(
                raw_values.get("IPAddress")
                or raw_values.get("GlobalIPv6Address")
                or ""
            ).strip()
            if address:
                networks.append((str(raw_name), address))
    networks.sort(key=lambda item: (item[0] != "bridge", item[0]))
    primary_host = networks[0][1] if networks else None

    config = attrs.get("Config") or {}
    if not isinstance(config, Mapping):
        config = {}
    host_config = attrs.get("HostConfig") or {}
    if not isinstance(host_config, Mapping):
        host_config = {}

    ports: set[int] = set()
    for source in (
        config.get("ExposedPorts"),
        network_settings.get("Ports"),
        host_config.get("PortBindings"),
    ):
        if not isinstance(source, Mapping):
            continue
        for key in source:
            port = _port_from_key(key)
            if port is not None:
                ports.add(port)

    raw_hostname = config.get("Hostname")
    hostname = str(raw_hostname) if raw_hostname else None

    pid: int | None = None
    state = attrs.get("State") or {}
    if isinstance(state, Mapping):
        try:
            raw_pid = int(state.get("Pid") or 0)
        except (TypeError, ValueError):
            raw_pid = 0
        if raw_pid > 0:
            pid = raw_pid

    return ContainerTemplateContext(
        primary_host=primary_host,
        network_hosts=tuple(networks),
        ports=tuple(sorted(ports)),
        hostname=hostname,
        pid=pid,
    )

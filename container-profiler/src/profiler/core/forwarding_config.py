"""Strict, disabled-by-default configuration for remote telemetry forwarding."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class ForwardingConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ForwardingConfig:
    enabled: bool = False
    metrics_endpoint: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    batch_points: int = 250
    max_payload_bytes: int = 1 * 1024 * 1024
    timeout_s: float = 5.0
    poll_interval_s: float = 1.0
    base_delay_s: float = 1.0
    max_delay_s: float = 300.0
    jitter: float = 0.2
    queue_max_items: int = 50_000
    queue_max_bytes: int = 256 * 1024 * 1024

    @property
    def endpoints(self) -> dict[str, str]:
        return {"metrics": self.metrics_endpoint} if self.metrics_endpoint else {}

    @property
    def header_mapping(self) -> dict[str, str]:
        return dict(self.headers)


_ALLOWED_FIELDS = {
    "enabled", "metrics_endpoint", "headers", "headers_from_env",
    "batch_points", "max_payload_bytes", "timeout_s", "poll_interval_s",
    "base_delay_s", "max_delay_s", "jitter", "queue_max_items",
    "queue_max_bytes", "allow_insecure_http",
}


def _int_field(raw: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool):
        raise ForwardingConfigError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ForwardingConfigError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise ForwardingConfigError(f"{name} must be between {low} and {high}")
    return number


def _float_field(raw: Mapping[str, Any], name: str, default: float, low: float, high: float) -> float:
    try:
        number = float(raw.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ForwardingConfigError(f"{name} must be numeric") from exc
    if not low <= number <= high:
        raise ForwardingConfigError(f"{name} must be between {low:g} and {high:g}")
    return number


def _validate_endpoint(value: Any, *, allow_insecure_http: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForwardingConfigError("metrics_endpoint is required when forwarding is enabled")
    endpoint = value.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ForwardingConfigError("metrics_endpoint must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ForwardingConfigError("credentials must not be embedded in metrics_endpoint")
    if parsed.fragment:
        raise ForwardingConfigError("metrics_endpoint fragments are not supported")
    if parsed.scheme == "http" and not allow_insecure_http:
        if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ForwardingConfigError(
                "plain HTTP forwarding is limited to loopback unless allow_insecure_http is true"
            )
    return endpoint


def _parse_headers(
    raw: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    require_environment_values: bool,
) -> tuple[tuple[str, str], ...]:
    result: dict[str, str] = {}
    literal = raw.get("headers", {})
    if not isinstance(literal, Mapping):
        raise ForwardingConfigError("headers must be an object")
    for key, value in literal.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
            raise ForwardingConfigError("headers must map non-empty strings to strings")
        if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
            raise ForwardingConfigError("header names/values must not contain newlines")
        result[key.strip()] = value

    env_headers = raw.get("headers_from_env", {})
    if not isinstance(env_headers, Mapping):
        raise ForwardingConfigError("headers_from_env must be an object")
    for header, variable in env_headers.items():
        if not isinstance(header, str) or not header.strip() or not isinstance(variable, str) or not variable:
            raise ForwardingConfigError("headers_from_env must map header names to env variable names")
        value = env.get(variable)
        if value is None:
            if require_environment_values:
                raise ForwardingConfigError(f"environment variable {variable!r} is not set")
            # Disabled configuration is inert: validate the reference but do not
            # require operators to export secrets until forwarding is enabled.
            continue
        if "\n" in value or "\r" in value:
            raise ForwardingConfigError(f"environment variable {variable!r} contains a newline")
        result[header.strip()] = value
    if len(result) > 32:
        raise ForwardingConfigError("too many forwarding headers")
    return tuple(sorted(result.items()))


def parse_forwarding_config(
    raw: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> ForwardingConfig:
    unknown = set(raw) - _ALLOWED_FIELDS
    if unknown:
        raise ForwardingConfigError("unknown forwarding fields: " + ", ".join(sorted(unknown)))
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ForwardingConfigError("enabled must be boolean")
    allow_insecure = raw.get("allow_insecure_http", False)
    if not isinstance(allow_insecure, bool):
        raise ForwardingConfigError("allow_insecure_http must be boolean")

    endpoint_raw = raw.get("metrics_endpoint")
    endpoint = None
    if enabled:
        endpoint = _validate_endpoint(endpoint_raw, allow_insecure_http=allow_insecure)
    elif endpoint_raw is not None:
        endpoint = _validate_endpoint(endpoint_raw, allow_insecure_http=allow_insecure)

    headers = _parse_headers(
        raw,
        os.environ if env is None else env,
        require_environment_values=enabled,
    )
    batch_points = _int_field(raw, "batch_points", 250, 1, 5000)
    max_payload_bytes = _int_field(
        raw, "max_payload_bytes", 1024 * 1024, 1024, 4 * 1024 * 1024
    )
    queue_max_bytes = _int_field(
        raw, "queue_max_bytes", 256 * 1024 * 1024,
        max_payload_bytes, 4 * 1024 * 1024 * 1024,
    )
    base_delay_s = _float_field(raw, "base_delay_s", 1.0, 0.1, 3600.0)
    max_delay_s = _float_field(raw, "max_delay_s", 300.0, 0.1, 86400.0)
    if max_delay_s < base_delay_s:
        raise ForwardingConfigError("max_delay_s must be >= base_delay_s")

    return ForwardingConfig(
        enabled=enabled,
        metrics_endpoint=endpoint,
        headers=headers,
        batch_points=batch_points,
        max_payload_bytes=max_payload_bytes,
        timeout_s=_float_field(raw, "timeout_s", 5.0, 0.1, 120.0),
        poll_interval_s=_float_field(raw, "poll_interval_s", 1.0, 0.05, 60.0),
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
        jitter=_float_field(raw, "jitter", 0.2, 0.0, 1.0),
        queue_max_items=_int_field(raw, "queue_max_items", 50_000, 1, 5_000_000),
        queue_max_bytes=queue_max_bytes,
    )


def load_forwarding_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> ForwardingConfig:
    path = Path(path)
    if not path.exists():
        return ForwardingConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ForwardingConfigError(f"cannot load forwarding config: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ForwardingConfigError("forwarding config root must be an object")
    return parse_forwarding_config(raw, env=env)

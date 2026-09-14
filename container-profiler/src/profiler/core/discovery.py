"""Container metadata extraction and event-driven Docker discovery."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

UNIFIED_LABELS = {
    "com.datadoghq.tags.env": "env",
    "com.datadoghq.tags.service": "service",
    "com.datadoghq.tags.version": "version",
}
AUTODISCOVERY_TAGS_LABEL = "com.datadoghq.ad.tags"


@dataclass(slots=True, frozen=True)
class ContainerMetadata:
    labels: tuple[tuple[str, str], ...] = ()
    tags: tuple[str, ...] = ()
    env: str | None = None
    service: str | None = None
    version: str | None = None


@dataclass(slots=True, frozen=True)
class ContainerEvent:
    timestamp: float
    action: str
    container_id: str
    name: str | None = None
    image: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()


def _clean_mapping(raw: Mapping[str, Any] | None) -> dict[str, str]:
    if not raw:
        return {}
    return {str(k): str(v) for k, v in raw.items() if k is not None and v is not None}


def _parse_ad_tags(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items() if k is not None and v is not None}
    if isinstance(parsed, list):
        result: dict[str, str] = {}
        for item in parsed:
            if not isinstance(item, str) or ":" not in item:
                continue
            key, tag_value = item.split(":", 1)
            key = key.strip()
            if key:
                result[key] = tag_value.strip()
        return result
    return {}


def extract_container_metadata(
    labels: Mapping[str, Any] | None,
    *,
    container_name: str | None = None,
    image: str | None = None,
) -> ContainerMetadata:
    """Normalize Docker labels into stable explorer tags.

    Supports Datadog unified-service labels and the
    ``com.datadoghq.ad.tags`` JSON label, plus common Docker Compose identity
    labels useful for local exploration.
    """
    normalized = _clean_mapping(labels)
    tags: dict[str, str] = {}
    if container_name:
        tags["container_name"] = container_name
    if image:
        tags["image_name"] = image

    unified: dict[str, str | None] = {"env": None, "service": None, "version": None}
    for label, tag_name in UNIFIED_LABELS.items():
        value = normalized.get(label)
        if value:
            unified[tag_name] = value
            tags[tag_name] = value

    for label, tag_name in {
        "com.docker.compose.project": "compose_project",
        "com.docker.compose.service": "compose_service",
    }.items():
        value = normalized.get(label)
        if value:
            tags[tag_name] = value

    tags.update(_parse_ad_tags(normalized.get(AUTODISCOVERY_TAGS_LABEL)))
    return ContainerMetadata(
        labels=tuple(sorted(normalized.items())),
        tags=tuple(f"{key}:{value}" for key, value in sorted(tags.items())),
        env=unified["env"],
        service=unified["service"],
        version=unified["version"],
    )


def normalize_container_event(raw: Mapping[str, Any]) -> ContainerEvent | None:
    """Convert a Docker event payload into a compact, stable representation."""
    event_type = str(raw.get("Type") or raw.get("type") or "")
    if event_type and event_type != "container":
        return None

    actor = raw.get("Actor") or raw.get("actor") or {}
    container_id = raw.get("id") or raw.get("ID") or actor.get("ID") or actor.get("id")
    if not container_id:
        return None

    attrs = _clean_mapping(actor.get("Attributes") or actor.get("attributes") or {})
    action = str(raw.get("Action") or raw.get("action") or raw.get("status") or "")
    timestamp = raw.get("timeNano")
    try:
        ts = float(timestamp) / 1_000_000_000.0 if timestamp is not None else float(raw.get("time") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0

    return ContainerEvent(
        timestamp=ts,
        action=action,
        container_id=str(container_id),
        name=attrs.get("name"),
        image=attrs.get("image"),
        attributes=tuple(sorted(attrs.items())),
    )


class DockerEventWatcher:
    """Run Docker's blocking event stream with cooperative cancellation."""

    def __init__(self, stream_factory: Callable[[], Iterable[Mapping[str, Any]]]) -> None:
        self._stream_factory = stream_factory
        self._stop = threading.Event()
        self._stream: Any | None = None
        self.last_error: str | None = None

    def run(self, callback: Callable[[ContainerEvent], None]) -> None:
        self._stop.clear()
        try:
            stream = self._stream_factory()
            self._stream = stream
            for raw in stream:
                if self._stop.is_set():
                    break
                event = normalize_container_event(raw)
                if event is not None:
                    callback(event)
            self.last_error = None
        except Exception as exc:
            if not self._stop.is_set():
                self.last_error = str(exc)
        finally:
            self._stream = None

    def stop(self) -> None:
        self._stop.set()
        close = getattr(self._stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

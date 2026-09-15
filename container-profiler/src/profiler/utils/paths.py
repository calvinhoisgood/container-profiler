"""Path helpers for source and frozen application environments."""
from __future__ import annotations

import sys
from pathlib import Path


def get_base_path() -> Path:
    """Return the application base directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent.parent.parent


def get_resource_path(relative_path: str) -> Path:
    """Resolve a bundled/static resource path."""
    return get_base_path() / relative_path


def get_config_dir() -> Path:
    """Return the user-writable application state directory."""
    config_dir = Path.home() / ".container-profiler"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_output_dir() -> Path:
    """Return the default export directory."""
    output_dir = get_config_dir() / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_telemetry_db_path() -> Path:
    """Return the durable local telemetry database path."""
    return get_config_dir() / "telemetry.sqlite3"


def get_alert_config_path() -> Path:
    """Return the per-user JSON threshold-alert configuration path."""
    return get_config_dir() / "alerts.json"


def get_forwarding_config_path() -> Path:
    """Return the disabled-by-default remote forwarding configuration path."""
    return get_config_dir() / "forwarding.json"


def get_forwarding_db_path() -> Path:
    """Return the durable bounded outbound spool path."""
    return get_config_dir() / "forwarding.sqlite3"

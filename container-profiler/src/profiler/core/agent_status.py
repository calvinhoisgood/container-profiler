"""Atomic heartbeat/status documents for the headless Agent process."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AgentStatusView:
    state: str
    pid: int | None
    started_at: float | None
    updated_at: float | None
    age_s: float | None
    stale: bool
    runtime: dict[str, Any]


def write_agent_status(
    path: str | Path,
    *,
    state: str,
    pid: int,
    started_at: float,
    runtime_snapshot: Any,
    database_path: str | Path,
    updated_at: float | None = None,
) -> None:
    """Atomically replace the status file so readers never see partial JSON."""
    if state not in {"running", "stopped"}:
        raise ValueError("state must be running or stopped")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if updated_at is None else float(updated_at)
    if hasattr(runtime_snapshot, "__dataclass_fields__"):
        runtime = asdict(runtime_snapshot)
    elif isinstance(runtime_snapshot, Mapping):
        runtime = dict(runtime_snapshot)
    else:
        raise TypeError("runtime_snapshot must be a dataclass or mapping")
    payload = {
        "schema_version": 1,
        "state": state,
        "pid": int(pid),
        "started_at": float(started_at),
        "updated_at": now,
        "database": str(database_path),
        "runtime": runtime,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except OSError:
                pass


def read_agent_status(
    path: str | Path,
    *,
    max_age_s: float = 15.0,
    now: float | None = None,
) -> AgentStatusView:
    if max_age_s <= 0:
        raise ValueError("max_age_s must be positive")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported or malformed agent status document")
    state = raw.get("state")
    if state not in {"running", "stopped"}:
        raise ValueError("invalid agent state")
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("invalid runtime status")
    updated_raw = raw.get("updated_at")
    updated_at = float(updated_raw) if updated_raw is not None else None
    current = time.time() if now is None else float(now)
    age = None if updated_at is None else max(0.0, current - updated_at)
    stale = state != "running" or age is None or age > max_age_s
    pid_raw = raw.get("pid")
    started_raw = raw.get("started_at")
    return AgentStatusView(
        state=state,
        pid=int(pid_raw) if pid_raw is not None else None,
        started_at=float(started_raw) if started_raw is not None else None,
        updated_at=updated_at,
        age_s=age,
        stale=stale,
        runtime=dict(runtime),
    )

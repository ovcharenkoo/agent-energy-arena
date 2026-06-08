"""Append-only action log. Every mutating endpoint call lands here regardless
of success — the log plus seed reproduces the game byte-for-byte.

Filesystem allocation is *lazy*: `__init__` only computes paths; the
`runs/<run_id>/` directory is created on the first `append` call. A
log that's constructed and never appended to leaves no trace on
disk. This keeps every `uvicorn` boot, `create_app(world=...)` test
helper, and read-only smoke test from littering the real `runs/`
folder with stub directories."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class ActionLog:
    def __init__(self, root: str | os.PathLike[str] = "runs", run_id: str | None = None) -> None:
        self.root = Path(root)
        self.run_id = run_id or _new_run_id()
        self.dir = self.root / self.run_id
        self.path = self.dir / "actions.jsonl"

    def append(
        self,
        endpoint: str,
        params: dict[str, Any],
        ok: bool,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "endpoint": endpoint,
            "params": params,
            "ok": ok,
        }
        if error is not None:
            entry["error"] = error
        if result is not None:
            entry["result"] = result
        # Materialize on first append — see module docstring.
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=_json_default) + "\n")


def _new_run_id() -> str:
    """Fallback folder name for an ActionLog with no recorder to anchor it.

    Matches the recorder's readable ``<prefix>-<YYYYMMDD-HHMMSS>`` scheme
    (with a uuid tail for same-second uniqueness) so a standalone log never
    produces an opaque ``<epoch>-<uuid>`` directory. In the normal server
    path the log inherits the recorder's run id and this is never reached.
    """
    return f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)

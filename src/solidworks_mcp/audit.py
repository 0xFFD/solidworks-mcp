"""Tamper-evident audit trail.

Every tool call is appended to a JSON Lines file. Each record carries the SHA-256 hash of the previous
record, so editing, reordering or deleting a line breaks the chain, which `solidworks-mcp verify-audit`
detects. This gives a traceable record of what an AI agent did to which document and when.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest(prev: str, record: dict) -> str:
    return hashlib.sha256((prev + _canonical(record)).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._prev: str | None = None
        self._seq = 0
        self._user = _user()
        self._host = socket.gethostname()

    def _tail(self) -> None:
        """Load the hash and sequence number of the last record."""
        self._prev, self._seq = GENESIS, 0
        if self.path is None or not self.path.exists():
            return
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        if last:
            rec = json.loads(last)
            self._prev, self._seq = rec["hash"], rec["seq"]

    def record(self, tool: str, args: dict, ok: bool, result: Any = None, error: str | None = None, document: str | None = None) -> None:
        if self.path is None:
            return
        with self._lock:
            if self._prev is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._tail()
            rec = {
                "seq": self._seq + 1,
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "user": self._user,
                "host": self._host,
                "pid": os.getpid(),
                "tool": tool,
                "args": args,
                "document": document,
                "ok": ok,
            }
            if error:
                rec["error"] = error[:2000]
            if result is not None:
                rec["result"] = _summary(result)
            rec["prev"] = self._prev
            rec["hash"] = _digest(self._prev, rec)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._prev, self._seq = rec["hash"], rec["seq"]


def _summary(result: Any) -> Any:
    """Keep audit records small: the result text up to 1000 characters."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= 1000 else text[:997] + "..."


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "unknown")


def verify(path: Path) -> tuple[bool, int, str]:
    """Check the hash chain. Returns (ok, records checked, message)."""
    if not path.exists():
        return False, 0, f"{path} does not exist"
    prev, n = GENESIS, 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, n, f"line {lineno}: not valid JSON"
            stored = rec.pop("hash", None)
            if rec.get("prev") != prev:
                return False, n, f"line {lineno}: chain broken (record {rec.get('seq')} does not follow the previous record)"
            if _digest(prev, rec) != stored:
                return False, n, f"line {lineno}: record {rec.get('seq')} was modified"
            prev, n = stored, n + 1
    return True, n, f"{n} records, chain intact"

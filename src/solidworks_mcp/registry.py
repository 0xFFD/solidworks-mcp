"""Shared runtime for tools: settings, path policy, audit, and the `tool` decorator.

Tool modules import from here (never from server.py), so they can be imported in any order. The
server collects everything registered here and adds it to the MCP server.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image
from mcp_types import ToolAnnotations

from .audit import AuditLog
from .com import SW, on_com
from .com import configure as configure_com
from .config import Settings
from .errors import SwError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

# (wrapper, annotations) for every tool, in definition order
TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = []


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.audit = AuditLog(settings.audit_path)
        configure_com(settings.launch, settings.visible)

    def path(self, value: str, must_exist: bool = False, suffixes: tuple[str, ...] | None = None) -> Path:
        """Resolve a user path: relative paths land in the workspace, absolute paths must be inside an allowed root."""
        if not value or not value.strip():
            raise SwError("A file path is required.")
        p = Path(value.strip().strip('"')).expanduser()
        if not p.is_absolute():
            p = self.settings.workspace / p
        p = p.resolve()
        roots = self.settings.roots
        if not any(p == r or r in p.parents for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise SwError(f"{p} is outside the allowed folders ({allowed}). Start the server with --root to allow another folder.")
        if suffixes and p.suffix.lower() not in suffixes:
            raise SwError(f"{p.name}: expected one of {', '.join(suffixes)}.")
        if must_exist and not p.exists():
            raise SwError(f"{p} does not exist.")
        if not must_exist:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p


APP: App | None = None


def app() -> App:
    global APP
    if APP is None:
        APP = App(Settings.from_env())
    return APP


def configure(settings: Settings) -> App:
    global APP
    settings.workspace.mkdir(parents=True, exist_ok=True)
    APP = App(settings)
    return APP


def encode(result: Any) -> Any:
    """One compact JSON text block per result (images pass through)."""
    if isinstance(result, Image):
        return result
    if isinstance(result, list) and any(isinstance(x, Image) for x in result):
        return [x if isinstance(x, Image) else encode(x) for x in result]
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _active_title() -> str | None:
    try:
        doc = SW.app.ActiveDoc if SW._app is not None else None
        if doc is None:
            return None
        doc = SW.cast(doc, "IModelDoc2")
        return doc.GetPathName() or doc.GetTitle()
    except Exception:
        return None


def _com_message(e: Exception) -> str:
    try:
        hr, msg, exc, _ = e.args
        if exc and exc[2]:
            msg = f"{msg}: {exc[2]}"
        return f"SolidWorks COM error {hr & 0xFFFFFFFF:#010x} ({msg}). The operation was not completed."
    except Exception:
        return f"SolidWorks COM error: {e}"


def _auditable(result: Any) -> Any:
    if isinstance(result, Image):
        return None
    if isinstance(result, list):
        return [x for x in result if not isinstance(x, Image)]
    return result


def tool(annotations: ToolAnnotations):
    """Register a tool. Each call runs on the COM thread, is written to the audit log, and is refused in
    read-only mode unless the tool is annotated read-only. The undecorated function is returned."""

    def deco(fn):
        sig = inspect.signature(fn)
        writes = not annotations.read_only_hint

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            import pywintypes

            a = app()
            call_args = dict(sig.bind_partial(*args, **kwargs).arguments)
            if writes and a.settings.read_only:
                err = f"{fn.__name__} changes the model or files, but the server runs in read-only mode."
                a.audit.record(fn.__name__, call_args, False, error=err)
                raise SwError(err)
            try:
                result = on_com(lambda: fn(*args, **kwargs))
            except SwError as e:
                a.audit.record(fn.__name__, call_args, False, error=str(e), document=on_com(_active_title))
                raise
            except pywintypes.com_error as e:
                msg = _com_message(e)
                a.audit.record(fn.__name__, call_args, False, error=msg, document=on_com(_active_title))
                raise SwError(msg) from None
            except Exception as e:  # noqa: BLE001 - report and audit unexpected failures instead of an empty error
                msg = f"Internal error in {fn.__name__}: {type(e).__name__}: {e}"
                a.audit.record(fn.__name__, call_args, False, error=msg, document=on_com(_active_title))
                raise SwError(msg) from e
            a.audit.record(fn.__name__, call_args, True, result=_auditable(result), document=on_com(_active_title))
            return encode(result)

        TOOLS.append((wrapper, annotations))
        return fn

    return deco

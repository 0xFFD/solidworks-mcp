"""Runtime settings, resolved from CLI flags and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_WORKSPACE = Path.home() / "solidworks-mcp"
DEFAULT_MARKING_PROPERTY = "ExportControl"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Default folder for saved models, exports, drawings and screenshots. Relative paths resolve here.
    workspace: Path = DEFAULT_WORKSPACE
    # Extra folders (besides the workspace) that tools may read from and write to.
    extra_roots: list[Path] = field(default_factory=list)
    # Only allow tools that do not change models or files.
    read_only: bool = False
    # Refuse exports to neutral formats (STEP, IGES, STL, PDF, DXF, ...) unless the document carries
    # a non-empty export-control marking property.
    require_marking: bool = False
    marking_property: str = DEFAULT_MARKING_PROPERTY
    # Hash-chained JSON Lines audit log of every tool call; None disables it.
    audit_log: Path | None = None
    audit_enabled: bool = True
    # Start SolidWorks when it is not running, and keep its window visible.
    launch: bool = True
    visible: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        if v := os.environ.get("SOLIDWORKS_MCP_WORKSPACE"):
            s.workspace = Path(v).expanduser()
        if v := os.environ.get("SOLIDWORKS_MCP_ROOTS"):
            s.extra_roots = [Path(p).expanduser() for p in v.split(os.pathsep) if p]
        s.read_only = _flag("SOLIDWORKS_MCP_READ_ONLY")
        s.require_marking = _flag("SOLIDWORKS_MCP_REQUIRE_MARKING")
        if v := os.environ.get("SOLIDWORKS_MCP_MARKING_PROPERTY"):
            s.marking_property = v
        if v := os.environ.get("SOLIDWORKS_MCP_AUDIT_LOG"):
            s.audit_log = Path(v).expanduser()
        if _flag("SOLIDWORKS_MCP_NO_AUDIT"):
            s.audit_enabled = False
        if _flag("SOLIDWORKS_MCP_NO_LAUNCH"):
            s.launch = False
        return s

    @property
    def roots(self) -> list[Path]:
        return [self.workspace.resolve(), *(p.resolve() for p in self.extra_roots)]

    @property
    def audit_path(self) -> Path | None:
        if not self.audit_enabled:
            return None
        return (self.audit_log or self.workspace / "audit" / "audit.jsonl").resolve()

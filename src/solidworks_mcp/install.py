"""Register the server with MCP clients: Claude Code, Claude Desktop, Codex CLI, Cursor, VS Code,
Windsurf, Gemini CLI and opencode. SolidWorks runs on Windows, so every path here is a Windows path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tomlkit

NAME = "solidworks"


@dataclass
class Launch:
    command: str
    args: list[str]

    @property
    def argv(self) -> list[str]:
        return [self.command, *self.args]


def launch(server_args: list[str]) -> Launch:
    return Launch(sys.executable, ["-m", "solidworks_mcp", "serve", *server_args])


@dataclass
class Target:
    key: str
    label: str
    path: Path
    marker: Path  # existence means the client is installed
    fmt: str  # mcpServers | claude-code | vscode | opencode | codex | gemini

    def describe(self) -> str:
        return f"{self.label} {self.path}"


def targets() -> list[Target]:
    h = Path.home()
    appdata = Path(os.environ.get("APPDATA") or h / "AppData" / "Roaming")
    codex_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else h / ".codex"
    return [
        Target("claude-code", "Claude Code", h / ".claude.json", h / ".claude", "claude-code"),
        Target("claude-desktop", "Claude Desktop", appdata / "Claude" / "claude_desktop_config.json", appdata / "Claude", "mcpServers"),
        Target("codex", "Codex CLI", codex_home / "config.toml", codex_home, "codex"),
        Target("cursor", "Cursor", h / ".cursor" / "mcp.json", h / ".cursor", "mcpServers"),
        Target("vscode", "VS Code", appdata / "Code" / "User" / "mcp.json", appdata / "Code" / "User", "vscode"),
        Target("windsurf", "Windsurf", h / ".codeium" / "windsurf" / "mcp_config.json", h / ".codeium" / "windsurf", "mcpServers"),
        Target("gemini", "Gemini CLI", h / ".gemini" / "settings.json", h / ".gemini", "gemini"),
        Target("opencode", "opencode", h / ".config" / "opencode" / "opencode.json", h / ".config" / "opencode", "opencode"),
    ]


def entry_for(fmt: str, spec: Launch) -> dict:
    if fmt == "claude-code":
        return {"type": "stdio", "command": spec.command, "args": spec.args, "env": {}}
    if fmt == "vscode":
        return {"type": "stdio", "command": spec.command, "args": spec.args}
    if fmt == "opencode":
        return {"type": "local", "command": spec.argv, "enabled": True}
    if fmt == "gemini":
        return {"command": spec.command, "args": spec.args, "timeout": 600000}
    return {"command": spec.command, "args": spec.args}


def _section_key(fmt: str) -> str:
    return {"vscode": "servers", "opencode": "mcp"}.get(fmt, "mcpServers")


def _codex_table(spec: Launch):
    tbl = tomlkit.table()
    tbl.add("command", spec.command)
    tbl.add("args", spec.args)
    tbl.add("startup_timeout_sec", 60)
    tbl.add("tool_timeout_sec", 600)
    tbl.add(tomlkit.nl())
    return tbl


def snippet(t: Target, spec: Launch) -> str:
    if t.fmt == "codex":
        doc = tomlkit.document()
        doc.add("mcp_servers", {NAME: _codex_table(spec)})
        return tomlkit.dumps(doc)
    return json.dumps({_section_key(t.fmt): {NAME: entry_for(t.fmt, spec)}}, indent=2)


def _backup(path: Path) -> None:
    bak = path.with_name(path.name + ".bak-solidworks-mcp")
    if path.exists() and not bak.exists():
        shutil.copyfile(path, bak)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-solidworks-mcp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not plain JSON ({e}); add the snippet by hand") from None


def apply(t: Target, spec: Launch, remove: bool = False) -> str:
    if t.fmt == "claude-code":
        cli = shutil.which("claude")
        if cli:
            kw = {"capture_output": True, "text": True, "timeout": 60}
            subprocess.run([cli, "mcp", "remove", "--scope", "user", NAME], **kw)
            if remove:
                return "removed (claude mcp remove)"
            r = subprocess.run([cli, "mcp", "add", "--scope", "user", NAME, "--", *spec.argv], **kw)
            if r.returncode == 0:
                return "added (claude mcp add --scope user)"
        # Fall back to editing ~/.claude.json directly.
    if t.fmt == "codex":
        doc = tomlkit.parse(t.path.read_text(encoding="utf-8")) if t.path.exists() else tomlkit.document()
        servers = doc.get("mcp_servers")
        if remove:
            if servers is None or NAME not in servers:
                return "not present"
            del servers[NAME]
        else:
            if servers is None:
                servers = tomlkit.table(is_super_table=True)
                doc.add("mcp_servers", servers)
            servers[NAME] = _codex_table(spec)
        _backup(t.path)
        _atomic_write(t.path, tomlkit.dumps(doc))
        return "removed" if remove else "written"

    data = _load_json(t.path)
    section = data.setdefault(_section_key(t.fmt), {})
    if remove:
        if NAME not in section:
            return "not present"
        del section[NAME]
    else:
        section[NAME] = entry_for(t.fmt, spec)
    if t.fmt == "opencode":
        data.setdefault("$schema", "https://opencode.ai/config.json")
    _backup(t.path)
    _atomic_write(t.path, json.dumps(data, indent=2) + "\n")
    return "removed" if remove else "written"


def run_install(clients: list[str] | None, server_args: list[str], dry_run: bool, remove: bool, force: bool) -> int:
    found = targets()
    if clients:
        unknown = set(clients) - {t.key for t in found}
        if unknown:
            print(f"Unknown client(s): {', '.join(sorted(unknown))}. Known: {', '.join(t.key for t in found)}")
            return 2
        chosen = [t for t in found if t.key in clients]
        if not force:
            missing = [t.key for t in chosen if not (t.marker.exists() or t.path.exists())]
            if missing:
                print(f"Not detected: {', '.join(missing)} (configuring anyway because they were named).")
    else:
        chosen = [t for t in found if t.marker.exists() or t.path.exists()]
    if not chosen:
        print("No MCP clients detected. Use --client <name> to configure one explicitly, or `solidworks-mcp config`.")
        return 1
    spec = launch(server_args)
    rc = 0
    for t in chosen:
        if dry_run:
            print(f"\n# {t.describe()}\n{snippet(t, spec)}")
            continue
        try:
            print(f"{'-' if remove else '+'} {t.describe()}: {apply(t, spec, remove)}")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            rc = 1
            print(f"! {t.describe()}: {e}\n{snippet(t, spec)}")
    if not dry_run and not remove:
        print(f"\nRestart the client(s) to load the '{NAME}' server.")
    return rc


def print_config(client: str | None, server_args: list[str]) -> None:
    spec = launch(server_args)
    for t in targets():
        if client and t.key != client:
            continue
        print(f"# {t.label}  ({t.path})\n{snippet(t, spec)}\n")
    if not client or client == "claude-code":
        print("# Claude Code (command line)\nclaude mcp add --scope user " + NAME + " -- " + " ".join(f'"{a}"' if " " in a else a for a in spec.argv))

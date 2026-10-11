"""Command line: `solidworks-mcp [serve|doctor|install|uninstall|config|verify-audit]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Settings


def _server_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("server options")
    g.add_argument("--workspace", help="Default folder for models, exports and screenshots (default ~/solidworks-mcp).")
    g.add_argument("--root", action="append", default=[], help="Extra folder tools may read and write (repeatable).")
    g.add_argument("--read-only", action="store_true", help="Only allow tools that do not change models or files.")
    g.add_argument("--require-marking", action="store_true", help="Block exports of documents without an export-control marking property.")
    g.add_argument("--marking-property", help="Custom property holding the marking (default ExportControl).")
    g.add_argument("--audit-log", help="Audit log file (default <workspace>/audit/audit.jsonl).")
    g.add_argument("--no-audit", action="store_true", help="Disable the audit log.")
    g.add_argument("--no-launch", action="store_true", help="Do not start SolidWorks; require a running session.")


def _settings(ns: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    if ns.workspace:
        s.workspace = Path(ns.workspace).expanduser().resolve()
    if ns.root:
        s.extra_roots += [Path(r).expanduser().resolve() for r in ns.root]
    if ns.read_only:
        s.read_only = True
    if ns.require_marking:
        s.require_marking = True
    if ns.marking_property:
        s.marking_property = ns.marking_property
    if ns.audit_log:
        s.audit_log = Path(ns.audit_log).expanduser().resolve()
    if ns.no_audit:
        s.audit_enabled = False
    if ns.no_launch:
        s.launch = False
    return s


def _forward(ns: argparse.Namespace) -> list[str]:
    """Server options to bake into client configs."""
    out: list[str] = []
    if ns.workspace:
        out += ["--workspace", str(Path(ns.workspace).expanduser().resolve())]
    for r in ns.root:
        out += ["--root", str(Path(r).expanduser().resolve())]
    for flag in ("read_only", "require_marking", "no_audit", "no_launch"):
        if getattr(ns, flag):
            out.append("--" + flag.replace("_", "-"))
    if ns.marking_property:
        out += ["--marking-property", ns.marking_property]
    if ns.audit_log:
        out += ["--audit-log", str(Path(ns.audit_log).expanduser().resolve())]
    return out


def cmd_serve(ns: argparse.Namespace) -> int:
    from . import server

    server.configure(_settings(ns))
    server.mcp.run("stdio")
    return 0


def cmd_doctor(ns: argparse.Namespace) -> int:
    s = _settings(ns)
    ok = True

    def line(status: str, label: str, detail: str = "") -> None:
        print(f"[{status:^4}] {label}{(': ' + detail) if detail else ''}")

    line("ok", "solidworks-mcp", f"{__version__} on Python {sys.version.split()[0]} ({sys.executable})")
    if sys.platform != "win32":
        line("FAIL", "platform", "SolidWorks automation needs Windows")
        return 1
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401

        line("ok", "pywin32", "installed")
    except ImportError as e:
        line("FAIL", "pywin32", str(e))
        return 1
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID") as k:
            clsid = winreg.QueryValueEx(k, "")[0]
        versions = []
        i = 0
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name.startswith("SldWorks.Application."):
                    versions.append(name.rsplit(".", 1)[1])
        detail = ", ".join(f"SOLIDWORKS {int(v) + 1992}" for v in sorted(versions) if v.isdigit()) or clsid
        line("ok", "SolidWorks registered", detail)
    except OSError:
        ok = False
        line("FAIL", "SolidWorks registered", "SldWorks.Application is not registered; is SolidWorks installed?")
    from .com import SW, configure, on_com

    configure(launch=s.launch, visible=True)

    def probe() -> tuple[str, str, int, str]:
        app = SW.app
        C = SW.C
        tpl = app.GetUserPreferenceStringValue(C.swDefaultTemplatePart)
        return SW.version_label(), SW.revision(), app.GetDocumentCount(), tpl

    try:
        label, rev, ndocs, tpl = on_com(probe)
        line("ok", "SolidWorks session", f"{label} (revision {rev}), {ndocs} document(s) open")
        line("ok" if tpl and Path(tpl).exists() else "warn", "part template", tpl or "not configured (Tools > Options > Default Templates)")
    except Exception as e:  # noqa: BLE001
        ok = False
        line("FAIL", "SolidWorks session", str(e))
    try:
        s.workspace.mkdir(parents=True, exist_ok=True)
        probe_file = s.workspace / ".write-test"
        probe_file.write_text("x")
        probe_file.unlink()
        line("ok", "workspace", str(s.workspace))
    except OSError as e:
        ok = False
        line("FAIL", "workspace", f"{s.workspace}: {e}")
    line("ok" if s.audit_path else "warn", "audit log", str(s.audit_path) if s.audit_path else "disabled")
    line("info", "mode", ", ".join(m for m, on in (("read-only", s.read_only), ("marking required", s.require_marking)) if on) or "read/write")
    return 0 if ok else 1


def cmd_verify_audit(ns: argparse.Namespace) -> int:
    from .audit import verify

    s = _settings(ns)
    path = Path(ns.file).expanduser() if ns.file else s.audit_path
    if path is None:
        print("Audit logging is disabled; pass the log file.")
        return 2
    ok, n, msg = verify(path)
    print(f"{'OK' if ok else 'TAMPERED'}: {path}: {msg}")
    return 0 if ok else 1


def cmd_install(ns: argparse.Namespace, remove: bool = False) -> int:
    from .install import run_install

    clients = [c.strip() for c in ns.client.split(",")] if ns.client else None
    return run_install(clients, _forward(ns), ns.dry_run, remove, ns.force)


def cmd_config(ns: argparse.Namespace) -> int:
    from .install import print_config

    print_config(ns.client, _forward(ns))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solidworks-mcp", description="MCP server for SolidWorks.")
    parser.add_argument("--version", action="version", version=f"solidworks-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="Run the MCP server over stdio (default).")
    _server_options(p)

    p = sub.add_parser("doctor", help="Check Python, pywin32, SolidWorks and the workspace.")
    _server_options(p)

    p = sub.add_parser("verify-audit", help="Verify the hash chain of an audit log.")
    _server_options(p)
    p.add_argument("file", nargs="?", help="Audit log file (default: the configured one).")

    for name, help_ in (("install", "Register the server with detected MCP clients."), ("uninstall", "Remove the server from MCP clients.")):
        p = sub.add_parser(name, help=help_)
        _server_options(p)
        p.add_argument("--client", help="Comma-separated: claude-code,claude-desktop,codex,cursor,vscode,windsurf,gemini,opencode.")
        p.add_argument("--dry-run", action="store_true", help="Print what would be written.")
        p.add_argument("--force", action="store_true", help="Configure named clients even if not detected.")

    p = sub.add_parser("config", help="Print configuration snippets for each client.")
    _server_options(p)
    p.add_argument("--client", help="Only this client.")

    ns = parser.parse_args(argv)
    if ns.command is None:
        ns = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    if ns.command == "serve":
        return cmd_serve(ns)
    if ns.command == "doctor":
        return cmd_doctor(ns)
    if ns.command == "verify-audit":
        return cmd_verify_audit(ns)
    if ns.command == "install":
        return cmd_install(ns)
    if ns.command == "uninstall":
        return cmd_install(ns, remove=True)
    if ns.command == "config":
        return cmd_config(ns)
    parser.print_help()
    return 2

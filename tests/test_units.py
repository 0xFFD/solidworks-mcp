"""Tests that run anywhere: no SolidWorks needed."""

import asyncio
import json
from pathlib import Path

import pytest

from solidworks_mcp import audit, geometry
from solidworks_mcp.config import Settings
from solidworks_mcp.errors import SwError
from solidworks_mcp.registry import App

# ------------------------------------------------------------------------------------------ audit


def test_audit_chain_and_tamper_detection(tmp_path: Path):
    log = tmp_path / "audit.jsonl"
    a = audit.AuditLog(log)
    a.record("new_document", {"kind": "part"}, True, result={"title": "Part1"}, document="Part1")
    a.record("export", {"path": "x.step"}, False, error="blocked")
    a.record("extrude", {"depth": 5}, True, result="ok")
    ok, n, msg = audit.verify(log)
    assert ok and n == 3, msg

    # a new logger continues the existing chain
    audit.AuditLog(log).record("sw_info", {}, True)
    assert audit.verify(log)[:2] == (True, 4)

    lines = log.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["args"]["path"] = "innocent.step"
    lines[1] = json.dumps(rec)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, msg = audit.verify(log)
    assert not ok and "record 2 was modified" in msg


def test_audit_detects_deleted_record(tmp_path: Path):
    log = tmp_path / "audit.jsonl"
    a = audit.AuditLog(log)
    for i in range(3):
        a.record("t", {"i": i}, True)
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    ok, _, msg = audit.verify(log)
    assert not ok and "chain broken" in msg


def test_audit_disabled(tmp_path: Path):
    s = Settings(workspace=tmp_path, audit_enabled=False)
    assert s.audit_path is None
    audit.AuditLog(None).record("t", {}, True)  # no-op, no error


# ------------------------------------------------------------------------------------------ path policy


def _app(tmp_path: Path, **kw) -> App:
    s = Settings(workspace=tmp_path / "ws", audit_enabled=False, **kw)
    s.workspace.mkdir()
    return App(s)


def test_relative_paths_land_in_workspace(tmp_path: Path):
    a = _app(tmp_path)
    p = a.path("parts/bracket.step")
    assert p == (tmp_path / "ws" / "parts" / "bracket.step").resolve()
    assert p.parent.is_dir()


@pytest.mark.parametrize("bad", [r"..\escape.step", "../../escape.step", r"C:\Windows\Temp\x.step"])
def test_paths_outside_roots_are_rejected(tmp_path: Path, bad: str):
    with pytest.raises(SwError, match="outside the allowed folders"):
        _app(tmp_path).path(bad)


def test_extra_root_is_allowed(tmp_path: Path):
    extra = tmp_path / "shared"
    extra.mkdir()
    a = _app(tmp_path, extra_roots=[extra])
    assert a.path(str(extra / "a.step")) == (extra / "a.step").resolve()


def test_suffix_and_existence_checks(tmp_path: Path):
    a = _app(tmp_path)
    with pytest.raises(SwError, match="expected one of"):
        a.path("model.txt", suffixes=(".sldprt",))
    with pytest.raises(SwError, match="does not exist"):
        a.path("missing.sldprt", must_exist=True)


def test_settings_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SOLIDWORKS_MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SOLIDWORKS_MCP_READ_ONLY", "1")
    monkeypatch.setenv("SOLIDWORKS_MCP_REQUIRE_MARKING", "yes")
    monkeypatch.setenv("SOLIDWORKS_MCP_MARKING_PROPERTY", "Distribution")
    s = Settings.from_env()
    assert s.workspace == tmp_path and s.read_only and s.require_marking and s.marking_property == "Distribution"
    assert s.audit_path == (tmp_path / "audit" / "audit.jsonl").resolve()


# ------------------------------------------------------------------------------------------ STEP read-back


def _step(schema: str, body: str) -> str:
    return f"ISO-10303-21;\nHEADER;\nFILE_SCHEMA (( '{schema}' ));\nENDSEC;\nDATA;\n{body}\nENDSEC;\nEND-ISO-10303-21;\n"


@pytest.mark.parametrize("schema,ap", [
    ("CONFIG_CONTROL_DESIGN", "AP203"),
    ("AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }", "AP214"),
    ("AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 1 1 4 }", "AP242"),
])
def test_verify_step(tmp_path: Path, schema: str, ap: str):
    from solidworks_mcp.tools_doc import verify_step

    p = tmp_path / "a.step"
    p.write_text(_step(schema, "#1 = MANIFOLD_SOLID_BREP ( 'x', #2 ) ;\n#3 = ADVANCED_FACE ( 'f', (#4), #5, .T. ) ;"), encoding="latin-1")
    info = verify_step(p)
    assert info == {"step_protocol": ap, "solids": 1, "faces": 1}


def test_verify_step_without_solids(tmp_path: Path):
    from solidworks_mcp.tools_doc import verify_step

    p = tmp_path / "wire.step"
    p.write_text(_step("CONFIG_CONTROL_DESIGN", "#1 = CIRCLE ( 'c', #2, 4.0 ) ;"), encoding="latin-1")
    assert verify_step(p)["solids"] == 0


# ------------------------------------------------------------------------------------------ geometry helpers


def test_sketch_frame_top_plane():
    """Top plane: SolidWorks reports rows [1,0,0],[0,0,1],[0,-1,0]; sketch +Y is model -Z."""
    from solidworks_mcp.tools_sketch import SketchFrame

    f = SketchFrame.__new__(SketchFrame)
    f.R = [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]
    f.T = [0.0, 0.0, -0.008]  # sketch plane at model y = 8 mm
    f.s = 1.0
    d = f.describe()
    assert d["sketch_x_axis"] == [1.0, 0.0, 0.0]
    assert d["sketch_y_axis"] == [0.0, 0.0, -1.0]
    assert d["origin_mm"] == [0.0, 8.0, 0.0]
    p = [0.03, 0.008, -0.015]
    q = f.to_sketch(p)
    assert q == pytest.approx([0.03, 0.015, 0.0])
    assert f.to_model(q) == pytest.approx(p)


@pytest.mark.parametrize("ref,kind,comp", [
    ("face:12", "face", None), ("edge:3", "edge", None), ("face:Bracket-1/7", "face", "Bracket-1"),
])
def test_index_refs(ref, kind, comp):
    m = geometry._IDX_RE.match(ref)
    assert m and m.group(1) == kind and m.group("comp") == comp


@pytest.mark.parametrize("ref,comp,xyz", [
    ("face@50,0,-10.5", None, ("50", "0", "-10.5")),
    ("edge@ 1.5e-3 , 2, 3", None, ("1.5e-3", "2", "3")),
    ("face@pin-1/0,80,0", "pin-1", ("0", "80", "0")),
])
def test_point_refs(ref, comp, xyz):
    m = geometry._AT_RE.match(ref)
    assert m and m.group("comp") == comp and (m.group(3), m.group(4), m.group(5)) == xyz


def test_datum_parsing():
    from solidworks_mcp import tools_drawing

    assert tools_drawing._DATUM_RE.match("A").groups() == ("A", None)
    assert tools_drawing._DATUM_RE.match("B(M)").groups() == ("B", "M")
    assert tools_drawing._DATUM_RE.match("c|l").groups() == ("c", "l")
    assert tools_drawing._DATUM_RE.match("A-B").group(1) == "A-B"
    assert tools_drawing._DATUM_RE.match("AB(Q)") is None
    assert tools_drawing._datum("B(M)") == "B<MOD-MMC>" and tools_drawing._datum("c|l") == "C<MOD-LMC>" and tools_drawing._datum("A") == "A"


# ------------------------------------------------------------------------------------------ server and install


def test_tools_registered_with_annotations():
    from solidworks_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert len(tools) >= 50
    for name in ("sw_info", "list_faces", "mass_properties", "screenshot", "check_geometry", "list_materials", "list_sketch", "list_bodies"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("close_document", "feature_action", "undo", "delete_body"):
        assert tools[name].annotations.destructive_hint is True, name
    for name in ("extrude", "hole", "export", "create_drawing", "add_geometric_tolerance", "modify_sketch", "edit_feature", "helix",
                 "configurations", "combine", "move_body", "sheet_metal_base", "edge_flange", "flat_pattern",
                 "detail_view", "projected_view", "auxiliary_view", "add_view", "sheets", "drawing_dimension", "drawing_table", "exploded_view", "add_datum_target", "hem", "sketched_bend", "split_body", "surface", "broken_view", "breakout_section"):
        assert tools[name].annotations.read_only_hint is False, name
    assert "millimetres" in server.INSTRUCTIONS


def test_install_snippets():
    from solidworks_mcp import install

    spec = install.Launch("C:\\py\\python.exe", ["-m", "solidworks_mcp", "serve", "--read-only"])
    by_key = {t.key: t for t in install.targets()}
    assert set(by_key) == {"claude-code", "claude-desktop", "codex", "cursor", "vscode", "windsurf", "gemini", "opencode"}
    codex = install.snippet(by_key["codex"], spec)
    assert "[mcp_servers.solidworks]" in codex and "--read-only" in codex
    vs = json.loads(install.snippet(by_key["vscode"], spec))
    assert vs["servers"]["solidworks"]["type"] == "stdio"
    oc = json.loads(install.snippet(by_key["opencode"], spec))
    assert oc["mcp"]["solidworks"]["command"][0] == "C:\\py\\python.exe"
    cur = json.loads(install.snippet(by_key["cursor"], spec))
    assert cur["mcpServers"]["solidworks"]["args"][-1] == "--read-only"


def test_install_writes_and_removes(tmp_path: Path):
    from solidworks_mcp import install

    t = install.Target("cursor", "Cursor", tmp_path / "mcp.json", tmp_path, "mcpServers")
    t.path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    spec = install.Launch("python", ["-m", "solidworks_mcp", "serve"])
    assert install.apply(t, spec) == "written"
    data = json.loads(t.path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "solidworks"}
    assert (tmp_path / "mcp.json.bak-solidworks-mcp").exists()
    assert install.apply(t, spec, remove=True) == "removed"
    assert set(json.loads(t.path.read_text(encoding="utf-8"))["mcpServers"]) == {"other"}


def test_cli_version(capsys):
    from solidworks_mcp import __version__, cli

    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_sketch_references():
    from solidworks_mcp.tools_sketch import _SEG_RE

    assert _SEG_RE.match("seg:3").group(2) == "3"
    assert _SEG_RE.match("pt@ -1.5, 2e-3").groups()[2:] == ("-1.5", "2e-3")
    assert _SEG_RE.match("SEG@4,5") is not None
    for bad in ("seg", "seg:", "seg@1", "face@1,2,3", "pt:1.5"):
        assert _SEG_RE.match(bad) is None, bad


def test_view_gap_search():
    from solidworks_mcp.tools_drawing import _first_gap

    # room between the parent's right edge (0.16) and the next view at 0.30
    assert _first_gap([(0.03, 0.16), (0.30, 0.41)], 0.172, 0.06, 0.41, 0.012) == pytest.approx(0.172)
    # the row is full up to the sheet limit: no place
    assert _first_gap([(0.03, 0.16), (0.17, 0.41)], 0.172, 0.06, 0.41, 0.012) is None
    # a view in the way is skipped
    assert _first_gap([(0.18, 0.20)], 0.172, 0.06, 0.41, 0.012) == pytest.approx(0.212)

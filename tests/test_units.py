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
                 "detail_view", "projected_view", "auxiliary_view", "add_view", "sheets", "drawing_dimension", "drawing_table", "exploded_view", "add_datum_target", "hem", "sketched_bend", "split_body", "surface", "broken_view", "breakout_section", "driven_pattern", "convert_to_sheet_metal", "view_dimensions", "structural_member", "closed_corner"):
        assert tools[name].annotations.read_only_hint is False, name
    for name in ("cut_list", "member_check"):
        assert tools[name].annotations.read_only_hint is True, name
    assert "millimetres" in server.INSTRUCTIONS
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"{len(tools)} tools and one prompt" in readme


def test_member_check_tool_needs_no_solidworks():
    from solidworks_mcp.tools_weldment import member_check

    # 25 x 2 square tube, 600 mm cantilever, 150 N at the tip: hand calc I = (25^4 - 21^4) / 12
    i = (25**4 - 21**4) / 12
    r = member_check(shape="square_tube", size=[25, 2], length=600, e_modulus=70000, yield_strength=240,
                     ultimate_strength=290, support="cantilever", load=150, max_deflection=5)
    assert r["bending_stress_MPa"] == pytest.approx(150 * 600 * 12.5 / i, rel=1e-3)
    assert r["deflection_mm"] == pytest.approx(150 * 600**3 / (3 * 70000 * i), rel=1e-3)
    assert r["ms_ultimate"] == pytest.approx(290 / (1.5 * r["max_stress_MPa"]) - 1, abs=2e-3)
    assert r["ok"] is (r["deflection_mm"] <= 5 and r["ms_yield"] >= 0 and r["ms_ultimate"] >= 0)
    assert "buckling" not in r
    with pytest.raises(SwError):
        member_check(shape="angle", size=[50, 5], length=100, e_modulus=70000, yield_strength=240)


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
    assert _SEG_RE.match("pt@ -1.5, 2e-3").groups()[2:] == ("-1.5", "2e-3", None)
    assert _SEG_RE.match("seg@40, 20.5, -3").groups()[2:] == ("40", "20.5", "-3")  # 3D sketches
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


def test_screenshot_crop_centres_model():
    from PIL import Image, ImageDraw

    from solidworks_mcp.tools_inspect import fit_crop

    im = Image.new("RGB", (400, 280))
    d = ImageDraw.Draw(im)
    for y in range(280):  # vertical gradient background like SolidWorks
        d.line([(0, y), (399, y)], fill=(70 - y // 5, 80 - y // 5, 100 - y // 4))
    d.rectangle([300, 200, 340, 240], fill=(200, 200, 200))  # small model in the lower right corner
    out = fit_crop(im, 200, 140)
    assert out.size == (200, 140)
    px = out.load()
    assert px[100, 70] == (200, 200, 200)  # the model is now centred
    assert px[2, 2] != (200, 200, 200)


def _xlsx(path: Path, rows: list[list[str]]) -> None:
    import zipfile

    cells = "".join(
        f'<row r="{i + 1}">' + "".join(f'<c r="{chr(65 + j)}{i + 1}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(r) if v) + "</row>"
        for i, r in enumerate(rows))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f"<sheetData>{cells}</sheetData></worksheet>")


def test_gauge_table_without_excel(tmp_path: Path):
    from solidworks_mcp import gauge

    f = tmp_path / "g.xlsx"
    _xlsx(f, [["Type:", "Steel"], ["K-Factor", "0.42"], ["Unit:", "inches"], [], ["Gauge No.", "Gauge(Thickness)", "Available Bend Radius"],
              ["16 Gauge", "0.0598", ".0625; .125"], ["14 Gauge ", "0.0747", ".125"]])
    t = gauge.parse_gauge_table(gauge.read_xlsx(f))
    assert t["k_factor"] == 0.42 and len(t["gauges"]) == 2
    g = gauge.find_gauge(t, "14")
    assert g["gauge"] == "14 Gauge" and g["thickness_mm"] == pytest.approx(0.0747 * 25.4, abs=1e-4) and g["bend_radii_mm"] == [3.175]
    with pytest.raises(SwError):
        gauge.find_gauge(t, "10")


def test_solidworks_sample_gauge_table():
    from solidworks_mcp import gauge

    f = Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\english\Sheet Metal Gauge Tables\sample table - aluminum - metric units.xlsx")
    if not f.exists():
        pytest.skip("SolidWorks gauge tables not installed")
    t = gauge.parse_gauge_table(gauge.read_xlsx(f))
    g = gauge.find_gauge(t, "Gauge 14")
    assert g["thickness_mm"] == 2.0 and 2.0 in g["bend_radii_mm"] and t["k_factor"] == 0.5


def test_tolerance_stack_2d_and_gdt_without_solidworks():
    from solidworks_mcp.tools_inspect import tolerance_stack

    # gap = 50 cos 60 (a 50 +/- 0.2 link at 60 deg to the gap) - 20 +/- 0.1, plus a 0.2 position zone with 0.1 MMC bonus
    r = tolerance_stack(contributors=[
        {"nominal": 50, "plus": 0.2, "minus": 0.2, "angle": 60, "name": "link"},
        {"nominal": 20, "plus": 0.1, "minus": 0.1, "direction": -1},
        {"gdt": "position", "tolerance": 0.2, "bonus": 0.1, "direction": -1}], requirement={"min": 4.5})
    assert r["nominal"] == pytest.approx(5.0)
    wc = 0.5 * 0.2 + 0.1 + 0.15
    assert r["worst_case"] == pytest.approx([5 - wc, 5 + wc], abs=1e-4)
    rss = (0.1**2 + 0.1**2 + 0.15**2) ** 0.5
    assert r["rss"] == pytest.approx([5 - rss, 5 + rss], abs=1e-4)
    assert r["contributors"][0]["sensitivity"] == pytest.approx(0.5) and r["worst_case_ok"] is True
    s = tolerance_stack(contributors=[{"nominal": 10, "plus": 0.1, "sensitivity": -2.0}])
    assert s["nominal"] == pytest.approx(-20) and s["worst_case"] == pytest.approx([-20.2, -19.8])
    with pytest.raises(SwError, match="gdt must be"):
        tolerance_stack(contributors=[{"gdt": "wobble", "tolerance": 0.1}])

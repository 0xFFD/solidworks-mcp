"""End-to-end tests against a real SolidWorks session, through the MCP protocol over stdio.

Skipped unless SolidWorks is installed. They create, save and close their own documents and never
touch documents that were already open.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _solidworks_installed() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID").Close()
        return True
    except OSError:
        return False


if not _solidworks_installed():
    pytest.skip("SolidWorks is not installed", allow_module_level=True)


class Session:
    def __init__(self, s):
        self.s = s

    async def call(self, name: str, /, **args):
        res = await self.s.call_tool(name, args, read_timeout_seconds=600)
        text = " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        if res.is_error:
            raise AssertionError(f"{name}({args}) failed: {text}")
        images = [c for c in res.content if getattr(c, "type", "") == "image"]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return (data, images) if images else data

    async def fails(self, name: str, /, **args) -> str:
        res = await self.s.call_tool(name, args, read_timeout_seconds=600)
        assert res.is_error, f"{name} should have failed"
        return " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def run(workspace: Path, body, *server_args: str):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp", "serve", "--workspace", str(workspace), *server_args])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await body(Session(s))

    asyncio.run(main())


def test_bracket_workflow(tmp_path: Path):
    async def body(c: Session):
        await c.call("new_document", kind="part", units="mmgs")
        try:
            await c.call("document_settings", drafting_standard="asme", decimals=2)
            sk = await c.call("start_sketch", on="plane:Top")
            assert sk["sketch_y_axis"] == [0.0, 0.0, -1.0]
            await c.call("sketch", entities=[{"type": "rounded_rectangle", "corner1": [-60, -30], "corner2": [60, 30], "radius": 8}])
            done = await c.call("finish_sketch", fully_define=True)
            assert done["status"] == "fully_defined" and done["closed_regions"] == 1
            base = await c.call("extrude", depth=8)
            assert base["volume_mm3"] == pytest.approx(120 * 60 * 8 - 4 * (8 * 8 - 3.14159265 * 64 / 4) * 8, abs=0.01)

            await c.call("start_sketch", on="face@0,8,0")
            await c.call("sketch", coords="model", entities=[{"type": "rectangle", "corner1": [-52, 8, -30], "corner2": [52, 8, -22]}])
            await c.call("finish_sketch")
            flange = await c.call("extrude", depth=40)
            assert flange["volume_mm3"] == pytest.approx(base["volume_mm3"] + 104 * 8 * 40, abs=0.01)

            await c.call("fillet", radius=4, edges=["edge@0,8,-22"])
            await c.call("hole", size="M6", kind="counterbore", positions=[[-40, 8, 12], [40, 8, 12]])
            tapped = await c.call("hole", size="M5", kind="tapped", positions=[[-30, 32, -22], [30, 32, -22]])
            assert tapped["holes"] == 2

            # hole centres are exact and the tapped holes run into the flange (axis along Z)
            cyl = (await c.call("list_faces", type="cylinder"))["faces"]
            holes = {(round(f["axis_point"][0], 3), tuple(abs(a) for a in f["axis"])) for f in cyl if f.get("hole")}
            assert (-40.0, (0.0, 1.0, 0.0)) in holes and (40.0, (0.0, 1.0, 0.0)) in holes
            assert (-30.0, (0.0, 0.0, 1.0)) in holes and (30.0, (0.0, 0.0, 1.0)) in holes

            await c.call("set_material", name="7075-T6 (SN)")
            await c.call("custom_properties", set={"PartNo": "BRK-1001", "Revision": "A", "ExportControl": "EAR99 (TEST DATA)"})
            await c.call("add_datum", face="face@50,0,0", label="A")
            await c.call("add_geometric_tolerance", face="face@50,0,0", characteristic="flatness", tolerance=0.05)
            mp = await c.call("mass_properties")
            assert mp["material"] == "7075-T6 (SN)" and mp["density_kg_m3"] == pytest.approx(2810)
            assert (await c.call("check_geometry"))["ok"] is True
            shot, images = await c.call("screenshot", view="isometric")
            assert images and shot["size"] == [1000, 700]

            await c.call("save_document", path="bracket.sldprt")
            step = await c.call("export", path="bracket.step", step_ap="214")
            assert step["step_protocol"] == "AP214" and step["solids"] == 1 and step["marking"] == "EAR99 (TEST DATA)"
            assert "outside the allowed folders" in await c.fails("export", path=r"..\outside.step")

            drw = await c.call("create_drawing", sheet="A3", save_as="bracket.slddrw")
            assert len(drw["views"]) == 4
            await c.call("section_view", view=drw["views"][0]["name"])
            pdf = await c.call("export", path="bracket.pdf")
            assert pdf["bytes"] > 10000
            await c.call("close_document", name="bracket.SLDDRW")
        finally:
            await c.call("close_document")

    run(tmp_path, body)
    from solidworks_mcp import audit

    ok, n, msg = audit.verify(tmp_path / "audit" / "audit.jsonl")
    assert ok and n > 20, msg


def test_airframe_features(tmp_path: Path):
    """The sketch vocabulary and assembly behaviour that design_guide recipes rely on."""

    async def body(c: Session):
        assert "43.13" in await c.call("design_guide", topic="multirotor")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "polyline", "closed": True, "radius": 6,
                                              "points": [[-30, -40], [30, -40], [30, 40], [-30, 40]]}])
            assert (await c.call("finish_sketch"))["closed_regions"] == 1
            plate = await c.call("extrude", depth=4)
            assert plate["volume_mm3"] == pytest.approx((60 * 80 - 4 * (36 - 3.14159265 * 9)) * 4, abs=0.05)
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[
                {"type": "bolt_circle", "center": [15, 25], "pcd": 22.63, "count": 4, "hole_diameter": 3.4, "start_angle": 45, "mirror": "both"},
                {"type": "slot", "start": [0, -10], "end": [0, 10], "width": 8},
            ])
            done = await c.call("finish_sketch")
            cut = await c.call("extrude", cut=True, end="through_all_both")
            holes = [f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole") and f["radius"] == pytest.approx(1.7)]
            assert len(holes) == 16
            await c.call("edit_sketch", sketch=done["sketch"])  # add one more hole to the same cut
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 30], "diameter": 5}])
            await c.call("finish_sketch")
            assert (await c.call("rebuild"))["ok"]
            assert (await c.call("mass_properties"))["volume_mm3"] < cut["volume_mm3"]
            await c.call("start_sketch", on="face@0,4,-35")
            await c.call("sketch", entities=[{"type": "text", "at": [-12, -36], "text": "PN 1001 REV A", "height": 3}])
            await c.call("finish_sketch")
            assert (await c.call("extrude", cut=True, depth=0.3))["type"] == "ICE"
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "airfoil", "naca": "2412", "chord": 40, "origin": [-20, 10], "angle": 3}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=6)
            await c.call("set_material", name="CFRP plate quasi-isotropic (generic)")
            assert (await c.call("check_geometry"))["ok"]
            await c.call("save_document", path="plate.sldprt")
            await c.call("export", path="plate.step", step_ap="214")

            imported = await c.call("import_file", path="plate.step", save_as="plate_imported.sldprt")
            assert imported["solid_bodies"] == 2  # plate and the separate airfoil block
            await c.call("close_document")

            await c.call("new_document", kind="part")  # ellipse loft uses the fallback call
            await c.call("ref_plane", mode="offset", refs=["plane:Front"], distance=40)
            for plane, (a, b) in (("plane:Front", (20, 12)), ("plane:Plane1", (8, 5))):
                await c.call("start_sketch", on=plane)
                await c.call("sketch", entities=[{"type": "ellipse", "center": [0, 0], "radius_x": a, "radius_y": b}])
                await c.call("finish_sketch")
            assert (await c.call("loft", profiles=["Sketch1", "Sketch2"]))["type"] == "Blend"
            await c.call("save_document", path="pod.sldprt")

            await c.call("new_document", kind="assembly")
            await c.call("insert_component", path="plate.sldprt", fix=True)
            await c.call("insert_component", path="pod.sldprt", position=[0, 30, 0])
            comps = {r["name"]: r for r in (await c.call("list_components"))["components"]}
            assert comps["plate-1"]["origin_mm"] == [0.0, 0.0, 0.0]  # origin, not the bounding box centre
            assert comps["pod-1"]["origin_mm"] == [0.0, 30.0, 0.0]
            pod_faces = (await c.call("list_faces", component="pod-1", type="plane"))["faces"]
            assert any(f["point"][1] == pytest.approx(30.0) for f in pod_faces)  # assembly coordinates
            bom = await c.call("bom")
            assert bom["parts"] == 2 and bom["total_mass_g"] > 0
            await c.call("save_document", path="assy.sldasm")
            await c.call("close_document", name="assy.SLDASM")
            await c.call("close_document", name="pod.SLDPRT")
            await c.call("close_document", name="plate.SLDPRT")
        except BaseException:
            await c.call("sw_info")
            raise

    run(tmp_path, body)


def test_sketch_and_feature_editing(tmp_path: Path):
    """Sketch editing, feature redefinition, configurations, helices, 3D sketches and multibody tools."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-20, -10], "corner2": [20, 10]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=5)

            # offset a converted face outline, trim it with a circle, dimension and constrain it
            await c.call("start_sketch", on="face@0,5,0")
            edit = await c.call("modify_sketch", operations=[
                {"op": "convert", "refs": ["face@0,5,0"]},
                {"op": "offset", "items": ["seg@0,10"], "distance": -3},
                {"op": "construction", "items": ["seg@0,10", "seg@0,-10", "seg@20,0", "seg@-20,0"]},
                {"op": "fillet", "items": ["pt@17,7"], "radius": 2},
            ])
            assert [d.get("segments_added") for d in edit["done"]] == [4, 4, None, 1]
            await c.call("sketch", entities=[{"type": "circle", "center": [17, 0], "radius": 4}])
            await c.call("modify_sketch", operations=[{"op": "trim", "at": [21, 0]}, {"op": "trim", "at": [17, -2]}])
            segs = (await c.call("list_sketch", near=[17, 0], limit=3))["segments"]
            arc = next(s for s in segs if s["type"] == "arc" and s["radius"] == 4)
            assert sorted([arc["start"], arc["end"]]) == [[20.0, -2.6458], [20.0, 2.6458]]
            await c.call("sketch", entities=[{"type": "line", "from": [-10, 0], "to": [0, 3]}, {"type": "circle", "center": [-8, -3], "radius": 1}])
            dims = await c.call("modify_sketch", operations=[
                {"op": "relation", "type": "horizontal", "items": ["seg@-5,1.5"]},
                {"op": "dimension", "items": ["seg@-5,0"], "value": 12},
                {"op": "dimension", "items": ["seg@-7,-3"], "value": 3},
                {"op": "delete", "items": ["seg@-5,0"]},
            ])
            assert dims["done"][1]["value"] == 12 and dims["done"][2]["value"] == 3
            assert "nearest one is" in await c.fails("modify_sketch", operations=[{"op": "trim", "at": [50, 50]}])
            await c.call("sketch", entities=[{"type": "line", "from": [-15, 3], "to": [-12, 3]}, {"type": "line", "from": [-10, -5], "to": [-10, 8]},
                                             {"type": "circle", "center": [-14, -4], "radius": 0.5}])
            more = await c.call("modify_sketch", operations=[
                {"op": "extend", "at": [-12.5, 3]}, {"op": "split", "at": [-10, 0]},
                {"op": "pattern", "items": ["seg@-13.5,-4"], "count": [2, 1], "spacing": [-2, 0]},
                {"op": "pattern", "items": ["seg@-13.5,-4"], "center": [-14, 0], "count": 4},
            ])
            assert more["done"][1]["segments_added"] == 1 and more["done"][3]["segments_added"] == 3  # split in two; 3 more circles
            lines = [s for s in (await c.call("list_sketch", near=[-11, 3], limit=3))["segments"] if s["type"] == "line"]
            assert any(s["end"] == [-10.0, 3.0] or s["start"] == [-10.0, 3.0] for s in lines)  # extended to the vertical line
            await c.call("set_dimensions", values={dims["done"][2]["dimension"]: 4})  # sketch stays open
            assert (await c.call("finish_sketch"))["closed_regions"] >= 1

            # redefine a cut: blind -> through all -> blind
            await c.call("start_sketch", on="face@0,5,0")
            await c.call("sketch", entities=[{"type": "circle", "center": [-12, 0], "diameter": 4}])
            await c.call("finish_sketch")
            cut = await c.call("extrude", cut=True, depth=2)
            through = await c.call("edit_feature", feature=cut["feature"], end="through_all")
            assert through["volume_mm3"] == pytest.approx(4000 - 3.14159265 * 4 * 5, abs=0.01)
            # resize Hole Wizard holes in place
            hw = await c.call("hole", size="M3", kind="tapped", depth=3, positions=[[10, 5, 5], [15, 5, 5]])
            m4 = await c.call("edit_feature", feature=hw["feature"], size="M4")
            assert m4["feature"].startswith("M4")
            radii = {f["radius"] for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole") and f["axis_point"][0] in (10.0, 15.0)}
            assert radii == {1.65}  # M4 tap drill 3.3
            assert "no size" in await c.fails("edit_feature", feature=m4["feature"], size="M99")
            await c.call("feature_action", features=[m4["feature"]], action="delete")
            two = await c.call("edit_feature", feature="Boss-Extrude1", depth2=4)
            assert two["volume_mm3"] == pytest.approx(through["volume_mm3"] + 800 * 4 - 3.14159265 * 4 * 4, abs=0.5)

            # configurations: a variant without the hole
            await c.call("configurations", action="add", name="Blank")
            await c.call("feature_action", features=[cut["feature"]], action="suppress", configurations=["Blank"])
            await c.call("custom_properties", set={"PartNo": "PLT-100-2"}, configuration="Blank")
            await c.call("set_material", name="6061-T6 (SS)", configuration="Blank")
            assert (await c.call("mass_properties"))["material"] == "6061-T6 (SS)"
            blank = (await c.call("mass_properties"))["volume_mm3"]
            await c.call("configurations", action="activate", name="Default")
            default = await c.call("mass_properties")
            assert blank > default["volume_mm3"] and default["material"] != "6061-T6 (SS)"
            assert (await c.call("custom_properties", configuration="Blank"))["properties"]["PartNo"] == "PLT-100-2"
            await c.call("close_document")

            # spring: helix plus swept wire; 3D sketch sweep path
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 20}])
            await c.call("finish_sketch")
            hx = await c.call("helix", pitch=5, revolutions=6)
            assert hx["end_point"][1] == pytest.approx(30)
            await c.call("start_sketch", on="plane:Right")
            await c.call("sketch", coords="model", entities=[{"type": "circle", "center": hx["start_point"], "diameter": 2}])
            await c.call("finish_sketch")
            spring = await c.call("sweep", path=hx["feature"], profile="Sketch2")
            assert spring["volume_mm3"] == pytest.approx(3.14159265 * 1 * ((3.14159265 * 20) ** 2 + 25) ** 0.5 * 6, rel=0.01)
            await c.call("start_sketch", on="3d")
            await c.call("sketch", entities=[{"type": "polyline", "radius": 8, "points": [[40, 0, 0], [40, 50, 0], [70, 50, 20]]}])
            assert (await c.call("finish_sketch"))["3d"] is True
            await c.call("sweep", path="3DSketch1", circular_diameter=4)
            tube = next(b for b in (await c.call("list_bodies"))["bodies"] if b["name"] == "Sweep2")
            # bent tube run: straight legs shortened by the tangent length, plus the bend arc
            l2 = (30**2 + 20**2) ** 0.5
            theta = math.pi / 2  # the legs (0,1,0) and (30,0,20) are perpendicular
            tan_len = 8 / math.tan(theta / 2)
            length = 50 + l2 - 2 * tan_len + 8 * (math.pi - theta)
            assert tube["volume_mm3"] == pytest.approx(math.pi * 4 * length, rel=1e-4)

            # multibody: an overlapping copy of the tube, merged back, and a mirrored spring
            moved = await c.call("move_body", bodies=["body:Sweep2"], translate=[0, 0, 2], copy=True)
            assert len(moved["bodies"]) == 3
            merged = await c.call("combine", operation="add", main="Sweep2", tools=[moved["feature"]])
            assert len(merged["bodies"]) == 2
            mirrored = await c.call("mirror", plane="plane:Front", features=["body:Sweep1"], merge=False)
            assert mirrored["type"] == "MirrorSolid" and len((await c.call("list_bodies"))["bodies"]) == 3
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_sheet_metal(tmp_path: Path):
    """An avionics-tray style sheet metal part: base flange, edge flanges, flat pattern and DXF blank."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            base = await c.call("sheet_metal_base", thickness=1.6, bend_radius=1.6)
            assert base["volume_mm3"] == pytest.approx(100 * 60 * 1.6)
            # every reported edge midpoint lies on its edge, so it can be used as a reference
            for e in (await c.call("list_edges", limit=50))["edges"]:
                assert (await c.call("measure", refs=["edge@{},{},{}".format(*e["mid"])]))["length_mm"] == pytest.approx(e["length"], abs=1e-3)
            await c.call("edge_flange", edges=["edge@0,0,30", "edge@0,0,-30"], length=15)
            await c.call("edge_flange", edges=["edge@50,0,0", "edge@-50,0,0"], length=15)
            open_volume = (await c.call("mass_properties"))["volume_mm3"]
            closed = await c.call("closed_corner", faces=["face@50,8.3,29.2", "face@50,8.3,-29.2", "face@-50,8.3,29.2", "face@-50,8.3,-29.2"])
            assert closed["volume_mm3"] > open_volume and "warning" not in closed
            info = await c.call("flat_pattern")
            assert info["bends"] == 4 and info["state"] == "formed"
            # outer size 100 x 60 kept (material inside); each side adds 15 - r straight plus the bend allowance
            ba = 3.14159265 / 2 * (1.6 + info["default_k_factor"] * 1.6)
            assert info["flat_size_mm"] == pytest.approx([100 - 2 * 3.2 + 2 * (ba + 13.4), 60 - 2 * 3.2 + 2 * (ba + 13.4)], abs=0.01)
            assert "Save the part" in await c.fails("flat_pattern", export="tray.dxf")
            await c.call("custom_properties", set={"ExportControl": "TEST DATA"})
            await c.call("save_document", path="tray.sldprt")
            flat = await c.call("flat_pattern", action="flatten", export="tray.dxf")
            assert flat["state"] == "flat" and flat["exported"]["bytes"] > 1000 and flat["marking"] == "TEST DATA"
            assert (await c.call("bounding_box"))["size"][1] == pytest.approx(1.6)
            await c.call("flat_pattern", action="fold")

            # a 45 degree lip with its own K-factor, and an open hat section
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -20], "corner2": [40, 20]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.0, bend_radius=1.0)
            lip = await c.call("edge_flange", edges=["edge@0,0,20"], length=10, angle=45, k_factor=0.4)
            assert "rebuild" not in lip
            assert (await c.call("bounding_box"))["max"][1] == pytest.approx(10 * 0.70710678, abs=0.01)
            await c.call("close_document")
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "polyline", "points": [[-30, 0], [-15, 0], [-15, 20], [15, 20], [15, 0], [30, 0]]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.2, bend_radius=2, depth=200)
            hat = await c.call("flat_pattern")
            assert hat["bends"] == 4 and hat["flat_size_mm"][0] == pytest.approx(200)
            await c.call("close_document")

            # a supplier's bent bracket arrives as a dumb STEP solid: convert it and get the blank
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "polyline", "closed": True, "points": [[0, 0], [60, 0], [60, 2], [2, 2], [2, 40], [0, 40]]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=30)
            await c.call("export", path="supplier_bracket.step", step_ap="214")
            await c.call("close_document")
            await c.call("import_file", path="supplier_bracket.step")
            conv = await c.call("convert_to_sheet_metal", fixed_face="face@30,0,15", bend_edges=["edge@0,0,15"], thickness=2, bend_radius=2)
            # outer legs 60 and 40, less 2 x (r + t), plus the bend allowance pi/2 x (r + K t)
            assert conv["bends"] == 1 and conv["flat_size_mm"][0] == pytest.approx(100 - 8 + 3.14159265 / 2 * (2 + 0.5 * 2), abs=0.01)
            await c.call("close_document")
            # a tray: one miter flange around three sides of a 100 x 60 x 2 blank
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=2, bend_radius=2)
            before = len((await c.call("list_features"))["features"])
            assert "connected straight edges" in await c.fails("miter_flange", edges=["edge@-50,-1,-30", "edge@0,0,30"], length=20)
            assert len((await c.call("list_features"))["features"]) == before  # no profile plane or sketch left behind
            tray = await c.call("miter_flange", edges=["edge@0,0,-30", "edge@50,0,0", "edge@0,0,30"], length=20)
            assert tray["type"] == "SMMiteredFlange"
            ba = 3.14159265 / 2 * (2 + 0.5 * 2)
            flat = (await c.call("flat_pattern"))["flat_size_mm"]
            assert flat == pytest.approx([96 + ba + 18, 52 + 2 * (ba + 18)], abs=0.01)  # outer 22 mm flanges, 4 mm setback per bend
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_drawing_details(tmp_path: Path):
    """Section, detail, projected and flat pattern views, sheets, dimensions, and BOM, revision and hole tables."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rounded_rectangle", "corner1": [-40, -25], "corner2": [40, 25], "radius": 5}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            await c.call("hole", size="M5", kind="counterbore", positions=[[-30, 10, 15], [30, 10, 15], [-30, 10, -15], [30, 10, -15]])
            await c.call("hole", size="M4", kind="tapped", positions=[[0, 10, 0]])
            await c.call("custom_properties", set={"Description": "Mounting block"})
            await c.call("save_document", path="block.sldprt")
            await c.call("close_document")
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.6, bend_radius=1.6)
            await c.call("edge_flange", edges=["edge@0,0,30", "edge@0,0,-30"], length=15)
            await c.call("save_document", path="cover.sldprt")
            await c.call("new_document", kind="assembly")
            await c.call("insert_component", path="block.sldprt", fix=True)
            await c.call("insert_component", path="cover.sldprt", position=[0, 40, 0])
            await c.call("save_document", path="assy.sldasm")
            await c.call("close_document", name="assy.SLDASM")
            await c.call("close_document", name="cover.SLDPRT")
            await c.call("close_document", name="block.SLDPRT")

            # a drawing of a model that is not open
            drw = await c.call("create_drawing", model="block.sldprt", isometric=False, save_as="block.slddrw")
            front, top = drw["views"][0]["name"], drw["views"][1]["name"]
            section = await c.call("section_view", view=top, direction="horizontal", offset=-15)
            assert section["cut_faces"] > 0 and "warning" not in section  # the cut goes through the counterbores
            stepped = await c.call("section_view", view=top, label="C", points=[[-50, 10, 15], [-10, 10, 15], [-10, 10, -15], [50, 10, -15]])
            assert stepped["cut_faces"] > section["cut_faces"]  # through two holes that do not line up
            aligned = await c.call("section_view", view=top, label="E", points=[[0, 10, -40], [0, 10, 0], [30, 10, 15], [45, 10, 22.5]])
            assert aligned["cut_faces"] > 0  # a leg at an angle through a counterbore
            assert (await c.call("detail_view", view=front, center=[30, 10, 15], radius=8, scale="3:1", label="D"))["scale"] == 3
            assert (await c.call("breakout_section", view=front, center=[-30, 5, 15], radius=7, depth=15))["cut_faces"] > 0
            right = await c.call("projected_view", view=front, direction="right")
            views = (await c.call("drawing_views"))["views"]
            boxes = [v["outline_mm"] for v in views if v["name"] != right["view"]]
            r = right["outline_mm"]
            assert not any(r[0] < b[2] and b[0] < r[2] and r[1] < b[3] and b[1] < r[3] for b in boxes)
            dim = await c.call("drawing_dimension", view=front, refs=["edge@-40,5,20", "edge@40,5,20"], orientation="horizontal")
            assert dim["value"] == pytest.approx(80)
            moved = await c.call("view_dimensions", view=front, move={dim["dimension"]: [95, 140]})
            assert next(d for d in moved["dimensions"] if d["name"] == dim["dimension"])["position_mm"] == [95.0, 140.0]
            height = await c.call("drawing_dimension", view=front, refs=["edge@-40,10,20", "edge@-40,0,20"], orientation="vertical", position=[30, 110])
            tol = await c.call("view_dimensions", view=front, tolerance={dim["dimension"]: {"type": "bilateral", "plus": 0.1, "minus": 0.02},
                                                                       height["dimension"]: {"type": "basic"}}, decimals={dim["dimension"]: 3})
            got = {d["name"]: d.get("tolerance") for d in tol["dimensions"]}
            assert got[dim["dimension"]] == {"type": "bilateral", "plus": 0.1, "minus": 0.02} and got[height["dimension"]] == {"type": "basic"}
            fit = await c.call("view_dimensions", view=front, tolerance={height["dimension"]: {"type": "fit_with_tolerance", "shaft": "h7"}})
            h7 = next(d for d in fit["dimensions"] if d["name"] == height["dimension"])["tolerance"]
            assert h7["shaft"] == "h7" and h7["plus"] == 0 and h7["minus"] == pytest.approx(0.015)  # ISO 286: 10 h7 is 0/-0.015
            holes = await c.call("drawing_table", kind="hole", view=top, face="face@10,10,0", position=[300, 285], anchor="top_left")
            assert len(holes["rows"]) == 6 and holes["rows"][-1][-1].startswith("<MOD-DIAM> 3.30")
            rev = await c.call("drawing_table", kind="revision", revisions=[{"rev": "A", "description": "INITIAL RELEASE", "approved": "QA"}])
            assert rev["rows"][-1][1:3] == ["A", "INITIAL RELEASE"]
            sheets = await c.call("sheets", action="add", name="Flat", size="A4")
            assert sheets["active"] == "Flat"
            await c.call("add_view", orientation="flat_pattern", model="cover.sldprt")
            assert (await c.call("add_view", orientation="right", model="block.sldprt", scale="1:5"))["scale"] == 0.2
            await c.call("sheets", action="activate", name="Sheet1")
            await c.call("save_document")
            pdf = await c.call("export", path="block.pdf")
            assert pdf["bytes"] > 10000
            await c.call("close_document")

            # a long bar at a set scale, shortened with a broken view
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-200, -10], "corner2": [200, 10]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            await c.call("save_document", path="bar.sldprt")
            bar = await c.call("create_drawing", sheet="A3", isometric=False, scale="1:2")
            assert bar["views"][0]["scale"] == 0.5 and min(v["outline_mm"][0] for v in bar["views"]) >= 0
            before = bar["views"][0]["outline_mm"]
            broken = await c.call("broken_view", view=bar["views"][0]["name"], start=[-150, 5, 0], end=[150, 5, 0])
            o = broken["outline_mm"]
            assert (before[2] - before[0]) - 150 < (o[2] - o[0]) < (before[2] - before[0]) - 130  # 300 mm cut out at 1:2, plus the break gap
            span = await c.call("drawing_dimension", view=bar["views"][0]["name"], refs=["edge@-200,5,10", "edge@200,5,10"], orientation="horizontal")
            assert span["value"] == pytest.approx(400)  # dimensions across the break keep the true length
            await c.call("close_document")
            await c.call("close_document", name="bar.SLDPRT")

            asm = await c.call("create_drawing", model="assy.sldasm", isometric=False)
            bom = await c.call("drawing_table", kind="bom", view=asm["views"][0]["name"])
            assert bom["balloons"] == 2 and [row[2] for row in bom["rows"][1:]] == ["Mounting block", ""]
            await c.call("close_document")
            for name in ("assy.SLDASM", "cover.SLDPRT", "block.SLDPRT"):  # models the drawings loaded
                await c.call("close_document", name=name)
        finally:
            await c.call("sw_info")

    run(tmp_path, body)


def test_advanced_mates_and_explode(tmp_path: Path):
    """Width, limit, gear and symmetric mates, and an exploded view with exact steps."""

    async def body(c: Session):
        async def part(name, entities, plane, depth):
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on=plane)
            await c.call("sketch", entities=entities)
            await c.call("finish_sketch")
            await c.call("extrude", depth=depth)
            await c.call("save_document", path=name)
            await c.call("close_document")

        await part("rail.sldprt", [{"type": "rectangle", "corner1": [-50, -10], "corner2": [50, 10]}], "plane:Top", 10)
        await part("slider.sldprt", [{"type": "rectangle", "corner1": [-10, -20], "corner2": [10, 20]}], "plane:Top", 8)
        await part("gear40.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 40}], "plane:Front", 5)
        await part("gear20.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 20}], "plane:Front", 5)
        await c.call("new_document", kind="assembly")
        try:
            await c.call("insert_component", path="rail.sldprt", fix=True)
            await c.call("insert_component", path="slider.sldprt", position=[5, 12, 3])
            await c.call("add_mate", type="coincident", refs=["face@slider-1/5,12,3", "face@rail-1/0,10,0"], alignment="anti_aligned")
            await c.call("add_mate", type="width", refs=["face@slider-1/5,14,23", "face@slider-1/5,14,-17"], tab=["face@rail-1/0,5,10", "face@rail-1/0,5,-10"])
            limit = await c.call("add_mate", type="limit_distance", refs=["face@slider-1/-5,14,0", "face@rail-1/-50,5,0"], distance=40, minimum=0, maximum=80)
            comps = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert comps["slider-1"] == pytest.approx([0, 10, 0])  # centred by the width mate, 40 mm from the rail end
            await c.call("set_dimensions", values={f"D1@{limit['mate']}": 70})
            comps = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert comps["slider-1"][0] == pytest.approx(30)
            assert "outside a limit" in await c.fails("set_dimensions", values={f"D1@{limit['mate']}": 100})

            await c.call("insert_component", path="gear40.sldprt", position=[0, 60, 0])
            await c.call("insert_component", path="gear20.sldprt", position=[30, 60, 0])
            assert (await c.call("add_mate", type="gear", refs=["face@gear40-1/20,60,2.5", "face@gear20-1/40,60,2.5"], ratio=[40, 20]))["mate"].startswith("Gear")
            assert (await c.call("add_mate", type="limit_angle", refs=["face@gear40-1/0,73,5", "face@rail-1/0,5,10"], minimum=-20, maximum=20))["mate"]
            await c.call("insert_component", path="slider.sldprt", position=[30, 40, 0])
            assert (await c.call("add_mate", type="symmetric", refs=["face@slider-1/40,14,0", "face@slider-2/40,44,0"], plane="plane:Right"))["mate"]

            before = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            await c.call("exploded_view", steps=[{"components": ["gear40-1"], "direction": "y", "distance": 40},
                                                 {"components": ["gear20-1"], "direction": "-z", "distance": 30}])
            after = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            moved = {k: [round(a - b, 3) for a, b in zip(after[k], before[k])] for k in before}
            assert moved["gear40-1"] == [0, 40, 0] and moved["gear20-1"] == [0, 0, -30] and moved["rail-1"] == [0, 0, 0]
            spun = await c.call("exploded_view", steps=[{"components": ["slider-2"], "rotate": 90, "axis": "y"}])
            assert spun["steps_added"][0]["rotate"] == 90
            await c.call("exploded_view", show=False)
            assert (await c.call("interference_check"))["interferences"] == 0
            # a pin centred in a slot, and a hinge between two plates
            await c.call("save_document", path="mates.sldasm")
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -20], "corner2": [40, 20]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=5)
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "slot", "start": [-20, 0], "end": [20, 0], "width": 8},
                                             {"type": "circle", "center": [30, 0], "diameter": 6}])
            await c.call("finish_sketch")
            await c.call("extrude", cut=True, end="through_all_both")
            await c.call("save_document", path="slotplate.sldprt")
            await c.call("close_document")
            await part("pin.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 8}], "plane:Top", 20)
            await c.call("insert_component", path="slotplate.sldprt", position=[0, -30, 0])
            await c.call("add_mate", type="lock", refs=["comp:slotplate-1", "comp:rail-1"])
            await c.call("insert_component", path="pin.sldprt", position=[10, -35, 7])
            assert (await c.call("add_mate", type="slot", refs=["face@slotplate-1/24,-27.5,0", "face@pin-1/14,-25,7"], slot_position="center"))["mate"]
            comps = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert comps["pin-1"][0] == pytest.approx(0) and comps["pin-1"][2] == pytest.approx(0)  # centred in the slot
            await c.call("insert_component", path="slotplate.sldprt", position=[15, -20, 5])
            hinge = await c.call("add_mate", type="hinge", refs=["face@slotplate-1/33,-27.5,0", "face@slotplate-2/48,-17.5,5"],
                                 faces=["face@slotplate-1/0,-25,15", "face@slotplate-2/15,-20,20"])
            assert hinge["type"] == "hinge"
            comps = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert comps["slotplate-2"][0] == pytest.approx(0) and comps["slotplate-2"][2] == pytest.approx(0)  # pivots on the hole axis
            # a jackscrew: 2 mm lead, so 0.5 mm of travel turns the screw a quarter turn
            await part("nut.sldprt", [{"type": "rectangle", "corner1": [-15, -15], "corner2": [15, 15]},
                                      {"type": "circle", "center": [0, 0], "diameter": 8}], "plane:Top", 10)
            await part("jackscrew.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 8}], "plane:Top", 60)
            await c.call("insert_component", path="nut.sldprt", position=[0, -200, 0])
            await c.call("add_mate", type="lock", refs=["comp:nut-1", "comp:rail-1"])
            await c.call("insert_component", path="jackscrew.sldprt", position=[0, -220, 0])
            await c.call("add_mate", type="concentric", refs=["face@nut-1/0,-195,4", "face@jackscrew-1/0,-210,-4"])
            assert (await c.call("add_mate", type="screw", refs=["face@nut-1/0,-195,4", "face@jackscrew-1/0,-210,-4"], distance=2))["mate"]
            travel = await c.call("add_mate", type="distance", refs=["face@nut-1/10,-200,10", "face@jackscrew-1/0,-220,-3"], distance=20)
            await c.call("set_dimensions", values={f"D1@{travel['mate']}": 20.5})
            screw_dims = (await c.call("dimensions", feature="Mates"))["dimensions"]["Mates"]
            assert any(d["name"].startswith("D1@Screw") and d["value"] == 2.0 for d in screw_dims)
            await c.call("insert_component", path="gear20.sldprt", position=[0, 120, 0])
            rack = await c.call("add_mate", type="rack_pinion", refs=["edge@rail-1/0,10,10", "face@gear20-2/10,120,2.5"], distance=20)
            assert rack["mate"].startswith("RackPinion")
            # a cam lobe (two arcs and two flats) standing upright: picking one flat, the follower rides over the top arc
            await part("lobe.sldprt", [{"type": "slot", "start": [0, -10], "end": [0, 10], "width": 40}], "plane:Front", 10)
            await part("follower.sldprt", [{"type": "rectangle", "corner1": [-10, 0], "corner2": [10, 20]}], "plane:Front", 10)
            await c.call("insert_component", path="lobe.sldprt", position=[0, 400, 0], fix=True)
            await c.call("insert_component", path="follower.sldprt", position=[0, 460, 0])
            assert (await c.call("add_mate", type="cam", refs=["face@lobe-1/20,400,5", "face@follower-1/0,460,5"]))["mate"]
            comps = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert comps["follower-1"][1] == pytest.approx(430)  # 10 mm flat half-length + 20 mm arc radius above the centre
            assert "tangent mate" in await c.fails("add_mate", type="cam", refs=["face@gear20-2/10,120,2.5", "face@follower-1/0,430,5"])
            # GD&T on assembly components, including one that is not at the origin
            assert (await c.call("add_datum_target", face="face@slotplate-1/-30,-25,10", at=[-30, -25, 10], label="C1"))["datum_target"] == "C1"
            await c.call("add_datum", face="face@rail-1/0,0,0", label="A")
            frame = await c.call("add_geometric_tolerance", face="face@rail-1/0,10,0", characteristic="parallelism", tolerance=0.05, datums=["A"])
            assert frame["feature_control_frame"] == "|parallelism|0.05|A"
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_gdt_and_fillets(tmp_path: Path):
    """Composite and projected-zone frames, datum targets, GD&T on assembly components, and fillet types."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            base = (await c.call("extrude", depth=10))["volume_mm3"]
            await c.call("hole", size="M5", kind="simple", positions=[[-30, 10, 15], [30, 10, 15]])
            await c.call("hole", size="M4", kind="tapped", positions=[[0, 10, -15]])
            multi = await c.call("hole", size="M3", kind="tapped", depth=6, positions=[[40, 5, 0], [-40, 5, -10], [20, 5, 25]])
            assert multi["faces"] == 3 and multi["holes"] == 3
            await c.call("add_datum", face="face@10,0,0", label="A")
            await c.call("add_datum", face="face@-40,5,0", label="B")
            await c.call("add_datum", face="face@0,5,25", label="C")
            comp = await c.call("add_geometric_tolerance", face="face@-30,5,12.25", characteristic="position", tolerance=0.2, datums=["A", "B", "C"],
                                diameter_zone=True, material_condition="mmc", composite={"tolerance": 0.05, "datums": ["A"]})
            assert comp["composite_lower"] == "|position|Ø0.05(M)|A"
            ptz = await c.call("add_geometric_tolerance", face="face@0,5,-13.35", characteristic="position", tolerance=0.1, datums=["A", "B", "C"],
                               diameter_zone=True, projected_zone=12)
            assert ptz["projected_zone"] == 12
            around = await c.call("add_geometric_tolerance", face="face@40,5,12", characteristic="profile_surface", tolerance=0.2, datums=["A"], applies_to="all_around")
            assert around["applies_to"] == "all_around"
            assert (await c.call("add_geometric_tolerance", face="face@-40,5,10", characteristic="profile_surface", tolerance=0.1, datums=["A"],
                                 between=["X", "Y"]))["between"] == ["X", "Y"]
            uz = await c.call("add_geometric_tolerance", face="face@40,5,12", characteristic="profile_surface", tolerance=0.4, unequal=0.1, datums=["A"])
            assert uz["feature_control_frame"] == "|profile_surface|0.4(U)0.1|A"
            assert "between 0 and the tolerance" in await c.fails("add_geometric_tolerance", face="face@40,5,12", characteristic="profile_surface",
                                                                  tolerance=0.1, unequal=0.2)
            assert "apply to profile" in await c.fails("add_geometric_tolerance", face="face@10,10,0", characteristic="flatness", tolerance=0.05, applies_to="all_over")
            assert "no larger than the upper" in await c.fails("add_geometric_tolerance", face="face@-40,5,-5", characteristic="position",
                                                               tolerance=0.05, composite={"tolerance": 0.1})
            holes_before = [f["name"] for f in (await c.call("list_features"))["features"] if f["type"] == "HoleWzd"]
            await c.fails("hole", size="M3", kind="tapped", depth=4, positions=[[-20, 10, -20], [0, 5, 25.5]])  # 2nd point is off the part
            assert [f["name"] for f in (await c.call("list_features"))["features"] if f["type"] == "HoleWzd"] == holes_before
            assert "Composite frames apply" in await c.fails("add_geometric_tolerance", face="face@10,10,0", characteristic="flatness", tolerance=0.05,
                                                         composite={"tolerance": 0.01})
            for label, at, shape, size in (("A1", [-30, 0, 0], "point", None), ("A2", [30, 0, 0], "circle", [6]), ("A3", [0, 0, 10], "rectangle", [8, 4])):
                t = await c.call("add_datum_target", face="face@10,0,0", at=at, label=label, shape=shape, size=size)
                assert t["datum_target"] == label
            await c.call("save_document", path="gdt.sldprt")
            drw = await c.call("create_drawing", sheet="A3")
            assert len(drw["views"]) == 4
            await c.call("close_document")

            # table- and curve-driven patterns of a hole
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-60, -40], "corner2": [60, 40]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=8)
            await c.call("start_sketch", on="face@0,8,0")
            await c.call("sketch", coords="model", entities=[{"type": "circle", "center": [-40, 8, -20], "diameter": 4}])
            await c.call("finish_sketch")
            seed = await c.call("extrude", cut=True, end="through_all")
            table = [[-20, 8, -20], [0, 8, -10], [25, 8, 5], [40, 8, 25]]
            await c.call("driven_pattern", features=[seed["feature"]], points=table)
            centres = {(f["axis_point"][0], f["axis_point"][2]) for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole")}
            assert centres == {(-40.0, -20.0)} | {(p[0], p[2]) for p in table}
            await c.call("driven_pattern", features=[seed["feature"]], curve="edge@0,8,40", count=4, spacing=25)
            assert len([f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole")]) == 8
            grid = await c.call("linear_pattern", features=[seed["feature"]], direction="edge@0,8,-40", count=2, spacing=10,
                                direction2="edge@-60,8,0", count2=2, spacing2=10)
            await c.call("edit_feature", feature=grid["feature"], spacing2=15)  # equal spacings: only direction 2 may change
            dims = {d["name"].split("@")[0]: d["value"] for d in (await c.call("dimensions", feature=grid["feature"]))["dimensions"][grid["feature"]]}
            assert dims["D3"] == 10 and dims["D4"] == 15
            await c.call("close_document")

            # fillets on fresh blocks: exact volumes
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            face = await c.call("fillet", kind="face", faces=["face@0,10,0"], faces2=["face@40,5,0"], radius=3)
            assert base - face["volume_mm3"] == pytest.approx(50 * 9 * (1 - 3.14159265 / 4), abs=0.01)
            start_z = (await c.call("list_edges", near=[-40, 10, 0], limit=1))["edges"][0]["start"][2]
            await c.call("fillet", kind="variable", edges=["edge@-40,10,0"], radii=[1, 4])
            ends = {e["mid"][2]: e["length"] for e in (await c.call("list_edges", type="spline"))["edges"] if e["mid"][0] < -35}
            # quarter-round profiles at the ends: 1 mm radius at the edge's start, 4 mm at its end
            assert ends[start_z] == pytest.approx(3.14159265 / 2, rel=0.05) and ends[-start_z] == pytest.approx(2 * 3.14159265, rel=0.05)
            await c.call("close_document")
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            block = (await c.call("extrude", depth=20))["volume_mm3"]
            conic = await c.call("fillet", radius=5, conic_rho=0.5, edges=["edge@0,20,25"])  # parabolic blend: 1/3 of the corner triangle
            assert block - conic["volume_mm3"] == pytest.approx(25 / 6 * 80, rel=0.01)
            asym = await c.call("fillet", radius=6, distance2=3, edges=["edge@0,0,-25"])
            assert conic["volume_mm3"] - asym["volume_mm3"] == pytest.approx(18 * (1 - 3.14159265 / 4) * 80, rel=0.02)
            await c.call("close_document")
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-5, -30], "corner2": [5, 30]}])
            await c.call("finish_sketch")
            rib = (await c.call("extrude", depth=20))["volume_mm3"]
            rnd = await c.call("fillet", kind="full_round", faces=["face@-5,10,0", "face@0,20,0", "face@5,10,0"])
            assert rib - rnd["volume_mm3"] == pytest.approx(60 * (50 - 3.14159265 * 25 / 2), abs=0.01)
            assert (await c.call("bounding_box"))["size"][1] == pytest.approx(20)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_hems_bends_splits_and_surfaces(tmp_path: Path):
    """Hems and sketched bends on sheet metal, body splits by planes and surfaces, and surface features."""

    async def body(c: Session):
        async def plate():
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.0, bend_radius=1.0)

        try:
            await plate()
            h = await c.call("hem", edges=["edge@0,0,30"], kind="closed", length=6)
            assert h["volume_mm3"] > 6000
            assert (await c.call("flat_pattern"))["bends"] == 1
            await c.call("close_document")
            for fixed, left_moves in (([-20, 0, 0], False), ([30, 0, 0], True)):
                await plate()
                await c.call("start_sketch", on="face@0,0,0")
                await c.call("sketch", coords="model", entities=[{"type": "line", "from": [10, 0, -30], "to": [10, 0, 30]}])
                await c.call("finish_sketch")
                await c.call("sketched_bend", fixed=fixed, angle=90)
                min_x = (await c.call("bounding_box"))["min"][0]
                assert (min_x > 5) if left_moves else min_x == pytest.approx(-50)  # the side holding the fixed point stays put
                await c.call("close_document")

            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=20)
            await c.call("ref_plane", mode="offset", refs=["plane:Right"], distance=10)
            halves = await c.call("split_body", tools=["plane:Plane1"])
            assert len(halves["bodies"]) == 2
            sizes = sorted(b["volume_mm3"] for b in (await c.call("list_bodies"))["bodies"])
            assert sizes == pytest.approx([30 * 50 * 20, 50 * 50 * 20])
            small = min((await c.call("list_bodies"))["bodies"], key=lambda b: b["volume_mm3"])["name"]
            assert (await c.call("delete_body", bodies=[f"body:{small}"]))["volume_mm3"] == pytest.approx(50 * 50 * 20)
            # a sloped surface through the rest splits it in two equal halves
            await c.call("ref_plane", mode="offset", refs=["plane:Front"], distance=30)
            await c.call("start_sketch", on="plane:Plane2")
            await c.call("sketch", entities=[{"type": "line", "from": [-50, 5], "to": [50, 15]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="extrude", depth=60, reverse=True)
            surf = (await c.call("list_bodies"))["surfaces"][0]["name"]
            cut = await c.call("split_body", tools=[f"body:{surf}"])
            assert len(cut["bodies"]) == 2
            assert "pass right through" in await c.fails("split_body", tools=["plane:Top"])
            await c.call("close_document")

            await c.call("new_document", kind="part")
            await c.call("ref_plane", mode="offset", refs=["plane:Front"], distance=200)
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "airfoil", "naca": "2412", "chord": 100, "origin": [-25, 0]}])
            await c.call("finish_sketch")
            await c.call("start_sketch", on="plane:Plane1")
            await c.call("sketch", entities=[{"type": "airfoil", "naca": "2412", "chord": 60, "origin": [-15, 0]}])
            await c.call("finish_sketch")
            skin = await c.call("surface", kind="loft", profiles=["Sketch1", "Sketch2"])
            assert skin["type"] == "BlendRefSurface"
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-30, -30], "corner2": [30, 30]}])
            await c.call("finish_sketch")
            assert (await c.call("surface", kind="planar"))["type"] == "PlanarSurface"
            assert len((await c.call("list_bodies"))["surfaces"]) == 2
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_weldment_frame(tmp_path: Path):
    """A square-tube frame with an upright (a branch), and an angle member, from generated profiles."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="3d")
            await c.call("sketch", entities=[{"type": "polyline", "closed": True, "points": [[0, 0, 0], [300, 0, 0], [300, 0, 200], [0, 0, 200]]},
                                             {"type": "line", "from": [0, 0, 0], "to": [0, 250, 0]}])
            await c.call("finish_sketch")
            frame = await c.call("structural_member", shape="square_tube", size=[20, 2], corner="butt")
            # tube section 400 - 256 = 144 mm2; 1000 mm of frame plus the upright, trimmed by half a tube at the joint
            assert frame["members"] == 5 and frame["volume_mm3"] == pytest.approx(144 * (1000 + 240), rel=0.001)
            cuts = {(i["quantity"], i["length_mm"]) for i in (await c.call("cut_list"))["items"]}
            assert cuts == {(2, 300.0), (2, 200.0), (1, 240.0)}  # butted members: long sides full length, upright trimmed
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "line", "from": [0, -300], "to": [300, -300]}])
            await c.call("finish_sketch")
            angle = await c.call("structural_member", shape="angle", size=[25, 25, 3])
            assert angle["volume_mm3"] - frame["volume_mm3"] == pytest.approx((25 + 22) * 3 * 300)
            assert "too thick" in await c.fails("structural_member", shape="round_tube", size=[16, 9])
            assert "half its height" in await c.fails("structural_member", shape="channel", size=[10, 20, 6])
            await c.call("set_material", name="6061-T6 (SS)")
            assert (await c.call("mass_properties"))["mass_kg"] == pytest.approx(angle["volume_mm3"] * 2.7e-6, rel=0.001)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_weldment_caps_and_gussets(tmp_path: Path):
    """A U frame of rectangular tube with its open ends capped and gussets in the corners."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "polyline", "points": [[300, 0], [0, 0], [0, 200]]},
                                             {"type": "line", "from": [300, 0], "to": [300, 150]}])
            await c.call("finish_sketch")
            frame = await c.call("structural_member", shape="rect_tube", size=[40, 30, 3])
            # section 40x30 - 34x24 = 384 mm2 over 650 mm of mitred centreline (the inner wall must stay 3 mm)
            assert frame["volume_mm3"] == pytest.approx(384 * 650, rel=1e-6)
            caps = await c.call("end_cap", thickness=4, offset=1, corner_chamfer=2)
            # only the two open tops are capped: (38 x 28 - 4 chamfers of 2 x 2 / 2) x 4
            assert [x["volume_mm3"] for x in caps["caps"]] == pytest.approx([(38 * 28 - 8) * 4] * 2)
            assert "No open tube ends" in await c.fails("end_cap")
            tri = await c.call("gusset", faces=["face@150,20,0", "face@20,100,0"], size=[80, 60], thickness=5)
            assert tri["plates"][0]["volume_mm3"] == pytest.approx(80 * 60 / 2 * 5)
            poly = await c.call("gusset", faces=["face@150,20,0", "face@280,75,0"], size=[80, 50], shape="polygon", end_length=10, thickness=4)
            assert poly["plates"][0]["volume_mm3"] == pytest.approx((80 * 50 - 70 * 40 / 2) * 4)
            assert "shorter than both legs" in await c.fails("gusset", faces=["face@150,20,0", "face@280,75,0"], size=[80, 50], shape="polygon", end_length=60)
            items = (await c.call("cut_list"))["items"]
            assert sum(i["quantity"] for i in items) == 3 + 2 + 2
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_dimxpert_schemes(tmp_path: Path):
    """Automatic DimXpert schemes on a drilled plate (prismatic) and a stepped shaft (turned)."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [80, 50]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            await c.call("start_sketch", on="face@40,10,-25")
            await c.call("sketch", coords="model", entities=[{"type": "circle", "center": [x, 10, z], "diameter": 6.6} for x in (15, 65) for z in (-15, -35)])
            await c.call("finish_sketch")
            await c.call("extrude", cut=True, end="through_all")
            datums = ["face@40,0,-25", "face@40,5,0", "face@0,5,-25"]
            geo = (await c.call("dimxpert", datums=datums))["annotations"]
            assert [a["label"] for a in geo if a["kind"] == "Datum"] == ["A", "B", "C"]
            kinds = {a["kind"] for a in geo}
            assert {"Flatness", "Perpendicularity", "Position", "Diameter"} <= kinds
            sizes = sorted(a["nominal"] for a in geo if a["kind"] == "DistanceBetween")
            assert sizes == pytest.approx([10, 50, 80])
            assert any(a["kind"] == "Diameter" and a["nominal"] == pytest.approx(6.6) for a in geo)
            pm = (await c.call("dimxpert", datums=datums, tolerance="plus_minus"))["annotations"]
            assert "Position" not in {a["kind"] for a in pm}  # replaced, not added to
            assert sorted(a["nominal"] for a in pm if "Hole Pattern" in a["name"] and a["kind"] == "DistanceBetween") == pytest.approx([15, 15])
            assert (await c.call("dimxpert", action="list"))["annotations"] == pm
            assert (await c.call("dimxpert", action="clear"))["annotations"] == []
            assert "1 to 3 datum faces" in await c.fails("dimxpert")
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 30}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=40)
            await c.call("start_sketch", on="face@0,0,40")
            await c.call("sketch", coords="model", entities=[{"type": "circle", "center": [0, 0, 40], "diameter": 20}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=30)
            shaft = (await c.call("dimxpert", datums=["face@15,0,20", "face@0,0,0"], part_type="turned"))["annotations"]
            assert sorted(a["nominal"] for a in shaft if a["kind"] == "Diameter") == pytest.approx([20, 30])
            assert any(a["kind"] == "CircularRunout" for a in shaft)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_radial_holes(tmp_path: Path):
    """Holes on a curved face: radial drillings in a tube, each square to the surface at its point."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 40}, {"type": "circle", "center": [0, 0], "diameter": 30}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=100)
            r = await c.call("hole", size="Ø6.6", fastener="drill", depth=5, positions=[[20, 0, 50], [0, 20, 70], [-20, 0, 30]])
            assert r["holes"] == 3 and len(r["features"]) == 3
            axes = {(tuple(f["axis"]), tuple(f["axis_point"])) for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole") and f["radius"] < 4}
            assert axes == {((1.0, 0.0, 0.0), (20.0, 0.0, 50.0)), ((0.0, 1.0, 0.0), (0.0, 20.0, 70.0)), ((-1.0, 0.0, 0.0), (-20.0, 0.0, 30.0))}
            tap = await c.call("hole", size="M5", kind="tapped", depth=5, positions=[[0, -20, 20]])
            assert tap["holes"] == 1 and ("note" in tap or "rebuild" not in tap)
            before = len((await c.call("list_features"))["features"])
            assert "nearest face" in await c.fails("hole", size="Ø6.6", fastener="drill", positions=[[20, 0, 10], [40, 40, 40]])
            assert len((await c.call("list_features"))["features"]) == before
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_read_only_mode(tmp_path: Path):
    async def body(c: Session):
        info = await c.call("sw_info")
        assert info["read_only"] is True
        assert "read-only mode" in await c.fails("new_document", kind="part")
        assert (await c.call("list_materials", search="Ti-6Al"))["count"] >= 1

    run(tmp_path, body, "--read-only")


def test_marking_required_for_export(tmp_path: Path):
    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 30}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            assert "Export blocked" in await c.fails("export", path="part.step")
            await c.call("custom_properties", set={"ExportControl": "TEST DATA"})
            assert (await c.call("export", path="part.step", step_ap="214"))["solids"] == 1
        finally:
            await c.call("close_document")

    run(tmp_path, body, "--require-marking")

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
            await c.call("modify_sketch", operations=[{"op": "relation", "type": "along_y", "items": ["seg@40,20,0"]}])
            assert "conflicts" in await c.fails("modify_sketch", operations=[{"op": "relation", "type": "along_x", "items": ["seg@40,20,0"]}])
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
            # a stiffening dimple and a flanged lightening hole in the tray floor, placed exactly; one off the edge is refused
            dimple = await c.call("forming_tool", tool_name="dimple", position=[-20, 0, 0])
            hole = await c.call("forming_tool", tool_name="extruded hole", position=[25, 0, 0])
            assert dimple["type"] == hole["type"] == "FormToolInstance"
            before = len((await c.call("list_features"))["features"])
            assert "could not be placed" in await c.fails("forming_tool", tool_name="dimple", position=[44, 0, 26])  # into the bends
            assert len((await c.call("list_features"))["features"]) == before
            assert "Available:" in await c.fails("forming_tool", tool_name="no such tool", position=[0, 0, 0])
            await c.call("close_document")
            # an L section (sketched on the inside, 40 and 60 mm legs) bent with K = 0.3: flat = 100 - 2 r + pi/2 (r + K t)
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "polyline", "points": [[0, 40], [0, 0], [60, 0]]}])
            await c.call("finish_sketch")
            lk = await c.call("sheet_metal_base", thickness=2, bend_radius=2, depth=30, k_factor=0.3)
            assert lk["k_factor"] == 0.3
            fp = await c.call("flat_pattern")
            assert fp["default_k_factor"] == 0.3 and fp["flat_size_mm"][0] == pytest.approx(100 - 4 + 3.14159265 / 2 * (2 + 0.3 * 2), abs=0.01)
            await c.call("close_document")
            # a 1.6 mm joggle in a 1.6 mm strip, so the end can lap over a doubler of the same thickness
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-60, -20], "corner2": [60, 20]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.6, bend_radius=1.6)
            j = await c.call("jog", line=[[0, 0, -20], [0, 0, 20]], fixed=[-30, 0, 0], offset=1.6, angle=30)
            assert j["offset"] == pytest.approx(1.6, abs=0.01) and len(j["features"]) == 2
            assert (await c.call("bounding_box"))["size"][1] == pytest.approx(3.2, abs=0.01)  # sheet plus joggle
            assert (await c.call("flat_pattern"))["bends"] == 2
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
            shown = [d["name"] for v in drw["views"] for d in (await c.call("view_dimensions", view=v["name"]))["dimensions"]]
            assert shown and len(shown) == len(set(shown))  # each model dimension in one view only
            rows = []
            for v in drw["views"]:
                tidy = await c.call("view_dimensions", view=v["name"], arrange=True, spacing=8)
                x0, y0, x1, y1 = tidy["outline_mm"]
                for d in tidy["dimensions"]:
                    if d["name"] in tidy["arranged"]:
                        x, y = d["position_mm"]
                        rows.append([h for h in (y - y1, y0 - y, x - x1, x0 - x) if h > 0])  # off the view on its side
            assert rows and all(any(abs(h / 8 - round(h / 8)) < 0.01 for h in r) for r in rows)  # rows 8 mm apart
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
            # a carriage corner on a curved track (an assembly sketch arc of radius 50 about (0, 600))
            await c.call("insert_component", path="slider.sldprt", position=[30, 660, 20])
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "arc", "center": [0, 600], "start": [50, 600], "end": [-50, 600]}])
            track = (await c.call("finish_sketch"))["sketch"]
            pm = await c.call("add_mate", type="path", refs=["vertex@slider-3/30,660,20", f"sketch:{track}"], path_orientation="fixed")
            assert len(pm["orientation_mates"]) == 2  # pitch, yaw and roll held by two parallel mates
            x, y, z = pm["vertex_at_mm"]
            assert math.hypot(x, y - 600) == pytest.approx(50, abs=1e-3) and z == pytest.approx(0, abs=1e-3)
            # a track of a straight run and a bend: the chain is fitted with one spline and the corner rides it
            await c.call("insert_component", path="slider.sldprt", position=[-40, 870, 10])
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "line", "from": [-80, 850], "to": [0, 850]},
                                             {"type": "arc", "center": [0, 800], "start": [50, 800], "end": [0, 850]}])
            chain = (await c.call("finish_sketch"))["sketch"]
            cm = await c.call("add_mate", type="path", refs=["vertex@slider-4/-40,870,10", f"sketch:{chain}"], path_orientation="fixed")
            cx, cy, cz = cm["vertex_at_mm"]
            assert cm["fit_deviation_mm"] < 0.01 and cy == pytest.approx(850, abs=1e-3) and cz == pytest.approx(0, abs=1e-3) and -80 <= cx <= 0
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
            frames = [a for v in (await c.call("annotation_views"))["views"] for a in v["annotations"] if a["kind"] == "feature_control_frame"]
            pick = next(a["name"] for a in frames if a.get("text", "").startswith("0.4"))
            av = await c.call("annotation_views", action="create", direction="right", annotations=[pick])
            assert av["annotations"] == [pick] and any(v["annotations"] and v["annotations"][0]["name"] == pick for v in av["views"])
            assert "No annotation named" in await c.fails("annotation_views", action="create", direction="top", annotations=["Nope1"])
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
            descs = {i.get("description") for i in items}
            assert {"RECTANGULAR TUBE 40 X 30 X 3", "END CAP 4 THK", "GUSSET 5 THK", "GUSSET 4 THK"} <= descs
            await c.call("save_document", path="u_frame.sldprt")
            d = await c.call("create_drawing", sheet="A3", isometric=False)
            table = await c.call("drawing_table", kind="cut_list", view=d["views"][0]["name"])
            assert table["rows"][0][:4] == ["ITEM NO.", "QTY.", "DESCRIPTION", "LENGTH"]
            assert sum(int(r[1]) for r in table["rows"][1:]) == 7 and any(r[2] == "END CAP 4 THK" for r in table["rows"])
            await c.call("close_document")
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
            pos = next(a["name"] for a in geo if a["kind"] == "Position")
            assert "geometric tolerance" in await c.fails("dimxpert", action="set_tolerance", tolerances={pos: {"plus": 0.1}})
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
            hole = next(a["name"] for a in pm if a["kind"] == "Diameter")
            spaced = next(a["name"] for a in pm if "Hole Pattern" in a["name"] and a["kind"] == "DistanceBetween")
            out = await c.call("dimxpert", action="set_tolerance", tolerances={hole: {"plus": 0.05, "minus": 0}, spaced: {"plus": 0.1}})
            got = {a["name"]: a for a in out["annotations"]}
            assert got[hole]["plus"] == pytest.approx(0.05) and got[hole]["minus"] == pytest.approx(0)
            assert got[spaced]["plus"] == pytest.approx(0.1) and got[spaced]["minus"] == pytest.approx(-0.1)
            assert "No DimXpert annotation" in await c.fails("dimxpert", action="set_tolerance", tolerances={"Nothing1": {"plus": 0.1}})
            # the scheme comes onto the drawing with its tolerances (MBD to 2D)
            await c.call("save_document", path="dx_plate.sldprt")
            d = await c.call("create_drawing", sheet="A3", isometric=False)
            shown = [x for v in d["views"] for x in (await c.call("view_dimensions", view=v["name"]))["dimensions"]]
            assert any(x["value"] == pytest.approx(6.6) and (x.get("tolerance") or {}).get("plus") == pytest.approx(0.05) for x in shown)
            await c.call("close_document")
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


def test_radial_explode(tmp_path: Path):
    """A ring of four parts exploded straight out from their centre axis."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-2, -2], "corner2": [2, 2]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=4)
        await c.call("save_document", path="nub.sldprt")
        await c.call("close_document")
        await c.call("new_document", kind="assembly")
        try:
            ring = [[20, 0, 0], [0, 20, 0], [-20, 0, 0], [0, -20, 0]]
            for p in ring:
                await c.call("insert_component", path="nub.sldprt", position=p)
            before = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            out = await c.call("exploded_view", steps=[{"components": list(before), "radial": True, "distance": 30}])
            assert out["steps_added"][0]["radial"] is True
            after = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            for k, b in before.items():  # each part moves 30 mm further out along its own radius
                assert after[k] == pytest.approx([v * 2.5 for v in b])
            assert "two or more components" in await c.fails("exploded_view", steps=[{"components": ["nub-1"], "radial": True, "distance": 30}])
            await c.call("exploded_view", show=False)
            # group two of the ring into a kit for the BOM, then dissolve it again; nothing may move
            kit = await c.call("subassembly", action="form", components=["nub-1", "nub-2"], name="Nub-Kit")
            assert kit["subassembly"].startswith("Nub-Kit") and sum("/" in n for n in kit["components"]) == 2
            assert any(line["file"].startswith("Nub-Kit") for line in (await c.call("bom"))["lines"])
            flat = await c.call("subassembly", action="dissolve", name="Nub-Kit")
            assert not any("/" in n for n in flat["components"]) and len(flat["components"]) == 4
            # a kit saved out to its own file for release: the assembly then references that file
            await c.call("subassembly", action="form", components=["nub-3", "nub-4"], name="Nub-Pair")
            out = await c.call("subassembly", action="save_out", name="Nub-Pair", path="Nub-Pair.sldasm")
            assert Path(out["file"]).exists() and out["subassembly"] == "Nub-Pair-1"
            assert sum(n.startswith("Nub-Pair-1/") for n in out["components"]) == 2  # instance numbers change on dissolve
            assert "already saved" in await c.fails("subassembly", action="save_out", name="Nub-Pair-1")
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_weldment_trim_extend(tmp_path: Path):
    """A post drawn 10 mm short of a beam, from a separate sketch, extended to meet it at a butted corner."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "line", "from": [0, 0], "to": [300, 0]}])
            await c.call("finish_sketch")
            await c.call("structural_member", shape="square_tube", size=[40, 3])
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "line", "from": [300, 200], "to": [300, 30]}])
            await c.call("finish_sketch")
            await c.call("structural_member", shape="square_tube", size=[40, 3])
            r = await c.call("trim_members", members=["body:square_tube_40x3 (2)"], to=["body:square_tube_40x3 (1)"], corner="butt")
            # the post comes down onto the beam's top face (y = 20); the beam runs out to the post's far side (x = 320)
            assert sorted(i["length_mm"] for i in r["cut_list"]) == [180.0, 320.0]
        finally:
            await c.call("close_document")
        # T-joints: a post stopping short of a beam, and one running through it, both ended on the beam's top face
        for end_y in (40, -60):
            await c.call("new_document", kind="part")
            try:
                await c.call("start_sketch", on="plane:Front")
                await c.call("sketch", entities=[{"type": "line", "from": [0, 0], "to": [300, 0]}])
                await c.call("finish_sketch")
                await c.call("structural_member", shape="square_tube", size=[40, 3])
                await c.call("start_sketch", on="plane:Front")
                await c.call("sketch", entities=[{"type": "line", "from": [150, 200], "to": [150, end_y]}])
                await c.call("finish_sketch")
                await c.call("structural_member", shape="square_tube", size=[40, 3])
                t = await c.call("trim_members", members=["body:square_tube_40x3 (2)"], to=["body:square_tube_40x3 (1)"], corner="end")
                assert t["members"][0]["moved_mm"] == pytest.approx(end_y - 20)  # +20 extends the short post, -80 trims the long one
                assert sorted(i["length_mm"] for i in t["cut_list"]) == [180.0, 300.0]  # the beam is left as it is
                assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(444 * (300 + 180))
            finally:
                await c.call("close_document")

    run(tmp_path, body)


def test_spline_editing(tmp_path: Path):
    """Reshape a spline by moving a fit point exactly and adding one."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Right")
            await c.call("sketch", entities=[{"type": "spline", "points": [[0, 0], [20, 10], [40, 5], [60, 15]]}])
            await c.call("modify_sketch", operations=[{"op": "set_point", "items": ["pt@20,10"], "to": [20, 25]}])
            ins = await c.call("modify_sketch", operations=[{"op": "spline_insert", "items": ["seg@40,5"], "at": [50, 10]}])
            added = ins["done"][0]["inserted"]
            pts = sorted(tuple(p["at"]) for p in (await c.call("list_sketch"))["points"])
            assert (20.0, 25.0) in pts and tuple(added) in pts and len(pts) == 5
            tan = await c.call("modify_sketch", operations=[{"op": "spline_tangent", "items": ["seg@40,5"], "at": [0, 0], "angle": 60, "weight": 1.5}])
            assert tan["done"][0]["tangent_deg"] == pytest.approx(60)
            assert "not a fit point" in await c.fails("modify_sketch", operations=[{"op": "spline_tangent", "items": ["seg@40,5"], "at": [5, 5], "angle": 0}])
            # a reference image to trace (a scanned legacy drawing), scaled to 120 mm wide
            from PIL import Image
            Image.new("RGB", (400, 200), "white").save(tmp_path / "scan.png")
            pic = await c.call("modify_sketch", operations=[{"op": "picture", "file": "scan.png", "at": [0, -80], "width": 120}])
            assert pic["done"][0]["size_mm"] == pytest.approx([120, 60]) and pic["done"][0]["origin_mm"] == pytest.approx([0, -80])
        finally:
            await c.call("close_document")
        # a sketch block (a cutout with its hole) placed twice more, then extruded
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [20, 10]}, {"type": "circle", "center": [10, 5], "diameter": 4}])
            blk = await c.call("modify_sketch", operations=[{"op": "block", "items": ["seg@10,0", "seg@20,5", "seg@10,10", "seg@0,5", "seg@12,5"],
                                                              "base": [0, 0], "copies": [[40, 0], [80, 20]], "save_as": "cutout.sldblk"}])
            assert blk["done"][0]["instances"] == 3 and (tmp_path / "cutout.sldblk").exists()
            await c.call("finish_sketch")
            assert (await c.call("extrude", depth=5))["volume_mm3"] == pytest.approx(3 * (200 - math.pi * 4) * 5)
            assert (await c.call("bounding_box"))["size"] == pytest.approx([100, 5, 30])
        finally:
            await c.call("close_document")
        # the saved block reused in another part, twice, the second turned 90 degrees
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("modify_sketch", operations=[{"op": "insert_block", "file": "cutout.sldblk", "at": [0, 0]},
                                                      {"op": "insert_block", "file": "cutout.sldblk", "at": [60, 0], "angle": 90}])
            await c.call("finish_sketch")
            assert (await c.call("extrude", depth=5))["volume_mm3"] == pytest.approx(2 * (200 - math.pi * 4) * 5)
            assert (await c.call("bounding_box"))["size"] == pytest.approx([60, 5, 20])
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_tolerance_stack(tmp_path: Path):
    """A clearance stack: a housing width toleranced on the drawing, less a spacer and a bracket."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [50, 30]}])
        await c.call("finish_sketch", fully_define=True)
        await c.call("extrude", depth=10)
        width = next(d["name"] for d in (await c.call("dimensions", feature="Sketch1"))["dimensions"]["Sketch1"] if d["value"] == 50)
        width = "@".join(width.split("@")[:2])
        await c.call("save_document", path="housing.sldprt")
        drw = await c.call("create_drawing", sheet="A3", isometric=False)
        try:
            for v in drw["views"]:
                if width in [d["name"] for d in (await c.call("view_dimensions", view=v["name"]))["dimensions"]]:
                    await c.call("view_dimensions", view=v["name"], tolerance={width: {"type": "bilateral", "plus": 0.1, "minus": 0.0}})
            r = await c.call("tolerance_stack", requirement={"min": 0.1, "max": 0.5}, contributors=[
                {"dimension": width, "direction": 1},
                {"name": "spacer", "nominal": 20, "plus": 0.05, "minus": 0.05, "direction": -1},
                {"name": "bracket", "nominal": 29.8, "plus": 0, "minus": 0.1, "direction": -1}])
            assert r["contributors"][0]["plus"] == pytest.approx(0.1) and r["contributors"][0]["minus"] == pytest.approx(0)
            assert r["nominal"] == pytest.approx(0.2) and r["worst_case"] == pytest.approx([0.15, 0.45])
            assert r["rss"] == pytest.approx([0.3 - 0.0866, 0.3 + 0.0866], abs=1e-4) and r["worst_case_ok"] and r["rss_ok"]
            tight = await c.call("tolerance_stack", requirement={"min": 0.2}, contributors=r["contributors"])
            assert tight["worst_case_ok"] is False and tight["rss_ok"] is True  # worst case 0.15 < 0.2; RSS 0.213
        finally:
            await c.call("close_document")
            await c.call("close_document")

    run(tmp_path, body)


def test_loft_and_sweep_editing(tmp_path: Path):
    """Insert a section into a loft and swap a sweep's profile, through edit_feature."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "line", "from": [0, 0], "to": [0, 100]}])
            await c.call("finish_sketch")
            for e in ({"type": "center_rectangle", "center": [0, 0], "size": [10, 10]}, {"type": "circle", "center": [0, 0], "diameter": 12}):
                await c.call("start_sketch", on="plane:Top")
                await c.call("sketch", entities=[e])
                await c.call("finish_sketch")
            sw = await c.call("sweep", path="Sketch1", profile="Sketch2")
            assert sw["volume_mm3"] == pytest.approx(100 * 100)
            r = await c.call("edit_feature", feature=sw["feature"], profiles=["Sketch3"])
            assert r["volume_mm3"] == pytest.approx(math.pi * 36 * 100)
            assert "not a sketch" in await c.fails("edit_feature", feature=sw["feature"], profiles=["Sweep1"])
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "center_rectangle", "center": [0, 0], "size": [40, 40]}])
            await c.call("finish_sketch")
            for d, dia in ((60, 30), (30, 60)):
                plane = (await c.call("ref_plane", mode="offset", refs=["plane:Top"], distance=d))["feature"]
                await c.call("start_sketch", on=plane)
                await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": dia}])
                await c.call("finish_sketch")
            lo = await c.call("loft", profiles=["Sketch1", "Sketch2"])
            fat = await c.call("edit_feature", feature=lo["feature"], profiles=["Sketch1", "Sketch3", "Sketch2"])
            assert fat["volume_mm3"] > lo["volume_mm3"] * 1.5  # bulges through the 60 mm section
            box = await c.call("bounding_box")
            assert box["size"][0] >= 60  # passes through the 60 mm section (a smooth loft bulges a little past it)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_gauges_and_configuration_table(tmp_path: Path):
    """A gauge-table blank, and a dash-numbered plate family built from a configuration table."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            table = await c.call("gauge_table", table="aluminum metric")
            assert any(g["gauge"] == "Gauge 14" and g["thickness_mm"] == 2.0 for g in table["gauges"])
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            base = await c.call("sheet_metal_base", gauge="14", gauge_table="aluminum metric")
            assert base["thickness"] == 2.0 and base["bend_radius"] == 2.0 and base["volume_mm3"] == pytest.approx(100 * 60 * 2)
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [60, 30]}])
            await c.call("finish_sketch", fully_define=True)
            await c.call("extrude", depth=5)
            await c.call("start_sketch", on="face@10,5,-10")
            await c.call("sketch", coords="model", entities=[{"type": "circle", "center": [15, 5, -15], "diameter": 6}])
            await c.call("finish_sketch")
            await c.call("extrude", cut=True, end="through_all")
            dims = (await c.call("dimensions"))["dimensions"]
            length = next(d["name"] for d in dims["Sketch1"] if d["value"] == 60).split("@Part")[0]
            thick = dims["Boss-Extrude1"][0]["name"].split("@Part")[0]
            t = await c.call("configuration_table", export_csv="plates.csv", rows=[
                {"config": "-001", "dimensions": {length: 60, thick: 5}, "properties": {"PartNo": "PLT-001"}},
                {"config": "-002", "dimensions": {length: 90, thick: 5}, "suppress": ["Cut-Extrude1"]},
                {"config": "-003", "dimensions": {length: 120, thick: 8}}])
            assert {r["config"]: r[length] for r in t["configurations"]} == {"Default": 60, "-001": 60, "-002": 90, "-003": 120}
            hole = math.pi * 9
            for cfg, vol in (("-001", 60 * 30 * 5 - hole * 5), ("-002", 90 * 30 * 5), ("-003", 120 * 30 * 8 - hole * 8)):
                await c.call("configurations", action="activate", name=cfg)
                assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(vol)  # -003 keeps the hole -002 suppressed
            assert (tmp_path / "plates.csv").read_text().splitlines()[0] == f"config,{length},{thick}"
            # the tabulated drawing: one dash-number row per configuration
            await c.call("save_document", path="plates.sldprt")
            await c.call("create_drawing", sheet="A3", isometric=False)
            tab = await c.call("drawing_table", kind="general", dimensions={length: "L", thick: "T"}, position=[330, 120])
            assert tab["cells"][0] == ["CONFIG", "L", "T"] and ["-003", "120", "8"] in tab["cells"]
            dash = await c.call("drawing_table", kind="general", dimensions={length: "L", thick: "T"}, configurations=["-001", "-002", "-003"], position=[330, 60])
            assert [r[0] for r in dash["cells"][1:]] == ["-001", "-002", "-003"]  # only the dash numbers, in order
            assert "No configuration -009" in await c.fails("drawing_table", kind="general", dimensions={length: "L"}, configurations=["-009"])
            assert "needs cells" in await c.fails("drawing_table", kind="general")
            await c.call("close_document")
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_setback_fillet(tmp_path: Path):
    """A corner where three filleted edges meet, blended with a setback; an impossible setback is refused cleanly."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            edges = ["edge@40,10,0", "edge@0,10,-25", "edge@40,5,-25"]
            plain = await c.call("fillet", radius=3, edges=edges)
            await c.call("undo")
            sb = await c.call("fillet", radius=3, edges=edges, setback=[{"vertex": [40, 10, -25], "distance": 6}])
            assert sb["setback"] and sb["volume_mm3"] < plain["volume_mm3"] - 10  # the setback blend removes more at the corner
            before = len((await c.call("list_features"))["features"])
            assert "refused the setback" in await c.fails("fillet", radius=2, edges=["edge@-40,10,0", "edge@0,10,25", "edge@-40,5,25"],
                                                          setback=[{"vertex": [-40, 10, 25], "distance": 12}])
            assert len((await c.call("list_features"))["features"]) == before
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_knit_and_thicken(tmp_path: Path):
    """Two skin panels knitted and thickened, and a closed set of surfaces knitted into a solid."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            for c1, c2 in (([0, 0], [40, 50]), ([40, 0], [80, 50])):
                await c.call("start_sketch", on="plane:Top")
                await c.call("sketch", entities=[{"type": "rectangle", "corner1": c1, "corner2": c2}])
                await c.call("finish_sketch")
                await c.call("surface", kind="planar")
            k = await c.call("knit_surfaces")
            assert k["surface_bodies"] == 1
            skin = (await c.call("list_bodies"))["surfaces"][0]["name"]
            t = await c.call("thicken", body=f"body:{skin}", thickness=1.5, side="normal")
            assert t["volume_mm3"] == pytest.approx(80 * 50 * 1.5)
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 30]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="extrude", sketch="Sketch1", depth=20)
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 30]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="planar")
            assert "did not close" in await c.fails("knit_surfaces", form_solid=True)  # the top is still open
            plane = (await c.call("ref_plane", mode="offset", refs=["plane:Top"], distance=20))["feature"]
            await c.call("start_sketch", on=plane)
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 30]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="planar")
            box = await c.call("knit_surfaces", form_solid=True)
            assert box["solid_bodies"] == 1 and box["volume_mm3"] == pytest.approx(40 * 30 * 20)
        finally:
            await c.call("close_document")
        # an open duct capped with fill surfaces and knitted solid
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 30]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="extrude", sketch="Sketch1", depth=20)
            cap = await c.call("fill_surface", edges=["edge@20,20,0", "edge@40,20,-15", "edge@20,20,-30", "edge@0,20,-15"])
            assert cap["patch"]["area_mm2"] == pytest.approx(1200)
            closed = await c.call("fill_surface", edges=["edge@20,0,0", "edge@40,0,-15", "edge@20,0,-30", "edge@0,0,-15"], knit=True)
            assert closed["knit"]["volume_mm3"] == pytest.approx(24000)
        finally:
            await c.call("close_document")
        # a skin trimmed by a cutting surface, keeping either side
        for keep, area in (([25, 0, 0], 30 * 50), ([-20, 0, 0], 50 * 50)):
            await c.call("new_document", kind="part")
            try:
                await c.call("start_sketch", on="plane:Top")
                await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
                await c.call("finish_sketch")
                await c.call("surface", kind="planar")
                plane = (await c.call("ref_plane", mode="offset", refs=["plane:Top"], distance=5, flip=True))["feature"]
                await c.call("start_sketch", on=plane)
                await c.call("sketch", coords="model", entities=[{"type": "line", "from": [10, -5, -40], "to": [10, -5, 40]}])
                await c.call("finish_sketch")
                await c.call("surface", kind="extrude", depth=10)
                t = await c.call("trim_surface", target="body:Surface-Plane1", tool_body="body:Surface-Extrude1", keep=keep)
                assert t["area_mm2"] == pytest.approx(area)
            finally:
                await c.call("close_document")

    run(tmp_path, body)


def test_direct_editing_of_imports(tmp_path: Path):
    """A supplier STEP part with no history: a pocket removed (its split cylinder found whole) and faces moved."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=10)
        await c.call("start_sketch", on="face@0,10,0")
        await c.call("sketch", coords="model", entities=[{"type": "circle", "center": [0, 10, 0], "diameter": 10}])
        await c.call("finish_sketch")
        await c.call("extrude", cut=True, depth=4)
        await c.call("save_document", path="supplier.sldprt")
        await c.call("export", path="supplier.step", step_ap="214")
        await c.call("close_document")
        await c.call("import_file", path="supplier.step")
        try:
            gone = await c.call("delete_faces", faces=["face@0,6,0", "face@5,8,0"])
            assert gone["deleted_faces"] == 3 and gone["volume_mm3"] == pytest.approx(80 * 50 * 10)  # both wall halves found
            assert (await c.call("move_faces", faces=["face@40,5,0"], distance=5))["volume_mm3"] == pytest.approx(85 * 50 * 10)
            assert (await c.call("move_faces", faces=["face@0,10,0"], distance=-2))["volume_mm3"] == pytest.approx(85 * 50 * 8)
            assert (await c.call("bounding_box"))["size"] == pytest.approx([85, 8, 50])
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_motion_study(tmp_path: Path):
    """A motor-driven rotor and slider: positions at chosen times match speed x time, and the model is left as it was."""

    async def body(c: Session):
        async def part(name, entities, plane, depth):
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on=plane)
            await c.call("sketch", entities=entities)
            await c.call("finish_sketch")
            await c.call("extrude", depth=depth)
            await c.call("save_document", path=name)
            await c.call("close_document")

        await part("shaft.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 10}], "plane:Front", 20)
        await part("rotor.sldprt", [{"type": "circle", "center": [0, 0], "diameter": 40}, {"type": "circle", "center": [0, 0], "diameter": 10}], "plane:Front", 5)
        await part("rail.sldprt", [{"type": "rectangle", "corner1": [-50, -10], "corner2": [50, 10]}], "plane:Top", 10)
        await part("slider.sldprt", [{"type": "rectangle", "corner1": [0, 0], "corner2": [10, 10]}], "plane:Top", 5)
        await c.call("new_document", kind="assembly")
        try:
            await c.call("insert_component", path="shaft.sldprt", fix=True)
            await c.call("insert_component", path="rotor.sldprt")
            await c.call("add_mate", type="concentric", refs=["face@rotor-1/5,0,2.5", "face@shaft-1/5,0,10"])
            await c.call("add_mate", type="coincident", refs=["face@rotor-1/15,0,0", "face@shaft-1/0,0,0"])
            spin = await c.call("motion_study", motors=[{"face": "face@rotor-1/20,0,2.5", "kind": "rotary", "rpm": 60}], duration=1.0, times=[0.25, 0.5])
            assert [r["components"]["rotor-1"]["turned_deg"] for r in spin["results"]] == pytest.approx([90, 180], abs=0.05)
            await c.call("insert_component", path="rail.sldprt", position=[0, 100, 0], fix=True)
            await c.call("insert_component", path="slider.sldprt", position=[-20, 110, 5])
            await c.call("add_mate", type="coincident", refs=["face@slider-1/-15,110,0", "face@rail-1/0,110,0"])
            await c.call("add_mate", type="coincident", refs=["face@slider-1/-15,112.5,5", "face@rail-1/0,105,10"], alignment="aligned")
            before = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            slide = await c.call("motion_study", motors=[{"face": "face@slider-1/-10,112.5,0", "kind": "linear", "speed": 10}], duration=2.0, times=[0.73, 1.95])
            for r in slide["results"]:  # speed x the time actually solved (the solver may round the end time to its step)
                assert r["components"]["slider-1"]["moved_mm"][0] == pytest.approx(10 * r.get("solved_time_s", r["time_s"]), abs=0.01)
            after = {r["name"]: r["origin_mm"] for r in (await c.call("list_components"))["components"]}
            assert after == before and "warning" not in slide  # the model is back where it started
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_mould_split(tmp_path: Path):
    """Core and cavity blocks for a drafted part on a flat parting line, and for a part the plane runs through."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "center_rectangle", "center": [0, 0], "size": [60, 40]}])
            await c.call("finish_sketch")
            part = (await c.call("extrude", depth=20, draft=3))["volume_mm3"]
            m = await c.call("mould_split", parting_plane="plane:Top", margin=20, above=30, below=30)
            assert m["core"]["volume_mm3"] == pytest.approx(100 * 80 * 30) and m["cavity"]["volume_mm3"] == pytest.approx(100 * 80 * 30 - part)
            assert len((await c.call("list_bodies"))["bodies"]) == 3  # part, cavity, core
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 20}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=40)
            m = await c.call("mould_split", parting_plane="plane:Top", margin=10, above=20, below=20, shrinkage=0.5)
            assert m["core"]["volume_mm3"] == pytest.approx(m["cavity"]["volume_mm3"])  # the plane halves the cylinder
            scaled = math.pi * (10 * 1.005) ** 2 * 40 * 1.005
            assert m["block_mm3"] == pytest.approx(m["core"]["volume_mm3"] * 2 + scaled, rel=1e-6)
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


def test_3d_sketch_dimensions(tmp_path: Path):
    """A tube route in a 3D sketch fully defined by axis relations and driving dimensions, then driven."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="3d")
            await c.call("sketch", entities=[{"type": "polyline", "points": [[0, 0, 0], [50, 0, 0], [50, 30, 0], [50, 30, 40]]}])
            out = await c.call("modify_sketch", operations=[
                {"op": "relation", "type": "coincident", "items": ["pt@0,0,0", "origin"]},
                {"op": "relation", "type": "along_x", "items": ["seg@25,0,0"]},
                {"op": "relation", "type": "along_y", "items": ["seg@50,15,0"]},
                {"op": "relation", "type": "along_z", "items": ["seg@50,30,20"]},
                {"op": "dimension", "items": ["seg@25,0,0"], "value": 80},
                {"op": "dimension", "items": ["seg:1"], "value": 25},
                {"op": "dimension", "items": ["seg:2"], "value": 60}])
            assert out["status"] == "fully_defined"
            segs = (await c.call("list_sketch"))["segments"]
            assert [s["length"] for s in segs] == pytest.approx([80, 25, 60])
            assert segs[2]["end"] == pytest.approx([80, 25, 60])
            assert "aligned" in await c.fails("modify_sketch", operations=[{"op": "dimension", "items": ["seg:0"], "orientation": "vertical"}])
            await c.call("finish_sketch")
            await c.call("set_dimensions", values={"D1@3DSketch1": 100})
            segs = (await c.call("list_sketch", sketch="3DSketch1"))["segments"]
            assert segs[2]["end"] == pytest.approx([100, 25, 60])
            # a 3D arc lists its own ends (its curve is a whole circle) and takes a radius
            await c.call("start_sketch", on="3d")
            await c.call("sketch", entities=[{"type": "arc", "start": [0, 0, 0], "mid": [10, 10, 5], "end": [20, 0, 10]}])
            arc = (await c.call("list_sketch"))["segments"][0]
            assert arc["start"] == pytest.approx([0, 0, 0], abs=1e-4) and arc["end"] == pytest.approx([20, 0, 10], abs=1e-4)
            assert (await c.call("modify_sketch", operations=[{"op": "dimension", "items": ["seg:0"], "value": 20}]))["done"][0]["value"] == 20
            await c.call("finish_sketch")
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_edit_reference_planes(tmp_path: Path):
    """An offset plane re-based on a datum face and flipped, and an angled plane re-angled; a boss on the plane follows."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            await c.call("ref_plane", mode="offset", refs=["plane:Top"], distance=20)
            await c.call("ref_plane", mode="angle", refs=["plane:Front", "edge@0,10,25"], angle=30)
            await c.call("start_sketch", on="plane:Plane1")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 10}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=5)
            boss = math.pi * 25 * 5
            out = await c.call("edit_feature", feature="Plane1", refs=["face@0,10,0"], distance=5)
            assert out["plane"]["origin_mm"][1] == pytest.approx(15) and out["changed"] == ["refs", "distance"]
            assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(40000 + boss)
            out = await c.call("edit_feature", feature="Plane1", reverse=True)  # into the block: the boss is swallowed
            assert out["plane"]["origin_mm"][1] == pytest.approx(5)
            assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(40000)
            out = await c.call("edit_feature", feature="Plane2", angle=45)
            assert [abs(v) for v in out["plane"]["normal"]] == pytest.approx([0, math.sqrt(0.5), math.sqrt(0.5)])
            assert "not an offset plane" in await c.fails("edit_feature", feature="Plane2", distance=4)
            assert "1 reference" in await c.fails("edit_feature", feature="Plane1", refs=["plane:Top", "plane:Front"])
            # an axis made from two planes moved onto a hole's cylinder
            await c.call("hole", size="Ø6.6", fastener="drill", positions=[[20, 10, 0]])
            await c.call("ref_axis", refs=["plane:Front", "plane:Right"])
            out = await c.call("edit_feature", feature="Axis1", refs=["face@23.3,5,0"])
            ax = out["axis"]
            assert [ax["start_mm"][0], ax["start_mm"][2], ax["end_mm"][0], ax["end_mm"][2]] == pytest.approx([20, 0, 20, 0], abs=1e-6)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_sheet_format_and_table_placement(tmp_path: Path):
    """An ISO title block on the sheet, and tables without a position placed clear of views, each other and the title block."""

    def clear(a, b):
        return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-80, -50], "corner2": [80, 50]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=30)
        await c.call("save_document", path="plate.sldprt")
        try:
            d = await c.call("create_drawing", sheet="A3", sheet_format="iso", projection="first")
            assert d["sheet_format"] == "a3 - iso.slddrt"
            tb = d["title_block_mm"]
            sec = await c.call("section_view", view=d["views"][1]["name"], direction="horizontal")
            o, area = sec["outline_mm"], d["drawing_area_mm"]
            assert area[0] <= o[0] and o[2] <= area[2] and area[1] <= o[1] and o[3] <= area[3], (o, area)
            assert o[2] <= tb[0] or tb[2] <= o[0] or o[3] <= tb[1] or tb[3] <= o[1], (o, tb)
            d["views"] = (await c.call("drawing_views"))["views"]
            assert tb[0] > 200 and tb[1] < 30 and tb[2] <= 420 and tb[3] < 120  # bottom right of the A3 sheet
            boxes = [v["outline_mm"] for v in d["views"]] + [tb]
            for kw in ({"kind": "general", "cells": [["A", "B"]] + [[str(i), "x" * 12] for i in range(12)]},
                       {"kind": "revision", "revisions": [{"rev": "A", "description": "INITIAL RELEASE"}]},
                       {"kind": "general", "cells": [["P", "Q"], ["1", "2"]]}):
                b = (await c.call("drawing_table", **kw))["box_mm"]
                assert 0 < b[0] and b[2] < 420 and 0 < b[1] and b[3] < 297
                assert all(clear(b, o) for o in boxes), (kw["kind"], b, boxes)
                boxes.append(b)
            _, images = await c.call("screenshot")
            import base64
            import io

            from PIL import Image as PILImage

            im = PILImage.open(io.BytesIO(base64.b64decode(images[0].data))).convert("L")
            w, h = im.size
            edges = [im.crop(b) for b in ((0, 0, 3, h), (w - 3, 0, w, h), (0, 0, w, 3), (0, h - 3, w, h))]
            assert all(e.getextrema()[0] > 200 for e in edges)  # the whole sheet is framed, nothing cut at the edges
            assert "must be one of" in await c.fails("create_drawing", model="plate.sldprt", sheet="A3", sheet_format="nasa")
            assert "does not exist" in await c.fails("sheets", action="add", name="Two", sheet_format="missing.slddrt")
            two = await c.call("sheets", action="add", name="Two", size="A4V", sheet_format="iso")
            assert two["active"] == "Two" and two["sheet_mm"] == [210.0, 297.0]
        finally:
            await c.call("close_document")
            await c.call("close_document", name="plate.SLDPRT")

    run(tmp_path, body)


def test_explode_along_references(tmp_path: Path):
    """Explode steps along a slanted edge and square to a face, checked against the measured movement."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "polyline", "closed": True, "points": [[0, 0], [40, 0], [0, 30]]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=5)
        await c.call("save_document", path="wedge.sldprt")
        await c.call("close_document")
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-2, -2], "corner2": [2, 2]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=4)
        await c.call("save_document", path="pin.sldprt")
        await c.call("close_document")
        await c.call("new_document", kind="assembly")
        try:
            await c.call("insert_component", path="wedge.sldprt", position=[0, 0, 0])
            await c.call("insert_component", path="pin.sldprt", position=[0, 20, 0])
            await c.call("insert_component", path="pin.sldprt", position=[30, 20, 0])
            edge = next(e for e in (await c.call("list_edges", component="wedge-1"))["edges"] if e["length"] == pytest.approx(50) and e["mid"][1] > 4)
            out = await c.call("exploded_view", steps=[
                {"components": ["pin-1"], "direction": f"edge@wedge-1/{','.join(str(v) for v in edge['mid'])}", "distance": 50},
                {"components": ["pin-2"], "direction": "face@wedge-1/10,5,-10", "distance": -15},
                {"components": ["pin-2"], "direction": "x", "distance": 10}])
            d = [b - a for a, b in zip(edge["start"], edge["end"])]
            n = math.sqrt(sum(v * v for v in d))
            assert out["moved_mm"]["pin-1"] == pytest.approx([50 * v / n for v in d], abs=1e-3)  # along the edge, start to end
            assert out["moved_mm"]["pin-2"] == pytest.approx([10, -15, 0], abs=1e-3)  # into the top face's normal, then along x
            assert "only moves" in await c.fails("exploded_view", steps=[{"components": ["pin-1"], "direction": "face@wedge-1/10,5,-10", "distance": 5, "rotate": 30}])
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_assembly_drawing_references(tmp_path: Path):
    """Reference dimensions between components and auxiliary views in an assembly drawing, kept clear of other views."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "polyline", "closed": True, "points": [[0, 0], [60, 0], [60, 20], [30, 40], [0, 40]]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=10)
        await c.call("save_document", path="bk.sldprt")
        await c.call("close_document")
        await c.call("new_document", kind="assembly")
        await c.call("insert_component", path="bk.sldprt", position=[0, 0, 0])
        await c.call("insert_component", path="bk.sldprt", position=[100, 0, 0])
        await c.call("save_document", path="pair.sldasm")
        try:
            d = await c.call("create_drawing", sheet="A3", isometric=False)
            front, top = d["views"][0]["name"], d["views"][1]["name"]
            span = ["edge@bk-1/0,10,-20", "edge@bk-2/160,10,-10"]
            # edges seen end-on in the front view cannot be dimensioned there; the failure must not break the next dimension
            assert "another view" in await c.fails("drawing_dimension", view=front, refs=span, orientation="horizontal")
            assert (await c.call("drawing_dimension", view=top, refs=span, orientation="horizontal"))["value"] == pytest.approx(160)
            assert (await c.call("drawing_dimension", view=top, refs=["edge@bk-1/60,10,-10", "edge@bk-2/100,10,-20"], orientation="horizontal"))["value"] == pytest.approx(40)
            assert (await c.call("drawing_dimension", view=front, refs=["edge@bk-2/130,10,0"]))["value"] == pytest.approx(60)
            await c.call("auxiliary_view", view=top, edge="edge@bk-2/145,10,-30", label="C")  # square to the chamfered face
            await c.call("auxiliary_view", view=front, edge="edge@bk-2/130,10,0", label="D")
            r = await c.call("drawing_views")
            boxes = [v["outline_mm"] for v in r["views"]]
            assert len(boxes) == 5
            assert not any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3] for i, a in enumerate(boxes) for b in boxes[:i])
            assert all(0 <= b[0] and b[2] <= r["sheet_mm"][0] and 0 <= b[1] and b[3] <= r["sheet_mm"][1] for b in boxes)
        finally:
            await c.call("close_document")
            await c.call("close_document", name="pair.SLDASM")

    run(tmp_path, body)


def test_variable_fillet_along_chain(tmp_path: Path):
    """A variable fillet along a line-arc-line chain with its own radius at each vertex, edges given out of order."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [60, 40]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=20)
            await c.call("fillet", radius=15, edges=["edge@60,10,0"])
            chain = ["edge@60,20,-27.5", "edge@22.5,20,0", "edge@55.6066,20,-4.3934"]
            assert "no radius for the chain vertex" in await c.fails("fillet", kind="variable", edges=chain, vertex_radii=[{"vertex": [0, 20, 0], "radius": 2}])
            out = await c.call("fillet", kind="variable", edges=chain, vertex_radii=[
                {"vertex": [60, 20, -40], "radius": 6}, {"vertex": [0, 20, 0], "radius": 2},
                {"vertex": [45, 20, 0], "radius": 3}, {"vertex": [60, 20, -15], "radius": 5}])
            assert len(out["vertex_radii"]) == 4
            top = [e["start"] for e in (await c.call("list_edges", limit=60))["edges"] if abs(e["start"][1] - 20) < 1e-6 and abs(e["end"][1] - 20) < 1e-6]
            # the top face now starts the vertex's radius in from each original vertex
            for p in ([0, 20, -2], [45, 20, -3], [55, 20, -15], [54, 20, -40]):
                assert any(q == pytest.approx(p, abs=1e-3) for q in top), (p, top)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_miter_flange_with_lip(tmp_path: Path):
    """Tray rims with a return lip: outward (a Z section) widens the tray by the lip, inward stays inside the blank."""

    async def body(c: Session):
        for lip, x_max, z_max in ((8, 58, 38), (-8, 50, 30)):
            await c.call("new_document", kind="part")
            try:
                await c.call("start_sketch", on="plane:Top")
                await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
                await c.call("finish_sketch")
                await c.call("sheet_metal_base", thickness=2, bend_radius=2)
                await c.call("miter_flange", edges=["edge@0,0,-30", "edge@50,0,0", "edge@0,0,30"], length=20, lip=lip)
                box = await c.call("bounding_box")
                assert box["max"][0] == pytest.approx(x_max) and box["max"][2] == pytest.approx(z_max) and box["min"][0] == pytest.approx(-50)
                assert (await c.call("flat_pattern"))["bends"] == 6  # three flange bends and three lip bends
            finally:
                await c.call("close_document")

    run(tmp_path, body)


def test_custom_forming_tool(tmp_path: Path):
    """A pierced boss modelled as a part, made into a forming tool, and formed into a sheet."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-20, -20], "corner2": [20, 20]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=5)
        await c.call("start_sketch", on="face@0,5,0")
        await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 16}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=3)
        assert "planar face" in await c.fails("create_forming_tool", stopping_face="face@8,6.5,0", save_as="bad.sldftp")
        made = await c.call("create_forming_tool", stopping_face="face@15,5,15", remove_faces=["face@0,8,0"], save_as="flanged_hole.sldftp")
        assert Path(made["file"]).exists()
        await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1, bend_radius=1)
            out = await c.call("forming_tool", tool_name="flanged_hole.sldftp", position=[10, 0, 0])
            assert out["type"] == "FormToolInstance"
            holes = [f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole")]
            assert holes and holes[0]["axis_point"][0] == pytest.approx(10) and holes[0]["radius"] == pytest.approx(8)
            assert (await c.call("bounding_box"))["size"][1] == pytest.approx(3)  # a 3 mm wall: the pierced end has no cap
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_drawing_gdt_shown_once_and_clear(tmp_path: Path):
    """Datums and feature control frames appear once each on the drawing, off the views, inside the border."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-60, -40], "corner2": [60, 40]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=12)
        await c.call("hole", size="Ø6.6", fastener="drill", positions=[[-40, 12, -20], [40, 12, -20], [40, 12, 20], [-40, 12, 20]])
        for face, label in (("face@0,0,0", "A"), ("face@-60,6,0", "B"), ("face@0,6,40", "C")):
            await c.call("add_datum", face=face, label=label)
        await c.call("add_geometric_tolerance", face="face@0,12,0", characteristic="flatness", tolerance=0.05)
        await c.call("add_geometric_tolerance", face="face@-36.7,6,-20", characteristic="position", tolerance=0.1, datums=["A", "B", "C"], diameter_zone=True)
        await c.call("save_document", path="datum_plate.sldprt")
        try:
            d = await c.call("create_drawing", sheet="A3", sheet_format="iso")
            ann = d["annotations"]
            assert ann["gdt_annotations"] == 5  # three datums, two frames: one of each on the sheet
            assert ann["duplicates_removed"] > 0 and ann["clear"] is True
        finally:
            await c.call("close_document")
            await c.call("close_document", name="datum_plate.SLDPRT")

    run(tmp_path, body)


def test_mould_split_stepped_parting(tmp_path: Path):
    """Mould halves split along a stepped parting surface, checked against hand-computed volumes."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-30, -20], "corner2": [30, 20]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=20)
            await c.call("move_body", bodies=["body:Boss-Extrude1"], translate=[0, -10, 0])  # 60 x 20 x 40 part centred on Top
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "polyline", "points": [[-80, 0], [0, 0], [0, 5], [80, 5]]}])
            await c.call("finish_sketch")
            await c.call("surface", kind="extrude", depth=100)
            await c.call("move_body", bodies=["body:Surface-Extrude1"], translate=[0, 0, -50])
            bodies = await c.call("list_bodies")
            part, srf = bodies["bodies"][0]["name"], bodies["surfaces"][0]["name"]
            assert "not a surface body" in await c.fails("mould_split", parting_plane="plane:Top", parting_surface=f"body:{part}", part=f"body:{part}")
            assert len((await c.call("list_bodies"))["bodies"]) == 1  # nothing built
            out = await c.call("mould_split", parting_plane="plane:Top", parting_surface=f"body:{srf}", part=f"body:{part}")
            # block 100 x 80 x 60; parting at y = 0 for x < 0 and y = 5 for x > 0
            assert out["block_mm3"] == pytest.approx(480000)
            assert out["cavity"]["volume_mm3"] == pytest.approx(120000 + 100000 - 12000 - 6000)
            assert out["core"]["volume_mm3"] == pytest.approx(480000 - 48000 - 202000)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_drawing_radial_dimensions_arranged(tmp_path: Path):
    """A diameter dimension dragged onto its view is moved clear again by view_dimensions(arrange=True)."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-60, -40], "corner2": [60, 40]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=12)
        await c.call("start_sketch", on="face@0,12,0")
        await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 30}])
        await c.call("modify_sketch", operations=[{"op": "dimension", "items": ["seg:0"], "value": 30}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=8)
        await c.call("save_document", path="boss_plate.sldprt")
        try:
            d = await c.call("create_drawing", sheet="A3", sheet_format="iso", isometric=False)
            top = d["views"][1]["name"]
            dims = (await c.call("view_dimensions", view=top))
            dia = next(x for x in dims["dimensions"] if x["value"] == pytest.approx(30))
            o = dims["outline_mm"]
            await c.call("view_dimensions", view=top, move={dia["name"]: [(o[0] + o[2]) / 2 + 20, (o[1] + o[3]) / 2]})
            out = await c.call("view_dimensions", view=top, arrange=True)
            assert out["annotations"]["moved"] >= 1 and out["annotations"]["clear"] is True
            x, y = next(x for x in out["dimensions"] if x["name"] == dia["name"])["position_mm"]
            assert not (o[0] < x < o[2] and o[1] < y < o[3])  # the text is off the view
        finally:
            await c.call("close_document")
            await c.call("close_document", name="boss_plate.SLDPRT")

    run(tmp_path, body)


def test_engraved_text_on_arc(tmp_path: Path):
    """Part number and serial engraved round a disc, clockwise over the top and counter-clockwise under the bottom."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 80}])
            await c.call("finish_sketch")
            v0 = (await c.call("extrude", depth=6))["volume_mm3"]
            await c.call("start_sketch", on="face@0,0,6")
            assert "does not fit" in await c.fails("sketch", entities=[{"type": "text", "text": "X" * 60, "height": 5, "arc": {"center": [0, 0], "radius": 5}}])
            out = await c.call("sketch", entities=[
                {"type": "text", "text": "PN 4471-201 REV B", "height": 4, "arc": {"center": [0, 0], "radius": 30}},
                {"type": "text", "text": "S/N 0042", "height": 4, "arc": {"center": [0, 0], "radius": 30, "direction": "ccw"}}])
            assert [a["segments"] for a in out["added"]] == [14, 7]  # one text entity per character, spaces skipped
            await c.call("finish_sketch")
            cut = await c.call("extrude", cut=True, depth=0.3)
            assert cut["type"] == "ICE" and cut["volume_mm3"] < v0
            box = await c.call("bounding_box")
            assert box["size"][0] == pytest.approx(80)  # the engraving stays inside the disc
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_release_workflow(tmp_path: Path):
    """Release A then B of a part and its drawing: stamped, issued, snapshotted and logged with checksums."""
    import csv
    import hashlib

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-40, -25], "corner2": [40, 25]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=10)
        await c.call("custom_properties", set={"PartNo": "BRK-1001", "Description": "BRACKET"})
        await c.call("save_document", path="bracket.sldprt")
        try:
            dwg = await c.call("create_drawing", sheet="A3", sheet_format="iso", save_as="bracket.slddrw")
            assert dwg["drawing_number_from"] == "PartNo"  # the title block's drawing number is the part number
            a = await c.call("release", revision="A", description="INITIAL RELEASE", approved_by="QA")
            assert a["revision_table"] == "created" and a["previous"] is None
            assert sorted(f["file"] for f in a["files"]) == ["BRK-1001_A.pdf", "BRK-1001_A.step", "bracket.SLDDRW", "bracket.SLDPRT"]
            assert "already released" in await c.fails("release", revision="A", description="again", approved_by="QA")
            assert "sheet metal" in await c.fails("release", revision="B", description="x", approved_by="QA", exports=["flat_dxf"])
            assert "does not come after" in await c.fails("release", revision="0", description="older", approved_by="QA")
            b = await c.call("release", revision="B", description="ECO-1042 HOLES ADDED", approved_by="QA")
            assert b["revision_table"] == "added" and b["history"] == ["A", "B"]
            folder = Path(b["folder"])
            for f in b["files"]:  # the logged checksums are those of the issued files
                assert hashlib.sha256((folder / f["file"]).read_bytes()).hexdigest() == f["sha256"]
            with open(b["log"], newline="", encoding="utf-8") as fh:
                assert sorted({r["revision"] for r in csv.DictReader(fh)}) == ["A", "B"]
            props = (await c.call("custom_properties"))["properties"]
            assert props["Revision"] == "B" and props["ApprovedBy"] == "QA"
        finally:
            await c.call("close_document")
            await c.call("close_document", name="bracket.SLDPRT")

    run(tmp_path, body)


def test_scaled_views_inside_border(tmp_path: Path):
    """At a set 1:1 scale the views are kept inside the ISO border and off the title block."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-100, -50], "corner2": [100, 50]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=60)
        await c.call("save_document", path="slab.sldprt")
        try:
            d = await c.call("create_drawing", sheet="A3", sheet_format="iso", scale="1:1")
            area, tb = d["drawing_area_mm"], d["title_block_mm"]
            assert area[0] > 15  # the ISO binding margin, not the sheet edge
            for v in d["views"]:
                o = v["outline_mm"]
                assert area[0] - 0.1 <= o[0] and o[2] <= area[2] + 0.1 and area[1] - 0.1 <= o[1] and o[3] <= area[3] + 0.1, (o, area)
                assert o[2] <= tb[0] or tb[2] <= o[0] or o[3] <= tb[1] or tb[3] <= o[1], (o, tb)
            assert "warning" not in d
            # an extra view without a position goes into clear space inside the border
            extra = await c.call("add_view", orientation="bottom", scale="1:4")
            o = extra["outline_mm"]
            others = [v["outline_mm"] for v in (await c.call("drawing_views"))["views"] if v["name"] != extra["view"]] + [tb]
            assert all(o[2] <= b[0] or b[2] <= o[0] or o[3] <= b[1] or b[3] <= o[1] for b in others), (o, others)
            assert area[0] <= o[0] and o[2] <= area[2] and area[1] <= o[1] and o[3] <= area[3]
        finally:
            await c.call("close_document")
            await c.call("close_document", name="slab.SLDPRT")

    run(tmp_path, body)


def test_tapped_hole_from_table_name(tmp_path: Path):
    """fastener='tapped' alone makes a tapped hole (tap drill bore), and a tapped hole refuses a clearance table."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-30, -20], "corner2": [30, 20]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=10)
            assert "needs a tapped table" in await c.fails("hole", size="M5", kind="tapped", fastener="clearance", positions=[[0, 10, 0]])
            out = await c.call("hole", size="M5", fastener="tapped", positions=[[0, 10, 0]])
            assert out["holes"] == 1
            bores = [f["radius"] for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole")]
            assert min(bores) == pytest.approx(2.1, abs=0.01)  # the M5 tap drill is 4.2 mm, not the 5.5 mm clearance
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_bom_shows_part_numbers(tmp_path: Path):
    """A drawing BOM lists the parts' PartNo property, not their file names."""

    async def body(c: Session):
        for name, pn, size in (("plate_a", "PLT-100", 60), ("lug_b", "LUG-200", 20)):
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-size / 2, -size / 2], "corner2": [size / 2, size / 2]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=5)
            await c.call("custom_properties", set={"PartNo": pn, "Description": name.upper()})
            await c.call("save_document", path=f"{name}.sldprt")
            await c.call("close_document")
        await c.call("new_document", kind="assembly")
        await c.call("insert_component", path="plate_a.sldprt", position=[0, 0, 0], fix=True)
        await c.call("insert_component", path="lug_b.sldprt", position=[0, 5, 0])
        await c.call("save_document", path="pn_asm.sldasm")
        stl = await c.call("export", path="pn_asm.stl")  # the whole assembly in one file (for printing a mock-up)
        assert Path(stl["file"]).exists() and stl["bytes"] > 1000
        try:
            d = await c.call("create_drawing", sheet="A3", isometric=False)
            bom = await c.call("drawing_table", kind="bom", view=d["views"][0]["name"], balloons=False)
            assert bom["part_number_from"] == "PartNo"
            assert sorted(row[1] for row in bom["rows"][1:]) == ["LUG-200", "PLT-100"]
            assert bom["rows"][0][1].upper() == "PART NUMBER"
            ballooned = await c.call("drawing_table", kind="bom", view=d["views"][1]["name"], position=[300, 200])
            assert ballooned["balloons"] == 2 and ballooned["annotations"]["clear"] is True  # balloons kept off views and dimensions
        finally:
            await c.call("close_document")
            await c.call("close_document", name="pn_asm.SLDASM")

    run(tmp_path, body)


def test_consumed_sketch_not_reused(tmp_path: Path):
    """After a failed sketch, a feature does not silently reuse an older sketch that another feature consumed."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-20, -20], "corner2": [20, 20]}])
            await c.call("finish_sketch")
            v = (await c.call("extrude", depth=10))["volume_mm3"]
            await c.call("start_sketch", on="face@0,10,0")
            await c.fails("sketch", entities=[{"type": "slot", "center1": [0, 0], "center2": [10, 0], "width": 4}])  # wrong keys
            await c.call("finish_sketch")
            assert "already used by Boss-Extrude1" in await c.fails("extrude", cut=True, depth=2)
            assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(v)  # nothing was cut
            assert (await c.call("extrude", sketch="Sketch1", depth=5, reverse=True))["volume_mm3"] == pytest.approx(v * 1.5)
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_delete_whole_hole_from_one_face(tmp_path: Path):
    """On an imported part one bore face removes a whole counterbored hole; a boss's top stays when its hole goes."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-50, -30], "corner2": [50, 30]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=15)
        await c.call("hole", size="M6", kind="counterbore", positions=[[-35, 15, -15], [35, 15, 15]])
        await c.call("export", path="cb_block.step", step_ap="214")
        await c.call("close_document")
        await c.call("import_file", path="cb_block.step")
        try:
            small = [f for f in (await c.call("list_faces", type="cylinder"))["faces"]
                     if f.get("hole") and f["radius"] == pytest.approx(3.3) and f["axis_point"][0] == pytest.approx(35)]
            out = await c.call("delete_faces", faces=[f"face:{small[0]['i']}"])
            cb_depth = 6.4  # ISO counterbore for M6: 11 diameter, 6.4 deep, then the 6.6 bore
            assert out["volume_change_mm3"] == pytest.approx(math.pi * (5.5**2 * cb_depth + 3.3**2 * (15 - cb_depth)), rel=1e-4)
            left = [f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole")]
            assert all(f["axis_point"][0] == pytest.approx(-35) for f in left)  # only the other hole remains
        finally:
            await c.call("close_document")
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-30, -30], "corner2": [30, 30]}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=5)
            await c.call("start_sketch", on="face@0,5,0")
            await c.call("sketch", entities=[{"type": "circle", "center": [0, 0], "diameter": 20}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=8)
            await c.call("hole", size="Ø6.6", fastener="drill", positions=[[0, 13, 0]])
            bore = next(f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole"))
            out = await c.call("delete_faces", faces=[f"face:{bore['i']}"])
            assert out["volume_change_mm3"] == pytest.approx(math.pi * 3.3**2 * 13, rel=1e-4)  # the boss is untouched
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_motion_study_with_limit_mate(tmp_path: Path):
    """A hinged surface driven past a +/-20 deg limit mate: the study still moves it and flags the limit, which is back on afterwards."""

    async def body(c: Session):
        for name, rect in (("spar", ([-40, -10], [8, 10])), ("flap", ([-8, -5], [60, 5]))):
            await c.call("new_document", kind="part")
            await c.call("start_sketch", on="plane:Front")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": rect[0], "corner2": rect[1]}, {"type": "circle", "center": [0, 0], "diameter": 6}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=20)
            await c.call("save_document", path=f"{name}.sldprt")
            await c.call("close_document")
        await c.call("new_document", kind="assembly")
        try:
            await c.call("insert_component", path="spar.sldprt", position=[0, 0, 0], fix=True)
            await c.call("insert_component", path="flap.sldprt", position=[0, 0, 25])
            await c.call("add_mate", type="concentric", refs=["face@spar-1/3,0,10", "face@flap-1/3,0,35"])
            await c.call("add_mate", type="coincident", refs=["face@spar-1/-20,0,20", "face@flap-1/30,0,25"])
            await c.call("add_mate", type="limit_angle", refs=["face@flap-1/30,5,35", "face@spar-1/-20,10,10"], minimum=-20, maximum=20)
            out = await c.call("motion_study", motors=[{"face": "face@flap-1/3,0,35", "kind": "rotary", "rpm": 10}], duration=1.0, times=[0.25, 0.5])
            turned = [r["components"]["flap-1"]["turned_deg"] for r in out["results"]]
            assert turned == pytest.approx([15, 30], abs=0.01)  # 60 deg/s
            lim = out["limit_mates_off"][0]
            assert (lim["minimum"], lim["maximum"]) == pytest.approx((-20, 20)) and lim["exceeded_at_s"] == 0.5
            # the limit mate is on again: a second study finds it active and switches it off once more
            again = await c.call("motion_study", motors=[{"face": "face@flap-1/3,0,35", "kind": "rotary", "rpm": 10}], duration=0.25)
            assert [m["mate"] for m in again["limit_mates_off"]] == [lim["mate"]]
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_box_flanges_in_one_call(tmp_path: Path):
    """Four flanges round a blank in one call come out clean (no overlap at the corners), and the corners can be closed."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-80, -50], "corner2": [80, 50]}])
            await c.call("finish_sketch")
            await c.call("sheet_metal_base", thickness=1.6, bend_radius=1.6)
            out = await c.call("edge_flange", edges=["edge@0,0,-50", "edge@0,0,50", "edge@80,0,0", "edge@-80,0,0"], length=20)
            assert out["flanges"] == 4 and len(out["features"]) == 2
            assert (await c.call("check_geometry"))["ok"] is True
            ends = [f for f in (await c.call("list_faces", type="plane", limit=80))["faces"]
                    if f.get("area_mm2", 0) == pytest.approx(1.6 * 20, abs=8) and abs(f["point"][0]) == pytest.approx(80)]
            await c.call("closed_corner", faces=["face@{},{},{}".format(*f["point"]) for f in ends])
            assert (await c.call("check_geometry"))["ok"] is True
            assert (await c.call("flat_pattern"))["bends"] == 4
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_import_round_trip_formats(tmp_path: Path):
    """A part exported to each neutral format imports back as one solid of the same volume (STL and 3MF as solids)."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-20, -10], "corner2": [20, 10]}])
        await c.call("finish_sketch")
        v = (await c.call("extrude", depth=10))["volume_mm3"]
        for ext in ("step", "igs", "x_t", "x_b", "sat", "stl", "3mf"):
            await c.call("export", path=f"rt.{ext}")
        await c.call("close_document")
        for ext in ("step", "igs", "x_t", "x_b", "sat", "stl", "3mf"):
            imp = await c.call("import_file", path=f"rt.{ext}")
            try:
                assert imp["solid_bodies"] == 1, ext
                # meshes are faceted, so their volume is close rather than exact (a box meshes exactly)
                assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(v, rel=1e-3), ext
            finally:
                await c.call("close_document")

    run(tmp_path, body)


def test_equations_set_by_name(tmp_path: Path):
    """Global variables drive a plate; changing one by name drives the model, and a deleted variable is flagged."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        try:
            await c.call("start_sketch", on="plane:Top")
            await c.call("sketch", entities=[{"type": "rectangle", "corner1": [0, 0], "corner2": [100, 40]}])
            await c.call("modify_sketch", operations=[{"op": "dimension", "items": ["seg@50,0"], "value": 100}, {"op": "dimension", "items": ["seg@100,20"], "value": 40}])
            await c.call("finish_sketch")
            await c.call("extrude", depth=8)
            await c.call("equations", set={"Length": "150mm", "Width": '"Length" / 3', "D1@Sketch1": '"Length"', "D2@Sketch1": '"Width"',
                                           "D1@Boss-Extrude1": '"Length" * 0.05'})
            out = await c.call("equations", set={"Length": "200mm"})  # replaced, not added
            assert sum(e["equation"].startswith('"Length"') for e in out["equations"]) == 1 and "warning" not in out
            assert (await c.call("bounding_box"))["size"] == pytest.approx([200, 10, 200 / 3], abs=1e-3)
            gone = await c.call("equations", set={"Width": ""})
            assert gone["unresolved"] == ['"D2@Sketch1" = "Width"']
        finally:
            await c.call("close_document")

    run(tmp_path, body)


def test_core_features_revolve_shell_draft(tmp_path: Path):
    """Revolve, shell, chamfer, circular pattern, cosmetic thread, draft, notes and surface finish, against hand calculations;
    then open and activate documents."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Front")
        await c.call("sketch", entities=[{"type": "centerline", "from": [0, -5], "to": [0, 50]}, {"type": "rectangle", "corner1": [0, 0], "corner2": [20, 40]}])
        await c.call("finish_sketch")
        cyl = await c.call("revolve", angle=360)
        assert cyl["volume_mm3"] == pytest.approx(math.pi * 20**2 * 40, rel=1e-6)
        cup = await c.call("shell", thickness=2, remove_faces=["face@0,40,10"])  # open at the top
        assert cup["volume_mm3"] == pytest.approx(math.pi * (20**2 * 40 - 18**2 * 38), rel=1e-6)
        ch = await c.call("chamfer", distance=1, edges=["edge@20,0,0"])  # bottom outer edge: a 1 x 1 triangle swept round
        assert cup["volume_mm3"] - ch["volume_mm3"] == pytest.approx(0.5 * 2 * math.pi * (20 - 1 / 3), rel=1e-4)
        await c.call("ref_axis", refs=["plane:Front", "plane:Right"])
        # Ø4 on a curved wall: SolidWorks offers Ø0.4 instead, which must be refused rather than drilled
        assert "offered" in await c.fails("hole", size="Ø4", fastener="drill", positions=[[20, 20, 0]])
        seed = await c.call("hole", size="Ø5", fastener="drill", positions=[[20, 20, 0]])  # radial, through one wall
        await c.call("circular_pattern", features=[seed["feature"]], axis="axis:Axis1", count=6)
        holes = [f for f in (await c.call("list_faces", type="cylinder"))["faces"] if f.get("hole") and f["radius"] == pytest.approx(2.5)]
        assert len(holes) == 6
        thread = await c.call("cosmetic_thread", edge="edge@0,40,18", note="M40x1.5-6H", diameter=36.4, depth=10)
        assert thread["type"] == "CosmeticThread"
        await c.call("add_surface_finish", face="face@0,0,10", roughness="Ra 1.6")
        await c.call("add_note", text="BREAK SHARP EDGES 0.2-0.5", position=[0, 50, 0])
        await c.call("save_document", path="cup.sldprt")
        await c.call("close_document")
        # a drafted block: four sides drafted 5 deg from the base, a frustum
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-20, -20], "corner2": [20, 20]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=20)
        out = await c.call("draft", angle=5, neutral="face@0,0,0", faces=["face@20,10,0", "face@-20,10,0", "face@0,10,20", "face@0,10,-20"])
        top = 40 - 2 * 20 * math.tan(math.radians(5))
        frustum = 20 / 3 * (40**2 + top**2 + 40 * top)
        assert out["volume_mm3"] == pytest.approx(frustum, rel=1e-4) or out["volume_mm3"] == pytest.approx(
            20 / 3 * (40**2 + (80 - top) ** 2 + 40 * (80 - top)), rel=1e-4)  # in or out, depending on the pull direction
        await c.call("save_document", path="drafted.sldprt")
        # documents: open the cup again (it is closed) and switch between the two
        opened = await c.call("open_document", path="cup.sldprt")
        assert opened["kind"] == "part" and opened["title"].lower().startswith("cup")
        back = await c.call("activate_document", name="drafted.SLDPRT")
        assert back["title"].lower().startswith("drafted")
        d = await c.call("create_drawing", model="cup.sldprt", sheet="A3")
        assert d["views"]
        await c.call("add_note", text="UNLESS OTHERWISE SPECIFIED: DIMENSIONS IN MM", position=[30, 40])
        await c.call("close_document")
        for name in ("cup.SLDPRT", "drafted.SLDPRT"):
            await c.call("close_document", name=name)

    run(tmp_path, body)


def test_assembly_configuration_suppresses_component(tmp_path: Path):
    """A build variant without one component: suppressed in that configuration only, with BOM and mass to match."""

    async def body(c: Session):
        await c.call("new_document", kind="part")
        await c.call("start_sketch", on="plane:Top")
        await c.call("sketch", entities=[{"type": "rectangle", "corner1": [-2, -2], "corner2": [2, 2]}])
        await c.call("finish_sketch")
        await c.call("extrude", depth=4)
        await c.call("save_document", path="lug.sldprt")
        await c.call("close_document")
        await c.call("new_document", kind="assembly")
        try:
            for p in ([0, 0, 0], [20, 0, 0], [40, 0, 0]):
                await c.call("insert_component", path="lug.sldprt", position=p)
            await c.call("configurations", action="add", name="Light", description="Two lugs")
            await c.call("feature_action", features=["lug-3"], action="suppress", configurations=["Light"])
            assert [line["qty"] for line in (await c.call("bom"))["lines"]] == [2]
            assert (await c.call("mass_properties"))["volume_mm3"] == pytest.approx(2 * 64)
            await c.call("configurations", action="activate", name="Default")
            assert [line["qty"] for line in (await c.call("bom"))["lines"]] == [3]  # the other configuration keeps it
        finally:
            await c.call("close_document")

    run(tmp_path, body)

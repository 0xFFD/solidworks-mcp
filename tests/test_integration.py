"""End-to-end tests against a real SolidWorks session, through the MCP protocol over stdio.

Skipped unless SolidWorks is installed. They create, save and close their own documents and never
touch documents that were already open.
"""

from __future__ import annotations

import asyncio
import json
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
            await c.call("set_dimensions", values={dims["done"][2]["dimension"]: 4})  # sketch stays open
            assert (await c.call("finish_sketch"))["closed_regions"] >= 1

            # redefine a cut: blind -> through all -> blind
            await c.call("start_sketch", on="face@0,5,0")
            await c.call("sketch", entities=[{"type": "circle", "center": [-12, 0], "diameter": 4}])
            await c.call("finish_sketch")
            cut = await c.call("extrude", cut=True, depth=2)
            through = await c.call("edit_feature", feature=cut["feature"], end="through_all")
            assert through["volume_mm3"] == pytest.approx(4000 - 3.14159265 * 4 * 5, abs=0.01)
            two = await c.call("edit_feature", feature="Boss-Extrude1", depth2=4)
            assert two["volume_mm3"] == pytest.approx(through["volume_mm3"] + 800 * 4 - 3.14159265 * 4 * 4, abs=0.5)

            # configurations: a variant without the hole
            await c.call("configurations", action="add", name="Blank")
            await c.call("feature_action", features=[cut["feature"]], action="suppress", configurations=["Blank"])
            blank = (await c.call("mass_properties"))["volume_mm3"]
            await c.call("configurations", action="activate", name="Default")
            assert blank > (await c.call("mass_properties"))["volume_mm3"]
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
            await c.call("sketch", entities=[{"type": "polyline", "points": [[40, 0, 0], [40, 50, 0], [70, 50, 20]]}])
            assert (await c.call("finish_sketch"))["3d"] is True
            await c.call("sweep", path="3DSketch1", circular_diameter=4)
            assert len((await c.call("list_bodies"))["bodies"]) == 2

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
            assert (await c.call("detail_view", view=front, center=[30, 10, 15], radius=8, scale="3:1", label="D"))["scale"] == 3
            right = await c.call("projected_view", view=front, direction="right")
            views = (await c.call("drawing_views"))["views"]
            boxes = [v["outline_mm"] for v in views if v["name"] != right["view"]]
            r = right["outline_mm"]
            assert not any(r[0] < b[2] and b[0] < r[2] and r[1] < b[3] and b[1] < r[3] for b in boxes)
            dim = await c.call("drawing_dimension", view=front, refs=["edge@-40,5,20", "edge@40,5,20"], orientation="horizontal")
            assert dim["value"] == pytest.approx(80)
            holes = await c.call("drawing_table", kind="hole", view=top, face="face@10,10,0", position=[300, 285], anchor="top_left")
            assert len(holes["rows"]) == 6 and holes["rows"][-1][-1].startswith("<MOD-DIAM> 3.30")
            rev = await c.call("drawing_table", kind="revision", revisions=[{"rev": "A", "description": "INITIAL RELEASE", "approved": "QA"}])
            assert rev["rows"][-1][1:3] == ["A", "INITIAL RELEASE"]
            sheets = await c.call("sheets", action="add", name="Flat", size="A4")
            assert sheets["active"] == "Flat"
            await c.call("add_view", orientation="flat_pattern", model="cover.sldprt")
            await c.call("sheets", action="activate", name="Sheet1")
            await c.call("save_document")
            pdf = await c.call("export", path="block.pdf")
            assert pdf["bytes"] > 10000
            await c.call("close_document")

            asm = await c.call("create_drawing", model="assy.sldasm", isometric=False)
            bom = await c.call("drawing_table", kind="bom", view=asm["views"][0]["name"])
            assert bom["balloons"] == 2 and [row[2] for row in bom["rows"][1:]] == ["Mounting block", ""]
            await c.call("close_document")
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
            await c.call("exploded_view", show=False)
            assert (await c.call("interference_check"))["interferences"] == 0
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

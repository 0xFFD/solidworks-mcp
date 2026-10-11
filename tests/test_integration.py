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

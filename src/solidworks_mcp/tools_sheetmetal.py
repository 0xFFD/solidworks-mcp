"""Sheet metal: base flanges, edge flanges, flat patterns and flat-pattern DXF export."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import gauge as gauge_mod
from . import geometry as g
from .com import SW, mm, rad, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import checked_marking
from .tools_features import _dispatch_array, _last_feature, _select_sketch, feature_error, result
from .tools_sketch import SketchFrame, sketch_name

_POSITION = {"material_inside": "swFlangePositionTypeMaterialInside", "material_outside": "swFlangePositionTypeMaterialOutside",
             "bend_outside": "swFlangePositionTypeBendOutside"}


def _sheet_metal_feature(doc: Any) -> Any:
    for f in g.iter_features(doc):
        if f.GetTypeName2() == "SheetMetal":
            return f
    raise SwError("The part has no sheet metal. Start it with sheet_metal_base.")


def _flat_pattern(doc: Any) -> Any:
    for f in g.iter_features(doc):
        if f.GetTypeName2() == "FlatPattern":
            return f
    raise SwError("The part has no flat pattern feature.")


def _is_flat(fp: Any) -> bool:
    sup = fp.IsSuppressed2(SW.C.swThisConfiguration, None)
    return not (sup[0] if isinstance(sup, (tuple, list)) else sup)


def _set_flat(doc: Any, fp: Any, flat: bool) -> None:
    C = SW.C
    fp.SetSuppression2(C.swUnSuppressFeature if flat else C.swSuppressFeature, C.swThisConfiguration, None)
    doc.ForceRebuild3(False)


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


@tool(WRITES)
def sheet_metal_base(
    thickness: Annotated[float | None, Field(gt=0, description="Sheet thickness in mm (e.g. 1.0, 1.6, 2.0 for aluminium 2024/5052/6061 sheet); or give gauge.")] = None,
    bend_radius: Annotated[float | None, Field(gt=0, description="Inside bend radius in mm; for aluminium alloys usually 1-3 x thickness depending on temper.")] = None,
    sketch: Annotated[str | None, Field(description="Sketch with a closed outline (a flat tab) or an open profile (an L, U, Z or hat section); default is the latest sketch.")] = None,
    depth: Annotated[float | None, Field(description="Open profiles: extrusion depth in mm.")] = None,
    midplane: Annotated[bool, Field(description="Open profiles: extrude half the depth each way.")] = False,
    reverse_thickness: Annotated[bool, Field(description="Add the thickness to the other side of the sketch.")] = False,
    gauge: Annotated[str | None, Field(description="Take thickness (and the bend radius if not given) from this gauge of gauge_table, e.g. 'Gauge 14' or '14'.")] = None,
    gauge_table: Annotated[str | None, Field(description="Gauge table: an .xlsx file, or words naming a SolidWorks sample table ('aluminum metric', 'steel').")] = None,
    k_factor: Annotated[float | None, Field(gt=0, lt=1, description="K-factor for the part's bends (0.3-0.5; aluminium alloys often 0.4-0.45); default the gauge table's, else SolidWorks' 0.5.")] = None,
) -> dict:
    """Start a sheet metal part with a base flange from a sketch: a closed outline gives a flat blank, an
    open profile gives a formed section with the bends. Add edge_flange to fold up edges and flat_pattern
    to flatten it and export the blank. k_factor sets the part's K-factor (used by every bend that does not
    set its own); edge_flange can still set its own."""
    doc = SW.active("part")
    C = SW.C
    gauge_row = table = None
    if gauge:
        table = _gauge(gauge_table)
        gauge_row = gauge_mod.find_gauge(table, gauge)
        thickness = gauge_row["thickness_mm"]
        radii = gauge_row["bend_radii_mm"]
        if bend_radius is None:
            bend_radius = min(radii) if radii else thickness
        elif radii and all(abs(bend_radius - r) > 1e-6 for r in radii):
            raise SwError(f"{gauge_row['gauge']} allows bend radii {radii} mm, not {bend_radius}.")
    if not thickness or not bend_radius:
        raise SwError("Give thickness and bend_radius, or a gauge from a gauge_table.")
    name = _select_sketch(doc, sketch)
    fm = doc.FeatureManager
    data = SW.cast(fm.CreateDefinition(C.swFmBaseFlange), "IBaseFlangeFeatureData")
    data.Thickness = mm(thickness)
    data.BendRadius = mm(bend_radius)
    data.ReverseThickness = reverse_thickness
    if depth is not None:
        data.D1EndConditionType = C.swEndCondMidPlane if midplane else C.swEndCondBlind
        data.D1EndConditionDistance = mm(depth)
    f = fm.CreateFeature(data)
    out = result(doc, f, "base flange", f"{name} needs one closed outline, or an open profile with depth=.")
    if k_factor is None and gauge_row and table.get("k_factor") is not None:
        k_factor = table["k_factor"]
    if k_factor is not None:
        _set_part_k_factor(doc, k_factor)
    out.update(thickness=thickness, bend_radius=bend_radius, k_factor=round(_part_k_factor(doc), 4))
    if k_factor is not None and abs(out["k_factor"] - k_factor) > 1e-9:
        raise SwError(f"SolidWorks kept K-factor {out['k_factor']} instead of {k_factor}.")
    if gauge_row:
        out["gauge"] = gauge_row["gauge"]
    return out


def _template_sheet_metal(doc: Any) -> Any:
    """The part's template sheet metal feature: its definition holds the defaults every bend falls back on
    (the per-body Sheet-Metal feature ignores K-factor changes)."""
    folder = doc.FeatureManager.GetSheetMetalFolder()
    if folder is None:
        raise SwError("The part has no sheet metal folder.")
    return SW.cast(SW.cast(folder, "ISheetMetalFolder").GetFeature(), "IFeature")


def _part_k_factor(doc: Any) -> float:
    return SW.cast(_template_sheet_metal(doc).GetDefinition(), "ISheetMetalFeatureData").KFactor


def _set_part_k_factor(doc: Any, k: float) -> None:
    f = _template_sheet_metal(doc)
    data = SW.cast(f.GetDefinition(), "ISheetMetalFeatureData")
    data.AccessSelections(doc, None)
    cba = SW.cast(data.GetCustomBendAllowance(), "ICustomBendAllowance")
    cba.Type = SW.C.swBendAllowanceKFactor
    cba.KFactor = k
    data.SetCustomBendAllowance(cba)
    data.KFactor = k
    if not f.ModifyDefinition(data, doc, None):
        raise SwError("SolidWorks rejected the K-factor.")
    doc.ForceRebuild3(False)


def _gauge(table: str | None) -> dict:
    """A gauge table by file (workspace) or by words naming one of SolidWorks' sample tables."""
    if table and table.lower().endswith(".xlsx"):
        return gauge_mod.parse_gauge_table(gauge_mod.read_xlsx(app().path(table, must_exist=True, suffixes=(".xlsx",))))
    folder = Path(SW.app.GetExecutablePath()) / "lang" / (SW.app.GetCurrentLanguage() or "english") / "Sheet Metal Gauge Tables"
    files = sorted(folder.glob("sample table*.xlsx")) if folder.is_dir() else []
    words = (table or "aluminum metric").lower().split()
    pick = [f for f in files if all(w in f.stem.lower() for w in words)]
    if not pick:
        raise SwError(f"No gauge table matching '{table}'. SolidWorks tables: {', '.join(f.stem for f in files) or 'none found'}; "
                      "or give an .xlsx file in the workspace.")
    return gauge_mod.parse_gauge_table(gauge_mod.read_xlsx(pick[0])) | {"file": pick[0].name}


@tool(READ_ONLY)
def gauge_table(
    table: Annotated[str | None, Field(description="An .xlsx gauge table in the workspace, or words naming a SolidWorks sample table ('aluminum metric', 'steel').")] = None,
) -> dict:
    """Gauges of a sheet metal gauge table with thickness and allowed bend radii in mm, read without Excel;
    use a gauge with sheet_metal_base(gauge=...)."""
    return _gauge(table)


def _edge_faces(doc: Any, ref: str) -> tuple[Any, Any, Any]:
    """(edge, sheet face, side face) for a straight edge of the sheet. Resolved afresh each time: COM
    objects for faces and edges go stale when a feature is added."""
    edge = g.resolve(doc, ref).obj
    if not SW.cast(edge.GetCurve(), "ICurve").IsLine():
        raise SwError(f"{ref}: edge flanges need straight edges.")
    faces = sorted(SW.cast_all(edge.GetTwoAdjacentFaces2(), "IFace2"), key=lambda f: f.GetArea())
    if len(faces) != 2:
        raise SwError(f"{ref} must lie between a sheet face and the sheet's edge face.")
    return edge, faces[1], faces[0]


def _flange_profile(doc: Any, ref: str, length: float) -> tuple[Any, Any]:
    """Reference plane through a straight edge, perpendicular to the sheet, with a rectangular flange profile
    on it (SolidWorks expects the profile square to the sheet and swings it to the bend angle itself).
    Returns (plane feature, sketch feature)."""
    C = SW.C
    fm = doc.FeatureManager
    edge, sheet, _ = _edge_faces(doc, ref)
    prm = SW.cast(edge.GetCurveParams3(), "ICurveParamData")
    a, b = list(prm.StartPoint)[:3], list(prm.EndPoint)[:3]
    n = _unit(list(sheet.Normal))
    corners = [a, b, [b[i] + n[i] * mm(length) for i in range(3)], [a[i] + n[i] * mm(length) for i in range(3)]]
    doc.ClearSelection2(True)
    g.Ref(sheet, "face", "sheet face").select(doc, mark=0)
    g.Ref(edge, "edge", "flange edge").select(doc, mark=1)
    before = _last_feature(doc).Name
    fm.InsertRefPlane(C.swRefPlaneReferenceConstraint_Perpendicular, 0, C.swRefPlaneReferenceConstraint_Coincident, 0, 0, 0)
    plane = _last_feature(doc)
    if plane is None or plane.Name == before:
        doc.ClearSelection2(True)
        raise SwError(f"Could not build the flange profile plane through {ref}.")
    doc.ClearSelection2(True)
    plane.Select2(False, 0)
    sm = doc.SketchManager
    sm.InsertSketch(True)
    sk = SW.cast(sm.ActiveSketch, "ISketch")
    frame = SketchFrame(sk)
    q = [frame.to_sketch(p) for p in corners]
    sm.AddToDB = True
    try:
        for i in range(4):
            sm.CreateLine(q[i][0], q[i][1], 0, q[(i + 1) % 4][0], q[(i + 1) % 4][1], 0)
    finally:
        sm.AddToDB = False
    skname = sketch_name(doc, sk)
    sm.InsertSketch(True)
    return plane, g.feature_by_name(doc, skname)


@tool(WRITES)
def edge_flange(
    edges: Annotated[list[str], Field(description="Straight edges of the sheet ('edge@x,y,z'). The flange rises on the side of the sheet face the edge belongs to (pick the top or bottom edge of the sheet).")],
    length: Annotated[float, Field(gt=0, description="Flange length in mm, measured from the face the edge lies on (inner virtual sharp).")],
    angle: Annotated[float, Field(gt=0, lt=180, description="Bend angle in degrees (90 for a right-angle flange).")] = 90.0,
    position: Annotated[Literal["material_inside", "material_outside", "bend_outside"], Field(description="Where the flange sits relative to the edge: material_inside keeps the outer size of the blank.")] = "material_inside",
    bend_radius: Annotated[float | None, Field(description="Inside bend radius in mm; default is the part's sheet metal radius.")] = None,
    k_factor: Annotated[float | None, Field(gt=0, lt=1, description="K-factor for these bends (0.3-0.5; aluminium alloys often 0.4-0.45); default is the part's.")] = None,
) -> dict:
    """Fold up flanges along edges of a sheet metal part (box sides, mounting tabs, stiffening lips)."""
    doc = SW.active("part")
    C = SW.C
    _sheet_metal_feature(doc)
    if not edges or any(g.resolve(doc, e).kind != "edge" for e in edges):
        raise SwError("Give one or more straight edges ('edge@x,y,z').")
    planes, sketches = [], []
    f = None
    try:
        for e in edges:
            plane, skf = _flange_profile(doc, e, length)
            planes.append(plane)
            sketches.append(skf)
        # re-resolve: building the planes and sketches can invalidate the edge objects
        objs = [g.resolve(doc, e).obj for e in edges]
        opts = C.swInsertEdgeFlangeUseDefaultRelief | (0 if bend_radius else C.swInsertEdgeFlangeUseDefaultRadius)
        cba = None
        if k_factor is not None:
            cba = SW.cast(doc.FeatureManager.CreateCustomBendAllowance(), "ICustomBendAllowance")
            cba.Type = C.swBendAllowanceKFactor
            cba.KFactor = k_factor
        doc.ClearSelection2(True)
        f = doc.FeatureManager.InsertSheetMetalEdgeFlange2(
            _dispatch_array(objs), _dispatch_array(sketches), opts, rad(angle), mm(bend_radius or 0), getattr(C, _POSITION[position]),
            0.0, C.swSheetMetalReliefRectangular, 0.5, 0.0, 0.0, C.swFlangeDimTypeInnerVirtualSharp, cba)
    finally:
        doc.ClearSelection2(True)
        if f is None:  # leave no profile planes or sketches behind; one at a time, a joint delete misses some
            for feat in sketches + planes:
                doc.ClearSelection2(True)
                feat.Select2(False, 0)
                doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
            doc.ClearSelection2(True)
        else:
            for p in planes:
                p.Select2(True, 0)
            doc.BlankRefGeom()  # hide the profile planes
    out = result(doc, f, "edge flange", "Use straight edges on the sheet's top or bottom face, and a length larger than the bend radius.")
    out["flanges"] = len(edges)
    if k_factor is not None:
        out["k_factor"] = k_factor
    return out


@tool(WRITES)
def flat_pattern(
    action: Annotated[Literal["info", "flatten", "fold"], Field(description="info: sheet metal data and flat blank size (the model is left as it was); flatten / fold: show the part flat or formed.")] = "info",
    export: Annotated[str | None, Field(description="Also write the flat blank to this .dxf or .dwg file, with bend lines (the part must be saved first).")] = None,
) -> dict:
    """Sheet metal data (thickness, bend radius, K-factor, bend count) and the flat blank size, flatten or
    fold the part, and export the flat pattern to DXF/DWG for laser, waterjet or punch programming."""
    doc = SW.active("part")
    C = SW.C
    sm = _sheet_metal_feature(doc)
    fp = _flat_pattern(doc)
    d = SW.cast(sm.GetDefinition(), "ISheetMetalFeatureData")
    out: dict[str, Any] = {"thickness": to_mm(d.Thickness), "bend_radius": to_mm(d.BendRadius), "default_k_factor": round(_part_k_factor(doc), 4)}
    was_flat = _is_flat(fp)
    _set_flat(doc, fp, True)
    try:
        if err := feature_error(fp):
            raise SwError(f"The part does not flatten ({err}). Check the flanges with list_features.")
        box = list(SW.part(doc).GetPartBox(True))
        size = sorted((to_mm(box[i + 3] - box[i]) for i in range(3)), reverse=True)
        out["flat_size_mm"] = [round(size[0], 3), round(size[1], 3)]
        bends = 0
        sub = SW.cast(fp.GetFirstSubFeature(), "IFeature")
        while sub is not None:
            bends += sub.GetTypeName2() == "UiBend"
            sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
        out["bends"] = bends
    finally:
        _set_flat(doc, fp, {"flatten": True, "fold": False}.get(action, was_flat))
    out["state"] = "flat" if _is_flat(fp) else "formed"
    if export:
        p = app().path(export, suffixes=(".dxf", ".dwg"))
        if not doc.GetPathName():
            raise SwError("Save the part with save_document before exporting its flat pattern.")
        mark = checked_marking(doc)
        p.unlink(missing_ok=True)
        opts = 1 | 4 | 16  # flat geometry, bend lines, merge coplanar faces
        ok = SW.part(doc).ExportToDWG2(str(p), doc.GetPathName(), C.swExportToDWG_ExportSheetMetal, True, None, False, False, opts, None)
        if not ok or not p.exists():
            raise SwError(f"SolidWorks could not export the flat pattern to {p.name}.")
        out["exported"] = {"file": str(p), "bytes": p.stat().st_size}
        if mark:
            out["marking"] = mark
    return out


_HEMS = {"open": "swHemTypeOpen", "closed": "swHemTypeClosed", "teardrop": "swHemTypeTearDrop", "rolled": "swHemTypeRolled", "double": "swHemTypeDouble"}


@tool(WRITES)
def hem(
    edges: Annotated[list[str], Field(description="Straight edges of the sheet to hem ('edge@x,y,z').")],
    kind: Annotated[Literal["closed", "open", "teardrop", "rolled", "double"], Field(description="Hem type: closed (flat, folded back), open (with a gap), teardrop, rolled or double.")] = "closed",
    length: Annotated[float, Field(gt=0, description="Hem length in mm (closed, open, double).")] = 6.0,
    gap: Annotated[float, Field(ge=0, description="Gap in mm (open hems).")] = 0.5,
    radius: Annotated[float, Field(ge=0, description="Radius in mm (teardrop and rolled hems).")] = 1.0,
    angle: Annotated[float, Field(description="Angle in degrees (teardrop and rolled hems).")] = 200.0,
    outside: Annotated[bool, Field(description="Put the hem outside the edge instead of inside the existing outline.")] = False,
    reverse: Annotated[bool, Field(description="Fold to the other side of the sheet.")] = False,
) -> dict:
    """Hem along sheet edges: stiffens and removes the sharp edge of covers, panels and access doors."""
    doc = SW.active("part")
    C = SW.C
    _sheet_metal_feature(doc)
    before = _last_feature(doc).Name
    doc.ClearSelection2(True)
    for e in edges:
        ref = g.resolve(doc, e)
        if ref.kind != "edge":
            raise SwError(f"{e} is not an edge.")
        ref.select(doc)
    pos = C.swHemPositionTypeOutside if outside else C.swHemPositionTypeInside
    f = doc.FeatureManager.InsertSheetMetalHem2(getattr(C, _HEMS[kind]), pos, reverse, mm(length), mm(gap), rad(angle), mm(radius), 0.0, None,
                                                True, 0, 0, False, 0.5, 0.0, 0.0)
    if f is None:
        doc.ClearSelection2(True)
        raise SwError("SolidWorks could not add the hem; use straight edges on the sheet's outline.")
    out = result(doc, f, "hem")
    if out["feature"] == before:
        raise SwError("SolidWorks did not add the hem.")
    out["edges"] = len(edges)
    return out


@tool(WRITES)
def sketched_bend(
    fixed: Annotated[list[float], Field(description="A point [x, y, z] in mm on the part of the sheet that stays in place.")],
    angle: Annotated[float, Field(gt=0, lt=180, description="Bend angle in degrees.")] = 90.0,
    sketch: Annotated[str | None, Field(description="Sketch on a sheet face holding the bend line(s); default is the latest sketch.")] = None,
    position: Annotated[Literal["centerline", "material_inside", "material_outside", "bend_outside"], Field(description="Where the bend sits relative to the line.")] = "centerline",
    radius: Annotated[float | None, Field(description="Inside bend radius in mm; default is the part's.")] = None,
    reverse: Annotated[bool, Field(description="Bend to the other side of the sheet.")] = False,
) -> dict:
    """Bend a sheet metal part along sketched lines (clips, brackets and tabs bent out of a flat blank)."""
    doc = SW.active("part")
    C = SW.C
    _sheet_metal_feature(doc)
    p = [mm(c) for c in fixed]
    name = _select_sketch(doc, sketch)  # closes an open sketch first, which invalidates earlier face objects
    face = g.resolve(doc, "face@{},{},{}".format(*fixed)).obj
    doc.ClearSelection2(True)
    g.select(doc, name if ":" in name else f"sketch:{name}")
    # the fixed side comes from where the face is picked, so select it at the point like a mouse click
    doc.Extension.SelectByID2("", "FACE", p[0], p[1], p[2], True, 0, None, 0)
    sel = SW.cast(doc.SelectionManager, "ISelectionMgr")
    picked = sel.GetSelectedObject6(sel.GetSelectedObjectCount2(-1), -1)
    if picked is None or SW.app.IsSame(picked, face) != 1:
        doc.ClearSelection2(True)
        raise SwError(f"Could not pick the face at {fixed}; give a point further from edges and holes.")
    data = SW.cast(doc.FeatureManager.CreateDefinition(C.swFmSketchBend), "ISketchedBendFeatureData")
    data.BendAngle = rad(angle)
    data.ReverseDirection = reverse
    data.PositionType = {"centerline": C.swFlangePositionTypeBendCenterLine, "material_inside": C.swFlangePositionTypeMaterialInside,
                         "material_outside": C.swFlangePositionTypeMaterialOutside, "bend_outside": C.swFlangePositionTypeBendOutside}[position]
    if radius:
        data.UseDefaultBendRadius = False
        data.BendRadius = mm(radius)
    data.SetFixedFace(p[0], p[1], p[2], face)
    f = doc.FeatureManager.CreateFeature(data)
    out = result(doc, f, "sketched bend", f"{name} needs straight lines on a flat face of the sheet.")
    out["angle"] = angle
    return out


@tool(WRITES)
def convert_to_sheet_metal(
    fixed_face: Annotated[str, Field(description="A flat face of the part that stays in place when it is flattened ('face@x,y,z').")],
    bend_edges: Annotated[list[str], Field(description="The sharp outside corner edges where the part bends ('edge@x,y,z'); each becomes a bend.")],
    thickness: Annotated[float, Field(gt=0, description="Wall thickness of the part in mm.")],
    bend_radius: Annotated[float, Field(gt=0, description="Inside bend radius in mm.")],
    gap: Annotated[float, Field(ge=0, description="Rip gap in mm at corners without a bend.")] = 0.1,
) -> dict:
    """Turn a solid of uniform thickness (such as an imported STEP bracket) into a sheet metal part with
    bends, so flat_pattern can report and export its blank."""
    doc = SW.active("part")
    C = SW.C
    if any(f.GetTypeName2() == "SheetMetal" for f in g.iter_features(doc)):
        raise SwError("The part is already sheet metal.")
    before = _last_feature(doc).Name
    doc.ClearSelection2(True)
    face = g.resolve(doc, fixed_face)
    if face.kind != "face":
        raise SwError(f"{fixed_face} is not a face.")
    face.select(doc, mark=0)
    for e in bend_edges:
        ref = g.resolve(doc, e)
        if ref.kind != "edge":
            raise SwError(f"{e} is not an edge.")
        ref.select(doc, mark=1)
    doc.FeatureManager.InsertConvertToSheetMetal2(mm(thickness), False, False, mm(bend_radius), mm(gap), C.swSheetMetalReliefRectangular,
                                                  0.5, 0, 0.5, False)
    doc.ClearSelection2(True)
    if not any(f.GetTypeName2() == "SheetMetal" for f in g.iter_features(doc)) or _last_feature(doc).Name == before:
        raise SwError("SolidWorks could not convert the part; give a flat fixed face and the outside corner edges of the bends.")
    doc.EditRebuild3()
    info = flat_pattern("info")
    info["feature"] = _last_feature(doc).Name
    if info.get("bends", 0) < len(bend_edges):
        info["warning"] = f"{len(bend_edges)} bend edges were given but the flat pattern has {info.get('bends', 0)} bends; check the edges."
    return info


@tool(WRITES)
def closed_corner(
    faces: Annotated[list[str], Field(description="End faces of flanges at the corners to close ('face@x,y,z'; list_faces near the corner shows the small end faces).")],
) -> dict:
    """Close the gaps where two flanges meet at a corner of a sheet metal box or tray, by extending one
    flange's end face up to the other (sealed avionics enclosures, drip-proof trays)."""
    doc = SW.active("part")
    _sheet_metal_feature(doc)
    before = _last_feature(doc).Name
    doc.ClearSelection2(True)
    for f in faces:
        ref = g.resolve(doc, f)
        if ref.kind != "face":
            raise SwError(f"{f} is not a face.")
        ref.select(doc)
    doc.InsertSheetMetalClosedCorner()
    doc.ClearSelection2(True)
    after = _last_feature(doc)
    if after is None or after.Name == before:
        raise SwError("SolidWorks could not close the corner; select the end face of a flange where it meets another flange.")
    out = result(doc, after, "closed corner")
    if err := feature_error(_flat_pattern(doc)):
        out["warning"] = f"The part no longer flattens cleanly ({err})."
    return out


def _free_vertex(e0: Any, e1: Any | None) -> list[float]:
    """End point of the first edge of a chain that the second edge does not touch (where the profile goes)."""
    ends = [list(SW.cast(v, "IVertex").GetPoint())[:3] for v in (e0.GetStartVertex(), e0.GetEndVertex())]
    if e1 is None:
        return ends[0]
    other = [list(SW.cast(v, "IVertex").GetPoint())[:3] for v in (e1.GetStartVertex(), e1.GetEndVertex())]
    return max(ends, key=lambda p: min(math.dist(p, q) for q in other))


@tool(WRITES)
def miter_flange(
    edges: Annotated[list[str], Field(min_length=1, description="Connected straight edges of the sheet, in order around the part ('edge@x,y,z' on the top or bottom face).")],
    length: Annotated[float, Field(gt=0, description="Flange height in mm, from the sheet face.")],
    position: Annotated[Literal["material_inside", "material_outside", "bend_outside"], Field(description="Where the flanges sit relative to the edges.")] = "material_inside",
    bend_radius: Annotated[float | None, Field(description="Inside bend radius in mm; default is the part's.")] = None,
    gap: Annotated[float, Field(ge=0, description="Rip gap at the mitred corners, mm.")] = 0.1,
) -> dict:
    """Miter flange: one continuous flange along a chain of edges with mitred corners (trays, enclosures,
    avionics boxes), from a straight profile generated square to the sheet at the free end of the chain."""
    doc = SW.active("part")
    C = SW.C
    _sheet_metal_feature(doc)
    if any(g.resolve(doc, e).kind != "edge" for e in edges):
        raise SwError("Give straight edges ('edge@x,y,z').")
    e0, sheet, _ = _edge_faces(doc, edges[0])
    e1 = g.resolve(doc, edges[1]).obj if len(edges) > 1 else None
    p = _free_vertex(e0, e1)
    n = _unit(list(sheet.Normal))
    fm = doc.FeatureManager
    doc.ClearSelection2(True)
    g.Ref(e0, "edge", edges[0]).select(doc, mark=0)
    vertex = next(v for v in (e0.GetStartVertex(), e0.GetEndVertex()) if math.dist(list(SW.cast(v, "IVertex").GetPoint())[:3], p) < 1e-9)
    data = SW.cast(SW.cast(doc.SelectionManager, "ISelectionMgr").CreateSelectData(), "ISelectData")
    data.Mark = 1
    SW.cast(vertex, "IEntity").Select4(True, data)
    before = _last_feature(doc).Name
    fm.InsertRefPlane(C.swRefPlaneReferenceConstraint_Perpendicular, 0, C.swRefPlaneReferenceConstraint_Coincident, 0, 0, 0)
    plane = _last_feature(doc)
    if plane is None or plane.Name == before:
        doc.ClearSelection2(True)
        raise SwError(f"Could not build the profile plane at the end of {edges[0]}.")
    skf = f = None
    try:
        doc.ClearSelection2(True)
        plane.Select2(False, 0)
        sm = doc.SketchManager
        sm.InsertSketch(True)
        sk = SW.cast(sm.ActiveSketch, "ISketch")
        frame = SketchFrame(sk)
        a, b = frame.to_sketch(p), frame.to_sketch([p[i] + n[i] * mm(length) for i in range(3)])
        sm.AddToDB = True
        try:
            sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
        finally:
            sm.AddToDB = False
        name = sketch_name(doc, sk)
        sm.InsertSketch(True)
        skf = g.feature_by_name(doc, name)
        doc.ClearSelection2(True)
        skf.Select2(False, 0)
        for e in edges:
            g.resolve(doc, e).select(doc, mark=0)
        f = fm.InsertSheetMetalMiterFlange(bend_radius is None, mm(bend_radius or 0), mm(gap), True, False, 0.5, 0.0, 0.0,
                                           C.swSheetMetalReliefRectangular, False, getattr(C, _POSITION[position]), 0.0, 0.0, None)
    finally:
        doc.ClearSelection2(True)
        if f is None:
            for feat in (skf, plane):  # one at a time: deleting both in one selection leaves one behind
                if feat is not None:
                    doc.ClearSelection2(True)
                    feat.Select2(False, 0)
                    doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
            doc.ClearSelection2(True)
        else:
            plane.Select2(False, 0)
            doc.BlankRefGeom()
    out = result(doc, f, "miter flange", "The edges must be connected straight edges on one face of the sheet, listed in order.")
    out["edges"] = len(edges)
    return out


def _forming_tools() -> dict[str, str]:
    """Forming tools of the SolidWorks design library, by name ('dimple', 'louver', 'extruded hole', ...)."""
    import glob
    import os

    roots = [os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "SolidWorks", f"SOLIDWORKS {y}", "design library", "forming tools")
             for y in range(2035, 2017, -1)]
    for root in roots:
        if os.path.isdir(root):
            return {os.path.splitext(os.path.basename(p))[0].lower(): p for p in glob.glob(os.path.join(root, "**", "*.sldftp"), recursive=True)}
    return {}


@tool(WRITES)
def forming_tool(
    tool_name: Annotated[str, Field(description=(
        "Forming tool from the SolidWorks design library by name ('dimple', 'louver', 'extruded hole', 'round flange', "
        "'single rib', 'circular emboss', 'counter sink emboss', 'bridge lance', ...) or a .sldftp file."))],
    position: Annotated[list[float], Field(min_length=3, max_length=3, description="Where on the sheet face it goes, model mm [x, y, z].")],
    angle: Annotated[float, Field(description="Rotation about the face normal, degrees (louvres, ribs, lances).")] = 0.0,
    flip: Annotated[bool, Field(description="Form towards the other side of the sheet.")] = False,
) -> dict:
    """Forming tool on a sheet metal face: dimples, embosses, flanged (extruded) holes, louvres, lances and stiffening
    ribs, placed exactly. They show in the flat pattern and its DXF as their tool profiles."""
    doc = SW.active("part")
    _sheet_metal_feature(doc)
    lib = _forming_tools()
    if tool_name.lower().endswith(".sldftp"):
        path = str(app().path(tool_name, must_exist=True, suffixes=(".sldftp",)))
    elif tool_name.lower() in lib:
        path = lib[tool_name.lower()]
    else:
        raise SwError(f"No forming tool '{tool_name}'. Available: {', '.join(sorted(lib)) or 'none (the design library is not installed)'}.")
    face = g.resolve(doc, "face@{},{},{}".format(*position))
    box0 = list(SW.part(doc).GetPartBox(True))
    doc.ClearSelection2(True)
    face.select(doc, append=False)
    f = doc.FeatureManager.InsertFormToolFeature(path, flip, rad(angle), "", False, False, False, False, True)
    doc.ClearSelection2(True)
    if f is None:
        raise SwError(f"SolidWorks could not place {tool_name}; pick a flat face of the sheet with room for it.")
    f = SW.cast(f, "IFeature")
    target = [mm(c) for c in position]
    # the tool lands at the sketch origin; its position sketch's origin is the insertion point and moves with
    # the sketch, so move the sketch until that origin is on the requested point
    sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
    while sub is not None and sub.GetTypeName2() != "ProfileFeature":
        sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
    placed = False
    if sub is not None:
        sk = SW.cast(sub.GetSpecificFeature2(), "ISketch")
        frame = SketchFrame(sk)
        b = frame.to_sketch(target)
        doc.ClearSelection2(True)
        sub.Select2(False, 0)
        doc.EditSketch()
        for s_ in SW.cast_all(sk.GetSketchSegments(), "ISketchSegment"):
            s_.Select4(True, None)
        for p_ in SW.cast_all(sk.GetSketchPoints2(), "ISketchPoint"):
            p_.Select4(True, None)
        doc.Extension.MoveOrCopy(False, 1, False, 0, 0, 0, b[0], b[1], 0)
        doc.SketchManager.InsertSketch(True)
        doc.ClearSelection2(True)
        doc.EditRebuild3()
        normal = _unit(list(face.obj.Normal))
        box1 = list(SW.part(doc).GetPartBox(True))
        # the form must stay on the sheet: across the face the part may not grow
        inside = all(box1[i] >= box0[i] - 1e-6 and box1[i + 3] <= box0[i + 3] + 1e-6 for i in range(3) if abs(normal[i]) < 0.99)

        def on_target(sketch_feat: Any) -> bool:
            o = SketchFrame(SW.cast(sketch_feat.GetSpecificFeature2(), "ISketch")).to_model([0.0, 0.0, 0.0])
            off = [o[i] - target[i] for i in range(3)]
            along = sum(off[i] * normal[i] for i in range(3))
            return math.dist(off, [along * n for n in normal]) < 1e-6

        # the tool's profile sketch follows the position sketch: its origin must now be on the point
        others = []
        x = SW.cast(f.GetFirstSubFeature(), "IFeature")
        while x is not None:
            if x.GetTypeName2() == "ProfileFeature" and x.Name != sub.Name:
                others.append(x)
            x = SW.cast(x.GetNextSubFeature(), "IFeature")
        placed = inside and any(on_target(o) for o in others) and f.GetErrorCode2()[0] == 0
    if not placed:
        doc.ClearSelection2(True)
        f.Select2(False, 0)
        doc.Extension.DeleteSelection2(SW.C.swDelete_Absorbed)
        raise SwError(f"{tool_name} could not be placed at {position} (it may run off the sheet or over a bend). Nothing was created.")
    out = result(doc, f, "forming tool")
    out.update(tool=Path(path).stem, position=position)
    return out


@tool(WRITES)
def jog(
    line: Annotated[list[list[float]], Field(min_length=2, max_length=2, description="The first bend line on the sheet face, [[x, y, z], [x, y, z]] model mm (the one nearer the fixed side).")],
    fixed: Annotated[list[float], Field(min_length=3, max_length=3, description="A point [x, y, z] on the part of the sheet that stays in place.")],
    offset: Annotated[float, Field(gt=0, description="Jog (joggle) depth: how far the far end moves off the sheet plane, mm.")],
    angle: Annotated[float, Field(gt=0, lt=90, description="Angle of the jog's slope, degrees.")] = 30.0,
    radius: Annotated[float | None, Field(description="Inside bend radius in mm; default is the part's.")] = None,
    reverse: Annotated[bool, Field(description="Jog to the other side of the sheet.")] = False,
) -> dict:
    """Jog (joggle) in a sheet: two opposite bends that step the far part of the sheet off its plane by offset and
    keep it parallel, as where a skin or bracket passes over a doubler or flange. Built from two sketched bends
    whose spacing is solved so the measured offset matches."""
    from .tools_inspect import feature_action
    from .tools_sketch import finish_sketch, sketch, start_sketch

    doc = SW.active("part")
    _sheet_metal_feature(doc)
    a, b = ([mm(c) for c in p] for p in line)
    fx = [mm(c) for c in fixed]
    face = g.resolve(doc, "face@{},{},{}".format(*[(line[0][i] + line[1][i]) / 2 for i in range(3)]))
    n = _unit(list(face.obj.Normal))
    along = _unit([b[i] - a[i] for i in range(3)])
    u = _unit([n[(i + 1) % 3] * along[(i + 2) % 3] - n[(i + 2) % 3] * along[(i + 1) % 3] for i in range(3)])  # in the face, across the line
    if sum((fx[i] - a[i]) * u[i] for i in range(3)) > 0:
        u = [-c for c in u]  # point away from the fixed side
    face_pt = list(face.obj.GetClosestPointOn(*a))[:3]

    def build(d: float) -> tuple[list[str], float]:
        made = []
        far = [[(p[i] + u[i] * d) / 0.001 for i in range(3)] for p in (a, b)]
        mid = [(a[i] + b[i]) / 2 / 0.001 + u[i] * d / 2 / 0.001 for i in range(3)]
        for pts, fix, rev in ((far, mid, reverse), ([[c / 0.001 for c in a], [c / 0.001 for c in b]], fixed, not reverse)):
            start_sketch(on="face@{},{},{}".format(*fixed))
            sketch(coords="model", entities=[{"type": "line", "from": pts[0], "to": pts[1]}])
            finish_sketch()
            made.append(sketched_bend(fixed=list(fix), angle=angle, radius=radius, reverse=rev)["feature"])
        # the offset: the far region's face of the same side, measured along the original normal
        beyond = [f for f in g.faces(doc) if SW.cast(f.GetSurface(), "ISurface").IsPlane() and sum(x * y for x, y in zip(f.Normal, n)) > 0.9999]
        offs = []
        for f in beyond:
            box = f.GetBox
            box = box() if callable(box) else box
            c = list(f.GetClosestPointOn(*[(box[i] + box[i + 3]) / 2 for i in range(3)]))[:3]
            if sum((c[i] - a[i]) * u[i] for i in range(3)) > d:
                offs.append(abs(sum((c[i] - face_pt[i]) * n[i] for i in range(3))))
        return made, (max(offs) if offs else 0.0)

    d = mm(offset) / math.sin(math.radians(angle))
    for _ in range(4):
        made, got = build(d)
        if got and abs(got - mm(offset)) < 1e-5:
            break
        feature_action(features=list(reversed(made)), action="delete")
        if not got:
            raise SwError("The jog did not form; give a straight line across a flat face of the sheet, with room beyond it.")
        d *= mm(offset) / got
    else:
        raise SwError(f"Could not reach a jog offset of {offset} mm (got {to_mm(got):.3f}); try a larger angle or smaller radius.")
    out = result(doc, g.feature_by_name(doc, made[-1]), "jog")
    out.update(features=made, offset=round(to_mm(got), 4), spacing=round(to_mm(d), 4), angle=angle)
    return out

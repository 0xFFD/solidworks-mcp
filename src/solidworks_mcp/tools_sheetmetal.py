"""Sheet metal: base flanges, edge flanges, flat patterns and flat-pattern DXF export."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, mm, rad, to_mm
from .errors import SwError
from .registry import WRITES, app, tool
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
    thickness: Annotated[float, Field(gt=0, description="Sheet thickness in mm (e.g. 1.0, 1.6, 2.0 for aluminium 2024/5052/6061 sheet).")],
    bend_radius: Annotated[float, Field(gt=0, description="Inside bend radius in mm; for aluminium alloys usually 1-3 x thickness depending on temper.")],
    sketch: Annotated[str | None, Field(description="Sketch with a closed outline (a flat tab) or an open profile (an L, U, Z or hat section); default is the latest sketch.")] = None,
    depth: Annotated[float | None, Field(description="Open profiles: extrusion depth in mm.")] = None,
    midplane: Annotated[bool, Field(description="Open profiles: extrude half the depth each way.")] = False,
    reverse_thickness: Annotated[bool, Field(description="Add the thickness to the other side of the sketch.")] = False,
) -> dict:
    """Start a sheet metal part with a base flange from a sketch: a closed outline gives a flat blank, an
    open profile gives a formed section with the bends. Add edge_flange to fold up edges and flat_pattern
    to flatten it and export the blank. Bends of the base flange use the part's default K-factor
    (reported here); edge_flange can set its own."""
    doc = SW.active("part")
    C = SW.C
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
    out.update(thickness=thickness, bend_radius=bend_radius, k_factor=round(SW.cast(_sheet_metal_feature(doc).GetDefinition(), "ISheetMetalFeatureData").KFactor, 4))
    return out


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
        if f is None:  # leave no profile planes or sketches behind
            for feat in sketches + planes:
                feat.Select2(True, 0)
            doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
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
    out: dict[str, Any] = {"thickness": to_mm(d.Thickness), "bend_radius": to_mm(d.BendRadius), "default_k_factor": round(d.KFactor, 4)}
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

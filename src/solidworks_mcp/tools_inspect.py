"""Inspection and editing of the model: tree, topology, dimensions, physical properties, images."""

from __future__ import annotations

import io
import math
import time
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image
from pydantic import Field

from . import geometry as g
from .com import SW, doc_kind, mm, to_deg, to_mm
from .errors import SwError
from .registry import DESTRUCTIVE, READ_ONLY, WRITES, app, tool
from .tools_features import feature_error

# Folder-like tree items that carry no geometry.
_HIDDEN_TYPES = {
    "CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder", "SensorFolder", "DocsFolder", "DetailCabinet",
    "SurfaceBodyFolder", "SolidBodyFolder", "EnvFolder", "InkMarkupFolder", "EqnFolder", "MaterialFolder", "MateReferenceGroupFolder",
    "AmbientLight", "DirectionLight", "SpotLight", "PointLight", "CameraFolder", "BlockFolder", "GridDetailFolder", "LiveSectionFolder",
    "DisplayStateFolder", "ProfileFeatureFolder", "RefAxisFolder", "CutListFolder", "SubWeldFolder", "MateGroup",
}


@tool(READ_ONLY)
def list_features(include_reference: Annotated[bool, Field(description="Include planes, axes and origin.")] = True) -> dict:
    """Feature tree of the active document with type, suppression state and rebuild errors."""
    doc = SW.active()
    rows = []
    errors = 0
    for f in g.iter_features(doc):
        t = f.GetTypeName2()
        if t in _HIDDEN_TYPES:
            continue
        if not include_reference and t in ("RefPlane", "RefAxis", "OriginProfileFeature", "CoordSys"):
            continue
        row: dict[str, Any] = {"name": f.Name, "type": t}
        try:
            sup = f.IsSuppressed2(1, None)
            if sup and any(sup):
                row["suppressed"] = True
        except Exception:
            pass
        if err := feature_error(f):
            row["rebuild"] = err
            errors += 1
        if t in ("ProfileFeature", "3DProfileFeature"):
            sk = SW.cast(f.GetSpecificFeature2(), "ISketch")
            if sk is not None:
                from .tools_sketch import sketch_status

                row["status"] = sketch_status(sk)
        rows.append(row)
    return {"document": doc.GetTitle(), "features": rows, "rebuild_errors": errors}


@tool(READ_ONLY)
def list_faces(
    component: Annotated[str | None, Field(description="Assembly component name (e.g. 'Bracket-1'); default is the active part.")] = None,
    feature: Annotated[str | None, Field(description="Only faces created by this feature.")] = None,
    type: Annotated[Literal["plane", "cylinder", "cone", "sphere", "torus", "freeform"] | None, Field(description="Only faces of this surface type.")] = None,
    near: Annotated[list[float] | None, Field(description="Sort by distance to this [x, y, z] point (mm).")] = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> dict:
    """Faces of the solid bodies with index, surface type, outward normal (planes), radius and axis
    (cylinders, 'hole': true for bores), area and a point on the face. Use 'face:<i>' to reference them."""
    doc = SW.active()
    rows = []
    xf = g.placement(doc, component)
    for i, f in enumerate(g.faces(doc, component)):
        info = g.face_info(f, i, xf)
        if feature and info.get("feature") != feature:
            continue
        if type and info["type"] != type:
            continue
        rows.append(info)
    if near:
        rows.sort(key=lambda r: math.dist(r["point"], near))
    prefix = f"face:{component}/" if component else "face:"
    return {"reference": prefix + "<i>", "count": len(rows), "faces": rows[:limit]}


@tool(READ_ONLY)
def list_edges(
    component: Annotated[str | None, Field(description="Assembly component name; default is the active part.")] = None,
    face: Annotated[str | None, Field(description="Only edges bounding this face ('face:N' or 'face@x,y,z').")] = None,
    type: Annotated[Literal["line", "circle", "arc", "ellipse", "spline"] | None, Field(description="Only edges of this curve type.")] = None,
    near: Annotated[list[float] | None, Field(description="Sort by distance to this [x, y, z] point (mm).")] = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> dict:
    """Edges with index, curve type, length, mid point, end points (open edges) and centre/radius
    (circles and arcs). Use 'edge:<i>' to reference them."""
    doc = SW.active()
    all_edges = g.edges(doc, component)
    xf0 = g.placement(doc, component)
    keep = None
    if face:
        ref = g.resolve(doc, face)
        if ref.kind != "face":
            raise SwError(f"{face} is not a face.")
        keep = []
        mids = [g.edge_info(e, 0, xf0)["mid"] for e in SW.cast_all(ref.obj.GetEdges(), "IEdge")]
    rows = []
    xf = g.placement(doc, component)
    for i, e in enumerate(all_edges):
        info = g.edge_info(e, i, xf)
        if keep is not None and not any(math.dist(info["mid"], m) < 1e-6 for m in mids):
            continue
        if type and info["type"] != type:
            continue
        rows.append(info)
    if near:
        rows.sort(key=lambda r: math.dist(r["mid"], near))
    prefix = f"edge:{component}/" if component else "edge:"
    return {"reference": prefix + "<i>", "count": len(rows), "edges": rows[:limit]}


def _dims_of(f: Any) -> list[dict]:
    out = []
    C = SW.C
    dd = f.GetFirstDisplayDimension()
    while dd is not None:
        dd = SW.cast(dd, "IDisplayDimension")
        d = SW.cast(dd.GetDimension2(0), "IDimension")
        if d is not None:
            v = d.GetSystemValue3(C.swThisConfiguration, None)
            v = v[0] if isinstance(v, (tuple, list)) else v
            kind = d.GetType()
            ang = kind == C.swDimensionParamTypeDoubleAngular
            row = {"name": d.FullName, "value": to_deg(v) if ang else (to_mm(v) if kind == C.swDimensionParamTypeDoubleLinear else v), "unit": "deg" if ang else "mm"}
            if d.DrivenState == C.swDimensionDriven:
                row["driven"] = True
            out.append(row)
        dd = f.GetNextDisplayDimension(dd)
    return out


@tool(READ_ONLY)
def dimensions(
    feature: Annotated[str | None, Field(description="Feature or sketch name; omit for every feature.")] = None,
) -> dict:
    """Dimensions that drive features and sketches, with full names (e.g. 'D1@Boss-Extrude1') for set_dimensions."""
    doc = SW.active()
    out = {}
    feats = [g.feature_by_name(doc, feature)] if feature else list(g.iter_features(doc))
    if feature and feats[0] is None:
        raise SwError(f"No feature named '{feature}'.")
    for f in feats:
        dims = _dims_of(f)
        sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
        while sub is not None:
            dims += _dims_of(sub)
            sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
        if dims:
            out[f.Name] = dims
    return {"dimensions": out}


@tool(WRITES)
def set_dimensions(
    values: Annotated[dict[str, float], Field(description="Full dimension name to new value: mm for lengths, degrees for angles, e.g. {'D1@Boss-Extrude1': 12.5}.")],
) -> dict:
    """Change driving dimensions in all configurations and rebuild."""
    doc = SW.active()
    C = SW.C
    changed = {}
    for name, val in values.items():
        d = doc.Parameter(name)
        if d is None:
            raise SwError(f"No dimension named '{name}'. Use dimensions() to list them.")
        d = SW.cast(d, "IDimension")
        sysval = val * math.pi / 180 if d.GetType() == C.swDimensionParamTypeDoubleAngular else mm(val)
        r = d.SetSystemValue3(sysval, C.swSetValue_InAllConfigurations, None)
        if r not in (0, None):
            raise SwError(f"SolidWorks rejected {name} = {val} (code {r}). Driven dimensions cannot be set.")
        changed[name] = val
    doc.EditRebuild3()
    errs = [{"feature": f.Name, "rebuild": e} for f in g.iter_features(doc) if (e := feature_error(f))]
    return {"changed": changed, "rebuild_errors": errs}


@tool(DESTRUCTIVE)
def feature_action(
    features: Annotated[list[str], Field(description="Feature names.")],
    action: Annotated[Literal["suppress", "unsuppress", "delete"], Field(description="delete also removes the feature's absorbed sketches.")],
) -> dict:
    """Suppress, unsuppress or delete features."""
    doc = SW.active()
    C = SW.C
    done = []
    for n in features:
        f = g.feature_by_name(doc, n)
        if f is None:
            raise SwError(f"No feature named '{n}'.")
        if action == "delete":
            doc.ClearSelection2(True)
            f.Select2(False, 0)
            if not doc.Extension.DeleteSelection2(C.swDelete_Absorbed):
                raise SwError(f"Could not delete {n}.")
        else:
            state = 0 if action == "suppress" else 2  # swSuppressFeature / swUnSuppressFeature
            if not f.SetSuppression2(state, C.swAllConfiguration, None):
                raise SwError(f"Could not {action} {n}.")
        done.append(n)
    doc.ClearSelection2(True)
    doc.EditRebuild3()
    return {action: done}


@tool(DESTRUCTIVE)
def undo(steps: Annotated[int, Field(ge=1, le=50)] = 1) -> dict:
    """Undo the last operations in the active document."""
    doc = SW.active()
    doc.EditUndo2(steps)
    last = SW.cast(doc.FeatureByPositionReverse(0), "IFeature")
    return {"undone_steps": steps, "last_feature": last.Name if last else None}


@tool(WRITES)
def rebuild(force: Annotated[bool, Field(description="Rebuild every feature, not only changed ones.")] = False) -> dict:
    """Rebuild the model and report features with errors."""
    doc = SW.active()
    ok = doc.ForceRebuild3(False) if force else doc.EditRebuild3()
    errs = [{"feature": f.Name, "rebuild": e} for f in g.iter_features(doc) if (e := feature_error(f))]
    return {"ok": bool(ok) and not errs, "rebuild_errors": errs}


@tool(READ_ONLY)
def mass_properties() -> dict:
    """Mass, volume, surface area, centre of mass and inertia (about the centre of mass, aligned with
    the model axes) using the assigned material. Units: kg, mm, kg*mm^2."""
    doc = SW.active()
    if doc_kind(doc) == "drawing":
        raise SwError("Open the part or assembly, not the drawing.")
    mp = SW.cast(doc.Extension.CreateMassProperty2(), "IMassProperty2")
    if mp is None:
        raise SwError("SolidWorks could not compute mass properties (no solid bodies?).")
    mp.UseSystemUnits = True
    C = SW.C
    moi = list(mp.GetMomentOfInertia(C.swMassPropertyMomentAboutCenterOfMass) or [])
    k = 1e6  # kg*m^2 -> kg*mm^2
    out = {
        "mass_kg": round(mp.Mass, 6),
        "volume_mm3": round(mp.Volume * 1e9, 3),
        "surface_area_mm2": round(mp.SurfaceArea * 1e6, 3),
        "density_kg_m3": round(mp.Density, 3),
        "center_of_mass_mm": [round(to_mm(c), 4) for c in mp.CenterOfMass],
        "inertia_at_com_kg_mm2": {
            "Ixx": round(moi[0] * k, 4), "Ixy": round(moi[1] * k, 4), "Ixz": round(moi[2] * k, 4),
            "Iyy": round(moi[4] * k, 4), "Iyz": round(moi[5] * k, 4), "Izz": round(moi[8] * k, 4),
        } if len(moi) == 9 else None,
        "principal_moments_kg_mm2": [round(v * k, 4) for v in (mp.PrincipalMomentsOfInertia or [])],
    }
    if doc_kind(doc) == "part":
        m = SW.part(doc).GetMaterialPropertyName2("")
        name = m[0] if isinstance(m, tuple) else m
        out["material"] = name or None
        if not name:
            out["warning"] = "No material assigned; SolidWorks used a default density. Use set_material."
    return out


@tool(READ_ONLY)
def bounding_box() -> dict:
    """Axis-aligned bounding box of the part or assembly (stock size), mm."""
    doc = SW.active()
    kind = doc_kind(doc)
    if kind == "part":
        box = SW.part(doc).GetPartBox(True)
    elif kind == "assembly":
        box = SW.assembly(doc).GetBox(0)
    else:
        raise SwError("Bounding box needs a part or assembly.")
    lo, hi = [to_mm(c) for c in box[0:3]], [to_mm(c) for c in box[3:6]]
    return {"min": lo, "max": hi, "size": [round(b - a, 4) for a, b in zip(lo, hi)]}


@tool(READ_ONLY)
def check_geometry() -> dict:
    """Validate the solid bodies (faults such as bad faces, gaps or self-intersections) and the
    rebuild state. Run before release or export."""
    doc = SW.active("part")
    bodies = g.bodies(doc)
    report = []
    for b in bodies:
        fe = SW.cast(b.Check3, "IFaultEntity")
        n = fe.Count if fe is not None else 0
        row = {"body": b.Name, "faults": n}
        if n:
            row["codes"] = [fe.ErrorCode(i) for i in range(min(n, 20))]
        report.append(row)
    errs = [{"feature": f.Name, "rebuild": e} for f in g.iter_features(doc) if (e := feature_error(f))]
    ok = all(r["faults"] == 0 for r in report) and not errs
    return {"ok": ok, "solid_bodies": len(bodies), "bodies": report, "rebuild_errors": errs}


@tool(READ_ONLY)
def measure(
    refs: Annotated[list[str], Field(description="One or two entity references (faces, edges, planes, axes).")],
) -> dict:
    """Measure entities: distance (minimum and centre-to-centre), normal distance, angle, length,
    radius and area, whatever applies to the selection. Values in mm, mm^2 and degrees."""
    doc = SW.active()
    doc.ClearSelection2(True)
    for r in refs:
        g.resolve(doc, r).select(doc, append=True)
    m = SW.cast(doc.Extension.CreateMeasure(), "IMeasure")
    m.ArcOption = 0
    if not m.Calculate(None):
        doc.ClearSelection2(True)
        raise SwError("SolidWorks could not measure that selection.")
    out: dict[str, Any] = {}
    for key, attr, conv in (("distance_mm", "Distance", to_mm), ("center_distance_mm", "CenterDistance", to_mm), ("normal_distance_mm", "NormalDistance", to_mm),
                            ("angle_deg", "Angle", to_deg), ("length_mm", "Length", to_mm), ("radius_mm", "Radius", to_mm),
                            ("diameter_mm", "Diameter", to_mm), ("area_mm2", "Area", lambda v: round(v * 1e6, 4)),
                            ("dx_mm", "DeltaX", to_mm), ("dy_mm", "DeltaY", to_mm), ("dz_mm", "DeltaZ", to_mm)):
        try:
            v = getattr(m, attr)
        except Exception:
            continue
        if v is not None and v != -1:
            out[key] = round(conv(v), 4)
    doc.ClearSelection2(True)
    return out


_VIEWS = {"isometric": "*Isometric", "trimetric": "*Trimetric", "dimetric": "*Dimetric", "front": "*Front", "back": "*Back",
          "top": "*Top", "bottom": "*Bottom", "left": "*Left", "right": "*Right"}


@tool(READ_ONLY)
def screenshot(
    view: Annotated[Literal["current", "isometric", "trimetric", "dimetric", "front", "back", "top", "bottom", "left", "right"], Field(description="Standard view orientation.")] = "isometric",
    width: Annotated[int, Field(ge=200, le=2400)] = 1000,
    height: Annotated[int, Field(ge=200, le=2400)] = 700,
    zoom_to_fit: bool = True,
    clean: Annotated[bool, Field(description="Hide planes, axes, origins and sketches for the capture (restored afterwards).")] = True,
    save_as: Annotated[str | None, Field(description="Also save the PNG to this path.")] = None,
) -> list:
    """Image of the active document as SolidWorks shows it, to check the model visually."""
    from PIL import Image as PILImage

    a = app()
    doc = SW.active()
    if view != "current" and doc_kind(doc) != "drawing":
        doc.ShowNamedView2(_VIEWS[view], -1)
    if zoom_to_fit:
        doc.ViewZoomtofit2()
    ext = SW.cast(doc.Extension, "IModelDocExtension")
    C = SW.C
    hide_was = None
    if clean and doc_kind(doc) != "drawing":
        try:
            hide_was = ext.GetUserPreferenceToggle(C.swViewDisplayHideAllTypes, 0)
            ext.SetUserPreferenceToggle(C.swViewDisplayHideAllTypes, 0, True)
        except Exception:
            hide_was = None
    doc.GraphicsRedraw2()
    shots = a.settings.workspace / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    bmp = shots / f"_shot_{int(time.time() * 1000)}.bmp"
    try:
        captured = doc.SaveBMP(str(bmp), width, height)
    finally:
        if hide_was is not None:
            ext.SetUserPreferenceToggle(C.swViewDisplayHideAllTypes, 0, hide_was)
            doc.GraphicsRedraw2()
    if not captured or not bmp.exists():
        raise SwError("SolidWorks could not capture the view.")
    try:
        im = PILImage.open(bmp).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
    finally:
        bmp.unlink(missing_ok=True)
    meta: dict[str, Any] = {"document": doc.GetTitle(), "view": view, "size": [im.width, im.height]}
    if save_as:
        p = a.path(save_as, suffixes=(".png",))
        p.write_bytes(buf.getvalue())
        meta["saved"] = str(p)
    return [meta, Image(data=buf.getvalue(), format="png")]

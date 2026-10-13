"""Assembly tools."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW, const_name, doc_kind, mm, rad, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import NATIVE, load_failure

MATES = {"coincident": "swMateCOINCIDENT", "concentric": "swMateCONCENTRIC", "perpendicular": "swMatePERPENDICULAR",
         "parallel": "swMatePARALLEL", "tangent": "swMateTANGENT", "distance": "swMateDISTANCE", "angle": "swMateANGLE", "lock": "swMateLOCK",
         "cam": "swMateCAMFOLLOWER"}
ALIGN = {"aligned": "swMateAlignALIGNED", "anti_aligned": "swMateAlignANTI_ALIGNED", "closest": "swMateAlignCLOSEST"}


@tool(READ_ONLY)
def list_components() -> dict:
    """Components of the active assembly with file, position, fixed/suppressed state and mate status."""
    doc = SW.active("assembly")
    rows = []
    for c in g.components(doc):
        row = {"name": c.Name2, "file": c.GetPathName()}
        try:
            t = list(SW.cast(c.Transform2, "IMathTransform").ArrayData)
            row["origin_mm"] = [round(to_mm(v), 3) for v in t[9:12]]
        except Exception:
            pass
        if c.IsFixed():
            row["fixed"] = True
        if c.IsSuppressed():
            row["suppressed"] = True
        rows.append(row)
    return {"assembly": doc.GetTitle(), "components": rows}


@tool(WRITES)
def insert_component(
    path: Annotated[str, Field(description="Part or sub-assembly file to insert.")],
    position: Annotated[list[float], Field(description="Where the component origin goes, assembly mm [x, y, z]. Orientation is unchanged; use mates to orient and constrain it.")] = [0.0, 0.0, 0.0],  # noqa: B006
    fix: Annotated[bool, Field(description="Fix the component in place (usually the first, base component).")] = False,
) -> dict:
    """Insert a part or sub-assembly into the active assembly."""
    a = app()
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    p = a.path(path, must_exist=True, suffixes=(".sldprt", ".sldasm"))
    C = SW.C
    title = doc.GetTitle()
    # the referenced document has to be loaded before AddComponent5
    res = SW.app.OpenDoc6(str(p), NATIVE[p.suffix.lower()], C.swOpenDocOptions_Silent, "", 0, 0)
    loaded = res[0] if isinstance(res, tuple) else res
    if loaded is None:
        raise SwError(load_failure(p))
    SW.app.ActivateDoc3(title, False, 0, 0)
    x, y, z = (mm(v) for v in (list(position) + [0, 0, 0])[:3])
    comp = asm.AddComponent5(str(p), C.swAddComponentConfigOptions_CurrentSelectedConfig, "", False, "", x, y, z)
    if comp is None:
        raise SwError(f"SolidWorks could not insert {p.name}.")
    comp = SW.cast(comp, "IComponent2")
    # AddComponent5 centres the component's bounding box on the point; move its origin there instead
    # so positions mean the same thing as in the part.
    import pythoncom
    from win32com.client import VARIANT

    arr = list(SW.cast(comp.Transform2, "IMathTransform").ArrayData)
    arr[9:12] = [x, y, z]
    comp.Transform2 = SW.math().CreateTransform(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr))
    doc.EditRebuild3()
    if fix:
        doc.ClearSelection2(True)
        comp.Select4(False, None, False)
        asm.FixComponent()
    elif comp.IsFixed():
        doc.ClearSelection2(True)
        comp.Select4(False, None, False)
        asm.UnfixComponent()
    doc.ClearSelection2(True)
    return {"component": comp.Name2, "file": str(p), "fixed": bool(comp.IsFixed())}


ADVANCED = ("limit_distance", "limit_angle", "width", "symmetric", "gear", "slot", "hinge", "rack_pinion", "screw", "universal_joint")


def _entities(doc, refs: list[str]):
    from .tools_features import _dispatch_array

    return _dispatch_array([g.resolve(doc, r).obj for r in refs])


def _advanced_mate(doc, asm, type: str, refs: list[str], distance: float, angle: float, alignment: str, flip: bool,
                   minimum: float | None, maximum: float | None, plane: str | None, tab: list[str] | None, ratio: list[float] | None,
                   faces: list[str] | None = None, slot_position: str = "free", percent: float | None = None) -> None:
    C = SW.C
    if type in ("limit_distance", "limit_angle"):
        if minimum is None or maximum is None or minimum > maximum:
            raise SwError(f"A {type} mate needs minimum <= maximum.")
        if type == "limit_distance":
            data = SW.cast(asm.CreateMateData(C.swMateDISTANCE), "IDistanceMateFeatureData")
            data.Distance, data.MinimumDistance, data.MaximumDistance = mm(distance), mm(minimum), mm(maximum)
        else:
            data = SW.cast(asm.CreateMateData(C.swMateANGLE), "IAngleMateFeatureData")
            data.Angle, data.MinimumAngle, data.MaximumAngle = rad(angle), rad(minimum), rad(maximum)
        data.IsAdvancedMate = True
        data.FlipDimension = flip
        data.MateAlignment = getattr(C, ALIGN[alignment])
        data.EntitiesToMate = _entities(doc, refs)
    elif type == "width":
        if len(refs) != 2 or not tab or len(tab) not in (1, 2):
            raise SwError("A width mate needs refs = the two width faces of one component and tab = two faces (or one cylinder) of the other.")
        data = SW.cast(asm.CreateMateData(C.swMateWIDTH), "IWidthMateFeatureData")
        data.WidthSelection = _entities(doc, refs)
        data.TabSelection = _entities(doc, tab)
        data.ConstraintType = C.swMateWidth_Centered
    elif type == "symmetric":
        if not plane:
            raise SwError("A symmetric mate needs plane= (the symmetry plane).")
        data = SW.cast(asm.CreateMateData(C.swMateSYMMETRIC), "ISymmetricMateFeatureData")
        data.EntitiesToMate = _entities(doc, refs)
        data.SymmetryPlane = g.resolve(doc, plane).obj
        data.MateAlignment = getattr(C, ALIGN[alignment])
    elif type == "slot":
        data = SW.cast(asm.CreateMateData(C.swMateSLOT), "ISlotMateFeatureData")
        data.EntitiesToMate = _entities(doc, refs)
        data.Constraint = {"free": C.swSlotMateConstraintOption_Free, "center": C.swSlotMateConstraintOption_Centered,
                           "distance": C.swSlotMateConstraintOption_Distance, "percent": C.swSlotMateConstraintOption_Percent}[slot_position]
        if slot_position == "distance":
            data.Distance = mm(distance)
        elif slot_position == "percent":
            data.Percent = float(percent if percent is not None else 50.0) / 100.0
    elif type == "rack_pinion":
        if not distance or distance <= 0:
            raise SwError("A rack and pinion mate needs distance= (the pinion pitch diameter in mm).")
        data = SW.cast(asm.CreateMateData(C.swMateRACKPINION), "IRackPinionMateFeatureData")
        data.SetEntitiesToMate(C.swRackPinionMateEntityType_Rack, g.resolve(doc, refs[0]).obj)
        data.SetEntitiesToMate(C.swRackPinionMateEntityType_Pinion, g.resolve(doc, refs[1]).obj)
        data.DiameterType = C.swPinionPitchDiameter
        data.DiameterVal = mm(distance)
        data.Reverse = flip
    elif type == "screw":
        if not distance or distance <= 0:
            raise SwError("A screw mate needs distance= (the lead: travel per revolution in mm).")
        data = SW.cast(asm.CreateMateData(C.swMateSCREW), "IScrewMateFeatureData")
        data.EntitiesToMate = _entities(doc, refs)
        data.RevolutionType = C.swDistancePerRevolution
        data.RevolutionVal = mm(distance)
        data.Reverse = flip
    elif type == "universal_joint":
        data = SW.cast(asm.CreateMateData(C.swMateUNIVERSALJOINT), "IUniversalJointMateFeatureData")
        data.EntitiesToMate = _entities(doc, refs)
    elif type == "hinge":
        if not faces or len(faces) != 2:
            raise SwError("A hinge mate needs refs = two cylindrical faces (the pivot) and faces = two planar faces that stay in contact.")
        data = SW.cast(asm.CreateMateData(C.swMateHINGE), "IHingeMateFeatureData")
        data.SetEntitiesToMate(C.swHingeMateEntityType_Concentric, _entities(doc, refs))
        data.SetEntitiesToMate(C.swHingeMateEntityType_Coincident, _entities(doc, faces))
    else:  # gear
        if not ratio or len(ratio) != 2 or min(ratio) <= 0:
            raise SwError("A gear mate needs ratio=[teeth or pitch diameter of the first, of the second].")
        data = SW.cast(asm.CreateMateData(C.swMateGEAR), "IGearMateFeatureData")
        data.EntitiesToMate = _entities(doc, refs)
        data.GearRatioNumerator, data.GearRatioDenominator = float(ratio[0]), float(ratio[1])
        data.Reverse = flip
    doc.ClearSelection2(True)
    if asm.CreateMate(data) is None:
        raise SwError(f"SolidWorks could not add the {type} mate. Check that the entities suit this mate type.")
    if type in ("screw", "rack_pinion"):
        # the lead / pitch diameter in the mate data is replaced by a default; it lives in the mate's dimension
        doc.EditRebuild3()
        dim = doc.Parameter(f"D1@{_last_mate(doc)}")
        if dim is not None:
            SW.cast(dim, "IDimension").SetSystemValue3(mm(distance), C.swSetValue_InAllConfigurations, None)


def _segment_points(seg: Any) -> list[list[float]]:
    """Points along a sketch segment from its start to its end (the curve itself is unbounded)."""
    from .tools_features import _doubles

    t = seg.GetType()
    if t == SW.C.swSketchSPLINE:
        pts = SW.cast_all(SW.cast(seg, "ISketchSpline").GetPoints2(), "ISketchPoint")
        a, b = pts[0], pts[-1]
    else:
        x = SW.cast(seg, "ISketchArc" if t == SW.C.swSketchARC else "ISketchLine")
        a, b = SW.cast(x.GetStartPoint2(), "ISketchPoint"), SW.cast(x.GetEndPoint2(), "ISketchPoint")
    a, b = [a.X, a.Y, a.Z], [b.X, b.Y, b.Z]
    tp = list(SW.cast(seg.GetCurve(), "ICurve").GetTessPts(1e-5, 5e-4, _doubles(a), _doubles(b)) or [])
    return [tp[i:i + 3] for i in range(0, len(tp), 3)] or [a, b]


def _chain_spline(doc: Any, segs: list[Any]) -> tuple[Any, float, str]:
    """A 3D sketch spline through a chain of sketch curves, resampled every 0.5 mm; returns it, its largest
    deviation from the chain (mm) and the new sketch's name."""
    from .tools_features import _doubles
    from .tools_sketch import sketch_name

    pieces = [_segment_points(s) for s in segs]
    order = shapes.order_chain([(tuple(p[0]), tuple(p[-1])) for p in pieces], tol=1e-7)
    chain: list[list[float]] = []
    for i, rev in order:
        pts = pieces[i][::-1] if rev else pieces[i]
        chain += pts if not chain else pts[1:]
    dense = [chain[0]]
    for a, b in zip(chain, chain[1:]):
        n = max(1, int(math.dist(a, b) / 0.0005))
        dense += [[a[k] + (b[k] - a[k]) * j / n for k in range(3)] for j in range(1, n + 1)]
    sm = doc.SketchManager
    sm.Insert3DSketch(True)
    sm.AddToDB = True
    try:
        spline = sm.CreateSpline2(_doubles([c for p in dense for c in p]), True)
    finally:
        sm.AddToDB = False
    name = sketch_name(doc, SW.cast(sm.ActiveSketch, "ISketch"))
    sm.Insert3DSketch(True)
    if spline is None:
        raise SwError("Could not fit a spline along the path.")
    spline = SW.cast(spline, "ISketchSegment")
    curve = SW.cast(spline.GetCurve(), "ICurve")
    dev = max(math.dist(p, list(curve.GetClosestPointOn(*p))[:3]) for q in pieces for p in q)
    return spline, to_mm(dev), name


def _path_mate(doc, asm, refs: list[str], orientation: str = "free") -> dict:
    """A path mate as a point-on-curve coincidence: SolidWorks' own path mate has no API (no path mate data in the
    type library, and AddMate5 makes nothing), but a vertex coincident with a curve follows it the same way."""
    import re

    m = re.match(r"^vertex@\s*([^/]+)/\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", refs[0].strip())
    if not m:
        raise SwError("A path mate needs refs[0] = 'vertex@Comp-1/x,y,z' (the component corner nearest that point).")
    comp = g.find_component(doc, m.group(1))
    xf = g.Placement(comp)
    target = [mm(float(m.group(k))) for k in (2, 3, 4)]
    verts = [v for b in SW.cast_all(comp.GetBodies2(0), "IBody2") for v in SW.cast_all(b.GetVertices(), "IVertex")]
    if not verts:
        raise SwError(f"{m.group(1)} has no vertices to put on the path.")
    vtx = min(verts, key=lambda v: math.dist(xf.point(v.GetPoint()), target))
    path = refs[1].strip()
    out_note: dict[str, Any] = {}
    doc.ClearSelection2(True)
    sel = SW.cast(doc.SelectionManager, "ISelectionMgr")
    data = SW.cast(sel.CreateSelectData(), "ISelectData")
    data.Mark = 1
    SW.cast(vtx, "IEntity").Select4(True, data)
    if path.startswith("edge"):
        g.select(doc, path, mark=1)
    else:
        f = g.feature_by_name(doc, path.removeprefix("sketch:"))
        if f is None or f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
            raise SwError(f"{path} is not a sketch of the assembly.")
        segs = [x for x in SW.cast_all(SW.cast(f.GetSpecificFeature2(), "ISketch").GetSketchSegments(), "ISketchSegment") if not x.ConstructionGeometry]
        if not segs:
            raise SwError(f"{path} holds no curves.")
        if len(segs) > 1:  # a chain: one spline fitted closely through it, since a point-on-curve mate takes one curve
            doc.ClearSelection2(True)
            curve, deviation, fitted = _chain_spline(doc, segs)
            out_note = {"path_fitted": fitted, "fit_deviation_mm": round(deviation, 5)}
            SW.cast(vtx, "IEntity").Select4(True, data)
            segs = [curve]
        data2 = SW.cast(sel.CreateSelectData(), "ISelectData")
        data2.Mark = 1
        segs[0].Select4(True, data2)
    C = SW.C
    res = asm.AddMate5(C.swMateCOINCIDENT, C.swMateAlignCLOSEST, False, 0, 0, 0, 0.001, 0.001, 0, 0, 0, False, False, 0, 0)
    mate, err = (res if isinstance(res, tuple) else (res, 0))
    doc.ClearSelection2(True)
    if mate is None:
        raise SwError(f"SolidWorks could not put the vertex on the path ({const_name('swAddMateError_', err)}).")
    doc.EditRebuild3()
    out: dict[str, Any] = {"mate": _last_mate(doc), "type": "path"}
    out.update(out_note)
    if orientation == "fixed":
        # lock pitch, yaw and roll: two planar faces of the component parallel to the assembly planes they face now
        planes = {0: "plane:Right", 1: "plane:Top", 2: "plane:Front"}
        locked, used = [], set()
        place = g.Placement(comp)
        for b in SW.cast_all(comp.GetBodies2(0), "IBody2"):
            for f in SW.cast_all(b.GetFaces(), "IFace2"):
                if len(locked) == 2 or not SW.cast(f.GetSurface(), "ISurface").IsPlane():
                    continue
                n = place.direction(f.Normal)
                axis = max(range(3), key=lambda i: abs(n[i]))
                if abs(abs(n[axis]) - 1) > 1e-6 or axis in used:
                    continue
                box = f.GetBox
                box = box() if callable(box) else box
                pt = place.point(list(f.GetClosestPointOn(*[(box[i] + box[i + 3]) / 2 for i in range(3)]))[:3])
                doc.ClearSelection2(True)
                g.Ref(f, "face", "face").select(doc, mark=1)
                g.select(doc, planes[axis], mark=1)
                res = asm.AddMate5(C.swMatePARALLEL, C.swMateAlignCLOSEST, False, 0, 0, 0, 0.001, 0.001, 0, 0, 0, False, False, 0, 0)
                doc.ClearSelection2(True)
                if (res[0] if isinstance(res, tuple) else res) is None:
                    raise SwError(f"Could not lock the orientation of {m.group(1)} (a face at {[round(to_mm(c), 2) for c in pt]}).")
                used.add(axis)
                locked.append(_last_mate(doc))
        if len(locked) < 2:
            raise SwError(f"{m.group(1)} needs two flat faces square to the assembly axes to lock its orientation.")
        doc.EditRebuild3()
        out["orientation_mates"] = locked
    at = g.Placement(comp).point(vtx.GetPoint())  # the component has moved onto the path
    out["vertex_at_mm"] = [round(to_mm(c), 4) for c in at]
    return out


def _check_cam(doc, ref: str) -> None:
    """A cam mate on a single closed face (a plain or eccentric cylinder) crashes SolidWorks, so refuse it."""
    face = g.resolve(doc, ref).obj
    if not hasattr(face, "GetEdges"):
        raise SwError("The cam (refs[0]) must be a face of the cam profile.")
    edges = SW.cast_all(face.GetEdges(), "IEdge")
    if edges and all(SW.cast(e.GetCurve(), "ICurve").IsCircle() and e.GetStartVertex() is None for e in edges):
        raise SwError("A cam needs a profile of several tangent faces (lines and arcs); for a round or eccentric disc use a tangent mate.")


@tool(WRITES)
def add_mate(
    type: Annotated[Literal["coincident", "concentric", "perpendicular", "parallel", "tangent", "distance", "angle", "lock",
                            "limit_distance", "limit_angle", "width", "symmetric", "gear", "slot", "hinge", "rack_pinion", "screw",
                            "universal_joint", "cam", "path"], Field(description=(
        "Mate type. limit_distance / limit_angle allow travel between minimum and maximum (control surface stops, slides); "
        "width centres a tab between two faces; symmetric places two entities symmetric about plane; gear couples rotation "
        "of two cylindrical faces or circular edges by ratio; slot keeps a pin (cylinder or axis) in a slot face; hinge is a "
        "concentric pivot (refs) plus coincident faces (faces) in one mate; rack_pinion couples a linear edge (refs[0]) "
        "to a rotating cylinder (refs[1]) with distance = pitch diameter; screw couples rotation and travel of two "
        "cylindrical faces with distance = lead per revolution (jackscrews, actuators); universal_joint couples two shafts; "
        "cam keeps a follower (refs[1]: a face, edge or vertex) on a cam (refs[0]: any face of the cam's closed chain of "
        "tangent faces, such as an extruded lobe of lines and arcs); path keeps a component vertex (refs[0]: 'vertex@Comp-1/x,y,z', "
        "the corner nearest that point) on a curve (refs[1]: an assembly sketch holding one line, arc or spline, or an edge), "
        "for carriages, cable runs and flap tracks."))],
    refs: Annotated[list[str], Field(description="Two references on different components: 'face:Comp-1/N', 'edge:Comp-1/N', 'face@Comp-1/x,y,z', plane names, or 'comp:Name' for lock. Width: the two width faces.")],
    distance: Annotated[float, Field(description="Distance in mm (distance mates; nominal position for limit_distance).")] = 0.0,
    angle: Annotated[float, Field(description="Angle in degrees (angle mates; nominal position for limit_angle).")] = 0.0,
    alignment: Annotated[Literal["aligned", "anti_aligned", "closest"], Field(description="Normal alignment.")] = "closest",
    flip: Annotated[bool, Field(description="Flip the dimension side (distance/angle mates) or reverse a gear.")] = False,
    minimum: Annotated[float | None, Field(description="limit_distance (mm) / limit_angle (deg): lower limit.")] = None,
    maximum: Annotated[float | None, Field(description="limit_distance (mm) / limit_angle (deg): upper limit.")] = None,
    plane: Annotated[str | None, Field(description="symmetric: the symmetry plane.")] = None,
    tab: Annotated[list[str] | None, Field(description="width: the tab's two faces (or one cylindrical face) on the other component.")] = None,
    ratio: Annotated[list[float] | None, Field(description="gear: [first, second] teeth counts or pitch diameters.")] = None,
    faces: Annotated[list[str] | None, Field(description="hinge: the two planar faces that stay in contact.")] = None,
    slot_position: Annotated[Literal["free", "center", "distance", "percent"], Field(description="slot: free along the slot, centred, at distance (mm from the slot end) or at percent.")] = "free",
    percent: Annotated[float | None, Field(description="slot: position along the slot in percent (slot_position='percent').")] = None,
    path_orientation: Annotated[Literal["free", "fixed"], Field(description=(
        "path: free lets the component turn as it moves; fixed keeps its orientation (pitch, yaw and roll locked) with "
        "parallel mates to the assembly planes, as for a carriage that translates along a track."))] = "free",
) -> dict:
    """Mate two entities of different components, including limit, width, symmetric and gear mates."""
    if len(refs) != 2:
        raise SwError("A mate needs exactly two references.")
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    C = SW.C
    if type in ADVANCED:
        _advanced_mate(doc, asm, type, refs, distance, angle, alignment, flip, minimum, maximum, plane, tab, ratio, faces, slot_position, percent)
        doc.EditRebuild3()
        return {"mate": _last_mate(doc), "type": type}
    if type == "path":
        return _path_mate(doc, asm, refs, path_orientation)
    if type == "cam":
        _check_cam(doc, refs[0])
    doc.ClearSelection2(True)
    for r in refs:
        g.select(doc, r, mark=1)
    d, ang = mm(distance), rad(angle)
    res = asm.AddMate5(getattr(C, MATES[type]), getattr(C, ALIGN[alignment]), flip, d, d, d, 0.001, 0.001, ang, ang, ang, False, False, 0, 0)
    mate, err = (res if isinstance(res, tuple) else (res, 0))
    doc.ClearSelection2(True)
    if mate is None:
        raise SwError(f"SolidWorks could not add the {type} mate ({const_name('swAddMateError_', err)}). Check that the entities suit this mate type.")
    doc.EditRebuild3()
    return {"mate": _last_mate(doc), "type": type}


_AXES = {"x": 0, "y": 1, "z": 2}
_ROT_AXES = {"x": 1, "y": 2, "z": 0}  # rotation ring index of the explode triad for each assembly axis (measured)


@tool(WRITES)
def exploded_view(
    steps: Annotated[list[dict[str, Any]] | None, Field(description=(
        "Explode steps in order: {components: ['Bracket-1', ...], direction: 'x' | 'y' | 'z' | '-x' | '-y' | '-z' "
        "(assembly axes), distance: mm, rotate: degrees (optional), axis: 'x' | 'y' | 'z' (rotation axis, default the "
        "direction axis)}. direction may also be a reference: along an edge or axis ('edge@Comp-1/x,y,z', from its start "
        "to its end) or square to a planar face ('face@Comp-1/x,y,z', along its normal); a negative distance reverses it "
        "(the result reports how far each component moved). A step may only rotate (distance 0). "
        "{components, radial: true, distance} moves a ring of parts "
        "(bolts around a flange, fasteners around a bulkhead) straight out from their common centre axis. Omit steps to "
        "just show or hide the exploded view."))] = None,
    show: Annotated[bool, Field(description="Show the assembly exploded (true) or collapsed (false) afterwards.")] = True,
) -> dict:
    """Exploded view of the active assembly for assembly instructions, manuals and drawings (an isometric
    drawing view of the assembly shows it once the view is set to exploded in SolidWorks)."""
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    cfg = SW.cast(doc.ConfigurationManager.ActiveConfiguration, "IConfiguration")
    done = []
    moved_names = sorted({c for st in steps or [] for c in st.get("components") or []})
    if steps:
        if not list(asm.GetExplodedViewNames() or []):
            asm.ShowExploded2(False, "")
            if not asm.CreateExplodedView():
                raise SwError("SolidWorks could not create an exploded view.")
            # a new exploded view starts with an automatic chain step that moves every component; drop it
            for k in reversed(range(cfg.GetNumberOfExplodeSteps())):
                cfg.DeleteExplodeStep(SW.cast(cfg.GetExplodeStep(k), "IExplodeStep").Name)
        for i, st in enumerate(steps):
            comps = st.get("components") or []
            dist = float(st.get("distance", 0))
            angle = float(st.get("rotate", 0))
            if not comps or not (dist or angle):
                raise SwError(f"steps[{i}] needs components and a distance or a rotation.")
            if st.get("radial"):
                if len(comps) < 2 or dist <= 0:
                    raise SwError(f"steps[{i}]: a radial step needs two or more components and a distance above 0.")
                doc.ClearSelection2(True)
                for c in comps:
                    g.select(doc, c if c.startswith("comp:") else f"comp:{c}", mark=1)
                # SolidWorks takes the axis through the centre of the ring, square to its plane
                res = cfg.AddRadialExplodeStep(mm(dist), 0.0, False)
                step, err = (res if isinstance(res, tuple) else (res, 0))
                doc.ClearSelection2(True)
                if step is None:
                    raise SwError(f"steps[{i}]: SolidWorks could not add the radial step ({const_name('swCreateExplodeStepError_', err)}).")
                done.append({"components": comps, "radial": True, "distance": dist})
                continue
            direction = str(st.get("direction", "y")).strip()
            if direction.lstrip("-+").lower() not in _AXES:
                if "@" not in direction and ":" not in direction:
                    raise SwError(f"steps[{i}]: direction must be x, y, z, -x, -y, -z, or an edge, axis or planar face reference.")
                if angle:
                    raise SwError(f"steps[{i}]: a step along a reference only moves; rotate in a step of its own about x, y or z.")
                if not dist:
                    raise SwError(f"steps[{i}] needs a distance.")
                ref = g.resolve(doc, direction)
                doc.ClearSelection2(True)
                for c in comps:
                    g.select(doc, c if c.startswith("comp:") else f"comp:{c}", mark=1)
                res = cfg.AddExplodeStep2(mm(abs(dist)), 0, False, 0.0, 0, False, False, False)
                step, err = (res if isinstance(res, tuple) else (res, 0))
                doc.ClearSelection2(True)
                if step is None:
                    raise SwError(f"steps[{i}]: SolidWorks could not add the explode step ({const_name('swCreateExplodeStepError_', err)}).")
                # the reference sets the step triad's Z axis (index 2): along an edge, or a face's normal
                step = SW.cast(step, "IExplodeStep")
                step.SetExplodeDirection(ref.obj, 2)
                step.ReverseTranslationDirection = dist < 0
                done.append({"components": comps, "direction": direction, "distance": dist})
                continue
            direction = direction.lower()
            reverse = direction.startswith("-")
            doc.ClearSelection2(True)
            for c in comps:
                g.select(doc, c if c.startswith("comp:") else f"comp:{c}", mark=1)
            # the direction index picks an axis of the move triad, which starts aligned with the assembly axes
            rot_axis = str(st.get("axis", direction.lstrip("-+"))).lower()
            if rot_axis not in _ROT_AXES:
                raise SwError(f"steps[{i}]: axis must be x, y or z.")
            res = cfg.AddExplodeStep2(mm(abs(dist)), _AXES[direction.lstrip("-+")], reverse != (dist < 0), math.radians(abs(angle)),
                                      _ROT_AXES[rot_axis], angle < 0, False, False)
            step, err = (res if isinstance(res, tuple) else (res, 0))
            doc.ClearSelection2(True)
            if step is None:
                raise SwError(f"steps[{i}]: SolidWorks could not add the explode step ({const_name('swCreateExplodeStepError_', err)}).")
            done.append({"components": comps, "direction": direction, "distance": dist, **({"rotate": angle, "axis": rot_axis} if angle else {})})
    names = list(asm.GetExplodedViewNames() or [])
    if not names:
        raise SwError("The assembly has no exploded view yet; give steps.")
    out: dict[str, Any] = {"exploded_view": names[0], "steps_added": done, "exploded": show}
    if moved_names:  # how far each component of these steps sits from its assembled position
        def origins() -> dict[str, list[float]]:
            return {n: _pose(g.find_component(doc, n))[1] for n in moved_names}

        asm.ShowExploded2(False, names[0])
        doc.EditRebuild3()
        home = origins()
        asm.ShowExploded2(True, names[0])
        doc.EditRebuild3()
        away = origins()
        out["moved_mm"] = {n: [round(to_mm(a - b), 3) + 0.0 for a, b in zip(away[n], home[n])] for n in moved_names}
    asm.ShowExploded2(show, names[0])
    doc.EditRebuild3()
    return out


def _last_mate(doc) -> str | None:
    """Name of the newest mate (AddMate5 returns an IMate2, which carries no feature name)."""
    name = None
    for f in g.iter_features(doc):
        if f.GetTypeName2() == "MateGroup":
            sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
            while sub is not None:
                name = sub.Name
                sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
    return name


def _prop(model, name: str) -> str:
    try:
        cpm = SW.cast(model.Extension.CustomPropertyManager(""), "ICustomPropertyManager")
        r = cpm.Get6(name, False)
        return (r[2] or r[1] or "").strip()
    except Exception:
        return ""


@tool(READ_ONLY)
def bom(
    top_level_only: Annotated[bool, Field(description="Only direct children of the assembly (sub-assemblies count as one line).")] = True,
) -> dict:
    """Bill of materials with a mass budget: quantity, part number, description, material and mass of
    each part in the active assembly, plus totals."""
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    rows: dict[str, dict] = {}
    for c in SW.cast_all(asm.GetComponents(top_level_only), "IComponent2"):
        if c.IsSuppressed():
            continue
        path = c.GetPathName()
        if path in rows:
            rows[path]["qty"] += 1
            continue
        model = c.GetModelDoc2()
        row: dict = {"qty": 1, "file": path.rsplit("\\", 1)[-1]}
        if model is not None:
            model = SW.cast(model, "IModelDoc2")
            row["part_no"] = _prop(model, "PartNo") or _prop(model, "PartNumber") or None
            row["description"] = _prop(model, "Description") or None
            if model.GetType() == 1:
                m = SW.part(model).GetMaterialPropertyName2("")
                row["material"] = (m[0] if isinstance(m, tuple) else m) or None
            try:
                mp = SW.cast(model.Extension.CreateMassProperty2(), "IMassProperty2")
                row["unit_mass_g"] = round(mp.Mass * 1000, 2)
            except Exception:
                pass
        rows[path] = row
    lines = sorted(rows.values(), key=lambda r: (-(r.get("unit_mass_g") or 0) * r["qty"]))
    for i, r in enumerate(lines, 1):
        r["item"] = i
        if r.get("unit_mass_g") is not None:
            r["total_mass_g"] = round(r["unit_mass_g"] * r["qty"], 2)
    missing = [r["file"] for r in lines if not r.get("material")]
    out = {"assembly": doc.GetTitle(), "lines": lines, "parts": sum(r["qty"] for r in lines),
           "total_mass_g": round(sum(r.get("total_mass_g", 0) for r in lines), 2)}
    if missing:
        out["warning"] = f"No material on {missing}: their mass uses a default density."
    return out


@tool(READ_ONLY)
def interference_check(
    coincident_as_interference: Annotated[bool, Field(description="Report touching faces as interference.")] = False,
) -> dict:
    """Find interfering components in the active assembly, with the interference volume."""
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    mgr = SW.cast(asm.InterferenceDetectionManager, "IInterferenceDetectionMgr")
    mgr.TreatCoincidenceAsInterference = coincident_as_interference
    mgr.TreatSubAssembliesAsComponents = True
    try:
        items = mgr.GetInterferences() or []
        rows = []
        for it in items:
            it = SW.cast(it, "IInterference")
            comps = [SW.cast(c, "IComponent2").Name2 for c in (it.Components or [])]
            rows.append({"components": comps, "volume_mm3": round(it.Volume * 1e9, 4)})
    finally:
        mgr.Done()
    return {"interferences": len(rows), "items": rows}


def _boxes(doc) -> list[tuple[float, ...]]:
    """Sorted world bounding boxes of every part instance, to check a restructure moved nothing."""
    out = []
    for c in g.components(doc):
        if SW.cast(c.GetModelDoc2(), "IModelDoc2") is not None and doc_kind(SW.cast(c.GetModelDoc2(), "IModelDoc2")) == "part":
            box = c.GetBox(False, False)
            if box:
                out.append(tuple(round(v * 1000, 3) for v in list(box)[:6]))
    return sorted(out)


@tool(WRITES)
def subassembly(
    action: Annotated[Literal["form", "dissolve", "save_out"], Field(description=(
        "form: move components into a new sub-assembly (stored inside the assembly until saved out); "
        "dissolve: move a sub-assembly's components up into its parent and remove it; "
        "save_out: save a virtual (in-assembly) sub-assembly or part to its own file, which the assembly then references."))],
    components: Annotated[list[str] | None, Field(description="form: the components to group, e.g. ['Bolt-1', 'Nut-1'].")] = None,
    name: Annotated[str | None, Field(description=(
        "form: name of the new sub-assembly (e.g. 'Hinge-Kit'). dissolve, save_out: the sub-assembly (or virtual part).") )] = None,
    path: Annotated[str | None, Field(description="save_out: file to save to (.sldasm, or .sldprt for a virtual part); default <name>.sldasm in the workspace.")] = None,
) -> dict:
    """Restructure an assembly's tree for the build and the indented BOM: group components into a new
    sub-assembly (a kit, an installation), dissolve one, or save a virtual one out to its own file for release.
    Components keep their positions."""
    from .tools_features import _dispatch_array

    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    before = _boxes(doc)
    if action == "form":
        if not components:
            raise SwError("Give the components to group.")
        comps = [g.find_component(doc, c) for c in components]
        existing = {c.Name2 for c in g.components(doc)}
        doc.ClearSelection2(True)
        asm.InsertNewAssembly("")  # an empty virtual sub-assembly, saved inside this assembly
        new = [c for c in g.components(doc) if c.Name2 not in existing]
        if len(new) != 1:
            raise SwError("SolidWorks could not create the sub-assembly.")
        sub = new[0]
        if not asm.ReorganizeComponents(_dispatch_array(comps), sub):
            doc.ClearSelection2(True)
            sub.Select4(False, None, False)
            doc.Extension.DeleteSelection2(SW.C.swDelete_Absorbed)
            raise SwError("SolidWorks could not move the components into the new sub-assembly (they must be top-level components).")
        if name:
            sub.Name2 = name
        doc.EditRebuild3()
        label = sub.Name2
    else:
        if not name:
            raise SwError(f"Give the sub-assembly to {action.replace('_', ' ')} (name=).")
        # a virtual sub-assembly is named 'Name^Parent-1'; accept the plain name too
        sub = next((c for c in g.components(doc) if "/" not in c.Name2 and c.Name2.split("^")[0] == name), None) or g.find_component(doc, name)
        label = sub.Name2
    if action == "save_out":
        if not sub.IsVirtual:
            raise SwError(f"{label} is already saved in its own file ({sub.GetPathName()}).")
        suffix = ".sldprt" if sub.GetPathName().lower().endswith(".sldprt") else ".sldasm"
        target = app().path(path or f"{label.split('^')[0]}{suffix}", suffixes=(suffix,))
        if target.exists():
            raise SwError(f"{target.name} already exists; give another path.")
        if not sub.SaveVirtualComponent(str(target)) or not target.exists():
            raise SwError(f"SolidWorks could not save {label} to {target}.")
        doc.EditRebuild3()
        saved = next((c for c in g.components(doc) if not c.IsVirtual and Path(c.GetPathName()).resolve() == target.resolve()), None)
        if saved is None:
            raise SwError(f"{target.name} was written but the assembly does not reference it.")
        if _boxes(doc) != before:
            raise SwError("Saving out moved components; undo it in SolidWorks.")
        return {"action": action, "subassembly": saved.Name2, "file": str(target),
                "components": [c.Name2 for c in g.components(doc)], "note": "Save the parent assembly to keep the new reference."}
    if action == "dissolve":
        doc.ClearSelection2(True)
        sub.Select4(False, None, False)
        if not asm.DissolveSubAssembly():
            doc.ClearSelection2(True)
            raise SwError(f"SolidWorks could not dissolve {name}.")
        doc.EditRebuild3()
    doc.ClearSelection2(True)
    after = _boxes(doc)
    if after != before:
        raise SwError("The restructure moved components; undo it in SolidWorks and check the mates of the moved components.")
    return {"action": action, "subassembly": label, "components": [c.Name2 for c in g.components(doc)]}


def _pose(comp: Any) -> tuple[list[list[float]], list[float]]:
    arr = list(SW.cast(comp.Transform2, "IMathTransform").ArrayData)
    return [arr[0:3], arr[3:6], arr[6:9]], arr[9:12]


def _relative_rotation(r0: list[list[float]], r1: list[list[float]]) -> tuple[float, list[float]]:
    """Angle (deg) and axis of the rotation taking orientation r0 to r1 (rows are the rotated axes)."""
    m = [[sum(r1[k][i] * r0[k][j] for k in range(3)) for j in range(3)] for i in range(3)]  # r1^T r0 in row form
    cos = max(-1.0, min(1.0, (m[0][0] + m[1][1] + m[2][2] - 1) / 2))
    ang = math.degrees(math.acos(cos))
    axis = [m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1]]
    n = math.sqrt(sum(a * a for a in axis))
    return ang, ([round(a / n, 4) for a in axis] if n > 1e-9 else [0.0, 0.0, 0.0])


@tool(WRITES)
def motion_study(
    motors: Annotated[list[dict[str, Any]], Field(min_length=1, description=(
        "Motors: {face: 'face@Comp-1/x,y,z' (a cylindrical face for rotary, a planar face for linear), kind: 'rotary' | 'linear', "
        "rpm (rotary) or speed (linear, mm/s), reverse}."))],
    duration: Annotated[float, Field(gt=0, description="Study length in seconds.")] = 1.0,
    times: Annotated[list[float] | None, Field(description="Times (s) to report the components at; default the end.")] = None,
) -> dict:
    """Motion study of the active assembly driven by motors (mechanisms, actuators, control surface linkages):
    calculates it and reports how far every component has moved and turned at the given times, to check travel,
    ratios and clearances. The study stays in the assembly for playback and video."""
    doc = SW.active("assembly")
    C = SW.C
    comps = [c for c in g.components(doc) if "/" not in c.Name2]
    start = {c.Name2: _pose(c) for c in comps}
    mgr = SW.cast(doc.Extension.GetMotionStudyManager(), "IMotionStudyManager")
    for i, m in enumerate(motors):
        kind = str(m.get("kind", "rotary")).lower()
        if kind not in ("rotary", "linear") or not m.get("face"):
            raise SwError(f"motors[{i}] needs face and kind 'rotary' or 'linear'.")
        if not m.get("rpm" if kind == "rotary" else "speed"):
            raise SwError(f"motors[{i}]: a {kind} motor needs {'rpm' if kind == 'rotary' else 'speed (mm/s)'}.")
    import time as _time

    def build(length: float) -> Any:
        st = SW.cast(mgr.CreateMotionStudy(), "IMotionStudy")
        if st is None:
            raise SwError("SolidWorks could not create a motion study.")
        st.Activate()  # motors only go on the active study
        # Basic Motion: a time-stepped solve that drives the motors at constant speed (an animation study eases
        # in and out, so its positions do not follow the motor speed)
        st.StudyType = C.swMotionStudyTypePhysicalSimulation
        st.SetDuration(float(length))
        for i, m in enumerate(motors):
            kind = str(m.get("kind", "rotary")).lower()
            md = SW.cast(st.CreateDefinition(C.swFmAEMRotationalMotor if kind == "rotary" else C.swFmAEMLinearMotor), "ISimulationMotorFeatureData")
            face = g.resolve(doc, m["face"]).obj
            md.Location = face
            md.DirectionReference = face
            md.ReverseDirection = bool(m.get("reverse", False))
            md.ConstantSpeedMotor(float(m["rpm"]) * 2 * math.pi / 60 if kind == "rotary" else mm(float(m["speed"])))  # rad/s or m/s
            if st.CreateFeature(md) is None:
                raise SwError(f"motors[{i}]: SolidWorks could not add the motor on {m['face']} (rotary needs a cylindrical or circular "
                              "reference, linear a planar face or straight edge).")
        return st

    def solve(st: Any, length: float) -> float:
        """Solve to `length` and return the time it stopped at (the solver may end on its own step). Calculate
        returns at once and solves in the background, so this waits until it has stopped and stayed stopped."""
        st.SetDuration(float(length))
        if not st.Calculate():
            raise SwError("The motion study did not calculate; check the mates (an over-constrained or unmated assembly fails).")
        t0 = _time.time()
        last, steady = None, 0
        while steady < 10:
            if _time.time() - t0 > max(120.0, 30 * length):
                raise SwError("The motion study did not finish calculating in time.")
            _time.sleep(0.15)
            now = st.GetTime()
            steady = steady + 1 if (now == last and now > 0 and not st.IsPlaying) else 0
            last = now
        return float(last)

    rows = []
    st = build(duration)
    # each time is the end state of a solve to that time: Basic Motion stores coarse frames, and scrubbing to a
    # time in between shows the frame before it
    for t in sorted(times or [duration]):
        if not 0 < t <= duration:
            raise SwError(f"Time {t} is outside the study (0 to {duration} s].")
        solved = solve(st, t)
        doc.GraphicsRedraw2()
        at: dict[str, Any] = {}
        for c in comps:
            r0, p0 = start[c.Name2]
            r1, p1 = _pose(c)
            ang, axis = _relative_rotation(r0, r1)
            move = [round(to_mm(p1[k] - p0[k]), 4) for k in range(3)]
            if ang > 1e-3 or any(abs(v) > 1e-4 for v in move):
                at[c.Name2] = {"moved_mm": move, "turned_deg": round(ang, 3), "axis": axis}
        rows.append({"time_s": t, **({"solved_time_s": round(solved, 6)} if abs(solved - t) > 1e-6 else {}), "components": at})
    if abs(st.GetTime() - duration) > 1e-6:
        solve(st, duration)  # leave the whole study calculated for playback and video
    st.SetTime(0.0)  # put the components back where they started, so the model reads as before
    # back to the Model tab: left on the study, later mates and edits would go into the study, not the model
    mgr.ActivateMotionStudy("Model")
    doc.GraphicsRedraw2()
    back = [c.Name2 for c in comps if max(abs(a - b) for a, b in zip(_pose(c)[1], start[c.Name2][1])) > 1e-7]
    out = {"study": st.Name, "duration_s": duration, "results": rows}
    if back:
        out["warning"] = f"{', '.join(back)} did not return to the start; check list_components before editing further."
    return out

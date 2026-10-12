"""Assembly tools."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, const_name, mm, rad, to_mm
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
                            "universal_joint", "cam"], Field(description=(
        "Mate type. limit_distance / limit_angle allow travel between minimum and maximum (control surface stops, slides); "
        "width centres a tab between two faces; symmetric places two entities symmetric about plane; gear couples rotation "
        "of two cylindrical faces or circular edges by ratio; slot keeps a pin (cylinder or axis) in a slot face; hinge is a "
        "concentric pivot (refs) plus coincident faces (faces) in one mate; rack_pinion couples a linear edge (refs[0]) "
        "to a rotating cylinder (refs[1]) with distance = pitch diameter; screw couples rotation and travel of two "
        "cylindrical faces with distance = lead per revolution (jackscrews, actuators); universal_joint couples two shafts; "
        "cam keeps a follower (refs[1]: a face, edge or vertex) on a cam (refs[0]: any face of the cam's closed chain of "
        "tangent faces, such as an extruded lobe of lines and arcs)."))],
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
        "direction axis)}. A step may only rotate (distance 0). {components, radial: true, distance} moves a ring of parts "
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
            direction = str(st.get("direction", "y")).strip().lower()
            reverse = direction.startswith("-")
            if direction.lstrip("-+") not in _AXES:
                raise SwError(f"steps[{i}]: direction must be x, y, z, -x, -y or -z.")
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
    asm.ShowExploded2(show, names[0])
    doc.EditRebuild3()
    return {"exploded_view": names[0], "steps_added": done, "exploded": show}


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

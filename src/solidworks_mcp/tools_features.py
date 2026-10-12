"""Feature tools: boss/cut features, dress-up features, holes, patterns and reference geometry."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, const_name, mm, rad, to_mm
from .errors import SwError
from .registry import WRITES, tool
from .tools_sketch import SketchFrame, _doubles, last_sketch

END = {"blind": "swEndCondBlind", "through_all": "swEndCondThroughAll", "through_all_both": "swEndCondThroughAllBoth",
       "midplane": "swEndCondMidPlane", "up_to_next": "swEndCondUpToNext"}

Refs = Annotated[list[str], Field(description="Entity references: 'face:N', 'edge:N' (from list_faces/list_edges), 'face@x,y,z', 'edge@x,y,z', or feature/plane/axis names.")]


# ------------------------------------------------------------------------------------------ helpers


def feature_error(f: Any) -> str | None:
    try:
        r = f.GetErrorCode2()
        code, warn = (r if isinstance(r, tuple) else (r, False))
    except Exception:
        return None
    if not code:
        return None
    return f"{'warning' if warn else 'error'}: {const_name('swFeatureError', code)}"


def _last_feature(doc: Any) -> Any:
    """Newest feature in the tree. Sheet metal parts keep their flat pattern last, so it is skipped."""
    for i in range(3):
        f = doc.FeatureByPositionReverse(i)
        if f is None:
            return None
        f = SW.cast(f, "IFeature")
        if f.GetTypeName2() != "FlatPattern":
            return f
    return None


def _volume(doc: Any) -> float | None:
    try:
        mp = SW.cast(doc.Extension.CreateMassProperty2(), "IMassProperty2")
        return round(mp.Volume * 1e9, 3)
    except Exception:
        return None


def result(doc: Any, feat: Any, what: str, hint: str = "") -> dict:
    doc.ClearSelection2(True)
    if feat is None:
        raise SwError(f"SolidWorks could not create the {what}." + (f" {hint}" if hint else ""))
    f = SW.cast(feat, "IFeature")
    out: dict[str, Any] = {"feature": f.Name, "type": f.GetTypeName2()}
    if err := feature_error(f):
        out["rebuild"] = err
    if (v := _volume(doc)) is not None:
        out["volume_mm3"] = v
    return out


def created_since(doc: Any, before: str | None, what: str, hint: str = "") -> dict:
    """For API calls that do not return the feature: compare the last tree item before and after."""
    f = _last_feature(doc)
    if f is None or f.Name == before:
        doc.ClearSelection2(True)
        raise SwError(f"SolidWorks could not create the {what}." + (f" {hint}" if hint else ""))
    return result(doc, f, what)


def _sketch_ref(doc: Any, sketch: str | None) -> str:
    return sketch or last_sketch(doc)


def _select_sketch(doc: Any, sketch: str | None, mark: int = 0) -> str:
    name = _sketch_ref(doc, sketch)
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    g.select(doc, name if ":" in name else f"sketch:{name}", mark=mark)
    return name


def _edges_from(doc: Any, edges: list[str] | None, faces: list[str] | None) -> list[Any]:
    out: list[Any] = []
    for r in edges or []:
        ref = g.resolve(doc, r)
        if ref.kind != "edge":
            raise SwError(f"{r} is not an edge.")
        out.append(ref.obj)
    for r in faces or []:
        ref = g.resolve(doc, r)
        if ref.kind != "face":
            raise SwError(f"{r} is not a face.")
        out += SW.cast_all(ref.obj.GetEdges(), "IEdge")
    if not out:
        raise SwError("Give at least one edge or face.")
    return out


# ------------------------------------------------------------------------------------------ boss / cut


@tool(WRITES)
def extrude(
    depth: Annotated[float, Field(description="Depth in mm (total depth for 'midplane'; ignored for through-all ends).")] = 10.0,
    sketch: Annotated[str | None, Field(description="Sketch name; default is the latest sketch.")] = None,
    cut: Annotated[bool, Field(description="Remove material instead of adding it.")] = False,
    end: Annotated[Literal["blind", "through_all", "through_all_both", "midplane", "up_to_next"], Field(description="End condition.")] = "blind",
    reverse: Annotated[bool, Field(description="Extrude to the other side of the sketch plane.")] = False,
    depth2: Annotated[float | None, Field(description="Also extrude this far in the opposite direction (blind).")] = None,
    draft: Annotated[float, Field(description="Draft angle in degrees (0 for none).")] = 0.0,
    draft_outward: bool = False,
    merge: Annotated[bool, Field(description="Merge with existing bodies (bosses only).")] = True,
) -> dict:
    """Extruded boss or cut from a closed sketch profile. A cut that finds no material in the
    requested direction is retried once in the opposite direction (reported in 'note')."""
    doc = SW.active("part")
    C = SW.C
    name = _select_sketch(doc, sketch)
    fm = doc.FeatureManager
    if end == "through_all_both":
        # both directions through all: two-sided feature with through-all on each side
        two, t1, t2 = True, C.swEndCondThroughAll, C.swEndCondThroughAll
    else:
        two = depth2 is not None and end != "midplane"
        t1, t2 = getattr(C, END[end]), C.swEndCondBlind
    d1, d2 = mm(depth), mm(depth2 or 0)
    has_draft = draft > 0

    def attempt(rev: bool) -> Any:
        doc.ClearSelection2(True)
        g.select(doc, name if ":" in name else f"sketch:{name}", mark=0)
        if cut:
            return fm.FeatureCut4(not two, False, rev, t1, t2, d1, d2, has_draft, has_draft and two, draft_outward, draft_outward, rad(draft), rad(draft),
                                  False, False, False, False, False, True, True, True, True, False, C.swStartSketchPlane, 0.0, False, False)
        return fm.FeatureExtrusion3(not two, False, rev, t1, t2, d1, d2, has_draft, has_draft and two, draft_outward, draft_outward, rad(draft), rad(draft),
                                    False, False, False, False, merge, True, True, C.swStartSketchPlane, 0.0, False)

    f = attempt(reverse)
    flipped = False
    if f is None and cut and not two and end != "midplane":
        f, flipped = attempt(not reverse), True
    out = result(doc, f, "cut" if cut else "extrusion",
                 f"Check that {name} has a closed profile (finish_sketch reports closed_regions) and, for a cut, that it overlaps the body.")
    if flipped:
        out["note"] = "The cut found no material in the requested direction and was made in the opposite direction."
    return out


@tool(WRITES)
def revolve(
    angle: Annotated[float, Field(description="Revolve angle in degrees.")] = 360.0,
    sketch: Annotated[str | None, Field(description="Sketch name; default is the latest sketch.")] = None,
    axis: Annotated[str | None, Field(description="Axis reference ('axis:Axis1', 'edge:N'); omit when the sketch contains one centerline.")] = None,
    cut: bool = False,
    midplane: Annotated[bool, Field(description="Revolve half the angle each way.")] = False,
    reverse: bool = False,
) -> dict:
    """Revolved boss or cut (shafts, bushings, nozzles, bodies of revolution)."""
    doc = SW.active("part")
    C = SW.C
    _select_sketch(doc, sketch)
    if axis:
        g.select(doc, axis, mark=4)
    t = C.swEndCondMidPlane if midplane else C.swEndCondBlind
    f = doc.FeatureManager.FeatureRevolve2(True, True, False, cut, reverse, False, t, C.swEndCondBlind, rad(angle), 0.0,
                                           False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True)
    return result(doc, f, "revolve", "The profile must be closed and lie entirely on one side of the axis; add a centerline or pass axis=.")


@tool(WRITES)
def sweep(
    path: Annotated[str, Field(description="Path: a sketch name or 'edge:N'.")],
    profile: Annotated[str | None, Field(description="Profile sketch (closed). Omit and give circular_diameter for a round section (tubes, ducts, harness runs).")] = None,
    circular_diameter: Annotated[float | None, Field(description="Diameter in mm of a circular profile swept along the path.")] = None,
    guides: Annotated[list[str] | None, Field(description="Optional guide curves (sketch names).")] = None,
    cut: bool = False,
    twist: Annotated[float, Field(description="Constant twist along the path, degrees.")] = 0.0,
) -> dict:
    """Swept boss or cut along a path."""
    doc = SW.active("part")
    C = SW.C
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    circular = profile is None
    if circular and not circular_diameter:
        raise SwError("Give a profile sketch or circular_diameter.")
    if not circular:
        g.select(doc, profile if ":" in profile else f"sketch:{profile}", mark=1)
    g.select(doc, path if ":" in path else f"sketch:{path}", mark=4)
    for gd in guides or []:
        g.select(doc, gd if ":" in gd else f"sketch:{gd}", mark=2)
    twist_opt = C.swTwistControlConstantTwistAlongPath if twist else C.swTwistControlFollowPath
    fm = doc.FeatureManager
    d = mm(circular_diameter or 0)
    if cut:
        f = fm.InsertCutSwept5(False, True, twist_opt, False, False, 0, 0, False, 0.0, 0.0, 0, 0, True, True, rad(twist), True, True, True, False, circular, d, 0)
    else:
        f = fm.InsertProtrusionSwept4(False, True, twist_opt, False, False, 0, 0, False, 0.0, 0.0, 0, 0, True, True, True, rad(twist), True, circular, d, 0)
    return result(doc, f, "sweep", "The profile should be perpendicular to the path start and the path must not self-intersect.")


@tool(WRITES)
def helix(
    pitch: Annotated[float | None, Field(description="Distance between turns in mm.")] = None,
    revolutions: Annotated[float | None, Field(description="Number of turns.")] = None,
    height: Annotated[float | None, Field(description="Total height in mm. Give two of pitch, revolutions and height.")] = None,
    sketch: Annotated[str | None, Field(description="Sketch holding exactly one circle (its diameter is the helix diameter); default is the latest sketch.")] = None,
    clockwise: bool = False,
    reverse: Annotated[bool, Field(description="Grow the helix to the other side of the sketch plane.")] = False,
    start_angle: Annotated[float, Field(description="Angle in degrees where the helix starts on the circle (0 = sketch +X).")] = 0.0,
    taper_angle: Annotated[float, Field(description="Taper in degrees for a conical helix (0 for none).")] = 0.0,
    taper_outward: bool = False,
    spiral: Annotated[bool, Field(description="Flat spiral in the sketch plane instead (pitch = radial growth per turn).")] = False,
) -> dict:
    """Helix or spiral curve from a sketched circle, used as a sweep path for springs, coils, augers
    and modelled threads (sweep(path='Helix/Spiral1', profile=...) with the profile sketched on a plane
    through the helix start point)."""
    doc = SW.active("part")
    C = SW.C
    if spiral:
        if not (pitch and revolutions):
            raise SwError("A spiral needs pitch and revolutions.")
        mode = C.swHelixDefinedBySpiral
    elif pitch and revolutions:
        mode = C.swHelixDefinedByPitchAndRevolution
    elif height and revolutions:
        mode = C.swHelixDefinedByHeightAndRevolution
    elif height and pitch:
        mode = C.swHelixDefinedByHeightAndPitch
    else:
        raise SwError("Give two of pitch, revolutions and height.")
    if not height:
        height = (pitch or 0) * (revolutions or 0)
    if not pitch:
        pitch = height / revolutions
    if not revolutions:
        revolutions = height / pitch
    name = _select_sketch(doc, sketch)
    before = _last_feature(doc).Name
    doc.InsertHelix(reverse, clockwise, bool(taper_angle), taper_outward, mode, mm(height), mm(pitch), float(revolutions), rad(taper_angle), rad(start_angle))
    out = created_since(doc, before, "helix", f"{name} must contain exactly one circle.")
    out.pop("volume_mm3", None)
    out.update(pitch=pitch, revolutions=revolutions, height=None if spiral else height)
    # where the curve starts and which way it leaves, so the sweep profile can be placed there
    try:
        rc = SW.cast(g.feature_by_name(doc, out["feature"]).GetSpecificFeature2(), "IReferenceCurve")
        edge = SW.cast(rc.GetFirstSegment(), "IEdge")
        prm = SW.cast(edge.GetCurveParams3(), "ICurveParamData")
        tan = list(SW.cast(edge.GetCurve(), "ICurve").Evaluate2(prm.UMinValue, 1))[3:6]
        n = math.sqrt(sum(c * c for c in tan)) or 1.0
        out["start_point"] = [round(to_mm(c), 4) + 0.0 for c in list(prm.StartPoint)[:3]]
        out["end_point"] = [round(to_mm(c), 4) + 0.0 for c in list(prm.EndPoint)[:3]]
        out["start_tangent"] = [round(c / n, 4) + 0.0 for c in tan]
        out["hint"] = ("Sketch the sweep profile centred on start_point, on a plane through the helix axis and start_point "
                       "(a default plane when start_point lies on one; otherwise ref_plane).")
    except Exception:
        pass
    return out


@tool(WRITES)
def loft(
    profiles: Annotated[list[str], Field(description="Two or more profile sketches (or planar faces) in order.")],
    guides: Annotated[list[str] | None, Field(description="Optional guide curve sketches.")] = None,
    cut: bool = False,
    closed: Annotated[bool, Field(description="Close the loft back to the first profile.")] = False,
) -> dict:
    """Lofted boss or cut between profiles (fairings, transitions, blade sections)."""
    if len(profiles) < 2:
        raise SwError("A loft needs at least two profiles.")
    doc = SW.active("part")
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    for p in profiles:
        g.select(doc, p if ":" in p else f"sketch:{p}", mark=1)
    for gd in guides or []:
        g.select(doc, gd if ":" in gd else f"sketch:{gd}", mark=2)
    fm = doc.FeatureManager
    before = _last_feature(doc).Name
    if cut:
        f = fm.InsertCutBlend(closed, True, False, 1.0, 0, 0, False, 0.0, 0.0, 0, True, True)
    else:
        f = fm.InsertProtrusionBlend2(closed, True, False, 1.0, 0, 0, 1.0, 1.0, False, False, False, 0.0, 0.0, 0, True, True, True, 0)
    if f is not None:
        return result(doc, f, "loft")
    # The FeatureManager call rejects some profiles (single closed curves such as ellipses) that the
    # older ModelDoc2 call accepts; reselect and try it before giving up.
    doc.ClearSelection2(True)
    for p in profiles:
        g.select(doc, p if ":" in p else f"sketch:{p}", mark=1)
    for gd in guides or []:
        g.select(doc, gd if ":" in gd else f"sketch:{gd}", mark=2)
    if cut:
        doc.InsertCutBlend(closed)
    else:
        doc.InsertProtrusionBlend2(closed, True, False)
    return created_since(doc, before, "loft", "Profiles should not cross each other; give them the same number of segments where possible.")


# ------------------------------------------------------------------------------------------ dress-up


@tool(WRITES)
def fillet(
    radius: Annotated[float, Field(description="Radius in mm (constant and face fillets).")] = 0.0,
    edges: Annotated[list[str] | None, Field(description="Edges to round ('edge:N', 'edge@x,y,z'). Variable: one edge.")] = None,
    faces: Annotated[list[str] | None, Field(description="Constant: round every edge of these faces. Face fillet: the first face set. Full round: [side face, centre face, side face].")] = None,
    kind: Annotated[Literal["constant", "variable", "face", "full_round"], Field(description=(
        "constant: one radius on edges; variable: radius changing along one edge from radii[0] at its start to radii[1] at its end; "
        "face: blend between two face sets (faces and faces2) that need not share an edge; full_round: replace the centre face "
        "with a full round between two side faces (rib and boss ends)."))] = "constant",
    radii: Annotated[list[float] | None, Field(description="Variable fillet: [radius at start, radius at end] in mm (list_edges gives the edge's start and end).")] = None,
    faces2: Annotated[list[str] | None, Field(description="Face fillet: the second face set.")] = None,
    tangent_propagation: bool = True,
) -> dict:
    """Fillets: constant radius on edges, variable radius along an edge, face fillets between face sets,
    and full round fillets."""
    doc = SW.active("part")
    C = SW.C
    fm = doc.FeatureManager

    def face_objs(refs: list[str] | None, what: str) -> list[Any]:
        out = []
        for r in refs or []:
            ref = g.resolve(doc, r)
            if ref.kind != "face":
                raise SwError(f"{what}: {r} is not a face.")
            out.append(ref.obj)
        return out

    if kind == "constant":
        if radius <= 0:
            raise SwError("Give a radius above 0.")
        data = SW.cast(fm.CreateDefinition(C.swFmFillet), "ISimpleFilletFeatureData2")
        data.Initialize(C.swConstRadiusFillet)
        data.DefaultRadius = mm(radius)
        data.PropagateToTangentFaces = tangent_propagation
        data.Edges = _dispatch_array(_edges_from(doc, edges, faces))
        return result(doc, fm.CreateFeature(data), "fillet", "The radius may be too large for the adjacent faces.")
    # variable, face and full round fillets go through FeatureFillet3 with the selection marks it expects
    before = _volume(doc)
    doc.ClearSelection2(True)
    radii_arr = None
    if kind == "variable":
        if not edges or len(edges) != 1 or not radii or len(radii) != 2 or min(radii) <= 0:
            raise SwError("A variable fillet needs one edge and radii=[start, end], both above 0.")
        g.select(doc, edges[0])
        radius, ftyp, radii_arr = radii[0], C.swFeatureFilletType_VariableRadius, _doubles([mm(radii[0]), mm(radii[1])])
    elif kind == "face":
        a, b = face_objs(faces, "faces"), face_objs(faces2, "faces2")
        if not a or not b or radius <= 0:
            raise SwError("A face fillet needs faces (first set), faces2 (second set) and a radius.")
        for f in a:
            g.Ref(f, "face", "face set 1").select(doc, mark=2)
        for f in b:
            g.Ref(f, "face", "face set 2").select(doc, mark=4)
        ftyp = C.swFeatureFilletType_Face
    else:
        fs = face_objs(faces, "faces")
        if len(fs) != 3:
            raise SwError("A full round fillet needs faces=[side face, centre face, side face].")
        for f, mark in zip(fs, (2, 512, 4)):
            g.Ref(f, "face", "full round face").select(doc, mark=mark)
        radius, ftyp = 0.001, C.swFeatureFilletType_FullRound
    opts = C.swFeatureFilletPropagate if tangent_propagation else 0
    f = fm.FeatureFillet3(opts, mm(radius), 0.0, 0.0, ftyp, C.swFilletOverFlowType_Default, 0, radii_arr, None, None, None, None, None, None)
    what = f"{kind.replace('_', ' ')} fillet"
    out = result(doc, f, what, "The radius may be too large for the adjacent faces.")
    if before is not None and out.get("volume_mm3") == before:
        raise SwError(f"The {what} did not change the part; check the faces (sides and centre for a full round) and the radius.")
    return out


@tool(WRITES)
def chamfer(
    distance: Annotated[float, Field(description="Chamfer distance in mm.")],
    edges: Annotated[list[str] | None, Field(description="Edges to chamfer.")] = None,
    faces: Annotated[list[str] | None, Field(description="Chamfer every edge of these faces.")] = None,
    angle: Annotated[float | None, Field(description="Angle in degrees for a distance x angle chamfer (default 45).")] = 45.0,
    distance2: Annotated[float | None, Field(description="Second distance for a distance x distance chamfer (overrides angle).")] = None,
    flip: bool = False,
) -> dict:
    """Edge chamfer: distance x angle, or distance x distance."""
    doc = SW.active("part")
    C = SW.C
    targets = _edges_from(doc, edges, faces)
    doc.ClearSelection2(True)
    for e in targets:
        SW.cast(e, "IEntity").Select4(True, None)
    opts = C.swFeatureChamferTangentPropagation | (C.swFeatureChamferFlipDirection if flip else 0)
    if distance2 is not None:
        f = doc.FeatureManager.InsertFeatureChamfer(opts, C.swChamferDistanceDistance, mm(distance), 0.0, mm(distance2), 0, 0, 0)
    else:
        f = doc.FeatureManager.InsertFeatureChamfer(opts, C.swChamferAngleDistance, mm(distance), rad(angle or 45.0), 0.0, 0, 0, 0)
    return result(doc, f, "chamfer")


@tool(WRITES)
def shell(
    thickness: Annotated[float, Field(description="Wall thickness in mm.")],
    remove_faces: Annotated[list[str] | None, Field(description="Faces to open up; omit for a closed hollow body.")] = None,
    outward: bool = False,
) -> dict:
    """Hollow the part with a uniform wall."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    for r in remove_faces or []:
        g.select(doc, r, mark=1)
    before = _last_feature(doc).Name
    doc.InsertFeatureShell(mm(thickness), outward)
    return created_since(doc, before, "shell", "The wall may be thicker than the smallest local radius of curvature.")


@tool(WRITES)
def draft(
    angle: Annotated[float, Field(description="Draft angle in degrees.")],
    neutral: Annotated[str, Field(description="Neutral plane or face.")],
    faces: Annotated[list[str], Field(description="Faces to draft.")],
    reverse: bool = False,
) -> dict:
    """Neutral-plane draft on faces (castings, forgings, moulded parts)."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    g.select(doc, neutral, mark=1)
    for r in faces:
        g.select(doc, r, mark=2)
    f = doc.FeatureManager.InsertMultiFaceDraft(rad(angle), reverse, False, 0, False, False)
    return result(doc, f, "draft")


# ------------------------------------------------------------------------------------------ holes

_FASTENERS = {
    "iso": {"clearance": "ISOScrewClearances", "drill": "ISODrillSizes", "tap_drill": "ISOTapDrills", "dowel": "ISODowelHole",
            "socket_head": "ISOSocketHeadCap", "hex_bolt": "ISOHexBolt", "flat_head": "ISOSocketCTSKFlatHead", "tapped": "ISOTappedHole"},
    "ansi_metric": {"clearance": "AnsiMetricScrewClearances", "drill": "AnsiMetricDrillSizes", "tap_drill": "AnsiMetricTapDrills",
                    "dowel": "AnsiMetricDowelHole", "socket_head": "AnsiMetricSocketHeadCapScrew", "hex_bolt": "AnsiMetricHexBolt",
                    "flat_head": "AnsiMetricFlatHead82", "tapped": "AnsiMetricTappedHole", "bottoming_tapped": "AnsiMetricBottomingTappedHole"},
    "ansi_inch": {"clearance": "AnsiInchScrewClearances", "drill": "AnsiInchFractionalDrillSizes", "number_drill": "AnsiInchNumberDrillSizes",
                  "letter_drill": "AnsiInchLetterDrillSizes", "all_drill": "AnsiInchAllDrillSizes", "tap_drill": "AnsiInchTapDrills",
                  "dowel": "AnsiInchDowelHole", "socket_head": "AnsiInchSocketCapScrew", "hex_bolt": "AnsiInchHexBolt",
                  "flat_head": "AnsiInchFlatHead82", "flat_head_100": "AnsiInchFlatHead100", "tapped": "AnsiInchTappedHole",
                  "bottoming_tapped": "AnsiInchBottomingTappedHole"},
}
_DEFAULT_FASTENER = {"simple": "clearance", "counterbore": "socket_head", "countersink": "flat_head", "tapped": "tapped"}
_STANDARD = {"iso": "swStandardISO", "ansi_metric": "swStandardAnsiMetric", "ansi_inch": "swStandardAnsiInch"}
_HOLE_TYPE = {"simple": "swWzdHole", "counterbore": "swWzdCounterBore", "countersink": "swWzdCounterSink", "tapped": "swWzdTap"}
_FIT = {"close": 0, "normal": 1, "loose": 2}
_DRILL_ANGLE = math.radians(118)


def _placement_sketch(feat: Any) -> tuple[Any, Any]:
    """The hole's position sketch: the sub-sketch with points and no segments."""
    sub = SW.cast(feat.GetFirstSubFeature(), "IFeature")
    while sub is not None:
        if sub.GetTypeName2() in ("ProfileFeature", "3DProfileFeature"):
            sk = SW.cast(sub.GetSpecificFeature2(), "ISketch")
            if sk is not None and not sk.GetSketchSegments() and sk.GetSketchPoints2():
                return sub, sk
        sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
    raise SwError("Could not find the hole position sketch to place the holes exactly.")


@tool(WRITES)
def hole(
    size: Annotated[str, Field(description="Fastener or drill size from the Hole Wizard tables, e.g. 'M6', 'M5', '#10', '1/4', '1/4-20', 'Ø6.6'.")],
    positions: Annotated[list[list[float]], Field(description="Hole centres [[x, y, z], ...] in model mm, all on the same planar face. Placed exactly.")],
    kind: Annotated[Literal["simple", "counterbore", "countersink", "tapped"], Field(description="Hole type.")] = "simple",
    standard: Annotated[Literal["iso", "ansi_metric", "ansi_inch"], Field(description="Hole standard.")] = "iso",
    fastener: Annotated[str | None, Field(description="Fastener/hole table. Defaults: simple=clearance, counterbore=socket_head, countersink=flat_head, tapped=tapped. Others: drill, tap_drill, dowel, hex_bolt, bottoming_tapped; ansi_inch also number_drill, letter_drill, all_drill, flat_head_100 (100 degree aerospace countersink).")] = None,
    face: Annotated[str | None, Field(description="Face to drill; default is the face under the first position.")] = None,
    depth: Annotated[float | None, Field(description="Hole (or tap drill) depth in mm; omit for through all.")] = None,
    thread_depth: Annotated[float | None, Field(description="Tapped holes: thread depth in mm (default from the standard).")] = None,
    fit: Annotated[Literal["close", "normal", "loose"], Field(description="Clearance fit for clearance, counterbore and countersink holes.")] = "normal",
    cosmetic_thread: Annotated[bool, Field(description="Tapped holes: add a cosmetic thread with callout.")] = True,
) -> dict:
    """Hole Wizard holes from standard tables (plain, counterbore, countersink, tapped), with every
    centre placed exactly at the given model coordinates."""
    doc = SW.active("part")
    C = SW.C
    if not positions:
        raise SwError("Give at least one position.")
    pts = [[mm(c) for c in p] for p in positions]
    if any(len(p) != 3 for p in pts):
        raise SwError("Positions are [x, y, z] in model mm.")
    table = _FASTENERS[standard]
    fname = fastener or _DEFAULT_FASTENER[kind]
    if fname not in table:
        raise SwError(f"Fastener '{fname}' is not available for {standard}. Choose from {sorted(table)}.")
    fast = getattr(C, "swStandard" + table[fname])
    std = getattr(C, _STANDARD[standard])
    htype = getattr(C, _HOLE_TYPE[kind])
    end_t = C.swEndCondBlind if depth else C.swEndCondThroughAll
    # face under the first point (Hole Wizard places the hole at the selection point, then we fix it)
    face_ref = g.resolve(doc, face) if face else g.resolve(doc, "face@{},{},{}".format(*positions[0]))
    if face_ref.kind != "face":
        raise SwError(f"{face} is not a face.")
    for p, raw in zip(pts, positions):
        q = face_ref.obj.GetClosestPointOn(*p)
        if q is None or math.dist(p, list(q)[:3]) > 1e-5:
            raise SwError(f"Position {raw} is not on the selected face.")
    doc.ClearSelection2(True)
    fm = doc.FeatureManager

    def pick() -> None:
        # Select the face entity itself: a pick by coordinates can land on a neighbouring face.
        doc.ClearSelection2(True)
        face_ref.select(doc, append=False)

    if kind == "simple":
        data = SW.cast(fm.CreateDefinition(C.swFmHoleWzd), "IWizardHoleFeatureData2")
        data.InitializeHole(htype, std, fast, size, end_t)
        try:
            if depth:
                data.HoleDepth = mm(depth)
            data.HoleFit = _FIT[fit]
        except Exception:
            pass
        pick()
        feat = fm.CreateFeature(data)
    else:
        v = [-1.0] * 12
        if kind == "counterbore":
            v[3] = _FIT[fit]
            if depth:
                v[4] = _DRILL_ANGLE
        elif kind == "countersink":
            v[3] = _FIT[fit]
            if depth:
                v[4] = _DRILL_ANGLE
        elif kind == "tapped":
            v[0] = mm(thread_depth) if thread_depth else -1.0
            v[5] = _DRILL_ANGLE if depth else -1.0
            v[6] = C.swCosmeticThreadWithCallout if cosmetic_thread else C.swCosmeticThreadNone
            v[7] = C.swEndThreadTypeBLIND if depth else C.swEndThreadTypeTHROUGH_ALL
        pick()
        feat = fm.HoleWizard5(htype, std, fast, size, end_t, -1.0, mm(depth) if depth else -1.0, -1.0, *v,
                              "", False, True, True, True, True, False)
    if feat is None:
        doc.ClearSelection2(True)
        raise SwError(f"Hole Wizard rejected {kind} '{size}' ({standard}, {fname}). The size string must match the Hole Wizard table, "
                      "e.g. 'M6' for metric clearance/counterbore/tapped, '#10' or '1/4' for inch, 'Ø6.6' for ISO drill sizes.")
    feat = SW.cast(feat, "IFeature")
    # exact positions: move the first point, add the rest
    sub, sk = _placement_sketch(feat)
    frame = SketchFrame(sk)
    off_plane = [raw for p, raw in zip(pts, positions) if abs(frame.to_sketch(p)[2]) > 1e-6]
    if off_plane:
        doc.ClearSelection2(True)
        feat.Select2(False, 0)
        doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
        raise SwError(f"Hole Wizard placed the holes on a different face than requested; positions {off_plane} are not on its plane. "
                      "Nothing was created. Pass face= explicitly.")
    doc.ClearSelection2(True)
    sub.Select2(False, 0)
    doc.EditSketch()
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        first = SW.cast(sk.GetSketchPoints2()[0], "ISketchPoint")
        q = frame.to_sketch(pts[0])
        first.SetCoords(q[0], q[1], 0.0)
        for p in pts[1:]:
            q = frame.to_sketch(p)
            sm.CreatePoint(q[0], q[1], 0.0)
    finally:
        sm.AddToDB = False
        sm.InsertSketch(True)
    doc.EditRebuild3()
    out = result(doc, feat, "hole")
    out["holes"] = len(pts)
    out["table"] = f"{standard}/{fname}"
    return out


@tool(WRITES)
def cosmetic_thread(
    edge: Annotated[str, Field(description="Circular edge at the start of the thread (hole or shaft end).")],
    note: Annotated[str, Field(description="Thread callout shown on drawings, e.g. 'M8x1.25-6H' or '1/4-28 UNF-3B'.")],
    diameter: Annotated[float, Field(description="Thread major diameter (shafts) or minor diameter (holes), mm.")],
    depth: Annotated[float | None, Field(description="Thread length in mm; omit for through.")] = None,
) -> dict:
    """Cosmetic thread with a drawing callout on a cylindrical feature."""
    doc = SW.active("part")
    C = SW.C
    doc.ClearSelection2(True)
    ref = g.resolve(doc, edge)
    if ref.kind != "edge":
        raise SwError(f"{edge} is not an edge.")
    ref.select(doc, append=False)
    end_t = C.swEndConditionBlind if depth else C.swEndConditionThrough
    f = doc.FeatureManager.InsertCosmeticThread3(C.swStandardType_StandardNone, "", "", mm(diameter), end_t, mm(depth or 0), note)
    return result(doc, f, "cosmetic thread")


# ------------------------------------------------------------------------------------------ patterns


def _select_seeds(doc: Any, features: list[str], mark: int) -> bool:
    """Select pattern/mirror seeds: features with `mark`, bodies ('body:Name') with the body mark 256.
    Returns True when bodies were given."""
    has_bodies = False
    for n in features:
        if n.lower().startswith("body:"):
            g.select(doc, n, mark=256)
            has_bodies = True
        else:
            g.select(doc, n if ":" in n else f"feature:{n}", mark=mark)
    return has_bodies


@tool(WRITES)
def linear_pattern(
    features: Annotated[list[str], Field(description="Feature names to pattern, or bodies ('body:Name' from list_bodies) for a body pattern.")],
    direction: Annotated[str, Field(description="Direction: a linear edge, axis, or plane/planar face (its normal).")],
    count: Annotated[int, Field(ge=2, description="Instances in direction 1, including the seed.")],
    spacing: Annotated[float, Field(description="Spacing in mm.")],
    reverse: bool = False,
    direction2: Annotated[str | None, Field(description="Optional second direction.")] = None,
    count2: Annotated[int, Field(ge=1)] = 1,
    spacing2: float = 0.0,
    reverse2: bool = False,
) -> dict:
    """Linear (or rectangular) pattern of features or bodies."""
    doc = SW.active("part")

    def attempt(rev: bool) -> Any:
        doc.ClearSelection2(True)
        _select_seeds(doc, features, 4)
        g.select(doc, direction, mark=1)
        if direction2:
            g.select(doc, direction2, mark=2)
        return doc.FeatureManager.FeatureLinearPattern5(count, mm(spacing), count2 if direction2 else 1, mm(spacing2), rev, reverse2, "", "",
                                                        False, False, False, False, False, False, False, False, False, False, 0.0, 0.0, False, False)

    f = attempt(reverse)
    flipped = False
    if f is None:
        # The edge's own direction may point off the part; SolidWorks then refuses the pattern.
        f, flipped = attempt(not reverse), True
    out = result(doc, f, "linear pattern", "Check the direction reference and that every instance stays on the part.")
    if flipped:
        out["note"] = "Direction 1 was reversed so the instances land on the part."
    return out


@tool(WRITES)
def circular_pattern(
    features: Annotated[list[str], Field(description="Feature names to pattern, or bodies ('body:Name') for a body pattern.")],
    axis: Annotated[str, Field(description="Axis, cylindrical face or circular edge.")],
    count: Annotated[int, Field(ge=2, description="Total instances including the seed.")],
    angle: Annotated[float, Field(description="Total angle in degrees when equal_spacing, otherwise the angle between instances.")] = 360.0,
    equal_spacing: bool = True,
    reverse: bool = False,
) -> dict:
    """Circular pattern of features or bodies (bolt circles, flanges, spokes, rotor arms)."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    _select_seeds(doc, features, 4)
    g.select(doc, axis, mark=1)
    f = doc.FeatureManager.FeatureCircularPattern5(count, rad(angle), reverse, "", False, equal_spacing, False, False, False, False, 1, 0.0, "", False)
    return result(doc, f, "circular pattern")


@tool(WRITES)
def mirror(
    plane: Annotated[str, Field(description="Mirror plane or planar face.")],
    features: Annotated[list[str] | None, Field(description="Features to mirror, or bodies ('body:Name').")] = None,
    bodies: Annotated[bool, Field(description="Mirror every solid body instead of features.")] = False,
    merge: Annotated[bool, Field(description="Merge mirrored bodies with the originals.")] = True,
) -> dict:
    """Mirror features or bodies about a plane."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    if bodies:
        for b in g.bodies(doc):
            g.Ref(b, "body", b.Name).select(doc, mark=256)
    elif features:
        bodies = _select_seeds(doc, features, 1)
    else:
        raise SwError("Give features to mirror or set bodies=true.")
    g.select(doc, plane, mark=2)
    f = doc.FeatureManager.InsertMirrorFeature2(bodies, False, bodies and merge, False, 0)
    return result(doc, f, "mirror")


# ------------------------------------------------------------------------------------------ bodies

_COMBINE = {"add": "SWBODYADD", "subtract": "SWBODYCUT", "common": "SWBODYINTERSECT"}


def _body(doc: Any, ref: str) -> Any:
    return g.resolve(doc, ref if ref.lower().startswith("body:") else f"body:{ref}").obj


@tool(WRITES)
def combine(
    operation: Annotated[Literal["add", "subtract", "common"], Field(description="add: union; subtract: remove the tool bodies from the main body; common: keep only the overlap.")],
    main: Annotated[str, Field(description="Main body ('body:Name' or the name from list_bodies).")],
    tools: Annotated[list[str], Field(description="Tool bodies combined with the main body; they are consumed.")],
) -> dict:
    """Boolean combination of solid bodies in a multibody part (build bodies with extrude(merge=false),
    import_file or move_body, then combine them)."""
    doc = SW.active("part")
    if not tools:
        raise SwError("Give at least one tool body.")
    m = _body(doc, main)
    ts = [_body(doc, t) for t in tools]
    doc.ClearSelection2(True)
    if operation == "subtract":
        f = doc.FeatureManager.InsertCombineFeature(SW.C.SWBODYCUT, m._oleobj_, _dispatch_array(ts))
    else:  # add and common take every body as a tool and no main body
        f = doc.FeatureManager.InsertCombineFeature(getattr(SW.C, _COMBINE[operation]), None, _dispatch_array([m, *ts]))
    out = result(doc, f, "combine", "The bodies must touch or overlap (common and subtract need an overlap).")
    out["bodies"] = [b.Name for b in g.bodies(doc)]
    return out


@tool(WRITES)
def move_body(
    bodies: Annotated[list[str], Field(description="Bodies to move or copy ('body:Name').")],
    translate: Annotated[list[float], Field(description="Translation [dx, dy, dz] in mm.")] = [0.0, 0.0, 0.0],  # noqa: B006
    rotate: Annotated[list[float], Field(description="Rotation [about X, about Y, about Z] in degrees, applied about center.")] = [0.0, 0.0, 0.0],  # noqa: B006
    center: Annotated[list[float], Field(description="Centre of rotation [x, y, z] in mm.")] = [0.0, 0.0, 0.0],  # noqa: B006
    copy: Annotated[bool, Field(description="Keep the originals and create copies.")] = False,
    count: Annotated[int, Field(ge=1, le=100, description="Number of copies (each one step further) when copy is set.")] = 1,
) -> dict:
    """Move, rotate or copy solid bodies in a multibody part."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    for b in bodies:
        g.Ref(_body(doc, b), "body", b).select(doc, mark=1)
    t = [mm(v) for v in (list(translate) + [0, 0, 0])[:3]]
    c = [mm(v) for v in (list(center) + [0, 0, 0])[:3]]
    r = [rad(v) for v in (list(rotate) + [0, 0, 0])[:3]]
    f = doc.FeatureManager.InsertMoveCopyBody2(*t, 0.0, *c, *r, copy, count if copy else 1)
    out = result(doc, f, "move/copy body")
    out["bodies"] = [b.Name for b in g.bodies(doc)]
    return out


# ------------------------------------------------------------------------------------------ editing

_DEFINITIONS = {
    "Extrusion": "IExtrudeFeatureData2", "Boss": "IExtrudeFeatureData2", "Cut": "IExtrudeFeatureData2", "ICE": "IExtrudeFeatureData2",
    "BaseBody": "IExtrudeFeatureData2", "Revolution": "IRevolveFeatureData2", "RevCut": "IRevolveFeatureData2",
    "BaseRevolve": "IRevolveFeatureData2", "Fillet": "ISimpleFilletFeatureData2", "Chamfer": "IChamferFeatureData2",
    "Shell": "IShellFeatureData", "LPattern": "ILinearPatternFeatureData", "CirPattern": "ICircularPatternFeatureData",
}


def _dispatch_array(objs: list[Any]) -> Any:
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [o._oleobj_ for o in objs])


def _dimension_with_value(f: Any, value: float, skip: list[str]) -> str:
    """Name of the feature's linear dimension that currently has `value` (metres)."""
    C = SW.C
    dd = f.GetFirstDisplayDimension()
    while dd is not None:
        d = SW.cast(SW.cast(dd, "IDisplayDimension").GetDimension2(0), "IDimension")
        name = "@".join(d.FullName.split("@")[:2])
        v = d.GetSystemValue3(C.swThisConfiguration, None)
        v = v[0] if isinstance(v, (tuple, list)) else v
        if d.GetType() == C.swDimensionParamTypeDoubleLinear and abs(v - value) < 1e-9 and name not in skip:
            return name
        dd = f.GetNextDisplayDimension(dd)
    raise SwError(f"Could not find the spacing dimension of {f.Name}; use dimensions() and set_dimensions instead.")


@tool(WRITES)
def edit_feature(
    feature: Annotated[str, Field(description="Feature name from list_features.")],
    end: Annotated[Literal["blind", "through_all", "through_all_both", "midplane", "up_to_next"] | None, Field(description="Extrude/cut: new end condition.")] = None,
    depth: Annotated[float | None, Field(description="Extrude/cut: depth in mm (direction 1).")] = None,
    depth2: Annotated[float | None, Field(description="Extrude/cut: blind depth in the second direction; 0 removes the second direction.")] = None,
    reverse: Annotated[bool | None, Field(description="Extrude/cut/revolve: flip direction 1.")] = None,
    flip_side_to_cut: Annotated[bool | None, Field(description="Cut: remove the material outside the profile instead.")] = None,
    merge: Annotated[bool | None, Field(description="Boss: merge with the existing bodies.")] = None,
    angle: Annotated[float | None, Field(description="Revolve: angle in degrees. Circular pattern: total angle (equal spacing) or angle between instances.")] = None,
    radius: Annotated[float | None, Field(description="Fillet: radius in mm.")] = None,
    distance: Annotated[float | None, Field(description="Chamfer: distance in mm.")] = None,
    thickness: Annotated[float | None, Field(description="Shell: wall thickness in mm.")] = None,
    edges: Annotated[list[str] | None, Field(description="Fillet/chamfer: replace the edges it rounds ('edge@x,y,z', 'edge:N').")] = None,
    count: Annotated[int | None, Field(ge=1, description="Pattern: instances in direction 1 (including the seed).")] = None,
    spacing: Annotated[float | None, Field(description="Linear pattern: spacing in mm.")] = None,
    count2: Annotated[int | None, Field(ge=1, description="Linear pattern: instances in direction 2.")] = None,
    spacing2: Annotated[float | None, Field(description="Linear pattern: spacing in direction 2, mm.")] = None,
) -> dict:
    """Change how an existing feature is defined without rebuilding it from scratch: an extrude's end
    condition (blind to through-all and back), depth, direction and second side; revolve angle; fillet
    radius or edges; chamfer distance; shell thickness; pattern counts and spacing. Plain driving
    dimensions can also be changed with set_dimensions."""
    doc = SW.active("part")
    C = SW.C
    f = g.feature_by_name(doc, feature)
    if f is None:
        raise SwError(f"No feature named '{feature}'. Use list_features to see names.")
    t = f.GetTypeName2()
    iface = _DEFINITIONS.get(t)
    if iface is None:
        raise SwError(f"edit_feature does not support {t} features yet. Change their dimensions with set_dimensions, or delete and recreate them.")
    data = SW.cast(f.GetDefinition(), iface)
    if not data.AccessSelections(doc, None):
        raise SwError(f"SolidWorks would not open {feature} for editing.")
    changed: list[str] = []
    spacing_dims: list[tuple[str, float]] = []

    def unsupported(name: str) -> SwError:
        return SwError(f"{name} does not apply to a {t} feature.")

    try:
        if iface == "IExtrudeFeatureData2":
            if end is not None:
                if end == "through_all_both":
                    data.BothDirections = True
                    data.SetEndCondition(True, C.swEndCondThroughAll)
                    data.SetEndCondition(False, C.swEndCondThroughAll)
                else:
                    data.SetEndCondition(True, getattr(C, END[end]))
                changed.append("end")
            if depth is not None:
                data.SetDepth(True, mm(depth))
                changed.append("depth")
            if depth2 is not None:
                data.BothDirections = depth2 > 0
                if depth2 > 0:
                    data.SetEndCondition(False, C.swEndCondBlind)
                    data.SetDepth(False, mm(depth2))
                changed.append("depth2")
            if reverse is not None:
                data.ReverseDirection = reverse
                changed.append("reverse")
            if flip_side_to_cut is not None:
                if data.IsBossFeature():
                    raise unsupported("flip_side_to_cut")
                data.FlipSideToCut = flip_side_to_cut
                changed.append("flip_side_to_cut")
            if merge is not None:
                data.Merge = merge
                changed.append("merge")
        elif iface == "IRevolveFeatureData2":
            if angle is not None:
                data.SetRevolutionAngle(True, rad(angle))
                changed.append("angle")
            if reverse is not None:
                data.ReverseDirection = reverse
                changed.append("reverse")
        elif iface == "ISimpleFilletFeatureData2":
            if radius is not None:
                data.DefaultRadius = mm(radius)
                changed.append("radius")
        elif iface == "IChamferFeatureData2":
            if distance is not None:
                data.SetEdgeChamferDistance(0, mm(distance))
                changed.append("distance")
        elif iface == "IShellFeatureData":
            if thickness is not None:
                data.Thickness = mm(thickness)
                changed.append("thickness")
        elif iface == "ILinearPatternFeatureData":
            for name, val, prop in (("count", count, "D1TotalInstances"), ("count2", count2, "D2TotalInstances")):
                if val is not None:
                    setattr(data, prop, int(val))
                    changed.append(name)
            # ModifyDefinition rejects new spacings; they are changed through the pattern's dimensions below
            for name, val, prop in (("spacing", spacing, "D1Spacing"), ("spacing2", spacing2, "D2Spacing")):
                if val is not None:
                    spacing_dims.append((_dimension_with_value(f, getattr(data, prop), [n for n, _ in spacing_dims]), mm(val)))
                    changed.append(name)
            if reverse is not None:
                data.D1ReverseDirection = reverse
                changed.append("reverse")
        elif iface == "ICircularPatternFeatureData":
            if count is not None:
                data.TotalInstances = int(count)
                changed.append("count")
            if angle is not None:
                data.Spacing = rad(angle)
                changed.append("angle")
            if reverse is not None:
                data.ReverseDirection = reverse
                changed.append("reverse")
        if edges is not None:
            if iface not in ("ISimpleFilletFeatureData2", "IChamferFeatureData2"):
                raise unsupported("edges")
            data.Edges = _dispatch_array(_edges_from(doc, edges, None))
            changed.append("edges")
        given = {k for k, v in dict(end=end, depth=depth, depth2=depth2, reverse=reverse, flip_side_to_cut=flip_side_to_cut, merge=merge, angle=angle,
                                    radius=radius, distance=distance, thickness=thickness, count=count, spacing=spacing, count2=count2,
                                    spacing2=spacing2).items() if v is not None}
        if ignored := sorted(given - set(changed)):
            raise unsupported(", ".join(ignored))
        if not changed:
            raise SwError("Nothing to change: give at least one new value.")
    except BaseException:
        data.ReleaseSelectionAccess()
        raise
    if len(changed) > len(spacing_dims):
        if not f.ModifyDefinition(data, doc, None):
            raise SwError(f"SolidWorks rejected the new definition of {feature}; the old one is kept.")
    else:
        data.ReleaseSelectionAccess()
    for name, value in spacing_dims:
        dim = SW.cast(doc.Parameter(name), "IDimension")
        if dim.SetSystemValue3(value, C.swSetValue_InAllConfigurations, None) not in (0, None):
            raise SwError(f"SolidWorks rejected the new spacing ({name}).")
    doc.EditRebuild3()
    out = result(doc, f, "feature")
    out["changed"] = changed
    return out


# ------------------------------------------------------------------------------------------ reference geometry


@tool(WRITES)
def ref_plane(
    mode: Annotated[Literal["offset", "angle", "midplane"], Field(description="offset: parallel at a distance from refs[0]; angle: through refs[1] (edge/axis) at an angle to refs[0]; midplane: halfway between refs[0] and refs[1].")],
    refs: Annotated[list[str], Field(description="Reference planes, faces, edges or axes.")],
    distance: Annotated[float, Field(description="Offset distance in mm.")] = 0.0,
    angle: Annotated[float, Field(description="Angle in degrees.")] = 0.0,
    flip: Annotated[bool, Field(description="Put the plane on the other side.")] = False,
) -> dict:
    """Reference plane for sketches, mirrors and sections."""
    doc = SW.active("part")
    C = SW.C
    doc.ClearSelection2(True)
    fl = C.swRefPlaneReferenceConstraint_OptionFlip if flip else 0
    before = _last_feature(doc).Name
    for i, r in enumerate(refs[:3]):
        g.select(doc, r, mark=i)
    fm = doc.FeatureManager
    if mode == "offset":
        fm.InsertRefPlane(C.swRefPlaneReferenceConstraint_Distance | fl, mm(distance), 0, 0, 0, 0)
    elif mode == "angle":
        if len(refs) < 2:
            raise SwError("angle mode needs a plane and an edge or axis.")
        fm.InsertRefPlane(C.swRefPlaneReferenceConstraint_Angle | fl, rad(angle), C.swRefPlaneReferenceConstraint_Coincident, 0, 0, 0)
    else:
        if len(refs) < 2:
            raise SwError("midplane mode needs two planes or faces.")
        fm.InsertRefPlane(C.swRefPlaneReferenceConstraint_MidPlane, 0, C.swRefPlaneReferenceConstraint_MidPlane, 0, 0, 0)
    return created_since(doc, before, "reference plane")


@tool(WRITES)
def ref_axis(
    refs: Annotated[list[str], Field(description="A cylindrical face, a linear edge, or two planes.")],
) -> dict:
    """Reference axis (for circular patterns, revolves and angled planes)."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    before = _last_feature(doc).Name
    for r in refs:
        g.select(doc, r)
    doc.InsertAxis2(True)
    return created_since(doc, before, "reference axis")

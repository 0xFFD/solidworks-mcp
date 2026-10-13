"""Feature tools: boss/cut features, dress-up features, holes, patterns and reference geometry."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, const_name, mm, rad, to_mm
from .errors import SwError
from .registry import DESTRUCTIVE, WRITES, tool
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


def _apply_setback(doc: Any, f: Any, setback: list[dict[str, Any]]) -> None:
    """Add setback corners to a constant fillet. SolidWorks only takes them with tangent propagation off;
    when it refuses, the fillet is removed so nothing half-made is left."""
    data = SW.cast(f.GetDefinition(), "ISimpleFilletFeatureData2")
    data.AccessSelections(doc, None)
    try:
        edges = SW.cast_all(data.Edges, "IEdge")
        verts = []
        for i, sb in enumerate(setback):
            p = [mm(c) for c in sb.get("vertex") or []]
            if len(p) != 3 or not sb.get("distance") or float(sb["distance"]) <= 0:
                raise SwError(f"setback[{i}] needs vertex [x, y, z] and a distance above 0.")
            v = next((SW.cast(x, "IVertex") for e in edges for x in (e.GetStartVertex(), e.GetEndVertex())
                      if x is not None and math.dist(list(SW.cast(x, "IVertex").GetPoint())[:3], p) < 1e-5), None)
            if v is None:
                raise SwError(f"setback[{i}]: {sb['vertex']} is not a corner of the filleted edges.")
            verts.append((v, mm(float(sb["distance"]))))
        data.SetSetbackVertices(_dispatch_array([v for v, _ in verts]))
        for v, dist in verts:
            got = data.GetSetbackVertexDistance(v)
            at = SW.cast_all(got[1], "IEdge") if isinstance(got, tuple) and len(got) > 1 and got[1] else edges
            data.SetSetbackVertexDistance(v, _dispatch_array(at), _doubles([dist] * len(at)))
    except BaseException:
        data.ReleaseSelectionAccess()
        raise
    ok = f.ModifyDefinition(data, doc, None)
    doc.EditRebuild3()
    if not ok or SW.cast(f.GetDefinition(), "ISimpleFilletFeatureData2").GetSetbackVerticesCount() != len(verts):
        name = f.Name
        doc.ClearSelection2(True)
        f.Select2(False, 0)
        doc.Extension.DeleteSelection2(SW.C.swDelete_Absorbed)
        raise SwError(f"SolidWorks refused the setback on {name} (removed): each vertex needs three or more filleted edges, "
                      "and the setback plus the radius must fit on the edges.")


@tool(WRITES)
def fillet(
    radius: Annotated[float, Field(description="Radius in mm (constant and face fillets).")] = 0.0,
    edges: Annotated[list[str] | None, Field(description="Edges to round ('edge:N', 'edge@x,y,z'). Variable: one edge.")] = None,
    faces: Annotated[list[str] | None, Field(description="Constant: round every edge of these faces. Face fillet: the first face set. Full round: [side face, centre face, side face].")] = None,
    kind: Annotated[Literal["constant", "variable", "face", "full_round"], Field(description=(
        "constant: one radius on edges; variable: radius changing along one edge from radii[0] at its start to radii[1] at its end, "
        "or along a chain of connected edges with vertex_radii; "
        "face: blend between two face sets (faces and faces2) that need not share an edge; full_round: replace the centre face "
        "with a full round between two side faces (rib and boss ends)."))] = "constant",
    radii: Annotated[list[float] | None, Field(description="Variable fillet: [radius at start, radius at end] in mm (list_edges gives the edge's start and end).")] = None,
    vertex_radii: Annotated[list[dict[str, Any]] | None, Field(description=(
        "Variable fillet along a chain of edges: the radius at every vertex of the chain, [{vertex: [x, y, z], radius: mm}] "
        "(the blend varies smoothly between them; fairings, transitions, cast blends).") )] = None,
    faces2: Annotated[list[str] | None, Field(description="Face fillet: the second face set.")] = None,
    distance2: Annotated[float | None, Field(description="Constant: makes an asymmetric fillet with legs radius and distance2 (mm).")] = None,
    conic_rho: Annotated[float | None, Field(gt=0, lt=1, description="Constant: conic profile with this rho instead of a circular arc (0.5 is a parabola, below is flatter, above is fuller).")] = None,
    tangent_propagation: bool = True,
    setback: Annotated[list[dict[str, Any]] | None, Field(description=(
        "Constant: setback corners where three or more filleted edges meet, [{vertex: [x, y, z], distance: mm}]; the blend "
        "starts that far back along each edge for a smoother corner (castings, machined pockets). Turns tangent propagation off."))] = None,
) -> dict:
    """Fillets: constant radius on edges (optionally asymmetric, with a conic profile or setback corners), variable
    radius along an edge, face fillets between face sets, and full round fillets."""
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
        if distance2 is not None:
            data.AsymmetricFillet = True
            data.DefaultDistance = mm(distance2)
        if conic_rho is not None:
            data.ConicTypeForCrossSectionProfile = C.swFeatureFilletConicRho
            data.DefaultConicRhoOrRadius = float(conic_rho)
        data.PropagateToTangentFaces = tangent_propagation and not setback
        data.Edges = _dispatch_array(_edges_from(doc, edges, faces))
        out = result(doc, fm.CreateFeature(data), "fillet", "The radius may be too large for the adjacent faces.")
        if setback:
            _apply_setback(doc, g.feature_by_name(doc, out["feature"]), setback)
            out = result(doc, g.feature_by_name(doc, out["feature"]), "fillet")
            out["setback"] = setback
        return out
    # variable, face and full round fillets go through FeatureFillet3 with the selection marks it expects
    before = _volume(doc)
    doc.ClearSelection2(True)
    radii_arr = None
    chain_verts: list[tuple[list[float], Any, float]] = []
    if kind == "variable":
        if vertex_radii:
            if not edges:
                raise SwError("A variable fillet needs its edges.")
            edge_objs = _edges_from(doc, edges, None)
            verts: list[tuple[list[float], Any]] = []
            for e in edge_objs:
                for v in (SW.cast(e, "IEdge").GetStartVertex(), SW.cast(e, "IEdge").GetEndVertex()):
                    if v is not None:
                        pt = list(SW.cast(v, "IVertex").GetPoint())[:3]
                        if all(math.dist(pt, q) > 1e-7 for q, _ in verts):
                            verts.append((pt, v))
            for pt, v in verts:  # every vertex of the chain needs its radius
                got = [vr for vr in vertex_radii if math.dist([mm(c) for c in vr.get("vertex", [])] or [1e9] * 3, pt) < mm(0.05)]
                if not got:
                    raise SwError(f"vertex_radii has no radius for the chain vertex at {[round(to_mm(c), 3) for c in pt]}; "
                                  f"the chain's vertices are {[[round(to_mm(c), 3) for c in q] for q, _ in verts]}.")
                r = float(got[0].get("radius", 0))
                if r <= 0:
                    raise SwError("Every vertex radius must be above 0.")
                chain_verts.append((pt, v, r))
            for e in edge_objs:
                SW.cast(e, "IEntity").Select4(True, None)
            radius = chain_verts[0][2]
            ftyp, radii_arr = C.swFeatureFilletType_VariableRadius, _doubles([mm(radius)] * len(chain_verts))
        else:
            if not edges or len(edges) != 1 or not radii or len(radii) != 2 or min(radii) <= 0:
                raise SwError("A variable fillet needs one edge and radii=[start, end], both above 0, or edges with vertex_radii.")
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
    if chain_verts:  # SolidWorks orders the radii its own way: set each vertex's radius on the feature itself
        feat = SW.cast(g.feature_by_name(doc, out["feature"]), "IFeature")
        data = SW.cast(feat.GetDefinition(), "IVariableFilletFeatureData2")
        data.AccessSelections(doc, None)
        own = []  # the feature's own vertex objects, matched to the given ones by position
        for i in range(len(edges or [])):
            e = SW.cast(data.GetFilletEdgeAtIndex(i), "IEdge")
            for v in (e.GetStartVertex(), e.GetEndVertex()) if e is not None else ():
                if v is not None:
                    own.append((list(SW.cast(v, "IVertex").GetPoint())[:3], v))
        chain_verts = [(pt, min(own, key=lambda o: math.dist(o[0], pt))[1] if own else v, r) for pt, v, r in chain_verts]
        for _, v, r in chain_verts:
            data.SetRadius(v, mm(r))
        if not feat.ModifyDefinition(data, doc, None):
            raise SwError("SolidWorks rejected the vertex radii; a radius may be too large for the faces there.")
        doc.EditRebuild3()
        data = SW.cast(feat.GetDefinition(), "IVariableFilletFeatureData2")
        data.AccessSelections(doc, None)
        got = [round(to_mm(data.GetRadius(v)), 4) for _, v, _ in chain_verts]
        data.ReleaseSelectionAccess()
        if any(abs(a - r) > 1e-3 for a, (_, _, r) in zip(got, chain_verts)):
            raise SwError(f"The vertex radii came out as {got}, not as given.")
        out = result(doc, feat, what)
        out["vertex_radii"] = [{"vertex": [round(to_mm(c), 3) for c in pt], "radius": r} for pt, _, r in chain_verts]
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


def _place_on_curved(doc: Any, feat: Any, sub: Any, sk: Any, p: list[float], raw: list[float], standard: str, fname: str) -> dict:
    """Move the 3D placement point of a hole on a curved face to `p` and check the hole is drilled there,
    square to the surface."""
    C = SW.C
    doc.ClearSelection2(True)
    sub.Select2(False, 0)
    doc.EditSketch()
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        SW.cast(sk.GetSketchPoints2()[0], "ISketchPoint").SetCoords(*p)
    finally:
        sm.AddToDB = False
        sm.InsertSketch(True)
    doc.EditRebuild3()
    placed = False
    for f in g.faces(doc):
        ff = f.GetFeature()
        surf = SW.cast(f.GetSurface(), "ISurface")
        if ff is None or SW.cast(ff, "IFeature").Name != feat.Name or not surf.IsCylinder():
            continue
        cp = list(surf.CylinderParams)
        o, a = cp[0:3], cp[3:6]
        d = [p[i] - o[i] for i in range(3)]
        t = sum(d[i] * a[i] for i in range(3))
        if math.dist(p, [o[i] + a[i] * t for i in range(3)]) < 1e-5:
            placed = True
            break
    if not placed:
        doc.ClearSelection2(True)
        feat.Select2(False, 0)
        doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
        raise SwError(f"The hole at {raw} did not end up on that point of the curved face. Nothing was created.")
    out = result(doc, feat, "hole")
    out["holes"] = 1
    out["table"] = f"{standard}/{fname}"
    if str(out.get("rebuild", "")).startswith("warning") and fname in ("tapped", "bottoming_tapped"):
        out["note"] = ("SolidWorks flags tapped holes that start on a curved face with a rebuild warning; the hole is cut "
                       "square to the surface. Spot-face the boss (a counterbore) if the thread start must be flat.")
    return out


@tool(WRITES)
def hole(
    size: Annotated[str, Field(description="Fastener or drill size from the Hole Wizard tables, e.g. 'M6', 'M5', '#10', '1/4', '1/4-20', 'Ø6.6'.")],
    positions: Annotated[list[list[float]], Field(description="Hole centres [[x, y, z], ...] in model mm on planar or curved faces (radial holes in tubes and shafts are drilled square to the surface); positions on different faces, and each position on a curved face, give their own hole feature. Placed exactly.")],
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
    # one Hole Wizard feature per planar face, and per hole on a curved face (its placement is a 3D sketch
    # point bound to the surface, so points added to it drill nothing)
    groups: list[tuple[Any, list[list[float]]]] = []
    for raw in positions:
        fobj = g.resolve(doc, face if face else "face@{},{},{}".format(*raw)).obj
        curved = not SW.cast(fobj.GetSurface(), "ISurface").IsPlane() if hasattr(fobj, "GetSurface") else False
        for gf, members in groups:
            if not curved and SW.app.IsSame(gf, fobj) == 1:
                members.append(raw)
                break
        else:
            groups.append((fobj, [raw]))
    if len(groups) > 1:
        done: list[dict] = []
        try:
            for _, members in groups:
                done.append(hole(size, members, kind, standard, fastener, face, depth, thread_depth, fit, cosmetic_thread))
        except SwError:
            # all or nothing: remove the hole features already made for earlier faces
            for d in reversed(done):
                f = g.feature_by_name(doc, d["feature"])
                if f is not None:
                    doc.ClearSelection2(True)
                    f.Select2(False, 0)
                    doc.Extension.DeleteSelection2(C.swDelete_Absorbed)
            doc.ClearSelection2(True)
            raise
        return {"features": [d["feature"] for d in done], "holes": sum(d["holes"] for d in done), "faces": len(groups),
                "table": done[0]["table"], "volume_mm3": done[-1].get("volume_mm3")}
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
    if sk.Is3D():  # a curved face: the single placement point moves in model coordinates
        return _place_on_curved(doc, feat, sub, sk, pts[0], positions[0], standard, fname)
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
def driven_pattern(
    features: Annotated[list[str], Field(description="Feature names to pattern.")],
    points: Annotated[list[list[float]] | None, Field(description="Table-driven: model points [[x, y, z], ...] in mm for the copies (on one face or plane); a point sketch is made for them.")] = None,
    on: Annotated[str | None, Field(description="Table-driven: face or plane for the point sketch; default is the face under the first point.")] = None,
    sketch: Annotated[str | None, Field(description="Sketch-driven: an existing sketch whose points place the copies.")] = None,
    curve: Annotated[str | None, Field(description="Curve-driven: an edge or sketch the copies follow ('edge@x,y,z', 'sketch:Sketch3').")] = None,
    count: Annotated[int, Field(ge=2, description="Curve-driven: instances including the seed.")] = 2,
    spacing: Annotated[float | None, Field(description="Curve-driven: spacing along the curve in mm; omit to spread the instances evenly over the whole curve.")] = None,
    reverse: Annotated[bool, Field(description="Curve-driven: go the other way along the curve.")] = False,
) -> dict:
    """Pattern features at arbitrary positions (table-driven, from a coordinate list), at the points of a
    sketch, or along a curve (fastener rows that follow an edge, rivet lines, lightening holes)."""
    doc = SW.active("part")
    modes = [m for m, v in (("points", points), ("sketch", sketch), ("curve", curve)) if v]
    if len(modes) != 1:
        raise SwError("Give exactly one of points, sketch or curve.")
    if points:
        from .tools_sketch import _add, start_sketch

        start_sketch(on or "face@{},{},{}".format(*points[0]))
        sm = doc.SketchManager
        frame = SketchFrame(SW.cast(sm.ActiveSketch, "ISketch"))
        sm.AddToDB = True
        try:
            for p in points:
                q = frame.to_sketch([mm(c) for c in p])
                if abs(q[2]) > 1e-6:
                    raise SwError(f"Point {p} is not on the sketch face or plane; give on= or points on one face.")
                _add(doc, sm, {"type": "point", "at": [q[0] * 1000, q[1] * 1000]})
        finally:
            sm.AddToDB = False
        sketch = last_sketch(doc)
        sm.InsertSketch(True)
    before = _last_feature(doc).Name
    doc.ClearSelection2(True)
    _select_seeds(doc, features, 4)
    if curve:
        g.select(doc, curve if ":" in curve or "@" in curve else f"sketch:{curve}", mark=1)
        doc.FeatureCurvePattern(count, mm(spacing or 0), 1, 0.0, reverse, False, spacing is None, False, True, False, False, False)
        return created_since(doc, before, "curve pattern", "Check that the curve runs past the seed and that every instance stays on the part.")
    g.select(doc, f"sketch:{sketch}", mark=64)
    f = doc.FeatureManager.FeatureSketchDrivenPattern(True, False)
    out = result(doc, f, "sketch-driven pattern", "Every sketch point must leave the copy on the part.")
    out["instances"] = len(points) if points else None
    return out


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


@tool(WRITES)
def split_body(
    tools: Annotated[list[str], Field(description="Cutting planes, faces, sketches or surface bodies ('plane:Plane1', 'face@x,y,z', 'sketch:Sketch3', 'body:Surface-Extrude1').")],
) -> dict:
    """Split the part's solid bodies with planes, faces or sketches into separate bodies (machining blanks,
    sections of a long part, master-model splits). The new bodies keep their volumes and can be combined,
    moved, patterned or deleted with the other body tools."""
    import pythoncom
    from win32com.client import VARIANT

    doc = SW.active("part")
    fm = doc.FeatureManager
    doc.ClearSelection2(True)
    for t in tools:
        g.select(doc, t if ":" in t or "@" in t else f"feature:{t}")
    existing = len(g.bodies(doc))
    pieces = fm.PreSplitBody2()  # every resulting body, the untouched ones included
    solid_pieces = [b for b in SW.cast_all(pieces, "IBody2") if b.GetType() == SW.C.swSolidBody]
    if len(solid_pieces) <= existing:
        doc.ClearSelection2(True)
        raise SwError("The tools do not cut any body into pieces; they must pass right through it.")
    bs = SW.cast_all(pieces, "IBody2")
    f = fm.PostSplitBody2(_dispatch_array(bs), False, VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [None] * len(bs)),
                          VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [""] * len(bs)), "")
    out = result(doc, f, "split")
    out["bodies"] = [b.Name for b in g.bodies(doc)]
    return out


@tool(DESTRUCTIVE)
def delete_body(
    bodies: Annotated[list[str], Field(description="Bodies ('body:Name').")],
    keep: Annotated[bool, Field(description="Keep these bodies and delete all the others instead.")] = False,
) -> dict:
    """Delete solid bodies from a multibody part, or keep only the given ones (as a feature, so it can be
    suppressed or deleted later)."""
    doc = SW.active("part")
    doc.ClearSelection2(True)
    for b in bodies:
        g.Ref(_body(doc, b), "body", b).select(doc)
    f = doc.FeatureManager.InsertDeleteBody2(keep)
    out = result(doc, f, "body delete")
    out["bodies"] = [b.Name for b in g.bodies(doc)]
    return out


# ------------------------------------------------------------------------------------------ surfaces


def _surfaces(doc: Any) -> list[str]:
    return [b.Name for b in g.surface_bodies(doc)]


@tool(WRITES)
def surface(
    kind: Annotated[Literal["extrude", "revolve", "loft", "planar", "offset"], Field(description=(
        "extrude: a sketch (open or closed) pulled into a surface; revolve: a sketch about its centerline or axis; "
        "loft: through two or more profile sketches (skins, fairings); planar: fill a closed sketch; offset: copy faces at a distance."))],
    sketch: Annotated[str | None, Field(description="extrude, revolve, planar: the sketch; default is the latest sketch.")] = None,
    profiles: Annotated[list[str] | None, Field(description="loft: profile sketches in order.")] = None,
    faces: Annotated[list[str] | None, Field(description="offset: the faces to offset.")] = None,
    depth: Annotated[float, Field(description="extrude: depth in mm.")] = 10.0,
    angle: Annotated[float, Field(description="revolve: angle in degrees.")] = 360.0,
    axis: Annotated[str | None, Field(description="revolve: axis reference when the sketch has no centerline.")] = None,
    distance: Annotated[float, Field(description="offset: distance in mm (0 copies the faces).")] = 0.0,
    reverse: bool = False,
    closed: Annotated[bool, Field(description="loft: close the loft back to the first profile.")] = False,
) -> dict:
    """Surface bodies: cutting and splitting tools (split_body(tools=['body:Surface-Extrude1'])), parting
    faces, and reference geometry for aerodynamic skins and fairings. list_bodies lists them."""
    doc = SW.active("part")
    C = SW.C
    fm = doc.FeatureManager
    before = _last_feature(doc).Name
    n0 = len(_surfaces(doc))
    if kind in ("extrude", "revolve", "planar"):
        name = _select_sketch(doc, sketch)
        if kind == "extrude":
            fm.FeatureExtruRefSurface3(True, reverse, 0, 0.0, C.swEndCondBlind, C.swEndCondBlind, mm(depth), 0.0, False, False, False, False,
                                       0.0, 0.0, False, False, False, False, False, False, False, False)
        elif kind == "revolve":
            if axis:
                g.select(doc, axis, mark=4)
            fm.InsertRevolvedRefSurface(rad(angle), reverse, 0.0, 0)
        else:
            doc.InsertPlanarRefSurface()
        hint = f"Check {name}" + (" (a planar surface needs one closed outline)." if kind == "planar" else ".")
    elif kind == "loft":
        if not profiles or len(profiles) < 2:
            raise SwError("A loft surface needs at least two profiles.")
        doc.ClearSelection2(True)
        for p in profiles:
            g.select(doc, p if ":" in p else f"sketch:{p}", mark=1)
        doc.InsertLoftRefSurface2(closed, True, False, 1.0, 0, 0)
        hint = "Profiles should not cross each other."
    else:
        if not faces:
            raise SwError("An offset surface needs faces.")
        doc.ClearSelection2(True)
        for f in faces:
            g.select(doc, f)
        doc.InsertOffsetSurface(mm(distance), reverse)
        hint = "The offset may be larger than the smallest radius of curvature."
    out = created_since(doc, before, f"{kind} surface", hint)
    out.pop("volume_mm3", None)
    out["surfaces"] = _surfaces(doc)
    if len(out["surfaces"]) <= n0:
        out["note"] = "No new surface body was reported; the surface may have joined an existing one."
    return out


# ------------------------------------------------------------------------------------------ editing

_DEFINITIONS = {
    "Extrusion": "IExtrudeFeatureData2", "Boss": "IExtrudeFeatureData2", "Cut": "IExtrudeFeatureData2", "ICE": "IExtrudeFeatureData2",
    "BaseBody": "IExtrudeFeatureData2", "Revolution": "IRevolveFeatureData2", "RevCut": "IRevolveFeatureData2",
    "BaseRevolve": "IRevolveFeatureData2", "Fillet": "ISimpleFilletFeatureData2", "Chamfer": "IChamferFeatureData2",
    "Shell": "IShellFeatureData", "LPattern": "ILinearPatternFeatureData", "CirPattern": "ICircularPatternFeatureData",
    "HoleWzd": "IWizardHoleFeatureData2", "Draft": "IDraftFeatureData2", "Sweep": "ISweepFeatureData", "SweepCut": "ISweepFeatureData",
    "Blend": "ILoftFeatureData", "BlendCut": "ILoftFeatureData", "RefPlane": "IRefPlaneFeatureData",
    "RefAxis": "IRefAxisFeatureData",
}
_HOLE_STANDARDS = {"ISO": "iso", "ANSI METRIC": "ansi_metric", "ANSI INCH": "ansi_inch"}


def _hole_table(data: Any, standard: str | None, fastener: str | None) -> tuple[int, int, str, str]:
    """Standard and fastener enums for a Hole Wizard redefinition: given, or inferred from the feature."""
    std = standard or _HOLE_STANDARDS.get(str(data.Standard).strip().upper())
    if std not in _FASTENERS:
        raise SwError(f"Unknown hole standard '{data.Standard}'; pass standard=.")
    table = _FASTENERS[std]
    if fastener is None:
        text = str(data.FastenerType).replace(" ", "").lower()
        fastener = next((k for k, v in table.items() if v.lower().endswith(text) or text.endswith(v.lower().removeprefix(std.replace("_", "")))), None)
        if fastener is None:
            raise SwError(f"Could not tell which hole table '{data.FastenerType}' is; pass fastener= (one of {sorted(table)}).")
    if fastener not in table:
        raise SwError(f"Fastener '{fastener}' is not available for {std}. Choose from {sorted(table)}.")
    return getattr(SW.C, _STANDARD[std]), getattr(SW.C, "swStandard" + table[fastener]), std, fastener


def _dispatch_array(objs: list[Any]) -> Any:
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [o._oleobj_ for o in objs])


def _dimension_with_value(f: Any, value: float, skip: list[str]) -> str:
    """Name of the feature's linear dimension that currently has `value` (metres)."""
    C = SW.C
    found = []
    dd = f.GetFirstDisplayDimension()
    while dd is not None:
        d = SW.cast(SW.cast(dd, "IDisplayDimension").GetDimension2(0), "IDimension")
        name = "@".join(d.FullName.split("@")[:2])
        v = d.GetSystemValue3(C.swThisConfiguration, None)
        v = v[0] if isinstance(v, (tuple, list)) else v
        if d.GetType() == C.swDimensionParamTypeDoubleLinear and abs(v - value) < 1e-9 and name not in skip:
            found.append(name)
        dd = f.GetNextDisplayDimension(dd)
    if not found:
        raise SwError(f"Could not find the spacing dimension of {f.Name}; use dimensions() and set_dimensions instead.")
    return found[0]


def _sketch_feature(doc: Any, name: str) -> Any:
    f = g.feature_by_name(doc, name)
    if f is None or f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
        raise SwError(f"'{name}' is not a sketch.")
    return f


@tool(WRITES)
def edit_feature(
    feature: Annotated[str, Field(description="Feature name from list_features.")],
    end: Annotated[Literal["blind", "through_all", "through_all_both", "midplane", "up_to_next"] | None, Field(description="Extrude/cut: new end condition.")] = None,
    depth: Annotated[float | None, Field(description="Extrude/cut: depth in mm (direction 1). Hole: hole depth (makes it blind).")] = None,
    depth2: Annotated[float | None, Field(description="Extrude/cut: blind depth in the second direction; 0 removes the second direction.")] = None,
    reverse: Annotated[bool | None, Field(description="Extrude/cut/revolve: flip direction 1. Reference plane: put it on the other side of its reference.")] = None,
    flip_side_to_cut: Annotated[bool | None, Field(description="Cut: remove the material outside the profile instead.")] = None,
    merge: Annotated[bool | None, Field(description="Boss: merge with the existing bodies.")] = None,
    angle: Annotated[float | None, Field(description="Degrees. Revolve: angle; draft: draft angle; chamfer: angle; circular pattern: total angle (equal spacing) or angle between instances.")] = None,
    radius: Annotated[float | None, Field(description="Fillet: radius in mm.")] = None,
    distance: Annotated[float | None, Field(description="Chamfer: distance in mm. Offset reference plane: offset in mm.")] = None,
    thickness: Annotated[float | None, Field(description="Shell: wall thickness in mm.")] = None,
    edges: Annotated[list[str] | None, Field(description="Fillet/chamfer: replace the edges it rounds ('edge@x,y,z', 'edge:N').")] = None,
    count: Annotated[int | None, Field(ge=1, description="Pattern: instances in direction 1 (including the seed).")] = None,
    spacing: Annotated[float | None, Field(description="Linear pattern: spacing in mm.")] = None,
    count2: Annotated[int | None, Field(ge=1, description="Linear pattern: instances in direction 2.")] = None,
    spacing2: Annotated[float | None, Field(description="Linear pattern: spacing in direction 2, mm.")] = None,
    size: Annotated[str | None, Field(description="Hole: new size from the Hole Wizard table, e.g. 'M6' (keeps the hole type and positions).")] = None,
    standard: Annotated[Literal["iso", "ansi_metric", "ansi_inch"] | None, Field(description="Hole: standard for size= (default: the hole's own).")] = None,
    fastener: Annotated[str | None, Field(description="Hole: table for size= such as clearance, tapped or socket_head (default: the hole's own).")] = None,
    diameter: Annotated[float | None, Field(description="Sweep with a circular profile: new diameter in mm.")] = None,
    twist: Annotated[float | None, Field(description="Sweep: twist angle along the path in degrees.")] = None,
    profiles: Annotated[list[str] | None, Field(description="Loft: the profile sketches in order (replace, add or reorder sections). Sweep: [the new profile sketch].")] = None,
    path: Annotated[str | None, Field(description="Sweep: a new path sketch.")] = None,
    close: Annotated[bool | None, Field(description="Loft: close the loft back to its first profile (three or more profiles).")] = None,
    refs: Annotated[list[str] | None, Field(description=(
        "Reference plane: new references in the same order as it was made (e.g. ['face@0,10,0'] to re-base an offset plane "
        "on a datum face, or [plane, edge] for an angled plane). Reference axis: its new references (a cylindrical face, "
        "a linear edge, two planes or two points).") )] = None,
) -> dict:
    """Change how an existing feature is defined without rebuilding it from scratch: an extrude's end
    condition (blind to through-all and back), depth, direction and second side; revolve angle; fillet
    radius or edges; chamfer distance and angle; shell thickness; pattern counts and spacing; Hole Wizard
    size, depth and end condition; draft angle; sweep diameter and twist; a reference plane's references,
    offset, angle and side; a reference axis's references. Plain driving dimensions can also be changed with set_dimensions."""
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
                    # linear pattern dimensions are named D1/D2 (counts) and D3/D4 (spacings of direction 1 and 2)
                    by_name = f"{'D3' if prop == 'D1Spacing' else 'D4'}@{f.Name}"
                    dim_name = by_name if doc.Parameter(by_name) is not None else _dimension_with_value(f, getattr(data, prop), [n for n, _ in spacing_dims])
                    spacing_dims.append((dim_name, mm(val)))
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
        elif iface == "IWizardHoleFeatureData2":
            if size is not None:
                std_e, fast_e, std, fastener = _hole_table(data, standard, fastener)
                if not data.ChangeStandard(std_e, fast_e, size):
                    raise SwError(f"Hole Wizard has no size '{size}' in {std}/{fastener}.")
                changed.append("size")
            if end is not None:
                if end not in ("blind", "through_all"):
                    raise unsupported(f"end='{end}'")
                data.EndCondition = getattr(C, END[end])
                changed.append("end")
            if depth is not None:
                data.EndCondition = C.swEndCondBlind
                data.HoleDepth = mm(depth)
                data.Depth = mm(depth)
                changed.append("depth")
            if reverse is not None:
                data.ReverseDirection = reverse
                changed.append("reverse")
        elif iface == "IDraftFeatureData2":
            if angle is not None:
                data.Angle = rad(angle)
                changed.append("angle")
            if reverse is not None:
                data.ReverseDirection = reverse
                changed.append("reverse")
        elif iface == "ILoftFeatureData":
            if profiles is not None:
                if len(profiles) < 2:
                    raise SwError("A loft needs two or more profiles.")
                data.Profiles = _dispatch_array([_sketch_feature(doc, x) for x in profiles])
                changed.append("profiles")
            if close is not None:
                data.Close = close
                changed.append("close")
            if merge is not None:
                data.Merge = merge
                changed.append("merge")
        elif iface == "ISweepFeatureData":
            if profiles is not None:
                if data.CircularProfile or len(profiles) != 1:
                    raise unsupported("profiles (give one sketch, for a sweep with a sketched profile)")
                data.Profile = _sketch_feature(doc, profiles[0])
                changed.append("profiles")
            if path is not None:
                data.Path = _sketch_feature(doc, path)
                changed.append("path")
            if diameter is not None:
                if not data.CircularProfile:
                    raise unsupported("diameter (the sweep has a sketched profile; edit the sketch)")
                data.CircularProfileDiameter = mm(diameter)
                changed.append("diameter")
            if twist is not None:
                data.TwistControlType = C.swTwistControlConstantTwistAlongPath if twist else C.swTwistControlFollowPath
                data.SetTwistAngle(rad(twist))
                changed.append("twist")
            if merge is not None:
                data.Merge = merge
                changed.append("merge")
        elif iface == "IRefPlaneFeatureData":
            first = data.Constraint(0)
            if refs is not None:
                n = sum(1 for i in range(3) if data.Constraint(i))  # GetSelectionsCount is write-only here
                if len(refs) != n:
                    raise SwError(f"{feature} has {n} reference(s); give that many refs in the same order (replacing a reference keeps its role).")
                for i, r in enumerate(refs):
                    data.SetReference(i, g.resolve(doc, r).obj)
                changed.append("refs")
            if distance is not None:
                if not first & C.swRefPlaneReferenceConstraint_Distance:
                    raise unsupported("distance (the plane is not an offset plane)")
                data.SetAngleOrDistance(0, mm(abs(distance)))
                changed.append("distance")
            if angle is not None:
                if not first & C.swRefPlaneReferenceConstraint_Angle:
                    raise unsupported("angle (the plane is not an angled plane)")
                data.SetAngleOrDistance(0, rad(angle))
                changed.append("angle")
            if reverse is not None:
                flip = C.swRefPlaneReferenceConstraint_OptionFlip
                data.SetConstraint(0, (first & ~flip) | (flip if reverse else 0))
                changed.append("reverse")
        elif iface == "IRefAxisFeatureData":
            if refs is not None:  # SolidWorks works out the axis type from the new references
                data.SetSelections(_dispatch_array([g.resolve(doc, r).obj for r in refs]))
                changed.append("refs")
        if iface == "IChamferFeatureData2" and angle is not None:
            data.EdgeChamferAngle = rad(angle)
            changed.append("angle")
        if edges is not None:
            if iface not in ("ISimpleFilletFeatureData2", "IChamferFeatureData2"):
                raise unsupported("edges")
            data.Edges = _dispatch_array(_edges_from(doc, edges, None))
            changed.append("edges")
        given = {k for k, v in dict(end=end, depth=depth, depth2=depth2, reverse=reverse, flip_side_to_cut=flip_side_to_cut, merge=merge, angle=angle,
                                    radius=radius, distance=distance, thickness=thickness, count=count, spacing=spacing, count2=count2,
                                    spacing2=spacing2, size=size, diameter=diameter, twist=twist, profiles=profiles, path=path,
                                    close=close, refs=refs).items() if v is not None}
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
    if t == "RefPlane":
        arr = list(SW.cast(SW.cast(f.GetSpecificFeature2(), "IRefPlane").Transform, "IMathTransform").ArrayData)
        out["plane"] = {"origin_mm": [round(to_mm(v), 4) + 0.0 for v in arr[9:12]], "normal": [round(v, 6) + 0.0 for v in arr[6:9]]}
    elif t == "RefAxis":
        ax = [round(to_mm(v), 4) + 0.0 for v in SW.cast(f.GetSpecificFeature2(), "IRefAxis").GetRefAxisParams()]
        out["axis"] = {"start_mm": ax[:3], "end_mm": ax[3:6]}
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


def _select_bodies(doc: Any, refs: list[str] | None, mark: int) -> list[Any]:
    """Select bodies (all surface bodies when refs is None) with the given mark; SolidWorks' knit and thicken
    only see bodies selected with mark 1."""
    objs = [g.resolve(doc, r if r.startswith("body:") else f"body:{r}").obj for r in refs] if refs else g.surface_bodies(doc)
    if not objs:
        raise SwError("There are no surface bodies; make them with surface().")
    doc.ClearSelection2(True)
    sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
    for b in objs:
        data = SW.cast(sm.CreateSelectData(), "ISelectData")
        data.Mark = mark
        if not SW.cast(b, "IBody2").Select2(True, data):
            raise SwError(f"Could not select {SW.cast(b, 'IBody2').Name}.")
    return objs


@tool(WRITES)
def knit_surfaces(
    bodies: Annotated[list[str] | None, Field(description="Surface bodies to knit ('body:Name' from list_bodies); default all of them.")] = None,
    form_solid: Annotated[bool, Field(description="Make a solid when the surfaces close a volume (a skin around a bay, a fairing).")] = False,
    tolerance: Annotated[float, Field(gt=0, description="Knit tolerance, mm: edges closer than this are joined.")] = 0.0025,
) -> dict:
    """Knit surface bodies along their shared edges into one surface, or into a solid when they enclose a volume."""
    doc = SW.active("part")
    before_s, before_solid = len(g.surface_bodies(doc)), len(g.bodies(doc))
    picked = _select_bodies(doc, bodies, 1)
    if len(picked) < 2 and not form_solid:
        raise SwError("Knit needs two or more surface bodies.")
    # forming a solid only happens with the gap filters on
    f = doc.FeatureManager.InsertSewRefSurface(form_solid, form_solid, True, mm(tolerance), mm(tolerance))
    out = result(doc, f, "surface knit", "The surfaces must meet along shared edges (within the tolerance).")
    after_s, after_solid = len(g.surface_bodies(doc)), len(g.bodies(doc))
    if form_solid and after_solid <= before_solid:
        raise SwError("The knit did not close into a solid; check for gaps or a missing face (list_bodies).")
    if not form_solid and after_s >= before_s:
        raise SwError("The surfaces were not joined; they must share edges within the tolerance.")
    out.update(surface_bodies=after_s, solid_bodies=after_solid)
    return out


@tool(WRITES)
def thicken(
    body: Annotated[str, Field(description="Surface body to thicken ('body:Name' from list_bodies).")],
    thickness: Annotated[float, Field(gt=0, description="Thickness in mm (each side when side='both').")],
    side: Annotated[Literal["normal", "reverse", "both"], Field(description="Grow along the surface normal, against it, or both ways.")] = "normal",
    merge: Annotated[bool, Field(description="Merge with solids it touches.")] = True,
) -> dict:
    """Thicken a surface into a solid: skins, fairings and panels modelled as surfaces, made solid for mass
    properties, drawings and manufacture."""
    doc = SW.active("part")
    before = _volume(doc) or 0.0
    _select_bodies(doc, [body], 1)
    f = doc.FeatureManager.FeatureBossThicken(mm(thickness), {"normal": 0, "reverse": 1, "both": 2}[side], 0, False, merge, True, True)
    out = result(doc, f, "thicken", "The surface may curve too tightly for that thickness.")
    if (out.get("volume_mm3") or 0.0) <= before:
        raise SwError("The thicken added no material.")
    return out


def _with_split_halves(doc: Any, faces: list[Any]) -> list[Any]:
    """Add the other pieces of split cylinders (STEP and IGES imports split each cylinder into halves), so a
    hole is selected whole."""
    out = list(faces)
    cyls = []
    for f in faces:
        s = SW.cast(f.GetSurface(), "ISurface")
        if s.IsCylinder():
            p = list(s.CylinderParams)
            cyls.append((p[0:3], p[3:6], p[6]))
    if not cyls:
        return out
    for f in g.faces(doc):
        s = SW.cast(f.GetSurface(), "ISurface")
        if not s.IsCylinder() or any(SW.app.IsSame(f, x) == 1 for x in out):
            continue
        p = list(s.CylinderParams)
        for o, a, r in cyls:
            d = [p[i] - o[i] for i in range(3)]
            t = sum(d[i] * a[i] for i in range(3))
            off = math.dist(p[0:3], [o[i] + a[i] * t for i in range(3)])
            if abs(p[6] - r) < 1e-7 and off < 1e-7 and abs(abs(sum(p[3 + i] * a[i] for i in range(3))) - 1) < 1e-9:
                out.append(f)
                break
    return out


@tool(WRITES)
def delete_faces(
    faces: Annotated[list[str], Field(min_length=1, description="Faces to remove ('face@x,y,z'); the other halves of a split hole are added automatically.")],
    patch: Annotated[bool, Field(description="Heal the gap by extending the neighbouring faces (removes holes, pockets, fillets and bosses); false leaves an open surface.")] = True,
) -> dict:
    """Direct editing: delete faces and heal the body, to remove holes, pockets, fillets or logos from any body,
    including imported STEP and IGES parts that have no feature history (defeaturing supplier models)."""
    doc = SW.active("part")
    picked = _with_split_halves(doc, [g.resolve(doc, f).obj for f in faces])
    before_faces, before_vol = len(g.faces(doc)), _volume(doc)
    doc.ClearSelection2(True)
    for f in picked:
        g.Ref(f, "face", "face").select(doc, mark=1)
    ok = doc.InsertDeleteFace2(1 if patch else 0)
    doc.ClearSelection2(True)
    doc.EditRebuild3()
    if not ok or len(g.faces(doc)) >= before_faces:
        raise SwError("SolidWorks could not delete and heal those faces; include every face of the feature (all walls and the floor).")
    out: dict[str, Any] = {"deleted_faces": len(picked)}
    if (v := _volume(doc)) is not None:
        out["volume_mm3"] = v
        if before_vol is not None:
            out["volume_change_mm3"] = round(v - before_vol, 3)
    return out


@tool(WRITES)
def move_faces(
    faces: Annotated[list[str], Field(min_length=1, description="Faces to move ('face@x,y,z').")],
    distance: Annotated[float, Field(description="Offset along each face's outward normal, mm (negative moves inward).")],
) -> dict:
    """Direct editing: offset faces of any body, including imported parts with no feature history (thicken a wall,
    lengthen a boss, open up a clearance)."""
    doc = SW.active("part")
    C = SW.C
    before = _volume(doc)
    doc.ClearSelection2(True)
    for f in faces:
        g.resolve(doc, f).select(doc, mark=1)
    feat = doc.FeatureManager.InsertMoveFace3(C.swMoveFaceTypeOffset, distance < 0, 0.0, mm(abs(distance)), None, None, C.swEndCondBlind, 0.0)
    out = result(doc, feat, "move face", "The faces must be on a solid, and the offset must not collapse a neighbouring face.")
    if before is not None and out.get("volume_mm3") == before:
        raise SwError("The face move changed nothing.")
    return out


@tool(WRITES)
def fill_surface(
    edges: Annotated[list[str], Field(min_length=1, description="Closed loop of boundary edges to fill ('edge@x,y,z'), of solids or surfaces.")],
    knit: Annotated[bool, Field(description="Knit the patch to the surfaces it borders (and make a solid if that closes a volume).")] = False,
) -> dict:
    """Patch a surface across a closed loop of edges: close openings in skins, cap ducts and fairings, repair
    missing faces of imported models."""
    doc = SW.active("part")
    objs = [g.resolve(doc, e).obj for e in edges]
    before = {b.Name for b in g.surface_bodies(doc)}
    import pythoncom
    from win32com.client import VARIANT

    doc.ClearSelection2(True)
    feat = doc.FeatureManager.InsertFillSurface2(1, 0, _dispatch_array(objs), VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_I4, [0] * len(objs)), None, None)
    out = result(doc, feat, "fill surface", "The edges must form one closed loop.")
    new = [b for b in g.surface_bodies(doc) if b.Name not in before]
    if not new:
        raise SwError("The fill made no surface; the edges must form one closed loop.")
    out["patch"] = {"name": new[0].Name, "area_mm2": round(sum(SW.cast(fc, "IFace2").GetArea() for fc in SW.cast_all(new[0].GetFaces(), "IFace2")) * 1e6, 3)}
    if knit:
        out["knit"] = knit_surfaces(form_solid=True) if len(g.surface_bodies(doc)) > 1 else None
    return out


@tool(WRITES)
def trim_surface(
    target: Annotated[str, Field(description="Surface body to trim ('body:Name').")],
    tool_body: Annotated[str, Field(description="Surface body that cuts it ('body:Name').")],
    keep: Annotated[list[float], Field(min_length=3, max_length=3, description="A point [x, y, z] mm on the piece of the target to keep.")],
) -> dict:
    """Trim a surface body with another surface and keep one piece: cut skins to a boundary, open windows and
    access panels in fairings, shape surfaces before knitting them into a solid."""
    doc = SW.active("part")
    fm = doc.FeatureManager
    p = [mm(c) for c in keep]
    tname = target.removeprefix("body:")
    tool_ref = tool_body if tool_body.startswith("body:") else f"body:{tool_body}"

    def on_body(body: Any) -> bool:
        return any(math.dist(p, list(f.GetClosestPointOn(*p))[:3]) < 1e-5 for f in SW.cast_all(body.GetFaces(), "IFace2"))

    # SolidWorks ignores which piece is picked: the keep mode keeps the first temporary piece and the remove
    # mode the other, so choose the mode from where the point lies before committing the trim
    tgt = g.resolve(doc, f"body:{tname}").obj
    tl = g.resolve(doc, tool_ref).obj

    def pre(remove: bool) -> list[Any]:
        doc.ClearSelection2(True)
        sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
        for b_, m in ((tl, 1), (tgt, 2)):
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = m
            SW.cast(b_, "IBody2").Select2(True, data)
        if not fm.PreTrimSurface(False, False, False, remove):
            doc.ClearSelection2(True)
            raise SwError("SolidWorks could not trim: the tool surface must cross the target.")
        return SW.cast_all(fm.GetPreTrimmedBodies(tgt) or [], "IBody2")

    def holds(body: Any) -> bool:
        box = list(body.GetBodyBox())
        return all(box[i] - 1e-6 <= p[i] <= box[i + 3] + 1e-6 for i in range(3))

    pieces = pre(False)
    if len(pieces) != 2:
        doc.ClearSelection2(True)
        raise SwError("The tool surface must cut the target into two pieces." if len(pieces) < 2 else
                      "The tool cuts the target into more than two pieces; trim in steps with one boundary each.")
    if not holds(pieces[0]):
        if not holds(pieces[1]):
            doc.ClearSelection2(True)
            raise SwError(f"{keep} is not on either piece of the target.")
        pre(True)
    else:
        # in the keep mode the post-trim needs a piece picked, although which one does not decide the result
        data = SW.cast(SW.cast(doc.SelectionManager, "ISelectionMgr").CreateSelectData(), "ISelectData")
        data.Mark = 2
        pieces[0].Select2(True, data)
    feat = fm.PostTrimSurface(False)
    doc.ClearSelection2(True)
    if feat is None:
        raise SwError("SolidWorks could not trim the surface.")
    feat = SW.cast(feat, "IFeature")
    kept = [x for x in g.surface_bodies(doc) if x.Name == feat.Name]
    if not kept or not on_body(kept[0]):
        feat.Select2(False, 0)
        doc.Extension.DeleteSelection2(SW.C.swDelete_Absorbed)
        raise SwError(f"SolidWorks kept the other piece; nothing was changed. Check that {keep} lies on the target.")
    out = result(doc, feat, "surface trim")
    out["area_mm2"] = round(sum(f.GetArea() for f in SW.cast_all(kept[0].GetFaces(), "IFace2")) * 1e6, 3)
    return out


def _split_by_surface(doc: Any, up: str, down: str, surface_ref: str, frame: Any) -> tuple[str, str]:
    """Join the two blocks, copy the result, and cut each copy with the parting surface so one keeps each side.
    Returns (cavity body, core body): the cavity is the piece on the parting plane's normal side."""
    C = SW.C
    srf = g.resolve(doc, surface_ref if surface_ref.startswith("body:") else f"body:{surface_ref}").obj
    if SW.cast(srf, "IBody2").GetType() != C.swSheetBody:
        raise SwError(f"{surface_ref} is not a surface body.")
    srf_name = SW.cast(srf, "IBody2").Name
    whole = combine(operation="add", main=f"body:{up}", tools=[f"body:{down}"])["feature"]
    before = {x.Name for x in g.bodies(doc)}
    move_body(bodies=[f"body:{whole}"], copy=True)
    other = [x.Name for x in g.bodies(doc) if x.Name not in before]
    if len(other) != 1:
        raise SwError("Could not copy the mould block.")
    pieces = []
    for name, flip in ((whole, False), (other[0], True)):
        doc.ClearSelection2(True)
        sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
        data = SW.cast(sm.CreateSelectData(), "ISelectData")
        data.Mark = 0
        SW.cast(g.resolve(doc, f"body:{srf_name}").obj, "IBody2").Select2(False, data)  # the surface body itself, not its faces
        before = {x.Name for x in g.bodies(doc)}
        res = doc.FeatureManager.InsertCutSurface(flip, 0, True, False, _dispatch_array([g.resolve(doc, f"body:{name}").obj]))
        doc.ClearSelection2(True)
        if (res[0] if isinstance(res, tuple) else res) is None:
            raise SwError("The parting surface does not cut the mould block in two; it must run right across the block (it may need extending).")
        new = [x for x in g.bodies(doc) if x.Name not in before and x.Name != name] or [x for x in g.bodies(doc) if x.Name not in before]
        if len(new) != 1:
            raise SwError("The parting surface cut a block into more than two pieces; it must run right across it once.")
        pieces.append(new[0])

    def height(b: Any) -> float:  # centroid along the parting plane's normal
        c = list(SW.cast(b, "IBody2").GetMassProperties(1.0))[:3]
        return frame.to_sketch(c)[2]

    cavity, core = sorted(pieces, key=height, reverse=True)
    return cavity.Name, core.Name


@tool(WRITES)
def mould_split(
    parting_plane: Annotated[str, Field(description="Plane (or planar face) of a flat parting line, e.g. 'plane:Top'.")],
    margin: Annotated[float, Field(gt=0, description="Mould block wall around the part, mm.")] = 20.0,
    above: Annotated[float, Field(gt=0, description="Block height on the plane's normal side (cavity), mm.")] = 30.0,
    below: Annotated[float, Field(gt=0, description="Block depth on the other side (core), mm.")] = 30.0,
    shrinkage: Annotated[float, Field(ge=0, lt=10, description="Scale the part up by this percentage first (moulding or casting shrinkage).")] = 0.0,
    part: Annotated[str | None, Field(description="The part body ('body:Name'); default the only solid body.")] = None,
    parting_surface: Annotated[str | None, Field(description=(
        "For a stepped or curved parting line: a surface body ('body:Name', e.g. from surface() or knit_surfaces) that runs "
        "through the part along its parting line and right across the mould block; the halves split along it instead "
        "of the plane, which then only sets the block's orientation and extent."))] = None,
) -> dict:
    """Mould or layup tool halves: a cavity block on one side of the parting line and a core block on the other,
    each with the part's shape cut out (composite layup tools, cast and moulded parts). The parting line is flat
    (the parting plane) or follows a parting surface. The part is left as it is (scaled by the shrinkage, if any)."""
    from .tools_sketch import SketchFrame, finish_sketch, sketch, start_sketch

    doc = SW.active("part")
    C = SW.C
    solids = g.bodies(doc)
    body = g.resolve(doc, part if part and part.startswith("body:") else f"body:{part}").obj if part else (solids[0] if len(solids) == 1 else None)
    if body is None:
        raise SwError("The part has several bodies; name the one to mould with part='body:Name'.")
    if parting_surface:  # check before anything is built
        srf = g.resolve(doc, parting_surface if parting_surface.startswith("body:") else f"body:{parting_surface}").obj
        if SW.cast(srf, "IBody2").GetType() != C.swSheetBody:
            raise SwError(f"{parting_surface} is not a surface body.")
    if shrinkage:
        k = 1 + shrinkage / 100
        if doc.FeatureManager.InsertScale(C.swScaleAboutCentroid, True, k, k, k) is None:
            raise SwError("SolidWorks could not scale the part for shrinkage.")
        body = SW.cast(SW.part(doc).GetBodies2(0, True)[0], "IBody2") if len(solids) == 1 else g.resolve(doc, part).obj
    name = body.Name
    # the part's outline on the parting plane, from its box corners in the plane's sketch coordinates
    start_sketch(on=parting_plane)
    frame = SketchFrame(SW.cast(doc.SketchManager.ActiveSketch, "ISketch"))
    b = list(body.GetBodyBox())
    pts = [frame.to_sketch([x, y, z]) for x in (b[0], b[3]) for y in (b[1], b[4]) for z in (b[2], b[5])]
    lo = [min(p[i] for p in pts) * 1000 - margin for i in range(2)]
    hi = [max(p[i] for p in pts) * 1000 + margin for i in range(2)]
    sketch(entities=[{"type": "rectangle", "corner1": lo, "corner2": hi}])
    finish_sketch()
    before = {x.Name for x in g.bodies(doc)}
    extrude(depth=above, merge=False, sketch=None)
    up = [x for x in g.bodies(doc) if x.Name not in before]
    start_sketch(on=parting_plane)
    sketch(entities=[{"type": "rectangle", "corner1": lo, "corner2": hi}])
    finish_sketch()
    before = {x.Name for x in g.bodies(doc)}
    extrude(depth=below, merge=False, reverse=True)
    down = [x for x in g.bodies(doc) if x.Name not in before]
    if len(up) != 1 or len(down) != 1:
        raise SwError("Could not build the two mould blocks.")
    up_name, down_name = up[0].Name, down[0].Name
    block = sum(x.GetMassProperties(1.0)[3] for x in (up[0], down[0]))
    if parting_surface:
        up_name, down_name = _split_by_surface(doc, up_name, down_name, parting_surface, frame)
    part_vol = g.resolve(doc, f"body:{name}").obj.GetMassProperties(1.0)[3]
    before = {x.Name for x in g.bodies(doc)}
    move_body(bodies=[f"body:{name}"], copy=True, count=2)
    copies = [x.Name for x in g.bodies(doc) if x.Name not in before]
    if len(copies) != 2:
        raise SwError("Could not copy the part to cut it from the blocks.")
    halves = {}
    for side, blk, cp in (("cavity", up_name, copies[0]), ("core", down_name, copies[1])):
        halves[side] = combine(operation="subtract", main=f"body:{blk}", tools=[f"body:{cp}"])["feature"]  # the result takes the feature's name
    vols = {k: g.resolve(doc, f"body:{v}").obj.GetMassProperties(1.0)[3] for k, v in halves.items()}
    if abs(sum(vols.values()) + part_vol - block) > 1e-9 * max(block, 1e-9) + 1e-12:
        raise SwError("The mould halves and the part do not add up to the block; check the part lies within the block.")
    return {"part": name, "cavity": {"body": halves["cavity"], "volume_mm3": round(vols["cavity"] * 1e9, 3)},
            "core": {"body": halves["core"], "volume_mm3": round(vols["core"] * 1e9, 3)},
            "block_mm3": round(block * 1e9, 3), "shrinkage_percent": shrinkage}

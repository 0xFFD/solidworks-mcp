"""Weldments: structural members from standard profiles along sketch lines.

SolidWorks profiles are library feature parts (.sldlfp) holding one sketch. The standard libraries are an
optional download, so profiles are generated here from their dimensions and kept in the workspace.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW, mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import TEMPLATE_PREFS
from .tools_features import _dispatch_array, result
from .tools_sketch import _add, last_sketch

SHAPES = {
    "rect_tube": ("width", "height", "wall"),
    "square_tube": ("width", "wall"),
    "round_tube": ("diameter", "wall"),
    "round_bar": ("diameter",),
    "flat_bar": ("width", "thickness"),
    "angle": ("leg1", "leg2", "thickness"),
    "channel": ("height", "flange", "thickness"),
}
_GENERATOR = "2"  # bump when generated profiles change, so cached ones are rebuilt
_CORNERS = {"miter": "swEndConditionMiter", "butt": "swEndConditionButt1", "butt_other": "swEndConditionButt2"}


def _profile_entities(shape: str, d: list[float]) -> list[dict]:
    """Closed profile sketch entities (mm) centred on the origin, which becomes the pierce point."""
    if shape in ("rect_tube", "square_tube"):
        w, h, t = (d[0], d[1], d[2]) if shape == "rect_tube" else (d[0], d[0], d[1])
        if 2 * t >= min(w, h):
            raise SwError("The tube wall is too thick for its size.")
        return [{"type": "center_rectangle", "center": [0, 0], "size": [w, h]}, {"type": "center_rectangle", "center": [0, 0], "size": [w - 2 * t, h - 2 * t]}]
    if shape == "round_tube":
        if 2 * d[1] >= d[0]:
            raise SwError("The tube wall is too thick for its diameter.")
        return [{"type": "circle", "center": [0, 0], "diameter": d[0]}, {"type": "circle", "center": [0, 0], "diameter": d[0] - 2 * d[1]}]
    if shape == "round_bar":
        return [{"type": "circle", "center": [0, 0], "diameter": d[0]}]
    if shape == "flat_bar":
        return [{"type": "center_rectangle", "center": [0, 0], "size": [d[0], d[1]]}]
    if shape == "angle":
        a, b, t = d
        if t >= min(a, b):
            raise SwError("The angle's thickness must be less than both legs.")
        pts = [[0, 0], [a, 0], [a, t], [t, t], [t, b], [0, b]]
    else:  # channel, opening towards +x
        h, b, t = d
        if 2 * t >= h or t >= b:
            raise SwError("The channel's thickness must be less than its flange and half its height.")
        pts = [[0, 0], [b, 0], [b, t], [t, t], [t, h - t], [b, h - t], [b, h], [0, h]]
        pts = [[x, y - h / 2] for x, y in pts]
    return [{"type": "polyline", "closed": True, "points": pts}]


def _profile_file(shape: str, size: list[float]) -> Path:
    """Library feature part for a profile, generated once and kept in <workspace>/profiles."""
    names = SHAPES.get(shape)
    if names is None:
        raise SwError(f"Unknown profile shape '{shape}'. Use one of {', '.join(SHAPES)}.")
    if len(size) != len(names) or min(size) <= 0:
        raise SwError(f"A {shape} profile needs size = [{', '.join(names)}] in mm, all above 0.")
    folder = app().settings.workspace / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = folder / ".generator"
    if not stamp.exists() or stamp.read_text().strip() != _GENERATOR:
        # profiles from an older generator may be wrong (rectangular tubes before 2: inner wall distorted)
        for old in folder.glob("*.sldlfp"):
            if old.stem.rsplit("_", 1)[0] in SHAPES:  # only files this generator made
                old.unlink(missing_ok=True)
        stamp.write_text(_GENERATOR)
    path = folder / f"{shape}_{'x'.join(f'{v:g}' for v in size)}.sldlfp"
    if path.exists():
        return path
    entities = _profile_entities(shape, [float(v) for v in size])
    sw, C = SW.app, SW.C
    active = sw.ActiveDoc
    tpl = sw.GetUserPreferenceStringValue(getattr(C, TEMPLATE_PREFS["part"]))
    pdoc = sw.NewDocument(tpl, 0, 0, 0)
    if pdoc is None:
        raise SwError("SolidWorks could not create the profile part.")
    pdoc = SW.cast(pdoc, "IModelDoc2")
    try:
        pdoc.ClearSelection2(True)
        g.select(pdoc, "plane:Front")
        pdoc.SketchManager.InsertSketch(True)
        sm = pdoc.SketchManager
        sm.AddToDB = True
        try:
            for e in entities:
                _add(pdoc, sm, e)
        finally:
            sm.AddToDB = False
        name = last_sketch(pdoc)
        sm.InsertSketch(True)
        pdoc.ClearSelection2(True)
        g.select(pdoc, f"sketch:{name}")
        ok, err, _ = pdoc.Extension.SaveAs3(str(path), 0, C.swSaveAsOptions_Silent | C.swSaveAsOptions_Copy, None, None, 0, 0)
        if not ok or not path.exists():
            raise SwError(f"SolidWorks could not save the profile {path.name} (code {err}).")
    finally:
        sw.CloseDoc(pdoc.GetTitle())
        if active is not None:
            sw.ActivateDoc3(SW.cast(active, "IModelDoc2").GetTitle(), False, 0, 0)
    return path


def _segment_ends(seg: Any) -> tuple[tuple, tuple]:
    """End points of a sketch segment, rounded to 1 µm so shared points compare equal."""
    t = seg.GetType()
    if t == SW.C.swSketchLINE:
        x = SW.cast(seg, "ISketchLine")
        a, b = x.GetStartPoint2(), x.GetEndPoint2()
    elif t == SW.C.swSketchARC:
        x = SW.cast(seg, "ISketchArc")
        a, b = x.GetStartPoint2(), x.GetEndPoint2()
    else:
        pts = SW.cast(seg, "ISketchSpline").GetPoints2() if t == SW.C.swSketchSPLINE else None
        if not pts:
            return (id(seg), 0), (id(seg), 1)
        a, b = pts[0], pts[-1]

    def key(p: Any) -> tuple:
        p = SW.cast(p, "ISketchPoint")
        return round(p.X, 6), round(p.Y, 6), round(p.Z, 6)

    return key(a), key(b)


@tool(WRITES)
def structural_member(
    shape: Annotated[Literal["rect_tube", "square_tube", "round_tube", "round_bar", "flat_bar", "angle", "channel"] | None, Field(description="Standard profile shape, generated from size.")] = None,
    size: Annotated[list[float] | None, Field(description=(
        "Profile dimensions in mm: rect_tube [width, height, wall], square_tube [width, wall], round_tube [diameter, wall], "
        "round_bar [diameter], flat_bar [width, thickness], angle [leg1, leg2, thickness], channel [height, flange, thickness]."))] = None,
    profile: Annotated[str | None, Field(description="Or an existing weldment profile file (.sldlfp).")] = None,
    sketch: Annotated[str | None, Field(description="2D or 3D sketch whose lines are the member centrelines; default is the latest sketch.")] = None,
    corner: Annotated[Literal["miter", "butt", "butt_other"], Field(description="Corner treatment where members meet.")] = "miter",
) -> dict:
    """Weldment structural members (tubes, bars, angles, channels) along every segment of a sketch: ground
    support equipment, test rigs, equipment racks and frames. The part becomes a weldment with one body per
    member; set_material and mass_properties give the frame mass."""
    doc = SW.active("part")
    C = SW.C
    if profile:
        path = app().path(profile, must_exist=True, suffixes=(".sldlfp",))
    elif shape and size:
        path = _profile_file(shape, size)
    else:
        raise SwError("Give shape and size, or a profile file.")
    name = sketch or last_sketch(doc)
    f = g.feature_by_name(doc, name)
    if f is None or f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
        raise SwError(f"'{name}' is not a sketch.")
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    segs = [s for s in SW.cast_all(SW.cast(f.GetSpecificFeature2(), "ISketch").GetSketchSegments(), "ISketchSegment") if not s.ConstructionGeometry]
    if not segs:
        raise SwError(f"{name} has no segments to put members on.")
    fm = doc.FeatureManager
    if not any(x.GetTypeName2() == "WeldMemberFeat" or x.GetTypeName2() == "Weldment" for x in g.iter_features(doc)):
        fm.InsertWeldmentFeature()
    # a member group must be a simple chain, so branches and T joints become groups of their own
    groups = []
    for chain in shapes.chains([_segment_ends(s) for s in segs]):
        grp = SW.cast(fm.CreateStructuralMemberGroup(), "IStructuralMemberGroup")
        grp.Segments = _dispatch_array([segs[i] for i in chain])
        grp.ApplyCornerTreatment = True
        grp.CornerTreatmentType = getattr(C, _CORNERS[corner])
        groups.append(grp._oleobj_)
    import pythoncom
    from win32com.client import VARIANT

    before = len(g.bodies(doc))
    feat = fm.InsertStructuralWeldment5(str(path), 1, True, VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, groups), "")
    out = result(doc, feat, "structural member", "The sketch lines must not overlap and the profile must be a closed sketch.")
    out["members"] = len(g.bodies(doc)) - before
    out["profile"] = str(path)
    out["cut_list"] = _cut_list(doc)
    return out


def _number(text: str) -> float | None:
    import re

    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def _cut_list(doc: Any) -> list[dict]:
    """Weldment cut list items: quantity, length and end-cut angles, as SolidWorks computes them."""
    doc.ForceRebuild3(False)
    for f in g.iter_features(doc):
        if f.GetTypeName2() == "SolidBodyFolder":
            SW.cast(f.GetSpecificFeature2(), "IBodyFolder").UpdateCutList()
    items = []
    for f in g.iter_features(doc):
        sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
        while sub is not None:
            if sub.GetTypeName2() == "CutListFolder":
                cpm = SW.cast(sub.CustomPropertyManager, "ICustomPropertyManager")
                props = {n.upper(): cpm.Get6(n, False)[2] for n in (cpm.GetNames() or [])}
                body_count = SW.cast(sub.GetSpecificFeature2(), "IBodyFolder").GetBodyCount()
                row: dict[str, Any] = {"item": sub.Name, "quantity": int(_number(props.get("QUANTITY", "")) or body_count)}
                for key, name in (("LENGTH", "length_mm"), ("ANGLE1", "angle1_deg"), ("ANGLE2", "angle2_deg")):
                    if (v := _number(props.get(key, ""))) is not None:
                        row[name] = v
                if props.get("DESCRIPTION"):
                    row["description"] = props["DESCRIPTION"]
                items.append(row)
            sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
    return items


@tool(READ_ONLY)
def cut_list() -> dict:
    """Weldment cut list of the active part: one row per group of identical members with quantity, cut
    length and end-cut angles, for the fabrication BOM and saw schedule."""
    doc = SW.active("part")
    items = _cut_list(doc)
    if not items:
        raise SwError("The part has no weldment cut list; add members with structural_member.")
    return {"items": items, "members": sum(i["quantity"] for i in items),
            "total_length_mm": round(sum(i.get("length_mm", 0) * i["quantity"] for i in items), 3)}


def _open_ends(doc: Any) -> list[Any]:
    """Tube end faces (planar, with an inner loop) that no other body closes off: not mitred or butted
    against another member and not already capped."""
    bodies = g.bodies(doc)
    boxes = [list(b.GetBodyBox()) for b in bodies]
    tol = 1e-6
    out = []
    for i, b in enumerate(bodies):
        for f in SW.cast_all(b.GetFaces(), "IFace2"):
            if not SW.cast(f.GetSurface(), "ISurface").IsPlane() or f.GetLoopCount() < 2:
                continue
            box = f.GetBox
            box = box() if callable(box) else box
            n = list(f.Normal)
            p = [(box[k] + box[k + 3]) / 2 + n[k] * 1e-3 for k in range(3)]  # just past the end, in the opening
            if not any(j != i and all(bx[k] - tol <= p[k] <= bx[k + 3] + tol for k in range(3)) for j, bx in enumerate(boxes)):
                out.append(f)
    return out


def _new_bodies(doc: Any, before: set[str]) -> list[dict]:
    return [{"name": b.Name, "volume_mm3": round(b.GetMassProperties(1.0)[3] * 1e9, 3)} for b in g.bodies(doc) if b.Name not in before]


@tool(WRITES)
def end_cap(
    faces: Annotated[list[str] | None, Field(description="Tube end faces to cap ('face@x,y,z' on the tube wall). Omit to cap every open tube end of the weldment.")] = None,
    thickness: Annotated[float, Field(gt=0, description="Cap plate thickness, mm.")] = 3.0,
    offset: Annotated[float, Field(ge=0, description="Inset of the cap edge from the tube outline, mm (room for the fillet weld).")] = 0.0,
    inward: Annotated[bool, Field(description="Set the cap into the tube end (the member is shortened by the thickness) instead of on it.")] = False,
    corner_chamfer: Annotated[float, Field(ge=0, description="Chamfer on the cap corners, mm (rectangular tubes).")] = 0.0,
) -> dict:
    """Weldment end caps closing tube ends: keeps water and debris out of ground support equipment and
    frames and gives a clean finish. Each cap is its own body and joins the cut list."""
    doc = SW.active("part")
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    if faces:
        targets = [r.obj for r in g.select(doc, faces, mark=1)]
    else:
        targets = _open_ends(doc)
        if not targets:
            raise SwError("No open tube ends found; give the end faces with faces=[...].")
        sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
        for f in targets:
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = 1
            if not SW.cast(f, "IEntity").Select4(True, data):
                raise SwError("Could not select a tube end face.")
    before = {b.Name for b in g.bodies(doc)}

    feat = doc.FeatureManager.InsertEndCapFeature3(mm(thickness), True, corner_chamfer > 0, mm(offset), 0.5, mm(corner_chamfer),
                                                   corner_chamfer > 0, 0.0, False, inward)
    out = result(doc, feat, "end cap", "Pick the planar end face of a tube or closed profile, on the wall material.")
    caps = _new_bodies(doc, before)
    if len(caps) != len(targets):
        out["warning"] = f"{len(targets)} end faces were selected but {len(caps)} caps were made."
    out["caps"] = caps
    return out


_GUSSET_AT = {"start": 0, "middle": 1, "end": 2}
_GUSSET_SIDE = {"one_side": 0, "centred": 1, "other_side": 2}


@tool(WRITES)
def gusset(
    faces: Annotated[list[str], Field(min_length=2, max_length=2, description="The two planar faces of the corner, one on each member ('face@x,y,z').")],
    size: Annotated[list[float], Field(min_length=2, max_length=2, description="Leg lengths [d1, d2] along the first and second face, mm.")],
    thickness: Annotated[float, Field(gt=0, description="Plate thickness, mm.")] = 5.0,
    shape: Annotated[Literal["triangle", "polygon"], Field(description="Triangle, or polygon with straight ends of end_length along each face before the diagonal.")] = "triangle",
    end_length: Annotated[float, Field(ge=0, description="Polygon gusset: length of the straight ends, mm.")] = 0.0,
    at: Annotated[Literal["start", "middle", "end"], Field(description="Where along the corner the plate sits.")] = "middle",
    side: Annotated[Literal["one_side", "centred", "other_side"], Field(description="Thickness direction from the location.")] = "centred",
) -> dict:
    """Weldment gusset plate stiffening the corner between two members (frames, stands and racks)."""
    doc = SW.active("part")
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    if shape == "polygon" and end_length <= 0:
        raise SwError("A polygon gusset needs end_length above 0.")
    if end_length >= min(size):
        raise SwError("end_length must be shorter than both legs.")

    doc.ClearSelection2(True)
    picked = [r.obj for r in g.select(doc, faces)]
    before = {b.Name for b in g.bodies(doc)}
    # the polygon's diagonal is set by its angle, which SolidWorks does not derive from the legs
    angle = math.atan2(size[0] - end_length, size[1] - end_length) if shape == "polygon" else 0.0
    feat = doc.FeatureManager.InsertGussetFeature2(
        mm(thickness), _GUSSET_SIDE[side], _GUSSET_AT[at], shape == "polygon", mm(size[0]), mm(size[1]), mm(end_length), angle, 0.0,
        False, 0.0, 0, False, False, False, _dispatch_array(picked))
    out = result(doc, feat, "gusset", "The two faces must be planar faces of different members that meet at a corner, and the legs must fit on them.")
    out["plates"] = _new_bodies(doc, before)
    return out


_TRIM_CORNER = {"miter": 1, "butt": 2, "butt_other": 3}  # weldment trim/extend corner types (measured)


def _end_trim(doc: Any, members: list[str], to: list[str]) -> dict:
    """T-joint end trim. SolidWorks' end trim does nothing through the API, so each member's end face nearest the
    boundary is moved (direct editing) onto the boundary face that faces it; the cut list follows the body."""
    if len(to) != 1:
        raise SwError("An end trim needs one boundary member in to=[...].")
    boundary = g.resolve(doc, to[0] if to[0].startswith("body:") else f"body:{to[0]}").obj
    bbox = list(boundary.GetBodyBox())
    centre = [(bbox[i] + bbox[i + 3]) / 2 for i in range(3)]

    def planar(body: Any) -> list[tuple[Any, list[float], list[float], list[float]]]:
        out = []
        for f in SW.cast_all(body.GetFaces(), "IFace2"):
            if SW.cast(f.GetSurface(), "ISurface").IsPlane():
                box = f.GetBox
                box = list(box() if callable(box) else box)
                out.append((f, list(f.Normal), list(f.GetClosestPointOn(*[(box[i] + box[i + 3]) / 2 for i in range(3)]))[:3], box))
        return out

    walls = planar(boundary)
    moved = []
    for ref in members:
        name = ref.removeprefix("body:")
        body = g.resolve(doc, f"body:{name}").obj
        best = None
        for f, n, c, _ in sorted(planar(body), key=lambda e: math.dist(e[2], centre)):
            for _, bn, bp, bb in walls:  # the boundary face facing this end, across the member's footprint
                if sum(x * y for x, y in zip(n, bn)) > -0.9999:
                    continue
                if not all(bb[i] - 1e-6 <= c[i] <= bb[i + 3] + 1e-6 for i in range(3) if abs(n[i]) < 0.5):
                    continue
                d = sum((bp[i] - c[i]) * n[i] for i in range(3))
                # the boundary's outer face is the first one met coming from the member's far end (smallest d),
                # not the inner wall of a tube behind it
                if best is None or d < best[1]:
                    best = (f, d)
            if best is not None:
                break
        if best is None:
            raise SwError(f"No face of {to[0]} lies square across the end of {name}; end trims need the member to meet a flat face.")
        f, d = best
        if abs(d) > 1e-7:
            # move this face object itself: a point on a tube end resolves to a neighbouring wall too easily
            C = SW.C
            doc.ClearSelection2(True)
            g.Ref(f, "face", f"end of {name}").select(doc, mark=1)
            feat = doc.FeatureManager.InsertMoveFace3(C.swMoveFaceTypeOffset, d < 0, 0.0, abs(d), None, None, C.swEndCondBlind, 0.0)
            doc.ClearSelection2(True)
            if feat is None:
                raise SwError(f"SolidWorks could not move the end of {name}.")
            doc.EditRebuild3()
            moved.append({"member": name, "moved_mm": round(d * 1000, 4)})
    out: dict[str, Any] = {"members": moved, "cut_list": _cut_list(doc)}
    if not moved:
        out["note"] = "The members already end on the boundary."
    return out


@tool(WRITES)
def trim_members(
    members: Annotated[list[str], Field(min_length=1, description="Members to trim or extend ('body:Name' from list_bodies).")],
    to: Annotated[list[str], Field(min_length=1, description="Members they meet at the corner ('body:Name').")],
    corner: Annotated[Literal["miter", "butt", "butt_other", "end"], Field(description=(
        "miter: both cut at the bisecting angle; butt: `members` end on the face of `to`, which runs to their far side; "
        "butt_other: the reverse; end: a T-joint, `members` are trimmed or extended to the face of `to`, which is left as it is."))] = "butt",
) -> dict:
    """Trim or extend weldment members to meet at a corner or a T-joint (members drawn short or overshooting, or
    from separate sketches). Reports the cut list afterwards."""
    doc = SW.active("part")
    if corner == "end":
        return _end_trim(doc, members, to)
    picked = []
    for r in members + to:
        ref = g.resolve(doc, r if r.startswith("body:") else f"body:{r}")
        if ref.kind != "body":
            raise SwError(f"{r} is not a body.")
        picked.append(ref.obj)
    before = {b.Name: round(b.GetMassProperties(1.0)[3] * 1e9, 3) for b in g.bodies(doc)}
    doc.ClearSelection2(True)
    f = doc.FeatureManager.InsertWeldmentTrimFeature2(_TRIM_CORNER[corner], 0, 0.0, _dispatch_array(picked[:len(members)]),
                                                     _dispatch_array(picked[len(members):]))
    out = result(doc, f, "trim/extend", "The members must meet at a corner, as two ends that touch or would touch if extended.")
    after = {b.Name: round(b.GetMassProperties(1.0)[3] * 1e9, 3) for b in g.bodies(doc)}
    if sorted(after.values()) == sorted(before.values()):
        doc.ClearSelection2(True)
        SW.cast(f, "IFeature").Select2(False, 0)
        doc.Extension.DeleteSelection2(SW.C.swDelete_Absorbed)
        raise SwError("The trim/extend changed nothing; the members must meet at a corner (end to end, not a T-joint).")
    out["bodies"] = [{"name": n, "volume_mm3": v} for n, v in after.items()]
    out["cut_list"] = _cut_list(doc)
    return out


@tool(READ_ONLY)
def member_check(
    shape: Annotated[Literal["rect_tube", "square_tube", "round_tube", "round_bar", "flat_bar", "angle", "channel"], Field(description="Profile shape, as in structural_member.")],
    size: Annotated[list[float], Field(description="Profile dimensions in mm, as in structural_member.")],
    length: Annotated[float, Field(gt=0, description="Span or cantilever length in mm (cut_list gives member lengths).")],
    e_modulus: Annotated[float, Field(gt=0, description="Young's modulus in MPa (e.g. 71000 for 7075-T6).")],
    yield_strength: Annotated[float, Field(gt=0, description="Tensile yield allowable Fty in MPa, from your design allowables.")],
    ultimate_strength: Annotated[float | None, Field(gt=0, description="Tensile ultimate allowable Ftu in MPa, for the ultimate margin.")] = None,
    support: Annotated[Literal["cantilever", "simply_supported", "fixed_fixed"], Field(description="End conditions; also set the buckling length (K = 2, 1, 0.5).")] = "simply_supported",
    load_type: Annotated[Literal["point", "distributed"], Field(description="Point load at the tip (cantilever) or mid-span, or a uniform load.")] = "point",
    load: Annotated[float, Field(description="Transverse limit load in N (total for a distributed load).")] = 0.0,
    direction: Annotated[Literal["x", "y"], Field(description=(
        "Load direction in the profile's sketch axes: rect tube width and channel flanges along x; angle legs along +x and +y."))] = "y",
    axial: Annotated[float, Field(description="Axial limit load in N, positive in tension, negative in compression.")] = 0.0,
    yield_factor: Annotated[float, Field(gt=0, description="Factor on limit load for the yield margin.")] = 1.0,
    ultimate_factor: Annotated[float, Field(gt=0, description="Factor of safety on limit load for the ultimate and buckling margins.")] = 1.5,
    max_deflection: Annotated[float | None, Field(gt=0, description="Allowed deflection in mm, to check against.")] = None,
) -> dict:
    """Hand-calculation strength check of a structural member (no Simulation licence needed): section
    properties of the profile, bending, axial and average shear stresses, deflection, column buckling (Euler,
    or Johnson for short columns) and margins of safety MS = allowable / (factor x stress) - 1. Bending of
    angles is unsymmetric (about both principal axes). Stresses are elastic and sharp-cornered; axial and
    bending are superposed without beam-column amplification, so keep compression well below the buckling load."""
    names = SHAPES[shape]
    if len(size) != len(names) or min(size) <= 0:
        raise SwError(f"A {shape} profile needs size = [{', '.join(names)}] in mm, all above 0.")
    sec = shapes.section_properties(shape, [float(v) for v in size])
    r = shapes.member_check(sec, length, support, load_type, load, direction, axial, e_modulus, yield_strength,
                            ultimate_strength, yield_factor, ultimate_factor)

    def rnd(v: float, n: int = 4) -> float:
        return float(f"{v:.{n}g}") if math.isfinite(v) else v

    out: dict[str, Any] = {
        "section": {"area_mm2": rnd(sec["area"]), "centroid_mm": [round(c, 3) for c in sec["centroid"]],
                    "ixx_mm4": rnd(sec["ixx"]), "iyy_mm4": rnd(sec["iyy"]), "ixy_mm4": rnd(sec["ixy"]) if abs(sec["ixy"]) > 1e-6 * sec["imax"] else 0.0,
                    "imax_mm4": rnd(sec["imax"]), "imin_mm4": rnd(sec["imin"]),
                    "principal_angle_deg": round(sec["principal_angle"], 2), "r_min_mm": rnd(sec["r_min"])},
        "bending_moment_Nmm": rnd(r["bending_moment"]), "bending_stress_MPa": rnd(r["bending_stress"]),
        "axial_stress_MPa": rnd(r["axial_stress"]), "max_stress_MPa": rnd(r["max_stress"]),
        "average_shear_stress_MPa": rnd(r["average_shear_stress"]), "deflection_mm": rnd(r["deflection"]),
        "ms_yield": round(r["ms_yield"], 3) if math.isfinite(r["ms_yield"]) else None,
    }
    if "ms_ultimate" in r:
        out["ms_ultimate"] = round(r["ms_ultimate"], 3) if math.isfinite(r["ms_ultimate"]) else None
    if "ms_buckling" in r:
        out["buckling"] = {"mode": r["buckling_mode"], "slenderness": rnd(r["slenderness"]),
                           "critical_load_N": rnd(r["critical_load"]), "ms": round(r["ms_buckling"], 3)}
    if max_deflection is not None:
        out["deflection_ok"] = r["deflection"] <= max_deflection
    margins = [m for m in (out["ms_yield"], out.get("ms_ultimate"), out.get("buckling", {}).get("ms")) if m is not None]
    out["ok"] = all(m >= 0 for m in margins) and out.get("deflection_ok", True)
    if load == 0 and axial == 0:
        out["warning"] = "No load given; only the section properties are meaningful."
    return out

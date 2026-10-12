"""Weldments: structural members from standard profiles along sketch lines.

SolidWorks profiles are library feature parts (.sldlfp) holding one sketch. The standard libraries are an
optional download, so profiles are generated here from their dimensions and kept in the workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW
from .errors import SwError
from .registry import WRITES, app, tool
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
        pts = [[0, 0], [a, 0], [a, t], [t, t], [t, b], [0, b]]
    else:  # channel, opening towards +x
        h, b, t = d
        pts = [[0, 0], [b, 0], [b, t], [t, t], [t, h - t], [b, h - t], [b, h], [0, h]]
        pts = [[x, y - h / 2] for x, y in pts]
    if t >= min(d[:2]):
        raise SwError("The section is thinner than its thickness.")
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
    return out

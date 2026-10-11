"""Model topology queries and entity references.

Faces and edges are addressed by references the model can build from `list_faces` / `list_edges`
output or from coordinates it already knows:

    face:3              face index 3 from list_faces (indices change after every feature)
    edge:12             edge index 12 from list_edges
    face@50,0,10        the face closest to the point (50, 0, 10) mm
    edge@25,0,0         the edge closest to the point
    face:Bracket-1/3    face 3 of assembly component Bracket-1 (same for edge:, face@ and edge@)
    plane:Top           a reference plane by name; Front, Top and Right map to the default planes
    axis:Axis1, feature:Boss-Extrude1, sketch:Sketch2, point:Point1, comp:Bracket-1, body:Boss-Extrude1
    Front Plane         a bare name is looked up as a feature (planes, sketches, axes, features)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .com import SW, doc_kind, mm, to_mm
from .errors import SwError

_POINT = r"\s*(-?[\d.]+(?:e-?\d+)?)\s*"
_AT_RE = re.compile(rf"^(face|edge)@(?:(?P<comp>[^/]+)/)?{_POINT},{_POINT},{_POINT}$", re.I)
_IDX_RE = re.compile(r"^(face|edge):(?:(?P<comp>.+)/)?(?P<idx>\d+)$", re.I)
_DEFAULT_PLANES = {"front": 0, "top": 1, "right": 2}


def _r(v: float, nd: int = 4) -> float:
    v = round(float(v), nd)
    return 0.0 if v == 0 else v


def _pt(xyz: Any) -> list[float]:
    return [_r(to_mm(c)) for c in list(xyz)[:3]]


def _vec(xyz: Any) -> list[float]:
    return [_r(c, 5) for c in list(xyz)[:3]]


# ---------------------------------------------------------------------------------------------- bodies


def components(doc: Any) -> list[Any]:
    asm = SW.assembly(doc)
    return SW.cast_all(asm.GetComponents(False), "IComponent2")


def find_component(doc: Any, name: str) -> Any:
    if doc_kind(doc) != "assembly":
        raise SwError(f"Component references like '{name}/...' only work in an assembly.")
    for c in components(doc):
        if c.Name2 == name or c.Name2.split("/")[-1] == name:
            return c
    names = [c.Name2 for c in components(doc)][:30]
    raise SwError(f"No component named '{name}'. Components: {names}")


def bodies(doc: Any, component: str | None = None) -> list[Any]:
    """Solid bodies of the active part, or of one component when `component` is given."""
    if component:
        comp = find_component(doc, component)
        raw = comp.GetBodies2(0)
    elif doc_kind(doc) == "part":
        raw = SW.part(doc).GetBodies2(0, True)
    else:
        raise SwError("Face and edge queries need a part, or an assembly component (component='Name-1').")
    return SW.cast_all(raw, "IBody2")


class Placement:
    """Component placement in its assembly (IComponent2.Transform2): local -> assembly coordinates.

    Bodies returned by IComponent2.GetBodies2 are in the component's own coordinates, so points and
    directions are mapped through this transform for output and back for point look-ups.
    """

    def __init__(self, comp: Any):
        arr = list(SW.cast(comp.Transform2, "IMathTransform").ArrayData)
        self.R = [arr[0:3], arr[3:6], arr[6:9]]
        self.T = arr[9:12]
        self.s = arr[12] or 1.0

    def point(self, p: Any) -> list[float]:
        p = list(p)[:3]
        return [self.s * sum(p[j] * self.R[j][i] for j in range(3)) + self.T[i] for i in range(3)]

    def direction(self, d: Any) -> list[float]:
        d = list(d)[:3]
        return [sum(d[j] * self.R[j][i] for j in range(3)) for i in range(3)]

    def local(self, p: Any) -> list[float]:
        q = [(list(p)[i] - self.T[i]) / self.s for i in range(3)]
        return [sum(q[j] * self.R[i][j] for j in range(3)) for i in range(3)]


def placement(doc: Any, component: str | None) -> Placement | None:
    return Placement(find_component(doc, component)) if component else None


def faces(doc: Any, component: str | None = None) -> list[Any]:
    out: list[Any] = []
    for b in bodies(doc, component):
        out += SW.cast_all(b.GetFaces(), "IFace2")
    return out


def edges(doc: Any, component: str | None = None) -> list[Any]:
    out: list[Any] = []
    for b in bodies(doc, component):
        out += SW.cast_all(b.GetEdges(), "IEdge")
    return out


# ---------------------------------------------------------------------------------------------- descriptors


def face_info(face: Any, index: int, xf: Placement | None = None) -> dict:
    """Describe a face; with `xf` (an assembly component) coordinates are in assembly space."""
    P = (lambda v: _pt(xf.point(v))) if xf else _pt  # noqa: E731
    V = (lambda v: _vec(xf.direction(v))) if xf else _vec  # noqa: E731
    surf = SW.cast(face.GetSurface(), "ISurface")
    info: dict[str, Any] = {"i": index}
    # FaceInSurfaceSense() is True when the face normal is opposite to the surface normal.
    flip = -1.0 if face.FaceInSurfaceSense() else 1.0
    if surf.IsPlane():
        info["type"] = "plane"
        info["normal"] = V(face.Normal)  # outward normal of a planar face
    elif surf.IsCylinder():
        p = surf.CylinderParams
        info["type"] = "cylinder"
        info["radius"] = _r(to_mm(p[6]))
        info["axis"] = V(p[3:6])
        info["axis_point"] = P(p[0:3])
        info["hole"] = flip < 0
    elif surf.IsCone():
        p = surf.ConeParams
        info["type"] = "cone"
        info["axis"] = V(p[3:6])
        info["half_angle_deg"] = _r(math.degrees(p[7]), 3)
    elif surf.IsSphere():
        p = surf.SphereParams
        info["type"] = "sphere"
        info["center"] = P(p[0:3])
        info["radius"] = _r(to_mm(p[3]))
    elif surf.IsTorus():
        info["type"] = "torus"
    else:
        info["type"] = "freeform"
    info["area_mm2"] = _r(face.GetArea() * 1e6, 3)
    box = face.GetBox
    if callable(box):
        box = box()
    c = [(box[i] + box[i + 3]) / 2 for i in range(3)]
    on = face.GetClosestPointOn(*c)
    info["point"] = P(on)
    feat = face.GetFeature()
    if feat is not None:
        info["feature"] = SW.cast(feat, "IFeature").Name
    return info


def _edge_mid(edge: Any, curve: Any, u0: float, u1: float, start: list[float], end: list[float], line: bool) -> list[float]:
    """A point halfway along the edge. The underlying curve can be parameterised against the edge's own
    direction, so the curve's mid parameter may land off the edge; such a point is rejected."""
    if line:
        return [(a + b) / 2 for a, b in zip(start, end)]
    for evaluate in (lambda: curve.Evaluate2((u0 + u1) / 2, 0), lambda: edge.Evaluate2((u0 + u1) / 2, 0)):
        try:
            p = list(evaluate())[:3]
        except Exception:
            continue
        q = edge.GetClosestPointOn(*p)
        if q is not None and math.dist(p, list(q)[:3]) < 1e-8:
            return p
    q = edge.GetClosestPointOn(*[(a + b) / 2 for a, b in zip(start, end)])
    return list(q)[:3] if q is not None else start


def edge_info(edge: Any, index: int, xf: Placement | None = None) -> dict:
    """Describe an edge; with `xf` (an assembly component) coordinates are in assembly space."""
    P = (lambda v: _pt(xf.point(v))) if xf else _pt  # noqa: E731
    V = (lambda v: _vec(xf.direction(v))) if xf else _vec  # noqa: E731
    curve = SW.cast(edge.GetCurve(), "ICurve")
    prm = SW.cast(edge.GetCurveParams3(), "ICurveParamData")
    u0, u1 = prm.UMinValue, prm.UMaxValue
    start, end = list(prm.StartPoint)[:3], list(prm.EndPoint)[:3]
    info: dict[str, Any] = {"i": index}
    if curve.IsLine():
        info["type"] = "line"
    elif curve.IsCircle():
        cp = curve.CircleParams
        closed = math.dist(start, end) < 1e-9
        info["type"] = "circle" if closed else "arc"
        info["center"] = P(cp[0:3])
        info["axis"] = V(cp[3:6])
        info["radius"] = _r(to_mm(cp[6]))
    elif curve.IsEllipse():
        info["type"] = "ellipse"
    else:
        info["type"] = "spline"
    try:
        info["length"] = _r(to_mm(curve.GetLength3(u0, u1)))
    except Exception:
        pass
    info["mid"] = P(_edge_mid(edge, curve, u0, u1, start, end, info["type"] == "line"))
    if info["type"] in ("line", "arc", "spline", "ellipse"):
        info["start"] = P(start)
        info["end"] = P(end)
    return info


# ---------------------------------------------------------------------------------------------- references


@dataclass
class Ref:
    obj: Any  # IFace2, IEdge, IFeature, IComponent2 or IBody2
    kind: str  # face, edge, feature, component, body
    label: str

    def select(self, doc: Any, append: bool = True, mark: int = 0) -> None:
        ok = False
        if self.kind in ("face", "edge"):
            sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = mark
            ok = SW.cast(self.obj, "IEntity").Select4(append, data)
        elif self.kind == "feature":
            ok = self.obj.Select2(append, mark)
        elif self.kind == "body":
            sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = mark
            ok = self.obj.Select2(append, data)
        elif self.kind == "component":
            sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = mark
            ok = self.obj.Select4(append, data, False)
        if not ok:
            raise SwError(f"Could not select {self.label}.")


def feature_by_name(doc: Any, name: str) -> Any:
    kind = doc_kind(doc)
    if kind == "part":
        f = SW.part(doc).FeatureByName(name)
    elif kind == "assembly":
        f = SW.assembly(doc).FeatureByName(name)
    else:
        f = SW.cast(doc, "IDrawingDoc").FeatureByName(name)
    return SW.cast(f, "IFeature") if f is not None else None


def iter_features(doc: Any):
    f = SW.cast(doc.FirstFeature(), "IFeature")
    while f is not None:
        yield f
        f = SW.cast(f.GetNextFeature(), "IFeature")


def default_plane(doc: Any, which: str) -> Any:
    n = _DEFAULT_PLANES[which]
    planes = [f for f in iter_features(doc) if f.GetTypeName2() == "RefPlane"]
    if len(planes) <= n:
        raise SwError("The document does not have the three default planes.")
    return planes[n]


def _nearest(items: list[Any], p: tuple[float, float, float]) -> tuple[int, float]:
    best, best_d = -1, float("inf")
    for i, it in enumerate(items):
        q = it.GetClosestPointOn(*p)
        if q is None:
            continue
        d = math.dist(p, list(q)[:3])
        if d < best_d:
            best, best_d = i, d
    return best, best_d


def resolve(doc: Any, ref: str) -> Ref:
    ref = ref.strip()
    if m := _IDX_RE.match(ref):
        kind, comp, idx = m.group(1).lower(), m.group("comp"), int(m.group("idx"))
        items = faces(doc, comp) if kind == "face" else edges(doc, comp)
        if idx >= len(items):
            raise SwError(f"{ref}: there are only {len(items)} {kind}s. Run list_{kind}s again, indices change after every feature.")
        return Ref(items[idx], kind, ref)
    if m := _AT_RE.match(ref):
        kind, comp = m.group(1).lower(), m.group("comp")
        p = (mm(float(m.group(3))), mm(float(m.group(4))), mm(float(m.group(5))))
        if comp:  # the point is given in assembly coordinates; component bodies are in local ones
            p = tuple(placement(doc, comp).local(p))
        items = faces(doc, comp) if kind == "face" else edges(doc, comp)
        i, d = _nearest(items, p)
        if i < 0:
            raise SwError(f"{ref}: the model has no {kind}s.")
        if d > 0.0005:
            raise SwError(f"{ref}: the nearest {kind} is {to_mm(d):.3f} mm away from that point. Use list_{kind}s to get exact positions.")
        return Ref(items[i], kind, ref)
    prefix, _, name = ref.partition(":")
    prefix = prefix.lower()
    if prefix not in ("plane", "axis", "feature", "sketch", "point", "comp", "coord", "body") or not name:
        prefix, name = "feature", ref
    if prefix == "comp":
        return Ref(find_component(doc, name), "component", ref)
    if prefix == "body":
        found = [b for b in bodies(doc) if b.Name == name]
        if not found:
            raise SwError(f"No solid body named '{name}'. Bodies: {[b.Name for b in bodies(doc)]}")
        return Ref(found[0], "body", ref)
    if prefix == "plane" and name.lower() in _DEFAULT_PLANES:
        return Ref(default_plane(doc, name.lower()), "feature", ref)
    if name.lower().removesuffix(" plane") in _DEFAULT_PLANES and feature_by_name(doc, name) is None:
        return Ref(default_plane(doc, name.lower().removesuffix(" plane")), "feature", ref)
    f = feature_by_name(doc, name)
    if f is None:
        raise SwError(f"Nothing named '{name}' in the feature tree. Use list_features to see names.")
    return Ref(f, "feature", ref)


def select(doc: Any, refs: list[str] | str, mark: int = 0, append: bool = True) -> list[Ref]:
    """Resolve and select references with the given selection mark (selection is not cleared)."""
    if isinstance(refs, str):
        refs = [refs]
    out = [resolve(doc, r) for r in refs]
    for r in out:
        r.select(doc, append=append, mark=mark)
    return out

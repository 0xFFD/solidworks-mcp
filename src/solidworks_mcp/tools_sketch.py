"""Sketch tools."""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW, const_name, mm, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool

STATUS = {1: "unknown", 2: "under_defined", 3: "fully_defined", 4: "over_defined", 5: "no_solution", 6: "invalid_solution", 7: "autosolve_off"}


# ------------------------------------------------------------------------------------------ transforms


class SketchFrame:
    """Model <-> sketch coordinates for a 2D sketch, from ISketch.ModelToSketchTransform.

    SolidWorks transforms act on row vectors: sketch = scale * (model . R) + T.
    """

    def __init__(self, sketch: Any):
        arr = list(SW.cast(sketch.ModelToSketchTransform, "IMathTransform").ArrayData)
        self.R = [arr[0:3], arr[3:6], arr[6:9]]
        self.T = arr[9:12]
        self.s = arr[12] or 1.0

    def to_sketch(self, p: list[float]) -> list[float]:
        return [self.s * sum(p[j] * self.R[j][i] for j in range(3)) + self.T[i] for i in range(3)]

    def to_model(self, p: list[float]) -> list[float]:
        q = [(p[i] - self.T[i]) / self.s for i in range(3)]
        return [sum(q[j] * self.R[i][j] for j in range(3)) for i in range(3)]

    def describe(self) -> dict:
        o = self.to_model([0.0, 0.0, 0.0])
        x = [a - b for a, b in zip(self.to_model([1.0, 0.0, 0.0]), o)]
        y = [a - b for a, b in zip(self.to_model([0.0, 1.0, 0.0]), o)]
        n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
        r = lambda v: [round(c, 6) + 0.0 for c in v]  # noqa: E731
        return {"origin_mm": [round(to_mm(c), 4) + 0.0 for c in o], "sketch_x_axis": r(x), "sketch_y_axis": r(y), "normal": r(n)}


def active_sketch(doc: Any) -> Any:
    sk = doc.SketchManager.ActiveSketch
    if sk is None:
        raise SwError("No sketch is open. Call start_sketch or edit_sketch first.")
    return SW.cast(sk, "ISketch")


def sketch_name(doc: Any, sketch: Any) -> str:
    """Feature name of an ISketch (an ISketch does not expose IFeature through QueryInterface)."""
    feats = [f for f in g.iter_features(doc) if f.GetTypeName2() in ("ProfileFeature", "3DProfileFeature")]
    for f in reversed(feats):
        if SW.app.IsSame(f.GetSpecificFeature2(), sketch) == 1:
            return f.Name
    return feats[-1].Name if feats else "?"


def last_sketch(doc: Any) -> str:
    """Name of the most recent sketch in the feature tree."""
    name = None
    for f in g.iter_features(doc):
        if f.GetTypeName2() in ("ProfileFeature", "3DProfileFeature"):
            name = f.Name
    if name is None:
        raise SwError("The model has no sketches yet. Create one with start_sketch.")
    return name


def sketch_status(sketch: Any) -> str:
    return STATUS.get(sketch.GetConstrainedStatus(), "unknown")


# ------------------------------------------------------------------------------------------ entities


def _to_sketch_mm(e: dict, frame: SketchFrame) -> dict:
    """Convert every model-space point [x, y, z] of an entity into sketch-space [x, y] (mm)."""

    def conv(p: Any, what: str) -> list[float]:
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            raise SwError(f"{what}: with coords='model' points are [x, y, z] in mm.")
        q = frame.to_sketch([mm(c) for c in p])
        return [to_mm(q[0]), to_mm(q[1])]

    out = dict(e)
    for k in shapes.POINT_KEYS:
        if out.get(k) is not None:
            out[k] = conv(out[k], f"{e.get('type')}.{k}")
    for k in shapes.LIST_KEYS:
        if out.get(k) is not None:
            out[k] = [conv(p, f"{e.get('type')}.{k}") for p in out[k]]
    return out


def _pt(e: dict, key: str) -> tuple[float, float]:
    p = e.get(key)
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        raise SwError(f"{e.get('type')}.{key}: expected [x, y] in mm.")
    return mm(p[0]), mm(p[1])


# Approximate advance widths (fraction of the character height) for the default sketch font, so
# characters placed one by one read as normal text without touching.
_GLYPH_ADVANCE = {
    **{c: 0.8 for c in "ABCDEFGHJKLNOPQRSTUVXYZ023456789"},
    "M": 1.05, "W": 1.15, "O": 0.95, "Q": 0.95, "C": 0.9, "G": 0.95, "D": 0.85,
    "I": 0.45, "1": 0.6, "J": 0.6, "-": 0.6, ".": 0.4, ",": 0.4, ":": 0.4, "/": 0.6, " ": 0.5,
}


def _doubles(values: list[float]) -> Any:
    """SolidWorks array arguments must be explicit arrays of doubles; a Python list is not accepted."""
    import pythoncom
    from win32com.client import VARIANT

    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def _circle(sm: Any, x: float, y: float, r: float) -> Any:
    return sm.CreateCircleByRadius(x, y, 0, r)


def _add(doc: Any, sm: Any, e: dict) -> list:
    """Create one entity (points in sketch space, mm) and return the created objects."""
    t = (e.get("type") or "").lower()
    P = lambda key: _pt(e, key)  # noqa: E731
    segs: list = []
    if t in ("line", "centerline"):
        (x1, y1), (x2, y2) = P("from"), P("to")
        segs.append((sm.CreateCenterLine if t == "centerline" else sm.CreateLine)(x1, y1, 0, x2, y2, 0))
    elif t == "polyline":
        pts = [(mm(p[0]), mm(p[1])) for p in e.get("points") or []]
        if len(pts) < 2:
            raise SwError("polyline needs at least 2 points.")
        closed = bool(e.get("closed"))
        if closed and len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-9:
            pts.pop()
        r = e.get("radius", 0) or 0
        radii = [mm(v) for v in r] if isinstance(r, (list, tuple)) else [mm(r)] * len(pts)
        if len(radii) != len(pts):
            raise SwError(f"polyline radius list needs one value per point ({len(pts)}).")
        for it in shapes.rounded_path(pts, radii, closed):
            if it[0] == "line":
                segs.append(sm.CreateLine(it[1], it[2], 0, it[3], it[4], 0))
            else:
                segs.append(sm.CreateArc(it[1], it[2], 0, it[3], it[4], 0, it[5], it[6], 0, it[7]))
    elif t == "rectangle":
        (x1, y1), (x2, y2) = P("corner1"), P("corner2")
        segs += list(sm.CreateCornerRectangle(x1, y1, 0, x2, y2, 0) or [])
    elif t == "center_rectangle":
        (cx, cy) = P("center")
        w, h = (mm(v) for v in e.get("size") or (0, 0))
        if w <= 0 or h <= 0:
            raise SwError("center_rectangle size must be [width, height] with both above 0.")
        # Drawn from its corners: the construction diagonals of CreateCenterRectangle capture the corners of
        # a second centre rectangle on the same centre and pull it to the outer one's proportions.
        segs += list(sm.CreateCornerRectangle(cx - w / 2, cy - h / 2, 0, cx + w / 2, cy + h / 2, 0) or [])
    elif t == "rounded_rectangle":
        (x1, y1), (x2, y2) = e["corner1"], e["corner2"]
        r = float(e.get("radius", 0))
        if r <= 0 or 2 * r > min(abs(x2 - x1), abs(y2 - y1)) + 1e-9:
            raise SwError("rounded_rectangle: radius must be positive and at most half the shorter side.")
        xa, xb, ya, yb = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        return _add(doc, sm, {"type": "polyline", "closed": True, "radius": r, "points": [[xa, ya], [xb, ya], [xb, yb], [xa, yb]]})
    elif t == "circle":
        (cx, cy) = P("center")
        r = mm(e["radius"]) if "radius" in e else mm(e.get("diameter", 0)) / 2
        if r <= 0:
            raise SwError("circle needs a positive radius or diameter.")
        segs.append(_circle(sm, cx, cy, r))
    elif t == "bolt_circle":
        (cx, cy) = P("center")
        pcd, hd, count = mm(e.get("pcd", 0)), mm(e.get("hole_diameter", 0)), int(e.get("count", 4))
        if pcd <= 0 or hd <= 0 or count < 1:
            raise SwError("bolt_circle needs pcd (pitch circle diameter), hole_diameter and count.")
        a0 = math.radians(e.get("start_angle", 0))
        for i in range(count):
            a = a0 + 2 * math.pi * i / count
            segs.append(_circle(sm, cx + pcd / 2 * math.cos(a), cy + pcd / 2 * math.sin(a), hd / 2))
    elif t == "circle_grid":
        (ox, oy) = P("origin")
        nx, ny = (int(v) for v in (e.get("count") or (1, 1)))
        dx, dy = (mm(v) for v in (e.get("pitch") or (0, 0)))
        hd = mm(e.get("diameter", 0))
        if hd <= 0 or nx < 1 or ny < 1:
            raise SwError("circle_grid needs origin, count [nx, ny], pitch [dx, dy] and diameter.")
        for i in range(nx):
            for j in range(ny):
                segs.append(_circle(sm, ox + i * dx, oy + j * dy, hd / 2))
    elif t == "arc":
        (cx, cy), (sx, sy), (ex, ey) = P("center"), P("start"), P("end")
        d = -1 if (e.get("direction") or "ccw").lower() == "cw" else 1
        segs.append(sm.CreateArc(cx, cy, 0, sx, sy, 0, ex, ey, 0, d))
    elif t == "ellipse":
        (cx, cy) = P("center")
        a, b = mm(e.get("radius_x", 0)), mm(e.get("radius_y", 0))
        if a <= 0 or b <= 0:
            raise SwError("ellipse needs radius_x and radius_y.")
        ang = math.radians(e.get("angle", 0))
        ca, sa = math.cos(ang), math.sin(ang)
        segs.append(sm.CreateEllipse(cx, cy, 0, cx + a * ca, cy + a * sa, 0, cx - b * sa, cy + b * ca, 0))
    elif t == "polygon":
        (cx, cy) = P("center")
        sides = int(e.get("sides", 6))
        r = mm(e.get("radius", 0))
        a0 = math.radians(e.get("angle", 90))
        segs += list(sm.CreatePolygon(cx, cy, 0, cx + r * math.cos(a0), cy + r * math.sin(a0), 0, sides, bool(e.get("inscribed", True))) or [])
    elif t == "slot":
        (x1, y1), (x2, y2) = P("start"), P("end")
        w = mm(e.get("width", 0))
        C = SW.C
        slot = sm.CreateSketchSlot(C.swSketchSlotCreationType_line, C.swSketchSlotLengthType_CenterCenter, w, x1, y1, 0, x2, y2, 0, 0, 0, 0, 1, False)
        if slot is not None:
            segs.append(slot)
    elif t == "point":
        (x, y) = P("at")
        segs.append(sm.CreatePoint(x, y, 0))
    elif t == "spline":
        pts = [(mm(p[0]), mm(p[1])) for p in e.get("points") or []]
        if len(pts) < 2:
            raise SwError("spline needs at least 2 points.")
        segs.append(sm.CreateSpline2(_doubles([c for x, y in pts for c in (x, y, 0.0)]), True))
    elif t == "airfoil":
        (ox, oy) = P("origin")
        chord = mm(e.get("chord", 0))
        if chord <= 0:
            raise SwError("airfoil needs a chord length.")
        loop = shapes.airfoil_loop(str(e.get("naca", "0012")), chord, (ox, oy), float(e.get("angle", 0)), int(e.get("points", 40)))
        segs.append(sm.CreateSpline2(_doubles([c for x, y in loop for c in (x, y, 0.0)]), False))
        segs.append(sm.CreateLine(loop[-1][0], loop[-1][1], 0, loop[0][0], loop[0][1], 0))  # blunt trailing edge
    elif t == "text":
        (x, y) = P("at")
        txt = str(e.get("text", ""))
        if not txt:
            raise SwError("text needs a non-empty text.")
        # One text entity per character at a fixed pitch: in a single text entity adjacent glyph
        # outlines can touch, and SolidWorks then refuses to extrude or cut the whole string.
        h = mm(e.get("height", 5))
        spacing = float(e.get("spacing", 1.0))
        ang = math.radians(e.get("angle", 0))
        sm.DisplayWhenAdded = True  # text outlines are only generated when the text is displayed
        try:
            pos = 0.0
            for ch in txt:
                adv = h * spacing * _GLYPH_ADVANCE.get(ch.upper(), 0.8)
                if ch.isspace():
                    pos += adv
                    continue
                st = doc.InsertSketchText(x + pos * math.cos(ang), y + pos * math.sin(ang), 0, ch, 0, 0, 0, 100, 0)
                pos += adv
                if st is not None:
                    st = SW.cast(st, "ISketchText")
                    fmt = SW.cast(st.GetTextFormat(), "ITextFormat")
                    if fmt is not None:
                        fmt.CharHeight = h
                        if e.get("font"):
                            fmt.TypeFaceName = str(e["font"])
                        if e.get("bold"):
                            fmt.Bold = True
                        if ang:
                            fmt.Escapement = ang
                        st.SetTextFormat(False, fmt)
                segs.append(st)
        finally:
            sm.DisplayWhenAdded = False
    else:
        raise SwError(
            f"Unknown sketch entity type '{t}'. Use line, centerline, polyline, rectangle, center_rectangle, rounded_rectangle, "
            "circle, bolt_circle, circle_grid, arc, ellipse, polygon, slot, point, spline, airfoil or text."
        )
    if not segs or any(s is None for s in segs):
        raise SwError(f"SolidWorks rejected the {t} {e}.")
    if e.get("construction"):
        for s in segs:
            try:
                SW.cast(s, "ISketchSegment").ConstructionGeometry = True
            except Exception:
                pass
    return segs


def _add3d(sm: Any, e: dict) -> list:
    """Create one entity of a 3D sketch (points [x, y, z] in model mm)."""
    t = (e.get("type") or "").lower()

    def pts(key: str) -> list[tuple[float, float, float]]:
        raw = e.get(key) if key == "points" else [e.get(key)]
        out = []
        for p in raw or []:
            if not isinstance(p, (list, tuple)) or len(p) != 3:
                raise SwError(f"{t}.{key}: in a 3D sketch points are [x, y, z] in model mm.")
            out.append(tuple(mm(c) for c in p))
        return out

    segs: list = []
    if t in ("line", "centerline"):
        (a,), (b,) = pts("from"), pts("to")
        segs.append((sm.CreateCenterLine if t == "centerline" else sm.CreateLine)(*a, *b))
    elif t == "polyline":
        ps = pts("points")
        closed = bool(e.get("closed"))
        if closed and len(ps) > 2 and math.dist(ps[0], ps[-1]) < 1e-9:
            ps.pop()
        if len(ps) < 2:
            raise SwError("polyline needs at least 2 points.")
        r = e.get("radius", 0) or 0
        radii = [mm(v) for v in r] if isinstance(r, (list, tuple)) else [mm(r)] * len(ps)
        if len(radii) != len(ps):
            raise SwError(f"polyline radius list needs one value per point ({len(ps)}).")
        for it in shapes.rounded_path_3d(ps, radii, closed):  # bend radii for tube and harness runs
            if it[0] == "line":
                segs.append(sm.CreateLine(*it[1], *it[2]))
            else:
                segs.append(sm.Create3PointArc(*it[1], *it[3], *it[2]))
    elif t == "arc":
        (a,), (m,), (b,) = pts("start"), pts("mid"), pts("end")
        segs.append(sm.Create3PointArc(*a, *b, *m))
    elif t == "spline":
        ps = pts("points")
        if len(ps) < 2:
            raise SwError("spline needs at least 2 points.")
        segs.append(sm.CreateSpline2(_doubles([c for p in ps for c in p]), True))
    elif t == "point":
        (a,) = pts("at")
        segs.append(sm.CreatePoint(*a))
    else:
        raise SwError(f"'{t}' is not available in a 3D sketch. Use line, centerline, polyline (with optional bend radius), arc (start/mid/end), spline or point.")
    if not segs or any(s is None for s in segs):
        raise SwError(f"SolidWorks rejected the {t} {e}.")
    if e.get("construction"):
        for s in segs:
            SW.cast(s, "ISketchSegment").ConstructionGeometry = True
    return segs


# ------------------------------------------------------------------------------------------ tools


@tool(WRITES)
def start_sketch(
    on: Annotated[str, Field(description="Sketch plane: 'plane:Front' (XY), 'plane:Top' (XZ), 'plane:Right' (YZ), another plane name, a planar face ('face:3' or 'face@x,y,z'), or '3d' for a 3D sketch (points [x, y, z] in model mm; for sweep paths, tubes and harness runs).")],
) -> dict:
    """Open a new 2D sketch on a plane or planar face, or a 3D sketch. Returns how sketch X/Y map into
    model space so coordinates can be converted; on the Top plane sketch +Y is model -Z."""
    doc = SW.active()
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    if on.strip().lower() in ("3d", "3d_sketch", "3dsketch"):
        doc.SketchManager.Insert3DSketch(True)
        sk = doc.SketchManager.ActiveSketch
        if sk is None:
            raise SwError("Could not start a 3D sketch.")
        return {"sketch": sketch_name(doc, SW.cast(sk, "ISketch")), "3d": True, "note": "Points are [x, y, z] in model mm."}
    g.select(doc, on, append=False)
    doc.SketchManager.InsertSketch(True)
    sk = doc.SketchManager.ActiveSketch
    if sk is None:
        raise SwError(f"Could not start a sketch on {on}. Use a plane or a planar face.")
    sk = SW.cast(sk, "ISketch")
    out = {"sketch": sketch_name(doc, sk)}
    out.update(SketchFrame(sk).describe())
    return out


@tool(WRITES)
def edit_sketch(
    sketch: Annotated[str, Field(description="Name of an existing sketch, e.g. 'Sketch3'.")],
) -> dict:
    """Reopen an existing sketch to add geometry with sketch(); close it again with finish_sketch.
    Features built on the sketch update when it is closed."""
    doc = SW.active()
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    f = g.feature_by_name(doc, sketch)
    if f is None or f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
        raise SwError(f"'{sketch}' is not a sketch. Use list_features to see sketch names.")
    f.Select2(False, 0)
    doc.EditSketch()
    sk = doc.SketchManager.ActiveSketch
    if sk is None:
        raise SwError(f"Could not open {sketch} for editing.")
    sk = SW.cast(sk, "ISketch")
    out = {"sketch": sketch, "status": sketch_status(sk)}
    out.update(SketchFrame(sk).describe())
    return out


@tool(WRITES)
def sketch(
    entities: Annotated[list[dict[str, Any]], Field(description=(
        "Entities to add, in mm. Points are [x, y] in sketch coordinates (or [x, y, z] with coords='model'). Types: "
        "{type:'line', from, to}, {type:'centerline', from, to}, "
        "{type:'polyline', points:[...], closed, radius} (radius: corner fillet in mm, one value or one per point; draws any rounded outline such as a frame plate or arm), "
        "{type:'rectangle', corner1, corner2}, {type:'center_rectangle', center, size:[w, h]}, {type:'rounded_rectangle', corner1, corner2, radius}, "
        "{type:'circle', center, radius|diameter}, "
        "{type:'bolt_circle', center, pcd, count, hole_diameter, start_angle} (pcd: pitch circle diameter; a square S x S pattern is pcd S*1.4142 at start_angle 45), "
        "{type:'circle_grid', origin, count:[nx, ny], pitch:[dx, dy], diameter}, "
        "{type:'arc', center, start, end, direction:'ccw'|'cw'}, {type:'ellipse', center, radius_x, radius_y, angle}, "
        "{type:'polygon', center, sides, radius, inscribed, angle}, {type:'slot', start, end, width}, {type:'point', at}, {type:'spline', points}, "
        "{type:'airfoil', naca:'2412', chord, origin (leading edge), angle (degrees counterclockwise), points} (NACA 4-digit section as a closed profile), "
        "{type:'text', at, text, height, angle, spacing, font, bold} (part number marking, at = start of the baseline, spacing scales the character pitch, default 1.0, font a Windows font name such as 'Arial' or a stencil font; extrude-cut it about 0.3 mm). "
        "Any entity can carry mirror:'left_right' | 'up_down' | 'both' to add copies mirrored about the sketch axes, "
        "and construction:true to make it construction geometry. In a 3D sketch (start_sketch on='3d') points are [x, y, z] in model mm "
        "and the types are line, centerline, polyline (radius bends the corners, for tube and harness runs), arc {start, mid, end}, spline and point."))],
    coords: Annotated[Literal["sketch", "model"], Field(description="Coordinate space of the points.")] = "sketch",
) -> dict:
    """Add geometry to the open sketch at exact coordinates (snapping and inference are bypassed)."""
    doc = SW.active()
    sk = active_sketch(doc)
    is3d = bool(sk.Is3D())
    frame = SketchFrame(sk) if coords == "model" and not is3d else None
    sm = doc.SketchManager
    sm.AddToDB = True
    sm.DisplayWhenAdded = False
    created = []
    try:
        for i, e in enumerate(entities):
            try:
                if is3d:
                    created.append({"type": e.get("type"), "segments": len(_add3d(sm, e))})
                    continue
                base = _to_sketch_mm(e, frame) if frame else e
                count = sum(len(_add(doc, sm, item)) for item in shapes.expand_mirror(base))
                created.append({"type": e.get("type"), "segments": count})
            except SwError as err:
                raise SwError(f"entities[{i}]: {err}") from None
    finally:
        sm.AddToDB = False
        sm.DisplayWhenAdded = True
    doc.GraphicsRedraw2()
    return {"sketch": sketch_name(doc, sk), "added": created, "status": sketch_status(sk)}


@tool(WRITES)
def finish_sketch(
    fully_define: Annotated[bool, Field(description="Add relations and dimensions so the sketch is fully defined (good practice for released parts; the dimensions also appear in drawings).")] = False,
    scheme: Annotated[Literal["baseline", "chain", "ordinate"], Field(description="Dimension scheme used by fully_define.")] = "baseline",
) -> dict:
    """Close the open sketch and report its definition status and closed regions."""
    doc = SW.active()
    sk = active_sketch(doc)
    name = sketch_name(doc, sk)
    C = SW.C
    note = None
    if fully_define:
        rel = 0
        for n in ("Coincident", "Colinear", "Concentric", "Equal", "Horizontal", "Midpoint", "Parallel", "Perpendicular", "Tangent", "Vertical"):
            rel |= getattr(C, f"swSketchFullyDefineRelationType_{n}")
        sch = {"chain": 0, "baseline": 1, "ordinate": 2}[scheme]
        doc.ClearSelection2(True)
        # the sketch origin is the datum for both directions (selection mark 6)
        doc.Extension.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, False, 6, None, 0)
        doc.SketchManager.FullyDefineSketch(True, True, rel, True, sch, None, sch, None, 1, 1)
        doc.ClearSelection2(True)
        if sketch_status(sk) != "fully_defined":
            note = "SolidWorks could not fully define every entity; check the sketch in SolidWorks."
    status = sketch_status(sk)
    is3d = bool(sk.Is3D())
    regions = sk.GetSketchRegionCount()
    doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    if is3d:
        return {"sketch": name, "status": status, "3d": True}
    out = {"sketch": name, "status": status, "closed_regions": regions}
    if note:
        out["note"] = note
    if regions == 0:
        out["hint"] = "No closed region: extrude and revolve need a closed profile (sweep paths and guide curves do not)."
    return out


# ------------------------------------------------------------------------------------------ editing

_SEG_RE = re.compile(r"^(seg|pt)(?::(\d+)|@\s*(-?[\d.]+(?:e-?\d+)?)\s*,\s*(-?[\d.]+(?:e-?\d+)?)\s*(?:,\s*(-?[\d.]+(?:e-?\d+)?)\s*)?)$", re.I)

RELATIONS = {
    "horizontal": "sgHORIZONTAL2D", "vertical": "sgVERTICAL2D", "coincident": "sgCOINCIDENT", "tangent": "sgTANGENT",
    "parallel": "sgPARALLEL", "perpendicular": "sgPERPENDICULAR", "concentric": "sgCONCENTRIC", "equal": "sgSAMELENGTH",
    "collinear": "sgCOLINEAR", "fixed": "sgFIXED", "midpoint": "sgATMIDDLE", "coradial": "sgCORADIAL",
    "symmetric": "sgSYMMETRIC", "merge": "sgMERGEPOINTS",
    # 3D sketches: a line along a model axis
    "along_x": "sgALONGX3D", "along_y": "sgALONGY3D", "along_z": "sgALONGZ3D",
}


def _xy(p: Any) -> tuple[float, float]:
    p = SW.cast(p, "ISketchPoint")
    return to_mm(p.X), to_mm(p.Y)


def _seg_geom(seg: Any) -> dict:
    """Describe a sketch segment in sketch mm. Keys starting with '_' are for picking only."""
    C = SW.C
    t = seg.GetType()
    r = lambda v: round(v, 4) + 0.0  # noqa: E731
    info: dict[str, Any] = {}
    if t == C.swSketchLINE:
        ln = SW.cast(seg, "ISketchLine")
        a, b = _xy(ln.GetStartPoint2()), _xy(ln.GetEndPoint2())
        info.update(type="line", start=[r(v) for v in a], end=[r(v) for v in b], length=r(math.dist(a, b)), _pick=("line", a, b))
    elif t == C.swSketchARC:
        arc = SW.cast(seg, "ISketchArc")
        c, rad_ = _xy(arc.GetCenterPoint2()), to_mm(arc.GetRadius())
        if arc.IsCircle():
            info.update(type="circle", center=[r(v) for v in c], radius=r(rad_), _pick=("arc", c, rad_, None, None, True))
        else:
            s, e = _xy(arc.GetStartPoint2()), _xy(arc.GetEndPoint2())
            ccw = arc.GetRotationDir() != -1
            info.update(type="arc", center=[r(v) for v in c], radius=r(rad_), start=[r(v) for v in s], end=[r(v) for v in e],
                        _pick=("arc", c, rad_, s, e, ccw))
    elif t == C.swSketchELLIPSE:
        el = SW.cast(seg, "ISketchEllipse")
        c, a, b = _xy(el.GetCenterPoint2()), _xy(el.GetMajorPoint2()), _xy(el.GetMinorPoint2())
        ra, rb = math.dist(c, a), math.dist(c, b)
        ang = math.atan2(a[1] - c[1], a[0] - c[0])
        ring = [(c[0] + ra * math.cos(u) * math.cos(ang) - rb * math.sin(u) * math.sin(ang),
                 c[1] + ra * math.cos(u) * math.sin(ang) + rb * math.sin(u) * math.cos(ang)) for u in (2 * math.pi * k / 72 for k in range(73))]
        info.update(type="ellipse", center=[r(v) for v in c], radius_x=r(ra), radius_y=r(rb), _pick=("poly", ring))
    elif t == C.swSketchSPLINE:
        sp = SW.cast(seg, "ISketchSpline")
        pts = [_xy(q) for q in sp.GetPoints2() or []]
        info.update(type="spline", start=[r(v) for v in pts[0]], end=[r(v) for v in pts[-1]], points=len(pts), _pick=("poly", pts))
    elif t == C.swSketchTEXT:
        info.update(type="text")
    else:
        info.update(type=const_name("swSketch", t).lower())
    if seg.ConstructionGeometry:
        info["construction"] = True
    return info


def _dist(info: dict, p: tuple[float, float]) -> float:
    pick = info.get("_pick")
    if pick is None:
        return float("inf")
    if pick[0] == "line":
        return shapes.dist_to_line(p, pick[1], pick[2])
    if pick[0] == "arc":
        return shapes.dist_to_arc(p, *pick[1:])
    return shapes.dist_to_polyline(p, pick[1])


def _segments(sk: Any) -> list[Any]:
    return SW.cast_all(sk.GetSketchSegments(), "ISketchSegment")


def _points(sk: Any) -> list[Any]:
    return SW.cast_all(sk.GetSketchPoints2(), "ISketchPoint")


def _xyz(p: Any) -> list[float]:
    p = SW.cast(p, "ISketchPoint")
    return [p.X, p.Y, p.Z]


def _dist3d(seg: Any, p: list[float]) -> float:
    """Distance (metres) from a model point to a 3D sketch segment, bounded to the segment."""
    seg = SW.cast(seg, "ISketchSegment")
    if seg.GetType() == SW.C.swSketchLINE:
        ln = SW.cast(seg, "ISketchLine")
        a, b = _xyz(ln.GetStartPoint2()), _xyz(ln.GetEndPoint2())
        d = [b[i] - a[i] for i in range(3)]
        n2 = sum(c * c for c in d) or 1e-30
        t = max(0.0, min(1.0, sum((p[i] - a[i]) * d[i] for i in range(3)) / n2))
        return math.dist(p, [a[i] + d[i] * t for i in range(3)])
    q = SW.cast(seg.GetCurve(), "ICurve").GetClosestPointOn(*p)
    return math.dist(p, list(q)[:3]) if q is not None else float("inf")


def _sketch_item(sk: Any, ref: str) -> Any:
    """A sketch segment or point: 'seg:N', 'pt:N' (list_sketch indices), 'seg@x,y', 'pt@x,y' (nearest, sketch mm)
    or, in a 3D sketch, 'seg@x,y,z', 'pt@x,y,z' (model mm)."""
    m = _SEG_RE.match(ref.strip())
    if not m:
        raise SwError(f"'{ref}' is not a sketch reference. Use 'seg:N', 'pt:N', 'seg@x,y' or 'pt@x,y' (sketch mm, from list_sketch).")
    kind = m.group(1).lower()
    what = "segments" if kind == "seg" else "points"
    items = _segments(sk) if kind == "seg" else _points(sk)
    if m.group(2) is not None:
        i = int(m.group(2))
        if i >= len(items):
            raise SwError(f"{ref}: the sketch has {len(items)} {what}. Indices change after edits; run list_sketch again.")
        return items[i]
    if m.group(5) is not None:  # 'seg@x,y,z' in a 3D sketch: model mm
        p3 = [mm(float(m.group(k))) for k in (3, 4, 5)]
        dists = [to_mm(math.dist(p3, _xyz(q))) for q in items] if kind == "pt" else [to_mm(_dist3d(s, p3)) for s in items]
    else:
        p = (float(m.group(3)), float(m.group(4)))
        dists = [math.dist(p, _xy(q)) for q in items] if kind == "pt" else [_dist(_seg_geom(s), p) for s in items]
    if not dists:
        raise SwError(f"{ref}: the sketch has no {what}.")
    i = min(range(len(dists)), key=dists.__getitem__)
    if dists[i] > 0.5:
        raise SwError(f"{ref}: the nearest one is {dists[i]:.3f} mm away. Use list_sketch for exact positions.")
    return items[i]


def _select_items(doc: Any, sk: Any, refs: list[str], mark: int = 0) -> list[Any]:
    """Add sketch items ('seg@..', 'pt:..'), the origin ('origin') or model entities (edges, faces, planes) to the selection."""
    sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
    out = []
    for ref in refs:
        if ref.strip().lower() == "origin":
            if not doc.Extension.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, True, mark, None, 0):
                raise SwError("Could not select the sketch origin.")
            out.append(None)
        elif _SEG_RE.match(ref.strip()):
            item = _sketch_item(sk, ref)
            data = SW.cast(sm.CreateSelectData(), "ISelectData")
            data.Mark = mark
            if not item.Select4(True, data):
                raise SwError(f"Could not select {ref}.")
            out.append(item)
        else:
            out += [r.obj for r in g.select(doc, ref, mark=mark)]
    return out


def _pick_segment(doc: Any, sk: Any, at: list[float], to_model) -> bool:
    """Select the segment under a sketch point the way a mouse pick does, which SketchTrim needs. The pick
    uses a screen tolerance, so when it lands on a neighbouring segment the view is zoomed in and it is
    retried. Returns True when the view was zoomed; zoom back out only after the trim."""
    want = _sketch_item(sk, f"seg@{at[0]},{at[1]}")
    p = to_model(at)
    sel = SW.cast(doc.SelectionManager, "ISelectionMgr")
    for zoom in (False, True):
        if zoom:
            h = 0.002
            doc.ViewZoomTo2(p[0] - h, p[1] - h, p[2] - h, p[0] + h, p[1] + h, p[2] + h)
        doc.ClearSelection2(True)
        doc.Extension.SelectByID2("", "SKETCHSEGMENT", p[0], p[1], p[2], False, 0, None, 0)
        got = sel.GetSelectedObject6(1, -1) if sel.GetSelectedObjectCount2(-1) else None
        if got is not None and SW.app.IsSame(got, want) == 1:
            return zoom
    doc.ViewZoomtofit2()
    doc.ClearSelection2(True)
    raise SwError(f"Could not pick the segment at {at}; give a point further from other segments.")


def _signature(sk: Any) -> list[str]:
    return sorted(str({k: v for k, v in _seg_geom(s).items() if not k.startswith("_")}) for s in _segments(sk))


@tool(READ_ONLY)
def list_sketch(
    sketch: Annotated[str | None, Field(description="Sketch name; default is the open sketch.")] = None,
    near: Annotated[list[float] | None, Field(description="Sort by distance from this sketch point [x, y] (mm).")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """Segments and points of a sketch in sketch coordinates (mm), with the indices that modify_sketch
    takes as 'seg:N' and 'pt:N'. Indices change after every edit; 'seg@x,y' and 'pt@x,y' do not."""
    doc = SW.active()
    if sketch:
        f = g.feature_by_name(doc, sketch)
        if f is None or f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
            raise SwError(f"'{sketch}' is not a sketch. Use list_features to see sketch names.")
        sk = SW.cast(f.GetSpecificFeature2(), "ISketch")
    else:
        sk = active_sketch(doc)
    segs = []
    for i, s in enumerate(_segments(sk)):
        info = _seg_geom(s)
        row = {"i": i, **{k: v for k, v in info.items() if not k.startswith("_")}}
        if near:
            row["_d"] = _dist(info, (near[0], near[1]))
        segs.append(row)
    pts = [{"i": i, "at": [round(v, 4) + 0.0 for v in _xy(p)]} for i, p in enumerate(_points(sk))]
    if near:
        segs.sort(key=lambda r: r.pop("_d"))
        pts.sort(key=lambda r: math.dist(r["at"], near[:2]))
    return {"sketch": sketch or sketch_name(doc, sk), "status": sketch_status(sk), "segment_count": len(segs), "segments": segs[:limit],
            "point_count": len(pts), "points": pts[:limit], **SketchFrame(sk).describe()}


def _no_dim_prompt(fn):
    """Run fn with the 'enter dimension value' dialog switched off."""
    C = SW.C
    old = SW.app.GetUserPreferenceToggle(C.swInputDimValOnCreate)
    SW.app.SetUserPreferenceToggle(C.swInputDimValOnCreate, False)
    try:
        return fn()
    finally:
        SW.app.SetUserPreferenceToggle(C.swInputDimValOnCreate, old)


def _dimension(doc: Any, sk: Any, op: dict, items: list[str], to_model) -> dict:
    C = SW.C
    if not items:
        raise SwError("dimension needs items: one segment (length, diameter or radius) or two segments/points.")
    _select_items(doc, sk, items)
    at = op.get("at")
    if at is None:  # beside the first item
        info = _seg_geom(_sketch_item(sk, items[0])) if items[0].strip().lower().startswith("seg") else {}
        base = info.get("center") or info.get("start") or [0, 0]
        at = [base[0] + 5, base[1] + 5]
    p = to_model(at)
    orient = (op.get("orientation") or "aligned").lower()
    add = {"aligned": doc.AddDimension2, "horizontal": doc.AddHorizontalDimension2, "vertical": doc.AddVerticalDimension2}.get(orient)
    if add is None:
        raise SwError("dimension orientation must be aligned, horizontal or vertical.")
    dd = _no_dim_prompt(lambda: add(p[0], p[1], p[2]))
    if dd is None:
        raise SwError("SolidWorks could not dimension the selection.")
    d = SW.cast(SW.cast(dd, "IDisplayDimension").GetDimension2(0), "IDimension")
    name = "@".join(d.FullName.split("@")[:2])
    angular = d.GetType() == C.swDimensionParamTypeDoubleAngular
    if op.get("value") is not None:
        v = float(op["value"])
        r = d.SetSystemValue3(math.radians(v) if angular else mm(v), C.swSetValue_InAllConfigurations, None)
        if r not in (0, None):
            raise SwError(f"SolidWorks rejected {name} = {v}: it would over-define the sketch or is a driven dimension.")
    val = d.GetSystemValue3(C.swThisConfiguration, None)
    val = val[0] if isinstance(val, (tuple, list)) else val
    return {"dimension": name, "value": round(math.degrees(val), 6) if angular else to_mm(val), "unit": "deg" if angular else "mm"}


def _op(doc: Any, sk: Any, op: dict) -> dict:
    C = SW.C
    sm = doc.SketchManager
    kind = (op.get("op") or "").lower()
    items = op.get("items") or []
    if isinstance(items, str):
        items = [items]
    frame = SketchFrame(sk)
    to_model = lambda xy: frame.to_model([mm(xy[0]), mm(xy[1]), 0.0])  # noqa: E731
    out: dict[str, Any] = {"op": kind}
    doc.ClearSelection2(True)
    n = len(_segments(sk))

    if kind == "delete":
        _select_items(doc, sk, items)
        if not doc.Extension.DeleteSelection2(0):
            raise SwError("SolidWorks could not delete the selection.")
    elif kind in ("move", "copy"):
        by = op.get("by") or [0, 0]
        _select_items(doc, sk, items)
        doc.Extension.MoveOrCopy(kind == "copy", int(op.get("count", 1)), bool(op.get("keep_relations", False)), 0, 0, 0, mm(by[0]), mm(by[1]), 0)
    elif kind == "set_point":
        to = op.get("to")
        if len(items) != 1 or not to or len(to) not in (2, 3):
            raise SwError("set_point needs one point (items:['pt@x,y']) and to:[x, y] (or [x, y, z] in a 3D sketch).")
        pt = SW.cast(_sketch_item(sk, items[0]), "ISketchPoint")
        q = [mm(c) for c in to] + [0.0] * (3 - len(to))
        sm.AddToDB = True
        try:
            ok = pt.SetCoords(*q)
        finally:
            sm.AddToDB = False
        got = [pt.X, pt.Y, pt.Z]
        if not ok or math.dist(got, q) > 1e-7:
            raise SwError(f"SolidWorks could not put the point at {to}; relations or dimensions may hold it (it is at {[round(to_mm(c), 4) for c in got]}).")
    elif kind == "spline_tangent":
        at, angle = op.get("at"), op.get("angle")
        if len(items) != 1 or not at or angle is None:
            raise SwError("spline_tangent needs one spline (items:['seg@x,y']), at:[x, y] (one of its fit points) and angle (degrees from sketch x).")
        seg = SW.cast(_sketch_item(sk, items[0]), "ISketchSegment")
        if seg.GetType() != C.swSketchSPLINE:
            raise SwError(f"{items[0]} is not a spline.")
        sp = SW.cast(seg, "ISketchSpline")
        q = [mm(c) for c in at] + [0.0] * (3 - len(at))
        handles = SW.cast_all(sp.GetSplineHandles(), "ISplineHandle")
        h = min(handles, key=lambda x: math.dist([x.X, x.Y, x.Z], q)) if handles else None
        if h is None or math.dist([h.X, h.Y, h.Z], q) > 1e-5:
            raise SwError(f"{at} is not a fit point of that spline; use list_sketch for its points.")
        h.TangentDriving = True
        h.TangentRadialDirection = math.radians(float(angle))
        if op.get("weight") is not None:
            h.SetTangentMagnitude(float(op["weight"]), 0)
        curve = SW.cast(seg.GetCurve(), "ICurve")
        on = curve.GetClosestPointOn(*q)
        d = list(curve.Evaluate2(on[3], 1))[3:5]
        got = math.degrees(math.atan2(d[1], d[0]))
        if min(abs((got - float(angle) + 180) % 360 - 180), abs((got - float(angle)) % 360 - 180)) > 0.01:
            raise SwError(f"SolidWorks did not set the tangent (it is {got:.2f} deg).")
        out["tangent_deg"] = round(got, 3)
    elif kind == "picture":
        file, at, width = op.get("file"), op.get("at") or [0, 0], op.get("width")
        if not file or not width:
            raise SwError("picture needs file (a PNG, JPG or BMP), at:[x, y] (lower-left corner) and width (mm; the height keeps the aspect ratio).")
        path = app().path(file, must_exist=True, suffixes=(".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"))
        pic = sm.InsertSketchPicture2(str(path), True)
        if pic is None:
            raise SwError(f"SolidWorks could not insert the picture {path.name}.")
        pic = SW.cast(pic, "ISketchPicture")
        w0, h0 = pic.GetSize(0.0, 0.0)[:2]
        h_mm = mm(width) * (h0 / w0) if w0 else mm(width)
        pic.SetSize(mm(width), h_mm, True)
        pic.SetOrigin(mm(at[0]), mm(at[1]))
        out["picture"] = path.name
        out["size_mm"] = [round(to_mm(c), 3) for c in pic.GetSize(0.0, 0.0)[:2]]
        out["origin_mm"] = [round(to_mm(c), 3) for c in pic.GetOrigin(0.0, 0.0)[:2]]
    elif kind == "spline_insert":
        at = op.get("at")
        if len(items) != 1 or not at:
            raise SwError("spline_insert needs one spline (items:['seg@x,y']) and at:[x, y], a point on or near it.")
        seg = SW.cast(_sketch_item(sk, items[0]), "ISketchSegment")
        if seg.GetType() != C.swSketchSPLINE:
            raise SwError(f"{items[0]} is not a spline.")
        sp = SW.cast(seg, "ISketchSpline")
        count = sp.GetPointCount()
        q = [mm(c) for c in at] + [0.0] * (3 - len(at))
        on = list(SW.cast(seg.GetCurve(), "ICurve").GetClosestPointOn(*q))[:3]
        if not sp.InsertPoint(*on) or sp.GetPointCount() != count + 1:
            raise SwError(f"SolidWorks could not insert a spline point near {at}.")
        out["inserted"] = [round(to_mm(c), 4) for c in on[:len(at)]]
    elif kind == "block":
        base, copies = op.get("base") or [0, 0], op.get("copies") or []
        if not items:
            raise SwError("block needs items (the geometry to make a block of), base:[x, y] and copies:[[x, y], ...].")
        _select_items(doc, sk, items)
        mu = SW.cast(SW.app.GetMathUtility(), "IMathUtility")
        defn = sm.MakeSketchBlockFromSelected(mu.CreatePoint(_doubles([mm(base[0]), mm(base[1]), 0.0])))
        doc.ClearSelection2(True)
        if defn is None:
            raise SwError("SolidWorks could not make a block of those items.")
        defn = SW.cast(defn, "ISketchBlockDefinition")
        ang = math.radians(float(op.get("angle", 0)))
        for at in copies:
            if sm.InsertSketchBlockInstance(defn, mu.CreatePoint(_doubles([mm(at[0]), mm(at[1]), 0.0])), 1.0, ang) is None:
                raise SwError(f"SolidWorks could not place a block instance at {at}.")
        out["block"] = SW.cast(defn.GetFeature(), "IFeature").Name if defn.GetFeature() is not None else None
        out["instances"] = defn.GetInstanceCount()
        if op.get("save_as"):
            target = app().path(str(op["save_as"]), suffixes=(".sldblk",))
            if not defn.Save(str(target)) or not target.exists():
                raise SwError(f"SolidWorks could not save the block to {target}.")
            out["saved"] = str(target)
    elif kind == "rotate":
        c = op.get("center") or [0, 0]
        _select_items(doc, sk, items)
        doc.Extension.RotateOrCopy(bool(op.get("copy", False)), int(op.get("count", 1)), bool(op.get("keep_relations", False)),
                                   mm(c[0]), mm(c[1]), 0, 0, 0, 1, math.radians(float(op.get("angle", 0))))
    elif kind == "trim":
        at = op.get("at")
        if not at:
            raise SwError("trim needs at:[x, y], a point on the piece to remove.")
        before = _signature(sk)
        zoomed = _pick_segment(doc, sk, at, to_model)
        # the pick point is in sketch coordinates here (SelectByID2 above took model coordinates);
        # SketchTrim returns False even when it trims
        sm.SketchTrim(C.swSketchTrimClosest, mm(at[0]), mm(at[1]), 0.0)
        if zoomed:
            doc.ViewZoomtofit2()
        if _signature(sk) == before:
            raise SwError(f"Nothing was trimmed at {at}. The point must lie on a segment.")
    elif kind in ("extend", "split"):
        at = op.get("at")
        if not at:
            raise SwError(f"{kind} needs at:[x, y], a point on the segment ({'near the end to extend' if kind == 'extend' else 'where to split it'}).")
        before = _signature(sk)
        if kind == "extend":
            zoomed = _pick_segment(doc, sk, at, to_model)
            sm.SketchExtend(mm(at[0]), mm(at[1]), 0.0)
            if zoomed:
                doc.ViewZoomtofit2()
        else:
            _sketch_item(sk, f"seg@{at[0]},{at[1]}").Select4(False, None)
            doc.SplitOpenSegment(mm(at[0]), mm(at[1]), 0.0)
        if _signature(sk) == before:
            raise SwError(f"Nothing was {'extended' if kind == 'extend' else 'split'} at {at}." + (" The segment must reach another segment when extended." if kind == "extend" else ""))
    elif kind == "pattern":
        _select_items(doc, sk, items)
        if op.get("center") is not None:  # circular, about center
            c = op["center"]
            seed = _seg_geom(_sketch_item(sk, items[0]))
            ref = seed.get("center") or seed.get("start")
            if ref is None:
                raise SwError("A circular sketch pattern needs a line, arc or circle as the first item.")
            count = int(op.get("count", 2))
            total = math.radians(float(op.get("angle", 360)))
            step = total / count if abs(total - 2 * math.pi) < 1e-9 else total / max(count - 1, 1)
            radius = math.dist(ref, c)
            if radius < 1e-9:
                raise SwError("The pattern centre must not be the first item's own centre.")
            ok = doc.CreateCircularSketchStepAndRepeat(mm(radius), math.atan2(c[1] - ref[1], c[0] - ref[0]), count, step, True, "")
        else:
            nx, ny = (int(v) for v in (op.get("count") or [2, 1]))
            dx, dy = (mm(v) for v in (op.get("spacing") or [0, 0]))
            ang = math.radians(float(op.get("angle", 0)))
            # spacings must be positive; a negative one turns its direction round
            ax, ay = ang + (math.pi if dx < 0 else 0.0), ang + math.pi / 2 + (math.pi if dy < 0 else 0.0)
            ok = doc.CreateLinearSketchStepAndRepeat(nx, ny, abs(dx), abs(dy), ax, ay, "")
        if not ok or len(_segments(sk)) == n:
            raise SwError("SolidWorks could not pattern the items.")
    elif kind == "offset":
        d = float(op.get("distance", 0))
        if not d:
            raise SwError("offset needs a non-zero distance (mm; the sign picks the side).")
        _select_items(doc, sk, items)
        if not sm.SketchOffset2(mm(d), bool(op.get("both_sides", False)), bool(op.get("chain", True)), 0, 0, False) or len(_segments(sk)) == n:
            raise SwError("SolidWorks could not offset the selection; a smaller distance may work.")
    elif kind == "convert":
        refs = op.get("refs") or items
        if not refs:
            raise SwError("convert needs refs: model edges or faces ('edge@x,y,z', 'face:N') to project into the sketch.")
        for r in refs:
            g.select(doc, r)
        if not sm.SketchUseEdge3(bool(op.get("chain", False)), bool(op.get("inner_loops", True))):
            raise SwError("SolidWorks could not convert the selected entities.")
    elif kind in ("fillet", "chamfer"):
        _select_items(doc, sk, items)
        if kind == "fillet":
            r = sm.CreateFillet(mm(float(op.get("radius", 0))), C.swConstrainedCornerKeepGeometry)
        else:
            r = sm.CreateChamfer(C.swSketchChamfer_DistanceEqual, mm(float(op.get("distance", 0))), 0)
        if r is None or len(_segments(sk)) == n:
            raise SwError(f"SolidWorks could not add the sketch {kind}. Give corner points ('pt@x,y') where two segments meet.")
    elif kind == "construction":
        for it in _select_items(doc, sk, items):
            if it is not None:
                SW.cast(it, "ISketchSegment").ConstructionGeometry = bool(op.get("on", True))
    elif kind == "relation":
        rel = (op.get("type") or "").lower()
        if rel not in RELATIONS:
            raise SwError(f"Unknown relation '{rel}'. Use one of {', '.join(RELATIONS)}.")
        _select_items(doc, sk, items)
        rm = SW.cast(sk.RelationManager, "ISketchRelationManager")
        before = rm.GetRelationsCount(C.swAll)
        was_over = sk.GetConstrainedStatus() in (4, 5, 6)
        doc.SketchAddConstraints(RELATIONS[rel])
        if rm.GetRelationsCount(C.swAll) == before:
            raise SwError(f"SolidWorks did not add the {rel} relation; check that the items suit it and that it does not exist already.")
        if not was_over and sk.GetConstrainedStatus() in (4, 5, 6):
            # a conflicting relation over-defines the sketch: take it back out
            added = SW.cast_all(rm.GetRelations(C.swAll), "ISketchRelation")[before:]
            for r in added:
                rm.DeleteRelation(r)
            doc.ClearSelection2(True)
            raise SwError(f"The {rel} relation conflicts with the sketch's existing relations or dimensions and was not added.")
    elif kind == "dimension":
        out.update(_dimension(doc, sk, op, items, to_model))
    else:
        raise SwError(f"Unknown sketch operation '{kind}'. Use delete, move, copy, rotate, block, set_point, spline_insert, spline_tangent, picture, trim, extend, split, pattern, offset, convert, fillet, chamfer, "
                      "construction, relation or dimension.")
    doc.ClearSelection2(True)
    change = len(_segments(sk)) - n
    if change > 0:
        out["segments_added"] = change
    elif change < 0:
        out["segments_removed"] = -change
    return out


@tool(WRITES)
def modify_sketch(
    operations: Annotated[list[dict[str, Any]], Field(description=(
        "Edits to the open sketch, applied in order. Items are 'seg:N' / 'pt:N' (indices from list_sketch, which change after "
        "every edit) or 'seg@x,y' / 'pt@x,y' (the segment or point nearest that sketch point, mm; in a 3D sketch 'seg@x,y,z', "
        "model mm), plus 'origin'. Operations: "
        "{op:'delete', items}, {op:'move', items, by:[dx, dy]}, {op:'copy', items, by, count}, "
        "{op:'rotate', items, center:[x, y], angle (deg, counterclockwise), copy}, "
        "{op:'trim', at:[x, y]} (removes the piece of the segment under the point, up to the nearest intersections), "
        "{op:'extend', at:[x, y]} (extends the segment from its end nearest the point to the next segment), "
        "{op:'split', at:[x, y]} (splits an open segment at the point), "
        "{op:'pattern', items, count:[nx, ny], spacing:[dx, dy], angle} (linear sketch pattern) or "
        "{op:'pattern', items, center:[x, y], count, angle (total, default 360)} (circular, first item a circle, arc or line), "
        "{op:'offset', items (sketch segments or model edges/faces), distance (mm; the sign picks the side), both_sides, chain}, "
        "{op:'convert', refs:['edge@x,y,z' | 'face:N', ...]} (project model edges into the sketch), "
        "{op:'fillet', items:['pt@x,y'], radius}, {op:'chamfer', items:['pt@x,y'], distance}, "
        "{op:'block', items, base:[x, y], copies:[[x, y], ...], angle, save_as:'name.sldblk'} (make a sketch block and place copies "
        "with their base point at each point, rotated by angle; optionally save it as a block file), "
        "{op:'spline_tangent', items:['seg@x,y'], at:[x, y] (a fit point), angle (deg from sketch x), weight}, "
        "{op:'picture', file, at:[x, y] (lower-left), width (mm)} (a reference image to trace, e.g. a legacy drawing), {op:'set_point', items:['pt@x,y'], to:[x, y]} (put a point at exact coordinates; a spline fit point reshapes the spline "
        "through it), {op:'spline_insert', items:['seg@x,y'], at:[x, y]} (add a fit point on a spline), "
        "{op:'construction', items, on}, "
        "{op:'relation', type, items} (types: " + ", ".join(RELATIONS) + "), "
        "{op:'dimension', items, value, orientation:'aligned'|'horizontal'|'vertical', at:[x, y]} "
        "(one line: length; circle: diameter; arc: radius; two items: distance, or the angle between two lines in degrees)."))],
) -> dict:
    """Edit the open sketch: delete, move, copy, rotate, trim, extend, split, pattern, offset, convert model edges, sketch fillets and
    chamfers, construction geometry, relations and driving dimensions (their names work with set_dimensions)."""
    doc = SW.active()
    sk = active_sketch(doc)
    done = []
    for i, op in enumerate(operations):
        try:
            done.append(_op(doc, sk, op))
        except SwError as err:
            doc.ClearSelection2(True)
            raise SwError(f"operations[{i}] ({op.get('op')}): {err}" + (f" Already applied: {done}" if done else "")) from None
    doc.GraphicsRedraw2()
    return {"sketch": sketch_name(doc, sk), "done": done, "status": sketch_status(sk), "segments": len(_segments(sk))}

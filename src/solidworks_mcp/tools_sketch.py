"""Sketch tools."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW, mm, to_mm
from .errors import SwError
from .registry import WRITES, tool

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
        segs += list(sm.CreateCenterRectangle(cx, cy, 0, cx + w / 2, cy + h / 2, 0) or [])
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


# ------------------------------------------------------------------------------------------ tools


@tool(WRITES)
def start_sketch(
    on: Annotated[str, Field(description="Sketch plane: 'plane:Front' (XY), 'plane:Top' (XZ), 'plane:Right' (YZ), another plane name, or a planar face ('face:3' or 'face@x,y,z').")],
) -> dict:
    """Open a new 2D sketch on a plane or planar face. Returns how sketch X/Y map into model space so
    coordinates can be converted; on the Top plane sketch +Y is model -Z."""
    doc = SW.active()
    if doc.SketchManager.ActiveSketch is not None:
        doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
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
        "{type:'text', at, text, height, angle, spacing} (part number marking, at = start of the baseline, spacing scales the character pitch, default 1.0; extrude-cut it about 0.3 mm). "
        "Any entity can carry mirror:'left_right' | 'up_down' | 'both' to add copies mirrored about the sketch axes, "
        "and construction:true to make it construction geometry."))],
    coords: Annotated[Literal["sketch", "model"], Field(description="Coordinate space of the points.")] = "sketch",
) -> dict:
    """Add geometry to the open sketch at exact coordinates (snapping and inference are bypassed)."""
    doc = SW.active()
    sk = active_sketch(doc)
    frame = SketchFrame(sk) if coords == "model" else None
    sm = doc.SketchManager
    sm.AddToDB = True
    sm.DisplayWhenAdded = False
    created = []
    try:
        for i, e in enumerate(entities):
            try:
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
    regions = sk.GetSketchRegionCount()
    doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    out = {"sketch": name, "status": status, "closed_regions": regions}
    if note:
        out["note"] = note
    if regions == 0:
        out["hint"] = "No closed region: extrude and revolve need a closed profile (sweep paths and guide curves do not)."
    return out

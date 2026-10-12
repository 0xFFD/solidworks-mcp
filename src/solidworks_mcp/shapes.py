"""Pure 2D geometry used by the sketch tool: rounded outlines, airfoils and mirrored copies.

Nothing here talks to SolidWorks, so it is unit tested directly. Lengths are in whatever unit the caller
uses consistently (the sketch tool passes metres).
"""

from __future__ import annotations

import math

from .errors import SwError

POINT_KEYS = ("from", "to", "corner1", "corner2", "center", "start", "end", "at", "origin")
LIST_KEYS = ("points",)

Line = tuple[str, float, float, float, float]
Arc = tuple[str, float, float, float, float, float, float, int]


def rounded_path(pts: list[tuple[float, float]], radii: list[float], closed: bool) -> list[tuple]:
    """Lines and tangent arcs for a polyline whose corners are rounded with the given radii.

    Returns ('line', x1, y1, x2, y2) and ('arc', cx, cy, sx, sy, ex, ey, direction) items, direction
    1 for counterclockwise from start to end. A radius of 0 leaves a sharp corner.
    """
    n = len(pts)
    corners: list[tuple[tuple[float, float], tuple[float, float], tuple | None]] = []  # (arrive, leave, arc)
    for i in range(n):
        p = pts[i]
        r = radii[i]
        if r <= 0 or (not closed and i in (0, n - 1)):
            corners.append((p, p, None))
            continue
        a, b = pts[i - 1], pts[(i + 1) % n]
        u = (a[0] - p[0], a[1] - p[1])
        v = (b[0] - p[0], b[1] - p[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        if lu < 1e-12 or lv < 1e-12:
            raise SwError("polyline has repeated points.")
        u, v = (u[0] / lu, u[1] / lu), (v[0] / lv, v[1] / lv)
        theta = math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1])))
        if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
            corners.append((p, p, None))  # collinear: nothing to round
            continue
        t = r / math.tan(theta / 2)
        # an end point of an open polyline has no arc, so the full segment is available
        limit_u = lu if (not closed and i - 1 == 0) else lu / 2
        limit_v = lv if (not closed and i + 1 == n - 1) else lv / 2
        if t > min(limit_u, limit_v) * (1 + 1e-9):
            raise SwError(f"Corner radius {r:g} at point {i} is too large for the adjacent segments.")
        t1 = (p[0] + u[0] * t, p[1] + u[1] * t)
        t2 = (p[0] + v[0] * t, p[1] + v[1] * t)
        bis = (u[0] + v[0], u[1] + v[1])
        lb = math.hypot(*bis)
        d = r / math.sin(theta / 2)
        c = (p[0] + bis[0] / lb * d, p[1] + bis[1] / lb * d)
        cross = (t1[0] - c[0]) * (t2[1] - c[1]) - (t1[1] - c[1]) * (t2[0] - c[0])
        corners.append((t1, t2, (c[0], c[1], t1[0], t1[1], t2[0], t2[1], 1 if cross > 0 else -1)))
    items: list[tuple] = []
    segments = n if closed else n - 1
    for i in range(n):
        if corners[i][2] is not None:
            items.append(("arc", *corners[i][2]))
        if i < segments:
            a = corners[i][1]
            b = corners[(i + 1) % n][0]
            if math.dist(a, b) > 1e-12:
                items.append(("line", a[0], a[1], b[0], b[1]))
    return items


def naca4(code: str, n: int = 40, closed_te: bool = False) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Upper and lower surfaces (unit chord, leading edge first, cosine spacing) of a NACA 4-digit airfoil."""
    code = str(code).strip()
    if not (len(code) == 4 and code.isdigit()):
        raise SwError(f"NACA code '{code}' must be 4 digits, e.g. '0012' or '2412'.")
    m, p, t = int(code[0]) / 100, int(code[1]) / 10, int(code[2:]) / 100
    if t <= 0:
        raise SwError("NACA thickness (last two digits) must be above 0.")
    if m > 0 and not 0 < p < 1:
        raise SwError("A cambered NACA section needs a camber position digit between 1 and 9.")
    a4 = -0.1036 if closed_te else -0.1015
    up, lo = [], []
    for i in range(n + 1):
        x = (1 - math.cos(math.pi * i / n)) / 2
        yt = 5 * t * (0.2969 * math.sqrt(x) - 0.1260 * x - 0.3516 * x**2 + 0.2843 * x**3 + a4 * x**4)
        if m > 0:
            if x < p:
                yc, dyc = m / p**2 * (2 * p * x - x**2), 2 * m / p**2 * (p - x)
            else:
                yc, dyc = m / (1 - p) ** 2 * ((1 - 2 * p) + 2 * p * x - x**2), 2 * m / (1 - p) ** 2 * (p - x)
        else:
            yc, dyc = 0.0, 0.0
        th = math.atan(dyc)
        up.append((x - yt * math.sin(th), yc + yt * math.cos(th)))
        lo.append((x + yt * math.sin(th), yc - yt * math.cos(th)))
    return up, lo


def airfoil_loop(code: str, chord: float, origin: tuple[float, float], angle_deg: float, n: int = 40) -> list[tuple[float, float]]:
    """Airfoil outline from the upper trailing edge round the nose to the lower trailing edge,
    scaled to `chord`, rotated counterclockwise by `angle_deg` about the leading edge at `origin`."""
    up, lo = naca4(code, n)
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    ox, oy = origin

    def tf(q: tuple[float, float]) -> tuple[float, float]:
        return ox + chord * (q[0] * ca - q[1] * sa), oy + chord * (q[0] * sa + q[1] * ca)

    return [tf(q) for q in reversed(up)] + [tf(q) for q in lo[1:]]


def mirrored(e: dict, sx: float, sy: float) -> dict:
    """Copy of an entity reflected by (x, y) -> (sx*x, sy*y)."""
    out = dict(e)
    out.pop("mirror", None)

    def f(p):
        return [p[0] * sx, p[1] * sy]

    for k in POINT_KEYS:
        if out.get(k) is not None:
            out[k] = f(out[k])
    for k in LIST_KEYS:
        if out.get(k) is not None:
            out[k] = [f(p) for p in out[k]]
    t = out.get("type")
    if sx * sy < 0:
        if t == "arc":
            out["direction"] = "cw" if (e.get("direction") or "ccw").lower() == "ccw" else "ccw"
        if t in ("airfoil", "ellipse", "polygon", "bolt_circle"):
            key = "start_angle" if t == "bolt_circle" else "angle"
            default = 90 if t == "polygon" else 0
            a = out.get(key, default)
            out[key] = (180 - a) if sx < 0 else -a
    elif sx < 0 and sy < 0 and t in ("airfoil", "ellipse", "polygon", "bolt_circle"):
        key = "start_angle" if t == "bolt_circle" else "angle"
        out[key] = out.get(key, 90 if t == "polygon" else 0) + 180
    return out


def expand_mirror(e: dict) -> list[dict]:
    """The entity plus mirrored copies requested with mirror='left_right' | 'up_down' | 'both'."""
    m = (e.get("mirror") or "").lower()
    if not m:
        return [e]
    base = dict(e)
    base.pop("mirror")
    copies = {"left_right": [(-1, 1)], "up_down": [(1, -1)], "both": [(-1, 1), (1, -1), (-1, -1)]}.get(m)
    if copies is None:
        raise SwError("mirror must be 'left_right' (about the sketch Y axis), 'up_down' (about the X axis) or 'both'.")
    return [base] + [mirrored(base, sx, sy) for sx, sy in copies]


# ---------------------------------------------------------------------------------------------- picking


def dist_to_line(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from p to the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ll))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def dist_to_arc(p: tuple[float, float], c: tuple[float, float], r: float, start: tuple[float, float] | None = None,
                end: tuple[float, float] | None = None, ccw: bool = True) -> float:
    """Distance from p to a circle, or to the arc from start to end (counterclockwise unless ccw is False)."""
    if start is None or end is None or math.dist(start, end) < 1e-12:
        return abs(math.dist(p, c) - r)
    a0 = math.atan2(start[1] - c[1], start[0] - c[0])
    a1 = math.atan2(end[1] - c[1], end[0] - c[0])
    ap = math.atan2(p[1] - c[1], p[0] - c[0])
    if not ccw:
        a0, a1 = a1, a0
    sweep = (a1 - a0) % (2 * math.pi)
    if (ap - a0) % (2 * math.pi) <= sweep:
        return abs(math.dist(p, c) - r)
    return min(math.dist(p, start), math.dist(p, end))


def dist_to_polyline(p: tuple[float, float], pts: list[tuple[float, float]]) -> float:
    if len(pts) == 1:
        return math.dist(p, pts[0])
    return min(dist_to_line(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# ---------------------------------------------------------------------------------------------- frames


def chains(ends: list[tuple[tuple, tuple]]) -> list[list[int]]:
    """Split segments (pairs of end points) into simple chains: runs that only pass through points where
    exactly two segments meet. Weldment member groups must be chains like this."""
    touching: dict[tuple, list[int]] = {}
    for i, (a, b) in enumerate(ends):
        touching.setdefault(a, []).append(i)
        touching.setdefault(b, []).append(i)
    used: set[int] = set()
    out: list[list[int]] = []

    def walk(start: int, node: tuple) -> list[int]:
        run, seg, here = [start], start, node
        used.add(start)
        while True:
            a, b = ends[seg]
            nxt = b if a == here else a
            others = [s for s in touching[nxt] if s != seg]
            if len(touching[nxt]) != 2 or others[0] in used:
                return run
            seg, here = others[0], nxt
            used.add(seg)
            run.append(seg)

    for p, segs in touching.items():  # start at every end or branch point
        if len(segs) != 2:
            for s in segs:
                if s not in used:
                    out.append(walk(s, p))
    for i, (a, _) in enumerate(ends):  # what is left are closed loops
        if i not in used:
            out.append(walk(i, a))
    return out


def rounded_path_3d(pts: list[tuple[float, float, float]], radii: list[float], closed: bool = False) -> list[tuple]:
    """Lines and arcs of a 3D polyline whose corners are bent to the given radii (tube and harness runs).

    Returns ('line', a, b) and ('arc', start, mid, end) items with 3D points. A radius of 0 leaves a sharp corner.
    """
    def sub(a, b):
        return tuple(x - y for x, y in zip(a, b))

    def add(a, b):
        return tuple(x + y for x, y in zip(a, b))

    def mul(a, k):
        return tuple(x * k for x in a)

    def unit(a):
        n = math.sqrt(sum(x * x for x in a))
        if n < 1e-12:
            raise SwError("polyline has repeated points.")
        return mul(a, 1 / n)

    n = len(pts)
    corners: list[tuple] = []
    for i in range(n):
        p, r = pts[i], radii[i]
        if r <= 0 or (not closed and i in (0, n - 1)):
            corners.append((p, p, None))
            continue
        u, v = unit(sub(pts[i - 1], p)), unit(sub(pts[(i + 1) % n], p))
        theta = math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v)))))
        if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
            corners.append((p, p, None))
            continue
        t = r / math.tan(theta / 2)
        lu, lv = math.dist(pts[i - 1], p), math.dist(pts[(i + 1) % n], p)
        limit_u = lu if (not closed and i - 1 == 0) else lu / 2
        limit_v = lv if (not closed and i + 1 == n - 1) else lv / 2
        if t > min(limit_u, limit_v) * (1 + 1e-9):
            raise SwError(f"Bend radius {r:g} at point {i} is too large for the adjacent segments.")
        b = unit(add(u, v))
        centre = add(p, mul(b, r / math.sin(theta / 2)))
        mid = sub(centre, mul(b, r))
        corners.append((add(p, mul(u, t)), add(p, mul(v, t)), mid))
    items: list[tuple] = []
    segments = n if closed else n - 1
    for i in range(n):
        if corners[i][2] is not None:
            items.append(("arc", corners[i][0], corners[i][2], corners[i][1]))
        if i < segments:
            a, b = corners[i][1], corners[(i + 1) % n][0]
            if math.dist(a, b) > 1e-12:
                items.append(("line", a, b))
    return items


def arrange_dimensions(dims: list[tuple], outline: tuple[float, float, float, float],
                       spacing: float, gap: float = 0.0, clearance: float = 0.0,
                       room: dict[str, float] | None = None) -> dict[str, tuple[float, float]]:
    """New text positions for linear dimensions around a drawing view, packed in rows by side.

    `dims` are (name, length on the sheet, (x, y) text position, vertical[, depth]) and `outline` is
    (x0, y0, x1, y1); depth is how far the text reaches across the row (its width when horizontal text labels a
    vertical dimension), and a side's rows are at least the largest depth plus `clearance` apart. `room` caps
    how far each side may reach (the gap to a neighbouring view or the sheet edge): rows are squeezed to fit,
    but never closer than `spacing`.
    Horizontal dimensions go above or below the view (the side their text is on), vertical ones left or right.
    Each side is filled in rows `spacing` apart, shortest first, each dimension in the nearest row where it
    does not overlap another along the row (its span is its length, centred on the text). Dimensions whose text
    sits inside the outline across the row direction are left where they are. The text keeps its position
    along the dimension line.
    """
    x0, y0, x1, y1 = outline
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sides: dict[str, list[tuple[float, str, tuple[float, float]]]] = {"top": [], "bottom": [], "left": [], "right": []}
    depth: dict[str, float] = {}
    for item in dims:
        name, length, (x, y), vertical = item[:4]
        depth[name] = item[4] if len(item) > 4 else 0.0
        if vertical:
            if x0 <= x <= x1:
                continue
            sides["right" if x > cx else "left"].append((length, name, (x, y)))
        else:
            if y0 <= y <= y1:
                continue
            sides["top" if y > cy else "bottom"].append((length, name, (x, y)))
    out: dict[str, tuple[float, float]] = {}
    for side, items in sides.items():
        rows: list[list[tuple[float, float]]] = []
        placed: list[tuple[str, int, float, float]] = []
        for length, name, (x, y) in sorted(items):
            c = x if side in ("top", "bottom") else y
            span = (c - length / 2 - gap, c + length / 2 + gap)
            k = next((i for i, row in enumerate(rows) if all(span[1] <= a or span[0] >= b for a, b in row)), len(rows))
            if k == len(rows):
                rows.append([])
            rows[k].append(span)
            placed.append((name, k, x, y))
        pitch = max([spacing] + [depth[n] + clearance for _, n, _ in items])
        if room and side in room and rows:
            # the outermost row's text must stay inside the room
            pitch = max(spacing, min(pitch, (room[side] - max(depth.get(n, 0.0) for _, n, _ in items)) / len(rows)))
        for name, k, x, y in placed:
            d = pitch * (k + 1)
            out[name] = {"top": (x, y1 + d), "bottom": (x, y0 - d), "right": (x1 + d, y), "left": (x0 - d, y)}[side]
    return out

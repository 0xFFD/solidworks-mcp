"""Pure geometry behind the sketch tool, the design guide and the bundled materials (no SolidWorks needed)."""

import math
import xml.etree.ElementTree as ET

import pytest

from solidworks_mcp import shapes
from solidworks_mcp.errors import SwError


def _closed(items) -> bool:
    """Every item starts where the previous one ended (lines and arcs alike)."""
    ends = []
    for it in items:
        if it[0] == "line":
            ends.append(((it[1], it[2]), (it[3], it[4])))
        else:
            ends.append(((it[3], it[4]), (it[5], it[6])))
    return all(math.dist(ends[i][1], ends[(i + 1) % len(ends)][0]) < 1e-9 for i in range(len(ends)))


def test_rounded_square_is_tangent_and_closed():
    pts = [(0, 0), (10, 0), (10, 10), (0, 10)]
    items = shapes.rounded_path(pts, [2] * 4, closed=True)
    assert [i[0] for i in items].count("arc") == 4 and [i[0] for i in items].count("line") == 4
    assert _closed(items)
    arc = next(i for i in items if i[0] == "arc")
    cx, cy, sx, sy, ex, ey, d = arc[1:]
    assert math.dist((cx, cy), (sx, sy)) == pytest.approx(2) and math.dist((cx, cy), (ex, ey)) == pytest.approx(2)
    assert d == 1  # counterclockwise outline gives counterclockwise fillets
    lines = [i for i in items if i[0] == "line"]
    assert all(math.dist(i[1:3], i[3:5]) == pytest.approx(6) for i in lines)


def test_rounded_path_mixed_radii_and_sharp_corner():
    items = shapes.rounded_path([(0, 0), (20, 0), (20, 10), (0, 10)], [0, 3, 3, 0], closed=True)
    assert [i[0] for i in items].count("arc") == 2
    assert _closed(items)


def test_rounded_open_polyline_keeps_ends():
    items = shapes.rounded_path([(0, 0), (10, 0), (10, 10)], [0, 4, 0], closed=False)
    assert items[0][0] == "line" and items[0][1:3] == (0, 0)
    assert items[-1][0] == "line" and items[-1][3:5] == (10, 10)


def test_rounded_path_rejects_oversized_radius():
    with pytest.raises(SwError, match="too large"):
        shapes.rounded_path([(0, 0), (4, 0), (4, 4), (0, 4)], [3] * 4, closed=True)


@pytest.mark.parametrize("code,thickness", [("0012", 0.12), ("2412", 0.12), ("4415", 0.15)])
def test_naca4_thickness_and_ends(code, thickness):
    up, lo = shapes.naca4(code, 80)
    assert up[0] == pytest.approx((0, 0)) and lo[0] == pytest.approx((0, 0))
    assert up[-1][0] == pytest.approx(1, abs=1e-3)
    t = max(u[1] - v[1] for u, v in zip(up, lo))
    assert t == pytest.approx(thickness, rel=0.03)


def test_naca_symmetric_and_cambered():
    up, lo = shapes.naca4("0012")
    assert all(u[1] == pytest.approx(-v[1]) for u, v in zip(up, lo))
    up, lo = shapes.naca4("4412")
    assert max(u[1] for u in up) > -min(v[1] for v in lo)  # camber lifts the section


@pytest.mark.parametrize("bad", ["12", "abcd", "0000", "4012"])
def test_naca_rejects_bad_codes(bad):
    with pytest.raises(SwError):
        shapes.naca4(bad)


def test_airfoil_loop_scale_rotation():
    loop = shapes.airfoil_loop("0012", chord=100, origin=(10, 5), angle_deg=90)
    xs = [p[0] for p in loop]
    ys = [p[1] for p in loop]
    assert min(ys) == pytest.approx(5, abs=0.5) and max(ys) == pytest.approx(105, abs=0.5)  # chord now along +Y
    assert max(xs) - min(xs) == pytest.approx(12, rel=0.05)


def test_mirror_expansion():
    e = {"type": "circle", "center": [90, 70], "diameter": 30, "mirror": "both"}
    out = shapes.expand_mirror(e)
    assert sorted(tuple(o["center"]) for o in out) == [(-90, -70), (-90, 70), (90, -70), (90, 70)]
    assert all("mirror" not in o for o in out)
    arc = shapes.mirrored({"type": "arc", "center": [0, 0], "start": [1, 0], "end": [0, 1], "direction": "ccw"}, -1, 1)
    assert arc["direction"] == "cw" and arc["start"] == [-1, 0]
    bc = shapes.mirrored({"type": "bolt_circle", "center": [5, 5], "start_angle": 45}, -1, 1)
    assert bc["start_angle"] == 135
    with pytest.raises(SwError):
        shapes.expand_mirror({"type": "circle", "center": [0, 0], "mirror": "sideways"})


def test_design_guide_topics():
    from solidworks_mcp import tools_guide

    for t in tools_guide.TOPICS:
        text = tools_guide.design_guide(t)
        assert len(text) > 500 and chr(0x2014) not in text and chr(0x2013) not in text
    assert "43.13" in tools_guide.design_guide("multirotor")  # 30.5 mm stack as a bolt circle
    assert "ISO 273" in tools_guide.design_guide("fasteners")


def test_bundled_material_database():
    from solidworks_mcp.tools_doc import BUNDLED_DB

    root = ET.fromstring(BUNDLED_DB.read_bytes())
    names = {m.get("name"): m for m in root.iter("material")}
    cfrp = names["CFRP plate quasi-isotropic (generic)"]
    assert float(cfrp.find("physicalproperties/DENS").get("value")) == pytest.approx(1550)
    assert all(n.endswith("(generic)") for n in names)
    assert len(names) >= 8


def test_distances_for_picking_sketch_segments():
    assert shapes.dist_to_line((5, 3), (0, 0), (10, 0)) == pytest.approx(3)
    assert shapes.dist_to_line((-4, 3), (0, 0), (10, 0)) == pytest.approx(5)  # beyond the end point
    assert shapes.dist_to_arc((0, 6), (0, 0), 5) == pytest.approx(1)  # full circle
    # quarter arc from (5,0) counterclockwise to (0,5): a point at 225 degrees is nearest an end point
    p = (-5 / math.sqrt(2), -5 / math.sqrt(2))
    assert shapes.dist_to_arc(p, (0, 0), 5, (5, 0), (0, 5)) == pytest.approx(math.dist(p, (5, 0)))
    assert shapes.dist_to_arc((0, 4), (0, 0), 5, (5, 0), (0, 5)) == pytest.approx(1)
    # the same end points clockwise cover the other three quarters
    assert shapes.dist_to_arc(p, (0, 0), 5, (5, 0), (0, 5), ccw=False) == pytest.approx(0)
    assert shapes.dist_to_polyline((1, 1), [(0, 0), (2, 0), (2, 2)]) == pytest.approx(1)


def test_frame_chains():
    sq = [((0, 0), (1, 0)), ((1, 0), (1, 1)), ((1, 1), (0, 1)), ((0, 1), (0, 0))]
    assert [sorted(c) for c in shapes.chains(sq)] == [[0, 1, 2, 3]]  # a closed loop is one chain
    branched = sq + [((0, 0), (0, -1))]
    parts = sorted(sorted(c) for c in shapes.chains(branched))
    assert sum(len(c) for c in parts) == 5 and [4] in parts  # the branch is its own chain
    assert all(len(set(c)) == len(c) for c in parts)
    open_l = [((0, 0), (1, 0)), ((1, 0), (1, 1))]
    assert [sorted(c) for c in shapes.chains(open_l)] == [[0, 1]]


def test_rounded_path_3d_bends():
    pts = [(0, 0, 0), (100, 0, 0), (100, 0, 80), (100, 60, 80)]
    items = shapes.rounded_path_3d(pts, [0, 10, 10, 0])
    arcs = [it for it in items if it[0] == "arc"]
    assert len(arcs) == 2 and [it[0] for it in items] == ["line", "arc", "line", "arc", "line"]
    s, m, e = arcs[0][1:]
    assert s == pytest.approx((90, 0, 0)) and e == pytest.approx((100, 0, 10))  # tangent points r back from the corner
    centre = (90, 0, 10)
    assert all(math.dist(q, centre) == pytest.approx(10) for q in (s, m, e))
    # consecutive items join up
    ends = [(it[1], it[2]) if it[0] == "line" else (it[1], it[3]) for it in items]
    assert all(math.dist(ends[i][1], ends[i + 1][0]) < 1e-9 for i in range(len(ends) - 1))
    with pytest.raises(SwError):
        shapes.rounded_path_3d([(0, 0, 0), (5, 0, 0), (5, 5, 0)], [0, 10, 0])


def test_arrange_dimensions_stacks_by_size():
    outline = (0, 0, 100, 50)
    # spans: short 10..30, mid 10..50 (overlaps short), side 70..90 (clear of both), long 0..100
    dims = [("long", 100, (50, 70), False), ("short", 20, (20, 52), False), ("mid", 40, (30, 90), False), ("side", 20, (80, 60), False),
            ("right", 50, (130, 25), True), ("thick", 10, (104, -8), True), ("inside", 8, (40, 20), False)]
    pos = shapes.arrange_dimensions(dims, outline, 8)
    assert pos["short"] == (20, 58) and pos["side"] == (80, 58)  # share the first row
    assert pos["mid"] == (30, 66) and pos["long"] == (50, 74)
    assert pos["thick"] == (108, -8) and pos["right"] == (108, 25)  # vertical: right of the view; no overlap in y, one row
    assert "inside" not in pos


def test_arrange_dimensions_wide_text_widens_columns():
    # horizontal text 12 mm wide on vertical dimensions: columns 14 mm apart instead of 8
    dims = [("a", 30, (110, 25), True, 12.0), ("b", 40, (115, 25), True, 12.0)]
    pos = shapes.arrange_dimensions(dims, (0, 0, 100, 50), 8, clearance=2)
    assert pos["a"] == (114, 25) and pos["b"] == (128, 25)


def test_arrange_dimensions_squeezes_into_room():
    dims = [("a", 30, (110, 25), True, 12.0), ("b", 40, (115, 25), True, 12.0)]
    pos = shapes.arrange_dimensions(dims, (0, 0, 100, 50), 8, clearance=2, room={"right": 30})
    assert pos["a"] == (109, 25) and pos["b"] == (118, 25)  # (30 - 12) / 2 = 9 mm columns
    tight = shapes.arrange_dimensions(dims, (0, 0, 100, 50), 8, clearance=2, room={"right": 10})
    assert tight["b"] == (116, 25)  # never closer than the spacing


def test_tolerance_stack_worst_case_and_rss():
    # gap = housing 50 +0.1/-0 minus two parts 20 +-0.05 and 29.8 +0/-0.1
    r = shapes.tolerance_stack([(50, 0.1, 0.0, 1), (20, 0.05, 0.05, -1), (29.8, 0.0, 0.1, -1)])
    assert r["nominal"] == pytest.approx(0.2)
    assert r["worst_case"] == pytest.approx((0.15, 0.45))  # tightest: 50 - 20.05 - 29.8; loosest: 50.1 - 19.95 - 29.7
    half = math.sqrt(0.05 ** 2 + 0.05 ** 2 + 0.05 ** 2)
    assert r["rss"] == pytest.approx((0.3 - half, 0.3 + half))

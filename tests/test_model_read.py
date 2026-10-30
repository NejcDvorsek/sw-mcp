"""Unit tests for read_model's geometry: no SolidWorks needed.

The COM side of read_model reads each body into plain dicts (faces with their
kind, axis, sample points and outward normal at a probe point; edges with
their two faces, midpoint, loop tangent and both normals). These tests build
such dicts by hand: a counterbore, a blind hole, a boss, a pocket.
"""

import math

import pytest

from sw_mcp.errors import SolidWorksError
from sw_mcp.model_read import (
    arc_span_deg,
    axis_line,
    cad_version,
    classify_stack,
    draft_angle_deg,
    edge_convexity,
    face_draft,
    find_hole_stacks,
    group_coaxial,
    group_edges,
    group_holes,
    internal_radii,
    material_side,
    merge_normals,
    merge_wall_sides,
    parse_sections,
    same_axis_line,
    setup_directions,
    sharp_internal_edges,
    snap_axis,
    snap_direction,
    thread_designation,
)

Z = (0.0, 0.0, 1.0)


# --- synthetic bodies -----------------------------------------------------------

def ring(cx, cy, radius, z_low, z_high, arc_deg=360.0, steps=24, rho=None):
    """Points on a round face about a vertical axis at (cx, cy), z_low..z_high;
    rho(z) gives a cone's radius."""
    points = []
    for z in (z_low, z_high):
        r = rho(z) if rho else radius
        for k in range(steps + (arc_deg < 360)):
            a = math.radians(arc_deg * k / steps)
            points.append([cx + r * math.cos(a), cy + r * math.sin(a), z])
    return points


def plane(normal, feature="Base", order=0):
    return {"kind": "plane", "normal": list(normal), "area": 100.0, "feature": feature, "feature_order": order}


def round_face(kind, cx, cy, radius, z_low, z_high, side, feature="Hole", order=1, arc_deg=360.0, rho=None):
    """A vertical cylinder (or cone, with rho) whose material is outside it (side 'hole') or inside ('boss')."""
    points = ring(cx, cy, radius, z_low, z_high, arc_deg, rho=rho)
    probe = max(points, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))  # off a cone's apex
    radial = [probe[0] - cx, probe[1] - cy, 0.0]
    length = math.hypot(*radial)
    normal = [(-1 if side == "hole" else 1) * c / length for c in radial]
    return {"kind": kind, "axis_point": [cx, cy, 0.0], "axis": list(Z), "radius": radius, "points": points,
            "probe": (probe, normal), "area": 50.0, "feature": feature, "feature_order": order}


def edge(i, j, mid=(0.0, 0.0, 0.0), tangent=(1.0, 0.0, 0.0), normals=((0, 0, 1), (0, 0, 1)), line=False, length=1.0):
    return {"faces": (i, j), "mid": list(mid), "tangent": list(tangent), "normals": normals, "line": line,
            "length": length}


def with_sides(faces, edges):
    """The plate's top (face 0) and bottom (face 1) also meet its sides, as a real plate's do."""
    side = len(faces)
    return faces + [plane((1, 0, 0))], edges + [edge(0, side), edge(1, side)]


def counterbore_body():
    """A 10 thick plate: a Ø9 bore through, a Ø15 counterbore 8.6 deep from +z, at (20, 10)."""
    faces = [
        plane((0, 0, 1)),                                  # 0 top, z 10
        plane((0, 0, -1)),                                 # 1 bottom, z 0
        round_face("cylinder", 20, 10, 7.5, 1.4, 10, "hole"),  # 2 counterbore
        plane((0, 0, 1), "Hole", 1),                       # 3 step, z 1.4
        round_face("cylinder", 20, 10, 4.5, 0, 1.4, "hole"),   # 4 bore
    ]
    edges = [edge(0, 2, (27.5, 10, 10)), edge(2, 3, (27.5, 10, 1.4)), edge(3, 4, (24.5, 10, 1.4)),
             edge(4, 1, (24.5, 10, 0))]
    return with_sides(faces, edges)


def blind_body(drill_point=False):
    """A Ø6 hole 8 deep from the top (z 10) of a plate, ending flat or in a 118 degree drill point."""
    faces = [plane((0, 0, 1)), plane((0, 0, -1)), round_face("cylinder", 60, 30, 3, 2, 10, "hole")]
    edges = [edge(0, 2, (63, 30, 10))]  # face 3: the floor or the drill point
    if drill_point:
        tip = 3 / math.tan(math.radians(59))
        faces.append(round_face("cone", 60, 30, 3, 2 - tip, 2, "hole", rho=lambda z: (z - (2 - tip)) * math.tan(math.radians(59))))
    else:
        faces.append(plane((0, 0, 1), "Hole", 1))
    edges.append(edge(2, 3, (63, 30, 2)))
    return with_sides(faces, edges)


# --- vectors and directions -------------------------------------------------------

def test_directions_snap_to_axes_within_half_a_degree():
    assert snap_direction([0, 0, 1]) == "+z" and snap_direction([-1e-4, 0, -1]) == "-z"
    tilted = [0, math.sin(math.radians(1)), math.cos(math.radians(1))]
    assert snap_direction(tilted) == [0.0, 0.0175, 0.9998]
    assert snap_axis([0, 0, -3]) == "z" and snap_axis([1, 1, 0]) is None


def test_draft_is_ninety_less_the_angle_to_the_pull():
    pull = (0, 0, 1)
    assert draft_angle_deg([1, 0, 0], pull) == pytest.approx(0)
    leaning = [math.cos(math.radians(2)), 0, math.sin(math.radians(2))]
    assert draft_angle_deg(leaning, pull) == pytest.approx(2)
    assert draft_angle_deg([leaning[0], 0, -leaning[2]], pull) == pytest.approx(-2), "leaning back is negative"
    assert draft_angle_deg([0, 0, 1], pull) is None and draft_angle_deg([0, 0, -1], pull) is None, "tops and floors"
    assert face_draft([[1, 0, 0], leaning, [0, 0, 1]], pull) == pytest.approx(0), "a curved face takes its worst"


def test_cad_version_and_thread_designation():
    assert cad_version("34.4.1") == "SOLIDWORKS 2026 SP4.1"
    assert cad_version("33.1.0") == "SOLIDWORKS 2025 SP1"
    assert thread_designation("M6", "ISO") == "M6x1.0" and thread_designation("M8", "ISO") == "M8x1.25"
    assert thread_designation("M6x0.75", "ISO") == "M6x0.75" and thread_designation("1/4-20", "ANSI Inch") == "1/4-20"


# --- arguments ----------------------------------------------------------------------

def test_sections_default_to_every_part_section_that_needs_no_argument():
    assert parse_sections(None, None, None) == ["core", "material", "holes", "radii", "setups"]
    assert parse_sections(None, 1.0, "+z")[-2:] == ["walls", "draft"]
    assert parse_sections(["holes", "core"], None, None) == ["core", "holes"]
    assert parse_sections("walls", 0.8, None) == ["core", "walls"]


@pytest.mark.parametrize("args, says", [
    ((["holez"], None, None), "Unknown section holez"),
    ((["walls"], None, None), "needs wall_mm"),
    ((["draft"], None, None), "needs pull"),
    ((["holes"], 1.0, None), "wall_mm goes with the walls section"),
    ((["holes"], None, "+z"), "pull goes with the draft section"),
    ((["draft"], None, "up"), "Unknown pull"),
    ((["walls"], -1.0, None), "must be > 0"),
    ((["assembly"], None, None), "not available yet"),
])
def test_bad_arguments_are_refused(args, says):
    with pytest.raises(SolidWorksError, match=says):
        parse_sections(*args)


def test_clearance_pairs_need_the_assembly_section():
    with pytest.raises(SolidWorksError, match="clearance_pairs"):
        parse_sections(None, None, None, [["a-1", "b-1"]])


# --- axes and sides ---------------------------------------------------------------------

def test_coaxial_cylinders_group_within_tolerance_in_either_sense():
    lines = [axis_line([20, 10, 0], [0, 0, 1]),
             axis_line([20, 10, 5], [0, 0, -1]),            # the same line, the other way
             axis_line([20 + 5e-4, 10, 0], [0, 0, 1]),      # half a micrometre off: the same
             axis_line([20.01, 10, 0], [0, 0, 1]),          # 10 micrometres off: another hole
             axis_line([20, 10, 0], [1e-3, 0, 1])]          # a milliradian off: another
    assert group_coaxial(lines) == [[0, 1, 2], [3], [4]]
    assert same_axis_line(lines[0], lines[1]) and not same_axis_line(lines[0], lines[3])


def test_a_hole_has_its_material_outside_a_boss_inside():
    radial = [3, 0, 0]  # from the axis to the point
    assert material_side([-1, 0, 0], radial) == "hole"
    assert material_side([1, 0, 0], radial) == "boss"
    assert material_side([0, 0, 1], radial) is None, "a normal along the axis says nothing"


def test_convex_and_concave_edges_from_normals_and_loop_tangent():
    # a loop runs anticlockwise seen from outside the face
    # a box's top-front edge: top (+z) then front (-y), the top's loop runs +x there
    assert edge_convexity([0, 0, 1], [0, -1, 0], [1, 0, 0]) == "convex"
    # a pocket's floor (+z) and its back wall (facing -y): the floor's loop runs -x along it
    assert edge_convexity([0, 0, 1], [0, -1, 0], [-1, 0, 0]) == "concave"
    # a fillet running out tangentially
    assert edge_convexity([0, 0, 1], [0, math.sin(math.radians(0.5)), math.cos(math.radians(0.5))], [1, 0, 0]) == "tangent"
    assert edge_convexity([0, 0, 0], [0, 0, 1], [1, 0, 0]) == "unknown"


def test_arc_span():
    full = [math.radians(a) for a in range(0, 360, 15)]
    assert arc_span_deg(full) == pytest.approx(345)
    assert arc_span_deg([math.radians(a) for a in range(0, 91, 15)]) == pytest.approx(90)


# --- holes -------------------------------------------------------------------------------

def test_a_counterbore_stacks_into_one_hole():
    faces, edges = counterbore_body()
    [stack] = find_hole_stacks(faces, edges)
    assert stack == [2, 3, 4], "both cylinders and the step between them"
    hole = classify_stack(faces, edges, stack)
    assert hole["kind"] == "counterbore" and hole["through"] and hole["confidence"] == "high"
    assert hole["diameter_mm"] == 9 and hole["depth_mm"] == 10
    assert hole["counterbore"] == {"diameter_mm": 15.0, "depth_mm": 8.6}
    assert hole["axis"] == [0, 0, -1] and hole["entrance"] == "+z", "drilled from the wide end"
    assert hole["centre_mm"] == [20, 10, 10]


@pytest.mark.parametrize("drill_point", [False, True])
def test_a_blind_hole_ends_in_a_floor(drill_point):
    faces, edges = blind_body(drill_point)
    [stack] = find_hole_stacks(faces, edges)
    hole = classify_stack(faces, edges, stack)
    assert not hole["through"] and hole["drill_point"] == drill_point
    assert hole["depth_mm"] == pytest.approx(8) and hole["diameter_mm"] == 6
    assert hole["entrance"] == "+z" and hole["centre_mm"] == [60, 30, 10] and hole["confidence"] == "high"


def test_a_boss_is_no_hole():
    faces = [plane((0, 0, 1)), round_face("cylinder", 60, 30, 10, 10, 20, "boss", "Boss"), plane((0, 0, 1), "Boss", 1)]
    edges = [edge(0, 1, (70, 30, 10)), edge(1, 2, (70, 30, 20))]
    assert find_hole_stacks(faces, edges) == []


def test_a_slot_end_is_no_hole_but_an_internal_radius():
    faces = [plane((0, 0, 1)), round_face("cylinder", 50, 72, 3, 0, 8, "hole", "Slot", 2, arc_deg=180)]
    edges = [edge(0, 1, (50, 75, 8))]
    assert find_hole_stacks(faces, edges) == []
    assert internal_radii(faces, set()) == [{"radius": 3.0, "feature": "Slot", "count": 1}]


def test_a_cylinder_in_two_halves_is_still_a_hole():
    halves = [round_face("cylinder", 0, 0, 2, 0, 5, "hole", arc_deg=180),
              round_face("cylinder", 0, 0, 2, 0, 5, "hole", arc_deg=180)]
    halves[1]["points"] = [[-x, -y, z] for x, y, z in halves[1]["points"]]
    faces, edges = with_sides([plane((0, 0, 1)), plane((0, 0, -1)), *halves],
                              [edge(0, 2, (2, 0, 5)), edge(0, 3, (-2, 0, 5)), edge(2, 3, (0, 2, 2.5), line=True),
                               edge(1, 2, (2, 0, 0)), edge(1, 3, (-2, 0, 0))])
    [stack] = find_hole_stacks(faces, edges)
    assert stack == [2, 3] and classify_stack(faces, edges, stack)["diameter_mm"] == 4


def test_identical_holes_group_with_count_and_centres_in_tree_order():
    base = {"source": "wizard", "confidence": "high", "kind": "counterbore", "diameter_mm": 9.0, "depth_mm": 10.0,
            "through": True, "axis": [0, 0, -1], "entrance": "+z", "body": "Base"}
    holes = [{**base, "feature": "CBORE2", "order": 6, "centre_mm": [105, 10, 10]},
             {**base, "feature": "CBORE1", "order": 5, "centre_mm": [20, 50, 10]},
             {**base, "feature": "CBORE1", "order": 5, "centre_mm": [20, 10, 10]},
             {**base, "feature": "Tap", "order": 2, "kind": "tapped", "diameter_mm": 5.0, "centre_mm": [60, 30, 10]}]
    grouped = group_holes(holes)
    assert [g["feature"] for g in grouped] == ["Tap", "CBORE1"], "tree order"
    cbore = grouped[1]
    assert cbore["count"] == 3 and cbore["features"] == ["CBORE1", "CBORE2"]
    assert cbore["centres_mm"] == [[20, 10, 10], [20, 50, 10], [105, 10, 10]], "sorted by x, y, z"


# --- radii, setups, walls ------------------------------------------------------------------

def test_sharp_internal_edges_skip_holes_and_group_by_direction():
    faces, edges = counterbore_body()
    faces[2]["feature_order"] = faces[3]["feature_order"] = faces[4]["feature_order"] = 1
    concave = dict(normals=((0, 0, 1), (0, -1, 0)), tangent=(-1, 0, 0), line=True)
    floor, wall = len(faces), len(faces) + 1
    faces += [plane((0, 0, 1), "Pocket", 3), plane((0, -1, 0), "Pocket", 3)]
    edges += [edge(floor, wall, length=30.0, **concave), edge(floor, wall, length=30.0, **concave),
              edge(2, 3, normals=((-1, 0, 0), (0, 0, 1)), tangent=(0, -1, 0))]  # inside the counterbore
    found = sharp_internal_edges(faces, edges, {2, 3, 4})
    assert group_edges(found) == [{"feature": "Pocket", "direction": "x", "length_mm": 30.0, "count": 2}]


def test_planar_normals_merge_and_give_setup_directions():
    tilt = [0, math.sin(math.radians(0.2)), math.cos(math.radians(0.2))]
    merged = merge_normals([((0, 0, 1), 900.0), (tilt, 100.0), ((1, 0, 0), 50.0), ((0, 0, -1), 1000.0)])
    assert [(m["normal"], m["count"], m["area_mm2"]) for m in merged] == [
        ([0, 0, 1], 2, 1000.0), ([0, 0, -1], 1, 1000.0), ([1, 0, 0], 1, 50.0)]
    assert setup_directions(merged, ["+z", "-y", None]) == ["+z", "-z", "+x", "-y"]


def test_the_two_sides_of_a_thin_wall_are_one_spot():
    spots = [{"body": "B", "face": 5, "point_mm": [10, 10, 0], "thickness_mm": 0.8, "normal": [0, 0, -1]},
             {"body": "B", "face": 9, "point_mm": [30, 4, 0.8], "thickness_mm": 0.8, "normal": [0, 0, 1]},
             {"body": "B", "face": 2, "point_mm": [0, 3, 5], "thickness_mm": 0.9, "normal": [-1, 0, 0]}]
    assert [s["face"] for s in merge_wall_sides(spots)] == [5, 2]

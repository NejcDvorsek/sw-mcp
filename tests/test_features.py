"""Integration tests: each feature verified against a hand calculation.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
This is the automated regression suite that replaces running the m4_*.py scripts
by hand. Volumes are in mm^3.

Every new part costs SolidWorks time and memory, so a test checks all it can
on one part: a refused call leaves the part as it was (test_guard), so the
refusals of a tool run first, on the part its passing case then builds on.
"""

import math

import pytest

from sw_mcp import binding
from sw_mcp.constants import SW_FILE_LOCATIONS_MATERIALS, SW_TOGGLE_DISPLAY_PLANES, VIEWS
from sw_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def test_a_box_its_faces_and_its_dimensions(part):
    box = part.add_box(40, 20, 10)
    assert abs(vol(box) - 8000) < 0.01
    assert part.list_faces()["count"] == 6 and part.list_edges()["count"] == 12
    dims = box["dimensions"]
    with pytest.raises(SolidWorksError, match="cannot be negative"):
        part.set_dimension(box["depth_dimension"], -7)
    assert abs(vol(part.set_dimension(box["depth_dimension"], 25)) - 20000) < 0.01
    assert abs(vol(part.set_dimension(dims["width"], 50)) - 50 * 20 * 25) < 0.01, "'width' does not drive the width"
    assert abs(vol(part.set_dimension(dims["height"], 30)) - 50 * 30 * 25) < 0.01, "'height' does not drive the height"
    # SolidWorks ignores a width of 0 without a word: set_dimension said ok with
    # applied false, and the sketch was left 'invalid solution' (the part
    # fixture fails on that)
    with pytest.raises(SolidWorksError, match="stays at 50 mm"):
        part.set_dimension(dims["width"], 0)
    assert vol(part.get_mass_properties()) == pytest.approx(50 * 30 * 25), "the refused write changed the part"


def test_a_template_with_its_planes_in_another_order_still_builds_on_front(part, monkeypatch):
    """A user's template held another plane first: every block came out
    turned, 10 along x instead of 40, and each tool that looks for a face by
    its direction missed it. SolidWorks will not move a default plane, so the
    tree is read here as such a template gives it: Right, Top, Front."""
    in_tree_order = part._ref_planes
    monkeypatch.setattr(part, "_ref_planes", lambda: list(reversed(in_tree_order()[:3])) + in_tree_order()[3:])

    part.add_box(40, 20, 10)

    box = part.get_bounding_box()["bounding_box_mm"]
    assert box["min_mm"] == pytest.approx([0, 0, 0], abs=1e-6), box
    assert box["max_mm"] == pytest.approx([40, 20, 10], abs=1e-6), f"the block was not built on the Front plane: {box}"
    hole = part.add_hole_on_face(4, "+x", 40, 10, 5, depth_mm=5)
    assert abs(vol(hole) - (8000 - math.pi * 4 * 5)) < 0.01


def test_a_points_coordinate_takes_its_sign(part):
    """x3 is the top-left corner's x, to SolidWorks a distance from the origin:
    -5 put the corner at x = -5 but read back 5, 'not applied', and a later 3
    left it on that side, at -3, reported as applied. A trapezoid 10 high,
    20 along the bottom, its top from the corner to x = 20, 5 deep."""
    x3 = part.add_extruded_profile([[0, 0], [20, 0], [20, 10], [8, 10]], 5)["dimensions"]["x3"]
    across = part.set_dimension(x3, -5)
    assert across["applied"] and across["new_value_mm"] == pytest.approx(-5) and across["old_value_mm"] == 8
    assert vol(across) == pytest.approx((20 + 25) / 2 * 10 * 5)
    # on the negative side already, -4 must not send it across to +4
    assert part.set_dimension(x3, -4)["new_value_mm"] == pytest.approx(-4)
    back = part.set_dimension(x3, 3)
    assert back["applied"] and back["new_value_mm"] == pytest.approx(3)
    assert vol(back) == pytest.approx((20 + 17) / 2 * 10 * 5), "the corner stayed at x = -3"


def test_cylinder_and_its_radius(part):
    dims = part.add_cylinder(20, 20)
    assert abs(vol(dims) - math.pi * 100 * 20) < 0.1
    got = vol(part.set_dimension(dims["dimensions"]["radius"], 15))
    assert abs(got - math.pi * 225 * 20) < 0.1, "'radius' does not drive the radius"


def test_cone_frustum_and_its_top_radius(part):
    def frustum(rb, rt, h):
        return math.pi * h / 3 * (rb * rb + rb * rt + rt * rt)

    cone = part.add_cone(20, 10, 20)
    assert abs(vol(cone) - frustum(10, 5, 20)) < 0.1
    got = vol(part.set_dimension(cone["dimensions"]["top_radius"], 7.5))
    assert abs(got - frustum(10, 7.5, 20)) < 0.1, "'top_radius' does not drive the top"


def test_a_rounded_tab_between_two_points(part):
    # a stadium from (10, 10) to (40, 25), 8 wide: the arcs are centred on the
    # points themselves, so nothing has to be worked out by hand
    length = math.hypot(30, 15)
    tab = part.add_extruded_slot([10, 10], [40, 25], 8, 5)

    assert abs(vol(tab) - (8 * length + math.pi * 4 ** 2) * 5) < 0.01
    box = tab["mass_properties"]["bounding_box_mm"]
    assert box["min_mm"][:2] == pytest.approx([6, 6], abs=1e-6) and box["max_mm"][:2] == pytest.approx([44, 29], abs=1e-6)
    longer = part.set_dimension(tab["dimensions"]["end2_x"], 50)
    assert abs(vol(longer) - (8 * math.hypot(40, 15) + math.pi * 4 ** 2) * 5) < 0.01, "the end did not move"


def test_a_disc_off_the_origin_keeps_its_centre_as_dimensions(part):
    # a round foot core at x = 69, y = 10: before, only a disc at the origin existed
    disc = part.add_disc(20, 5, x_mm=69, y_mm=10)

    assert abs(vol(disc) - math.pi * 10 ** 2 * 5) < 0.1
    assert disc["mass_properties"]["bounding_box_mm"]["min_mm"][:2] == pytest.approx([59, 0], abs=1e-6)
    moved = part.set_dimension(disc["dimensions"]["x"], 80)
    assert moved["mass_properties"]["bounding_box_mm"]["min_mm"][0] == pytest.approx(70, abs=1e-6), (
        "the disc did not follow its x dimension"
    )


def test_revolve_and_its_angle_in_degrees(part):
    rod = [[0, 0], [10, 0], [10, 20], [0, 20]]
    # a profile crossing the axis (negative r) or on it (zero volume) must fail
    # fast, not build a garbage solid; 0 < angle <= 360
    with pytest.raises(SolidWorksError):
        part.add_revolved_profile([[5, 0], [-5, 0], [-5, 10], [5, 10]])
    with pytest.raises(SolidWorksError):
        part.add_revolved_profile([[0, 0], [0, 10], [0, 20]])
    for angle in (0, 361):
        with pytest.raises(SolidWorksError):
            part.add_revolved_profile(rod, angle)

    full = vol(part.add_revolved_profile(rod, 360))  # 360 is the inclusive upper bound
    assert abs(full - math.pi * 100 * 20) < 0.1
    # 180 used to land as 0.18 radians: the rod came out 10 degrees, not half
    [angle] = [d["name"] for d in part.list_dimensions()["dimensions"] if d["unit"] == "deg"]
    half = part.set_dimension(angle, 180)
    assert half["applied"] and half["new_value_deg"] == pytest.approx(180)
    assert vol(half) == pytest.approx(full / 2, rel=1e-6)


def test_revolved_ring_and_its_outer_radius(part):
    # a profile off the axis turns into a ring: outer 10, inner 5, height 2
    ring = part.add_revolved_profile([[5, 0], [10, 0], [10, 2], [5, 2]])
    assert abs(vol(ring) - math.pi * (10 ** 2 - 5 ** 2) * 2) < 0.1
    got = vol(part.set_dimension(ring["dimensions"]["x1"], 12))
    assert abs(got - math.pi * (12 ** 2 - 5 ** 2) * 2) < 0.1, "'x1' does not drive the outer radius"


def test_swept_pipes_straight_and_bent(part):
    """Three pipes Ø10, apart from each other: by Pappus each adds pi*25 per mm of its path."""
    with pytest.raises(SolidWorksError):
        part.add_swept_pipe([[0, 0], [50, 0]], 0)
    # straight 50 mm path -> cylinder r=5
    got = vol(part.add_swept_pipe([[0, 0], [50, 0]], 10))
    assert abs(got - math.pi * 25 * 50) < 0.5
    # L path with 10 mm bend: length = 20 + 20 + (pi/2)*10
    before, length = got, 20 + 20 + math.pi / 2 * 10
    got = vol(part.add_swept_pipe([[0, 30], [30, 30], [30, 60]], 10, 10))
    assert abs(got - before - math.pi * 25 * length) < 0.5, "the L bend"
    # S-path: left turn then right turn -> exercises BOTH arc directions end-to-end.
    # straights 30+20+30 = 80, two quarter arcs = 2*(pi/2)*10 = 10*pi
    before, length = got, 80 + 10 * math.pi
    got = vol(part.add_swept_pipe([[0, 100], [40, 100], [40, 140], [80, 140]], 10, 10))
    assert abs(got - before - math.pi * 25 * length) < 0.5, "the S bend"


def test_swept_profile_draws_u_along_minus_z_and_v_along_y(part):
    """The docs said u runs along +Y and v along +Z; a cross-section placed by
    them came out turned a quarter. The profile is off-centre and wider in u
    than in v, so a swap or a flipped sign each land somewhere else."""
    profile = [[-5, -2], [15, -2], [15, 8], [-5, 8]]  # u -5..15, v -2..8
    swept = part.add_swept_profile(profile, [[0, 0], [40, 0]])
    assert abs(vol(swept) - 20 * 10 * 40) < 0.5
    bbox = swept["mass_properties"]["bounding_box_mm"]
    # u -> z = -u (-15..5), v -> y = v (-2..8)
    assert bbox["min_mm"] == pytest.approx([0, -2, -15], abs=1e-6) and \
        bbox["max_mm"] == pytest.approx([40, 8, 5], abs=1e-6), \
        f"the cross-section does not lie as documented (u along -z, v along +y): {bbox}"


def test_swept_profile_L_bend(part):
    # rect 20x10 (area 200) along an L path (R10) -> Pappus: area * path_length
    rect = [[-10, -5], [10, -5], [10, 5], [-10, 5]]
    length = 20 + 20 + math.pi / 2 * 10
    got = vol(part.add_swept_profile(rect, [[0, 0], [30, 0], [30, 30]], 10))
    assert abs(got - 200 * length) < 0.5


SW_VISIBILITY_HIDE = 1  # swVisibilityState_e.swVisibilityStateHide


def test_loft_two_squares(part):
    sq_a = [[-20, -20], [20, -20], [20, 20], [-20, 20]]
    sq_b = [[-10, -10], [10, -10], [10, 10], [-10, 10]]
    for squares, heights in (([sq_a, sq_b], [0]), ([sq_a, sq_b], [0, 0]), ([sq_a], [0])):
        with pytest.raises(SolidWorksError):
            part.add_lofted_solid(squares, heights)

    # ruled loft between square side 40 @ z=0 and side 20 @ z=30 -> prismatoid
    # V = h/6 * (a^2 + (a+b)^2 + b^2) = 30/6 * (1600 + 3600 + 400) = 28000
    loft = part.add_lofted_solid([sq_a, sq_b], [0, 30])
    assert abs(vol(loft) - 28000) < 1.0
    # the offset plane a loft needs is construction geometry: left visible, it
    # clutters every screenshot the agent takes to check its work
    helpers = part._ref_planes()[3:]  # after Front, Top and Right
    assert len(helpers) == 1 and helpers[0].Visible == SW_VISIBILITY_HIDE, "the loft left its helper plane visible"
    # the prismatoid scales linearly with its height: 56000 at 60
    got = vol(part.set_dimension(loft["dimensions"]["profile1_height"], 60))
    assert abs(got - 56000) < 2.0, "'profile1_height' does not move the top profile"


# L-bracket 60 x 60, walls 5 thick, 40 deep: (60*5 + 5*55) * 40 = 23000 mm^3.
L_BRACKET = [[0, 0], [60, 0], [60, 5], [5, 5], [5, 60], [0, 60]]


@pytest.mark.parametrize("start,end", [((35, 5), (5, 35)), ((5, 35), (35, 5))])
def test_rib_gusset_fills_the_inner_corner(part, start, end):
    part.add_extruded_profile(L_BRACKET, 40)
    # toward a point outside the bracket: nothing for the rib to grow into. Run
    # guarded, as every MCP call is: the refused rib takes its plane along
    with pytest.raises(SolidWorksError):
        part.run_guarded(part.add_rib, start, end, (50, 50), 4, 20)
    # gusset from the base (y=5) to the wall (x=5) at mid-depth, 4 thick:
    # triangle with legs 30 and 30 -> 450 mm^2 * 4 = 1800 mm^3, either end order
    got = vol(part.add_rib(start, end, (5, 5), 4, 20))
    assert abs(got - 24800) < 0.5, f"gusset added {got - 23000:.1f} mm^3, expected 1800"
    helpers = part._ref_planes()[3:]  # after Front, Top and Right
    assert len(helpers) == 1 and helpers[0].Visible == SW_VISIBILITY_HIDE, (
        "the rib's offset plane is left visible and clutters screenshots"
    )


@pytest.mark.parametrize("points", [
    [[20.0, 0.0], [0.0, 0.0], [0.0, 10.0], [20.007, 10.0]],   # closing segment nearly vertical
    [[0.0, 0.0], [20.0, 0.0], [20.007, 10.0], [0.0, 10.0]],   # a middle segment nearly vertical
])
def test_profile_is_drawn_exactly_not_snapped(part, points):
    """SolidWorks' automatic relations snap a nearly vertical line to vertical:
    a closing segment then fails outright ('Could not create line segment'),
    a middle one silently moves the point. Real outlines (meshes, splines) are
    full of such segments. Area 200.035 mm^2 exactly, not the snapped 200."""
    got = vol(part.add_extruded_profile(points, 5))
    assert abs(got - 1000.175) < 0.01, f"volume {got:.3f}: the outline was snapped, not drawn as given"


def test_cut_through_plane_removes_a_wedge_across_the_part(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError):  # a point off the plane
        part.cut_profile_through_plane([[1, 20, 10], [0, 20, 5], [0, 10, 10]], "right")
    # triangle on the Right plane (x = 0), legs 5 (z) and 10 (y) -> 25 mm^2,
    # cut through all in x across the 40-wide box: 8000 - 25*40 = 7000
    got = vol(part.cut_profile_through_plane([[0, 20, 10], [0, 20, 5], [0, 10, 10]], "right"))
    assert abs(got - 7000) < 0.01


def test_cut_through_plane_with_depth_is_centred_on_the_plane(part):
    # 4 x 4 rectangle on the Right plane, 30 deep centred on x = 0 -> x -15..15,
    # fully inside the Ø40 disc at y <= 9: removes 30*4*4 = 480. A one-sided
    # 30 mm cut would be clipped by the disc edge and remove only ~290.
    disc = vol(part.add_disc(40, 10))
    got = vol(part.cut_profile_through_plane([[0, 5, 2], [0, 9, 2], [0, 9, 6], [0, 5, 6]], "right", 30))
    assert abs(disc - got - 480) < 0.01, f"removed {disc - got:.3f} mm^3, a centred 30 mm cut removes 480"


def _iso_groove_mm3_per_mm(d, p, inner_width, outer_width):
    """Thread groove volume per mm of thread length, by hand.

    The groove is the ISO basic trapezoid (depth 5*sqrt(3)/16 * P, from the minor
    to the major radius) swept along a helix. A profile in a plane through the
    axis, swept helically, fills area * (centroid circumference) per turn, so per
    mm of length: area * 2*pi*r_centroid / P.
    """
    h = 5 * math.sqrt(3) / 16 * p
    area = (inner_width + outer_width) / 2 * h
    r_centroid = (d / 2 - h) + h * (inner_width + 2 * outer_width) / (3 * (inner_width + outer_width))
    return area * 2 * math.pi * r_centroid / p


def test_external_thread_cuts_the_iso_groove(part):
    rod = vol(part.add_disc(10, 40))
    # SolidWorks takes 'M10x1.3' without complaint and cuts a meaningless
    # groove, so the size must be checked against the thread-profile library
    with pytest.raises(SolidWorksError, match="M10x1.5"):  # lists the valid M10 sizes
        part.add_thread("M10x1.3", 0, 0, 40, 20)
    with pytest.raises(SolidWorksError):  # no edge at the point
        part.add_thread("M10x1.5", 30, 30, 40, 20)
    # M10x1.5 on a Ø10 rod, 20 mm from the top face: the groove is the nut's
    # tooth, P/4 wide at the minor radius and 7P/8 at the major radius
    removed = rod - vol(part.add_thread("M10x1.5", 0, 0, 40, 20))
    expected = 20 * _iso_groove_mm3_per_mm(10, 1.5, 1.5 / 4, 7 * 1.5 / 8)
    assert abs(removed - expected) < 0.001 * expected, (
        f"removed {removed:.3f} mm^3; an M10x1.5 groove over 20 mm is {expected:.3f}"
    )


def test_internal_thread_cuts_the_iso_groove(part):
    part.add_box(30, 30, 20)
    # SolidWorks shifts a tapped thread with the hole, so an Ø8.5 hole would
    # give an oversized M10: refuse it and say which hole is needed
    part.add_hole(8.5, 6, 6)
    with pytest.raises(SolidWorksError, match="8.376"):
        part.add_thread("M10x1.5", 6, 6, 20, 12, internal=True)
    # M10x1.5 in a hole of the ISO basic minor diameter (10 - 1.082532*1.5),
    # 12 mm deep: the groove is the bolt's tooth, 3P/4 wide inside, P/8 outside
    block = vol(part.add_hole(8.376202, 15, 15))
    removed = block - vol(part.add_thread("M10x1.5", 15, 15, 20, 12, internal=True))
    expected = 12 * _iso_groove_mm3_per_mm(10, 1.5, 3 * 1.5 / 4, 1.5 / 8)
    # SolidWorks' tap profile runs ~0.06% leaner than the ISO basic trapezoid
    assert abs(removed - expected) < 0.002 * expected, (
        f"removed {removed:.3f} mm^3; an M10x1.5 tapped groove over 12 mm is {expected:.3f}"
    )


def test_round_flange(part):
    # disc + centre bore + bolt hole + 6x circular pattern = a round flange
    part.add_disc(80, 15)
    part.add_hole(20, 0, 0, name="Bore")
    part.add_hole(10, 30, 0, name="Bolt")
    r = part.add_circular_pattern(6, 0, 0)
    expected = math.pi * 15 * (40 ** 2 - 10 ** 2 - 6 * 5 ** 2)
    assert abs(vol(r) - expected) < 0.5


def test_profile_gets_one_dimension_per_distinct_edge_position(part):
    # L-bracket 1800 mm^2: vertical walls at x=60 and x=20, horizontal at y=20
    # and y=50 (x=0 and y=0 sit on the origin). Moving the x=60 wall to 70 adds
    # 10 x 20 mm^2 of profile.
    result = part.add_extruded_profile([[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]], 10)
    assert abs(vol(result) - 18000) < 0.1
    dims = result["dimensions"]
    assert set(dims) == {"x1", "x3", "y2", "y4", "depth"}, f"unexpected dimensions {sorted(dims)}"
    assert abs(vol(part.set_dimension(dims["x1"], 70)) - 20000) < 0.01, "'x1' does not move the x=60 wall"


def test_extruded_profile_explicitly_closed(part):
    # repeating the first point must give the same result (ring normalised)
    pts = [[0, 0], [40, 0], [40, 20], [0, 0]]  # triangle, area 400
    assert abs(vol(part.add_extruded_profile(pts, 10)) - 4000) < 0.1


def test_extruded_spline_circle_approx(part):
    with pytest.raises(SolidWorksError):
        part.add_extruded_spline([[0, 0], [10, 0]], 5)
    # a closed spline through 24 points on a circle r=20 approximates the circle;
    # extruded 10 mm -> volume ~= pi*r^2*h (spline area is not analytic, so loose tol)
    pts = [[20 * math.cos(2 * math.pi * i / 24), 20 * math.sin(2 * math.pi * i / 24)]
           for i in range(24)]
    assert abs(vol(part.add_extruded_spline(pts, 10)) - math.pi * 400 * 10) < 30


def test_hole_and_its_dimensions(part):
    part.add_box(40, 20, 10)
    hole = part.add_hole(8, 20, 10)
    assert abs(vol(hole) - (8000 - math.pi * 16 * 10)) < 0.1
    assert hole["fully_defined"] is True
    listed = {d["name"]: d for d in part.list_dimensions()["dimensions"]}
    for name, value in ((hole["dimensions"]["diameter"], 8), (hole["dimensions"]["x"], 20),
                        (hole["dimensions"]["y"], 10), ("D1@BlockExtrude", 10)):
        assert name in listed, f"{name} is missing from list_dimensions"
        assert abs(listed[name]["value"] - value) < 1e-6 and listed[name]["unit"] == "mm"
    assert all(d["driving"] for d in listed.values())
    got = vol(part.set_dimension(hole["dimensions"]["diameter"], 10))
    assert abs(got - (8000 - math.pi * 25 * 10)) < 0.1, "'diameter' does not drive the hole size"
    # (x, y) are add_box coordinates: the material's centre of mass moves away
    part.set_dimension(hole["dimensions"]["x"], 10)
    com = part.set_dimension(hole["dimensions"]["y"], 6)["mass_properties"]["center_of_mass_mm"]
    assert com[0] > 20 and com[1] > 10, "moving the hole to (10, 6) must shift the material's centre of mass to +x, +y"


def test_counterbore_hole(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError):  # the counterbore must be wider than the hole
        part.add_counterbore_hole(10, 5, 4, 20, 10)
    # clearance Ø5 through 10 mm + Ø10 counterbore 4 mm deep, centred at (20,10).
    # removed = pi*2.5^2*10 + pi*(5^2 - 2.5^2)*4 = 196.35 + 235.62 = 431.97
    hole = part.add_counterbore_hole(5, 10, 4, 20, 10)
    removed = math.pi * 2.5 ** 2 * 10 + math.pi * (5 ** 2 - 2.5 ** 2) * 4
    assert abs(vol(hole) - (8000 - removed)) < 0.5
    removed = math.pi * 2.5 ** 2 * 10 + math.pi * (6 ** 2 - 2.5 ** 2) * 4
    got = vol(part.set_dimension(hole["dimensions"]["cbore_diameter"], 12))
    assert abs(got - (8000 - removed)) < 0.5, "'cbore_diameter' does not drive the pocket"


def test_hole_on_x_face(part):
    part.add_box(40, 20, 10)
    # Fail-fast: a point 5 mm off the +X face must error, not be silently
    # projected onto it (x=35 instead of the on-face x=40)
    with pytest.raises(SolidWorksError):
        part.add_hole_on_face(8, "+x", 35, 10, 5)
    # +X face at x=40 (20x10); centre (40,10,5); hole runs through the 40 mm length
    hole = part.add_hole_on_face(8, "+x", 40, 10, 5)
    assert abs(vol(hole) - (8000 - math.pi * 16 * 40)) < 0.1
    got = vol(part.set_dimension(hole["dimensions"]["diameter"], 6))
    assert abs(got - (8000 - math.pi * 9 * 40)) < 0.1, "'diameter' does not drive the hole size"


def test_hole_on_y_face(part):
    # +Y face at y=20 (40x10); centre (20,20,5); hole runs through the 20 mm width
    part.add_box(40, 20, 10)
    r = part.add_hole_on_face(8, "+y", 20, 20, 5)
    assert abs(vol(r) - (8000 - math.pi * 16 * 20)) < 0.1


def test_cut_profile_blind_through_and_on_a_side_face(part):
    part.add_box(40, 20, 10)
    history = part.list_features()["features"]
    pts = [[10, 5], [30, 5], [30, 15], [10, 15]]  # 20x10 pocket
    with pytest.raises(SolidWorksError, match="depth must be > 0"):
        part.cut_profile(pts, -1)
    assert part.list_features()["features"] == history, "the refused pocket left its sketch in the tree"
    assert abs(vol(part.cut_profile(pts, 4)) - (8000 - 200 * 4)) < 0.1
    # a 5 x 5 window through the 10 mm block, beside the pocket
    got = vol(part.cut_profile([[2, 2], [7, 2], [7, 7], [2, 7]], None))
    assert abs(got - (8000 - 200 * 4 - 25 * 10)) < 0.1, "the window does not go through"

    # Fail-fast: one vertex 2 mm off the +X face (x=38) must error, not be
    # silently projected onto the face
    with pytest.raises(SolidWorksError):
        part.cut_profile_on_face([[40, 5, 2], [40, 15, 2], [38, 15, 8], [40, 5, 8]], "+x", 5)
    # 10(y) x 6(z) pocket on the +X face (x=40), 5 mm deep: x 35..40, clear of the first
    r = part.cut_profile_on_face([[40, 5, 2], [40, 15, 2], [40, 15, 8], [40, 5, 8]], "+x", 5)
    assert abs(vol(r) - (8000 - 200 * 4 - 25 * 10 - 10 * 6 * 5)) < 0.1


def test_cut_slot_blind_and_its_width(part):
    # L=20 (centre-to-centre), W=10 obround at (20,10), 5 mm deep.
    # area = L*W + pi*(W/2)^2 = 200 + 25*pi
    part.add_box(40, 20, 10)
    slot = part.cut_slot(20, 10, 20, 10, 0, 5)
    assert abs(vol(slot) - (8000 - (20 * 10 + math.pi * 5 ** 2) * 5)) < 0.5
    area = 20 * 8 + math.pi * 4 ** 2
    got = vol(part.set_dimension(slot["dimensions"]["width"], 8))
    assert abs(got - (8000 - area * 5)) < 0.5, "'width' does not drive the slot width"


def test_cut_slot_through(part):
    part.add_box(40, 20, 10)
    area = 20 * 10 + math.pi * 5 ** 2
    assert abs(vol(part.cut_slot(20, 10, 20, 10, 0, None)) - (8000 - area * 10)) < 1.0


def test_cut_slot_angled_same_volume(part):
    # A 90-degree slot fits the 20-wide plate and removes the same volume.
    part.add_box(40, 20, 10)
    area = 10 * 8 + math.pi * 4 ** 2
    assert abs(vol(part.cut_slot(10, 8, 20, 10, 90, 5)) - (8000 - area * 5)) < 0.5


def test_cut_slot_angled_through(part):
    # 45-degree slot cut THROUGH the plate; slot area is rotation-invariant
    part.add_box(40, 20, 10)
    area = 8 * 6 + math.pi * 3 ** 2
    assert abs(vol(part.cut_slot(8, 6, 20, 10, 45, None)) - (8000 - area * 10)) < 1.0


def test_fillet_all_edges(part):
    part.add_box(40, 20, 10)
    # an agent picks indices from list_edges; a stale one must say which exist
    with pytest.raises(SolidWorksError, match=r"Edge index 12 is out of range \(0\.\.11\)"):
        part.add_fillet(2, edges="12")
    r = part.add_fillet(2)
    assert r["edges_filleted"] == 12 and vol(r) < 8000


def test_fillet_one_axis(part):
    part.add_box(40, 20, 10)
    assert part.add_fillet(2, edges="z")["edges_filleted"] == 4


def test_fillet_the_outline_of_a_face_but_not_its_holes(part):
    # the hole is far from the edges, so rounding the outline removes the same
    # volume with or without it; rounding its rim too would remove more
    part.add_box(40, 20, 10)
    plain = part.add_fillet(1, edges="+z:outline")
    assert plain["edges_filleted"] == 4
    part.close_part()
    part.new_part()
    part.add_box(40, 20, 10)
    part.add_hole(6, 10, 12)

    holes = [f["cylinder"] for f in part.list_faces()["faces"] if "cylinder" in f]
    assert len(holes) == 1, holes
    assert holes[0]["radius_mm"] == pytest.approx(3)
    assert [abs(a) for a in holes[0]["axis"]] == pytest.approx([0, 0, 1])
    assert holes[0]["point_mm"] == pytest.approx([10, 12, 5], abs=1e-4), "a hole circle must be readable off its faces"

    holed = part.add_fillet(1, edges="+z:outline")

    assert holed["edges_filleted"] == 4, "the hole's rim was taken for the outline too"
    assert abs(vol(holed) - (vol(plain) - math.pi * 3 ** 2 * 10)) < 0.01, "the hole's rim got rounded"


def test_chamfer(part):
    part.add_box(40, 20, 10)
    assert vol(part.add_chamfer(2)) < 8000


@pytest.mark.parametrize("face", ["-z", "+z"])
def test_a_chamfer_at_60_degrees_leaves_the_named_face(part, face):
    """Only 45 degrees could be had; an underside edge prints without support
    at 60. Round the rim of a Ø40 x 10 disc, 2 mm in along the named face
    and 2 tan 60 down the side: a ring of that triangle, by Pappus. The
    distance on the side instead would remove 410.1, not 420.8."""
    part.add_disc(40, 10)
    with pytest.raises(SolidWorksError, match="needs from_face"):
        part.add_chamfer(2, edges=f"{face}:outline", angle_deg=60)
    before = vol(part.get_mass_properties())
    a, b = 2, 2 * math.tan(math.radians(60))
    chamfered = part.add_chamfer(2, edges=f"{face}:outline", angle_deg=60, from_face=face)
    removed = before - vol(chamfered)
    assert removed == pytest.approx(math.pi * (20 - a / 3) * a * b, rel=1e-3), (
        f"the 2 mm did not run along the {face} face: {removed:.1f} mm3 removed")


def test_a_part_keeps_its_shape_and_colour_after_saving(part, tmp_path):
    part.add_box(40, 20, 10)
    assert part.set_appearance(rgb=[200, 210, 220])["rgb"] == [200, 210, 220]
    path = part.save_part(str(tmp_path / "coloured.sldprt"))["path"]
    part.close_part()
    part.open_part(path)
    assert abs(vol(part.get_mass_properties()) - 8000) < 0.01
    assert [round(c * 255) for c in part._model.MaterialPropertyValues[:3]] == [200, 210, 220]


def test_shell_open(part):
    part.add_box(40, 20, 10)
    assert abs(vol(part.add_shell(2, "+z")) - (8000 - 36 * 16 * 8)) < 0.1


def test_linear_pattern(part):
    part.add_box(40, 20, 10)
    part.add_hole(8, 10, 10)
    r = part.add_linear_pattern(3, 10, "+x")
    assert abs(vol(r) - (8000 - 3 * math.pi * 16 * 10)) < 0.1


def test_circular_pattern(part):
    part.add_box(40, 40, 10)
    part.add_hole(8, 20, 20, name="CenterHole")
    part.add_hole(6, 10, 20, name="BoltHole")
    r = part.add_circular_pattern(4, 20, 20)
    expected = 40 * 40 * 10 - math.pi * 16 * 10 - 4 * math.pi * 9 * 10
    assert abs(vol(r) - expected) < 0.1


def test_material(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError):
        part.set_material("Definitely Not A Material 1234")
    # a refused name gives the agent names to choose from, also on a
    # SolidWorks in another language, where the database holds its own names
    with pytest.raises(SolidWorksError, match="Close names: .*6061 Alloy"):
        part.set_material("6061 Aloy")
    r = part.set_material("6061 Alloy")
    assert abs(r["mass_properties"]["density_kg_m3"] - 2700) < 50


def test_a_material_of_its_own_by_its_density(part, tmp_path):
    # TPU is in none of SolidWorks' libraries. The part shows it by name and
    # keeps it when saved and opened again, while SolidWorks' material
    # folders stay as they were; a second density for the name replaces the first
    part.add_box(10, 10, 10)
    folders = part._sw.GetUserPreferenceStringValue(SW_FILE_LOCATIONS_MATERIALS)
    part.set_material("TPU", density_kg_m3=1100)
    tpu = part.set_material("TPU", density_kg_m3=1210)
    assert tpu["material"] == "TPU" and tpu["mass_properties"]["mass_kg"] == pytest.approx(1.21e-3)
    assert part._sw.GetUserPreferenceStringValue(SW_FILE_LOCATIONS_MATERIALS) == folders, \
        "set_material left SolidWorks' material folders changed"
    saved = part.save_part(str(tmp_path / "tpu_block.sldprt"))["path"]
    part.close_part()
    part.open_part(saved)
    assert part.get_mass_properties()["mass_properties"]["density_kg_m3"] == pytest.approx(1210), \
        "the part lost its TPU density once the material file was gone"


def test_a_part_reopened_behind_another_document_can_still_be_cut(part, tmp_path):
    """open_part on a part that is already open returned it without making it
    SolidWorks' active document, and every sketch on it was refused ('refused
    a horizontal relation'), until everything was closed and reopened."""
    part.add_box(40, 20, 10)
    # a name no one has open: SolidWorks opens no second document of a title
    saved = part.save_part(str(tmp_path / "reopened_behind_another.sldprt"))["path"]
    other = part.new_part()["title"]  # now SolidWorks' active document
    try:
        part.open_part(saved)
        pocket = part.cut_profile([[5, 5], [15, 5], [15, 15], [5, 15]], 2)
        assert abs(vol(pocket) - (8000 - 10 * 10 * 2)) < 0.01
    finally:
        part._sw.CloseDoc(other)


def test_close_part_without_a_current_document_closes_the_saved_active_one(sw, tmp_path):
    """After close_part there is no current document, yet SolidWorks still shows
    one; closing that took an open_part first. A saved one may simply close."""
    from sw_mcp import __version__

    assert sw.get_status()["server_version"] == __version__, "a user cannot tell which server version runs"
    sw.new_part()
    sw.add_box(10, 10, 10)
    sw.save_part(str(tmp_path / "saved.sldprt"))
    sw.new_part()
    sw.close_part()
    assert sw.get_status()["current_part"] is None

    closed = sw.close_part()

    assert closed["closed"].lower() == "saved.sldprt", closed


def test_close_part_leaves_an_active_document_with_unsaved_work_open(sw):
    # closing never asks, so unsaved work in SolidWorks' active document would be lost
    sw.new_part()
    sw.add_box(10, 10, 10)
    unsaved = sw.get_status()["current_part"]
    sw.new_part()
    sw.close_part()
    try:
        with pytest.raises(SolidWorksError, match="unsaved"):
            sw.close_part()
        open_titles = [binding.wrap(d, binding.module().IModelDoc2).GetTitle() for d in sw._sw.GetDocuments()]
        assert unsaved in open_titles, "the unsaved document was closed"
    finally:
        sw._sw.CloseDoc(unsaved)


def test_a_disc_and_its_stl_export(part, tmp_path):
    from sw_mcp.constants import SW_STL_QUALITY, SW_TOGGLE_STL_DONT_TRANSLATE
    from sw_mcp.mesh_tools import load_mesh
    translate = part._sw.GetUserPreferenceToggle(SW_TOGGLE_STL_DONT_TRANSLATE)
    quality = part._sw.GetUserPreferenceIntegerValue(SW_STL_QUALITY)
    assert abs(vol(part.add_disc(40, 10)) - math.pi * 20 ** 2 * 10) < 0.1

    # a curved part tessellates finer at 'fine' -> more triangles -> bigger STL file
    coarse = part.export(str(tmp_path / "coarse.stl"), quality="coarse")["bytes"]
    fine = part.export(str(tmp_path / "fine.stl"), quality="fine")["bytes"]
    assert fine > coarse
    # SolidWorks moved an STL into positive space: a part measured back from
    # its STL was off by the shift. The Ø40 disc is centred on the origin
    exported = part.export(str(tmp_path / "disc.stl"))
    xs = [p[0] for triangle in load_mesh(exported["path"]) for p in triangle]
    assert (min(xs), max(xs)) == pytest.approx((-20, 20), abs=1e-3), "the STL was moved off the part's origin"
    assert exported["frame"] == "part"
    # SolidWorks' own settings are saved and restored around each export
    assert part._sw.GetUserPreferenceToggle(SW_TOGGLE_STL_DONT_TRANSLATE) == translate, "SolidWorks' own setting changed"
    assert part._sw.GetUserPreferenceIntegerValue(SW_STL_QUALITY) == quality, "the STL quality setting changed"


# The model axis that points at the viewer in each standard view.
VIEW_FACING_AXES = {"front": [0, 0, 1], "back": [0, 0, -1], "right": [1, 0, 0], "left": [-1, 0, 0],
                    "top": [0, 1, 0], "bottom": [0, -1, 0], "iso": [3 ** -0.5] * 3}


def shown_view(session):
    return binding.wrap(session._model.ActiveView, binding.module().IModelView)


def _facing(session, axis):
    """The model axis as seen on screen: [0, 0, 1] when it points at the viewer."""
    data = binding.wrap(shown_view(session).Orientation3, binding.module().IMathTransform).ArrayData
    return [sum(data[col * 3 + row] * axis[col] for col in range(3)) for row in range(3)]


def test_screenshot_looks_from_the_chosen_side(part, tmp_path):
    """Checked by the axis that faces the viewer, so a view SolidWorks numbers
    differently from what we assumed shows up."""
    part.add_box(40, 20, 10)
    wrong = []
    for view, axis in VIEW_FACING_AXES.items():
        shot = part.screenshot(str(tmp_path / f"{view}.png"), view=view)
        # the view was stilled again for the write: SOLIDWORKS 2024 saved a blank
        # picture, all white, 1.2 kB, while the tool reported success. A picture
        # with the part on it holds tens of kB
        if view in ("iso", "front"):
            assert shot["bytes"] > 10_000, f"the '{view}' screenshot is blank ({shot['bytes']} bytes)"
        if _facing(part, axis) != pytest.approx([0, 0, 1], abs=1e-3):
            wrong.append(view)
    assert not wrong, f"these screenshots look from another side: {wrong}"

    # iso looks only from +x+y+z: what sits underneath, behind, needs another corner
    below = [-1, 1, -1]
    assert part.screenshot(str(tmp_path / "below.png"), from_dir=below)["bytes"] > 0
    assert _facing(part, [c / 3 ** 0.5 for c in below]) == pytest.approx([0, 0, 1], abs=1e-3), \
        "the screenshot does not look from -x, +y, -z"

    # a 4 x 2 mm region of the 40 x 20 mm front fills the image: about 10 times larger
    part.screenshot(str(tmp_path / "whole.png"), view="front")
    whole = shown_view(part).Scale2
    part.screenshot(str(tmp_path / "detail.png"), view="front", zoom_mm=[[0, 0, 10], [4, 2, 10]])
    assert shown_view(part).Scale2 > 5 * whole, "the zoomed screenshot shows the whole part"


def on_screen(session, point_mm):
    """Where a model point lands in the view, in SolidWorks' screen coordinates."""
    import pythoncom
    import win32com.client
    mod = binding.module()
    xform = binding.wrap(shown_view(session).Transform, mod.IMathTransform)
    mathutil = binding.wrap(session._sw.GetMathUtility(), mod.IMathUtility)
    coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [c / 1000 for c in point_mm])
    point = binding.wrap(mathutil.CreatePoint(coords), mod.IMathPoint)
    return list(binding.wrap(point.MultiplyTransform(xform), mod.IMathPoint).ArrayData)[:2]


def test_screenshot_zooms_onto_the_region_from_every_side(part, tmp_path):
    # ViewZoomTo2 reads its corners along the screen's axes, which are x, y, z
    # only in the front view: elsewhere the region landed off screen and the
    # picture showed something else, or only the background
    part.add_box(40, 20, 10)
    region, centre = [[26, 11, 0], [34, 19, 10]], [30, 15, 5]
    part.screenshot(str(tmp_path / "front.png"), view="front", zoom_mm=region)
    middle = on_screen(part, centre)
    off = {}
    for view in ("back", "left", "right", "top", "bottom", "iso"):
        part.screenshot(str(tmp_path / f"{view}.png"), view=view, zoom_mm=region)
        landed = on_screen(part, centre)
        if math.dist(landed, middle) > 1:
            off[view] = [round(c) for c in landed]
    assert not off, f"the zoomed region is not in the middle of the picture ({middle}) in these views: {off}"


def test_screenshot_leaves_the_planes_out(part, tmp_path, monkeypatch):
    """At the moment of the picture SolidWorks' View > Planes is off, unless
    show_planes asks for it, and the part's own setting comes back afterwards.
    (The pictures themselves are no proof: shown planes render differently
    from shot to shot.)"""
    part.add_box(40, 20, 10)
    part.add_plane("front", offset_mm=5)
    extension = binding.wrap(part._model.Extension, binding.module().IModelDocExtension)
    extension.SetUserPreferenceToggle(SW_TOGGLE_DISPLAY_PLANES, 0, True)  # planes on, as a person may have them
    at_capture, take = [], part._take_screenshot

    def watched(*args):
        at_capture.append(extension.GetUserPreferenceToggle(SW_TOGGLE_DISPLAY_PLANES, 0))
        return take(*args)

    monkeypatch.setattr(part, "_take_screenshot", watched)
    part.screenshot(str(tmp_path / "plain.png"))
    part.screenshot(str(tmp_path / "planes.png"), show_planes=True)
    assert at_capture == [False, True], "planes should be hidden in the picture unless asked for"
    assert extension.GetUserPreferenceToggle(SW_TOGGLE_DISPLAY_PLANES, 0) is True, "the part's setting changed"


def test_profile_with_many_points_is_fixed_not_dimensioned(part):
    # 64 points (a mesh-like outline, past MAX_DIMENSIONED_VERTICES): dimensioning
    # it would take seconds per point and nobody edits that; it must be frozen,
    # yet fully defined
    n, r = 64, 10.0
    ring = [[r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)] for k in range(n)]
    result = part.add_extruded_profile(ring, 5)
    assert result["fully_defined"] is True
    assert set(result["dimensions"]) == {"depth"}
    assert abs(vol(result) - n / 2 * r * r * math.sin(2 * math.pi / n) * 5) < 0.01


# --- faces found by their point; blind holes and bosses on any face ------------
# '+z' used to mean the OUTERMOST +z face only (':inner' the innermost), so a
# pocket floor or a step in between could not be sketched on.

def test_hole_on_a_pocket_floor_is_found_by_its_point(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError):  # no +z face at z=8 yet
        part.add_hole_on_face(4, "+z", 20, 10, 8)
    # 20x10 pocket 4 deep: its floor is the +z face at z=6, not the top at z=10
    part.cut_profile([[10, 5], [30, 5], [30, 15], [10, 15]], 4)
    got = vol(part.add_hole_on_face(4, "+z", 20, 10, 6))
    assert abs(got - (8000 - 800 - math.pi * 4 * 6)) < 0.1, "the hole was not drilled from the pocket floor"


def test_face_between_the_outermost_and_innermost_is_found_by_its_point(part):
    # three +z levels: z=10 (x 20..40), z=7 (x 10..20), z=4 (x 0..10)
    part.add_box(40, 20, 10)
    part.cut_profile([[0, 0], [20, 0], [20, 20], [0, 20]], 3)
    part.cut_profile([[0, 0], [10, 0], [10, 20], [0, 20]], 6)
    before = vol(part.get_mass_properties())
    got = vol(part.add_hole_on_face(4, "+z", 15, 10, 7))
    assert abs(before - got - math.pi * 4 * 7) < 0.1, "the hole was not drilled from the middle step"


def test_blind_hole_on_a_side_face(part):
    part.add_box(40, 20, 10)
    hole = part.add_hole_on_face(6, "+x", 40, 10, 5, depth_mm=4)
    assert abs(vol(hole) - (8000 - math.pi * 9 * 4)) < 0.1
    assert set(hole["dimensions"]) == {"diameter", "x", "y", "depth"}
    got = vol(part.set_dimension(hole["dimensions"]["depth"], 8))
    assert abs(got - (8000 - math.pi * 9 * 8)) < 0.1, "'depth' does not drive the blind hole"


def test_bosses_on_the_top_and_a_side_face(part):
    part.add_box(40, 20, 10)
    boss = part.add_extruded_profile_on_face([[10, 5, 10], [30, 5, 10], [30, 15, 10], [10, 15, 10]], "+z", 5)
    assert abs(vol(boss) - (8000 + 200 * 5)) < 0.1
    assert abs(boss["mass_properties"]["bounding_box_mm"]["max_mm"][2] - 15) < 1e-6, "the boss must grow out of the face"

    boss = part.add_boss_on_face(8, "+x", 40, 10, 5, 6)
    assert abs(vol(boss) - (8000 + 200 * 5 + math.pi * 16 * 6)) < 0.1
    assert abs(boss["mass_properties"]["bounding_box_mm"]["max_mm"][0] - 46) < 1e-6, "the boss must grow out of the face"
    assert set(boss["dimensions"]) == {"diameter", "x", "y", "height"}


def test_standoff_is_a_boss_with_a_blind_hole_in_its_top(part):
    # a PCB standoff: 8 mm boss 6 high, then a 4 mm insert hole 5 deep in its top
    part.add_box(40, 20, 10)
    part.add_boss_on_face(8, "+z", 20, 10, 10, 6)
    got = vol(part.add_hole_on_face(4, "+z", 20, 10, 16, depth_mm=5))
    assert abs(got - (8000 + math.pi * 16 * 6 - math.pi * 4 * 5)) < 0.1


# --- Hole Wizard: SolidWorks' own ISO tables ------------------------------------
# A 40 x 20 x 10 block, holes from its top face along y = 10. Clearances follow
# ISO 273, tap drills ISO 2306; counterbore/countersink sizes are SolidWorks'
# ISO tables for socket head (ISO 4762) and countersunk (ISO 10642) screws.
# Several holes share a block: each one's removed volume is measured alone.

def _drill_point(d):
    """Volume of a 118 degree drill point of diameter d."""
    r = d / 2
    return math.pi * r * r * (r / math.tan(math.radians(59))) / 3


def _removed(part, hole):
    """The hole's result, and the volume it took out of the part."""
    before = vol(part.get_mass_properties())
    result = hole()
    return result, before - vol(result)


def test_wizard_clearance_holes_follow_iso_273(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError, match="M3.3"):
        part.add_hole_wizard("clearance", "M3.3", "+z", 20, 10, 10)
    for x, fit, d in ((7, "close", 3.2), (17, "normal", 3.4), (27, "loose", 3.6)):
        hole, removed = _removed(part, lambda: part.add_hole_wizard("clearance", "M3", "+z", x, 10, 10, fit=fit))
        assert hole["hole"]["diameter_mm"] == pytest.approx(d), f"an ISO 273 {fit} M3 clearance hole is Ø{d}"
        assert abs(removed - math.pi * (d / 2) ** 2 * 10) < 0.01
    _, removed = _removed(part, lambda: part.add_hole_wizard("clearance", "M3", "+z", 35, 10, 10, depth_mm=6))
    assert abs(removed - (math.pi * 1.7 ** 2 * 6 + _drill_point(3.4))) < 0.01, "a blind hole ends in a drill point"


def test_wizard_counterbores_for_a_socket_head_screw(part):
    part.add_box(40, 20, 10)
    hole, removed = _removed(part, lambda: part.add_hole_wizard("counterbore", "M3", "+z", 10, 10, 10))
    info = hole["hole"]
    assert (info["diameter_mm"], info["cbore_diameter_mm"], info["cbore_depth_mm"]) == pytest.approx((3.4, 6.5, 3.4))
    assert abs(removed - (math.pi * 1.7 ** 2 * 10 + math.pi * (3.25 ** 2 - 1.7 ** 2) * 3.4)) < 0.01
    # blind depth counts from the surface, the counterbore included
    _, removed = _removed(part, lambda: part.add_hole_wizard("counterbore", "M3", "+z", 30, 10, 10, depth_mm=6))
    assert abs(removed - (math.pi * 3.25 ** 2 * 3.4 + math.pi * 1.7 ** 2 * (6 - 3.4) + _drill_point(3.4))) < 0.01


def test_wizard_countersink_for_a_countersunk_screw(part):
    part.add_box(40, 20, 10)
    hole = part.add_hole_wizard("countersink", "M3", "+z", 20, 10, 10)
    info = hole["hole"]
    assert (info["csink_diameter_mm"], info["csink_angle_deg"]) == pytest.approx((6.72, 90.0))
    h = (6.72 - 3.4) / 2  # a 90 degree countersink is as deep as it is wide per side
    cone = math.pi * h / 3 * (3.36 ** 2 + 3.36 * 1.7 + 1.7 ** 2) - math.pi * 1.7 ** 2 * h
    assert abs(8000 - vol(hole) - (math.pi * 1.7 ** 2 * 10 + cone)) < 0.01
    # the face lies at the next point's height, but the point falls into this
    # hole's countersink (2.5 mm out: past the 1.7 bore, within the 3.36 rim):
    # the wizard then picked the cone there and failed with "has no position
    # sketch to define"
    before = part.list_features()["count"]
    with pytest.raises(SolidWorksError, match=r"not on the \+z face"):
        part.add_hole_wizard("countersink", "M3", "+z", 22.5, 10, 10)
    assert part.list_features()["count"] == before, "a refused hole must leave the part as it was"


def test_wizard_tapped_holes(part):
    part.add_box(40, 20, 10)
    hole, removed = _removed(part, lambda: part.add_hole_wizard("tapped", "M3", "+z", 7, 10, 10, depth_mm=8))
    info = hole["hole"]
    assert (info["tap_drill_diameter_mm"], info["thread_depth_mm"]) == pytest.approx((2.5, 6.0))
    assert info["cosmetic_thread"] is True
    assert abs(removed - (math.pi * 1.25 ** 2 * 8 + _drill_point(2.5))) < 0.01

    # printable: the hole is drilled at the ISO basic minor diameter and the
    # Thread feature cuts the bolt's tooth over the standard thread depth (2D)
    hole, removed = _removed(part, lambda: part.add_hole_wizard("tapped", "M3", "+z", 20, 10, 10, depth_mm=8,
                                                                thread="modeled"))
    d1 = 3 - 1.0825318 * 0.5
    assert hole["thread"]["size"] == "M3x0.5" and hole["thread"]["length_mm"] == pytest.approx(6.0)
    expected = math.pi * (d1 / 2) ** 2 * 8 + _drill_point(d1) + 6 * _iso_groove_mm3_per_mm(3, 0.5, 0.375, 0.0625)
    assert abs(removed - expected) < 0.002 * expected

    hole, removed = _removed(part, lambda: part.add_hole_wizard("tapped", "M4", "+z", 32, 10, 10, thread="modeled"))
    d1 = 4 - 1.0825318 * 0.7
    assert hole["thread"]["length_mm"] == pytest.approx(10.0), "a through tapped hole is threaded all the way"
    expected = math.pi * (d1 / 2) ** 2 * 10 + 10 * _iso_groove_mm3_per_mm(4, 0.7, 0.525, 0.0875)
    # The groove matches ISO exactly per mm, but the Thread feature's ends vary
    # by up to ~0.5 mm^3 (measured: -0.34 where it runs out of a 10 mm plate).
    # A thread one turn short would miss 1.3 mm^3, so this still catches that.
    assert abs(removed - expected) < 0.6, "the thread does not run the full 10 mm through the plate"


def test_wizard_hole_lands_exactly_and_its_position_is_a_dimension(part):
    # SolidWorks places a wizard hole where the face is picked, some 0.04 mm
    # off; the tool pins the position sketch to the exact point
    part.add_box(40, 20, 10)
    hole = part.add_hole_wizard("clearance", "M3", "+z", 20, 14, 10)
    assert hole["fully_defined"] is True
    assert part._circular_edges_at(20, 14, 10), "the hole edge is not centred on (20, 14): it landed off the point"
    com = part.set_dimension(hole["dimensions"]["x"], 30)["mass_properties"]["center_of_mass_mm"]
    assert com[0] < 20, "moving the hole to x=30 must shift the centre of mass to -x"


def test_wizard_hole_lands_on_its_face_whatever_the_view(part):
    # an L, 20 deep: the low +y step at y = 10 sits behind the -x face as seen
    # from the left. The wizard's face was picked on screen, so in this view
    # (a screenshot leaves one behind) it hit that -x face instead
    part.add_extruded_profile([[0, 0], [40, 0], [40, 10], [10, 10], [10, 20], [0, 20]], 20)
    part._model.ShowNamedView2("", VIEWS["left"])
    hole = part.add_hole_wizard("clearance", "M3", "+y", 25, 10, 10)
    assert hole["fully_defined"] is True
    assert part._circular_edges_at(25, 10, 10), "the hole went into the face seen first in the view, not +y at (25, 10, 10)"


def test_wizard_hole_on_either_of_two_top_faces_at_one_height(part):
    # a 5 deep slot right across splits the top into two faces at z = 10; the
    # point decides which one, so neither may be refused as off the face
    part.add_box(40, 20, 10)
    part.cut_profile([[18, 0], [22, 0], [22, 20], [18, 20]], 5)
    for x in (8, 32):
        hole = part.add_hole_wizard("clearance", "M3", "+z", x, 10, 10)
        assert hole["fully_defined"] is True, f"the hole at x={x} did not land on its top face"


# --- compare the part with a reference mesh --------------------------------------

def _box_stl(path, x0, y0, z0, w, h, d):
    """A binary STL of a w x h x d box with its corner at (x0, y0, z0)."""
    import struct
    v = [(x0 + dx * w, y0 + dy * h, z0 + dz * d) for dx, dy, dz in
         ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for face in faces:
            f.write(struct.pack("<12fH", 0, 0, 0, *[c for i in face for c in v[i]], 0))
    return str(path)


def test_part_matching_its_reference_mesh_shows_no_difference(part, tmp_path):
    # the part is measured in its own frame: SolidWorks' STL shift to positive
    # space would show up here as a 5 mm extent difference
    part.add_extruded_profile([[-5, -5], [35, -5], [35, 15], [-5, 15]], 10)
    result = part.compare_with_mesh(_box_stl(tmp_path / "ref.stl", -5, -5, 0, 40, 20, 10), "z", [2, 8])
    assert result["worst_extent_diff_mm"] < 1e-3 and result["worst_area_diff_mm2"] < 1e-3


def test_compare_moves_the_mesh_and_shows_a_missing_pocket(part, tmp_path):
    part.add_box(40, 20, 10)
    shifted = _box_stl(tmp_path / "shifted.stl", 100, 0, 0, 40, 20, 10)
    assert part.compare_with_mesh(shifted, "z", [5])["worst_extent_diff_mm"] == pytest.approx(100, abs=1e-3)
    assert part.compare_with_mesh(shifted, "z", [5], offset_mm=[-100, 0, 0])["worst_extent_diff_mm"] < 1e-3

    part.cut_profile([[10, 5], [30, 5], [30, 15], [10, 15]], 4)  # a 20 x 10 pocket, floor at z=6
    result = part.compare_with_mesh(_box_stl(tmp_path / "ref.stl", 0, 0, 0, 40, 20, 10), "z", [3, 8])
    low, high = result["sections"]
    assert low["pairs"][0]["area_diff_mm2"] == pytest.approx(0, abs=1e-3), "below the pocket the part is solid"
    assert high["unmatched_part"] == 1, "the pocket is a loop the reference does not have"

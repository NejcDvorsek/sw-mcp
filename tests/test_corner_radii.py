"""Integration tests for rounded polygon corners (corner_radii_mm).

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
A right-angled corner rounded with radius r loses r^2 (1 - pi/4) of area; a
concave one (the inside of an L) gains the same. Volumes in mm^3.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

L_BRACKET = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]  # 1800 mm^2, concave corner 3
CONCAVE = {3}


def corner(r):
    return r * r * (1 - math.pi / 4)


def bracket_area(radii):
    return 1800 - sum(corner(r) * (-1 if i in CONCAVE else 1) for i, r in enumerate(radii))


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def test_one_radius_rounds_every_corner_with_one_dimension(part):
    """Design intent: 'all corners R5' is one dimension, so changing it changes
    them all, the way a designer's equal fillets work."""
    result = part.add_extruded_profile(L_BRACKET, 10, corner_radii_mm=5)

    assert abs(vol(result) - bracket_area([5] * 6) * 10) < 0.01
    assert result["fully_defined"] and "radius" in result["dimensions"], result["dimensions"]

    smaller = part.set_dimension(result["dimensions"]["radius"], 3)
    assert abs(vol(smaller) - bracket_area([3] * 6) * 10) < 0.01, "the corners do not share their radius"


def test_corners_keep_their_dimensions_as_virtual_sharps(part):
    # moving the corner at x = 60 to x = 70 widens the base by 10 x 20 mm^2,
    # whatever the corners are rounded with
    result = part.add_extruded_profile(L_BRACKET, 10, corner_radii_mm=5)

    wider = part.set_dimension(result["dimensions"]["x1"], 70)

    assert abs(vol(wider) - (bracket_area([5] * 6) + 10 * 20) * 10) < 0.01


def test_each_radius_gets_its_own_dimension(part):
    radii = [5, 5, 3, 0, 5, 2]  # corner 3 (the inside of the L) stays sharp
    result = part.add_extruded_profile(L_BRACKET, 10, corner_radii_mm=radii)

    assert abs(vol(result) - bracket_area(radii) * 10) < 0.01
    dims = result["dimensions"]
    assert {"r0", "r2", "r5"} <= set(dims) and "radius" not in dims, (
        f"one dimension per distinct radius, named after its first corner: {sorted(dims)}"
    )
    changed = part.set_dimension(dims["r2"], 6)
    assert abs(vol(changed) - bracket_area([5, 5, 6, 0, 5, 2]) * 10) < 0.01


def test_a_rounded_pocket(part):
    part.add_box(60, 40, 10)
    pocket = part.cut_profile([[10, 10], [50, 10], [50, 30], [10, 30]], 4, corner_radii_mm=5)

    assert abs(vol(pocket) - (60 * 40 * 10 - (40 * 20 - 4 * corner(5)) * 4)) < 0.01


def test_a_rounded_pad_on_a_face(part):
    part.add_box(40, 20, 10)
    pad = part.add_extruded_profile_on_face([[5, 5, 10], [35, 5, 10], [35, 15, 10], [5, 15, 10]], "+z", 2,
                                            corner_radii_mm=3)

    assert abs(vol(pad) - (8000 + (30 * 10 - 4 * corner(3)) * 2)) < 0.01
    assert pad["fully_defined"] and "radius" in pad["dimensions"]


def test_a_rounded_window_through_a_plane(part):
    part.add_box(40, 20, 10)
    window = part.cut_profile_through_plane([[0, 5, 2], [0, 15, 2], [0, 15, 8], [0, 5, 8]], "right",
                                            corner_radii_mm=2)

    # SolidWorks measures this through-all cut 0.0007 mm^2 per mm of depth short
    # (+0.027 here), although its sketch is exact: radius 2.0000000, the corners
    # in place, and a boss from the same profile measures exactly. A wrong
    # radius (R2.1: 14 mm^3) or a sharp corner (34 mm^3) is still far outside.
    assert abs(vol(window) - (8000 - (10 * 6 - 4 * corner(2)) * 40)) < 0.05


SPANDREL_CENTROID = (10 - 3 * math.pi) / (3 * (4 - math.pi))  # x r from the corner, along both edges


def test_a_turned_ring_with_rounded_outer_edges(part):
    # Pappus: a ring r 5..15, z 0..10 spun 360 deg, minus the two rounded outer
    # corners, each r^2 (1 - pi/4) with its centroid 0.2234 r inside r = 15
    ring = 2 * math.pi * (10 * 10) * 10
    outer = 2 * math.pi * corner(2) * (15 - SPANDREL_CENTROID * 2)
    turned = part.add_revolved_profile([[5, 0], [15, 0], [15, 10], [5, 10]], corner_radii_mm=[0, 2, 2, 0])

    assert abs(vol(turned) - (ring - 2 * outer)) < 0.01, "the rounded edges are not where Pappus puts them"
    assert turned["fully_defined"] and "radius" in turned["dimensions"]


def test_a_corner_on_the_revolve_axis_cannot_be_rounded(part):
    with pytest.raises(SolidWorksError, match="on the axis"):
        part.add_revolved_profile([[0, 0], [10, 0], [10, 20], [0, 20]], corner_radii_mm=2)


def test_a_rounded_profile_swept_along_a_path(part):
    square = [[-5, -5], [5, -5], [5, 5], [-5, 5]]
    swept = part.add_swept_profile(square, [[0, 0], [50, 0]], corner_radii_mm=2)

    assert abs(vol(swept) - (100 - 4 * corner(2)) * 50) < 0.01
    assert swept["fully_defined"] and "radius" in swept["dimensions"]


def test_a_radius_too_big_fails_before_anything_is_sketched(part):
    part.add_box(40, 20, 10)
    history = part.list_features()["features"]

    with pytest.raises(SolidWorksError, match="Edge 1-2 is 10 mm long"):
        part.add_extruded_profile_on_face([[5, 5, 10], [35, 5, 10], [35, 15, 10], [5, 15, 10]], "+z", 2,
                                          corner_radii_mm=5)

    assert part.list_features()["features"] == history, "the refused profile left a sketch behind"

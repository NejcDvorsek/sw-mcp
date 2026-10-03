"""Integration tests for shaping from outlines and views: offset pockets and keep-inside cuts.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The block is 60 x 20 x 10 with R5 corners, so the outline has arcs to follow:
an inward offset by `rim` leaves a rounded rectangle 60 - 2 rim by 20 - 2 rim
with R(5 - rim) corners. Volumes are in mm^3.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK = 60 * 20 * 10 - (4 - math.pi) * 5 ** 2 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def inner_area(rim):
    """The face inside the rim: a rounded rectangle with R(5 - rim) corners."""
    return (60 - 2 * rim) * (20 - 2 * rim) - (4 - math.pi) * (5 - rim) ** 2


def rounded_block(session):
    return session.add_extruded_profile([[0, 0], [60, 0], [60, 20], [0, 20]], 10, corner_radii_mm=5)


def test_offset_pocket_leaves_a_rim_along_a_rounded_outline(part):
    rounded_block(part)
    pocket = part.cut_offset_pocket("+z", 30, 10, 10, rim_mm=2, depth_mm=3)
    assert vol(pocket) == pytest.approx(BLOCK - inner_area(2) * 3, abs=1e-3)
    assert set(pocket["dimensions"]) == {"rim", "depth"} and pocket["fully_defined"] is True
    assert pocket["dimensions"]["rim"].startswith("rim@"), "a person sees the rim as D1 in SolidWorks"

    # the rim stays a dimension a person can change; the pocket follows it
    wider = part.set_dimension(pocket["dimensions"]["rim"], 4)
    assert vol(wider) == pytest.approx(BLOCK - inner_area(4) * 3, abs=1e-3)


def test_offset_pocket_through_all_leaves_a_frame(part):
    rounded_block(part)
    frame = part.cut_offset_pocket("+z", 30, 10, 10, rim_mm=2)
    assert vol(frame) == pytest.approx(BLOCK - inner_area(2) * 10, abs=1e-3)


def test_offset_pocket_keeps_the_holes_in_the_face(part):
    # the hole was empty already: the pocket takes the rest of the inner area
    rounded_block(part)
    part.add_hole(6, 30, 10)
    pocket = part.cut_offset_pocket("+z", 10, 10, 10, rim_mm=2, depth_mm=3)
    hole_area = math.pi * 3 ** 2
    assert vol(pocket) == pytest.approx(BLOCK - hole_area * 10 - (inner_area(2) - hole_area) * 3, abs=1e-3)


def test_offset_pocket_with_a_rim_too_wide_leaves_the_part_as_it_was(sw):
    """Run guarded, as every MCP call is: the refused pocket leaves no sketch behind."""
    sw.new_part()
    try:
        rounded_block(sw)
        features = sw.list_features()["features"]
        with pytest.raises(SolidWorksError, match="rim wider than half the face"):
            sw.run_guarded(sw.cut_offset_pocket, "+z", 30, 10, 10, 11, 3)
        assert sw.list_features()["features"] == features
    finally:
        sw.close_part()


def test_keep_inside_turns_a_side_view_into_the_shape(part):
    # the front view is the 40 x 20 block; seen from the side only the triangle
    # y 0..20, z 0..6 is kept (a cut of the triangle would leave 5600 instead)
    part.add_box(40, 20, 10)
    wedge = part.cut_profile_through_plane([[0, 0, 0], [0, 20, 0], [0, 0, 6]], "right", keep_inside=True)
    assert vol(wedge) == pytest.approx(40 * 20 * 6 / 2, abs=1e-3), "the triangle was cut away, not kept"
    assert wedge["mass_properties"]["bounding_box_mm"]["max_mm"][2] == pytest.approx(6, abs=1e-4)

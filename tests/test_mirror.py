"""Integration tests for add_mirror: features, or the whole body, about a plane.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The disc is Ø40 x 10 and centred on the origin, so a mirror plane through the
origin cuts it in half. Volumes are in mm^3, positions in mm.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

DISC = math.pi * 20 ** 2 * 10
HOLE = math.pi * 3 ** 2 * 10  # Ø6 through the disc


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def com_x(result):
    return result["mass_properties"]["center_of_mass_mm"][0]


def disc_with_hole(part, x_mm):
    part.add_disc(40, 10, name="Disc")
    return part.add_hole(6, x_mm, 0, name="Hole")


def test_a_mirrored_hole_lands_on_the_other_side_of_the_plane(part):
    # the hole at x = 5 mirrored about x = -2.5 lands at x = -10; with material
    # gone at x = 5 and x = -10 the centre of mass sits at (10 - 5) * HOLE / (DISC - 2 * HOLE)
    disc_with_hole(part, 5)

    mirrored = part.add_mirror("right", offset_mm=-2.5, features=["Hole"])

    assert abs(vol(mirrored) - (DISC - 2 * HOLE)) < 0.01, "the mirror must add exactly one more hole"
    assert abs(com_x(mirrored) - 5 * HOLE / (DISC - 2 * HOLE)) < 1e-4, (
        f"the copy is not at x = -10 (centre of mass x = {com_x(mirrored):.5f}): the plane offset or its side is wrong"
    )


def test_the_mirror_plane_position_is_a_dimension(part):
    disc_with_hole(part, 5)
    mirror = part.add_mirror("right", offset_mm=-2.5, features=["Hole"])

    moved = part.set_dimension(mirror["dimensions"]["plane_offset"], 5)  # plane at x = -5, copy at x = -15

    assert abs(com_x(moved) - 10 * HOLE / (DISC - 2 * HOLE)) < 1e-4, "the copy did not follow its plane"


def test_mirrored_copies_follow_their_seed(part):
    """Design intent: a copy is no dumb duplicate, resizing the seed resizes it."""
    hole = disc_with_hole(part, 10)
    part.add_mirror("right", features=["Hole"])

    resized = part.set_dimension(hole["dimensions"]["diameter"], 8)

    assert abs(vol(resized) - (DISC - 2 * math.pi * 4 ** 2 * 10)) < 0.01, "the mirrored hole kept its old size"


def test_a_mirror_plane_tied_to_the_width_keeps_the_part_symmetric(part):
    """Design intent: with the plane at half the width by an equation, a wider
    block moves the copy along, so the holes stay symmetric (centre of mass in
    the middle). Without the link the copy would stay at x = 30."""
    block = part.add_box(40, 20, 10)
    part.add_hole(6, 10, 10, name="Hole")
    mirror = part.add_mirror("right", offset_mm=20, features=["Hole"])
    part.set_equation(f'"{mirror["dimensions"]["plane_offset"]}" = "{block["dimensions"]["width"]}" / 2')

    wider = part.set_dimension(block["dimensions"]["width"], 60)  # copy now at x = 50

    assert abs(vol(wider) - (60 * 20 * 10 - 2 * HOLE)) < 0.01
    assert abs(com_x(wider) - 30) < 1e-4, f"the copy did not stay symmetric (centre of mass x = {com_x(wider):.4f})"


def test_mirroring_the_body_makes_the_whole_from_a_half(part):
    part.add_box(20, 20, 10)

    whole = part.add_mirror("right", offset_mm=20)

    assert abs(vol(whole) - 8000) < 0.01
    assert whole["mass_properties"]["bounding_box_mm"]["max_mm"] == pytest.approx([40, 20, 10])
    # two touching halves weigh and measure the same as one body, but later
    # tools would work on one half only: the top face shows they merged
    assert max(f["area_mm2"] for f in part.list_faces()["faces"]) == pytest.approx(40 * 20), (
        "the mirrored half did not merge with the original into one body"
    )


def test_a_copy_outside_the_part_fails_and_leaves_it_as_it_was(part):
    # the box spans x 0..40, so the hole mirrored about x = 0 misses it;
    # SolidWorks builds that mirror anyway, with only a warning
    part.add_box(40, 20, 10)
    part.add_hole(6, 10, 10, name="Hole")
    history = part.list_features()["features"]

    with pytest.raises(SolidWorksError, match="outside the part"):
        part.add_mirror("right", features=["Hole"])

    assert part.list_features()["features"] == history, "the useless mirror stayed in the tree"


def test_a_body_copy_that_does_not_touch_fails_and_leaves_no_plane_behind(part):
    part.add_box(20, 20, 10)
    history = part.list_features()["features"]

    with pytest.raises(SolidWorksError, match="touch"):
        part.add_mirror("right", offset_mm=-5)

    assert part.list_features()["features"] == history, "the helper plane of the failed mirror stayed in the tree"

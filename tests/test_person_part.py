"""Integration tests for working in a part a person drew: their sketches and planes, by name.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
A person's sketch is drawn here straight through the API and left without
dimensions, as a quick hand sketch often is; a person's plane is an offset
plane. Volumes are in mm^3, positions in mm.
"""

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BOX = 40 * 20 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


# --- planes by name -------------------------------------------------------------


def test_mirror_about_a_persons_plane_moved_along_its_normal(part):
    # the person's plane at x = 15, moved 5 further: the hole at x = 10 lands at 30
    part.add_box(40, 20, 10)
    part.add_hole(6, 10, 10, name="Hole")
    plane, _ = part._plane_at("right", 15)
    part.add_mirror(plane.Name, offset_mm=5, features=["Hole"])
    holes = sorted(f["cylinder"]["point_mm"][0] for f in part.list_faces()["faces"] if "cylinder" in f)
    assert holes == pytest.approx([10, 30], abs=1e-4), f"mirrored about the wrong place: holes at x = {holes}"


def test_cut_through_a_persons_plane(part):
    # the person's plane at z = 5: the square cuts through the box both ways
    part.add_box(40, 20, 10)
    plane, _ = part._plane_at("front", 5)
    result = part.cut_profile_through_plane([[10, 5, 5], [20, 5, 5], [20, 15, 5], [10, 15, 5]], plane.Name)
    assert vol(result) == pytest.approx(BOX - 10 * 10 * 10)


def test_an_unknown_plane_names_the_planes_of_the_part(part):
    part.add_box(40, 20, 10)
    plane, _ = part._plane_at("front", 5)
    with pytest.raises(SolidWorksError, match=f"No plane 'side'.*{plane.Name}"):
        part.add_mirror("side")
    with pytest.raises(SolidWorksError, match=f"No plane 'side'.*{plane.Name}"):
        part.cut_profile_through_plane([[0, 0, 0], [0, 1, 0], [0, 0, 1]], "side")

"""Integration tests: weld beads, weldment members, end caps, gussets, the cut
list and weld symbols, each against a hand calculation.

A weld bead's length is where its faces meet. A member's volume is its
profile's area times its length along the sketch line, mitres included (a
mitre adds at one corner what it takes at the other). Volumes in mm^3.
"""

import os

import pytest

from sw_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

TUBE = ("square tube", "30 x 30 x 2.6")
WALL = 2.6


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def tee_of_plates(part):
    """A 5 mm plate standing on a 60 x 5 base, 40 deep, two bodies."""
    part.add_extruded_profile([[0, 0], [60, 0], [60, 5], [0, 5]], 40, name="Base")
    return part.add_extruded_profile([[25, 5], [30, 5], [30, 35], [25, 35]], 40, name="Upright", merge=False)


def tee_of_tubes(part):
    """A 400 mm tube along x, centred on y = 0, and one standing on it at x = 200."""
    part.add_structural_member(*TUBE, path_mm=[[0, 0], [400, 0]])
    return part.add_structural_member(*TUBE, path_mm=[[200, 15], [200, 300]])


def test_weld_beads_run_where_the_faces_meet_and_add_no_mass(part, tmp_path):
    before = vol(tee_of_plates(part))
    with pytest.raises(SolidWorksError, match="not a weldment"):
        part.add_gusset([25, 20, 20], [10, 5, 20], 10, 10, 2)

    result = part.add_weld_bead([25, 20, 20], [10, 5, 20], 4)
    assert result["weld_length_mm"] == pytest.approx(40)
    assert result["beads"] == 1
    assert result["size_mm"] == 4
    assert vol(result) == pytest.approx(before)
    # it is in the history and deletes
    assert part.list_features()["features"][-1]["name"] == result["feature"]
    part.delete_feature(result["feature"])
    with pytest.raises(SolidWorksError, match="no members and no weld beads"):
        part.cut_list()

    for side, length in (("both", 80), ("all_around", 2 * 40 + 2 * 5)):
        bead = part.add_weld_bead([25, 20, 20], [10, 5, 20], 4, side=side)
        assert bead["weld_length_mm"] == pytest.approx(length), side
        part.delete_feature(bead["feature"])
    # an intermittent weld lays beads along the pitch: 40 long, beads at 0 and 20
    result = part.add_weld_bead([25, 20, 20], [10, 5, 20], 4, length_mm=10, pitch_mm=20)
    assert result["beads"] == 2
    assert result["weld_length_mm"] == pytest.approx(20)

    # a weld symbol on an edge of the drawing
    part.save_part(str(tmp_path / "tee.sldprt"))
    part.new_drawing("A4")
    try:
        view = part.add_view("front")["view"]["name"]
        with pytest.raises(SolidWorksError, match="Unknown weld"):
            part.add_symbol("weld", view, [27.5, 35, 20], weld={"weld": "x"})
        made = part.add_symbol("weld", view, [27.5, 35, 20],
                               weld={"weld": "fillet", "size": "a3", "length": "40", "side": "both",
                                     "all_around": True})
        assert made["type"] == "weld_symbol"
        assert "a3" in made["text"] and "40" in made["text"]
    finally:
        part.close_drawing()


def test_one_body_is_not_welded_to_itself(part):
    part.add_extruded_profile([[0, 0], [60, 0], [60, 5], [30, 5], [30, 35], [25, 35], [25, 5], [0, 5]], 40)
    with pytest.raises(SolidWorksError, match="one body"):
        part.add_weld_bead([25, 20, 20], [10, 5, 20], 4)
    assert part.list_features()["features"][-1]["type"] != "CosmeticWeldBead"


def test_a_mitred_frame_has_members_as_long_as_its_sides(part):
    with pytest.raises(SolidWorksError, match="30 x 30 x 2.6"):  # an unknown size lists the sizes
        part.add_structural_member("square tube", "31 x 31", path_mm=[[0, 0], [100, 0]])
    straight = vol(part.add_structural_member(*TUBE, path_mm=[[0, -100], [100, -100]]))
    area = straight / 100
    result = part.add_structural_member(*TUBE, path_mm=[[0, 0], [400, 0], [400, 300], [0, 300], [0, 0]])
    assert result["members"] == 4
    # mass properties of the tube's rounded corners agree to a few parts in 10^4
    assert vol(result) - straight == pytest.approx(area * (2 * 400 + 2 * 300), rel=5e-4)
    frame = sorted((i["length_mm"], i["quantity"]) for i in result["cut_list"] if i["length_mm"] > 100)
    assert frame == [(330, 2), (430, 2)]  # outside lengths: a mitre adds half the tube at each end
    assert all(i["angle1_deg"] == 45 and i["angle2_deg"] == 45 for i in result["cut_list"] if i["length_mm"] > 100)
    assert part.get_bounding_box()["bounding_box_mm"]["max_mm"][:2] == pytest.approx([415, 315], abs=1e-6)


def test_butted_corners_keep_the_total_length(part):
    result = part.add_structural_member(*TUBE, path_mm=[[0, 0], [400, 0], [400, 300], [0, 300], [0, 0]],
                                        corner="butt1")
    assert sum(i["length_mm"] * i["quantity"] for i in result["cut_list"]) == pytest.approx(1400)
    assert not any(i.get("angle1_deg") == 45 for i in result["cut_list"])


def test_end_caps_close_the_tube_set_in_half_the_wall(part):
    """Three tubes along x, at y = 0, 100 and 200, each capped at x = 200."""
    # inward: inside the opening, 30 - 2 x 2.6 wide less its inset and the
    # tube's inner corner radii
    before = vol(part.add_structural_member(*TUBE, path_mm=[[0, 0], [200, 0]]))
    result = part.add_end_cap([200, 14, 0], 5, inward=True)
    assert 0 < vol(result) - before < (30 - 2 * WALL) ** 2 * 5
    assert part.get_bounding_box()["bounding_box_mm"]["max_mm"][0] == pytest.approx(200, abs=1e-6)

    before = vol(part.add_structural_member(*TUBE, path_mm=[[0, 100], [200, 100]]))
    result = part.add_end_cap([200, 114, 0], 5, inset_mm=1)
    assert vol(result) - before == pytest.approx(28 ** 2 * 5, abs=0.01)
    assert result["inset_mm"] == 1

    before = vol(part.add_structural_member(*TUBE, path_mm=[[0, 200], [200, 200]]))
    result = part.add_end_cap([200, 214, 0], 5)
    assert vol(result) - before == pytest.approx((30 - WALL) ** 2 * 5, abs=0.01)
    assert part.get_bounding_box()["bounding_box_mm"]["max_mm"][0] == pytest.approx(205, abs=1e-6)


def test_gussets_triangular_with_its_first_leg_along_the_first_face_and_polygon(part):
    before = vol(tee_of_tubes(part))
    result = part.add_gusset([185, 100, 0], [100, 15, 0], 60, 40, 5)
    assert vol(result) - before == pytest.approx(60 * 40 / 2 * 5, abs=0.01)
    [plate] = [b for b in part.list_bodies()["bodies"] if b["name"].startswith("Gusset")]
    assert plate["bounding_box_mm"]["min_mm"] == pytest.approx([145, 15, -2.5], abs=1e-6)
    assert plate["bounding_box_mm"]["max_mm"] == pytest.approx([185, 75, 2.5], abs=1e-6)
    # a polygon gusset has its tips cut; in the corner on the other side
    before = vol(result)
    result = part.add_gusset([215, 100, 0], [300, 15, 0], 50, 50, 5, shape="polygon", end_mm=10)
    assert vol(result) - before == pytest.approx((50 * 50 - 40 * 40 / 2) * 5, abs=0.01)


def test_the_cut_list_and_make_drawing_bring_members_and_weld_beads(part, tmp_path):
    tee_of_tubes(part)
    bead = part.add_weld_bead([185, 100, 0], [100, 15, 0], 3)
    side = part._part_face_at([185, 100, 0]).GetBox()  # the standing tube's flat side, between its round corners
    assert bead["weld_length_mm"] == pytest.approx((side[5] - side[2]) * 1000)
    listed = part.cut_list()
    assert sorted((i["length_mm"], i["quantity"]) for i in listed["items"]) == [(285, 1), (400, 1)]
    assert listed["total_weld_length_mm"] == pytest.approx(bead["weld_length_mm"])
    assert listed["weld_beads"][0]["feature"] == bead["feature"]

    part.save_part(str(tmp_path / "frame.sldprt"))
    result = part.make_drawing(str(tmp_path / "frame.pdf"), "A3", keep_open=True)
    try:
        assert result["weld_beads"] > 0
        assert result["cut_list"] == 2
        kinds = [a["type"] for a in part.list_annotations()["annotations"]]
        assert "weld_symbol" in kinds and "table" in kinds
    finally:
        part.close_drawing()
    assert os.path.getsize(result["path"]) > 0

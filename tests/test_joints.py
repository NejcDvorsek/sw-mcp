"""Integration tests for joints: concentric and angle mates, and a joint stepped through its range.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
Two links, 60 x 10 x 5 with a 4 mm hole: the thigh's at (5, 5), fixed at the
origin; the shin's at its other end, (55, 5), so the two parts differ where a
missed transform would hide. The shin turns on the thigh's hole axis, stacked on
it (z 5..10). A stop, 20 x 70 x 20, stands at x 20..40 across both sides of the
thigh: at 150 degrees the shin swings through it, at 30 and 90 it is clear.
Distances are in mm, angles in degrees.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks


@pytest.fixture(scope="module")
def linkage_parts(sw, tmp_path_factory):
    # "link_": a person's open thigh.sldprt would stand in for one of the same
    # name, and closing by title closed theirs
    directory = tmp_path_factory.mktemp("linkage")
    paths = {}
    for name, size, hole_at in (("link_thigh", (60, 10, 5), (5, 5)), ("link_shin", (60, 10, 5), (55, 5)),
                                ("link_stop", (20, 70, 20), None)):
        sw.new_part()
        sw.add_box(*size)
        if hole_at:
            sw.add_hole(4, *hole_at)
        paths[name] = sw.save_part(str(directory / f"{name}.sldprt"))["path"]
        sw.close_part()
    return paths


@pytest.fixture
def linkage(sw, linkage_parts):
    """The thigh fixed at the origin, the shin loose elsewhere, the stop fixed at x 20..40."""
    sw.new_assembly()
    sw.insert_component(linkage_parts["link_thigh"], 0, 0, 0)
    sw.insert_component(linkage_parts["link_shin"], 30, 40, 20)
    sw.insert_component(linkage_parts["link_stop"], 20, -30, 5, fixed=True)
    yield sw
    try:
        sw.close_part()
    except SolidWorksError:
        pass
    for path in linkage_parts.values():
        sw._sw.CloseDoc(path)


def hole(session, component):
    """The '#index' of the component's hole wall, as list_faces gives it."""
    [index] = [f["index"] for f in session.list_faces(component=component)["faces"] if "cylinder" in f]
    return f"#{index}"


def knee(session, angle_deg):
    """The shin on the thigh's hole axis, stacked on it, at angle_deg; returns the angle mate."""
    session.add_mate("link_shin", hole(session, "link_shin"), "link_thigh", hole(session, "link_thigh"), "concentric")
    session.add_mate("link_shin", "-z", "link_thigh", "+z", "coincident")
    return session.add_mate("link_shin", "-y", "link_thigh", "-y", "angle", angle_deg=angle_deg)


def shin_size(session):
    [shin] = [c for c in session.list_components()["components"] if c["name"].startswith("link_shin")]
    return shin["bounding_box_mm"]["size_mm"]


def test_concentric_mate_puts_the_hole_on_the_other_holes_axis(linkage):
    mated = linkage.add_mate("link_shin", hole(linkage, "link_shin"), "link_thigh", hole(linkage, "link_thigh"), "concentric")
    assert mated["axis_offset_mm"] == pytest.approx(0, abs=1e-4)
    # checked apart from the mate's own measurement: the thigh's hole axis
    # (x 5, y 5) now runs through the shin's hole, 2 mm from its wall
    probe = linkage.measure_distance("link_shin", point_mm=[5, 5, 22.5])
    assert probe["inside"] is False and probe["distance_mm"] == pytest.approx(2, abs=1e-4), probe


def test_concentric_mate_takes_cylindrical_faces(linkage):
    with pytest.raises(SolidWorksError, match="takes cylindrical faces"):
        linkage.add_mate("link_shin", "-z", "link_thigh", hole(linkage, "link_thigh"), "concentric")


def test_an_angle_mate_sets_the_joint_angle_as_one_number(linkage):
    """At 90 the shin stands up; its dimension then turns it to 30, and its box
    follows the turned link (60 cos 30 + 10 sin 30 wide)."""
    mate = knee(linkage, 90)
    assert mate["angle_deg"] == pytest.approx(90, abs=1e-3)
    assert shin_size(linkage) == pytest.approx([10, 60, 5], abs=1e-3)

    turned = linkage.set_dimension(mate["dimension"], 30)
    assert turned["applied"] and turned["new_value_deg"] == pytest.approx(30)
    width = 60 * math.cos(math.radians(30)) + 10 * math.sin(math.radians(30))
    depth = 60 * math.sin(math.radians(30)) + 10 * math.cos(math.radians(30))
    assert shin_size(linkage) == pytest.approx([width, depth, 5], abs=1e-3), "the shin did not follow the angle"


def test_check_motion_steps_the_knee_and_finds_the_clash(linkage):
    # at 150 the shin swings out over x 20..40 through the stop; at 90 it hangs
    # along the y axis, x 0..10, 10 mm short of it
    mate = knee(linkage, 90)
    motion = linkage.check_motion(mate["dimension"], [30, 90, 150], distances=[["link_shin", "link_stop"]])

    by_angle = {step["value_deg"]: step for step in motion["steps"]}
    assert [i["components"] for i in by_angle[150]["interferences"]] == [["link_shin-1", "link_stop-1"]]
    assert by_angle[30]["interferences"] == [] and by_angle[90]["interferences"] == []
    assert by_angle[90]["distances"][0]["distance_mm"] == pytest.approx(10, abs=1e-3)
    assert motion["clash_free"] is False
    assert motion["smallest_distances"] == [{"between": ["link_shin-1", "link_stop-1"], "distance_mm": 0, "at_deg": 150}]
    assert shin_size(linkage) == pytest.approx([10, 60, 5], abs=1e-3), "the knee was not put back at 90"


def test_a_refused_mate_leaves_the_assembly_as_it_was(linkage):
    """SolidWorks adds an over-defining mate all the same, in error, and flags
    the mate it fights; left in place it would fight every next attempt."""
    knee(linkage, 90)
    before = linkage.list_components()
    with pytest.raises(SolidWorksError, match="contradicts Angle1"):
        linkage.add_mate("link_shin", "-y", "link_thigh", "-y", "parallel")  # against the 90 degrees
    after = linkage.list_components()
    assert after["mates"] == before["mates"], f"the refused mate stayed: {after['mates']}"
    assert not any("error" in mate for mate in after["mates"])
    assert after["components"] == before["components"]


def test_a_sub_assembly_is_inserted_and_mated_on_a_parts_hole(sw, linkage_parts, tmp_path):
    """The real servo arrives as an assembly: inserted whole, its parts' holes
    are listed in its own frame and take a concentric mate."""
    sw.new_assembly()
    sw.insert_component(linkage_parts["link_shin"], 10, 0, 0)  # the shin's hole (55, 5) lands at (65, 5)
    pair = sw.save_assembly(str(tmp_path / "pair.sldasm"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        sw.insert_component(linkage_parts["link_thigh"], 0, 0, 0)
        inserted = sw.insert_component(pair, 30, 40, 20)
        assert inserted["component"]["name"].startswith("pair")
        [shin_hole] = [f for f in sw.list_faces(component="pair")["faces"] if "cylinder" in f]
        assert shin_hole["part"].endswith("link_shin-1")
        assert shin_hole["cylinder"]["point_mm"][:2] == pytest.approx([65, 5], abs=1e-4), "not in the pair's frame"
        mated = sw.add_mate("pair", f"#{shin_hole['index']}", "link_thigh", hole(sw, "link_thigh"), "concentric")
        assert mated["axis_offset_mm"] == pytest.approx(0, abs=1e-4)
        probe = sw.measure_distance("pair", point_mm=[5, 5, 22.5])  # the thigh's hole axis, through the shin's hole
        assert probe["inside"] is False and probe["distance_mm"] == pytest.approx(2, abs=1e-4), probe
        # a part inside the sub-assembly by its path: the shin sits at z 20..25, the thigh's top at 5
        gap = sw.measure_distance("pair-1/link_shin-1", "link_thigh")
        assert gap["distance_mm"] == pytest.approx(15, abs=1e-4), gap
        whole = sw.measure_distance("pair", "link_thigh")  # SolidWorks would not measure to it as a whole
        assert whole["distance_mm"] == pytest.approx(15, abs=1e-4), whole
    finally:
        sw.close_part()
        for path in (pair, linkage_parts["link_thigh"], linkage_parts["link_shin"]):
            sw._sw.CloseDoc(path)


def test_a_joint_inside_a_sub_assembly_is_stepped_from_the_top(sw, linkage_parts, tmp_path):
    """The knee lives in the leg's own assembly, the leg sits in a bigger one:
    its angle is reached as D1@Angle1@leg-1, and stepping it moves the shin
    inside the leg, which check_motion took for nothing moving."""
    sw.new_assembly()
    sw.insert_component(linkage_parts["link_thigh"], 0, 0, 0)
    sw.insert_component(linkage_parts["link_shin"], 30, 40, 20)
    angle = knee(sw, 90)["dimension"]
    leg = sw.save_assembly(str(tmp_path / "knee_leg.sldasm"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        sw.insert_component(leg, 0, 0, 0)
        turned = sw.set_dimension(f"{angle}@knee_leg-1", 30)
        assert turned["applied"], turned
        motion = sw.check_motion(f"{angle}@knee_leg-1", [60, 90])
        assert motion["moving"] == ["knee_leg-1/link_shin-1"], motion["moving"]
    finally:
        sw.close_part()
        for path in (leg, linkage_parts["link_thigh"], linkage_parts["link_shin"]):
            sw._sw.CloseDoc(path)


def test_add_mate_names_the_mate_it_made(linkage):
    angle = knee(linkage, 90)
    assert angle["mate"] in [m["name"] for m in linkage.list_components()["mates"]]


def test_a_suppressed_angle_mate_moves_nothing_and_check_motion_says_so(linkage):
    """Suppressed, the angle mate lets go of the shin: its dimension still
    changes, but nothing turns, so a clash-free verdict would be empty."""
    angle = knee(linkage, 90)
    assert linkage.suppress_mate(angle["mate"])["suppressed"]
    with pytest.raises(SolidWorksError, match="moved no component"):
        linkage.check_motion(angle["dimension"], [30, 60])
    assert not linkage.suppress_mate(angle["mate"], suppress=False)["suppressed"]
    assert linkage.check_motion(angle["dimension"], [30, 60])["moving"] == ["link_shin-1"]


def test_a_deleted_mate_is_gone(linkage):
    angle = knee(linkage, 90)
    gone = linkage.delete_mate(angle["mate"])
    assert gone["deleted"] == angle["mate"] and angle["mate"] not in gone["mates"] and len(gone["mates"]) == 2
    with pytest.raises(SolidWorksError, match=rf"No mate '{angle['mate']}'"):
        linkage.delete_mate(angle["mate"])


def test_stepping_a_joint_with_a_broken_mate_is_refused(sw, tmp_path):
    """The arm's hole was deleted and made again: the concentric mate on it
    held nothing, and check_motion let the part slide off its axis through
    the next one. Two 60 x 20 x 5 bars, Ø6 holes at (10, 10) and (50, 10)."""
    paths = {}
    for name, x in (("pivot_base", 10), ("pivot_arm", 50)):
        sw.new_part()
        sw.add_box(60, 20, 5)
        sw.add_hole(6, x, 10, name="PivotHole")
        paths[name] = sw.save_part(str(tmp_path / f"{name}.sldprt"))["path"]
        sw.close_part()
    sw.new_assembly()
    try:
        sw.insert_component(paths["pivot_base"], 0, 0, 0)
        sw.insert_component(paths["pivot_arm"], 10, 40, 5)
        held = sw.add_mate("pivot_arm", "@53, 10, 2.5", "pivot_base", "@13, 10, 2.5", "concentric")["mate"]
        sw.add_mate("pivot_arm", "-z", "pivot_base", "+z", "coincident")
        angle = sw.add_mate("pivot_arm", "-y", "pivot_base", "-y", "angle", angle_deg=90)
        assembly = sw._model.GetTitle()
        sw.open_part(paths["pivot_arm"])
        sw.delete_feature("PivotHole")
        sw.add_hole(6, 50, 10, name="PivotHole")
        sw.save_part(paths["pivot_arm"])
        sw.close_part()
        sw.activate_document(assembly)
        [entry] = [m for m in sw.list_components()["mates"] if m["name"] == held]
        assert "broken" in entry["error"]["cause"], entry
        with pytest.raises(SolidWorksError, match=rf"{held} \(broken"):
            sw.check_motion(angle["dimension"], [60, 90])
    finally:
        sw.close_part()
        for path in paths.values():
            sw._sw.CloseDoc(path)


def test_a_face_is_picked_by_a_point_on_it(linkage):
    """Face numbers came out in another order after a mate, and add_mate took
    the wrong faces. A point on a hole's wall, in the part's own coordinates,
    picks that hole whatever the order: Ø4 at (55, 5) in the shin, at (5, 5)
    in the thigh, both 5 thick."""
    mated = linkage.add_mate("link_shin", "@57, 5, 2.5", "link_thigh", "@7, 5, 2.5", "concentric")
    assert mated["axis_offset_mm"] == pytest.approx(0, abs=1e-4)
    with pytest.raises(SolidWorksError, match="nearest lies 1 mm away"):
        linkage.add_mate("link_shin", "@56, 5, 2.5", "link_thigh", "@7, 5, 2.5", "concentric")


def test_swept_region_outlines_what_the_shin_covers(linkage):
    """The shin, 60 x 10, turns about its hole 5 mm from one end. At 90 and at
    180 degrees it lies as two bars at right angles over one 10 x 10 square
    round the hole: 600 + 600 - 100. Cut at z 7.5, inside it (z 5..10)."""
    angle = knee(linkage, 90)
    swept = linkage.swept_region("link_shin", angle["dimension"], [90, 180], [7.5])
    [region] = swept["regions"]
    assert region["area_mm2"] == pytest.approx(1100, rel=0.03)
    assert shin_size(linkage) == pytest.approx([10, 60, 5], abs=1e-3), "the knee was not put back at 90"
    # in the coordinates of the stop, turned 90 degrees about z at (20, -30, 5):
    # the same outline, moved back and turned back
    linkage.set_component_transform("link_stop", 20, -30, 5, rz_deg=90)
    seen = linkage.swept_region("link_shin", angle["dimension"], [90, 180], [2.5], frame="link_stop")
    expected = sorted((round(y + 30, 4), round(20 - x, 4)) for x, y in region["outline_mm"])
    got = sorted((round(x, 4), round(y, 4)) for x, y in seen["regions"][0]["outline_mm"])
    assert got == expected, "the outline is not in the stop's coordinates"


def test_a_coincident_mate_holds_on_slanted_faces(sw, tmp_path):
    """A drafted wedge's slanted face is a trapezoid whose box centre lies 0.87
    mm off its plane. Measured from that centre a block's face mated onto it
    looked 0.87 mm away, and the mate was refused although the faces met."""
    sw.new_part()
    sw.add_extruded_profile([[0, 0], [40, 0], [0, 20]], 10, draft_deg=10)
    [slanted] = [f["index"] for f in sw.list_faces()["faces"] if 0.1 < abs(f["normal"][0]) < 0.9]
    wedge = sw.save_part(str(tmp_path / "slanted_wedge.sldprt"))["path"]
    sw.close_part()
    sw.new_part()
    sw.add_box(20, 20, 20)
    block = sw.save_part(str(tmp_path / "mate_block.sldprt"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        first = sw.insert_component(wedge, 0, 0, 0)["component"]["name"]
        second = sw.insert_component(block, 50, 50, 0)["component"]["name"]
        mate = sw.add_mate(second, "+x", first, f"#{slanted}", "coincident")
        assert mate["ok"] and mate["mate"] in [m["name"] for m in sw.list_components()["mates"]]
    finally:
        sw.close_part()
        for path in (wedge, block):
            sw._sw.CloseDoc(path)

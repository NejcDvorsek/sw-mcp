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
    directory = tmp_path_factory.mktemp("linkage")
    paths = {}
    for name, size, hole_at in (("thigh", (60, 10, 5), (5, 5)), ("shin", (60, 10, 5), (55, 5)),
                                ("stop", (20, 70, 20), None)):
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
    sw.insert_component(linkage_parts["thigh"], 0, 0, 0)
    sw.insert_component(linkage_parts["shin"], 30, 40, 20)
    sw.insert_component(linkage_parts["stop"], 20, -30, 5, fixed=True)
    yield sw
    try:
        sw.close_part()
    except SolidWorksError:
        pass
    for title in ("thigh.sldprt", "shin.sldprt", "stop.sldprt"):
        sw._sw.CloseDoc(title)


def hole(session, component):
    """The '#index' of the component's hole wall, as list_faces gives it."""
    [index] = [f["index"] for f in session.list_faces(component=component)["faces"] if "cylinder" in f]
    return f"#{index}"


def knee(session, angle_deg):
    """The shin on the thigh's hole axis, stacked on it, at angle_deg; returns the angle mate."""
    session.add_mate("shin", hole(session, "shin"), "thigh", hole(session, "thigh"), "concentric")
    session.add_mate("shin", "-z", "thigh", "+z", "coincident")
    return session.add_mate("shin", "-y", "thigh", "-y", "angle", angle_deg=angle_deg)


def shin_size(session):
    [shin] = [c for c in session.list_components()["components"] if c["name"].startswith("shin")]
    return shin["bounding_box_mm"]["size_mm"]


def test_concentric_mate_puts_the_hole_on_the_other_holes_axis(linkage):
    mated = linkage.add_mate("shin", hole(linkage, "shin"), "thigh", hole(linkage, "thigh"), "concentric")
    assert mated["axis_offset_mm"] == pytest.approx(0, abs=1e-4)
    # checked apart from the mate's own measurement: the thigh's hole axis
    # (x 5, y 5) now runs through the shin's hole, 2 mm from its wall
    probe = linkage.measure_distance("shin", point_mm=[5, 5, 22.5])
    assert probe["inside"] is False and probe["distance_mm"] == pytest.approx(2, abs=1e-4), probe


def test_concentric_mate_takes_cylindrical_faces(linkage):
    with pytest.raises(SolidWorksError, match="takes cylindrical faces"):
        linkage.add_mate("shin", "-z", "thigh", hole(linkage, "thigh"), "concentric")


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
    motion = linkage.check_motion(mate["dimension"], [30, 90, 150], distances=[["shin", "stop"]])

    by_angle = {step["value_deg"]: step for step in motion["steps"]}
    assert [i["components"] for i in by_angle[150]["interferences"]] == [["shin-1", "stop-1"]]
    assert by_angle[30]["interferences"] == [] and by_angle[90]["interferences"] == []
    assert by_angle[90]["distances"][0]["distance_mm"] == pytest.approx(10, abs=1e-3)
    assert motion["clash_free"] is False
    assert motion["smallest_distances"] == [{"between": ["shin-1", "stop-1"], "distance_mm": 0, "at_deg": 150}]
    assert shin_size(linkage) == pytest.approx([10, 60, 5], abs=1e-3), "the knee was not put back at 90"


def test_a_refused_mate_leaves_the_assembly_as_it_was(linkage):
    """SolidWorks adds an over-defining mate all the same, in error, and flags
    the mate it fights; left in place it would fight every next attempt."""
    knee(linkage, 90)
    before = linkage.list_components()
    with pytest.raises(SolidWorksError, match="contradicts Angle1"):
        linkage.add_mate("shin", "-y", "thigh", "-y", "parallel")  # against the 90 degrees
    after = linkage.list_components()
    assert after["mates"] == before["mates"], f"the refused mate stayed: {after['mates']}"
    assert not any("error" in mate for mate in after["mates"])
    assert after["components"] == before["components"]


def test_a_sub_assembly_is_inserted_and_mated_on_a_parts_hole(sw, linkage_parts, tmp_path):
    """The real servo arrives as an assembly: inserted whole, its parts' holes
    are listed in its own frame and take a concentric mate."""
    sw.new_assembly()
    sw.insert_component(linkage_parts["shin"], 10, 0, 0)  # the shin's hole (55, 5) lands at (65, 5)
    pair = sw.save_assembly(str(tmp_path / "pair.sldasm"))["path"]
    sw.close_part()
    sw.new_assembly()
    try:
        sw.insert_component(linkage_parts["thigh"], 0, 0, 0)
        inserted = sw.insert_component(pair, 30, 40, 20)
        assert inserted["component"]["name"].startswith("pair")
        [shin_hole] = [f for f in sw.list_faces(component="pair")["faces"] if "cylinder" in f]
        assert shin_hole["part"].endswith("shin-1")
        assert shin_hole["cylinder"]["point_mm"][:2] == pytest.approx([65, 5], abs=1e-4), "not in the pair's frame"
        mated = sw.add_mate("pair", f"#{shin_hole['index']}", "thigh", hole(sw, "thigh"), "concentric")
        assert mated["axis_offset_mm"] == pytest.approx(0, abs=1e-4)
        probe = sw.measure_distance("pair", point_mm=[5, 5, 22.5])  # the thigh's hole axis, through the shin's hole
        assert probe["inside"] is False and probe["distance_mm"] == pytest.approx(2, abs=1e-4), probe
    finally:
        sw.close_part()
        for title in ("pair.sldasm", "thigh.sldprt", "shin.sldprt"):
            sw._sw.CloseDoc(title)

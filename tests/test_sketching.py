"""Integration tests for free sketches: lines, tangent arcs and splines, fully defined.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The `part` fixture also fails any test that leaves a sketch under-defined.
Volumes are in mm^3.
"""

import math

import pytest

pytestmark = pytest.mark.solidworks

STADIUM = [{"line": [40, 0]}, {"arc": [40, 20], "tangent": True}, {"line": [0, 20]}, {"arc": [0, 0], "tangent": True}]


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def box(result):
    return result["mass_properties"]["bounding_box_mm"]


def segment_area(radius, chord):
    """The area between a chord and its arc (the smaller side)."""
    d = math.sqrt(radius ** 2 - (chord / 2) ** 2)
    return radius ** 2 * math.acos(d / radius) - d * chord / 2


def test_a_stadium_is_two_dimensions_and_stays_a_stadium(part):
    """Tangent arcs make half circles on a 40 x 20 rectangle; a taller stadium
    keeps half circles, because the tangents hold, not the coordinates."""
    sketch = part.add_sketch("front", [0, 0], STADIUM)
    assert sketch["fully_defined"] and sketch["closed"]
    assert set(sketch["dimensions"]) == {"x1", "y2"}
    slab = part.extrude_sketch(sketch["sketch"], 10)
    assert vol(slab) == pytest.approx((40 * 20 + math.pi * 10 ** 2) * 10)
    assert box(slab)["min_mm"][0] == pytest.approx(-10, abs=1e-3) and box(slab)["max_mm"][0] == pytest.approx(50, abs=1e-3), \
        "the ends should bulge outwards"
    taller = part.set_dimension(sketch["dimensions"]["y2"], 30)
    assert vol(taller) == pytest.approx((40 * 30 + math.pi * 15 ** 2) * 10), \
        "the ends should stay half circles tangent to the sides"


def test_a_free_arc_keeps_its_ends_when_its_radius_changes(part):
    """An arc through (45, 10) from (40, 0) to (40, 20) has R12.5 and bulges 5 mm;
    at R15 the same chord holds a flatter segment. A spline closes the top."""
    sketch = part.add_sketch("front", [0, 0], [{"line": [40, 0]}, {"arc": [40, 20], "through": [45, 10]},
                                               {"spline": [[20, 25], [0, 20]]}, {"line": [0, 0]}])
    assert sketch["fully_defined"] and sketch["points_mm"][3] == [20, 25]
    before = vol(part.extrude_sketch(sketch["sketch"], 5))
    after = vol(part.set_dimension(sketch["dimensions"]["r1"], 15))
    assert before - after == pytest.approx((segment_area(12.5, 20) - segment_area(15, 20)) * 5, rel=1e-6), \
        "only the arc's bulge should change: its ends and the spline stay put"


def test_a_sketch_on_the_top_plane_follows_its_frame(part):
    """On the Top plane u runs along x and v along -z; the extrusion goes up +y."""
    sketch = part.add_sketch("top", [0, 0], [{"line": [40, 0]}, {"line": [40, 20]}, {"line": [0, 20]},
                                             {"line": [0, 0]}], name="Footprint")
    assert sketch["sketch"] == "Footprint" and sketch["dimensions"]["x1"].endswith("@Footprint")
    assert sketch["x_axis"] == [1, 0, 0] and sketch["y_axis"] == [0, 0, -1]
    block = part.extrude_sketch("Footprint", 5)
    assert box(block)["min_mm"] == pytest.approx([0, 0, -20]) and box(block)["max_mm"] == pytest.approx([40, 5, 0])


def test_an_s_bend_is_two_quarter_rings_between_straight_bars(part):
    """A bar 10 wide bends left round (30, 20) and right round (70, 20): quarter
    rings of R10-R20 and R20-R30 between two 30 x 10 bars, so the area is
    600 + 200 pi. Every bend flows on from the segment before."""
    s_bend = [{"line": [30, 0]}, {"arc": [50, 20], "tangent": True}, {"arc": [70, 40], "tangent": True},
              {"line": [100, 40]}, {"line": [100, 50]}, {"line": [70, 50]}, {"arc": [40, 20], "tangent": True},
              {"arc": [30, 10], "tangent": True}, {"line": [0, 10]}, {"line": [0, 0]}]
    sketch = part.add_sketch("front", [0, 0], s_bend)
    assert sketch["fully_defined"] and sketch["closed"]
    leg = part.extrude_sketch(sketch["sketch"], 5)
    assert vol(leg) == pytest.approx((600 + 200 * math.pi) * 5)

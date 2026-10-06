"""Unit tests for region_tools: the outline of what a set of sections covers.

Pure Python, no SolidWorks. Areas in mm^2; a 0.1 mm grid puts an outline up to
about 0.1 mm off, so areas are compared within its perimeter times that.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.region_tools import swept_outline


def square(x0, y0, size):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


def test_overlapping_sections_make_one_region():
    # two 10 x 10 squares overlapping in a 5 x 5 corner: 175 mm^2
    regions = swept_outline([[square(0, 0, 10)], [square(5, 5, 10)]], 0.1)
    assert len(regions) == 1
    assert regions[0]["area_mm2"] == pytest.approx(175, abs=60 * 0.1)


def test_apart_sections_make_two_regions_largest_first():
    regions = swept_outline([[square(0, 0, 10)], [square(30, 0, 5)]], 0.1)
    assert [round(r["area_mm2"]) for r in regions] == [100, 25]


def test_sections_touching_at_one_corner_stay_two_regions():
    # their outline would otherwise cross itself at the corner, a figure eight
    regions = swept_outline([[square(0, 0, 10)], [square(10, 10, 10)]], 0.1)
    assert [r["area_mm2"] for r in regions] == pytest.approx([100, 100], abs=4)


def test_a_hole_in_a_section_stays_a_hole():
    # a 20 x 20 plate with a 6 x 6 hole: the section has the outline and the hole
    regions = swept_outline([[square(0, 0, 20), square(7, 7, 6)]], 0.1)
    [plate] = regions
    assert len(plate["holes_mm"]) == 1
    assert plate["area_mm2"] == pytest.approx(400 - 36, abs=104 * 0.1)


def test_a_margin_grows_the_region_with_round_corners():
    # a 10 x 10 square 1 mm larger all round: 100 + 4 x 10 x 1 + pi x 1^2
    [grown] = swept_outline([[square(0, 0, 10)]], 0.1, margin_mm=1)
    assert grown["area_mm2"] == pytest.approx(100 + 40 + math.pi, abs=50 * 0.1)


def test_the_outline_runs_round_the_region_close_to_its_edge():
    """Ordered, counter-clockwise and on the edge within about the grid's
    size: ready to give add_sketch as a spline."""
    [region] = swept_outline([[square(0, 0, 10)]], 0.1)
    points = region["outline_mm"]
    signed = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2
    assert signed > 0, "the outline runs clockwise"
    assert all(min(abs(x), abs(x - 10)) <= 0.15 or min(abs(y), abs(y - 10)) <= 0.15 for x, y in points), \
        "a point of the outline lies off the square's edge"
    assert len(points) < 40, f"{len(points)} points for a square: the grid's steps were not smoothed out"


def test_nothing_to_outline_is_refused():
    with pytest.raises(SolidWorksError, match="no section"):
        swept_outline([], 0.1)

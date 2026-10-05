"""Integration tests for exact placement: points just off an axis stay where they were put.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import pytest

pytestmark = pytest.mark.solidworks


def hole_centres(part):
    return [f["cylinder"]["point_mm"] for f in part.list_faces()["faces"] if "cylinder" in f]


def plate(part):
    part.add_extruded_profile([[0, -10], [120, -10], [120, 10], [0, 10]], 10)


TOOLS = {  # each makes one round face; the disc stands alone, a plate would swallow it
    "hole on a face": lambda part, y: (plate(part), part.add_hole_on_face(3, "+z", 97.59, y, 10))[1],
    "hole through": lambda part, y: (plate(part), part.add_hole(3, 97.59, y))[1],
    "disc": lambda part, y: part.add_disc(3, 4, x_mm=97.59, y_mm=y),
}


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("y", [-0.75, 0.3], ids=["below", "above"])
def test_a_circle_just_off_an_axis_stays_where_it_was_put(part, tool, y):
    """A circle 0.75 mm below (0.3 above) the x axis, far out along it:
    SolidWorks' sketch inference must not snap its centre onto the axis."""
    circle = TOOLS[tool](part, y)
    assert {"x", "y"} <= set(circle["dimensions"]), "the centre needs an x and a y dimension"
    [centre] = hole_centres(part)
    assert centre[:2] == pytest.approx([97.59, y], abs=1e-6), "the circle should sit where it was asked"

"""Integration tests for exact placement: points just off an axis stay where they were put.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import pytest

pytestmark = pytest.mark.solidworks


@pytest.mark.parametrize("y", [-0.75, 0.3], ids=["below", "above"])
def test_a_circle_just_off_an_axis_stays_where_it_was_put(part, y):
    """A circle 0.75 mm below (0.3 above) the x axis, far out along it:
    SolidWorks' sketch inference must not snap its centre onto the axis. Each
    tool makes one round face; the disc stands alone, past the plate's end,
    as the plate would swallow it."""
    part.add_extruded_profile([[0, -10], [120, -10], [120, 10], [0, 10]], 10)
    made = {
        97.59: ("hole on a face", part.add_hole_on_face(3, "+z", 97.59, y, 10)),
        77.59: ("hole through", part.add_hole(3, 77.59, y)),
        147.59: ("disc", part.add_disc(3, 4, x_mm=147.59, y_mm=y)),
    }
    for tool, circle in made.values():
        assert {"x", "y"} <= set(circle["dimensions"]), f"the {tool}'s centre needs an x and a y dimension"
    centres = {round(f["cylinder"]["point_mm"][0], 2): f["cylinder"]["point_mm"]
               for f in part.list_faces()["faces"] if "cylinder" in f}  # the plate's body: its holes
    [disc] = [b["bounding_box_mm"] for b in part.list_bodies()["bodies"] if b["bounding_box_mm"]["min_mm"][0] > 120]
    centres[147.59] = [(lo + hi) / 2 for lo, hi in zip(disc["min_mm"], disc["max_mm"])]
    assert sorted(centres) == sorted(made), centres
    for x, (tool, _) in made.items():
        assert centres[x][:2] == pytest.approx([x, y], abs=1e-6), f"the {tool} should sit where it was asked"

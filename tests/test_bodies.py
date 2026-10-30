"""Integration tests for parts of several bodies: separate, combined, split.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The block is 40 x 20 x 10; the second body is 20 x 10 x 10 at x 30..50, y 5..15,
so it overlaps the block by 10 x 10 x 10. Volumes are in mm^3.
"""

import pytest

from sw_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK, TAB, OVERLAP = 40 * 20 * 10, 20 * 10 * 10, 10 * 10 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def two_bodies(part):
    block = part.add_extruded_profile([[0, 0], [40, 0], [40, 20], [0, 20]], 10, name="Block")
    tab = part.add_extruded_profile([[30, 5], [50, 5], [50, 15], [30, 15]], 10, name="Tab", merge=False)
    return block, tab


@pytest.mark.parametrize("operation,expected", [("add", BLOCK + TAB - OVERLAP), ("subtract", BLOCK - OVERLAP),
                                                ("common", OVERLAP)])
def test_combine_bodies(part, operation, expected):
    _, tab = two_bodies(part)
    # merge=False keeps a body of its own
    bodies = {b["name"]: b for b in part.list_bodies()["bodies"]}
    assert len(bodies) == 2 and tab["body"] in bodies
    assert bodies[tab["body"]]["volume_mm3"] == pytest.approx(TAB)
    assert bodies[tab["body"]]["bounding_box_mm"]["min_mm"] == pytest.approx([30, 5, 0])
    assert vol(tab) == pytest.approx(BLOCK + TAB), "separate bodies count apart, overlap and all"
    with pytest.raises(SolidWorksError, match="No body 'Nope'"):
        part.combine_bodies(operation, "Nope")

    [main] = [name for name in bodies if name != tab["body"]]
    combined = part.combine_bodies(operation, main)
    assert vol(combined) == pytest.approx(expected) and len(combined["bodies"]) == 1


def test_split_body_along_a_plane(part):
    """Split at z = 4: the block falls apart into 3200 and 4800, nothing lost."""
    part.add_box(40, 20, 10)
    beyond = part.add_plane("front", offset_mm=25)["plane"]
    with pytest.raises(SolidWorksError, match="does not cut through the part"):
        part.split_body(beyond)
    cut_at = part.add_plane("front", offset_mm=4)["plane"]
    split = part.split_body(cut_at)
    volumes = sorted(b["volume_mm3"] for b in part.list_bodies()["bodies"])
    assert volumes == pytest.approx([40 * 20 * 4, 40 * 20 * 6]) and vol(split) == pytest.approx(BLOCK)
    assert len(split["bodies"]) == 2

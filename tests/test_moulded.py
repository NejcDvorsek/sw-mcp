"""Integration tests for the fillets of a moulded part: variable radius and full round.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
A fillet of radius r on a right-angled edge takes r^2 (1 - pi/4) off its
cross-section; a radius running linearly from r1 to r2 along an edge of length
L takes (1 - pi/4) L (r1^2 + r1 r2 + r2^2) / 3. Volumes are in mm^3.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

BLOCK = 40 * 20 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def taken_off(length, r1, r2):
    return (1 - math.pi / 4) * length * (r1 ** 2 + r1 * r2 + r2 ** 2) / 3


def edge_between(part, a, b):
    """The list_edges index of the edge from a to b, either way round."""
    [index] = [e["index"] for e in part.list_edges()["edges"]
               if sorted(map(tuple, e.get("ends_mm", []))) == sorted([tuple(a), tuple(b)])]
    return str(index)


def edge_ends(part):
    return [sorted(map(tuple, e["ends_mm"])) for e in part.list_edges()["edges"] if "ends_mm" in e]


def test_a_variable_fillet_grows_along_its_edge(part):
    """The top front edge of the block: R2 at x = 0, growing to R5 at x = 40."""
    part.add_box(40, 20, 10)
    fillet = part.add_fillet(2, edge_between(part, [0, 0, 10], [40, 0, 10]), radii_at_mm=[[40, 0, 10, 5]])
    assert vol(fillet) == pytest.approx(BLOCK - taken_off(40, 2, 5), rel=1e-5), \
        "the radius should run linearly from 2 at one end to 5 at the other"
    ends = {tuple(v["at_mm"]): v for v in fillet["vertex_radii"]}
    assert ends[(0, 0, 10)]["radius_mm"] == 2 and ends[(40, 0, 10)]["radius_mm"] == 5
    wider = part.set_dimension(ends[(0, 0, 10)]["dimension"], 3)
    assert vol(wider) == pytest.approx(BLOCK - taken_off(40, 3, 5), rel=1e-5), \
        "each end's radius is a dimension of its own: the x = 0 end should now be R3"


def test_a_variable_fillet_over_two_edges_puts_each_radius_at_its_corner(part):
    """Two top edges meeting at (0, 0, 10): R4 at the far end of one, R6 at the
    far end of the other. Each end cap of the fillet runs from the edge's end
    r down to r along the face."""
    part.add_box(40, 20, 10)
    front = edge_between(part, [0, 0, 10], [40, 0, 10])
    left = edge_between(part, [0, 0, 10], [0, 20, 10])
    part.add_fillet(2, f"{front},{left}", radii_at_mm=[[40, 0, 10, 4], [0, 20, 10, 6]])
    ends = edge_ends(part)
    assert sorted([(40, 0, 6), (40, 4, 10)]) in ends, "R4 belongs at the x = 40 end"
    assert sorted([(0, 20, 4), (6, 20, 10)]) in ends, "R6 belongs at the y = 20 end"


def test_a_variable_fillet_needs_its_points_at_the_edge_ends(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError, match=r"No end of the edges at \(20, 0, 10\)"):
        part.add_fillet(2, edge_between(part, [0, 0, 10], [40, 0, 10]), radii_at_mm=[[20, 0, 10, 5]])
    assert part.get_mass_properties()["mass_properties"]["volume_mm3"] == pytest.approx(BLOCK)

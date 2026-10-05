"""Integration tests for equations: list, replace, batch, delete, and what fails.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The block is 40 x 20 x 10, so its volume tells the depth: 800 mm^3 per mm.
"""

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def by_name(part):
    return {e["name"]: e for e in part.list_equations()["equations"]}


def test_an_equation_with_the_same_name_is_replaced(part):
    """"L" = 85 drives the depth; setting "L" again changes it, in place."""
    block = part.add_box(40, 20, 10)
    first = part.set_equation('"L" = 85')
    part.set_equation(f'"{block["dimensions"]["depth"]}" = "L" / 5')
    again = part.set_equation('"L" = 100')
    assert again["replaced"] and again["index"] == first["index"]
    assert vol(again) == pytest.approx(40 * 20 * 20), "the depth should follow L: 100 / 5"
    assert by_name(part)["L"]["value"] == 100 and len(by_name(part)) == 2


def test_a_list_of_equations_goes_in_at_once(part):
    block = part.add_box(40, 20, 10)
    dims = block["dimensions"]
    done = part.set_equations(['"T" = 4', f'"{dims["depth"]}" = "T" * 3', f'"{dims["width"]}" = "T" * 10'])
    assert [e["replaced"] for e in done["equations"]] == [False, False, False]
    assert vol(done) == pytest.approx(40 * 20 * 12)
    redone = part.set_equations(['"t" = 5'])  # SolidWorks takes global names in any case
    assert redone["equations"][0]["replaced"] and vol(redone) == pytest.approx(50 * 20 * 15)


def test_an_equation_left_behind_by_a_deleted_feature_shows_and_goes(part):
    part.add_box(40, 20, 10)
    part.add_disc(10, 5, x_mm=20, y_mm=10, name="Puck")
    part.set_equation('"W" = 7')
    part.set_equation('"D1@Puck" = "W"')
    part.delete_feature("Puck")
    listed = by_name(part)
    assert listed["D1@Puck"]["broken"] and not listed["W"]["broken"]
    gone = part.delete_equation("D1@Puck")
    assert "D1@Puck" not in by_name(part) and gone["deleted"] == '"D1@Puck" = "W"'


def test_a_refused_equation_leaves_the_others_as_they_were(part):
    block = part.add_box(40, 20, 10)
    part.set_equations(['"L" = 85', f'"{block["dimensions"]["depth"]}" = "L" / 5'])
    with pytest.raises(SolidWorksError, match=r'refused "M" = 3 \+\* 4'):
        part.set_equations(['"L" = 50', '"M" = 3 +* 4'])
    with pytest.raises(SolidWorksError, match=r'refused "L" = 3 \+\* 4'):  # SolidWorks keeps the old one, silently
        part.set_equations(['"L" = 3 +* 4'])
    assert by_name(part)["L"]["equation"] == '"L" = 85' and "M" not in by_name(part)
    part.rebuild()
    assert vol(part.get_mass_properties()) == pytest.approx(40 * 20 * 17), "the part should be as it was"


def test_an_equation_that_breaks_a_feature_names_it(part):
    """R25 on the vertical edges of a 40 x 20 x 10 block fails to rebuild
    (SolidWorks still builds overlapping fillets up to R15 there, measured)."""
    block = part.add_box(40, 20, 10)
    part.add_fillet(2, "z", name="Round")
    [radius] = [d["name"] for d in part.list_dimensions()["dimensions"] if d["feature"] == "Round"]
    part.set_equations(['"R" = 2', f'"{radius}" = "R"'])
    broken = part.set_equation('"R" = 25')
    assert [f["name"] for f in broken["failing_features"]] == ["Round"] and not broken["rebuild_ok"]
    fixed = part.set_equation('"R" = 3')
    assert fixed["failing_features"] == [] and fixed["rebuild_ok"]

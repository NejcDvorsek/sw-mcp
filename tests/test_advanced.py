"""Axes anywhere, patterns about them, planes by normal, lofts between named
sketches, fixed sketches and run_script.

The pure helpers run everywhere; the rest is marked `solidworks`.
"""

import math
import os

import pytest

from sw_mcp.advanced import json_safe, unit
from sw_mcp.errors import SolidWorksError


def volume(result):
    return result["mass_properties"]["volume_mm3"]


def square(half):
    return [{"line": [half, -half]}, {"line": [half, half]}, {"line": [-half, half]}, {"line": [-half, -half]}]


# --- pure ------------------------------------------------------------------------


def test_a_direction_is_scaled_to_one():
    assert unit([0, 3, 4]) == pytest.approx((0.0, 0.6, 0.8))
    with pytest.raises(SolidWorksError, match="cannot be zero"):
        unit([0, 0, 0])
    with pytest.raises(SolidWorksError, match=r"\[dx, dy, dz\]"):
        unit([1, 2])


def test_a_script_result_comes_back_as_json():
    assert json_safe({"a": (1, 2.5), "b": {3}}) == {"a": [1, 2.5], "b": [3]}
    assert json_safe(float("nan")) == "nan"
    assert json_safe(object()).startswith("<object")


# --- SolidWorks -------------------------------------------------------------------


@pytest.mark.solidworks
def test_a_revolved_part_is_patterned_about_its_own_axis(part):
    """Revolves turn about y; a pattern about a hole's cylinder could not."""
    part.add_cylinder(40, 10)
    part.add_hole_on_face(4, "+y", 15, 10, 0)
    pattern = part.add_circular_pattern(6, axis="y")
    assert pattern["axis"] == "AxisY"
    assert volume(pattern) == pytest.approx(math.pi * 20 ** 2 * 10 - 6 * math.pi * 2 ** 2 * 10, rel=1e-6)


@pytest.mark.solidworks
def test_a_pattern_can_spread_over_part_of_a_turn(part):
    part.add_cylinder(40, 10)
    part.add_hole_on_face(4, "+y", 15, 10, 0)
    pattern = part.add_circular_pattern(3, axis="y", angle_deg=90)
    assert volume(pattern) == pytest.approx(math.pi * 20 ** 2 * 10 - 3 * math.pi * 2 ** 2 * 10, rel=1e-6)


@pytest.mark.solidworks
def test_axes_by_point_and_direction_or_by_two_planes(part):
    part.add_box(40, 20, 10)
    slanted = part.add_axis(through=[[0, 5, 0], [1, 1, 0]], name="Slanted")
    assert slanted["axis"] == "Slanted"
    assert slanted["direction"] == pytest.approx([math.sqrt(0.5), math.sqrt(0.5), 0.0])
    crossing = part.add_axis(planes=["front", "right"])
    assert [abs(c) for c in crossing["direction"]] == pytest.approx([0.0, 1.0, 0.0])
    with pytest.raises(SolidWorksError, match="No axis 'Nope'"):
        part.add_circular_pattern(3, axis="Nope")


@pytest.mark.solidworks
def test_a_plane_goes_through_a_point_square_to_a_direction(part):
    part.add_box(40, 20, 10)
    plane = part.add_plane(normal=[1, 0, 1], through=[0, 5, 0], name="Tilted")
    assert plane["plane"] == "Tilted"
    assert plane["origin_mm"] == pytest.approx([0, 5, 0])
    assert [abs(c) for c in plane["normal"]] == pytest.approx([math.sqrt(0.5), 0.0, math.sqrt(0.5)])
    fixed = part.add_sketch("Tilted", [0, 0], [{"line": [5, 0]}, {"line": [5, 5]}, {"line": [0, 0]}], fixed=True)
    assert fixed["fully_defined"] and fixed["dimensions"] == {}


@pytest.mark.solidworks
def test_a_loft_and_a_loft_cut_between_named_sketches(part):
    part.add_sketch("front", [-10, -10], square(10), name="Base")
    part.add_plane("front", offset_mm=20, name="P20")
    part.add_sketch("P20", [-5, -5], square(5), name="Top")
    loft = part.add_loft(["Base", "Top"], name="Frustum")
    frustum = 20 / 3 * (400 + 100 + math.sqrt(400 * 100))
    assert loft["feature"] == "Frustum" and volume(loft) == pytest.approx(frustum, rel=1e-6)
    part.add_plane("front", offset_mm=5, name="P5")
    part.add_plane("front", offset_mm=15, name="P15")
    part.add_sketch("P5", [-3, -3], square(3), name="C1")
    part.add_sketch("P15", [-2, -2], square(2), name="C2")
    cut = part.add_loft(["C1", "C2"], cut=True)
    assert volume(cut) == pytest.approx(frustum - 10 / 3 * (36 + 16 + math.sqrt(36 * 16)), rel=1e-6)


@pytest.mark.solidworks
def test_a_loft_needs_sketches(part):
    part.add_box(10, 10, 10)
    with pytest.raises(SolidWorksError, match="No sketch Nope"):
        part.add_loft(["Nope", "Sketch1"])
    with pytest.raises(SolidWorksError, match="two or more"):
        part.add_loft(["Sketch1"])


@pytest.mark.solidworks
def test_a_script_runs_and_reports(part):
    part.add_box(10, 10, 10)
    done = part.run_script("print('hi')\nresult = {'title': model.GetTitle(), 'v': session.get_mass_properties()"
                           "['mass_properties']['volume_mm3']}")
    assert done["output"] == "hi\n" and done["result"]["v"] == pytest.approx(1000)
    assert done["mass_properties"]["volume_mm3"] == pytest.approx(1000)


@pytest.mark.solidworks
def test_a_failing_script_is_rolled_back(part):
    part.add_box(10, 10, 10)
    before = part.list_features()["features"]
    with pytest.raises(SolidWorksError, match="ValueError: boom"):
        part.run_guarded(part.run_script, "session.add_box(5, 5, 5)\nraise ValueError('boom')")
    assert part.list_features()["features"] == before


def test_scripts_are_on_unless_the_server_turns_them_off(monkeypatch):
    import asyncio

    from sw_mcp import server

    if not os.environ.get("SOLIDWORKS_MCP_ALLOW_SCRIPTS"):
        assert server._ALLOW_SCRIPTS
    monkeypatch.setattr(server, "_ALLOW_SCRIPTS", False)
    refused = asyncio.run(server.run_script("result = 1"))
    assert not refused["ok"] and "--no-scripts" in refused["error"]

"""Integration tests: sheet metal flanges, the flat pattern and its drawing,
each against a hand calculation.

A bend of inner radius r in a sheet of thickness t keeps the length of its
neutral fibre, at r + K t from the inside: pi / 2 (r + K t) for a square bend.
Straight legs end where the bend starts, r + t from the outer corner. Volumes
are in mm^3.
"""

import math
import os

import pytest

from sw_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

T, R = 2.0, 3.0                    # sheet thickness, inner bend radius
TAB = [[0, 0], [50, 0], [50, 30], [0, 30], [0, 0]]
L_SECTION = [[0, 0], [50, 0], [50, 30]]


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def bend(k=0.5):
    """Flat length of a square bend."""
    return math.pi / 2 * (R + k * T)


def box(part):
    return part.get_bounding_box()["bounding_box_mm"]


def test_a_closed_profile_is_a_flat_tab_along_plus_z(part):
    result = part.add_base_flange(T, R, profile_mm=TAB)
    assert vol(result) == pytest.approx(50 * 30 * T, abs=0.01)
    assert box(part)["min_mm"] == pytest.approx([0, 0, 0], abs=1e-6)
    assert box(part)["max_mm"] == pytest.approx([50, 30, T], abs=1e-6)
    assert result["sheet_metal"] == {"thickness_mm": T, "bend_radius_mm": R, "k_factor": 0.5}


def test_an_open_profile_is_bent_with_the_thickness_on_its_right(part):
    """The L walked from (0, 0): its right is -y along the bottom, +x up the side."""
    result = part.add_base_flange(T, R, profile_mm=L_SECTION, depth_mm=20)
    developed = (50 - R) + (30 - R) + math.pi / 2 * (R + T / 2)  # the middle of the sheet
    assert vol(result) == pytest.approx(developed * T * 20, abs=0.01)
    assert box(part)["min_mm"] == pytest.approx([0, -T, 0], abs=1e-6)
    assert box(part)["max_mm"] == pytest.approx([50 + T, 30, 20], abs=1e-6)
    assert result["dimensions"]["depth"].startswith("D")


def test_the_k_factor_sets_the_flat_length(part):
    part.add_base_flange(T, R, profile_mm=L_SECTION, depth_mm=20, k_factor=0.4)
    info = part.flat_pattern("info")
    flat = (50 - R) + (30 - R) + bend(0.4)
    assert info["bends"] == 1
    assert info["k_factor"] == pytest.approx(0.4)
    assert info["flat_size_mm"] == pytest.approx([flat, 20], abs=1e-3)


def test_an_edge_flange_rises_from_the_top_face_its_length_to_the_outside(part):
    part.add_base_flange(T, R, profile_mm=TAB)
    result = part.add_edge_flange([25, 30, T], 20)
    assert box(part)["max_mm"] == pytest.approx([50, 30, 20], abs=1e-6)  # material inside: still 30 deep
    flat = (30 - (R + T)) + (20 - (R + T)) + bend()
    assert vol(result) == pytest.approx(flat * 50 * T, abs=0.01)
    assert set(result["dimensions"]) == {"length", "angle"}
    assert part.flat_pattern("info")["flat_size_mm"] == pytest.approx([50, flat], abs=1e-3)


def test_a_flipped_edge_flange_folds_down(part):
    part.add_base_flange(T, R, profile_mm=TAB)
    part.add_edge_flange([25, 30, T], 20, flip=True)
    assert box(part)["min_mm"][2] == pytest.approx(T - 20, abs=1e-6)


def test_the_flange_length_is_a_dimension(part):
    part.add_base_flange(T, R, profile_mm=TAB)
    dims = part.add_edge_flange([25, 30, T], 20)["dimensions"]
    part.set_dimension(dims["length"], 35)
    assert box(part)["max_mm"][2] == pytest.approx(35, abs=1e-6)


def test_bend_outside_moves_the_flange_past_the_edge(part):
    part.add_base_flange(T, R, profile_mm=TAB)
    part.add_edge_flange([25, 30, T], 20, position="bend_outside")
    assert box(part)["max_mm"][1] == pytest.approx(30 + R + T, abs=1e-6)


def test_an_edge_flange_needs_a_straight_edge_through_the_point(part):
    part.add_base_flange(T, R, profile_mm=TAB)
    with pytest.raises(SolidWorksError, match="No edge goes through"):
        part.add_edge_flange([25, 15, T], 20)


def test_a_part_that_is_not_sheet_metal_is_refused(part):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError, match="not sheet metal"):
        part.add_edge_flange([20, 20, 10], 10)
    with pytest.raises(SolidWorksError, match="not sheet metal"):
        part.flat_pattern("info")


def test_flatten_and_fold(part):
    part.add_base_flange(T, R, profile_mm=L_SECTION, depth_mm=20)
    flat = part.flat_pattern("flatten")
    assert flat["flat"] is True
    assert sorted(flat["bounding_box_mm"]["size_mm"]) == pytest.approx(sorted([T, 20, flat["flat_size_mm"][0]]),
                                                                       abs=1e-3)
    part.flat_pattern("fold")
    assert box(part)["max_mm"] == pytest.approx([50 + T, 30, 20], abs=1e-6)


def test_the_flat_pattern_exports_to_dxf(part, tmp_path):
    part.add_base_flange(T, R, profile_mm=TAB)
    part.add_edge_flange([25, 30, T], 20)
    part.save_part(str(tmp_path / "tab.sldprt"))
    result = part.flat_pattern("export", str(tmp_path / "tab.dxf"))
    assert result["bytes"] > 0
    text = open(result["path"], encoding="latin-1").read()
    assert "LINE" in text


def test_make_drawing_shows_the_flat_pattern_dimensioned(part, tmp_path):
    part.add_base_flange(T, R, profile_mm=[[0, 0], [80, 0], [80, 50], [0, 50], [0, 0]])
    part.add_edge_flange([40, 50, T], 20)
    part.add_edge_flange([40, 0, T], 20)
    part.save_part(str(tmp_path / "tray.sldprt"))
    result = part.make_drawing(str(tmp_path / "tray.pdf"), "A4", keep_open=True)
    try:
        assert result["flat_pattern"] in result["views"]
        assert result["flat_dimensions"] == 2
        flat = 50 - 2 * (R + T) + 2 * (20 - (R + T)) + 2 * bend()
        values = sorted(round(a["value"], 2) for a in part.list_annotations(result["flat_pattern"])["annotations"]
                        if a["type"] == "dimension")
        assert values == sorted([80.0, round(flat, 2)])
        kinds = {p["kind"] for p in result["problems"]}
        assert "text_on_view" not in kinds  # bend notes belong on their bend lines
    finally:
        part.close_drawing()
    assert os.path.getsize(result["path"]) > 0

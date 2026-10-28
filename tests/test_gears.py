"""Pure unit tests for the gear data of a drawing's gear data table."""

import math

import pytest

from solidworks_mcp import gears
from solidworks_mcp.errors import SolidWorksError


def test_a_spur_gear_against_the_tables():
    data = gears.spur(20, 2, face_width=15, mating_teeth=40)
    assert data["pitch_diameter"] == 40 and data["tip_diameter"] == 44 and data["root_diameter"] == 35
    assert data["base_diameter"] == pytest.approx(37.5877, abs=1e-4)
    # span over 3 teeth: 7.6604 mm per mm of module in the usual tables (z = 20, 20 deg)
    assert data["span_teeth"] == 3 and data["span"] == pytest.approx(2 * 7.6604, abs=1e-3)
    assert data["center_distance"] == 60


def test_a_profile_shift_moves_tip_root_and_span():
    plain, shifted = gears.spur(12, 1), gears.spur(12, 1, profile_shift=0.3)
    assert shifted["tip_diameter"] - plain["tip_diameter"] == pytest.approx(0.6)
    assert shifted["root_diameter"] - plain["root_diameter"] == pytest.approx(0.6)
    assert shifted["span"] - plain["span"] == pytest.approx(2 * 0.3 * math.sin(math.radians(20)))


def test_a_bevel_pair_meets_at_the_shaft_angle():
    pinion = gears.bevel(17, 14, 36, face_width=80)
    gear = gears.bevel(36, 14, 17)
    assert pinion["pitch_angle"] + gear["pitch_angle"] == pytest.approx(90)
    assert pinion["pitch_angle"] == pytest.approx(math.degrees(math.atan(17 / 36)))
    assert pinion["outer_cone_distance"] == pytest.approx(gear["outer_cone_distance"])
    assert pinion["outer_cone_distance"] == pytest.approx(0.5 * 14 * math.hypot(17, 36))
    # constant clearance: the face angle exceeds the pitch angle by the mate's dedendum angle
    assert pinion["face_angle"] - pinion["pitch_angle"] == pytest.approx(gear["pitch_angle"] - gear["root_angle"])


def test_a_bevel_face_is_at_most_a_third_of_the_cone():
    with pytest.raises(SolidWorksError, match="third"):
        gears.bevel(17, 14, 36, face_width=100)


def test_bad_gear_input_is_refused():
    with pytest.raises(SolidWorksError, match="teeth"):
        gears.spur(0, 2)
    with pytest.raises(SolidWorksError, match="module"):
        gears.bevel(17, -1, 36)


def test_angles_read_in_degrees_minutes_seconds():
    assert gears.dms(25.27840) == "25° 16' 42\""
    assert gears.dms(-1.5) == "-1° 30' 00\""


def test_the_table_rows():
    rows = gears.table_rows(gears.spur(20, 2), accuracy="ISO 1328 class 8",
                            extra=[["Material", "42CrMo4"], ["Hardness", "HRC", "54-58"]])
    assert rows[0][0] == "GEAR DATA - SPUR GEAR"
    assert ["Number of teeth", "z", "20"] in rows and ["Module", "m", "2"] in rows
    assert rows[-3:] == [["Accuracy grade", "", "ISO 1328 class 8"], ["Material", "", "42CrMo4"],
                         ["Hardness", "HRC", "54-58"]]
    assert all(len(r) == 3 for r in rows)
    bevel = gears.table_rows(gears.bevel(17, 14, 36))
    assert bevel[0][0] == "GEAR DATA - STRAIGHT BEVEL GEAR"
    assert ["Module (outer)", "m", "14"] in bevel
    with pytest.raises(SolidWorksError, match="extra row"):
        gears.table_rows(gears.spur(20, 2), extra=[["only"]])

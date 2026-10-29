"""The merged tools pick the right session method and refuse stray arguments (no SolidWorks)."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.families import ToolFamilies


class Recorder(ToolFamilies):
    """Every session method a family routes to just records its call."""

    ROUTED = ("add_hole", "add_counterbore_hole", "add_hole_on_face", "add_hole_wizard",
              "add_extruded_profile", "add_extruded_profile_on_face", "add_extruded_profile_on_plane",
              "add_extruded_spline", "cut_profile", "cut_profile_on_face", "cut_profile_through_plane",
              "add_loft", "add_lofted_solid", "set_equation", "set_equations", "add_mate", "add_mates",
              "new_part", "new_assembly", "open_part", "open_assembly")

    def __init__(self):
        self.calls = []

    def __getattribute__(self, name):
        if name in type(self).ROUTED:
            return lambda *args: self.calls.append((name, args)) or {"ok": True, "via": name}
        return super().__getattribute__(name)


@pytest.fixture
def s():
    return Recorder()


def via(result):
    return result["via"]


def test_hole_variants(s):
    assert via(s.hole(10, 5, diameter_mm=4)) == "add_hole"
    assert via(s.hole(10, 5, 8, diameter_mm=4, face="+z", depth_mm=6)) == "add_hole_on_face"
    assert via(s.hole(10, 5, diameter_mm=6.6, cbore_diameter_mm=11, cbore_depth_mm=6.4)) == "add_counterbore_hole"
    assert via(s.hole(10, 5, 8, face="+z", iso="tapped", size="M6", thread="modeled")) == "add_hole_wizard"
    assert s.calls[1] == ("add_hole_on_face", (4, "+z", 10, 5, 8, 6, "Hole"))
    assert s.calls[3] == ("add_hole_wizard", ("tapped", "M6", "+z", 10, 5, 8, None, "normal", "modeled", None))


@pytest.mark.parametrize("kwargs,says", [
    ({"diameter_mm": 4, "depth_mm": 3}, "depth_mm"),           # blind needs a face
    ({"diameter_mm": 4, "face": "+z"}, "x, y, z"),
    ({"diameter_mm": 4, "size": "M4"}, "size"),               # size without iso
    ({"iso": "tapped", "size": "M6", "face": "+z", "z_mm": 8, "diameter_mm": 5}, "diameter_mm"),
    ({"iso": "tapped", "face": "+z", "z_mm": 8}, "size"),
    ({"diameter_mm": 4, "cbore_diameter_mm": 8}, "both"),
    ({"diameter_mm": 4, "cbore_diameter_mm": 8, "cbore_depth_mm": 4, "face": "-x"}, "face"),
    ({}, "diameter_mm"),
])
def test_hole_refuses(s, kwargs, says):
    with pytest.raises(SolidWorksError, match=says):
        s.hole(10, 5, **kwargs)
    assert s.calls == []


def test_extrusion_variants(s):
    square = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert via(s.extruded_profile(square, 5, draft_deg=2)) == "add_extruded_profile"
    assert via(s.extruded_profile(square, 5, spline=True)) == "add_extruded_spline"
    assert via(s.extruded_profile(square, 5, face="+z", corner_radii_mm=1)) == "add_extruded_profile_on_face"
    assert via(s.extruded_profile(square, 5, plane="Plane1", reverse=True)) == "add_extruded_profile_on_plane"
    assert [c[1][-1] for c in s.calls] == [True, "Spline", 1, True]  # merge, name, radii, merge
    assert s.calls[2][1][3] == "Boss"


@pytest.mark.parametrize("kwargs,says", [
    ({"reverse": True}, "reverse"),
    ({"face": "+z", "plane": "Plane1"}, "not both"),
    ({"face": "+z", "draft_deg": 3}, "draft_deg"),
    ({"face": "+z", "merge": False}, "merge"),
    ({"plane": "Plane1", "rotate_deg": 30}, "rotate_deg"),
    ({"spline": True, "corner_radii_mm": 2}, "corner_radii_mm"),
])
def test_extrusion_refuses(s, kwargs, says):
    with pytest.raises(SolidWorksError, match=says):
        s.extruded_profile([[0, 0], [1, 0], [1, 1]], 2, **kwargs)


def test_cut_variants(s):
    tri = [[0, 0], [5, 0], [0, 5]]
    assert via(s.profile_cut(tri, 2, rotate_deg=60)) == "cut_profile"
    assert via(s.profile_cut(tri, face="-x")) == "cut_profile_on_face"
    assert via(s.profile_cut(tri, plane="right", keep_inside=True)) == "cut_profile_through_plane"
    with pytest.raises(SolidWorksError, match="keep_inside"):
        s.profile_cut(tri, keep_inside=True)
    with pytest.raises(SolidWorksError, match="rotate_deg"):
        s.profile_cut(tri, face="-x", rotate_deg=10)


def test_loft_and_equations(s):
    assert via(s.loft(["Heel", "Toe"], cut=True)) == "add_loft"
    assert via(s.loft(profiles_mm=[[[0, 0], [1, 0], [0, 1]]] * 2, heights_mm=[0, 5])) == "add_lofted_solid"
    for bad in ({}, {"sketches": ["A"], "profiles_mm": []}, {"profiles_mm": [], "cut": True},
                {"profiles_mm": []}, {"sketches": ["A"], "heights_mm": [0]}):
        with pytest.raises(SolidWorksError):
            s.loft(**bad)
    assert via(s.equation('"W" = 40')) == "set_equation"
    assert via(s.equation(['"W" = 40', '"H" = "W" / 2'])) == "set_equations"


def test_mates(s):
    assert via(s.mate("Bolt-1", "#3", "Plate-1", "#12", "concentric")) == "add_mate"
    assert via(s.mate(mates=[{"comp_a": "A"}], stop_on_error=False)) == "add_mates"
    assert s.calls[1] == ("add_mates", ([{"comp_a": "A"}], False))
    with pytest.raises(SolidWorksError, match="comp_a"):
        s.mate("Bolt-1", mates=[{}])
    with pytest.raises(SolidWorksError, match="needs comp_a"):
        s.mate("Bolt-1", "#3")


def test_documents(s):
    assert via(s.new_document()) == "new_part"
    assert via(s.new_document("Assembly")) == "new_assembly"
    assert via(s.open_document("C:/x/plate.SLDPRT")) == "open_part"
    assert via(s.open_document("C:/x/robot.sldasm")) == "open_assembly"
    assert via(s.open_document("C:/x/robot.step", kind="assembly")) == "open_assembly"
    for bad in (lambda: s.new_document("drawing"), lambda: s.open_document("a.step", kind="sheet")):
        with pytest.raises(SolidWorksError, match="drawing_file"):
            bad()


def test_neutral_file_falls_back_to_an_assembly():
    class Importer(Recorder):
        ROUTED = tuple(n for n in Recorder.ROUTED if n != "open_part")

        def open_part(self, path):
            raise SolidWorksError("robot.step holds an assembly, not a part: open it with open_assembly.")

    s = Importer()
    assert via(s.open_document("robot.step")) == "open_assembly"

    class Broken(Importer):
        def open_part(self, path):
            raise SolidWorksError("File not found")

    with pytest.raises(SolidWorksError, match="not found"):
        Broken().open_document("missing.step")

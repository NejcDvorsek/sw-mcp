"""Integration tests for read_model: one read-only call for a whole part.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
Each test builds its part with the modelling tools, so every expected value
is a hand calculation. The geometry rules themselves are unit-tested without
SolidWorks in test_model_read.py.

The golden test reads scripts/m5_demo_bracket.py's bracket and compares it
with tests/fixtures/bracket.model.json. When read_model's output changes on
purpose, regenerate that file with SW_MCP_REGENERATE=1 and review the diff.
"""

import importlib.util
import json
import math
import os
from pathlib import Path

import pytest

from sw_mcp import binding

pytestmark = pytest.mark.solidworks

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "bracket.model.json"
POCKET = [[10, 10], [40, 10], [40, 30], [10, 30]]  # 30 x 20, cut 6 deep into a 10 thick block


def read(part, **kwargs):
    """read_model as the server runs it, which must not fail a section."""
    result = part.run_guarded(part.read_model, **kwargs)
    assert result["ok"] and result["schema"] == "sw-mcp/model@1"
    assert not [g for g in result["gaps"] if g["what"].startswith("the ")], f"a section failed: {result['gaps']}"
    [found] = result["parts"]
    return found


def test_four_wizard_counterbores_are_one_group(part):
    part.add_box(120, 60, 10)
    centres = [(20, 10), (105, 10), (20, 50), (105, 50)]
    for x, y in centres:
        made = part.add_hole_wizard("counterbore", "M8", "+z", x, y, 10)
    [hole] = read(part, sections=["holes"])["holes"]
    wizard = made["hole"]  # what the Hole Wizard took from its table
    assert hole["source"] == "wizard" and hole["confidence"] == "high"
    assert (hole["kind"], hole["standard"], hole["size"], hole["fit"]) == ("counterbore", "ISO", "M8", "normal")
    assert hole["diameter_mm"] == wizard["diameter_mm"]
    assert hole["counterbore"] == {"diameter_mm": wizard["cbore_diameter_mm"], "depth_mm": wizard["cbore_depth_mm"]}
    assert hole["through"] and hole["depth_mm"] == pytest.approx(10)
    assert hole["count"] == 4 and len(hole["features"]) == 4
    assert hole["centres_mm"] == sorted([[x, y, 10.0] for x, y in centres])
    assert hole["axis"] == [0, 0, -1] and hole["entrance"] == "+z"


def test_a_blind_cut_hole_is_measured_from_the_geometry(part):
    part.add_box(120, 60, 10)
    part.add_hole_on_face(6, "+z", 60, 30, 10, depth_mm=8)
    [hole] = read(part, sections=["holes"])["holes"]
    assert hole["source"] == "geometry" and hole["kind"] == "simple" and hole["confidence"] == "high"
    assert (hole["diameter_mm"], hole["depth_mm"], hole["through"]) == (6.0, 8.0, False)
    assert hole["centres_mm"] == [[60, 30, 10]] and hole["entrance"] == "+z"


def test_a_boss_is_not_a_hole(part):
    part.add_box(120, 60, 10)
    part.add_boss_on_face(20, "+z", 60, 30, 10, 10)
    found = read(part, sections=["holes", "radii"])
    assert found["holes"] == []
    assert found["internal_radii_mm"] == [], "a boss's round side is convex"


def test_a_pocket_has_four_sharp_vertical_corners_and_four_floor_edges(part):
    part.add_box(120, 60, 10)
    part.cut_profile(POCKET, 6, name="Pocket1")
    sharp = read(part, sections=["radii"])["sharp_internal_edges"]
    assert sharp == {"count": 8, "groups": [
        {"feature": "Pocket1", "direction": "x", "length_mm": 30.0, "count": 2},
        {"feature": "Pocket1", "direction": "y", "length_mm": 20.0, "count": 2},
        {"feature": "Pocket1", "direction": "z", "length_mm": 6.0, "count": 4}]}


def test_filleted_pocket_corners_are_internal_radii(part):
    part.add_box(120, 60, 10)
    part.cut_profile(POCKET, 6, name="Pocket1")
    part.add_fillet(3, edges="z", name="Fillet1")  # the block's corners too, but those are convex
    found = read(part, sections=["radii"])
    assert found["internal_radii_mm"] == [{"radius": 3.0, "feature": "Fillet1", "count": 4}]
    groups = found["sharp_internal_edges"]["groups"]
    assert not [g for g in groups if g["direction"] == "z"], "no sharp vertical corner is left"
    assert found["sharp_internal_edges"]["count"] == 8, "the floor's 4 lines and 4 arcs"


def test_holes_in_every_body(part):
    part.add_box(40, 20, 10)
    part.add_extruded_profile([[60, 0], [100, 0], [100, 20], [60, 20]], 10, merge=False)
    part.add_hole(6, 20, 10)
    part.add_hole(6, 80, 10)
    found = read(part, sections=["holes"])
    assert len(found["bodies"]) == 2
    bodies = {h["body"] for h in found["holes"]}
    assert len(bodies) == 2, f"a hole in each body: {found['holes']}"
    assert sorted(c[0] for h in found["holes"] for c in h["centres_mm"]) == [20, 80]


def test_a_thin_floor_is_one_thin_spot(part):
    part.add_box(40, 20, 10)
    part.cut_profile([[5, 5], [35, 5], [35, 15], [5, 15]], 9.2)
    walls = read(part, sections=["walls"], wall_mm=1.0)["wall_check"]
    assert walls["threshold_mm"] == 1.0 and walls["min_found_mm"] == pytest.approx(0.8, abs=1e-3)
    [spot] = walls["thin_spots"]
    assert spot["thickness_mm"] == pytest.approx(0.8, abs=1e-3) and 5 <= spot["point_mm"][0] <= 35


def test_a_drafted_extrude_has_its_draft(part):
    part.add_extruded_profile([[0, 0], [40, 0], [40, 20], [0, 20]], 10, draft_deg=2)
    draft = read(part, sections=["draft"], pull="+z")["draft_check"]
    assert draft["pull"] == "+z" and draft["min_draft_deg"] == pytest.approx(2, abs=1e-3)
    assert draft["faces_below_1deg"] == 0 and len(draft["faces"]) == 4 and draft["undercut_faces"] == []


def test_a_side_hole_is_an_undercut_for_a_vertical_pull(part):
    part.add_box(40, 20, 10)
    part.add_hole_on_face(4, "+x", 40, 10, 5)
    draft = read(part, sections=["draft"], pull="+z")["draft_check"]
    assert [f["face"] for f in draft["undercut_faces"]], "the hole's underside is shadowed by its top"


def test_a_tapped_hole_has_its_thread(part):
    part.add_box(120, 60, 10)
    part.add_hole_wizard("tapped", "M6", "+z", 60, 30, 10, depth_mm=8)
    [hole] = read(part, sections=["holes"])["holes"]
    assert hole["kind"] == "tapped" and hole["size"] == "M6" and not hole["through"]
    assert hole["thread"] == {"designation": "M6x1.0", "depth_mm": pytest.approx(8.0), "cosmetic": True}
    assert hole["diameter_mm"] == pytest.approx(5.0), "the tap drill"


def test_a_countersink_has_its_head(part):
    part.add_box(120, 60, 10)
    part.add_hole_wizard("countersink", "M5", "+z", 100, 30, 10)
    [hole] = read(part, sections=["holes"])["holes"]
    assert hole["kind"] == "countersink" and hole["countersink"]["angle_deg"] == pytest.approx(90)
    assert hole["countersink"]["diameter_mm"] > hole["diameter_mm"] and hole["through"]


def test_core_names_the_part_and_its_history(part):
    part.add_box(120, 60, 10)
    part.set_equation('"W" = 120')
    part.set_equation('"width@Sketch1" = "W"')
    found = read(part)
    assert found["volume_mm3"] == pytest.approx(72000) and found["bbox_mm"]["size"] == [120, 60, 10]
    assert [b["faces"] for b in found["bodies"]] == [6]
    assert [f["name"] for f in found["features"]] == ["Sketch1", "BlockExtrude"]
    assert {"id": "D1@BlockExtrude", "feature": "BlockExtrude", "value": 10.0, "unit": "mm", "driving": True,
            "tolerance": None} in found["dimensions"]
    assert {"name": "W", "value": 120.0, "expression": "120", "global": True} in found["equations"]
    assert found["under_defined_sketches"] == [] and found["material"] is None
    assert found["setup_directions"] == ["+z", "-z", "+y", "-y", "+x", "-x"]


def test_a_dimension_tolerance_and_the_material_are_read(part):
    part.add_box(120, 60, 10)
    part.set_material("6061 Alloy")
    dim = binding.wrap(part._model.Parameter("D1@BlockExtrude"), part._mod.IDimension)
    tolerance = binding.wrap(dim.Tolerance, part._mod.IDimensionTolerance)
    tolerance.Type = 2  # swTolBILAT
    tolerance.SetValues(-0.0001, 0.0002)  # m
    found = read(part, sections=["material"])
    [depth] = [d for d in found["dimensions"] if d["id"] == "D1@BlockExtrude"]
    assert depth["tolerance"] == {"type": "bilateral", "plus": 0.2, "minus": -0.1, "fit": None}
    assert found["material"]["name"] == "6061 Alloy" and found["material"]["density_kg_m3"] == pytest.approx(2700)


def test_reading_changes_nothing(part, tmp_path):
    part.add_box(120, 60, 10)
    part.cut_profile(POCKET, 6)
    part.add_hole_wizard("counterbore", "M8", "+z", 100, 30, 10)
    part.save_part(str(tmp_path / "read_only.sldprt"))
    model = part._model
    selection = binding.wrap(model.SelectionManager, part._mod.ISelectionMgr)

    def state():
        return (part.list_features()["features"], part.get_mass_properties()["mass_properties"],
                bool(model.GetSaveFlag()), selection.GetSelectedObjectCount2(-1))

    before = state()
    result = part.run_guarded(part.read_model, wall_mm=1.0, pull="+z")
    assert result["document"]["saved"] and result["sections"][-2:] == ["walls", "draft"]
    assert state() == before and before[2] is False


def test_a_part_reads_the_same_twice(part):
    part.add_box(120, 60, 10)
    part.add_hole_wizard("counterbore", "M8", "+z", 20, 10, 10)
    part.cut_profile(POCKET, 6)
    first, second = (_comparable(part.run_guarded(part.read_model, wall_mm=1.0, pull="+z")) for _ in range(2))
    assert first == second


# --- golden file --------------------------------------------------------------------

def _comparable(result) -> dict:
    """What must not change from run to run: not the timings, versions or the part's name and path."""
    [found] = result["parts"]
    found = {k: v for k, v in found.items() if k not in ("name", "path")}
    return {"sections": result["sections"], "part": found, "gaps": result["gaps"]}


def _same(got, want, where="") -> list:
    """Differences between two JSON values, numbers to 1e-3 mm (or 1e-6 relative)."""
    if isinstance(want, dict) and isinstance(got, dict):
        out = [f"{where}.{k}: missing" for k in want if k not in got]
        out += [f"{where}.{k}: unexpected" for k in got if k not in want]
        return out + [d for k in want if k in got for d in _same(got[k], want[k], f"{where}.{k}")]
    if isinstance(want, list) and isinstance(got, list):
        if len(got) != len(want):
            return [f"{where}: {len(got)} items, expected {len(want)}"]
        return [d for i, (g, w) in enumerate(zip(got, want)) for d in _same(g, w, f"{where}[{i}]")]
    if isinstance(want, float) or isinstance(got, float):
        if isinstance(got, (int, float)) and isinstance(want, (int, float)) and not isinstance(got, bool) \
                and math.isclose(got, want, rel_tol=1e-6, abs_tol=1e-3):
            return []
    elif got == want:
        return []
    return [f"{where}: {got!r}, expected {want!r}"]


def test_the_bracket_matches_its_golden_file(part):
    spec = importlib.util.spec_from_file_location("m5_demo_bracket", ROOT / "scripts" / "m5_demo_bracket.py")
    bracket = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bracket)
    bracket.build(part)
    got = _comparable(part.run_guarded(part.read_model, wall_mm=1.0, pull="+z"))
    if os.environ.get("SW_MCP_REGENERATE") == "1":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"regenerated {FIXTURE.name}: review its diff before committing")
    assert FIXTURE.exists(), f"no {FIXTURE.name}: run this test once with SW_MCP_REGENERATE=1"
    differences = _same(got, json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert not differences, "read_model's output changed:\n" + "\n".join(differences[:40])

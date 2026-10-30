"""Integration tests for drawings: make_drawing, the view, annotation and
title block tools, the checks, and build_drawing.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
The plate is built once and opened by each test; the annotation tools share
one drawing, each step checked as it goes.
"""

import os

import pytest

from sw_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks

HOLES = [(5, 5), (55, 5), (5, 35), (55, 35)]  # counterbores in the plate, model mm


@pytest.fixture(scope="module")
def plate_file(sw, tmp_path_factory):
    """60 x 40 x 10 with four identical M4 counterbores from the Hole Wizard."""
    sw.new_part()
    try:
        sw.add_box(60, 40, 10)
        for i, (x, y) in enumerate(HOLES, 1):
            sw.add_hole_wizard("counterbore", "M4", "+z", x, y, 10, name=f"CBore{i}")
        return sw.save_part(str(tmp_path_factory.mktemp("plate") / "plate.sldprt"))["path"]
    finally:
        sw.close_part()


def _close_drawings(session):
    while session._drawing is not None:
        try:
            session.close_drawing()
        except SolidWorksError:
            break


@pytest.fixture
def sheet(sw, plate_file):
    """The plate, open as the current part; with any drawing a test leaves
    open, it closes afterwards."""
    sw.open_part(plate_file)
    yield sw
    _close_drawings(sw)
    sw.close_part()


# --- make_drawing ---------------------------------------------------------------


def test_make_drawing_shows_every_dimension_once_and_closes_again(part, tmp_path):
    """Duplicated in two views, a dimension says twice what one view says. The
    drawing is saved and closed: it would stay open in SolidWorks, and keep
    the part loaded after close_part."""
    part.add_box(40, 20, 10)
    part.add_hole(6, 30, 10)
    with pytest.raises(SolidWorksError, match="Save the part first"):
        part.make_drawing(str(tmp_path / "unsaved.pdf"))
    part.save_part(str(tmp_path / "holed_box.sldprt"))
    try:
        drawing = part.make_drawing(str(tmp_path / "holed_box.pdf"))
        assert drawing["bytes"] > 1000
        assert drawing["dimensions"] == part.list_dimensions()["count"] == 6  # width height depth, hole d x y
        assert len(drawing["views"]) == 4  # front, top, side and isometric
        kept = part.make_drawing(str(tmp_path / "holed_box.slddrw"))
        assert kept["path"].lower().endswith(".slddrw") and kept["bytes"] > 1000
        documents = part.list_documents()["documents"]
        assert [d["title"] for d in documents if d["type"] == "drawing"] == [], "the drawing stayed open"
        assert [d["title"] for d in documents if d["current"]] == ["holed_box.SLDPRT"], "the part should be current again"
    finally:
        _close_drawings(part)


def test_identical_holes_get_one_counted_callout_and_the_title_block_is_filled(sheet, tmp_path):
    """Four counterbores: one '4X' callout, still linked to the model, and
    their centres dimensioned (5 and 55 across, 5 and 35 up). The title block
    shows material and mass."""
    sheet.set_material("AISI 304")
    sheet.save_part(str(tmp_path / "plate.sldprt"))  # a copy: the module's plate stays as it was
    drawing = sheet.make_drawing(str(tmp_path / "plate.slddrw"), "A3", keep_open=True)
    assert drawing["scale"] == "2:1" and drawing["sheet"] == "A3"
    assert drawing["hole_callouts"] == 1 and drawing["hole_locations"] == 4
    assert drawing["problems"] == [], drawing["problems"]
    annotations = sheet.list_annotations()["annotations"]
    callout = next(a for a in annotations if a["type"] == "hole_callout")
    assert callout["text"].startswith("4X ⌀")
    locations = sorted(a["value"] for a in annotations if a["type"] == "dimension" and a["id"].startswith("RD"))
    assert locations == [5.0, 5.0, 35.0, 55.0]

    fields = sheet.set_drawing_properties({"description": "MOUNTING PLATE", "drawn_by": "NDV",
                                           "part_no": "MP-001"})
    shown = fields["title_block"]
    assert shown["Material"] == "AISI 304"
    assert shown["Weight"].startswith("WEIGHT: ") and float(shown["Weight"].split()[-1]) > 100  # g of steel
    assert shown["Description"] == "MOUNTING PLATE" and shown["DrawnBy"] == "NDV"
    assert fields["not_in_title_block"] == ["PartNo"]  # the ISO format has no field for it

    whole = sheet.render_drawing(str(tmp_path / "sheet.png"))
    zoomed = sheet.render_drawing(str(tmp_path / "zoom.png"), [[20, 150], [200, 290]])
    assert whole["bytes"] > 5000 and zoomed["bytes"] > 5000
    assert os.path.getsize(zoomed["path"]) != os.path.getsize(whole["path"])


# --- views ---------------------------------------------------------------------------


def test_views_go_where_they_are_told(sheet):
    sheet.new_drawing("A3")
    front = sheet.add_view("front", at=[100, 200], scale="1:1", name="front")["view"]
    assert front["geometry_mm"] == pytest.approx([70, 180, 130, 220], abs=0.05)

    # a failed call leaves the drawing as it was
    before = sheet.list_annotations()["count"]
    with pytest.raises(SolidWorksError):
        sheet.run_guarded(sheet.add_dimension, "front", "hole:CBore1", "hole:NoSuchHole")
    assert sheet.list_annotations()["count"] == before
    assert [v["name"] for v in sheet.list_views()["views"]] == ["front"]

    # a section is cut through a model point; its alias works where a view name is asked for
    made = sheet.add_section_view("front", [5, 5, 5], "y", "A", name="A-A")
    assert made["view"]["type"] == "section" and made["view"]["alias"] == "A-A"
    assert "warnings" not in made
    assert sheet.set_view("A-A", display="hidden_removed")["ok"]

    side = sheet.add_projected_view("front", "right", gap_mm=30, name="side")["view"]
    assert side["geometry_mm"][0] == pytest.approx(160, abs=0.05)  # 30 mm right of the front
    iso = sheet.add_view("iso", right_of="side", scale="1:2")["view"]
    assert iso["geometry_mm"][0] > side["geometry_mm"][2]
    moved = sheet.move_view("front", at=[120, 200])["view"]
    assert moved["outline_mm"][0] > front["outline_mm"][0]


# --- annotations -----------------------------------------------------------------------


def test_annotations_on_a_front_and_a_top_view(sheet):
    sheet.new_drawing("A3")
    sheet.add_view("front", at=[100, 220], scale="1:1", name="front")

    # the check finds the undimensioned holes
    problems = sheet.check_drawing()["problems"]
    assert sorted(p["items"][0] for p in problems if p["kind"] == "undimensioned_feature") == \
        ["CBore1", "CBore2", "CBore3", "CBore4"]

    # ordinate dimensions count from a zero
    made = sheet.add_dimension_set("front", "edge:-x", ["hole:CBore1", "hole:CBore2"], "ordinate")
    assert [d["value"] for d in made["dimensions"]] == [0.0, 5.0, 55.0]
    assert all(d["kind"] == "ordinate" for d in made["dimensions"])

    # a second dimension goes on the next row
    made = sheet.add_dimension_set("front", "edge:-x", ["hole:CBore1", "hole:CBore2"], "baseline", side="below")
    first, second = (sheet._row_of(d) for d in made["dimensions"])
    assert (first, second) == (pytest.approx(10), pytest.approx(18))

    # dimensions follow semantic references
    sheet.add_projected_view("front", "below", name="top")
    assert sheet.add_dimension("front", "edge:-x", "hole:CBore2")["value"] == pytest.approx(55)
    assert sheet.add_dimension("front", "hole:CBore1", "hole:CBore3")["value"] == pytest.approx(30)
    assert sheet.add_dimension("front", "hole:CBore1")["value"] == pytest.approx(8)  # counterbore diameter
    assert sheet.add_dimension("top", "edge:+z", "edge:-z")["value"] == pytest.approx(10)
    with pytest.raises(SolidWorksError, match="faces the viewer"):
        sheet.add_dimension("front", "edge:+z", "edge:-z")

    # a callout counts identical holes, and then none is undimensioned
    made = sheet.add_hole_callout("front", "CBore4")
    assert sorted(made["holes"]) == ["CBore1", "CBore2", "CBore3", "CBore4"]
    assert made["text"].startswith("4X ")
    single = sheet.add_hole_callout("front", "CBore1", at=[40, 150], count=False)
    assert not single["text"].startswith("4X")
    assert not [p for p in sheet.check_drawing()["problems"] if p["kind"] == "undimensioned_feature"]

    # notes and edits
    note = sheet.add_note("NOTES:\n1. BREAK SHARP EDGES.")
    assert note["type"] == "note" and note["id"].endswith("@sheet") and "warnings" not in note
    dim = sheet.add_dimension("front", "edge:-x", "edge:+x")
    edited = sheet.edit_annotation(dim["id"], tolerance=[0.1, -0.1])
    assert "±" in edited["text"] and "changes_part" not in edited
    sheet.edit_annotation(note["id"], text="NOTES:\n1. DEBURR.")
    sheet.delete_annotation(dim["id"])
    ids = [a["id"] for a in sheet.list_annotations()["annotations"]]
    assert dim["id"] not in ids and note["id"] in ids

    # symbols attach to edges
    finish = sheet.add_surface_finish("top", "edge:+z", "Ra 1.6")
    datum = sheet.add_datum("top", "edge:-z", "A")
    frame = sheet.add_gtol("top", "edge:+z", "parallelism", "0.05", ["A"])
    position = sheet.add_gtol("front", "hole:CBore2", "position", "0.1", ["A"], diameter=True,
                              material_condition="MMC")
    assert (finish["type"], finish["text"]) == ("surface_finish", "Ra 1.6")
    assert (datum["type"], datum["text"]) == ("datum", "A")
    assert (frame["type"], frame["text"]) == ("gtol", "0.05 A")
    assert position["text"].startswith("⌀ 0.1")
    with pytest.raises(SolidWorksError, match="Unknown symbol"):
        sheet.add_gtol("top", "edge:+z", "wobble", "0.1")

    # a gear data table is computed
    with pytest.raises(SolidWorksError, match="teeth"):
        sheet.add_gear_data_table("spur")  # the plate has no Teeth property
    table = sheet.add_gear_data_table("spur", 20, 2, 40, face_width_mm=15, extra_rows=[["Material", "42CrMo4"]])
    assert table["type"] == "table" and table["rows"] == 14 and table["columns"] == 3
    assert "Span over teeth W3 15.321" in table["text"] and "Material 42CrMo4" in table["text"]
    bevel = sheet.add_gear_data_table("bevel", 17, 14, 36, at=[30, 120])
    assert bevel["gear"]["pitch_angle"] == pytest.approx(25.2777, abs=1e-3)


def test_auto_layout_untangles_the_views(sheet):
    sheet.new_drawing("A3")
    sheet.add_view("front", at=[300, 150], scale="1:1", name="front")
    sheet.add_projected_view("front", "right", gap_mm=5, name="side")
    sheet.add_view("iso", at=[310, 160])  # on top of the others
    assert any(p["kind"] == "views_overlap" for p in sheet.check_drawing()["problems"])
    laid = sheet.auto_layout()
    assert laid["main"] == "front" and laid["scale"] == "2:1"
    assert not [p for p in laid["problems"] if p["kind"].startswith("view")], laid["problems"]


# --- build_drawing -------------------------------------------------------------------------


SPEC = {
    "sheet": "A3", "scale": "2:1",
    "views": [{"name": "front", "orient": "front", "at": [110, 220]},
              {"name": "side", "projected_from": "front", "side": "right"},
              {"name": "A-A", "section_of": "front", "through": [5, 5, 5], "normal": "y", "label": "A"}],
    "import": ["model"],
    "dims": [{"view": "front", "baseline": ["edge:-x", "hole:CBore1", "hole:CBore2"]},
             {"view": "front", "chain": ["edge:-y", "hole:CBore1", "hole:CBore3"]}],
    "callouts": [{"view": "front", "hole": "CBore3"}],
    "notes": ["GENERAL TOLERANCES ISO 2768-mK."],
    "props": {"description": "MOUNTING PLATE"},
}


def test_a_spec_builds_a_clean_drawing(sheet, tmp_path):
    # a spec that fails names the entry, and its drawing closes
    with pytest.raises(SolidWorksError, match=r"dims\[0\]"):
        sheet.build_drawing({**SPEC, "dims": [{"view": "front", "ref": "hole:Nope"}]})
    assert [d for d in sheet.list_documents()["documents"] if d["type"] == "drawing"] == []

    built = sheet.build_drawing(SPEC, str(tmp_path / "built.pdf"))
    assert built["views"] == ["front", "side", "Section View A-A"]
    assert len(built["dimensions"]) == 4 and len(built["callouts"]) == 1
    assert built["problems"] == [], built["problems"]
    assert os.path.getsize(built["saved"]) > 1000
    _close_drawings(sheet)

    # symbols, tables and the automatic layout
    spec = {"sheet": "A3", "layout": "auto",
            "views": [{"name": "front", "orient": "front"}, {"name": "top", "projected_from": "front", "side": "below"}],
            "dims": [{"view": "front", "ordinate": ["edge:-x", "hole:CBore1", "hole:CBore2"]}],
            "callouts": [{"view": "front", "hole": "CBore4"}],
            "symbols": [{"type": "datum", "view": "top", "ref": "edge:-z", "label": "A"},
                        {"type": "gtol", "view": "top", "ref": "edge:+z", "symbol": "parallelism", "tolerance": "0.05",
                         "datums": ["A"]},
                        {"type": "surface_finish", "view": "top", "ref": "edge:+z", "roughness": "Ra 1.6"}],
            "tables": [[["REVISIONS", ""], ["A", "First issue"]]]}
    built = sheet.build_drawing(spec)
    assert len(built["symbols"]) == 3 and len(built["tables"]) == 1 and len(built["dimensions"]) == 3


def test_the_merged_tools_end_to_end(sheet, tmp_path):
    """The one-per-family entry points the MCP server offers."""
    assert sheet.drawing_file("new", sheet="A3")["ok"]
    sheet.add_drawing_view("model", "front", at=[100, 220], scale="1:1", name="front")
    sheet.add_drawing_view("projected", parent="front", side="below", name="top")
    sheet.add_drawing_view("section", parent="front", through=[5, 5, 5], normal="y", label="A", name="A-A")
    imported = sheet.import_annotations(kinds=["callouts", "center_marks"])
    assert imported["center_marks"] > 0 and len(imported["hole_callouts"]) == 1
    ordinates = sheet.dimension("front", "edge:-x", ["hole:CBore1", "hole:CBore2"], "ordinate")
    assert [d["value"] for d in ordinates["dimensions"]] == [0.0, 5.0, 55.0]
    single = sheet.dimension("top", "edge:+z", "edge:-z")
    assert single["value"] == pytest.approx(10)
    assert sheet.add_symbol("gtol", "top", "edge:+z", symbol="flatness", tolerance="0.02")["type"] == "gtol"
    assert sheet.table(gear={"kind": "spur", "teeth": 20, "module": 2})["type"] == "table"
    listed = sheet.annotations("front")
    assert [v["name"] for v in listed["views"]] == ["front"] and listed["views"][0]["annotations"]
    sheet.edit_view("top", display="hidden_visible")
    sheet.change_annotation(single["id"], delete=True)
    laid = sheet.lay_out(arrange_dimensions=True)
    assert laid["arranged_dimensions"] > 0
    assert sheet.drawing_file("export", str(tmp_path / "merged.pdf"))["bytes"] > 1000
    assert sheet.drawing_file("close")["ok"] and sheet._drawing is None

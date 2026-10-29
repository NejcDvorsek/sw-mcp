"""Pure unit tests for drawing_layout: sheets, scales, frames, placement, checks."""

import pytest

from sw_mcp import drawing_layout as lay
from sw_mcp.errors import SolidWorksError

# The A3 ISO sheet format of SOLIDWORKS 2026 (sheet mm): outer border, inner
# frame, a zone tick between them, and the title block's lines in the bottom
# right corner of the frame.
A3_LINES = [
    (15.0, 292.0, 415.0, 292.0), (415.0, 5.0, 15.0, 5.0), (15.0, 5.0, 15.0, 292.0), (415.0, 292.0, 415.0, 5.0),
    (20.0, 287.0, 410.0, 287.0), (410.0, 10.0, 20.0, 10.0), (20.0, 10.0, 20.0, 287.0), (410.0, 287.0, 410.0, 10.0),
    (68.75, 287.0, 68.75, 292.0),
    (230.0, 14.2, 410.0, 14.2), (410.0, 48.2, 230.0, 48.2), (230.0, 65.2, 410.0, 65.2), (230.0, 10.0, 230.0, 65.2),
]

# A4 upright: the title block's top line runs the frame's full width
A4_LINES = [
    (10.0, 10.0, 200.0, 10.0), (10.0, 287.0, 200.0, 287.0), (10.0, 10.0, 10.0, 287.0), (200.0, 10.0, 200.0, 287.0),
    (5.0, 5.0, 205.0, 5.0), (5.0, 292.0, 205.0, 292.0), (5.0, 5.0, 5.0, 292.0), (205.0, 5.0, 205.0, 292.0),
    (10.0, 65.25, 200.0, 65.25), (100.0, 10.0, 100.0, 65.25),
]


def test_sheets_follow_the_iso_formats():
    assert lay.sheet_size("a3") == (420.0, 297.0, 8)
    assert lay.sheet_size("A4") == (210.0, 297.0, 7)  # SolidWorks' ISO A4 format stands upright
    with pytest.raises(SolidWorksError, match="Unknown sheet"):
        lay.sheet_size("letter")


@pytest.mark.parametrize("given, ratio", [("1:2", (1, 2)), ("2:1", (2, 1)), ([1, 5], (1, 5)), (0.5, (1, 2)),
                                          (2, (2, 1)), ("1/10", (1, 10))])
def test_a_scale_reads_several_ways(given, ratio):
    assert lay.as_ratio(given) == ratio
    assert lay.scale_text(ratio) == f"{ratio[0]}:{ratio[1]}"


@pytest.mark.parametrize("bad", ["2", "0:1", -1, [1, 2, 3]])
def test_a_bad_scale_is_refused(bad):
    with pytest.raises(SolidWorksError):
        lay.as_ratio(bad)


def test_the_largest_standard_scale_that_fits_is_picked():
    assert lay.pick_scale(lambda s: 60 * s <= 200) == (2, 1)
    assert lay.pick_scale(lambda s: 600 * s <= 200) == (1, 5)
    assert lay.pick_scale(lambda s: False) == lay.STANDARD_SCALES[-1]


def test_the_frame_and_title_block_come_from_the_format():
    frame, block = lay.frame_and_title_block(A3_LINES, 420.0, 297.0)
    assert frame == (20.0, 10.0, 410.0, 287.0)
    assert block == (230.0, 10.0, 410.0, 65.2)


def test_a_title_block_across_the_frame_is_not_the_frame():
    frame, block = lay.frame_and_title_block(A4_LINES, 210.0, 297.0)
    assert frame == (10.0, 10.0, 200.0, 287.0)
    assert block == (10.0, 10.0, 200.0, 65.25)
    assert lay.drawing_area(frame, block) == [(10.0, 65.25, 200.0, 287.0)]


def test_without_a_format_a_margin_is_kept():
    frame, block = lay.frame_and_title_block([], 420.0, 297.0)
    assert frame == (10.0, 10.0, 410.0, 287.0) and block is None


def test_the_free_areas_go_round_the_title_block():
    areas = lay.drawing_area((20.0, 10.0, 410.0, 287.0), (230.0, 10.0, 410.0, 65.2))
    assert areas == [(20.0, 65.2, 410.0, 287.0), (20.0, 10.0, 230.0, 287.0)]


@pytest.mark.parametrize("side, centre", [("right", (75.0, 25.0)), ("left", (-35.0, 25.0)),
                                          ("above", (20.0, 85.0)), ("below", (20.0, -35.0))])
def test_a_box_goes_beside_another(side, centre):
    assert lay.place_beside((0.0, 0.0, 40.0, 50.0), (30.0, 30.0), side, 20.0) == centre


def test_a_box_moves_past_what_is_in_the_way():
    anchor = (20.0, 200.0, 80.0, 260.0)
    in_the_way = (120.0, 200.0, 140.0, 260.0)
    side, (x, y) = lay.place_clear(anchor, (20.0, 60.0), ["right"], 44.0, [anchor, in_the_way],
                                   [(10.0, 10.0, 410.0, 287.0)], margin=22.0)
    assert side == "right" and y == 230.0
    assert x - 10.0 >= in_the_way[2] + 22.0  # clear of the box in the way by the margin


def test_no_room_on_any_side_gives_none():
    assert lay.place_clear((0.0, 0.0, 100.0, 100.0), (50.0, 50.0), ["right"], 10.0, [],
                           [(0.0, 0.0, 120.0, 120.0)]) is None


def test_the_standard_views_fit_or_not():
    areas = lay.drawing_area((20.0, 10.0, 410.0, 287.0), (230.0, 10.0, 410.0, 65.2))
    placed = lay.first_angle_layout((120.0, 80.0), (120.0, 20.0), (20.0, 80.0), (50.0, 40.0), areas, 0.0, 22.0)
    assert set(placed) == {"front", "top", "side", "iso"}
    front, top, side = placed["front"], placed["top"], placed["side"]
    assert top[0] == front[0] and top[1] < front[1]   # first angle: top view below the front
    assert side[1] == front[1] and side[0] > front[0]  # side view to its right
    assert lay.first_angle_layout((400.0, 80.0), (400.0, 20.0), (20.0, 80.0), None, areas, 0.0, 22.0) is None


def test_dimension_rows_stack():
    assert lay.next_row([]) == 10.0
    assert lay.next_row([10.0]) == 18.0
    assert lay.next_row([10.0, 18.0]) == 26.0
    assert lay.next_row([18.0]) == 10.0


def test_a_dimension_line_runs_between_its_arrowheads():
    # a vertical dimension (reported as 'linear'): two horizontal extension lines
    # longer than the dimension line, and arrow tips at (5, 0) and (5, 30)
    lines = [(0.0, 0.0, 40.0, 0.0), (0.0, 30.0, 40.0, 30.0), (5.0, 0.0, 5.0, 30.0), (5.0, 0.0, 5.0, 0.0),
             (5.0, 30.0, 5.0, 30.0)]
    assert lay.dimension_orientation(lines)[0] == "vertical"
    assert lay.dimension_row(lines, (10.0, -5.0, 60.0, 40.0)) == ("left", 5.0, (0.0, 30.0))
    assert lay.dimension_row(lines[:3], (10.0, -5.0, 60.0, 40.0)) is None  # no arrowheads


@pytest.mark.parametrize("raw, shown", [
    ("<MOD-DIAM> 4.5 THRU ALL", "⌀ 4.5 THRU ALL"),
    ("<HOLE-SPOT><MOD-DIAM> 8 <HOLE-DEPTH> 4.4", "⌴⌀ 8 ↧ 4.4"),
    ("Material <not specified>", "Material <not specified>"),
    ("<FONT size=3.5mm>NOTE", "NOTE"),
])
def test_text_tokens_show_as_symbols(raw, shown):
    assert lay.clean_text(raw) == shown


def test_a_text_box_turns_with_the_text():
    flat = lay.text_box(0.0, 0.0, 3.5, 0.0, "60")
    assert flat == pytest.approx((0.0, 0.0, 2 * lay.CHAR_WIDTH * 3.5, 3.5))
    upright = lay.text_box(0.0, 0.0, 3.5, 1.5707963, "60")
    assert upright == pytest.approx((-3.5, 0.0, 0.0, 2 * lay.CHAR_WIDTH * 3.5), abs=1e-6)


@pytest.mark.parametrize("ref, parsed", [
    ("edge:-x", ("face", "-x")), ("face:+z:inner", ("face", "+z:inner")), ("hole:M4_CBore_1", ("hole", "M4_CBore_1")),
    ("feature:CornerFillet", ("feature", "CornerFillet")), ([1, 2, 3], ("point", (1.0, 2.0, 3.0))),
    ("point:1, 2, 3", ("point", (1.0, 2.0, 3.0))),
])
def test_references_parse(ref, parsed):
    assert lay.parse_ref(ref) == parsed


@pytest.mark.parametrize("bad", ["-x", "hole:", "bolt:M4", [1, 2]])
def test_bad_references_are_refused(bad):
    with pytest.raises(SolidWorksError):
        lay.parse_ref(bad)


def test_counts():
    assert lay.count_prefix(1) == "" and lay.count_prefix(4) == "4X "


def test_crossing_segments():
    assert lay.segments_cross((0, 0, 10, 10), (0, 10, 10, 0))
    assert not lay.segments_cross((0, 0, 10, 0), (10, 0, 10, 10))  # meeting at an end
    assert not lay.segments_cross((0, 0, 10, 0), (0, 1, 10, 1))


FRAME = (20.0, 10.0, 410.0, 287.0)
BLOCK = (230.0, 10.0, 410.0, 65.2)


def note(id_, box, **extra):
    return {"id": id_, "type": "note", "text": id_, "box": box, "text_boxes": [box], "lines": [], "leaders": [],
            **extra}


def kinds(problems):
    return sorted(p["kind"] for p in problems)


def test_a_clean_sheet_has_no_problems():
    views = [{"name": "front", "box": (40.0, 150.0, 160.0, 250.0)}]
    assert lay.find_problems(views, [note("a", (40.0, 260.0, 80.0, 265.0))], FRAME, BLOCK) == []


def test_the_checks_find_what_is_wrong():
    views = [{"name": "front", "box": (40.0, 150.0, 160.0, 250.0)},
             {"name": "side", "box": (150.0, 150.0, 200.0, 250.0)},
             {"name": "far", "box": (380.0, 40.0, 420.0, 60.0)}]
    annotations = [
        note("out", (0.0, 100.0, 30.0, 105.0)),
        note("on_block", (300.0, 30.0, 330.0, 35.0)),
        note("on_view", (60.0, 200.0, 90.0, 205.0)),
        note("small", (40.0, 260.0, 60.0, 262.0), text_height=1.8),
        note("clash1", (250.0, 260.0, 280.0, 265.0)),
        note("clash2", (270.0, 262.0, 300.0, 267.0)),
        {"id": "d1", "type": "dimension", "value": 10.0, "kind": "horizontal", "axis": "x", "box": None},
        {"id": "d2", "type": "dimension", "value": 10.0, "kind": "horizontal", "axis": "x", "box": None},
        {"id": "d3", "type": "dimension", "value": 10.0, "kind": "vertical", "axis": "y", "box": None},
    ]
    found = lay.find_problems(views, annotations, FRAME, BLOCK)
    assert kinds(found) == sorted(["views_overlap", "view_outside_frame", "view_over_title_block", "outside_frame",
                                   "over_title_block", "text_on_view", "text_too_small", "texts_overlap",
                                   "duplicate_dimension"])
    duplicate = next(p for p in found if p["kind"] == "duplicate_dimension")
    assert duplicate["items"] == ["d1", "d2"]


def test_crossing_leaders_are_found():
    a = note("a", (300.0, 200.0, 310.0, 205.0), leaders=[(300.0, 200.0, 340.0, 240.0)])
    b = note("b", (300.0, 240.0, 310.0, 245.0), leaders=[(300.0, 240.0, 340.0, 200.0)])
    assert kinds(lay.find_problems([], [a, b], FRAME, BLOCK)) == ["leaders_cross"]


def test_a_sheet_of_views_is_arranged_round_the_main_one():
    areas = lay.drawing_area((20.0, 10.0, 410.0, 287.0), (230.0, 10.0, 410.0, 65.2))
    placed = lay.arrange_views((120.0, 80.0), [("top", "below", (120.0, 20.0)), ("side", "right", (20.0, 80.0)),
                                               ("A-A", "right", (20.0, 80.0))],
                               [("iso", (50.0, 40.0))], areas, margin=10.0)
    main, top, side, section = placed["main"], placed["top"], placed["side"], placed["A-A"]
    assert top[0] == main[0] and top[1] < main[1]
    assert side[1] == main[1] and main[0] < side[0] < section[0]   # stacked outward to the right
    assert section[0] - side[0] == pytest.approx(20.0 + 2 * 10.0)
    assert "iso" in placed


def test_views_left_and_above_push_the_main_view_in():
    areas = [(0.0, 0.0, 400.0, 300.0)]
    placed = lay.arrange_views((100.0, 50.0), [("l", "left", (30.0, 50.0)), ("a", "above", (100.0, 20.0))], [],
                               areas, margin=10.0)
    assert placed["l"][0] - 15.0 >= 10.0          # the left view's box keeps its margin from the sheet edge
    assert placed["a"][1] + 10.0 <= 290.0
    assert lay.arrange_views((500.0, 50.0), [], [], areas, margin=10.0) is None

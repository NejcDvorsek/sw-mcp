"""run_script as a whole part in one call: a script file with helpers that
reload on every run, and one report with bodies, clashes and a picture.

The pure helpers run everywhere; the rest is marked `solidworks`.
"""

import struct
import sys
import zlib

import pytest

from sw_mcp.advanced import boxes_overlap, script_folder, sheet_tile
from sw_mcp.errors import SolidWorksError
from sw_mcp.image_sheet import png_bytes, read_bmp, tile


def bmp(width, height, pixel, bits=24, top_down=False):
    """A BMP filled by pixel(x, y) -> (r, g, b), y counted from the top."""
    step = bits // 8
    stride = (width * step + 3) // 4 * 4
    stored = range(height) if top_down else range(height - 1, -1, -1)
    body = b""
    for y in stored:
        row = b"".join(bytes([*reversed(pixel(x, y)), *([0] * (step - 3))]) for x in range(width))
        body += row + b"\x00" * (stride - len(row))
    header = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bits, 0, len(body),
                         2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54) + header + body


def png_pixels(data):
    """(width, height, rows) of a PNG png_bytes wrote: filter 0 on every row."""
    width, height = struct.unpack_from(">II", data, 16)
    start = data.index(b"IDAT") + 4
    length = struct.unpack_from(">I", data, start - 8)[0]
    raw = zlib.decompress(data[start:start + length])
    stride = width * 3 + 1
    return width, height, [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]


# --- pure ------------------------------------------------------------------------


@pytest.mark.parametrize("bits,top_down", [(24, False), (24, True), (32, False)])
def test_a_bmp_reads_as_rgb_rows_from_the_top(bits, top_down):
    # 3 wide pads each 24-bit row by 3 bytes; red marks the top left
    image = bmp(3, 2, lambda x, y: (255, 0, 0) if (x, y) == (0, 0) else (0, 10 * x, 100 + y), bits, top_down)
    width, height, rows = read_bmp(image)
    assert (width, height) == (3, 2)
    assert rows[0] == bytes([255, 0, 0, 0, 10, 100, 0, 20, 100])
    assert rows[1] == bytes([0, 0, 101, 0, 10, 101, 0, 20, 101])


def test_a_compressed_bmp_is_refused():
    image = bytearray(bmp(2, 2, lambda x, y: (0, 0, 0)))
    struct.pack_into("<I", image, 30, 1)  # BI_RLE8
    with pytest.raises(ValueError, match="compression"):
        read_bmp(bytes(image))


def test_four_views_tile_two_by_two_with_a_gutter():
    views = [(2, 1, [bytes([shade] * 6)]) for shade in (10, 20, 30, 40)]
    width, height, rows = tile(views, 2)
    assert (width, height) == (2 + 2 + 2, 1 + 2 + 1)
    assert rows[0][:6] == bytes([10] * 6) and rows[0][-6:] == bytes([20] * 6)
    assert rows[1] == rows[2] == b"\x80" * 18
    assert rows[3][:6] == bytes([30] * 6) and rows[3][-6:] == bytes([40] * 6)


def test_an_odd_view_out_leaves_its_neighbour_blank():
    width, height, rows = tile([(1, 1, [b"\x00\x00\x00"])] * 3, 2)
    assert rows[-1][-3:] == b"\xff\xff\xff"


def test_views_of_different_sizes_do_not_tile():
    with pytest.raises(ValueError, match="differ"):
        tile([(1, 1, [b"\x00" * 3]), (2, 1, [b"\x00" * 6])])


def test_the_sheet_is_a_png_holding_the_pixels():
    rows = [bytes([1, 2, 3, 4, 5, 6]), bytes([7, 8, 9, 10, 11, 12])]
    data = png_bytes(2, 2, rows)
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
    assert png_pixels(data) == (2, 2, rows)


def test_each_view_keeps_the_window_shape():
    assert sheet_tile(1447, 694, 640) == (640, 307)
    assert sheet_tile(0, 0, 640) == (640, 480), "a hidden window draws 4:3"
    assert sheet_tile(100, 5000, 640) == (640, 1280), "no taller than twice the width"


def test_boxes_that_only_touch_do_not_overlap():
    a = {"min_mm": [0, 0, 0], "max_mm": [10, 10, 10]}
    assert boxes_overlap(a, {"min_mm": [5, 5, 5], "max_mm": [15, 15, 15]})
    assert not boxes_overlap(a, {"min_mm": [10, 0, 0], "max_mm": [20, 10, 10]})
    assert not boxes_overlap(a, None)


def test_helpers_next_to_a_script_load_fresh_every_run(tmp_path):
    helper = tmp_path / "parthelper_fresh.py"
    helper.write_text("VALUE = 1\n")
    with script_folder(str(tmp_path)):
        import parthelper_fresh
        assert parthelper_fresh.VALUE == 1
    assert "parthelper_fresh" not in sys.modules and str(tmp_path) not in sys.path
    helper.write_text("VALUE = 2\n")
    with script_folder(str(tmp_path)):
        import parthelper_fresh  # noqa: F811 - imported again on purpose
        assert parthelper_fresh.VALUE == 2


# --- SolidWorks ----------------------------------------------------------------------


@pytest.mark.solidworks
def test_a_script_file_builds_a_part_and_reports_it(part, tmp_path):
    (tmp_path / "parthelper_box.py").write_text("SIZE = 10\n")
    script = tmp_path / "block.py"
    script.write_text("from parthelper_box import SIZE\n"
                      "session.add_box(SIZE, SIZE, SIZE, name='Block')\n"
                      "result = SIZE\n")
    done = part.run_script(path=str(script))
    assert done["result"] == 10 and done["mass_properties"]["volume_mm3"] == pytest.approx(1000)
    assert done["features_added"] == ["Block (Extrusion)"], "only what the script added, not its sketch"
    [body] = done["bodies"]
    assert body["volume_mm3"] == pytest.approx(1000) and body["bounding_box_mm"]["size_mm"] == [10, 10, 10]
    assert "clashes" not in done and "failing_features" not in done and "under_defined_sketches" not in done
    picture = (tmp_path / "block.png").read_bytes()
    assert done["image"]["path"] == str(tmp_path / "block.png") and picture.startswith(b"\x89PNG")
    width, height, rows = png_pixels(picture)
    assert width == 2 * 640 + 2 and height % 2 == 0, "two views across, two down"
    assert len(set(rows[height // 4])) > 3, "the iso view came out blank"
    assert done["seconds"]["script"] >= 0 and done["seconds"]["report"] > 0


@pytest.mark.solidworks
def test_overlapping_bodies_are_reported_as_a_clash(part):
    part.add_box(20, 10, 10, name="Left")
    part.add_extruded_profile([[10, 0], [30, 0], [30, 10], [10, 10]], 10, name="Right", merge=False)
    part.add_extruded_profile([[50, 0], [60, 0], [60, 10], [50, 10]], 10, name="Apart", merge=False)
    done = part.run_script(code="result = len(session.list_bodies()['bodies'])")
    assert done["result"] == 3 and len(done["bodies"]) == 3
    [clash] = done["clashes"]
    assert clash["volume_mm3"] == pytest.approx(1000)
    assert "image" not in done, "inline code draws no picture unless asked"


@pytest.mark.solidworks
def test_a_failing_script_file_leaves_the_part_as_it_was(part, tmp_path):
    part.add_box(10, 10, 10)
    before = part.list_features()["features"]
    script = tmp_path / "broken.py"
    script.write_text("session.add_box(5, 5, 5)\n\nraise ValueError('boom')\n")
    with pytest.raises(SolidWorksError, match=r"ValueError: boom .*broken\.py\", line 3"):
        part.run_guarded(part.run_script, None, str(script))
    assert part.list_features()["features"] == before
    assert not (tmp_path / "broken.png").exists()


@pytest.mark.solidworks
def test_a_script_that_starts_its_own_part_reports_all_of_it(sw):
    try:
        done = sw.run_script("session.new_part()\nsession.add_box(20, 10, 5, name='Base')\n"
                             "session.add_extruded_profile([[0, 0], [5, 0], [5, 10], [0, 10]], 15, name='Wall')")
        assert done["features_added"] == ["Base (Extrusion)", "Wall (ICE)"]
        assert done["mass_properties"]["volume_mm3"] == pytest.approx(20 * 10 * 5 + 5 * 10 * 10)
    finally:
        sw.close_part()


@pytest.mark.solidworks
def test_pictures_leave_the_view_animation_as_it_was(part, tmp_path):
    from sw_mcp.constants import SW_VIEW_ANIMATION_SPEED

    app = part._ensure()
    before = app.GetUserPreferenceDoubleValue(SW_VIEW_ANIMATION_SPEED)
    app.SetUserPreferenceDoubleValue(SW_VIEW_ANIMATION_SPEED, 0.75)
    try:
        part.add_box(10, 10, 10)
        part.screenshot(str(tmp_path / "box.png"))
        part.run_script(code="result = 1", image=str(tmp_path / "views.png"))
        assert app.GetUserPreferenceDoubleValue(SW_VIEW_ANIMATION_SPEED) == pytest.approx(0.75)
        assert (tmp_path / "views.png").exists()
    finally:
        app.SetUserPreferenceDoubleValue(SW_VIEW_ANIMATION_SPEED, before)


def test_a_script_comes_as_code_or_as_a_file():
    from sw_mcp.session import SolidWorksSession

    session = SolidWorksSession.__new__(SolidWorksSession)  # refused before it reaches SolidWorks
    with pytest.raises(SolidWorksError, match="one of them"):
        session.run_script()
    with pytest.raises(SolidWorksError, match="one of them"):
        session.run_script("result = 1", "part.py")
    with pytest.raises(SolidWorksError, match="No script"):
        session.run_script(path="Z:/no/such/part.py")
    with pytest.raises(SolidWorksError, match=".png"):
        session.run_script(code="result = 1", image="views.jpg")

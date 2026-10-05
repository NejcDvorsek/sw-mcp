"""Integration tests for make_drawing: a dimensioned 2D drawing of the part.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks


def holed_box(part, tmp_path):
    part.add_box(40, 20, 10)
    part.add_hole(6, 30, 10)
    return part.save_part(str(tmp_path / "holed_box.sldprt"))["path"]


def test_a_drawing_shows_every_model_dimension_once(part, tmp_path):
    """Duplicated in two views, a dimension says twice what one view says."""
    holed_box(part, tmp_path)
    drawing = part.make_drawing(str(tmp_path / "holed_box.pdf"))
    assert drawing["bytes"] > 1000
    assert drawing["dimensions"] == part.list_dimensions()["count"] == 6  # width height depth, hole d x y
    assert len(drawing["views"]) == 4  # front, top, right and isometric


def test_a_drawing_can_be_kept_to_edit(part, tmp_path):
    holed_box(part, tmp_path)
    drawing = part.make_drawing(str(tmp_path / "holed_box.slddrw"))
    assert drawing["path"].lower().endswith(".slddrw") and drawing["bytes"] > 1000


@pytest.mark.parametrize("name", ["holed_box.pdf", "holed_box.slddrw"])
def test_a_drawing_closes_again(part, tmp_path, name):
    """The drawing is saved and closed: it would stay open in SolidWorks, and
    keep the part loaded after close_part."""
    holed_box(part, tmp_path)
    part.make_drawing(str(tmp_path / name))
    documents = part.list_documents()["documents"]
    assert [d["title"] for d in documents if d["type"] == "drawing"] == [], "the drawing stayed open"
    assert [d["title"] for d in documents if d["current"]] == ["holed_box.SLDPRT"], "the part should be current again"


def test_a_drawing_needs_the_part_saved(part, tmp_path):
    part.add_box(40, 20, 10)
    with pytest.raises(SolidWorksError, match="Save the part first"):
        part.make_drawing(str(tmp_path / "unsaved.pdf"))

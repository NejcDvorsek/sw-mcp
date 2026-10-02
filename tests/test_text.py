"""Text on a face: engraved labels and embossed version numbers.

A font's letters have no hand-calculable area, so the checks compare engraving
with embossing (the same letters), and where the letters land. Marked
`solidworks` where SolidWorks is needed. Volumes in mm^3, areas in mm^2.
"""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession

BLOCK = 60 * 30 * 10


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def letter_tops(session, z_mm):
    """The flat tops of embossed letters: faces facing +z at height z_mm."""
    return [f for f in session.list_faces()["faces"]
            if f.get("normal") == [0, 0, 1] and abs(f["center_mm"][2] - z_mm) < 1e-6]


def test_text_needs_letters_and_a_size():
    session = SolidWorksSession()  # refused before SolidWorks is needed
    with pytest.raises(SolidWorksError, match="No text"):
        session.add_text_on_face("  ", "+z", 10, 10, 10, 8, 0.5)
    with pytest.raises(SolidWorksError, match="height"):
        session.add_text_on_face("V1", "+z", 10, 10, 10, 0, 0.5)


@pytest.mark.solidworks
def test_engraved_and_embossed_text_cut_and_add_the_same_letters(part):
    part.add_box(60, 30, 10)
    engraved = part.add_text_on_face("V1.2", "+z", 10, 10, 10, 8, 0.5)
    area = engraved["text_area_mm2"]
    assert abs(vol(engraved) - (BLOCK - area * 0.5)) < 0.01
    assert engraved["fully_defined"] and {"x", "y", "depth"} <= set(engraved["dimensions"])
    part.close_part()

    part.new_part()
    part.add_box(60, 30, 10)
    embossed = part.add_text_on_face("V1.2", "+z", 10, 10, 10, 8, 1, emboss=True)

    assert abs(embossed["text_area_mm2"] - area) < 0.01, "engraving and embossing the same text gave different letters"
    assert abs(vol(embossed) - (BLOCK + area * 1)) < 0.01
    assert embossed["mass_properties"]["bounding_box_mm"]["max_mm"][2] == pytest.approx(11), "the letters must stand 1 mm"


@pytest.mark.solidworks
def test_the_text_starts_at_its_point_and_moves_with_its_dimension(part):
    part.add_box(60, 30, 10)
    embossed = part.add_text_on_face("V1.2", "+z", 10, 10, 10, 8, 1, emboss=True)

    tops = letter_tops(part, 11)
    assert len(tops) == 4, f"expected the letters V, 1, . and 2: {tops}"
    xs, ys = [f["center_mm"][0] for f in tops], [f["center_mm"][1] for f in tops]
    assert 10 < min(xs) and max(xs) < 10 + 4 * 8 and 10 < min(ys) and max(ys) < 18, (
        f"the letters do not start at (10, 10) and run along +x: centres x {xs}, y {ys}"
    )
    # the tallest letters' centres sit well above 40 % of an 8 mm height
    # (SolidWorks' default 3.5 mm text would stay below 12)
    assert max(ys) > 10 + 0.4 * 8, f"the letters are not 8 mm high: centres y {ys}"
    part.set_dimension(embossed["dimensions"]["x"], 30)
    moved = [f["center_mm"][0] for f in letter_tops(part, 11)]
    assert min(moved) == pytest.approx(min(xs) + 20, abs=1e-6), "the text did not follow its x dimension"


@pytest.mark.solidworks
def test_text_on_a_side_face(part):
    part.add_box(60, 30, 10)
    label = part.add_text_on_face("A", "+x", 60, 10, 2, 5, 0.5)

    assert label["text_area_mm2"] > 1 and label["fully_defined"], label
    assert abs(vol(label) - (BLOCK - label["text_area_mm2"] * 0.5)) < 0.01


@pytest.mark.solidworks
def test_an_unknown_font_is_refused_before_anything_is_sketched(part):
    # Windows would silently draw it in another font, and SolidWorks would
    # still report the name it was given
    part.add_box(60, 30, 10)
    history = part.list_features()["features"]

    with pytest.raises(SolidWorksError, match="not installed"):
        part.add_text_on_face("V1", "+z", 10, 10, 10, 8, 0.5, font="NoSuchFont123")

    assert part.list_features()["features"] == history

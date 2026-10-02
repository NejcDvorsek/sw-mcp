"""Opening STEP and Parasolid files as parts to build on.

A supplier's model has no history, but every tool must work on the imported
body. Marked `solidworks` where SolidWorks is needed. Volumes in mm^3.
"""

import math

import pytest

from solidworks_mcp.constants import SW_TOGGLE_3D_INTERCONNECT
from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession

BLOCK_WITH_HOLE = 8000 - math.pi * 3 ** 2 * 10  # 40 x 20 x 10 with a Ø6 through hole
SIDE_HOLE = math.pi * 2 ** 2 * 5                 # Ø4 x 5 blind


def vol(result):
    return result["mass_properties"]["volume_mm3"]


def test_open_part_names_the_formats_it_takes(tmp_path):
    drawing = tmp_path / "bracket.dxf"
    drawing.write_text("not a part")

    with pytest.raises(SolidWorksError, match="step"):
        SolidWorksSession().open_part(str(drawing))  # refused before SolidWorks is needed


@pytest.mark.solidworks
@pytest.mark.parametrize("extension", ["step", "x_t"])
def test_an_imported_part_can_be_built_on(part, tmp_path, extension):
    part.add_box(40, 20, 10)
    part.add_hole(6, 10, 10)
    exported = part.export(str(tmp_path / f"block.{extension}"))["path"]
    part.close_part()

    imported = part.open_part(exported)

    assert imported["imported"] and imported["solid_bodies"] == 1, imported
    assert abs(vol(imported) - BLOCK_WITH_HOLE) < 0.01, "the import must report the body it brought in"
    assert len(part.list_features()["features"]) == 1, "an import is one feature without history"
    side_hole = part.add_hole_on_face(4, "+x", 40, 10, 5, depth_mm=5)
    assert abs(vol(side_hole) - (BLOCK_WITH_HOLE - SIDE_HOLE)) < 0.01, "a tool did not work on the imported body"
    assert side_hole["fully_defined"]


def test_open_assembly_names_the_formats_it_takes(tmp_path):
    drawing = tmp_path / "pair.dxf"
    drawing.write_text("not an assembly")

    with pytest.raises(SolidWorksError, match="step"):
        SolidWorksSession().open_assembly(str(drawing))  # refused before SolidWorks is needed


@pytest.mark.solidworks
def test_an_assembly_step_opens_with_its_components_in_place(assembly, blocks, tmp_path):
    assembly.insert_component(blocks["block_a"], 0, 0, 0)
    assembly.insert_component(blocks["block_b"], 100, 0, 0)
    step = assembly.export(str(tmp_path / "pair.step"))["path"]
    assembly.close_part()
    interconnect = assembly._sw.GetUserPreferenceToggle(SW_TOGGLE_3D_INTERCONNECT)

    opened = assembly.open_assembly(step)

    assert opened["imported"] and opened["components"] == 2, opened
    assert assembly._sw.GetUserPreferenceToggle(SW_TOGGLE_3D_INTERCONNECT) == interconnect, (
        "the import switched the user's 3D Interconnect setting and left it switched"
    )
    placed = sorted(c["position_mm"] for c in assembly.list_components()["components"])
    assert placed == [pytest.approx([0, 0, 0], abs=1e-6), pytest.approx([100, 0, 0], abs=1e-6)], (
        f"the components did not keep their places: {placed}"
    )


@pytest.mark.solidworks
def test_open_assembly_refuses_a_part_file_and_closes_it(part, tmp_path):
    part.add_box(40, 20, 10)
    step = part.export(str(tmp_path / "block.step"))["path"]
    documents_before = len(part._sw.GetDocuments() or ())

    with pytest.raises(SolidWorksError, match="holds a single part, not an assembly"):
        part.open_assembly(step)

    assert len(part._sw.GetDocuments() or ()) == documents_before, "the refused import stayed open"


@pytest.mark.solidworks
def test_an_assembly_step_is_refused_and_leaves_nothing_open(assembly, blocks, tmp_path):
    assembly.insert_component(blocks["block_a"], 0, 0, 0)
    assembly.insert_component(blocks["block_b"], 100, 0, 0)
    step = assembly.export(str(tmp_path / "pair.step"))["path"]
    documents_before = len(assembly._sw.GetDocuments() or ())

    with pytest.raises(SolidWorksError, match="holds an assembly, not a part"):  # the tmp path says 'assembly' too
        assembly.open_part(step)

    assert len(assembly._sw.GetDocuments() or ()) == documents_before, (
        "the refused import left its assembly or component documents open"
    )

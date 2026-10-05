"""Integration tests for open documents and components: back to an unsaved
part by its title, a component out of an assembly with its mates.

Marked `solidworks` -- they need a running SolidWorks (auto-skipped otherwise).
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError

pytestmark = pytest.mark.solidworks


def test_an_unsaved_part_can_be_made_current_again(sw):
    """A new part, then another that is closed again: without a path, the first
    one can only come back by its title."""
    sw.new_part()
    first = next(d for d in sw.list_documents()["documents"] if d["current"])
    try:
        sw.add_box(10, 10, 10)
        sw.new_part()
        sw.close_part()  # the second one goes, and nothing is current now
        with pytest.raises(SolidWorksError, match="No active document"):
            sw.add_box(5, 5, 5)
        back = sw.activate_document(first["title"])
        assert back["current"] == first["title"] and back["path"] == "" and back["modified"]
        assert sw.add_hole(2, 5, 5)["mass_properties"]["volume_mm3"] == pytest.approx(1000 - math.pi * 10), \
            "the hole should go into the first part"
    finally:
        sw._sw.CloseDoc(first["title"])
        sw._model = None


def test_activate_document_names_the_open_ones(part):
    title = next(d["title"] for d in part.list_documents()["documents"] if d["current"])
    with pytest.raises(SolidWorksError, match=rf"No open document 'Nope'.*{title}"):
        part.activate_document("Nope")


def test_a_component_goes_with_its_mates(assembly, blocks):
    assembly.insert_component(blocks["block_a"])
    assembly.insert_component(blocks["block_b"], 0, 0, 10)
    assembly.add_mate("block_b-1", "-z", "block_a-1", "+z", "coincident")
    gone = assembly.delete_component("block_b-1")
    assert gone["components"] == ["block_a-1"] and len(gone["mates_deleted"]) == 1
    assert gone["mass_properties"]["volume_mm3"] == pytest.approx(40 * 20 * 10)
    assert not assembly.list_components().get("mates")


def test_a_mate_whose_face_is_gone_says_so(sw, tmp_path):
    """The lower block is rebuilt as a cylinder: the top face the mate held is
    gone, and list_components says so in words, not only as error 48."""
    paths = {}
    for name, size in (("lower", (40, 20, 10)), ("upper", (20, 20, 20))):
        sw.new_part()
        sw.add_box(*size)
        paths[name] = sw.save_part(str(tmp_path / f"{name}.sldprt"))["path"]
        sw.close_part()
    sw.new_assembly()
    title = sw._model.GetTitle()
    try:
        sw.insert_component(paths["lower"])
        sw.insert_component(paths["upper"], 0, 0, 30)
        mate = sw.add_mate("upper-1", "-z", "lower-1", "+z", "coincident")["mate"]
        sw.open_part(paths["lower"])
        sw.delete_feature("BlockExtrude", with_children=True)
        sw.add_cylinder(10, 10)
        sw.save_part(paths["lower"])
        sw.close_part()
        sw.activate_document(title)
        sw.rebuild()
        [entry] = [m for m in sw.list_components()["mates"] if m["name"] == mate]
        assert entry["error"]["code"] == 48 and "gone" in entry["error"]["cause"], entry
    finally:
        sw.activate_document(title)
        sw.close_part()
        for name in ("lower.sldprt", "upper.sldprt"):
            sw._sw.CloseDoc(name)

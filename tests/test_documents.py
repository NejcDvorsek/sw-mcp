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

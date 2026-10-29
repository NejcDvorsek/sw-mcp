"""The merged drawing tools route each kind / type / action / style to the
method that does it, and refuse arguments that belong to another kind.

Pure: the methods they route to are replaced by recorders.
"""

import pytest

from sw_mcp.errors import SolidWorksError
from sw_mcp.session import SolidWorksSession

ROUTED = ["new_drawing", "open_drawing", "save_drawing", "export_drawing", "close_drawing", "add_view",
          "add_projected_view", "add_section_view", "add_detail_view", "move_view", "set_view", "delete_view",
          "add_dimension", "add_dimension_set", "add_hole_callout", "add_surface_finish", "add_datum", "add_gtol",
          "add_table", "add_gear_data_table", "edit_annotation", "delete_annotation"]


@pytest.fixture
def s(monkeypatch):
    session = SolidWorksSession()
    session.calls = []
    for name in ROUTED:
        def record(*args, _name=name, **kwargs):
            session.calls.append((_name, args, kwargs))
            return {"ok": True, "called": _name}
        monkeypatch.setattr(session, name, record)
    return session


def called(s):
    return [c[0] for c in s.calls]


@pytest.mark.parametrize("action, path, method", [("new", None, "new_drawing"), ("open", "a.slddrw", "open_drawing"),
                                                  ("save", "a.slddrw", "save_drawing"), ("export", "a.pdf", "export_drawing"),
                                                  ("close", None, "close_drawing")])
def test_drawing_file_actions(s, action, path, method):
    s.drawing_file(action, path)
    assert called(s) == [method]


def test_drawing_file_refusals(s):
    with pytest.raises(SolidWorksError, match="needs path"):
        s.drawing_file("save")
    with pytest.raises(SolidWorksError, match="Unknown action"):
        s.drawing_file("print")
    with pytest.raises(SolidWorksError, match="path does not go with action 'new'"):
        s.drawing_file("new", "x.slddrw")


def test_view_kinds(s):
    s.add_drawing_view("model", "top", beside="front", side="below", name="t")
    name, args, _ = s.calls[-1]
    assert name == "add_view" and args[0] == "top" and args[5] == "front"  # below=front
    s.add_drawing_view("projected", parent="front", side="left")
    assert s.calls[-1][0] == "add_projected_view" and s.calls[-1][1][:2] == ("front", "left")
    s.add_drawing_view("section", parent="front", through=[1, 2, 3], normal="y")
    assert s.calls[-1][0] == "add_section_view" and s.calls[-1][1][3] == "A"
    s.add_drawing_view("detail", parent="front", center=[0, 0, 0], radius_mm=5)
    assert s.calls[-1][0] == "add_detail_view" and s.calls[-1][1][4] == "B"


@pytest.mark.parametrize("kwargs, message", [
    ({"kind": "section", "parent": "front"}, "through"),
    ({"kind": "detail", "parent": "front", "center": [0, 0, 0]}, "radius_mm"),
    ({"kind": "projected"}, "parent"),
    ({"kind": "projected", "parent": "front", "through": [0, 0, 0]}, "through does not go with kind 'projected'"),
    ({"kind": "model", "side": "right"}, "beside"),
    ({"kind": "model", "parent": "front"}, "parent does not go with kind 'model'"),
    ({"kind": "cutaway", "parent": "front"}, "Unknown kind"),
])
def test_view_refusals(s, kwargs, message):
    with pytest.raises(SolidWorksError, match=message):
        s.add_drawing_view(**kwargs)
    assert s.calls == []


def test_edit_view_moves_changes_or_deletes(s):
    s.edit_view("front", at=[10, 20])
    assert called(s) == ["move_view"]
    s.edit_view("front", scale="1:2", beside="side", side="left")
    assert called(s)[-2:] == ["set_view", "move_view"] and s.calls[-1][1][3] == "side"  # left_of
    s.edit_view("front", delete=True)
    assert called(s)[-1] == "delete_view"
    with pytest.raises(SolidWorksError, match="do not go with delete"):
        s.edit_view("front", delete=True, at=[1, 2], scale="1:1")
    with pytest.raises(SolidWorksError, match="Say what to change"):
        s.edit_view("front")


def test_dimension_styles(s):
    s.dimension("front", "edge:-x", "hole:H1")
    assert called(s) == ["add_dimension"]
    s.dimension("front", "edge:-x", ["hole:H1", "hole:H2"], "ordinate")
    assert s.calls[-1][0] == "add_dimension_set" and s.calls[-1][1][3] == "ordinate"
    with pytest.raises(SolidWorksError, match="to=\\[ref"):
        s.dimension("front", "edge:-x", [1.0, 2.0, 3.0], "chain")  # a point, not a list of refs
    with pytest.raises(SolidWorksError, match="offset_mm"):
        s.dimension("front", "edge:-x", ["hole:H1"], "baseline", offset_mm=10)


def test_symbol_types(s):
    s.add_symbol("hole_callout", "front", "hole:CBore1", count=False)
    assert s.calls[-1][0] == "add_hole_callout" and s.calls[-1][1][1] == "CBore1" and s.calls[-1][1][3] is False
    s.add_symbol("surface_finish", "top", "edge:+z", roughness="Ra 1.6")
    s.add_symbol("datum", "top", "edge:-z", label="B")
    s.add_symbol("gtol", "top", "edge:+z", symbol="flatness", tolerance="0.02")
    assert called(s)[1:] == ["add_surface_finish", "add_datum", "add_gtol"]
    with pytest.raises(SolidWorksError, match="tolerance does not go with a datum"):
        s.add_symbol("datum", "top", "edge:-z", tolerance="0.1")
    with pytest.raises(SolidWorksError, match="needs symbol"):
        s.add_symbol("gtol", "top", "edge:+z", tolerance="0.1")
    with pytest.raises(SolidWorksError, match="count does not go with a datum"):
        s.add_symbol("datum", "top", "edge:-z", count=False)


def test_tables_plain_or_gear(s):
    s.table(rows=[["a", "b"]])
    s.table(gear={"kind": "spur", "teeth": 20, "module": 2})
    assert called(s) == ["add_table", "add_gear_data_table"]
    assert s.calls[-1][2] == {"kind": "spur", "teeth": 20, "module": 2, "at": None}
    with pytest.raises(SolidWorksError, match="rows=\\[\\[...\\], ...\\] for a table, or gear"):
        s.table()
    with pytest.raises(SolidWorksError, match="Unknown gear keys"):
        s.table(gear={"teeth": 20, "colour": "red"})


def test_an_annotation_is_edited_or_deleted(s):
    s.change_annotation("RD1@front", prefix="2X ")
    s.change_annotation("RD1@front", delete=True)
    assert called(s) == ["edit_annotation", "delete_annotation"]
    with pytest.raises(SolidWorksError, match="do not go with delete"):
        s.change_annotation("RD1@front", delete=True, prefix="2X ")

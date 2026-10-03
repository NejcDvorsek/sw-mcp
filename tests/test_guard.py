"""A failed tool call leaves the part as it was.

Every MCP tool runs through SolidWorksSession.run_guarded: on a failure it
removes what the call added to the current part. Without it a refused call left
its sketch behind, which also counted in the bounding box.
"""

import asyncio

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession


class RecordingSession(SolidWorksSession):
    """Records the guard's steps instead of touching SolidWorks."""

    def __init__(self, left=()):
        super().__init__()
        self.events = []
        self.left = list(left)

    def _history_snapshot(self):
        self.events.append("snapshot")
        return "Part1", {"Sketch1", "Base"}

    def _roll_back(self, snapshot):
        self.events.append(("roll back", snapshot))
        return self.left


def test_a_failed_call_is_rolled_back_to_the_snapshot_taken_before_it():
    session = RecordingSession()

    def fails():
        session.events.append("call")
        raise SolidWorksError("FeatureCut4 failed (None).")

    with pytest.raises(SolidWorksError, match="FeatureCut4"):
        session.run_guarded(fails)

    assert session.events == ["snapshot", "call", ("roll back", ("Part1", {"Sketch1", "Base"}))]


def test_a_successful_call_is_left_alone():
    session = RecordingSession()

    assert session.run_guarded(lambda: "done") == "done"
    assert session.events == ["snapshot"], "a call that worked must not be rolled back"


def test_what_could_not_be_removed_is_named_in_the_error():
    session = RecordingSession(left=["Sketch5"])

    def fails():
        raise SolidWorksError("Point is not on the +z face.")

    with pytest.raises(SolidWorksError, match="not on the .z face. Could not remove what the call added: Sketch5"):
        session.run_guarded(fails)


@pytest.mark.solidworks
def test_a_failed_tool_call_through_the_server_leaves_the_part_as_it_was():
    """End to end, the way an MCP client calls: a pocket drawn beside the block
    fails only after its sketch is complete, which then stayed in the tree and
    stretched the bounding box to x = 110. (A sketch left empty SolidWorks drops
    by itself, so that case proves nothing.)"""
    from solidworks_mcp import server

    def call(tool, *args):
        return asyncio.run(tool(*args))

    call(server.new_part)
    try:
        block = call(server.add_box, 40, 20, 10)
        before = call(server.list_features)["features"]

        refused = call(server.cut_profile, [[100, 100], [110, 100], [110, 110], [100, 110]], 2)

        assert refused["ok"] is False, refused
        assert call(server.list_features)["features"] == before, "the failed call left its sketch in the tree"
        assert call(server.get_bounding_box)["bounding_box_mm"] == block["mass_properties"]["bounding_box_mm"]
    finally:
        call(server.close_part)

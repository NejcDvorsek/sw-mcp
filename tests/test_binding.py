"""Pure unit tests for binding.py -- the COM boundary is faked, no SolidWorks needed."""

import types

import pythoncom
import pytest

from sw_mcp import binding
from sw_mcp.errors import SolidWorksError


class _FakeSldWorks:
    def __init__(self, oleobj):
        self.oleobj = oleobj
        self.Visible = False


@pytest.fixture
def running_solidworks(monkeypatch):
    """Fake a running SolidWorks that reports `revision`; returns the list of
    (major, minor) typelib versions gencache is asked to load."""

    def start(revision):
        requested = []
        raw = types.SimpleNamespace(_oleobj_=object())
        monkeypatch.setattr(binding.win32com.client, "GetActiveObject", lambda progid: raw)
        monkeypatch.setattr(
            binding.win32com.client.dynamic,
            "DumbDispatch",
            lambda oleobj: types.SimpleNamespace(RevisionNumber=revision),
        )

        def ensure_module(guid, lcid, major, minor):
            requested.append((major, minor))
            return types.SimpleNamespace(ISldWorks=_FakeSldWorks, loaded_for=major)

        monkeypatch.setattr(binding.gencache, "EnsureModule", ensure_module)
        monkeypatch.setattr(binding, "_mod", None)
        return requested

    return start


@pytest.mark.parametrize("revision, major", [("34.3.0", 34), ("33.1.0", 33), ("31.5.0", 31)])
def test_connect_loads_the_typelib_of_the_running_solidworks(running_solidworks, revision, major):
    """Regression: the typelib version was hard-coded to 34 (SW 2026), so on any
    other release the first call failed with 'Library not registered'."""
    requested = running_solidworks(revision)

    binding.connect()

    assert requested == [(major, 0)], (
        f"SolidWorks {revision} is running but typelib {requested} was loaded -- "
        "the server only works on the one release whose version is hard-coded"
    )
    assert binding.module().loaded_for == major, (
        "module() must hand the session the wrappers for the SolidWorks it connected to"
    )


def test_connect_unregistered_typelib_is_a_readable_error(running_solidworks, monkeypatch):
    running_solidworks("33.1.0")

    def not_registered(*args):
        raise pythoncom.com_error(-2147319779, "Library not registered.", None, None)

    monkeypatch.setattr(binding.gencache, "EnsureModule", not_registered)

    with pytest.raises(SolidWorksError, match="33"):
        binding.connect()


def test_connect_rejects_an_unparseable_revision(running_solidworks):
    running_solidworks("")

    with pytest.raises(SolidWorksError):
        binding.connect()


def test_module_before_connect_fails_loud(monkeypatch):
    monkeypatch.setattr(binding, "_mod", None)

    with pytest.raises(SolidWorksError):
        binding.module()


# --- invoke(): rebuilding a call from the generated wrapper --------------------


class _OleObj:
    """A wrapped COM object, recording what it is asked to invoke."""

    def __init__(self):
        self.calls = []

    def InvokeTypes(self, dispid, lcid, invkind, result, arguments, *args):
        self.calls.append((dispid, result, arguments, args))
        return "raw"


class _WithDispid:
    """A wrapper with a docstring, as makepy writes most of them: the dispid is
    the int after it (SOLIDWORKS 2026, IModelDoc2.GetFeatureCount: co_consts
    ('Get number of features in tree', 66005, (3, 0), ()))."""

    def __init__(self):
        self._oleobj_ = _OleObj()

    def Method(self, arg=None):
        """docstring"""
        return self._oleobj_.InvokeTypes(1234, 0, 1, (9, 0), (), arg)


def _wrapper_without_a_docstring():
    """Compiled apart so the code object has no docstring: python 3.14 then
    keeps the dispid at index 0 (measured: co_consts == (1234, (9, 0), ())),
    and reading from index 1 onward skipped it."""

    class _WithDispidFirst:
        def __init__(self):
            self._oleobj_ = _OleObj()

        def Method(self, arg=None):
            return self._oleobj_.InvokeTypes(1234, 0, 1, (9, 0), (), arg)

    return _WithDispidFirst


class _WithoutDispid:
    """A wrapper whose code carries no dispid at all, as SOLIDWORKS 2025 writes
    IFeature.GetFirstDisplayDimension: its co_consts are the docstring, None,
    the method name, a result tuple and an argument tuple, and nowhere a dispid
    to rebuild the call from."""

    def __init__(self):
        self.calls = []

    def Method(self, arg=None):
        self.calls.append(arg)
        return "through the wrapper"


def test_invoke_rebuilds_the_call_from_a_dispid_after_the_docstring():
    obj = _WithDispid()

    assert binding.invoke(obj, _WithDispid, "Method", 7) == "raw"
    assert obj._oleobj_.calls == [(1234, (9, 0), (), (7,))], (
        "the fast path must go straight to InvokeTypes, unwrapped: this is the read path over every face and edge"
    )


def test_invoke_finds_a_dispid_that_is_the_first_constant():
    """Regression: the dispid was read from `co_consts[1:]`, so a wrapper whose
    first constant is the dispid raised StopIteration on python 3.14."""
    cls = _wrapper_without_a_docstring()
    obj = cls()

    assert binding.invoke(obj, cls, "Method", 7) == "raw"
    assert obj._oleobj_.calls == [(1234, (9, 0), (), (7,))]


def test_invoke_calls_through_when_the_wrapper_has_no_dispid():
    """Regression: reading the dispid with `next(...)` and no default raised
    StopIteration here, which took read_model, list_dimensions and read_sketch
    down on SOLIDWORKS 2025 (issue #2) while the selftest still passed."""
    obj = _WithoutDispid()

    assert binding.invoke(obj, _WithoutDispid, "Method", 7) == "through the wrapper"
    assert obj.calls == [7], "the generated method must receive the arguments"


def test_invoke_without_a_dispid_and_without_the_method_is_a_readable_error():
    with pytest.raises(SolidWorksError, match=r"_WithoutDispid\.Missing"):
        binding.invoke(_WithoutDispid(), _WithoutDispid, "Missing")

"""Early-binding plumbing for the SolidWorks COM API.

Why this exists: GetActiveObject returns a dispatch whose GetTypeInfo() fails on
this SolidWorks build, so EnsureDispatch/CastTo cannot infer the type. Pure late
binding then breaks for model-level objects -- e.g. IModelDoc2.FirstFeature
raises DISP_E_MEMBERNOTFOUND because the object's default dispinterface does not
expose that name, even though the dispid is valid.

Fix: generate the makepy wrappers from the installed typelib once, then wrap raw
dispatches in the generated interface classes. Early-bound calls go through
InvokeTypes(dispid, ...) and bypass name resolution entirely.

All functions here must be called on the thread that owns the COM apartment
(see com_worker.ComWorker).
"""

import functools

import pythoncom
import win32com.client
from win32com.client import gencache

from .errors import SolidWorksError

# SolidWorks main typelib (sldworks.tlb). Its major version equals the major
# revision of the release (2026 = 34, 2025 = 33, ...), so connect() reads it from
# the running instance instead of pinning one release.
_SLDWORKS_TLB_GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
_SLDWORKS_TLB_LCID = 0

_mod = None


def module():
    """Return the generated sldworks wrapper module (ISldWorks, IModelDoc2, ...)
    for the SolidWorks that connect() attached to."""
    if _mod is None:
        raise SolidWorksError("Not connected to SolidWorks yet; call connect() first.")
    return _mod


def _typelib_major(revision):
    """Typelib major version for a SolidWorks revision string ("34.3.0" -> 34)."""
    head = str(revision).split(".")[0]
    if not head.isdigit():
        raise SolidWorksError(f"Unexpected SolidWorks revision number: {revision!r}.")
    return int(head)


def _load_module(revision):
    major = _typelib_major(revision)
    try:
        mod = gencache.EnsureModule(_SLDWORKS_TLB_GUID, _SLDWORKS_TLB_LCID, major, 0)
    except pythoncom.com_error as exc:
        raise SolidWorksError(
            f"SolidWorks {revision} is running, but its type library (version {major}) is not "
            f"registered. Repair the SolidWorks installation. (COM error: {exc})"
        )
    if mod is None:
        raise SolidWorksError(
            f"Could not load or generate the SolidWorks type library wrappers (version {major}). "
            "Is SolidWorks installed correctly?"
        )
    return mod


def wrap(obj, cls):
    """Re-wrap a raw or dynamic dispatch as an early-bound generated class.

    Idempotent. Returns None unchanged so callers can guard on falsy COM returns
    (many SolidWorks methods return None/False on failure without raising).
    """
    if obj is None:
        return None
    oleobj = getattr(obj, "_oleobj_", obj)
    return cls(oleobj)


@functools.cache
def _dispatch_spec(cls, name: str) -> tuple:
    """(dispid, result type, argument types) of a generated method, read from
    its code: makepy writes the dispid first and then the result and argument
    type tuples.

    `dispid` is None when the wrapper carries none, which SOLIDWORKS 2025 does
    for e.g. IFeature.GetFirstDisplayDimension: its co_consts are
    ('Get first display dimension', None, 'GetFirstDisplayDimension', (9, 0),
    ()). invoke() then lets the generated method build the call itself.

    The dispid is the first int anywhere in co_consts, not the first after a
    leading docstring: on Python 3.14 a wrapper without a docstring keeps it at
    index 0 (cpython 3.14 no longer puts small-int call arguments such as the
    LCID in a generated InvokeTypes(dispid, LCID, 1, ...) into co_consts, so
    the dispid can be first), and skipping index 0 made such a method raise
    StopIteration.
    """
    consts = getattr(getattr(cls, name, None), "__code__", None)
    if consts is None:  # no such method here, or not a generated one: no early path
        return None, None, None
    consts = consts.co_consts
    dispid = next((c for c in consts if isinstance(c, int) and not isinstance(c, bool)), None)
    if dispid is None:
        return None, None, None
    result, arguments = [c for c in consts if isinstance(c, tuple)][:2]
    return dispid, result, arguments


def invoke(obj, cls, name: str, *args):
    """Call cls.name on obj and return what COM returns, objects unwrapped.

    The generated method wraps every object it returns in a CDispatch, which
    asks SolidWorks for its type info: an object array of two faces costs 0.9
    ms that way and 0.13 ms raw (SOLIDWORKS 2026, measured). For reads over
    every face and edge of a part.

    A wrapper that carries no dispid to rebuild that raw call from is called
    through instead: same COM call, one wrapper more. Reading the dispid with
    `next(...)` and no default made every such method raise StopIteration,
    which took the dimension and sketch read paths down on SOLIDWORKS 2025
    (read_model, list_dimensions, read_sketch) while --selftest still passed.
    """
    dispid, result, arguments = _dispatch_spec(cls, name)
    if dispid is None:
        try:
            method = getattr(obj, name)
        except AttributeError as exc:
            raise SolidWorksError(
                f"{cls.__name__}.{name} is not on the object SolidWorks returned: {exc}"
            ) from exc
        return method(*args)
    return getattr(obj, "_oleobj_", obj).InvokeTypes(dispid, 0, 1, result, arguments, *args)


def bare(obj, cls):
    """wrap() without the QueryInterface, for an object with one interface
    (ISurface, ICurve, IDimension): the first QueryInterface of a new object
    goes to SolidWorks and costs some 2 ms, the calls after it 0.1 ms."""
    if obj is None:
        return None
    wrapped = cls.__new__(cls)
    wrapped.__dict__["_oleobj_"] = getattr(obj, "_oleobj_", obj)
    return wrapped


def connect():
    """Attach to a running SolidWorks and return an early-bound ISldWorks.

    Also loads the wrapper module matching that release, which module() returns.
    Raises SolidWorksError with a readable message if SolidWorks is not running.
    """
    global _mod
    try:
        raw = win32com.client.GetActiveObject("SldWorks.Application")
    except pythoncom.com_error as exc:
        raise SolidWorksError(
            "No running SolidWorks found. Start SolidWorks and try again. "
            f"(COM error: {exc})"
        )
    # Read the revision without type info: the matching wrappers aren't loaded yet.
    revision = win32com.client.dynamic.DumbDispatch(raw._oleobj_).RevisionNumber
    _mod = _load_module(revision)
    sw = wrap(raw, _mod.ISldWorks)
    sw.Visible = True
    return sw

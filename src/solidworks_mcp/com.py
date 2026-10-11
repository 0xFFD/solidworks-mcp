"""SolidWorks COM access.

Every call into SolidWorks runs on one dedicated thread that owns the COM apartment, because COM
objects cannot be shared between threads. Interfaces are early bound from the type library of the
running SolidWorks version, so methods with many arguments (FeatureCut4 takes 27) work directly and
zero-argument methods are not mistaken for properties.
"""

from __future__ import annotations

import concurrent.futures
import math
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .errors import SwError

T = TypeVar("T")

SLDWORKS_TLB = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
SWCONST_TLB = "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"

MM = 0.001  # SolidWorks API lengths are metres
DEG = math.pi / 180.0


def mm(v: float | None) -> float:
    return 0.0 if v is None else float(v) * MM


def to_mm(v: float) -> float:
    return round(float(v) / MM, 6)


def rad(v: float | None) -> float:
    return 0.0 if v is None else float(v) * DEG


def to_deg(v: float) -> float:
    return round(float(v) / DEG, 6)


class _ComThread:
    """Single worker thread with COM initialised; all SolidWorks calls are funnelled through it."""

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sw-com", initializer=self._init)

    @staticmethod
    def _init() -> None:
        import pythoncom

        pythoncom.CoInitialize()

    def run(self, fn: Callable[[], T], timeout: float | None = None) -> T:
        if threading.current_thread().name.startswith("sw-com"):
            return fn()
        return self._pool.submit(fn).result(timeout)


COM = _ComThread()


class SolidWorks:
    """Lazily connected SolidWorks session. Use only from the COM thread."""

    def __init__(self, launch: bool = True, visible: bool = True):
        self.launch = launch
        self.visible = visible
        self._app: Any = None
        self._mod: Any = None
        self._C: Any = None

    # ------------------------------------------------------------------ connection

    @property
    def app(self) -> Any:
        if self._app is not None:
            try:
                self._app.RevisionNumber()
                return self._app
            except Exception:  # SolidWorks was closed; reconnect
                self._app = None
        self._connect()
        return self._app

    def _connect(self) -> None:
        import pythoncom
        import win32com.client as wc

        raw = None
        try:
            raw = wc.GetActiveObject("SldWorks.Application")
        except pythoncom.com_error:
            if not self.launch:
                raise SwError("SolidWorks is not running. Start it, or run the server without --no-launch.") from None
            try:
                raw = wc.Dispatch("SldWorks.Application")
            except pythoncom.com_error as e:
                raise SwError(f"Could not start SolidWorks through COM: {e}") from None
        rev = raw.RevisionNumber
        rev = rev() if callable(rev) else rev
        major = _typelib_version(SLDWORKS_TLB, int(str(rev).split(".")[0]))
        self._mod = wc.gencache.EnsureModule(SLDWORKS_TLB, 0, major, 0)
        if self._mod is None:
            raise SwError("Could not load the SolidWorks type library (sldworks.tlb).")
        wc.gencache.EnsureModule(SWCONST_TLB, 0, _typelib_version(SWCONST_TLB, major), 0)
        self._C = wc.constants
        app = self.cast(raw, "ISldWorks")
        if self.visible and not app.Visible:
            app.Visible = True
        self._app = app

    @property
    def C(self) -> Any:  # noqa: N802 - SolidWorks constants, e.g. SW.C.swDocPART
        if self._C is None:
            self.app  # noqa: B018 - connecting loads the constants type library
        return self._C

    def cast(self, obj: Any, iface: str) -> Any:
        """Wrap a COM object in the early-bound class for `iface` (e.g. 'IFace2')."""
        if obj is None:
            return None
        if self._mod is None:
            self.app  # noqa: B018 - connect and load the type library
        import pythoncom

        cls = getattr(self._mod, iface)
        if isinstance(obj, cls):
            return obj
        raw = getattr(obj, "_oleobj_", obj)
        # Ask the object for the interface itself (an ISketch is also an IFeature, a face is an IEntity, ...),
        # so the dispatch IDs used by the wrapper belong to that interface.
        try:
            raw = raw.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch)
        except pythoncom.com_error:
            pass
        return cls(raw)

    def cast_all(self, objs: Any, iface: str) -> list[Any]:
        if not objs:
            return []
        return [self.cast(o, iface) for o in objs]

    def revision(self) -> str:
        return self.app.RevisionNumber()

    def version_label(self) -> str:
        major = int(self.revision().split(".")[0])
        return f"SOLIDWORKS {major + 1992}"

    # ------------------------------------------------------------------ documents

    def active(self, kind: str | None = None) -> Any:
        """Active document as IModelDoc2, optionally requiring 'part', 'assembly' or 'drawing'."""
        doc = self.app.ActiveDoc
        if doc is None:
            raise SwError("No document is open. Use new_document or open_document first.")
        doc = self.cast(doc, "IModelDoc2")
        if kind and doc_kind(doc) != kind:
            raise SwError(f"The active document '{doc.GetTitle()}' is a {doc_kind(doc)}, but this tool needs a {kind}.")
        return doc

    def part(self, doc: Any) -> Any:
        return self.cast(doc, "IPartDoc")

    def assembly(self, doc: Any) -> Any:
        return self.cast(doc, "IAssemblyDoc")

    def drawing(self, doc: Any) -> Any:
        return self.cast(doc, "IDrawingDoc")

    def math(self) -> Any:
        return self.cast(self.app.GetMathUtility(), "IMathUtility")


def _typelib_version(guid: str, wanted: int) -> int:
    """Major version of a registered type library: `wanted` if registered, else the newest one.

    SolidWorks registers its libraries as e.g. "22.0" (hex), which is major 34 for SOLIDWORKS 2026,
    matching the first field of RevisionNumber().
    """
    import winreg

    found: list[int] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"TypeLib\{guid}") as key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    found.append(int(name.split(".")[0], 16))
                except ValueError:
                    pass
    except OSError:
        raise SwError(f"SolidWorks type library {guid} is not registered. Repair the SolidWorks installation.") from None
    if wanted in found:
        return wanted
    if not found:
        raise SwError(f"SolidWorks type library {guid} has no registered versions.")
    return max(found)


def const_names(prefix: str, value: int, bitmask: bool = False) -> list[str]:
    """Names of SolidWorks constants starting with `prefix` that equal `value` (or are set in it)."""
    out = []
    for d in getattr(SW._C, "__dicts__", []):
        for name, v in d.items():
            if not name.startswith(prefix) or not isinstance(v, int):
                continue
            if (bitmask and v and value & v == v) or (not bitmask and v == value):
                out.append(name)
    return sorted(set(out))


def const_name(prefix: str, value: int) -> str:
    names = const_names(prefix, value)
    return names[0].removeprefix(prefix) if names else str(value)


def doc_kind(doc: Any) -> str:
    return {1: "part", 2: "assembly", 3: "drawing"}.get(doc.GetType(), "unknown")


SW = SolidWorks()


def configure(launch: bool, visible: bool) -> None:
    SW.launch = launch
    SW.visible = visible


def on_com(fn: Callable[[], T]) -> T:
    return COM.run(fn)

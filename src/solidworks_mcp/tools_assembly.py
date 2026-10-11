"""Assembly tools."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, const_name, mm, rad, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import NATIVE

MATES = {"coincident": "swMateCOINCIDENT", "concentric": "swMateCONCENTRIC", "perpendicular": "swMatePERPENDICULAR",
         "parallel": "swMatePARALLEL", "tangent": "swMateTANGENT", "distance": "swMateDISTANCE", "angle": "swMateANGLE", "lock": "swMateLOCK"}
ALIGN = {"aligned": "swMateAlignALIGNED", "anti_aligned": "swMateAlignANTI_ALIGNED", "closest": "swMateAlignCLOSEST"}


@tool(READ_ONLY)
def list_components() -> dict:
    """Components of the active assembly with file, position, fixed/suppressed state and mate status."""
    doc = SW.active("assembly")
    rows = []
    for c in g.components(doc):
        row = {"name": c.Name2, "file": c.GetPathName()}
        try:
            t = list(SW.cast(c.Transform2, "IMathTransform").ArrayData)
            row["origin_mm"] = [round(to_mm(v), 3) for v in t[9:12]]
        except Exception:
            pass
        if c.IsFixed():
            row["fixed"] = True
        if c.IsSuppressed():
            row["suppressed"] = True
        rows.append(row)
    return {"assembly": doc.GetTitle(), "components": rows}


@tool(WRITES)
def insert_component(
    path: Annotated[str, Field(description="Part or sub-assembly file to insert.")],
    position: Annotated[list[float], Field(description="Where the component origin goes, assembly mm [x, y, z]. Orientation is unchanged; use mates to orient and constrain it.")] = [0.0, 0.0, 0.0],  # noqa: B006
    fix: Annotated[bool, Field(description="Fix the component in place (usually the first, base component).")] = False,
) -> dict:
    """Insert a part or sub-assembly into the active assembly."""
    a = app()
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    p = a.path(path, must_exist=True, suffixes=(".sldprt", ".sldasm"))
    C = SW.C
    title = doc.GetTitle()
    # the referenced document has to be loaded before AddComponent5
    res = SW.app.OpenDoc6(str(p), NATIVE[p.suffix.lower()], C.swOpenDocOptions_Silent, "", 0, 0)
    loaded = res[0] if isinstance(res, tuple) else res
    if loaded is None:
        raise SwError(f"Could not load {p}.")
    SW.app.ActivateDoc3(title, False, 0, 0)
    x, y, z = (mm(v) for v in (list(position) + [0, 0, 0])[:3])
    comp = asm.AddComponent5(str(p), C.swAddComponentConfigOptions_CurrentSelectedConfig, "", False, "", x, y, z)
    if comp is None:
        raise SwError(f"SolidWorks could not insert {p.name}.")
    comp = SW.cast(comp, "IComponent2")
    # AddComponent5 centres the component's bounding box on the point; move its origin there instead
    # so positions mean the same thing as in the part.
    import pythoncom
    from win32com.client import VARIANT

    arr = list(SW.cast(comp.Transform2, "IMathTransform").ArrayData)
    arr[9:12] = [x, y, z]
    comp.Transform2 = SW.math().CreateTransform(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr))
    doc.EditRebuild3()
    if fix:
        doc.ClearSelection2(True)
        comp.Select4(False, None, False)
        asm.FixComponent()
    elif comp.IsFixed():
        doc.ClearSelection2(True)
        comp.Select4(False, None, False)
        asm.UnfixComponent()
    doc.ClearSelection2(True)
    return {"component": comp.Name2, "file": str(p), "fixed": bool(comp.IsFixed())}


@tool(WRITES)
def add_mate(
    type: Annotated[Literal["coincident", "concentric", "perpendicular", "parallel", "tangent", "distance", "angle", "lock"], Field(description="Mate type.")],
    refs: Annotated[list[str], Field(description="Two references on different components: 'face:Comp-1/N', 'edge:Comp-1/N', 'face@Comp-1/x,y,z', plane names, or 'comp:Name' for lock.")],
    distance: Annotated[float, Field(description="Distance in mm (distance mates).")] = 0.0,
    angle: Annotated[float, Field(description="Angle in degrees (angle mates).")] = 0.0,
    alignment: Annotated[Literal["aligned", "anti_aligned", "closest"], Field(description="Normal alignment.")] = "closest",
    flip: Annotated[bool, Field(description="Flip the dimension side (distance/angle mates).")] = False,
) -> dict:
    """Mate two entities of different components."""
    if len(refs) != 2:
        raise SwError("A mate needs exactly two references.")
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    C = SW.C
    doc.ClearSelection2(True)
    for r in refs:
        g.select(doc, r, mark=1)
    d, ang = mm(distance), rad(angle)
    res = asm.AddMate5(getattr(C, MATES[type]), getattr(C, ALIGN[alignment]), flip, d, d, d, 0.001, 0.001, ang, ang, ang, False, False, 0, 0)
    mate, err = (res if isinstance(res, tuple) else (res, 0))
    doc.ClearSelection2(True)
    if mate is None:
        raise SwError(f"SolidWorks could not add the {type} mate ({const_name('swAddMateError_', err)}). Check that the entities suit this mate type.")
    doc.EditRebuild3()
    return {"mate": _last_mate(doc), "type": type}


def _last_mate(doc) -> str | None:
    """Name of the newest mate (AddMate5 returns an IMate2, which carries no feature name)."""
    name = None
    for f in g.iter_features(doc):
        if f.GetTypeName2() == "MateGroup":
            sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
            while sub is not None:
                name = sub.Name
                sub = SW.cast(sub.GetNextSubFeature(), "IFeature")
    return name


def _prop(model, name: str) -> str:
    try:
        cpm = SW.cast(model.Extension.CustomPropertyManager(""), "ICustomPropertyManager")
        r = cpm.Get6(name, False)
        return (r[2] or r[1] or "").strip()
    except Exception:
        return ""


@tool(READ_ONLY)
def bom(
    top_level_only: Annotated[bool, Field(description="Only direct children of the assembly (sub-assemblies count as one line).")] = True,
) -> dict:
    """Bill of materials with a mass budget: quantity, part number, description, material and mass of
    each part in the active assembly, plus totals."""
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    rows: dict[str, dict] = {}
    for c in SW.cast_all(asm.GetComponents(top_level_only), "IComponent2"):
        if c.IsSuppressed():
            continue
        path = c.GetPathName()
        if path in rows:
            rows[path]["qty"] += 1
            continue
        model = c.GetModelDoc2()
        row: dict = {"qty": 1, "file": path.rsplit("\\", 1)[-1]}
        if model is not None:
            model = SW.cast(model, "IModelDoc2")
            row["part_no"] = _prop(model, "PartNo") or _prop(model, "PartNumber") or None
            row["description"] = _prop(model, "Description") or None
            if model.GetType() == 1:
                m = SW.part(model).GetMaterialPropertyName2("")
                row["material"] = (m[0] if isinstance(m, tuple) else m) or None
            try:
                mp = SW.cast(model.Extension.CreateMassProperty2(), "IMassProperty2")
                row["unit_mass_g"] = round(mp.Mass * 1000, 2)
            except Exception:
                pass
        rows[path] = row
    lines = sorted(rows.values(), key=lambda r: (-(r.get("unit_mass_g") or 0) * r["qty"]))
    for i, r in enumerate(lines, 1):
        r["item"] = i
        if r.get("unit_mass_g") is not None:
            r["total_mass_g"] = round(r["unit_mass_g"] * r["qty"], 2)
    missing = [r["file"] for r in lines if not r.get("material")]
    out = {"assembly": doc.GetTitle(), "lines": lines, "parts": sum(r["qty"] for r in lines),
           "total_mass_g": round(sum(r.get("total_mass_g", 0) for r in lines), 2)}
    if missing:
        out["warning"] = f"No material on {missing}: their mass uses a default density."
    return out


@tool(READ_ONLY)
def interference_check(
    coincident_as_interference: Annotated[bool, Field(description="Report touching faces as interference.")] = False,
) -> dict:
    """Find interfering components in the active assembly, with the interference volume."""
    doc = SW.active("assembly")
    asm = SW.assembly(doc)
    mgr = SW.cast(asm.InterferenceDetectionManager, "IInterferenceDetectionMgr")
    mgr.TreatCoincidenceAsInterference = coincident_as_interference
    mgr.TreatSubAssembliesAsComponents = True
    try:
        items = mgr.GetInterferences() or []
        rows = []
        for it in items:
            it = SW.cast(it, "IInterference")
            comps = [SW.cast(c, "IComponent2").Name2 for c in (it.Components or [])]
            rows.append({"components": comps, "volume_mm3": round(it.Volume * 1e9, 4)})
    finally:
        mgr.Done()
    return {"interferences": len(rows), "items": rows}

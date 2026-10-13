"""Session, document, file and engineering-data tools."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import __version__
from .com import SW, const_name, const_names, doc_kind
from .errors import SwError
from .registry import DESTRUCTIVE, READ_ONLY, WRITES, app, tool

NATIVE = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}

# Neutral formats SaveAs3 can write, with the document kinds they apply to.
EXPORTS = {
    ".step": ("part", "assembly"), ".stp": ("part", "assembly"),
    ".igs": ("part", "assembly"), ".iges": ("part", "assembly"),
    ".x_t": ("part", "assembly"), ".x_b": ("part", "assembly"),
    ".sat": ("part", "assembly"), ".stl": ("part", "assembly"), ".3mf": ("part", "assembly"),
    ".wrl": ("part", "assembly"), ".eprt": ("part",), ".easm": ("assembly",), ".edrw": ("drawing",),
    ".pdf": ("part", "assembly", "drawing"), ".dxf": ("drawing", "part"), ".dwg": ("drawing", "part"),
    ".png": ("part", "assembly", "drawing"), ".jpg": ("part", "assembly", "drawing"), ".tif": ("part", "assembly", "drawing"),
}

UNIT_SYSTEMS = {"mmgs": "swUnitSystem_MMGS", "cgs": "swUnitSystem_CGS", "mks": "swUnitSystem_MKS", "ips": "swUnitSystem_IPS"}
STANDARDS = {"ansi": "swDetailingStandardANSI", "asme": "swDetailingStandardANSI", "iso": "swDetailingStandardISO",
             "din": "swDetailingStandardDIN", "bsi": "swDetailingStandardBS", "jis": "swDetailingStandardJIS", "gb": "swDetailingStandardGB",
             "gost": "swDetailingStandardGOST"}
TEMPLATE_PREFS = {"part": "swDefaultTemplatePart", "assembly": "swDefaultTemplateAssembly", "drawing": "swDefaultTemplateDrawing"}


def doc_summary(doc) -> dict:
    out = {"title": doc.GetTitle(), "kind": doc_kind(doc), "path": doc.GetPathName() or None, "unsaved_changes": bool(doc.GetSaveFlag())}
    return out


def all_docs() -> list:
    out = []
    d = SW.app.GetFirstDocument()
    while d is not None:
        d = SW.cast(d, "IModelDoc2")
        if d.Visible:
            out.append(d)
        d = d.GetNext()
    return out


def find_doc(name: str):
    """Find an open document by title, full path, file name, or (when unambiguous) file stem."""
    docs = all_docs()
    key = name.strip().lower()
    for test in (
        lambda d: key in (d.GetTitle().lower(), (d.GetPathName() or "").lower()),
        lambda d: bool(d.GetPathName()) and Path(d.GetPathName()).name.lower() == key,
    ):
        hits = [d for d in docs if test(d)]
        if hits:
            return hits[0]
    hits = [d for d in docs if Path(d.GetPathName() or d.GetTitle()).stem.lower() == Path(key).stem]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SwError(f"'{name}' matches several documents ({[Path(d.GetPathName() or d.GetTitle()).name for d in hits]}); use the file name with its extension.")
    raise SwError(f"No open document named '{name}'. Open documents: {[d.GetTitle() for d in docs]}")


def unit_system(doc) -> str:
    C = SW.C
    v = SW.cast(doc.Extension, "IModelDocExtension").GetUserPreferenceInteger(C.swUnitSystem, 0)
    return {getattr(C, n): k for k, n in UNIT_SYSTEMS.items()}.get(v, "custom")


def marking(doc) -> str:
    """Export-control marking of a document ('' when absent)."""
    name = app().settings.marking_property
    cpm = SW.cast(doc.Extension.CustomPropertyManager(""), "ICustomPropertyManager")
    try:
        r = cpm.Get6(name, False)
        return (r[2] or r[1] or "").strip()
    except Exception:
        return ""


def load_failure(path: Path) -> str:
    """Why SolidWorks would not load a file: most often another open document has the same file name."""
    for d in SW.app.GetDocuments() or []:
        other = SW.cast(d, "IModelDoc2").GetPathName()
        if other and Path(other).name.lower() == path.name.lower() and Path(other).resolve() != path.resolve():
            return (f"Could not load {path}: a different file with the same name is already open ({other}). SolidWorks "
                    "keeps one document per file name; close it with close_document or rename one of the files.")
    return f"Could not load {path}."


def checked_marking(doc) -> str:
    """The document's export-control marking; raises when the server requires one and it is missing."""
    mark = marking(doc)
    if app().settings.require_marking and not mark:
        raise SwError(
            f"Export blocked: the document has no '{app().settings.marking_property}' custom property, and the server requires an "
            "export-control marking before data leaves SolidWorks. Set it with custom_properties."
        )
    return mark


@tool(READ_ONLY)
def sw_info() -> dict:
    """SolidWorks version, server settings and the open documents (the active one first)."""
    a = app()
    sw = SW.app
    docs = [doc_summary(d) for d in all_docs()]
    active = SW.cast(sw.ActiveDoc, "IModelDoc2") if sw.ActiveDoc is not None else None
    info = {
        "solidworks": SW.version_label(),
        "revision": SW.revision(),
        "server": __version__,
        "workspace": str(a.settings.workspace.resolve()),
        "read_only": a.settings.read_only,
        "require_marking": a.settings.require_marking,
        "audit_log": str(a.settings.audit_path) if a.settings.audit_path else None,
        "active": doc_summary(active) if active is not None else None,
        "documents": docs,
    }
    if active is not None:
        info["active"]["units"] = unit_system(active)
        info["active"]["marking"] = marking(active) or None
    return info


@tool(WRITES)
def new_document(
    kind: Annotated[Literal["part", "assembly", "drawing"], Field(description="Document type.")] = "part",
    units: Annotated[Literal["mmgs", "ips", "cgs", "mks"] | None, Field(description="Unit system shown in SolidWorks: mmgs (mm, g, s) or ips (inch, lb, s). Tool inputs stay in mm either way.")] = "mmgs",
    template: Annotated[str | None, Field(description="Template file; default is the SolidWorks default template for this kind.")] = None,
    sheet: Annotated[str, Field(description="Drawings only: paper size A4, A4V, A3, A2, A1, A0, A, AV, B, C, D or E.")] = "A3",
) -> dict:
    """Create a new part, assembly or drawing and make it active."""
    sw, C = SW.app, SW.C
    tpl = template or sw.GetUserPreferenceStringValue(getattr(C, TEMPLATE_PREFS[kind]))
    if not tpl or not Path(tpl).exists():
        raise SwError(f"No default {kind} template is configured in SolidWorks (Tools > Options > Default Templates). Pass template=<path to .{'prtdot' if kind == 'part' else 'asmdot' if kind == 'assembly' else 'drwdot'}>.")
    if kind == "drawing":
        doc = new_drawing(tpl, sheet)
    else:
        doc = sw.NewDocument(tpl, 0, 0, 0)
    if doc is None:
        raise SwError(f"SolidWorks could not create a {kind} from {tpl}.")
    doc = SW.cast(doc, "IModelDoc2")
    if units and kind != "drawing":
        SW.cast(doc.Extension, "IModelDocExtension").SetUserPreferenceInteger(C.swUnitSystem, 0, getattr(C, UNIT_SYSTEMS[units]))
    out = doc_summary(doc)
    out["template"] = tpl
    return out


def new_drawing(tpl: str, sheet: str) -> Any:
    """A new drawing document without SolidWorks' interactive prompts: the Sheet Format/Size dialog and the
    model view command would wait for a click that never comes and hang the session."""
    sw, C = SW.app, SW.C
    prompts = (C.swDrawingShowSheetFormatDialog, C.swInsertViewForNewDrawing)
    old = [sw.GetUserPreferenceToggle(t) for t in prompts]
    for t in prompts:
        sw.SetUserPreferenceToggle(t, False)
    try:
        return sw.NewDocument(tpl, paper_size(sheet), 0, 0)
    finally:
        for t, v in zip(prompts, old):
            sw.SetUserPreferenceToggle(t, v)


def paper_size(sheet: str) -> int:
    C = SW.C
    key = sheet.strip().upper().replace(" ", "")
    names = {"A4": "swDwgPaperA4size", "A4V": "swDwgPaperA4sizeVertical", "A3": "swDwgPaperA3size", "A2": "swDwgPaperA2size",
             "A1": "swDwgPaperA1size", "A0": "swDwgPaperA0size", "A": "swDwgPaperAsize", "AV": "swDwgPaperAsizeVertical",
             "B": "swDwgPaperBsize", "C": "swDwgPaperCsize", "D": "swDwgPaperDsize", "E": "swDwgPaperEsize"}
    if key not in names:
        raise SwError(f"Unknown sheet size '{sheet}'. Use one of {', '.join(names)}.")
    return getattr(C, names[key])


@tool(WRITES)
def open_document(
    path: Annotated[str, Field(description="Path to a .sldprt, .sldasm or .slddrw file (relative paths are inside the workspace).")],
    read_only: Annotated[bool, Field(description="Open without write access.")] = False,
) -> dict:
    """Open a SolidWorks document and make it active."""
    a = app()
    p = a.path(path, must_exist=True, suffixes=tuple(NATIVE))
    C = SW.C
    opts = C.swOpenDocOptions_Silent | (C.swOpenDocOptions_ReadOnly if read_only else 0)
    res = SW.app.OpenDoc6(str(p), NATIVE[p.suffix.lower()], opts, "", 0, 0)
    doc, err, warn = (res if isinstance(res, tuple) else (res, 0, 0))
    if doc is None:
        raise SwError(f"SolidWorks could not open {p} (error code {err}).")
    doc = SW.cast(doc, "IModelDoc2")
    SW.app.ActivateDoc3(doc.GetTitle(), False, 0, 0)
    out = doc_summary(doc)
    if warn:
        out["warnings_code"] = warn
    return out


IMPORTS = (".step", ".stp", ".igs", ".iges", ".x_t", ".x_b", ".sat", ".stl", ".3mf")


@tool(WRITES)
def import_file(
    path: Annotated[str, Field(description="STEP, IGES, Parasolid (.x_t/.x_b), ACIS (.sat), STL or 3MF file, e.g. a supplier's motor, connector or fastener model.")],
    save_as: Annotated[str | None, Field(description="Save the imported model as this .sldprt/.sldasm (needed before inserting it into an assembly).")] = None,
) -> dict:
    """Import a neutral CAD file as a new SolidWorks part or assembly (bought-in parts, customer data).
    Run check_geometry on the result before using it."""
    a = app()
    p = a.path(path, must_exist=True, suffixes=IMPORTS)
    res = SW.app.LoadFile4(str(p), "r", None, 0)
    doc, err = (res if isinstance(res, tuple) else (res, 0))
    if doc is None:
        raise SwError(f"SolidWorks could not import {p.name} (error code {err}).")
    doc = SW.cast(doc, "IModelDoc2")
    out = doc_summary(doc)
    if doc_kind(doc) == "part":
        out["solid_bodies"] = len(SW.part(doc).GetBodies2(0, True) or [])
        out["surface_bodies"] = len(SW.part(doc).GetBodies2(1, True) or [])
    if save_as:
        target = a.path(save_as, suffixes=(".sldprt", ".sldasm"))
        ok, err, _ = doc.Extension.SaveAs3(str(target), 0, SW.C.swSaveAsOptions_Silent, None, None, 0, 0)
        if not ok:
            raise SwError(f"Imported, but saving to {target} failed: {_save_errors(err)}")
        out["saved"] = doc.GetPathName()
    return out


@tool(WRITES)
def activate_document(name: Annotated[str, Field(description="Title or path of an open document.")]) -> dict:
    """Make an open document the active one."""
    d = find_doc(name)
    SW.app.ActivateDoc3(d.GetTitle(), False, 0, 0)
    return doc_summary(d)


def _save_errors(code: int) -> str:
    hits = [n for prefix in ("swFileSave", "swGenericSaveError", "swReadOnlySaveError", "swFileLockError", "swFileName")
            for n in const_names(prefix, code, bitmask=True)]
    return ", ".join(sorted(set(hits))) or str(code)


@tool(WRITES)
def save_document(
    path: Annotated[str | None, Field(description="Save under this name (.sldprt/.sldasm/.slddrw). Omit to save in place; a new document needs a path.")] = None,
) -> dict:
    """Save the active document in SolidWorks format. Use export for STEP, PDF and other formats."""
    a = app()
    doc = SW.active()
    ext = SW.cast(doc.Extension, "IModelDocExtension")
    C = SW.C
    if path:
        p = a.path(path)
        want = {"part": ".sldprt", "assembly": ".sldasm", "drawing": ".slddrw"}[doc_kind(doc)]
        if p.suffix.lower() != want:
            p = p.with_suffix(want) if p.suffix.lower() in NATIVE or not p.suffix else p
            if p.suffix.lower() != want:
                raise SwError(f"A {doc_kind(doc)} must be saved as {want}. Use export for other formats.")
        ok, err, warn = ext.SaveAs3(str(p), 0, C.swSaveAsOptions_Silent, None, None, 0, 0)
    else:
        if not doc.GetPathName():
            raise SwError("This document has never been saved; pass path='name.sldprt'.")
        ok, err, warn = doc.Save3(C.swSaveAsOptions_Silent, 0, 0)
    if not ok:
        raise SwError(f"Save failed: {_save_errors(err)}")
    return {"saved": doc.GetPathName(), "warnings_code": warn or None}


@tool(WRITES)
def export(
    path: Annotated[str, Field(description="Output file; the extension picks the format: .step/.stp, .igs, .x_t, .x_b, .sat, .stl, .3mf, .pdf, .dxf, .dwg, .png, .jpg, .eprt/.easm/.edrw.")],
    step_ap: Annotated[Literal["auto", "203", "214", "242"], Field(description="STEP application protocol. AP242 (needs a SOLIDWORKS MBD licence) carries PMI and custom properties and is the usual aerospace exchange format; 'auto' uses AP242 when available, otherwise AP214.")] = "auto",
    all_sheets: Annotated[bool, Field(description="Drawings to PDF: export every sheet instead of the active one.")] = True,
) -> dict:
    """Export the active document to a neutral or viewing format (a copy; the document itself is unchanged)."""
    a = app()
    doc = SW.active()
    kind = doc_kind(doc)
    p = a.path(path)
    sfx = p.suffix.lower()
    if sfx not in EXPORTS:
        raise SwError(f"Unsupported export format '{sfx}'. Supported: {', '.join(sorted(EXPORTS))}.")
    if kind not in EXPORTS[sfx]:
        raise SwError(f"A {kind} cannot be exported to {sfx}.")
    mark = marking(doc) if sfx in (".png", ".jpg", ".tif") else checked_marking(doc)
    sw, C = SW.app, SW.C
    ext = SW.cast(doc.Extension, "IModelDocExtension")
    doc.ClearSelection2(True)  # a selection would limit some exports to the selected entities
    out: dict = {"format": sfx.lstrip(".")}
    if sfx in (".step", ".stp"):
        out.update(_export_step(doc, ext, p, step_ap))
    else:
        data = None
        if sfx == ".pdf" and kind == "drawing":
            data = SW.cast(sw.GetExportFileData(C.swExportPdfData), "IExportPdfData")
            data.SetSheets(C.swExportData_ExportAllSheets if all_sheets else C.swExportData_ExportCurrentSheet, None)
        ok, err, warn = ext.SaveAs3(str(p), 0, C.swSaveAsOptions_Silent | C.swSaveAsOptions_Copy, data, None, 0, 0)
        if not ok or not p.exists():
            raise SwError(f"Export to {p.name} failed: {_save_errors(err)}")
    out = {"file": str(p), "bytes": p.stat().st_size, **out}
    if mark:
        out["marking"] = mark
    return out


_STEP_SCHEMAS = {"CONFIG_CONTROL_DESIGN": "AP203", "AUTOMOTIVE_DESIGN": "AP214", "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING": "AP242",
                 "AP203_CONFIGURATION_CONTROLLED_3D_DESIGN_OF_MECHANICAL_PARTS_AND_ASSEMBLY_MIM_LF": "AP203e2"}


def verify_step(p: Path) -> dict:
    """Read back a STEP file: protocol from FILE_SCHEMA, and whether it holds solid geometry."""
    import re

    text = p.read_text(encoding="latin-1", errors="replace")
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", text)
    schema = m.group(1).split(" ")[0].upper() if m else "UNKNOWN"
    ap = next((v for k, v in _STEP_SCHEMAS.items() if schema.startswith(k)), schema)
    return {"step_protocol": ap, "solids": text.count("MANIFOLD_SOLID_BREP") + text.count("BREP_WITH_VOIDS"), "faces": text.count("ADVANCED_FACE")}


def _export_step(doc, ext, p: Path, step_ap: str) -> dict:
    sw, C = SW.app, SW.C
    note = None
    if step_ap in ("auto", "242"):
        data = SW.cast(ext.GetMBDSTEP242Data(), "IMBDSTEP242Data")
        r = None
        target = p if p.suffix.lower() == ".stp" else p.with_name(p.stem + "._ap242_.stp")
        if data is not None:
            data.PublishFileName = str(target)
            data.PublishWithAllCustomProperties = True
            r = ext.PublishSTEP242File2(data)
        if r == 0 and target.exists() and target != p:
            os.replace(target, p)
        if r == 0 and p.exists():
            info = verify_step(p)
            info["custom_properties_included"] = True
            return _check_step(doc, info)
        why = const_name("swPublishStep242_", r) if r is not None else "no MBD data object"
        if step_ap == "242":
            raise SwError(f"STEP AP242 publishing failed ({why}). AP242 needs the SOLIDWORKS MBD licence; use step_ap='214' otherwise.")
        note = f"AP242 not available ({why}); exported AP214"
    ap = 203 if step_ap == "203" else 214
    sw.SetUserPreferenceIntegerValue(C.swStepAP, ap)
    ok, err, _ = ext.SaveAs3(str(p), 0, C.swSaveAsOptions_Silent | C.swSaveAsOptions_Copy, None, None, 0, 0)
    if not ok or not p.exists():
        raise SwError(f"STEP export failed: {_save_errors(err)}")
    info = verify_step(p)
    if note:
        info["note"] = note
    return _check_step(doc, info)


def _check_step(doc, info: dict) -> dict:
    if doc_kind(doc) == "part" and SW.part(doc).GetBodies2(0, True) and info["solids"] == 0:
        raise SwError(f"The STEP file was written but holds no solid geometry ({info}). Rebuild the model and export again.")
    return info


@tool(DESTRUCTIVE)
def close_document(
    name: Annotated[str | None, Field(description="Title or path of the document; default is the active one.")] = None,
    save: Annotated[bool, Field(description="Save before closing. Unsaved changes are discarded when false.")] = False,
) -> dict:
    """Close a document."""
    doc = find_doc(name) if name else SW.active()
    title = doc.GetTitle()
    discarded = bool(doc.GetSaveFlag()) and not save
    if save:
        if not doc.GetPathName():
            raise SwError("This document has never been saved; call save_document(path=...) first.")
        ok, err, _ = doc.Save3(SW.C.swSaveAsOptions_Silent, 0, 0)
        if not ok:
            raise SwError(f"Save failed: {_save_errors(err)}")
    SW.app.CloseDoc(title)
    return {"closed": title, "discarded_changes": discarded}


@tool(WRITES)
def document_settings(
    units: Annotated[Literal["mmgs", "ips", "cgs", "mks"] | None, Field(description="Unit system displayed in SolidWorks.")] = None,
    drafting_standard: Annotated[Literal["asme", "ansi", "iso", "din", "bsi", "jis", "gb", "gost"] | None, Field(description="Overall drafting standard. 'asme'/'ansi' is ASME Y14.5 (US aerospace and defence); 'iso' is ISO GPS.")] = None,
    decimals: Annotated[int | None, Field(ge=0, le=8, description="Decimal places for linear dimensions.")] = None,
) -> dict:
    """Read or change document units, drafting standard and dimension precision."""
    doc = SW.active()
    ext = SW.cast(doc.Extension, "IModelDocExtension")
    C = SW.C
    if units:
        ext.SetUserPreferenceInteger(C.swUnitSystem, 0, getattr(C, UNIT_SYSTEMS[units]))
    if drafting_standard:
        ext.SetUserPreferenceInteger(C.swDetailingDimensionStandard, 0, getattr(C, STANDARDS[drafting_standard]))
    if decimals is not None:
        ext.SetUserPreferenceInteger(C.swUnitsLinearDecimalPlaces, 0, decimals)
    std = ext.GetUserPreferenceInteger(C.swDetailingDimensionStandard, 0)
    std_name = next((k for k, n in STANDARDS.items() if getattr(C, n) == std and k != "ansi"), str(std))
    return {
        "units": unit_system(doc),
        "drafting_standard": std_name,
        "decimals": ext.GetUserPreferenceInteger(C.swUnitsLinearDecimalPlaces, 0),
    }


@tool(WRITES)
def custom_properties(
    set: Annotated[dict[str, str] | None, Field(description="Properties to add or replace, e.g. {'PartNo': 'BRK-1001', 'Revision': 'A', 'Description': 'Bracket', 'ExportControl': 'EAR99'}. An empty string deletes the property.")] = None,
    configuration: Annotated[str, Field(description="Configuration name; '' for file-level properties (what drawings and BOMs use by default).")] = "",
) -> dict:
    """Read and edit custom properties: part number, revision, description, drawn-by and export-control
    marking. Drawing title blocks pick these up through $PRP links."""
    doc = SW.active()
    cpm = SW.cast(doc.Extension.CustomPropertyManager(configuration), "ICustomPropertyManager")
    C = SW.C
    for k, v in (set or {}).items():
        if v == "":
            cpm.Delete2(k)
        else:
            cpm.Add3(k, C.swCustomInfoText, str(v), C.swCustomPropertyReplaceValue)
    props = {}
    for name in cpm.GetNames() or ():
        r = cpm.Get6(name, False)
        props[name] = r[2] if r[2] != r[1] else r[1]
        if r[1] != r[2]:
            props[name] = {"value": r[1], "resolved": r[2]}
    mp = app().settings.marking_property
    return {"configuration": configuration or "(file)", "properties": props, "marking_property": mp, "marked": bool(props.get(mp))}


BUNDLED_DB = Path(__file__).parent / "data" / "aerospace materials.sldmat"


def _custom_material_dirs() -> list[Path]:
    pref = SW.app.GetUserPreferenceStringValue(SW.C.swFileLocationsMaterialDatabases) or ""
    return [Path(d.strip()) for d in pref.split(";") if d.strip()]


def ensure_bundled_materials() -> Path:
    """Copy the bundled aerospace material database into the first custom material folder SolidWorks
    already searches (it only reads databases from those folders). SolidWorks settings are not changed."""
    global _MATERIALS
    data = BUNDLED_DB.read_bytes()
    for d in _custom_material_dirs():
        target = d / BUNDLED_DB.name
        if target.exists() and target.read_bytes() == data:
            return target
    for d in _custom_material_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / BUNDLED_DB.name).write_bytes(data)
            _MATERIALS = None
            return d / BUNDLED_DB.name
        except OSError:
            continue
    raise SwError("Could not install the bundled material database: no writable folder in Tools > Options > File Locations > Material Databases.")


def _material_dbs() -> list[Path]:
    """Built-in databases (install folder), the folders in Tools > Options > File Locations, and the
    bundled aerospace database."""
    sw = SW.app
    dirs: list[Path] = []
    try:
        dirs.append(Path(sw.GetExecutablePath()) / "lang" / (sw.GetCurrentLanguage() or "english") / "sldmaterials")
    except Exception:
        pass
    dirs += _custom_material_dirs()
    out: list[Path] = []
    for d in dirs:
        if d.is_dir():
            out += [f for f in sorted(d.glob("*.sldmat")) if f not in out]
    # List the bundled database from the package (an installed copy may be an older version;
    # set_material refreshes it before use).
    return [f for f in out if f.name.lower() != BUNDLED_DB.name.lower()] + [BUNDLED_DB]


_MATERIALS: list[dict] | None = None


def materials() -> list[dict]:
    global _MATERIALS
    if _MATERIALS is not None:
        return _MATERIALS
    out = []
    for f in _material_dbs():
        try:
            root = ET.fromstring(f.read_bytes())
        except ET.ParseError:
            continue
        db = f.stem
        for cls in root.iter("classification"):
            for mat in cls.iter("material"):
                rec = {"name": mat.get("name"), "class": cls.get("name"), "database": db}
                phys = mat.find("physicalproperties")
                if phys is not None:
                    for tag, key, scale in (("DENS", "density_kg_m3", 1), ("SIGYLD", "yield_mpa", 1e-6), ("SIGXT", "tensile_mpa", 1e-6), ("EX", "modulus_gpa", 1e-9)):
                        el = phys.find(tag)
                        if el is not None and el.get("value"):
                            try:
                                rec[key] = round(float(el.get("value")) * scale, 3)
                            except ValueError:
                                pass
                out.append(rec)
    _MATERIALS = out
    return out


@tool(READ_ONLY)
def list_materials(
    search: Annotated[str | None, Field(description="Case-insensitive text to match in the material or class name, e.g. '7075', 'titanium', 'CFRP', 'PA12', 'generic'.")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 60,
) -> dict:
    """Materials available in the SolidWorks material databases, with density, yield and tensile strength.
    Includes a bundled 'aerospace materials' database of generic composites and engineering polymers
    (CFRP laminates, aramid, G10/FR4, PA12, PEEK-CF): approximate values for sizing and mass budgets."""
    mats = materials()
    if search:
        s = search.lower()
        mats = [m for m in mats if s in (m["name"] or "").lower() or s in (m["class"] or "").lower()]
    return {"count": len(mats), "materials": mats[:limit]}


@tool(WRITES)
def set_material(
    name: Annotated[str, Field(description="Exact material name from list_materials, e.g. '7075-T6 (SN)', 'Ti-6Al-4VSolution treated and aged (SS)' or 'CFRP plate quasi-isotropic (generic)'.")] = "",
    database: Annotated[str | None, Field(description="Material database name; found automatically when omitted.")] = None,
    configuration: Annotated[str, Field(description="Configuration; '' for the active one.")] = "",
    material: Annotated[str, Field(description="Same as name (either may be used).")] = "",
) -> dict:
    """Assign a material to the active part (drives mass properties and the drawing material field)."""
    name = name or material
    if not name:
        raise SwError("Give the material name, e.g. set_material(name='6061-T6 (SS)').")
    doc = SW.active("part")
    requested = name
    if database is None:
        hit = next((m for m in materials() if (m["name"] or "").lower() == name.lower()), None)
        if hit is None:
            # '6061-T6' -> '6061-T6 (SS)': accept a name that matches exactly one entry up to its suffix
            starts = [m for m in materials() if (m["name"] or "").lower().startswith(name.lower().strip() + " ")]
            if len(starts) == 1:
                hit = starts[0]
        if hit is None:
            near = [m["name"] for m in materials() if name.lower()[:4] in (m["name"] or "").lower()][:10]
            raise SwError(f"No material named '{name}'. Similar: {near}. Use list_materials(search=...).")
        name, database = hit["name"], hit["database"]
    if database.lower() == BUNDLED_DB.stem.lower():
        ensure_bundled_materials()
    part = SW.part(doc)
    part.SetMaterialPropertyName2(configuration, database, name)
    got = part.GetMaterialPropertyName2(configuration)
    got_name = got[0] if isinstance(got, tuple) else got
    if (got_name or "").lower() != name.lower():
        raise SwError(f"SolidWorks did not apply '{name}' from '{database}'.")
    out = {"material": got_name, "database": database}
    if got_name.lower() != requested.lower():
        out["note"] = f"'{requested}' matched '{got_name}'."
    return out


@tool(WRITES)
def equations(
    add: Annotated[list[str] | None, Field(description="Equations or global variables to add, in SolidWorks syntax, e.g. '\"WallThk\" = 2.5mm' or '\"D1@Boss-Extrude1\" = \"WallThk\" * 4'.")] = None,
    delete: Annotated[list[int] | None, Field(description="Indices (from the listing) of equations to delete.")] = None,
) -> dict:
    """List, add or delete equations and global variables, then rebuild."""
    doc = SW.active()
    mgr = SW.cast(doc.GetEquationMgr(), "IEquationMgr")
    for i in sorted(delete or [], reverse=True):
        mgr.Delete(i)
    errors = []
    for eq in add or []:
        # Add2 appends for all configurations; Add3 rejects a null configuration list through COM.
        if mgr.Add2(-1, eq, True) < 0:
            errors.append(eq)
    doc.EditRebuild3()
    rows = []
    for i in range(mgr.GetCount()):
        rows.append({"i": i, "equation": mgr.Equation(i), "value": round(mgr.Value(i), 6), "global": bool(mgr.GlobalVariable(i))})
    out = {"equations": rows}
    if errors:
        out["rejected"] = errors
        out["hint"] = "Names go in double quotes and lengths carry units, e.g. '\"Rib\" = 3mm'."
    return out

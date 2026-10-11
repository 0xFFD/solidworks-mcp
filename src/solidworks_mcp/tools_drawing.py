"""GD&T annotations on the model, and 2D drawings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, doc_kind, mm, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import TEMPLATE_PREFS, paper_size

# Symbol codes from the SolidWorks symbol library (gtol.sym); IGtol takes them as text.
GCS = {
    "straightness": "STRAIGHT", "flatness": "FLAT", "circularity": "CIRC", "cylindricity": "CYL",
    "profile_line": "LPROF", "profile_surface": "SPROF", "angularity": "ANGULAR", "perpendicularity": "PERP",
    "parallelism": "PARA", "position": "POSI", "concentricity": "CONC", "symmetry": "SYMMETRY",
    "circular_runout": "SRUN", "total_runout": "TRUN",
}
MC = {"none": "", "mmc": "<MOD-MMC>", "lmc": "<MOD-LMC>"}
_DATUM_RE = re.compile(r"^\s*([A-Z]{1,2}(?:-[A-Z]{1,2})?)\s*(?:[(|]\s*(M|L|S)\s*\)?)?\s*$", re.I)


def _place(ann: Any, face: Any, offset: float = 0.02) -> None:
    """Move a model annotation off the face along its normal so it does not sit inside the part."""
    try:
        box = face.GetBox
        box = box() if callable(box) else box
        c = [(box[i] + box[i + 3]) / 2 for i in range(3)]
        p = list(face.GetClosestPointOn(*c))[:3]
        n = list(face.Normal) if SW.cast(face.GetSurface(), "ISurface").IsPlane() else [0.0, 0.0, 0.0]
        ann.SetPosition2(p[0] + n[0] * offset, p[1] + n[1] * offset, p[2] + n[2] * offset)
    except Exception:
        pass


@tool(WRITES)
def add_datum(
    face: Annotated[str, Field(description="Datum feature face ('face:N' or 'face@x,y,z').")],
    label: Annotated[str, Field(description="Datum letter, e.g. 'A'.")],
) -> dict:
    """Datum feature symbol on a model face (ASME Y14.5 / ISO 5459). Carried into drawings by
    create_drawing and into STEP AP242."""
    doc = SW.active("part")
    ref = g.resolve(doc, face)
    if ref.kind != "face":
        raise SwError(f"{face} is not a face.")
    doc.ClearSelection2(True)
    ref.select(doc, append=False)
    dt = doc.InsertDatumTag2()
    doc.ClearSelection2(True)
    if dt is None:
        raise SwError("SolidWorks could not insert the datum symbol.")
    dt = SW.cast(dt, "IDatumTag")
    dt.SetLabel(label.upper())
    _place(SW.cast(dt.GetAnnotation(), "IAnnotation"), ref.obj)
    return {"datum": label.upper(), "face": face}


def _datum(s: str | None) -> str:
    """'B(M)' -> 'B<MOD-MMC>' (datum reference with its material boundary modifier)."""
    if not s:
        return ""
    m = _DATUM_RE.match(s)
    if not m:
        raise SwError(f"Datum reference '{s}' not understood; use e.g. 'A', 'B(M)', 'C|L'.")
    mod = (m.group(2) or "").upper()
    return m.group(1).upper() + {"M": MC["mmc"], "L": MC["lmc"]}.get(mod, "")


@tool(WRITES)
def add_geometric_tolerance(
    face: Annotated[str, Field(description="Toleranced feature: a face reference.")],
    characteristic: Annotated[Literal["straightness", "flatness", "circularity", "cylindricity", "profile_line", "profile_surface", "angularity",
                                      "perpendicularity", "parallelism", "position", "concentricity", "symmetry", "circular_runout", "total_runout"], Field(description="Geometric characteristic.")],
    tolerance: Annotated[float, Field(description="Tolerance zone size in mm.")],
    datums: Annotated[list[str] | None, Field(description="Up to three datum references in order of precedence, with an optional material condition: ['A', 'B', 'C(M)'].")] = None,
    diameter_zone: Annotated[bool, Field(description="Cylindrical tolerance zone (diameter symbol), typical for position of holes.")] = False,
    material_condition: Annotated[Literal["none", "mmc", "lmc"], Field(description="Material condition modifier on the tolerance.")] = "none",
) -> dict:
    """Feature control frame on a model face (ASME Y14.5 / ISO 1101). Carried into drawings and STEP AP242."""
    doc = SW.active("part")
    ref = g.resolve(doc, face)
    if ref.kind != "face":
        raise SwError(f"{face} is not a face.")
    ds = [_datum(d) for d in (datums or [])[:3]]
    ds += [""] * (3 - len(ds))
    if characteristic in ("flatness", "straightness", "circularity", "cylindricity") and any(ds):
        raise SwError(f"{characteristic} is a form tolerance and takes no datum references.")
    doc.ClearSelection2(True)
    ref.select(doc, append=False)
    gt = doc.InsertGtol()
    doc.ClearSelection2(True)
    if gt is None:
        raise SwError("SolidWorks could not insert the feature control frame.")
    gt = SW.cast(gt, "IGtol")
    gt.SetFrameSymbols2(1, f"<IGTOL-{GCS[characteristic]}>", diameter_zone, MC[material_condition], False, "", "", "", "")
    tol = f"{tolerance:g}"
    gt.SetFrameValues2(1, tol, "", ds[0], ds[1], ds[2])
    shown = gt.GetFrameSymbols3(1) if hasattr(gt, "GetFrameSymbols3") else None
    if shown is not None and GCS[characteristic] not in str(shown):
        raise SwError(f"SolidWorks did not accept the {characteristic} symbol (frame reads {shown}).")
    _place(SW.cast(gt.GetAnnotation(), "IAnnotation"), ref.obj, 0.03)
    frame = f"|{characteristic}|{'Ø' if diameter_zone else ''}{tol}{'(M)' if material_condition == 'mmc' else '(L)' if material_condition == 'lmc' else ''}|" + "|".join(d for d in (datums or []))
    return {"feature_control_frame": frame, "face": face}


@tool(WRITES)
def add_surface_finish(
    face: Annotated[str, Field(description="Face reference.")],
    roughness: Annotated[str, Field(description="Maximum roughness text, e.g. 'Ra 1.6' or '63'.")],
    machining: Annotated[Literal["required", "prohibited", "any"], Field(description="Material removal requirement.")] = "required",
    lay: Annotated[Literal["none", "parallel", "perpendicular", "cross", "multi", "circular", "radial"], Field(description="Lay direction symbol.")] = "none",
) -> dict:
    """Surface texture symbol on a model face (ASME Y14.36 / ISO 1302)."""
    doc = SW.active()
    C = SW.C
    ref = g.resolve(doc, face)
    doc.ClearSelection2(True)
    ref.select(doc, append=False)
    sym = {"required": C.swSFMachining_Req, "prohibited": C.swSFDont_Machine, "any": C.swSFBasic}[machining]
    layv = {"none": C.swSFNone, "parallel": C.swSFParallel, "perpendicular": C.swSFPerp, "cross": C.swSFCross, "multi": C.swSFMultiDir,
            "circular": C.swSFCircular, "radial": C.swSFRadial}[lay]
    p = list(ref.obj.GetClosestPointOn(0.0, 0.0, 0.0))[:3] if ref.kind == "face" else [0.0, 0.0, 0.0]
    sf = doc.Extension.InsertSurfaceFinishSymbol3(sym, C.swSTRAIGHT, p[0], p[1], p[2], layv, 0, "", "", "", "", roughness, "", "")
    doc.ClearSelection2(True)
    if not sf:
        raise SwError("SolidWorks could not insert the surface finish symbol.")
    return {"surface_finish": roughness, "face": face}


@tool(WRITES)
def add_note(
    text: Annotated[str, Field(description="Note text. Use for general notes, e.g. 'UNLESS OTHERWISE SPECIFIED: BREAK SHARP EDGES 0.2-0.5'.")],
    position: Annotated[list[float] | None, Field(description="Drawing: [x, y] in mm from the sheet's lower-left corner. Part: [x, y, z] model mm.")] = None,
) -> dict:
    """Free text note on the active drawing sheet or model."""
    doc = SW.active()
    doc.ClearSelection2(True)
    note = doc.InsertNote(text)
    if note is None:
        raise SwError("SolidWorks could not insert the note.")
    note = SW.cast(note, "INote")
    if position:
        ann = SW.cast(note.GetAnnotation(), "IAnnotation")
        p = [mm(c) for c in position] + [0.0] * (3 - len(position))
        ann.SetPosition2(p[0], p[1], p[2])
    return {"note": text[:80]}


# ------------------------------------------------------------------------------------------ drawings


def _views(drw: Any) -> list[dict]:
    out = []
    v = drw.GetFirstView()  # the sheet itself
    v = SW.cast(v, "IView").GetNextView() if v is not None else None
    while v is not None:
        v = SW.cast(v, "IView")
        o = list(v.GetOutline() or [0, 0, 0, 0])
        row = {"name": v.Name, "orientation": v.GetOrientationName() or None, "scale": round(v.ScaleDecimal, 6),
               "outline_mm": [round(to_mm(c), 2) for c in o[:4]]}
        out.append(row)
        v = v.GetNextView()
    return out


def _sheet_size(drw: Any) -> tuple[float, float]:
    sheet = SW.cast(drw.GetCurrentSheet(), "ISheet")
    props = list(sheet.GetProperties2())
    return props[5], props[6]


def _inherit_settings(drawing_doc: Any, model_path: Path) -> None:
    """Give the drawing the model's unit system, precision and drafting standard (drawing templates
    are often inch/ANSI by default while the part is metric)."""
    model = SW.app.GetOpenDocumentByName(str(model_path))
    if model is None:
        return
    src = SW.cast(SW.cast(model, "IModelDoc2").Extension, "IModelDocExtension")
    dst = SW.cast(drawing_doc.Extension, "IModelDocExtension")
    C = SW.C
    for pref in (C.swDetailingDimensionStandard, C.swUnitSystem, C.swUnitsLinearDecimalPlaces):
        dst.SetUserPreferenceInteger(pref, 0, src.GetUserPreferenceInteger(pref, 0))


def _set_position(view: Any, x: float, y: float) -> None:
    """IView.Position needs an explicit array of doubles; a Python tuple loses the first value."""
    import pythoncom
    from win32com.client import VARIANT

    view.Position = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y])


def _outline(view: Any) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = list(view.GetOutline())[:4]
    return x0, y0, x1, y1


def _move_outline(view: Any, left: float | None = None, right: float | None = None, top: float | None = None, bottom: float | None = None) -> None:
    """Move a view so one side of its outline lands on the given sheet coordinate."""
    x0, y0, x1, y1 = _outline(view)
    px, py = list(view.Position)[:2]
    dx = (left - x0) if left is not None else (right - x1) if right is not None else 0.0
    dy = (top - y1) if top is not None else (bottom - y0) if bottom is not None else 0.0
    _set_position(view, px + dx, py + dy)


def _fit_top_right(view: Any, w: float, h: float, margin: float = 0.015) -> None:
    """Move a view into the upper-right corner of the sheet, halving its scale until it fits."""
    try:
        for _ in range(4):
            x0, y0, x1, y1 = _outline(view)
            if x1 - x0 <= w * 0.38 and y1 - y0 <= h * 0.4:
                break
            view.ScaleDecimal = view.ScaleDecimal / 2
        _move_outline(view, right=w - margin, top=h - 3 * margin)
    except Exception:
        pass


def _place_beside(view: Any, drw: Any, parent: str, direction: str, gap: float = 0.012) -> None:
    """Put a section view in free space: right of every view in the parent's row (vertical cut) or
    below every view in the parent's column (horizontal cut)."""
    try:
        own = view.Name
        x0, y0, x1, y1 = _outline(view)
        others = [v for v in _views(drw) if v["name"] != own]
        par = next(v for v in others if v["name"] == parent)
        px0, py0, px1, py1 = (mm(c) for c in par["outline_mm"])
        if direction == "vertical":
            row = [mm(v["outline_mm"][2]) for v in others if mm(v["outline_mm"][1]) < py1 and mm(v["outline_mm"][3]) > py0]
            _move_outline(view, left=max(row) + gap)
        else:
            col = [mm(v["outline_mm"][1]) for v in others if mm(v["outline_mm"][0]) < px1 and mm(v["outline_mm"][2]) > px0]
            _move_outline(view, top=min(col) - gap)
    except Exception:
        pass


@tool(READ_ONLY)
def drawing_views() -> dict:
    """Views on the active drawing sheet with orientation, scale and outline [xmin, ymin, xmax, ymax] in sheet mm."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    w, h = _sheet_size(drw)
    return {"sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)], "views": _views(drw)}


@tool(WRITES)
def create_drawing(
    model: Annotated[str | None, Field(description="Saved part or assembly file; default is the active document (it must be saved).")] = None,
    sheet: Annotated[str, Field(description="Paper size: A4, A4V, A3, A2, A1, A0, A, AV, B, C, D, E.")] = "A3",
    projection: Annotated[Literal["third", "first"], Field(description="Third angle (ASME, US) or first angle (ISO, Europe).")] = "third",
    isometric: Annotated[bool, Field(description="Add an isometric view.")] = True,
    scale: Annotated[str | None, Field(description="Sheet scale like '1:2' or '2:1'; default chosen by SolidWorks.")] = None,
    annotations: Annotated[bool, Field(description="Import model dimensions, datums, GD&T, surface finish, notes and cosmetic threads into the views.")] = True,
    template: Annotated[str | None, Field(description="Drawing template (.drwdot); default is the SolidWorks default.")] = None,
    save_as: Annotated[str | None, Field(description="Save the drawing as this .slddrw.")] = None,
) -> dict:
    """Create a drawing with the three standard views (and isometric) of a part or assembly, with the
    model's dimensions and GD&T inserted. Review and arrange dimensions before release."""
    a = app()
    sw, C = SW.app, SW.C
    if model:
        mpath = a.path(model, must_exist=True, suffixes=(".sldprt", ".sldasm"))
    else:
        src = SW.active()
        if doc_kind(src) == "drawing":
            raise SwError("The active document is a drawing; pass model=<part or assembly path>.")
        if not src.GetPathName():
            raise SwError("Save the model first (save_document) so the drawing can reference it.")
        if src.GetSaveFlag():
            src.Save3(C.swSaveAsOptions_Silent, 0, 0)
        mpath = Path(src.GetPathName())
    tpl = template or sw.GetUserPreferenceStringValue(getattr(C, TEMPLATE_PREFS["drawing"]))
    if not tpl or not Path(tpl).exists():
        raise SwError("No default drawing template is configured; pass template=<.drwdot>.")
    doc = sw.NewDocument(tpl, paper_size(sheet), 0, 0)
    if doc is None:
        raise SwError("SolidWorks could not create the drawing.")
    doc = SW.cast(doc, "IModelDoc2")
    drw = SW.drawing(doc)
    w, h = _sheet_size(drw)
    sh = SW.cast(drw.GetCurrentSheet(), "ISheet")
    if scale:
        try:
            n, d = (float(x) for x in scale.split(":"))
            sh.SetScale(n, d, True, False)
        except ValueError:
            raise SwError(f"Scale '{scale}' must look like '1:2'.") from None
    ok = (drw.Create3rdAngleViews2 if projection == "third" else drw.Create1stAngleViews2)(str(mpath))
    if not ok:
        raise SwError(f"SolidWorks could not create views of {mpath.name}.")
    _inherit_settings(doc, mpath)
    if isometric:
        iso = drw.CreateDrawViewFromModelView3(str(mpath), "*Isometric", w * 0.78, h * 0.72, 0)
        if iso is not None:
            _fit_top_right(SW.cast(iso, "IView"), w, h)
    if annotations:
        types = 0
        for n in ("swInsertDimensionsMarkedForDrawing", "swInsertDatums", "swInsertGTols", "swInsertSFSymbols", "swInsertNotes",
                  "swInsertCThreads", "swInsertHoleWizardProfileDimensions", "swInsertHoleWizardLocationDimensions", "swInsertholeCallout"):
            types |= getattr(C, n)
        drw.InsertModelAnnotations3(C.swImportModelItemsFromEntireModel, types, True, False, False, False)
    doc.ForceRebuild3(True)
    doc.ViewZoomtofit2()
    out: dict[str, Any] = {"drawing": doc.GetTitle(), "model": str(mpath), "projection": projection,
                           "sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)], "views": _views(drw)}
    if save_as:
        p = a.path(save_as, suffixes=(".slddrw",))
        ok, err, _ = doc.Extension.SaveAs3(str(p), 0, C.swSaveAsOptions_Silent, None, None, 0, 0)
        if not ok:
            raise SwError(f"Drawing created but saving to {p} failed (code {err}).")
        out["saved"] = str(p)
    return out


@tool(WRITES)
def section_view(
    view: Annotated[str, Field(description="Parent view name from drawing_views (e.g. 'Drawing View1').")],
    direction: Annotated[Literal["horizontal", "vertical"], Field(description="Orientation of the cutting line on the sheet.")] = "vertical",
    offset: Annotated[float, Field(description="Offset of the cutting line from the view centre, in model mm.")] = 0.0,
    label: Annotated[str, Field(description="Section label letter.")] = "A",
    flip: Annotated[bool, Field(description="Look in the opposite direction.")] = False,
) -> dict:
    """Full section view through a drawing view; placed beside the parent view."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    target = None
    for v in _views(drw):
        if v["name"] == view:
            target = v
    if target is None:
        raise SwError(f"No view named '{view}'. Use drawing_views.")
    if not drw.ActivateView(view):
        raise SwError(f"Could not activate {view}.")
    x0, y0, x1, y1 = (mm(c) for c in target["outline_mm"])
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    off = mm(offset) * target["scale"]
    pad = 0.005
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        if direction == "vertical":
            line = sm.CreateLine(cx + off, y0 - pad, 0, cx + off, y1 + pad, 0)
            place = (x1 + (x1 - x0) * 0.75, cy)
        else:
            line = sm.CreateLine(x0 - pad, cy + off, 0, x1 + pad, cy + off, 0)
            place = (cx, y0 - (y1 - y0) * 0.75)
    finally:
        sm.AddToDB = False
    if line is None:
        raise SwError("Could not sketch the cutting line.")
    doc.ClearSelection2(True)
    SW.cast(line, "ISketchSegment").Select4(False, None)
    opts = C.swCreateSectionView_ChangeDirection if flip else 0
    sv = drw.CreateSectionViewAt5(place[0], place[1], 0, label, opts, None, 0)
    doc.ClearSelection2(True)
    if sv is None:
        raise SwError("SolidWorks could not create the section view (does the line cross the part?).")
    sv = SW.cast(sv, "IView")
    _place_beside(sv, drw, view, direction)
    o = [round(to_mm(c), 2) for c in _outline(sv)]
    return {"section_view": sv.Name, "label": label, "scale": round(sv.ScaleDecimal, 6), "outline_mm": o}

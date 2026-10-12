"""GD&T annotations on the model, and 2D drawings."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, const_name, doc_kind, mm, to_deg, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import TEMPLATE_PREFS, load_failure, paper_size

# Symbol codes from the SolidWorks symbol library (gtol.sym); IGtol takes them as text.
GCS = {
    "straightness": "STRAIGHT", "flatness": "FLAT", "circularity": "CIRC", "cylindricity": "CYL",
    "profile_line": "LPROF", "profile_surface": "SPROF", "angularity": "ANGULAR", "perpendicularity": "PERP",
    "parallelism": "PARA", "position": "POSI", "concentricity": "CONC", "symmetry": "SYMMETRY",
    "circular_runout": "SRUN", "total_runout": "TRUN",
}
MC = {"none": "", "mmc": "<MOD-MMC>", "lmc": "<MOD-LMC>"}
_DATUM_TARGET_RE = re.compile(r"^[A-Z]{1,2}\d{1,2}$", re.I)
_DATUM_RE = re.compile(r"^\s*([A-Z]{1,2}(?:-[A-Z]{1,2})?)\s*(?:[(|]\s*(M|L|S)\s*\)?)?\s*$", re.I)


def _place(ann: Any, face: Any, offset: float = 0.02, xf: Any = None) -> None:
    """Move a model annotation off the face along its normal so it does not sit inside the part. Component
    faces are in the component's coordinates, so `xf` (its placement) maps them into the assembly."""
    try:
        box = face.GetBox
        box = box() if callable(box) else box
        c = [(box[i] + box[i + 3]) / 2 for i in range(3)]
        p = list(face.GetClosestPointOn(*c))[:3]
        n = list(face.Normal) if SW.cast(face.GetSurface(), "ISurface").IsPlane() else [0.0, 0.0, 0.0]
        if xf is not None:
            p, n = xf.point(p), xf.direction(n)
        ann.SetPosition2(p[0] + n[0] * offset, p[1] + n[1] * offset, p[2] + n[2] * offset)
    except Exception:
        pass


@tool(WRITES)
def add_datum(
    face: Annotated[str, Field(description="Datum feature face ('face:N' or 'face@x,y,z').")],
    label: Annotated[str, Field(description="Datum letter, e.g. 'A'.")],
) -> dict:
    """Datum feature symbol on a model face (ASME Y14.5 / ISO 5459) of a part, or of a component in an
    assembly ('face@Comp-1/x,y,z'). Carried into drawings by create_drawing and into STEP AP242."""
    doc = _annotated_doc()
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
    _place(SW.cast(dt.GetAnnotation(), "IAnnotation"), ref.obj, xf=g.placement(doc, g.component_of(face)))
    return {"datum": label.upper(), "face": face}


def _annotated_doc() -> Any:
    doc = SW.active()
    if doc_kind(doc) == "drawing":
        raise SwError("Put datums and tolerances on the part or assembly; create_drawing carries them into the drawing.")
    return doc


@tool(WRITES)
def add_datum_target(
    face: Annotated[str, Field(description="Face carrying the target ('face@x,y,z' or 'face@Comp-1/x,y,z').")],
    at: Annotated[list[float], Field(description="Target location [x, y, z] in model mm, on the face.")],
    label: Annotated[str, Field(description="Target label, datum letter and number, e.g. 'A1'.")],
    shape: Annotated[Literal["point", "circle", "rectangle"], Field(description="Point target, or a circular or rectangular target area.")] = "point",
    size: Annotated[list[float] | None, Field(description="Target area size in mm: [diameter] for circle, [width, height] for rectangle.")] = None,
) -> dict:
    """Datum target symbol (ASME Y14.5 / ISO 5459) for parts located on points or small areas, such as
    castings, forgings and sheet metal."""
    doc = _annotated_doc()
    C = SW.C
    ref = g.resolve(doc, face)
    if ref.kind != "face":
        raise SwError(f"{face} is not a face.")
    p = [mm(c) for c in at]
    xf = g.placement(doc, g.component_of(face))
    local = xf.local(p) if xf else p
    q = ref.obj.GetClosestPointOn(*local)
    if q is None or sum((a - b) ** 2 for a, b in zip(local, list(q)[:3])) > 1e-10:
        raise SwError(f"{at} is not on {face}.")
    m = _DATUM_TARGET_RE.match(label.strip())
    if not m:
        raise SwError("The label is a datum letter and target number, e.g. 'A1'.")
    sz = [float(v) for v in (size or [])]
    if shape == "circle" and len(sz) != 1 or shape == "rectangle" and len(sz) != 2:
        raise SwError("size is [diameter] for a circle and [width, height] for a rectangle.")
    area = {"point": C.swDatumTargetAreaPoint, "circle": C.swDatumTargetAreaCircle, "rectangle": C.swDatumTargetAreaRectangle}[shape]
    doc.ClearSelection2(True)
    if not doc.Extension.SelectByID2("", "FACE", p[0], p[1], p[2], False, 0, None, 0):
        ref.select(doc, append=False)
    v1, v2 = (mm(sz[0]) if sz else 0.0), (mm(sz[1]) if len(sz) > 1 else 0.0)
    s1 = f"<MOD-DIAM>{sz[0]:g}" if shape == "circle" else (f"{sz[0]:g}x{sz[1]:g}" if shape == "rectangle" else "")
    dt = doc.Extension.InsertDatumTargetSymbol2(label.upper(), "", "", area, False, v1, v2, s1, "", True, C.swCLOSED_ARROWHEAD,
                                      C.swSTRAIGHT, False, shape != "point", True)
    doc.ClearSelection2(True)
    if dt is None:
        raise SwError("SolidWorks could not insert the datum target.")
    _place(SW.cast(SW.cast(dt, "IDatumTargetSym").GetAnnotation(), "IAnnotation"), ref.obj, 0.025, xf)
    return {"datum_target": label.upper(), "shape": shape, "size": sz or None, "at": at}


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
    composite: Annotated[dict[str, Any] | None, Field(description=(
        "Lower segment of a composite frame (position or profile): {tolerance, datums, material_condition}, e.g. "
        "{tolerance: 0.05, datums: ['A'], material_condition: 'mmc'} for a pattern-locating / feature-relating position."))] = None,
    projected_zone: Annotated[float | None, Field(description="Projected tolerance zone height in mm (P modifier), for position of tapped or press-fit holes.")] = None,
    applies_to: Annotated[Literal["face", "all_around", "all_over"], Field(description="Profile tolerances: just the face, all around this side (circle on the leader), or all over the part.")] = "face",
    between: Annotated[list[str] | None, Field(description="Profile tolerances: limit the zone between two labelled points, e.g. ['X', 'Y'] (shows X <-> Y below the frame).")] = None,
    unequal: Annotated[float | None, Field(ge=0, description=(
        "Profile tolerances: unequally disposed zone (ASME Y14.5-2018 U modifier), the part of the tolerance in mm "
        "that lies outside the true profile (toward adding material); shown as 'tolerance (U) unequal'."))] = None,
) -> dict:
    """Feature control frame on a model face (ASME Y14.5 / ISO 1101) of a part or an assembly component,
    with optional composite lower segment and projected tolerance zone. Carried into drawings and STEP AP242."""
    doc = _annotated_doc()
    ref = g.resolve(doc, face)
    if ref.kind != "face":
        raise SwError(f"{face} is not a face.")
    ds = [_datum(d) for d in (datums or [])[:3]]
    ds += [""] * (3 - len(ds))
    if characteristic in ("flatness", "straightness", "circularity", "cylindricity") and any(ds):
        raise SwError(f"{characteristic} is a form tolerance and takes no datum references.")
    if composite and characteristic not in ("position", "profile_surface", "profile_line"):
        raise SwError("Composite frames apply to position and profile tolerances.")
    if composite:
        lower_check = float(composite.get("tolerance", 0))
        if not 0 < lower_check <= tolerance:
            raise SwError("The composite lower segment needs a tolerance above 0 and no larger than the upper one.")
    if (applies_to != "face" or between) and characteristic not in ("profile_surface", "profile_line"):
        raise SwError("all_around, all_over and between apply to profile tolerances.")
    if unequal is not None and (characteristic not in ("profile_surface", "profile_line") or unequal > tolerance):
        raise SwError("unequal applies to profile tolerances and must be between 0 and the tolerance.")
    if between is not None and len(between) != 2:
        raise SwError("between needs two point labels, e.g. ['X', 'Y'].")
    doc.ClearSelection2(True)
    ref.select(doc, append=False)
    gt = doc.InsertGtol()
    doc.ClearSelection2(True)
    if gt is None:
        raise SwError("SolidWorks could not insert the feature control frame.")
    gt = SW.cast(gt, "IGtol")
    gt.SetFrameSymbols2(1, f"<IGTOL-{GCS[characteristic]}>", diameter_zone, MC[material_condition], False, "", "", "", "")
    tol = f"{tolerance:g}"
    if unequal is not None:
        # the GD&T font has no circled U, so it comes from Segoe UI Symbol and the frame font is restored after it
        fmt = SW.cast(SW.cast(gt.GetAnnotation(), "IAnnotation").GetTextFormat(0), "ITextFormat")
        font = (fmt.TypeFaceName if fmt is not None else "") or "Century Gothic"
        tol = f'{tol} <FONT name="Segoe UI Symbol">Ⓤ<FONT name="{font}"> {unequal:g}'
    gt.SetFrameValues2(1, tol, "", ds[0], ds[1], ds[2])
    shown = gt.GetFrameSymbols3(1) if hasattr(gt, "GetFrameSymbols3") else None
    if shown is not None and GCS[characteristic] not in str(shown):
        raise SwError(f"SolidWorks did not accept the {characteristic} symbol (frame reads {shown}).")

    def text(t: float, dz: bool, mc: str, dl: list[str] | None) -> str:
        return f"|{characteristic}|{'Ø' if dz else ''}{t:g}{'(M)' if mc == 'mmc' else '(L)' if mc == 'lmc' else ''}|" + "|".join(dl or [])

    out: dict[str, Any] = {"feature_control_frame": text(tolerance, diameter_zone, material_condition, datums), "face": face}
    if unequal is not None:
        out["feature_control_frame"] = out["feature_control_frame"].replace(f"|{tolerance:g}|", f"|{tolerance:g}(U){unequal:g}|", 1)
    if projected_zone:
        gt.SetPTZHeight2(1, 1, True, f"{projected_zone:g}")
        out["projected_zone"] = projected_zone
    if applies_to == "all_around":
        gt.SetAllAroundThisSide(True)
        out["applies_to"] = applies_to
    elif applies_to == "all_over":
        gt.SetAllOver(True)
        out["applies_to"] = applies_to
    if between:
        gt.SetBetweenTwoPoints(True, str(between[0]).upper(), str(between[1]).upper())
        out["between"] = [str(b).upper() for b in between]
    if composite:
        lower_tol = float(composite["tolerance"])
        lower_mc = str(composite.get("material_condition", material_condition)).lower()
        lower_datums = list(composite.get("datums") or [])
        lds = [_datum(d) for d in lower_datums[:3]] + [""] * (3 - len(lower_datums[:3]))
        gt.SetFrameSymbols2(2, f"<IGTOL-{GCS[characteristic]}>", diameter_zone, MC.get(lower_mc, ""), False, "", "", "", "")
        gt.SetFrameValues2(2, f"{lower_tol:g}", "", lds[0], lds[1], lds[2])
        gt.SetCompositeFrame(True)
        out["composite_lower"] = text(lower_tol, diameter_zone, lower_mc, lower_datums)
    _place(SW.cast(gt.GetAnnotation(), "IAnnotation"), ref.obj, 0.03, g.placement(doc, g.component_of(face)))
    return out


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


def _dimxpert_part(doc: Any) -> Any:
    # re-fetched after every change: DeleteAllTolerances leaves the previous object empty
    cfg = doc.ConfigurationManager.ActiveConfiguration.Name
    mgr = doc.Extension.DimXpertManager(cfg, True)
    if mgr is None:
        raise SwError("DimXpert is not available for this part.")
    return SW.cast(mgr.DimXpertPart, "IDimXpertPart")


_UNIT_MM = {0: 1.0, 1: 10.0, 2: 1000.0, 3: 25.4, 4: 304.8}  # swLengthUnit_e: mm, cm, m, in, ft


def _dimxpert_annotations(doc: Any, part: Any) -> list[dict]:
    """DimXpert reports values in document units; returned here in mm (angles in degrees)."""
    k = _UNIT_MM.get(doc.Extension.GetUserPreferenceInteger(SW.C.swUnitsLinear, 0), 1.0)
    out = []
    for a in part.GetAnnotations() or []:
        a = SW.cast(a, "IDimXpertAnnotation")
        name = a.Name
        row: dict[str, Any] = {"name": name, "kind": re.sub(r"\d*@.*$", "", name)}
        if (label := re.search(r"\(([A-Z]{1,2})\)$", name)) and row["kind"] == "Datum":
            row["label"] = label.group(1)
        f = 1.0 if row["kind"].lower().startswith("angle") else k

        def conv(v: float, f: float = f) -> float:
            return v * f
        if (d := SW.try_cast(a, "IDimXpertDimensionTolerance")) is not None:
            row["nominal"] = round(conv(d.GetNominalValue()), 4)
            pm = d.GetPlusAndMinusTolerance(0.0, 0.0)
            if isinstance(pm, tuple) and len(pm) >= 3 and pm[0]:
                row["plus"], row["minus"] = round(conv(pm[1]), 4), round(conv(pm[2]), 4)
        elif (t := SW.try_cast(a, "IDimXpertTolerance")) is not None:
            row["tolerance"] = round(t.Tolerance * k, 4)
        out.append(row)
    return out


@tool(WRITES)
def dimxpert(
    action: Annotated[Literal["auto", "list", "clear"], Field(description="auto: apply an automatic dimension scheme; list: report the scheme; clear: delete it.")] = "auto",
    datums: Annotated[list[str] | None, Field(description="auto: primary, secondary and tertiary datum faces ('face@x,y,z', planar or cylindrical); at least the primary.")] = None,
    tolerance: Annotated[Literal["geometric", "plus_minus"], Field(description="Geometric: datums with flatness, perpendicularity and position tolerances. plus_minus: size and location dimensions.")] = "geometric",
    part_type: Annotated[Literal["prismatic", "turned"], Field(description="Prismatic (machined block) or turned (lathe part).")] = "prismatic",
    pattern: Annotated[Literal["linear", "polar"], Field(description="How hole patterns are located.")] = "linear",
) -> dict:
    """DimXpert model-based definition: recognises manufacturing features (planes, holes, patterns) and applies a
    complete datum, size and geometric tolerance scheme to the 3D model for MBD and inspection (ASME Y14.41 style).
    Faces turn green when fully toleranced. Replaces any existing DimXpert scheme. Tolerance values come from the
    DimXpert options of the document (block tolerances, default ASME inch), so review them against the drawing's
    general tolerances."""
    doc = SW.active("part")
    if action == "clear":
        _dimxpert_part(doc).DeleteAllTolerances()
        return {"annotations": []}
    if action == "list":
        part = _dimxpert_part(doc)
        return {"features": part.GetFeatureCount(), "annotations": _dimxpert_annotations(doc, part)}
    if not datums or len(datums) > 3:
        raise SwError("An automatic scheme needs 1 to 3 datum faces, primary first.")
    refs = [g.resolve(doc, r) for r in datums]
    _dimxpert_part(doc).DeleteAllTolerances()
    part = _dimxpert_part(doc)
    opt = SW.cast(part.GetAutoDimSchemeOption(), "IDimXpertAutoDimSchemeOption")
    dc = SW.C  # the scheme enums are registered with the other SolidWorks constants once the library is loaded
    opt.PartType = dc.swDimXpertAutoDimSchemePartType_Prismatic if part_type == "prismatic" else dc.swDimXpertAutoDimSchemePartType_Turned
    opt.ToleranceType = dc.swDimXpertAutoDimSchemeToleranceType_Geometric if tolerance == "geometric" else dc.swDimXpertAutoDimSchemeToleranceType_PlusMinus
    opt.PatternType = dc.swDimXpertAutoDimSchemePatternType_Linear if pattern == "linear" else dc.swDimXpertAutoDimSchemePatternType_Polar
    opt.ScopeAllFeature = True
    doc.ClearSelection2(True)
    for i, r in enumerate(refs):  # the datums are taken from selection marks 1, 2 and 3
        r.select(doc, mark=i + 1)
    part.AutoDimensionScheme(opt)  # returns False even when the scheme is applied; judge by the annotations
    doc.ClearSelection2(True)
    part = _dimxpert_part(doc)
    anns = _dimxpert_annotations(doc, part)
    if not anns:
        raise SwError("DimXpert applied no annotations. Pick planar or cylindrical datum faces of the part's main features.")
    return {"features": part.GetFeatureCount(), "annotations": anns}


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


def _load_model(path: Path) -> None:
    """Views can only be made of a model that is loaded; open it silently when it is not."""
    if SW.app.GetOpenDocumentByName(str(path)) is not None:
        return
    C = SW.C
    kind = C.swDocASSEMBLY if path.suffix.lower() == ".sldasm" else C.swDocPART
    active = SW.app.ActiveDoc
    res = SW.app.OpenDoc6(str(path), kind, C.swOpenDocOptions_Silent, "", 0, 0)
    if (res[0] if isinstance(res, tuple) else res) is None:
        raise SwError(load_failure(path))
    if active is not None:
        SW.app.ActivateDoc3(SW.cast(active, "IModelDoc2").GetTitle(), False, 0, 0)


def _set_position(view: Any, x: float, y: float) -> None:
    """IView.Position needs an explicit array of doubles; a Python tuple loses the first value."""
    import pythoncom
    from win32com.client import VARIANT

    view.Position = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y])


def _view_sketch_xy(view: Any, x: float, y: float) -> tuple[float, float]:
    """Sheet coordinates to the coordinates of the view's own sketch, where geometry drawn in an
    activated view lives (it is relative to the view, not the sheet)."""
    sk = view.GetSketch()
    if sk is None:
        return x, y
    arr = list(SW.cast(SW.cast(sk, "ISketch").ModelToSketchTransform, "IMathTransform").ArrayData)
    r, t, sc = [arr[0:3], arr[3:6], arr[6:9]], arr[9:12], arr[12] or 1.0
    p = [x, y, 0.0]
    return tuple(sc * sum(p[j] * r[j][i] for j in range(3)) + t[i] for i in range(2))  # type: ignore[return-value]


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


def _fit_views(drw: Any, w: float, h: float, margin: float = 0.01) -> str | None:
    """Shift every view by the same amount so the group lies on the sheet (alignment is kept)."""
    vs = [SW.cast(v, "IView") for v in (SW.cast(drw.GetCurrentSheet(), "ISheet").GetViews() or [])]
    if not vs:
        return None
    boxes = [_outline(v) for v in vs]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    dx = (margin - x0) if x0 < margin else min(0.0, w - margin - x1)
    dy = (margin - y0) if y0 < margin else min(0.0, h - margin - y1)
    if dx or dy:
        for v in vs:
            px, py = list(v.Position)[:2]
            _set_position(v, px + dx, py + dy)
    if x1 - x0 > w - 2 * margin or y1 - y0 > h - 2 * margin:
        return "The views do not fit on the sheet at this scale; use a smaller scale or a larger sheet."
    return None


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


def _first_gap(spans: list[tuple[float, float]], start: float, size: float, limit: float, gap: float) -> float | None:
    """Lowest position >= start where [pos, pos + size] clears every span (with gap) and ends before limit."""
    pos = start
    for a, b in sorted(spans):
        if b + gap <= pos:
            continue
        if pos + size + gap <= a:
            break
        pos = max(pos, b + gap)
    return pos if pos + size <= limit else None


def _place_beside(view: Any, drw: Any, parent: str, direction: str, gap: float = 0.012) -> None:
    """Put a section view in free space on the sheet, aligned with its parent: in the parent's row for a
    vertical cut (right of the parent, else left), in its column for a horizontal cut (below, else above)."""
    try:
        own = view.Name
        x0, y0, x1, y1 = _outline(view)
        w, h = _sheet_size(drw)
        margin = 0.01
        others = [[mm(c) for c in v["outline_mm"]] for v in _views(drw) if v["name"] != own]
        par = [mm(c) for c in next(v for v in _views(drw) if v["name"] == parent)["outline_mm"]]
        if direction == "vertical":
            row = [(o[0], o[2]) for o in others if o[1] < y1 and o[3] > y0]
            pos = _first_gap(row, par[2] + gap, x1 - x0, w - margin, gap)
            if pos is not None:
                _move_outline(view, left=pos)
            else:  # mirror the search to the left of the parent
                mirrored = [(w - b, w - a) for a, b in row]
                pos = _first_gap(mirrored, w - par[0] + gap, x1 - x0, w - margin, gap)
                _move_outline(view, right=w - pos) if pos is not None else _move_outline(view, left=max(b for _, b in row) + gap)
        else:
            col = [(h - o[3], h - o[1]) for o in others if o[0] < x1 and o[2] > x0]  # measured downwards from the top
            pos = _first_gap(col, h - par[1] + gap, y1 - y0, h - margin, gap)
            if pos is not None:
                _move_outline(view, top=h - pos)
            else:
                up = [(o[1], o[3]) for o in others if o[0] < x1 and o[2] > x0]
                pos = _first_gap(up, par[3] + gap, y1 - y0, h - margin, gap)
                _move_outline(view, bottom=pos) if pos is not None else _move_outline(view, top=min(o[1] for o in others) - gap)
    except Exception:
        pass


@tool(READ_ONLY)
def drawing_views() -> dict:
    """Views on the active drawing sheet with orientation, scale and outline [xmin, ymin, xmax, ymax] in sheet mm."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    w, h = _sheet_size(drw)
    return {"sheet": SW.cast(drw.GetCurrentSheet(), "ISheet").GetName(), "sheets": list(drw.GetSheetNames() or []),
            "sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)], "views": _views(drw)}


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
        _load_model(mpath)
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
    fit_note = None
    if scale:  # the first views auto-scale the sheet; put the requested scale back and keep the views on the sheet
        sh.SetScale(n, d, True, False)
        doc.ForceRebuild3(True)
        fit_note = _fit_views(drw, w, h)
    _inherit_settings(doc, mpath)
    if isometric:
        iso = drw.CreateDrawViewFromModelView3(str(mpath), "*Isometric", w * 0.78, h * 0.72, 0)
        if iso is not None:
            _fit_top_right(SW.cast(iso, "IView"), w, h)
    if annotations:
        types = 0
        for n in ("swInsertDimensionsMarkedForDrawing", "swInsertDatums", "swInsertDatumTargets", "swInsertGTols", "swInsertSFSymbols", "swInsertNotes",
                  "swInsertCThreads", "swInsertHoleWizardProfileDimensions", "swInsertHoleWizardLocationDimensions", "swInsertholeCallout"):
            types |= getattr(C, n)
        drw.InsertModelAnnotations3(C.swImportModelItemsFromEntireModel, types, True, False, False, False)
    doc.ForceRebuild3(True)
    doc.ViewZoomtofit2()
    out: dict[str, Any] = {"drawing": doc.GetTitle(), "model": str(mpath), "projection": projection,
                           "sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)], "views": _views(drw)}
    if fit_note:
        out["warning"] = fit_note
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
    points: Annotated[list[list[float]] | None, Field(description=(
        "Cutting path with corners as model points [[x, y, z], ...] in mm, for half sections (e.g. from beyond the part "
        "along the axis to the centre, then out through the side), offset sections through features that do not line up, and "
        "aligned sections (a leg at an angle, e.g. from the rim to the centre and out through a bolt-circle hole). "
        "The path should start and end outside the part. Overrides direction and offset."))] = None,
) -> dict:
    """Section view through a drawing view: a straight full section, or a half, offset or aligned section
    along a path with corners; placed beside the parent view."""
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
    parent = _view(drw, view)
    if points:
        if len(points) < 2:
            raise SwError("A cutting path needs at least two points.")
        sheet_pts = [_to_sheet(parent, p) for p in points]
        # the first segment's orientation on the sheet decides where the view goes
        dx, dy = sheet_pts[1][0] - sheet_pts[0][0], sheet_pts[1][1] - sheet_pts[0][1]
        direction = "vertical" if abs(dy) >= abs(dx) else "horizontal"
        path = [_view_sketch_xy(parent, *q) for q in sheet_pts]
    elif direction == "vertical":
        path = [_view_sketch_xy(parent, cx + off, y0 - pad), _view_sketch_xy(parent, cx + off, y1 + pad)]
    else:
        path = [_view_sketch_xy(parent, x0 - pad, cy + off), _view_sketch_xy(parent, x1 + pad, cy + off)]
    place = (x1 + (x1 - x0) * 0.75, cy) if direction == "vertical" else (cx, y0 - (y1 - y0) * 0.75)
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        lines = [sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0) for a, b in zip(path, path[1:])]
    finally:
        sm.AddToDB = False
    if not lines or any(ln is None for ln in lines):
        raise SwError("Could not sketch the cutting line.")
    doc.ClearSelection2(True)
    for ln in lines:
        SW.cast(ln, "ISketchSegment").Select4(True, None)
    opts = C.swCreateSectionView_ChangeDirection if flip else 0
    sv = drw.CreateSectionViewAt5(place[0], place[1], 0, label, opts, None, 0)
    doc.ClearSelection2(True)
    if sv is None:
        raise SwError("SolidWorks could not create the section view (does the line cross the part?).")
    sv = SW.cast(sv, "IView")
    _place_beside(sv, drw, view, direction, gap=0.03)  # leave room for the parent view's dimensions
    o = [round(to_mm(c), 2) for c in _outline(sv)]
    doc.ForceRebuild3(True)
    hatched = sv.GetFaceHatchCount()
    out = {"section_view": sv.Name, "label": label, "scale": round(sv.ScaleDecimal, 6), "outline_mm": o, "cut_faces": hatched}
    if not hatched:
        out["warning"] = "The cutting line does not pass through material; move it with offset (model mm from the view centre)."
    return out


# ------------------------------------------------------------------------------------------ more views, sheets, tables

_ORIENTATIONS = {"front": "*Front", "back": "*Back", "top": "*Top", "bottom": "*Bottom", "left": "*Left", "right": "*Right",
                 "isometric": "*Isometric", "trimetric": "*Trimetric", "dimetric": "*Dimetric"}


def _view(drw: Any, name: str) -> Any:
    """IView by name on the active sheet."""
    v = SW.cast(drw.GetFirstView(), "IView")
    v = SW.cast(v.GetNextView(), "IView") if v is not None else None
    while v is not None:
        if v.Name == name:
            return v
        v = SW.cast(v.GetNextView(), "IView")
    raise SwError(f"No view named '{name}' on the active sheet. Use drawing_views.")


def _scale(scale: str | None) -> tuple[float, float] | None:
    if not scale:
        return None
    try:
        n, d = (float(x) for x in scale.split(":"))
    except ValueError:
        raise SwError(f"Scale '{scale}' must look like '2:1'.") from None
    return n, d


def _to_sheet(view: Any, p: list[float]) -> list[float]:
    """Model point (mm) to sheet coordinates (metres) through the view's transform."""
    import pythoncom
    from win32com.client import VARIANT

    pt = SW.cast(SW.math().CreatePoint(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [mm(c) for c in (list(p) + [0, 0, 0])[:3]])), "IMathPoint")
    return list(SW.cast(pt.MultiplyTransform(SW.cast(view.ModelToViewTransform, "IMathTransform")), "IMathPoint").ArrayData)[:2]


def _view_model(view: Any) -> Any:
    m = view.ReferencedDocument
    if m is None:
        raise SwError(f"{view.Name} does not show a model.")
    return SW.cast(m, "IModelDoc2")


def _free_spot(drw: Any, w: float, h: float) -> tuple[float, float]:
    """Centre of the emptiest corner region of the sheet, for a new view without a position."""
    occupied = [[mm(c) for c in v["outline_mm"]] for v in _views(drw)]
    candidates = [(w * fx, h * fy) for fy in (0.75, 0.45) for fx in (0.2, 0.45, 0.7)]

    def clearance(pt: tuple[float, float]) -> float:
        return min((max(o[0] - pt[0], pt[0] - o[2], o[1] - pt[1], pt[1] - o[3]) for o in occupied), default=1.0)

    return max(candidates, key=clearance)


def _view_result(view: Any) -> dict:
    return {"view": view.Name, "scale": round(view.ScaleDecimal, 6), "outline_mm": [round(to_mm(c), 2) for c in _outline(view)]}


@tool(WRITES)
def add_view(
    orientation: Annotated[Literal["front", "back", "top", "bottom", "left", "right", "isometric", "trimetric", "dimetric", "flat_pattern"], Field(description="Standard model view, or flat_pattern for the flat blank of a sheet metal part (with bend lines and bend notes).")],
    model: Annotated[str | None, Field(description="Saved part or assembly; default is the model of the first view on the sheet.")] = None,
    position: Annotated[list[float] | None, Field(description="View centre [x, y] in sheet mm; default is a free area of the sheet.")] = None,
    scale: Annotated[str | None, Field(description="View scale like '1:2'; default is the sheet scale.")] = None,
    configuration: Annotated[str | None, Field(description="Model configuration to show.")] = None,
) -> dict:
    """Place a model view or a sheet metal flat pattern view on the active drawing sheet (for example on a
    second sheet made with sheets(action='add'))."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    if model:
        mpath = app().path(model, must_exist=True, suffixes=(".sldprt", ".sldasm"))
    else:
        first = None
        for sheet_name in drw.GetSheetNames() or []:
            sh = SW.cast(drw.Sheet(sheet_name), "ISheet")
            views = [SW.cast(v, "IView") for v in (sh.GetViews() or [])]
            first = next((v for v in views if v.ReferencedDocument is not None), None)
            if first is not None:
                break
        if first is None:
            raise SwError("The drawing shows no model yet; pass model=<part or assembly path>.")
        mpath = Path(_view_model(first).GetPathName())
    _load_model(mpath)
    w, h = _sheet_size(drw)
    x, y = (mm(position[0]), mm(position[1])) if position else _free_spot(drw, w, h)
    if orientation == "flat_pattern":
        if mpath.suffix.lower() != ".sldprt":
            raise SwError("A flat pattern view needs a sheet metal part.")
        v = drw.CreateFlatPatternViewFromModelView3(str(mpath), configuration or "", x, y, 0, False, False)
        if v is None:
            raise SwError(f"SolidWorks could not make a flat pattern view of {mpath.name}; is it sheet metal?")
    else:
        v = drw.CreateDrawViewFromModelView3(str(mpath), _ORIENTATIONS[orientation], x, y, 0)
        if v is None:
            raise SwError(f"SolidWorks could not make a {orientation} view of {mpath.name}.")
    v = SW.cast(v, "IView")
    if configuration and orientation != "flat_pattern":
        v.ReferencedConfiguration = configuration
    if sc := _scale(scale):
        v.UseSheetScale = False
        v.ScaleDecimal = sc[0] / sc[1]
    doc.ForceRebuild3(True)
    return _view_result(v)


@tool(WRITES)
def projected_view(
    view: Annotated[str, Field(description="Parent view name.")],
    direction: Annotated[Literal["left", "right", "above", "below"], Field(description="Where the new view goes relative to the parent; the projection follows the sheet's first or third angle setting.")],
    gap: Annotated[float, Field(description="Gap between the views in sheet mm.")] = 15.0,
) -> dict:
    """Projected view from an existing view, aligned with it."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    parent = _view(drw, view)
    x0, y0, x1, y1 = _outline(parent)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    gp = mm(gap)
    x, y = {"left": (x0 - gp - (x1 - x0) / 2, cy), "right": (x1 + gp + (x1 - x0) / 2, cy),
            "above": (cx, y1 + gp + (y1 - y0) / 2), "below": (cx, y0 - gp - (y1 - y0) / 2)}[direction]
    doc.ClearSelection2(True)
    doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
    v = drw.CreateUnfoldedViewAt3(x, y, 0, False)
    doc.ClearSelection2(True)
    if v is None:
        raise SwError(f"SolidWorks could not project a view from {view}.")
    v = SW.cast(v, "IView")
    # slide it further out (staying aligned) while it overlaps another view
    for _ in range(10):
        a = _outline(v)
        hits = [[mm(c) for c in o["outline_mm"]] for o in _views(drw) if o["name"] != v.Name]
        hits = [b for b in hits if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]]
        if not hits:
            break
        if direction == "right":
            _move_outline(v, left=max(b[2] for b in hits) + gp)
        elif direction == "left":
            _move_outline(v, right=min(b[0] for b in hits) - gp)
        elif direction == "above":
            _move_outline(v, bottom=max(b[3] for b in hits) + gp)
        else:
            _move_outline(v, top=min(b[1] for b in hits) - gp)
    return _view_result(v)


@tool(WRITES)
def detail_view(
    view: Annotated[str, Field(description="Parent view name.")],
    center: Annotated[list[float], Field(description="Centre of the detail circle as a model point [x, y, z] in mm (a feature visible in the parent view).")],
    radius: Annotated[float, Field(gt=0, description="Radius of the detail circle in model mm.")],
    scale: Annotated[str, Field(description="Detail view scale, e.g. '2:1' or '5:1'.")] = "2:1",
    label: Annotated[str, Field(description="Detail label letter.")] = "B",
    position: Annotated[list[float] | None, Field(description="Detail view centre [x, y] in sheet mm; default is a free area.")] = None,
) -> dict:
    """Enlarged detail view of a circular region of a view (small features, thread runouts, edge breaks)."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    parent = _view(drw, view)
    sx, sy = _view_sketch_xy(parent, *_to_sheet(parent, center))
    if not drw.ActivateView(view):
        raise SwError(f"Could not activate {view}.")
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        circ = sm.CreateCircleByRadius(sx, sy, 0, mm(radius))  # view-sketch space is model-sized
    finally:
        sm.AddToDB = False
    if circ is None:
        raise SwError("Could not sketch the detail circle.")
    doc.ClearSelection2(True)
    SW.cast(circ, "ISketchSegment").Select4(False, None)
    w, h = _sheet_size(drw)
    x, y = (mm(position[0]), mm(position[1])) if position else _free_spot(drw, w, h)
    n, d = _scale(scale) or (2.0, 1.0)
    v = drw.CreateDetailViewAt4(x, y, 0, C.swDetViewSTANDARD, n, d, label, C.swDetCircleCIRCLE, True, False, False, 5)
    doc.ClearSelection2(True)
    if v is None:
        raise SwError("SolidWorks could not create the detail view; check that the centre lies on the part as seen in the view.")
    out = _view_result(SW.cast(v, "IView"))
    out["label"] = label
    return out


@tool(WRITES)
def auxiliary_view(
    view: Annotated[str, Field(description="Parent view name.")],
    edge: Annotated[str, Field(description="Model edge seen as a line in the parent view; the new view looks perpendicular to it ('edge@x,y,z' in model mm).")],
    label: Annotated[str, Field(description="View label letter.")] = "C",
    flip: bool = False,
    position: Annotated[list[float] | None, Field(description="View centre [x, y] in sheet mm; default is a free area.")] = None,
) -> dict:
    """Auxiliary view looking square onto an inclined face, perpendicular to an edge in the parent view."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    parent = _view(drw, view)
    ent = g.resolve(_view_model(parent), edge)
    if ent.kind != "edge":
        raise SwError(f"{edge} is not an edge.")
    doc.ClearSelection2(True)
    if not parent.SelectEntity(ent.obj, False):
        raise SwError(f"{edge} is not visible in {view}.")
    w, h = _sheet_size(drw)
    x, y = (mm(position[0]), mm(position[1])) if position else _free_spot(drw, w, h)
    v = drw.CreateAuxiliaryViewAt2(x, y, 0, False, label, True, flip)
    doc.ClearSelection2(True)
    if v is None:
        raise SwError("SolidWorks could not create the auxiliary view; the edge must appear as a straight line in the parent view.")
    out = _view_result(SW.cast(v, "IView"))
    out["label"] = label
    return out


@tool(WRITES)
def sheets(
    action: Annotated[Literal["list", "add", "activate"], Field(description="list the sheets, add one (it becomes active), or activate one.")] = "list",
    name: Annotated[str | None, Field(description="Sheet name (add, activate).")] = None,
    size: Annotated[str, Field(description="Paper size for a new sheet: A4, A4V, A3, A2, A1, A0, A, AV, B, C, D, E.")] = "A3",
    scale: Annotated[str, Field(description="Sheet scale for a new sheet, e.g. '1:1'.")] = "1:1",
) -> dict:
    """Sheets of the active drawing: list, add (for flat patterns, details or parts lists) or activate."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    names = list(drw.GetSheetNames() or [])
    if action != "list" and not name:
        raise SwError(f"{action} needs a sheet name.")
    if action == "add":
        if name in names:
            raise SwError(f"A sheet named '{name}' already exists.")
        n, d = _scale(scale) or (1.0, 1.0)
        first_angle = bool(list(SW.cast(drw.GetCurrentSheet(), "ISheet").GetProperties2())[4])
        paper = paper_size(size)
        template = getattr(C, "swDwgTemplate" + const_name("swDwgPaper", paper))
        if not drw.NewSheet4(name, paper, template, n, d, first_angle, "", 0, 0, "", 0, 0, 0, 0, 0, 0):
            raise SwError(f"SolidWorks could not add sheet '{name}'.")
        drw.ActivateSheet(name)
    elif action == "activate":
        if name not in names:
            raise SwError(f"No sheet named '{name}'. Sheets: {names}")
        drw.ActivateSheet(name)
    w, h = _sheet_size(drw)
    return {"sheets": list(drw.GetSheetNames() or []), "active": SW.cast(drw.GetCurrentSheet(), "ISheet").GetName(),
            "sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)]}


def _table_template(name: str) -> str:
    """A table template shipped with SolidWorks (lang/<language>/...)."""
    p = Path(SW.app.GetExecutablePath()) / "lang" / SW.app.GetCurrentLanguage() / name
    if not p.exists():
        raise SwError(f"Table template {p} was not found; pass template= with a path to a table template.")
    return str(p)


def _table_rows(ta: Any, limit: int = 30) -> list[list[str]]:
    return [[ta.Text2(r, c, True) for c in range(ta.ColumnCount)] for r in range(min(ta.RowCount, limit))]


_ANCHORS = {"top_left": "swBOMConfigurationAnchor_TopLeft", "top_right": "swBOMConfigurationAnchor_TopRight",
            "bottom_left": "swBOMConfigurationAnchor_BottomLeft", "bottom_right": "swBOMConfigurationAnchor_BottomRight"}


@tool(WRITES)
def drawing_table(
    kind: Annotated[Literal["bom", "revision", "hole"], Field(description="bom: parts list of an assembly view (with balloons); revision: revision block; hole: hole table of a face seen in a view.")],
    view: Annotated[str | None, Field(description="bom and hole: the view the table belongs to.")] = None,
    position: Annotated[list[float] | None, Field(description="Table corner [x, y] in sheet mm (the corner given by anchor).")] = None,
    anchor: Annotated[Literal["top_left", "top_right", "bottom_left", "bottom_right"], Field(description="Which table corner position refers to.")] = "top_right",
    balloons: Annotated[bool, Field(description="bom: add item balloons to the view.")] = True,
    revisions: Annotated[list[dict[str, str]] | None, Field(description="revision: rows to add, e.g. [{'rev': 'A', 'description': 'INITIAL RELEASE', 'approved': 'J. SMITH'}]; the date is filled in.")] = None,
    face: Annotated[str | None, Field(description="hole: model face holding the holes ('face@x,y,z'), seen square in the view.")] = None,
    origin: Annotated[str | None, Field(description="hole: model vertex for the X/Y datum, as a point 'x,y,z' in mm (the nearest vertex is used); default is a corner of the face.")] = None,
    template: Annotated[str | None, Field(description="Table template file; defaults: bom-standard, standard revision block, standard hole table--letters.")] = None,
) -> dict:
    """Tables on the active drawing sheet: bill of materials with balloons, revision block with revision
    rows, or a hole table with tags, positions from a datum corner and hole callouts."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    w, h = _sheet_size(drw)
    use_anchor = position is None
    x, y = (mm(position[0]), mm(position[1])) if position else (0.0, 0.0)
    anc = getattr(C, _ANCHORS[anchor])
    if kind in ("bom", "hole") and not view:
        raise SwError(f"A {kind} table needs view=.")
    out: dict[str, Any] = {"table": kind}
    if kind == "bom":
        v = _view(drw, view)
        if doc_kind(_view_model(v)) != "assembly":
            raise SwError("A bill of materials needs a view of an assembly.")
        tpl = app().path(template, must_exist=True) if template else _table_template("bom-standard.sldbomtbt")
        t = v.InsertBomTable4(use_anchor, x, y, anc, C.swBomType_PartsOnly, v.ReferencedConfiguration or "", str(tpl), False, C.swNumberingType_Detailed, False)
        if t is None:
            raise SwError("SolidWorks could not insert the bill of materials.")
        ta = SW.cast(t, "ITableAnnotation")
        if balloons:
            doc.ClearSelection2(True)
            doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
            made = drw.AutoBalloon5(SW.cast(drw.CreateAutoBalloonOptions(), "IAutoBalloonOptions"))
            doc.ClearSelection2(True)
            out["balloons"] = len(made) if made else 0
    elif kind == "revision":
        sheet = SW.cast(drw.GetCurrentSheet(), "ISheet")
        tpl = app().path(template, must_exist=True) if template else _table_template("standard revision block.sldrevtbt")
        t = sheet.InsertRevisionTable2(use_anchor, x, y, anc, str(tpl), C.swRevisionTable_CircleSymbol, True)
        if t is None:
            raise SwError("SolidWorks could not insert the revision table (a sheet holds one).")
        rt = SW.cast(t, "IRevisionTableAnnotation")
        ta = SW.cast(t, "ITableAnnotation")
        header = [ta.Text2(1, c, True).strip().rstrip(".").upper() for c in range(ta.ColumnCount)]
        cols = {"rev": "REV", "description": "DESCRIPTION", "date": "DATE", "approved": "APPROVED", "zone": "ZONE"}
        for row in revisions or []:
            rev = str(row.get("rev", "")).strip()
            if not rev:
                raise SwError("Each revision row needs a 'rev'.")
            rid = rt.AddRevision(rev)
            r = rt.GetRowNumberForId(rid)
            for key, val in row.items():
                title = cols.get(key.lower())
                if title in header and key.lower() != "rev":
                    ta.SetText(r, header.index(title), str(val))
    else:
        v = _view(drw, view)
        model = _view_model(v)
        if not face:
            raise SwError("A hole table needs face= (the face the holes go through).")
        fref = g.resolve(model, face)
        if fref.kind != "face":
            raise SwError(f"{face} is not a face.")
        verts = []
        for e in SW.cast_all(fref.obj.GetEdges(), "IEdge"):
            for vx in (e.GetStartVertex(), e.GetEndVertex()):
                if vx is not None:
                    verts.append((vx, list(SW.cast(vx, "IVertex").GetPoint())[:3]))
        if not verts:
            raise SwError(f"{face} has no corner vertices to use as the datum.")
        if origin:
            o = [mm(float(c)) for c in origin.replace("@", "").split(",")]
            vtx = min(verts, key=lambda it: sum((a - b) ** 2 for a, b in zip(it[1], o)))[0]
        else:  # lower-left corner as seen in the view
            vtx = min(verts, key=lambda it: sum(_to_sheet(v, [to_mm(c) for c in it[1]])))[0]
        doc.ClearSelection2(True)
        if not (v.SelectEntity(vtx, False) and v.SelectEntity(fref.obj, True)):
            raise SwError(f"Could not select {face} in {view}.")
        sm = SW.cast(doc.SelectionManager, "ISelectionMgr")
        sm.SetSelectedObjectMark(1, 1, C.swSelectionMarkSet)  # datum origin
        sm.SetSelectedObjectMark(2, 2, C.swSelectionMarkSet)  # face with the holes
        tpl = app().path(template, must_exist=True) if template else _table_template("standard hole table--letters.sldholtbt")
        t = v.InsertHoleTable2(use_anchor, x, y, anc, "A", str(tpl))
        doc.ClearSelection2(True)
        if t is None:
            raise SwError("SolidWorks could not insert the hole table.")
        ta = SW.cast(t, "ITableAnnotation")
        if ta.RowCount < 2:
            raise SwError(f"The hole table is empty: no holes go through {face} as seen in {view}.")
    doc.ForceRebuild3(True)
    out["rows"] = _table_rows(ta)
    return out


@tool(WRITES)
def drawing_dimension(
    view: Annotated[str, Field(description="View name.")],
    refs: Annotated[list[str], Field(description="One or two model edges, faces or vertices seen in the view ('edge@x,y,z', 'face@x,y,z' in model mm; a face must be seen edge-on as a line).")],
    position: Annotated[list[float] | None, Field(description="Dimension text position [x, y] in sheet mm; default is just above the view.")] = None,
    orientation: Annotated[Literal["aligned", "horizontal", "vertical"], Field(description="Linear dimension direction on the sheet.")] = "aligned",
) -> dict:
    """Add a reference dimension to a drawing view between model edges, faces or vertices, for sizes the
    imported model dimensions do not show (overall envelope, hole-to-edge distances)."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    v = _view(drw, view)
    model = _view_model(v)
    if not 1 <= len(refs) <= 2:
        raise SwError("Give one or two references.")
    doc.ClearSelection2(True)
    for i, r in enumerate(refs):
        ent = g.resolve(model, r)
        if ent.kind not in ("edge", "face"):
            raise SwError(f"{r} is not an edge or face.")
        if not v.SelectEntity(ent.obj, i > 0):
            raise SwError(f"{r} is not visible in {view}.")
    if position:
        x, y = mm(position[0]), mm(position[1])
    else:
        x0, y0, x1, y1 = _outline(v)
        x, y = (x0 + x1) / 2, y1 + 0.012
    from .tools_sketch import _no_dim_prompt

    add = {"aligned": doc.AddDimension2, "horizontal": doc.AddHorizontalDimension2, "vertical": doc.AddVerticalDimension2}[orientation]
    dd = _no_dim_prompt(lambda: add(x, y, 0))
    doc.ClearSelection2(True)
    if dd is None:
        raise SwError("SolidWorks could not dimension that selection; use edges seen as lines or points in the view.")
    d = SW.cast(SW.cast(dd, "IDisplayDimension").GetDimension2(0), "IDimension")
    angular = d.GetType() == SW.C.swDimensionParamTypeDoubleAngular
    val = d.SystemValue
    return {"dimension": "@".join(d.FullName.split("@")[:2]), "value": round(to_deg(val), 4) if angular else round(to_mm(val), 4),
            "unit": "deg" if angular else "mm"}


@tool(WRITES)
def broken_view(
    view: Annotated[str, Field(description="View name.")],
    start: Annotated[list[float], Field(description="Model point [x, y, z] in mm where the removed section begins.")],
    end: Annotated[list[float], Field(description="Model point [x, y, z] in mm where it ends; the part between the two is cut out of the view.")],
    style: Annotated[Literal["zigzag", "small_zigzag", "straight", "curve", "jagged"], Field(description="Break line style.")] = "zigzag",
) -> dict:
    """Broken view: shorten a long part (spar, stringer, tube, rail) by cutting out a uniform middle
    section between two model points. The break runs across the direction in which the points differ most
    on the sheet."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    v = _view(drw, view)
    before = [round(to_mm(c), 2) for c in _outline(v)]
    a, b = _to_sheet(v, start), _to_sheet(v, end)
    px, py = list(v.Position)[:2]
    horizontal_gap = abs(b[0] - a[0]) >= abs(b[1] - a[1])
    if max(abs(b[0] - a[0]), abs(b[1] - a[1])) < 1e-4:
        raise SwError("start and end are at the same place in this view.")
    # break positions are offsets from the view's position on the sheet
    p1, p2 = sorted((a[0] - px, b[0] - px) if horizontal_gap else (a[1] - py, b[1] - py))
    orient = C.swBreakLineVertical if horizontal_gap else C.swBreakLineHorizontal
    styles = {"zigzag": C.swBreakLine_ZigZag, "small_zigzag": C.swBreakLine_SmallZigZag, "straight": C.swBreakLine_Straight,
              "curve": C.swBreakLine_Curve, "jagged": C.swBreakLine_Jagged}
    doc.ClearSelection2(True)
    doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
    if v.InsertBreak3(orient, p1, p2, styles[style], 1, False) is None:
        doc.ClearSelection2(True)
        raise SwError("SolidWorks could not insert the break; both points must lie within the view.")
    doc.ClearSelection2(True)
    doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
    drw.BreakView()
    doc.ClearSelection2(True)
    doc.ForceRebuild3(True)
    out = _view_result(v)
    out["outline_before_mm"] = before
    return out


@tool(WRITES)
def breakout_section(
    view: Annotated[str, Field(description="View name.")],
    center: Annotated[list[float], Field(description="Centre of the cut-away region as a model point [x, y, z] in mm.")],
    radius: Annotated[float, Field(gt=0, description="Radius of the circular cut-away region in model mm.")],
    depth: Annotated[float, Field(gt=0, description="How deep to cut, in model mm from the front of the part as seen in the view (reach the bore, pocket or thread to show).")],
) -> dict:
    """Break-out section: cut away a circular region of a view to a depth, showing an internal bore, pocket
    or insert without a separate section view."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    v = _view(drw, view)
    sx, sy = _view_sketch_xy(v, *_to_sheet(v, center))
    if not drw.ActivateView(view):
        raise SwError(f"Could not activate {view}.")
    sm = doc.SketchManager
    sm.AddToDB = True
    try:
        circ = sm.CreateCircleByRadius(sx, sy, 0, mm(radius))
    finally:
        sm.AddToDB = False
    if circ is None:
        raise SwError("Could not sketch the break-out region.")
    hatched = v.GetFaceHatchCount()
    doc.ClearSelection2(True)
    SW.cast(circ, "ISketchSegment").Select4(False, None)
    made = drw.CreateBreakOutSection(mm(depth))
    doc.ClearSelection2(True)
    doc.ForceRebuild3(True)
    if made is None:
        raise SwError("SolidWorks could not create the break-out section; the region must overlap the part in the view.")
    out = _view_result(v)
    out["cut_faces"] = v.GetFaceHatchCount() - hatched
    if out["cut_faces"] <= 0:
        out["warning"] = "The break-out cut no material; increase depth or move the centre onto the part."
    return out


def _view_dims(v: Any) -> list[tuple[str, Any, Any]]:
    """(short name, IDisplayDimension, IDimension) for every dimension shown in a view."""
    out = []
    dd = v.GetFirstDisplayDimension5()
    while dd is not None:
        dd = SW.cast(dd, "IDisplayDimension")
        dim = SW.cast(dd.GetDimension2(0), "IDimension")
        if dim is not None:
            out.append(("@".join(dim.FullName.split("@")[:2]), dd, dim))
        dd = dd.GetNext5()
    return out


@tool(WRITES)
def view_dimensions(
    view: Annotated[str, Field(description="View name.")],
    move: Annotated[dict[str, list[float]] | None, Field(description="Dimensions to move: {name: [x, y]} with the new text position in sheet mm (names as listed, e.g. 'D1@Sketch1').")] = None,
    tolerance: Annotated[dict[str, dict[str, Any]] | None, Field(description=(
        "Tolerances: {name: {type, plus, minus}} in mm (degrees for angles). Types: none, basic (boxed theoretically exact), "
        "symmetric (± plus), bilateral (+plus / -minus), limit (shown as upper and lower size), min, max; ISO fits with "
        "{type: fit | fit_with_tolerance | fit_tolerance_only, hole: 'H7', shaft: 'g6'}."))] = None,
    decimals: Annotated[dict[str, int] | None, Field(description="Decimal places per dimension, {name: places}; the tolerance uses the same.")] = None,
) -> dict:
    """List the dimensions shown in a drawing view with their values, tolerances and text positions (sheet
    mm); move them to tidy crowded annotations, and set tolerances (±, bilateral, limits, basic) and precision."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    v = _view(drw, view)
    dims = _view_dims(v)
    by_name = {n: (dd, dim) for n, dd, dim in dims}
    for name, pos in (move or {}).items():
        if name not in by_name:
            raise SwError(f"No dimension '{name}' in {view}. Dimensions: {sorted(by_name)}")
        if len(pos) < 2:
            raise SwError(f"{name}: the position is [x, y] in sheet mm.")
        ann = SW.cast(by_name[name][0].GetAnnotation(), "IAnnotation")
        if not ann.SetPosition2(mm(pos[0]), mm(pos[1]), 0.0):
            raise SwError(f"SolidWorks could not move {name}.")
    C = SW.C
    tol_types = {"none": C.swTolNONE, "basic": C.swTolBASIC, "symmetric": C.swTolSYMMETRIC, "bilateral": C.swTolBILAT,
                 "limit": C.swTolLIMIT, "min": C.swTolMIN, "max": C.swTolMAX, "fit": C.swTolFIT,
                 "fit_with_tolerance": C.swTolFITWITHTOL, "fit_tolerance_only": C.swTolFITTOLONLY}
    for name, spec in (tolerance or {}).items():
        if name not in by_name:
            raise SwError(f"No dimension '{name}' in {view}. Dimensions: {sorted(by_name)}")
        kind = str(spec.get("type", "symmetric")).lower()
        if kind not in tol_types:
            raise SwError(f"{name}: tolerance type must be one of {', '.join(tol_types)}.")
        dim = by_name[name][1]
        angular = dim.GetType() == C.swDimensionParamTypeDoubleAngular
        conv = (lambda v: math.radians(float(v))) if angular else (lambda v: mm(float(v)))
        tol = SW.cast(dim.Tolerance, "IDimensionTolerance")
        tol.Type = tol_types[kind]
        if kind.startswith("fit"):
            hole, shaft = str(spec.get("hole", "")), str(spec.get("shaft", ""))
            if not (hole or shaft):
                raise SwError(f"{name}: a fit needs hole (e.g. 'H7') and/or shaft (e.g. 'g6') classes.")
            if not tol.SetFitValues(hole, shaft):
                raise SwError(f"SolidWorks rejected the fit {hole}/{shaft} on {name}.")
        elif kind in ("symmetric", "bilateral", "limit", "min", "max"):
            plus = float(spec.get("plus", 0))
            minus = float(spec.get("minus", plus if kind == "symmetric" else 0))
            if plus <= 0 and minus <= 0:
                raise SwError(f"{name}: give plus (and minus) tolerance values above 0.")
            if not tol.SetValues(-conv(abs(minus)), conv(plus)):
                raise SwError(f"SolidWorks rejected the tolerance on {name}.")
    for name, places in (decimals or {}).items():
        if name not in by_name:
            raise SwError(f"No dimension '{name}' in {view}. Dimensions: {sorted(by_name)}")
        by_name[name][0].SetPrecision3(int(places), -1, int(places), -1)
    if move or tolerance or decimals:
        doc.EditRebuild3()
        doc.GraphicsRedraw2()
    names_tol = {v: k for k, v in tol_types.items()}
    rows = []
    for name, dd, dim in _view_dims(v):
        angular = dim.GetType() == SW.C.swDimensionParamTypeDoubleAngular
        val = dim.SystemValue
        row = {"name": name, "value": round(to_deg(val), 4) if angular else round(to_mm(val), 4), "unit": "deg" if angular else "mm",
               "position_mm": [round(to_mm(c), 2) for c in list(SW.cast(dd.GetAnnotation(), "IAnnotation").GetPosition())[:2]]}
        tol = SW.cast(dim.Tolerance, "IDimensionTolerance")
        if tol is not None and tol.Type in names_tol and tol.Type != C.swTolNONE:
            back = to_deg if angular else to_mm
            row["tolerance"] = {"type": names_tol[tol.Type]}
            if tol.Type in (C.swTolFIT, C.swTolFITWITHTOL, C.swTolFITTOLONLY):
                row["tolerance"].update(hole=tol.GetHoleFitValue() or None, shaft=tol.GetShaftFitValue() or None)
            if tol.Type not in (C.swTolBASIC, C.swTolFIT):
                row["tolerance"].update(plus=round(back(tol.GetMaxValue()), 4) + 0.0, minus=round(-back(tol.GetMinValue()), 4) + 0.0)
        rows.append(row)
    out: dict[str, Any] = {"view": view, "outline_mm": [round(to_mm(c), 2) for c in _outline(v)], "dimensions": rows}
    if move:
        out["moved"] = sorted(move)
    return out

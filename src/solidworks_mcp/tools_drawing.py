"""GD&T annotations on the model, and 2D drawings."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from . import shapes
from .com import SW, const_name, doc_kind, mm, to_deg, to_mm
from .errors import SwError
from .registry import READ_ONLY, WRITES, app, tool
from .tools_doc import TEMPLATE_PREFS, load_failure, new_drawing, paper_size

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


def _dimxpert_set_tolerances(doc: Any, tolerances: dict[str, dict[str, float]]) -> dict:
    """Plus/minus tolerances on DimXpert size and location annotations, through the model dimension each one shows as."""
    C = SW.C
    if not tolerances:
        raise SwError("Give tolerances={annotation name: {plus, minus}}; dimxpert(action='list') gives the names.")
    part = _dimxpert_part(doc)
    by_name = {SW.cast(a, "IDimXpertAnnotation").Name: SW.cast(a, "IDimXpertAnnotation") for a in part.GetAnnotations() or []}
    for name, spec in tolerances.items():
        a = by_name.get(name)
        if a is None:
            raise SwError(f"No DimXpert annotation '{name}'. Annotations: {sorted(by_name)}")
        if SW.try_cast(a, "IDimXpertDimensionTolerance") is None:
            raise SwError(f"{name} is a geometric tolerance; its value follows the DimXpert options (SolidWorks stores an "
                          "edited frame value inconsistently with what the frame shows), so change it with add_geometric_tolerance.")
        ann = SW.try_cast(a.GetDisplayEntity(), "IAnnotation")  # the annotation it shows as; its specific one is the dimension
        disp = SW.try_cast(ann.GetSpecificAnnotation(), "IDisplayDimension") if ann is not None and ann.GetType() == C.swDisplayDimension else None
        if disp is None:
            raise SwError(f"{name} has no dimension in the model to carry its tolerance.")
        dim = SW.cast(disp.GetDimension2(0), "IDimension")
        plus, minus = float(spec.get("plus", 0)), abs(float(spec.get("minus", spec.get("plus", 0))))
        if plus < 0 or (plus == 0 and minus == 0):
            raise SwError(f"{name}: give plus (and minus) in mm.")
        angular = dim.GetType() == C.swDimensionParamTypeDoubleAngular
        conv = math.radians if angular else mm
        tol = SW.cast(dim.Tolerance, "IDimensionTolerance")
        tol.Type = C.swTolSYMMETRIC if abs(plus - minus) < 1e-12 else C.swTolBILAT
        if not tol.SetValues(-conv(minus), conv(plus)):
            raise SwError(f"SolidWorks rejected the tolerance on {name}.")
    doc.EditRebuild3()
    anns = _dimxpert_annotations(doc, _dimxpert_part(doc))
    got = {r["name"]: r for r in anns}
    for name, spec in tolerances.items():
        r = got.get(name, {})
        plus, minus = float(spec.get("plus", 0)), abs(float(spec.get("minus", spec.get("plus", 0))))
        if abs(r.get("plus", math.nan) - plus) > 1e-4 or abs(abs(r.get("minus", math.nan)) - minus) > 1e-4:
            raise SwError(f"{name} reads back as +{r.get('plus')}/{r.get('minus')}, not +{plus}/-{minus}.")
    return {"changed": sorted(tolerances), "annotations": anns}


@tool(WRITES)
def dimxpert(
    action: Annotated[Literal["auto", "list", "clear", "set_tolerance"], Field(description=(
        "auto: apply an automatic dimension scheme; list: report the scheme; clear: delete it; set_tolerance: change the "
        "plus/minus tolerance of size and location annotations by name."))] = "auto",
    datums: Annotated[list[str] | None, Field(description="auto: primary, secondary and tertiary datum faces ('face@x,y,z', planar or cylindrical); at least the primary.")] = None,
    tolerance: Annotated[Literal["geometric", "plus_minus"], Field(description="Geometric: datums with flatness, perpendicularity and position tolerances. plus_minus: size and location dimensions.")] = "geometric",
    part_type: Annotated[Literal["prismatic", "turned"], Field(description="Prismatic (machined block) or turned (lathe part).")] = "prismatic",
    pattern: Annotated[Literal["linear", "polar"], Field(description="How hole patterns are located.")] = "linear",
    tolerances: Annotated[dict[str, dict[str, float]] | None, Field(description=(
        "set_tolerance: {annotation name from list: {plus: mm, minus: mm}} (minus as a positive number; equal values "
        "give a symmetric tolerance), e.g. {'Diameter1@Hole Pattern1(6.6)': {'plus': 0.05, 'minus': 0}}."))] = None,
) -> dict:
    """DimXpert model-based definition: recognises manufacturing features (planes, holes, patterns) and applies a
    complete datum, size and geometric tolerance scheme to the 3D model for MBD and inspection (ASME Y14.41 style).
    Faces turn green when fully toleranced. Replaces any existing DimXpert scheme. Tolerance values come from the
    DimXpert options of the document (block tolerances, default ASME inch), so review them against the drawing's
    general tolerances, and adjust size and location tolerances one by one with action='set_tolerance'."""
    doc = SW.active("part")
    if action == "clear":
        _dimxpert_part(doc).DeleteAllTolerances()
        return {"annotations": []}
    if action == "list":
        part = _dimxpert_part(doc)
        return {"features": part.GetFeatureCount(), "annotations": _dimxpert_annotations(doc, part)}
    if action == "set_tolerance":
        return _dimxpert_set_tolerances(doc, tolerances or {})
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


_ANN_KINDS = {"swDatumTag": "datum", "swGTol": "feature_control_frame", "swSFSymbol": "surface_finish", "swNote": "note",
              "swDisplayDimension": "dimension", "swDatumTargetSym": "datum_target", "swCThread": "cosmetic_thread"}
_VIEW_DIRS = {"front": "swFrontView", "back": "swBackView", "left": "swLeftView", "right": "swRightView", "top": "swTopView", "bottom": "swBottomView"}


def _annotation_row(a: Any) -> dict:
    a = SW.cast(a, "IAnnotation")
    t = a.GetType()
    kind = next((v for k, v in _ANN_KINDS.items() if getattr(SW.C, k, None) == t), "other")
    row: dict[str, Any] = {"name": a.GetName(), "kind": kind}
    try:
        if kind == "datum":
            row["text"] = SW.cast(a.GetSpecificAnnotation(), "IDatumTag").GetLabel()
        elif kind == "feature_control_frame":
            vals = SW.cast(a.GetSpecificAnnotation(), "IGtol").GetFrameValues(1)
            row["text"] = "|".join(str(x) for x in vals if x)
    except Exception:
        pass
    return row


@tool(WRITES)
def annotation_views(
    action: Annotated[Literal["list", "create"], Field(description="list the model's annotation views and what each holds; create one and move annotations into it.")] = "list",
    direction: Annotated[Literal["front", "back", "left", "right", "top", "bottom"] | None, Field(description="create: the direction the view looks from.")] = None,
    annotations: Annotated[list[str] | None, Field(description="create: annotation names to move into the new view (from list, e.g. 'DetailItem2').")] = None,
) -> dict:
    """3D annotation views for model-based definition: group the model's datums, feature control frames and
    notes into views that each face one direction, so the annotations read flat to the viewer and carry into
    3D PDF and STEP AP242 presentation."""
    doc = SW.active()
    if doc_kind(doc) == "drawing":
        raise SwError("Annotation views belong to parts and assemblies.")
    ext = doc.Extension

    def views() -> list[dict]:
        out = []
        for i, v in enumerate(ext.AnnotationViews or []):
            v = SW.cast(v, "IAnnotationView")
            out.append({"index": i, "unassigned": bool(v.UnassignedView), "annotations": [_annotation_row(a) for a in (v.Annotations or [])]})
        return out

    if action == "list":
        return {"views": views()}
    if not direction:
        raise SwError("create needs direction= (front, back, left, right, top or bottom).")
    by_name = {}
    a = doc.GetFirstAnnotation2()
    while a is not None:
        a = SW.cast(a, "IAnnotation")
        by_name[a.GetName()] = a
        a = a.GetNext3()
    missing = [n for n in annotations or [] if n not in by_name]
    if missing:
        raise SwError(f"No annotation named {', '.join(missing)}. Annotations: {sorted(by_name)}")
    from .tools_features import _dispatch_array

    v = ext.InsertAnnotationView(getattr(SW.C, _VIEW_DIRS[direction]), None, False, None, 0)
    if v is None:
        raise SwError("SolidWorks could not create the annotation view.")
    v = SW.cast(v, "IAnnotationView")
    if annotations and not v.MoveAnnotations(_dispatch_array([by_name[n] for n in annotations])):
        raise SwError("SolidWorks could not move the annotations into the new view (they must face that direction's plane).")
    doc.GraphicsRedraw2()
    held = sorted(SW.cast(x, "IAnnotation").GetName() for x in (v.Annotations or []))
    if sorted(annotations or []) != held:
        raise SwError(f"The new view holds {held} instead of {sorted(annotations or [])}.")
    return {"direction": direction, "annotations": held, "views": views()}


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


_FORMAT_STYLES = ("iso", "ansi", "din", "jis", "bsi", "gb")


def _format_file(sheet_format: str, size: str) -> Path:
    """Sheet format file (.slddrt) for a style name and paper size from SolidWorks' format folder, or a given path.
    The file must exist: for a missing one SolidWorks opens its Sheet Format/Size dialog and waits for a click."""
    if sheet_format.lower().endswith(".slddrt"):
        return app().path(sheet_format, must_exist=True, suffixes=(".slddrt",))
    style = sheet_format.strip().lower()
    if style not in _FORMAT_STYLES:
        raise SwError(f"sheet_format must be one of {', '.join(_FORMAT_STYLES)} or a .slddrt file.")
    key = size.strip().upper().replace(" ", "")
    prefix = {"A4V": "a4", "AV": "a"}.get(key, key.lower())
    if style == "ansi":
        style = "portrait" if key in ("A4V", "AV") else "landscape"
    folders = [f for f in (SW.app.GetUserPreferenceStringValue(SW.C.swFileLocationsSheetFormat) or "").split(";") if f]
    for folder in folders:
        f = Path(folder) / f"{prefix} - {style}.slddrt"
        if f.exists():
            return f
    have = sorted({x.stem for folder in folders if Path(folder).is_dir() for x in Path(folder).glob(f"{prefix} - *.slddrt")})
    raise SwError(f"No '{style}' sheet format for size {size}" + (f"; available: {', '.join(have)}." if have else ".") +
                  " Pass a .slddrt file instead.")


def _apply_format(doc: Any, drw: Any, fmt: Path, size: str) -> int:
    """Put a sheet format (border, zones, title block) on the active sheet, keeping its size, scale and projection.
    Returns the number of format notes; raises if the format did not load or does not fit the sheet."""
    C = SW.C
    sh = SW.cast(drw.GetCurrentSheet(), "ISheet")
    p = list(sh.GetProperties2())
    if not fmt.exists():  # SolidWorks would ask for one in a dialog
        raise SwError(f"Sheet format {fmt} does not exist.")
    if not drw.SetupSheet5(sh.GetName(), paper_size(size), C.swDwgTemplateCustom, p[2], p[3], bool(p[4]), str(fmt), p[5], p[6], "Default", True):
        raise SwError(f"SolidWorks could not apply the sheet format {fmt.name}.")
    sh.SheetFormatVisible = True
    doc.ForceRebuild3(True)
    notes = _format_notes(drw)
    if not notes:
        raise SwError(f"The sheet format {fmt.name} did not load.")
    w, h = _sheet_size(drw)
    if any(n[2] > w + 1e-3 or n[3] > h + 1e-3 for n in notes):
        raise SwError(f"The sheet format {fmt.name} is larger than the {size} sheet; choose a format for that size.")
    return len(notes)


def _format_notes(drw: Any) -> list[tuple[float, float, float, float]]:
    """Extents (x0, y0, x1, y1, metres) of the sheet format's notes: zone labels and title block fields."""
    out = []
    n = SW.cast(drw.GetFirstView(), "IView").GetFirstNote()
    while n is not None:
        n = SW.cast(n, "INote")
        e = n.GetExtent()
        if e:
            e = list(e)
            out.append((min(e[0], e[3]), min(e[1], e[4]), max(e[0], e[3]), max(e[1], e[4])))
        n = n.GetNext()
    return out


def _drawing_area(drw: Any) -> tuple[float, float, float, float]:
    """Usable part of the sheet (metres): inside the sheet format's border, found from the zone labels that run
    between its frames (ISO formats have a wider binding margin on the left), or 12 mm in from the edges."""
    w, h = _sheet_size(drw)
    x0, y0, x1, y1 = 0.012, 0.012, w - 0.012, h - 0.012
    edge, pad = 0.03, 0.004
    notes = _format_notes(drw)

    def labels(cands: list[tuple[float, float]]) -> list[float]:
        """The zone labels of one side: the largest group of notes in a line (same offset from the edge); title
        block fields near an edge sit at assorted offsets and are left out."""
        groups: dict[int, list[float]] = {}
        for key, val in cands:
            groups.setdefault(round(key * 1000), []).append(val)
        best = max(groups.values(), key=len, default=[])
        return best if len(best) >= 2 else []

    left = labels([(n[0], n[2]) for n in notes if n[2] < edge and n[1] > edge and n[3] < h - edge])
    right = labels([(n[0], n[0]) for n in notes if n[0] > w - edge and n[1] > edge and n[3] < h - edge])
    bottom = labels([(n[1], n[3]) for n in notes if n[3] < edge and n[0] > edge and n[2] < w - edge])
    top = labels([(n[1], n[1]) for n in notes if n[1] > h - edge and n[0] > edge and n[2] < w - edge])
    return (max(left) + pad if left else x0, max(bottom) + pad if bottom else y0,
            min(right) - pad if right else x1, min(top) - pad if top else y1)


def _link_drawing_number(drw: Any, model: Any) -> str | None:
    """Point the title block's drawing number (linked to the file name in SolidWorks' formats) at the model's part
    number property, as drawings are numbered by part number. Returns the property used, or None."""
    if model is None:
        return None
    cpm = SW.cast(SW.cast(model, "IModelDoc2").Extension.CustomPropertyManager(""), "ICustomPropertyManager")
    names = {n.lower(): n for n in cpm.GetNames() or ()}
    prop = next((names[k.lower()] for k in _PART_NO_PROPS if k.lower() in names and cpm.Get6(names[k.lower()], False)[2]), None)
    if prop is None:
        return None
    linked = False
    n = SW.cast(drw.GetFirstView(), "IView").GetFirstNote()
    while n is not None:
        n = SW.cast(n, "INote")
        text = str(n.PropertyLinkedText or "")
        if '$PRP:"SW-File Name"' in text:
            n.PropertyLinkedText = text.replace('$PRP:"SW-File Name"', f'$PRPSHEET:"{prop}"')
            linked = True
        n = n.GetNext()
    return prop if linked else None


def _title_block(drw: Any) -> tuple[float, float, float, float] | None:
    """Box (metres) around the title block: the format notes that are not zone labels along the border."""
    w, h = _sheet_size(drw)
    edge = 0.025
    inner = [n for n in _format_notes(drw) if n[0] > edge and n[1] > edge and n[2] < w - edge and n[3] < h - edge]
    if not inner:
        return None
    pad = 0.004
    return (min(n[0] for n in inner) - pad, min(n[1] for n in inner) - pad, max(n[2] for n in inner) + pad, max(n[3] for n in inner) + pad)


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
        SW.activate(active)


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


def _fit_views(drw: Any, w: float, h: float) -> str | None:
    """Shift every view by the same amount (alignment is kept) so the group lies inside the drawing area (inside the
    sheet format's border) and, where there is room, off the title block."""
    vs = [SW.cast(v, "IView") for v in (SW.cast(drw.GetCurrentSheet(), "ISheet").GetViews() or [])]
    if not vs:
        return None
    ax0, ay0, ax1, ay1 = _drawing_area(drw)

    def shift(dx: float, dy: float) -> None:
        for v in vs:
            px, py = list(v.Position)[:2]
            _set_position(v, px + dx, py + dy)

    boxes = [_outline(v) for v in vs]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    dx = (ax0 - x0) if x0 < ax0 else min(0.0, ax1 - x1)
    dy = (ay0 - y0) if y0 < ay0 else min(0.0, ay1 - y1)
    if dx or dy:
        shift(dx, dy)
    if x1 - x0 > ax1 - ax0 or y1 - y0 > ay1 - ay0:
        return "The views do not fit inside the border at this scale; use a smaller scale or a larger sheet."
    tb = _title_block(drw)
    if tb:
        gap = 0.004
        boxes = [_outline(v) for v in vs]
        hit = [b for b in boxes if _overlap(b, tb, gap)]
        if hit:
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            up = tb[3] + gap - min(b[1] for b in hit)
            left = max(b[2] for b in hit) - (tb[0] - gap)
            if y1 + up <= ay1:
                shift(0.0, up)
            elif x0 - left >= ax0:
                shift(-left, 0.0)
            else:
                return "A view overlaps the title block at this scale; use a smaller scale or a larger sheet."
    return None


_STANDARD_SCALES = ((20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50), (1, 100), (1, 200))  # ISO 5455


def _auto_scale(doc: Any, drw: Any, sheet: Any, isometric: bool) -> str | None:
    """Pick the largest ISO 5455 scale at which the standard views, with room between and around them for
    dimensions and half the area to spare, fit the drawing area clear of the title block (SolidWorks' own choice is often a size or two
    small), then lay the views out from the bottom left. Returns the scale chosen, or None to keep SolidWorks'."""
    vs = [SW.cast(v, "IView") for v in (sheet.GetViews() or [])]
    if not vs:
        return None
    front = next((v for v in vs if (v.GetOrientationName() or "") in ("*Front", "*Back")), vs[0])
    s0 = front.ScaleDecimal
    o0 = {v.Name: _outline(v) for v in vs}
    s1 = s0 * 2
    sheet.SetScale(s1 if s1 >= 1 else 1, 1 if s1 >= 1 else 1 / s1, True, False)
    doc.ForceRebuild3(True)
    o1 = {v.Name: _outline(v) for v in vs}

    def size(v: Any, s: float) -> tuple[float, float]:
        a, b = o0[v.Name], o1[v.Name]
        res = []
        for i in (0, 1):
            ext = ((b[i + 2] - b[i]) - (a[i + 2] - a[i])) / (s1 - s0)  # model extent; the outline adds a fixed pad
            res.append((a[i + 2] - a[i]) + ext * (s - s0))
        return res[0], res[1]

    fx, fy = list(front.Position)[:2]
    row, col = [], []  # views beside the front view (left/right), and above/below it
    for v in vs:
        if v.Name != front.Name:
            px, py = list(v.Position)[:2]
            (row if abs(px - fx) >= abs(py - fy) else col).append((v, px > fx if abs(px - fx) >= abs(py - fy) else py > fy))
    ax0, ay0, ax1, ay1 = _drawing_area(drw)
    aw, ah = ax1 - ax0, ay1 - ay0
    tb = _title_block(drw)
    gap, margin = 0.018, 0.012

    def group(s: float) -> tuple[float, float]:
        fw, fh = size(front, s)
        return (fw + sum(size(v, s)[0] + gap for v, _ in row), fh + sum(size(v, s)[1] + gap for v, _ in col))

    def fits(s: float) -> bool:
        gw, gh = group(s)
        if gw + 2 * margin > aw or gh + 2 * margin > ah or gw > 0.8 * aw or gh > 0.8 * ah or gw * gh > 0.5 * aw * ah:
            return False  # half the area is left for sections, details, tables and notes
        if tb and gw + 2 * margin > tb[0] - ax0 and gh + 2 * margin > ay1 - tb[3]:
            return False  # neither beside nor above the title block
        return not isometric or (aw - gw) >= 0.25 * aw or (ah - gh) >= 0.25 * ah

    chosen = next(((n, d) for n, d in _STANDARD_SCALES if fits(n / d)), None)
    if chosen is None:
        sheet.SetScale(s0 if s0 >= 1 else 1, 1 if s0 >= 1 else 1 / s0, True, False)
        doc.ForceRebuild3(True)
        return None
    n, d = chosen
    s = n / d
    sheet.SetScale(n, d, True, False)
    doc.ForceRebuild3(True)
    gw, gh = group(s)
    fw, fh = size(front, s)
    left = ax0 + margin + sum(size(v, s)[0] + gap for v, right in row if not right)
    bottom = ay0 + margin + sum(size(v, s)[1] + gap for v, up in col if not up)
    if tb and gw + 2 * margin > tb[0] - ax0:  # too wide to sit beside the title block: above it
        bottom = max(bottom, tb[3] + margin + sum(size(v, s)[1] + gap for v, up in col if not up))
    _move_outline(front, left=left)
    _move_outline(front, bottom=bottom)
    x0, y0, x1, y1 = _outline(front)
    for v, right in row:
        _move_outline(v, left=x1 + gap) if right else _move_outline(v, right=x0 - gap)
    for v, up in col:
        _move_outline(v, bottom=y1 + gap) if up else _move_outline(v, top=y0 - gap)
    return f"{n:g}:{d:g}"


def _taken_boxes(drw: Any, own: str) -> list[tuple]:
    """Sheet boxes (metres) a new view must keep clear of: the other views, the title block and the tables."""
    boxes = [tuple(mm(c) for c in v["outline_mm"]) for v in _views(drw) if v["name"] != own]
    if tb := _title_block(drw):
        boxes.append(tb)
    sv = SW.cast(drw.GetFirstView(), "IView")
    boxes += [b for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation") if (b := _table_box(t)) is not None]
    return boxes


def _is_clear(view: Any, drw: Any, gap: float = 0.004) -> bool:
    o = _outline(view)
    a = _drawing_area(drw)
    inside = a[0] <= o[0] + 1e-6 and o[2] <= a[2] + 1e-6 and a[1] <= o[1] + 1e-6 and o[3] <= a[3] + 1e-6
    return inside and not any(_overlap(o, b, gap) for b in _taken_boxes(drw, view.Name))


def _place_free(view: Any, drw: Any, goal: tuple[float, float], gap: float = 0.008) -> bool:
    """Move a view (unaligned if need be) to the free spot inside the border nearest goal; False if there is none."""
    x0, y0, x1, y1 = _outline(view)
    spot = _free_box(_drawing_area(drw), x1 - x0, y1 - y0, _taken_boxes(drw, view.Name), goal, gap=gap)
    if spot is None:
        return False
    try:
        view.RemoveAlignment()
    except Exception:
        pass
    _move_outline(view, left=spot[0])
    _move_outline(view, top=spot[1])
    return _is_clear(view, drw, gap / 2)


def _fit_top_right(view: Any, w: float, h: float, area: tuple[float, float, float, float] | None = None, margin: float = 0.015) -> None:
    """Move a view into the upper-right corner of the drawing area, halving its scale until it fits."""
    try:
        for _ in range(4):
            x0, y0, x1, y1 = _outline(view)
            if x1 - x0 <= w * 0.38 and y1 - y0 <= h * 0.4:
                break
            view.ScaleDecimal = view.ScaleDecimal / 2
        right, top = (area[2] - 0.005, area[3] - 0.01) if area else (w - margin, h - 3 * margin)
        _move_outline(view, right=right, top=top)
        drw = SW.drawing(SW.active("drawing"))
        for _ in range(3):  # the corner is taken (a wide side view, a table): the nearest free spot, smaller if need be
            if _is_clear(view, drw) or _place_free(view, drw, (right, top)):
                break
            view.ScaleDecimal = view.ScaleDecimal / 2
            _move_outline(view, right=right, top=top)
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


def _place_beside(view: Any, drw: Any, parent: str, direction: str, gap: float = 0.012) -> bool:
    """Put a section view in free space on the sheet, aligned with its parent: in the parent's row for a
    vertical cut (right of the parent, else left), in its column for a horizontal cut (below, else above)."""
    try:
        own = view.Name
        x0, y0, x1, y1 = _outline(view)
        w, h = _sheet_size(drw)
        ax0, ay0, ax1, ay1 = _drawing_area(drw)  # inside the sheet format's border
        others = [[mm(c) for c in v["outline_mm"]] for v in _views(drw) if v["name"] != own]
        tb = _title_block(drw)
        sv = SW.cast(drw.GetFirstView(), "IView")
        others += ([list(tb)] if tb else []) + [list(b) for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation")
                                                if (b := _table_box(t)) is not None]
        par = [mm(c) for c in next(v for v in _views(drw) if v["name"] == parent)["outline_mm"]]
        if direction == "vertical":
            row = [(o[0], o[2]) for o in others if o[1] < y1 and o[3] > y0]
            pos = _first_gap(row, par[2] + gap, x1 - x0, ax1, gap)
            if pos is not None:
                _move_outline(view, left=pos)
            else:  # mirror the search to the left of the parent
                mirrored = [(w - b, w - a) for a, b in row]
                pos = _first_gap(mirrored, w - par[0] + gap, x1 - x0, w - ax0, gap)
                _move_outline(view, right=w - pos) if pos is not None else _move_outline(view, left=max(b for _, b in row) + gap)
        else:
            col = [(h - o[3], h - o[1]) for o in others if o[0] < x1 and o[2] > x0]  # measured downwards from the top
            pos = _first_gap(col, h - par[1] + gap, y1 - y0, h - ay0, gap)
            if pos is not None:
                _move_outline(view, top=h - pos)
            else:
                up = [(o[1], o[3]) for o in others if o[0] < x1 and o[2] > x0]
                pos = _first_gap(up, par[3] + gap, y1 - y0, ay1, gap)
                _move_outline(view, bottom=pos) if pos is not None else _move_outline(view, top=min(o[1] for o in others) - gap)
        if _is_clear(view, drw):
            return True
        # no room in line with the parent: the nearest free spot (the view loses its alignment)
        # aimed at the top-left corner the view would have in line with the parent, so it stays as near aligned as it can
        goal = (par[0], par[1] - gap) if direction != "vertical" else (par[2] + gap, par[3])
        return _place_free(view, drw, goal)
    except Exception:
        return False


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
    scale: Annotated[str | None, Field(description=(
        "Sheet scale like '1:2' or '2:1'; default is the largest standard scale (ISO 5455) at which the views and their "
        "dimensions fit the sheet."))] = None,
    annotations: Annotated[bool, Field(description="Import model dimensions, datums, GD&T, surface finish, notes and cosmetic threads into the views.")] = True,
    template: Annotated[str | None, Field(description="Drawing template (.drwdot); default is the SolidWorks default.")] = None,
    sheet_format: Annotated[str | None, Field(description=(
        "Border, zones and title block: a SolidWorks format style for the paper size (iso, ansi, din, jis, bsi, gb) or a "
        ".slddrt file (a company format). The title block fills from the model's custom properties.") )] = None,
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
    fmt = _format_file(sheet_format, sheet) if sheet_format else None
    doc = new_drawing(tpl, sheet)
    if doc is None:
        raise SwError("SolidWorks could not create the drawing.")
    doc = SW.cast(doc, "IModelDoc2")
    drw = SW.drawing(doc)
    if fmt is not None:
        _apply_format(doc, drw, fmt, sheet)
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
    fit_note = auto = None
    if scale:  # the first views auto-scale the sheet; put the requested scale back and keep the views on the sheet
        sh.SetScale(n, d, True, False)
        doc.ForceRebuild3(True)
        fit_note = _fit_views(drw, w, h)
    elif auto := _auto_scale(doc, drw, sh, isometric):
        fit_note = _fit_views(drw, w, h)
    _inherit_settings(doc, mpath)
    if isometric:
        iso = drw.CreateDrawViewFromModelView3(str(mpath), "*Isometric", w * 0.78, h * 0.72, 0)
        if iso is not None:
            _fit_top_right(SW.cast(iso, "IView"), w, h, _drawing_area(drw))
    if annotations:
        types = 0
        for n in ("swInsertDimensionsMarkedForDrawing", "swInsertDatums", "swInsertDatumTargets", "swInsertGTols", "swInsertSFSymbols", "swInsertNotes",
                  "swInsertCThreads", "swInsertHoleWizardProfileDimensions", "swInsertHoleWizardLocationDimensions", "swInsertholeCallout"):
            types |= getattr(C, n)
        # InsertAllAnnotations also brings the model's annotation views: DimXpert schemes and MBD annotations
        try:
            drw.InsertModelAnnotations4(C.swImportModelItemsFromEntireModel, types, True, True, False, False, True, False)
        except AttributeError:
            drw.InsertModelAnnotations3(C.swImportModelItemsFromEntireModel, types, True, True, False, False)
    doc.ForceRebuild3(True)
    tidy = _tidy_annotations(doc, drw) if annotations else None
    doc.ViewZoomtofit2()
    out: dict[str, Any] = {"drawing": doc.GetTitle(), "model": str(mpath), "projection": projection,
                           "sheet_mm": [round(to_mm(w), 1), round(to_mm(h), 1)], "views": _views(drw)}
    if scale or auto:
        out["scale"] = scale or auto
    if tidy and (tidy["gdt_annotations"] or tidy["moved"]):
        out["annotations"] = tidy
    if fmt is not None:
        out["sheet_format"] = fmt.name
        out["drawing_area_mm"] = [round(to_mm(c), 1) for c in _drawing_area(drw)]
        model_doc = SW.app.GetOpenDocumentByName(str(mpath))
        if prop := _link_drawing_number(drw, model_doc):
            out["drawing_number_from"] = prop
            doc.ForceRebuild3(True)
        tb = _title_block(drw)
        if tb:
            out["title_block_mm"] = [round(to_mm(c), 1) for c in tb]
            if any(_overlap(tuple(mm(c) for c in v["outline_mm"]), tb) for v in out["views"]):
                out["warning"] = (out.get("warning", "") + " A view overlaps the title block; move it with view_dimensions or choose a smaller scale.").strip()
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
    placed = _place_beside(sv, drw, view, direction, gap=0.03)  # leave room for the parent view's dimensions
    o = [round(to_mm(c), 2) for c in _outline(sv)]
    doc.ForceRebuild3(True)
    hatched = sv.GetFaceHatchCount()
    out = {"section_view": sv.Name, "label": label, "scale": round(sv.ScaleDecimal, 6), "outline_mm": o, "cut_faces": hatched}
    if not hatched:
        out["warning"] = "The cutting line does not pass through material; move it with offset (model mm from the view centre)."
    elif not placed:
        out["warning"] = ("There is no clear space for the section view on this sheet; move it with view_dimensions, "
                          "use a smaller scale, or put it on another sheet.")
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
    """Centre of the emptiest region of the drawing area (inside the format's border), clear of views, tables and
    the title block as far as possible, for a new view without a position."""
    occupied = [[mm(c) for c in v["outline_mm"]] for v in _views(drw)]
    tb = _title_block(drw)
    if tb:
        occupied.append(list(tb))
    sv = SW.cast(drw.GetFirstView(), "IView")
    occupied += [list(b) for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation") if (b := _table_box(t)) is not None]
    ax0, ay0, ax1, ay1 = _drawing_area(drw)
    candidates = [(ax0 + (ax1 - ax0) * fx, ay0 + (ay1 - ay0) * fy) for fy in (0.8, 0.55, 0.3) for fx in (0.15, 0.4, 0.65, 0.85)]

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
    out = _view_result(v)
    if not position:  # now its size is known: move it into clear space if it landed on something
        o = _outline(v)
        taken = [tuple(mm(c) for c in r["outline_mm"]) for r in _views(drw) if r["name"] != v.Name]
        tb = _title_block(drw)
        taken += [tb] if tb else []
        sv = SW.cast(drw.GetFirstView(), "IView")
        taken += [b for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation") if (b := _table_box(t)) is not None]
        area = _drawing_area(drw)
        inside = area[0] <= o[0] and o[2] <= area[2] and area[1] <= o[1] and o[3] <= area[3]
        if not inside or any(_overlap(o, t, 0.004) for t in taken):
            spot = _free_box(area, o[2] - o[0], o[3] - o[1], taken, (o[0], o[3]), gap=0.008)
            if spot is None:
                out["warning"] = "There is no clear space on the sheet for the view; give position=, a smaller scale, or another sheet."
            else:
                px, py = list(v.Position)[:2]
                _set_position(v, px + spot[0] - o[0], py + spot[1] - o[3])
                out = _view_result(v)
    return out


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
    # slide it further out (staying aligned) while it overlaps another view, a table or the title block
    tb = _title_block(drw)
    sv = SW.cast(drw.GetFirstView(), "IView")
    fixed = ([list(tb)] if tb else []) + [list(b) for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation") if (b := _table_box(t)) is not None]
    for _ in range(10):
        a = _outline(v)
        hits = [[mm(c) for c in o["outline_mm"]] for o in _views(drw) if o["name"] != v.Name] + fixed
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
    out = _view_result(v)
    o, area = _outline(v), _drawing_area(drw)
    if o[0] < area[0] - 1e-4 or o[1] < area[1] - 1e-4 or o[2] > area[2] + 1e-4 or o[3] > area[3] + 1e-4:
        out["warning"] = "The projected view runs past the border on its projection line; move the views apart, use a smaller scale or a larger sheet."
    return out


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
    v = SW.cast(v, "IView")
    out = {}
    if not position:  # the view stays aligned with its parent, so slide it out along that line until it is clear
        out = _slide_clear(drw, v, parent, w, h)
    out = {**_view_result(v), **out}
    out["label"] = label
    return out


def _slide_clear(drw: Any, v: Any, parent: Any, w: float, h: float, gap: float = 0.008) -> dict:
    """Move a view aligned to its parent outward along the line between their centres until it overlaps no
    other view; warn if that takes it off the sheet."""
    def centre(view: Any) -> tuple[float, float]:
        o = _outline(view)
        return (o[0] + o[2]) / 2, (o[1] + o[3]) / 2

    (px, py), (cx, cy) = centre(parent), centre(v)
    d = math.hypot(cx - px, cy - py)
    if d < 1e-6:
        return {}
    ux, uy = (cx - px) / d, (cy - py) / d
    start = list(v.Position)[:2]
    for _ in range(60):
        o = _outline(v)
        area = _drawing_area(drw)
        if o[0] < area[0] or o[1] < area[1] or o[2] > area[2] or o[3] > area[3]:
            break
        others = [tuple(mm(c) for c in r["outline_mm"]) for r in _views(drw) if r["name"] != v.Name]
        if not any(_overlap(o, b, gap) for b in others):
            return {}
        x, y = list(v.Position)[:2]
        _set_position(v, x + ux * 0.01, y + uy * 0.01)
    # no room along the projection line: break the alignment (as drafting practice allows) and use free space near the parent
    _set_position(v, *start)
    v.RemoveAlignment()
    o = _outline(v)
    vw, vh = o[2] - o[0], o[3] - o[1]
    others = [tuple(mm(c) for c in r["outline_mm"]) for r in _views(drw) if r["name"] != v.Name]
    spot = _free_box(_drawing_area(drw), vw, vh, others, (cx - vw / 2, cy + vh / 2), gap=gap)
    if spot is None:
        return {"warning": "There is no free space on the sheet for the view; give position= or use a larger sheet."}
    x, y = list(v.Position)[:2]
    _set_position(v, x + spot[0] - o[0], y + spot[1] - o[3])
    return {"aligned": False, "note": "No room along the projection line, so the view was placed unaligned (its view arrow and label identify it)."}


@tool(WRITES)
def sheets(
    action: Annotated[Literal["list", "add", "activate"], Field(description="list the sheets, add one (it becomes active), or activate one.")] = "list",
    name: Annotated[str | None, Field(description="Sheet name (add, activate).")] = None,
    size: Annotated[str, Field(description="Paper size for a new sheet: A4, A4V, A3, A2, A1, A0, A, AV, B, C, D, E.")] = "A3",
    scale: Annotated[str, Field(description="Sheet scale for a new sheet, e.g. '1:1'.")] = "1:1",
    sheet_format: Annotated[str | None, Field(description="add: border and title block, as in create_drawing (iso, ansi, din, jis, bsi, gb or a .slddrt file).")] = None,
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
        fmt = _format_file(sheet_format, size) if sheet_format else None
        if not drw.NewSheet4(name, paper, template, n, d, first_angle, "", 0, 0, "", 0, 0, 0, 0, 0, 0):
            raise SwError(f"SolidWorks could not add sheet '{name}'.")
        drw.ActivateSheet(name)
        if fmt is not None:
            _apply_format(doc, drw, fmt, size)
            model = None
            for sheet_name in drw.GetSheetNames() or []:  # the drawing's model, from any sheet's views
                for v in SW.cast(drw.Sheet(sheet_name), "ISheet").GetViews() or []:
                    model = model or SW.cast(v, "IView").ReferencedDocument
            _link_drawing_number(drw, model)
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


def _overlap(a: tuple, b: tuple, gap: float = 0.0) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _table_corner(ta: Any) -> tuple[float, float]:
    """Where a table's position point sits on it, as fractions of its width and height from the bottom left:
    the corner of its anchor type (a revision block is held by its top right, a general table by its top left)."""
    return {1: (0.0, 1.0), 2: (1.0, 1.0), 3: (0.0, 0.0), 4: (1.0, 0.0)}.get(ta.AnchorType, (0.0, 1.0))


def _table_size(ta: Any) -> tuple[float, float]:
    return sum(ta.GetColumnWidth(i) for i in range(ta.ColumnCount)), sum(ta.GetRowHeight(i) for i in range(ta.RowCount))


def _free_box(area: tuple[float, float, float, float], bw: float, bh: float, taken: list[tuple], goal: tuple[float, float],
              gap: float = 0.004, step: float = 0.0025) -> tuple[float, float] | None:
    """Top-left corner (metres) nearest goal for a bw x bh box inside area (x0, y0, x1, y1), clear of the taken boxes."""
    best = None
    x = area[0] + (goal[0] - area[0]) % step  # the grid runs through the goal, so a spot in line with it is exact
    while x + bw <= area[2] + 1e-9:
        y = area[1] + bh + (goal[1] - area[1] - bh) % step
        while y <= area[3] + 1e-9:
            if not any(_overlap((x, y - bh, x + bw, y), t, gap) for t in taken):
                d = math.hypot(x - goal[0], y - goal[1])
                if best is None or d < best[0]:
                    best = (d, x, y)
            y += step
        x += step
    return best[1:] if best else None


def _table_box(ta: Any) -> tuple[float, float, float, float] | None:
    """Sheet box (metres) of a table; None for one held by a sheet format anchor (SolidWorks reports those at 0, 0)."""
    if ta.Anchored:
        return None
    x, y = list(SW.cast(ta.GetAnnotation(), "IAnnotation").GetPosition())[:2]
    w, h = _table_size(ta)
    fx, fy = _table_corner(ta)
    x0, y0 = x - fx * w, y - fy * h
    return (x0, y0, x0 + w, y0 + h)


def _place_table(drw: Any, ta: Any, kind: str) -> tuple[float, float]:
    """Move a table (inserted with its top-left corner as the anchor point) to free space on the sheet: clear of
    views, other tables and the title block, inside the border. Revision blocks go towards the top right, other
    tables towards the bottom right just above the title block. Returns the top-left corner (metres)."""
    w, h = _sheet_size(drw)
    box = _table_box(ta)
    tw, th = box[2] - box[0], box[3] - box[1]
    taken = [tuple(mm(c) for c in v["outline_mm"]) for v in _views(drw)]
    tb = _title_block(drw)
    if tb:
        taken.append(tb)
    sv = SW.cast(drw.GetFirstView(), "IView")
    for other in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation"):
        if SW.app.IsSame(other, ta) != 1 and (ob := _table_box(other)) is not None:
            taken.append(ob)
    gap = 0.004
    area = _drawing_area(drw)
    if kind == "revision":
        goal = (area[2] - tw, area[3])
    elif tb:
        goal = (tb[2] - tw, tb[3] + gap + th)
    else:
        goal = (area[2] - tw, area[1] + th)
    spot = _free_box(area, tw, th, taken, goal, gap)
    if spot is None:
        raise SwError(f"There is no free space of {to_mm(tw):.0f} x {to_mm(th):.0f} mm on the sheet for the table; give position=, "
                      "or add a sheet with sheets(action='add').")
    x, y = spot  # top-left corner; the table is moved by its own anchor corner
    fx, fy = _table_corner(ta)
    if not SW.cast(ta.GetAnnotation(), "IAnnotation").SetPosition2(x + fx * tw, y - th + fy * th, 0):
        raise SwError("SolidWorks could not move the table.")
    return x, y


def _annotation_box(a: Any, whole: bool = False) -> tuple[float, float, float, float] | None:
    """Sheet box (metres) of a datum symbol or feature control frame: its texts and the frame lines around them,
    leaving out the leader or stem that runs to the feature. whole=True takes every line as well (a dimension's
    extension and dimension lines and arrows), for an annotation that others must keep clear of."""
    dd = SW.cast(a.GetDisplayData(), "IDisplayData")
    if dd is None or not dd.GetTextCount():
        return None
    import re

    texts = []
    for i in range(dd.GetTextCount()):
        x, y = list(dd.GetTextPositionAtIndex(i))[:2]
        size = dd.GetTextHeightAtIndex(i)
        chars = len(re.sub(r"<[^>]*>", "#", dd.GetTextAtIndex(i) or ""))  # a <SYMBOL> tag draws one glyph
        texts.append((x, y, size, x + max(chars, 1) * 0.62 * size))
    tx0, tx1 = min(t[0] for t in texts), max(t[3] for t in texts)
    ty0, ty1 = min(t[1] for t in texts), max(t[1] + t[2] for t in texts)
    band = 0.004
    xs, ys = [tx0, tx1], [ty0, ty1]
    for i in range(dd.GetLineCount()):
        ln = list(dd.GetLineAtIndex3(i))
        for x, y in ((ln[4], ln[5]), (ln[7], ln[8])):
            if whole or (tx0 - band <= x <= tx1 + band and ty0 - band <= y <= ty1 + band):
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _gdt_annotations(drw: Any) -> list[tuple[Any, Any, str]]:
    """(view, annotation, key) for every datum symbol and feature control frame on the sheet, in view order;
    the key (type and text) is the same for the copies SolidWorks puts in each view that can show one."""
    C = SW.C
    out = []
    v = SW.cast(drw.GetFirstView(), "IView").GetNextView()
    while v is not None:
        v = SW.cast(v, "IView")
        for a in SW.cast_all(v.GetAnnotations() or [], "IAnnotation"):
            t = a.GetType()
            if t in (C.swDatumTag, C.swGTol) and a.Visible != C.swAnnotationHidden:
                dd = SW.cast(a.GetDisplayData(), "IDisplayData")
                text = "|".join(dd.GetTextAtIndex(i) for i in range(dd.GetTextCount())) if dd is not None else ""
                out.append((v, a, f"{t}:{text}"))
        v = v.GetNextView()
    return out


def _tidy_annotations(doc: Any, drw: Any) -> dict:
    """Show each datum symbol and feature control frame once (in the first orthographic view that has it), and move
    any that sit on a view, on each other, on the title block or off the drawing area into free space nearby."""
    found = _gdt_annotations(drw)
    iso = {v.Name for v, _, _ in found if (v.GetOrientationName() or "") in ("*Isometric", "*Trimetric", "*Dimetric")}
    ordered = sorted(found, key=lambda r: r[0].Name in iso)  # orthographic views first, sheet order kept
    keep, removed = {}, 0
    for v, a, key in ordered:
        if key in keep:  # imported model annotations cannot be deleted from a view, only hidden there
            a.Visible = SW.C.swAnnotationHidden
            removed += a.Visible == SW.C.swAnnotationHidden
        else:
            keep[key] = (v, a)
    area = _drawing_area(drw)
    taken = [tuple(mm(c) for c in r["outline_mm"]) for r in _views(drw)]
    tb = _title_block(drw)
    if tb:
        taken.append(tb)
    sv = SW.cast(drw.GetFirstView(), "IView")
    taken += [b for t in SW.cast_all(sv.GetTableAnnotations() or [], "ITableAnnotation") if (b := _table_box(t)) is not None]
    boxes = [(a, _annotation_box(a)) for _, a in keep.values()]
    # diameter and radius dimensions and BOM balloons are placed like the frames; other dimensions and notes stay
    # where they are
    # (arrange stacks the linear ones) unless they fall outside the drawing area
    C = SW.C
    v = sv.GetNextView()
    while v is not None:
        v = SW.cast(v, "IView")
        for a in SW.cast_all(v.GetAnnotations() or [], "IAnnotation"):
            if a.GetType() not in (C.swNote, C.swDisplayDimension) or a.Visible == C.swAnnotationHidden:
                continue
            b = _annotation_box(a)
            if b is None:
                continue
            radial = a.GetType() == C.swDisplayDimension and                 SW.cast(a.GetSpecificAnnotation(), "IDisplayDimension").GetType() in (C.swDiameterDimension, C.swRadialDimension)
            balloon = a.GetType() == C.swNote and SW.cast(a.GetSpecificAnnotation(), "INote").GetBalloonStyle() != C.swBS_None
            if radial or balloon or b[0] < area[0] or b[1] < area[1] or b[2] > area[2] or b[3] > area[3]:
                boxes.append((a, b))
            else:
                taken.append(_annotation_box(a, whole=True) or b)
        v = v.GetNextView()
    moved = set()
    current = {id(a): b for a, b in boxes}
    for _ in range(3):  # a dimension's text re-flows along its leader when moved, so measure again and retry
        changed = False
        for a, _b in boxes:
            b = current[id(a)]
            if b is None:
                continue
            others = [o for k, o in current.items() if k != id(a) and o is not None]
            off_area = b[0] < area[0] or b[1] < area[1] or b[2] > area[2] or b[3] > area[3]
            if not off_area and not any(_overlap(b, t, 0.002) for t in taken + others):
                continue
            bw, bh = b[2] - b[0], b[3] - b[1]
            spot = _free_box(area, bw, bh, taken + others, (b[0], b[3]), gap=0.003)
            if spot is None:
                continue
            x, y = list(a.GetPosition())[:2]
            if a.SetPosition2(x + spot[0] - b[0], y + spot[1] - b[3], 0):
                moved.add(id(a))
                changed = True
                current[id(a)] = _annotation_box(a) or (spot[0], spot[1] - bh, spot[0] + bw, spot[1])
        if not changed:
            break
    doc.GraphicsRedraw2()
    final = [b for a, _ in boxes if (b := _annotation_box(a)) is not None]
    clear = all(area[0] - 1e-4 <= b[0] and area[1] - 1e-4 <= b[1] and b[2] <= area[2] + 1e-4 and b[3] <= area[3] + 1e-4 for b in final) and         not any(_overlap(b, t) for b in final for t in taken) and not any(_overlap(b, c) for i, b in enumerate(final) for c in final[:i])
    return {"gdt_annotations": len(keep), "duplicates_removed": removed, "moved": len(moved), "clear": clear}


def _table_place_result(drw: Any, ta: Any, kind: str, position: list[float] | None) -> dict:
    if position is None:
        _place_table(drw, ta, kind)
    box = _table_box(ta)
    return {"box_mm": [round(to_mm(c), 1) for c in box]} if box else {}


_PART_NO_PROPS = ("PartNo", "PartNumber", "Part Number", "Number")


def _bom_part_number_property(asm: Any) -> str | None:
    """The custom property the assembly's parts keep their part number in, if they have one."""
    for comp in g.components(asm):
        m = comp.GetModelDoc2()
        if m is None:
            continue
        cpm = SW.cast(SW.cast(m, "IModelDoc2").Extension.CustomPropertyManager(""), "ICustomPropertyManager")
        names = {n.lower(): n for n in cpm.GetNames() or ()}
        for key in _PART_NO_PROPS:
            if key.lower() in names and cpm.Get6(names[key.lower()], False)[2]:
                return names[key.lower()]
    return None


def _bom_part_numbers(ta: Any, bt: Any, prop: str) -> None:
    """Show part numbers from a custom property in the BOM's PART NUMBER column instead of file names. The built-in
    column cannot change type, so a property column takes its place."""
    C = SW.C
    header = [ta.Text2(0, c, True).strip().upper() for c in range(ta.ColumnCount)]
    col = next((i for i, h in enumerate(header) if "PART" in h and "NUMBER" in h), None)
    if col is None:
        return
    if not ta.InsertColumn2(C.swTableItemInsertPosition_After, col, ta.Text2(0, col, True), C.swInsertColumn_DefaultWidth):
        return
    ta.SetColumnType2(col + 1, C.swBomTableColumnType_CustomProperty, False)
    if bt.SetColumnCustomProperty(col + 1, prop):
        ta.DeleteColumn2(col, False)
    else:
        ta.DeleteColumn2(col + 1, False)


def _sub_features(f: Any):
    sub = SW.cast(f.GetFirstSubFeature(), "IFeature")
    while sub is not None:
        yield sub
        sub = SW.cast(sub.GetNextSubFeature(), "IFeature")


def _table_rows(ta: Any, limit: int = 30) -> list[list[str]]:
    return [[ta.Text2(r, c, True) for c in range(ta.ColumnCount)] for r in range(min(ta.RowCount, limit))]


_ANCHORS = {"top_left": "swBOMConfigurationAnchor_TopLeft", "top_right": "swBOMConfigurationAnchor_TopRight",
            "bottom_left": "swBOMConfigurationAnchor_BottomLeft", "bottom_right": "swBOMConfigurationAnchor_BottomRight"}


@tool(WRITES)
def drawing_table(
    kind: Annotated[Literal["bom", "revision", "hole", "general", "cut_list"], Field(description=(
        "bom: parts list of an assembly view (with balloons); revision: revision block; hole: hole table of a face seen in a view; "
        "cut_list: weldment cut list (item, quantity, description, length) of a weldment part's view; "
        "general: a table of given cells, or a tabulated (dash-number) table of the model's configurations."))],
    view: Annotated[str | None, Field(description="bom and hole: the view the table belongs to.")] = None,
    position: Annotated[list[float] | None, Field(description="Table corner [x, y] in sheet mm (the corner given by anchor); default is free space clear of views, other tables and the title block.")] = None,
    anchor: Annotated[Literal["top_left", "top_right", "bottom_left", "bottom_right"], Field(description="Which table corner position refers to.")] = "top_right",
    balloons: Annotated[bool, Field(description="bom: add item balloons to the view.")] = True,
    revisions: Annotated[list[dict[str, str]] | None, Field(description="revision: rows to add, e.g. [{'rev': 'A', 'description': 'INITIAL RELEASE', 'approved': 'J. SMITH'}]; the date is filled in.")] = None,
    face: Annotated[str | None, Field(description="hole: model face holding the holes ('face@x,y,z'), seen square in the view.")] = None,
    origin: Annotated[str | None, Field(description="hole: model vertex for the X/Y datum, as a point 'x,y,z' in mm (the nearest vertex is used); default is a corner of the face.")] = None,
    template: Annotated[str | None, Field(description="Table template file; defaults: bom-standard, standard revision block, standard hole table--letters.")] = None,
    cells: Annotated[list[list[str]] | None, Field(description="general: the table text, row by row; the first row is the header.")] = None,
    dimensions: Annotated[dict[str, str] | None, Field(description=(
        "general: a tabulated drawing table of the model's configurations instead of cells, {dimension: column heading}, "
        "e.g. {'D1@Sketch1': 'L', 'D1@Boss-Extrude1': 'T'}; one row per configuration."))] = None,
    configurations: Annotated[list[str] | None, Field(description=(
        "general with dimensions: the configurations to tabulate, in order (e.g. ['-001', '-002']); default every one."))] = None,
) -> dict:
    """Tables on the active drawing sheet: bill of materials with balloons, revision block with revision
    rows, or a hole table with tags, positions from a datum corner and hole callouts."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    C = SW.C
    w, h = _sheet_size(drw)
    # without a position the table goes in free-standing at the top left and is then moved to free space
    use_anchor = False
    x, y = (mm(position[0]), mm(position[1])) if position else (0.012, h - 0.012)
    anc = getattr(C, _ANCHORS[anchor if position else "top_left"])
    if kind in ("bom", "hole", "cut_list") and not view:
        raise SwError(f"A {kind} table needs view=.")
    out: dict[str, Any] = {"table": kind}
    if kind == "general":
        if dimensions:
            from .tools_inspect import configuration_table

            first = SW.cast(SW.cast(drw.GetFirstView(), "IView").GetNextView(), "IView")
            model = SW.app.GetOpenDocumentByName(first.GetReferencedModelName()) if first is not None else None
            if model is None:
                raise SwError("The drawing has no model view to tabulate.")
            SW.activate(model)
            try:
                rows = configuration_table(columns=list(dimensions))["configurations"]
            finally:
                SW.activate(doc)
            if configurations:
                by_cfg = {r["config"]: r for r in rows}
                if missing := [c for c in configurations if c not in by_cfg]:
                    raise SwError(f"No configuration {', '.join(missing)}; the model has {', '.join(by_cfg)}.")
                rows = [by_cfg[c] for c in configurations]
            cells = [["CONFIG", *dimensions.values()]] + [[r["config"], *[f"{r[k]:g}" for k in dimensions]] for r in rows]
        if not cells or not cells[0]:
            raise SwError("A general table needs cells (rows of text) or dimensions (to tabulate configurations).")
        ncol = max(len(r) for r in cells)
        t = drw.InsertTableAnnotation2(use_anchor, x, y, anc, str(app().path(template, must_exist=True)) if template else "", len(cells), ncol)
        if t is None:
            raise SwError("SolidWorks could not insert the table.")
        ta = SW.cast(t, "ITableAnnotation")
        for r, row in enumerate(cells):
            for col in range(ncol):
                ta.SetText(r, col, str(row[col]) if col < len(row) else "")
        doc.GraphicsRedraw2()
        got = [[ta.DisplayedText2(r, col, True) for col in range(ncol)] for r in range(len(cells))]
        if [[str(v) for v in row] + [""] * (ncol - len(row)) for row in cells] != got:
            raise SwError(f"The table text did not come out as given: {got}")
        out.update(rows=len(cells), columns=ncol, cells=got)
        out.update(_table_place_result(drw, ta, kind, position))
        return out
    if kind == "bom":
        v = _view(drw, view)
        if doc_kind(_view_model(v)) != "assembly":
            raise SwError("A bill of materials needs a view of an assembly.")
        tpl = app().path(template, must_exist=True) if template else _table_template("bom-standard.sldbomtbt")
        t = v.InsertBomTable4(use_anchor, x, y, anc, C.swBomType_PartsOnly, v.ReferencedConfiguration or "", str(tpl), False, C.swNumberingType_Detailed, False)
        if t is None:
            raise SwError("SolidWorks could not insert the bill of materials.")
        ta = SW.cast(t, "ITableAnnotation")
        if prop := _bom_part_number_property(_view_model(v)):
            out["part_number_from"] = prop
            _bom_part_numbers(ta, SW.cast(t, "IBomTableAnnotation"), prop)
        if balloons:
            doc.ClearSelection2(True)
            doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
            made = drw.AutoBalloon5(SW.cast(drw.CreateAutoBalloonOptions(), "IAutoBalloonOptions"))
            doc.ClearSelection2(True)
            out["balloons"] = len(made) if made else 0
    elif kind == "cut_list":
        v = _view(drw, view)
        model = _view_model(v)
        if doc_kind(model) != "part" or not any(sub.GetTypeName2() == "CutListFolder" for feat in g.iter_features(model)
                                                 for sub in _sub_features(feat)):
            raise SwError("A cut list table needs a view of a weldment part (structural_member).")
        tpl = app().path(template, must_exist=True) if template else _table_template("cut list.sldwldtbt")
        t = v.InsertWeldmentTable(use_anchor, x, y, anc, "", str(tpl))  # "" is the view's own configuration
        if t is None:
            raise SwError("SolidWorks could not insert the cut list table.")
        ta = SW.cast(t, "ITableAnnotation")
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
    out.update(_table_place_result(drw, ta, kind, position))
    if kind == "bom" and out.get("balloons"):  # auto-balloons land where SolidWorks likes: move any on a view or a dimension
        out["annotations"] = _tidy_annotations(doc, drw)
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
        doc.SetPickMode()  # a failed add leaves the dimension tool running, and the next dimension would fail too
        raise SwError("SolidWorks could not dimension that selection; use edges seen as lines (or end-on as points) in the view, "
                      "or another view.")
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


def _room(drw: Any, view: Any, sheet_w: float, sheet_h: float) -> dict[str, float]:
    """Free distance on each side of a view up to the nearest view beside it, or the sheet edge (metres)."""
    x0, y0, x1, y1 = _outline(view)
    room = {"top": sheet_h - y1, "bottom": y0, "right": sheet_w - x1, "left": x0}
    sheet_view = SW.cast(drw.GetFirstView(), "IView")
    other = SW.cast(sheet_view.GetNextView(), "IView") if sheet_view is not None else None
    while other is not None:
        if other.GetName2() != view.GetName2():
            a0, b0, a1, b1 = _outline(other)
            if a0 < x1 and a1 > x0:  # overlaps across: above or below
                if b0 >= y1:
                    room["top"] = min(room["top"], b0 - y1)
                elif b1 <= y0:
                    room["bottom"] = min(room["bottom"], y0 - b1)
            if b0 < y1 and b1 > y0:  # beside
                if a0 >= x1:
                    room["right"] = min(room["right"], a0 - x1)
                elif a1 <= x0:
                    room["left"] = min(room["left"], x0 - a1)
        other = SW.cast(other.GetNextView(), "IView")
    return {k: max(0.0, v - 0.002) for k, v in room.items()}


def _text_depth(ann: Any, vertical: bool) -> float:
    """How far a dimension's text reaches across its row (sheet metres): its height, or its width when the
    text reads horizontally on a vertical dimension (ASME unidirectional)."""
    data = SW.cast(ann.GetDisplayData(), "IDisplayData")
    best = 0.0
    for i in range(data.GetTextCount() if data is not None else 0):
        h = data.GetTextHeightAtIndex(i)
        across_is_width = vertical == (abs(math.sin(data.GetTextAngleAtIndex(i))) < 0.5)
        best = max(best, len(data.GetTextAtIndex(i) or "") * h * 0.8 if across_is_width else h)
    return best


def _is_vertical(ann: Any) -> bool | None:
    """Whether a linear dimension measures vertically on the sheet, from its first extension line (which runs
    across the measured direction); None for aligned dimensions."""
    data = SW.cast(ann.GetDisplayData(), "IDisplayData")
    if data is None or data.GetLineCount() < 1:
        return None
    line = list(data.GetLineAtIndex3(0))
    dx, dy = abs(line[7] - line[4]), abs(line[8] - line[5])
    if min(dx, dy) > 0.02 * max(dx, dy, 1e-12):
        return None
    return dx > dy


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
    arrange: Annotated[bool, Field(description=(
        "Stack the linear dimensions around the view in rows by size, shortest nearest the part, spacing apart, and move "
        "diameter and radius dimensions, datums and tolerance frames on the sheet clear of the views (run before move)."))] = False,
    spacing: Annotated[float, Field(gt=0, description="arrange: distance between rows in sheet mm.")] = 8.0,
) -> dict:
    """List the dimensions shown in a drawing view with their values, tolerances and text positions (sheet
    mm); move them to tidy crowded annotations, and set tolerances (±, bilateral, limits, basic) and precision."""
    doc = SW.active("drawing")
    drw = SW.drawing(doc)
    v = _view(drw, view)
    dims = _view_dims(v)
    by_name = {n: (dd, dim) for n, dd, dim in dims}
    arranged: dict[str, Any] = {}
    if arrange:
        k = v.ScaleDecimal or 1.0
        linear = []
        for n, dd, dim in dims:
            if dd.GetType() != SW.C.swLinearDimension:
                continue
            ann = SW.cast(dd.GetAnnotation(), "IAnnotation")
            vertical = _is_vertical(ann)
            if vertical is not None:
                linear.append((n, abs(dim.SystemValue) * k, tuple(list(ann.GetPosition())[:2]), vertical, _text_depth(ann, vertical)))
        sw_, sh_ = _sheet_size(drw)
        room = _room(drw, v, sw_, sh_)
        # chained dimensions touch and share a row
        arranged = shapes.arrange_dimensions(linear, _outline(v), mm(spacing), clearance=mm(2), room=room)
        for name, (x, y) in arranged.items():
            SW.cast(by_name[name][0].GetAnnotation(), "IAnnotation").SetPosition2(x, y, 0.0)
        tidy = _tidy_annotations(doc, drw)  # diameter and radius dimensions, datums and frames off the views
        x0, y0, x1, y1 = _outline(v)
        off = sorted(n for n, (x, y) in arranged.items()
                     if not (0 < x < sw_ and 0 < y < sh_) or y - y1 > room["top"] + 1e-6 or y0 - y > room["bottom"] + 1e-6
                     or x - x1 > room["right"] + 1e-6 or x0 - x > room["left"] + 1e-6)
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
    if move or tolerance or decimals or arranged:
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
    if arrange:
        out["arranged"] = sorted(arranged)
        out["annotations"] = tidy
        if off:
            out["warning"] = (f"{', '.join(off)} did not fit between the view and its neighbours or the sheet edge; "
                              "move the views apart or use a larger sheet.")
    return out

"""Release: a file-based revision workflow for when there is no PDM vault.

A release stamps the revision on the model and its drawing, adds the revision row to the drawing, rebuilds and
saves both, exports the neutral and viewing files into releases/<part number>/<revision>/, and records every
file with its SHA-256 checksum in a manifest and in the workspace's release log. A revision is released once.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import geometry as g
from .com import SW, doc_kind
from .errors import SwError
from .registry import WRITES, app, tool
from .tools_features import feature_error

_LOG_FIELDS = ["part_no", "revision", "date", "description", "approved_by", "file", "sha256"]
_PART_NO_KEYS = ("PartNo", "PartNumber", "Part Number", "Number")


def _rev_key(rev: str) -> tuple:
    """Order revisions: letters (A < B < ... < Z < AA), numbers numerically, dashes for pre-release first."""
    rev = rev.strip().upper()
    if rev in ("-", ""):
        return (0, 0, "")
    if rev.isdigit():
        return (1, int(rev), "")
    return (2, len(rev), rev)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _prop(doc: Any, names: tuple[str, ...]) -> str:
    cpm = SW.cast(doc.Extension.CustomPropertyManager(""), "ICustomPropertyManager")
    have = {n.lower(): n for n in cpm.GetNames() or ()}
    for n in names:
        if n.lower() in have:
            r = cpm.Get6(have[n.lower()], False)
            if r[2] or r[1]:
                return str(r[2] or r[1])
    return ""


def _set_props(doc: Any, values: dict[str, str]) -> None:
    cpm = SW.cast(doc.Extension.CustomPropertyManager(""), "ICustomPropertyManager")
    for k, v in values.items():
        cpm.Add3(k, SW.C.swCustomInfoText, v, SW.C.swCustomPropertyReplaceValue)


def _rebuild_errors(doc: Any) -> list[str]:
    doc.ForceRebuild3(False)
    if doc_kind(doc) == "drawing":
        return []
    return [f.Name for f in g.iter_features(doc) if feature_error(f)]


def _history(log: Path, part_no: str) -> list[dict]:
    if not log.exists():
        return []
    with log.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("part_no") == part_no]


def _add_revision_row(doc: Any, rev: str, description: str, approved: str, date: str) -> str:
    """Add the revision to the active sheet's revision table, creating the table (top right) when there is none."""
    from .tools_drawing import _table_template

    drw = SW.drawing(doc)
    sheet = SW.cast(drw.GetCurrentSheet(), "ISheet")
    rt = sheet.RevisionTable
    created = False
    if rt is None:
        from .tools_drawing import drawing_table

        drawing_table(kind="revision")
        rt = SW.cast(drw.GetCurrentSheet(), "ISheet").RevisionTable
        created = True
        if rt is None:
            raise SwError(f"Could not add a revision table ({_table_template('standard revision block.sldrevtbt')}).")
    rt = SW.cast(rt, "IRevisionTableAnnotation")
    ta = SW.cast(rt, "ITableAnnotation")
    header = [ta.Text2(1, c, True).strip().rstrip(".").upper() for c in range(ta.ColumnCount)]
    rid = rt.AddRevision(rev)
    r = rt.GetRowNumberForId(rid)
    for title, val in (("DESCRIPTION", description), ("DATE", date), ("APPROVED", approved)):
        if title in header and val:
            ta.SetText(r, header.index(title), val)
    return "created" if created else "added"


@tool(WRITES)
def release(
    revision: Annotated[str, Field(min_length=1, max_length=8, description="The revision being released, e.g. 'A', 'B', '01'; it must come after the last release of the part.")],
    description: Annotated[str, Field(min_length=1, description="What changed (the revision block's description), e.g. 'INITIAL RELEASE' or 'ECO-1042: HOLE 2X Ø6.6 WAS Ø6.4'.")],
    approved_by: Annotated[str, Field(min_length=1, description="Who approved the release (initials or name), for the revision block and the log.")],
    drawing: Annotated[str | None, Field(description="The drawing (.slddrw) to release with the model; default the active document if it is a drawing.")] = None,
    exports: Annotated[list[Literal["step", "pdf", "dxf", "x_t", "stl", "flat_dxf"]], Field(description=(
        "Files to issue: step, x_t and stl from the model, pdf and dxf from the drawing, flat_dxf (the flat blank with "
        "bend lines) from a sheet metal part."))] = ["step", "pdf"],  # noqa: B006
    step_ap: Annotated[Literal["auto", "203", "214", "242"], Field(description="STEP protocol for the step export.")] = "auto",
) -> dict:
    """Release a part or assembly (and its drawing) at a revision, without a PDM vault: refuses a revision already
    released or not after the last one, and models with rebuild errors; stamps Revision, ReleaseDate and ApprovedBy
    on the model and drawing, adds the revision row to the drawing, saves both, exports the issue files into
    releases/<part number>/<revision>/ with a copy of the saved native files, and logs every file with its SHA-256
    checksum (manifest.json there, and releases/release_log.csv for the history). The export-control marking rule
    applies to every export. An assembly's components are not copied; release them first."""
    from .tools_doc import export

    a = app()
    sw = SW.app
    active = SW.active()
    if drawing:
        dpath = a.path(drawing, must_exist=True, suffixes=(".slddrw",))
        ddoc = sw.GetOpenDocumentByName(str(dpath))
        if ddoc is None:
            raise SwError(f"Open the drawing first (open_document path='{drawing}').")
        ddoc = SW.cast(ddoc, "IModelDoc2")
    else:
        ddoc = active if doc_kind(active) == "drawing" else None
    if ddoc is not None:
        view = SW.cast(SW.cast(SW.drawing(ddoc).GetFirstView(), "IView").GetNextView(), "IView")
        model = SW.cast(view.ReferencedDocument, "IModelDoc2") if view is not None and view.ReferencedDocument is not None else None
        if model is None:
            raise SwError("The drawing shows no model to release.")
    else:
        model = active
    if not model.GetPathName() or (ddoc is not None and not ddoc.GetPathName()):
        raise SwError("Save the model and drawing first (save_document); a release needs their files.")
    rev = revision.strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{1,8}", rev):
        raise SwError("A revision is letters, digits or '-', e.g. 'A', 'B', '01'.")
    part_no = _prop(model, _PART_NO_KEYS) or Path(model.GetPathName()).stem
    root = a.settings.workspace / "releases"
    log = root / "release_log.csv"
    history = _history(log, part_no)
    released = sorted({r["revision"] for r in history}, key=_rev_key)
    if rev in released:
        raise SwError(f"{part_no} revision {rev} is already released ({', '.join(released)}); release a new revision.")
    if released and _rev_key(rev) <= _rev_key(released[-1]):
        raise SwError(f"Revision {rev} does not come after {part_no}'s last release {released[-1]}.")
    if "flat_dxf" in exports:  # checked before anything is stamped or saved
        from .tools_sheetmetal import _sheet_metal_feature

        if doc_kind(model) != "part":
            raise SwError("A flat_dxf export needs a sheet metal part, not an assembly.")
        _sheet_metal_feature(model)
    if ddoc is None and any(k in ("pdf", "dxf") for k in exports):
        raise SwError("pdf and dxf are made from the drawing; give drawing= (or release from the drawing), or leave them out.")
    errs = _rebuild_errors(model)
    if errs:
        raise SwError(f"{Path(model.GetPathName()).name} has rebuild errors in {', '.join(errs)}; fix them before release.")
    date = dt.date.today().isoformat()
    stamp = {"Revision": rev, "ReleaseDate": date, "ApprovedBy": approved_by}
    _set_props(model, stamp)
    out: dict[str, Any] = {"part_no": part_no, "revision": rev, "previous": released[-1] if released else None, "date": date}

    def activate(doc: Any) -> None:
        SW.activate(doc)

    try:
        activate(model)
        if not model.Save3(SW.C.swSaveAsOptions_Silent, 0, 0):
            raise SwError(f"Could not save {model.GetTitle()}.")
        if ddoc is not None:
            activate(ddoc)
            _set_props(ddoc, stamp)
            out["revision_table"] = _add_revision_row(ddoc, rev, description, approved_by, date)
            ddoc.ForceRebuild3(True)
            if not ddoc.Save3(SW.C.swSaveAsOptions_Silent, 0, 0):
                raise SwError(f"Could not save {ddoc.GetTitle()}.")
        safe = re.sub(r"[^\w.-]+", "_", part_no)
        folder = root / safe / rev
        folder.mkdir(parents=True, exist_ok=True)
        issued = []
        for kind in exports:
            if kind == "flat_dxf":
                from .tools_sheetmetal import flat_pattern

                activate(model)
                target = folder / f"{safe}_{rev}_FLAT.dxf"
                try:
                    flat_pattern(export=str(target))
                except SwError as err:
                    raise SwError(f"No flat blank for {part_no}: {err}") from None
                issued.append(target)
                continue
            src = model if kind in ("step", "x_t", "stl") else ddoc
            if src is None:
                raise SwError(f"A {kind} export needs a drawing; give drawing=.")
            activate(src)
            target = folder / f"{safe}_{rev}.{kind}"
            export(path=str(target), step_ap=step_ap) if kind == "step" else export(path=str(target))
            issued.append(target)
        # a snapshot of the saved native files, so the release stays as issued when work goes on in the originals
        natives = []
        for src_doc in [model] + ([ddoc] if ddoc is not None else []):
            src_path = Path(src_doc.GetPathName())
            copy = folder / src_path.name
            copy.write_bytes(src_path.read_bytes())
            natives.append(copy)
        files = natives + issued
    finally:
        activate(active)
    records = [{"part_no": part_no, "revision": rev, "date": date, "description": description, "approved_by": approved_by,
                "file": str(p), "sha256": _sha256(p)} for p in files]
    (folder / "manifest.json").write_text(json.dumps({"part_no": part_no, "revision": rev, "date": date, "description": description,
                                                      "approved_by": approved_by, "files": [{"file": r["file"], "sha256": r["sha256"]} for r in records]},
                                                     indent=2), encoding="utf-8")
    new_log = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        if new_log:
            w.writeheader()
        w.writerows(records)
    out.update(folder=str(folder), files=[{"file": Path(r["file"]).name, "sha256": r["sha256"]} for r in records],
               log=str(log), history=released + [rev])
    return out

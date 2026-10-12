"""Sheet metal gauge tables (.xlsx) read without Excel.

SolidWorks reads its gauge tables through Microsoft Excel, so on machines without Excel they cannot be used
in the part. The files are plain Office Open XML, so the values are read here directly and applied as the
sheet thickness and bend radius.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .errors import SwError

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_TO_MM = {"millimeters": 1.0, "millimetres": 1.0, "mm": 1.0, "inches": 25.4, "inch": 25.4, "in": 25.4}


def read_xlsx(path: str | Path) -> list[dict[str, str]]:
    """Rows of the first worksheet as {column letter: text}."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise SwError(f"Could not read {path} as an .xlsx file: {e}") from None
    with z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        sheet = "xl/worksheets/sheet1.xml"
        if sheet not in names:
            raise SwError(f"{path} has no first worksheet.")
        rows = []
        for r in ET.fromstring(z.read(sheet)).iter(f"{_NS}row"):
            row = {}
            for c in r.iter(f"{_NS}c"):
                v = c.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    text = shared[int(v.text)] if c.get("t") == "s" else v.text
                else:
                    inline = c.find(f"{_NS}is")
                    text = "".join(t.text or "" for t in inline.iter(f"{_NS}t")) if inline is not None else ""
                m = re.match(r"[A-Z]+", c.get("r") or "")
                if m and text.strip():
                    row[m.group(0)] = text.strip()
            rows.append(row)
        return rows


def parse_gauge_table(rows: list[dict[str, str]]) -> dict:
    """A SolidWorks gauge table: header lines (Type, Process, K-Factor, Unit) then gauge rows of name,
    thickness and available bend radii. Lengths are returned in mm."""
    head: dict[str, str] = {}
    gauges = []
    unit = 1.0
    in_table = False
    for row in rows:
        a, b, c = row.get("A", ""), row.get("B", ""), row.get("C", "")
        key = a.rstrip(":").strip().lower()
        if not in_table:
            if key.startswith("gauge"):
                in_table = True
                continue
            if key and b:
                head[key] = b
                if key == "unit":
                    unit = _TO_MM.get(b.strip().lower(), 0.0)
                    if not unit:
                        raise SwError(f"Unknown gauge table unit '{b}'.")
            continue
        if not a or not b:
            continue
        try:
            thickness = float(b) * unit
            radii = [float(x) * unit for x in re.split(r"[;,]", c) if x.strip()]
        except ValueError:
            continue
        gauges.append({"gauge": " ".join(a.split()), "thickness_mm": round(thickness, 4), "bend_radii_mm": [round(r, 4) for r in radii]})
    if not gauges:
        raise SwError("The table has no gauge rows (expected 'Gauge No.' followed by gauge, thickness and bend radii).")
    k = head.get("k-factor")
    return {"type": head.get("type"), "process": head.get("process"), "k_factor": float(k) if k else None, "gauges": gauges}


def find_gauge(table: dict, gauge: str) -> dict:
    """The row for a gauge given as 'Gauge 14', '14 Gauge' or just '14'."""
    want = " ".join(str(gauge).split()).lower()
    num = re.sub(r"[^0-9.]", "", want)
    for g in table["gauges"]:
        name = g["gauge"].lower()
        if name == want or (num and re.sub(r"[^0-9.]", "", name) == num):
            return g
    raise SwError(f"No gauge '{gauge}' in the table. Gauges: {', '.join(g['gauge'] for g in table['gauges'])}")

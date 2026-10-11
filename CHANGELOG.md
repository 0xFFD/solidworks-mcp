# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Sketch editing: `list_sketch` lists the segments and points of a sketch, and `modify_sketch` trims, offsets,
  converts model edges, adds sketch fillets and chamfers, relations and driving dimensions, and moves, rotates,
  copies, deletes or toggles construction on individual entities. Sketch items are addressed as `seg:N`, `pt:N`,
  `seg@x,y` or `pt@x,y`.
- `edit_feature` redefines existing features: extrude and cut end conditions (blind, through all, mid plane,
  up to next), depths, second direction and flip side; revolve angle; fillet radius and edges; chamfer distance;
  shell thickness; pattern counts and spacing.
- `configurations` lists, adds (including derived configurations), activates and deletes configurations, and
  `set_dimensions` and `feature_action` take `configurations` to change only some of them.
- `helix` creates helices and spirals from a sketched circle and reports the start point and tangent, so a
  profile can be swept along it for springs, coils and modelled threads.
- 3D sketches (`start_sketch(on="3d")`) with lines, polylines, splines and points in model coordinates, for
  sweep paths, tube runs and harnesses.
- Multibody parts: `list_bodies`, `combine` (add, subtract, common) and `move_body` (move, rotate, copy), and
  `body:Name` references so linear and circular patterns and mirror can repeat bodies.
- Sketch text takes a `font` and `bold`.
- Sheet metal: `sheet_metal_base` (flat blanks from closed outlines, formed sections from open profiles),
  `edge_flange` (any angle, bend position, radius and K-factor; the flange profile is generated from the
  edge) and `flat_pattern` (thickness, radius, K-factor, bend count and flat blank size; flatten and fold;
  DXF/DWG export of the blank with bend lines, subject to the export-control marking check).
- `design_guide('sheet_metal')`: bend radii for 5052, 6061, 2024 and 7075 aluminium, stainless and
  titanium, K-factors, flange and hole-to-bend rules and an avionics tray recipe.

### Fixed
- `set_dimensions` treated unitless dimensions such as pattern instance counts as millimetres, so setting a
  count of 6 set it to 0.006. `dimensions` now reports their unit as `count`.
- `set_dimensions` no longer closes a sketch that is open for editing.
- `list_edges` could report a `mid` point off the edge when the edge runs against its curve's
  parameter direction, so `edge@` references built from it failed. Every reported midpoint now lies on
  its edge.
- Features created in sheet metal parts were not found after creation (the flat pattern stays last in
  the tree), which made `ref_plane`, `shell`, `ref_axis` and `loft` report failure there.

## [0.2.0] - 2026-10-11

Design from a one-line request: an agent told "build an interceptor drone frame" can now plan and
build the parts and assembly without further guidance.

### Added
- `design_guide`: workflows, sizing rules, standard mounting patterns, fastener tables, structural and
  manufacturing rules, material choices and release checks, plus a step-by-step recipe for a
  multirotor frame that was verified end to end. A `design` MCP prompt starts the same workflow in
  clients that show prompts.
- Sketch entities: polylines with corner radii (any rounded outline), bolt circles, hole grids, NACA
  4-digit airfoils, ellipses and engraved text, and a `mirror` option on every entity for symmetric
  parts.
- `bom`: parts list of the active assembly with quantity, part number, description, material and a
  mass budget.
- Screenshots hide planes, axes, origins and sketches by default (`clean`).
- `edit_sketch` to reopen an existing sketch, and `import_file` for STEP, IGES, Parasolid, ACIS, STL
  and 3MF models of bought-in parts.
- A bundled `aerospace materials` database with generic CFRP laminates, aramid, G10/FR4 and
  engineering polymers (PA12, PA12-CF, PETG, PC, PEEK-CF30), GFRP, foam cores (PMI, PVC, XPS, EPP)
  and balsa, installed into the existing custom
  material folder the first time one is used. SolidWorks settings are not changed.

### Tested
- Independent Claude Code agents with only this server: "Build an interceptor drone frame." (four parts
  and an assembly in 117 calls) and "Design the main wing for a small fixed-wing interceptor UAV."
  (swept, tapered, lofted NACA wing with spar bore in 35 calls). The frame agent found a prop-disc
  clearance flaw in the recipe, which is fixed, and the guide now states the clearance rule.
- The frame request repeated with Claude Haiku: same assembly, 109 calls, three recovered errors.
- Codex CLI with an OpenAI model built a 5 inch quadcopter frame from one sentence in 30 calls. It
  declines requests that say "interceptor" on its own policy grounds.
- `set_material` takes `material=` as well as `name=`, and accepts a name that matches exactly one material up to its suffix ('6061-T6' ->
  '6061-T6 (SS)') and reports the match.

### Fixed
- Faces, edges and points on assembly components are reported and matched in assembly coordinates
  (they were in the component's own coordinates).
- `insert_component` puts the component origin at `position`; SolidWorks centres the bounding box there.
- `extrude(end='through_all_both')` works for cuts, and a cut that finds no material is retried in the
  opposite direction.
- Lofts between single closed curves such as ellipses fall back to the API call that accepts them.
- Spline and airfoil points are passed as arrays of doubles, which SolidWorks requires.
- Engraved text is built one character per text entity, because SolidWorks cannot extrude a string
  whose glyph outlines touch.

## [0.1.0] - 2026-10-11

First public release.

### Added
- 54 MCP tools covering parts, drawings and assemblies in SolidWorks: documents and exports, sketching,
  boss and cut features (extrude, revolve, sweep, loft), dress-up features (fillet, chamfer, shell, draft),
  Hole Wizard holes, patterns, mirror, reference geometry, cosmetic threads, GD&T, drawings with section
  views, materials, custom properties, equations, mass properties, geometry checks, measurement,
  screenshots, mates and interference detection.
- Entity references for faces and edges by index (`face:12`) or by a point on them (`face@50,0,-10`),
  including assembly components, with face and edge listings that describe normals, radii, axes, holes
  and bosses.
- Exact Hole Wizard placement: every hole centre is moved to the requested coordinates in the hole's
  position sketch, and positions that are not on the selected face are refused.
- STEP export read-back: the protocol is taken from the written file and an export without solid
  geometry is reported as an error. AP242 through `PublishSTEP242File2` when the MBD licence is present,
  otherwise AP214.
- Drawings inherit the model's unit system, precision and drafting standard; isometric and section views
  are placed in free space on the sheet.
- Controls for regulated work: hash-chained audit log with `solidworks-mcp verify-audit`, folder
  confinement, `--read-only`, and `--require-marking` to block exports of documents without an
  export-control marking.
- `solidworks-mcp install` for Claude Code, Claude Desktop, Codex CLI, Cursor, VS Code, Windsurf,
  Gemini CLI and opencode; `solidworks-mcp doctor` and `solidworks-mcp config`.
- Tested against SOLIDWORKS 2026 SP4 through the MCP protocol over stdio.

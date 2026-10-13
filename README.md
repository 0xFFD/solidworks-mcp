# solidworks-mcp

**An MCP server that lets AI agents design in [SolidWorks](https://www.solidworks.com).** It sketches, builds features, places standard holes exactly, adds GD&T, makes drawings, exports STEP and checks its own work with mass properties, geometry checks and screenshots. It works from Claude Code, Codex CLI, Cursor, VS Code, Windsurf, Gemini CLI, Claude Desktop and opencode.

It was built for aerospace and defence parts, so every call is written to a tamper-evident audit log, file access is confined to folders you choose, and exports can be blocked until a part carries an export-control marking.

The bracket below was modelled, toleranced and drawn entirely through the server's tools, from a blank part to the PDF drawing:

| Part (`screenshot`) | Drawing (`create_drawing`, `section_view`, `add_note`) |
|---|---|
| ![bracket](https://raw.githubusercontent.com/0xFFD/solidworks-mcp/main/docs/images/bracket-isometric.png) | ![drawing](https://raw.githubusercontent.com/0xFFD/solidworks-mcp/main/docs/images/bracket-drawing.png) |

The plate is 7075-T6 aluminium with counterbored M6 holes and M5 tapped holes placed at exact coordinates. Datum A carries a flatness tolerance, the counterbores a position tolerance of Ø0.1 at MMC to A and B at MMC, and the drawing picks all of that up together with the hole callouts and a section view. The calls were `new_document → start_sketch → sketch → finish_sketch(fully_define) → extrude → fillet → hole (counterbore) → hole (tapped) → extrude (cut) → chamfer → set_material → custom_properties → add_datum → add_geometric_tolerance → add_surface_finish → check_geometry → mass_properties → export (STEP) → create_drawing → section_view → add_note → export (PDF)`.

## From one sentence to an airframe

Agents are good at following a request and bad at knowing that a 5 inch quad needs motors at least 137 mm apart, that a 30.5 mm flight controller stack is a 43.13 mm bolt circle, or that bolts in carbon plate want three diameters of edge distance. `design_guide` gives them that knowledge together with a verified step-by-step recipe, and the sketch tool has the vocabulary airframes need: rounded outlines, bolt circles, NACA airfoils, engraved part numbers and mirrored copies.

We tested this with independent agents that had nothing but this server and one sentence:

- **"Build an interceptor drone frame."** A fresh Claude Code session read the guide and chose a stretched-X layout for 5 inch props. It built the bottom plate, top plate, standoffs and nose fairing as separate parts, mated them into an assembly, checked interference, exported STEP and reported a 139 g frame with its centre of mass on the centreline. That took 117 tool calls in 7 minutes. Its single error was a material name, which it corrected from the suggestion in the error message. It also ran its own arm stiffness check, and it spotted that the prop discs overlapped the corners of the centre plate in plan view. That was a real flaw in the recipe, and the recipe has since been corrected.
- **"Design the main wing for a small fixed-wing interceptor UAV."** The guide has no recipe for this, so the agent designed the wing from the fixed-wing rules on its own. It made a 1000 mm span wing with 25 degree leading-edge sweep, tapering from a NACA 2410 root to a NACA 2409 tip with 2 degrees of washout, and lofted it between airfoil sections. It cut a bore for a carbon spar tube along the quarter-chord line, mirrored the half-wing and checked the geometry. It then exported the wing and reported wing loading, stall speed and where the centre of mass should go. That took 35 tool calls in under 3 minutes, with no errors.
- **The same frame request with a small model.** Claude Haiku built the same five-file interceptor assembly in 109 tool calls and under 6 minutes, for about 6 cents of model usage. The result was 141 g with no interferences, and it used the BOM tool for the weight budget. Its three errors were all corrected from the server's messages, and material names like "6061-T6" are now matched to "6061-T6 (SS)" automatically. The server does not depend on the strongest model to fill gaps.
- **Codex CLI with an OpenAI model.** Codex declined the word "interceptor" on its own policy grounds and offered only a non-functional mock-up, which is a decision made by the model, not by this server. Given "Build a 5-inch quadcopter frame in SolidWorks." instead, it read the guide, followed the multirotor recipe and built a one-piece 250 mm CFRP X-frame with motor patterns and a 30.5 mm stack in 30 tool calls. It passed the geometry check and was saved as a part. Its one error was calling `set_material` with `material=` instead of `name=`, and both are now accepted.

| Interceptor frame from the recipe (141 g, centre of mass on the centreline) | Bottom plate: arms, motor patterns, 30.5 mm stack, lightening slots, engraved part number |
|---|---|
| ![interceptor frame](https://raw.githubusercontent.com/0xFFD/solidworks-mcp/main/docs/images/interceptor-assembly.png) | ![bottom plate](https://raw.githubusercontent.com/0xFFD/solidworks-mcp/main/docs/images/interceptor-bottom-plate.png) |

The wing the agent designed from one sentence:

![wing designed by an agent](https://raw.githubusercontent.com/0xFFD/solidworks-mcp/main/docs/images/agent-wing.png)

The plates are 5 mm and 2 mm quasi-isotropic CFRP, the standoffs are 6061-T6 hex with tapped M3 ends, and the fairing is a lofted and shelled PA12 part. Each part has its material, part number and description set, and the assembly is exported to STEP. In clients that show MCP prompts, the `design` prompt starts the same workflow.

The results are first designs to hand to an engineer, not parts ready to fly. The agents say so themselves, and each one lists what still needs checking (vibration, crash loads, mounting details).

## Why another SolidWorks MCP?

The existing servers cover a few sketch and extrude calls, are written against older SolidWorks versions, and most have never been run against a live session. This one was written against the SolidWorks 2026 API, and every tool has been exercised through the MCP protocol on a real installation.

| | solidworks-mcp |
|---|---|
| Modelling | 2D and 3D sketches with exact coordinates in sketch or model space, sketch editing (trim, offset, convert edges, relations, driving dimensions, move, rotate, copy, delete), extrude, revolve, sweep (including round sections along a path), helices and spirals for springs and modelled threads, loft, fillets (constant, asymmetric, conic, variable radius, face and full round), chamfer, shell, draft, linear, circular, table-driven, sketch-driven and curve-driven patterns of features or bodies, mirror, multibody combine, split, delete and move/copy, surface bodies (extruded, revolved, lofted, planar, offset) knitted into solids, thickened into skins or used as split tools, reference planes and axes |
| Weldments | Structural members (rectangular, square and round tube, round and flat bar, angle, channel) along 2D or 3D sketch lines, with mitred or butted corners, end caps on open tube ends, triangular and polygonal gussets, and a cut list (quantities, cut lengths, end angles); profiles are generated from their dimensions |
| Sheet metal | Base flanges from closed outlines or open L, U, Z and hat profiles, a K-factor for the part, edge flanges at any angle with their own bend radius and K-factor, miter flanges along chains of edges (with return lips), forming tools from the design library or custom punches (dimples, embosses, flanged holes, louvres, lances, ribs) placed exactly, hems (closed, open, teardrop, rolled, double), sketched bends with a chosen fixed side, closed corners, conversion of solids (such as imported brackets) to sheet metal, flat pattern size and bend count, flatten and fold, flat-pattern DXF/DWG export with bend lines (blocked without an export-control marking when required) |
| Holes | Hole Wizard plain, counterbore, countersink (including 100 degree aerospace countersinks) and tapped holes from ISO, ANSI metric and ANSI inch tables, with every centre placed exactly where you ask |
| GD&T and MBD | Datums, datum targets (points, circular and rectangular areas), feature control frames (all 14 characteristics, diameter zones, MMC and LMC on tolerances and datums, composite position and profile frames, projected tolerance zones, all-around, all-over and between modifiers, unequally disposed profiles), surface finish symbols and cosmetic threads on parts or assembly components, automatic DimXpert schemes (datums, sizes, flatness, perpendicularity, position and runout for prismatic and turned parts), carried into drawings and STEP |
| Drawings | Standard three views plus isometric in first or third angle projection, model dimensions, hole callouts and GD&T imported, full, half, offset and aligned section, break-out section, detail, projected, auxiliary and broken views, any model view or sheet metal flat pattern view on further sheets, reference dimensions between model edges, listing, moving and tolerancing (±, bilateral, limit, basic, ISO fits such as H7/g6) the dimensions of a view with their precision, bills of materials with balloons, revision blocks with revision rows, hole tables, notes, PDF and DWG export. Drawings take the part's units and drafting standard |
| Engineering data | Materials from the SolidWorks databases (with density, yield and modulus) plus a bundled database of generic CFRP, aramid, G10/FR4 and engineering polymer grades, custom properties for part number, revision and markings, equations and global variables, configurations (part variants with their own dimensions and suppressed features), ASME or ISO drafting standard |
| Design knowledge | `design_guide` with workflows, multirotor and fixed-wing sizing, standard mounting patterns, ISO 273 clearances, tap drills, edge distances, machining, sheet metal (bend radii by alloy, K-factors, hole-to-bend distances, an avionics tray recipe), composite and additive rules, quick stiffness checks and release checks |
| Checks | Mass, centre of mass and inertia tensor, bounding box, body fault check, rebuild errors, measurement, interference detection, and screenshots returned as images |
| Picking geometry | `list_faces` and `list_edges` describe every face and edge (type, outward normal, radius, axis, hole or boss, a point on it), and any entity can be picked by index or by a point near it: `face@50,0,0` |
| Exports | STEP AP242 (with the MBD licence), AP214 and AP203, IGES, Parasolid, STL, 3MF, PDF, DXF, DWG, eDrawings, PNG. Every STEP file is read back to confirm the protocol and that it holds solid geometry |
| Assemblies | Insert components, mates (coincident, concentric, distance, angle, parallel, perpendicular, tangent, lock, and limit distance and angle for travel stops, width, symmetric, gear, slot, hinge, rack and pinion, screw, universal joint, cam follower and path), exploded views with exact translation, rotation and radial steps, component listing and interference check |
| Every client | `solidworks-mcp install` configures Claude Code, Claude Desktop, Codex CLI, Cursor, VS Code, Windsurf, Gemini CLI and opencode |

## Install

Requirements: Windows 10 or 11, a licensed SolidWorks installation and Python 3.10 or newer. The server talks to SolidWorks through its COM API, so it runs on the same machine as SolidWorks.

```powershell
uv tool install git+https://github.com/0xFFD/solidworks-mcp    # or: pipx install git+https://github.com/0xFFD/solidworks-mcp
solidworks-mcp doctor        # checks pywin32, the SolidWorks registration, templates and the workspace
solidworks-mcp install       # registers the server with every MCP client it finds
```

`install` writes the configuration of every detected client and keeps a one-time `.bak-solidworks-mcp` backup of each file it changes. Pick clients with `--client codex,cursor`, preview with `--dry-run`, and undo with `solidworks-mcp uninstall`. Options such as `--workspace`, `--read-only` or `--require-marking` given to `install` are written into the client configurations.

SolidWorks starts automatically on the first call if it is not running; pass `--no-launch` to require a session that is already open.

<details>
<summary>Manual configuration</summary>

`solidworks-mcp config` prints a snippet for every client. Examples:

**Claude Code**
```powershell
claude mcp add --scope user solidworks -- C:\Users\me\.local\bin\solidworks-mcp.exe serve
```

**Codex CLI** (`%USERPROFILE%\.codex\config.toml`)
```toml
[mcp_servers.solidworks]
command = "C:\\Users\\me\\AppData\\Roaming\\uv\\tools\\solidworks-mcp\\Scripts\\python.exe"
args = ["-m", "solidworks_mcp", "serve"]
startup_timeout_sec = 60
tool_timeout_sec = 600
```

**Cursor, Windsurf, Gemini CLI, Claude Desktop** (`mcpServers` JSON)
```json
{ "mcpServers": { "solidworks": { "command": "C:\\Users\\me\\...\\python.exe", "args": ["-m", "solidworks_mcp", "serve"] } } }
```

**VS Code** (`%APPDATA%\Code\User\mcp.json`)
```json
{ "servers": { "solidworks": { "type": "stdio", "command": "C:\\Users\\me\\...\\python.exe", "args": ["-m", "solidworks_mcp", "serve"] } } }
```
</details>

## Tools

All lengths are millimetres and all angles degrees, in both directions, whatever unit system the document shows.

| Area | Tools |
|---|---|
| Design knowledge | `design_guide` (workflow, multirotor, fixed_wing, fasteners, structures, sheet_metal, materials, release) |
| Session and files | `sw_info`, `new_document`, `open_document`, `import_file` (STEP, IGES, Parasolid, ACIS, STL, 3MF), `activate_document`, `save_document`, `export`, `close_document`, `document_settings` |
| Sketching | `start_sketch` (on a plane or face, or a 3D sketch with lines, bent polylines, arcs and splines), `edit_sketch`, `sketch` (line, centerline, polyline with corner radii, rectangle, center rectangle, rounded rectangle, circle, bolt circle, circle grid, arc, ellipse, polygon, slot, point, spline, NACA airfoil, engraved text in any font, construction geometry, mirrored copies), `list_sketch`, `modify_sketch` (spline fit-point editing, trim, extend, split, linear and circular sketch patterns, offset, convert model edges, sketch fillet and chamfer, relations, driving dimensions, move, rotate, copy, delete, construction), `finish_sketch` (optionally fully defined with dimensions) |
| Features | `extrude`, `revolve`, `sweep`, `helix`, `loft`, `fillet`, `chamfer`, `shell`, `draft`, `hole`, `cosmetic_thread`, `linear_pattern`, `circular_pattern`, `driven_pattern` (table, sketch or curve driven), `mirror`, `ref_plane`, `ref_axis` |
| Multibody and surfaces | `list_bodies`, `combine` (add, subtract, common), `move_body` (move, rotate, copy), `split_body` (by planes, faces, sketches or surfaces), `delete_body`, `delete_faces`, `move_faces` (direct editing, also of imports), `knit_surfaces`, `thicken`, `fill_surface`, `trim_surface`, `mould_split` (core and cavity), `surface` (extrude, revolve, loft, planar, offset) |
| Weldments | `structural_member`, `trim_members`, `end_cap`, `gusset`, `cut_list`, `member_check` (hand-calculation stress, deflection, buckling and margins of safety) |
| Sheet metal | `sheet_metal_base` (incl. gauge tables), `gauge_table`, `edge_flange`, `miter_flange`, `jog`, `hem`, `forming_tool`, `create_forming_tool` (custom punches), `sketched_bend`, `closed_corner`, `convert_to_sheet_metal`, `flat_pattern` (info, flatten, fold, DXF/DWG export) |
| Model tree and edits | `list_features`, `dimensions`, `set_dimensions`, `edit_feature` (end conditions, depths, directions, radii, thickness, pattern counts, hole sizes, draft angles, sweep and loft definitions, reference plane and axis references), `configurations`, `configuration_table` (dash-number families, CSV), `equations`, `feature_action` (suppress, unsuppress, delete; per configuration), `undo`, `rebuild` |
| Geometry and checks | `list_faces`, `list_edges`, `measure`, `mass_properties`, `bounding_box`, `check_geometry`, `tolerance_stack` (worst case and RSS, 1D chains, linearised 2D/3D stacks and GD&T zones), `screenshot` |
| Engineering data | `list_materials`, `set_material`, `custom_properties` |
| GD&T | `add_datum`, `add_datum_target`, `add_geometric_tolerance` (incl. composite frames and projected zones), `dimxpert` (automatic MBD scheme), `annotation_views` (3D annotation views), `add_surface_finish`, `add_note` |
| Drawings | `create_drawing` (with ISO, ANSI, DIN, JIS, BSI or GB borders and title blocks, or a company .slddrt), `drawing_views`, `section_view`, `detail_view`, `projected_view`, `auxiliary_view`, `broken_view`, `breakout_section`, `add_view` (model or flat pattern), `sheets`, `drawing_dimension`, `view_dimensions` (list, arrange, move, tolerance, precision), `drawing_table` (BOM with balloons, revision block, hole table, general and tabulated dash-number tables, placed clear of views and the title block) |
| Assemblies | `insert_component`, `list_components`, `add_mate` (including limit, width, symmetric, gear, slot, hinge, rack and pinion, screw, universal joint, cam and path mates), `exploded_view`, `subassembly` (form, dissolve, save out to its own file), `motion_study` (motor-driven mechanisms), `interference_check`, `bom` (parts list with mass budget) |

113 tools and one prompt (`design`) in total. Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`) so clients can approve reads automatically and ask before deleting or closing.

### Picking faces and edges

Features need faces, edges and planes, and the model cannot click on the screen. Every tool that takes geometry accepts the same references:

| Reference | Meaning |
|---|---|
| `face:12`, `edge:3` | Index from `list_faces` / `list_edges`. Indices change after every feature, so list again before reusing them |
| `face@50,0,-10`, `edge@0,8,-22` | The face or edge passing through that point (mm). Refused if nothing is within 0.5 mm, so a guess never selects the wrong face |
| `face:Bracket-1/7`, `face@Bracket-1/0,22,0` | The same, on an assembly component |
| `plane:Front`, `plane:Top`, `plane:Right` | The default planes, whatever they are called in your template |
| `plane:Plane1`, `axis:Axis1`, `sketch:Sketch2`, `feature:Boss-Extrude1`, `comp:Bracket-1` | Anything in the feature tree by name |
| `body:Boss-Extrude2` | A solid body of a multibody part, by the name `list_bodies` reports |
| `seg:4`, `pt:2`, `seg@12,5`, `pt@17,7` | Inside the open sketch (`modify_sketch`): a segment or point by `list_sketch` index, or the one nearest a sketch point (mm) |

`start_sketch` reports how the sketch axes map into model space (on the Top plane, sketch +Y is model -Z), and `sketch(coords="model")` takes points in model coordinates directly, so the model never has to work that out itself.

### What the agent sees

Results are compact JSON meant for a language model. A hole call, for example:

```json
{"feature": "M5 Tapped Hole1", "type": "HoleWzd", "volume_mm3": 89250.019, "holes": 2, "table": "iso/tapped"}
```

Every feature reports its rebuild state and the new body volume, so a failed or unexpected feature shows up immediately. `screenshot` returns an image of the SolidWorks window. `export` to STEP returns the protocol read back from the file and the number of solids in it.

## Aerospace and defence use

The server includes controls that a regulated engineering environment usually asks for. They support a compliant process, but they do not make one: AS9100, ITAR, EAR and similar obligations apply to your organisation and its procedures, and no software can certify them.

**Audit trail.** Every tool call is appended to `<workspace>\audit\audit.jsonl` with the time (UTC), Windows user, host, tool, arguments, active document and outcome. Each record carries the SHA-256 hash of the one before it, so editing, deleting or reordering a record breaks the chain:

```powershell
solidworks-mcp verify-audit                 # OK: ...\audit.jsonl: 412 records, chain intact
```

Copy the log to write-once storage if you need it to survive a determined administrator; the hash chain proves integrity, it does not prevent deletion of the whole file.

**Export-control markings.** `custom_properties` sets a marking property (`ExportControl` by default) like any other property, so drawing title blocks can show it. With `--require-marking`, `export` refuses to write STEP, IGES, Parasolid, STL, PDF, DXF or DWG until the document has a non-empty marking. Every export result also echoes the marking it carried. STEP AP242 exports include the custom properties in the file.

**Data stays local.** The server uses stdio only, opens no network ports, makes no network calls and sends no telemetry. Be aware that the AI model you connect it to does receive tool results (dimensions, feature names, file names, images). If your parts are export controlled, check with your export-control officer that the model and client you use are approved for that data before you connect them.

**File confinement.** Every path is resolved inside the workspace (default `%USERPROFILE%\solidworks-mcp`) or a folder added with `--root`. Absolute paths elsewhere and `..` escapes are rejected, for opening as well as saving.

**Read-only mode.** `--read-only` allows only the tools that do not change models or files, for design reviews and audits.

**No code execution.** There is no tool that runs macros, scripts or arbitrary API calls. Everything the agent can do is one of the 113 tools above.

**Engineering standards.** `document_settings` switches between ASME Y14.5 and ISO drafting standards, `create_drawing` supports third and first angle projection, GD&T is applied to model faces (model-based definition), holes come from the standard Hole Wizard tables rather than hand-drawn circles, `finish_sketch(fully_define=true)` produces fully defined sketches, and `check_geometry` runs the SolidWorks body check before release.

**Human review.** Treat the agent's output like a junior designer's: an engineer reviews every model and drawing before it is released.

## Limitations

The tools cover the common path from a blank part to a released drawing. Plenty of SolidWorks is not exposed yet, and an agent cannot do the following:

- **Other modelling environments.** Mould halves split along a flat parting plane or a given parting surface (`mould_split`, with shrinkage; SolidWorks' tooling split does not work through the API, and the parting surface of a stepped or curved parting line is not generated from the part), routing is a separate add-in, and weldment members can be trimmed or extended to meet at corners and at T-joints (the T-joint end trim moves the member's end face, since SolidWorks' own end trim does nothing through the API). Surfaces can be created, trimmed, filled, knitted (into a solid when they close a volume), thickened and used to split bodies. 3D sketches take relations (including along X, Y and Z) and driving dimensions (true lengths and distances, so a tube or harness route can be fully defined and driven). Excel-linked design tables need Microsoft Excel (gauge tables are read without it, and `configuration_table` covers tabulated variants).
- **Sheet metal beyond flanges.** Base and edge flanges, hems, sketched bends, flat patterns and DXF blanks work. Jogs are built from two sketched bends solved to the requested offset (SolidWorks' own jog feature does not work through the API), gauge tables set thickness and bend radius (read without Excel; the table's K-factor applies to edge flanges), forming tools come from the SolidWorks design library or are made from a modelled punch with `create_forming_tool` (including pierced forms), and miter flanges take a straight profile or one with a return lip (Z and C rims), not arbitrary sketched profiles. The part's K-factor is set with the base flange (or taken from the gauge table), and edge flanges can override it.
- **Editing what already exists.** `edit_feature` redefines extrudes, cuts, revolves, fillets, chamfers, shells, patterns, Hole Wizard holes (size, depth, end condition), drafts, sweeps (diameter, twist, profile and path) and lofts (profiles in order, close). Reference planes can be re-based on new references, re-offset, re-angled and flipped, and reference axes moved to new references. Other feature types change through their dimensions or sketches only.
- **Detailed sketch work.** Spline fit points can be moved and added and their tangents set, sketch blocks can be made, placed and saved as block files, and reference pictures can be placed for tracing. Engraved text is spaced from each character's measured outline, so any font reads as normal text; it runs in a straight line at any angle or round an arc (clockwise over the top, counter-clockwise under the bottom), not along other curves.
- **Advanced features.** Fillets can be constant, asymmetric, conic, variable (between the two ends of one edge, or along a chain of edges with a radius at every vertex; no control points between vertices), face or full round, with setback corners where three filleted edges meet (tangent propagation is then off). Holes go on planar faces (one feature per face) and curved faces such as tube walls (one feature per hole, drilled square to the surface). Bodies can be combined, split, moved, copied and deleted.
- **Design tables.** Equations, global variables and configurations work (each with its own dimensions, suppressed features, material and custom properties), and `configuration_table` builds dash-numbered variants from rows of values and exports them as CSV, and `drawing_table(kind='general')` puts the tabulated (dash-number) table on the drawing; there is no Excel-linked design table inside the part.
- **Imported geometry.** `import_file` brings STEP, IGES and Parasolid models in as dumb solids: there is no feature recognition (FeatureWorks ships with SolidWorks but is not registered as an add-in on this installation, so its API cannot be reached), so imported parts are not edited by dimension, but they can be referenced, measured, mated, cut, split, patterned, converted to sheet metal and edited directly: `delete_faces` removes holes, pockets and fillets and heals the body, and `move_faces` offsets faces.
- **Design judgement.** The guide gives rules of thumb and a worked recipe, not analysis. Strength, vibration, thermal and airworthiness decisions stay with engineers.
- **Drawings beyond the basics.** `view_dimensions(arrange=true)` stacks linear dimensions in rows around their view within the free space, and moves diameter and radius dimensions, datum symbols and feature control frames that sit on a view into free space; `create_drawing` shows each datum symbol and feature control frame once and does the same placement, also for notes or hole callouts that fall outside the border. Placement is by clear space, not by drafting preference (leaders may cross the view). The title block is filled through custom properties (its fields are the sheet format's own), and views placed by SolidWorks are reported, not moved, when they overlap the title block. Reference dimensions and auxiliary views in assembly drawings take component references (`edge@Part-1/x,y,z`), including dimensions between two components.
- **Advanced GD&T and MBD.** DimXpert schemes take their tolerance values from the document's DimXpert options; size and location tolerances can then be changed annotation by annotation (`dimxpert(action='set_tolerance')`), but geometric tolerance values cannot (SolidWorks stores an edited frame value inconsistently with what the frame shows). `tolerance_stack` adds up one-dimensional chains and linearised 2D and 3D stacks (each contributor's angle or sensitivity) with position, profile and other zones (MMC bonus as a given extra zone), worst case and RSS; it does not derive sensitivities from the model or simulate (Monte Carlo) non-linear stacks. Annotation views group annotations by viewing direction but cannot be named (SolidWorks refuses the rename through the API) or set to section views. The unequally disposed profile modifier is drawn as text in the frame, so STEP carries it graphically rather than as semantic PMI.
- **Advanced assembly work.** Path mates keep a vertex on one curve (a point-on-curve mate, since SolidWorks' path mate has no API), along a chain of curves (fitted with one spline, its deviation reported), with the orientation either free or fixed; tangent-following pitch and yaw are not available (no standard mate ties a direction to a curve's tangent), motion studies are Basic Motion with constant-speed motors (SOLIDWORKS Motion analysis is a separate add-in). Sub-assemblies formed with `subassembly` live inside the parent file until `subassembly(action='save_out')` writes them to their own files. Explode steps move and rotate about the assembly X, Y and Z axes or move along an edge, axis or face normal (along a bolt axis, square to a mounting face), and radial steps move a ring of parts out from its own centre axis; steps along a reference cannot also rotate.
- **Analysis.** It cannot run SolidWorks Simulation, Flow Simulation, Costing or DFM checks (separate add-ins: Simulation is present but unlicensed here and will not load, the others are not installed). `member_check` covers the hand calculations used for member substantiation (section properties, unsymmetric bending, deflection, Euler and Johnson buckling, margins of safety) for straight members of the generated profiles; it has no stress concentrations, joints or plate behaviour. Mass properties, measurement, interference and the body check are the other analysis tools.
- **Data management.** There is no SOLIDWORKS PDM or vault access, check-in, revision workflow or ECO handling.
- **Seeing and clicking.** The agent sees the model through `screenshot` images and the JSON returned by tools. It cannot click in the SolidWorks window, and there is deliberately no way to run macros or arbitrary API calls to get around a missing tool.
- **Platform.** Windows only, one SolidWorks session at a time, and each call is as fast as SolidWorks itself. STEP AP242 needs the SOLIDWORKS MBD licence.

Issues and pull requests that close any of these gaps are welcome.

## Configuration

| Flag | Environment variable | Default |
|---|---|---|
| `--workspace DIR` | `SOLIDWORKS_MCP_WORKSPACE` | `%USERPROFILE%\solidworks-mcp` |
| `--root DIR` (repeatable) | `SOLIDWORKS_MCP_ROOTS` (separated by `;`) | none; extra folders tools may read and write |
| `--read-only` | `SOLIDWORKS_MCP_READ_ONLY=1` | off |
| `--require-marking` | `SOLIDWORKS_MCP_REQUIRE_MARKING=1` | off |
| `--marking-property NAME` | `SOLIDWORKS_MCP_MARKING_PROPERTY` | `ExportControl` |
| `--audit-log FILE` | `SOLIDWORKS_MCP_AUDIT_LOG` | `<workspace>\audit\audit.jsonl` |
| `--no-audit` | `SOLIDWORKS_MCP_NO_AUDIT=1` | audit on |
| `--no-launch` | `SOLIDWORKS_MCP_NO_LAUNCH=1` | start SolidWorks when needed |

## Tested with

- SOLIDWORKS 2026 SP4 on Windows 11 Pro, Python 3.12, MCP Python SDK 2.3.
- Every tool, driven through the MCP protocol over stdio: the bracket above, a revolved titanium pin with a cosmetic thread, a shelled and drafted plate with patterned and mirrored clearance holes, 100 degree countersinks, drilled holes, a sweep and a loft, and a plate and pin assembly with mates and an interference check (1124.9 mm³ found when the pin was pushed into the plate).
- The security controls: read-only mode, blocked exports without a marking, paths outside the workspace, and audit tampering.
- Independent agents (Claude Code in headless mode, restricted to this server's tools) given only "Build an interceptor drone frame." and "Design the main wing for a small fixed-wing interceptor UAV.", as described above. Claude Code was run with both a large model and Claude Haiku. Codex CLI 0.160 with an OpenAI model was run end to end on a quadcopter frame request, as described above.
- STEP AP242 needs the SOLIDWORKS MBD licence. Without it `step_ap="auto"` falls back to AP214 and says so; this path is the one that was tested.
- Other SolidWorks versions: the server loads whichever type library the running SolidWorks registers, so 2021 and newer should work. They have not been tested yet, and reports are welcome.

## Development

```powershell
git clone https://github.com/0xFFD/solidworks-mcp; cd solidworks-mcp
uv sync
uv run pytest            # unit tests run anywhere; integration tests run when SolidWorks is installed
uv run ruff check src tests
npx @modelcontextprotocol/inspector uv run solidworks-mcp serve --workspace C:\temp\swmcp
```

The integration tests start the server over stdio exactly as a client does, build parts in a temporary workspace, check volumes, hole positions, STEP contents, drawings and the audit chain, and close only the documents they created.

Project layout: `server.py` (MCP server and instructions) · `registry.py` (tool decorator, path policy, audit hook, read-only gate) · `com.py` (COM thread, type library loading, unit conversion) · `geometry.py` (face and edge descriptors, entity references, component placement) · `shapes.py` (rounded outlines, NACA airfoils, mirroring) · `tools_guide.py` (design knowledge and recipes) · `data/aerospace materials.sldmat` (bundled materials) · `tools_doc.py`, `tools_sketch.py`, `tools_features.py`, `tools_inspect.py`, `tools_drawing.py`, `tools_assembly.py` (the tools) · `audit.py` (hash-chained log) · `install.py` (client configuration) · `cli.py`.

A few things about the SolidWorks API that this code handles and that are easy to trip over if you extend it:

- All COM calls run on one dedicated thread, because SolidWorks objects cannot be shared between threads.
- Interfaces are early bound from the type library, so 27-argument calls like `FeatureCut4` work directly and zero-argument methods are not mistaken for properties.
- Constant-radius fillets need the feature definition API since SolidWorks 2020; `FeatureFillet3` silently returns nothing.
- Hole Wizard places holes at the approximate pick point and sometimes on a neighbouring face. The `hole` tool selects the face entity, then moves every centre to its exact coordinates in the hole's position sketch and refuses positions that are not on that plane.
- `IGtol.SetFrameSymbols2` takes symbol codes as text (`<IGTOL-POSI>`, `<MOD-MMC>`). Numeric enum values are accepted without error and produce an empty frame.
- Plain STEP export cannot write AP242; that goes through `PublishSTEP242File2`, which only accepts a `.stp` file name.

## License

MIT. SOLIDWORKS is a registered trademark of Dassault Systèmes SolidWorks Corporation. This project is not affiliated with or endorsed by Dassault Systèmes.

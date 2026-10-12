"""The MCP server: instructions for the model, and every tool from the tools_* modules."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import (  # noqa: F401
    __version__,
    tools_assembly,
    tools_doc,
    tools_drawing,
    tools_features,
    tools_guide,
    tools_inspect,
    tools_sheetmetal,
    tools_sketch,
    tools_weldment,
)
from .registry import TOOLS, app, configure  # noqa: F401

INSTRUCTIONS = """\
SolidWorks CAD server. Units: every length is in millimetres and every angle in degrees, in and out.
For any design request ("build an interceptor drone frame", "design a bracket for ..."), call
design_guide first (topic 'workflow', then the matching one such as 'multirotor' or 'fixed_wing'): it gives
the modelling approach, sizing rules, standard mounting patterns and a step-by-step recipe. Choose the
missing numbers yourself, state them as assumptions, and build part by part.
Typical part workflow:
1. sw_info -> SolidWorks version and open documents. new_document(kind="part"), open_document(path), or
   import_file(path) for STEP/IGES/Parasolid models of bought-in parts.
2. start_sketch(on="plane:Top" | "plane:Front" | "plane:Right" | "face:N" | "face@x,y,z") -> returns how
   sketch X/Y map to model space. sketch(entities=[...]) adds lines, circles, arcs, rectangles, slots,
   polygons, splines, bolt circles, hole grids, NACA airfoils and engraved text in sketch coordinates
   (or model coordinates with coords="model"); polyline with radius draws any rounded outline and
   mirror:'both' makes symmetric copies. edit_sketch reopens an existing sketch; list_sketch and
   modify_sketch trim, offset, convert edges, add relations and driving dimensions, move or delete items.
   start_sketch(on="3d") opens a 3D sketch for sweep paths.
   finish_sketch(fully_define=true) closes it and reports whether it is fully defined.
3. Features use the latest sketch unless one is named: extrude (boss or cut), revolve, sweep, loft, then
   fillet, chamfer, shell, hole (Hole Wizard: plain, counterbore, countersink, tapped, at exact
   positions), draft, linear_pattern, circular_pattern, driven_pattern (table/sketch/curve), mirror, ref_plane, ref_axis, cosmetic_thread,
   helix (springs, modelled threads). Multibody: list_bodies, combine, move_body, split_body, delete_body,
   surface (surface bodies as split tools), 'body:Name' references.
   edit_feature changes an existing feature's end condition, depth, radius, thickness or pattern count.
   Sheet metal: sheet_metal_base (closed outline or open profile), edge_flange, miter_flange, hem, sketched_bend, closed_corner, convert_to_sheet_metal, flat_pattern (blank size,
   flatten, DXF export); design_guide('sheet_metal') has bend radii and a tray recipe.
4. Pick faces and edges with list_faces / list_edges (indices change after every feature) or by point:
   "face@x,y,z", "edge@x,y,z". Planes: "plane:Front|Top|Right" or any plane name.
5. Check the result: screenshot (returns an image), list_features (rebuild errors), mass_properties,
   bounding_box, check_geometry, measure, tolerance_stack (worst case and RSS clearances). Fix with set_dimensions,
   feature_action, undo.
6. Engineering data: set_material / list_materials, custom_properties (part number, revision,
   export-control marking), equations, configurations (variants: set_dimensions and feature_action take
   configurations=[...]), document_settings (units, ASME or ISO drafting standard),
   add_datum, add_datum_target, add_geometric_tolerance (composite frames, projected zones) and
   add_surface_finish (GD&T on part or assembly component faces, carried into drawings); dimxpert applies a
   complete automatic MBD scheme from chosen datum faces.
7. Output: save_document (native files), export (STEP AP242/AP214/AP203, IGES, Parasolid, STL, 3MF, PDF,
   DXF, DWG, PNG), create_drawing (standard views, model dimensions and GD&T), section_view, add_note;
   detail_view, projected_view, auxiliary_view, broken_view, breakout_section, add_view (incl. flat_pattern), sheets, drawing_dimension, view_dimensions,
   drawing_table (bom with balloons, revision block, hole table).
Weldments: structural_member (tube, bar, angle, channel along sketch lines; frames, rigs, GSE), trim_members (corner trim/extend), end_cap, gusset, cut_list.
Assemblies: insert_component (origin at position), add_mate (incl. limit_distance/limit_angle travel stops, width,
symmetric, gear, slot, hinge, rack_pinion, screw, universal_joint, cam), exploded_view (incl. radial steps), subassembly (form,
dissolve), list_components, interference_check, bom (mass budget).
Results are proposals for an engineer to review; check every model and drawing before release.
"""

mcp = MCPServer(
    name="solidworks",
    title="SolidWorks",
    version=__version__,
    instructions=INSTRUCTIONS,
    website_url="https://github.com/0xFFD/solidworks-mcp",
)

for _fn, _annotations in TOOLS:
    mcp.add_tool(_fn, annotations=_annotations, structured_output=False)


@mcp.prompt(name="design", title="Design hardware in SolidWorks", description="Design a part or assembly from a short request, e.g. 'an interceptor drone frame'.")
def design_prompt(request: str) -> str:
    steps = [
        f"Design the following in SolidWorks using the solidworks tools: {request}",
        "",
        "1. Call design_guide('workflow') and the guide that matches the request (multirotor, fixed_wing, ...).",
        "2. Decide the missing requirements yourself (size, materials, components it must carry) and list them as assumptions.",
        "3. Plan the parts and how they join, then model each part, checking every feature result and taking screenshots.",
        "4. Set materials and custom properties, run check_geometry and mass_properties, assemble with mates and run interference_check.",
        "5. Save every file, export STEP, create drawings for manufactured parts, and report mass, centre of mass, files and open engineering checks.",
    ]
    return chr(10).join(steps)

"""Design knowledge for agents: workflows, worked recipes and engineering rules of thumb.

An agent asked to "build an interceptor drone frame" usually knows the words but not the numbers or
the order of SolidWorks operations that produce a sound part. design_guide gives it both. The values are
general guidance for preliminary design; the agent is told to state its assumptions and an engineer
reviews the result.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .registry import READ_ONLY, tool

GUIDES: dict[str, str] = {}

GUIDES["workflow"] = """\
WORKFLOW FOR ANY DESIGN REQUEST
1. Pin down the requirements before modelling. If the request is short ("build an interceptor drone
   frame"), choose sensible values from these guides, state them as assumptions in your reply, and
   record them in custom_properties (Description, plus any key figures).
2. Plan the parts: list each part, its material, the manufacturing process (CNC plate, machined
   block, composite plate, additive) and how parts join (fasteners, standoffs, bonding).
3. Model each part as its own .sldprt in a clean feature order:
   base sketch -> extrude/revolve -> secondary bosses -> cuts and holes -> patterns/mirrors ->
   fillets and chamfers last. Keep the part symmetric about the default planes where the design is.
4. After every feature read the result: a missing feature, a rebuild error or an unexpected
   volume_mm3 means something went wrong; fix it (undo, set_dimensions) before continuing.
   Take a screenshot after each major step and look at it.
5. Engineering data: set_material, custom_properties (PartNo, Description, Revision, Material,
   ExportControl marking when required), document_settings(drafting_standard='asme' or 'iso').
6. Verify: check_geometry (must be ok), mass_properties (mass and centre of mass against the
   budget), bounding_box (envelope), measure for critical clearances.
7. Assemble: new assembly, insert the base part with fix=true, add the others, mate them
   (concentric for holes and pins, coincident for faces), then interference_check (must be 0)
   and bom() for the parts list and mass budget.
8. Output: save_document every part and the assembly, export STEP for suppliers, create_drawing
   for parts that get manufactured, add datums and GD&T on functional faces first.
9. Report: what you built, the assumptions, mass and CoG, files written, and anything an engineer
   must check (strength, vibration, thermal, fasteners).

COORDINATES
- Units are mm and degrees. Model axes: X right, Y up, Z towards the viewer (Front view).
- The Top plane is XZ: a sketch on it uses sketch +X = model +X and sketch +Y = model -Z, and an
  extrude from it goes +Y. Use coords='model' in sketch() to avoid converting by hand.
- The Front plane is XY (extrudes go +Z); the Right plane is YZ (extrudes go +X).
- For flat parts (frame plates, brackets) sketch on the Top plane and extrude up in Y.

SKETCH TECHNIQUE
- One closed outline per region. Overlapping closed shapes in one sketch create ambiguous regions:
  build overlapping shapes as separate features (each extrude merges with the body) instead.
- polyline with radius draws any rounded outline in one entity (plates, arms, brackets).
- bolt_circle and circle_grid place hole patterns; mirror:'both' makes symmetric copies.
- Cutting features: put all holes of one depth in one sketch and do one extrude(cut=True).
- Prefer hole() for standard fastener holes (counterbore, countersink, tapped): exact positions and
  correct callouts on drawings. Plain circles cut through are fine for clearance holes in plates.
"""

GUIDES["multirotor"] = """\
MULTIROTOR FRAMES (quad, hex, octo; racing, cinematic, interceptor and other high-speed types)

SIZING
- Wheelbase W = diagonal distance between opposite motor centres. For an X quad the motors sit at
  (+-W/2*cos45, +-W/2*sin45) about the centre, so adjacent motors are W/sqrt(2) apart.
- Propeller clearance: W/sqrt(2) >= prop diameter + 6..10 mm. Prop diameters: 3 in = 76.2 mm,
  5 in = 127 mm, 7 in = 177.8 mm, 10 in = 254 mm.
- Propeller disc clearance in plan view: every part of the frame and payload that is not under the
  motor's own arm must stay at least prop radius + 5 mm from that motor's centre. Check the corners of
  the centre plates and of any fairing: they are the usual offenders on stretched-X frames.
- Typical wheelbases: 3 in props ~ 120-150 mm, 5 in ~ 210-240 mm, 7 in ~ 280-330 mm,
  10 in ~ 400-450 mm.
- Stretched-X (longer front-back than side-side) suits fast forward flight and gives room for a
  forward sensor; keep the thrust centre at the centre of mass.

MOUNTING PATTERNS (check the actual component datasheet)
- Flight controller / ESC stacks: 30.5 x 30.5 mm M3, 20 x 20 mm M2 or M3, 25.5 x 25.5 mm M2.
  As bolt_circle: pcd = side * 1.4142 (30.5 -> 43.13, 20 -> 28.28, 25.5 -> 36.06), start_angle 45.
- Motors, common bases: 12 x 12 mm M2 (13xx-15xx), 16 x 16 mm M3 (22xx-24xx),
  19 x 19 mm M3 (25xx-28xx), 25 x 25 mm M3 (larger). As bolt_circle pcd 16.97 / 22.63 / 26.87 / 35.36.
  Add a centre clearance hole for the shaft and circlip, typically 6-9 mm.
- Camera mounts: 19 mm (micro) and 20-22 mm wide cages are common; a forward camera on a fast frame
  is tilted up 30-60 degrees.

STRUCTURE
- Carbon fibre plate (material 'CFRP plate quasi-isotropic (generic)'): arms 4-6 mm thick for
  5-7 in craft, bottom plate 2-3 mm, top plate 1.5-2 mm. Unibody (arms and centre in one plate)
  is stiffer; separate arms are replaceable.
- Arm width at the root 12-16 mm (5 in) to 18-25 mm (7-10 in); taper towards the motor; round the
  motor pad to the motor diameter plus 2-4 mm.
- Arm stiffness check (cantilever): tip deflection d = F * L^3 / (3 * E * I), I = b * t^3 / 12,
  with F = maximum thrust per motor (N), L = arm length (mm), b = width, t = thickness, E for CFRP
  plate about 50 GPa (50000 N/mm^2). Aim for d below about 1 % of L.
- Standoffs between plates: M3 aluminium, 20-35 mm tall; give the stack and wiring 2-3 mm clearance.
- Inner corners need radii (CNC routing, stress): at least 1.5 mm, more at arm roots (3-8 mm).
- Lightening cut-outs: keep at least 3 x hole diameter of material around fastener holes and at
  least 4 mm webs in 3-5 mm carbon plate; never cut through the arm root.
- Interceptor and other high-speed airframes: aim for thrust-to-weight above 4:1, small frontal
  area, a canopy or fairing over the stack, a stiff stretched-X frame, a forward sensor or camera
  mount with clear field of view past the props, and mass concentrated near the centre. Model
  the fairing as a separate part (loft between ellipses on offset planes, then shell 1.2-2 mm for
  PA12 or PETG). Keep payload, release or effector design out of the frame model unless the
  requirement explicitly specifies an approved interface.

RECIPE: STRETCHED-X QUAD FRAME, 5 IN PROPS (verified end to end with this server; adapt the numbers)
Layout on the Top plane: sketch +Y is forward (model -Z). Motors at (+-100, +-75): side spacing 200 mm,
fore-aft 150 mm, both above 127 + 10. Body 60 x 124 mm (corners at (+-30, +-62)): nearest body corner is
71 mm from each motor, clear of the 63.5 mm prop radius by 7.7 mm. Standoffs at (+-21, +-52).
BOTTOM PLATE (5 mm CFRP)
  new_document(kind='part'); document_settings(drafting_standard='iso')
  start_sketch('plane:Top'); sketch([{type:'rounded_rectangle', corner1:[-30,-62], corner2:[30,62], radius:8}])
  finish_sketch(fully_define=true); extrude(depth=5)                                    # body
  start_sketch('plane:Top'); sketch([{type:'slot', start:[18,15], end:[100,75], width:14, mirror:'both'}])
  finish_sketch(); extrude(depth=5)                                                    # four arms, merged
  start_sketch('plane:Top'); sketch([{type:'circle', center:[100,75], diameter:30, mirror:'both'}])
  finish_sketch(); extrude(depth=5)                                                    # motor pads
  list_edges(type='line'); fillet(radius=5, edges=[the 5 mm tall edges at x = +-30 where arms meet the body])
  start_sketch('plane:Top'); sketch([
     {type:'bolt_circle', center:[100,75], pcd:22.63, count:4, hole_diameter:3.4, start_angle:45, mirror:'both'},
     {type:'circle', center:[100,75], diameter:7, mirror:'both'},                      # motor shaft
     {type:'bolt_circle', center:[0,0], pcd:43.13, count:4, hole_diameter:3.4, start_angle:45},  # 30.5 stack
     {type:'circle', center:[21,52], diameter:3.4, mirror:'both'},                     # standoffs
     {type:'slot', start:[0,25], end:[0,42], width:10, mirror:'up_down'},              # body lightening
     {type:'slot', start:[42.6,33], end:[72.1,54.6], width:5, mirror:'both'}])       # arm lightening
  finish_sketch(); extrude(cut=true, end='through_all_both')
  start_sketch('face@0,5,55'); sketch([{type:'text', at:[-14,-58], text:'INT-FRM-001', height:3.5}])
  finish_sketch(); extrude(cut=true, depth=0.3)                                        # part marking
  set_material('CFRP plate quasi-isotropic (generic)'); custom_properties({PartNo, Description, Revision})
  check_geometry(); mass_properties(); screenshot(view='top'); save_document('drone/frame_bottom.sldprt')
TOP PLATE (2 mm CFRP): same body outline extruded 2 mm; the same stack holes, standoff holes and
  body slots cut through_all_both. STANDOFF: polygon sides 6 radius 2.75 (5.5 mm hex) extruded 25,
  hole(size='M3', kind='tapped', depth=7, thread_depth=6) at [0,25,0] and at [0,0,0], material 6061-T6.
FAIRING (PA12): ref_plane offset from 'plane:Front' 45 and 95 with flip=true (forward), ellipses
  27x20, 25x19 (center [0,2]) and 9x7 (center [0,4]) on Front, Plane1, Plane2, loft(profiles=[all three]),
  shell(thickness=1.5, remove_faces=['face@0,0,0']).
ASSEMBLY
  new_document(kind='assembly'); insert_component('drone/frame_bottom.sldprt', fix=true)
  insert_component positions put the component ORIGIN at the point, in assembly mm.
  For each standoff at (x, z) = (+-21, -+52) in model coordinates (sketch y 52 is model z -52):
    insert_component('drone/standoff_25.sldprt', position=[x, 5, z]); list_faces(component='standoff_25-N', type='cylinder')
    add_mate('concentric', [standoff hole face 'face:standoff_25-N/<i>', 'face@frame_bottom-1/<x+1.7>,2.5,<z>'])
    add_mate('coincident', ['face@standoff_25-N/<x>,5,<z+2>', 'face@frame_bottom-1/<x>,5,<z+3.5>'], alignment='anti_aligned')
  insert_component('drone/frame_top.sldprt', position=[0,30,0]); coincident its bottom face to a standoff top,
    concentric two of its standoff holes with the bottom plate's.
  insert_component('drone/fairing.sldprt', position=[0,52,40], fix=true)
  interference_check() must report 0; mass_properties() gives the frame mass and centre of mass;
  bom() lists every part with quantity, part number, material and mass.
  Points given as 'face@Component-1/x,y,z' are in assembly coordinates.
Result of this recipe: about 140 g frame (without electronics), centre of mass on the centreline.
"""

GUIDES["fixed_wing"] = """\
FIXED-WING PARTS: WINGS, FINS, TAILS, FUSELAGES

AIRFOILS
- sketch {type:'airfoil', naca:'2412', chord, origin:[x, y] (leading edge), angle} draws a closed
  NACA 4-digit section. Symmetric sections (0008-0012) suit fins, tails and fast small wings;
  cambered sections (2412, 4412) suit lifting wings at low speed.
- Wing: make planes at each span station with ref_plane(mode='offset', refs=['plane:Front'],
  distance=span_station) and sketch one airfoil per plane (root chord, tip chord, sweep by moving
  the origin back, washout by reducing angle), then loft(profiles=[root, tip]). Mirror the wing
  about the Front plane for the other side.
- Dihedral: offset the tip origin up by span * tan(dihedral).
- Fin or flat tail: extrude a fin planform (polyline with radius) to its thickness, or loft
  airfoils for a profiled fin.

FUSELAGES AND FAIRINGS
- Loft between ellipses (sketch type 'ellipse') on offset planes for the cross-sections, or
  revolve a spline half-profile about a centerline for a body of revolution; then shell(thickness,
  remove_faces=[...]) to make it hollow and add mounting bosses inside.
- Nose cones: revolve; ogive or elliptical profiles drawn as a spline.

STRUCTURE
- Wing spars: tubes or flat carbon strips at about 25-30 % chord, where the section is thickest.
- Keep skins at least 0.8 mm (machined aluminium) or 1.2-2 mm (printed polymer).
- Check centre of mass against the intended position (for conventional layouts roughly 25-33 % of
  the mean aerodynamic chord) with mass_properties on the assembly.
"""

GUIDES["fasteners"] = """\
FASTENERS AND HOLES

CLEARANCE HOLES (ISO 273: fine / medium / coarse, mm)
  M2 2.2 / 2.4 / 2.6    M2.5 2.7 / 2.9 / 3.1    M3 3.2 / 3.4 / 3.6
  M4 4.3 / 4.5 / 4.8    M5 5.3 / 5.5 / 5.8      M6 6.4 / 6.6 / 7.0    M8 8.4 / 9.0 / 10.0
TAP DRILLS (coarse thread): M2 1.6, M2.5 2.05, M3 2.5, M4 3.3, M5 4.2, M6 5.0, M8 6.8.
Use hole(kind='tapped') so the drawing gets a proper thread callout and cosmetic thread.

SPACING
- Metal parts: edge distance (hole centre to edge) at least 2 x D, pitch at least 3-4 x D.
- Composite laminates: edge distance at least 3 x D, side distance and pitch at least 4-5 x D;
  avoid countersinks in thin laminates (keep the cylindrical part at least 1/3 of the thickness).
- Thread engagement: at least 1.5 x D in aluminium, 1 x D in steel or titanium; for threads that are
  reassembled often in aluminium or polymer, use helical inserts (hole wizard Helicoil tables) or
  heat-set inserts in polymer.

AEROSPACE PRACTICE
- Countersunk fasteners in aerospace are commonly 100 degrees (hole fastener='flat_head_100',
  standard='ansi_inch'); metric industrial ones are 90 degrees.
- Secure fasteners against vibration: self-locking nuts, threadlocker or lock wire; note it on the
  drawing with add_note.
- Do not put bolted joints in highly loaded fillets or at arm roots; move them at least 2 x D away.
"""

GUIDES["structures"] = """\
STRUCTURAL AND MANUFACTURING RULES OF THUMB

CNC MACHINED METAL (aluminium 6061-T6 / 7075-T6, titanium Ti-6Al-4V)
- Minimum wall 1.0 mm aluminium, 1.5 mm titanium; pocket floors 1.0 mm minimum.
- Inside corners need a radius at least the cutter radius (2-3 mm typical); deep pockets want
  corner radius >= 1/6 of the depth. Pocket depth <= 4 x cutter diameter.
- Fillet load-path transitions (radius >= wall thickness) to reduce stress concentration.
- Specify a general edge break with add_note (for example 'BREAK SHARP EDGES 0.2-0.5').

COMPOSITE PLATES (CFRP, G10/FR4)
- 2D profiles only (water-jet or CNC routed): sketch, extrude, through-cuts. No blind pockets.
- Inside corner radius at least 1.5 mm. Keep material between holes at least 2 x plate thickness.

ADDITIVE (PA12 SLS, PETG or PA12-CF FDM, metal LPBF)
- Walls: SLS 1.0-1.5 mm, FDM 1.2-2.0 mm (multiples of the nozzle width), metal LPBF 0.5-1.0 mm.
- FDM: avoid unsupported overhangs below 45 degrees from horizontal; orient load paths along layers.
- Holes for screws in polymers: design tapped holes as tap-drill size or use heat-set inserts.

STIFFNESS AND STRENGTH QUICK CHECKS
- Cantilever tip deflection d = F L^3 / (3 E I); rectangle I = b t^3 / 12; tube I = pi (D^4 - d^4) / 64.
- Bending stress s = M c / I with M = F L, c = t / 2. Keep s below yield (metals) or about 30-40 %
  of tensile strength (composites) for preliminary sizing; a stress engineer signs off.
- E: CFRP plate ~50 GPa, 7075-T6 72 GPa, 6061-T6 69 GPa, Ti-6Al-4V ~105-114 GPa, PA12 ~1.7 GPa.
"""

GUIDES["materials"] = """\
MATERIALS (exact names for set_material; use list_materials(search=...) to see more)
- 'CFRP plate quasi-isotropic (generic)': drone frames, plates, ribs. 1550 kg/m3.
- 'CFRP unidirectional 0 deg T700-epoxy (generic)': spars and tubes loaded along the fibre.
- 'CFRP woven 0-90 twill (generic)', 'Aramid-epoxy laminate (generic)', 'G10-FR4 glass-epoxy (generic)'.
- 'GFRP plate quasi-isotropic (generic)'. Wing and fairing cores: 'PMI foam core 52 kg-m3 (generic)',
  'PVC foam core 80 kg-m3 (generic)', 'XPS foam 33 kg-m3 (generic)', 'EPP foam 30 kg-m3 (generic)' (crash-tolerant),
  'Balsa medium 160 kg-m3 (generic)'. For skinned parts model the core as the part and add the skin mass
  (cloth areal weight plus resin) to the budget by hand, or model the skin as a shell body.
- '7075-T6 (SN)' and '7075-T6, Plate (SS)': high-strength machined aluminium.
- '6061-T6 (SS)': general machined aluminium, weldable. '2024-T3': skins and fatigue-critical parts.
- 'Ti-6Al-4VSolution treated and aged (SS)': high-strength, high-temperature fittings.
- 'AISI 4130 Steel, normalized at 870C': welded steel structure and fittings.
- 'PA12 SLS (generic)', 'PA12-CF FDM (generic)', 'PETG (generic)', 'Polycarbonate (generic)',
  'Polyetheretherketone (PEEK)', 'PEEK-CF30 (generic)': fairings, canopies, brackets, ducts.
The '(generic)' materials come with this server: approximate properties good enough for mass
budgets and early sizing, not for qualification. Replace them with supplier data for release.
"""

GUIDES["sheet_metal"] = """\
SHEET METAL (avionics trays, chassis, covers, brackets, clips, ribs)
Preliminary rules of thumb; the shop's bend tables and the material specification govern.

MATERIALS AND MINIMUM INSIDE BEND RADIUS (multiples of thickness t, 90 degree bends)
- 5052-H32 aluminium: 0.5-1 t. Easy to form, not heat treatable; covers, trays, enclosures.
- 6061-T6: about 1.5-3 t (bend 6061-T4 or O tighter, then age). General brackets.
- 2024-T3: about 2.5-4 t. Aerospace skins, ribs and clips; fatigue-critical parts.
- 7075-T6: about 5-9 t and prone to cracking; form in the O or W temper and heat treat, or machine instead.
- 300-series stainless (annealed): 0.5-1 t. Ti-6Al-4V (annealed): about 3-5 t cold, often hot formed.
- Bend across the rolling direction when the radius is near the minimum.
K-FACTOR: about 0.33 for radii below t, 0.40-0.45 for 1-3 t, 0.5 for large radii. Set the part's K-factor
with sheet_metal_base(k_factor=...) (a gauge table's K-factor is applied automatically); edge_flange can
override it for its own bends. Check the flat size from flat_pattern against the shop's bend allowance before release.

GEOMETRY RULES
- Flange length at least 4 t (shorter flanges do not form cleanly in a press brake).
- Holes and slots at least 2.5 t + r from a bend (measured to the hole edge), or they distort.
- Hole diameter at least t; hole edge to sheet edge at least 2 x hole diameter (see fasteners).
- Keep one bend radius and one thickness per part. Relief cuts where a bend runs into an edge are
  added by SolidWorks; adjacent flanges at different angles can collide at the corner (rebuild
  warning, flat pattern fails): leave a gap or use the same angle.

RECIPE: AVIONICS TRAY
1. start_sketch('plane:Top'); sketch rectangle of the outer footprint; finish_sketch.
2. sheet_metal_base(thickness=1.6, bend_radius=1.6) (5052-H32 or 6061-T6).
3. edge_flange(edges=['edge@0,0,W/2', 'edge@0,0,-W/2'], length=H) then the two short sides; the
   flange rises on the side of the face the edge belongs to. Or one miter_flange along the chain of
   edges in order (mitred corners with a small rip gap), which keeps the corners tidy for welding.
4. Mounting holes with hole(...) on the floor and flanges (keep them 2.5 t + r from the bends).
5. set_material, custom_properties; flat_pattern() to read the blank size and bend count.
6. save_document, then flat_pattern(export='part_flat.dxf') for laser or punch programming.
Open sections (L brackets, U channels, hat stiffeners): sketch the open profile on a plane and use
sheet_metal_base(..., depth=length).
"""

GUIDES["release"] = """\
BEFORE CALLING A PART DONE
- check_geometry ok, list_features without rebuild errors, sketches fully defined where practical.
- Material set; custom properties PartNo, Description, Revision, Material, DrawnBy, and the
  ExportControl marking if your organisation requires one.
- Datums on the faces that locate the part in the assembly (primary on the largest mounting face),
  flatness on that face, position tolerances on mounting holes (diameter_zone, MMC where they are
  clearance holes for fasteners), surface finish on functional faces.
- create_drawing with the right projection (third angle for ASME, first angle for ISO); check the
  views with screenshot, add a section through holes or pockets, and general notes (material,
  finish, edge breaks, unspecified tolerances).
- export STEP for suppliers and PDF of the drawing; save everything.
- Tell the user what an engineer still has to verify.
"""

TOPICS = ("workflow", "multirotor", "fixed_wing", "fasteners", "structures", "sheet_metal", "materials", "release")


@tool(READ_ONLY)
def design_guide(
    topic: Annotated[Literal["workflow", "multirotor", "fixed_wing", "fasteners", "structures", "sheet_metal", "materials", "release", "all"], Field(description=(
        "workflow: how to approach any design request with these tools; multirotor: drone frames incl. sizing, "
        "mounting patterns and a step by step recipe; fixed_wing: wings, fins, fuselages with airfoils and lofts; "
        "fasteners: clearance and tap sizes, edge distances; structures: machining, composite and additive rules and "
        "quick strength checks; sheet_metal: bend radii, K-factors and a tray recipe; materials: which material names to use; "
        "release: checks before a part is done."))] = "workflow",
) -> str:
    """Engineering guidance and step-by-step recipes for designing aerospace and defence hardware with
    this server. Call it first when asked to design something (for example "build an interceptor
    drone frame"), then choose dimensions, state assumptions and build."""
    if topic == "all":
        return "\n\n".join(GUIDES[t] for t in TOPICS)
    return GUIDES[topic]

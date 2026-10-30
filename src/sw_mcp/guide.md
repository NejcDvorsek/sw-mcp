# Modelling guide for the SolidWorks MCP server

How to get parts out of this server that are right the first time. Read the
short connect-time instructions first; this is the long version.

## 1. Coordinates and conventions

- All lengths are **millimetres**, angles **degrees**. The server converts to
  SolidWorks' metres and radians.
- New geometry starts on the **Front plane** (XY) and extrudes along **+Z** from
  z = 0: `add_box`, `add_extruded_profile` (also `spline=True`), `add_disc`,
  `cut_profile` and the lofts all use this frame.
  - `add_box` spans x 0..width, y 0..height, z 0..depth (corner at the origin).
  - `add_disc` is centred on the origin, `add_cylinder` and
    `add_revolved_profile` revolve about the **Y** axis; give
    `add_revolved_profile` an `axis_mm` (two points) to turn [x, y] points about
    any line on the Front plane instead (Pappus still holds: area x 2 pi x
    the centroid's distance from that line).
  - Sweeps: the path lies on the Front plane. For `add_swept_profile` it **must
    start at the origin heading +X**, and the profile is drawn on the Right
    plane (u along -Z, v along +Y).
- Faces are selected by the direction they face: `+x`, `-z`, ... The `*_on_face`
  tools take **3D points on the face** and use the planar face facing that way
  **through the point**: the top, a pocket floor, a step in between. A point on
  no such face is refused instead of being projected, and the error lists the
  levels that do exist. `+z:outer` / `+z:inner` force the outermost / innermost
  face. Tools without a 3D point (`cut_profile`, `add_hole`, `cut_slot`) use
  the outermost +Z face.

## 2. Work in small, verified steps

- Every modelling call returns the measured volume, mass and bounding box.
  **Compare with your own hand calculation after every step** and stop at the
  first mismatch. A wrong feature under ten correct ones is expensive to find.
- Hand calculations that work: prism = area x depth; revolve = area x 2 pi x
  centroid radius (Pappus); sweep = profile area x path length; a helical
  thread groove = area x 2 pi x centroid radius / pitch per mm of thread.
- Look before you select: `list_faces` / `list_edges` give indices, normals,
  areas and axes; `screenshot` shows the shape, from any standard view
  (`view="front"`, `"top"`, ...) or any direction (`from_dir=[-1, 1, -1]` sees
  what iso hides underneath, behind), and zoomed onto a detail with
  `zoom_mm=[[x1, y1, z1], [x2, y2, z2]]`. A cylindrical face comes with
  its axis, radius and a point on the axis, so holes and hole circles can be
  measured; `list_faces(component=...)` does that for a part in an assembly,
  in the part's own frame.
- **Undo a step** with `delete_feature(name)`: the feature goes together with
  its sketch. It refuses while other features are built on it (a fillet on its
  edges, a sketch on its face) and names them, and what is built on those;
  `with_children=True` deletes them all, `dry_run=True` only lists them. A
  loft or rib keeps its hidden helper plane, as in SolidWorks itself: delete
  that plane as well (`list_features` shows it as a RefPlane).
  Equations that named its dimensions break and come back as
  `broken_equations`; `with_equations=True` deletes them.
  `suppress_feature` takes a feature out but keeps it and its
  dimensions, to try a variant; `suppress=False` brings it back together with
  what depends on it.
- **A boss fills the cuts made before it**: every tool adds its feature at the
  end of the history, so a plate drawn over earlier holes closes them, and
  only the volume shows it. `reorder_feature("Plate", before="Hole1")` moves
  it before the first of them, and they cut through it again.
- A tool that cannot do what was asked returns `{ok: false, error}`. The error
  names the cause. Several SolidWorks calls fail silently (no feature, no
  error); the server checks for that and fails loud instead.

## 3. Dimensions: every sketch is fully defined

Every tool leaves its sketches fully defined, the way a designer would: a
relation only where the input is exactly horizontal, vertical or on the origin,
and one dimension from the origin for everything else. Nothing moves, and no
stray drag in SolidWorks can change the part.

- Tools return `dimensions`, `{role: name}`, and `fully_defined: true`. Change a
  dimension with `set_dimension(name, value)`; the new volume shows it worked.
  Roles follow the tool: box `width` `height` `depth`; disc `diameter`
  `thickness`; cylinder `radius` `height`; cone `bottom_radius` `top_radius`
  `height`; holes `diameter` `x` `y` (a counterbore also `clearance_diameter`,
  `cbore_diameter`, `cbore_x`, `cbore_y`, `cbore_depth`); slot `end1_x`
  `end1_y` `end2_x` `end2_y` `width`; loft `profile1_height`, `profile0_x1`, ...
- Polygons get `x<i>` / `y<i>`: the x or y of vertex i, measured from the
  origin, once per wall position (the vertices of one vertical wall share one
  `x`). On a face sketch these are that sketch's own horizontal and vertical
  directions, not the model axes. A coordinate of 0 is a relation, so it has no
  dimension.
- Rounded corners (`corner_radii_mm`) are sketch fillets: each corner keeps its
  `x<i>` / `y<i>` as a virtual sharp, and corners with the same radius share one
  dimension, `radius` when all rounded corners share it, else `r<i>` named after
  the first corner with that radius.
- Profiles with more than 24 points (typically traced from a mesh), sweep paths
  and splines are **fixed** instead: dimensioning them takes minutes and gives
  nobody a usable handle. Rebuild them to change them.
- `list_dimensions` names every dimension in the part with its value, e.g. for
  a part opened from disk.
- `list_features` gives a part's history in tree order (name, SolidWorks type,
  suppressed), flags features that fail to rebuild, and names the sketches that
  are not fully defined. A part drawn by hand may have those: their geometry
  can still move, so say so before you build on it.
- Drive several dimensions from one number with a global variable:
  `set_equation('"W" = 40')`, then `set_equation('"width@Sketch1" = "W"')`;
  `set_equation('"W" = 45')` changes it again. `set_equation([...])` sets a
  list with one rebuild, and every result names the features that fail to
  rebuild (`failing_features`): set a variable over a range to find where a
  part breaks. `list_equations` marks equations left broken by a deleted
  feature; `delete_equation` removes them. An
  equation-driven dimension refuses `set_dimension` and names its equation. An
  angle (a revolve's, an angle mate's) is set in degrees; `list_dimensions`
  shows each dimension's unit.

## 4. Recipes

- **Holes**: one tool, `add_hole`. `diameter_mm` at (x, y) goes through along
  Z; `cbore_diameter_mm` + `cbore_depth_mm` make a counterbore from +Z; `face`
  and `z_mm` put it on any planar face, through or blind with `depth_mm` (a
  heat-set insert hole, a screw pilot).
- **Standard holes**: `add_hole(iso=..., size="M6", face=..., x, y, z)` sizes a
  hole from SolidWorks' own Hole Wizard ISO tables: `clearance` (ISO 273; M3 is 3.2 / 3.4 / 3.6 for a close / normal /
  loose fit), `counterbore` (socket head cap screw), `countersink` (socket
  countersunk screw) and `tapped` (ISO tap drill plus a cosmetic thread). The
  sizes it used come back in `hole`, ready for your hand calculation. A blind
  hole ends in a 118 degree drill point; its depth counts from the surface.
  The Hole Wizard only makes *cosmetic* threads; `thread="modeled"` drills the
  ISO basic minor diameter and cuts a real, printable thread with the Thread
  feature, over the standard depth (2 x D) or all the way through.
- **Adding material to a part**: `add_boss_on_face` (a round boss: standoff,
  peg) and `add_extruded_profile(face=...)` (a polygon pad or ledge) grow out of
  any planar face. A PCB standoff is a boss plus a blind hole in its top.
- **Pockets and slots**: `cut_profile` (+Z face, or any face with `face=`), `cut_slot` (obround on +Z). A rounded tab or lug between two points
  is `add_extruded_slot(start, end, width, depth)`: the round ends are centred
  on the points, so nothing needs working out. `cut_offset_pocket(face, x, y,
  z, rim_mm, depth_mm)` pockets a whole face and leaves a rim along its
  outline, whatever its shape: the recessed web of an I-beam along a curved
  link, a tray, or a frame when cut through. Hand calculation: the face's area
  shrunk by the rim (a corner of radius R keeps radius R - rim).
- **Rounded outlines**: give `corner_radii_mm` to `add_extruded_profile`,
  `cut_profile` (with or without `face=` / `plane=`),
  `add_revolved_profile` (rounded edges of a turned part; not corners on the
  axis) or `add_swept_profile`: one radius for every corner, or one per vertex
  (0 = sharp). Hand calculation: a right-angled corner of radius r loses
  r^2 (1 - pi/4) of area, a concave one gains it; in a revolve that area sits
  0.2234 r from the corner along both edges (Pappus). A radius too big for its
  edges is refused before anything is sketched, with the edge that is too
  short; so are arcs that would meet, as SolidWorks merges them: a circle is
  `add_disc`, round ends are `add_extruded_slot`, and any other outline is
  `add_sketch` with tangent arcs. Profiles over 24 points cannot be
  rounded (they are fixed, not dimensioned). For a plain rounded block,
  `add_box` plus `add_fillet(edges="z")` works as well.
- **Flowing outlines** (an S-bend in a leg, a hub running into a beam):
  `add_sketch(plane, start_mm, segments)` draws a chain on any plane:
  `{"line": [u, v]}`, `{"arc": [u, v], "through": [u, v]}`, `{"arc": [u, v],
  "center": [u, v]}` (the short way round), `{"arc": [u, v], "tangent": true}`
  (flowing on from the segment before) and `{"spline": [[u, v], ...]}`; end on
  the start to close it. A centre on the origin or on another arc's centre is
  tied to it, also when rounded points put it a hair beside it. Points are [u, v] in the plane's
  axes, which the result gives: front (x, y), top (x, -z), right (-z, y).
  Where segments flow into each other the sketch gets tangent relations, and
  the rest is dimensioned from the origin (`x<i>`/`y<i>` for the i-th point,
  `r<k>` for the radius of a free arc), so a changed dimension keeps the
  flow. Then `extrude_sketch` or `cut_sketch` the returned sketch. A tangent
  arc turns towards its end point: from (30, 0) heading +x, an arc to (50, 20)
  is a quarter circle round (30, 20).
- **Side-view shapes** (wedges, windows, symmetric recesses):
  `cut_profile(plane=...)` on the Front/Top/Right plane or another plane by
  name, through all or a depth centred on the plane.
- **Planes at an angle**: `add_plane("front", angle_deg=30, about="y")` turns a
  default plane about a model axis it holds (right-hand rule: positive turns
  counterclockwise seen from the axis's positive end); `add_plane("Plane1", offset_mm=5)`
  moves any plane. Both return `origin_mm`, `x_axis` and `y_axis`: the point
  (u, v) on the plane is origin + u x_axis + v y_axis, which is what
  `add_extruded_profile` and `cut_profile` take with `plane=`. The
  angle or offset is a dimension, and what is built on the plane follows it.
- **Parts at an angle on the Front plane**: draw the profile straight and give
  `rotate_deg` and `about_mm` (the pivot) to `add_extruded_profile` or
  `cut_profile`; the turned points are dimensioned like any profile.
- **Tapered walls**: `draft_deg` on `add_extruded_profile` (also with
  `plane=`) leans every wall inwards as it rises
  (negative: outwards); the angle is a dimension, `draft`. Hand calculation
  for a w x h rectangle extruded d with t = tan(draft): w h d - (w + h) t d^2
  + 4/3 t^2 d^3.
- **Organic shapes**: `add_loft(profiles_mm=..., heights_mm=...)` takes round sections,
  `{"center_mm": [x, y], "diameter_mm": d}`: a leg thick at the knee and thin
  at the foot, the centres free to wander. Two sections hold
  pi h (R^2 + Rr + r^2) / 3 wherever the centres lie (a steep slant comes out
  up to 1% fuller). Every profile starts at its first vertex (a round one on
  +x), so give polygons in the same vertex order. `add_swept_pipe(smooth=True)`
  runs a pipe along a spline through the points: pi r^2 x `path_length_mm`.
- **Several bodies**: `merge=False` keeps a new extrusion a body of its own;
  `list_bodies` names them, with volume and box. `combine_bodies("subtract",
  main, [tool])` cuts one out of another, `add` joins them and `common` keeps
  their overlap; `split_body(plane)` splits the part along a plane, such as
  one from `add_plane`. Faces, edges, fillets and the printability check see
  the first body only: combine the bodies before you work on their faces.
- **A shape from two views**: extrude the front view, then cut the side view
  with `cut_profile(..., plane=..., keep_inside=True)`: everything outside
  the side outline goes, and what is left matches both views.
- **Fillets and chamfers**: edges by axis (`x`/`y`/`z`), by index, `all`, or a
  face's outline (`+z:outline`: the outer edges of the top face, not the rims of
  holes in it), or every edge of one feature (`feature:Boss`: a boss's top and
  the edge where it meets the part, for a moulded look). On a big part,
  `list_edges(face="#5")`, `feature=`, `within_mm=` or `min_length_mm=` narrow
  the list; the indices stay those of the whole part. A refused fillet names
  the edges the radius does not fit, the largest round one that fails alone
  takes, and the ones that round together;
  `skip_shorter_mm` leaves out slivers. A chamfer is 45 degrees unless
  `angle_deg` says otherwise, measured from the face `from_face` names:
  `add_chamfer(1, "-z:outline", angle_deg=60, from_face="-z")` leaves the
  underside at 60 degrees, printable without support (1 mm in, tan 60 up).
  `radii_at_mm=[[x, y, z, r]]` lets the radius vary: r at the edge end at
  that point (`list_edges` gives the ends), `radius_mm` at the others, straight
  in between. Hand calculation for a right-angled edge of length L:
  (1 - pi/4) L (r1^2 + r1 r2 + r2^2) / 3 comes off.
  `add_full_round(face, x, y, z)` rounds a rib's top off completely: the point
  lies on the top, the round runs across the rib between its closest opposite
  sides, and a rib w wide and L long loses L w^2 (1/2 - pi/8).
  Round vertical edges **before** cutting slots: a slot adds tangent edges that
  an axis selector would catch too.
- **Ribs / gussets**: `add_rib` draws a straight rib in a plane parallel to the
  Front plane at `z_mm`; give `toward_mm` as a point on the side to fill (for an
  L-bracket: its inner corner).
- **Threads** (real, printable): `add_thread` with an ISO size as SolidWorks
  names it (`M10x1.5`, `M3x0.5`, `M10x1.0`). External: the rod must have the
  nominal diameter. Internal: drill the ISO basic minor diameter first,
  D - 1.0825 P (M10x1.5 -> 8.376 mm); the error tells you the value.
- **Patterns**: `add_linear_pattern`, `add_circular_pattern` (the axis is the
  cylindrical face nearest the centre you give, or `axis='y'` for a revolved
  part, which turns about y, or any axis made with `add_axis`).
- **Gears and other lofted cuts**: draw each section with `add_sketch` on its
  own plane (`add_plane(normal=..., through=...)` makes one square to a cone's
  generatrix; `fixed=True` keeps a computed involute as drawn), then
  `add_loft([heel, toe], cut=True)` and `add_circular_pattern(z, axis='y')`.
  Draw the sections in the same order and sense, or the loft twists.
- **Something no tool does**: `run_script(code)` (when the server allows it)
  runs Python with `sw`, `model`, `mod`, `wrap` and `session` at hand; the part
  is rebuilt and reported afterwards and rolled back if the script raises.
  Script it once, then ask for a tool.
- **A whole part in one call**: write the part as a script file that calls the
  tools (`session.new_part()`, `session.add_box(...)`, ...) and run
  `run_script(path='part.py')`. Instead of one call per feature you get one
  report: `features_added`, `failing_features`, `bodies` (volume, box),
  `clashes` between bodies, `under_defined_sketches`, mass properties, and
  `image`, a PNG of the iso, right, top and bottom views next to the script.
  Fix the file and run it again; helpers in its folder reload every run. Check
  the report against your hand calculation as you would after each tool.
- **Mirror**: `add_mirror(plane, offset_mm, features)` mirrors features about
  the Front/Top/Right plane moved `offset_mm` (z / y / x = offset). Put the
  plane where the copies land in material: a box spans x 0..w, so mirror about
  `offset_mm = w / 2`; a copy outside the part fails. The copies follow their
  seeds. To keep the plane in the middle when the part grows, tie it to the
  width: `set_equation('"<plane_offset>" = "<width>" / 2')` with the names the
  tools returned. Without `features` the whole body is mirrored and merged:
  model half of a symmetric part and mirror it about the face where the
  halves meet. `plane` may also name another plane of the part (`"Plane1"`),
  moved `offset_mm` along its normal.
- **Text**: `add_text_on_face(text, face, x, y, z, height_mm, depth_mm)`
  engraves a label into any planar face; `emboss=True` raises it instead. The
  point is the text's lower-left corner, and the text runs along the face
  sketch's horizontal axis. It reads upright on the top (+z) and front (-y)
  faces, along +x; SolidWorks turns it on the others (vertical on +x and -x,
  upside down on +y), so put labels on the top or front. Letters have no hand calculation:
  the result's `text_area_mm2` is what the next steps change by (area x depth).
  `font` must be an installed font; an unknown one is refused, because Windows
  would quietly draw another.
- **A supplier's model**: `open_document` imports a STEP (`.step`/`.stp`), IGES or
  Parasolid (`.x_t`/`.x_b`) file as a new part. The imported body has no
  history, but the tools work on it: holes, pockets and bosses on its faces,
  fillets. The result has `solid_bodies` and mass properties: read the bounding
  box to see where the model lies before you place anything. With several
  bodies the tools act on the first. A file holding an assembly opens as an
  assembly (`kind="assembly"` skips trying it as a part): its parts come in as components at their places,
  ready for `list_components` and `add_mate`.
- **Sheet metal**: `add_base_flange(thickness_mm, bend_radius_mm, profile_mm)`
  starts the part. A closed profile on the Front plane is a flat tab, the
  sheet's thickness along +z; an open one (an L, U, Z or hat section) is bent
  at its corners with the inner bend radius and extruded `depth_mm` along +z,
  its thickness on the right of the profile walked from the start
  (`flip_thickness=True`: the left). `add_edge_flange(at_mm, length_mm)` bends
  a flange up from the straight edge through that point, the edge's whole
  length; it rises the way the face at the edge looks (`flip=True`: the other
  way), and `length_mm` is the outside length, from the outer corner of the
  bend. With `position="material_inside"` (default) the part keeps its outer
  size. Holes and cuts work as on any part. Hand calculation: a bend of
  inner radius r in a sheet t thick keeps its neutral fibre, at r + K t:
  pi/2 (r + K t) for a square bend, and each straight leg ends r + t short of
  the outer corner. `flat_pattern("info")` gives the flat blank's size and
  the bends; `"flatten"` / `"fold"` show it; `"export"` writes the DXF for
  laser cutting (save the part first). `make_drawing` shows a sheet metal
  part's flat pattern in place of the iso view, with bend lines, bend notes
  and the blank's size; `add_view(kind="flat_pattern")` adds one to any drawing.
  `add_closed_corner(at_mm)` closes the open corner between two flanges: at_mm
  on the end face of the flange that should grow (list_faces: after two edge
  flanges on neighbouring edges one stops r + t short of the corner); with
  `kind="overlap"` it runs to the outer corner, the other flange stopping
  t + gap short; `"underlap"` the other way round.
- **Welds**: `add_weld_bead(from_mm, to_mm, size_mm)` lays a fillet weld where
  two faces meet, a point on each; the faces belong to two bodies (a plate
  added with `merge=False`, structural members): one body cannot be welded to
  itself, and a sheet metal part is one body. `side="both"` welds both sides
  of the from face, `"all_around"` all round it; `length_mm` + `pitch_mm`
  make it intermittent. Hand check: `weld_length_mm` is the length of the
  line where the faces meet (a 5 x 40 plate standing on a base: 40 one side,
  80 both, 90 all round). A bead adds no mass.
- **Weldments**: `add_structural_member(profile, size, path_mm)` runs members
  of a library profile along a chain of lines on the Front plane (or `plane`,
  or a `sketch`); ISO profiles: square tube 20 x 20 x 2, 30 x 30 x 2.6,
  40 x 40 x 4, 80 x 80 x 5; rectangular tube 50 x 30 x 2.6, 60 x 40 x 3.2, ...;
  pipe 21.3 x 2.3, 26.9 x 3.2, 33.7 x 4.0; angle iron, c channel, sb beam (a
  wrong name lists what the library holds). The profile is centred on the
  lines, so a frame drawn on its centre lines; with `corner="miter"` a member
  is as long along its centre line as its line, and the cut list's length is
  the outside one (+ the tube's width for two mitres). Members of one call
  are one group; a member standing on another goes in its own call, its line
  starting on the other's face. `add_end_cap(at_mm, thickness_mm)` closes an
  open end (a point on the tube wall's end; set in half the wall unless
  `inset_mm`), `add_gusset(first_mm, second_mm, leg1_mm, leg2_mm,
  thickness_mm)` fills the corner of two faces at right angles, leg1 along
  the first; `cut_list` gives the members and the weld beads.
- **Weld symbols**: `add_symbol("weld", view, ref, weld={"weld": "fillet",
  "size": "a4", "length": "100", "side": "both", "all_around": True})` (ISO
  2553: a = throat, z = leg; '3x50 (100)' for an intermittent weld).
  `make_drawing` brings in the weld beads' own symbols and a weldment's cut
  list table.
- **Drawings**: save the part, then `make_drawing("part.pdf", sheet="A3")`
  lays out front, top and side views (first angle) and an iso view on an ISO
  sheet with a title block, at the largest standard scale with room for
  dimensions. It brings in the model's dimensions once each, one callout per
  set of identical Hole Wizard holes ('4X ...', linked to the model), their
  centres, and material and mass in the title block. Read its `problems`
  (the same list `check_drawing` gives) before sending it out.
  - To refine it, `make_drawing(..., keep_open=True)` and use the drawing
    tools; they work on the current drawing, apart from the current part.
    Everything is in sheet mm from the bottom-left corner; `list_views` gives
    each view's geometry box and how model points map onto the sheet.
  - Dimension by reference, not by clicking: `add_dimension("Drawing View1",
    "edge:-x", "hole:CBore1")` measures from the part's left face to the hole
    centre, on the first free row off the view (10, 18, 26 mm ...). A face
    that faces the viewer is refused: use a view that sees it edge-on.
    `add_dimension(view, "edge:-x", ["hole:H1", "hole:H2"], style="baseline"
    | "chain" | "ordinate")` does a set at once.
  - `add_view("section", parent="Drawing View1", through=[x, y, z],
    normal="x")` cuts through a model point; `add_view("detail", parent=...,
    center=[x, y, z], radius_mm=5)`.
    A section or detail view keeps the name of its label; the `name` you
    give works as an alias in this session.
  - A model dimension's prefix, tolerance and precision live in the part:
    `edit_annotation` on one changes the part and every drawing of it (the
    result says `changes_part`). Add a drawing dimension to tolerance only
    the drawing.
  - Symbols attach to an edge mid-way: `add_symbol("datum", view, "edge:-z",
    label="A")`, `add_symbol("gtol", view, "edge:+z", symbol="parallelism",
    tolerance="0.05", datums=["A"])`, `add_symbol("gtol", view, "hole:H1",
    symbol="position", tolerance="0.1", datums=["A", "B"], diameter=True,
    material_condition="MMC")`, `add_symbol("surface_finish", view,
    "edge:+z", roughness="Ra 1.6")`, `add_symbol("hole_callout", view,
    "hole:H1")`.
  - Gears: `add_table(gear={"kind": "spur" | "bevel", "teeth": 20,
    "module": 2, "mating_teeth": 40})` computes the table (span over k
    teeth, cone angles, chordal thickness); put Teeth / Module / MatingTeeth
    on the part as custom properties and it reads them. `add_table(rows)`
    takes any other table.
  - Views in a tangle after adding a few: `auto_layout()` lays them all out
    again at the largest scale that fits.
  - Look with `render_drawing(path, zoom_mm=[[x1, y1], [x2, y2]])`; fix
    what `check_drawing` lists. `build_drawing(spec)` rebuilds a drawing
    from a spec, so fine-tuning is editing the spec, not the sheet.
- **Open documents**: `list_documents` lists what SolidWorks has open;
  `activate_document(title)` makes one current again, also a new part that was
  never saved (after `open_document` and `close_document` on another).
  `close_document(title)` closes any one of them, part, assembly or drawing,
  without making it current first; `save=True` saves it over its file first.
  Parts an assembly loaded close with that assembly.
- **Assemblies**: `insert_component` puts a part's origin at a point; a
  `.sldasm` goes in whole as a sub-assembly (save an imported servo first).
  `delete_component` takes one out again, with its mates, and
  `replace_component(component, path)` swaps it for another part in the same
  place: the mates that broke come back with the face each held on the old
  part and candidate faces (`#index`) on the new one, to re-mate.
  On a big assembly, `list_components(name="Bolt*")` keeps the components
  that match and the mates that hold them; `brief=True` and `mates=False`
  shorten it further. Several mates go in with one `add_mate(mates=[...])` call
  and out with one `delete_mate([...])`.
  `list_faces(component=...)` then lists every part's faces in the
  sub-assembly's own frame, naming the part, and `add_mate` takes them;
  transforms are read back, mates are measured back after the rebuild, and
  `check_interference` reports overlapping pairs with their volume.
  Component boxes are the parts' own extent, also when turned.
  `measure_distance` gives the clearance between two components (0 when
  they touch or overlap), or from a component to a point such as a pivot,
  with the nearest point; `inside: true` means the point lies in the material.
  `axis_mm=[[x, y, z], [dx, dy, dz]]` measures to an endless line instead,
  such as the axis of a bolt that is not modelled.
- **Colours**: `set_appearance(rgb=[30, 30, 30], component="Frame-1")` in an
  assembly (the part keeps its own colour), `set_appearance(transparency=0.6,
  component=...)` to see what lies behind a cover, `transparency=0` to make
  it solid again; without component, the current part itself.
- **Joints**: a hinge is a `concentric` mate between the two holes (pick a
  cylinder by its `list_faces(component=...)` index, `"#5"`), a `coincident`
  mate between the faces that slide on each other, and an `angle` mate between
  two side faces. The angle mate returns its `dimension`: `set_dimension`
  turns the joint, and `check_motion(dimension, [30, 60, 90, 120, 150],
  distances=[["Rod", "Bolt"]])` checks overlaps and clearances at every step
  and puts the joint back. The mate is made at 0..180 degrees, but its
  dimension turns the joint the whole way round: a hip of -35..115 steps as
  `[-35, 0, 60, 115]` from any pair of side faces (a negative angle is the
  turn the other way from 0). `flip` turns an angle mate the other way. A mate
  that contradicts the others is refused and removed, naming the one it
  fights; `list_components` lists the mates and their errors, in words.
  `check_motion` fails when nothing moves: then the mate lost a face, or is
  suppressed. `delete_mate` and `suppress_mate` take the name add_mate returns.
  What a moving part sweeps over, to keep another part clear of it:
  `swept_region(part, dimension, values, heights_mm, margin_mm=..., frame=
  "Cover-1")` outlines it in a plane, in the cover's coordinates, as points for
  an `add_sketch` spline to cut with.
- **A person's part**: `list_features` names their sketches and planes.
  `read_sketch` reads a sketch back in model coordinates, so you can check it
  against the points that must fit before building on it. `extrude_sketch` and
  `cut_sketch` build on it by name, and it keeps its own dimensions and
  relations. A cut goes against the sketch's normal: from a face that is into
  the part; from a plane with the part in front of it (a box standing on the
  Front plane) it needs `reverse=True`. A post or rib that must end in a
  curved wall: `extrude_sketch(up_to="next")` ends on the wall's shape and
  follows it, where no single depth fits; `up_to="@x,y,z"` ends on the face
  through that point instead, also where "next" fails because a side of the
  profile lies on a face. Change the person's dimensions with
  `set_dimension` (names from `read_sketch` or `list_dimensions`). Their
  planes work by name in `add_mirror`, `add_extruded_profile` and `cut_profile` (`plane=`).
- **Reviewing a part**: `read_model()` reads it in one call, without changing
  it: mass, bodies, features, dimensions with their tolerances, equations,
  material, holes, internal radii and sharp internal edges, and the setup
  directions. Add `wall_mm=1.0` for the walls thinner than that and
  `pull="+z"` for draft and undercuts, or ask for just the sections you need
  (`sections=["holes"]`). Holes come grouped: `count` identical holes at
  `centres_mm` (at the entrance), drilled along `axis` from `entrance`.
  `source: "wizard"` holes carry the Hole Wizard's standard sizes;
  `"geometry"` ones are measured, with `confidence` lower where the shape is
  odd (another hole breaking into the side). `sharp_internal_edges` with
  `direction: "z"` are inside corners along z that a milling cutter cannot
  reach square; floor edges can be milled. Anything left out or cut short is
  named in `gaps`. Walls take seconds on a large part (one ray per sample
  point); the rest is about a second for 500 faces.

## 5. 3D printing

- Leave a gap of about 0.2-0.4 mm between printed parts that slide together,
  more for ASA/ABS than for PLA. Print a small fit test of a critical interface
  before the whole part.
- Printed holes come out 0.1-0.2 mm small. A hole that a part is pushed through
  must pass the part's widest section, e.g. the diagonal of a flat blade (a
  6.35 x 0.81 mm spade tab needs more than 6.4 mm), not just its width.
- Heat-set inserts: follow the insert maker's hole size. For common M3 x 5.7 mm
  inserts that is about 4.0 mm, at least 0.5 mm deeper than the insert: the
  displaced plastic needs room, or it bulges at the mouth and the mating part no
  longer seats flat.
- Text prints best large and bold: as a rule of thumb at least 5 mm high, with
  strokes wider than the nozzle (0.4 mm), engraved 0.4-0.6 mm or embossed about
  1 mm. On a face that prints facing down, engrave rather than emboss.
- Before printing, `check_printability(up="+z", overhang_deg=45, min_wall_mm=0.8)`
  names the faces that need support for that build direction (worst lean,
  area, centre) and the walls thinner than the minimum (two nozzle widths is
  a common floor), with where they are thinnest. Try another `up` to find the
  orientation with the least overhang; the bed contact area tells how well it
  sticks.
- Export for slicing with `export(..., quality="fine")` as 3MF or STL. Both
  keep the model's own coordinates (`frame` in the result), so a mesh
  measures back where the part is; the slicer places it on the bed.

## 6. Reverse-engineering from a mesh (STL/3MF)

When a part must mate with something that only exists as a mesh, for example
a proven battery socket:

1. Slice the mesh with `slice_mesh` across the axis the parts slide along, at
   small steps, and note where the cross-section changes: those heights are
   your feature boundaries. For a slicer's 3MF, `frame='object'` gives the
   designer's modelling frame, `frame='build'` the print orientation.
2. Use the sections as polygon profiles; `slice_mesh` already drops only
   exactly collinear points. **Do not simplify them further**: mesh walls are
   rarely perfectly straight, so two sections of the same wall differ by a few
   micrometres. Profiles over 24 points are fixed rather than dimensioned.
3. **Build every section from one master**: take the master's points wherever a
   section shares its walls and keep the section's own points only where it
   really differs. Successive cuts along nearly (not exactly) coincident walls
   leave sliver faces, and the next cut then fails with `FeatureCut4 failed`.
4. Start each cut from the face it belongs to (give a point on it). Cut
   undercuts, such as latch recesses, from the floor of the narrower region
   above them.
5. Compare with `compare_with_mesh` at the same heights (`offset_mm` moves the
   mesh into your part's frame). Equal area with different extents means a
   shifted frame; a very different area means a misread feature, for example
   a solid wall where the reference is hollow; an unmatched loop is a pocket
   or hole only one of them has.

## 7. Limits and housekeeping

- Not available: importing meshes as bodies (slice them instead), sketches on
  arbitrary planes (a person's planes work in `add_mirror` and
  `plane=` of the profile tools), free arcs inside polygon profiles (round corners with
  `corner_radii_mm`; other curves need revolves, splines, slots or holes).
- SolidWorks' memory grows over long sessions. If it warns about low memory,
  save your work and restart SolidWorks. Never restart it during a run: the COM
  connection breaks and every later call fails.
- Close documents you no longer need; saving over a file that is still open in
  SolidWorks fails. With no title and no current document, `close_document` closes SolidWorks'
  active one if it is saved; `get_status` shows which one that is.
- Tools failing in ways this guide does not explain? Ask the user to run
  `uvx sw-mcp --selftest` with SolidWorks open: it shows which tool
  areas work on their installation and ends with where to report it.

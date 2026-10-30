"""MCP server exposing SolidWorks part-modelling tools over stdio.

All tools delegate to a single SolidWorksSession that runs on one dedicated COM
thread (ComWorker). Failures are returned as {"ok": false, "error": "..."} so
the agent can read and react to them in the build -> measure -> correct loop,
rather than getting an opaque stack trace.
"""

import argparse
import os
import sys
from importlib import resources

from mcp.server.fastmcp import FastMCP

from .errors import SolidWorksError
from .selftest import main as run_selftest

# Shown to the model by every MCP client at connect time, so keep it short; the
# long version is the solidworks://guide resource.
INSTRUCTIONS = """\
Builds, measures and verifies parametric parts and assemblies in the user's running SolidWorks.

Conventions
- Millimetres and degrees. New geometry starts on the Front plane (XY) and extrudes along +Z from z = 0. add_box spans x 0..w, y 0..h; add_disc is centred on the origin; revolves turn about Y.
- Faces are picked by direction: '+z', '-x', ... *_on_face tools take 3D points on the face and use the face facing that way through them (the top, a pocket floor, a step); '+z:outer' / '+z:inner' force the outermost / innermost one. Other tools use the outermost face.
- Every sketch is fully defined. Tools return `dimensions` ({role: 'width@Sketch1', ...}): change one with set_dimension, or drive several from a global variable via set_equation.
- Other orientations: add_plane (an offset, or turned about x/y/z) and build on it with add_extruded_profile or cut_profile, plane='Plane1'.
- In a part a person drew, their sketches and planes work by name: read_sketch, extrude_sketch, cut_sketch; plane='Plane1' in add_mirror, add_extruded_profile and cut_profile.
- Flowing outlines (an S-bend, a hub running into a beam): add_sketch with lines, tangent arcs and splines, then extrude_sketch / cut_sketch.
- A joint: concentric mate (cylinders by list_faces index, '#5') + coincident + angle mate; the angle mate's dimension turns it (set_dimension, check_motion).
- Sheet metal: add_base_flange (a tab, or a bent section from an open profile), add_edge_flange on its edges, holes and cuts as usual; flat_pattern for the flat size and the DXF; make_drawing adds the flat pattern.
- Welds: add_weld_bead between faces of two bodies; weldment frames with add_structural_member (library profiles), add_end_cap, add_gusset, cut_list; add_closed_corner on sheet metal; add_symbol(type='weld') on drawings.

Work in small verified steps
- Every modelling call returns volume, mass and bounding box: check them against your own hand calculation after each step, and fix the first mismatch before adding more.
- Look before selecting by index: list_faces / list_edges; screenshot to see the shape.
- Undo a wrong step with delete_feature; list_features shows the history, also of a part opened from disk.
- {ok: false, error} means the tool refused or SolidWorks failed; the error names the cause, and the part is left as it was.

Pitfalls
- Walls shared by successive polygon cuts must use identical points: sections a few micrometres apart leave sliver faces and the next cut fails.
- Round polygon corners with corner_radii_mm (real sketch fillets; equal radii share one dimension). Other curves: revolves, splines, slots and holes.
- add_mirror needs its plane where the copies land in material: add_box spans x 0..w, so mirror about offset_mm = w / 2, not x = 0.
- SolidWorks' memory grows in long sessions: on a low-memory warning, save and restart SolidWorks (never during a run).

Read the resource solidworks://guide for the full guide: recipes (holes, ribs, threads, patterns, assemblies), 3D printing, and reverse-engineering a part from a mesh.
"""

mcp = FastMCP("sw-mcp", instructions=INSTRUCTIONS)

# run_script executes any Python it is given: on unless the person starting
# the server turns it off
_ALLOW_SCRIPTS = os.environ.get("SOLIDWORKS_MCP_ALLOW_SCRIPTS", "").strip().lower() not in ("0", "false", "no")


@mcp.resource("solidworks://guide", name="guide", mime_type="text/markdown",
              description="Modelling guide: conventions, verification, recipes, 3D printing, "
                          "reverse-engineering from a mesh, limits.")
def guide() -> str:
    return resources.files("sw_mcp").joinpath("guide.md").read_text(encoding="utf-8")


class _NoSolidWorks:
    """Stand-in session off Windows: every method fails with a readable error.

    The server still starts and lists its tools there, because MCP directories
    introspect servers in a Linux container before listing them.
    """

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise SolidWorksError(
                f"SolidWorks MCP only works on Windows with SolidWorks installed (this is {sys.platform})."
            )
        return unavailable


if sys.platform == "win32":
    import pythoncom

    from .com_worker import ComWorker
    from .session import SolidWorksSession

    _worker = ComWorker()
    _session = SolidWorksSession()
    _COM_ERROR = pythoncom.com_error
else:
    _worker = None
    _session = _NoSolidWorks()
    _COM_ERROR = ()  # an empty tuple matches no exception


async def _call(fn, *args, **kwargs) -> dict:
    """Run a session method on the COM thread and normalise errors to a result dict.

    It runs guarded: a call that fails leaves the current part as it was.
    """
    try:
        if _worker is None:
            return fn(*args, **kwargs)
        return await _worker.call(lambda: _session.run_guarded(fn, *args, **kwargs))
    except SolidWorksError as exc:
        return {"ok": False, "error": str(exc)}
    except _COM_ERROR as exc:
        # Extract the human-readable description if SolidWorks supplied one;
        # raw HRESULT tuples are useless as a correction-loop signal.
        info = getattr(exc, "excepinfo", None)
        desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
        return {"ok": False, "error": f"SolidWorks COM error: {desc or exc}"}
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the agent
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def get_status() -> dict:
    """Report whether SolidWorks is reachable, its revision, this server's version, and the active/current part."""
    return await _call(_session.get_status)


@mcp.tool()
async def new_document(kind: str = "part") -> dict:
    """Create a new empty part (kind='part') or assembly (kind='assembly'); it becomes the current document.

    Assemblies compose saved parts: insert_component places each part, add_mate
    constrains them, check_interference proves nothing overlaps. Drawings: drawing_file.
    """
    return await _call(_session.new_document, kind)


@mcp.tool()
async def add_box(width_mm: float, height_mm: float, depth_mm: float,
                  name: str = "BlockExtrude") -> dict:
    """Add a rectangular block: sketch width x height on the first plane, extrude by depth.

    Dimensions are in millimetres. Returns the created feature name, the
    addressable depth dimension ('D1@<name>'), `dimensions` (width, height,
    depth; for set_dimension) and the resulting mass properties.
    """
    return await _call(_session.add_box, width_mm, height_mm, depth_mm, name)


@mcp.tool()
async def add_extruded_profile(points_mm: list, depth_mm: float, face: str | None = None, plane: str | None = None,
                               reverse: bool = False, name: str | None = None,
                               corner_radii_mm: float | list | None = None, rotate_deg: float = 0.0,
                               about_mm: list | None = None, draft_deg: float = 0.0, merge: bool = True,
                               spline: bool = False) -> dict:
    """Extrude a closed polygon into a solid; it is auto-closed.

    - Default: points_mm = [[x,y], ...] on the first plane (same as add_box), extruded by depth_mm along +Z.
      rotate_deg turns the profile counterclockwise about about_mm ([x, y], the origin by default) first:
      draw a crank or a lug straight, then set it at its angle. spline=True runs a smooth closed spline
      through the points instead (organic shapes).
    - face='+z' (or '-x', ...): a boss grown out of ANY planar face; points_mm = [[x,y,z], ...] on that
      face (the face facing that way through them, as for add_hole). For pads, ledges, mounting blocks.
    - plane='Plane1' (add_plane, or 'front'/'top'/'right'): points_mm = 3D [x, y, z] points ON the plane
      (origin + u x_axis + v y_axis from add_plane); depth_mm along its normal, the other way with
      reverse=True.

    corner_radii_mm rounds the corners with real sketch fillets: one radius for all, or one per vertex
    (0 = sharp); equal radii share one dimension ('radius', else 'r<i>'). draft_deg tapers the walls
    inwards as they rise (negative: outwards), a dimension 'draft' (a moulded boss, a printable taper).
    merge=False keeps the result a separate body: list_bodies, combine_bodies. An argument that does not
    go with the variant is refused. Returns mass properties (volume = polygon area * depth; a
    right-angled corner of radius r loses r^2 (1 - pi/4), a concave one gains it).
    """
    return await _call(_session.extruded_profile, points_mm, depth_mm, face, plane, reverse, name,
                       corner_radii_mm, rotate_deg, about_mm, draft_deg, merge, spline)


@mcp.tool()
async def add_disc(diameter_mm: float, thickness_mm: float, name: str = "Disc",
                   x_mm: float = 0.0, y_mm: float = 0.0) -> dict:
    """Create a disc/puck/flange: a circle extruded along +Z, centred at (x_mm, y_mm).

    Flat faces are +Z/-Z, so add_hole and add_circular_pattern compose with it
    (round-flange bolt circles). The centre defaults to the origin; off it, its
    x and y are dimensions. Returns mass properties. Use new_document first.
    """
    return await _call(_session.add_disc, diameter_mm, thickness_mm, name, x_mm, y_mm)


@mcp.tool()
async def add_cylinder(diameter_mm: float, height_mm: float, name: str = "Revolve") -> dict:
    """Create a cylinder by revolving a profile 360° about an axis.

    The first revolve-based primitive. Returns the resulting mass properties
    (volume = π · r² · h). Use new_document first.
    """
    return await _call(_session.add_cylinder, diameter_mm, height_mm, name)


@mcp.tool()
async def add_cone(bottom_diameter_mm: float, top_diameter_mm: float,
                   height_mm: float, name: str = "Revolve") -> dict:
    """Create a cone/frustum by revolving a trapezoidal profile 360°.

    top_diameter_mm = 0 gives a full cone. Returns mass properties
    (volume = π·h/3 · (rb² + rb·rt + rt²)). Use new_document first.
    """
    return await _call(_session.add_cone, bottom_diameter_mm, top_diameter_mm, height_mm, name)


@mcp.tool()
async def add_revolved_profile(profile_mm: list, angle_deg: float = 360.0,
                               name: str = "Revolve", corner_radii_mm: float | list | None = None,
                               axis_mm: list | None = None) -> dict:
    """Revolve a closed profile on the Front plane about an axis in it.

    Without axis_mm: profile_mm = [[r, z], …] in mm, r = distance from the Y axis,
    z = position along it. With axis_mm = [[x1, y1], [x2, y2]]: profile_mm gives
    [x, y] points and the axis is the line through those two points, anywhere and
    at any angle (a hub turned about its own centre). Auto-closed and spun
    angle_deg (default 360°). Points on the axis give a solid (shafts, vases); a
    profile away from it gives a ring. The profile may not cross the axis.
    corner_radii_mm rounds corners as for add_extruded_profile (not those on the
    axis). Returns mass properties. Use new_document first.
    """
    return await _call(_session.add_revolved_profile, profile_mm, angle_deg, name, corner_radii_mm, axis_mm)


@mcp.tool()
async def add_swept_pipe(path_mm: list, diameter_mm: float,
                         bend_radius_mm: float = 0.0, name: str = "Pipe", smooth: bool = False) -> dict:
    """Sweep a circular profile (pipe/tube/rod) along a 2D path on the Front plane.

    path_mm = [[x, y], …] in mm is the centreline. Interior corners are rounded
    with bend_radius_mm (required when the path turns; a 2-point straight path
    needs none); smooth=True runs a spline through the points instead, a
    flowing curve. diameter_mm = outer Ø; the round profile is auto-generated
    perpendicular to the path. Returns mass properties (pi r^2 x
    path_length_mm). Use new_document first.
    """
    return await _call(_session.add_swept_pipe, path_mm, diameter_mm, bend_radius_mm, name, smooth)


@mcp.tool()
async def add_swept_profile(profile_mm: list, path_mm: list,
                            bend_radius_mm: float = 0.0, name: str = "Sweep",
                            corner_radii_mm: float | list | None = None) -> dict:
    """Sweep an arbitrary closed PROFILE (cross-section) along a 2D PATH.

    profile_mm = [[u,v],…] in mm: the closed cross-section on the Right plane (u →
    world -Z, v → world +Y), centred near the origin. path_mm = [[x,y],…] in mm on
    the Front plane — MUST start at the origin heading +X (the profile is
    perpendicular to the path there). Path corners are rounded with bend_radius_mm;
    corner_radii_mm rounds the profile's corners as for add_extruded_profile.
    Volume = profile_area · path_length. For non-round extrusions along a path
    (rails, gaskets, trim, channels). Returns mass properties. Use new_document first.
    """
    return await _call(_session.add_swept_profile, profile_mm, path_mm, bend_radius_mm, name, corner_radii_mm)


@mcp.tool()
async def add_loft(sketches: list | None = None, cut: bool = False, profiles_mm: list | None = None,
                   heights_mm: list | None = None, name: str | None = None) -> dict:
    """Loft (blend) 2+ closed profiles, given one of two ways.

    - sketches=['Heel', 'Toe']: closed sketches by name (add_sketch on any planes), in order; cut=True
      cuts the lofted shape away (a straight bevel gear's tooth gap is a loft cut between heel and toe
      profiles).
    - profiles_mm + heights_mm: per profile a polygon [[x,y], ...] (mm) in Front-plane coords, or a round
      section {"center_mm": [x, y], "diameter_mm": d}, on parallel planes at heights_mm along +Z (same
      length, strictly increasing, starting at 0). A 2-profile loft is a ruled transition; 3+ blend
      smoothly. Each profile starts at its first vertex (a round one on +x).

    Give profiles in the same order and sense so they do not twist. Returns mass properties.
    """
    return await _call(_session.loft, sketches, cut, profiles_mm, heights_mm, name)


@mcp.tool()
async def cut_profile(points_mm: list, depth_mm: float | None = None, face: str | None = None,
                      plane: str | None = None, name: str = "Cut", corner_radii_mm: float | list | None = None,
                      rotate_deg: float = 0.0, about_mm: list | None = None, keep_inside: bool = False) -> dict:
    """Cut a polygonal pocket, slot or window; the polygon is auto-closed.

    - Default: points_mm = [[x,y], ...] cut down from the +Z face. rotate_deg turns the profile about
      about_mm as for add_extruded_profile (notches at +/-60 degrees around a pivot).
    - face='+x' (or ...): points_mm = [[x,y,z], ...] on one face facing that way, found through them;
      for side pockets and cutouts.
    - plane='right' ('front' z = 0, 'top' y = 0, 'right' x = 0, or 'Plane1'): points_mm = 3D points ON
      that plane, cut symmetric about it. For shapes seen from the side: wedges, windows, symmetric
      recesses. keep_inside=True cuts away everything OUTSIDE the profile (through all): extrude the
      front view, cut the side view with keep_inside, and the part is the 3D shape both views agree on.

    Blind by depth_mm (with plane=: in total, centred on the plane), or through all when omitted.
    corner_radii_mm rounds the corners as for add_extruded_profile. Returns mass properties.
    """
    return await _call(_session.profile_cut, points_mm, depth_mm, face, plane, name, corner_radii_mm,
                       rotate_deg, about_mm, keep_inside)


@mcp.tool()
async def add_plane(base: str = "front", offset_mm: float = 0.0, angle_deg: float = 0.0,
                    about: str | None = None, name: str | None = None, normal: list | None = None,
                    through: list | None = None) -> dict:
    """Make a reference plane to sketch on: an offset or a turned plane, its position a dimension.

    base moved offset_mm along its normal (base: front/top/right or a plane by
    name), or a default plane turned angle_deg about a model axis it holds,
    about='x'/'y'/'z' (front holds x and y, top x and z, right y and z), by the
    right-hand rule. For both, turn first, then offset that plane. Or the plane
    through model point through=[x, y, z] square to normal=[dx, dy, dz] (any
    direction, e.g. square to a cone's generatrix). Returns its name for
    plane= arguments, and origin_mm, normal, x_axis and y_axis: a point (u, v)
    on it is origin + u x_axis + v y_axis.
    """
    return await _call(_session.add_plane, base, offset_mm, angle_deg, about, name, normal, through)


@mcp.tool()
async def list_bodies() -> dict:
    """List the part's solid bodies: name, volume and bounding box.

    A part holds several after merge=False or split_body; combine_bodies joins
    them again.
    """
    return await _call(_session.list_bodies)


@mcp.tool()
async def combine_bodies(operation: str, main: str, tools: list | None = None, name: str = "Combine") -> dict:
    """Combine solid bodies of the part into `main` (a list_bodies name).

    operation 'add' joins the tools to main, 'subtract' cuts them out of it,
    'common' keeps only what main shares with them. tools: body names, every
    other body by default. Returns mass properties and the bodies left.
    """
    return await _call(_session.combine_bodies, operation, main, tools, name)


@mcp.tool()
async def split_body(plane: str, name: str = "Split") -> dict:
    """Split the part's body in two along a plane (front/top/right or a plane by name).

    The two bodies come back by name: combine them differently, or check each
    with list_bodies. Use add_plane first for a cut anywhere else.
    """
    return await _call(_session.split_body, plane, name)


@mcp.tool()
async def add_thread(size: str, x_mm: float, y_mm: float, z_mm: float, length_mm: float,
                     internal: bool = False, name: str = "Thread") -> dict:
    """Cut a real, printable ISO metric thread (SolidWorks' own Thread feature).

    size: e.g. 'M10x1.5', 'M3x0.5', 'M10x1.0' -- checked against SolidWorks'
    thread profiles. (x_mm, y_mm, z_mm) = centre of the circular edge where the
    thread starts: the end face of a rod, or the mouth of a hole (internal=True).
    It runs length_mm into the material, right-handed. External: the rod must
    have the nominal diameter (M10 -> Ø10). Internal: drill the ISO basic minor
    diameter first, D - 1.0825*P (M10x1.5 -> Ø8.376). Returns mass properties
    and the thread's size, pitch and diameters.
    """
    return await _call(_session.add_thread, size, x_mm, y_mm, z_mm, length_mm, internal, name)


@mcp.tool()
async def add_rib(start_mm: list, end_mm: list, toward_mm: list, thickness_mm: float,
                  z_mm: float, name: str = "Rib") -> dict:
    """Add a straight stiffening rib / gusset in a plane parallel to the Front plane.

    start_mm/end_mm = [x, y] ends of the rib's free edge (add_extruded_profile
    coordinates); the plane sits at height z_mm. The rib grows toward toward_mm
    (any [x, y] point on the side to fill, e.g. the inner corner of an L-bracket)
    until it meets the part, thickness_mm thick, centred on the plane. Returns mass
    properties: a triangular gusset with legs a and b adds a*b/2 * thickness.
    """
    return await _call(_session.add_rib, start_mm, end_mm, toward_mm, thickness_mm, z_mm, name)


@mcp.tool()
async def add_hole(x_mm: float, y_mm: float, z_mm: float | None = None, diameter_mm: float | None = None,
                   face: str | None = None, depth_mm: float | None = None, cbore_diameter_mm: float | None = None,
                   cbore_depth_mm: float | None = None, iso: str | None = None, size: str | None = None,
                   fit: str = "normal", thread: str = "cosmetic", name: str | None = None) -> dict:
    """Cut a round hole. Which one follows from the arguments:

    - diameter_mm at (x, y): straight through the thickness (the add_box extrude direction); coordinates
      share add_box's system (the centre of a 40x20 profile is x=20, y=10).
    - + face='+z' (or '-x', ...) and z_mm: on ANY planar face, centred at 3D point (x, y, z), the face
      facing that way through the point (a pocket floor or a step works too). Through all, or depth_mm
      deep (blind: heat-set inserts, screw pilots). Returns `dimensions` (diameter, x, y, depth).
    - + cbore_diameter_mm and cbore_depth_mm: a counterbored screw hole from the +Z face, a flat-bottom
      pocket over the clearance shank so a cap-head screw or heat-set insert sits flush.
    - iso='clearance' (ISO 273), 'counterbore' (socket head cap screw), 'countersink' (socket
      countersunk screw) or 'tapped', with size='M6', face and z_mm: SolidWorks' Hole Wizard, sized by
      the standard's tables and carried into drawing callouts. fit 'close' / 'normal' / 'loose';
      thread='modeled' cuts a real, printable ISO thread in a tapped hole instead of the cosmetic one.
      Returns the standard's sizes in `hole`.

    Prefer iso= for standard fasteners. Returns mass properties.
    """
    return await _call(_session.hole, x_mm, y_mm, z_mm, diameter_mm, face, depth_mm, cbore_diameter_mm,
                       cbore_depth_mm, iso, size, fit, thread, name)


@mcp.tool()
async def add_boss_on_face(diameter_mm: float, face: str, x_mm: float, y_mm: float, z_mm: float,
                           height_mm: float, name: str = "Boss") -> dict:
    """Grow a round boss (standoff, peg) height_mm out of ANY planar face, centred at (x, y, z) mm.

    The face is found as for add_hole with face=. For a PCB standoff, add a blind
    hole in its top. Returns mass properties and `dimensions`.
    """
    return await _call(_session.add_boss_on_face, diameter_mm, face, x_mm, y_mm, z_mm, height_mm, name)


@mcp.tool()
async def add_text_on_face(text: str, face: str, x_mm: float, y_mm: float, z_mm: float,
                           height_mm: float, depth_mm: float, emboss: bool = False,
                           font: str | None = None, name: str = "Text") -> dict:
    """Engrave text into ANY planar face, or emboss it with emboss=True (labels, version numbers).

    (x, y, z) is the text's lower-left corner on the face; the text runs along
    the face sketch's horizontal axis: upright along +x on the top (+z) and
    front (-y) faces, turned on the others (vertical on +/-x, upside down on
    +y), so put labels on the top or front. height_mm is the
    character height, depth_mm the engraving depth or embossing height. The
    position is two dimensions ('x', 'y'); font must be installed (default:
    SolidWorks' own). Returns text_area_mm2, since letters have no hand calculation.
    """
    return await _call(_session.add_text_on_face, text, face, x_mm, y_mm, z_mm,
                       height_mm, depth_mm, emboss, font, name)


@mcp.tool()
async def cut_offset_pocket(face: str, x_mm: float, y_mm: float, z_mm: float, rim_mm: float,
                            depth_mm: float | None = None, name: str = "Pocket") -> dict:
    """Pocket a planar face, leaving a rim rim_mm wide along its outline.

    The recessed web of an I-beam that follows a curved link, a tray, or a frame
    (depth_mm omitted: through all). The face is the one facing `face`
    ('+z', ...) through the point (x, y, z). The rim follows the outline, arcs
    and splines included; holes in the face stay holes. rim and depth come back
    as dimensions, and the pocket follows the outline when it changes. Returns
    mass properties.
    """
    return await _call(_session.cut_offset_pocket, face, x_mm, y_mm, z_mm, rim_mm, depth_mm, name)


@mcp.tool()
async def cut_slot(length_mm: float, width_mm: float, x_mm: float, y_mm: float,
                   angle_deg: float = 0.0, depth_mm: float | None = None,
                   name: str = "Slot") -> dict:
    """Cut a straight slotted hole (obround) on the +Z face.

    Centred at (x_mm, y_mm); length_mm is centre-to-centre of the rounded ends,
    width_mm the slot width, angle_deg its orientation in the +Z plane (0 = +X).
    Cut blind by depth_mm or through when omitted. Returns mass properties.
    """
    return await _call(_session.cut_slot, length_mm, width_mm, x_mm, y_mm,
                       angle_deg, depth_mm, name)


@mcp.tool()
async def add_extruded_slot(start_mm: list, end_mm: list, width_mm: float, depth_mm: float,
                            name: str = "Slot") -> dict:
    """Extrude a stadium (rounded tab, lug or link) from point start_mm to end_mm ([x, y] mm).

    The half-round ends are centred on the two points themselves; width_mm is
    the tab's width, depth_mm the extrusion along +Z. Volume = (width * length
    + pi * (width/2)^2) * depth. Returns mass properties.
    """
    return await _call(_session.add_extruded_slot, start_mm, end_mm, width_mm, depth_mm, name)


@mcp.tool()
async def extrude_sketch(sketch: str, depth_mm: float | None = None, reverse: bool = False,
                         name: str = "Extrude", up_to: str | None = None) -> dict:
    """Extrude an existing sketch of the current part by name, e.g. one a person drew.

    depth_mm along the sketch's normal (reverse=True: the other way), merged
    with the body. Or up_to='next': up to the next face of the part, ending on
    its shape (a post into a curved wall) and following it when it changes;
    or up_to='@x,y,z': up to the face through that point, also where 'next'
    fails (a side of the profile on a face: the refusal names a point ahead).
    The sketch keeps its own dimensions and relations, so the person's design
    stays in charge; read_sketch shows it first. Returns mass properties.
    """
    return await _call(_session.extrude_sketch, sketch, depth_mm, reverse, name, up_to)


@mcp.tool()
async def add_sketch(plane: str, start_mm: list, segments: list, name: str | None = None,
                     fixed: bool = False) -> dict:
    """Draw a sketch of lines, arcs and splines on a plane, fully defined, to extrude or cut.

    plane: "front", "top", "right" or a plane by name (add_plane). Points are
    [u, v] in the plane's own axes, which the result gives (origin_mm + u
    x_axis + v y_axis): front is (x, y), top (x, -z), right (-z, y).
    The outline starts at start_mm; each segment runs on from where the last
    ended: {"line": [u, v]}; {"arc": [u, v], "through": [u, v]} (an arc through
    a middle point); {"arc": [u, v], "tangent": true} (an arc flowing on from
    the segment before); {"spline": [[u, v], ..., [u, v]]} (through the points,
    ending at the last). "tangent": true on a line checks that it flows on.
    End on start_mm to close the outline. Segments that meet smoothly get a
    tangent relation, the rest is dimensioned from the origin: x<i>/y<i> for
    the i-th point given (points_mm lists them; start_mm is 0), r<k> for the
    radius of segment k. Then extrude_sketch / cut_sketch the returned sketch.
    fixed=True freezes computed geometry (an involute, a cam) with Fix
    relations instead of dimensions.
    """
    return await _call(_session.add_sketch, plane, start_mm, segments, name, fixed)


@mcp.tool()
async def cut_sketch(sketch: str, depth_mm: float | None = None, reverse: bool = False,
                     name: str = "Cut") -> dict:
    """Cut an existing sketch of the current part by name into the body.

    depth_mm deep, or through all when depth_mm is omitted, against the
    sketch's normal: into the part from a face. From a plane with the part in
    front of it (a box on the Front plane) give reverse=True. Returns mass
    properties.
    """
    return await _call(_session.cut_sketch, sketch, depth_mm, reverse, name)


@mcp.tool()
async def add_fillet(radius_mm: float, edges: str = "all", name: str = "Fillet",
                     radii_at_mm: list | None = None, skip_shorter_mm: float | None = None,
                     tangent_propagation: bool = False) -> dict:
    """Round edges of the current part with one constant radius (mm).

    edges: "all" (default), "x"/"y"/"z" for edges parallel to that world axis,
    a face outline like "+z:outline" (the outer edges of the top face, not those
    of holes in it), every edge of one feature like "feature:Boss" (its top and
    where it meets the part: a moulded look), or explicit indices like "2,5" from
    list_edges. radii_at_mm = [[x, y, z, r], ...] makes the radius vary: r at
    the edge end at each point (list_edges gives the ends), radius_mm at the
    other ends, straight in between; 'vertex_radii' gives each end's dimension.
    skip_shorter_mm leaves out edges shorter than that. tangent_propagation=True
    carries the round on along edges that run on smoothly from the given ones,
    as SolidWorks does by default: an edge ending where it runs into another
    tangentially, such as round a fillet, only rounds so. A refusal names the
    edges the radius does not fit (each tried alone, and with propagation).
    Returns the number of edges filleted and the mass properties.
    """
    return await _call(_session.add_fillet, radius_mm, edges, name, radii_at_mm, skip_shorter_mm,
                       tangent_propagation)


@mcp.tool()
async def add_full_round(face: str, x_mm: float, y_mm: float, z_mm: float, name: str = "FullRound") -> dict:
    """Round a rib's top off completely: a full round fillet, as on a moulded part.

    The face facing `face` ("+z", ...) through the point (x, y, z) is the top;
    the two opposite flat side faces along it that lie closest together are the
    sides, so the round runs across the rib, not along it. The radius is half
    their distance (width_mm in the result) and follows the rib. Hand
    calculation: a rib w wide and L long loses L w^2 (1/2 - pi/8).
    """
    return await _call(_session.add_full_round, face, x_mm, y_mm, z_mm, name)


@mcp.tool()
async def add_chamfer(distance_mm: float, edges: str = "all", name: str = "Chamfer",
                      angle_deg: float = 45.0, from_face: str | None = None) -> dict:
    """Chamfer edges of the current part: distance_mm back along a face, at angle_deg (default 45°) to it.

    edges: "all" (default), "x"/"y"/"z", a face outline like "+z:outline", a
    feature's edges like "feature:Boss", or explicit indices like "2,5" from
    list_edges. Another angle than 45° needs from_face, which way the face that
    the distance runs along faces: "-z", angle_deg=60 makes an underside edge
    printable without support (round an arc the chamfer is a cone). Returns the
    number of edges chamfered and the mass properties.
    """
    return await _call(_session.add_chamfer, distance_mm, edges, name, angle_deg, from_face)


@mcp.tool()
async def add_linear_pattern(count: int, spacing_mm: float, direction: str = "+x",
                             feature_name: str | None = None) -> dict:
    """Repeat a feature `count` times, `spacing_mm` apart, along a direction.

    direction: "+x"/"-x"/"+y"/... feature_name: the feature to repeat (e.g.
    "Hole"); defaults to the most recently added feature. Returns mass properties.
    """
    return await _call(_session.add_linear_pattern, count, spacing_mm, direction, feature_name)


@mcp.tool()
async def add_circular_pattern(count: int, center_x_mm: float | None = None, center_y_mm: float | None = None,
                               feature_name: str | None = None, axis: str | None = None,
                               angle_deg: float = 360.0) -> dict:
    """Repeat a feature `count` times evenly around 360° about an axis.

    The axis is the cylindrical face nearest (center_x_mm, center_y_mm) — e.g. a
    centre hole drilled there — or axis='y' (a model axis through the origin:
    revolved parts turn about y) or a reference axis by name (add_axis).
    feature_name defaults to the last feature. Bolt circle: drill a centre hole
    + one bolt hole, then pattern the bolt hole. angle_deg < 360 spreads the
    instances over that angle, first to last (with axis=).
    """
    return await _call(_session.add_circular_pattern, count, center_x_mm, center_y_mm, feature_name, axis,
                       angle_deg)


@mcp.tool()
async def add_axis(through: list | None = None, planes: list | None = None, name: str | None = None) -> dict:
    """A reference axis anywhere: through=[[x, y, z], [dx, dy, dz]] (point and direction, model mm) or where two planes meet (planes=['Plane1', 'right']).

    Patterns turn about it by name (add_circular_pattern(axis=...)).
    """
    return await _call(_session.add_axis, through, planes, name)


@mcp.tool()
async def run_script(code: str | None = None, path: str | None = None, image: str | None = None) -> dict:
    """Run Python against the live SolidWorks: a whole part in one call, or what no tool covers; off when the server runs with --no-scripts.

    Give the script as code, or as a file at path: it may import helpers
    from its own folder (reloaded every run), so fix a part by editing the
    file and running it again. The script sees sw, model (current part/
    assembly), drawing, mod (typed interfaces), wrap(obj, mod.IFeature),
    mm (mm -> m), to_mm, doubles([..]) (a VARIANT array) and session (this
    server's tools: session.new_part(), session.add_box(...)). print() is
    captured and `result = ...` comes back. All or nothing: a script that
    raises leaves the part as it was.

    One report comes back for a part: features_added, failing_features,
    bodies (volume, box), clashes between bodies, under_defined_sketches,
    mass properties, seconds, and image: one PNG of the iso, right, top and
    bottom views (for a path, next to the script; image='x.png' elsewhere).
    """
    if not _ALLOW_SCRIPTS:
        return {"ok": False, "error": "run_script is off: the server was started with --no-scripts "
                                      "(or SOLIDWORKS_MCP_ALLOW_SCRIPTS=0). Start it without that to allow it."}
    return await _call(_session.run_script, code, path, image)


@mcp.tool()
async def add_mirror(plane: str, offset_mm: float = 0.0, features: list | None = None,
                     name: str = "Mirror") -> dict:
    """Mirror features, or the whole body, about the 'front'/'top'/'right' plane moved offset_mm.

    Mirrors about z / y / x = offset_mm; plane may also name another plane in
    the part ('Plane1'), moved offset_mm along its normal. features: list_features names; the
    copies follow their seeds. Without features the body is mirrored and
    merged: model half of a symmetric part, mirror it about the face where the
    halves meet. The plane position comes back as dimension 'plane_offset'.
    Fails, leaving the part as it was, when a copy would land outside the part.
    """
    return await _call(_session.add_mirror, plane, offset_mm, features, name)


@mcp.tool()
async def add_shell(thickness_mm: float, open_face: str = "+z") -> dict:
    """Hollow the current part to a wall of thickness_mm, opening one face.

    open_face: a direction "+z"/"-z"/"+x"/... removes that planar face (open
    shell); "none" makes a closed hollow. Returns the resulting mass properties.
    """
    return await _call(_session.add_shell, thickness_mm, open_face)


@mcp.tool()
async def add_base_flange(thickness_mm: float, bend_radius_mm: float, profile_mm: list | None = None,
                          sketch: str | None = None, depth_mm: float | None = None, k_factor: float = 0.5,
                          reverse: bool = False, flip_thickness: bool = False, name: str = "Base-Flange") -> dict:
    """Start a sheet metal part: a flat tab from a closed profile, or a bent section from an open one.

    profile_mm: [[x, y], ...] on the Front plane, closed when the last point is
    the first (a tab, thickness_mm thick along +z); open (an L, U or Z section,
    corners bent with bend_radius_mm inside) extruded depth_mm along +z.
    sketch: a sketch of the part by name instead (add_sketch: arcs, other
    planes). The thickness of an open profile lies on its right walked from
    the start (flip_thickness: left); reverse: along -z. k_factor sets the flat
    length. Returns the sheet's thickness, bend radius and K-factor.
    """
    return await _call(_session.add_base_flange, thickness_mm, bend_radius_mm, profile_mm, sketch, depth_mm,
                       k_factor, reverse, flip_thickness, name)


@mcp.tool()
async def add_edge_flange(at_mm: list, length_mm: float, angle_deg: float = 90.0,
                          bend_radius_mm: float | None = None, position: str = "material_inside",
                          flip: bool = False, name: str = "Edge-Flange") -> dict:
    """Bend a flange up from a straight edge of a sheet metal part, the edge's whole length.

    at_mm: a model point [x, y, z] on the edge (list_edges). length_mm is the
    flange's outside length, from the outer corner of the bend; angle_deg the
    bend. It rises the way the face at the edge looks (from an edge of the
    top face of a tab: up); flip=True folds it the other way. position:
    'material_inside' (the part keeps its outer size), 'material_outside' or
    'bend_outside'. bend_radius_mm defaults to the sheet's. Returns
    dimensions 'length' and 'angle' for set_dimension.
    """
    return await _call(_session.add_edge_flange, at_mm, length_mm, angle_deg, bend_radius_mm, position, flip, name)


@mcp.tool()
async def flat_pattern(action: str = "info", path: str | None = None) -> dict:
    """A sheet metal part's flat pattern: action 'info' (thickness, bend radius,
    K-factor, number of bends, flat blank size), 'flatten' / 'fold' (show the
    part flat or bent), or 'export' to path (.dxf or .dwg: the flat outline and
    bend lines for laser cutting; save the part first).
    """
    return await _call(_session.flat_pattern, action, path)


@mcp.tool()
async def add_closed_corner(at_mm: list, kind: str = "overlap", gap_mm: float = 0.1, name: str = "Closed Corner") -> dict:
    """Close the open corner between two flanges of a sheet metal part, ready to weld.

    at_mm: a model point [x, y, z] on the end face of one flange at the corner
    (list_faces); that flange grows to meet the one beside it. kind 'overlap'
    (it runs over the other flange's end), 'underlap' (it stops behind it) or
    'butt'; gap_mm stays between them. The flat pattern keeps the extra
    material. Mark the corner's weld on the drawing with add_symbol(type='weld').
    """
    return await _call(_session.add_closed_corner, at_mm, kind, gap_mm, name)


@mcp.tool()
async def add_weld_bead(from_mm: list, to_mm: list, size_mm: float, side: str = "one",
                        length_mm: float | None = None, pitch_mm: float | None = None, staggered: bool = False,
                        name: str = "Weld Bead") -> dict:
    """A fillet weld bead where two bodies meet: a plate on a base (merge=False), a member on a plate.

    from_mm / to_mm: model points [x, y, z] on the two faces, of two bodies
    (one body cannot be welded to itself). size_mm: the leg. side 'one' (along
    the from face where the faces meet), 'both' (both sides of it) or
    'all_around'. length_mm + pitch_mm make it intermittent (staggered with
    side='both'). Shows in the model and drawings, adds no mass. Returns the
    weld's length (its beads' sum) for a hand check.
    """
    return await _call(_session.add_weld_bead, from_mm, to_mm, size_mm, side, length_mm, pitch_mm, staggered, name)


@mcp.tool()
async def add_structural_member(profile: str, size: str, path_mm: list | None = None, sketch: str | None = None,
                                plane: str = "front", standard: str = "iso", corner: str | None = "miter",
                                rotate_deg: float = 0.0, name: str | None = None) -> dict:
    """Weldment frame members of a library profile along the lines of a path.

    profile / size as SolidWorks' weldment profile library names them, e.g.
    standard 'iso': 'square tube' '20 x 20 x 2' / '30 x 30 x 2.6' / '40 x 40 x 4',
    'rectangular tube' '50 x 30 x 2.6', 'pipe' '26.9 x 3.2', 'angle iron'
    '25 x 25 x 4', 'c channel' '80 x 8', 'sb beam' '100 x 8' (a wrong name
    lists what there is). path_mm: [[x, y], ...] on plane, lines in a chain
    (closed when the last point is the first); or sketch by name. The profile
    is centred on the lines; corner 'miter', 'butt1', 'butt2' or None where
    they meet; rotate_deg turns it about the line. Several calls build a frame
    of several groups. Returns the cut list (cut_list).
    """
    return await _call(_session.add_structural_member, profile, size, path_mm, sketch, plane, standard, corner,
                       rotate_deg, name)


@mcp.tool()
async def add_end_cap(at_mm: list, thickness_mm: float, inset_mm: float | None = None, inward: bool = False,
                      chamfer_mm: float | None = None, name: str = "End cap") -> dict:
    """Close the open end of a tube or pipe member with a plate.

    at_mm: a model point on the end face (the tube wall's end). thickness_mm
    outward past the end (inward=True: into the tube); its edge inset_mm in
    from the tube's outside (default half the wall); chamfer_mm on its corners.
    """
    return await _call(_session.add_end_cap, at_mm, thickness_mm, inset_mm, inward, chamfer_mm, name)


@mcp.tool()
async def add_gusset(first_mm: list, second_mm: list, leg1_mm: float, leg2_mm: float, thickness_mm: float,
                     shape: str = "triangle", end_mm: float | None = None, position: str = "center",
                     thickness_side: str = "both", name: str = "Gusset") -> dict:
    """A gusset plate in the corner of two weldment members.

    first_mm / second_mm: model points on the two faces, at right angles.
    leg1_mm runs along the first, leg2_mm along the second. shape 'triangle'
    (area leg1 leg2 / 2), or 'polygon' with both tips cut square end_mm long.
    position 'center' / 'start' / 'end' along the corner; thickness_side
    'both' (centred), 'inner' or 'outer'.
    """
    return await _call(_session.add_gusset, first_mm, second_mm, leg1_mm, leg2_mm, thickness_mm, shape, end_mm,
                       position, thickness_side, name)


@mcp.tool()
async def cut_list() -> dict:
    """A weldment's cut list: each kind of member with its description, length
    (outside, mitres included), end angles and quantity; and the weld beads
    with their size and length, and the total weld length."""
    return await _call(_session.cut_list)


@mcp.tool()
async def set_dimension(dimension_name: str, value_mm: float) -> dict:
    """Set a named driving dimension (e.g. 'D1@BlockExtrude') in mm, rebuild, and remeasure.

    This is the parametric edit at the heart of the correction loop. Every
    modelling tool returns its dimensions by role in `dimensions`. An angle (a
    revolve, an angle mate) takes degrees; list_dimensions shows the unit. A
    point's coordinate from the origin (a sketch's 'x3', 'y2') takes a sign,
    -5 being across the origin; any other length is a size, refused below 0.
    A value SolidWorks does not take (a dimension an equation drives, a size
    of 0) is refused with the reason, and the part is left as it was.
    """
    return await _call(_session.set_dimension, dimension_name, value_mm)


@mcp.tool()
async def slice_mesh(path: str, axis: str, heights_mm: list, frame: str = "object") -> dict:
    """Cross-sections of an STL/3MF mesh at the given heights along axis 'x'/'y'/'z'.

    For modelling a part that must fit something that only exists as a mesh.
    Returns each section's closed loops, largest first, as polygon points (mm)
    ready to use as profiles. frame (3MF): 'object' (the mesh's own frame) or
    'build' (as placed on the slicer's plate).
    """
    return await _call(_session.slice_mesh, path, axis, heights_mm, frame)


@mcp.tool()
async def compare_with_mesh(path: str, axis: str, heights_mm: list, frame: str = "object",
                            offset_mm: list | None = None) -> dict:
    """Compare the current part's cross-sections with a reference mesh's at the same heights.

    offset_mm [dx, dy, dz] moves the mesh into the part's frame. A different
    area means a misread feature; equal areas with different extents a
    shifted frame. Returns per-section pairs and the worst differences.
    """
    return await _call(_session.compare_with_mesh, path, axis, heights_mm, frame, offset_mm)


@mcp.tool()
async def list_dimensions() -> dict:
    """List every dimension in the current part: name (for set_dimension), feature, value, unit.

    For a part opened from disk, or when the names the tools returned are gone.
    """
    return await _call(_session.list_dimensions)


@mcp.tool()
async def read_sketch(name: str) -> dict:
    """Read a sketch of the current part back, e.g. one a person drew (a list_features name).

    Gives its lines, arcs, circles and splines in MODEL coordinates (mm),
    construction geometry marked, its dimensions (for set_dimension) and
    whether it is fully defined: check a design against fixed points before
    building on it with extrude_sketch / cut_sketch.
    """
    return await _call(_session.read_sketch, name)


@mcp.tool()
async def list_features() -> dict:
    """List the current part's modelling history in tree order: name, type, suppressed.

    For a part opened from disk, or to find the step to undo. Flags features
    that fail to rebuild and names sketches that are not fully defined.
    """
    return await _call(_session.list_features)


@mcp.tool()
async def delete_feature(name: str, with_children: bool = False, with_equations: bool = False,
                         dry_run: bool = False) -> dict:
    """Delete a feature (a list_features name) with its sketch, then rebuild and remeasure.

    The way to undo a step. Refuses when other features are built on it and
    names them, and what is built on those; with_children=True deletes them
    all. dry_run=True only lists what would go (would_delete). Equations that
    named its dimensions break and come back as broken_equations;
    with_equations=True deletes them (global variables stay).
    """
    return await _call(_session.delete_feature, name, with_children, with_equations, dry_run)


@mcp.tool()
async def suppress_feature(name: str, suppress: bool = True) -> dict:
    """Suppress a feature, or bring it back with suppress=False; rebuild and remeasure.

    Keeps the feature and its dimensions, unlike delete_feature, so it suits
    trying a variant. Features that depend on it follow both ways.
    """
    return await _call(_session.suppress_feature, name, suppress)


@mcp.tool()
async def reorder_feature(name: str, before: str) -> dict:
    """Move a feature (a list_features name), with its sketch, to just before another one; rebuild and remeasure.

    For a boss added last that filled holes cut earlier: build it at the end
    as usual, then move it before the first of those cuts, and they cut
    through it again. Refused, the part as it was, when the feature is built
    on something at or after that place. Returns the new order (features)
    and the features that fail to rebuild (failing_features).
    """
    return await _call(_session.reorder_feature, name, before)


@mcp.tool()
async def set_appearance(rgb: list | None = None, transparency: float | None = None,
                         component: str | None = None) -> dict:
    """Colour and/or see-through of the current part, or of one component in an assembly.

    rgb: [r, g, b], 0..255 each; transparency: 0 solid .. 1 clear (0 makes a
    see-through component solid again). In an assembly, component names it
    ('Bracket-1', 'Leg-1/Thigh-1'); the colour then belongs to the assembly and
    the part keeps its own. What is left out stays as it was.
    """
    return await _call(_session.set_appearance, rgb, transparency, component)


@mcp.tool()
async def set_material(name: str, database: str = "", density_kg_m3: float | None = None) -> dict:
    """Assign a material by name (e.g. "6061 Alloy", "AISI 1020", "ABS").

    Makes mass and density reflect a real material instead of the 1000 kg/m³
    default. One SolidWorks does not have (say "TPU") comes with its density:
    density_kg_m3=1210 makes it, by that name, carrying only the density.
    The English names work on a SolidWorks in another language too.
    Returns mass properties including density.
    """
    return await _call(_session.set_material, name, database, density_kg_m3)


@mcp.tool()
async def set_equation(equation: str | list) -> dict:
    """Add an equation, or replace the one that sets the same name; rebuild and remeasure.

    A SolidWorks equation string, e.g. '"D1@BlockExtrude" = 25' or
    '"D1@BlockExtrude" = 2 * "D1@Sketch1"'. A global variable is '"W" = 40';
    link a dimension to it with '"width@Sketch1" = "W"', and set '"W" = 45'
    again to change it. Persists a relation (unlike set_dimension). A list of
    equations is set in order with one rebuild at the end (later ones may use
    earlier ones), and a refused one undoes the whole list. Returns the index
    (per equation for a list), whether it replaced one, the features that fail
    to rebuild (failing_features) and mass properties.
    """
    return await _call(_session.equation, equation)


@mcp.tool()
async def list_equations() -> dict:
    """List the equations and global variables: index, equation, name it sets, value, global.

    `broken` marks an equation that names a dimension or variable that is gone,
    as one left behind by delete_feature; delete_equation removes it.
    automatic_solve_order False means SolidWorks solves them as listed: one
    above the line that sets its variable then warns and lags a rebuild. Any
    set_equation turns it on.
    """
    return await _call(_session.list_equations)


@mcp.tool()
async def delete_equation(equation: str) -> dict:
    """Delete an equation by the name it sets ("L_thigh", "D1@Boss") or its list_equations index.

    Rebuilds and returns what was deleted, failing_features and mass properties.
    """
    return await _call(_session.delete_equation, equation)


@mcp.tool()
async def list_documents() -> dict:
    """List the documents open in SolidWorks: title, path (empty until saved), type, unsaved changes.

    `current` marks the one the tools work on, `active` SolidWorks' own. Parts
    an open assembly loaded come along, with visible false.
    """
    return await _call(_session.list_documents)


@mcp.tool()
async def activate_document(title: str) -> dict:
    """Make an open document current again by its title, also a new one that was never saved.

    For after open_document / close_document on another document; list_documents gives
    the titles.
    """
    return await _call(_session.activate_document, title)


@mcp.tool()
async def delete_mate(name: str | list[str]) -> dict:
    """Delete a mate of the current assembly by name (list_components, or add_mate's 'mate').

    A list of names deletes them all at once, with one rebuild. Their
    components are free to move again. Returns what was deleted and how many
    mates are left (list_components lists them).
    """
    return await _call(_session.delete_mate, name)


@mcp.tool()
async def suppress_mate(name: str, suppress: bool = True) -> dict:
    """Suppress a mate so it stops holding its components (it stays in the tree), or bring it back.

    suppress=False brings it back. Returns whether it is suppressed now.
    """
    return await _call(_session.suppress_mate, name, suppress)


@mcp.tool()
async def delete_component(component: str) -> dict:
    """Remove a component from the current assembly, with the mates that hold it.

    component: its name in list_components ("Bracket-1", or "Bracket" while
    there is one). Returns the mates deleted, how many components are left and
    the assembly's mass properties.
    """
    return await _call(_session.delete_component, component)


@mcp.tool()
async def replace_component(component: str, path: str) -> dict:
    """Swap a top-level component for another .sldprt or .sldasm, in the same place.

    SolidWorks re-attaches each mate to the new part's matching face where it
    can. Returns the new component, the mates kept, and the mates that broke
    or went: each with its type, the other components, and what it held on
    the old part (the face's type, normal or axis and centre in the
    component's own frame), plus up to three candidate faces on the new part
    ('#index', nearest first). Re-mate with delete_mate and add_mate(mates=[...]).
    """
    return await _call(_session.replace_component, component, path)


@mcp.tool()
async def rebuild(top_only: bool = False) -> dict:
    """Force a rebuild of the current part or assembly and report whether it rebuilt without errors.

    In an assembly, mate_errors names each mate SolidWorks flags, and each
    mate that lost a face or edge it used (it rebuilds fine, but holds nothing).
    """
    return await _call(_session.rebuild, top_only)


@mcp.tool()
async def get_mass_properties() -> dict:
    """Get volume (mm^3), mass (kg), surface area (mm^2), centre of mass, and bounding box."""
    return await _call(_session.get_mass_properties)


@mcp.tool()
async def get_bounding_box() -> dict:
    """Get the bounding box (min/max/size in mm) of the current part, tight, or of the whole assembly."""
    return await _call(_session.bounding_box)


@mcp.tool()
async def list_faces(component: str | None = None) -> dict:
    """List the part's faces (index, planar?, normal, area, centre) for inspection.

    A cylindrical face also gives its axis, radius and a point on the axis (for
    a hole: its centre), so hole patterns can be read off. component: a
    component of the current assembly, listed in its own frame (e.g. an
    imported servo); for a sub-assembly, every part's faces in the
    sub-assembly's frame, each naming its part. A part's indices shift as
    features are added; a component's are ordered by part, type, centre and
    area, so they stay while its shape does. add_mate takes them as '#5'.
    """
    return await _call(_session.list_faces, component)


@mcp.tool()
async def list_edges(face: str | None = None, feature: str | None = None, within_mm: list | None = None,
                     min_length_mm: float | None = None) -> dict:
    """List the part's edges: index, type, length, ends; lines also axis and midpoint.

    Narrow it on a big part: face (a list_faces index, "#5"), feature (the edges
    of the faces it made), within_mm ([[x1, y1, z1], [x2, y2, z2]]: both ends
    inside), min_length_mm. Indices are always those of the whole part: use them
    with add_fillet/add_chamfer edges="2,5".
    """
    return await _call(_session.list_edges, face, feature, within_mm, min_length_mm)


@mcp.tool()
async def check_printability(up: str = "+z", overhang_deg: float = 45.0,
                             min_wall_mm: float | None = None) -> dict:
    """Check the current part for 3D printing, built along `up` ('+z', '-y', ...).

    Overhangs: downward faces leaning more than overhang_deg from vertical (90 =
    a flat ceiling) need support; the faces on the bed do not count. Gives the
    overhanging area per face (list_faces index) with its worst lean and centre,
    the bed contact area and the print height. With min_wall_mm (e.g. two
    nozzle widths) also the walls thinner than that, measured straight through
    the material from points spread over every face, with where each is thinnest.
    A spot right beside an edge where faces meet at a sharp angle reads as thin
    as the wedge of material there is: that is the edge, not a wall.
    """
    return await _call(_session.check_printability, up, overhang_deg, min_wall_mm)


@mcp.tool()
async def read_model(sections: list[str] | None = None, wall_mm: float | None = None, pull: str | None = None,
                     clearance_pairs: list | None = None) -> dict:
    """Read everything a design review needs of the current part in one read-only call (schema sw-mcp/model@1).

    Sections ('core' is always read; None reads core, material, holes, radii
    and setups, plus walls with wall_mm and draft with pull):
    - core: mass, volume, area, density, centre of mass, bbox, bodies, features
      with rebuild errors, dimensions with their own tolerance (type, plus/minus,
      ISO fit such as 'H7'), equations, under-defined sketches.
    - material: name, database, density; null if none is assigned.
    - holes: every hole of every body, identical ones grouped with count and
      centres_mm (at the entrance). Hole Wizard holes give standard, size, fit
      and sizes from the wizard (source 'wizard'); other holes are measured
      from the geometry (source 'geometry'): kind simple / counterbore /
      countersink / tapped, diameter, depth, through, drilling axis, entrance.
    - radii: internal (concave) radii by feature, and sharp internal edges by
      feature, direction (x/y/z, null if curved) and length.
    - setups: planar face normals and the setup directions (+x ... or a vector).
    - walls (needs wall_mm): the thinnest wall and up to 20 spots below wall_mm.
    - draft (needs pull, '+z'...): each face's draft, the worst 20, and undercut faces.
    A section that fails is null and named in `gaps`, with anything cut off
    to keep the answer small. Nothing is selected, changed or saved.
    clearance_pairs belongs to the assembly section, not available yet.
    """
    return await _call(_session.read_model, sections, wall_mm, pull, clearance_pairs)


@mcp.tool()
async def export(path: str, file_format: str | None = None, quality: str = "fine",
                 deviation_mm: float | None = None, angle_deg: float | None = None,
                 per_component: bool = False) -> dict:
    """Export the current part or assembly to STEP/STL/IGES/Parasolid/3MF (format from extension).

    Silent (no prompts). Verifies the file appears on disk and reports its size.
    For STL/3MF, tessellation resolution is set first: quality 'coarse'|'fine'
    (default 'fine' for print quality), or pass deviation_mm (+ optional angle_deg)
    for a reproducible custom resolution (overrides quality). Ignored for other formats.
    An assembly goes to STL as one file, or one file per component with
    per_component=True (SolidWorks names them); `files` lists what was written.
    STL/3MF keep the part's or assembly's own coordinates (`frame`), also per
    component, so a mesh measures back where the model is; slicers place it anyway.
    """
    return await _call(_session.export, path, file_format, quality, deviation_mm, angle_deg, per_component)


@mcp.tool()
async def screenshot(path: str, view: str = "iso", zoom_mm: list | None = None,
                     show_planes: bool = False, from_dir: list | None = None) -> dict:
    """Save a screenshot of the current part or assembly (PNG/JPG/TIF).

    view: 'iso' (default), 'front', 'back', 'left', 'right', 'top' or 'bottom';
    or from_dir = [x, y, z], the direction to look from, e.g. [-1, 1, -1] to
    see from behind and below what iso hides (the model's +y stays up).
    Zoomed to fit, or onto a detail: zoom_mm = [[x1, y1, z1], [x2, y2, z2]], the
    corners of the region to fill the image (model mm, in any view). Reference
    planes and axes are left out, so they do not cut through the shape;
    show_planes=True keeps them.
    """
    return await _call(_session.screenshot, path, view, zoom_mm, show_planes, from_dir)


# --- drawings -------------------------------------------------------------------
# Sheet millimetres, origin at the sheet's bottom-left corner. Annotations are
# named '<name>@<view>'. References into a view: 'edge:-x' (the face facing -x,
# seen edge-on), 'hole:<feature>', 'feature:<name>' or a model point [x, y, z].
# Each family of operations is one tool with a kind/type/action/style switch,
# so a client loads few drawing tools.


@mcp.tool()
async def make_drawing(path: str, sheet: str = "A4", scale: str | None = None, keep_open: bool = False) -> dict:
    """Make a manufacturing drawing of the current part in one call: PDF, .slddrw, DXF or DWG.

    Front, top and side views (first angle) plus an iso view on an ISO sheet
    ('A4' upright, 'A3'..'A0' landscape) with SolidWorks' ISO title block, at
    the largest standard scale that leaves room for dimensions (or scale='1:2').
    The model's dimensions come in once each, one hole callout per set of
    identical Hole Wizard holes ('4X ...'), their centres dimensioned, and the
    title block shows material and mass. A sheet metal part gets its flat
    pattern in place of the iso view, with bend lines, bend notes and the
    blank's overall size. Weld beads come in with their weld symbols; a
    weldment gets its cut list table. Returns `problems` (check_drawing).
    keep_open=True keeps it as the current drawing to refine with the other
    drawing tools. Save the part first (save_document): the drawing refers to its file.
    """
    return await _call(_session.make_drawing, path, sheet, scale, keep_open)


@mcp.tool()
async def build_drawing(spec: dict, path: str | None = None) -> dict:
    """Build a drawing from scratch from a spec; edit the spec and rebuild to fine-tune.

    spec: {"part": path (default the current part), "sheet": "A3",
    "scale": "1:2", "views": [{"name": "front", "orient": "front", "at": [x, y]},
    {"name": "side", "projected_from": "front", "side": "right"},
    {"name": "A-A", "section_of": "front", "through": [x, y, z], "normal": "x"},
    {"name": "B", "detail_of": "front", "center": [x, y, z], "radius": 5},
    {"name": "flat", "flat_pattern": true, "right_of": "front"}],
    "import": ["model", "callouts", "hole_locations"],
    "dims": [{"view": "front", "ref": "edge:-x", "to": "hole:H1"},
    {"view": "front", "chain": ["edge:-x", "hole:H1", "hole:H2"]}],
    "callouts": [{"view": "front", "hole": "H1"}],
    "symbols": [{"type": "datum", "view": "top", "ref": "edge:-z", "label": "A"},
    {"type": "gtol", "view": "top", "ref": "edge:+z", "symbol": "parallelism",
    "tolerance": "0.05", "datums": ["A"]}, {"type": "surface_finish", ...}],
    "notes": ["..."], "tables": [[["a", "b"], ...]], "gear_table": {"kind":
    "spur", "teeth": 20, "module": 2}, "props": {"material": "from part"},
    "layout": "auto"}. Saved to path when given. Returns what was made and the
    problems check_drawing finds.
    """
    return await _call(_session.build_drawing, spec, path)


@mcp.tool()
async def drawing_file(action: str, path: str | None = None, sheet: str = "A3", part: str | None = None) -> dict:
    """The current drawing's document: action 'new' (sheet 'A4' upright or 'A3'..'A0', part: the saved
    model its views show, default the current part), 'open' (path .slddrw), 'save' (path .slddrw),
    'export' (path: PDF, DXF, DWG, PNG/JPG/TIF of the sheet) or 'close' (without saving).

    The drawing is kept apart from the current part. 'new' returns the frame,
    title block and free areas in sheet mm.
    """
    return await _call(_session.drawing_file, action, path, sheet, part)


@mcp.tool()
async def set_drawing_properties(properties: dict) -> dict:
    """Fill the title block through the custom properties its fields show.

    description, material, weight, finish go to the part ('from part' links
    material / weight to the part's own, so they follow the model); drawn_by,
    drawn_date, checked_by, checked_date, approved_by, revision, company,
    part_no, qty or any other name go to the drawing. Returns what each title
    block field now shows and which properties it has no field for.
    """
    return await _call(_session.set_drawing_properties, properties)


@mcp.tool()
async def add_view(kind: str = "model", orientation: str = "front", parent: str | None = None,
                   side: str | None = None, through: list | None = None, normal: str = "x",
                   label: str | None = None, flip: bool = False, center: list | None = None,
                   radius_mm: float | None = None, at: list | None = None, beside: str | None = None,
                   gap_mm: float | None = None, scale: str | None = None, name: str | None = None,
                   part: str | None = None) -> dict:
    """Add a view. kind:
    - 'model': orientation 'front', 'top', 'right', 'left', 'back', 'bottom', 'iso' or '*Name'; its
      geometry centred on at=[x, y] (sheet mm), or beside='<view>' on side 'right'/'left'/'above'/'below';
    - 'projected': from parent, on side (first angle), aligned with it;
    - 'section': parent cut by the plane through model point through=[x, y, z] square to model axis
      normal ('x'/'y'/'z'); label 'A'; flip=True looks the other way;
    - 'detail': of parent, the circle round model point center=[x, y, z] with model radius radius_mm,
      at scale (default about 2.5x the parent's).
    - 'flat_pattern': a sheet metal part unfolded, with bend lines, bend notes and its overall size
      dimensioned; at=[x, y], or beside='<view>' on side.
    Placed with room for dimensions, past views in the way (gap_mm to fix the gap). scale '1:2'.
    A section or detail view keeps its label's name; `name` becomes an alias for it.
    """
    return await _call(_session.add_drawing_view, kind, orientation, parent, side, through, normal, label, flip,
                       center, radius_mm, at, beside, gap_mm, scale, name, part)


@mcp.tool()
async def edit_view(name: str, at: list | None = None, beside: str | None = None, side: str | None = None,
                    gap_mm: float | None = None, align: str = "center", scale: str | None = None,
                    display: str | None = None, tangent_edges: bool | None = None, new_name: str | None = None,
                    delete: bool = False) -> dict:
    """Move a view (its geometry centred on at=[x, y], or beside='<view>' on side, gap_mm apart; align
    'center' or 'start'), change it (scale '1:2' or 'sheet'; display 'hidden_removed', 'hidden_visible',
    'wireframe', 'shaded', 'shaded_edges'; tangent_edges; new_name), or delete=True it.

    Projected and section views stay aligned with their parent, so they only
    move along that line.
    """
    return await _call(_session.edit_view, name, at, beside, side, gap_mm, align, scale, display, tangent_edges,
                       new_name, delete)


@mcp.tool()
async def list_views(view: str | None = None) -> dict:
    """The sheet's frame and title block, and each view (or one): name, type, scale, outline and geometry
    box (sheet mm), the model-to-sheet mapping (a model point p lands at origin + p.x * x + p.y * y +
    p.z * z), and its annotations: id, type, shown text, value, box."""
    return await _call(_session.annotations, view)


@mcp.tool()
async def import_dimensions(view: str | None = None, kinds: list | None = None) -> dict:
    """Bring in what the model already knows. kinds: 'model' (its dimensions, each once), 'callouts'
    (one counted hole callout per set of identical Hole Wizard holes, '4X ...'), 'hole_locations' (their
    centres dimensioned from the left and bottom edges), 'center_marks' (centre marks and lines).

    Default the first three, into every view (or one).
    """
    return await _call(_session.import_annotations, view, kinds)


@mcp.tool()
async def add_dimension(view: str, ref: str | list, to: str | list | None = None, style: str = "single",
                        direction: str = "auto", side: str | None = None, offset_mm: float | None = None) -> dict:
    """Dimension in a view between references, or the size of one.

    ref / to: 'edge:-x' (the face facing -x, seen edge-on), 'hole:CBore1' (its
    centre; alone its diameter), 'feature:CornerFillet' (alone: its radius), or
    a model point [x, y, z] (the edge nearest it). style 'single' (ref to `to`),
    or 'baseline' / 'chain' / 'ordinate' with to=[ref, ref, ...] measured from
    ref. direction 'auto', 'horizontal', 'vertical', 'aligned'. side 'below',
    'above', 'left', 'right' (default nearer the geometry); offset_mm from the
    outline (default the first free row: 10, 18, 26 ...). Returns ids, values,
    boxes, and warnings on overlaps.
    """
    return await _call(_session.dimension, view, ref, to, style, direction, side, offset_mm)


@mcp.tool()
async def add_symbol(type: str, view: str, ref: str | list, at: list | None = None, count: bool = True,
                     roughness: str | None = None, process: str = "machined", label: str | None = None,
                     symbol: str | None = None, tolerance: str | None = None, datums: list | None = None,
                     diameter: bool = False, material_condition: str | None = None,
                     weld: dict | None = None) -> dict:
    """A symbol on an edge of a view, its leader attached mid-edge. type:
    - 'hole_callout': ref 'hole:<feature>'; diameter, depth, counterbore/countersink, thread, linked to
      the model; count=True counts identical holes in the view into it ('4X ...');
    - 'surface_finish': ISO 1302, roughness 'Ra 1.6', process 'machined', 'any' or 'no_removal';
    - 'datum': lettered datum feature symbol, label 'A';
    - 'gtol': ISO 1101 frame, symbol straightness, flatness, circularity, cylindricity, line_profile,
      surface_profile, parallelism, perpendicularity, angularity, position, concentricity, symmetry,
      runout or total_runout; tolerance '0.05'; datums ['A', 'B']; diameter=True for a round zone;
      material_condition 'MMC' / 'LMC';
    - 'weld': ISO 2553, weld={'weld': 'fillet' (square, v, bevel, v_root, bevel_root, u, j, backing,
      plug, spot, seam), 'size': 'a4', 'length': '100' or '3x50 (100)', 'side': 'arrow' / 'other' /
      'both', 'contour': 'flat' / 'convex' / 'concave', 'all_around': true, 'field': true}.
    ref as in add_dimension. at=[x, y] places it (sheet mm), by default off the view, clear of text.
    """
    return await _call(_session.add_symbol, type, view, ref, at, count, roughness, process, label, symbol,
                       tolerance, datums, diameter, material_condition, weld)


@mcp.tool()
async def add_note(text: str, at: list | None = None, height_mm: float = 3.5) -> dict:
    """A note on the sheet; at=[x, y] its top-left corner (sheet mm), by default above the title block. Lines: '\\n'."""
    return await _call(_session.add_note, text, at, height_mm)


@mcp.tool()
async def add_table(rows: list | None = None, gear: dict | None = None, at: list | None = None, title: bool = True,
                    text_height_mm: float = 2.5, column_widths_mm: list | None = None, cut_list: bool = False) -> dict:
    """A table on the sheet: rows [[cell, ...], ...] (title=True merges the first row), or gear={...} for a
    computed gear data table: kind 'spur' (diameters, span over k teeth, centre distance) or 'bevel'
    (straight: cone angles, cone distance, chordal thickness and height, crown to apex), teeth, module,
    mating_teeth, pressure_angle_deg, face_width_mm, profile_shift, shaft_angle_deg, accuracy,
    extra_rows [[label, value], ...]; teeth, module and mating_teeth default to the part's custom
    properties Teeth, Module, MatingTeeth. cut_list=True: the weldment's cut list (item, quantity,
    description, length, angles) and total weld length. at=[x, y] its top-left corner, by default free room.
    """
    return await _call(_session.table, rows, gear, at, title, text_height_mm, column_widths_mm, cut_list)


@mcp.tool()
async def edit_annotation(annotation_id: str, prefix: str | None = None, suffix: str | None = None,
                          precision: int | None = None, tolerance: list | None = None, fit: str | None = None,
                          text: str | None = None, at: list | None = None, diametric: bool | None = None,
                          delete: bool = False) -> dict:
    """Change an annotation by id ('RD3@Drawing View1'), or delete=True it.

    prefix / suffix (a prefix replaces SolidWorks' own symbol: a radius needs
    'R', e.g. '4X R'), precision (decimals), tolerance [upper, lower] mm, fit
    ('H7'), a note's text, its place at=[x, y] (sheet mm), diametric=True. A
    model dimension keeps these in the part (the result says changes_part).
    """
    return await _call(_session.change_annotation, annotation_id, prefix, suffix, precision, tolerance, fit, text,
                       at, diametric, delete)


@mcp.tool()
async def auto_layout(scale: str | None = None, main: str | None = None, arrange_dimensions: bool = False) -> dict:
    """Lay all views out again: the main view top left, its projected and section views beside it, the
    rest in the room left, with room for dimensions, at the largest standard scale that fits (or scale).
    arrange_dimensions=True then lets SolidWorks space each view's dimensions evenly."""
    return await _call(_session.lay_out, scale, main, arrange_dimensions)


@mcp.tool()
async def check_drawing() -> dict:
    """Problems on the sheet, to fix before sending the drawing out.

    Views or annotations outside the frame or over the title block, views
    overlapping, texts overlapping or on view geometry, crossing leaders, text
    under 2.5 mm, probable duplicate dimensions, and hole/cut/fillet/chamfer
    features no dimension or callout covers.
    """
    return await _call(_session.check_drawing)


@mcp.tool()
async def render_drawing(path: str, zoom_mm: list | None = None) -> dict:
    """Picture (PNG/JPG) of the sheet, or of the region zoom_mm = [[x1, y1], [x2, y2]] (sheet mm), to look at it."""
    return await _call(_session.render_drawing, path, zoom_mm)


@mcp.tool()
async def close_document(title: str | None = None, save: bool = False) -> dict:
    """Close one open document by its title (list_documents): a part, assembly or drawing.

    Without a title it closes the current part or assembly. save=True saves it
    over its own file first; otherwise closing never asks and unsaved changes
    are lost (the result says discarded_changes). Parts that an open assembly
    loaded (visible false) close with that assembly and are refused here. With
    no title and no current document it closes SolidWorks' active one, but
    only when that has no unsaved changes.
    """
    return await _call(_session.close_document, title, save)


@mcp.tool()
async def save_document(path: str | None = None) -> dict:
    """Save the current part (.sldprt) or assembly (.sldasm) to path, or over its own file without one.

    Saves silently and checks the file was really written. Drawings: drawing_file(action='save').
    """
    return await _call(_session.save_document, path)


@mcp.tool()
async def open_document(path: str, kind: str | None = None) -> dict:
    """Open a .sldprt or .sldasm, or import a STEP/IGES/Parasolid file; it becomes the current document.

    A neutral file opens as a part, or as an assembly if it holds one
    (kind='part' / 'assembly' says which up front). A part shows its history in
    list_features and its dimensions in list_dimensions; an imported body has
    no history, but the tools work on it (holes and pockets on its faces,
    bosses, fillets), and the result gives its solid body count and mass
    properties. An imported assembly's parts arrive as components at their
    places, ready for list_components and add_mate; the result gives the
    component count. Drawings: drawing_file(action='open').
    """
    return await _call(_session.open_document, path, kind)


# --- assemblies ---------------------------------------------------------------


@mcp.tool()
async def insert_component(path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                           z_mm: float = 0.0, fixed: bool | None = None) -> dict:
    """Insert a .sldprt, or a .sldasm as a sub-assembly, with its ORIGIN at (x, y, z) mm.

    The part's own origin lands exactly on that point, and the placement is read
    back and verified. fixed=True pins the component; fixed=False leaves it free
    for mates. The default fixes only the FIRST component, giving the assembly a
    ground to build against. Returns the component's name, placement and box.
    """
    return await _call(_session.insert_component, path, x_mm, y_mm, z_mm, fixed)


@mcp.tool()
async def list_components(name: str | None = None, mates: bool = True, brief: bool = False) -> dict:
    """List the assembly's components (name, path, fixed, position, rotation, bounding box) and mates.

    Positions are in mm and rotations in degrees, both in assembly coordinates;
    the bounding box of each component is in assembly coordinates too. Each
    mate gives its name, SolidWorks type, the components it holds, the
    dimension of a distance or angle mate, and an error when SolidWorks flags it.
    On a big assembly, narrow it: name keeps the matching components ('Bolt*'
    as a wildcard, 'bolt' anywhere in the name) and the mates holding them;
    mates=False leaves the mates out; brief=True gives only name, path, fixed.
    """
    return await _call(_session.list_components, name, mates, brief)


@mcp.tool()
async def set_component_transform(name: str, x_mm: float, y_mm: float, z_mm: float,
                                  rx_deg: float = 0.0, ry_deg: float = 0.0,
                                  rz_deg: float = 0.0) -> dict:
    """Move/rotate a component: its origin to (x, y, z) mm, rotated rx/ry/rz degrees.

    name is the component name ('Bed' or 'Bed-1'); rotations apply X, then Y,
    then Z about the assembly axes, and work on a fixed component too. The
    transform is read back and compared, so a move SolidWorks ignored (e.g. one
    already pinned by mates) fails loudly instead of silently leaving the part
    where it was.
    """
    return await _call(_session.set_component_transform, name, x_mm, y_mm, z_mm,
                       rx_deg, ry_deg, rz_deg)


@mcp.tool()
async def add_mate(comp_a: str | None = None, face_a: str | None = None, comp_b: str | None = None,
                   face_b: str | None = None, mate_type: str = "coincident", distance_mm: float = 0.0,
                   angle_deg: float = 0.0, flip: bool = False, mates: list[dict] | None = None,
                   stop_on_error: bool = True) -> dict:
    """Mate a face of one component to a face of another, or add several mates in one call.

    comp_a/comp_b are component names ('Bed' or 'Bed-1'; 'Leg-1/Thigh-1' inside
    a sub-assembly). face_a/face_b select a face in that component's OWN frame:
    by direction "+x"/"-x"/"+y"/... (optionally "+y:inner" for the cavity side
    of a hollow part), by its list_faces(component=...) index, "#5", or by a
    point on it, "@x,y,z" (a hole's wall: its centre plus the radius along x).
    mate_type between planar faces:
    "coincident", "distance" (distance_mm), "parallel", "perpendicular" or
    "angle" (angle_deg between the faces' normals, 0..180); "concentric" between
    two cylindrical faces (a pin in a hole, a hinge axis), leaving the turn
    about the axis free. flip takes the other solution: the mirror side of a
    distance, the other turning direction of an angle. A distance or angle
    mate returns its `dimension`: drive it with
    set_dimension, or step it with check_motion. An angle mate's dimension
    then turns the joint the whole way round, past 180 and below 0 (-30 is 30
    the other way, the same turn as 330). The result is measured back
    after the rebuild; a mate that does not hold is removed and the components
    are put back.

    Several at once: mates=[{"comp_a": "Bolt-1", "face_a": "#3", "comp_b":
    "Plate-1", "face_b": "#12", "mate_type": "concentric"}, ...], each with the
    arguments above. stop_on_error=True stops at the first that fails; the mates
    before it stay. Returns a result per mate (its name and dimension, or the
    error), how many were added, failed and not tried, and the mate count.
    """
    return await _call(_session.mate, comp_a, face_a, comp_b, face_b, mate_type, distance_mm, angle_deg, flip,
                       mates, stop_on_error)


@mcp.tool()
async def check_interference() -> dict:
    """Report component pairs whose solids overlap, with the volume in mm^3.

    Touching faces do not count (a bed standing on the floor is fine); only real
    overlapping material does. count == 0 means the assembly is clash-free.
    """
    return await _call(_session.check_interference)


@mcp.tool()
async def measure_distance(component_a: str, component_b: str | None = None,
                           point_mm: list | None = None, axis_mm: list | None = None) -> dict:
    """The smallest distance (mm) from a component to another one, a point or an axis.

    Give component_b; or point_mm = [x, y, z] in assembly mm, and the nearest
    point on the component comes back too, with inside: true when the point lies
    in its material; or axis_mm = [[x, y, z], [dx, dy, dz]], the endless line
    through that point along that direction, such as a bolt's axis. 0 means they
    touch or overlap; check_interference tells which. Sub-assemblies count with
    all their parts; one part inside is 'Leg-1/Thigh-1'.
    """
    return await _call(_session.measure_distance, component_a, component_b, point_mm, axis_mm)


@mcp.tool()
async def check_motion(dimension_name: str, values: list, distances: list | None = None) -> dict:
    """Step a joint through its range and check the assembly at every step.

    dimension_name: an angle or distance mate's dimension (add_mate returns it);
    values in degrees for an angle, mm for a distance, e.g. [30, 60, 90, 120,
    150]. Each step gives the overlapping pairs with their volume and the
    distance between each pair in distances ([["Rod", "Bolt"], ...]); the
    summary gives clash_free and each pair's smallest distance with the value
    where it occurs. The dimension goes back to its value afterwards. A joint
    inside a sub-assembly is 'D1@Angle1@Leg-1'. Refused while a mate is broken:
    the parts it should hold would drift and the result mean nothing.
    """
    return await _call(_session.check_motion, dimension_name, values, distances)


@mcp.tool()
async def swept_region(component: str, dimension: str, values: list, heights_mm: list, axis: str = "z",
                       margin_mm: float = 0.0, tolerance_mm: float = 0.2, frame: str | None = None) -> dict:
    """Outline what a component covers in a plane while a joint moves, to keep
    clear of it or cut it away.

    Steps the joint's dimension through values (as check_motion, e.g. every
    5 degrees over 30..150), cuts the component at axis = each of heights_mm
    (a few heights inside a layer, not on its faces) and joins all of it, made
    margin_mm wider, within about tolerance_mm. frame: the component to give the
    coordinates in, such as the part to cut from; else the assembly's. Each
    region's outline runs counter-clockwise in the plane's other two axes,
    (x, y) across z: give it to add_sketch as a spline. count says how many
    separate regions there are.
    """
    return await _call(_session.swept_region, component, dimension, values, heights_mm, axis,
                       margin_mm, tolerance_mm, frame)


def main() -> None:
    """Entry point: run the MCP server over stdio, or the selftest with --selftest."""
    parser = argparse.ArgumentParser(prog="sw-mcp", description="MCP server for a running SolidWorks.")
    parser.add_argument("--selftest", action="store_true",
                        help="check this machine's SolidWorks with a few small parts, print a report and exit")
    parser.add_argument("--no-scripts", action="store_true",
                        help="turn off the run_script tool, which runs any Python against SolidWorks")
    parser.add_argument("--allow-scripts", action="store_true",
                        help=argparse.SUPPRESS)  # the default now; still accepted for older configs
    args = parser.parse_args()
    selftest = args.selftest
    global _ALLOW_SCRIPTS
    if args.no_scripts:
        _ALLOW_SCRIPTS = False
    elif args.allow_scripts:
        _ALLOW_SCRIPTS = True
    try:
        if not selftest:
            mcp.run()
        elif _worker is None:
            sys.exit(run_selftest(_session))
        else:
            sys.exit(_worker.submit(lambda: run_selftest(_session)).result())
    finally:
        if _worker is not None:
            _worker.shutdown()


if __name__ == "__main__":
    main()

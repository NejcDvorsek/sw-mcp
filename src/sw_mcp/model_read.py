"""read_model: what a design review reads of a part, in one read-only call.

Mass, features, dimensions with their tolerances, equations, material, every
hole with its sizes, internal radii and sharp internal edges, setup
directions, thin walls and draft. The server derives them, so a client does
not read faces and guess what they mean. The output is sw-mcp's own schema,
sw-mcp/model@1.

Mixed into SolidWorksSession, so every method runs on the COM worker thread.
The COM side reads each body once into plain dicts (faces, edges); everything
after that is the module-level functions below, without COM, unit-tested in
tests/test_model_read.py.

API facts this depends on (SOLIDWORKS 2026 SP4.1, verified):
- IFace2.FaceInSurfaceSense() is True where the face's outward normal runs
  AGAINST its surface's own normal (ISurface.EvaluateAtPoint, PlaneParams), on
  a box's six faces, a hole, a boss and convex and concave fillets alike.
  IFace2.Normal and the tessellation normals are outward already.
- With an edge's tangent taken in the sense of its first face's loop
  (IEdge.EdgeInFaceSense), (n1 x n2) . t > 0 at a convex edge and < 0 at a
  concave one: a box's 12 edges, a pocket's corners and floor edges, the floor
  circle of a blind hole.
- A face or edge handed out twice comes in two wrappers of one COM object;
  the object's IUnknown pointer tells them apart (_com_key).
- IWizardHoleFeatureData2.Type holds the specific hole type (14, 44, 46, ...),
  so the kind is read from which of its diameters are set.
"""

import math
import re
import time

import pythoncom
import win32com.client

from . import __version__, binding
from .constants import (
    RAY_HIT_WIDTH,
    SKETCH_STATUSES,
    SW_ANGULAR_DIMENSION,
    SW_BODY_SOLID,
    SW_DIMENSION_DRIVING,
    SW_DOC_ASSEMBLY,
    SW_DOC_DRAWING,
    SW_DOC_PART,
    SW_END_COND_THROUGH_ALL,
    SW_FULLY_CONSTRAINED,
    SW_RAY_HIT_EXIT,
    SW_RAY_NORMALS_ENTRY_EXIT,
)
from .errors import SolidWorksError
from .units import m_to_mm, mm_to_m

SCHEMA = "sw-mcp/model@1"
PART_SECTIONS = ("core", "material", "holes", "radii", "setups", "walls", "draft")
SECTIONS = (*PART_SECTIONS, "assembly")
SECTION_FIELDS = {  # what a section adds to a part; all None when it fails
    "core": ("name", "path", "configuration", "mass_kg", "volume_mm3", "surface_area_mm2", "density_kg_m3",
             "centre_of_mass_mm", "bbox_mm", "bodies", "features", "dimensions", "equations",
             "under_defined_sketches"),
    "material": ("material",),
    "holes": ("holes",),
    "radii": ("internal_radii_mm", "sharp_internal_edges"),
    "setups": ("planar_face_normals", "setup_directions"),
    "walls": ("wall_check",),
    "draft": ("draft_check",),
}
LIST_CAP = 20            # thin spots, draft faces, undercut faces, edge groups
SNAP_DEG = 0.5           # a direction this close to an axis is that axis; normals this close merge
AXIS_TOL_MM = 1e-3       # two cylinders share an axis line this close ...
AXIS_TOL_RAD = 1e-4      # ... and this parallel
HOLE_MIN_ARC_DEG = 300.0  # a cylinder rounder than this can be a hole; less is a slot end or a fillet
TANGENT_DEG = 1.0        # faces meeting flatter than this meet smoothly: no sharp edge
SIDE_MIN_COS = 0.1       # the material-side test needs the normal this radial, else it is ambiguous
UNDERCUT_RAYS_PER_FACE = 12  # spread over each face that faces the pull
WIZARD_TYPES = ("HoleWzd", "AdvHoleWzd")
PATTERN_TYPES = ("LPattern", "CirPattern", "MirrorPattern", "MirrorSolid", "TablePattern", "SketchPattern",
                 "CurvePattern", "FillPattern", "VarPattern", "DerivedLPattern", "DerivedCirPattern")
_THROUGH_END_CONDITIONS = {SW_END_COND_THROUGH_ALL, 2, 9}  # through all, through next, through all both
_WIZARD_FITS = {0: "close", 1: "normal", 2: "loose"}  # swWzdHoleScrewClearanceTypes_e
_TOLERANCE_TYPES = {  # swTolType_e; swTolNONE (0) is no tolerance
    1: "basic", 2: "bilateral", 3: "limit", 4: "symmetric", 5: "min", 6: "max",
    7: "fit", 8: "fit_with_tolerance", 9: "fit_tolerance_only", 10: "block", 11: "general",
}
_FIT_TYPES = {"fit", "fit_with_tolerance", "fit_tolerance_only"}
# ISO 261 coarse pitches: the Hole Wizard's ISO tapped holes cut the coarse thread unless the size says 'M6x0.75'
_ISO_COARSE_PITCH = {
    "M1.6": 0.35, "M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M3.5": 0.6, "M4": 0.7, "M5": 0.8, "M6": 1.0, "M8": 1.25,
    "M10": 1.5, "M12": 1.75, "M14": 2.0, "M16": 2.0, "M18": 2.5, "M20": 2.5, "M22": 2.5, "M24": 3.0, "M27": 3.0,
    "M30": 3.5, "M33": 3.5, "M36": 4.0, "M42": 4.5, "M48": 5.0,
}
DIRECTIONS = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}
_SURFACE_KINDS = {4001: "plane", 4002: "cylinder", 4003: "cone", 4004: "sphere", 4005: "torus"}  # ISurface.Identity


# --- vectors ------------------------------------------------------------------

def r4(value) -> float:
    """Rounded to 4 decimals, never -0.0."""
    return round(float(value), 4) + 0.0


def _vec(vector, digits: int = 4) -> list:
    return [round(float(c), digits) + 0.0 for c in vector]


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> list:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _sub(a, b) -> list:
    return [x - y for x, y in zip(a, b)]


def _unit(vector):
    length = math.sqrt(_dot(vector, vector))
    return [c / length for c in vector] if length > 1e-12 else None


def snap_direction(vector, tol_deg: float = SNAP_DEG):
    """'+z' when the vector lies within tol_deg of an axis, else it as a rounded unit vector."""
    unit = _unit(vector)
    if unit is None:
        return None
    for name, axis in DIRECTIONS.items():
        if _dot(unit, axis) >= math.cos(math.radians(tol_deg)):
            return name
    return _vec(unit)


def snap_axis(vector, tol_deg: float = SNAP_DEG):
    """'x', 'y' or 'z' for a line along that axis in either sense, else None."""
    unit = _unit(vector)
    if unit is None:
        return None
    for name, axis in (("x", (1.0, 0.0, 0.0)), ("y", (0.0, 1.0, 0.0)), ("z", (0.0, 0.0, 1.0))):
        if abs(_dot(unit, axis)) >= math.cos(math.radians(tol_deg)):
            return name
    return None


def _direction_order(direction) -> tuple:
    """Axis names first in a fixed order, then vectors."""
    names = list(DIRECTIONS)
    return (names.index(direction), []) if isinstance(direction, str) else (len(names), direction)


# --- arguments ----------------------------------------------------------------

def parse_sections(sections, wall_mm, pull, clearance_pairs=None) -> list:
    """The sections to read, in output order, with 'core' always first.

    None reads every part section; walls and draft then join only when
    wall_mm and pull are given. Named sections are refused when unknown, when
    walls lacks wall_mm or draft lacks pull, and an argument without its
    section is refused rather than ignored.
    """
    if sections is None:
        chosen = [s for s in PART_SECTIONS if (s != "walls" or wall_mm is not None) and (s != "draft" or pull is not None)]
    else:
        names = [sections] if isinstance(sections, str) else list(sections)
        names = [str(name).strip().lower() for name in names]
        unknown = [name for name in names if name not in SECTIONS]
        if unknown:
            raise SolidWorksError(f"Unknown section{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}. "
                                  f"Sections: {', '.join(SECTIONS)}.")
        chosen = [s for s in SECTIONS if s == "core" or s in names]
    if "assembly" in chosen:
        raise SolidWorksError("The assembly section is not available yet (planned for sw-mcp 0.3.0); "
                              "read a part, or use list_components and check_interference.")
    if clearance_pairs:
        raise SolidWorksError("clearance_pairs goes with the assembly section.")
    if "walls" in chosen and wall_mm is None:
        raise SolidWorksError("The walls section needs wall_mm, the thickness to measure against (e.g. wall_mm=1.0).")
    if wall_mm is not None:
        if "walls" not in chosen:
            raise SolidWorksError("wall_mm goes with the walls section.")
        if wall_mm <= 0:
            raise SolidWorksError(f"wall_mm must be > 0 (got {wall_mm}).")
    if "draft" in chosen and pull is None:
        raise SolidWorksError("The draft section needs pull, the direction the mould opens: '+z', '-y', ...")
    if pull is not None:
        if "draft" not in chosen:
            raise SolidWorksError("pull goes with the draft section.")
        if str(pull).strip().lower() not in DIRECTIONS:
            raise SolidWorksError(f"Unknown pull '{pull}'. Use +x/-x/+y/-y/+z/-z.")
    return chosen


def cad_version(revision) -> str:
    """'34.4.1' -> 'SOLIDWORKS 2026 SP4.1'; the major revision counts from 1992."""
    parts = str(revision).split(".")
    if not parts[0].isdigit():
        return f"SOLIDWORKS {revision}"
    sp = ".".join(parts[1:3]) if len(parts) > 2 and parts[2] not in ("", "0") else (parts[1] if len(parts) > 1 else "0")
    return f"SOLIDWORKS {1992 + int(parts[0])} SP{sp}"


def thread_designation(size, standard) -> str | None:
    """'M6' -> 'M6x1.0' for an ISO coarse thread; a size with its pitch stays as it is."""
    if not size:
        return None
    size = str(size).strip()
    pitch = _ISO_COARSE_PITCH.get(size) if str(standard or "").upper() == "ISO" else None
    if pitch is None:
        return size
    text = f"{pitch:g}"
    return f"{size}x{text if '.' in text else text + '.0'}"


# --- geometry -----------------------------------------------------------------

def axis_line(point, direction) -> tuple:
    """(unit direction in a fixed sense, the line's point nearest the origin).

    The sense is the one whose first clear component is positive, so the same
    line read from two faces comes out the same.
    """
    unit = _unit(direction)
    for c in unit:
        if abs(c) > 1e-9:
            if c < 0:
                unit = [-v for v in unit]
            break
    along = _dot(point, unit)
    return unit, [p - along * u for p, u in zip(point, unit)]


def same_axis_line(a, b, tol_mm: float = AXIS_TOL_MM, tol_rad: float = AXIS_TOL_RAD) -> bool:
    """Do two (direction, point) lines coincide, in either sense?"""
    (da, pa), (db, pb) = a, b
    if abs(_dot(da, db)) < math.cos(tol_rad):
        return False
    offset = _sub(pb, pa)
    along = _dot(offset, da)
    return math.dist(offset, [along * c for c in da]) <= tol_mm


def group_coaxial(lines) -> list:
    """Indices of `lines` (direction, point) grouped by the axis line they share, in first-seen order."""
    groups = []
    for i, line in enumerate(lines):
        for group in groups:
            if same_axis_line(lines[group[0]], line):
                group.append(i)
                break
        else:
            groups.append([i])
    return groups


def material_side(normal, radial) -> str | None:
    """'hole' when the outward normal points back at the axis (the material
    is outside the round face), 'boss' when it points away; None when the
    normal is too far off radial to tell."""
    r = _unit(radial)
    n = _unit(normal)
    if r is None or n is None:
        return None
    cos = _dot(n, r)
    if abs(cos) < SIDE_MIN_COS:
        return None
    return "hole" if cos < 0 else "boss"


def edge_convexity(n1, n2, tangent, tangent_deg: float = TANGENT_DEG) -> str:
    """'convex', 'concave' or 'tangent' for an edge between faces with outward
    normals n1 and n2 at a point, its tangent in the sense of face 1's loop;
    'unknown' when a normal could not be read."""
    n1, n2 = _unit(n1), _unit(n2)
    if n1 is None or n2 is None or _unit(tangent) is None:
        return "unknown"
    if _dot(n1, n2) >= math.cos(math.radians(tangent_deg)):
        return "tangent"
    return "convex" if _dot(_cross(n1, n2), tangent) > 0 else "concave"


def draft_angle_deg(normal, pull, skip_deg: float = SNAP_DEG):
    """A face's draft against the pull, 90 - angle(normal, pull) in degrees:
    0 for a wall along the pull, negative for one leaning back. None for a
    face square to the pull (a top or a floor), which needs no draft."""
    unit = _unit(normal)
    cos = max(-1.0, min(1.0, _dot(unit, pull)))
    if abs(cos) >= math.cos(math.radians(skip_deg)):
        return None
    return 90.0 - math.degrees(math.acos(cos))


def arc_span_deg(angles_rad) -> float:
    """How far round the axis points at these angles reach: 360 less the widest gap between them."""
    if len(angles_rad) < 2:
        return 0.0
    ordered = sorted(a % (2 * math.pi) for a in angles_rad)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])] + [ordered[0] + 2 * math.pi - ordered[-1]]
    return 360.0 - math.degrees(max(gaps))


def _frame(axis) -> tuple:
    """Two unit vectors square to the axis and each other."""
    helper = [1.0, 0.0, 0.0] if abs(axis[0]) < 0.9 else [0.0, 1.0, 0.0]
    u = _unit(_cross(axis, helper))
    return u, _cross(axis, u)


def revolved_extent(points, line) -> dict:
    """Where points of a round face lie about the (direction, foot) line:
    along-axis range, radius at each end, and the arc they span."""
    axis, foot = line
    u, v = _frame(axis)
    rows = []
    for p in points:
        offset = _sub(p, foot)
        s = _dot(offset, axis)
        radial = [o - s * a for o, a in zip(offset, axis)]
        rows.append((s, math.sqrt(_dot(radial, radial)), math.atan2(_dot(radial, v), _dot(radial, u))))
    low, high = min(rows), max(rows)
    return {"s": [low[0], high[0]], "rho": [low[1], high[1]], "rho_max": max(r[1] for r in rows),
            "rho_min": min(r[1] for r in rows), "span_deg": arc_span_deg([r[2] for r in rows if r[1] > 1e-6])}


def merge_normals(entries, tol_deg: float = SNAP_DEG) -> list:
    """Planar faces (normal, area) merged by normal within tol_deg: normal (snapped
    where it lies on an axis), count and area, the largest area first."""
    merged = []
    for normal, area in entries:
        for group in merged:
            if _dot(group["unit"], normal) >= math.cos(math.radians(tol_deg)):
                group["count"] += 1
                group["area"] += area
                break
        else:
            merged.append({"unit": _unit(normal), "count": 1, "area": area})
    out = []
    for group in merged:
        snapped = snap_direction(group["unit"])
        normal = list(DIRECTIONS[snapped]) if isinstance(snapped, str) else snapped
        out.append({"normal": _vec(normal), "direction": snapped, "count": group["count"], "area_mm2": r4(group["area"])})
    out.sort(key=lambda g: (-g["area_mm2"], _direction_order(g["direction"])))
    return out


def setup_directions(planar, entrances) -> list:
    """The distinct outward directions of planar faces (largest area first) and of hole entrances."""
    found = [group["direction"] for group in planar]
    for entrance in entrances:
        if entrance is not None and entrance not in found:
            found.append(entrance)
    return found


# --- holes --------------------------------------------------------------------

def face_line(face) -> tuple:
    return axis_line(face["axis_point"], face["axis"])


def round_side(face) -> str | None:
    """'hole' or 'boss' for a round face, from its outward normal at a point on it."""
    probe = face.get("probe")
    if not probe:
        return None
    point, normal = probe
    axis, foot = face_line(face)
    offset = _sub(point, foot)
    along = _dot(offset, axis)
    if face["kind"] == "torus":  # radial from the tube's centre circle, not the axis
        radial = [o - along * a for o, a in zip(offset, axis)]
        to_tube = _unit(radial)
        if to_tube is None:
            return None
        centre = [f + along * a + face["radius"] * t for f, a, t in zip(foot, axis, to_tube)]
        return material_side(normal, _sub(point, centre))
    return material_side(normal, [o - along * a for o, a in zip(offset, axis)])


def adjacency(faces, edges) -> list:
    """For each face, (edge index, the face on the other side) of each of its edges."""
    around = [[] for _ in faces]
    for k, edge in enumerate(edges):
        i, j = edge["faces"]
        around[i].append((k, j))
        around[j].append((k, i))
    return around


def find_hole_stacks(faces, edges) -> list:
    """The holes of one body: each a list of face indices on one axis line.

    A hole starts at a cylinder whose material lies outside it and that runs
    round at least HOLE_MIN_ARC_DEG. Its stack grows through the faces next to
    it on the same axis line: more such cylinders, cones with the material
    outside (a countersink, a drill point), and planes square to the axis
    whose neighbours all belong to the stack (a counterbore's step, a floor).
    """
    around = adjacency(faces, edges)
    lines = {i: face_line(f) for i, f in enumerate(faces) if f["kind"] in ("cylinder", "cone")}
    sides = {i: round_side(faces[i]) for i in lines}

    def round_member(i, line) -> bool:
        return i in lines and sides[i] == "hole" and same_axis_line(lines[i], line)

    # a cylinder can come in pieces (two halves in an imported body): pieces
    # that touch, on one axis line with one radius, count round together
    concave = [i for i in lines if faces[i]["kind"] == "cylinder" and sides[i] == "hole"]
    piece_of = {i: i for i in concave}

    def root(i):
        while piece_of[i] != i:
            i = piece_of[i]
        return i

    for i in concave:
        for _, j in around[i]:
            if j in piece_of and same_axis_line(lines[i], lines[j]) \
                    and abs(faces[i]["radius"] - faces[j]["radius"]) <= AXIS_TOL_MM:
                piece_of[root(j)] = root(i)
    points = {}
    for i in concave:
        points.setdefault(root(i), []).extend(faces[i]["points"])
    span = {r: revolved_extent(p, lines[r])["span_deg"] for r, p in points.items()}

    def barrel(i) -> bool:
        return i in piece_of and span[root(i)] >= HOLE_MIN_ARC_DEG

    def inside(i, line) -> bool:  # a round face of the hole: a bore or a cone
        return round_member(i, line) and (faces[i]["kind"] == "cone" or barrel(i))

    stacks, taken = [], set()
    for seed in sorted(i for i in lines if barrel(i)):
        if seed in taken:
            continue
        line = lines[seed]
        stack, queue = {seed}, [seed]
        while queue:
            current = queue.pop()
            for _, other in around[current]:
                if other in stack:
                    continue
                face = faces[other]
                if face["kind"] == "plane":  # a step or a floor: square to the axis, bordering only the hole
                    member = (abs(_dot(face["normal"], line[0])) >= math.cos(math.radians(SNAP_DEG))
                              and all(inside(n, line) for _, n in around[other]))
                else:
                    member = inside(other, line)
                if member:
                    stack.add(other)
                    queue.append(other)
        taken |= stack
        stacks.append(sorted(stack))
    return stacks


def classify_stack(faces, edges, stack) -> dict | None:
    """A hole's shape from its stack of faces, in mm along its axis line.

    The end closed by a floor (a plane or a drill-point cone) makes it blind;
    the other end is the entrance. Through, the entrance is the wider end, or
    the end further along the axis line's fixed sense when both are alike.
    Returns None for a closed void (no open end).
    """
    around = adjacency(faces, edges)
    seed = next(i for i in stack if faces[i]["kind"] == "cylinder")
    line = face_line(faces[seed])
    axis, foot = line
    members = set(stack)
    rounds, planes = [], []
    for i in stack:
        face = faces[i]
        if face["kind"] == "plane":
            mids = [edges[k]["mid"] for k, _ in around[i]]
            planes.append({"face": i, "s": sum(_dot(_sub(m, foot), axis) for m in mids) / len(mids),
                           "disk": len({round(revolved_extent(faces[n]["points"], line)["rho_min"], 3)
                                        for _, n in around[i]}) == 1})
        else:
            rounds.append({"face": i, "kind": face["kind"], **revolved_extent(face["points"], line),
                           "neighbours": len({n for _, n in around[i]})})
    low = min(r["s"][0] for r in rounds)
    high = max(r["s"][1] for r in rounds)
    if high - low < 1e-6:
        return None
    end_tol = max(1e-3, 1e-4 * (high - low))

    def closed_at(s) -> bool:  # a cone with one neighbour closes at its apex: a drill point
        floor = any(p["disk"] and abs(p["s"] - s) <= end_tol for p in planes)
        point = any(r["kind"] == "cone" and r["neighbours"] == 1
                    and min(abs(r["s"][0] - s), abs(r["s"][1] - s)) <= end_tol for r in rounds)
        return floor or point

    closed_low, closed_high = closed_at(low), closed_at(high)
    if closed_low and closed_high:
        return None

    def radius_at(s) -> float:
        return max((r["rho_max"] for r in rounds if r["s"][0] - end_tol <= s <= r["s"][1] + end_tol), default=0.0)

    if closed_low or closed_high:
        entrance_high = closed_low
    else:
        wide_low, wide_high = radius_at(low), radius_at(high)
        entrance_high = not wide_low > wide_high + 1e-3

    def depth_of(s) -> float:  # from the entrance, into the material
        return high - s if entrance_high else s - low

    cylinders = [r for r in rounds if r["kind"] == "cylinder"]
    smallest = min(cylinders, key=lambda r: (round(r["rho_max"], 4), -max(depth_of(s) for s in r["s"])))
    radius = smallest["rho_max"]
    counterbore = countersink = None
    for r in rounds:
        near = min(depth_of(s) for s in r["s"])
        far = max(depth_of(s) for s in r["s"])
        if near > end_tol:
            continue
        if r["kind"] == "cylinder" and r["rho_max"] > radius + 1e-3:
            counterbore = {"diameter_mm": r4(2 * r["rho_max"]), "depth_mm": r4(far)}
        elif r["kind"] == "cone" and r["neighbours"] > 1:
            rise = abs(r["rho"][1] - r["rho"][0])
            countersink = {"diameter_mm": r4(2 * r["rho_max"]),
                           "angle_deg": r4(2 * math.degrees(math.atan2(rise, r["s"][1] - r["s"][0])))}
    through = not (closed_low or closed_high)
    depth = high - low if through else max(depth_of(s) for s in smallest["s"])
    # clean: a bore, plus a counterbore and its step, a countersink, a floor
    diameters = {round(r["rho_max"], 3) for r in cylinders}  # the pieces of one cylinder count once
    expected = 1 + (counterbore is not None) * 2 + (countersink is not None) + (not through)
    drill = [r for r in rounds if r["kind"] == "cone" and r["neighbours"] == 1]
    clean = len(diameters) + len(planes) + len(rounds) - len(cylinders) == expected \
        and len(diameters) == 1 + (counterbore is not None)
    middle = any(n not in members and end_tol < depth_of(_dot(_sub(edges[k]["mid"], foot), axis)) < depth - end_tol
                 for i in stack for k, n in around[i])  # another feature breaks into its side
    entrance_s = high if entrance_high else low
    drilling = [-c for c in axis] if entrance_high else list(axis)
    centre = [f + entrance_s * a for f, a in zip(foot, axis)]
    return {
        "kind": "counterbore" if counterbore else "countersink" if countersink else "simple",
        "diameter_mm": r4(2 * radius),
        "depth_mm": r4(depth),
        "through": through,
        "counterbore": counterbore,
        "countersink": countersink,
        "drill_point": bool(drill),
        "axis": _vec([c + 0.0 for c in drilling]),
        "entrance": snap_direction([-c for c in drilling]),
        "centre_mm": _vec(centre),
        "confidence": "high" if clean and not middle else "medium",
        "faces": sorted(stack),
        "smallest_face": smallest["face"],
    }


_GROUP_FIELDS = ("source", "confidence", "kind", "standard", "size", "fit", "diameter_mm", "depth_mm",
                 "through", "counterbore", "countersink", "thread", "axis", "entrance", "body")


def group_holes(holes) -> list:
    """Identical holes as one entry with `count` and `centres_mm`, also when
    separate features made them (four Hole Wizard holes, a hole and its
    pattern): `feature` is the first in the tree, `features` all of them.
    In feature-tree order (`order`), then by their first centre (x, y, z)."""
    groups = {}
    for hole in holes:
        key = repr([hole.get(f) for f in _GROUP_FIELDS])
        group = groups.setdefault(key, {**{f: hole.get(f) for f in _GROUP_FIELDS}, "made": {}, "centres_mm": []})
        group["made"][hole.get("feature")] = min(hole.get("order", 0), group["made"].get(hole.get("feature"), math.inf))
        group["centres_mm"].append(hole["centre_mm"])
    out = []
    for group in groups.values():
        made = sorted(group.pop("made").items(), key=lambda kv: (kv[1], str(kv[0])))
        group.update(order=made[0][1], feature=made[0][0], features=[name for name, _ in made])
        group["centres_mm"].sort()
        group["count"] = len(group["centres_mm"])
        out.append(group)
    out.sort(key=lambda g: (g["order"], g["centres_mm"][0]))
    return [{"feature": g["feature"], "features": g["features"], **{f: g[f] for f in _GROUP_FIELDS},
             "count": g["count"], "centres_mm": g["centres_mm"]} for g in out]


# --- radii and edges ----------------------------------------------------------

def internal_radii(faces, hole_faces) -> list:
    """Concave round faces that are not part of a hole (rounded pocket
    corners, fillets of inside edges), by radius and feature."""
    found = {}
    for i, face in enumerate(faces):
        if i in hole_faces or face["kind"] not in ("cylinder", "torus") or round_side(face) != "hole":
            continue
        radius = face["radius"] if face["kind"] == "cylinder" else face["minor_radius"]
        key = (face.get("feature_order", 0), face.get("feature"), round(radius, 4))
        found[key] = found.get(key, 0) + 1
    return [{"radius": r4(radius), "feature": feature, "count": count}
            for (_, feature, radius), count in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][2]))]


def sharp_internal_edges(faces, edges, hole_faces) -> list:
    """Concave edges between faces that meet at an angle (not a smooth
    fillet run-out), skipping those inside holes: each with the feature that
    made it (the later of its two faces'), its axis or None, and its length."""
    found = []
    for k, edge in enumerate(edges):
        i, j = edge["faces"]
        if i in hole_faces and j in hole_faces:
            continue
        if edge_convexity(*edge["normals"], edge["tangent"]) != "concave":
            continue
        later = max((faces[i], faces[j]), key=lambda f: f.get("feature_order", 0))
        found.append({"edge": k, "feature": later.get("feature"), "order": later.get("feature_order", 0),
                      "direction": snap_axis(edge["tangent"]) if edge["line"] else None,
                      "length_mm": None if edge["length"] is None else round(edge["length"], 3) + 0.0})
    return found


def group_edges(found) -> list:
    """Sharp edges grouped by feature, direction and length."""
    groups = {}
    for edge in found:
        key = (edge["order"], edge["feature"] or "", edge["direction"] or "~", edge["length_mm"])
        groups[key] = groups.get(key, 0) + 1
    return [{"feature": feature or None, "direction": None if direction == "~" else direction,
             "length_mm": length, "count": count}
            for (_, feature, direction, length), count in sorted(groups.items())]


# --- walls and draft ----------------------------------------------------------

def merge_wall_sides(spots) -> list:
    """Thin spots with the two sides of one flat wall kept once: a spot facing
    the other way, as thin, one thickness behind the first, is its far side."""
    kept = []
    for spot in sorted(spots, key=lambda s: (s["thickness_mm"], s["body"], s["face"])):
        twin = False
        for other in kept:
            if other["body"] != spot["body"] or spot.get("normal") is None or other.get("normal") is None:
                continue
            t = other["thickness_mm"]
            if abs(spot["thickness_mm"] - t) > max(1e-3, 0.02 * t) or _dot(spot["normal"], other["normal"]) > -0.999:
                continue
            if abs(_dot(_sub(spot["point_mm"], other["point_mm"]), other["normal"]) + t) <= max(0.05, 0.1 * t):
                twin = True
                break
        if not twin:
            kept.append(spot)
    return kept


def face_draft(normals, pull):
    """The worst draft of a face over its normals, or None when every normal
    is square to the pull."""
    drafts = [d for d in (draft_angle_deg(n, pull) for n in normals) if d is not None]
    return min(drafts) if drafts else None


def _com_key(obj) -> str:
    """The COM object behind a wrapper: its IUnknown pointer, which pywin32
    only shows in the repr ('<PyIUnknown at 0x... with obj at 0x...>')."""
    unknown = getattr(obj, "_oleobj_", obj).QueryInterface(pythoncom.IID_IUnknown)
    return repr(unknown).rsplit(" ", 1)[-1]


def _why(exc) -> str:
    if isinstance(exc, SolidWorksError):
        return str(exc)
    if isinstance(exc, pythoncom.com_error):
        info = getattr(exc, "excepinfo", None)
        return f"SolidWorks COM error: {(info[2] if info and len(info) > 2 and info[2] else None) or exc}"
    return f"{type(exc).__name__}: {exc}"


class _Reading:
    """What one read_model call has read so far, so sections share it."""

    def __init__(self):
        self.bodies = None     # [(name, IBody2)]
        self.tree = None       # [(IFeature, type, [(sub-feature, type)])], the whole tree in order
        self.history = None    # [(IFeature, type)] after the origin: what was modelled
        self.features = None   # {face key: (name, order, type, IFeature)}
        self.graphs = {}       # body index -> faces, edges, face objects
        self.triangles = {}    # body index -> [(face index, a, b, c, normal)]
        self.hole_faces = {}   # body index -> set of face indices in holes
        self.entrances = None  # hole entrance directions, for the setups
        self.mass = None       # get_mass_properties()
        self.alive = []        # COM objects keyed by pointer, held so no pointer is reused while reading
        self.wizards = {}      # Hole Wizard feature name -> _wizard_hole_info
        self.orders = {}       # pattern seed name -> tree order


class ModelReader:
    """Mixed into SolidWorksSession."""

    def read_model(self, sections: list | None = None, wall_mm: float | None = None, pull: str | None = None,
                   clearance_pairs: list | None = None) -> dict:
        """Everything a review reads of the current part, in one read-only call."""
        chosen = parse_sections(sections, wall_mm, pull, clearance_pairs)
        model = self._require_model()
        kind = int(model.GetType())
        path = model.GetPathName() or None
        title = re.sub(r"\.(sldprt|sldasm|slddrw)$", "", model.GetTitle(), flags=re.IGNORECASE)
        dirty = bool(model.GetSaveFlag())
        result = {
            "ok": True, "schema": SCHEMA, "server_version": __version__, "cad_version": cad_version(self._revision()),
            "sections": chosen,
            "document": {"type": {SW_DOC_ASSEMBLY: "assembly", SW_DOC_DRAWING: "drawing"}.get(kind, "part"),
                         "name": title, "path": path, "saved": bool(path) and not dirty},
            "parts": [], "assembly": None, "gaps": [], "timings_ms": {},
        }
        if kind != SW_DOC_PART:
            result["gaps"].append({"what": f"the {result['document']['type']} '{title}'",
                                   "why": "read_model reads parts; assemblies follow in sw-mcp 0.3.0, and drawings "
                                          "have their own tools (list_views, check_drawing).", "section": "core"})
            return result
        reading, part = _Reading(), {}
        for section in chosen:
            start = time.perf_counter()
            try:
                part.update(getattr(self, f"_read_{section}")(reading, result["gaps"], wall_mm=wall_mm, pull=pull))
            except Exception as exc:  # noqa: BLE001 - one section failing leaves the others
                part.update({field: None for field in SECTION_FIELDS[section]})
                result["gaps"].append({"what": f"the {section} section", "why": _why(exc), "section": section})
            result["timings_ms"][section] = round((time.perf_counter() - start) * 1000)
        if "core" in chosen and part.get("name") is None:
            part.update(name=title, path=path)
        result["parts"].append(part)
        if not dirty and model.GetSaveFlag():
            result["gaps"].append({"what": "the document's saved state", "section": "core",
                                   "why": "SolidWorks marked the part changed while it was read."})
        return result

    # --- shared reads ----------------------------------------------------------

    def _reading_bodies(self, reading) -> list:
        if reading.bodies is None:
            part = binding.wrap(self._model, self._mod.IPartDoc)
            bodies = part.GetBodies2(SW_BODY_SOLID, True) or ()
            if not isinstance(bodies, (list, tuple)):
                bodies = [bodies]
            reading.bodies = [(b.Name, b) for b in (binding.wrap(body, self._mod.IBody2) for body in bodies)]
        return reading.bodies

    def _reading_mass(self, reading) -> dict:
        if reading.mass is None:
            reading.mass = self.get_mass_properties()["mass_properties"]
        return reading.mass

    def _reading_tree(self, reading) -> list:
        """The feature tree walked once: each feature, its type and its sub-features."""
        if reading.tree is None:
            reading.tree, reading.history, after_origin = [], [], False
            feature_class = self._mod.IFeature

            def step(obj, name):  # raw and bare: a wrapped walk spent most of its time asking for interfaces
                return binding.bare(binding.invoke(obj, feature_class, name), feature_class)

            feat = binding.bare(binding.invoke(self._model, self._mod.IModelDoc2, "FirstFeature"), feature_class)
            while feat is not None:
                kind, subs = feat.GetTypeName2(), []
                sub = step(feat, "GetFirstSubFeature")
                while sub is not None:
                    subs.append((sub, sub.GetTypeName2()))
                    sub = step(sub, "GetNextSubFeature")
                reading.tree.append((feat, kind, subs))
                if after_origin:
                    reading.history.append((feat, kind))
                elif kind == "OriginProfileFeature":
                    after_origin = True
                feat = step(feat, "GetNextFeature")
        return reading.tree

    def _reading_history(self, reading) -> list:
        self._reading_tree(reading)
        return reading.history

    def _reading_features(self, reading) -> dict:
        """Each face's feature (name, tree order, type, IFeature), by face key."""
        if reading.features is None:
            reading.features = {}
            for order, (feat, kind) in enumerate(self._reading_history(reading)):
                faces = binding.invoke(feat, self._mod.IFeature, "GetFaces") or ()
                reading.alive += faces  # a released face's pointer can come back as another face
                for face in faces:
                    reading.features[_com_key(face)] = (feat.Name, order, kind, feat)
        return reading.features

    def _reading_faces(self, reading, index: int) -> list:
        """A body's faces (IFace2), in its own order."""
        if index in reading.graphs:
            return reading.graphs[index]["objects"]
        body = self._reading_bodies(reading)[index][1]
        # bare: the same answers as wrapped faces (verified call by call), without 1.3 ms each to wrap
        return [binding.bare(f, self._mod.IFace2) for f in binding.invoke(body, self._mod.IBody2, "GetFaces") or ()]

    def _reading_graph(self, reading, index: int) -> dict:
        """One body's faces and edges as plain dicts (mm, outward unit normals)."""
        if index in reading.graphs:
            return reading.graphs[index]
        body = self._reading_bodies(reading)[index][1]
        features = self._reading_features(reading)
        objects = self._reading_faces(reading, index)
        keys = {_com_key(face): i for i, face in enumerate(objects)}
        faces, surfaces = [], []
        for i, face in enumerate(objects):
            feature = features.get(_com_key(face))
            entry = {"index": i, "feature": feature[0] if feature else None, "feature_order": feature[1] if feature else -1}
            normal = list(face.Normal)  # zero for a curved face
            surface = None
            if any(abs(c) > 1e-12 for c in normal):
                entry.update(kind="plane", normal=normal, area=face.GetArea() * 1e6)
            else:
                surface = binding.bare(binding.invoke(face, self._mod.IFace2, "GetSurface"), self._mod.ISurface)
                entry.update(self._round_face(face, surface))
            faces.append(entry)
            surfaces.append(surface)
        edges, curves = [], []
        for edge_dispatch in binding.invoke(body, self._mod.IBody2, "GetEdges") or ():
            found = self._edge_entry_for_reading(binding.bare(edge_dispatch, self._mod.IEdge), keys, faces, surfaces)
            if found is not None:
                edges.append(found[0])
                curves.append(found[1])
        reading.graphs[index] = {"faces": faces, "edges": edges, "objects": objects, "curves": curves}
        return reading.graphs[index]

    def _round_face(self, face, surface) -> dict:
        """A curved face's kind and axis, sample points on it and its outward normal at one."""
        kind = _SURFACE_KINDS.get(surface.Identity(), "other")
        entry = {"kind": kind, "sign": -1.0 if face.FaceInSurfaceSense() else 1.0}
        if kind == "cylinder":
            params = surface.CylinderParams  # axis point xyz, axis xyz (m), radius (m)
            entry.update(axis_point=[m_to_mm(c) for c in params[0:3]], axis=list(params[3:6]), radius=m_to_mm(params[6]))
        elif kind == "cone":
            params = surface.ConeParams  # apex-side point xyz, axis xyz, radius, half angle
            entry.update(axis_point=[m_to_mm(c) for c in params[0:3]], axis=list(params[3:6]))
        elif kind == "torus":
            params = surface.TorusParams  # centre xyz, axis xyz, major radius, minor radius (m)
            entry.update(axis_point=[m_to_mm(c) for c in params[0:3]], axis=list(params[3:6]),
                         radius=m_to_mm(params[6]), minor_radius=m_to_mm(params[7]))
        else:
            return entry
        corners = face.GetTessTriangles(True) or ()
        points = [[m_to_mm(c) for c in corners[k:k + 3]] for k in range(0, len(corners), 3)]
        stride = max(1, len(points) // 72)
        entry["points"] = points[::stride]
        if points:  # off the axis: at a cone's apex the surface has no normal
            axis, foot = face_line(entry)
            probe = max(points, key=lambda p: math.dist(p, [f + _dot(_sub(p, foot), axis) * a for f, a in zip(foot, axis)]))
            entry["probe"] = (probe, self._outward_normal(entry, surface, probe))
        return entry

    def _outward_normal(self, face: dict, surface, point_mm) -> list:
        """The face's outward unit normal at a point on it."""
        if face["kind"] == "plane":
            return face["normal"]
        if face["kind"] == "cylinder":  # the surface's normal points away from the axis
            axis, foot = face_line(face)
            offset = _sub(point_mm, foot)
            along = _dot(offset, axis)
            radial = _unit([o - along * a for o, a in zip(offset, axis)])
            if radial is not None:
                return [face["sign"] * c for c in radial]
        evaluated = surface.EvaluateAtPoint(*(mm_to_m(c) for c in point_mm))
        normal = _unit(list(evaluated[0:3])) if evaluated else None
        return [face["sign"] * c for c in normal] if normal else [0.0, 0.0, 0.0]  # unknown: tests say so

    def _edge_entry_for_reading(self, edge, keys, faces, surfaces) -> tuple | None:
        """(entry, curve) for an edge: its two faces, its midpoint, its tangent
        in the sense of the first face's loop, both faces' outward normals
        there and a straight edge's length; for a curved edge, the edge and its
        parameter range, to measure it only if it is reported (SolidWorks
        disconnects a curve object it handed out once the call is over)."""
        pair = binding.invoke(edge, self._mod.IEdge, "GetTwoAdjacentFaces2") or ()
        if len(pair) != 2 or None in pair:
            return None
        first, second = pair
        i, j = keys.get(_com_key(first)), keys.get(_com_key(second))
        if i is None or j is None:
            return None
        params = edge.GetCurveParams2()  # start xyz, end xyz (m), start and end parameter
        start, end = [m_to_mm(c) for c in params[0:3]], [m_to_mm(c) for c in params[3:6]]
        chord = [(a + b) / 2 for a, b in zip(start, end)]
        line = faces[i]["kind"] == "plane" and faces[j]["kind"] == "plane"  # two planes meet in a line
        curve, length = None, None
        if line:
            mid, tangent = chord, _unit(_sub(end, start))
        else:
            curve = binding.bare(binding.invoke(edge, self._mod.IEdge, "GetCurve"), self._mod.ICurve)
            evaluated = curve.Evaluate2((params[6] + params[7]) / 2, 1)  # point, first derivative
            mid = [m_to_mm(c) for c in evaluated[0:3]]
            tangent = _unit(list(evaluated[3:6]))
            if tangent is not None and edge.IsParamReversed():
                tangent = [-c for c in tangent]
            line = math.dist(start, end) > 1e-6 and math.dist(mid, chord) < 1e-6  # its middle on the chord
        if tangent is None:
            return None
        if line:
            length = math.dist(start, end)
        if not edge.EdgeInFaceSense(first):
            tangent = [-c for c in tangent]
        entry = {"faces": (i, j), "mid": mid, "tangent": tangent, "line": line, "length": length,
                 "normals": (self._outward_normal(faces[i], surfaces[i], mid),
                             self._outward_normal(faces[j], surfaces[j], mid))}
        return entry, (None if line else (edge, params[6], params[7]))

    def _reading_triangles(self, reading, index: int) -> list:
        if index not in reading.triangles:
            reading.triangles[index] = [(i, *tri) for i, face in enumerate(self._reading_faces(reading, index))
                                        for tri in self._face_triangles(face)]
        return reading.triangles[index]

    # --- sections ----------------------------------------------------------------

    def _read_core(self, reading, gaps, **_) -> dict:
        model = self._model
        props = self._reading_mass(reading)
        box = props.get("bounding_box_mm")
        bodies = []
        for name, body in self._reading_bodies(reading):
            bodies.append({"name": name, "volume_mm3": r4(body.GetMassProperties(1.0)[3] * 1e9),
                           "faces": int(body.GetFaceCount())})
        features, dimensions, seen = [], [], set()
        for feat, kind in self._reading_history(reading):
            code, warning = feat.GetErrorCode2()
            error = None
            if code:
                full = self._error_entry(code, warning)
                error = {"code": full["code"], "warning": full["warning"]}
            features.append({"name": feat.Name, "type": kind, "suppressed": self._is_suppressed(feat),
                             "error": error})
            for entry in self._dimension_entries(feat, tolerance=True):
                if entry["name"] not in seen:
                    seen.add(entry["name"])
                    dimensions.append({"id": entry["name"], "feature": entry["feature"], "value": r4(entry["value"]),
                                       "unit": entry["unit"], "driving": entry["driving"],
                                       "tolerance": entry["tolerance"]})
        equations = [{"name": e["name"], "value": r4(e["value"]),
                      "expression": e["equation"].split("=", 1)[1].strip() if "=" in e["equation"] else e["equation"],
                      "global": e["global"]}
                     for e in self._equation_entries()]
        configuration = binding.wrap(model.GetActiveConfiguration(), self._mod.IConfiguration)
        return {
            "name": re.sub(r"\.sldprt$", "", model.GetTitle(), flags=re.IGNORECASE),
            "path": model.GetPathName() or None,
            "configuration": configuration.Name if configuration is not None else None,
            "mass_kg": round(props["mass_kg"], 6) + 0.0,
            "volume_mm3": r4(props["volume_mm3"]),
            "surface_area_mm2": r4(props["surface_area_mm2"]),
            "density_kg_m3": r4(props["density_kg_m3"]),
            "centre_of_mass_mm": _vec(props["center_of_mass_mm"]),
            "bbox_mm": None if box is None else {"min": _vec(box["min_mm"]), "max": _vec(box["max_mm"]),
                                                 "size": _vec(box["size_mm"])},
            "bodies": bodies,
            "features": features,
            "dimensions": dimensions,
            "equations": equations,
            "under_defined_sketches": self._reading_under_defined(reading),
        }

    def _reading_under_defined(self, reading) -> list:
        """_under_defined_sketches from the tree already walked."""
        found, seen = [], set()
        for feat, kind, subs in self._reading_tree(reading):
            for candidate, candidate_kind in [(feat, kind), *subs]:
                if candidate_kind != "ProfileFeature" or candidate.Name in seen:
                    continue
                seen.add(candidate.Name)
                sketch = binding.invoke(candidate, self._mod.IFeature, "GetSpecificFeature2")
                status = binding.bare(sketch, self._mod.ISketch).GetConstrainedStatus()
                if status != SW_FULLY_CONSTRAINED:
                    found.append(f"{candidate.Name} ({SKETCH_STATUSES.get(status, f'status {status}')})")
        return found

    def _dimension_tolerance(self, dim, angular: bool):
        """A dimension's own tolerance (mm or degrees), or None when it has none.
        Read through IDimension itself: its IDimensionTolerance is one more new
        object per dimension, needed only for a fit's designation."""
        kind = _TOLERANCE_TYPES.get(int(dim.GetToleranceType()))
        if kind is None:
            return None
        scale = math.degrees if angular else m_to_mm
        fit = None
        if kind in _FIT_TYPES:
            tolerance = binding.wrap(dim.Tolerance, self._mod.IDimensionTolerance)
            fit = tolerance.GetHoleFitValue() or tolerance.GetShaftFitValue() or None
        plus = minus = None
        if kind not in ("basic", "fit", "general"):
            low, high = dim.GetToleranceValues()[:2]  # system units
            plus, minus = r4(scale(high)), r4(scale(low))
        return {"type": kind, "plus": plus, "minus": minus, "fit": fit}

    def _read_material(self, reading, gaps, **_) -> dict:
        part = binding.wrap(self._model, self._mod.IPartDoc)
        named = part.GetMaterialPropertyName2("")
        name, database = (named[0], named[1]) if isinstance(named, (list, tuple)) else (named, None)
        if not name:
            return {"material": None}
        props = self._reading_mass(reading)
        return {"material": {"name": name, "database": database or None, "density_kg_m3": r4(props["density_kg_m3"])}}

    def _read_holes(self, reading, gaps, **_) -> dict:
        features = self._reading_features(reading)
        holes = []
        for index, (body_name, _) in enumerate(self._reading_bodies(reading)):
            graph = self._reading_graph(reading, index)
            faces, edges, objects = graph["faces"], graph["edges"], graph["objects"]
            reading.hole_faces[index] = set()
            for stack in find_hole_stacks(faces, edges):
                found = classify_stack(faces, edges, stack)
                if found is None:
                    continue
                reading.hole_faces[index].update(stack)
                smallest = faces[found["smallest_face"]]
                hole = {"feature": smallest["feature"], "order": smallest["feature_order"], "source": "geometry",
                        "confidence": found["confidence"], "kind": found["kind"], "standard": None, "size": None,
                        "fit": None, "diameter_mm": found["diameter_mm"], "depth_mm": found["depth_mm"],
                        "through": found["through"], "counterbore": found["counterbore"],
                        "countersink": found["countersink"], "thread": None, "axis": found["axis"],
                        "entrance": found["entrance"], "body": body_name, "centre_mm": found["centre_mm"]}
                wizard = self._wizard_of(reading, objects[found["smallest_face"]], features)
                if wizard is not None:
                    if wizard[0] not in reading.wizards:  # once per feature, not per hole of a pattern
                        reading.wizards[wizard[0]] = self._wizard_hole_info(wizard[2])
                    self._apply_wizard(hole, found, wizard, reading.wizards[wizard[0]])
                holes.append(hole)
        self._apply_cosmetic_threads(reading, holes, gaps)
        reading.entrances = [h["entrance"] for h in holes]
        return {"holes": group_holes(holes)}

    def _wizard_of(self, reading, face, features) -> tuple | None:
        """(name, tree order, IFeature) of the Hole Wizard feature that made a hole's face, also through a pattern."""
        feature = features.get(_com_key(face))
        if feature is None:
            return None
        name, order, kind, feat = feature
        if kind in WIZARD_TYPES:
            return name, order, feat
        if kind in PATTERN_TYPES:
            seed = binding.bare(binding.invoke(face, self._mod.IFace2, "GetPatternSeedFeature"), self._mod.IFeature)
            if seed is not None and seed.GetTypeName2() in WIZARD_TYPES:
                seed_name = seed.Name
                seed_order = next((o for o, (f, _) in enumerate(self._reading_history(reading)) if f.Name == seed_name),
                                  order) if seed_name not in reading.orders else reading.orders[seed_name]
                reading.orders[seed_name] = seed_order
                return seed_name, seed_order, seed
        return None

    def _apply_wizard(self, hole: dict, found: dict, wizard: tuple, info: dict) -> None:
        name, order, _ = wizard
        hole.update(feature=name, order=order, source="wizard", confidence="high", kind=info["kind"],
                    standard=info["standard"], size=info["size"], fit=info.get("fit"))
        if info["kind"] in ("tapped", "pipe_tapped"):
            hole["diameter_mm"] = info["tap_drill_diameter_mm"] or found["diameter_mm"]
            hole["thread"] = {"designation": thread_designation(info["size"], info["standard"]),
                              "depth_mm": info["thread_depth_mm"] or None, "cosmetic": info["cosmetic_thread"]}
        else:
            hole["diameter_mm"] = info["diameter_mm"] or found["diameter_mm"]
        if info["kind"] == "counterbore":
            hole["counterbore"] = {"diameter_mm": info["cbore_diameter_mm"], "depth_mm": info["cbore_depth_mm"]}
            hole["countersink"] = None
        elif info["kind"] == "countersink":
            hole["countersink"] = {"diameter_mm": info["csink_diameter_mm"], "angle_deg": info["csink_angle_deg"]}
            hole["counterbore"] = None
        hole["through"] = info["end_condition"] in _THROUGH_END_CONDITIONS or found["through"]
        if not hole["through"] and info["depth_mm"]:
            hole["depth_mm"] = info["depth_mm"]

    def _apply_cosmetic_threads(self, reading, holes: list, gaps: list) -> None:
        """A cosmetic thread a person put on a hole (not a Hole Wizard's own) gives it its thread."""
        for feat, kind, subs in self._reading_tree(reading):
            for thread, thread_kind in [(feat, kind), *subs]:
                if thread_kind != "CosmeticThread" or kind in WIZARD_TYPES:
                    continue
                if self._is_suppressed(thread):
                    continue
                try:
                    found = self._cosmetic_thread(thread)
                except (SolidWorksError, pythoncom.com_error) as exc:
                    gaps.append({"what": f"cosmetic thread '{thread.Name}'", "why": _why(exc), "section": "holes"})
                    continue
                if found is None:
                    continue
                line, radius, entry = found
                for hole in holes:
                    if hole["thread"] is None and same_axis_line(axis_line(hole["centre_mm"], hole["axis"]), line, 1e-2, 1e-3) \
                            and abs(hole["diameter_mm"] / 2 - radius) < 1.5 * radius:
                        hole["thread"] = entry
                        if hole["kind"] == "simple":
                            hole["kind"] = "tapped"
                        break

    def _cosmetic_thread(self, thread) -> tuple | None:
        """(axis line, radius of the edge, thread entry) of a cosmetic thread feature."""
        data = binding.wrap(thread.GetDefinition(), self._mod.ICosmeticThreadFeatureData)
        if data is None or not data.AccessSelections(self._model, None):
            raise SolidWorksError("could not read which edge it is on")
        try:
            edge = binding.wrap(data.Edge, self._mod.IEdge)
            curve = binding.wrap(edge.GetCurve(), self._mod.ICurve) if edge is not None else None
            if curve is None or not curve.IsCircle():
                return None
            params = curve.CircleParams  # centre xyz, axis xyz, radius (m)
            depth = None if data.EndCondition == SW_END_COND_THROUGH_ALL else r4(m_to_mm(data.BlindDepth))
            designation = (data.Size or data.ThreadCallout or "").strip() or None
            return (axis_line([m_to_mm(c) for c in params[0:3]], list(params[3:6])), m_to_mm(params[6]),
                    {"designation": designation, "depth_mm": depth, "cosmetic": True})
        finally:
            data.ReleaseSelectionAccess()

    def _read_radii(self, reading, gaps, **_) -> dict:
        radii, sharp = [], []
        for index, _ in enumerate(self._reading_bodies(reading)):
            graph = self._reading_graph(reading, index)
            holes = reading.hole_faces.get(index)
            if holes is None:
                holes = set()
                for stack in find_hole_stacks(graph["faces"], graph["edges"]):
                    if classify_stack(graph["faces"], graph["edges"], stack) is not None:
                        holes.update(stack)
            radii += internal_radii(graph["faces"], holes)
            for edge in sharp_internal_edges(graph["faces"], graph["edges"], holes):
                if edge["length_mm"] is None:  # curved: measured only now it is reported
                    owner, low, high = graph["curves"][edge["edge"]]
                    curve = binding.bare(binding.invoke(owner, self._mod.IEdge, "GetCurve"), self._mod.ICurve)
                    edge["length_mm"] = round(m_to_mm(curve.GetLength3(low, high)), 3) + 0.0
                sharp.append(edge)
        merged = {}
        for entry in radii:  # the same feature and radius in several bodies
            key = (entry["feature"], entry["radius"])
            merged[key] = {**entry, "count": merged.get(key, {"count": 0})["count"] + entry["count"]}
        groups = group_edges(sharp)
        if len(groups) > LIST_CAP:
            gaps.append({"what": "sharp internal edge groups", "section": "radii",
                         "why": f"kept the first {LIST_CAP} of {len(groups)} in feature order"})
        return {"internal_radii_mm": list(merged.values()),
                "sharp_internal_edges": {"count": len(sharp), "groups": groups[:LIST_CAP]}}

    def _read_setups(self, reading, gaps, **_) -> dict:
        planar = []
        for index, _ in enumerate(self._reading_bodies(reading)):
            planar += [(f["normal"], f["area"]) for f in self._reading_graph(reading, index)["faces"]
                       if f["kind"] == "plane"]
        if reading.entrances is None:  # the holes section was not asked for, or failed
            try:
                self._read_holes(reading, [])
            except Exception:  # noqa: BLE001 - the planar faces still give the setups
                reading.entrances = []
                gaps.append({"what": "hole entrances", "section": "setups",
                             "why": "the holes could not be read, so only planar faces give the setups"})
        normals = merge_normals(planar)
        return {"planar_face_normals": [{k: g[k] for k in ("normal", "count", "area_mm2")} for g in normals],
                "setup_directions": setup_directions(normals, reading.entrances)}

    def _read_walls(self, reading, gaps, wall_mm=None, **_) -> dict:
        spots, thinnest, measured = [], math.inf, False
        for index, (body_name, body) in enumerate(self._reading_bodies(reading)):
            triangles = self._reading_triangles(reading, index)
            if not triangles:
                gaps.append({"what": f"walls of body '{body_name}'", "why": "it has no tessellation", "section": "walls"})
                continue
            try:
                walls = self._thin_walls(body, triangles, wall_mm)
            except SolidWorksError as exc:
                gaps.append({"what": f"walls of body '{body_name}'", "why": str(exc), "section": "walls"})
                continue
            measured = True
            thinnest = min(thinnest, walls["thinnest_mm"])
            normals = {}
            for i, *_, normal in triangles:
                normals.setdefault(i, []).append(normal)
            for face in walls["faces"]:
                ns = normals.get(face["index"], [])
                flat = ns and all(_dot(n, ns[0]) > 0.99999 for n in ns)
                spots.append({"body": body_name, "face": face["index"], "point_mm": _vec(face["at_mm"]),
                              "thickness_mm": r4(face["thinnest_mm"]), "normal": ns[0] if flat else None})
        if not measured:
            raise SolidWorksError("No body's walls could be measured.")
        spots = merge_wall_sides(spots)
        if len(spots) > LIST_CAP:
            gaps.append({"what": "thin spots", "section": "walls", "why": f"kept the {LIST_CAP} thinnest of {len(spots)}"})
        return {"wall_check": {"threshold_mm": wall_mm, "min_found_mm": r4(thinnest),
                               "thin_spots": [{k: s[k] for k in ("body", "face", "point_mm", "thickness_mm")}
                                              for s in spots[:LIST_CAP]]}}

    def _read_draft(self, reading, gaps, pull=None, **_) -> dict:
        direction = DIRECTIONS[str(pull).strip().lower()]
        faces, all_triangles = [], []
        bodies = self._reading_bodies(reading)
        for index, (body_name, _) in enumerate(bodies):
            triangles = self._reading_triangles(reading, index)
            all_triangles.append(triangles)
            normals, areas = {}, {}
            for i, a, b, c, normal in triangles:
                normals.setdefault(i, []).append(normal)
                areas[i] = areas.get(i, 0.0) + self._triangle_area(a, b, c)
            for i in sorted(normals):
                draft = face_draft(normals[i], direction)
                if draft is not None:
                    faces.append({"body": body_name, "face": i, "draft_deg": r4(draft), "area_mm2": r4(areas[i])})
        faces.sort(key=lambda f: (f["draft_deg"], -f["area_mm2"], f["body"], f["face"]))
        if len(faces) > LIST_CAP:
            gaps.append({"what": "draft faces", "section": "draft", "why": f"kept the {LIST_CAP} worst of {len(faces)}"})
        undercuts = self._undercut_faces([b for _, b in bodies], [n for n, _ in bodies], all_triangles, direction)
        if len(undercuts) > LIST_CAP:
            gaps.append({"what": "undercut faces", "section": "draft",
                         "why": f"kept the {LIST_CAP} largest of {len(undercuts)}"})
        return {"draft_check": {"pull": str(pull).strip().lower(),
                                "min_draft_deg": min((f["draft_deg"] for f in faces), default=None),
                                "faces_below_1deg": sum(1 for f in faces if f["draft_deg"] < 1.0),
                                "faces": faces[:LIST_CAP], "undercut_faces": undercuts[:LIST_CAP]}}

    def _undercut_faces(self, bodies, names, triangles_by_body, pull) -> list:
        """Faces a straight pull cannot clear: from points on a face that faces
        along +pull (or -pull), a ray that way runs into material. Up to
        UNDERCUT_RAYS_PER_FACE rays per face, from facets spread over it; the
        shadowed area is that share of the face's area facing the pull."""
        facing_area, facets = {}, {}  # (body name, face) -> area, [(start, direction)]
        for name, triangles in zip(names, triangles_by_body):
            for i, a, b, c, normal in triangles:
                facing = _dot(normal, pull)
                if abs(facing) < math.sin(math.radians(SNAP_DEG)):
                    continue  # a wall along the pull: the draft check's
                key = (name, i)
                facing_area[key] = facing_area.get(key, 0.0) + self._triangle_area(a, b, c)
                centre = [(p + q + r) / 3 for p, q, r in zip(a, b, c)]
                facets.setdefault(key, []).append(([p + 1e-3 * n for p, n in zip(centre, normal)],
                                                   list(pull) if facing > 0 else [-v for v in pull]))
        samples = []  # (key, start, direction)
        for key, rays in facets.items():
            stride = max(1, math.ceil(len(rays) / UNDERCUT_RAYS_PER_FACE))
            samples += [(key, *ray) for ray in rays[::stride]]
        if not samples:
            return []
        starts = [mm_to_m(c) for _, start, _ in samples for c in start]
        directions = [c for _, _, way in samples for c in way]
        model = self._model
        hits = model.RayIntersections(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [b._oleobj_ for b in bodies]),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, starts),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, directions),
            SW_RAY_NORMALS_ENTRY_EXIT, 0.0, 0.0)
        points = model.GetRayIntersectionsPoints() if hits else ()
        shadowed = set()
        for h in range(hits or 0):
            row = points[h * RAY_HIT_WIDTH:(h + 1) * RAY_HIT_WIDTH]
            ray = int(row[1])
            # only a ray that enters material is blocked: one from a facet's
            # centre, a hair inside a convex face, first leaves it
            if not int(row[2]) & SW_RAY_HIT_EXIT and m_to_mm(math.dist(row[3:6], starts[3 * ray:3 * ray + 3])) > 1e-2:
                shadowed.add(ray)
        cast, blocked = {}, {}
        for ray, (key, _, _) in enumerate(samples):
            cast[key] = cast.get(key, 0) + 1
            blocked[key] = blocked.get(key, 0) + (ray in shadowed)
        out = [{"body": name, "face": face, "area_mm2": r4(facing_area[(name, face)] * blocked[(name, face)] / cast[(name, face)])}
               for (name, face) in cast if blocked[(name, face)]]
        out.sort(key=lambda f: (-f["area_mm2"], f["body"], f["face"]))
        return out

"""Read triangle meshes (STL, 3MF) and slice them into cross-sections.

For modelling a part that must mate with something that only exists as a mesh:
its sections are the profiles to build, and slicing your own part the same way
checks the result (see the guide). Pure Python, no SolidWorks.
"""

import math
import posixpath
import struct
import xml.etree.ElementTree as ET
import zipfile

from .errors import SolidWorksError

_KEPT = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}  # section axis -> the two coordinates of a section
_AXIS = {"x": 0, "y": 1, "z": 2}

# Only points this close to the line through their neighbours are dropped. A
# sliced mesh wall is often a micrometre off straight, and those points are real.
COLLINEAR_MM = 1e-4

_ROUND = 6  # decimals (mm) to match section points shared by neighbouring triangles

_3MF_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_3MF_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
_3MF_UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def load_mesh(path: str, frame: str = "object") -> list:
    """Triangles ((x, y, z) x 3, in mm) of an STL (binary or ASCII) or 3MF file.

    frame (3MF only): 'object' keeps the objects' own modelling coordinates;
    'build' also applies the build-item transforms (where a slicer project
    placed and turned them on the plate).
    """
    lower = str(path).lower()
    try:
        if lower.endswith(".3mf"):
            return _load_3mf(path, frame)
        if lower.endswith(".stl"):
            return _load_stl(path)
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, struct.error) as exc:
        raise SolidWorksError(f"Could not read the mesh '{path}': {exc}")
    raise SolidWorksError(f"Cannot read '{path}': give an .stl or .3mf mesh.")


def _load_stl(path) -> list:
    data = open(path, "rb").read()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * count:  # binary: the size gives it away (ASCII may start with 'solid' too)
            return [tuple(tuple(struct.unpack_from("<3f", data, 84 + 50 * k + 12 + 12 * j)) for j in range(3))
                    for k in range(count)]
    text = data.decode("ascii")
    vertices = [tuple(float(v) for v in line.split()[1:4]) for line in text.splitlines()
                if line.strip().startswith("vertex")]
    if not vertices or len(vertices) % 3:
        raise ValueError("no triangles found")
    return [tuple(vertices[i:i + 3]) for i in range(0, len(vertices), 3)]


def _parse_transform(text) -> tuple:
    values = tuple(float(v) for v in text.split()) if text else _IDENTITY
    if len(values) != 12:
        raise ValueError(f"bad 3MF transform '{text}'")
    return values


def _apply(m, p):
    """3MF transform: the point is a row vector times the 4x3 matrix."""
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9], x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def _compose(inner, outer):
    """The transform applying `inner` first, then `outer`."""
    return tuple(_apply(outer, (inner[i], inner[i + 1], inner[i + 2]))[j] - (outer[9 + j] if i < 9 else 0.0)
                 for i in (0, 3, 6, 9) for j in range(3))


def _load_3mf(path, frame: str) -> list:
    if frame not in ("object", "build"):
        raise SolidWorksError(f"Unknown frame '{frame}'. Use 'object' or 'build'.")
    archive = zipfile.ZipFile(path)
    models = {}

    def model(part):
        if part not in models:
            root = ET.fromstring(archive.read(part.lstrip("/")))
            scale = _3MF_UNITS[root.get("unit", "millimeter")]
            objects = {}
            for obj in root.iter(f"{_3MF_CORE}object"):
                mesh = obj.find(f"{_3MF_CORE}mesh")
                if mesh is not None:
                    verts = [(float(v.get("x")) * scale, float(v.get("y")) * scale, float(v.get("z")) * scale)
                             for v in mesh.iter(f"{_3MF_CORE}vertex")]
                    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
                            for t in mesh.iter(f"{_3MF_CORE}triangle")]
                    objects[obj.get("id")] = ("mesh", verts, tris)
                else:
                    comps = [(c.get(_3MF_PATH) or part, c.get("objectid"), _parse_transform(c.get("transform")))
                             for c in obj.iter(f"{_3MF_CORE}component")]
                    objects[obj.get("id")] = ("components", comps, scale)
            models[part] = (root, objects)
        return models[part]

    def expand(part, object_id, transform, out):
        kind, *body = model(part)[1][object_id]
        if kind == "mesh":
            verts, tris = body
            points = [_apply(transform, v) for v in verts]
            out.extend((points[a], points[b], points[c]) for a, b, c in tris)
        else:
            comps, scale = body
            for comp_part, comp_id, comp_transform in comps:
                scaled = comp_transform[:9] + tuple(t * scale for t in comp_transform[9:])
                expand(comp_part, comp_id, _compose(scaled, transform), out)

    root_part = "/3D/3dmodel.model"
    root, _ = model(root_part)
    scale = _3MF_UNITS[root.get("unit", "millimeter")]
    triangles = []
    for item in root.iter(f"{_3MF_CORE}item"):
        placed = _parse_transform(item.get("transform")) if frame == "build" else _IDENTITY
        placed = placed[:9] + tuple(t * scale for t in placed[9:])
        expand(item.get(_3MF_PATH) or root_part, item.get("objectid"), placed, triangles)
    if not triangles:
        raise ValueError("the 3MF has no triangles")
    return triangles


def _cross(p, q, a, c):
    """Where edge p-q crosses coordinate value c along axis a, as the full point.

    The edge is taken in a fixed vertex order, so two triangles sharing it give
    bit-identical points and the section closes.
    """
    if q < p:
        p, q = q, p
    t = (c - p[a]) / (q[a] - p[a])
    return tuple(p[i] + t * (q[i] - p[i]) for i in range(3))


def section(triangles, axis: str, height: float) -> list:
    """The closed loops where the plane `axis = height` cuts the mesh, largest first.

    Each loop is a list of (u, v) points (mm): (y, z) across x, (x, z) across y,
    (x, y) across z. A vertex exactly on the plane counts as above it.
    """
    key = str(axis).lower()
    if key not in _AXIS:
        raise SolidWorksError(f"Unknown axis '{axis}'. Use 'x', 'y' or 'z'.")
    a, (i, j) = _AXIS[key], _KEPT[key]
    neighbours = {}
    for tri in triangles:
        cuts = []
        for p, q in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            if (p[a] >= height) != (q[a] >= height):
                point = _cross(p, q, a, height)
                cuts.append((round(point[i], _ROUND), round(point[j], _ROUND)))
        if len(cuts) == 2 and cuts[0] != cuts[1]:
            neighbours.setdefault(cuts[0], []).append(cuts[1])
            neighbours.setdefault(cuts[1], []).append(cuts[0])
    loops, seen = [], set()
    for start in neighbours:
        if start in seen:
            continue
        loop, previous, current = [start], None, start
        seen.add(start)
        while True:
            ahead = [n for n in neighbours[current] if n != previous and n not in seen]
            if not ahead:
                break
            previous, current = current, ahead[0]
            seen.add(current)
            loop.append(current)
        if len(loop) > 2:
            loops.append(loop)
    return sorted(loops, key=lambda l: -area(l))


def area(loop) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]))) / 2


def extents(loop) -> tuple:
    us, vs = [p[0] for p in loop], [p[1] for p in loop]
    return min(us), max(us), min(vs), max(vs)


def compare_sections(reference, part) -> dict:
    """Pair each reference loop with the part loop nearest to it (by the centre of
    its extents) and report how they differ.

    A different area means a misread feature (a wall where the reference is
    hollow); equal areas with different extents mean a shifted frame.
    """
    def centre(loop):
        u0, u1, v0, v1 = extents(loop)
        return (u0 + u1) / 2, (v0 + v1) / 2

    free = list(range(len(part)))
    pairs = []
    for ref in reference:
        if not free:
            break
        k = min(free, key=lambda i: math.dist(centre(ref), centre(part[i])))
        free.remove(k)
        a_ref, a_part = area(ref), area(part[k])
        pairs.append({
            "reference_area_mm2": round(a_ref, 4),
            "part_area_mm2": round(a_part, 4),
            "area_diff_mm2": round(a_part - a_ref, 4),
            "max_extent_diff_mm": round(max(abs(r - p) for r, p in zip(extents(ref), extents(part[k]))), 4),
        })
    return {"pairs": pairs, "unmatched_reference": len(reference) - len(pairs), "unmatched_part": len(free)}


def _off_line(b, a, c) -> float:
    """Distance of b from the line through a and c."""
    dx, dy = c[0] - a[0], c[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.dist(a, b)
    return abs(dx * (b[1] - a[1]) - dy * (b[0] - a[0])) / length


def simplify(points, tolerance: float = COLLINEAR_MM) -> list:
    """The loop without points that lie (within tolerance) on the line through their neighbours."""
    pts = [tuple(p) for p in points]
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for k in range(len(pts)):
            if _off_line(pts[k], pts[k - 1], pts[(k + 1) % len(pts)]) < tolerance:
                del pts[k]
                changed = True
                break
    return pts

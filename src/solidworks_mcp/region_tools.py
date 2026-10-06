"""The outline of everything a set of sections covers, such as a part swept
through a joint's range, with a margin round it.

The sections are filled on a grid and joined there; the outline is traced
along the grid and smoothed back to within its size. Each grid row is a
Python int whose bits are the cells, so filling, joining and growing are
whole-row operations. Pure Python, no SolidWorks.
"""

import math

from .errors import SolidWorksError


def swept_outline(sections, tolerance_mm: float, margin_mm: float = 0.0) -> list:
    """The regions that `sections` cover, largest first, each as
    {"outline_mm": [(u, v), ...] counter-clockwise, "holes_mm": [[...]], "area_mm2"}.

    sections: a list of sections, each a list of closed loops (u, v) in mm, an
    outline and its holes (filled even-odd). The outline lies within about
    tolerance_mm of the true edge, margin_mm further out; a coarser tolerance
    gives fewer points (a bar swept 120 degrees: 31 at 0.2, 138 at 0.1).
    """
    if tolerance_mm <= 0:
        raise SolidWorksError(f"tolerance_mm must be > 0 (got {tolerance_mm}).")
    if margin_mm < 0:
        raise SolidWorksError(f"margin_mm must be >= 0 (got {margin_mm}).")
    resolution_mm = tolerance_mm / 2  # the grid; the outline then drops its steps within 1.5 cells
    points = [p for loops in sections for loop in loops for p in loop]
    if not points:
        raise SolidWorksError("There is no section to outline: nothing was cut.")
    pad = margin_mm + 2 * resolution_mm
    u0 = math.floor((min(p[0] for p in points) - pad) / resolution_mm) * resolution_mm
    v0 = math.floor((min(p[1] for p in points) - pad) / resolution_mm) * resolution_mm
    rows = math.ceil((max(p[1] for p in points) + pad - v0) / resolution_mm)
    grid = [0] * rows
    for loops in sections:
        _fill(grid, loops, u0, v0, resolution_mm)
    if margin_mm > 0:
        grid = _grow(grid, margin_mm / resolution_mm)
    loops = [[(u0 + c * resolution_mm, v0 + r * resolution_mm) for c, r in _smooth(loop, 1.5)]
             for loop in _trace(grid)]
    outers = [loop for loop in loops if _signed_area(loop) > 0]
    holes = [loop for loop in loops if _signed_area(loop) < 0]
    regions = []
    for outer in sorted(outers, key=lambda loop: -_signed_area(loop)):
        inside = [hole for hole in holes if _inside(hole[0], outer)]
        holes = [hole for hole in holes if hole not in inside]
        area = _signed_area(outer) + sum(_signed_area(hole) for hole in inside)
        regions.append({"outline_mm": [_rounded(p) for p in outer],
                        "holes_mm": [[_rounded(p) for p in hole] for hole in inside],
                        "area_mm2": round(area, 3)})
    return regions


def _rounded(point) -> tuple:
    return round(point[0], 4) + 0.0, round(point[1], 4) + 0.0


def _fill(grid, loops, u0, v0, step) -> None:
    """Set the cells whose centres lie inside the loops (even-odd)."""
    crossings = {}
    for loop in loops:
        for (ua, va), (ub, vb) in zip(loop, loop[1:] + loop[:1]):
            if va == vb:
                continue
            low, high = min(va, vb), max(va, vb)
            first = max(0, math.ceil((low - v0) / step - 0.5))
            for r in range(first, len(grid)):
                v = v0 + (r + 0.5) * step
                if v >= high:
                    break
                if v >= low:
                    crossings.setdefault(r, []).append(ua + (v - va) * (ub - ua) / (vb - va))
    for r, us in crossings.items():
        us.sort()
        for a, b in zip(us[0::2], us[1::2]):
            first, last = math.ceil((a - u0) / step - 0.5), math.ceil((b - u0) / step - 0.5)  # centres in [a, b)
            if last > first:
                grid[r] |= (1 << last) - (1 << first)


def _grow(grid, radius) -> list:
    """Every cell within `radius` cells of a set one, set too."""
    reach = math.floor(radius)
    grown = [0] * len(grid)
    for r, row in enumerate(grid):
        if not row:
            continue
        for dy in range(-reach, reach + 1):
            if 0 <= r + dy < len(grid):
                width = math.floor(math.sqrt(radius * radius - dy * dy))
                smeared = row
                for dx in range(1, width + 1):
                    smeared |= (row << dx) | (row >> dx)
                grown[r + dy] |= smeared
    return grown


def _bits(value):
    while value:
        low = value & -value
        yield low.bit_length() - 1
        value ^= low


def _trace(grid) -> list:
    """The boundaries of the set cells as loops of grid corners (column, row):
    outlines counter-clockwise, holes clockwise; cells that touch only at a
    corner stay apart."""
    starts = {}  # corner -> the edge directions leaving it, set cells on their left
    for r, row in enumerate(grid):
        below = grid[r - 1] if r else 0
        above = grid[r + 1] if r + 1 < len(grid) else 0
        for c in _bits(row & ~below):
            starts.setdefault((c, r), []).append((1, 0))
        for c in _bits(row & ~above):
            starts.setdefault((c + 1, r + 1), []).append((-1, 0))
        for c in _bits(row & ~(row >> 1)):  # the cell to the right is empty
            starts.setdefault((c + 1, r), []).append((0, 1))
        for c in _bits(row & ~(row << 1)):  # the cell to the left is empty
            starts.setdefault((c, r + 1), []).append((0, -1))
    loops = []
    while starts:
        first = next(iter(starts))
        start = starts[first][0]
        _take(starts, first, start)
        loop, direction = [first], start
        corner = (first[0] + start[0], first[1] + start[1])
        while True:
            options = starts.get(corner, [])
            if corner == first and _turn(direction, options + [start]) == start:
                break  # back on the edge it began with
            direction = _turn(direction, options)
            _take(starts, corner, direction)
            loop.append(corner)
            corner = (corner[0] + direction[0], corner[1] + direction[1])
        loops.append(_corners_only(loop))
    return loops


def _turn(direction, options):
    """The edge to go on with: at a corner two cells share only there, turn
    left, so the loop stays round the cell it came along."""
    if len(options) == 1:
        return options[0]
    left = (-direction[1], direction[0])
    return left if left in options else options[0]


def _take(starts, corner, direction) -> None:
    starts[corner].remove(direction)
    if not starts[corner]:
        del starts[corner]


def _corners_only(loop) -> list:
    """The loop without the points along its straight runs."""
    kept = []
    for k, p in enumerate(loop):
        a, b = loop[k - 1], loop[(k + 1) % len(loop)]
        if (p[0] - a[0], p[1] - a[1]) != (b[0] - p[0], b[1] - p[1]):
            kept.append(p)
    return kept


def _smooth(loop, tolerance) -> list:
    """Douglas-Peucker on a closed loop: drop the grid's steps, keep its shape
    within `tolerance` (in cells)."""
    if len(loop) < 4:
        return loop
    far = max(range(len(loop)), key=lambda k: math.dist(loop[0], loop[k]))
    first = _douglas_peucker(loop[:far + 1], tolerance)
    second = _douglas_peucker(loop[far:] + loop[:1], tolerance)
    return first[:-1] + second[:-1]


def _douglas_peucker(points, tolerance) -> list:
    keep = [True] + [False] * (len(points) - 2) + [True]
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        (ax, ay), (bx, by) = points[i], points[j]
        length = math.hypot(bx - ax, by - ay)
        worst, at = -1.0, i
        for k in range(i + 1, j):
            px, py = points[k]
            d = (abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / length) if length else math.hypot(px - ax, py - ay)
            if d > worst:
                worst, at = d, k
        if worst > tolerance:
            keep[at] = True
            stack += [(i, at), (at, j)]
    return [p for p, k in zip(points, keep) if k]


def _signed_area(loop) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1])) / 2


def _inside(point, loop) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside

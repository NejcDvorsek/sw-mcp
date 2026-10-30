"""Sheet geometry for drawings: sizes, scales, frames, placement and checks.

Pure Python, no SolidWorks, so all of it is unit-tested. Everything here works
in sheet millimetres with the origin at the sheet's bottom-left corner; a
rectangle is (x1, y1, x2, y2) with x1 <= x2 and y1 <= y2.
"""

import math
import re

from .errors import SolidWorksError

# ISO 216 sheets as SolidWorks' ISO sheet formats have them (A4 upright, the
# rest landscape), and their swDwgPaperSizes_e (= swDwgTemplates_e) values
SHEETS = {
    "A4": (210.0, 297.0, 7),
    "A3": (420.0, 297.0, 8),
    "A2": (594.0, 420.0, 9),
    "A1": (841.0, 594.0, 10),
    "A0": (1189.0, 841.0, 11),
}

# ISO 5455 scales, largest first, as (numerator, denominator)
STANDARD_SCALES = [(50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10),
                   (1, 20), (1, 50), (1, 100), (1, 200), (1, 500), (1, 1000)]

# Room left around a view for its dimensions, sheet mm: first row 10 mm off the
# outline, a second row 8 mm further, plus text
DIMENSION_MARGIN_MM = 22.0
FIRST_ROW_MM = 10.0
ROW_PITCH_MM = 8.0

# A character of SolidWorks' drawing font is about this wide per unit of text height
CHAR_WIDTH = 0.6

# Text tokens SolidWorks puts in dimension and callout text, and what they show
_TOKENS = {
    "<MOD-DIAM>": "⌀", "<HOLE-DEPTH>": "↧", "<HOLE-SPOT>": "⌴", "<HOLE-SINK>": "⌵",
    "<MOD-DEG>": "°", "<MOD-PM>": "±", "<MOD-RAD>": "R", "<MOD-SQUARE>": "□",
    "<DIM>": "", "<NUM_INST>": "#",
}


def sheet_size(sheet: str) -> tuple:
    """(width, height, paper enum) of an ISO sheet name."""
    key = str(sheet).strip().upper()
    if key not in SHEETS:
        raise SolidWorksError(f"Unknown sheet '{sheet}'. Use {', '.join(SHEETS)} (ISO; A4 upright, A3-A0 landscape).")
    return SHEETS[key]


def scale_text(scale) -> str:
    """(1, 2) -> '1:2'; a float 0.5 -> '1:2', 2.0 -> '2:1'."""
    if isinstance(scale, (int, float)):
        scale = as_ratio(scale)
    num, den = scale
    return f"{num:g}:{den:g}"


def as_ratio(value) -> tuple:
    """A scale given as '1:2', [1, 2] or 0.5, as (numerator, denominator)."""
    if isinstance(value, str):
        parts = value.replace("/", ":").split(":")
        if len(parts) != 2:
            raise SolidWorksError(f"A scale reads like '1:2' or '2:1' (got '{value}').")
        num, den = (float(p) for p in parts)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        num, den = (float(v) for v in value)
    elif isinstance(value, (int, float)):
        if value <= 0:
            raise SolidWorksError(f"A scale must be positive (got {value}).")
        num, den = (float(value), 1.0) if value >= 1 else (1.0, round(1.0 / value, 6))
    else:
        raise SolidWorksError(f"A scale reads like '1:2', [1, 2] or 0.5 (got {value!r}).")
    if num <= 0 or den <= 0:
        raise SolidWorksError(f"A scale must be positive (got {value!r}).")
    return (int(num) if num == int(num) else num, int(den) if den == int(den) else den)


def pick_scale(fits) -> tuple:
    """The largest standard scale for which fits(scale_decimal) is True.

    Falls back to the smallest standard scale, so a caller always gets one.
    """
    for num, den in STANDARD_SCALES:
        if fits(num / den):
            return num, den
    return STANDARD_SCALES[-1]


# --- rectangles -----------------------------------------------------------------

def rect(x1, y1, x2, y2) -> tuple:
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def width(r) -> float:
    return r[2] - r[0]


def height(r) -> float:
    return r[3] - r[1]


def center(r) -> tuple:
    return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)


def union(rects) -> tuple | None:
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def grow(r, by: float) -> tuple:
    return (r[0] - by, r[1] - by, r[2] + by, r[3] + by)


def overlap(a, b, tolerance: float = 0.0) -> bool:
    """True when the rectangles share more than `tolerance` mm both ways."""
    if a is None or b is None:
        return False
    return (min(a[2], b[2]) - max(a[0], b[0]) > tolerance
            and min(a[3], b[3]) - max(a[1], b[1]) > tolerance)


def inside(inner, outer, tolerance: float = 0.0) -> bool:
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def shift(r, dx: float, dy: float) -> tuple:
    return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)


def rounded(values, digits: int = 2) -> list:
    return [round(v, digits) + 0.0 for v in values]  # + 0.0 turns -0.0 into 0.0


# --- text -----------------------------------------------------------------------

def clean_text(raw: str) -> str:
    """Display text with SolidWorks' tokens turned into the symbols they show."""
    text = str(raw or "")
    for token, symbol in _TOKENS.items():
        text = text.replace(token, symbol)
    text = re.sub(r"<(?:[A-Z][A-Z0-9_-]*|hw-[^<>]*|FONT[^<>]*|/?[a-z])>", "", text)  # other formatting tokens
    return re.sub(r"[ \t]+", " ", text).strip()


def text_box(x: float, y: float, text_height: float, angle_rad: float, text: str) -> tuple:
    """Rectangle around one line of text whose baseline starts at (x, y),
    turned by angle_rad about that point (sheet mm). Its width is estimated
    from the character count."""
    length = max(len(clean_text(text)), 1) * CHAR_WIDTH * text_height
    ux, uy = math.cos(angle_rad), math.sin(angle_rad)
    vx, vy = -uy, ux
    corners = [(x, y), (x + ux * length, y + uy * length),
               (x + vx * text_height, y + vy * text_height),
               (x + ux * length + vx * text_height, y + uy * length + vy * text_height)]
    return rect(min(c[0] for c in corners), min(c[1] for c in corners),
                max(c[0] for c in corners), max(c[1] for c in corners))


# --- sheet format -----------------------------------------------------------------

def frame_and_title_block(lines, sheet_w: float, sheet_h: float) -> tuple:
    """(frame, title_block) from the sheet format's straight lines.

    The frame is the innermost rectangle of border lines: its sides are the
    lines running along most of the sheet, and it spans what the shortest of
    them span (a title block's top line can run the frame's full width, so
    the frame is not read from where horizontal lines lie). The title block is
    everything drawn inside the frame, which on ISO formats is the block in
    the bottom right corner. Without a format: a 10 mm margin and no title
    block.
    """
    tol = 0.5
    horizontal = [l for l in lines if abs(l[1] - l[3]) < 1e-6 and abs(l[2] - l[0]) > 0.6 * sheet_w]
    vertical = [l for l in lines if abs(l[0] - l[2]) < 1e-6 and abs(l[3] - l[1]) > 0.6 * sheet_h]
    if not horizontal or not vertical:
        return (10.0, 10.0, sheet_w - 10.0, sheet_h - 10.0), None
    left = max(min(l[0], l[2]) for l in horizontal)
    right = min(max(l[0], l[2]) for l in horizontal)
    bottom = max(min(l[1], l[3]) for l in vertical)
    top = min(max(l[1], l[3]) for l in vertical)
    frame = (left, bottom, right, top)

    def on_frame(l):
        return ((abs(l[1] - l[3]) < 1e-6 and (abs(l[1] - bottom) < tol or abs(l[1] - top) < tol))
                or (abs(l[0] - l[2]) < 1e-6 and (abs(l[0] - left) < tol or abs(l[0] - right) < tol)))

    within = [l for l in lines if not on_frame(l)
              and inside(rect(*l), frame, tol) and math.hypot(l[2] - l[0], l[3] - l[1]) > 1.0]
    block = union([rect(*l) for l in within])
    return frame, block


def drawing_area(frame, title_block) -> list:
    """Free rectangles of the frame around the title block: the band above it
    and the band left of it (they overlap in the top left)."""
    if title_block is None:
        return [frame]
    areas = []
    if frame[3] - title_block[3] > 1.0:
        areas.append((frame[0], title_block[3], frame[2], frame[3]))
    if title_block[0] - frame[0] > 1.0:
        areas.append((frame[0], frame[1], title_block[0], frame[3]))
    return areas or [frame]


# --- placement --------------------------------------------------------------------

SIDES = ("right", "left", "above", "below")


def place_beside(anchor, size, side: str, gap: float, align: str = "center") -> tuple:
    """Centre of a size = (w, h) rectangle put `gap` mm beside `anchor` on `side`,
    centred on it (align='center') or lined up with its start ('start')."""
    if side not in SIDES:
        raise SolidWorksError(f"Unknown side '{side}'. Use {', '.join(SIDES)}.")
    w, h = size
    cx, cy = center(anchor)
    if side == "right":
        x = anchor[2] + gap + w / 2.0
        return x, (cy if align == "center" else anchor[3] - h / 2.0)
    if side == "left":
        x = anchor[0] - gap - w / 2.0
        return x, (cy if align == "center" else anchor[3] - h / 2.0)
    if side == "above":
        y = anchor[3] + gap + h / 2.0
        return (cx if align == "center" else anchor[0] + w / 2.0), y
    y = anchor[1] - gap - h / 2.0
    return (cx if align == "center" else anchor[0] + w / 2.0), y


def place_clear(anchor, size, sides, gap: float, taken, areas, margin: float = DIMENSION_MARGIN_MM):
    """(side, centre) for a size = (w, h) box beside `anchor`: the first of
    `sides` where it fits one of the free areas and stays `margin` clear of
    the taken boxes, moved further out in 5 mm steps past what is in the way.
    None when no side has room."""
    for side in sides:
        extra = 0.0
        while extra < 1000.0:
            cx, cy = place_beside(anchor, size, side, gap + extra)
            box = (cx - size[0] / 2.0, cy - size[1] / 2.0, cx + size[0] / 2.0, cy + size[1] / 2.0)
            if not any(inside(box, a, 0.01) for a in areas) and not any(overlap(box, a) for a in areas):
                break  # left the sheet: this side has no room
            if any(inside(box, a, 0.01) for a in areas) and not any(overlap(grow(box, margin), t) for t in taken):
                return side, (cx, cy)
            extra += 5.0
    return None


def first_angle_layout(front, top, side, iso, areas, gap: float, margin: float):
    """Centres for the front, top (below it), side (right of it) and iso view
    boxes (w, h, sheet mm at the chosen scale) in the free areas, or None when
    they do not fit. The group sits top left; the iso view goes into whatever
    room is left, right of the side view or above the title block.

    margin is the room each view keeps around itself for dimensions, gap the
    extra space between neighbouring views.
    """
    full = union(areas)
    left = full[0] + margin
    top_edge = full[3] - margin
    fx = left + front[0] / 2.0
    fy = top_edge - front[1] / 2.0
    boxes = {"front": (fx, fy, front)}
    front_box = (fx - front[0] / 2.0, fy - front[1] / 2.0, fx + front[0] / 2.0, fy + front[1] / 2.0)
    if top is not None:
        boxes["top"] = (fx, front_box[1] - 2 * margin - gap - top[1] / 2.0, top)
    if side is not None:
        boxes["side"] = (front_box[2] + 2 * margin + gap + side[0] / 2.0, fy, side)
    placed = {k: (x - s[0] / 2.0, y - s[1] / 2.0, x + s[0] / 2.0, y + s[1] / 2.0) for k, (x, y, s) in boxes.items()}
    if not _fits(placed.values(), areas, margin):
        return None
    if iso is not None:
        spot = _free_spot(iso, list(placed.values()), areas, margin)
        if spot is None:
            return None
        placed["iso"] = spot
    return {k: center(r) for k, r in placed.items()}


def arrange_views(main, children, others, areas, margin: float = DIMENSION_MARGIN_MM):
    """Centres for a whole sheet of views, or None when they do not fit.

    main: (w, h) of the view the others are projected from, put top left;
    children: [(key, side, (w, h))] projected or section views on `side` of it,
    stacked outward in the order given; others: [(key, (w, h))] (iso, details,
    views of their own) dropped into the room left. Every view keeps `margin`
    for dimensions all round, so neighbours sit 2 * margin apart.
    """
    full = union(areas)
    stacks = {side: [c for c in children if c[1] == side] for side in SIDES}
    span = {side: sum((s[0] if side in ("left", "right") else s[1]) + 2 * margin for _, _, s in stacks[side])
            for side in SIDES}
    mx = full[0] + margin + span["left"] + main[0] / 2.0
    my = full[3] - margin - span["above"] - main[1] / 2.0
    boxes = {"main": (mx - main[0] / 2.0, my - main[1] / 2.0, mx + main[0] / 2.0, my + main[1] / 2.0)}
    for side, stack in stacks.items():
        anchor = boxes["main"]
        for key, _, size in stack:
            cx, cy = place_beside(anchor, size, side, 2 * margin)
            anchor = (cx - size[0] / 2.0, cy - size[1] / 2.0, cx + size[0] / 2.0, cy + size[1] / 2.0)
            boxes[key] = anchor
    if not _fits(boxes.values(), areas, margin) or any(
            overlap(a, b) for i, a in enumerate(boxes.values()) for b in list(boxes.values())[i + 1:]):
        return None
    for key, size in others:
        spot = _free_spot(size, list(boxes.values()), areas, margin)
        if spot is None:
            return None
        boxes[key] = spot
    return {k: center(r) for k, r in boxes.items()}


def _fits(boxes, areas, margin) -> bool:
    return all(any(inside(grow(b, margin), a, 0.01) for a in areas) for b in boxes)


def _free_spot(size, taken, areas, margin):
    """A box of `size` inside one of the areas, clear of the taken boxes with
    their margins: tried from the top right corner, leftwards and down."""
    w, h = size
    step = 2.0
    for area in areas:
        y = area[3] - margin - h
        while y >= area[1] + margin - 1e-9:
            x = area[2] - margin - w
            while x >= area[0] + margin - 1e-9:
                box = (x, y, x + w, y + h)
                if not any(overlap(grow(box, margin), grow(t, margin)) for t in taken):
                    return box
                x -= step
            y -= step
    return None


def dimension_orientation(lines):
    """('horizontal' | 'vertical' | 'aligned', dimension line) of a linear
    dimension, from its two arrowheads (zero-length entries of `lines`): the
    dimension line runs from tip to tip. SolidWorks reports a vertical
    dimension between a line and a circle as plain 'linear', so its type does
    not tell. None without two arrowheads."""
    tips = [(l[0], l[1]) for l in lines if math.hypot(l[2] - l[0], l[3] - l[1]) <= 1e-9]
    if len(tips) < 2:
        return None
    (x1, y1), (x2, y2) = tips[0], tips[1]
    if abs(y2 - y1) < 1e-6 and abs(x2 - x1) > 1e-6:
        return "horizontal", (x1, y1, x2, y2)
    if abs(x2 - x1) < 1e-6 and abs(y2 - y1) > 1e-6:
        return "vertical", (x1, y1, x2, y2)
    return "aligned", (x1, y1, x2, y2)


def dimension_row(lines, geometry):
    """(side, offset, span) of a horizontal or vertical dimension: which side
    of the view's geometry box its dimension line runs on, how far off it, and
    the stretch it covers along that side. None for other dimensions, or one
    whose line runs over the geometry."""
    found = dimension_orientation(lines)
    if found is None or found[0] == "aligned":
        return None
    kind, line = found
    if kind == "horizontal":
        y, span = line[1], (min(line[0], line[2]), max(line[0], line[2]))
        if y < geometry[1]:
            return "below", geometry[1] - y, span
        if y > geometry[3]:
            return "above", y - geometry[3], span
        return None
    x, span = line[0], (min(line[1], line[3]), max(line[1], line[3]))
    if x < geometry[0]:
        return "left", geometry[0] - x, span
    if x > geometry[2]:
        return "right", x - geometry[2], span
    return None


def next_row(taken_rows, first: float = FIRST_ROW_MM, pitch: float = ROW_PITCH_MM) -> float:
    """Distance from a view's outline of the first free dimension row, given the
    distances already used on that side."""
    k = 0
    while any(abs(r - (first + k * pitch)) < pitch / 2.0 for r in taken_rows):
        k += 1
    return first + k * pitch


# --- references -------------------------------------------------------------------

_REF_KINDS = ("face", "edge", "hole", "feature", "point")


def parse_ref(ref) -> tuple:
    """A semantic reference into a view, as (kind, value):

    'edge:-x' / 'face:+z:inner' -> ('face', '-x' / '+z:inner'): the planar face
        of the part facing that way, seen edge-on in the view;
    'hole:M4_CBore_1'           -> ('hole', 'M4_CBore_1'): the hole feature's circle;
    'feature:CornerFillet'      -> ('feature', 'CornerFillet'): an edge of that feature;
    [x, y, z] or 'point:x,y,z'  -> ('point', (x, y, z)): the edge nearest that model point.
    """
    if isinstance(ref, (list, tuple)):
        if len(ref) != 3:
            raise SolidWorksError(f"A point reference is [x, y, z] in model mm (got {ref}).")
        return "point", tuple(float(c) for c in ref)
    text = str(ref or "").strip()
    kind, sep, value = text.partition(":")
    kind = kind.lower()
    if not sep or kind not in _REF_KINDS or not value.strip():
        raise SolidWorksError(
            f"Unknown reference '{ref}'. Use 'edge:-x' (the face facing -x, seen edge-on), "
            "'hole:<feature>', 'feature:<name>' or a model point [x, y, z]."
        )
    value = value.strip()
    if kind == "edge":
        kind = "face"
    if kind == "point":
        parts = value.replace(";", ",").split(",")
        if len(parts) != 3:
            raise SolidWorksError(f"A point reference reads 'point:x,y,z' in model mm (got '{ref}').")
        return "point", tuple(float(p) for p in parts)
    return kind, value


def count_prefix(count: int) -> str:
    """'4X ' for four identical features; empty for one."""
    return f"{count}X " if count > 1 else ""


# --- checks -----------------------------------------------------------------------

def segments_cross(a, b) -> bool:
    """True when segments a = (x1, y1, x2, y2) and b cross properly (touching
    at an end does not count: a leader ends on what it points at)."""
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    p1, p2, q1, q2 = a[:2], a[2:4], b[:2], b[2:4]
    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0


def find_problems(views, annotations, frame, title_block, min_text_mm: float = 2.5) -> list:
    """Layout problems of a sheet, each {'kind', 'items', 'message'}.

    views: [{'name', 'box'}] (the outline of each view's geometry);
    annotations: [{'id', 'type', 'box', 'text_boxes', 'leaders', 'value', 'axis',
    'text_height', 'view'}]; boxes are sheet mm rectangles.
    """
    problems = []

    def add(kind, items, message):
        problems.append({"kind": kind, "items": items, "message": message})

    for v in views:
        if v.get("box") is None:
            continue
        if not inside(v["box"], frame, 0.5):
            add("view_outside_frame", [v["name"]], f"View {v['name']} reaches outside the drawing frame.")
        if overlap(v["box"], title_block, 0.5):
            add("view_over_title_block", [v["name"]], f"View {v['name']} runs into the title block.")
    for i, a in enumerate(views):
        for b in views[i + 1:]:
            if overlap(a.get("box"), b.get("box"), 0.5):
                add("views_overlap", [a["name"], b["name"]], f"Views {a['name']} and {b['name']} overlap.")
    for a in annotations:
        box = a.get("box")
        if box is None:
            continue
        if not inside(box, frame, 0.5):
            add("outside_frame", [a["id"]], f"{a['id']} ({a.get('text', '')}) reaches outside the drawing frame.")
        if overlap(box, title_block, 0.5):
            add("over_title_block", [a["id"]], f"{a['id']} ({a.get('text', '')}) lies over the title block.")
        for v in views:
            if a.get("type") == "bend_note":
                break  # it belongs on its bend line, in the view
            if any(overlap(t, v.get("box"), 0.3) for t in a.get("text_boxes", ())):
                add("text_on_view", [a["id"], v["name"]],
                    f"The text of {a['id']} ({a.get('text', '')}) lies on the geometry of view {v['name']}.")
        if a.get("text_height") and a["text_height"] < min_text_mm - 1e-6:
            add("text_too_small", [a["id"]],
                f"{a['id']} uses {a['text_height']:g} mm text; ISO 3098 asks for at least {min_text_mm:g} mm.")
    for i, a in enumerate(annotations):
        for b in annotations[i + 1:]:
            if any(overlap(s, t, 0.2) for s in a.get("text_boxes", ()) for t in b.get("text_boxes", ())):
                add("texts_overlap", [a["id"], b["id"]],
                    f"The texts of {a['id']} ({a.get('text', '')}) and {b['id']} ({b.get('text', '')}) overlap.")
            b_segments = [*(b.get("leaders") or ()), *(b.get("lines") or ())]
            if any(segments_cross(l, m) for l in a.get("leaders") or () for m in b_segments) \
                    or any(segments_cross(l, m) for l in b.get("leaders") or () for m in a.get("lines") or ()):
                add("leaders_cross", [a["id"], b["id"]], f"The leaders of {a['id']} and {b['id']} cross.")
    problems += _duplicates(annotations)
    return problems


def _duplicates(annotations) -> list:
    seen = {}
    out = []
    for a in annotations:
        if a.get("type") != "dimension" or a.get("value") is None or a.get("axis") is None:
            continue
        key = (round(a["value"], 4), a.get("kind"), a["axis"])
        if key in seen:
            out.append({"kind": "duplicate_dimension", "items": [seen[key], a["id"]],
                        "message": f"{seen[key]} and {a['id']} both give {a['value']:g} mm along {a['axis']}: "
                                   "probably the same size twice."})
        else:
            seen[key] = a["id"]
    return out

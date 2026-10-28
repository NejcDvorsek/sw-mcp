"""Gear data for a drawing's gear data table: spur gears (ISO 21771 terms,
standard basic rack: addendum m, dedendum 1.25 m) and straight bevel gears
(ISO 23509 terms, equal addendum, constant clearance).

Pure Python, unit-tested. Lengths in mm, angles in degrees.
"""

import math

from .errors import SolidWorksError


def _positive(**values) -> None:
    for name, value in values.items():
        if value is None or value <= 0:
            raise SolidWorksError(f"{name} must be positive (got {value}).")


def involute(alpha_rad: float) -> float:
    return math.tan(alpha_rad) - alpha_rad


def dms(deg: float) -> str:
    """43.6 -> "43° 36' 00\"" (degrees, minutes, seconds)."""
    sign = "-" if deg < 0 else ""
    total = round(abs(deg) * 3600)
    d, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{sign}{d}° {m:02d}' {s:02d}\""


def spur(teeth: int, module: float, pressure_angle: float = 20.0, face_width: float | None = None,
         profile_shift: float = 0.0, mating_teeth: int | None = None) -> dict:
    """Data of a spur gear with the standard basic rack, and the span
    measurement over k teeth (base tangent length) that checks its thickness."""
    _positive(teeth=teeth, module=module, pressure_angle=pressure_angle)
    alpha = math.radians(pressure_angle)
    z, m, x = int(teeth), float(module), float(profile_shift)
    d = m * z
    k = max(2, round(z * pressure_angle / 180.0 + 0.5))
    span = m * math.cos(alpha) * (math.pi * (k - 0.5) + z * involute(alpha)) + 2 * x * m * math.sin(alpha)
    data = {
        "kind": "spur", "teeth": z, "module": m, "pressure_angle": pressure_angle, "profile_shift": x,
        "pitch_diameter": d, "tip_diameter": d + 2 * m * (1 + x), "root_diameter": d - 2 * m * (1.25 - x),
        "base_diameter": d * math.cos(alpha), "whole_depth": 2.25 * m,
        "span_teeth": k, "span": span,
    }
    if face_width is not None:
        data["face_width"] = float(face_width)
    if mating_teeth:
        data["mating_teeth"] = int(mating_teeth)
        data["center_distance"] = m * (z + int(mating_teeth)) / 2.0
    return data


def bevel(teeth: int, module: float, mating_teeth: int, pressure_angle: float = 20.0,
          face_width: float | None = None, shaft_angle: float = 90.0) -> dict:
    """Data of a straight bevel gear at the outer (heel) end: equal addendum
    m, dedendum 1.25 m, constant clearance (the face angle follows the mate's
    root angle), chordal tooth thickness and height on the back cone."""
    _positive(teeth=teeth, module=module, mating_teeth=mating_teeth, pressure_angle=pressure_angle,
              shaft_angle=shaft_angle)
    z1, z2, m = int(teeth), int(mating_teeth), float(module)
    sigma = math.radians(shaft_angle)
    delta = math.atan2(math.sin(sigma), z2 / z1 + math.cos(sigma))
    delta_mate = sigma - delta
    de = m * z1
    re = de / (2.0 * math.sin(delta))
    hae, hfe = m, 1.25 * m
    theta_f = math.atan(hfe / re)
    face = delta + theta_f   # constant clearance: the tip cone runs parallel to the mate's root cone
    root = delta - theta_f
    rv = de / (2.0 * math.cos(delta))  # back cone radius
    psi = (math.pi * m / 2.0) / (2.0 * rv)
    data = {
        "kind": "bevel", "teeth": z1, "module": m, "pressure_angle": pressure_angle, "mating_teeth": z2,
        "shaft_angle": shaft_angle, "pitch_angle": math.degrees(delta), "mating_pitch_angle": math.degrees(delta_mate),
        "face_angle": math.degrees(face), "root_angle": math.degrees(root), "outer_cone_distance": re,
        "pitch_diameter": de, "tip_diameter": de + 2 * hae * math.cos(delta),
        "addendum": hae, "dedendum": hfe, "whole_depth": hae + hfe,
        "chordal_thickness": 2.0 * rv * math.sin(psi), "chordal_height": hae + rv * (1.0 - math.cos(psi)),
        "crown_to_apex": re * math.cos(delta) - hae * math.sin(delta),
    }
    if face_width is not None:
        if face_width > re / 3.0 + 1e-9:
            raise SolidWorksError(f"A bevel gear's face width is at most a third of its cone distance "
                                  f"({re / 3.0:.2f} mm); got {face_width}.")
        data["face_width"] = float(face_width)
    return data


def table_rows(data: dict, accuracy: str | None = None, extra: list | None = None) -> list:
    """Rows [label, symbol, value] for a gear data table."""
    f = lambda v, n=3: f"{v:.{n}f}"  # noqa: E731
    rows = [[f"GEAR DATA - {'STRAIGHT BEVEL GEAR' if data['kind'] == 'bevel' else 'SPUR GEAR'}", "", ""],
            ["Number of teeth", "z", str(data["teeth"])],
            ["Module" + (" (outer)" if data["kind"] == "bevel" else ""), "m", f(data["module"], 3).rstrip("0").rstrip(".")],
            ["Pressure angle", "α", f"{data['pressure_angle']:g}°"]]
    if data["kind"] == "spur":
        rows += [["Profile shift coefficient", "x", f"{data['profile_shift']:+.3f}"],
                 ["Pitch diameter", "d", f(data["pitch_diameter"])],
                 ["Tip diameter", "da", f(data["tip_diameter"])],
                 ["Root diameter", "df", f(data["root_diameter"])],
                 ["Base diameter", "db", f(data["base_diameter"])],
                 ["Span over teeth", f"W{data['span_teeth']}", f(data["span"])]]
        if "face_width" in data:
            rows.append(["Face width", "b", f(data["face_width"], 2)])
        if "mating_teeth" in data:
            rows += [["Mating gear teeth", "z2", str(data["mating_teeth"])],
                     ["Centre distance", "a", f(data["center_distance"])]]
    else:
        rows += [["Shaft angle", "Σ", f"{data['shaft_angle']:g}°"],
                 ["Pitch angle", "δ", dms(data["pitch_angle"])],
                 ["Face angle", "δa", dms(data["face_angle"])],
                 ["Root angle", "δf", dms(data["root_angle"])],
                 ["Outer cone distance", "Re", f(data["outer_cone_distance"])],
                 ["Pitch diameter (outer)", "de", f(data["pitch_diameter"])],
                 ["Tip diameter (outer)", "dae", f(data["tip_diameter"])],
                 ["Addendum / dedendum", "hae / hfe", f"{f(data['addendum'])} / {f(data['dedendum'])}"],
                 ["Chordal tooth thickness", "sc", f(data["chordal_thickness"])],
                 ["Chordal height", "hc", f(data["chordal_height"])],
                 ["Crown to cone apex", "", f(data["crown_to_apex"])]]
        if "face_width" in data:
            rows.append(["Face width", "b", f(data["face_width"], 2)])
        rows.append(["Mating gear teeth", "z2", str(data["mating_teeth"])])
    if accuracy:
        rows.append(["Accuracy grade", "", str(accuracy)])
    for row in extra or ():
        if not isinstance(row, (list, tuple)) or len(row) not in (2, 3):
            raise SolidWorksError(f"An extra row is [label, value] or [label, symbol, value] (got {row}).")
        label, symbol, value = (row[0], "", row[1]) if len(row) == 2 else row
        rows.append([str(label), str(symbol), str(value)])
    return rows

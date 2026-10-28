"""Reference axes anywhere, planes by point and normal, lofts between named
sketches, and run_script: the modelling gaps found building a bevel gear pair.

Mixed into SolidWorksSession, so every method runs on the COM worker thread.

API facts this depends on (SOLIDWORKS 2026 SP4.1):
- A line in a 3D sketch, fixed in place, carries an axis or a plane anywhere:
  IModelDoc2.InsertAxis2 with the line selected lies on it; InsertRefPlane with
  the line (perpendicular, mark 0) and its start point (coincident, mark 1) is
  the plane through that point square to it. The fixed line leaves the sketch
  fully defined.
- A loft takes its profiles as sketches selected with mark 1, in order;
  IFeatureManager.InsertProtrusionBlend / InsertCutBlend do the rest. Each
  profile starts where SolidWorks finds its first point, so profiles drawn in
  the same order and sense do not twist.
- FeatureCircularPattern takes the axis with mark 1 and the seed with mark 4.
"""

import contextlib
import io
import json
import math
import traceback

from . import binding
from .constants import SW_REF_PLANE_COINCIDENT, SW_REF_PLANE_PERPENDICULAR
from .errors import SolidWorksError
from .units import deg_to_rad, m_to_mm, mm_to_m

_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
_HELPER_LENGTH_MM = 10.0  # length of the fixed 3D line an axis or plane is built on


def unit(vector, label: str = "direction") -> tuple:
    """A direction [dx, dy, dz] scaled to length 1; pure, unit-tested."""
    if len(vector) != 3:
        raise SolidWorksError(f"A {label} is [dx, dy, dz] (got {vector}).")
    length = math.sqrt(sum(float(c) ** 2 for c in vector))
    if length < 1e-9:
        raise SolidWorksError(f"A {label} cannot be zero (got {vector}).")
    return tuple(float(c) / length for c in vector)


def json_safe(value, depth: int = 0):
    """What run_script's `result` can hand back: plain JSON, or its repr."""
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, depth + 1) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


class AdvancedTools:
    """Axes, planes by normal, lofts, patterns about any axis, run_script;
    a mixin for SolidWorksSession."""

    # --- a fixed 3D line to build references on ------------------------------------

    def _fixed_3d_line(self, start_mm, direction) -> tuple:
        """A fixed line in a new 3D sketch from start_mm along `direction`;
        returns (sketch name, line name, line) with the sketch closed."""
        model = self._require_model()
        self._require_part()
        start = [float(c) for c in start_mm]
        end = [s + _HELPER_LENGTH_MM * d for s, d in zip(start, direction)]
        sketcher = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        before = {f.Name for f in self._iter_features()}
        model.ClearSelection2(True)
        sketcher.Insert3DSketch(True)
        try:
            sketcher.AddToDB = True
            try:
                line = sketcher.CreateLine(*(mm_to_m(c) for c in start), *(mm_to_m(c) for c in end))
            finally:
                sketcher.AddToDB = False
            if line is None:
                raise SolidWorksError("SolidWorks did not draw the 3D line for the reference.")
            line = binding.wrap(line, self._mod.ISketchSegment)
            model.ClearSelection2(True)
            line.Select4(False, None)
            model.SketchAddConstraints("sgFIXED")
        finally:
            model.ClearSelection2(True)
            sketcher.Insert3DSketch(True)
        return self._new_feature("3DProfileFeature", before).Name, line.GetName(), line

    def _select_sketch_line(self, sketch: str, line: str, append: bool = False, mark: int = 0) -> None:
        extension = binding.wrap(self._model.Extension, self._mod.IModelDocExtension)
        if not extension.SelectByID2(f"{line}@{sketch}", "EXTSKETCHSEGMENT", 0, 0, 0, append, mark, None, 0):
            raise SolidWorksError(f"Could not select {line} of {sketch}.")

    def _new_feature(self, type_name: str, before: set):
        """The one feature of `type_name` the history gained since `before`."""
        added = [f for f in self._iter_features() if f.Name not in before and f.GetTypeName2() == type_name]
        if len(added) != 1:
            raise SolidWorksError(f"Could not find the new {type_name} ({len(added)} found).")
        return added[0]

    def _rename(self, feature, name: str | None) -> str:
        if name:
            feature.Name = name
            if feature.Name != name:
                raise SolidWorksError(f"Could not name it '{name}' (it is '{feature.Name}'): is the name taken?")
        return feature.Name

    # --- axes ------------------------------------------------------------------------

    def add_axis(self, through: list | None = None, planes: list | None = None, name: str | None = None) -> dict:
        """A reference axis: through=[[x, y, z], [dx, dy, dz]] (a point, model mm,
        and a direction), or planes=['Plane1', 'right'] (where two planes meet).
        Patterns and revolves can then turn about it by name."""
        model = self._require_model()
        self._require_part()
        if (through is None) == (planes is None):
            raise SolidWorksError("Give through=[[x, y, z], [dx, dy, dz]] or planes=[plane, plane].")
        before = {f.Name for f in self._iter_features()}
        if through is not None:
            if len(through) != 2:
                raise SolidWorksError(f"through is [[x, y, z], [dx, dy, dz]] (got {through}).")
            direction = unit(through[1])
            sketch, line, _ = self._fixed_3d_line(through[0], direction)
            model.ClearSelection2(True)
            self._select_sketch_line(sketch, line)
            point = [float(c) for c in through[0]]
        else:
            if len(planes) != 2:
                raise SolidWorksError(f"planes names two planes (got {planes}).")
            model.ClearSelection2(True)
            for k, plane in enumerate(planes):
                feature, label = self._named_plane(plane)
                if not feature.Select2(k > 0, 0):
                    raise SolidWorksError(f"Could not select the {label}.")
            direction = point = None
        try:
            if not model.InsertAxis2(True):
                raise SolidWorksError("SolidWorks made no axis there (parallel planes?).")
        finally:
            model.ClearSelection2(True)
        axis = self._new_feature("RefAxis", before)
        axis_name = self._rename(axis, name)
        params = binding.wrap(axis.GetSpecificFeature2(), self._mod.IRefAxis).GetRefAxisParams()
        a, b = [m_to_mm(c) for c in params[:3]], [m_to_mm(c) for c in params[3:6]]
        found = unit([q - p for p, q in zip(a, b)])
        return {"ok": True, "axis": axis_name, "point_mm": [round(c, 6) for c in (point or a)],
                "direction": [round(c, 9) + 0.0 for c in (direction or found)]}

    def _pattern_axis(self, axis) -> str:
        """The name of the axis a circular pattern turns about: an axis by name,
        or 'x' / 'y' / 'z' (the model axis through the origin, made once)."""
        key = str(axis).strip()
        if key.lower() in _AXES:
            wanted = f"Axis{key.upper()}"
            if any(f.Name == wanted for f in self._iter_features()):
                return wanted
            return self.add_axis(through=[[0.0, 0.0, 0.0], list(_AXES[key.lower()])], name=wanted)["axis"]
        if not any(f.Name == key and f.GetTypeName2() == "RefAxis" for f in self._iter_features()):
            raise SolidWorksError(f"No axis '{key}' in the part: make one with add_axis, or give 'x', 'y' or 'z'.")
        return key

    def _circular_pattern_about(self, axis, count: int, seed: str, angle_deg: float) -> dict:
        model = self._require_model()
        name = self._pattern_axis(axis)
        model.ClearSelection2(True)
        feature = next(f for f in self._iter_features() if f.Name == name)
        if not feature.Select2(False, 1):
            raise SolidWorksError(f"Could not select the axis {name}.")
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        if not extension.SelectByID2(seed, "BODYFEATURE", 0.0, 0.0, 0.0, True, 4, None, 0):
            raise SolidWorksError(f"Could not select the seed feature '{seed}'.")
        full = abs(abs(angle_deg) - 360.0) < 1e-9
        spacing = deg_to_rad(angle_deg) / (count if full else count - 1)
        manager = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        pattern = manager.FeatureCircularPattern(count, spacing, False, "NULL")
        model.ClearSelection2(True)
        if pattern is None:
            raise SolidWorksError(f"SolidWorks could not pattern {seed} about {name}.")
        return self._finish_feature(pattern, "CircularPattern", instances=count, seed=seed, axis=name,
                                    angle_deg=angle_deg)

    # --- planes by point and normal ----------------------------------------------------

    def _plane_through(self, through, normal):
        """A plane through model point `through` square to `normal`."""
        model = self._require_model()
        direction = unit(normal, "normal")
        sketch, line_name, line = self._fixed_3d_line(through, direction)
        model.ClearSelection2(True)
        self._select_sketch_line(sketch, line_name)
        selection = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        data = binding.wrap(selection.CreateSelectData(), self._mod.ISelectData)
        data.Mark = 1
        start = binding.wrap(binding.wrap(line, self._mod.ISketchLine).GetStartPoint2(), self._mod.ISketchPoint)
        if not start.Select4(True, data):
            model.ClearSelection2(True)
            raise SolidWorksError("Could not select the point the plane goes through.")
        manager = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        made = manager.InsertRefPlane(SW_REF_PLANE_PERPENDICULAR, 0, SW_REF_PLANE_COINCIDENT, 0, 0, 0)
        model.ClearSelection2(True)
        if made is None:
            raise SolidWorksError("SolidWorks could not make the plane.")
        plane = self._last_ref_plane()
        frame = self._plane_frame(plane)
        if sum(n * d for n, d in zip(frame["normal"], direction)) < 0:
            frame = {**frame, "note": "SolidWorks keeps the plane's normal the other way round."}
        return plane, frame

    # --- lofts -------------------------------------------------------------------------

    def add_loft(self, sketches: list, cut: bool = False, name: str | None = None) -> dict:
        """Loft through closed sketches given by name, on any planes, in order;
        cut=True takes the lofted shape away instead (a gear's tooth gap).
        Draw the profiles in the same order and sense so they do not twist."""
        model = self._require_model()
        self._require_part()
        if not isinstance(sketches, (list, tuple)) or len(sketches) < 2:
            raise SolidWorksError("A loft runs through two or more sketches: sketches=['Heel', 'Toe'].")
        names = {f.Name: f for f in self._iter_features()}
        missing = [s for s in sketches if s not in names]
        if missing:
            raise SolidWorksError(f"No sketch {', '.join(missing)} in the part.")
        model.ClearSelection2(True)
        for sketch in sketches:
            if names[sketch].GetTypeName2() not in ("ProfileFeature", "3DProfileFeature"):
                raise SolidWorksError(f"'{sketch}' is a {names[sketch].GetTypeName2()}, not a sketch.")
            if not names[sketch].Select2(True, 1):
                model.ClearSelection2(True)
                raise SolidWorksError(f"Could not select the sketch {sketch}.")
        manager = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        if cut:
            loft = manager.InsertCutBlend(False, True, False, 1.0, 0, 0, False, 0.0, 0.0, 0, True, True)
        else:
            loft = manager.InsertProtrusionBlend(False, True, False, 1.0, 0, 0, 1.0, 1.0, True, True, False,
                                                 0.0, 0.0, 0, True, True, True)
        model.ClearSelection2(True)
        if loft is None:
            raise SolidWorksError(f"SolidWorks could not loft through {', '.join(sketches)}: are they closed, "
                                  "on different planes, and drawn in the same order?")
        return self._finish_feature(loft, name or ("LoftCut" if cut else "Loft"), sketches=list(sketches), cut=cut)

    # --- run_script ----------------------------------------------------------------------

    def run_script(self, code: str) -> dict:
        """Run Python against the live SolidWorks on the server's COM thread.

        The script sees: sw (ISldWorks), model (current IModelDoc2 or None),
        drawing (current drawing or None), mod (the typed interfaces), wrap
        (re-type a COM object: wrap(obj, mod.IFeature)), mm (mm -> m), to_mm
        (m -> mm), doubles (a VARIANT array of doubles), session (the server's
        own tools, e.g. session.list_faces()), math, json. print() is captured;
        whatever the script puts in `result` comes back. Afterwards the part is
        rebuilt and reported like any modelling call. A script that raises is
        rolled back like any failed tool.
        """
        if not isinstance(code, str) or not code.strip():
            raise SolidWorksError("Give the script as code='...'.")
        sw = self._ensure()
        namespace = {
            "sw": sw, "model": self._model, "drawing": self._drawing, "mod": self._mod, "wrap": binding.wrap,
            "mm": mm_to_m, "to_mm": m_to_mm, "doubles": self._doubles, "session": self, "math": math,
            "json": json, "result": None,
        }
        output = io.StringIO()
        try:
            compiled = compile(code, "<run_script>", "exec")
        except SyntaxError as exc:
            raise SolidWorksError(f"The script does not parse: {exc}") from exc
        try:
            with contextlib.redirect_stdout(output):
                exec(compiled, namespace)  # noqa: S102 - the point of the tool; off unless allowed
        except SolidWorksError:
            raise
        except Exception as exc:  # noqa: BLE001 - relay the script's own error with its line
            trace = traceback.format_exception(type(exc), exc, exc.__traceback__)
            lines = [t for t in trace if "<run_script>" in t] or trace[-1:]
            raise SolidWorksError(f"The script failed: {type(exc).__name__}: {exc} "
                                  f"({lines[-1].strip()}). Output so far: {output.getvalue()[-2000:]}") from exc
        out = {"ok": True, "output": output.getvalue()[-20000:], "result": json_safe(namespace.get("result"))}
        if self._model is not None and int(self._model.GetType()) == 1:  # a part: rebuild and report it
            self._model.ForceRebuild3(False)
            failing = self._failing_features()
            if failing:
                out["failing_features"] = failing
            out.update({k: v for k, v in self.get_mass_properties().items() if k != "ok"})
        return out


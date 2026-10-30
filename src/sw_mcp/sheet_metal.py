"""Sheet metal: base flanges, edge flanges, the flat pattern and its DXF.

Mixed into SolidWorksSession, so every method runs on the COM worker thread.

API facts this depends on (SOLIDWORKS 2026 SP4.1):
- InsertSheetMetalBaseFlange2 returns None with Merge=True, even in an empty
  part; with Merge=False it works. DirToUse 1 extrudes an open profile one way,
  along the sketch normal (FlipExtruDir: the other way). ThickenDir False puts
  the thickness on the right of an open profile walked from its start, seen
  from the front of its sketch. A closed profile (a tab) grows its thickness
  against the sketch normal unless FlipExtruDir.
- The base flange adds Sheet-Metal1 (thickness, radius, K-factor:
  ISheetMetalFeatureData), the flange and Flat-Pattern1, suppressed. The part
  shows flat while Flat-Pattern1 is unsuppressed.
- InsertSheetMetalEdgeFlange2 makes nothing from an edge alone: it needs a
  profile sketch. IModelDoc2.InsertSketchForEdgeFlange(edge, angle, flip)
  makes that sketch, empty, on the plane the flange starts in, its x axis
  along the edge; a rectangle standing on the edge is the profile. The
  flange's length is then its own dimension (IEdgeFlangeFeatureData.
  OffsetDistance, from the outer virtual sharp), which drives the profile:
  the profile stays under defined, and relations tying it to the edge make
  the flange fail. A flipped flange takes the flip in the flange's options
  (swInsertEdgeFlangeFlipDir) only: a flipped sketch as well fails.
- IPartDoc.ExportToDWG2 and IDrawingDoc.CreateFlatPatternViewFromModelView3
  fail (False / None) for a path spelt other than the document's own
  (forward slashes): they get IModelDoc2.GetPathName.
"""

import math
import os

import pythoncom
import win32com.client

from . import binding
from . import drawing_layout as lay
from .constants import (
    FLANGE_POSITIONS,
    SW_BEND_ALLOWANCE_K_FACTOR,
    SW_DXF_BEND_LINES,
    SW_DXF_FLAT_GEOMETRY,
    SW_EDGE_FLANGE_DEFAULT_RADIUS,
    SW_EDGE_FLANGE_DEFAULT_RELIEF,
    SW_EDGE_FLANGE_FLIP,
    SW_EXPORT_DWG_SHEET_METAL,
    SW_FLANGE_DIM_OUTER_VIRTUAL_SHARP,
    SW_END_COND_BLIND,
)
from .errors import SolidWorksError
from .units import m_to_mm, mm_to_m

_ON_EDGE_MM = 0.01  # a point picks an edge it lies this close to


class SheetMetalTools:
    """Mixed into SolidWorksSession."""

    # --- flanges ----------------------------------------------------------------

    def add_base_flange(self, thickness_mm: float, bend_radius_mm: float, profile_mm: list | None = None,
                        sketch: str | None = None, depth_mm: float | None = None, k_factor: float = 0.5,
                        reverse: bool = False, flip_thickness: bool = False, name: str = "Base-Flange") -> dict:
        """The first sheet metal feature of a part: a flat tab from a closed
        profile, or a bent section from an open one, extruded depth_mm.

        profile_mm: [[x, y], ...] on the Front plane (closed when the last point
        is the first); sketch: a sketch of the part by name instead, such as one
        from add_sketch with arcs or on another plane. The thickness goes on the
        right of an open profile walked from its start (flip_thickness: the
        left); a tab grows along +z (reverse: -z), an open profile extrudes
        along +z (reverse: -z). Bends are rounded with bend_radius_mm inside;
        k_factor sets the flat length (0.5: the bend's middle keeps its length).
        """
        if (profile_mm is None) == (sketch is None):
            raise SolidWorksError("Give profile_mm (points on the Front plane) or sketch (a sketch by name).")
        for label, value in (("thickness", thickness_mm), ("bend radius", bend_radius_mm)):
            if value is None or value <= 0:
                raise SolidWorksError(f"{label} must be > 0 (got {value}).")
        if not 0 < k_factor <= 1:
            raise SolidWorksError(f"k_factor lies between 0 and 1 (got {k_factor}).")
        if depth_mm is not None and depth_mm <= 0:
            raise SolidWorksError(f"depth must be > 0 (got {depth_mm}).")
        self._require_part()
        if self._sheet_metal_feature() is not None:
            raise SolidWorksError("The part is sheet metal already: add to it with add_edge_flange, "
                                  "or cut it with cut_profile and add_hole.")
        if profile_mm is not None:
            closed = len(profile_mm) > 2 and list(profile_mm[0]) == list(profile_mm[-1])
            drawn = self.add_sketch("front", profile_mm[0], [{"line": p} for p in profile_mm[1:]])
            sketch = drawn["sketch"]
        else:
            closed = depth_mm is None
        if closed and depth_mm is not None:
            raise SolidWorksError("A closed profile makes a flat tab, as thick as the sheet: leave out depth_mm.")
        if not closed and depth_mm is None:
            raise SolidWorksError("An open profile needs depth_mm, how far it extrudes.")
        defined = self._select_person_sketch(sketch)
        model = self._model
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        allowance = binding.wrap(feat_mgr.CreateCustomBendAllowance(), self._mod.ICustomBendAllowance)
        allowance.Type = SW_BEND_ALLOWANCE_K_FACTOR
        allowance.KFactor = float(k_factor)
        flange = feat_mgr.InsertSheetMetalBaseFlange2(
            mm_to_m(thickness_mm), bool(flip_thickness), mm_to_m(bend_radius_mm),
            0.0 if closed else mm_to_m(depth_mm), 0.0,   # ExtrudeDist1, ExtrudeDist2
            closed != bool(reverse),                     # FlipExtruDir: a tab grows along +z unless reversed
            SW_END_COND_BLIND, SW_END_COND_BLIND,
            1,                                           # DirToUse: one direction
            allowance,
            False, 0, 0.0001, 0.0001, 0.5, True,         # UseDefaultRelief .. UseReliefRatio
            False, True, True,                           # Merge (True fails), UseFeatScope, UseAutoSelect
        )
        if flange is None:
            hint = "Is it a closed profile?" if closed else "Is it an open chain of lines and arcs, no corner sharper than the sheet allows?"
            raise SolidWorksError(f"InsertSheetMetalBaseFlange2 failed (None). {hint}")
        result = self._finish_feature(binding.wrap(flange, self._mod.IFeature), name, sketch=sketch, **defined)
        if not closed:
            depth = [d["name"] for d in self._dimension_entries(binding.wrap(flange, self._mod.IFeature))
                     if d["name"].endswith("@" + result["feature"]) and d["unit"] == "mm"]
            if depth:
                result["dimensions"]["depth"] = depth[0]
        result["sheet_metal"] = self._sheet_metal_parameters()
        return result

    def add_edge_flange(self, at_mm: list, length_mm: float, angle_deg: float = 90.0,
                        bend_radius_mm: float | None = None, position: str = "material_inside",
                        flip: bool = False, name: str = "Edge-Flange") -> dict:
        """A flange bent up from a straight edge of the sheet, the whole edge
        long: the edge through model point at_mm.

        length_mm runs from the outside of the bend's corner (outer virtual
        sharp) to the flange's end; angle_deg is the bend (90: square). The
        flange rises the way the sheet's broad face at the edge looks: from an
        edge of the top (+z) face of a flat tab it rises along +z; flip=True
        folds it the other way. position: 'material_inside' (the flange's outside on the
        edge: the part keeps its outer size), 'material_outside' (its inside on
        the edge) or 'bend_outside' (the bend starts at the edge). bend_radius_mm
        defaults to the sheet's.
        """
        if length_mm is None or length_mm <= 0:
            raise SolidWorksError(f"length must be > 0 (got {length_mm}).")
        if not 0 < angle_deg < 180:
            raise SolidWorksError(f"angle lies between 0 and 180 degrees (got {angle_deg}).")
        if bend_radius_mm is not None and bend_radius_mm <= 0:
            raise SolidWorksError(f"bend radius must be > 0 (got {bend_radius_mm}).")
        place = FLANGE_POSITIONS.get(str(position).lower())
        if place is None:
            raise SolidWorksError(f"Unknown position '{position}'. Use {', '.join(FLANGE_POSITIONS)}.")
        self._require_sheet_metal()
        edge = self._sheet_edge_at(at_mm)
        model = self._model
        angle = math.radians(angle_deg)
        profile = binding.wrap(model.InsertSketchForEdgeFlange(edge, angle, False), self._mod.IFeature)
        if profile is None:
            raise SolidWorksError("SolidWorks made no flange sketch on that edge: is it an outside edge of the sheet?")
        self._draw_flange_profile(profile, edge, length_mm)
        options = SW_EDGE_FLANGE_DEFAULT_RELIEF
        if bend_radius_mm is None:
            options |= SW_EDGE_FLANGE_DEFAULT_RADIUS
        if flip:
            options |= SW_EDGE_FLANGE_FLIP
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        model.ClearSelection2(True)
        flange = feat_mgr.InsertSheetMetalEdgeFlange2(
            self._dispatch_array([edge]), self._dispatch_array([profile]), options, angle,
            mm_to_m(bend_radius_mm or self._sheet_metal_parameters()["bend_radius_mm"]), place,
            0.0, 0, 0.5, 0.0001, 0.0001,                 # offset, relief type, ratio, width, depth
            SW_FLANGE_DIM_OUTER_VIRTUAL_SHARP, None,
        )
        if flange is None:
            raise SolidWorksError("InsertSheetMetalEdgeFlange2 failed (None): is the edge free (no flange on it "
                                  "yet), and does the flange fit past its neighbours?")
        flange = binding.wrap(flange, self._mod.IFeature)
        data = binding.wrap(flange.GetDefinition(), self._mod.IEdgeFlangeFeatureData)
        data.AccessSelections(model, None)
        data.OffsetDimType = SW_FLANGE_DIM_OUTER_VIRTUAL_SHARP
        data.OffsetDistance = mm_to_m(length_mm)
        if not flange.ModifyDefinition(data, model, None):
            data.ReleaseSelectionAccess()
            raise SolidWorksError("SolidWorks would not set the flange length.")
        result = self._finish_feature(flange, name)
        dims = {}
        for entry in self._dimension_entries(flange):
            role = "angle" if entry["unit"] == "deg" else "length"
            dims.setdefault(role, entry["name"])
        result["dimensions"] = dims
        if flange.GetErrorCode2()[0]:
            raise SolidWorksError("The flange does not rebuild: does it run into the part, or is it too short "
                                  "for its bend?")
        return result

    def _draw_flange_profile(self, profile, edge, length_mm: float) -> None:
        """The rectangle on the edge, length_mm high, in the flange sketch:
        SolidWorks sets its height from the flange's length afterwards."""
        model = self._model
        sketch = binding.wrap(profile.GetSpecificFeature2(), self._mod.ISketch)
        to_sketch = binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform)
        ends = [binding.wrap(v, self._mod.IVertex).GetPoint() for v in (edge.GetStartVertex(), edge.GetEndVertex())]
        (u0, v0), (u1, _) = sorted(tuple(mm_to_m(c) for c in self._to_model_mm(to_sketch, *p[:3])[:2]) for p in ends)
        top = v0 + mm_to_m(length_mm)
        model.ClearSelection2(True)
        if not profile.Select2(False, 0):
            raise SolidWorksError("Could not open the flange sketch.")
        model.EditSketch()
        sketcher = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        sketcher.AddToDB = True
        try:
            corners = [(u0, v0), (u1, v0), (u1, top), (u0, top)]
            for (a, b), (c, d) in zip(corners, corners[1:] + corners[:1]):
                if not sketcher.CreateLine(a, b, 0.0, c, d, 0.0):
                    raise SolidWorksError("SolidWorks could not draw the flange profile.")
        finally:
            sketcher.AddToDB = False
            model.ClearSelection2(True)
            sketcher.InsertSketch(True)

    def _sheet_edge_at(self, at_mm: list):
        """The straight edge of the sheet through model point at_mm (IEdge)."""
        if at_mm is None or len(at_mm) != 3:
            raise SolidWorksError(f"at_mm is a model point [x, y, z] on the edge (got {at_mm}).")
        point = [mm_to_m(float(c)) for c in at_mm]

        def gap(edge) -> float:
            return m_to_mm(math.dist(point, edge.GetClosestPointOn(*point)[:3]))

        edges = [binding.wrap(e, self._mod.IEdge) for body in self._part_bodies() for e in body.GetEdges() or ()]
        if not edges:
            raise SolidWorksError("The part has no edges.")
        edge = min(edges, key=gap)
        if gap(edge) > _ON_EDGE_MM:
            raise SolidWorksError(f"No edge goes through ({', '.join(f'{c:g}' for c in at_mm)}): the nearest lies "
                                  f"{gap(edge):.3g} mm away (list_edges shows them).")
        if not binding.wrap(edge.GetCurve(), self._mod.ICurve).IsLine():
            raise SolidWorksError("A flange grows from a straight edge; that one is curved.")
        return edge

    @staticmethod
    def _dispatch_array(items):
        return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(items))

    # --- the sheet ----------------------------------------------------------------

    def _sheet_metal_feature(self, document=None):
        """The part's Sheet-Metal feature (IFeature), or None for a part that is not sheet metal."""
        model = document or self._model
        feat = binding.wrap(model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            if feat.GetTypeName2() == "SheetMetal":
                return feat
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        return None

    def _flat_pattern_feature(self, document=None):
        model = document or self._model
        feat = binding.wrap(model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            if feat.GetTypeName2() == "FlatPattern":
                return feat
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        return None

    def _require_sheet_metal(self):
        self._require_part()
        feature = self._sheet_metal_feature()
        if feature is None:
            raise SolidWorksError("The part is not sheet metal: start it with add_base_flange.")
        return feature

    def _sheet_metal_parameters(self) -> dict:
        data = binding.wrap(self._require_sheet_metal().GetDefinition(), self._mod.ISheetMetalFeatureData)
        out = {"thickness_mm": round(m_to_mm(data.Thickness), 6), "bend_radius_mm": round(m_to_mm(data.BendRadius), 6)}
        if data.BendAllowanceType == SW_BEND_ALLOWANCE_K_FACTOR:
            out["k_factor"] = round(data.KFactor, 6)
        return out

    def _set_flat(self, flat: bool) -> bool:
        """Show the part flat or folded; returns whether it was flat."""
        feature = self._flat_pattern_feature()
        if feature is None:
            raise SolidWorksError("The part has no flat pattern.")
        was_flat = not self._is_suppressed(feature)
        if was_flat != flat:
            self._model.ClearSelection2(True)
            if not feature.Select2(False, 0):
                raise SolidWorksError("Could not select the flat pattern.")
            ok = self._model.EditUnsuppress2() if flat else self._model.EditSuppress2()
            self._model.ClearSelection2(True)
            if not ok:
                raise SolidWorksError(f"SolidWorks would not {'flatten' if flat else 'fold'} the part.")
            self._model.ForceRebuild3(False)
        return was_flat

    def _flat_report(self) -> dict:
        """The sheet's parameters, its bends and the flat blank's size, read flat."""
        was_flat = self._set_flat(True)
        try:
            box = self.get_bounding_box()["bounding_box_mm"]
            bends = [sub.Name for sub in self._sub_features(self._flat_pattern_feature())
                     if sub.GetTypeName2() == "UiBend"]
        finally:
            if not was_flat:
                self._set_flat(False)
        size = sorted(box["size_mm"], reverse=True)
        return {**self._sheet_metal_parameters(), "bends": len(bends),
                "flat_size_mm": [round(size[0], 4), round(size[1], 4)]}

    def flat_pattern(self, action: str = "info", path: str | None = None) -> dict:
        """The sheet metal part's flat pattern: 'info' (sheet thickness, bend
        radius, K-factor, number of bends, flat blank size), 'flatten' / 'fold'
        (show the part so), or 'export' to path (.dxf / .dwg: the flat outline
        and its bend lines, for laser cutting; the part must be saved)."""
        key = str(action).lower()
        self._require_sheet_metal()
        if key != "export" and path is not None:
            raise SolidWorksError("path goes with action 'export'.")
        if key == "info":
            return {"ok": True, **self._flat_report()}
        if key in ("flatten", "fold"):
            self._set_flat(key == "flatten")
            return {"ok": True, "flat": key == "flatten", **self._flat_report(),
                    "bounding_box_mm": self.get_bounding_box()["bounding_box_mm"]}
        if key != "export":
            raise SolidWorksError(f"Unknown action '{action}'. Use info, flatten, fold or export.")
        if not path or os.path.splitext(path)[1].lower() not in (".dxf", ".dwg"):
            raise SolidWorksError(f"The flat pattern exports to a .dxf or .dwg path (got {path!r}).")
        part_path = self._model.GetPathName()
        if not part_path:
            raise SolidWorksError("Save the part first (save_document): the export reads its file.")
        target = os.path.abspath(path)
        part = binding.wrap(self._model, self._mod.IPartDoc)
        if os.path.exists(target):
            os.remove(target)
        if not part.ExportToDWG2(target, part_path, SW_EXPORT_DWG_SHEET_METAL, True, None, False, False,
                                 SW_DXF_FLAT_GEOMETRY | SW_DXF_BEND_LINES, None) or not os.path.exists(target):
            raise SolidWorksError(f"SolidWorks did not write {target}. Is the folder writable?")
        return {"ok": True, "path": target, "bytes": os.path.getsize(target), **self._flat_report()}

    # --- drawings -------------------------------------------------------------------

    def _create_flat_view(self, drawing, part_path: str, x_mm: float, y_mm: float):
        """A flat pattern view of the part at part_path, with its bend lines, at a sheet point."""
        view = drawing.CreateFlatPatternViewFromModelView3(os.path.normpath(part_path), "", mm_to_m(x_mm),
                                                           mm_to_m(y_mm), 0.0, False, False)
        if view is None:
            raise SolidWorksError(f"SolidWorks made no flat pattern view of {os.path.basename(part_path)}: "
                                  "is it a sheet metal part?")
        return binding.wrap(view, self._mod.IView)

    def add_flat_pattern_view(self, at: list | None = None, beside: str | None = None, side: str | None = None,
                              gap_mm: float | None = None, scale=None, name: str | None = None,
                              part: str | None = None, dimensions: bool = True) -> dict:
        """The sheet metal part's flat pattern with its bend lines and bend
        notes, at a sheet point or beside a view; dimensions=True adds the flat
        blank's overall length and width."""
        _, drawing = self._require_drawing()
        path = part and self._drawing_model_path(part) or getattr(self, "_drawing_model", None) \
            or self._drawing_model_path(None)
        frame, block = self._sheet_frame()
        start = lay.center(lay.drawing_area(frame, block)[0])
        view = self._create_flat_view(drawing, path, *start)
        self._drawing_model = path
        if scale is None and len(self._views()) == 1:
            view.UseSheetScale = True
        if scale is not None:
            self._finish_view(view, None, scale, None)
        placement = self._resolve_placement(view, at, beside, side, gap_mm)
        result = self._finish_view(view, name, None, placement)
        if dimensions:
            result["dimensions"] = self._dimension_flat_blank(view)
        return result

    def _dimension_flat_blank(self, view) -> list:
        """The flat blank's overall length (below the view) and width (left of
        it): between its outermost straight edges, as the sheet shows them."""
        mapping = self._model_map(view)
        lines = [e for e in self._visible_edges(view, mapping) if e["kind"] == "line" and e["length"] > 0.1]

        def along(entry, axis):  # how far the line runs along the sheet axis, against its length
            s = entry["sheet"]
            return abs(s[2 + axis] - s[axis]) / entry["length"]

        def at(entry, axis):
            s = entry["sheet"]
            return (s[axis] + s[2 + axis]) / 2.0

        made = []
        for across, direction, side in ((0, "horizontal", "below"), (1, "vertical", "left")):
            walls = [e for e in lines if along(e, 1 - across) > 0.999]  # square to the measured direction
            if len(walls) < 2:
                continue
            low, high = min(walls, key=lambda e: at(e, across)), max(walls, key=lambda e: at(e, across))
            if at(high, across) - at(low, across) < 0.1:
                continue
            made.append(self.add_dimension(view.GetName2(), list(low["mid"]), list(high["mid"]), direction, side))
        return [{k: m[k] for k in ("id", "text", "warnings") if k in m} for m in made]

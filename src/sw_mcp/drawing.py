"""Manufacturing drawings: sheets, views, dimensions, hole callouts, notes, checks.

Mixed into SolidWorksSession, so every method runs on the COM worker thread
like the part tools. The current drawing is kept apart from the current part:
modelling and drawing calls can alternate without switching documents.

One coordinate system for callers: sheet millimetres, origin at the sheet's
bottom-left corner. Where geometry matters a caller can also give a MODEL point
(model mm), or a semantic reference ('edge:-x', 'hole:M4_CBore_1'), and the
tools convert. Annotations are named '<name>@<view>' as SolidWorks names
dimensions ('RD3@Drawing View1').

API facts this depends on, found on SOLIDWORKS 2026 SP4.1:
- IView.ModelToViewTransform maps model metres to SHEET metres (scale included).
- IView.GetVisibleEntities2 returns the part's own edges; IsSame never matches
  them with the part's faces, so an edge is matched to a face by geometry.
- A view's own sketch sits at (sheet - view position) / view scale: draw section
  lines and detail circles through ISketch.ModelToSketchTransform of it.
- IView.Position must be set with a VARIANT array of doubles; a tuple moves the
  view to x = 0 without an error.
- IDisplayData.GetLineAtIndex returns [?, x1, y1, z1, x2, y2, z2] in sheet metres;
  GetTextPositionAtIndex is the start of the text's baseline.
- Hole callouts keep their live text in slot 7 (swDimensionTextCalloutAboveDefinition);
  writing the resolved text (slot 3) would freeze the value.
- Setting a dimension prefix replaces SolidWorks' own one: a radius needs 'R' in it.
- Dimensions added in a drawing are driven and show grey in a PDF unless their
  colour is set (IAnnotation.Color = 0).
- The title block evaluates SW-Mass in the DRAWING's mass unit.
- SetupSheet5's scale gives way to SolidWorks' automatic scale once the first
  view goes in: views take the scale explicitly afterwards.
- InsertModelAnnotations3 brings in nothing (None) on a drawing that was
  never saved unless HiddenFeatureDims is set: the dimensions sit in sketches
  their features absorbed, which are hidden. Once the drawing is saved or
  exported it brings them in either way.
"""

import math
import os
import re

import pythoncom

from . import binding, gears
from . import drawing_layout as lay
from .constants import (
    ANNOTATION_TYPES,
    DIMENSION_KINDS,
    DRAWING_EXPORTS,
    GTOL_SYMBOLS,
    SURFACE_FINISH_PROCESSES,
    SW_ARROW_CLOSED,
    SW_LEADER_STRAIGHT,
    SW_ORDINATE_HORIZONTAL,
    SW_ORDINATE_VERTICAL,
    SW_SF_ROUGHNESS,
    SW_TABLE_ANCHOR_TOP_LEFT,
    SW_ALIGN_AUTO_ARRANGE,
    SW_ANNOTATION_DIMENSION,
    SW_ANNOTATION_NOTE,
    SW_ANNOTATION_OWNER_TEMPLATE,
    SW_AUTO_CENTER_MARK_FILLETS,
    SW_AUTO_CENTER_MARK_HOLES,
    SW_CENTER_MARK_SINGLE,
    SW_CREATE_SECTION_CHANGE_DIRECTION,
    SW_CUSTOM_INFO_TEXT,
    SW_CUSTOM_PROPERTY_REPLACE,
    SW_DELETE_NO_OPTION,
    SW_DET_CIRCLE_CIRCLE,
    SW_DET_VIEW_STANDARD,
    SW_DETAILING_DIM_TRAILING_ZERO,
    SW_DETAILING_DIMENSION_STANDARD,
    SW_DETAILING_NO_OPTION,
    SW_DETAILING_STANDARD_ISO,
    SW_DIM_REMOVE_TRAILING_ZEROES,
    SW_DIMENSION_TEXT_CALLOUT_ABOVE,
    SW_DIMENSION_TEXT_CALLOUT_ABOVE_DEFINITION,
    SW_DIMENSION_TEXT_PREFIX,
    SW_DIMENSION_TEXT_PREFIX_DEFINITION,
    SW_DIMENSION_TEXT_SUFFIX,
    SW_DOC_ASSEMBLY,
    SW_DOC_DRAWING,
    SW_DOC_PART,
    SW_DWG_TEMPLATE_CUSTOM,
    SW_IMPORT_ENTIRE_MODEL,
    SW_INSERT_DIMENSIONS,
    SW_INSERT_WELDS,
    SW_FIELD_WELD,
    WELD_CONTOURS,
    WELD_SYMBOLS,
    SW_OPEN_DOC_SILENT,
    SW_PREF_DEFAULT_TEMPLATE_DRAWING,
    SW_FILE_LOCATIONS_SHEET_FORMAT,
    SW_TANGENT_EDGES_HIDDEN,
    SW_TANGENT_EDGES_VISIBLE,
    SW_UNITS_MASS,
    SW_UNITS_MASS_DECIMALS,
    SW_UNITS_MASS_GRAMS,
    SW_UNITS_MASS_KILOGRAMS,
    SW_UNIT_SYSTEM,
    SW_UNIT_SYSTEM_CUSTOM,
    SW_VIEW_ENTITY_EDGE,
    TOLERANCE_TYPES,
    VIEW_DISPLAY_MODES,
    VIEW_TYPES,
)
from .errors import SolidWorksError
from .units import m_to_mm, mm_to_m

# Model views by name: SolidWorks' own (English) view names
_MODEL_VIEWS = {"front": "*Front", "back": "*Back", "left": "*Left", "right": "*Right", "top": "*Top",
                "bottom": "*Bottom", "iso": "*Isometric", "isometric": "*Isometric",
                "trimetric": "*Trimetric", "dimetric": "*Dimetric"}

# Title block fields of SolidWorks' ISO sheet formats and the custom property each
# one shows: $PRPSHEET reads the part, $PRP the drawing
PART_PROPERTIES = {"description": "Description", "material": "Material", "weight": "Weight", "finish": "Finish"}
DRAWING_PROPERTIES = {"drawn_by": "DrawnBy", "drawn_date": "DrawnDate", "checked_by": "CheckedBy",
                      "checked_date": "CheckedDate", "approved_by": "EngineeringApproval",
                      "approved_date": "EngAppDate", "manufacturing": "ManufacturingApproval",
                      "manufacturing_date": "MfgAppDate", "qa": "QAApproval", "qa_date": "QAAppDate",
                      "company": "COMPANYNAME", "revision": "Revision", "part_no": "PartNo", "qty": "Qty"}

_DEFAULT_GAP_MM = 20.0          # free space between neighbouring views
_CALLOUT_OFFSET_MM = 12.0       # how far a hole callout's text sits off the view
_EDGE_ON_FACE_M = 1e-5          # an edge lies on a face when its midpoint is this close


UNFIT = ("Views or annotations still reach off the frame, onto the title block or into another view on {}: "
         "leave a view out, show it as a section or detail, or use a larger sheet (max_sheet).")


class DrawingTools:
    """Drawing tools; a mixin for SolidWorksSession, whose __init__ sets the
    state they keep: _drawing, _drawing_model, _frames and _view_aliases."""

    # --- documents --------------------------------------------------------------

    def _require_drawing(self):
        """(IModelDoc2, IDrawingDoc) of the current drawing, made SolidWorks'
        active document. Without one, SolidWorks' active document is taken when
        it is a drawing."""
        sw = self._ensure()
        if self._drawing is not None:
            try:
                self._drawing.GetTitle()
            except pythoncom.com_error:
                self._drawing = None  # closed in SolidWorks
        if self._drawing is None:
            active = binding.wrap(sw.ActiveDoc, self._mod.IModelDoc2)
            if active is None or int(active.GetType()) != SW_DOC_DRAWING:
                raise SolidWorksError("No drawing is open here. Start one with new_drawing or make_drawing "
                                      "(keep_open=True), or open one with open_drawing.")
            self._drawing = active
        model = self._drawing
        active = binding.wrap(sw.ActiveDoc, self._mod.IModelDoc2)
        if active is None or active.GetTitle() != model.GetTitle():
            sw.ActivateDoc3(model.GetTitle(), False, 0, 0)
        return model, binding.wrap(model, self._mod.IDrawingDoc)

    def _sheet_format_path(self, sheet: str) -> str | None:
        folder = self._sw.GetUserPreferenceStringValue(SW_FILE_LOCATIONS_SHEET_FORMAT) or ""
        path = os.path.join(folder, f"{sheet.lower()} - iso.slddrt")
        return path if os.path.isfile(path) else None

    def _start_drawing(self, sheet: str, scale=(1, 1)):
        """A new drawing document on an ISO sheet with SolidWorks' own ISO sheet
        format (border, zones, title block linked to custom properties), first
        angle projection, ISO dimensioning without trailing zeros."""
        w, h, paper = lay.sheet_size(sheet)
        sw = self._ensure()
        template = sw.GetUserPreferenceStringValue(SW_PREF_DEFAULT_TEMPLATE_DRAWING)
        model = binding.wrap(sw.NewDocument(template, paper, mm_to_m(w), mm_to_m(h)), self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"SolidWorks could not start a drawing from the template '{template}'.")
        drawing = binding.wrap(model, self._mod.IDrawingDoc)
        sheet_format = self._sheet_format_path(sheet)
        current = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet)
        if not drawing.SetupSheet5(current.GetName(), paper, SW_DWG_TEMPLATE_CUSTOM if sheet_format else paper,
                                   float(scale[0]), float(scale[1]), True, sheet_format or "",
                                   mm_to_m(w), mm_to_m(h), "Default", True):
            sw.CloseDoc(model.GetTitle())
            raise SolidWorksError(f"SolidWorks could not set up an {sheet} sheet.")
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        extension.SetUserPreferenceInteger(SW_DETAILING_DIMENSION_STANDARD, SW_DETAILING_NO_OPTION,
                                           SW_DETAILING_STANDARD_ISO)
        extension.SetUserPreferenceInteger(SW_DETAILING_DIM_TRAILING_ZERO, SW_DETAILING_NO_OPTION,
                                           SW_DIM_REMOVE_TRAILING_ZEROES)
        self._drawing = model
        self._frames.pop(model.GetTitle(), None)
        return model, drawing

    def new_drawing(self, sheet: str = "A3", part: str | None = None) -> dict:
        """Start an empty drawing on an ISO sheet with a title block; it becomes
        the current drawing. part (a saved .sldprt / .sldasm, the current part by
        default) is what add_view shows."""
        path = self._drawing_model_path(part)
        self._start_drawing(sheet)
        self._drawing_model = path
        frame, block = self._sheet_frame()
        return {"ok": True, "title": self._drawing.GetTitle(), "sheet": sheet.upper(), "model": path,
                "frame_mm": lay.rounded(frame), "title_block_mm": lay.rounded(block) if block else None,
                "free_areas_mm": [lay.rounded(a) for a in lay.drawing_area(frame, block)]}

    def _drawing_model_path(self, part: str | None) -> str:
        if part:
            path = os.path.abspath(part)
            if not os.path.isfile(path):
                raise SolidWorksError(f"No such file: {path}")
            return path
        if self._model is None:
            raise SolidWorksError("No current part: give part='<path to .sldprt>' or open the part first.")
        path = self._model.GetPathName()
        if not path:
            raise SolidWorksError("Save the part first (save_document): a drawing refers to its file.")
        return path

    def open_drawing(self, path: str) -> dict:
        """Open a .slddrw; it becomes the current drawing."""
        abs_path = os.path.abspath(path)
        if not abs_path.lower().endswith(".slddrw") or not os.path.isfile(abs_path):
            raise SolidWorksError(f"No drawing at {abs_path} (a .slddrw file).")
        result = self._ensure().OpenDoc6(abs_path, SW_DOC_DRAWING, SW_OPEN_DOC_SILENT, "", 0, 0)
        model = binding.wrap(result[0] if isinstance(result, tuple) else result, self._mod.IModelDoc2)
        if model is None:
            raise SolidWorksError(f"SolidWorks could not open the drawing {abs_path}.")
        self._drawing = model
        self._frames.pop(model.GetTitle(), None)
        views = self._views()
        self._drawing_model = views[0].GetReferencedModelName() if views else None
        return {"ok": True, "title": model.GetTitle(), "path": abs_path, "views": [v.GetName2() for v in views]}

    def save_drawing(self, path: str) -> dict:
        """Save the current drawing as an editable .slddrw."""
        model, _ = self._require_drawing()
        abs_path = os.path.abspath(path)
        if not abs_path.lower().endswith(".slddrw"):
            abs_path += ".slddrw"
        self._write_via_saveas3(abs_path, model)
        return {"ok": True, "path": abs_path, "bytes": os.path.getsize(abs_path)}

    def export_drawing(self, path: str) -> dict:
        """Write the current drawing as PDF, DXF, DWG or an image (PNG/JPG/TIF of
        the whole sheet), or as .slddrw."""
        model, _ = self._require_drawing()
        fmt = os.path.splitext(path)[1].lstrip(".").lower()
        if fmt not in DRAWING_EXPORTS:
            raise SolidWorksError(f"A drawing exports to {', '.join(sorted(DRAWING_EXPORTS))} (got '{fmt}').")
        abs_path = os.path.abspath(path)
        if fmt in ("png", "jpg", "tif"):
            return self._render(model, abs_path, None)
        model.ForceRebuild3(False)
        self._write_via_saveas3(abs_path, model)
        return {"ok": True, "path": abs_path, "format": fmt, "bytes": os.path.getsize(abs_path)}

    def close_drawing(self) -> dict:
        """Close the current drawing without saving (CloseDoc never prompts)."""
        model, _ = self._require_drawing()
        title = model.GetTitle()
        self._sw.CloseDoc(title)
        self._drawing = None
        self._frames.pop(title, None)
        if self._model is not None:
            try:
                self._sw.ActivateDoc3(self._model.GetTitle(), False, 0, 0)
            except pythoncom.com_error:
                pass
        return {"ok": True, "closed": title}

    def make_drawing(self, path: str, sheet: str = "A4", scale=None, keep_open: bool = False,
                     max_sheet: str | None = "A1") -> dict:
        """A manufacturing drawing of the current (saved) part in one call.

        Front, top and side views in first angle projection plus an isometric
        view, laid out on an ISO sheet with SolidWorks' ISO sheet format at the
        largest standard scale that leaves room for dimensions (or `scale`).
        The model's dimensions come in once each; each set of identical Hole
        Wizard holes gets one counted callout ('4X ...') and their centres
        dimensioned; the title block shows the part's material and mass.
        Weld beads come in with their weld symbols, and a weldment gets its
        cut list as a table.
        Then the views move with their annotations until nothing reaches off
        the frame, onto the title block or into another view (fit_sheet). When
        that cannot be done, the iso view is left out (unless it carries
        dimensions), then the next larger sheet is tried, up to `max_sheet`
        (None: `sheet` only), at the scale picked there or, failing that, the
        one tried on the smaller sheet. `fit` says what was tried.
        Saved to `path` (PDF, .slddrw, DXF, DWG); keep_open=True keeps it as the
        current drawing to refine with the other drawing tools.
        """
        fmt = os.path.splitext(path)[1].lstrip(".").lower()
        if fmt not in DRAWING_EXPORTS - {"png", "jpg", "tif"}:
            raise SolidWorksError(f"A drawing is saved as pdf, slddrw, dxf or dwg (got '{fmt}').")
        part = self._require_part()
        part_path = self._model.GetPathName()
        if not part_path:
            raise SolidWorksError("Save the part first (save_document): the drawing refers to its file.")
        abs_path = os.path.abspath(path)
        sheets = lay.sheets_up_to(sheet, max_sheet or sheet)
        sheet_metal = self._sheet_metal_feature() is not None
        tried, linked = [], None
        for i, attempt in enumerate(sheets):
            model, drawing = self._start_drawing(attempt)
            self._drawing_model = part_path
            try:
                result = self._draw_part(drawing, part, part_path, scale, sheet_metal)
                linked = result["part_properties"] if linked is None else linked
                fits = self._fit_standard_views(result, scale, tried[-1].split()[1] if tried else None)
                tried.append(f"{attempt} {result['scale']}")
                if fits or i == len(sheets) - 1:
                    result["part_properties"] = linked
                    result["problems"] = self.check_drawing()["problems"]
                    result["fit"] = {"fits": fits, "tried": tried}
                    if not fits:
                        result.setdefault("warnings", []).append(UNFIT.format(attempt))
                    model.ForceRebuild3(False)
                    self._write_via_saveas3(abs_path, model)
                    break
            except BaseException:
                self.close_drawing()
                raise
            self.close_drawing()
        if not keep_open:
            self.close_drawing()
        return {"ok": True, "path": abs_path, "bytes": os.path.getsize(abs_path), "sheet": attempt,
                **result, "open": bool(keep_open)}

    def _fit_standard_views(self, result: dict, scale, earlier: str | None) -> bool:
        """fit_sheet for make_drawing's views; failing that, again without the
        iso view (unless it carries annotations, or the rest does not fit
        without it either), then (no scale given) at `earlier`, the scale that
        did not fit the smaller sheet."""
        if self.fit_sheet()["fits"]:
            return True
        iso = result.get("iso")
        if (iso and not self._annotations(self._view(iso))
                and self._plan_footprints(leave_out={iso})[-1] is not None):
            self.delete_view(iso)
            result["views"].remove(iso)
            for key in ("iso", "iso_scale"):
                result.pop(key, None)
            result["left_out"] = [f"{iso} (iso)"]
            if self.fit_sheet()["fits"]:
                return True

        def decimal(text):
            num, den = lay.as_ratio(text)
            return num / den

        if scale is None and earlier and decimal(earlier) < decimal(result["scale"]):
            self._scale_down_extra_view(result, decimal(earlier))
            laid = self.auto_layout(earlier)
            result["scale"] = laid["scale"]
            return not lay.fit_problems(laid["problems"])
        return False

    def _scale_down_extra_view(self, result: dict, s: float) -> None:
        """Keep make_drawing's iso view a standard step below a sheet scale
        lowered to `s`, and its flat pattern no larger than it, and say so in
        `result`."""
        flat = "flat_pattern" in result
        name = result.get("flat_pattern" if flat else "iso")
        if not name:
            return
        view = self._view(name)
        key = "flat_scale" if flat else "iso_scale"
        if view.UseSheetScale:  # follows the sheet down
            result.pop(key, None)
            return
        own = float(view.ScaleDecimal)
        if (own <= s + 1e-9) if flat else (own < s - 1e-9):
            return
        ratio = next((r for r in lay.STANDARD_SCALES
                      if (r[0] / r[1] <= s + 1e-9 if flat else r[0] / r[1] < s - 1e-9)), lay.STANDARD_SCALES[-1])
        if flat and abs(ratio[0] / ratio[1] - s) < 1e-9:
            view.UseSheetScale = True
            result.pop(key, None)
        else:
            view.ScaleDecimal = ratio[0] / ratio[1]
            result[key] = lay.scale_text(ratio)
        self._drawing.ForceRebuild3(False)

    def _draw_part(self, drawing, part, part_path: str, scale, sheet_metal: bool) -> dict:
        """make_drawing's views, dimensions, callouts and title block on the
        current (new) drawing."""
        result = self._lay_out_standard_views(drawing, part_path, scale, flat=sheet_metal)
        imported = self.import_dimensions()
        if sheet_metal and "flat_pattern" in result:
            flat = self._view(result["flat_pattern"])
            for annotation in self._annotations(flat):  # the folded part's sizes, shown in the views already
                if int(annotation.GetType()) == SW_ANNOTATION_DIMENSION:
                    self._delete_annotation(annotation)
            result["flat_dimensions"] = len(self._dimension_flat_blank(flat))
            result["sheet_metal"] = self._sheet_metal_parameters()
        if self._weld_beads():
            result["weld_beads"] = self._import_welds()
        members = self._cut_list_items()
        if members:
            try:
                self.add_cut_list_table()
                result["cut_list"] = len(members)
            except SolidWorksError as exc:
                result.setdefault("warnings", []).append(f"No cut list table: {exc}")
        result["dimensions"] = sum(1 for a in self._all_annotation_entries() if a["type"] == "dimension")
        result["hole_callouts"] = sum(1 for a in self._all_annotation_entries() if a["type"] == "hole_callout")
        result["hole_locations"] = len(imported.get("hole_locations", ()))
        result["part_properties"] = self._link_title_block(part)
        return result

    def _lay_out_standard_views(self, drawing, part_path: str, scale, flat: bool = False) -> dict:
        """Front, top, side and iso views placed by drawing_layout at one scale;
        flat=True: a sheet metal part's flat pattern in place of the iso view,
        at the same scale where it fits."""
        if not drawing.Create1stAngleViews2(part_path):
            raise SolidWorksError("SolidWorks could not place the front, top and side views.")
        views = {}
        for view in self._views():
            looking = self._view_direction(self._model_map(view))
            views[{2: "front", 1: "top", 0: "side"}[looking]] = view
        frame, block = self._sheet_frame()
        if flat:
            iso = self._create_flat_view(drawing, part_path, frame[2] - 40, frame[3] - 40)
        else:
            iso = binding.wrap(drawing.CreateDrawViewFromModelView3(part_path, "*Isometric", mm_to_m(frame[2] - 40),
                                                                    mm_to_m(frame[3] - 40), 0.0), self._mod.IView)
        if iso is None:
            raise SolidWorksError("SolidWorks could not place the isometric view.")
        # sizes at scale 1 from the model box turned into each view
        sizes = {}
        for key, view in [*views.items(), ("iso", iso)]:
            box = self._view_geometry_box(view)
            sizes[key] = (lay.width(box) / view.ScaleDecimal, lay.height(box) / view.ScaleDecimal)
        areas = lay.drawing_area(frame, block)
        margin, gap = lay.DIMENSION_MARGIN_MM, 0.0

        def arranged(s, iso_s):
            return lay.first_angle_layout(*(tuple(c * s for c in sizes[k]) for k in ("front", "top", "side")),
                                          tuple(c * iso_s for c in sizes["iso"]), areas, gap, margin)

        def three(s):
            return lay.first_angle_layout(*(tuple(c * s for c in sizes[k]) for k in ("front", "top", "side")),
                                          None, areas, gap, margin)

        ratio = lay.as_ratio(scale) if scale is not None else lay.pick_scale(lambda s: three(s) is not None)
        s = ratio[0] / ratio[1]
        # the iso view one standard step smaller than the others: it only shows the shape;
        # a flat pattern is dimensioned, so at the same scale where it fits
        iso_ratio = next((r for r in lay.STANDARD_SCALES
                          if (r[0] / r[1] <= s if flat else r[0] / r[1] < s) and arranged(s, r[0] / r[1])), None)
        sheet = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet)
        sheet.SetScale(float(ratio[0]), float(ratio[1]), False, False)
        for view in views.values():
            view.UseSheetScale = True
        placed = arranged(s, iso_ratio[0] / iso_ratio[1]) if iso_ratio else three(s)
        if iso_ratio is None:  # no room by the margins: fit_sheet places it by its footprint, or it is left out
            iso_ratio = ratio if flat else next((r for r in lay.STANDARD_SCALES if r[0] / r[1] < s),
                                                lay.STANDARD_SCALES[-1])
        iso.UseSheetScale = iso_ratio == ratio
        if iso_ratio != ratio:
            iso.ScaleDecimal = iso_ratio[0] / iso_ratio[1]
        self._drawing.ForceRebuild3(False)
        if placed is None:  # a scale given that does not fit: keep SolidWorks' places
            placed = {}
        for key in ("front", "top", "side", "iso"):
            view = iso if key == "iso" else views.get(key)
            if view is not None and key in placed:
                self._move_view_box(view, placed[key])
        self._drawing.ForceRebuild3(False)
        names = [v.GetName2() for v in self._views()]
        out = {"views": names, "scale": lay.scale_text(ratio), "flat_pattern" if flat else "iso": iso.GetName2()}
        if iso_ratio != ratio:
            out["flat_scale" if flat else "iso_scale"] = lay.scale_text(iso_ratio)
        return out

    def _link_title_block(self, part) -> dict:
        """Link the title block's material and weight to the part's own, unless
        the part already sets them; a part property changes the part, which
        keeps them once saved."""
        document = binding.wrap(part, self._mod.IModelDoc2)
        extension = binding.wrap(document.Extension, self._mod.IModelDocExtension)
        manager = binding.wrap(extension.CustomPropertyManager(""), self._mod.ICustomPropertyManager)
        present = set(manager.GetNames() or ())
        wanted = {k: v for k, v in (("material", "from part"), ("weight", "from part"))
                  if PART_PROPERTIES[k] not in present}
        if not wanted:
            self._set_mass_unit(self._drawing, document)
            return {}
        return self.set_drawing_properties(wanted)["part"]

    def build_drawing(self, spec: dict, path: str | None = None) -> dict:
        """A drawing built from scratch from a spec (see the build_drawing tool),
        so fine-tuning is editing the spec and building again: nothing drifts.
        A failing entry closes the half-built drawing and names the entry.
        Last, fit_sheet moves the views with their annotations, and the notes
        and tables, until nothing reaches off the frame, onto the title block
        or into another view (spec 'fit': false leaves them where they are);
        when they cannot fit, the drawing is built again on the next larger
        sheet, up to spec 'max_sheet' (default A1)."""
        if not isinstance(spec, dict):
            raise SolidWorksError("spec is a dict: {'sheet': 'A3', 'views': [...], 'dims': [...], ...}.")
        known = {"part", "sheet", "max_sheet", "fit", "scale", "views", "layout", "import", "dims", "callouts",
                 "symbols", "notes", "tables", "gear_table", "props"}
        unknown = sorted(set(spec) - known)
        if unknown:
            raise SolidWorksError(f"Unknown spec keys {unknown}; use {sorted(known)}.")
        model_path = self._drawing_model_path(spec.get("part"))
        first = str(spec.get("sheet", "A3"))
        fit = spec.get("fit", True)
        sheets = lay.sheets_up_to(first, spec.get("max_sheet") or "A1") if fit else [first]
        tried = []
        for i, sheet in enumerate(sheets):
            out = self._build_on(spec, sheet, model_path, path if not fit else None)
            tried.append(f"{sheet.upper()} {lay.scale_text(self._sheet_scale())}")
            if not fit:
                return out
            if out["fits"] or i == len(sheets) - 1:
                out["fit"] = {"fits": out.pop("fits"), "tried": tried}
                out["sheet"] = sheet.upper()
                if not out["fit"]["fits"]:
                    out["warnings"] = [UNFIT.format(sheet.upper())]
                if path:
                    try:
                        out["saved"] = self.export_drawing(path)["path"]
                    except SolidWorksError as exc:
                        self.close_drawing()
                        raise SolidWorksError(f"build_drawing stopped at save: {exc} The drawing was closed.") from exc
                return out
            self.close_drawing()

    def _build_on(self, spec: dict, sheet: str, model_path: str, path: str | None) -> dict:
        """build_drawing on one sheet; the drawing stays open. `fits` (with
        spec 'fit') says whether fit_sheet cleared the sheet."""
        self._start_drawing(sheet)
        self._drawing_model = model_path
        made = {"views": [], "dimensions": [], "callouts": [], "notes": []}
        step = "sheet"
        try:
            ratio = lay.as_ratio(spec["scale"]) if spec.get("scale") else None
            for i, entry in enumerate(spec.get("views", [])):
                step = f"views[{i}]"
                made["views"].append(self._build_view(entry, ratio)["view"]["name"])
            if ratio is not None:
                self._apply_sheet_scale(ratio)
            if spec.get("layout") == "auto":
                step = "layout"
                self.auto_layout(spec.get("scale"))
            part = self._drawing_part_document()
            if part is not None and int(part.GetType()) == SW_DOC_PART:
                step = "title block"
                made["part_properties"] = self._link_title_block(part)
            if spec.get("import"):
                step = "import"
                kinds = spec["import"] if isinstance(spec["import"], list) else None
                made["imported"] = self.import_dimensions(None, kinds)["count"]
            for i, entry in enumerate(spec.get("dims", [])):
                step = f"dims[{i}]"
                made["dimensions"] += self._build_dimension(entry)
            for i, entry in enumerate(spec.get("callouts", [])):
                step = f"callouts[{i}]"
                made["callouts"].append(self.add_hole_callout(entry["view"], entry["hole"], entry.get("at"),
                                                              entry.get("count", True))["id"])
            for i, entry in enumerate(spec.get("notes", [])):
                step = f"notes[{i}]"
                note = entry if isinstance(entry, dict) else {"text": entry}
                made["notes"].append(self.add_note(note["text"], note.get("at"), note.get("height_mm", 3.5))["id"])
            for i, entry in enumerate(spec.get("symbols", [])):
                step = f"symbols[{i}]"
                made.setdefault("symbols", []).append(self._build_symbol(entry)["id"])
            for i, entry in enumerate(spec.get("tables", [])):
                step = f"tables[{i}]"
                table = entry if isinstance(entry, dict) else {"rows": entry}
                made.setdefault("tables", []).append(self.add_table(table["rows"], table.get("at"),
                                                                    table.get("title", True))["id"])
            if spec.get("gear_table"):
                step = "gear_table"
                made.setdefault("tables", []).append(self.add_gear_data_table(**spec["gear_table"])["id"])
            if spec.get("props"):
                step = "props"
                self.set_drawing_properties(spec["props"])
            if spec.get("fit", True):
                step = "fit"
                fitted = self.fit_sheet()
                made["fits"] = fitted["fits"]
            step = "check"
            problems = self.check_drawing()["problems"]
            out = {"ok": True, "title": self._drawing.GetTitle(), **made, "problems": problems}
            if path:
                step = "save"
                out["saved"] = self.export_drawing(path)["path"]
        except (SolidWorksError, KeyError, TypeError, ValueError) as exc:
            self.close_drawing()
            what = f"missing key {exc}" if isinstance(exc, KeyError) else str(exc)
            raise SolidWorksError(f"build_drawing stopped at {step}: {what} The drawing was closed.") from exc
        return out

    def _build_view(self, entry: dict, ratio) -> dict:
        name = entry.get("name")
        if "projected_from" in entry:
            return self.add_projected_view(entry["projected_from"], entry.get("side", "right"), entry.get("gap_mm"), name)
        if "section_of" in entry:
            return self.add_section_view(entry["section_of"], entry["through"], entry.get("normal", "x"),
                                         entry.get("label", "A"), entry.get("side"), entry.get("gap_mm"),
                                         entry.get("flip", False), name)
        if "detail_of" in entry:
            return self.add_detail_view(entry["detail_of"], entry["center"], entry["radius"], entry.get("scale"),
                                        entry.get("label", "B"), entry.get("at"), name)
        scale = entry.get("scale") or (lay.scale_text(ratio) if ratio is not None else None)
        if entry.get("flat_pattern"):
            beside = next((entry[k] for k in ("right_of", "left_of", "above", "below") if entry.get(k)), None)
            side = next((s for k, s in (("right_of", "right"), ("left_of", "left"), ("above", "above"),
                                        ("below", "below")) if entry.get(k)), None)
            return self.add_flat_pattern_view(entry.get("at"), beside, side, entry.get("gap_mm"), scale, name,
                                              dimensions=entry.get("dimensions", True))
        return self.add_view(entry.get("orient", "front"), entry.get("at"), entry.get("right_of"),
                             entry.get("left_of"), entry.get("above"), entry.get("below"), entry.get("gap_mm"),
                             scale, name)

    def _build_symbol(self, entry: dict) -> dict:
        kind = str(entry.get("type", "")).lower()
        if kind == "datum":
            return self.add_datum(entry["view"], entry["ref"], entry.get("label", "A"), entry.get("at"))
        if kind == "surface_finish":
            return self.add_surface_finish(entry["view"], entry["ref"], entry.get("roughness", "Ra 3.2"),
                                           entry.get("process", "machined"), entry.get("at"))
        if kind == "gtol":
            return self.add_gtol(entry["view"], entry["ref"], entry["symbol"], entry["tolerance"],
                                 entry.get("datums"), entry.get("diameter", False),
                                 entry.get("material_condition"), entry.get("at"))
        if kind == "weld":
            return self.add_symbol("weld", entry["view"], entry["ref"], entry.get("at"), weld=entry.get("weld"))
        raise SolidWorksError(f"A symbol's type is 'datum', 'surface_finish', 'gtol' or 'weld' "
                              f"(got {entry.get('type')!r}).")

    def _build_dimension(self, entry: dict) -> list:
        view = entry["view"]
        for style in ("chain", "baseline", "ordinate"):
            if style in entry:
                refs = entry[style]
                made = self.add_dimension_set(view, refs[0], refs[1:], style, entry.get("direction", "auto"),
                                              entry.get("side"))
                return [d["id"] for d in made["dimensions"]]
        made = self.add_dimension(view, entry["ref"], entry.get("to"), entry.get("direction", "auto"),
                                  entry.get("side"), entry.get("offset_mm"))
        return [made["id"]]

    def _apply_sheet_scale(self, ratio) -> None:
        """Set the sheet's scale after the views are in: SolidWorks rescales
        the sheet when the first view goes in (verified)."""
        _, drawing = self._require_drawing()
        sheet = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet)
        sheet.SetScale(float(ratio[0]), float(ratio[1]), False, False)
        self._drawing.ForceRebuild3(False)

    # --- title block --------------------------------------------------------------

    def set_drawing_properties(self, properties: dict) -> dict:
        """Fill the title block through the custom properties its notes show.

        Part fields (description, material, weight, finish) go to the part shown
        in the drawing; material='from part' and weight='from part' link to the
        part's own material and mass, so they follow the model. Everything else
        (drawn_by, drawn_date, checked_by, revision, company, part_no, qty, or
        any other name) goes to the drawing.
        """
        if not isinstance(properties, dict) or not properties:
            raise SolidWorksError("Give the properties as {name: value}, e.g. {'drawn_by': 'NDV', 'material': 'from part'}.")
        model, _ = self._require_drawing()
        part = self._drawing_part_document()
        written = {"part": {}, "drawing": {}}
        for key, value in properties.items():
            name = str(key).strip()
            lowered = name.lower()
            if lowered in PART_PROPERTIES:
                if part is None:
                    raise SolidWorksError(f"'{name}' belongs to the part, but the drawing shows no part yet: add a view first.")
                prop = PART_PROPERTIES[lowered]
                text = self._linked_part_value(part, lowered, value)
                self._write_property(part, prop, text)
                written["part"][prop] = text
            else:
                prop = DRAWING_PROPERTIES.get(lowered, name)
                self._write_property(model, prop, str(value))
                written["drawing"][prop] = str(value)
        if part is not None and written["part"]:
            self._set_mass_unit(model, part)
        model.ForceRebuild3(False)
        shown = self._title_block_fields()
        missing = [p for side in written.values() for p in side if p not in shown]
        result = {"ok": True, **written, "title_block": shown}
        if missing:
            result["not_in_title_block"] = missing
        return result

    def _linked_part_value(self, part, key: str, value) -> str:
        text = str(value).strip()
        if key in ("material", "weight") and text.lower() in ("from part", "from_part", "auto", ""):
            source = "SW-Material" if key == "material" else "SW-Mass"
            return f'"{source}@{os.path.basename(part.GetPathName())}"'
        return text

    def _write_property(self, document, name: str, value: str) -> None:
        extension = binding.wrap(document.Extension, self._mod.IModelDocExtension)
        manager = binding.wrap(extension.CustomPropertyManager(""), self._mod.ICustomPropertyManager)
        status = manager.Add3(name, SW_CUSTOM_INFO_TEXT, value, SW_CUSTOM_PROPERTY_REPLACE)
        if status not in (0, 1, True):  # swCustomInfoAddResult_AddedOrChanged = 0
            raise SolidWorksError(f"SolidWorks did not write the property '{name}' (status {status}).")

    def _set_mass_unit(self, drawing, part) -> None:
        """Show the mass in g below 1 kg, else in kg; the title block evaluates
        SW-Mass in the drawing's unit."""
        try:
            mass_kg = self._part_mass_kg(part)
        except (pythoncom.com_error, SolidWorksError, TypeError, IndexError):
            mass_kg = 0.0
        extension = binding.wrap(drawing.Extension, self._mod.IModelDocExtension)
        grams = mass_kg < 1.0
        # the mass unit only counts in a custom unit system (lengths stay as they are)
        extension.SetUserPreferenceInteger(SW_UNIT_SYSTEM, SW_DETAILING_NO_OPTION, SW_UNIT_SYSTEM_CUSTOM)
        extension.SetUserPreferenceInteger(SW_UNITS_MASS, SW_DETAILING_NO_OPTION,
                                           SW_UNITS_MASS_GRAMS if grams else SW_UNITS_MASS_KILOGRAMS)
        extension.SetUserPreferenceInteger(SW_UNITS_MASS_DECIMALS, SW_DETAILING_NO_OPTION, 1 if grams else 2)

    def _part_mass_kg(self, part) -> float:
        extension = binding.wrap(part.Extension, self._mod.IModelDocExtension)
        values = extension.GetMassProperties2(1, 0, True)  # SI: kg
        return float(values[5]) if values else 0.0

    def _title_block_fields(self) -> dict:
        """{property: text shown} for each custom property the sheet format's
        notes show, read from the template."""
        model, drawing = self._require_drawing()
        drawing.EditTemplate()
        try:
            sheet_view = binding.wrap(drawing.GetFirstView(), self._mod.IView)
            fields = {}
            note = sheet_view.GetFirstNote()
            while note is not None:
                note = binding.wrap(note, self._mod.INote)
                linked = note.PropertyLinkedText or ""
                for name in re.findall(r'\$PRP(?:SHEET)?:"([^"]+)"', linked):
                    if not name.startswith("SW-"):
                        fields[name] = lay.clean_text(note.GetText())
                note = note.GetNext()
        finally:
            drawing.EditSheet()
        return fields

    def _drawing_part_document(self):
        """The model document the drawing's first view shows (IModelDoc2), or None."""
        views = self._views()
        if not views:
            return None
        return binding.wrap(views[0].ReferencedDocument, self._mod.IModelDoc2)

    # --- sheet geometry -------------------------------------------------------------

    def _sheet_frame(self):
        """(frame, title block) of the current sheet in sheet mm, from its format."""
        model, drawing = self._require_drawing()
        key = model.GetTitle()
        if key not in self._frames:
            sheet = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet)
            props = sheet.GetProperties2()
            w, h = m_to_mm(props[5]), m_to_mm(props[6])
            lines = []
            sketch = binding.wrap(sheet.GetTemplateSketch(), self._mod.ISketch)
            for segment in (sketch.GetSketchSegments() or ()) if sketch is not None else ():
                segment = binding.wrap(segment, self._mod.ISketchSegment)
                if segment.GetType() != 0:  # swSketchLINE
                    continue
                line = binding.wrap(segment, self._mod.ISketchLine)
                a = binding.wrap(line.GetStartPoint2(), self._mod.ISketchPoint)
                b = binding.wrap(line.GetEndPoint2(), self._mod.ISketchPoint)
                lines.append((m_to_mm(a.X), m_to_mm(a.Y), m_to_mm(b.X), m_to_mm(b.Y)))
            self._frames[key] = lay.frame_and_title_block(lines, w, h)
        return self._frames[key]

    def _sheet_scale(self) -> tuple:
        _, drawing = self._require_drawing()
        props = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet).GetProperties2()
        return props[2], props[3]

    # --- views ---------------------------------------------------------------------

    def _views(self) -> list:
        """The current sheet's drawing views (IView), without the sheet itself."""
        _, drawing = self._require_drawing()
        views = []
        view = binding.wrap(drawing.GetFirstView(), self._mod.IView)  # the sheet comes first
        view = binding.wrap(view.GetNextView(), self._mod.IView) if view is not None else None
        while view is not None:
            views.append(view)
            view = binding.wrap(view.GetNextView(), self._mod.IView)
        return views

    def _sheet_view(self):
        _, drawing = self._require_drawing()
        return binding.wrap(drawing.GetFirstView(), self._mod.IView)

    def _view(self, name: str):
        views = self._views()
        name = self._alias_target(str(name)) or name
        for view in views:
            if view.GetName2() == name:
                return view
        lowered = str(name).strip().lower()
        for view in views:
            if view.GetName2().lower() == lowered:
                return view
        listed = ", ".join(v.GetName2() for v in views) or "none"
        raise SolidWorksError(f"No view '{name}' on the sheet (views: {listed}).")

    def _model_map(self, view):
        """(origin, ex, ey, ez): sheet mm of the model origin and the sheet
        offset of one model mm along x, y and z in this view."""
        transform = binding.wrap(view.ModelToViewTransform, self._mod.IMathTransform)
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        out = []
        for p in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
            point = binding.wrap(mathutil.CreatePoint(self._doubles(p)), self._mod.IMathPoint)
            moved = binding.wrap(point.MultiplyTransform(transform), self._mod.IMathPoint).ArrayData
            out.append((moved[0], moved[1]))
        origin = (m_to_mm(out[0][0]), m_to_mm(out[0][1]))
        # one metre along an axis moved this many sheet metres: per model mm the same in mm
        axes = tuple((out[i][0] - out[0][0], out[i][1] - out[0][1]) for i in (1, 2, 3))
        return origin, *axes

    @staticmethod
    def _to_sheet(mapping, p_mm) -> tuple:
        origin, ex, ey, ez = mapping
        return (origin[0] + ex[0] * p_mm[0] + ey[0] * p_mm[1] + ez[0] * p_mm[2],
                origin[1] + ex[1] * p_mm[0] + ey[1] * p_mm[1] + ez[1] * p_mm[2])

    def _view_geometry_box(self, view, mapping=None):
        """Sheet rectangle of the view's model geometry: the model's box turned
        into the view, clipped to the view outline (detail and section views)."""
        outline = self._outline(view)
        document = binding.wrap(view.ReferencedDocument, self._mod.IModelDoc2)
        if document is None:
            return outline
        mapping = mapping or self._model_map(view)
        box = self._model_box_mm(document)
        if box is None:
            return outline
        corners = [self._to_sheet(mapping, (x, y, z)) for x in (box[0], box[3]) for y in (box[1], box[4])
                   for z in (box[2], box[5])]
        projected = lay.rect(min(c[0] for c in corners), min(c[1] for c in corners),
                             max(c[0] for c in corners), max(c[1] for c in corners))
        clipped = (max(projected[0], outline[0]), max(projected[1], outline[1]),
                   min(projected[2], outline[2]), min(projected[3], outline[3]))
        return clipped if clipped[0] < clipped[2] and clipped[1] < clipped[3] else outline

    def _model_box_mm(self, document):
        kind = int(document.GetType())
        if kind == SW_DOC_PART:
            box = binding.wrap(document, self._mod.IPartDoc).GetPartBox(True)
        elif kind == SW_DOC_ASSEMBLY:
            box = binding.wrap(document, self._mod.IAssemblyDoc).GetBox(0)
        else:
            return None
        return [m_to_mm(c) for c in box] if box else None

    def _outline(self, view) -> tuple:
        o = view.GetOutline()
        return lay.rect(m_to_mm(o[0]), m_to_mm(o[1]), m_to_mm(o[2]), m_to_mm(o[3]))

    def _view_entry(self, view, with_mapping: bool = True) -> dict:
        kind = VIEW_TYPES.get(int(view.Type), f"type {view.Type}")
        entry = {"name": view.GetName2(), "type": kind, "orientation": (view.GetOrientationName() or "").lstrip("*"),
                 "scale": lay.scale_text(view.ScaleDecimal), "outline_mm": lay.rounded(self._outline(view)),
                 "model": os.path.basename(view.GetReferencedModelName() or "")}
        base = binding.wrap(view.GetBaseView(), self._mod.IView)
        if base is not None and kind != "standard":
            entry["parent"] = base.GetName2()
        if with_mapping and view.ReferencedDocument is not None:
            mapping = self._model_map(view)
            entry["geometry_mm"] = lay.rounded(self._view_geometry_box(view, mapping))
            origin, ex, ey, ez = mapping
            entry["model_to_sheet"] = {"origin": lay.rounded(origin), "x": lay.rounded(ex, 4),
                                       "y": lay.rounded(ey, 4), "z": lay.rounded(ez, 4)}
        return entry

    def list_views(self) -> dict:
        """The views on the current sheet: name, type, orientation, scale, outline
        and geometry box (sheet mm), and the model-to-sheet mapping: a model point
        p lands at origin + p.x * x + p.y * y + p.z * z (sheet mm)."""
        model, _ = self._require_drawing()
        frame, block = self._sheet_frame()
        views = [self._view_entry(v) for v in self._views()]
        return {"ok": True, "title": model.GetTitle(), "sheet_scale": lay.scale_text(self._sheet_scale()),
                "frame_mm": lay.rounded(frame), "title_block_mm": lay.rounded(block) if block else None,
                "count": len(views), "views": views}

    def _set_view_position(self, view, x_mm: float, y_mm: float) -> None:
        view.Position = self._doubles((mm_to_m(x_mm), mm_to_m(y_mm)))

    def _move_view_box(self, view, target_center) -> None:
        """Shift the view so its geometry box is centred on target_center;
        repeated because SolidWorks re-fits the outline after a move."""
        for _ in range(3):
            box = self._view_geometry_box(view)
            cx, cy = lay.center(box)
            dx, dy = target_center[0] - cx, target_center[1] - cy
            if abs(dx) < 0.05 and abs(dy) < 0.05:
                return
            px, py = view.Position
            self._set_view_position(view, m_to_mm(px) + dx, m_to_mm(py) + dy)
            self._drawing.GraphicsRedraw2()

    def _resolve_placement(self, view, at=None, beside=None, side=None, gap=None, align="center", sides=None):
        """Where the view's geometry box goes: at=[x, y] (its centre, sheet mm),
        or `side` of the view named `beside` with `gap` mm free in between.
        Without a gap it keeps room for two rows of dimensions and moves out
        past other views in the way (trying `sides` in turn, if given)."""
        if at is not None:
            if len(at) != 2:
                raise SolidWorksError(f"at is a sheet point [x, y] in mm (got {at}).")
            return float(at[0]), float(at[1])
        if beside is None:
            return None
        anchor = self._view_geometry_box(self._view(beside))
        box = self._view_geometry_box(view)
        size = (lay.width(box), lay.height(box))
        if gap is not None:
            return lay.place_beside(anchor, size, side or "right", float(gap), align)
        frame, block = self._sheet_frame()
        taken = [self._view_geometry_box(v) for v in self._views() if v.GetName2() != view.GetName2()]
        found = lay.place_clear(anchor, size, sides or [side or "right"], 2 * lay.DIMENSION_MARGIN_MM, taken,
                                lay.drawing_area(frame, block))
        return found[1] if found else lay.place_beside(anchor, size, side or "right", 2 * lay.DIMENSION_MARGIN_MM, align)

    def _relative_args(self, right_of, left_of, above, below):
        given = [(v, s) for v, s in ((right_of, "right"), (left_of, "left"), (above, "above"), (below, "below"))
                 if v]
        if len(given) > 1:
            raise SolidWorksError("Give one of right_of, left_of, above or below.")
        return given[0] if given else (None, None)

    def _finish_view(self, view, name, scale, placement) -> dict:
        if view is None:
            raise SolidWorksError("SolidWorks did not create the view.")
        if scale is not None:
            ratio = lay.as_ratio(scale)
            view.UseSheetScale = False
            view.ScaleDecimal = ratio[0] / ratio[1]
        alias = None
        if name and not view.SetName2(str(name)):
            if int(view.Type) not in (2, 3) or self._alias_target(str(name)):  # swDrawingSectionView, DetailView
                raise SolidWorksError(f"SolidWorks refused the view name '{name}' (already used?).")
            # section and detail views keep the name of their label: the name
            # given becomes an alias the tools accept (for this session)
            self._view_aliases.setdefault(self._drawing.GetTitle(), {})[str(name)] = view.GetName2()
            alias = str(name)
        self._drawing.ForceRebuild3(False)
        if placement is not None:
            self._move_view_box(view, placement)
        entry = self._view_entry(view)
        if alias:
            entry["alias"] = alias
        return {"ok": True, "view": entry, **self._overlaps_for_view(view)}

    def _alias_target(self, name: str):
        return self._view_aliases.get(self._drawing.GetTitle(), {}).get(name) if self._drawing is not None else None

    def _overlaps_for_view(self, view) -> dict:
        frame, block = self._sheet_frame()
        box = self._view_geometry_box(view)
        others = [v.GetName2() for v in self._views()
                  if v.GetName2() != view.GetName2() and lay.overlap(box, self._view_geometry_box(v), 0.5)]
        warnings = []
        if others:
            warnings.append(f"overlaps view(s) {', '.join(others)}")
        if not lay.inside(box, frame, 0.5):
            warnings.append("reaches outside the drawing frame")
        if lay.overlap(box, block, 0.5):
            warnings.append("runs into the title block")
        return {"warnings": warnings} if warnings else {}

    def add_view(self, orientation: str = "front", at: list | None = None, right_of: str | None = None,
                 left_of: str | None = None, above: str | None = None, below: str | None = None,
                 gap_mm: float | None = None, scale=None, name: str | None = None, part: str | None = None) -> dict:
        """A model view (front, top, right, iso, ... or a named view of the
        model) placed at a sheet point or beside another view."""
        _, drawing = self._require_drawing()
        path = part and self._drawing_model_path(part) or getattr(self, "_drawing_model", None) \
            or self._drawing_model_path(None)
        key = str(orientation).strip()
        view_name = _MODEL_VIEWS.get(key.lower(), key if key.startswith("*") else None)
        if view_name is None:
            raise SolidWorksError(f"Unknown orientation '{orientation}'. Use {', '.join(_MODEL_VIEWS)} "
                                  "or a model view name starting with '*'.")
        frame, block = self._sheet_frame()
        start = lay.center(lay.drawing_area(frame, block)[0])
        view = binding.wrap(drawing.CreateDrawViewFromModelView3(path, view_name, mm_to_m(start[0]),
                                                                 mm_to_m(start[1]), 0.0), self._mod.IView)
        if view is None:
            raise SolidWorksError(f"SolidWorks could not place the {orientation} view of {os.path.basename(path)}.")
        self._drawing_model = path
        if scale is None and len(self._views()) == 1:
            view.UseSheetScale = True
        beside, side = self._relative_args(right_of, left_of, above, below)
        if scale is not None:
            self._finish_view(view, None, scale, None)
        placement = self._resolve_placement(view, at, beside, side, gap_mm)
        return self._finish_view(view, name, None, placement)

    def add_projected_view(self, parent: str, side: str = "right", gap_mm: float | None = None,
                           name: str | None = None) -> dict:
        """A view projected from `parent` (first angle: the view placed right of
        the front view shows the part from the left). It stays aligned with it."""
        model, drawing = self._require_drawing()
        base = self._view(parent)
        if side not in lay.SIDES:
            raise SolidWorksError(f"Unknown side '{side}'. Use {', '.join(lay.SIDES)}.")
        box = self._view_geometry_box(base)
        gap = 2 * lay.DIMENSION_MARGIN_MM if gap_mm is None else float(gap_mm)
        start = lay.place_beside(box, (lay.width(box), lay.height(box)), side, gap)
        self._select_view(base)
        view = binding.wrap(drawing.CreateUnfoldedViewAt3(mm_to_m(start[0]), mm_to_m(start[1]), 0.0, False),
                            self._mod.IView)
        model.ClearSelection2(True)
        if view is None:
            raise SolidWorksError(f"SolidWorks could not project a view {side} of {parent}.")
        placement = self._resolve_placement(view, None, base.GetName2(), side, gap_mm)
        return self._finish_view(view, name, None, placement)

    def _select_view(self, view) -> None:
        model, _ = self._require_drawing()
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        model.ClearSelection2(True)
        if not extension.SelectByID2(view.GetName2(), "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0):
            raise SolidWorksError(f"Could not select the view {view.GetName2()}.")

    def _view_sketch_point(self, view, sheet_mm) -> tuple:
        """A sheet point in the coordinates of the view's own sketch (metres)."""
        sketch = binding.wrap(view.GetSketch(), self._mod.ISketch)
        transform = binding.wrap(sketch.ModelToSketchTransform, self._mod.IMathTransform)
        mathutil = binding.wrap(self._sw.GetMathUtility(), self._mod.IMathUtility)
        point = binding.wrap(mathutil.CreatePoint(self._doubles((mm_to_m(sheet_mm[0]), mm_to_m(sheet_mm[1]), 0.0))),
                             self._mod.IMathPoint)
        moved = binding.wrap(point.MultiplyTransform(transform), self._mod.IMathPoint).ArrayData
        return moved[0], moved[1]

    def _view_direction(self, mapping) -> int:
        """The model axis (0, 1, 2) the view looks along: the one that moves
        nothing on the sheet. Raises for a view at an angle."""
        lengths = [math.hypot(*axis) for axis in mapping[1:]]
        axis = min(range(3), key=lambda i: lengths[i])
        if lengths[axis] > 1e-6 * max(lengths):
            raise SolidWorksError("That view looks along no model axis (an iso or turned view); "
                                  "cut sections and details from a front, top or side view.")
        return axis

    def add_section_view(self, parent: str, through: list, normal: str = "x", label: str = "A",
                         side: str | None = None, gap_mm: float | None = None, flip: bool = False,
                         name: str | None = None) -> dict:
        """Section view: the parent is cut by the plane through the model point
        `through` (model mm) square to model axis `normal` ('x', 'y' or 'z');
        the section line is placed from that, not drawn by hand."""
        model, drawing = self._require_drawing()
        base = self._view(parent)
        if len(through) != 3:
            raise SolidWorksError(f"through is a model point [x, y, z] in mm (got {through}).")
        axis = "xyz".find(str(normal).lower().lstrip("+-"))
        if axis < 0:
            raise SolidWorksError(f"normal is the model axis the cutting plane is square to: x, y or z (got '{normal}').")
        mapping = self._model_map(base)
        looking = self._view_direction(mapping)
        if looking == axis:
            raise SolidWorksError(f"The cutting plane square to {normal} lies flat in view {parent}: "
                                  "cut from a view that sees it edge-on.")
        document = binding.wrap(base.ReferencedDocument, self._mod.IModelDoc2)
        box = self._model_box_mm(document)
        run = 3 - axis - looking  # the model axis the section line runs along
        reach = (box[run + 3] - box[run]) / 2.0 + 5.0
        middle = (box[run + 3] + box[run]) / 2.0
        ends = []
        for t in (middle - reach, middle + reach):
            p = [float(c) for c in through]
            p[run] = t
            ends.append(self._view_sketch_point(base, self._to_sheet(mapping, p)))
        drawing.ActivateView(base.GetName2())
        sketcher = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        model.ClearSelection2(True)
        sketcher.AddToDB = True
        try:
            line = sketcher.CreateLine(ends[0][0], ends[0][1], 0.0, ends[1][0], ends[1][1], 0.0)
        finally:
            sketcher.AddToDB = False
        if line is None:
            raise SolidWorksError("SolidWorks did not draw the section line.")
        binding.wrap(line, self._mod.ISketchSegment).Select4(False, None)
        geometry = self._view_geometry_box(base)
        # the section view sits square to the section line, beside the parent
        upright = abs(mapping[1 + run][0]) < abs(mapping[1 + run][1])
        sides = [side] if side else (["right", "left"] if upright else ["below", "above"])
        gap = 2 * lay.DIMENSION_MARGIN_MM if gap_mm is None else float(gap_mm)
        start = lay.place_beside(geometry, (lay.width(geometry), lay.height(geometry)), sides[0], gap)
        view = binding.wrap(drawing.CreateSectionViewAt5(mm_to_m(start[0]), mm_to_m(start[1]), 0.0, str(label),
                                                         SW_CREATE_SECTION_CHANGE_DIRECTION if flip else 0, None, 0.0),
                            self._mod.IView)
        model.ClearSelection2(True)
        drawing.ActivateSheet(binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet).GetName())
        if view is None:
            raise SolidWorksError(f"SolidWorks could not cut section {label}-{label} through "
                                  f"({', '.join(f'{c:g}' for c in through)}) in view {parent}.")
        placement = self._resolve_placement(view, None, base.GetName2(), sides[0], gap_mm, sides=sides)
        return self._finish_view(view, name, None, placement)

    def add_detail_view(self, parent: str, center: list, radius_mm: float, scale=None, label: str = "B",
                        at: list | None = None, name: str | None = None) -> dict:
        """Detail view of the circle round model point `center` with radius_mm
        (model mm), enlarged (by default 2.5x the parent's scale, rounded to a
        standard scale) and placed at `at` or in free room."""
        model, drawing = self._require_drawing()
        base = self._view(parent)
        if len(center) != 3 or radius_mm <= 0:
            raise SolidWorksError("center is a model point [x, y, z] and radius_mm a model radius > 0.")
        mapping = self._model_map(base)
        middle = self._view_sketch_point(base, self._to_sheet(mapping, center))
        drawing.ActivateView(base.GetName2())
        sketcher = binding.wrap(model.SketchManager, self._mod.ISketchManager)
        model.ClearSelection2(True)
        sketcher.AddToDB = True
        try:
            circle = sketcher.CreateCircleByRadius(middle[0], middle[1], 0.0, mm_to_m(radius_mm))
        finally:
            sketcher.AddToDB = False
        if circle is None:
            raise SolidWorksError("SolidWorks did not draw the detail circle.")
        binding.wrap(circle, self._mod.ISketchSegment).Select4(False, None)
        if scale is None:
            wanted = base.ScaleDecimal * 2.5
            ratio = min(lay.STANDARD_SCALES, key=lambda r: abs(math.log(r[0] / r[1] / wanted)))
        else:
            ratio = lay.as_ratio(scale)
        frame, block = self._sheet_frame()
        size = 2 * radius_mm * ratio[0] / ratio[1]
        if at is None:
            taken = [self._view_geometry_box(v) for v in self._views()]
            spot = lay._free_spot((size, size), taken, lay.drawing_area(frame, block), lay.DIMENSION_MARGIN_MM)
            at = lay.center(spot) if spot else lay.center(frame)
        view = binding.wrap(drawing.CreateDetailViewAt4(mm_to_m(at[0]), mm_to_m(at[1]), 0.0, SW_DET_VIEW_STANDARD,
                                                        float(ratio[0]), float(ratio[1]), str(label),
                                                        SW_DET_CIRCLE_CIRCLE, True, False, False, 5),
                            self._mod.IView)
        model.ClearSelection2(True)
        drawing.ActivateSheet(binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet).GetName())
        if view is None:
            raise SolidWorksError(f"SolidWorks could not make detail {label} in view {parent}.")
        return self._finish_view(view, name, None, (float(at[0]), float(at[1])))

    def move_view(self, name: str, at: list | None = None, right_of: str | None = None,
                  left_of: str | None = None, above: str | None = None, below: str | None = None,
                  gap_mm: float | None = None, align: str = "center") -> dict:
        """Move a view: its geometry centred on at=[x, y] (sheet mm), or beside
        another view with gap_mm in between. A projected or section view stays
        aligned with its parent, so it only moves along that line."""
        self._require_drawing()
        view = self._view(name)
        beside, side = self._relative_args(right_of, left_of, above, below)
        placement = self._resolve_placement(view, at, beside, side, gap_mm, align)
        if placement is None:
            raise SolidWorksError("Say where: at=[x, y] or right_of / left_of / above / below another view.")
        self._move_view_box(view, placement)
        self._drawing.ForceRebuild3(False)
        return {"ok": True, "view": self._view_entry(view, with_mapping=False), **self._overlaps_for_view(view)}

    def set_view(self, name: str, scale=None, display: str | None = None, tangent_edges: bool | None = None,
                 new_name: str | None = None) -> dict:
        """Change a view: scale ('1:2', or 'sheet' to follow the sheet), display
        ('hidden_removed', 'hidden_visible', 'wireframe', 'shaded',
        'shaded_edges'), tangent edges shown or not, or its name."""
        self._require_drawing()
        view = self._view(name)
        if scale is not None:
            if str(scale).lower() == "sheet":
                view.UseSheetScale = True
            else:
                ratio = lay.as_ratio(scale)
                view.UseSheetScale = False
                view.ScaleDecimal = ratio[0] / ratio[1]
        if display is not None:
            mode = VIEW_DISPLAY_MODES.get(str(display).lower())
            if mode is None:
                raise SolidWorksError(f"Unknown display '{display}'. Use {', '.join(VIEW_DISPLAY_MODES)}.")
            view.SetDisplayMode4(False, mode, False, True, False)
        if tangent_edges is not None:
            view.SetDisplayTangentEdges2(SW_TANGENT_EDGES_VISIBLE if tangent_edges else SW_TANGENT_EDGES_HIDDEN)
        if new_name:
            if not view.SetName2(str(new_name)):
                raise SolidWorksError(f"SolidWorks refused the view name '{new_name}'.")
        self._drawing.ForceRebuild3(False)
        return {"ok": True, "view": self._view_entry(view, with_mapping=False), **self._overlaps_for_view(view)}

    def delete_view(self, name: str) -> dict:
        """Delete a view with its annotations (and views derived from it)."""
        model, _ = self._require_drawing()
        view = self._view(name)
        title = view.GetName2()
        self._select_view(view)
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        extension.DeleteSelection2(SW_DELETE_NO_OPTION)
        model.ClearSelection2(True)
        if any(v.GetName2() == title for v in self._views()):
            raise SolidWorksError(f"SolidWorks did not delete the view {title}.")
        return {"ok": True, "deleted": title, "views": [v.GetName2() for v in self._views()]}

    # --- references into a view -------------------------------------------------------

    def _visible_edges(self, view, mapping) -> list:
        """The view's visible model edges with what the tools need of them:
        {'edge', 'kind' ('line' | 'circle' | 'arc' | 'curve'), 'mid' (model mm),
        'sheet' (projected ends, or centre and radius), 'length' on the sheet}."""
        out = []
        for dispatch in view.GetVisibleEntities2(None, SW_VIEW_ENTITY_EDGE) or ():
            edge = binding.wrap(dispatch, self._mod.IEdge)
            curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
            params = edge.GetCurveParams2()
            mid = curve.Evaluate2((params[6] + params[7]) / 2.0, 0)[:3]
            entry = {"edge": edge, "mid": tuple(m_to_mm(c) for c in mid)}
            if curve.IsCircle():
                cp = curve.CircleParams
                centre = tuple(m_to_mm(c) for c in cp[:3])
                # seen face-on, its axis points at the viewer and moves nothing on the sheet
                tilt = math.hypot(*(sum(cp[3 + i] * mapping[1 + i][k] for i in range(3)) for k in range(2)))
                entry.update(kind="circle" if edge.GetStartVertex() is None else "arc",
                             center=centre, radius=m_to_mm(cp[6]), axis=tuple(cp[3:6]),
                             face_on=tilt < 1e-3 * max(math.hypot(*m) for m in mapping[1:]))
                c = self._to_sheet(mapping, centre)
                entry["sheet"] = (c[0], c[1])
                entry["length"] = 2 * math.pi * m_to_mm(cp[6])
            else:
                start = tuple(m_to_mm(c) for c in params[0:3])
                end = tuple(m_to_mm(c) for c in params[3:6])
                a, b = self._to_sheet(mapping, start), self._to_sheet(mapping, end)
                entry.update(kind="line" if curve.IsLine() else "curve", start=start, end=end)
                entry["sheet"] = (a[0], a[1], b[0], b[1])
                entry["length"] = math.hypot(b[0] - a[0], b[1] - a[1])
            out.append(entry)
        return out

    def _view_part(self, view):
        document = binding.wrap(view.ReferencedDocument, self._mod.IModelDoc2)
        if document is None or int(document.GetType()) != SW_DOC_PART:
            raise SolidWorksError(f"View {view.GetName2()} shows no part: references like 'edge:-x' and "
                                  "'hole:...' work in views of a part.")
        return document

    def _feature_faces(self, document, name: str) -> list:
        feature = binding.wrap(binding.wrap(document, self._mod.IPartDoc).FeatureByName(name), self._mod.IFeature)
        if feature is None:
            raise SolidWorksError(f"The part has no feature '{name}'.")
        faces = feature.GetFaces() or ()
        if not faces:
            raise SolidWorksError(f"Feature '{name}' has no faces of its own (suppressed, or a sketch?).")
        return [binding.wrap(f, self._mod.IFace2) for f in faces]

    def _on_faces(self, entry, faces) -> bool:
        return self._near_faces([mm_to_m(c) for c in entry["mid"]], faces)

    def _resolve_ref(self, view, ref, edges, mapping) -> dict:
        """The visible edge a semantic reference means in this view."""
        kind, value = lay.parse_ref(ref)
        if kind == "point":
            p = [mm_to_m(c) for c in value]
            best = min(edges, key=lambda e: math.dist(p, e["edge"].GetClosestPointOn(*p)[:3]), default=None)
            if best is None:
                raise SolidWorksError(f"View {view.GetName2()} shows no edges.")
            return {**best, "ref": ref}
        document = self._view_part(view)
        if kind == "face":
            normal, side = self._parse_face_selector(value)
            faces = [f for body in (binding.wrap(document, self._mod.IPartDoc).GetBodies2(0, True) or ())
                     for f in (binding.wrap(body, self._mod.IBody2).GetFaces() or ())]
            face, _ = self._pick_planar_face(faces, normal, side)
            if face is None:
                raise SolidWorksError(f"The part has no planar face facing {value}.")
            on = [e for e in edges if e["kind"] == "line" and e["length"] > 0.1 and self._on_faces(e, [face])]
            if not on:
                try:
                    looking = self._view_direction(mapping)
                except SolidWorksError:
                    looking = None
                if looking is not None and abs(normal[looking]) > 0.99:
                    raise SolidWorksError(f"The {value} face faces the viewer in view {view.GetName2()}: "
                                          "dimension to it in a view that sees it edge-on.")
                raise SolidWorksError(f"No visible edge of the {value} face in view {view.GetName2()}.")
            return {**max(on, key=lambda e: e["length"]), "ref": ref}
        faces = self._feature_faces(document, value)
        on = [e for e in edges if self._on_faces(e, faces)]
        if not on:
            raise SolidWorksError(f"Feature '{value}' shows no visible edge in view {view.GetName2()}.")
        round_edges = [e for e in on if e["kind"] in ("circle", "arc") and e["face_on"]]
        if kind == "hole":
            if not round_edges:
                raise SolidWorksError(f"'{value}' shows no circle in view {view.GetName2()}: "
                                      "use a view that looks along the hole.")
            return {**max(round_edges, key=lambda e: e["radius"]), "ref": ref}
        if round_edges:
            return {**max(round_edges, key=lambda e: e["radius"]), "ref": ref}
        return {**max(on, key=lambda e: e["length"]), "ref": ref}

    @staticmethod
    def _anchor(entry) -> tuple:
        s = entry["sheet"]
        return (s[0], s[1]) if len(s) == 2 else ((s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0)

    @staticmethod
    def _sheet_direction(entry):
        """Unit direction of a line edge on the sheet, or None."""
        s = entry["sheet"]
        if len(s) != 4:
            return None
        dx, dy = s[2] - s[0], s[3] - s[1]
        length = math.hypot(dx, dy)
        return (dx / length, dy / length) if length > 1e-9 else None

    # --- annotations: reading --------------------------------------------------------

    def _annotations(self, view) -> list:
        return [binding.wrap(a, self._mod.IAnnotation) for a in (view.GetAnnotations() or ())]

    def _annotation_entry(self, annotation, view_name: str, mapping=None) -> dict:
        kind = int(annotation.GetType())
        data = binding.wrap(annotation.GetDisplayData(), self._mod.IDisplayData)
        lines, texts, text_boxes, heights = [], [], [], []
        if data is not None:
            for i in range(data.GetLineCount()):
                raw = data.GetLineAtIndex(i)
                if raw and len(raw) >= 7:
                    lines.append(tuple(m_to_mm(c) for c in (raw[1], raw[2], raw[4], raw[5])))
            for i in range(data.GetTextCount()):
                text = data.GetTextAtIndex(i) or ""
                position = data.GetTextPositionAtIndex(i)
                h = m_to_mm(data.GetTextHeightAtIndex(i))
                texts.append(text)
                heights.append(h)
                if position and lay.clean_text(text):
                    text_boxes.append(lay.text_box(m_to_mm(position[0]), m_to_mm(position[1]), h,
                                                   data.GetTextAngleAtIndex(i), text))
            for i in range(data.GetArrowHeadCount()):
                head = data.GetArrowHeadAtIndex(i)
                if head:
                    x, y = m_to_mm(head[0]), m_to_mm(head[1])
                    lines.append((x, y, x, y))
        boxes = [lay.rect(*l) for l in lines] + text_boxes
        entry = {"id": f"{annotation.GetName()}@{view_name}", "view": view_name,
                 "text": lay.clean_text(" ".join(texts)), "box": lay.union(boxes),
                 "text_boxes": text_boxes, "lines": [], "leaders": [],
                 "text_height": round(max(heights), 2) if heights else None}
        if kind == SW_ANNOTATION_DIMENSION:
            dim = binding.wrap(annotation.GetSpecificAnnotation(), self._mod.IDisplayDimension)
            measured = binding.wrap(dim.GetDimension2(0), self._mod.IDimension)
            callout = bool(dim.IsHoleCallout())
            entry["type"] = "hole_callout" if callout else "dimension"
            entry["kind"] = DIMENSION_KINDS.get(int(dim.Type2), f"type {dim.Type2}")
            if measured is not None and not callout:
                value = measured.SystemValue
                entry["value"] = round(math.degrees(value) if entry["kind"] == "angular" else m_to_mm(value), 4)
                entry["name"] = measured.FullName
            if callout:
                entry["leaders"] = lines
            else:
                entry["lines"] = lines
                entry["axis"] = self._dimension_axis(entry, lines, mapping)
        elif kind == SW_ANNOTATION_NOTE:
            note = binding.wrap(annotation.GetSpecificAnnotation(), self._mod.INote)
            # a flat pattern's bend notes sit on their bend lines
            entry["type"] = "bend_note" if note is not None and note.IsBendLineNote else "note"
            entry["leaders"] = lines
            extent = note.GetExtent() if note is not None else None
            if extent and len(extent) >= 6:  # the text's real size: the estimate runs short on capitals
                text_box = lay.rect(*(m_to_mm(extent[i]) for i in (0, 1, 3, 4)))
                entry["text_boxes"] = [text_box]
                entry["box"] = lay.union([lay.rect(*l) for l in lines] + [text_box])
        else:
            entry["type"] = ANNOTATION_TYPES.get(kind, f"type {kind}")
            entry["text_boxes"] = []  # marks and lines carry no text to clash
        if not entry["text"]:
            entry.pop("text")
        return entry

    @staticmethod
    def _dimension_axis(entry, lines, mapping):
        """The model axis a linear dimension measures along in its view ('x',
        'y', 'z'); 'd' / 'r' for a diameter or radius; None at an angle."""
        kind = entry.get("kind")
        if kind == "diameter":
            return "d"
        if kind == "radius":
            return "r"
        found = lay.dimension_orientation(lines)
        if mapping is None or found is None:
            return None
        entry["orientation"] = found[0]
        x1, y1, x2, y2 = found[1]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        best, best_dot = None, 0.0
        for name, axis in zip("xyz", mapping[1:]):
            norm = math.hypot(*axis)
            if norm < 1e-9:
                continue
            dot = abs(dx * axis[0] + dy * axis[1]) / (length * norm)
            if dot > best_dot:
                best, best_dot = name, dot
        return best if best_dot > 0.999 else None

    def _all_annotation_entries(self, view_name: str | None = None) -> list:
        out = []
        views = [self._view(view_name)] if view_name else self._views()
        for view in views:
            mapping = self._model_map(view) if view.ReferencedDocument is not None else None
            out += [self._annotation_entry(a, view.GetName2(), mapping) for a in self._annotations(view)]
        if not view_name:
            out += [self._annotation_entry(a, "sheet") for a in self._sheet_annotations()]
        return out

    def _sheet_annotations(self) -> list:
        """Annotations placed on the sheet itself, without the sheet format's
        own notes (zone letters, title block labels), which SolidWorks lists
        with them but owns by the template."""
        return [a for a in self._annotations(self._sheet_view())
                if int(a.OwnerType) != SW_ANNOTATION_OWNER_TEMPLATE]

    @staticmethod
    def _public_entry(entry) -> dict:
        out = {k: v for k, v in entry.items() if k not in ("text_boxes", "lines", "leaders")}
        if out.get("box") is not None:
            out["box_mm"] = lay.rounded(out.pop("box"))
        else:
            out.pop("box", None)
        return out

    def list_annotations(self, view: str | None = None) -> dict:
        """Annotations on the sheet (or in one view): id ('<name>@<view>'), type
        (dimension, hole_callout, note, center_mark, ...), shown text, value (mm
        or degrees) and bounding box (sheet mm)."""
        self._require_drawing()
        entries = [self._public_entry(e) for e in self._all_annotation_entries(view)]
        return {"ok": True, "count": len(entries), "annotations": entries}

    def _find_annotation(self, annotation_id: str):
        """(IAnnotation, view) for '<name>@<view>' or a bare name."""
        name, _, view_name = str(annotation_id).partition("@")
        views = [self._view(view_name)] if view_name and view_name != "sheet" else \
            ([] if view_name == "sheet" else self._views())
        found = [(a, v) for v in views for a in self._annotations(v) if a.GetName() == name]
        if not view_name or view_name == "sheet":
            found += [(a, self._sheet_view()) for a in self._sheet_annotations() if a.GetName() == name]
        if not found:
            raise SolidWorksError(f"No annotation '{annotation_id}' (list_annotations lists them).")
        if len(found) > 1:
            raise SolidWorksError(f"'{name}' is in several views; give '{name}@<view>'.")
        return found[0]

    # --- annotations: adding -----------------------------------------------------------

    def _created(self, annotation, view, mapping=None) -> dict:
        """The result entry of a new annotation, with what it now overlaps."""
        view_name = "sheet" if int(view.Type) == 1 else view.GetName2()  # swDrawingSheet
        entry = self._annotation_entry(annotation, view_name, mapping)
        frame, block = self._sheet_frame()
        warnings = []
        box = entry.get("box")
        if box is not None:
            if not lay.inside(box, frame, 0.5):
                warnings.append("reaches outside the drawing frame")
            if lay.overlap(box, block, 0.5):
                warnings.append("lies over the title block")
            clashes = [e["id"] for e in self._all_annotation_entries()
                       if e["id"] != entry["id"] and any(lay.overlap(t, u, 0.2) for t in entry["text_boxes"]
                                                         for u in e.get("text_boxes", ()))]
            if clashes:
                warnings.append(f"text overlaps {', '.join(clashes)}")
        out = self._public_entry(entry)
        if warnings:
            out["warnings"] = warnings
        return out

    def _dimension_rows(self, view, geometry, side: str, span) -> list:
        """How far from the geometry box the dimension lines on `side` already
        sit, for dimensions whose stretch overlaps `span` along that side."""
        rows = []
        for annotation in self._annotations(view):
            if int(annotation.GetType()) != SW_ANNOTATION_DIMENSION:
                continue
            entry = self._annotation_entry(annotation, view.GetName2())
            row = lay.dimension_row(entry["lines"], geometry)
            if row and row[0] == side and row[2][1] >= span[0] - 2 and row[2][0] <= span[1] + 2:
                rows.append(row[1])
        return rows

    def add_dimension(self, view: str, ref: object, to: object = None, direction: str = "auto",
                      side: str | None = None, offset_mm: float | None = None) -> dict:
        """Dimension between two references in a view, or the size of one.

        ref / to: 'edge:-x' (the face facing -x, seen edge-on), 'hole:<feature>'
        (to its centre; alone: its diameter), 'feature:<name>' (a fillet alone:
        its radius), or a model point [x, y, z] (the edge nearest it).
        direction: 'auto', 'horizontal', 'vertical' or 'aligned' (sheet).
        side: where the dimension line goes ('below', 'above', 'left', 'right';
        default the side nearer the geometry); offset_mm: its distance from the
        view's outline (default the first free row: 10, 18, 26, ...).
        """
        model, drawing = self._require_drawing()
        v = self._view(view)
        mapping = self._model_map(v)
        edges = self._visible_edges(v, mapping)
        first = self._resolve_ref(v, ref, edges, mapping)
        second = self._resolve_ref(v, to, edges, mapping) if to is not None else None
        geometry = self._view_geometry_box(v, mapping)
        model.ClearSelection2(True)
        if not v.SelectEntity(first["edge"], False) or (second and not v.SelectEntity(second["edge"], True)):
            model.ClearSelection2(True)
            raise SolidWorksError("SolidWorks would not select the referenced edges in the view.")
        try:
            dim = self._place_dimension(model, first, second, geometry, v, direction, side, offset_mm)
        finally:
            model.ClearSelection2(True)
        if dim is None:
            raise SolidWorksError(f"SolidWorks made no dimension between {ref} and {to} in view {view}.")
        dim = binding.wrap(dim, self._mod.IDisplayDimension)
        annotation = binding.wrap(dim.GetAnnotation(), self._mod.IAnnotation)
        annotation.Color = 0  # a driven dimension prints grey otherwise
        return {"ok": True, **self._created(annotation, v, mapping)}

    def _place_dimension(self, model, first, second, geometry, view, direction, side, offset_mm):
        a = self._anchor(first)
        if second is None:
            if first["kind"] in ("circle", "arc"):
                # the text off the view: out from its centre through the circle, past the outline
                cx, cy = lay.center(geometry)
                dx, dy = a[0] - cx, a[1] - cy
                length = math.hypot(dx, dy)
                ux, uy = (dx / length, dy / length) if length > 1e-6 else (math.sqrt(0.5), math.sqrt(0.5))
                to_x = ((geometry[2] if ux > 0 else geometry[0]) - a[0]) / ux if abs(ux) > 1e-6 else math.inf
                to_y = ((geometry[3] if uy > 0 else geometry[1]) - a[1]) / uy if abs(uy) > 1e-6 else math.inf
                reach = min(to_x, to_y) + 8.0
                spot = (a[0] + reach * ux, a[1] + reach * uy)
                if first["kind"] == "circle":
                    return model.AddDiameterDimension2(mm_to_m(spot[0]), mm_to_m(spot[1]), 0.0)
                return model.AddRadialDimension2(mm_to_m(spot[0]), mm_to_m(spot[1]), 0.0)
            s = first["sheet"]
            b = (s[2], s[3])
            a = (s[0], s[1])
        else:
            b = self._anchor(second)
        measuring = self._measuring(first, second, a, b, direction)
        if measuring == "aligned":
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / length, dx / length
            off = 10.0 if offset_mm is None else float(offset_mm)
            spot = ((a[0] + b[0]) / 2 + nx * off, (a[1] + b[1]) / 2 + ny * off)
            return model.AddDimension2(mm_to_m(spot[0]), mm_to_m(spot[1]), 0.0)
        if measuring == "horizontal":
            side = side or ("below" if (a[1] + b[1]) / 2 <= lay.center(geometry)[1] else "above")
            if side not in ("below", "above"):
                raise SolidWorksError("A horizontal dimension goes 'below' or 'above'.")
            span = (min(a[0], b[0]), max(a[0], b[0]))
            off = lay.next_row(self._dimension_rows(view, geometry, side, span)) if offset_mm is None else float(offset_mm)
            y = geometry[1] - off if side == "below" else geometry[3] + off
            return model.AddHorizontalDimension2(mm_to_m((a[0] + b[0]) / 2), mm_to_m(y), 0.0)
        side = side or ("left" if (a[0] + b[0]) / 2 <= lay.center(geometry)[0] else "right")
        if side not in ("left", "right"):
            raise SolidWorksError("A vertical dimension goes 'left' or 'right'.")
        span = (min(a[1], b[1]), max(a[1], b[1]))
        off = lay.next_row(self._dimension_rows(view, geometry, side, span)) if offset_mm is None else float(offset_mm)
        x = geometry[0] - off if side == "left" else geometry[2] + off
        return model.AddVerticalDimension2(mm_to_m(x), mm_to_m((a[1] + b[1]) / 2), 0.0)

    def _measuring(self, first, second, a, b, direction) -> str:
        direction = str(direction or "auto").lower()
        if direction in ("horizontal", "vertical", "aligned"):
            return direction
        if direction != "auto":
            raise SolidWorksError(f"Unknown direction '{direction}'. Use auto, horizontal, vertical or aligned.")
        for entry in (first, second):
            d = self._sheet_direction(entry) if entry is not None and entry["kind"] == "line" else None
            if d is not None and (second is not None):
                # distance to a line is measured square to it
                if abs(d[1]) > 0.999:
                    return "horizontal"
                if abs(d[0]) > 0.999:
                    return "vertical"
        if second is None:
            d = self._sheet_direction(first)
            if d is not None:
                return "horizontal" if abs(d[0]) >= abs(d[1]) else "vertical"
        return "horizontal" if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else "vertical"

    def add_dimension_set(self, view: str, base: object, refs: list, style: str = "baseline",
                          direction: str = "auto", side: str | None = None) -> dict:
        """Several dimensions in one go: 'baseline' measures every ref from base,
        each on its own row; 'chain' measures base -> ref1 -> ref2 ... in one row;
        'ordinate' gives base as 0 and each ref its distance from it along one
        line. Refs as in add_dimension (e.g. 'hole:M4_CBore_1')."""
        if not refs:
            raise SolidWorksError("Give the references to dimension: refs=['hole:...', ...].")
        style = str(style).lower()
        if style not in ("baseline", "chain", "ordinate"):
            raise SolidWorksError(f"Unknown style '{style}'. Use 'baseline', 'chain' or 'ordinate'.")
        if style == "ordinate":
            return self._ordinate_set(view, base, refs, direction, side)
        created = []
        if style == "baseline":
            for ref in refs:
                created.append(self.add_dimension(view, base, ref, direction, side))
        else:
            chain = [base, *refs]
            first = self.add_dimension(view, chain[0], chain[1], direction, side)
            created.append(first)
            row = self._row_of(first)
            v = self._view(view)
            placed = lay.dimension_row(self._annotation_entry(
                self._find_annotation(first["id"])[0], v.GetName2())["lines"], self._view_geometry_box(v))
            side = side or (placed[0] if placed else None)
            if direction == "auto" and first.get("orientation") in ("horizontal", "vertical"):
                direction = first["orientation"]
            for a, b in zip(chain[1:], chain[2:]):
                created.append(self.add_dimension(view, a, b, direction, side, row))
        return {"ok": True, "count": len(created), "dimensions": [c for c in created],
                "warnings": [w for c in created for w in c.get("warnings", ())] or None}

    def _row_of(self, created) -> float | None:
        """The offset of a just-made dimension's line from its view's geometry."""
        view = self._view(created["view"])
        annotation, _ = self._find_annotation(created["id"])
        entry = self._annotation_entry(annotation, view.GetName2())
        row = lay.dimension_row(entry["lines"], self._view_geometry_box(view))
        return row[1] if row else None

    def _hole_signature(self, document, name: str):
        """What makes holes the same: their round edges' radii and depths below
        the hole's start, rounded to a micrometre."""
        faces = self._feature_faces(document, name)
        circles = []
        for face in faces:
            for edge in face.GetEdges() or ():
                curve = binding.wrap(binding.wrap(edge, self._mod.IEdge).GetCurve(), self._mod.ICurve)
                if curve.IsCircle():
                    cp = curve.CircleParams
                    circles.append((cp[:3], cp[3:6], cp[6]))
        if not circles:
            return None
        axis = circles[0][1]
        heights = [sum(c[0][i] * axis[i] for i in range(3)) for c in circles]
        top = max(heights)
        return tuple(sorted({(round(m_to_mm(c[2]), 3), round(m_to_mm(top - h), 3)) for c, h in zip(circles, heights)}))

    def _hole_features(self, document) -> list:
        """Names of the part's hole features (Hole Wizard and simple holes)."""
        out = []
        feature = binding.wrap(document.FirstFeature(), self._mod.IFeature)
        while feature is not None:
            if feature.GetTypeName2() in ("HoleWzd", "AdvHoleWzd", "SimpleHole") and not feature.IsSuppressed2(1, None)[0]:
                out.append(feature.Name)
            feature = binding.wrap(feature.GetNextFeature(), self._mod.IFeature)
        return out

    def add_hole_callout(self, view: str, hole: str, at: list | None = None, count: bool = True) -> dict:
        """Hole callout (diameter, depth, counterbore/countersink, thread) on a
        hole feature, linked to the model. With count=True identical holes of
        the part that show in the view are counted into the text ('4X ...').
        at=[x, y] is where the text goes (sheet mm); by default off the view's
        nearest corner, clear of other text."""
        model, drawing = self._require_drawing()
        v = self._view(view)
        mapping = self._model_map(v)
        edges = self._visible_edges(v, mapping)
        target = self._resolve_ref(v, f"hole:{hole}", edges, mapping)
        document = self._view_part(v)
        geometry = self._view_geometry_box(v, mapping)
        centre = self._anchor(target)
        placed = at is not None
        if not placed:  # above (or below) the view, the text running off to the right of the hole
            above = centre[1] >= lay.center(geometry)[1]
            at = (centre[0] + _CALLOUT_OFFSET_MM / 2.0,
                  geometry[3] + _CALLOUT_OFFSET_MM if above else geometry[1] - _CALLOUT_OFFSET_MM)
        model.ClearSelection2(True)
        if not v.SelectEntity(target["edge"], False):
            raise SolidWorksError(f"Could not select the edge of {hole} in view {view}.")
        try:
            callout = drawing.AddHoleCallout2(mm_to_m(at[0]), mm_to_m(at[1]), 0.0)
        finally:
            model.ClearSelection2(True)
        if callout is None:
            raise SolidWorksError(f"SolidWorks made no hole callout on {hole} in view {view}.")
        callout = binding.wrap(callout, self._mod.IDisplayDimension)
        same = [hole]
        if count:
            mine = self._hole_signature(document, hole)
            visible = [e for e in edges if e["kind"] == "circle"]
            for other in self._hole_features(document):
                if other == hole or self._hole_signature(document, other) != mine:
                    continue
                faces = self._feature_faces(document, other)
                if any(self._on_faces(e, faces) for e in visible):
                    same.append(other)
            if len(same) > 1:
                self._prefix_callout(callout, lay.count_prefix(len(same)))
        annotation = binding.wrap(callout.GetAnnotation(), self._mod.IAnnotation)
        annotation.Color = 0  # prints grey otherwise, as a driven dimension
        if not placed:
            self._clear_of_text(annotation, v, 1.0 if at[1] >= lay.center(geometry)[1] else -1.0)
        return {"ok": True, **self._created(annotation, v, mapping), "holes": same}

    def _clear_of_text(self, annotation, view, direction: float) -> None:
        """Move a new annotation away from the view (up or down in 7 mm steps)
        until its text overlaps no other text, at most six times."""
        mine_id = f"{annotation.GetName()}@{view.GetName2()}"
        others = [t for e in self._all_annotation_entries() if e["id"] != mine_id for t in e.get("text_boxes", ())]
        for _ in range(6):
            mine = self._annotation_entry(annotation, view.GetName2())["text_boxes"]
            if not any(lay.overlap(t, u, 0.2) for t in mine for u in others):
                return
            x, y = annotation.GetPosition()[:2]
            annotation.SetPosition2(x, y + direction * mm_to_m(7.0), 0.0)

    def _prefix_callout(self, callout, prefix: str) -> None:
        """Put the count in front of the callout's first line and keep it live.

        A callout shows its first line in slot 3 and keeps its definition, with
        the model's variables, in slot 7. Writing the definition back into slot
        3 keeps the values linked; writing slot 7 itself turned a two-line
        counterbore callout into one line (verified).
        """
        definition = callout.GetText(SW_DIMENSION_TEXT_CALLOUT_ABOVE_DEFINITION) or ""
        slot = SW_DIMENSION_TEXT_CALLOUT_ABOVE
        if not definition:  # a one-line callout: its text sits in the prefix
            definition = callout.GetText(SW_DIMENSION_TEXT_PREFIX_DEFINITION) or ""
            slot = SW_DIMENSION_TEXT_PREFIX
        callout.SetText(slot, prefix + re.sub(r"^\d+X ", "", definition))

    def _select_annotation(self, annotation, append: bool = False) -> bool:
        """Select an annotation. IAnnotation.Select3 refuses None for its
        selection data (returns False, verified), so it gets a real one."""
        model, _ = self._require_drawing()
        selection = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        return bool(annotation.Select3(append, selection.CreateSelectData()))

    def _delete_annotation(self, annotation) -> None:
        model, _ = self._require_drawing()
        model.ClearSelection2(True)
        if self._select_annotation(annotation):
            binding.wrap(model.Extension, self._mod.IModelDocExtension).DeleteSelection2(SW_DELETE_NO_OPTION)
        model.ClearSelection2(True)

    def import_dimensions(self, view: str | None = None, kinds: list | None = None) -> dict:
        """Bring the model's own annotations into the drawing: 'model' (the
        dimensions marked for drawing, each shown once), 'callouts' (one hole
        callout per set of identical Hole Wizard holes, counted: '4X ...'),
        'hole_locations' (each Hole Wizard hole's centre dimensioned from the
        left and bottom edges of the view). Default: all three, into every view
        (or into one); callouts and locations go into the view where most of
        the holes show as circles."""
        model, drawing = self._require_drawing()
        kinds = [k.lower() for k in (kinds or ["model", "callouts", "hole_locations"])]
        unknown = set(kinds) - {"model", "callouts", "hole_locations"}
        if unknown:
            raise SolidWorksError(f"Unknown kinds {sorted(unknown)}. Use model, callouts, hole_locations.")
        views = [self._view(view)] if view else self._views()
        before = {e["id"] for e in self._all_annotation_entries()}
        result = {"ok": True}
        if "model" in kinds:
            if view:
                self._select_view(views[0])
            # a drawing never saved gets nothing from the dimensions of absorbed (hidden)
            # sketches, a box's own, until hidden features' dimensions are asked for too
            for hidden in (False, True):
                if drawing.InsertModelAnnotations3(SW_IMPORT_ENTIRE_MODEL, SW_INSERT_DIMENSIONS, view is None,
                                                   True, hidden, False) is not None:
                    break
            model.ClearSelection2(True)
            self._color_dimensions_black(views)
        holes = self._hole_view(views) if {"callouts", "hole_locations"} & set(kinds) else None
        if "callouts" in kinds:
            result["hole_callouts"] = self._call_out_holes(holes)
        if "hole_locations" in kinds:
            result["hole_locations"] = self._locate_holes(holes)
        self._drawing.ForceRebuild3(False)
        added = [self._public_entry(e) for e in self._all_annotation_entries() if e["id"] not in before]
        result.update(count=len(added), added=added)
        return result

    def _color_dimensions_black(self, views) -> None:
        for v in views:
            for annotation in self._annotations(v):
                if int(annotation.GetType()) == SW_ANNOTATION_DIMENSION:
                    annotation.Color = 0

    def _hole_view(self, views):
        """(view, [(hole, edge entry)], edges, mapping) for the view where the
        most Hole Wizard holes show as circles, or None."""
        best, best_count = None, 0
        for v in views:
            document = binding.wrap(v.ReferencedDocument, self._mod.IModelDoc2)
            if document is None or int(document.GetType()) != SW_DOC_PART:
                continue
            part = binding.wrap(document, self._mod.IPartDoc)
            holes = [h for h in self._hole_features(document)
                     if binding.wrap(part.FeatureByName(h), self._mod.IFeature).GetTypeName2() != "SimpleHole"]
            if not holes:
                continue
            mapping = self._model_map(v)
            edges = self._visible_edges(v, mapping)
            shown = []
            for h in holes:
                try:
                    shown.append((h, self._resolve_ref(v, f"hole:{h}", edges, mapping)))
                except SolidWorksError:
                    pass
            if len(shown) > best_count:
                best, best_count = (v, shown, edges, mapping), len(shown)
        return best

    def _call_out_holes(self, holes) -> list:
        """One counted callout per set of identical holes in the hole view."""
        if holes is None:
            return []
        v, shown, _, _ = holes
        document = self._view_part(v)
        groups = {}
        for name, entry in shown:
            groups.setdefault(self._hole_signature(document, name), []).append((name, entry))
        made = []
        for members in groups.values():
            # the top-most (then right-most) hole carries it: its callout goes above the view
            name = max(members, key=lambda m: (round(m[1]["sheet"][1], 3), m[1]["sheet"][0]))[0]
            created = self.add_hole_callout(v.GetName2(), name)
            made.append({"id": created["id"], "holes": created["holes"]})
        return made

    def _locate_holes(self, holes) -> list:
        """Dimension each hole's centre from the view's left and bottom edges,
        as baseline dimensions; centres on one line share a dimension."""
        if holes is None:
            return []
        v, shown, edges, _ = holes
        lines = [e for e in edges if e["kind"] == "line" and e["length"] > 1.0]
        verticals = [e for e in lines if abs(self._sheet_direction(e)[0]) < 1e-3]
        horizontals = [e for e in lines if abs(self._sheet_direction(e)[1]) < 1e-3]
        made = []
        if verticals:
            left = min(verticals, key=lambda e: e["sheet"][0])
            for h, _ in self._distinct(shown, 0):
                made.append(self.add_dimension(v.GetName2(), list(left["mid"]), f"hole:{h}", "horizontal", "below")["id"])
        if horizontals:
            bottom = min(horizontals, key=lambda e: e["sheet"][1])
            for h, _ in self._distinct(shown, 1):
                made.append(self.add_dimension(v.GetName2(), list(bottom["mid"]), f"hole:{h}", "vertical", "left")["id"])
        return made

    @staticmethod
    def _distinct(shown, coordinate: int) -> list:
        """One hole per distinct centre position along a sheet coordinate,
        nearest the datum first."""
        out, seen = [], []
        for name, entry in sorted(shown, key=lambda s: s[1]["sheet"][coordinate]):
            position = entry["sheet"][coordinate]
            if all(abs(position - p) > 0.01 for p in seen):
                seen.append(position)
                out.append((name, entry))
        return out

    def _ordinate_set(self, view: str, base, refs, direction, side) -> dict:
        """Ordinate dimensions: base is 0, every ref its distance from it."""
        model, drawing = self._require_drawing()
        v = self._view(view)
        mapping = self._model_map(v)
        edges = self._visible_edges(v, mapping)
        zero = self._resolve_ref(v, base, edges, mapping)
        targets = [self._resolve_ref(v, r, edges, mapping) for r in refs]
        geometry = self._view_geometry_box(v, mapping)
        direction = str(direction or "auto").lower()
        if direction == "auto":
            line = self._sheet_direction(zero) if zero["kind"] == "line" else None
            if line is not None:
                direction = "horizontal" if abs(line[1]) > abs(line[0]) else "vertical"
            else:
                a, b = self._anchor(zero), self._anchor(targets[0])
                direction = "horizontal" if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else "vertical"
        if direction not in ("horizontal", "vertical"):
            raise SolidWorksError("Ordinate dimensions run 'horizontal' or 'vertical'.")
        a = self._anchor(zero)
        if direction == "horizontal":
            side = side or "below"
            if side not in ("below", "above"):
                raise SolidWorksError("Horizontal ordinates go 'below' or 'above' the view.")
            spot = (a[0], geometry[1] - lay.FIRST_ROW_MM if side == "below" else geometry[3] + lay.FIRST_ROW_MM)
        else:
            side = side or "left"
            if side not in ("left", "right"):
                raise SolidWorksError("Vertical ordinates go 'left' or 'right' of the view.")
            spot = (geometry[0] - lay.FIRST_ROW_MM if side == "left" else geometry[2] + lay.FIRST_ROW_MM, a[1])
        before = {a.GetName() for a in self._annotations(v)}
        model.ClearSelection2(True)
        if not v.SelectEntity(zero["edge"], False) or not all(v.SelectEntity(t["edge"], True) for t in targets):
            model.ClearSelection2(True)
            raise SolidWorksError("SolidWorks would not select the referenced edges in the view.")
        try:
            drawing.AddOrdinateDimension2(SW_ORDINATE_HORIZONTAL if direction == "horizontal" else SW_ORDINATE_VERTICAL,
                                          mm_to_m(spot[0]), mm_to_m(spot[1]), 0.0)
        finally:
            model.ClearSelection2(True)
        added = [a for a in self._annotations(v) if a.GetName() not in before]
        if not added:
            raise SolidWorksError(f"SolidWorks made no ordinate dimensions in view {view}.")
        for annotation in added:
            annotation.Color = 0  # driven dimensions print grey otherwise
        made = [self._created(a, v, mapping) for a in added]
        return {"ok": True, "count": len(made), "dimensions": made,
                "warnings": [w for m in made for w in m.get("warnings", ())] or None}

    # --- symbols on edges ---------------------------------------------------------------

    def _attach_to(self, view, ref):
        """Select the edge a reference means, picked at a point on it, so a
        symbol inserted next attaches there and not at the edge's end.
        Returns (view, mapping, point on the edge, geometry box)."""
        model, _ = self._require_drawing()
        v = self._view(view)
        mapping = self._model_map(v)
        entry = self._resolve_ref(v, ref, self._visible_edges(v, mapping), mapping)
        point = self._to_sheet(mapping, entry["mid"])
        model.ClearSelection2(True)
        if not v.SelectEntity(entry["edge"], False):
            raise SolidWorksError(f"Could not select {ref} in view {view}.")
        selection = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        selection.SetSelectionPoint2(1, -1, mm_to_m(point[0]), mm_to_m(point[1]), 0.0)
        return v, mapping, point, self._view_geometry_box(v, mapping)

    @staticmethod
    def _off_the_view(geometry, point, distance: float) -> tuple:
        """A spot `distance` mm off the view's geometry, straight out from the
        point on its nearer side."""
        cx, cy = lay.center(geometry)
        dx, dy = point[0] - cx, point[1] - cy
        w, h = max(lay.width(geometry), 1e-6), max(lay.height(geometry), 1e-6)
        if abs(dx) / w > abs(dy) / h:
            return (geometry[2] + distance if dx > 0 else geometry[0] - distance), point[1]
        return point[0], (geometry[3] + distance if dy >= 0 else geometry[1] - distance)

    def _finish_symbol(self, specific, view, mapping, at) -> dict:
        model, _ = self._require_drawing()
        model.ClearSelection2(True)
        if specific is None:
            raise SolidWorksError(f"SolidWorks did not insert the symbol in view {view.GetName2()}.")
        annotation = binding.wrap(specific.GetAnnotation(), self._mod.IAnnotation)
        annotation.SetPosition2(mm_to_m(at[0]), mm_to_m(at[1]), 0.0)
        annotation.Color = 0
        model.GraphicsRedraw2()
        return {"ok": True, **self._created(annotation, view, mapping)}

    def add_surface_finish(self, view: str, ref, roughness: str = "Ra 3.2", process: str = "machined",
                           at: list | None = None) -> dict:
        """Surface texture symbol (ISO 1302) on an edge of a view, e.g.
        roughness 'Ra 1.6'; process 'machined' (material removal required),
        'any' or 'no_removal'. at=[x, y] places it (sheet mm), by default
        off the view, straight out from the edge."""
        kind = SURFACE_FINISH_PROCESSES.get(str(process).lower())
        if kind is None:
            raise SolidWorksError(f"Unknown process '{process}'. Use {', '.join(SURFACE_FINISH_PROCESSES)}.")
        model, _ = self._require_drawing()
        v, mapping, point, geometry = self._attach_to(view, ref)
        at = at or self._off_the_view(geometry, point, 12.0)
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        symbol = extension.InsertSurfaceFinishSymbol3(kind, SW_LEADER_STRAIGHT, mm_to_m(at[0]), mm_to_m(at[1]), 0.0,
                                                      0, SW_ARROW_CLOSED, "", "", "", "", "", "", "")
        symbol = binding.wrap(symbol, self._mod.ISFSymbol)
        if symbol is not None and roughness:
            symbol.SetText(SW_SF_ROUGHNESS, str(roughness))
        return self._finish_symbol(symbol, v, mapping, at)

    def add_datum(self, view: str, ref, label: str = "A", at: list | None = None) -> dict:
        """Datum feature symbol (a lettered box on a filled triangle) on an
        edge of a view; at=[x, y] places it (sheet mm), by default 10 mm off
        the view, straight out from the edge."""
        model, _ = self._require_drawing()
        if not str(label).strip():
            raise SolidWorksError("A datum needs a letter: label='A'.")
        v, mapping, point, geometry = self._attach_to(view, ref)
        at = at or self._off_the_view(geometry, point, 10.0)
        tag = binding.wrap(model.InsertDatumTag2(), self._mod.IDatumTag)
        if tag is not None:
            tag.SetLabel(str(label).strip())
        return self._finish_symbol(tag, v, mapping, at)

    def add_gtol(self, view: str, ref, symbol: str, tolerance: str, datums: list | None = None,
                 diameter: bool = False, material_condition: str | None = None, at: list | None = None) -> dict:
        """Geometric tolerance frame (ISO 1101) with a leader to an edge of a
        view: symbol 'flatness', 'parallelism', 'perpendicularity',
        'position', ... ; tolerance '0.05'; datums ['A', 'B']; diameter=True
        for a round zone (position of a hole); material_condition 'MMC' or
        'LMC'. at=[x, y] places it, by default off the view."""
        model, _ = self._require_drawing()
        token = GTOL_SYMBOLS.get(str(symbol).lower().replace(" ", "_"))
        if token is None:
            raise SolidWorksError(f"Unknown symbol '{symbol}'. Use {', '.join(sorted(GTOL_SYMBOLS))}.")
        datums = [str(d).strip() for d in (datums or [])]
        if len(datums) > 3:
            raise SolidWorksError("A tolerance frame takes up to three datums.")
        condition = ""
        if material_condition:
            condition = {"mmc": "<MOD-MMC>", "lmc": "<MOD-LMC>"}.get(str(material_condition).lower())
            if condition is None:
                raise SolidWorksError("material_condition is 'MMC' or 'LMC'.")
        v, mapping, point, geometry = self._attach_to(view, ref)
        at = at or self._off_the_view(geometry, point, 15.0)
        frame = binding.wrap(model.InsertGtol(), self._mod.IGtol)
        if frame is not None:
            frame.SetFrameSymbols2(1, token, bool(diameter), condition, False, "", "", "", "")
            frame.SetFrameValues2(1, str(tolerance), "", *(datums + ["", "", ""])[:3])
        return self._finish_symbol(frame, v, mapping, at)

    def add_weld_symbol(self, view: str, ref, weld: str = "fillet", size: str = "", length: str = "",
                        side: str = "arrow", contour: str = "none", all_around: bool = False, field: bool = False,
                        at: list | None = None) -> dict:
        """Weld symbol (ISO 2553) with its arrow on an edge of a view: weld
        'fillet', 'square', 'v', 'bevel', 'u', 'j', ... ; size left of the
        symbol ('a4' throat, 'z5' leg), length right of it ('100', '3x50 (100)'
        for an intermittent weld); side 'arrow', 'other' or 'both' sides of
        the joint; contour 'flat', 'convex', 'concave'; all_around adds the
        circle, field the flag. at=[x, y] places it, by default off the view."""
        symbol = WELD_SYMBOLS.get(str(weld).lower())
        if symbol is None:
            raise SolidWorksError(f"Unknown weld '{weld}'. Use {', '.join(WELD_SYMBOLS)}.")
        shape = WELD_CONTOURS.get(str(contour).lower())
        if shape is None:
            raise SolidWorksError(f"Unknown contour '{contour}'. Use {', '.join(WELD_CONTOURS)}.")
        sides = {"arrow": (False,), "other": (True,), "both": (False, True)}.get(str(side).lower())
        if sides is None:
            raise SolidWorksError(f"Unknown side '{side}'. Use arrow, other or both.")
        model, _ = self._require_drawing()
        v, mapping, point, geometry = self._attach_to(view, ref)
        at = at or self._off_the_view(geometry, point, 15.0)
        mark = binding.wrap(model.InsertWeldSymbol3(), self._mod.IWeldSymbol)
        if mark is not None:
            for top in sides:  # SetText's Top: the other side, above the reference line
                if not mark.SetText(top, str(size), symbol, str(length), "", shape):
                    raise SolidWorksError("SolidWorks would not write the weld symbol.")
            if all_around:
                mark.SetPeripheral(True)
            if field:
                mark.SetFieldWeld(SW_FIELD_WELD)
        return self._finish_symbol(mark, v, mapping, at)

    def _import_welds(self, view=None) -> int:
        """Bring the part's weld beads into the views, with their weld
        symbols; returns how many came."""
        model, drawing = self._require_drawing()
        if view is not None:
            self._select_view(view)
        made = drawing.InsertModelAnnotations3(SW_IMPORT_ENTIRE_MODEL, SW_INSERT_WELDS, view is None, True, False,
                                               False)
        model.ClearSelection2(True)
        return len(made or ())

    def add_cut_list_table(self, at: list | None = None, text_height_mm: float = 2.5) -> dict:
        """The weldment's cut list as a table on the sheet: item, quantity,
        description, length and end angles of each kind of member, and the
        weld beads' total length."""
        self._require_drawing()
        part = self._drawing_part_document()
        if part is None:
            raise SolidWorksError("The drawing shows no part.")
        items = self._cut_list_items(part)
        if not items:
            raise SolidWorksError("The drawing's part is not a weldment: it has no cut list.")
        rows = [["CUT LIST", "", "", "", ""], ["ITEM", "QTY", "DESCRIPTION", "LENGTH", "ANGLES"]]
        for k, item in enumerate(items, 1):
            angles = "/".join(f"{item[a]:g}°" for a in ("angle1_deg", "angle2_deg") if a in item)
            length = f"{item['length_mm']:g}" if "length_mm" in item else ""
            rows.append([str(k), str(item["quantity"]), item["description"], length, angles])
        beads = [self._bead_report(b, part) for b in self._weld_beads(part)]
        if beads:
            rows.append(["", "", f"WELDS: {len(beads)}", f"{sum(b['weld_length_mm'] for b in beads):g}", ""])
        return self.add_table(rows, at, True, text_height_mm)

    # --- tables ------------------------------------------------------------------------

    def add_table(self, rows: list, at: list | None = None, title: bool = True, text_height_mm: float = 2.5,
                  column_widths_mm: list | None = None) -> dict:
        """A table on the sheet: rows = [[cell, cell, ...], ...] of equal
        length; title=True merges the first row into one title cell. at=[x, y]
        is its top-left corner (sheet mm), by default free room top right."""
        model, drawing = self._require_drawing()
        if not rows or not all(isinstance(r, (list, tuple)) for r in rows):
            raise SolidWorksError("rows is a list of rows, each a list of cells.")
        columns = max(len(r) for r in rows)
        cells = [[str(c) for c in (*r, *[""] * (columns - len(r)))] for r in rows]
        row_height = text_height_mm * 1.8
        if column_widths_mm is None:
            body = cells[1:] if title and len(cells) > 1 else cells
            # wider per character than a note's estimate: a cell too narrow wraps ('42CrMo' / '4')
            column_widths_mm = [max(len(lay.clean_text(r[j])) for r in body) * 0.8 * text_height_mm + 4.0
                                for j in range(columns)]
        if len(column_widths_mm) != columns:
            raise SolidWorksError(f"column_widths_mm needs {columns} widths.")
        size = (sum(column_widths_mm), row_height * len(cells))
        if at is None:
            frame, block = self._sheet_frame()
            taken = [self._view_geometry_box(v) for v in self._views()]
            taken += [e["box"] for e in self._all_annotation_entries() if e.get("box") is not None]
            spot = lay._free_spot(size, taken, lay.drawing_area(frame, block), 5.0)
            if spot is None:
                raise SolidWorksError(f"No free room for a {size[0]:.0f} x {size[1]:.0f} mm table: give at=[x, y].")
            at = (spot[0], spot[3])
        table = binding.wrap(drawing.InsertTableAnnotation2(False, mm_to_m(at[0]), mm_to_m(at[1]),
                                                            SW_TABLE_ANCHOR_TOP_LEFT, "", len(cells), columns),
                             self._mod.ITableAnnotation)
        if table is None:
            raise SolidWorksError("SolidWorks did not insert the table.")
        for i, row in enumerate(cells):
            for j, text in enumerate(row):
                if text:
                    table.SetText(i, j, text)
        if title and len(cells) > 1 and columns > 1:
            table.MergeCells(0, 0, 0, columns - 1)
        text_format = binding.wrap(table.GetTextFormat(), self._mod.ITextFormat)
        text_format.CharHeight = mm_to_m(text_height_mm)
        table.SetTextFormat(False, text_format)
        for i in range(len(cells)):
            table.SetRowHeight(i, mm_to_m(row_height), 0)
        for j, w in enumerate(column_widths_mm):
            table.SetColumnWidth(j, mm_to_m(w), 0)
        model.GraphicsRedraw2()
        annotation = binding.wrap(table.GetAnnotation(), self._mod.IAnnotation)
        return {"ok": True, "rows": len(cells), "columns": columns, **self._created(annotation, self._sheet_view())}

    def add_gear_data_table(self, kind: str = "spur", teeth: int | None = None, module: float | None = None,
                            mating_teeth: int | None = None, pressure_angle_deg: float = 20.0,
                            face_width_mm: float | None = None, profile_shift: float = 0.0,
                            shaft_angle_deg: float = 90.0, accuracy: str | None = None,
                            extra_rows: list | None = None, at: list | None = None) -> dict:
        """The gear data table a gear drawing needs, computed: 'spur' (pitch,
        tip, root and base diameters, span over k teeth, centre distance) or
        'bevel' (straight: pitch, face and root angles, cone distance, chordal
        thickness and height, crown to apex). Teeth, module and mating teeth
        default to the part's custom properties Teeth, Module, MatingTeeth.
        extra_rows: [[label, value], ...] such as material or backlash."""
        props = self._part_properties()
        teeth = teeth or self._number(props, ("Teeth", "z", "NumberOfTeeth"), "teeth")
        module = module or self._number(props, ("Module", "m"), "module")
        if mating_teeth is None and any(k.lower() in ("matingteeth", "z2") for k in props):
            mating_teeth = int(self._number(props, ("MatingTeeth", "z2"), "mating_teeth"))
        key = str(kind).lower()
        if key == "spur":
            data = gears.spur(int(teeth), float(module), pressure_angle_deg, face_width_mm, profile_shift,
                              mating_teeth)
        elif key == "bevel":
            if not mating_teeth:
                raise SolidWorksError("A bevel gear's data needs the mating gear's teeth: mating_teeth=...")
            data = gears.bevel(int(teeth), float(module), int(mating_teeth), pressure_angle_deg, face_width_mm,
                               shaft_angle_deg)
        else:
            raise SolidWorksError(f"Unknown gear kind '{kind}'. Use 'spur' or 'bevel'.")
        made = self.add_table(gears.table_rows(data, accuracy, extra_rows), at)
        return {**made, "gear": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in data.items()}}

    def _part_properties(self) -> dict:
        part = self._drawing_part_document()
        if part is None:
            return {}
        extension = binding.wrap(part.Extension, self._mod.IModelDocExtension)
        manager = binding.wrap(extension.CustomPropertyManager(""), self._mod.ICustomPropertyManager)
        out = {}
        for name in manager.GetNames() or ():
            value = manager.Get6(name, False)
            out[name] = value[2] if isinstance(value, tuple) and len(value) > 2 else value
        return out

    @staticmethod
    def _number(props: dict, names, label: str) -> float:
        lowered = {k.lower(): v for k, v in props.items()}
        for name in names:
            if name.lower() in lowered:
                try:
                    return float(str(lowered[name.lower()]).replace(",", "."))
                except ValueError:
                    raise SolidWorksError(f"The part's property {name} = {lowered[name.lower()]!r} is no number.")
        raise SolidWorksError(f"Give {label}=..., or set the part's custom property {names[0]}.")

    # --- layout -------------------------------------------------------------------------

    def auto_layout(self, scale=None, main: str | None = None) -> dict:
        """Lay out all views of the sheet again: the main view top left, the
        views projected or cut from it beside it on their own sides, the rest
        (iso, details, views of their own) in the room left, every view with
        room for dimensions; at the largest standard scale that fits, or
        `scale`. Annotations move with their views."""
        model, drawing = self._require_drawing()
        views = self._views()
        if not views:
            raise SolidWorksError("The sheet has no views to lay out.")
        frame, block = self._sheet_frame()
        areas = lay.drawing_area(frame, block)
        sheet_scale = self._sheet_scale()
        current = sheet_scale[0] / sheet_scale[1]
        boxes = {v.GetName2(): self._view_geometry_box(v) for v in views}
        follows = {v.GetName2(): bool(v.UseSheetScale) for v in views}
        root, children, others = self._view_family(views, boxes, main)

        def size(name, s):
            box = boxes[name]
            k = s / current if follows[name] else 1.0
            return lay.width(box) * k, lay.height(box) * k

        def arranged(s):
            return lay.arrange_views(size(root.GetName2(), s), [(n, side, size(n, s)) for n, side, _ in children],
                                     [(n, size(n, s)) for n in others], areas)

        ratio = lay.as_ratio(scale) if scale is not None else lay.pick_scale(lambda s: arranged(s) is not None)
        placed = arranged(ratio[0] / ratio[1])
        sheet = binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet)
        sheet.SetScale(float(ratio[0]), float(ratio[1]), False, False)
        model.ForceRebuild3(False)
        order = [root.GetName2()] + [n for n, _, _ in children] + others
        if placed is not None:
            for name in order:
                self._move_view_box(self._view(name), placed["main" if name == root.GetName2() else name])
            model.ForceRebuild3(False)
        fit = self.fit_sheet(main=root.GetName2())  # by the real footprints, where the margins ran out
        out = {"ok": True, "scale": lay.scale_text(ratio), "main": root.GetName2(),
               "moved": order if placed is not None else fit["moved"]}
        if not fit["fits"]:
            out["warnings"] = [f"The views do not fit at {lay.scale_text(ratio)} with their annotations; "
                               "leave a view out, or use a larger sheet."]
        out["views"] = [self._view_entry(self._view(n), with_mapping=False) for n in order]
        out["problems"] = fit["problems"]
        return out

    def _view_family(self, views, boxes, main=None):
        """(main view, [(name, side, distance)] of the views projected or cut
        from it, nearest first, [names] of the rest)."""
        root = self._view(main) if main else next(
            (v for v in views if VIEW_TYPES.get(int(v.Type)) in ("standard", "named") and self._looks_along_axis(v)),
            views[0])
        rc = lay.center(boxes[root.GetName2()])
        children, others = [], []
        for v in views:
            name = v.GetName2()
            if name == root.GetName2():
                continue
            base = binding.wrap(v.GetBaseView(), self._mod.IView)
            kind = VIEW_TYPES.get(int(v.Type))
            if base is not None and base.GetName2() == root.GetName2() and kind in ("projected", "section", "auxiliary"):
                cx, cy = lay.center(boxes[name])
                dx, dy = cx - rc[0], cy - rc[1]
                side = ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("above" if dy > 0 else "below")
                children.append((name, side, abs(dx) + abs(dy)))
            else:
                others.append(name)
        children.sort(key=lambda c: c[2])  # nearest first, so stacks keep their order
        return root, children, others

    def fit_sheet(self, main: str | None = None) -> dict:
        """Clear what check_drawing finds off the frame, on the title block, or
        clashing between views: every view moves with its annotations as one
        footprint, the main view and its projections top left, the rest
        (iso, details, the sheet's notes and tables) in the room left.
        Nothing moves while the sheet is clean or when the footprints cannot
        fit; `fits` says whether the sheet is clear afterwards."""
        model, _ = self._require_drawing()
        problems = self.check_drawing()["problems"]
        if not lay.fit_problems(problems):
            return {"fits": True, "moved": [], "problems": problems}
        root, children, others, geometry, loose, placed = self._plan_footprints(main)
        if placed is None:
            return {"fits": False, "moved": [], "problems": problems}
        moved = []
        for name in [root.GetName2()] + [n for n, _, _ in children] + others:
            target = placed["main" if name == root.GetName2() else name]
            if math.dist(target, lay.center(geometry[name])) > 0.05:
                self._move_view_box(self._view(name), target)
                moved.append(name)
        for annotation_id, box in loose.items():
            dx, dy = (t - c for t, c in zip(placed[annotation_id], lay.center(box)))
            if math.hypot(dx, dy) > 0.05:
                annotation, _ = self._find_annotation(annotation_id)
                x, y, _ = annotation.GetPosition()
                annotation.SetPosition2(x + mm_to_m(dx), y + mm_to_m(dy), 0.0)
                moved.append(annotation_id)
        model.ForceRebuild3(False)
        problems = self.check_drawing()["problems"]
        return {"fits": not lay.fit_problems(problems), "moved": moved, "problems": problems}

    def _plan_footprints(self, main: str | None = None, leave_out=()):
        """fit_sheet's plan: (main view, children, others, {view: geometry
        box}, {sheet annotation id: box}, geometry centres from
        arrange_footprints or None), without the views in `leave_out`."""
        views = [v for v in self._views() if v.GetName2() not in leave_out]
        frame, block = self._sheet_frame()
        geometry = {v.GetName2(): self._view_geometry_box(v) for v in views}
        entries = [e for e in self._all_annotation_entries() if e.get("box") is not None]
        extents = {name: lay.extents(box, lay.union([box] + [e["box"] for e in entries if e["view"] == name]))
                   for name, box in geometry.items()}
        loose = {e["id"]: e["box"] for e in entries if e["view"] == "sheet"}
        root, children, others = self._view_family(views, geometry, main)
        placed = lay.arrange_footprints(
            extents[root.GetName2()], [(n, side, extents[n]) for n, side, _ in children],
            [(n, extents[n]) for n in others] + [(i, lay.extents(b, b)) for i, b in loose.items()],
            lay.drawing_area(frame, block))
        return root, children, others, geometry, loose, placed

    def _looks_along_axis(self, view) -> bool:
        try:
            self._view_direction(self._model_map(view))
            return True
        except SolidWorksError:
            return False

    def add_note(self, text: str, at: list | None = None, height_mm: float = 3.5) -> dict:
        """A note on the sheet; at=[x, y] is its top-left corner (sheet mm), by
        default just above the title block on the left. Several lines: '\\n'."""
        model, drawing = self._require_drawing()
        if not str(text).strip():
            raise SolidWorksError("A note needs text.")
        drawing.ActivateSheet(binding.wrap(drawing.GetCurrentSheet(), self._mod.ISheet).GetName())
        model.ClearSelection2(True)
        note = binding.wrap(model.InsertNote(str(text).replace("\r\n", "\n").replace("\n", "\r\n")), self._mod.INote)
        if note is None:
            raise SolidWorksError("SolidWorks did not insert the note.")
        note.SetHeight(mm_to_m(height_mm))
        frame, block = self._sheet_frame()
        if at is None:
            lines = str(text).count("\n") + 1
            top = (block[3] if block else frame[1]) + 4.0 + lines * height_mm * 1.6
            at = ((block[0] if block else frame[0] + 5.0), top)
        annotation = binding.wrap(note.GetAnnotation(), self._mod.IAnnotation)
        annotation.SetPosition2(mm_to_m(at[0]), mm_to_m(at[1]), 0.0)
        model.ClearSelection2(True)
        return {"ok": True, **self._created(annotation, self._sheet_view())}

    def add_center_marks(self, view: str, holes: bool = True, fillets: bool = False,
                         centerlines: bool = True) -> dict:
        """Centre marks on the circles of a view, and centre lines on the
        cylinders it sees from the side."""
        model, drawing = self._require_drawing()
        v = self._view(view)
        before = {a.GetName() for a in self._annotations(v)}
        kinds = (SW_AUTO_CENTER_MARK_HOLES if holes else 0) | (SW_AUTO_CENTER_MARK_FILLETS if fillets else 0)
        if kinds:
            v.AutoInsertCenterMarks2(kinds, SW_CENTER_MARK_SINGLE, False, False, True, 0.0025, 0.001, True, False, 0.0)
        if centerlines:
            self._select_view(v)
            drawing.InsertCenterLine2()
            model.ClearSelection2(True)
        added = [a for a in self._annotations(v) if a.GetName() not in before]
        return {"ok": True, "count": len(added),
                "added": [self._public_entry(self._annotation_entry(a, v.GetName2())) for a in added]}

    # --- annotations: changing ----------------------------------------------------------

    def edit_annotation(self, annotation_id: str, prefix: str | None = None, suffix: str | None = None,
                        precision: int | None = None, tolerance: list | None = None, fit: str | None = None,
                        text: str | None = None, at: list | None = None, diametric: bool | None = None) -> dict:
        """Change an annotation: prefix/suffix of a dimension (a prefix
        replaces SolidWorks' own symbol, so a radius needs 'R': '4X R'),
        precision (decimals), tolerance [upper, lower] mm (bilateral; equal and
        opposite: symmetric), fit ('H7'), a note's text, its place at=[x, y]
        (sheet mm), or diametric=True for a diameter shown as a radius."""
        model, _ = self._require_drawing()
        annotation, view = self._find_annotation(annotation_id)
        kind = int(annotation.GetType())
        changes_part = False
        if kind == SW_ANNOTATION_DIMENSION:
            dim = binding.wrap(annotation.GetSpecificAnnotation(), self._mod.IDisplayDimension)
            measured = binding.wrap(dim.GetDimension2(0), self._mod.IDimension)
            changes_part = measured is not None and measured.FullName.lower().endswith(".part") and \
                any(v is not None for v in (prefix, suffix, precision, tolerance, fit, diametric))
            if prefix is not None:
                if dim.IsHoleCallout():
                    self._prefix_callout(dim, prefix)
                else:
                    dim.SetText(SW_DIMENSION_TEXT_PREFIX, prefix)
            if suffix is not None:
                dim.SetText(SW_DIMENSION_TEXT_SUFFIX, suffix)
            if precision is not None:
                dim.SetPrecision3(int(precision), int(precision), int(precision), int(precision))
            if diametric is not None:
                dim.Diametric = bool(diametric)
            if tolerance is not None or fit is not None:
                self._set_tolerance(dim, tolerance, fit)
        elif kind == SW_ANNOTATION_NOTE:
            note = binding.wrap(annotation.GetSpecificAnnotation(), self._mod.INote)
            if text is not None:
                note.SetText(str(text).replace("\r\n", "\n").replace("\n", "\r\n"))
            if any(v is not None for v in (prefix, suffix, precision, tolerance, fit, diametric)):
                raise SolidWorksError("A note takes text and at; prefix, precision and tolerances are for dimensions.")
        elif any(v is not None for v in (prefix, suffix, precision, tolerance, fit, text, diametric)):
            raise SolidWorksError(f"{annotation_id} can only be moved (at=[x, y]).")
        if at is not None:
            annotation.SetPosition2(mm_to_m(at[0]), mm_to_m(at[1]), 0.0)
        model.ForceRebuild3(False)
        out = {"ok": True, **self._created(annotation, view, self._model_map(view)
                                           if view.ReferencedDocument is not None else None)}
        if changes_part:
            out["changes_part"] = ("a model dimension keeps its text and tolerance in the part: "
                                   "the part, and every drawing of it, changed too")
        return out

    def _set_tolerance(self, dim, tolerance, fit) -> None:
        measured = binding.wrap(dim.GetDimension2(0), self._mod.IDimension)
        tol = binding.wrap(measured.Tolerance, self._mod.IDimensionTolerance)
        if fit is not None:
            tol.Type = TOLERANCE_TYPES["fit"]
            if not tol.SetFitValues(str(fit), ""):
                raise SolidWorksError(f"SolidWorks refused the fit '{fit}'.")
            return
        if len(tolerance) != 2:
            raise SolidWorksError(f"tolerance is [upper, lower] in mm, e.g. [0.1, -0.1] (got {tolerance}).")
        upper, lower = (float(t) for t in tolerance)
        tol.Type = TOLERANCE_TYPES["symmetric" if abs(upper + lower) < 1e-9 else "bilateral"]
        if not tol.SetValues(mm_to_m(lower), mm_to_m(upper)):
            raise SolidWorksError(f"SolidWorks refused the tolerance {tolerance}.")

    def delete_annotation(self, annotation_id: str) -> dict:
        """Delete one annotation by id ('<name>@<view>')."""
        model, _ = self._require_drawing()
        annotation, view = self._find_annotation(annotation_id)
        name = annotation.GetName()
        self._delete_annotation(annotation)
        if any(a.GetName() == name for a in self._annotations(view)):
            raise SolidWorksError(f"SolidWorks did not delete {annotation_id}.")
        return {"ok": True, "deleted": f"{name}@{view.GetName2()}"}

    def arrange_dimensions(self, view: str) -> dict:
        """Let SolidWorks space a view's dimensions evenly off the geometry
        (auto arrange), then report what check_drawing finds there."""
        model, _ = self._require_drawing()
        v = self._view(view)
        model.ClearSelection2(True)
        selected = 0
        for annotation in self._annotations(v):
            if int(annotation.GetType()) == SW_ANNOTATION_DIMENSION:
                selected += self._select_annotation(annotation, True)
        if selected:
            binding.wrap(model.Extension, self._mod.IModelDocExtension).AlignDimensions(SW_ALIGN_AUTO_ARRANGE, 0.006)
        model.ClearSelection2(True)
        model.ForceRebuild3(False)
        check = self.check_drawing()
        problems = [p for p in check["problems"] if any(i.endswith("@" + v.GetName2()) or i == v.GetName2()
                                                        for i in p["items"])]
        return {"ok": True, "arranged": selected, "problems": problems}

    # --- checking and seeing -------------------------------------------------------------

    def check_drawing(self) -> dict:
        """Problems on the current sheet: views or annotations outside the frame
        or over the title block, views overlapping, texts overlapping each other
        or lying on view geometry, crossing leaders, text under 2.5 mm, probable
        duplicate dimensions, and features with no dimension or callout."""
        self._require_drawing()
        frame, block = self._sheet_frame()
        views = [{"name": v.GetName2(), "box": self._view_geometry_box(v)} for v in self._views()]
        annotations = self._all_annotation_entries()
        problems = lay.find_problems(views, annotations, frame, block)
        problems += self._undimensioned_features()
        return {"ok": True, "count": len(problems), "problems": problems,
                "views": len(views), "annotations": len(annotations)}

    def _undimensioned_features(self) -> list:
        """Hole, cut, fillet and chamfer features of the drawn part that no
        dimension or callout of the drawing covers: a model dimension of the
        feature (or of its sketch), a drawing dimension on one of its edges, or
        a counted callout ('4X ...') on an identical hole."""
        views = self._views()
        if not views:
            return []
        document = binding.wrap(views[0].ReferencedDocument, self._mod.IModelDoc2)
        if document is None or int(document.GetType()) != SW_DOC_PART:
            return []
        features, owner = [], {}
        feature = binding.wrap(document.FirstFeature(), self._mod.IFeature)
        while feature is not None:
            features.append(feature)
            owner[feature.Name] = feature.Name
            for sub in self._sub_features(feature):
                owner[sub.Name] = feature.Name
            feature = binding.wrap(feature.GetNextFeature(), self._mod.IFeature)
        named, points, counted = set(), [], []
        for v in views:
            for annotation in self._annotations(v):
                if int(annotation.GetType()) != SW_ANNOTATION_DIMENSION:
                    continue
                dim = binding.wrap(annotation.GetSpecificAnnotation(), self._mod.IDisplayDimension)
                measured = binding.wrap(dim.GetDimension2(0), self._mod.IDimension)
                parts = (measured.FullName if measured is not None else "").split("@")
                if len(parts) >= 3 and parts[-1].lower().endswith(".part"):  # a model dimension
                    named.add(owner.get(parts[1], parts[1]))
                found = self._attached_points(annotation)
                points += found
                if dim.IsHoleCallout() and re.search(r"\d+X ", " ".join(dim.GetText(k) or "" for k in (1, 3))):
                    counted += found
        holes = self._hole_features(document)
        counted_kinds = {self._hole_signature(document, h) for h in holes
                         if any(self._near_faces(p, self._feature_faces(document, h)) for p in counted)}
        problems = []
        kinds = ("HoleWzd", "AdvHoleWzd", "SimpleHole", "ICE", "Cut", "Fillet", "Chamfer")
        for feature in features:
            if feature.GetTypeName2() not in kinds or feature.IsSuppressed2(1, None)[0]:
                continue
            faces = [binding.wrap(f, self._mod.IFace2) for f in (feature.GetFaces() or ())]
            if not faces or feature.Name in named or any(self._near_faces(p, faces) for p in points):
                continue
            if feature.Name in holes and self._hole_signature(document, feature.Name) in counted_kinds:
                continue
            problems.append({"kind": "undimensioned_feature", "items": [feature.Name],
                             "message": f"No dimension or callout covers {feature.Name} ({feature.GetTypeName2()})."})
        return problems

    def _attached_points(self, annotation) -> list:
        """Midpoints (model metres) of the edges an annotation is attached to."""
        out = []
        for entity in annotation.GetAttachedEntities3() or ():
            try:
                edge = binding.wrap(entity, self._mod.IEdge)
                params = edge.GetCurveParams2()
                curve = binding.wrap(edge.GetCurve(), self._mod.ICurve)
                out.append(curve.Evaluate2((params[6] + params[7]) / 2.0, 0)[:3])
            except (pythoncom.com_error, TypeError, AttributeError):
                continue  # attached to something other than an edge
        return out

    @staticmethod
    def _near_faces(point, faces) -> bool:
        return any(math.dist(point, face.GetClosestPointOn(*point)[:3]) < _EDGE_ON_FACE_M for face in faces)

    def render_drawing(self, path: str, zoom_mm: list | None = None) -> dict:
        """A picture (PNG/JPG) of the sheet, or of the region zoom_mm =
        [[x1, y1], [x2, y2]] (sheet mm), to look at the drawing without a PDF."""
        model, _ = self._require_drawing()
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in ("png", "jpg", "tif"):
            raise SolidWorksError(f"render_drawing writes png, jpg or tif (got '{ext}').")
        if zoom_mm is not None and (len(zoom_mm) != 2 or any(len(c) != 2 for c in zoom_mm)):
            raise SolidWorksError(f"zoom_mm is two sheet corners [[x1, y1], [x2, y2]] (got {zoom_mm}).")
        return self._render(model, os.path.abspath(path), zoom_mm)

    def _render(self, model, abs_path: str, zoom_mm) -> dict:
        # the window must draw for the picture: wake it, as screenshot does
        depth, self._quiet_depth = self._quiet_depth, 0
        self._wake_all()
        busy = self._command_in_progress(False)
        try:
            model.GraphicsRedraw2()
            if zoom_mm is None:
                model.ViewZoomtofit2()
            else:
                (x1, y1), (x2, y2) = zoom_mm
                model.ViewZoomTo2(mm_to_m(min(x1, x2)), mm_to_m(min(y1, y2)), 0.0,
                                  mm_to_m(max(x1, x2)), mm_to_m(max(y1, y2)), 0.0)
            self._write_via_saveas3(abs_path, model)
            model.ViewZoomtofit2()
        finally:
            self._command_in_progress(busy)
            self._quiet_depth = depth
        return {"ok": True, "path": abs_path, "format": os.path.splitext(abs_path)[1].lstrip(".").lower(),
                "bytes": os.path.getsize(abs_path)}

    # --- one entry point per tool family ---------------------------------------------
    #
    # The MCP server offers each family of drawing operations as one tool, so a
    # client loads 16 drawing tools instead of 34. These route to the methods
    # above and refuse arguments that do not belong to the kind asked for.

    @staticmethod
    def _only(kind: str, given: dict, allowed: set) -> None:
        stray = sorted(k for k, v in given.items() if v is not None and v is not False and k not in allowed)
        if stray:
            raise SolidWorksError(f"{', '.join(stray)} {'does' if len(stray) == 1 else 'do'} not go with {kind}.")

    def drawing_file(self, action: str, path: str | None = None, sheet: str = "A3", part: str | None = None) -> dict:
        """new (sheet, part) | open (path) | save (path, .slddrw) | export (path:
        PDF, DXF, DWG, picture) | close: the current drawing's document."""
        key = str(action).lower()
        if key == "new":
            self._only("action 'new'", {"path": path}, set())
            return self.new_drawing(sheet, part)
        if key in ("open", "save", "export"):
            if not path:
                raise SolidWorksError(f"action '{key}' needs path=...")
            self._only(f"action '{key}'", {"part": part}, set())
            return {"open": self.open_drawing, "save": self.save_drawing, "export": self.export_drawing}[key](path)
        if key == "close":
            self._only("action 'close'", {"path": path, "part": part}, set())
            return self.close_drawing()
        raise SolidWorksError(f"Unknown action '{action}'. Use new, open, save, export or close.")

    def add_drawing_view(self, kind: str = "model", orientation: str = "front", parent: str | None = None,
                         side: str | None = None, through: list | None = None, normal: str = "x",
                         label: str | None = None, flip: bool = False, center: list | None = None,
                         radius_mm: float | None = None, at: list | None = None, beside: str | None = None,
                         gap_mm: float | None = None, scale=None, name: str | None = None,
                         part: str | None = None) -> dict:
        """A view of kind 'model' (orientation; at, or beside + side),
        'projected' (parent, side), 'section' (parent, through, normal, label,
        side, flip), 'detail' (parent, center, radius_mm, scale, label, at) or
        'flat_pattern' (a sheet metal part unfolded; at, or beside + side)."""
        key = str(kind).lower()
        given = {"orientation": None if orientation == "front" else orientation, "parent": parent, "side": side,
                 "through": through, "normal": None if normal == "x" else normal, "label": label, "flip": flip,
                 "center": center, "radius_mm": radius_mm, "at": at, "beside": beside, "gap_mm": gap_mm,
                 "scale": scale, "part": part}
        if key == "model":
            self._only("kind 'model'", given, {"orientation", "at", "beside", "side", "gap_mm", "scale", "part"})
            relative = {"right_of": None, "left_of": None, "above": None, "below": None}
            if beside:
                spot = {"right": "right_of", "left": "left_of", "above": "above", "below": "below"}.get(side or "right")
                if spot is None:
                    raise SolidWorksError(f"Unknown side '{side}'. Use {', '.join(lay.SIDES)}.")
                relative[spot] = beside
            elif side:
                raise SolidWorksError("side places a model view beside another: give beside='<view>' too.")
            return self.add_view(orientation, at, *relative.values(), gap_mm, scale, name, part)
        if key == "flat_pattern":
            self._only("kind 'flat_pattern'", given, {"at", "beside", "side", "gap_mm", "scale", "part"})
            if side and not beside:
                raise SolidWorksError("side places the flat pattern beside another view: give beside='<view>' too.")
            return self.add_flat_pattern_view(at, beside, side, gap_mm, scale, name, part)
        if not parent:
            raise SolidWorksError(f"A {key} view needs parent='<view>'.")
        if key == "projected":
            self._only("kind 'projected'", given, {"parent", "side", "gap_mm"})
            return self.add_projected_view(parent, side or "right", gap_mm, name)
        if key == "section":
            self._only("kind 'section'", given, {"parent", "through", "normal", "label", "side", "flip", "gap_mm"})
            if through is None:
                raise SolidWorksError("A section view needs through=[x, y, z], a model point on the cutting plane.")
            return self.add_section_view(parent, through, normal, label or "A", side, gap_mm, flip, name)
        if key == "detail":
            self._only("kind 'detail'", given, {"parent", "center", "radius_mm", "scale", "label", "at"})
            if center is None or radius_mm is None:
                raise SolidWorksError("A detail view needs center=[x, y, z] and radius_mm.")
            return self.add_detail_view(parent, center, radius_mm, scale, label or "B", at, name)
        raise SolidWorksError(f"Unknown kind '{kind}'. Use model, projected, section, detail or flat_pattern.")

    def edit_view(self, name: str, at: list | None = None, beside: str | None = None, side: str | None = None,
                  gap_mm: float | None = None, align: str = "center", scale=None, display: str | None = None,
                  tangent_edges: bool | None = None, new_name: str | None = None, delete: bool = False) -> dict:
        """Move a view (at, or beside + side), change it (scale, display,
        tangent_edges, new_name), or delete it."""
        if delete:
            self._only("delete", {"at": at, "beside": beside, "side": side, "gap_mm": gap_mm, "scale": scale,
                                  "display": display, "tangent_edges": tangent_edges, "new_name": new_name}, set())
            return self.delete_view(name)
        result = None
        if any(v is not None for v in (scale, display, tangent_edges, new_name)):
            result = self.set_view(name, scale, display, tangent_edges, new_name)
            name = new_name or name
        if at is not None or beside:
            relative = {"right": None, "left": None, "above": None, "below": None}
            if beside:
                if (side or "right") not in relative:
                    raise SolidWorksError(f"Unknown side '{side}'. Use {', '.join(lay.SIDES)}.")
                relative[side or "right"] = beside
            result = self.move_view(name, at, *relative.values(), gap_mm, align)
        if result is None:
            raise SolidWorksError("Say what to change: at / beside to move it, scale, display, tangent_edges, "
                                  "new_name, or delete=True.")
        return result

    def dimension(self, view: str, ref, to=None, style: str = "single", direction: str = "auto",
                  side: str | None = None, offset_mm: float | None = None) -> dict:
        """style 'single': ref to `to` (or the size of ref); 'baseline' /
        'chain' / 'ordinate': `to` is a list of references measured from ref."""
        key = str(style).lower()
        if key == "single":
            return self.add_dimension(view, ref, to, direction, side, offset_mm)
        if key not in ("baseline", "chain", "ordinate"):
            raise SolidWorksError(f"Unknown style '{style}'. Use single, baseline, chain or ordinate.")
        if not isinstance(to, (list, tuple)) or not to or all(isinstance(c, (int, float)) for c in to):
            raise SolidWorksError(f"style '{key}' takes to=[ref, ref, ...], the references measured from ref.")
        if offset_mm is not None:
            raise SolidWorksError(f"offset_mm is for a single dimension; a {key} set finds its own rows.")
        return self.add_dimension_set(view, ref, list(to), key, direction, side)

    def add_symbol(self, type: str, view: str, ref, at: list | None = None, count: bool = True,  # noqa: A002
                   roughness: str | None = None, process: str = "machined", label: str | None = None,
                   symbol: str | None = None, tolerance: str | None = None, datums: list | None = None,
                   diameter: bool = False, material_condition: str | None = None, weld: dict | None = None) -> dict:
        """type 'hole_callout' (ref 'hole:<feature>', count), 'surface_finish'
        (roughness, process), 'datum' (label), 'gtol' (symbol, tolerance,
        datums, diameter, material_condition) or 'weld' (weld={weld, size,
        length, side, contour, all_around, field}), attached to an edge of a view."""
        key = str(type).lower()
        given = {"roughness": roughness, "process": None if process == "machined" else process, "label": label,
                 "symbol": symbol, "tolerance": tolerance, "datums": datums, "diameter": diameter,
                 "material_condition": material_condition, "count": None if count else "False", "weld": weld}
        if key == "hole_callout":
            self._only("a hole callout", given, {"count"})
            hole = str(ref)
            return self.add_hole_callout(view, hole[5:] if hole.lower().startswith("hole:") else hole, at, count)
        if key == "surface_finish":
            self._only("a surface finish", given, {"roughness", "process"})
            return self.add_surface_finish(view, ref, roughness or "Ra 3.2", process, at)
        if key == "datum":
            self._only("a datum", given, {"label"})
            return self.add_datum(view, ref, label or "A", at)
        if key == "gtol":
            self._only("a tolerance frame", given, {"symbol", "tolerance", "datums", "diameter", "material_condition"})
            if not symbol or tolerance is None:
                raise SolidWorksError("A tolerance frame needs symbol='flatness' (or ...) and tolerance='0.05'.")
            return self.add_gtol(view, ref, symbol, tolerance, datums, diameter, material_condition, at)
        if key == "weld":
            self._only("a weld symbol", given, {"weld"})
            weld = weld or {}
            if not isinstance(weld, dict):
                raise SolidWorksError("weld is a dict: {'weld': 'fillet', 'size': 'a4', 'length': '100', ...}.")
            known = {"weld", "size", "length", "side", "contour", "all_around", "field"}
            unknown = sorted(set(weld) - known)
            if unknown:
                raise SolidWorksError(f"Unknown weld keys {unknown}; use {sorted(known)}.")
            return self.add_weld_symbol(view, ref, **weld, at=at)
        raise SolidWorksError(f"Unknown type '{type}'. Use hole_callout, surface_finish, datum, gtol or weld.")

    def table(self, rows: list | None = None, gear: dict | None = None, at: list | None = None, title: bool = True,
              text_height_mm: float = 2.5, column_widths_mm: list | None = None, cut_list: bool = False) -> dict:
        """rows [[cell, ...], ...] for any table, gear={kind, teeth, module,
        mating_teeth, ...} for a computed gear data table, or cut_list=True
        for a weldment's cut list."""
        if sum((rows is not None, gear is not None, bool(cut_list))) != 1:
            raise SolidWorksError("Give rows=[[...], ...] for a table, gear={...} for a gear data table, or "
                                  "cut_list=True for a weldment's cut list.")
        if cut_list:
            return self.add_cut_list_table(at, text_height_mm)
        if gear is not None:
            if not isinstance(gear, dict):
                raise SolidWorksError("gear is a dict: {'kind': 'spur', 'teeth': 20, 'module': 2, ...}.")
            known = {"kind", "teeth", "module", "mating_teeth", "pressure_angle_deg", "face_width_mm",
                     "profile_shift", "shaft_angle_deg", "accuracy", "extra_rows"}
            unknown = sorted(set(gear) - known)
            if unknown:
                raise SolidWorksError(f"Unknown gear keys {unknown}; use {sorted(known)}.")
            return self.add_gear_data_table(**gear, at=at)
        return self.add_table(rows, at, title, text_height_mm, column_widths_mm)

    def annotations(self, view: str | None = None) -> dict:
        """list_views with every annotation listed under the view it is in."""
        listed = self.list_views()
        entries = self.list_annotations(view)["annotations"]
        for entry in listed["views"]:
            entry["annotations"] = [a for a in entries if a["view"] == entry["name"]]
        if view is not None:
            target = self._view(view).GetName2()
            listed["views"] = [v for v in listed["views"] if v["name"] == target]
            listed["count"] = len(listed["views"])
        else:
            listed["sheet_annotations"] = [a for a in entries if a["view"] == "sheet"]
        return listed

    def import_annotations(self, view: str | None = None, kinds: list | None = None) -> dict:
        """import_dimensions, with kind 'center_marks' (centre marks and lines) too."""
        kinds = [str(k).lower() for k in (kinds or ["model", "callouts", "hole_locations"])]
        marks = "center_marks" in kinds
        rest = [k for k in kinds if k != "center_marks"]
        result = self.import_dimensions(view, rest) if rest else {"ok": True, "count": 0, "added": []}
        if marks:
            added = []
            for v in ([self._view(view)] if view else self._views()):
                added += self.add_center_marks(v.GetName2())["added"]
            result["center_marks"] = len(added)
            result["added"] = result.get("added", []) + added
            result["count"] = len(result["added"])
        return result

    def lay_out(self, scale=None, main: str | None = None, arrange_dimensions: bool = False) -> dict:
        """auto_layout, then (arrange_dimensions=True) SolidWorks' auto arrange
        of every view's dimensions."""
        result = self.auto_layout(scale, main)
        if arrange_dimensions:
            arranged = sum(self.arrange_dimensions(v.GetName2())["arranged"] for v in self._views())
            result["arranged_dimensions"] = arranged
            result["problems"] = self.check_drawing()["problems"]
        return result

    def change_annotation(self, annotation_id: str, prefix: str | None = None, suffix: str | None = None,
                          precision: int | None = None, tolerance: list | None = None, fit: str | None = None,
                          text: str | None = None, at: list | None = None, diametric: bool | None = None,
                          delete: bool = False) -> dict:
        """edit_annotation, or delete=True."""
        changes = {"prefix": prefix, "suffix": suffix, "precision": precision, "tolerance": tolerance, "fit": fit,
                   "text": text, "at": at, "diametric": diametric}
        if delete:
            stray = sorted(k for k, v in changes.items() if v is not None)
            if stray:
                raise SolidWorksError(f"{', '.join(stray)} do not go with delete.")
            return self.delete_annotation(annotation_id)
        return self.edit_annotation(annotation_id, **changes)

    # --- rollback -------------------------------------------------------------------

    def _drawing_snapshot(self):
        """(title, view names, annotation ids) of the current drawing, or None."""
        if self._drawing is None:
            return None
        try:
            model, _ = self._require_drawing()
            return (model.GetTitle(), {v.GetName2() for v in self._views()},
                    {f"{a.GetName()}@{v.GetName2()}" for v in self._views() for a in self._annotations(v)}
                    | {f"{a.GetName()}@sheet" for a in self._sheet_annotations()})
        except (pythoncom.com_error, SolidWorksError):
            return None

    def _drawing_roll_back(self, snapshot) -> list:
        """Delete views and annotations added since the snapshot; returns what
        could not be removed."""
        if snapshot is None or self._drawing is None:
            return []
        try:
            model, _ = self._require_drawing()
        except SolidWorksError:
            return []
        if model.GetTitle() != snapshot[0]:
            return []
        extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
        left = []
        for v in self._views():
            if v.GetName2() not in snapshot[1]:
                self._select_view(v)
                extension.DeleteSelection2(SW_DELETE_NO_OPTION)
        added = [(a, f"{a.GetName()}@{v.GetName2()}") for v in self._views() for a in self._annotations(v)]
        added += [(a, f"{a.GetName()}@sheet") for a in self._sheet_annotations()]
        for a, key in added:
            if key not in snapshot[2]:
                self._delete_annotation(a)
        model.ClearSelection2(True)
        for v in self._views():
            if v.GetName2() not in snapshot[1]:
                left.append(v.GetName2())
        return left

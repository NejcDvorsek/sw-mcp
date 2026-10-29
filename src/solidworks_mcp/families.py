"""One MCP tool per family of modelling and document tools.

Each method here picks the existing session method that does the work from
the arguments given (a face, a plane, a list, a document type), so the server
offers one tool where it had up to four. Arguments that don't go with the
chosen variant are refused, not ignored: a silently dropped draft angle would
only show up as wrong geometry.
"""

import os
import re

from .constants import SW_DOC_ASSEMBLY, SW_DOC_DRAWING, SW_DOC_PART
from .errors import SolidWorksError

_SW_EXTENSION = re.compile(r"\.(sldprt|sldasm|slddrw)$", re.IGNORECASE)


class ToolFamilies:
    """Mixed into SolidWorksSession."""

    @staticmethod
    def _refuse_stray(variant: str, given: dict) -> None:
        """given maps an argument to its value when it differs from its default."""
        stray = sorted(k for k, v in given.items() if v)
        if stray:
            raise SolidWorksError(f"{', '.join(stray)} {'does' if len(stray) == 1 else 'do'} not go with {variant}.")

    # --- modelling ------------------------------------------------------------

    def hole(self, x_mm: float, y_mm: float, z_mm: float | None = None, diameter_mm: float | None = None,
             face: str | None = None, depth_mm: float | None = None, cbore_diameter_mm: float | None = None,
             cbore_depth_mm: float | None = None, iso: str | None = None, size: str | None = None,
             fit: str = "normal", thread: str = "cosmetic", name: str | None = None) -> dict:
        """iso + size: Hole Wizard; cbore_*: a counterbore from +z; face: on that
        face; otherwise a plain hole through the thickness."""
        if iso:
            self._refuse_stray("an ISO hole (iso=...)", {"diameter_mm": diameter_mm is not None,
                                                          "cbore_diameter_mm": cbore_diameter_mm is not None,
                                                          "cbore_depth_mm": cbore_depth_mm is not None})
            if not size or face is None or z_mm is None:
                raise SolidWorksError("An ISO hole needs size='M6', face='+z' and the point x, y, z on that face.")
            return self.add_hole_wizard(iso, size, face, x_mm, y_mm, z_mm, depth_mm, fit, thread, name)
        self._refuse_stray("a hole without iso=...", {"size": size is not None, "fit": fit != "normal",
                                                      "thread": thread != "cosmetic"})
        if diameter_mm is None:
            raise SolidWorksError("A hole needs diameter_mm (or iso='clearance' and size='M6' for a standard one).")
        if cbore_diameter_mm is not None or cbore_depth_mm is not None:
            self._refuse_stray("a counterbore (it goes in from +z, through the thickness)",
                               {"face": face is not None, "z_mm": z_mm is not None, "depth_mm": depth_mm is not None})
            if cbore_diameter_mm is None or cbore_depth_mm is None:
                raise SolidWorksError("A counterbore needs both cbore_diameter_mm and cbore_depth_mm.")
            return self.add_counterbore_hole(diameter_mm, cbore_diameter_mm, cbore_depth_mm, x_mm, y_mm,
                                             name or "Counterbore")
        if face is not None:
            if z_mm is None:
                raise SolidWorksError("A hole on a face needs the point x, y, z on that face.")
            return self.add_hole_on_face(diameter_mm, face, x_mm, y_mm, z_mm, depth_mm, name or "Hole")
        self._refuse_stray("a hole through the thickness (give face= for a blind one)",
                           {"z_mm": z_mm is not None, "depth_mm": depth_mm is not None})
        return self.add_hole(diameter_mm, x_mm, y_mm, name or "Hole")

    def extruded_profile(self, points_mm: list, depth_mm: float, face: str | None = None, plane: str | None = None,
                         reverse: bool = False, name: str | None = None,
                         corner_radii_mm: float | list | None = None, rotate_deg: float = 0.0,
                         about_mm: list | None = None, draft_deg: float = 0.0, merge: bool = True,
                         spline: bool = False) -> dict:
        """plane: on a reference plane; face: a boss on a face; spline: a closed
        spline; otherwise a polygon on the first plane."""
        if face is not None and plane is not None:
            raise SolidWorksError("Give face= or plane=, not both.")
        if plane is not None:
            self._refuse_stray("an extrusion on a plane", {"rotate_deg": rotate_deg, "about_mm": about_mm,
                                                           "spline": spline})
            return self.add_extruded_profile_on_plane(points_mm, plane, depth_mm, reverse, name or "Extrude",
                                                      corner_radii_mm, draft_deg, merge)
        if face is not None:
            self._refuse_stray("a boss on a face", {"reverse": reverse, "rotate_deg": rotate_deg,
                                                    "about_mm": about_mm, "draft_deg": draft_deg,
                                                    "merge": not merge, "spline": spline})
            return self.add_extruded_profile_on_face(points_mm, face, depth_mm, name or "Boss", corner_radii_mm)
        if spline:
            self._refuse_stray("a spline", {"reverse": reverse, "corner_radii_mm": corner_radii_mm is not None,
                                            "rotate_deg": rotate_deg, "about_mm": about_mm,
                                            "draft_deg": draft_deg, "merge": not merge})
            return self.add_extruded_spline(points_mm, depth_mm, name or "Spline")
        self._refuse_stray("an extrusion on the first plane (it rises along +z)", {"reverse": reverse})
        return self.add_extruded_profile(points_mm, depth_mm, name or "Extrude", corner_radii_mm, rotate_deg,
                                         about_mm, draft_deg, merge)

    def profile_cut(self, points_mm: list, depth_mm: float | None = None, face: str | None = None,
                    plane: str | None = None, name: str = "Cut", corner_radii_mm: float | list | None = None,
                    rotate_deg: float = 0.0, about_mm: list | None = None, keep_inside: bool = False) -> dict:
        """plane: through a reference plane, symmetric; face: into a face;
        otherwise down from +z."""
        if face is not None and plane is not None:
            raise SolidWorksError("Give face= or plane=, not both.")
        if plane is not None:
            self._refuse_stray("a cut through a plane", {"rotate_deg": rotate_deg, "about_mm": about_mm})
            return self.cut_profile_through_plane(points_mm, plane, depth_mm, name, corner_radii_mm, keep_inside)
        self._refuse_stray("a cut without plane=", {"keep_inside": keep_inside})
        if face is not None:
            self._refuse_stray("a cut on a face", {"rotate_deg": rotate_deg, "about_mm": about_mm})
            return self.cut_profile_on_face(points_mm, face, depth_mm, name, corner_radii_mm)
        return self.cut_profile(points_mm, depth_mm, name, corner_radii_mm, rotate_deg, about_mm)

    def loft(self, sketches: list | None = None, cut: bool = False, profiles_mm: list | None = None,
             heights_mm: list | None = None, name: str | None = None) -> dict:
        """sketches by name, or profiles_mm stacked at heights_mm along +z."""
        if (sketches is None) == (profiles_mm is None):
            raise SolidWorksError("Give sketches=[...] (by name) or profiles_mm with heights_mm, one of the two.")
        if profiles_mm is not None:
            self._refuse_stray("a loft of profiles_mm (it adds material)", {"cut": cut})
            if heights_mm is None:
                raise SolidWorksError("profiles_mm needs heights_mm, one height per profile.")
            return self.add_lofted_solid(profiles_mm, heights_mm, name or "Loft")
        self._refuse_stray("a loft through sketches", {"heights_mm": heights_mm is not None})
        return self.add_loft(sketches, cut, name)

    def equation(self, equation) -> dict:
        """One equation string, or a list of them with one rebuild."""
        if isinstance(equation, (list, tuple)):
            return self.set_equations(list(equation))
        return self.set_equation(equation)

    # --- assemblies -----------------------------------------------------------

    def mate(self, comp_a: str | None = None, face_a: str | None = None, comp_b: str | None = None,
             face_b: str | None = None, mate_type: str = "coincident", distance_mm: float = 0.0,
             angle_deg: float = 0.0, flip: bool = False, mates: list | None = None,
             stop_on_error: bool = True) -> dict:
        """One mate from the arguments, or several from mates=[{...}, ...]."""
        if mates is not None:
            self._refuse_stray("mates=[...] (each mate carries its own arguments)",
                               {"comp_a": comp_a, "face_a": face_a, "comp_b": comp_b, "face_b": face_b,
                                "mate_type": mate_type != "coincident", "distance_mm": distance_mm,
                                "angle_deg": angle_deg, "flip": flip})
            return self.add_mates(mates, stop_on_error)
        if not (comp_a and face_a and comp_b and face_b):
            raise SolidWorksError("A mate needs comp_a, face_a, comp_b and face_b (or mates=[...] for several).")
        return self.add_mate(comp_a, face_a, comp_b, face_b, mate_type, distance_mm, angle_deg, flip)

    def bounding_box(self) -> dict:
        """Of the current part, or of the whole assembly."""
        if int(self._require_model().GetType()) == SW_DOC_ASSEMBLY:
            return self.get_assembly_bounding_box()
        return self.get_bounding_box()

    # --- documents ------------------------------------------------------------

    def new_document(self, kind: str = "part") -> dict:
        """kind 'part' or 'assembly'."""
        key = str(kind).lower()
        if key == "part":
            return self.new_part()
        if key == "assembly":
            return self.new_assembly()
        raise SolidWorksError(f"Unknown kind '{kind}'. Use 'part' or 'assembly' (drawings: drawing_file).")

    def open_document(self, path: str, kind: str | None = None) -> dict:
        """.sldprt / .sldasm by extension; STEP, IGES, Parasolid as kind, or
        as a part first and an assembly if it holds one."""
        extension = os.path.splitext(str(path))[1].lower()
        key = str(kind).lower() if kind else {".sldprt": "part", ".sldasm": "assembly"}.get(extension)
        if key == "part":
            return self.open_part(path)
        if key == "assembly":
            return self.open_assembly(path)
        if key is not None:
            raise SolidWorksError(f"Unknown kind '{kind}'. Use 'part' or 'assembly' (drawings: drawing_file).")
        try:
            return self.open_part(path)
        except SolidWorksError as exc:
            if "holds an assembly" not in str(exc):
                raise
        return self.open_assembly(path)

    def save_document(self, path: str | None = None) -> dict:
        """The current part or assembly, to path or over its own file."""
        model = self._require_model()
        if path is None:
            path = model.GetPathName()
            if not path:
                raise SolidWorksError(f"'{model.GetTitle()}' was never saved: give path=...")
        if int(model.GetType()) == SW_DOC_ASSEMBLY:
            return self.save_assembly(path)
        return self.save_part(path)

    def _find_document(self, title: str):
        """The one open document with this title, with or without its extension."""
        documents = self._open_documents()
        key = _SW_EXTENSION.sub("", str(title).strip()).lower()
        matches = [d for d in documents if d.GetTitle() == title] or \
            [d for d in documents if _SW_EXTENSION.sub("", d.GetTitle()).lower() == key]
        if len(matches) != 1:
            listed = ", ".join(d.GetTitle() for d in documents) or "none"
            raise SolidWorksError(f"No open document '{title}' (open: {listed}).")
        return matches[0]

    def close_document(self, title: str | None = None, save: bool = False) -> dict:
        """Close one document by title (part, assembly or drawing; the current
        part or assembly without a title), saving it over its own file first
        with save=True. CloseDoc never prompts: without save, changes are lost."""
        if title is None:
            if self._model is None:
                if save:
                    raise SolidWorksError("There is no current document to save: give title=...")
                return self.close_part()
            title = self._require_model().GetTitle()
        document = self._find_document(title)
        title = document.GetTitle()
        if not document.Visible:
            raise SolidWorksError(f"'{title}' was loaded by an open assembly and closes with it.")
        doc_type = int(document.GetType())
        modified = bool(document.GetSaveFlag())
        if save and modified:
            path = document.GetPathName()
            if not path:
                raise SolidWorksError(f"'{title}' was never saved: save it to a path first "
                                      "(save_document or drawing_file action='save').")
            self._write_via_saveas3(path, document)
        # compare titles before closing: a closed document's object answers nothing
        current = self._drawing if doc_type == SW_DOC_DRAWING else self._model
        was_current = current is not None and current.GetTitle() == title
        self._sw.CloseDoc(title)
        if doc_type == SW_DOC_DRAWING:
            self._frames.pop(title, None)
            if was_current:
                self._drawing = None
        elif was_current:
            self._model = None
        kind = {SW_DOC_PART: "part", SW_DOC_ASSEMBLY: "assembly", SW_DOC_DRAWING: "drawing"}.get(doc_type, "other")
        return {"ok": True, "closed": title, "type": kind, "saved": bool(save and modified),
                "discarded_changes": bool(modified and not save)}

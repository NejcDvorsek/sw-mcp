"""Welds: weld beads between bodies, and weldments (structural members, end
caps, gussets and their cut list).

Mixed into SolidWorksSession, so every method runs on the COM worker thread.

API facts this depends on (SOLIDWORKS 2026 SP4.1):
- IFeatureManager.InsertCosmeticWeldBead2 returns None for every input tried
  (faces as arrays, selected faces, weld path edges). The older
  InsertCosmeticWeldBead(size) works on two selected faces: the first is the
  'weld from' face, the second the 'weld to' face; a third is ignored. Its
  ICosmeticWeldBeadFeatureData then takes the side and an intermittent bead
  through ModifyDefinition, but not other faces (SetEntitiesWeldFrom/To fail).
  GetReferenceEdges gives the bead's edges: their lengths are the weld's.
- A weld bead joins faces of two bodies; between faces of one body, as in a
  single body part or across a sheet metal corner, it fails. Weld path mode
  (along edges) fails as well.
- InsertFilletBeadFeature (1, 2 and 3) returns None for every input tried,
  in weldments of extrusions and of structural members alike: the weld bead
  does the fillet bead's job.
- InsertStructuralWeldment5 needs a weldment part (InsertWeldmentFeature
  first): without the Weldment feature the members come, but no cut list.
  Path is the profile's .sldlfp file, ConfigurationName its size. The
  members' bodies are centred on the sketch lines; with corner treatment 1
  (mitre) a closed rectangle gives members as long along their centre line
  as the sketch's sides. Each cut list folder carries LENGTH (outer, mitres
  included), ANGLE1 / ANGLE2, QUANTITY and DESCRIPTION.
- InsertEndCapFeature3 caps the selected end face of a member; its first
  flag False sets the cap's inset from the wall thickness (ratio).
- InsertGussetFeature3 takes the two supporting faces selected with mark 1
  (marks 0 or 1 and 2 fail). ProfileD1 runs along the first face, ProfileD2
  along the second. BIsProfile True is the polygon (its ends cut square,
  ProfileD3 long), False the triangle.
"""

import math
import os
import re

from . import binding
from .constants import (
    GUSSET_POSITIONS,
    GUSSET_THICKNESS_SIDES,
    SW_CONNECTED_SEGMENTS_SIMPLE_CUT,
    SW_CORNER_TREATMENTS,
    SW_PREF_WELDMENT_PROFILES,
    WELD_BEAD_SIDES,
)
from .errors import SolidWorksError
from .units import deg_to_rad, m_to_mm, mm_to_m


class WeldTools:
    """Mixed into SolidWorksSession."""

    # --- weld beads ---------------------------------------------------------------

    def add_weld_bead(self, from_mm: list, to_mm: list, size_mm: float, side: str = "one",
                      length_mm: float | None = None, pitch_mm: float | None = None, staggered: bool = False,
                      name: str = "Weld Bead") -> dict:
        """A fillet weld bead where the face through from_mm meets the face
        through to_mm, faces of two bodies (a plate standing on a base, a
        tube on a plate, two members of a weldment). size_mm is the leg.
        side 'one' welds along the from face on the side the faces meet;
        'both' on both sides of it, 'all_around' all round it. length_mm and
        pitch_mm make it intermittent: beads length_mm long, pitch_mm apart
        (start to start); staggered=True offsets the two sides' beads.
        The bead shows in the model and in drawings; it adds no mass.
        """
        if size_mm is None or size_mm <= 0:
            raise SolidWorksError(f"size must be > 0 (got {size_mm}).")
        place = WELD_BEAD_SIDES.get(str(side).lower())
        if place is None:
            raise SolidWorksError(f"Unknown side '{side}'. Use {', '.join(WELD_BEAD_SIDES)}.")
        if (length_mm is None) != (pitch_mm is None):
            raise SolidWorksError("An intermittent weld needs both length_mm (each bead) and pitch_mm.")
        if length_mm is not None and not 0 < length_mm < pitch_mm:
            raise SolidWorksError(f"Each bead is shorter than the pitch: 0 < length_mm < pitch_mm "
                                  f"(got {length_mm} and {pitch_mm}).")
        if staggered and (length_mm is None or place != WELD_BEAD_SIDES["both"]):
            raise SolidWorksError("staggered goes with an intermittent weld on side='both'.")
        self._require_part()
        first, second = self._part_face_at(self._point(from_mm, "from_mm")), self._part_face_at(self._point(to_mm, "to_mm"))
        if self._same_body(first, second):
            raise SolidWorksError("Both faces belong to one body: a weld bead joins two bodies (build the second "
                                  "part with merge=False, or as a structural member).")
        model = self._model
        model.ClearSelection2(True)
        for face in (first, second):
            if not binding.wrap(face, self._mod.IEntity).Select4(True, None):
                raise SolidWorksError("Could not select the faces to weld.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        bead = feat_mgr.InsertCosmeticWeldBead(mm_to_m(size_mm))
        model.ClearSelection2(True)
        if bead is None:
            raise SolidWorksError("SolidWorks made no weld bead: do the two faces meet (touch along a line)?")
        bead = binding.wrap(bead, self._mod.IFeature)
        if place != WELD_BEAD_SIDES["one"] or length_mm is not None:
            data = binding.wrap(bead.GetDefinition(), self._mod.ICosmeticWeldBeadFeatureData)
            data.AccessSelections(model, None)
            data.Side = place
            if length_mm is not None:
                data.IntermittentWeld = True
                data.IntermittentWeldLength = mm_to_m(length_mm)
                data.GapOrPitch = True
                data.Pitch = mm_to_m(pitch_mm)
                data.Staggered = bool(staggered)
            if not bead.ModifyDefinition(data, model, None):
                data.ReleaseSelectionAccess()
                raise SolidWorksError("SolidWorks would not set the weld's side and spacing.")
        result = self._finish_feature(bead, name)
        result.update(self._bead_report(bead))
        if not result["weld_length_mm"]:
            raise SolidWorksError("The weld bead found no line where the faces meet: give faces that touch.")
        return result

    def _bead_report(self, bead, document=None) -> dict:
        """A weld bead's size, side and length (the sum of its beads)."""
        data = binding.wrap(bead.GetDefinition(), self._mod.ICosmeticWeldBeadFeatureData)
        data.AccessSelections(document or self._model, None)
        try:
            lengths = [self._edge_length_mm(e) for e in data.GetReferenceEdges() or ()]
            side = next((k for k, v in WELD_BEAD_SIDES.items() if v == data.Side), str(data.Side))
            out = {"size_mm": round(m_to_mm(data.BeadSize), 6), "side": side, "beads": len(lengths),
                   "weld_length_mm": round(sum(lengths), 4)}
            if data.IntermittentWeld:
                out.update(length_mm=round(m_to_mm(data.IntermittentWeldLength), 6),
                           pitch_mm=round(m_to_mm(data.Pitch), 6))
        finally:
            data.ReleaseSelectionAccess()
        return out

    def _edge_length_mm(self, edge) -> float:
        edge = binding.wrap(edge, self._mod.IEdge)
        params = edge.GetCurveParams2()
        return m_to_mm(binding.wrap(edge.GetCurve(), self._mod.ICurve).GetLength3(params[6], params[7]))

    def _same_body(self, first, second) -> bool:
        return binding.wrap(first.GetBody(), self._mod.IBody2).Name == binding.wrap(second.GetBody(), self._mod.IBody2).Name

    def _weld_beads(self, document=None) -> list:
        """Every weld bead feature of the part: in the sub folders of its Weld
        Folder, not in the history after the origin."""
        model = document or self._model
        found, feat = [], binding.wrap(model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            if feat.GetTypeName2() == "CosmeticWeldCutList":
                found += [bead for folder in self._sub_features(feat) for bead in self._sub_features(folder)
                          if bead.GetTypeName2() == "CosmeticWeldBead"]
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        return found

    @staticmethod
    def _point(value, label: str) -> list:
        if value is None or len(value) != 3:
            raise SolidWorksError(f"{label} is a model point [x, y, z] on a face (got {value}).")
        return [float(c) for c in value]

    # --- structural members ---------------------------------------------------------

    def _profile_library(self) -> str:
        folder = self._sw.GetUserPreferenceStringValue(SW_PREF_WELDMENT_PROFILES)
        if not folder or not os.path.isdir(folder):
            raise SolidWorksError(f"SolidWorks' weldment profile folder is not there ({folder!r}): set it under "
                                  "Options > File Locations > Weldment Profiles.")
        return folder

    def _profile_file(self, standard: str, profile: str, size: str) -> tuple:
        """(the .sldlfp file, its size name) of a library profile, matched without
        regard to case; the error names what the library holds."""
        root = self._profile_library()
        standards = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
        folder = next((d for d in standards if d.lower() == str(standard).lower()), None)
        if folder is None:
            raise SolidWorksError(f"No profile standard '{standard}'. The library holds: {', '.join(standards)}.")
        files = sorted(f[:-7] for f in os.listdir(os.path.join(root, folder)) if f.lower().endswith(".sldlfp"))
        name = next((f for f in files if f.lower() == str(profile).lower()), None)
        if name is None:
            raise SolidWorksError(f"No profile '{profile}' in {folder}. It holds: {', '.join(files)}.")
        path = os.path.join(root, folder, name + ".sldlfp")
        sizes = list(self._sw.GetConfigurationNames(path) or ())

        def plain(text) -> str:
            return re.sub(r"\s+", "", str(text)).lower()

        match = next((s for s in sizes if plain(s) == plain(size)), None)
        if match is None:
            raise SolidWorksError(f"No size '{size}' of {folder} {name}. Sizes: {', '.join(sizes)}.")
        return path, match

    def add_structural_member(self, profile: str, size: str, path_mm: list | None = None,
                              sketch: str | None = None, plane: str = "front", standard: str = "iso",
                              corner: str | None = "miter", rotate_deg: float = 0.0, name: str | None = None) -> dict:
        """Structural members (tube, pipe, angle, channel, beam) of a library
        profile along the lines of a path: a weldment frame. path_mm:
        [[x, y], ...] on `plane`, a chain of lines (closed when the last point
        is the first); sketch: a sketch of the part by name instead (lines and
        arcs; construction lines are left out). profile and size as the
        library names them ('square tube', '30 x 30 x 2.6'). The profile is
        centred on each line; corner 'miter', 'butt1', 'butt2' or None treats
        where members meet. rotate_deg turns the profile about the line.
        The part becomes a weldment: cut_list gives the members' lengths.
        """
        if (path_mm is None) == (sketch is None):
            raise SolidWorksError("Give path_mm (points on a plane) or sketch (a sketch by name).")
        treatment = None
        if corner is not None:
            treatment = SW_CORNER_TREATMENTS.get(str(corner).lower())
            if treatment is None:
                raise SolidWorksError(f"Unknown corner '{corner}'. Use {', '.join(SW_CORNER_TREATMENTS)} or None.")
        self._require_part()
        library_file, size_name = self._profile_file(standard, profile, size)
        if path_mm is not None:
            if len(path_mm) < 2:
                raise SolidWorksError("path_mm needs at least two points.")
            sketch = self.add_sketch(plane, path_mm[0], [{"line": p} for p in path_mm[1:]])["sketch"]
        segments = self._path_segments(sketch)
        model = self._model
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        if not binding.wrap(model, self._mod.IPartDoc).IsWeldment():
            if feat_mgr.InsertWeldmentFeature() is None:
                raise SolidWorksError("SolidWorks would not make the part a weldment.")
        group = binding.wrap(feat_mgr.CreateStructuralMemberGroup(), self._mod.IStructuralMemberGroup)
        group.Segments = self._dispatch_array(segments)
        group.ApplyCornerTreatment = treatment is not None
        if treatment is not None:
            group.CornerTreatmentType = treatment
        group.Angle = deg_to_rad(rotate_deg)
        model.ClearSelection2(True)
        bodies_before = {b.Name for b in self._part_bodies()}
        member = feat_mgr.InsertStructuralWeldment5(library_file, SW_CONNECTED_SEGMENTS_SIMPLE_CUT, True,
                                                    self._dispatch_array([group]), size_name)
        if member is None:
            raise SolidWorksError(f"InsertStructuralWeldment5 failed (None): are the lines of {sketch} one chain, "
                                  "or parallel, with no corner too sharp for the profile?")
        result = self._finish_feature(binding.wrap(member, self._mod.IFeature), name or f"{profile} {size_name}",
                                      sketch=sketch, profile=f"{os.path.basename(os.path.dirname(library_file))} "
                                      f"{profile} {size_name}")
        result["members"] = len([b for b in self._part_bodies() if b.Name not in bodies_before])
        result["cut_list"] = self._cut_list_items()
        return result

    def _path_segments(self, sketch: str) -> list:
        """The sketch's lines and arcs that are not construction geometry."""
        drawn = binding.wrap(self._sketch_by_name(sketch).GetSpecificFeature2(), self._mod.ISketch)
        segments = [binding.wrap(s, self._mod.ISketchSegment) for s in drawn.GetSketchSegments() or ()]
        segments = [s for s in segments if not s.ConstructionGeometry]
        if not segments:
            raise SolidWorksError(f"{sketch} has no lines for members to run along.")
        return segments

    def _require_weldment(self):
        part = binding.wrap(self._require_part(), self._mod.IPartDoc)
        if not part.IsWeldment():
            raise SolidWorksError("The part is not a weldment: start it with add_structural_member.")
        return part

    def _planar_face_at(self, point_mm: list, label: str):
        face = self._part_face_at(self._point(point_mm, label))
        if not binding.wrap(face.GetSurface(), self._mod.ISurface).IsPlane():
            raise SolidWorksError(f"The face through {label} is curved; it needs a flat one.")
        return face

    def add_end_cap(self, at_mm: list, thickness_mm: float, inset_mm: float | None = None, inward: bool = False,
                    chamfer_mm: float | None = None, name: str = "End cap") -> dict:
        """A plate closing the open end of a tube or pipe member: the end face
        through at_mm (a point on the tube wall's end). thickness_mm thick,
        outward past the end (inward=True: into the tube); its edge set in
        from the tube's outside by inset_mm (default half the wall).
        chamfer_mm chamfers its corners."""
        if thickness_mm is None or thickness_mm <= 0:
            raise SolidWorksError(f"thickness must be > 0 (got {thickness_mm}).")
        if inset_mm is not None and inset_mm < 0:
            raise SolidWorksError(f"inset must be >= 0 (got {inset_mm}).")
        if chamfer_mm is not None and chamfer_mm <= 0:
            raise SolidWorksError(f"chamfer must be > 0 (got {chamfer_mm}).")
        self._require_weldment()
        face = self._planar_face_at(at_mm, "at_mm")
        model = self._model
        model.ClearSelection2(True)
        if not binding.wrap(face, self._mod.IEntity).Select4(True, None):
            raise SolidWorksError("Could not select the end face.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        cap = feat_mgr.InsertEndCapFeature3(mm_to_m(thickness_mm), inset_mm is not None, chamfer_mm is not None,
                                            mm_to_m(inset_mm or 0.0), 0.5, mm_to_m(chamfer_mm or 0.0),
                                            False, 0.0, False, bool(inward))
        model.ClearSelection2(True)
        if cap is None:
            raise SolidWorksError("InsertEndCapFeature3 failed (None): is the face the open end of a tube or pipe?")
        cap = binding.wrap(cap, self._mod.IFeature)
        result = self._finish_feature(cap, name)
        data = binding.wrap(cap.GetDefinition(), self._mod.IEndCapFeatureData)
        result["inset_mm"] = round(m_to_mm(data.OffsetDistance), 6) if inset_mm is not None else None
        if cap.GetErrorCode2()[0]:
            raise SolidWorksError("The end cap does not rebuild: is the inset larger than the opening?")
        return result

    def add_gusset(self, first_mm: list, second_mm: list, leg1_mm: float, leg2_mm: float, thickness_mm: float,
                   shape: str = "triangle", end_mm: float | None = None, position: str = "center",
                   thickness_side: str = "both", name: str = "Gusset") -> dict:
        """A gusset plate in the corner of two members: the faces through
        first_mm and second_mm, at right angles. leg1_mm runs along the first
        face, leg2_mm along the second. shape 'triangle', or 'polygon' with
        its two tips cut square end_mm long. position 'center', 'start' or
        'end' along the corner; thickness_side 'both' (centred), 'inner' or
        'outer'."""
        for label, value in (("leg1", leg1_mm), ("leg2", leg2_mm), ("thickness", thickness_mm)):
            if value is None or value <= 0:
                raise SolidWorksError(f"{label} must be > 0 (got {value}).")
        key = str(shape).lower()
        if key not in ("triangle", "polygon"):
            raise SolidWorksError(f"Unknown shape '{shape}'. Use triangle or polygon.")
        if (key == "polygon") != (end_mm is not None):
            raise SolidWorksError("end_mm goes with shape='polygon' (and a polygon needs it).")
        if end_mm is not None and not 0 < end_mm < min(leg1_mm, leg2_mm):
            raise SolidWorksError(f"end_mm lies between 0 and the shorter leg (got {end_mm}).")
        loc = GUSSET_POSITIONS.get(str(position).lower())
        if loc is None:
            raise SolidWorksError(f"Unknown position '{position}'. Use {', '.join(GUSSET_POSITIONS)}.")
        side = GUSSET_THICKNESS_SIDES.get(str(thickness_side).lower())
        if side is None:
            raise SolidWorksError(f"Unknown thickness_side '{thickness_side}'. Use {', '.join(GUSSET_THICKNESS_SIDES)}.")
        self._require_weldment()
        faces = [self._planar_face_at(first_mm, "first_mm"), self._planar_face_at(second_mm, "second_mm")]
        normals = [binding.wrap(f, self._mod.IFace2).Normal for f in faces]
        if abs(sum(a * b for a, b in zip(*normals))) > 0.01:
            raise SolidWorksError("A gusset fills the corner of two faces at right angles; these are not.")
        model = self._model
        selection = binding.wrap(model.SelectionManager, self._mod.ISelectionMgr)
        marked = binding.wrap(selection.CreateSelectData(), self._mod.ISelectData)
        marked.Mark = 1
        model.ClearSelection2(True)
        for face in faces:
            if not binding.wrap(face, self._mod.IEntity).Select4(True, marked):
                raise SolidWorksError("Could not select the gusset's faces.")
        feat_mgr = binding.wrap(model.FeatureManager, self._mod.IFeatureManager)
        quarter = math.pi / 4
        gusset = feat_mgr.InsertGussetFeature3(
            mm_to_m(thickness_mm), side, loc, key == "polygon", mm_to_m(leg1_mm), mm_to_m(leg2_mm),
            mm_to_m(end_mm or 0.0), quarter, mm_to_m(end_mm or 0.0),
            False, 0.0, 0, False, False, False,            # offset, its distance, curve, reverse dir / face, length dim
            0.0, 0.0, quarter, False, False,               # chamfer: distances, angle, length dim, used
        )
        model.ClearSelection2(True)
        if gusset is None:
            raise SolidWorksError("InsertGussetFeature3 failed (None): do the faces meet in a corner, with room "
                                  "for the legs?")
        return self._finish_feature(binding.wrap(gusset, self._mod.IFeature), name)

    # --- cut list ---------------------------------------------------------------------

    def cut_list(self) -> dict:
        """The weldment's cut list (each kind of member: description, length,
        end angles, quantity) and its weld beads with their lengths."""
        self._require_part()
        items = self._cut_list_items()
        beads = [{"feature": b.Name, **self._bead_report(b)} for b in self._weld_beads()]
        if not items and not beads:
            raise SolidWorksError("The part has no members and no weld beads: add_structural_member, add_weld_bead.")
        return {"ok": True, "items": items, "weld_beads": beads,
                "total_weld_length_mm": round(sum(b["weld_length_mm"] for b in beads), 4)}

    def _cut_list_items(self, document=None) -> list:
        """Each cut list folder of a weldment, its properties read."""
        model = document or self._model
        if not binding.wrap(model, self._mod.IPartDoc).IsWeldment():
            return []
        folders, feat = [], binding.wrap(model.FirstFeature(), self._mod.IFeature)
        while feat is not None:
            kind = feat.GetTypeName2()
            if kind == "SolidBodyFolder":
                binding.wrap(feat.GetSpecificFeature2(), self._mod.IBodyFolder).UpdateCutList()
            elif kind == "CutListFolder":
                folders.append(feat)
            feat = binding.wrap(feat.GetNextFeature(), self._mod.IFeature)
        items = []
        for folder in folders:
            manager = binding.wrap(folder.CustomPropertyManager, self._mod.ICustomPropertyManager)
            props = {}
            for key in manager.GetNames() or ():
                value = manager.Get6(key, False)
                props[key.upper()] = value[2] if isinstance(value, tuple) and len(value) > 2 else value
            count = binding.wrap(folder.GetSpecificFeature2(), self._mod.IBodyFolder).GetBodyCount()
            item = {"name": folder.Name, "description": props.get("DESCRIPTION", ""), "quantity": count}
            for key, label in (("LENGTH", "length_mm"), ("ANGLE1", "angle1_deg"), ("ANGLE2", "angle2_deg")):
                number = self._leading_number(props.get(key))
                if number is not None:
                    item[label] = number
            items.append(item)
        return items

    @staticmethod
    def _leading_number(text):
        """'430' -> 430.0, '45°' -> 45.0, '' -> None."""
        found = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", str(text or ""))
        return float(found.group(1).replace(",", ".")) if found else None

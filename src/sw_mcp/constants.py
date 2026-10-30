"""Verified SolidWorks enum constants.

Source: the installed swconst.tlb (SOLIDWORKS 2026, typelib v34), read directly
via scripts/introspect_api.py. Hardcoded here -- with their enum of origin -- so
the server has no runtime dependency on the makepy constant cache. Note e.g.
swDefaultTemplatePart == 8 on this build (not 9, as often stated online): always
trust the installed library over web docs.
"""

# swDocumentTypes_e (for OpenDoc6 / IModelDoc2.GetType)
SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_DOC_DRAWING = 3

# swEndConditions_e
SW_END_COND_BLIND = 0
SW_END_COND_THROUGH_ALL = 1
SW_END_COND_UP_TO_SURFACE = 4  # the face selected with mark 1
SW_END_COND_MID_PLANE = 6
SW_END_COND_UP_TO_NEXT = 11

# swBodyType_e
SW_BODY_SOLID = 0

# swFeatureError_e: what IFeature.GetErrorCode2 reports, for the codes a tool meets
FEATURE_ERRORS = {
    1: "unknown",
    12: "the fillet radius does not fit",
    38: "a mate edge is not valid",
    39: "a mate face is not valid",
    41: "a mate entity is not valid",
    43: "dangling: a face or edge it used is gone",
    46: "over-defined: it fights another mate",
    47: "ill-defined",
    48: "broken: a face or edge it used is gone",
}

# swFeatureFilletType_e
SW_FILLET_TYPE_SIMPLE = 0
SW_FILLET_TYPE_VARIABLE = 1
SW_FILLET_TYPE_FULL_ROUND = 3

# swFeatureFilletOptions_e (bitmask). UNIFORM_RADIUS makes the fillet use the
# single R1 radius for all edges; without it the API expects a per-edge Radii
# array and returns None. Tangent propagation is intentionally NOT enabled, so
# the explicit edge selection equals exactly what gets filleted.
SW_FILLET_OPT_UNIFORM_RADIUS = 2
SW_FILLET_OPT_PROPAGATE = 1        # swFeatureFilletPropagate: on along edges that run on tangentially
# swFeatureFilletVarRadiusType: a variable radius runs straight from end to end
# (without it, along a smooth curve that misses the hand calculation)
SW_FILLET_OPT_STRAIGHT_TRANSITION = 4

# Full round fillet selection marks: side face set 1, centre face set, side face set 2
SW_MARK_FULL_ROUND_SIDE_1 = 2
SW_MARK_FULL_ROUND_CENTRE = 512
SW_MARK_FULL_ROUND_SIDE_2 = 4

# swChamferType_e -- AngleDistance is a setback distance + an angle (45 deg gives
# a symmetric chamfer). EqualDistance(16) alone is a silent no-op on this build.
SW_CHAMFER_ANGLE_DISTANCE = 1
# swFeatureChamferOption_e: measure the distance and angle from each edge's other face
SW_FEATURE_CHAMFER_FLIP = 1

# swSketchSlotCreationType_e / swSketchSlotLengthType_e
SW_SLOT_CREATION_LINE = 0       # straight slot
SW_SLOT_LENGTH_CENTER = 0       # length is centre-to-centre of the end arcs

# swRefPlaneReferenceConstraints_e -- offset a new plane a fixed distance from a
# selected reference plane (for lofts: one parallel plane per profile); with
# OptionFlip the offset goes against the plane's normal.
SW_REF_PLANE_DISTANCE = 8
SW_REF_PLANE_FLIP = 256
# A plane at an angle to another about an axis it holds (add_plane): Angle to
# the plane (selected first, mark 0) and Coincident with the axis (mark 1).
SW_REF_PLANE_COINCIDENT = 4
SW_REF_PLANE_ANGLE = 16
# A plane through a point square to a line (add_plane with normal): Perpendicular
# to the line (mark 0) and Coincident with its start point (mark 1).
SW_REF_PLANE_PERPENDICULAR = 2

# IFeatureManager.InsertMirrorFeature2 (verified): selection marks for what to
# mirror (features 1, a body 256) and the mirror plane (2); swFeatureScope_e.
SW_MARK_MIRROR_FEATURE = 1
SW_MARK_MIRROR_PLANE = 2
SW_MARK_MIRROR_BODY = 256
SW_FEATURE_SCOPE_ALL_BODIES = 0

# SolidWorks' own modelled thread: IFeatureManager.CreateDefinition(swFmSweepThread)
# gives an IThreadFeatureData. The profile libraries (File Locations > Thread
# Profiles) hold one configuration per valid size, e.g. 'M10x1.5'.
SW_FM_SWEEP_THREAD = 87          # swFeatureNameID_e
SW_THREAD_METHOD_CUT = 0         # swThreadMethod_e
SW_THREAD_END_BLIND = 0          # swThreadEndCondition_e
THREAD_PROFILE_EXTERNAL = "Metric Die"
THREAD_PROFILE_INTERNAL = "Metric Tap"

# Hole Wizard (IFeatureManager.HoleWizard5): SolidWorks' own standard tables.
SW_WZD_COUNTERBORE = 0             # swWzdGeneralHoleTypes_e
SW_WZD_COUNTERSINK = 1
SW_WZD_HOLE = 2
SW_WZD_TAP = 4
SW_WZD_STANDARD_ISO = 8            # swWzdHoleStandards_e
SW_ISO_SOCKET_HEAD_CAP = 139       # swWzdHoleStandardFastenerTypes_e (ISO 4762)
SW_ISO_SOCKET_COUNTERSUNK = 140    # ISO 10642
SW_ISO_SCREW_CLEARANCES = 144      # ISO 273
SW_ISO_TAPPED_HOLE = 147
SW_COSMETIC_THREAD_WITH_CALLOUT = 1  # swWzdHoleCosmeticThreadTypes_e
SCREW_FITS = {"close": 0, "normal": 1, "loose": 2}  # swWzdHoleScrewClearanceTypes_e
SW_SEL_FACES = 2                   # swSelectType_e, for IModelDocExtension.SelectByRay
SW_SEL_SKETCH_POINTS = 11          # swSelectType_e: a sketch point a dimension is attached to
SW_SEL_EXT_SKETCH_POINTS = 25      # swSelectType_e: an external one, such as the origin's
SW_HOR_LINEAR_DIMENSION = 11       # swDimensionType_e of a display dimension
SW_VERT_LINEAR_DIMENSION = 12

# STL/3MF tessellation, set as ISldWorks user preferences BEFORE SaveAs3 (the mesh
# translator reads them at save time). These are GLOBAL/application prefs, so the
# caller must save and restore them around the export.
SW_STL_QUALITY = 78            # swUserPreferenceIntegerValue_e (swSTLQuality)
SW_STL_QUALITY_COARSE = 1      # swSTLQuality_e
SW_STL_QUALITY_FINE = 2
SW_STL_QUALITY_CUSTOM = 3      # enables swSTLDeviation + swSTLAngleTolerance
SW_STL_DEVIATION = 2           # swUserPreferenceDoubleValue_e -- chord tolerance (METRES)
SW_STL_ANGLE_TOLERANCE = 3     # swUserPreferenceDoubleValue_e -- angular tolerance (RADIANS)

# swStartConditions_e
SW_START_SKETCH_PLANE = 0

# swUserPreferenceStringValue_e
SW_PREF_DEFAULT_TEMPLATE_PART = 8
SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY = 9
SW_PREF_DEFAULT_TEMPLATE_DRAWING = 10
SW_FILE_LOCATIONS_MATERIALS = 28   # swFileLocationsMaterialDatabases: the folders SolidWorks reads .sldmat from
MCP_MATERIAL_DATABASE = "solidworks-mcp"  # where set_material(density_kg_m3=...) materials come from

# A document's length unit (IModelDocExtension.GetUserPreferenceInteger with
# swUnitsLinear, swUserPreferenceIntegerValue_e, and no option): the tools work
# in SI, but equations are typed in these units. Names per swLengthUnit_e.
SW_UNITS_LINEAR = 47
SW_DETAILING_NO_OPTION = 0
LENGTH_UNITS = {0: "millimetres", 1: "centimetres", 2: "metres", 3: "inches", 4: "feet",
                5: "feet and inches", 6: "angstroms", 7: "nanometres", 8: "microns", 9: "mils",
                10: "microinches"}

# swConstraintType_e -- relation types for ISketchRelationManager.AddRelation,
# which (unlike the string-keyed SketchAddConstraints) returns the relation, so a
# refusal is detectable.
SW_CONSTRAINT_HORIZONTAL = 4
SW_CONSTRAINT_VERTICAL = 5
SW_CONSTRAINT_COINCIDENT = 9
SW_CONSTRAINT_FIXED = 17
SW_CONSTRAINT_HORIZONTAL_POINTS = 25   # two points on one horizontal line
SW_CONSTRAINT_VERTICAL_POINTS = 26     # two points on one vertical line
SW_CONSTRAINT_RADIUS = 3               # the relation behind a radius dimension
SW_CONSTRAINT_TANGENT = 6
SW_CONSTRAINT_COLINEAR = 27
SW_RELATIONS_ALL = 0                   # swSketchRelationFilterType_e, for GetRelations

# swTextJustification_e (IModelDoc2.InsertSketchText): the point is the text's
# lower-left corner and the text runs right from it.
SW_TEXT_JUSTIFY_LEFT = 1

# swConstrainedCornerAction_e (ISketchManager.CreateFillet): keep a rounded
# corner as a virtual sharp, so its dimensions and relations stay.
SW_CONSTRAINED_CORNER_KEEP = 1

# swConstrainedStatus_e (ISketch.GetConstrainedStatus), in the words SolidWorks
# uses in its status bar
SW_FULLY_CONSTRAINED = 3
SKETCH_STATUSES = {1: "status unknown", 2: "under defined", 3: "fully defined", 4: "over defined",
                   5: "no solution", 6: "invalid solution", 7: "autosolve off"}

# swDeleteSelectionOptions_e (IModelDocExtension.DeleteSelection2, a bitmask)
SW_DELETE_CHILDREN = 1
SW_DELETE_ABSORBED = 2   # the sketches the feature consumed go with it

# swReplaceComponentsConfiguration_e (IAssemblyDoc.ReplaceComponents2)
SW_REPLACE_CONFIG_MATCH_NAME = 0   # the new file's configuration of the same name, else its active one

# swMoveLocation_e (IModelDocExtension.ReorderFeature): the feature lands before the
# location feature, with the sketch it absorbed
SW_MOVE_BEFORE = 2

# swFeatureSuppressionAction_e / swInConfigurationOpts_e (IFeature.SetSuppression2).
# Suppressing takes the dependents along by itself; unsuppressing brings them
# back only with UNSUPPRESS_DEPENDENT (verified).
SW_SUPPRESS_FEATURE = 0
SW_UNSUPPRESS_DEPENDENT = 2
SW_THIS_CONFIGURATION = 1

# swSketchSegments_e (ISketchSegment.GetType)
SW_SKETCH_LINE = 0
SW_SKETCH_ARC = 1
SW_SKETCH_SPLINE = 3
SW_SKETCH_TEXT = 4

# swDimensionType_e (IDisplayDimension.GetType) / swDimensionDrivenState_e
SW_ANGULAR_DIMENSION = 3
SW_DIMENSION_DRIVING = 2
# swDimensionParamType_e (IDimension.GetType): its SystemValue is in radians
SW_DIMENSION_PARAM_ANGULAR = 1

# --- assemblies ---------------------------------------------------------------

# swAddComponentConfigOptions_e -- insert the component using the configuration
# that is currently selected in the part.
SW_ADD_COMPONENT_CURRENT_CONFIG = 0

# swMateType_e. Concentric takes two cylindrical faces (picked by list_faces
# index), the others two planar faces.
MATE_TYPES = {
    "coincident": 0,     # swMateCOINCIDENT
    "concentric": 1,     # swMateCONCENTRIC
    "perpendicular": 2,  # swMatePERPENDICULAR
    "parallel": 3,       # swMatePARALLEL
    "distance": 5,       # swMateDISTANCE
    "angle": 6,          # swMateANGLE
}

# swMateAlign_e -- CLOSEST lets SolidWorks keep the solution nearest the current
# position, which is what we want because every component is pre-positioned
# before it is mated.
SW_MATE_ALIGN_CLOSEST = 2

# swAddMateError_e -- note NoError is 1, not 0.
SW_ADD_MATE_NO_ERROR = 1

# swComponentSuppressionState_e (IComponent2.GetSuppression2): a suppressed
# component holds no material; a lightweight one has its geometry unloaded.
SW_COMPONENT_SUPPRESSED = 0
SW_COMPONENT_LIGHTWEIGHT_STATES = {1, 4}

# swUserPreferenceToggle_e
SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE = 10
SW_TOGGLE_STL_DONT_TRANSLATE = 71  # keep STL output in model coordinates (default: moved to positive space)
SW_TOGGLE_STL_ONE_FILE = 72        # swSTLComponentsIntoOneFile: an assembly's STL as one file, not one per part
SW_TOGGLE_DISPLAY_AXES = 4         # swDisplayAxes: a document's View > Axes
SW_TOGGLE_DISPLAY_PLANES = 5       # swDisplayPlanes: a document's View > Planes
SW_VIEW_ANIMATION_SPEED = 38       # swViewAnimationSpeed (a double): 0 shows view changes at once
# swMultiCAD_Enable3DInterconnect: with it on, an imported assembly arrives as ONE
# wrapped sub-assembly, so its parts cannot be listed or mated (verified)
SW_TOGGLE_3D_INTERCONNECT = 691

# swRebuildOnActivation_e (ISldWorks.ActivateDoc3): bring a document to the front as it is
SW_DONT_REBUILD_ACTIVE_DOC = 1

# swOpenDocOptions_e -- silent load, no dialogs. AddComponent5 returns None for a
# part that is not loaded yet (verified), so components are opened this way first.
SW_OPEN_DOC_SILENT = 1

# swSaveAsVersion_e
SW_SAVE_AS_CURRENT_VERSION = 0

# swSaveAsOptions_e
SW_SAVE_AS_OPTIONS_SILENT = 1

# swStandardViews_e
SW_VIEW_ISOMETRIC = 7
VIEWS = {"front": 1, "back": 2, "left": 3, "right": 4, "top": 5, "bottom": 6, "iso": SW_VIEW_ISOMETRIC}

# Drawings (drawing.py). swDwgPaperSizes_e / swDwgTemplates_e sit in
# drawing_layout.SHEETS; swDwgTemplateCustom takes a sheet format file.
SW_FILE_LOCATIONS_SHEET_FORMAT = 7   # swFileLocationsSheetFormat
SW_DWG_TEMPLATE_CUSTOM = 12
SW_DETAILING_DIMENSION_STANDARD = 13           # swUserPreferenceIntegerValue_e
SW_DETAILING_STANDARD_ISO = 2                  # swDetailingStandard_e
SW_DETAILING_DIM_TRAILING_ZERO = 15
SW_DIM_REMOVE_TRAILING_ZEROES = 2              # swDetailingDimTrailingZero_e
SW_UNIT_SYSTEM = 263                           # swUnitSystem
SW_UNIT_SYSTEM_CUSTOM = 4
SW_UNITS_MASS = 259                            # swUnitsMassPropMass
SW_UNITS_MASS_GRAMS = 2
SW_UNITS_MASS_KILOGRAMS = 3
SW_UNITS_MASS_DECIMALS = 261                   # swUnitsMassPropDecimalPlaces
SW_VIEW_ENTITY_EDGE = 1                        # swViewEntityType_e
# IDrawingDoc.InsertModelAnnotations3: swImportModelItemsSource_e and
# swInsertAnnotation_e (dimensions + those marked for drawings)
SW_IMPORT_ENTIRE_MODEL = 0
SW_INSERT_DIMENSIONS = 8 | 32768
# swAnnotationType_e
SW_ANNOTATION_DIMENSION = 4
SW_ANNOTATION_NOTE = 6
SW_ANNOTATION_CENTER_MARK = 13
SW_ANNOTATION_CENTERLINE = 15
SW_ANNOTATION_OWNER_TEMPLATE = 2                # swAnnotationOwner_e: the sheet format's own notes
ANNOTATION_TYPES = {SW_ANNOTATION_CENTER_MARK: "center_mark", SW_ANNOTATION_CENTERLINE: "centerline",
                    1: "cosmetic_thread", 2: "datum", 5: "gtol", 7: "surface_finish", 8: "weld_symbol",
                    14: "table", 17: "weld_bead"}
# swDimensionTextParts_e. A hole callout shows its first line in CALLOUT_ABOVE
# and keeps it with the model's variables in CALLOUT_ABOVE_DEFINITION
SW_DIMENSION_TEXT_PREFIX = 1
SW_DIMENSION_TEXT_SUFFIX = 2
SW_DIMENSION_TEXT_CALLOUT_ABOVE = 3
SW_DIMENSION_TEXT_PREFIX_DEFINITION = 5
SW_DIMENSION_TEXT_CALLOUT_ABOVE_DEFINITION = 7
SW_ALIGN_AUTO_ARRANGE = 0                      # swAlignDimensionType_e
SW_DELETE_NO_OPTION = 0
SW_CUSTOM_INFO_TEXT = 30                       # swCustomInfoType_e
SW_CUSTOM_PROPERTY_REPLACE = 2                 # swCustomPropertyAddOption_e
SW_CREATE_SECTION_CHANGE_DIRECTION = 4         # swCreateSectionViewAtOptions_e
SW_DET_VIEW_STANDARD = 0                       # swDetViewStyle_e
SW_DET_CIRCLE_CIRCLE = 1                       # swDetCircleShowType_e
SW_AUTO_CENTER_MARK_HOLES = 1                  # swAutoInsertCenterMarkTypes_e
SW_AUTO_CENTER_MARK_FILLETS = 2
SW_CENTER_MARK_SINGLE = 2                      # swCenterMarkConnectionLine_e
SW_TANGENT_EDGES_HIDDEN = 0                    # swDisplayTangentEdges_e
SW_TANGENT_EDGES_VISIBLE = 2
# IDisplayDimension.Type2 (swDimensionType_e)
DIMENSION_KINDS = {1: "ordinate", 2: "linear", 3: "angular", 4: "arc_length", 5: "radius", 6: "diameter",
                   7: "horizontal_ordinate", 8: "vertical_ordinate", 10: "chamfer", 11: "horizontal",
                   12: "vertical"}
# IView.Type (swDrawingViewTypes_e)
VIEW_TYPES = {1: "sheet", 2: "section", 3: "detail", 4: "projected", 5: "auxiliary", 6: "standard",
              7: "named", 8: "relative"}
# IView.SetDisplayMode4 (swDisplayMode_e)
VIEW_DISPLAY_MODES = {"wireframe": 0, "hidden_visible": 1, "hidden_removed": 2, "shaded": 3, "shaded_edges": 7}
# IDimensionTolerance.Type (swTolType_e)
TOLERANCE_TYPES = {"none": 0, "basic": 1, "bilateral": 2, "limit": 3, "symmetric": 4, "min": 5, "max": 6,
                   "fit": 7, "fit_with_tolerance": 8, "fit_tolerance_only": 9}
# IDrawingDoc.AddOrdinateDimension2 (swAddOrdinateDims_e)
SW_ORDINATE_VERTICAL = 2
SW_ORDINATE_HORIZONTAL = 3
# Symbols: swLeaderStyle_e, swArrowStyle_e, and the slot of an ISFSymbol's
# roughness value (swSurfaceFinishSymbolText_e)
SW_LEADER_STRAIGHT = 1
SW_ARROW_CLOSED = 1
SW_SF_ROUGHNESS = 8
# InsertSurfaceFinishSymbol3's SymType (swSFSymType_e)
SURFACE_FINISH_PROCESSES = {"any": 0, "machined": 1, "no_removal": 2}
# Geometric characteristics as SolidWorks' gtol97.sym names them, for IGtol.SetFrameSymbols2
GTOL_SYMBOLS = {"straightness": "<GTOL-STRAIGHT>", "flatness": "<GTOL-FLAT>", "circularity": "<GTOL-CIRC>",
                "roundness": "<GTOL-CIRC>", "cylindricity": "<GTOL-CYL>", "line_profile": "<GTOL-LPROF>",
                "surface_profile": "<GTOL-SPROF>", "parallelism": "<GTOL-PARA>",
                "perpendicularity": "<GTOL-PERP>", "angularity": "<GTOL-ANGULAR>", "position": "<GTOL-POSI>",
                "concentricity": "<GTOL-CONC>", "coaxiality": "<GTOL-CONC>", "symmetry": "<GTOL-SYMMETRY>",
                "runout": "<GTOL-SRUN>", "total_runout": "<GTOL-TRUN>"}
SW_TABLE_ANCHOR_TOP_LEFT = 1                   # swBOMConfigurationAnchorType_e
DRAWING_EXPORTS = {"pdf", "dxf", "dwg", "png", "jpg", "tif", "slddrw"}

# swRayPtsOpts_e (IModelDoc2.RayIntersections): hit normals, and entry/exit
# points. Each hit comes back as 9 doubles: body, ray, hit type, x y z, nx ny nz.
SW_RAY_NORMALS_ENTRY_EXIT = 1 | 4
RAY_HIT_WIDTH = 9
SW_RAY_HIT_EXIT = 32  # swRayPtsResults_e flag in a hit's type: the ray leaves the body there

# Neutral formats open_part imports as a new part (ISldWorks.LoadFile4).
IMPORT_FORMATS = {"step", "stp", "iges", "igs", "x_t", "x_b"}

# Formats SaveAs3 can write (by extension), allow-listed for `export`. Image
# extensions (png/jpg/tif) are here because `screenshot` writes via the same
# SaveAs3 path; .bmp wrote nothing.
EXPORT_FORMATS = {"step", "stp", "stl", "iges", "igs", "x_t", "x_b", "3mf", "png", "jpg", "tif"}

# --- sheet metal ---------------------------------------------------------------
# swBendAllowanceTypes_e
SW_BEND_ALLOWANCE_K_FACTOR = 2
# swInsertEdgeFlangeOptions_e (bits)
SW_EDGE_FLANGE_DEFAULT_RADIUS = 1
SW_EDGE_FLANGE_FLIP = 2
SW_EDGE_FLANGE_DEFAULT_RELIEF = 128
# swFlangePositionTypes_e: where the flange stands against the edge it grows from
FLANGE_POSITIONS = {"material_inside": 1, "material_outside": 2, "bend_outside": 3}
# swFlangeDimTypes_e
SW_FLANGE_DIM_OUTER_VIRTUAL_SHARP = 1
# swExportToDWG_e, and IPartDoc.ExportToDWG2's SheetMetalOptions bits
SW_EXPORT_DWG_SHEET_METAL = 1
SW_DXF_FLAT_GEOMETRY = 1
SW_DXF_BEND_LINES = 4
# Features whose sketches SolidWorks drives itself: an edge flange's profile
# follows its length, the flat pattern's bend lines and bounding box the part.
# A gusset's profile follows its legs.
# They stay under defined in SolidWorks' own models too.
SOLIDWORKS_DRIVEN_SKETCH_OWNERS = {"EdgeFlange", "FlatPattern", "Gusset"}
# swClosedCornerTypes_e
CLOSED_CORNER_TYPES = {"butt": 1, "overlap": 2, "underlap": 3}

# --- welds ---------------------------------------------------------------------
# swCosmeticWeldBeadSide_e: the bead along the first face only, on both sides of
# it, or all round it
WELD_BEAD_SIDES = {"one": 1, "both": 2, "all_around": 3}
# A structural member group's CornerTreatmentType (no named enum in swconst):
# 1 mitres the members where they meet; 2 / 3 butt them, either one running on
SW_CORNER_TREATMENTS = {"miter": 1, "butt1": 2, "butt2": 3}
# swConnectedSegmentsOption_e
SW_CONNECTED_SEGMENTS_SIMPLE_CUT = 1
# swGussetThicknessType_e, swGussetProfileLocationType_e
GUSSET_THICKNESS_SIDES = {"inner": 0, "both": 1, "outer": 2}
GUSSET_POSITIONS = {"start": 0, "center": 1, "end": 2}
# swUserPreferenceStringValue_e: the folder of the weldment profile library
SW_PREF_WELDMENT_PROFILES = 29
# swInsertAnnotation_e: weld symbols, and the beads of weld bead features
SW_INSERT_WELDS = 256 | 8388608
# ISO 2553 weld symbols as SolidWorks names them (gtol.sym, #WELD)
WELD_SYMBOLS = {
    "fillet": "<WELD-FILL>", "square": "<WELD-BUSQ>", "v": "<WELD-BUSV>", "bevel": "<WELD-BUSB>",
    "v_root": "<WELD-BUSVBR>", "bevel_root": "<WELD-BUSBR>", "u": "<WELD-BUSU>", "j": "<WELD-BUSJ>",
    "backing": "<WELD-BACK>", "plug": "<WELD-PLUG>", "spot": "<WELD-SPOT>", "seam": "<WELD-SEAM>",
}
# swWeldSymbolContourTypes_e, swWeldSymbolField_e
WELD_CONTOURS = {"none": 1, "flat": 2, "convex": 3, "concave": 4}
SW_FIELD_WELD = 2

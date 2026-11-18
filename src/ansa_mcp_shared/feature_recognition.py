"""Shared request contract for native Shell/Solid Feature recognition."""

import math
from typing import Literal


ModellingMode = Literal["Shell", "Solid"]
RecognitionInputKind = Literal["cad", "shell_mesh"]
RecognizeFeatureType = Literal[
    "HOLE 2D", "FILLET", "CHAMFER", "SPECIAL PERIMETER", "STAMP", "FLANGE 2D",
    "RIB 2D", "LOGO", "HOLE 3D", "FLANGE 3D", "FASTENER",
]

RECOGNITION_OPTIONS = {
    "FILLET": {
        "fillet_max_radius": "recognize_fillets_max_radius",
        "fillet_min_angle": "recognize_fillets_min_angle",
    },
    "CHAMFER": {
        "chamfer_max_width": "recognize_chamfers_max_width",
        "chamfer_min_angle": "recognize_chamfers_min_angle",
        "chamfer_max_angle": "recognize_chamfers_max_angle",
    },
    "HOLE 2D": {"hole_max_diameter": "recognize_holes_2d_max_diameter"},
    "HOLE 3D": {},
    "SPECIAL PERIMETER": {
        "special_fe_break_angle": "special_perimeters_fe_break_angle",
        "special_geom_break_angle": "special_perimeters_geom_break_angle",
        "special_sharp": "recognize_sharp_perimeters",
    },
    "STAMP": {
        "stamp_max_height": "recognize_stamps_height",
        "stamp_max_size": "recognize_stamps_size",
    },
    "FLANGE 2D": {
        "flange_2d_min_width": "recognize_flanges_2d_min_width",
        "flange_2d_max_width": "recognize_flanges_2d_max_width",
    },
    "FLANGE 3D": {},
    "RIB 2D": {},
    "LOGO": {
        "logo_max_height": "recognize_logos_height",
        "logo_max_size": "recognize_logos_size",
        "logo_allow_base_fillets": "recognize_logos_allow_fillets_at_base",
        "logo_base_fillet_min_angle": "recognize_logos_base_fillet_min_angle",
    },
    "FASTENER": {
        "fastener_embedded_bolts": "recognize_fasteners_search_for_embedded",
        "fastener_embedded_clips": "recognize_fasteners_search_for_clips",
    },
}
LEGACY_FEATURES = frozenset({"FILLET", "CHAMFER", "HOLE 2D"})
MODELLING_VALUES = {"Shell": "Shell Modelling", "Solid": "Solid Modelling"}
_OTHER_MODE_FEATURES = {
    "Shell": {"FLANGE 3D", "FASTENER"},
    "Solid": {"HOLE 2D", "FLANGE 2D", "RIB 2D"},
}
_BOOLEAN_OPTIONS = frozenset({
    "special_sharp", "logo_allow_base_fillets", "fastener_embedded_bolts", "fastener_embedded_clips",
})


def validate_recognition_request(feature_types, parameters, modelling, *, input_kind="cad"):
    if (
        type(feature_types) is not list
        or not feature_types
        or len(feature_types) > len(RECOGNITION_OPTIONS)
        or any(type(kind) is not str or kind not in RECOGNITION_OPTIONS for kind in feature_types)
        or len(set(feature_types)) != len(feature_types)
    ):
        raise ValueError("feature_types must be a nonempty selection of unique native recognition categories")
    kinds = set(feature_types)
    if input_kind not in ("cad", "shell_mesh"):
        raise ValueError("input_kind must be cad or shell_mesh")
    if input_kind == "shell_mesh" and (modelling != "Shell" or kinds != {"RIB 2D"}):
        raise ValueError("shell_mesh input requires Shell modelling and only RIB 2D")
    if modelling is None:
        if not kinds <= LEGACY_FEATURES:
            raise ValueError("modelling must be Shell or Solid for the extended Feature categories")
    elif type(modelling) is not str or modelling not in MODELLING_VALUES:
        raise ValueError("modelling must be Shell or Solid")
    else:
        incompatible = kinds & _OTHER_MODE_FEATURES[modelling]
        if incompatible:
            raise ValueError(f"{modelling} modelling does not support: " + ", ".join(sorted(incompatible)))

    if type(parameters) is not dict or any(type(key) is not str for key in parameters):
        raise ValueError("parameters must be an object")
    required = {key for kind in kinds for key in RECOGNITION_OPTIONS[kind]}
    missing = sorted(required - parameters.keys())
    unexpected = sorted(parameters.keys() - required)
    if missing:
        raise ValueError("missing parameters: " + ", ".join(missing))
    if unexpected:
        raise ValueError("parameters not enabled by feature_types: " + ", ".join(unexpected))

    values = {}
    for name, raw in parameters.items():
        if name in _BOOLEAN_OPTIONS:
            if type(raw) is not bool:
                raise ValueError(name + " must be a boolean")
            values[name] = raw
            continue
        if type(raw) not in (int, float):
            raise ValueError(name + " must be a finite number")
        try:
            value = float(raw)
        except OverflowError:
            raise ValueError(name + " must be a finite number") from None
        if not math.isfinite(value):
            raise ValueError(name + " must be a finite number")
        if name.endswith("_angle"):
            if not 0 <= value < 180 or (name.endswith("_max_angle") and value == 0):
                raise ValueError(name + " is outside the supported angle range")
        elif value < 0 or (value == 0 and name != "flange_2d_min_width"):
            raise ValueError(name + " is outside the supported length range")
        values[name] = value

    if "CHAMFER" in kinds and values["chamfer_min_angle"] >= values["chamfer_max_angle"]:
        raise ValueError("chamfer_min_angle must be less than chamfer_max_angle")
    if "FLANGE 2D" in kinds and values["flange_2d_min_width"] >= values["flange_2d_max_width"]:
        raise ValueError("flange_2d_min_width must be less than flange_2d_max_width")
    return values

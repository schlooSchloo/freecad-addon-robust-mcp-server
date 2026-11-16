"""Compile-test argument overrides for the geometry tools.

Merged into ARGUMENT_OVERRIDES by test_generated_code_compiles.py.
"""

from typing import Any

# Element names and sample options are checked before calling FreeCAD.
OVERRIDES: dict[str, dict[str, Any]] = {
    "get_shape_geometry": {
        "elements": ["Edge1"],
        "samples": 4,
        "sample_tolerance": None,
    },
    "get_section_outline": {
        "plane": "XY",
        "origin": None,
        "normal": None,
        "samples": 4,
        "sample_tolerance": None,
    },
}

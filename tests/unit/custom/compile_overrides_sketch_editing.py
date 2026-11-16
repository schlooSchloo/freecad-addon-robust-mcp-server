"""Compile-test argument overrides for the sketch editing tools.

Merged into ARGUMENT_OVERRIDES by test_generated_code_compiles.py.
"""

from typing import Any

OVERRIDES: dict[str, dict[str, Any]] = {
    "add_sketch_polyline": {"points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]},
    "move_sketch_point": {"position": [1.0, 1.0]},
}

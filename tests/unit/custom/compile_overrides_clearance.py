"""Compile-test argument overrides for the clearance tools.

Merged into ARGUMENT_OVERRIDES by test_generated_code_compiles.py.
"""

from typing import Any

# Moving and fixed objects must differ; excluded pairs must name them.
OVERRIDES: dict[str, dict[str, Any]] = {
    "sweep_clash": {
        "moving": ["Arm"],
        "fixed": ["Wall"],
        "exclude_pairs": [["Arm", "Wall"]],
    },
}

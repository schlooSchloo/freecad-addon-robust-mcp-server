"""Validation tools for FreeCAD Robust MCP Server.

This module provides tools for validating FreeCAD objects and documents,
checking for errors, and providing automatic rollback capabilities.

These tools are essential for robust CAD workflows where operations
may fail or create invalid geometry.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from freecad_mcp.bridge.object_health import OBJECT_HEALTH_HELPERS
from freecad_mcp.tools._registration import unsafe_tool


def register_validation_tools(
    mcp: Any,
    get_bridge: Callable[[], Awaitable[Any]],
    *,
    allow_unsafe_code: bool = False,
) -> None:
    """Register validation-related tools with the Robust MCP Server.

    Args:
        mcp: The MCPServer instance.
        get_bridge: Async function to get the active bridge.
        allow_unsafe_code: Whether to expose transactional Python execution.
    """

    @mcp.tool()
    async def validate_object(
        object_name: str,
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check the health and validity of a FreeCAD object.

        This tool inspects an object to determine if it is in a valid state,
        has any computation errors, or needs recomputation. Use this after
        performing operations to verify they succeeded. When FreeCAD marks the
        object as failed, its own error message is included. Links whose
        target file, document or object is missing are reported as
        unresolved. Sketches with only construction or external (projected)
        geometry are valid references with no output geometry, not failures.

        Args:
            object_name: Name of the object to validate.
            doc_name: Document containing the object. Uses active document if None.

        Returns:
            Dictionary containing:
                - valid: False if FreeCAD reports an error, the shape is
                  invalid, or a link target can't be found
                - object_name: Name of the validated object
                - label: Object label
                - type_id: FreeCAD type identifier
                - classification: "ok", "error", "invalid_shape",
                  "unresolved_link", "reference_only" (sketch with no
                  output geometry), "no_shape" or "not_found"
                - shape_valid: Whether the shape geometry is valid, or None
                  when there is no shape (missing or null) to check
                - has_shape / shape_null: Whether the object has a shape
                  property, and whether that shape is null (empty)
                - output_geometry: Whether the object produces geometry
                - unbounded: True for infinite shapes (datum planes, lines)
                - has_errors: Whether FreeCAD marked the object as failed
                - status_message: FreeCAD's own error message, or None
                - state: List of state flags (e.g., ["Invalid", "Touched"])
                - recompute_needed: Whether recomputation is needed
                - volume: Total volume of the solids (None if no solids or
                  unbounded)
                - area: Shape surface area (None if no shape or unbounded)
                - sketch_geometry: For sketches, counts of normal,
                  construction and external geometry (None otherwise)
                - link_problems: Unresolved links, each with property,
                  problem ("no_target", "missing_file", "document_not_open",
                  "missing_object", "not_restored"), message and, where
                  known, linked_object, linked_file and linked_document
                - error_messages: Error messages, starting with FreeCAD's own
                - warnings: List of any warnings

        Example:
            Validate an object after creating it::

                result = await validate_object("MyBox")
                if not result["valid"]:
                    print(f"Errors: {result['error_messages']}")
        """
        bridge = await get_bridge()

        code = (
            OBJECT_HEALTH_HELPERS
            + f"""
import FreeCAD

doc_name = {doc_name!r}
object_name = {object_name!r}

# Get document
if doc_name:
    doc = FreeCAD.getDocument(doc_name)
else:
    doc = FreeCAD.ActiveDocument

def _not_found(message):
    return {{
        "valid": False,
        "object_name": object_name,
        "error_messages": [message],
        "shape_valid": None,
        "has_errors": True,
        "state": [],
        "recompute_needed": False,
        "volume": None,
        "area": None,
        "warnings": [],
        "classification": "not_found",
    }}

if doc is None:
    _result_ = _not_found("No active document found")
else:
    obj = doc.getObject(object_name)
    if obj is None:
        _result_ = _not_found(
            f"Object '{{object_name}}' not found in document '{{doc.Name}}'"
        )
    else:
        check = _mcp_check_object(obj)
        warnings = check["warnings"]

        # Check for PartDesign-specific issues
        if hasattr(obj, 'BaseFeature') and obj.BaseFeature is None:
            if obj.TypeId not in ["PartDesign::Body", "Sketcher::SketchObject"]:
                warnings.append("PartDesign feature has no base feature")

        check["object_name"] = check.pop("name")
        _result_ = check
"""
        )
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "valid": False,
            "object_name": object_name,
            "error_messages": [result.error_traceback or "Validation failed"],
            "shape_valid": None,
            "has_errors": True,
            "state": [],
            "recompute_needed": False,
            "volume": None,
            "area": None,
            "warnings": [],
        }

    @mcp.tool()
    async def validate_document(
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check the health of all objects in a FreeCAD document.

        This tool validates every object in the document (see
        validate_object for the rules) and provides a summary of the
        document's overall health, with FreeCAD's own error messages. Use this after
        complex operations or before saving/exporting.

        Args:
            doc_name: Name of document to validate. Uses active document if None.

        Returns:
            Dictionary containing:
                - valid: Overall document validity (True if all objects valid)
                - doc_name: Name of the validated document
                - total_objects: Total number of objects in document
                - valid_objects: Count of valid objects
                - invalid_objects: List of names of invalid objects
                - objects_with_errors: List of names with error states
                - objects_needing_recompute: List of objects that need recompute
                - recompute_needed: Whether document needs recomputation
                - summary: Human-readable summary of document health,
                  including FreeCAD's error messages for the first problems
                - problems: Up to 50 objects that are not valid, each with
                  name, label, type_id, classification, state,
                  status_message (FreeCAD's own error text), error_messages
                  and link_problems (see validate_object)
                - problems_truncated: True if more problems exist than listed
                - unresolved_links: Names of objects with link targets that
                  can't be found (missing file, document or object)
                - reference_only_objects: Sketches with only construction or
                  external geometry; valid, but they produce no geometry

        Example:
            Check document health before saving::

                result = await validate_document()
                if result["valid"]:
                    await save_document()
                else:
                    print(f"Issues: {result['invalid_objects']}")
        """
        bridge = await get_bridge()

        code = (
            OBJECT_HEALTH_HELPERS
            + f"""
import FreeCAD

doc_name = {doc_name!r}
_problem_limit = 50

# Get document
if doc_name:
    doc = FreeCAD.getDocument(doc_name)
else:
    doc = FreeCAD.ActiveDocument

if doc is None:
    _result_ = {{
        "valid": False,
        "doc_name": None,
        "total_objects": 0,
        "valid_objects": 0,
        "invalid_objects": [],
        "objects_with_errors": [],
        "objects_needing_recompute": [],
        "recompute_needed": False,
        "summary": "No active document found",
        "problems": [],
        "problems_truncated": False,
        "unresolved_links": [],
        "reference_only_objects": [],
    }}
else:
    total_objects = len(doc.Objects)
    valid_count = 0
    invalid_objects = []
    objects_with_errors = []
    objects_needing_recompute = []
    unresolved_links = []
    reference_only = []
    problems = []
    problem_count = 0

    for obj in doc.Objects:
        check = _mcp_check_object(obj)

        if check["has_errors"]:
            objects_with_errors.append(obj.Name)
        if check["shape_valid"] is False:
            invalid_objects.append(obj.Name)
        if check["link_problems"]:
            unresolved_links.append(obj.Name)
        if check["recompute_needed"]:
            objects_needing_recompute.append(obj.Name)
        if check["classification"] == "reference_only":
            reference_only.append(obj.Name)

        if check["valid"]:
            valid_count += 1
        else:
            problem_count += 1
            if len(problems) < _problem_limit:
                problems.append({{
                    "name": obj.Name,
                    "label": check["label"],
                    "type_id": check["type_id"],
                    "classification": check["classification"],
                    "state": check["state"],
                    "status_message": check["status_message"],
                    "error_messages": check["error_messages"],
                    "link_problems": check["link_problems"],
                }})

    # Build summary
    if valid_count == total_objects:
        summary = f"Document '{{doc.Name}}' is healthy: all {{total_objects}} objects are valid"
    else:
        issues = []
        if invalid_objects:
            issues.append(f"{{len(invalid_objects)}} invalid objects")
        if objects_with_errors:
            issues.append(f"{{len(objects_with_errors)}} objects with errors")
        if unresolved_links:
            issues.append(f"{{len(unresolved_links)}} unresolved links")
        if objects_needing_recompute:
            issues.append(f"{{len(objects_needing_recompute)}} objects need recompute")
        summary = f"Document '{{doc.Name}}' has issues: " + ", ".join(issues)
        details = [
            p["name"] + ": " + (p["error_messages"][0] if p["error_messages"] else p["classification"])
            for p in problems[:5]
        ]
        if details:
            summary += ". " + "; ".join(details)
    if reference_only:
        summary += (
            f". {{len(reference_only)}} sketch(es) have no output geometry "
            "(reference only, not an error)"
        )

    _result_ = {{
        "valid": valid_count == total_objects,
        "doc_name": doc.Name,
        "total_objects": total_objects,
        "valid_objects": valid_count,
        "invalid_objects": invalid_objects,
        "objects_with_errors": objects_with_errors,
        "objects_needing_recompute": objects_needing_recompute,
        "recompute_needed": len(objects_needing_recompute) > 0,
        "summary": summary,
        "problems": problems,
        "problems_truncated": problem_count > len(problems),
        "unresolved_links": unresolved_links,
        "reference_only_objects": reference_only,
    }}
"""
        )
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "valid": False,
            "doc_name": doc_name,
            "total_objects": 0,
            "valid_objects": 0,
            "invalid_objects": [],
            "objects_with_errors": [],
            "objects_needing_recompute": [],
            "recompute_needed": False,
            "summary": result.error_traceback or "Validation failed",
        }

    @mcp.tool()
    async def undo_if_invalid(
        doc_name: str | None = None,
    ) -> dict[str, Any]:
        """Check document health and undo the last operation if invalid objects exist.

        This tool first validates all objects in the document. If any objects
        are invalid or have errors, it automatically performs an undo operation.
        Sketches with no output geometry and objects without a shape do not
        count as invalid.
        Use this after risky operations to ensure the model stays in a valid state.

        Args:
            doc_name: Name of document to check. Uses active document if None.

        Returns:
            Dictionary containing:
                - was_valid: Whether the document was valid before any undo
                - undone: Whether an undo operation was performed
                - invalid_objects: List of invalid objects found (before undo)
                - objects_with_errors: List of objects with errors (before undo)
                - problems: Failed objects (before undo), each with name and
                  error_messages (FreeCAD's own error text first)
                - message: Human-readable description of what happened
                - validation_after: Validation result after undo (if performed)

        Example:
            Auto-recover from a failed boolean operation::

                await boolean_operation("cut", "Box", "InvalidShape")
                result = await undo_if_invalid()
                if result["undone"]:
                    print("Recovered from invalid operation")
        """
        bridge = await get_bridge()

        code = (
            OBJECT_HEALTH_HELPERS
            + f"""
import FreeCAD

doc_name = {doc_name!r}

# Get document
if doc_name:
    doc = FreeCAD.getDocument(doc_name)
else:
    doc = FreeCAD.ActiveDocument

def _scan(doc):
    # Only failures FreeCAD reports, or invalid shapes, count: a sketch
    # with no output geometry or an object without a shape is not a failure.
    invalid, errors, problems = [], [], []
    for obj in doc.Objects:
        check = _mcp_check_object(obj)
        if check["has_errors"]:
            errors.append(obj.Name)
        if check["shape_valid"] is False:
            invalid.append(obj.Name)
        if (check["has_errors"] or check["shape_valid"] is False) and len(problems) < 50:
            problems.append({{"name": obj.Name, "error_messages": check["error_messages"]}})
    return invalid, errors, problems

if doc is None:
    _result_ = {{
        "was_valid": False,
        "undone": False,
        "invalid_objects": [],
        "objects_with_errors": [],
        "message": "No active document found",
        "validation_after": None
    }}
else:
    # First, check current state
    invalid_objects, objects_with_errors, problems = _scan(doc)

    was_valid = len(invalid_objects) == 0 and len(objects_with_errors) == 0

    if was_valid:
        _result_ = {{
            "was_valid": True,
            "undone": False,
            "invalid_objects": [],
            "objects_with_errors": [],
            "problems": [],
            "message": "Document is valid, no undo needed",
            "validation_after": None
        }}
    else:
        # Perform undo
        try:
            doc.undo()
            doc.recompute()
            undone = True

            # Re-validate after undo
            invalid_after, errors_after, problems_after = _scan(doc)

            valid_after = len(invalid_after) == 0 and len(errors_after) == 0

            if valid_after:
                message = f"Undid last operation. Document is now valid."
            else:
                message = f"Undid last operation, but document still has issues."

            validation_after = {{
                "valid": valid_after,
                "invalid_objects": invalid_after,
                "objects_with_errors": errors_after,
                "problems": problems_after
            }}

        except Exception as e:
            undone = False
            message = f"Failed to undo: {{str(e)}}"
            validation_after = None

        _result_ = {{
            "was_valid": False,
            "undone": undone,
            "invalid_objects": invalid_objects,
            "objects_with_errors": objects_with_errors,
            "problems": problems,
            "message": message,
            "validation_after": validation_after
        }}
"""
        )
        result = await bridge.execute_python(code)
        if result.success and result.result:
            return result.result
        return {
            "was_valid": False,
            "undone": False,
            "invalid_objects": [],
            "objects_with_errors": [],
            "message": result.error_traceback or "Operation failed",
            "validation_after": None,
        }

    @unsafe_tool(mcp, allow_unsafe_code)
    async def safe_execute(
        code: str,
        doc_name: str | None = None,
        validate_after: bool = True,
        auto_undo_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Execute Python code with automatic validation and rollback on failure.

        This tool provides transactional semantics for FreeCAD operations:
        1. Opens an undo transaction
        2. Executes the provided code
        3. Validates all objects
        4. Automatically rolls back if validation fails (optional)

        Use this for complex operations where you want automatic error recovery.
        Local approval requires a GUI bridge and a socket connection. XML-RPC,
        embedded, headless, and older bridges cannot provide it. A failed review
        is reported without execution; after a lost commit response, execution
        may already be running and is not retried.

        Args:
            code: Python code to execute. Use _result_ = value to return data.
            doc_name: Target document. Uses active document if None.
            validate_after: Whether to validate objects after execution.
            auto_undo_on_failure: Whether to automatically undo if validation fails.

        Returns:
            Dictionary containing:
                - success: Whether the operation succeeded (execution + validation)
                - result: The value assigned to _result_ in the code (if any)
                - rolled_back: Whether rollback was performed, or None if unknown
                - execution_success: Execution outcome, or None if unknown
                - execution_error: Any execution error message
                - validation: Validation results (if validate_after is True),
                  with error_messages mapping each failed object to
                  FreeCAD's own error text
                - message: Human-readable summary

        Example:
            Execute code with automatic rollback on failure::

                result = await safe_execute('''
                box = doc.addObject("Part::Box", "MyBox")
                box.Length = 100
                _result_ = {"created": box.Name}
                ''')
                if result["success"]:
                    print(f"Created: {result['result']['created']}")
        """
        bridge = await get_bridge()

        # Escape the user code for embedding in the wrapper
        escaped_code = code.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')

        wrapper_code = f"""
import FreeCAD

doc_name = {doc_name!r}
validate_after = {validate_after!r}
auto_undo_on_failure = {auto_undo_on_failure!r}

# Get document
if doc_name:
    doc = FreeCAD.getDocument(doc_name)
else:
    doc = FreeCAD.ActiveDocument

if doc is None:
    _result_ = {{
        "success": False,
        "result": None,
        "rolled_back": False,
        "execution_success": False,
        "execution_error": "No active document found",
        "validation": None,
        "message": "No active document found"
    }}
else:
    # Start transaction
    doc.openTransaction("SafeExecute")

    execution_success = False
    execution_error = None
    user_result = None

    # Execute user code
    try:
        user_code = \"\"\"{escaped_code}\"\"\"
        exec_globals = {{"FreeCAD": FreeCAD, "App": FreeCAD, "doc": doc}}

        # Try to import common modules
        try:
            import Part
            exec_globals["Part"] = Part
        except ImportError:
            pass
        try:
            import PartDesign
            exec_globals["PartDesign"] = PartDesign
        except ImportError:
            pass
        try:
            import Sketcher
            exec_globals["Sketcher"] = Sketcher
        except ImportError:
            pass

        exec(user_code, exec_globals)

        if "_result_" in exec_globals:
            user_result = exec_globals["_result_"]

        execution_success = True
        doc.recompute()

    except Exception as e:
        execution_error = str(e)
        execution_success = False

    # Validate if requested
    validation = None
    validation_passed = True

    if validate_after and execution_success:
        invalid_objects = []
        objects_with_errors = []

        error_messages = {{}}

        for obj in doc.Objects:
            state = list(obj.State) if hasattr(obj, 'State') else []

            if "Invalid" in state or "Error" in state:
                objects_with_errors.append(obj.Name)
                try:
                    error_messages[obj.Name] = str(obj.getStatusString())[:1000]
                except Exception:
                    pass

            # A null shape (e.g. a sketch with only external geometry) has
            # nothing to check and is not a failure.
            shape = getattr(obj, 'Shape', None)
            if shape is not None and not (hasattr(shape, 'isNull') and shape.isNull()):
                try:
                    if not shape.isValid():
                        invalid_objects.append(obj.Name)
                except Exception:
                    invalid_objects.append(obj.Name)

        validation_passed = len(invalid_objects) == 0 and len(objects_with_errors) == 0
        validation = {{
            "valid": validation_passed,
            "invalid_objects": invalid_objects,
            "objects_with_errors": objects_with_errors,
            "error_messages": error_messages
        }}

    # Determine if we need to rollback
    rolled_back = False
    if not execution_success or (validate_after and not validation_passed and auto_undo_on_failure):
        doc.abortTransaction()
        doc.recompute()
        rolled_back = True
    else:
        doc.commitTransaction()

    # Build message
    if execution_success and validation_passed:
        message = "Operation completed successfully"
    elif not execution_success:
        message = f"Execution failed: {{execution_error}}"
        if rolled_back:
            message += " (rolled back)"
    else:
        message = f"Validation failed: {{len(validation.get('invalid_objects', []))}} invalid objects"
        if rolled_back:
            message += " (rolled back)"

    overall_success = execution_success and (not validate_after or validation_passed)

    _result_ = {{
        "success": overall_success,
        "result": user_result,
        "rolled_back": rolled_back,
        "execution_success": execution_success,
        "execution_error": execution_error,
        "validation": validation,
        "message": message
    }}
"""
        result = await bridge.execute_reviewed_python(wrapper_code)
        if result.success and result.result:
            return result.result
        # After dispatch, even an exception or an unserializable result can
        # follow document or external side effects. Only a denied/unavailable
        # review establishes that the operation did not start.
        unknown = result.error_type not in {"ReviewUnavailable", "ReviewDenied"}
        return {
            "success": False,
            "result": None,
            "rolled_back": None if unknown else False,
            "execution_success": None if unknown else False,
            "error_type": result.error_type,
            "execution_error": result.error_traceback
            or result.stderr
            or "Safe execute failed",
            "validation": None,
            "message": result.error_traceback or result.stderr or "Safe execute failed",
        }

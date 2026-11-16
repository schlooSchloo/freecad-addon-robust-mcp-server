---
name: freecad-mcp-modelling
description: Inspect, measure and change FreeCAD parts through the FreeCAD MCP tools while keeping the model tree tidy and protected geometry intact. Use for live CAD modelling, measurements and topology edits in FreeCAD.
---

# FreeCAD MCP modelling

Make the requested change in the intended document, keep the model tree
tidy, and check the result.

## Know the working state

- Find the document and objects from the live model: internal names can
  differ from labels and file names. Pass `doc_name` to every tool.
- Note placements, links, visibility and unsaved changes before editing.
- Establish the goal, which parts may change, which interfaces are protected
  (ball joints, mating faces), and how the user will judge the result. Look
  at the model before asking.

## Read before you change

- Measure with the read-only tools. They work on copies and never add
  objects:
  - `get_shape_geometry`: vertices, edges (including B-spline samples),
    faces, sphere centres and radii, optionally in a sketch's or body's
    frame (`frame`) and limited to named `elements`.
  - `get_section_outline`: cross-sections through a plane, with areas.
  - `find_sphere_centres` for ball joints; `inspect_object` for properties.
- Don't infer a centre or size from a bounding box.
- Face and edge numbers change when the shape changes: find them again by
  geometry after each edit.

## Change

- Prefer live (parametric) features; results say `parametric: true/false`.
  State when something is a static copy that won't follow its source.
- `edit_object` sets links, expressions (`"=Spreadsheet.Width"`) and
  placements, and refuses unknown property names. Read back what it stored.
- Sketches: `add_sketch_polyline` for outlines; `move_sketch_point` and
  `set_sketch_constraint_value` to adjust instead of deleting and redrawing.
  Check the returned sketch state: degrees of freedom, conflicting or
  redundant constraints, closed wires.
- Geometry from another body: `create_subshape_binder` or
  `project_into_sketch`, not direct links. Datums on edges and faces:
  `create_attached_datum`.
- Spreadsheet text in expressions uses `<<` and `>>`, not quotes.

## Check

- Check both the tool error and the result fields (`success`, `valid`,
  warnings). A call can succeed while the operation fails.
- Feature tools report `volume_before`, `volume_after` and a warning when a
  feature added or removed nothing: read them.
- `validate_object` and `recompute` give FreeCAD's own error text. A sketch
  of only reference geometry is a reference, not a failure.
- Use `get_screenshot` to judge appearance alongside the numbers;
  `view_angle="Current"` keeps the user's view.

## Keep the tree and folders tidy

- Use at most one scratch document per session, with a clear name. Don't
  save it unless asked, and then only in the project folder the user named.
  Close it at the end.
- Keep a list of objects you created. Remove only those, and never delete
  the user's references to make a check pass.
- Prefer read-only checks (`check_overlap`, `get_shape_geometry`) to
  temporary booleans in the user's document.

## Approvals and limits

- Python, macros and file operations may need the user's approval in
  FreeCAD. Don't retry a denied request or look for another route to do the
  same thing.
- Use the MCP tools only: no console, macros or UI automation as a
  workaround. If a tool you expect is missing, run
  `get_tool_registration_status`, then report the gap.

## Deliver

- When saving a revision, check its links afterwards with
  `get_document_dependencies`, especially after saving to a new folder.
- Report what changed, what you checked and how, what is static versus live,
  and what remains unverified. Don't claim print results the user hasn't
  reported.

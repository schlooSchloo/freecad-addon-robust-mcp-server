# Custom changes (schlooSchloo fork)

This file tracks how the `custom` branch differs from upstream
([spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server)),
plus the backlog of fixes and features we have planned but not started.

Upstream's `RELEASE_NOTES.md` and `CHANGELOG.md` are left alone so they merge
cleanly. Our notes go here instead. Issues are not enabled on the fork, so
this file is also the bug and feature tracker.

## Branch workflow

- `main` is an untouched mirror of upstream `main`.
- `custom` is our version. The workbench is installed from it.
- New work goes on short-lived feature branches, merged into `custom` by PR.
- Upstream changes come into `main` first, then get merged from `main` into `custom`.
- Anything offered upstream is rebuilt on its own branch from `main`, one fix
  per PR, with upstream's tests and release-notes conventions. Nothing from
  `custom` (this file, `tools/custom/`) goes upstream. Once a PR is accepted,
  the next merge from `main` brings in upstream's version and we drop ours.

## Status key

| Status    | Meaning                                                                     |
| --------- | --------------------------------------------------------------------------- |
| Done      | Merged into `custom`                                                        |
| Proposed  | Agreed in principle, design still being settled                             |
| Backlog   | Known need, not started                                                     |
| Upstream? | Good candidate to offer upstream as a PR, which would remove the difference |

---

## 1. Merged into `custom`

| Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Files                                                                                                                                     | Status          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| **Bind sketch dimensions to spreadsheet cells.** `spreadsheet_bind_property` accepts `Constraints[N]` and `Constraints.<Name>` paths, and `add_sketch_constraint` takes an optional `name` so a dimension can be bound by name. Named bindings survive constraints being added or deleted.                                                                                                                                                                                                                                      | `tools/partdesign.py`, `tools/spreadsheet.py`                                                                                             | Done, Upstream? |
| **Lazy connect (`FREECAD_LAZY_CONNECT`).** The server starts without FreeCAD running and connects on the first tool call instead of exiting.                                                                                                                                                                                                                                                                                                                                                                                    | `server.py`, `config.py`, `resources/freecad.py`                                                                                          | Done, Upstream? |
| **Show expected tool errors to the client.** `ValueError`, `ConnectionError`, `TimeoutError` and file errors reach the client with their real message instead of the MCP SDK's generic "Error executing tool". Messages are capped at 4,000 characters.                                                                                                                                                                                                                                                                         | `tools/_registration.py`, `tools/__init__.py`                                                                                             | Done, Upstream? |
| **Safe tool module registration.** Each tool module is imported and registered on its own. A module that fails to import or register is skipped and logged, and adds none of its tools, while the rest of the server starts. Duplicate tool names are logged as errors instead of being dropped silently. See 2.2.                                                                                                                                                                                                              | `tools/__init__.py`, `tools/custom/loader.py`                                                                                             | Done            |
| **`tools/custom/` package and `get_tool_registration_status`.** Home for our own tools, registered after upstream's. The first tool reports which modules loaded, which were skipped and why, and any name clashes. See 2.1.                                                                                                                                                                                                                                                                                                    | `tools/custom/__init__.py`, `tools/custom/status.py`                                                                                      | Done            |
| **Tool-name collision test and generated-code compile test.** See 2.3 and 2.4.                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `tests/unit/custom/`                                                                                                                      | Done            |
| **Upstream 0.8.0 merged** (PR #5). Socket is now the default connection; Python, macros and file operations need approving in FreeCAD.                                                                                                                                                                                                                                                                                                                                                                                          | Merge commit                                                                                                                              | Done            |
| **P1/P2 fixes from usage feedback** (PR #6): `create_sketch` plane lookup; `edit_object` typed values, expressions and cross-document links; `delete_object` keeps the body's Tip; truthful `recompute`, validation and inspection; `scale_object` as `Part::Scale`; `extrude_shape` solids; outlines from `slice_shape`/`section_shape`; `parametric` flags; screenshots as images with a current-view option and 3D-view targeting; camera and zoom without `pivy`; spreadsheet alias readers and `set_cell`. Details in 3.4. | `tools/`, `bridge/` (new: `object_editing.py`, `object_health.py`, `view3d.py`), `tools/custom/shape_snippets.py`, `tools/view_camera.py` | Done            |

### Known merge friction

A trial merge of upstream `main` (Nov 2026, upstream at workbench v0.7.2)
into `custom` stopped on two files:

- `tools/_registration.py`: upstream added tool risk annotations (`file_tool`,
  annotations on `unsafe_tool`) in the same area as our error-reporting
  wrapper. Small, easy to resolve.
- `RELEASE_NOTES.md`: both sides added notes at the top. We stop editing that
  file from now on (see this file).

`tools/partdesign.py` and `tools/spreadsheet.py` merged cleanly even though
both sides had changed them.

Merging upstream 0.8.0 (Nov 2026, PR #5) conflicted in the same two places
plus `docs/getting-started/configuration.md` (the environment variable
table, where we add `FREECAD_LAZY_CONNECT`). `src/freecad_mcp/RELEASE_NOTES.md`
was resolved by taking upstream's version, which drops our old notes from it;
they are kept in section 1. Upstream's `tools/__init__.py` was unchanged, so
our module loader needed no changes.

---

## 2. Structure and safety net

### 2.1 `tools/custom/` package for our own tools (Done)

- New tools live in `src/freecad_mcp/tools/custom/`, split into modules by
  topic (e.g. `binders.py`, `datums.py`, `clash.py`), not one large file.
- Each module is listed in `CUSTOM_TOOL_MODULES` in
  `tools/custom/__init__.py`. `tools/__init__.py` registers those after
  upstream's `TOOL_MODULES`, so upstream keeps its name in any clash. That
  one line is our only hook into upstream's registration code.
- Follow the CAM pattern rather than code-in-strings: thin MCP tool functions
  that pass JSON arguments to real Python functions running inside FreeCAD.
  That code can then be linted, type-checked and tested like any other module.
- Any names we inject into FreeCAD's execution namespace use a `_p3d_` prefix
  so they can't clash with upstream helpers such as
  `_robust_mcp_cam_dispatch`.
- Tests go in `tests/unit/custom/` and `tests/integration/custom/`.
- The FreeCAD-side code ships inside the workbench addon, like
  `cam_runtime.py`, in the package
  `freecad/RobustMCPBridge/freecad_mcp_bridge/custom_runtime/`. Each module
  there that defines an `OPERATIONS` dictionary is picked up automatically,
  so tool groups never edit a shared list. A module that fails to import is
  skipped and named in the error for its operations.
- The bridge puts `_p3d_dispatch` into every execution environment: one
  method and one line in `freecad_mcp_bridge/server.py`, our only edit to the
  bridge. Server-side tools call it through `tools/custom/runtime.py`
  (`run_operation`), which sends the arguments as JSON, so model text never
  becomes code.
- The dispatcher is versioned (`P3D_API_VERSION`, like CAM's
  `CAM_API_VERSION`), so a server newer than the installed workbench gives a
  clear "update the workbench" error instead of a confusing failure.
  Changing this code means reinstalling the workbench and restarting FreeCAD.
- Embedded mode (Linux only) doesn't load the custom runtime; custom tools
  there report that they need the workbench.
- Custom tools are documented in their docstrings and in this file. We don't
  add them to upstream's tool lists (`freecad://capabilities`,
  `prompts/freecad.py`, `docs/guide/tools.md`), to avoid merge conflicts. The
  client still sees them in the normal tool list.

### 2.2 Safe module registration (Done)

Before this, a Python error in any tool module (syntax error, bad import, or
an exception during registration) stopped the whole server from starting.

- `tools/custom/loader.py` imports and registers each module on its own. Each
  module's tools are collected first and only added once the whole module has
  registered, so a module that fails part-way adds nothing.
- Failures are logged with a full traceback, and recorded in a report that the
  `get_tool_registration_status` tool returns.
- A single tool the server rejects (e.g. a bad signature) is skipped without
  losing the rest of its module.
- Tool modules must only call `mcp.tool()` while registering. They all do
  today; anything else would need adding to the loader's staging stand-in.

### 2.3 Tool-name collision check (Done)

The MCP SDK (v2.3.0) keeps the first tool registered under a name and quietly
drops any later one, logging only a warning. If upstream adds a tool with the
same name as one of ours, upstream's version silently wins.

- `tests/unit/custom/test_tool_name_collisions.py` fails on any name shared by
  upstream and custom tools, so the merge fails loudly in tests and we decide
  whether to drop ours, rename it, or keep it.
- At startup the loader also logs a duplicate as an error and records it in
  the registration report.
- Similar tools under different names (e.g. upstream `create_link` alongside
  our `make_link`) can't be caught automatically. Check for them when
  reviewing each upstream merge.

### 2.4 "Every tool generates valid Python" test (Done)

- `tests/unit/custom/test_generated_code_compiles.py` calls every registered
  tool against a mock bridge and compiles each piece of code it sends to
  FreeCAD. Each tool runs twice: required arguments only, and every argument
  filled with booleans flipped, to reach more branches.
- Sample arguments come from each tool's signature. `ARGUMENT_OVERRIDES` holds
  values for tools that reject the placeholders (three today), and
  `NO_BRIDGE_TOOLS` lists tools that never contact FreeCAD.
- A new upstream tool is checked automatically. The test only needs editing
  if the tool rejects the sample values.
- Results at the time of writing: 137 tools send code, all of it compiles in
  both passes. 36 tools use the bridge's higher-level methods instead (see
  section 4).
- Compiling only catches syntax errors. Undefined names at run time are not
  caught (see section 5).

---

## 3. Backlog

### 3.1 How items are prioritised

| Priority | Meaning                                                                                                                                               |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| P1       | Gives a wrong or silent result (the AI thinks something worked when it didn't), or blocks common work unless unsafe Python is switched on. Fix first. |
| P2       | Saves many calls or a lot of time, or is needed for current modelling work.                                                                           |
| P3       | Minor, cosmetic, or documentation only.                                                                                                               |

- Bugs in upstream's tools are fixed in place, and are candidates to offer
  upstream. New capabilities go in `tools/custom/`.
- Sources:
  - our own sessions (C13 chest clash dry run, EV3 chest base redesign);
  - usage feedback from a C13 chest modelling session (12 Nov 2026);
  - consolidated findings from the artist-manikin pelvis sessions in another
    AI client (14 Nov 2026), with FreeCAD 1.1.4 on Windows. Their IDs
    (MCP-01 to MCP-21) are given in brackets so the two lists can be
    matched up.
- Priorities are ours. Where the pelvis findings ranked an item differently,
  the row says why.

### 3.2 Decision: read-only geometry tools instead of Python

The first feedback's main blocker was having no way to run Python, mostly to
read geometry. The workaround was putting formulas in a temporary
spreadsheet, which worked but was slow. The pelvis sessions reached the same
conclusion independently: they finished every task with the exposed tools
and recommended against adding console access.

The unsafe tools (`execute_python` and friends) stay off by default. Instead,
each gap gets a specific read-only tool:

- **A "read-only Python" mode can't be enforced.** Any script can call
  something that changes the document, so it would only be read-only by
  convention.
- **Specific tools keep the tree tidy.** Read-only tools work on in-memory
  copies of shapes and never add objects to the document.
- **They can be approved automatically.** Read-only tools are marked as such,
  so a client can allow them without asking every time. Python always needs
  checking. Since upstream 0.8.0, Python also needs approving in FreeCAD.
- **Results come back structured**, not as text to parse.

### 3.3 Next up

1. Geometry query tool (`get_shape_geometry`, 3.5).
2. Overlap and distance check (`check_overlap`, 3.5), then the clash sweep
   that builds on it.
3. Sketch editing and closed polylines (3.5).
4. Native mesh creation and mesh quality checks (3.5).

### 3.4 Bugs in existing tools

| Priority | Bug                                                                                                                         | Cause and fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Status                                                 |
| -------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| P1       | **`create_sketch` quietly makes an unattached sketch.**                                                                     | It looks the plane up by exact name (`XZ_Plane`) in the body's origin. Bodies whose origin planes are numbered (`XZ_Plane001`) get nothing, and any other plane name (a datum plane, say) is ignored without an error. Seen in both chest sessions. Fix: find the body's origin plane by its role instead of its name, accept any plane or datum plane by name, and raise an error if nothing matches.                                                                                                                                                                                                                                                                                                                                                                                                    | Done, Upstream?                                        |
| P1       | **`edit_object` can't set link properties, expressions or cross-document links, and skips unknown names.** (MCP-05, MCP-21) | Values are set exactly as given, so link properties (`BaseFeature`, `Tip`, attachment, `UpToFace`, `Profile`) receive a string and FreeCAD rejects them. `App::Link.LinkedObject` (a cross-document link) fails too, expressions can't be set, and property names the object doesn't have are skipped silently. A support given an empty sub-element list `[]` is dropped, while `[""]` attaches to the whole object. The CAM tool `cam_set_properties` already converts most of these, which is why the pelvis sessions used it for ordinary modelling objects. Fix: convert values by property type (links, link lists, sub-element links, cross-document links), set `=expressions`, treat `[]` as the whole object, refuse unknown names without making a partial edit, and return the stored values. | Done, Upstream?                                        |
| P1       | **`recompute` reports success on a broken feature, and validation gives no reasons.** (MCP-03, MCP-14)                      | The `recompute` tool always returns success. `validate_object` checks FreeCAD's error flags and shape validity, but never reads FreeCAD's own message for the object (`getStatusString()`). A broken pocket showed "Invalid" with no reason. A link whose target file was missing showed `shape_valid=true` with no explanation, and a sketch of only projected reference geometry was reported as invalid. Fix: list objects in error with FreeCAD's message in `recompute`, `recompute_document` and validation; name unresolved link targets; never claim a shape is valid when there is no shape; class reference-only sketches as references, not failures.                                                                                                                                          | Done, Upstream?                                        |
| P1       | **`scale_object` produces invalid geometry.** (MCP-01)                                                                      | Scaling a valid solid by 1.1575 made a static `Part::Feature` with no link to its source, whose shape was invalid and whose volume didn't match the source × 1.1575³. The response didn't flag it. FreeCAD's own `Part::Scale` feature works, stays linked to its source and keeps exact factors. Fix: build the result as a `Part::Scale`, and report validity, volume and solid count.                                                                                                                                                                                                                                                                                                                                                                                                                  | Done, Upstream?                                        |
| P1       | **Spreadsheet alias readers say an alias is missing when it exists.** (MCP-02)                                              | `spreadsheet_get_cell` and `spreadsheet_get_aliases` look aliases up indirectly (through the sheet's property list) and hide lookup failures, so a real alias read as none. Binding to it still worked. Risk: the AI "recreates" a parameter that already exists. Fix: use the spreadsheet's own alias functions, and report a failed lookup as an error, not as "no alias".                                                                                                                                                                                                                                                                                                                                                                                                                              | Done, Upstream?                                        |
| P1       | **Deleting a body's last feature leaves the body with no shape.**                                                           | `delete_object` removes the object and nothing else. If it was the body's Tip, the Tip is left empty. Fix: move the Tip back to the previous feature, as the FreeCAD GUI does, and say so in the result. Deleting an object that doesn't exist also returns a raw traceback (P3): return a clean "not found" error.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Done, Upstream?                                        |
| P1       | **Screenshots come back as text, not an image.** (MCP-11)                                                                   | `get_screenshot` returns the image as base64 text inside its result, so clients show small ones as text, and large ones only work because the client saves long results to a file. Fix: return MCP image content, which clients display. The pelvis findings ranked this P3 because large captures worked; it's P1 here because the AI can't see what it built.                                                                                                                                                                                                                                                                                                                                                                                                                                           | Done, Upstream?                                        |
| P2       | **`get_screenshot` always resets the view and can't target the right window.** (MCP-10)                                     | Every capture applies an angle (default isometric) and zooms to fit, so there's no way to capture the current view. With a spreadsheet as the document's active window, capture failed with `success=false` but wasn't marked as an error, although a 3D view existed. Fix: a "current view" option; capture the document's 3D view even when another window is active; mark real failures as errors.                                                                                                                                                                                                                                                                                                                                                                                                     | Done, Upstream?                                        |
| P2       | **`set_camera_position` and the zoom tools fail.**                                                                          | They use the 3D scene library (`pivy`), which a standard FreeCAD install doesn't make available here ("No SWIG wrapped library loaded"). The zoom tools also ignore `doc_name`. Fix: use the view's own camera and zoom functions, and target the given document.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Done, Upstream?                                        |
| P2       | **`inspect_object` misses key measurements and reports nonsense for datum planes.** (MCP-08, MCP-13)                        | It reports volume and area only: no bounding box, centre of mass or solid count, and no way to ask for particular properties or know when output was cut short. An infinite datum plane reported an area around 8×10¹⁰⁰ mm² and a volume around 5×10¹⁶⁷ mm³. Fix: add bounding box, centre of mass, topology counts and placement; report unbounded quantities as not applicable, with the plane's position and normal; flag truncated output.                                                                                                                                                                                                                                                                                                                                                            | Done, Upstream?                                        |
| P2       | **`extrude_shape` of a closed wire makes an open shell, not a solid.** (MCP-07)                                             | A closed sketch wire extruded into a valid but open shell with positive "volume", which then failed as a cutter. Fix: a solid option (FreeCAD's `Part::Extrusion` with Solid set), and report shape type, closedness and solid count.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Done, Upstream?                                        |
| P2       | **`slice_shape` and `section_shape` only report counts.**                                                                   | They add a new object to the tree on every call and return how many pieces it has, not the outline. Fix in place: return the outline geometry (edge types, end points, lengths, circle centres and radii), capped and flagged when cut short. The read-only `get_section_outline` (3.5) would avoid the extra tree object.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Done                                                   |
| P2       | **Results don't say whether they're live features or static copies.** (MCP-06)                                              | Copy, loft, extrude, compound and some fuse helpers return static `Part::Feature` shapes that don't update when their source changes, while native booleans stay linked. Tool names don't tell you which. Fix: add `parametric: true/false` to each helper's result and say it in the description.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Done                                                   |
| P2       | **`spreadsheet_set_cell` reports failure when it succeeded.**                                                               | Setting a value with units, e.g. `=40mm`, sets the cell correctly but returns a failure. A false failure could make the AI retry and apply a change twice.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Done, Upstream?                                        |
| P2       | **`uv.lock` points at a private package mirror.**                                                                           | Every package in the lockfile resolves from `pkgs.safetycli.com` (upstream's Safety mirror), so `uv sync` may fail for anyone without access to it. Waiting to check whether the mirror works locally before deciding.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | On hold                                                |
| P2       | **Sketch degrees of freedom read as zero while unconstrained.** (MCP-12)                                                    | Eight unconstrained lines reported `dof=0`. Upstream 0.8.0 changed how degrees of freedom are reported (#220), which may fix this.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Fixed upstream (#220), checked: 8 free lines report 32 |
| P3       | **The undo history is hard to read.**                                                                                       | Edits made through the bridge get generic undo names ("Create Object", "Edit Object", "Delete Object"), and spreadsheet edits share the document's single undo history. Fix: name each undo step after what changed (e.g. "Edit Pocket.TaperAngle").                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Done                                                   |
| P3       | **Spreadsheet expressions with string literals break.**                                                                     | FreeCAD expressions quote text with `<<` and `>>`, not quote marks, so `";"` broke an expression. Fix: say so in the spreadsheet tool descriptions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Done, Upstream?                                        |
| P3       | **Rotation convention isn't stated.**                                                                                       | `set_placement` and `rotate_object` take `[yaw, pitch, roll]` in degrees without saying which axes or in what order. The pelvis sessions found yaw/pitch/roll are about Z/Y/X, and that `rotate_object` is incremental while `set_placement` is absolute. Fix: state both in the descriptions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Done, Upstream?                                        |
| P3       | **Generated code compares strings with `is`.**                                                                              | 102 tools build lines like `'MyDoc' is None` when given a document name (still true after upstream's 0.8.0 hardening). Python warns about this but gives the right answer, so it is untidy, not broken. Fix by deciding in the tool which document expression to send.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Backlog, Upstream?                                     |
| P3       | **A closed edge can't be used where a wire is needed.** (MCP-15)                                                            | `create_ellipse` returns a closed edge, and `part_loft` rejects it with "Object has no wires". `make_wire` only takes points, not existing edges. A closed ellipse in a sketch works. Fix: let `make_wire` join existing edges, or let loft accept a closed edge, and report open/closed status.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Backlog                                                |
| P3       | **STEP round trips lose names and grouping.** (MCP-18)                                                                      | Exporting four named objects and importing them again gave seven generically named solids with slightly different volumes. Probably FreeCAD's exchange behaviour rather than ours. Fix: optional mapping report from exported to imported objects.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Backlog                                                |
| P3       | **Selection tools can't select faces or edges.** (MCP-19)                                                                   | `set_selection` takes object names only. Fix: accept sub-elements (`Face3`) and return what was selected; warn that face numbers change after edits.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Backlog                                                |
| P3       | **Undocumented tool behaviour.**                                                                                            | `add_external_geometry` with an empty element string imports all eligible elements, and `import_stl` without `doc_name` creates a new document. Both caught the pelvis sessions out. Fix: say so in the descriptions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Backlog                                                |
| P3       | **`get_sketch_info` doesn't say which constraints conflict.**                                                               | It reports degrees of freedom and an Invalid state, but not which constraints are redundant or conflicting, although FreeCAD lists them (`RedundantConstraints`, `ConflictingConstraints`, `PartiallyRedundantConstraints`, `MalformedConstraints`). Fix: add those lists to the result.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Backlog, Upstream?                                     |
| P3       | **`inspect_object` errors arrive as a full traceback.**                                                                     | An unknown name in `property_names`, or a missing object, comes back as FreeCAD's whole traceback rather than one line. Fix: return the final error message only, as `edit_object` and `delete_object` now do.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Backlog, Upstream?                                     |
| P3       | **Literal text starting with `=` can't be set with `edit_object`.**                                                         | Since the fix, a string starting with `=` is always treated as an expression, so a Label of `=x` can't be stored. Documented in the tool. Fix if it's ever needed: an explicit way to mark a value as literal text.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Backlog                                                |

### 3.5 New tools and capabilities (in `tools/custom/`)

All read-only unless stated: they work on in-memory shape copies and never
add objects to the document.

| Priority | Tool                                                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Status  |
| -------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------- |
| P1       | **`get_shape_geometry`** (working name) (MCP-08)      | Returns an object's vertices, edges (type, curve details such as radius and centre, end points, length, sample points at a stated tolerance, including B-splines, which generic inspection only shows as `<BSplineCurve object>`) and faces (type, area, centre, normal, bounding box, sphere centre and radius). Can be limited to listed sub-elements (`Edge3`, `Face2`), reported in another object's local frame, and paged, with truncation always flagged. Replaces the spreadsheet workaround. `cam_analyze_model` already covers face types, normals and areas, which the pelvis sessions relied on. | Backlog |
| P2       | **`check_overlap`** (MCP-09)                          | Checks two objects (or sub-elements) for overlap: shared volume and where the contact is, or the minimum distance and closest points when they don't touch. Distinguishes empty, touching, overlapping and failed results, because an empty intersection doesn't prove a clearance gap. Today this needs temporary Boolean objects: about 9 calls per angle in the EV3 session. The clash sweep builds on it.                                                                                                                                                                                                | Backlog |
| P2       | **Clash sweep** (MCP-20)                              | Rotate one part around a pivot and report where it starts to hit another. Design in 3.6.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Backlog |
| P2       | **Sketch editing tools** (not read-only)              | Move or edit existing sketch geometry with the solver, keeping constraints (e.g. move a line's end point). Plus a closed polyline helper that adds connected lines with coincident corners in one call, optionally making axis-aligned lines horizontal or vertical. Saves the delete-and-redraw cycles from the feedback.                                                                                                                                                                                                                                                                                   | Backlog |
| P2       | **Native mesh from shape** (MCP-16) (not read-only)   | Make a `Mesh::Feature` straight from a solid, with linear and angular deflection settings, and return mesh counts and bounds, stated as a static snapshot. Today this needs exporting an STL and importing it again. Since upstream 0.8.0, that round trip also needs file approval in FreeCAD.                                                                                                                                                                                                                                                                                                              | Backlog |
| P2       | **Mesh quality check** (MCP-17)                       | Connected components, open and non-manifold edges, degenerate facets, winding, bounds and enclosed volume, with stated tolerances. Repair, if added, is a separate explicit tool. Today this needs analysing the exported STL outside FreeCAD.                                                                                                                                                                                                                                                                                                                                                               | Backlog |
| P2       | **External dependency report and packaging** (MCP-04) | Saving a document that links to another file into a new folder kept a relative path to the linked file, so it reopened broken unless the linked file was copied alongside. This is FreeCAD's save-as behaviour, not a tool bug. Add a dependency report to `save_document`, and optionally a check after saving that every link still resolves (or copy the dependencies alongside).                                                                                                                                                                                                                         | Backlog |
| P2       | **`get_section_outline`**                             | Cross-section of a shape through a plane (or several parallel offsets), returning the outline in the same format as `get_shape_geometry`, without adding objects to the tree.                                                                                                                                                                                                                                                                                                                                                                                                                                | Backlog |
| P2       | **ShapeBinder and SubShapeBinder** (not read-only)    | Needed for master-sketch setups and for using another body's geometry.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Backlog |
| P2       | **Projecting edges and faces from another body**      | `add_external_geometry` works within a body, but a sketch linked straight into a different body hits FreeCAD's "out of allowed scope" error. The usual fix is a SubShapeBinder first, then project from that, so this depends on the item above. Needed for aligning the C13 chest and pelvis cut-outs to the new waist.                                                                                                                                                                                                                                                                                     | Backlog |
| P2       | **Datum planes attached to edges and vertices**       | `create_datum_plane` can only offset from XY, XZ or YZ. Needs attachment modes such as normal-to-edge and attachment offsets with rotation, e.g. angled planes along a curved path. (A standalone `Part::DatumPlane` can already be made with `create_object`.)                                                                                                                                                                                                                                                                                                                                              | Backlog |
| P2       | **Volume before and after for more features**         | `pocket_sketch` reporting volume before and after was the most useful single feature in the first feedback, catching a cut into the ball support. Do the same for pad, groove, subtractive loft and pipe, fillet and chamfer. Small change to upstream tools, so a good one to offer upstream.                                                                                                                                                                                                                                                                                                               | Backlog |
| P3       | **`save_screenshot`** (not read-only)                 | Save a capture to a file, for documentation or listings. Kept out of `get_screenshot` so that tool stays read-only: a file target would make every capture need approval in FreeCAD.                                                                                                                                                                                                                                                                                                                                                                                                                         | Backlog |
| P3       | **Fit the view to chosen objects**                    | Frame a set of objects (including ones inside `App::Part` containers) before a capture. `set_camera_position` with a look-at point and view height already covers framing a region.                                                                                                                                                                                                                                                                                                                                                                                                                          | Backlog |
| P3       | **Use less context, for smaller local models**        | All tool descriptions together are about 50–65k tokens per conversation (section 5), which may be too much for small local models, and they may struggle to choose among 170+ tools. Not tested yet. First step: run the server with a local model in Continue or similar and measure. Then pick from the ideas in section 5 (toolsets, loading tools on demand, shorter descriptions).                                                                                                                                                                                                                      | Backlog |
| P3       | **Sweep and pipe orientation modes**                  | `sweep_sketch` and `subtractive_pipe` take one profile and one path. Need the orientation mode (Frenet, Binormal, auxiliary spine) and extra cross-sections for variable-section sweeps.                                                                                                                                                                                                                                                                                                                                                                                                                     | Backlog |
| P3       | **Create `App::Link` objects**                        | No tool creates links. They currently have to be made by hand in FreeCAD (Make Link), then pointed at the right target (which the `edit_object` fix will allow).                                                                                                                                                                                                                                                                                                                                                                                                                                             | Backlog |
| P3       | **MultiTransform**                                    | Not covered.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Backlog |
| P3       | **PartDesign primitives and PartDesign helix**        | Additive and subtractive box, cylinder and so on. A Part-workbench helix exists, but not the PartDesign feature.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Backlog |

### 3.6 Clash sweep design

A tool that rotates one solid around a pivot and reports when it starts to
overlap another. Prototyped twice: with `execute_python` on in-memory copies
(one call per sweep), and in the EV3 session with `set_placement` plus
temporary Boolean objects (about 9 calls per angle). The pelvis sessions used
the second method and wrote up a careful procedure. Combined design:

- **Inputs:** fixed object(s); moving object(s), moved together as one rigid
  group; their neutral placements; pivot point; axis or axes with an explicit
  order for combined rotations; angle range and step; both directions.
- **Pose maths:** pose = translate(pivot) × rotate(angle) × translate(−pivot)
  × neutral placement. Always start each sample from the neutral placement,
  never by adding rotations, so errors don't build up.
- **Works on in-memory shape copies** (`Shape.copy()` then transform), so
  the document never changes and there's nothing to clean up.
- **Speed:** check bounding boxes first and run the full intersection only
  when they meet. Sweep coarse first (e.g. 5°), then refine the first
  clear-to-colliding interval (e.g. 1°).
- **Allowed contact:** an intentional bearing contact (e.g. the ball's grip,
  about 3 mm³ constant in the EV3 test) is reported separately, or excluded
  with an explicit, sized mask. A mask must not hide the real stop.
- **Thresholds:** an overlap threshold (e.g. 0.01 mm³) only filters numerical
  noise. It isn't a print clearance allowance. Report both separately.
- **Output per direction:** last clear angle and first colliding angle
  (a bracket, not an exact limit), overlap volume, contact region centre and
  bounding box, and which moving part made contact. Optionally, the minimum
  gap and closest points near the limit.
- **Honesty rules:** zero overlap isn't proof of a positive gap (touching
  counts as zero); stepped samples aren't a continuous sweep; independent
  axis limits don't define a safe region for combined poses.
- **Runtime:** a fine sweep on detailed parts can take minutes, so it needs a
  generous timeout or should report progress.
- **Helper wanted:** measure ball-joint centres or pivot points from a
  spherical face, so pivots don't have to be entered by hand or guessed from
  bounding boxes.
- **Shares code with `check_overlap`,** so build that first.

### 3.7 What worked well (keep)

So these don't get lost in later changes:

- `pocket_sketch` reporting volume before and after.
- `validate_object` and `undo`.
- Rotation as yaw, pitch and roll (once the convention is documented).
- Native `Part::Scale` created with `create_object` and configured with
  `cam_set_properties`, including exact factors like 1.1575.
- `cam_set_properties` / `cam_get_properties` as general property tools for
  modelling objects, including expressions on Placement and AttachmentOffset.
  Keep them working for that even though the `edit_object` fix covers most of
  it.
- `cam_analyze_model` for face types, plane normals and areas without
  creating a CAM job.
- Native `PartDesign::Boolean` with a working Suppressed property, and
  `recompute=false` while wiring up a Body's references in stages.
- `explode_compound` to count the solids inside a compound.

---

## 4. Backlog: testing

Integration tests that call the actual MCP tool functions against a real
FreeCAD, as of Nov 2026:

| Area                                                   | Covered |
| ------------------------------------------------------ | ------- |
| CAM                                                    | 23 / 23 |
| PartDesign                                             | 3 / 49  |
| Draft                                                  | 2 / 6   |
| Documents                                              | 1 / 7   |
| Execution                                              | 1 / 5   |
| Objects, View, Spreadsheet, Export, Macros, Validation | 0       |

The larger "workflow" integration tests send hand-written FreeCAD code
straight to the bridge, so they test the bridge and FreeCAD, not the tools.

- **Code built by the bridge's own methods (Backlog).** 36 tools call
  higher-level bridge methods (`create_object`, `get_objects`, ...), which
  build their FreeCAD code inside `bridge/xmlrpc.py` and `bridge/socket.py`.
  The generated-code compile test doesn't reach that code yet.
- **Smoke test per tool (Backlog).** Call each tool at least once against real
  FreeCAD, starting with sketch, constraints, spreadsheet binding, pad and
  pocket, and export.
- **Local test run on Windows (Backlog).** The `just` test recipes are bash
  scripts that look for `freecad`/`freecadcmd` on the PATH and use `lsof` to
  free ports, so they don't work as-is on Windows. Write down a manual
  Windows procedure, or add a PowerShell equivalent.
- **CI on `custom` (Deferred).** The test workflows only run for pushes and
  PRs to `main`/`master`, so work merged into `custom` isn't tested by CI.

---

## 5. Ideas: later

- **Toolsets.** Let groups of tools be switched on or off, e.g.
  `FREECAD_TOOLSETS=partdesign,spreadsheet,documents,view`. All 170 default
  tool definitions come to roughly 210,000 characters (about 50–65k tokens)
  that the model receives on every turn. PartDesign alone is about 60,000
  characters and CAM about 39,000. Trimming the list matters most for
  smaller local models, and helps any model choose the right tool.
- **Load toolsets on demand.** Start with a small core (documents, view,
  validation) plus `list_toolsets` and `enable_toolset(name)`.
  `enable_toolset` returns a short guide to that workbench with example
  workflows, registers its tools, and tells the client the tool list has
  changed (the MCP `tools/list_changed` notification, which SDK v2.3.0
  supports). The tools keep their real names and schemas, so per-tool
  approval settings in the client still work. Depends on the client
  refreshing its tool list when notified; check which clients do before
  relying on it.
- **Avoid a single "call any tool" dispatcher.** One tool that takes a tool
  name and arguments would be smallest, but it hides which tool runs from
  the client's approval settings (including the unsafe Python tools) and
  loses argument checking. Only consider it as an opt-in fallback for
  clients that can't refresh their tool list.
- **Check generated code for undefined names.** Extend the compile test with
  a lint pass (e.g. ruff's undefined-name rule) over the generated code,
  allowing the names FreeCAD provides (`FreeCAD`, `App`, `Part`, ...). That
  would catch typos that compile but fail when FreeCAD runs them.
- **Shorter tool descriptions.** Descriptions account for about half of that
  total. Moving long examples out of descriptions and into MCP prompts or
  resources would cut the per-turn cost.

---

## 6. Housekeeping

- [ ] Switch the Claude app's FreeCAD connection to socket mode and install
      workbench 0.8.0. Since upstream 0.8.0, file operations (open/save to a
      path, STEP/STL import and export) over XML-RPC are refused unless
      `FREECAD_ALLOW_UNREVIEWED_FILE_OPERATIONS=true` is set.
- [ ] Decide which "Upstream?" items to offer to spkane as PRs, and which to
      raise as issues only.
- [ ] Add each new custom change to section 1 when it merges.

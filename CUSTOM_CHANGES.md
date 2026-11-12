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

| Change                                                                                                                                                                                                                                                                                                             | Files                                                | Status          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- | --------------- |
| **Bind sketch dimensions to spreadsheet cells.** `spreadsheet_bind_property` accepts `Constraints[N]` and `Constraints.<Name>` paths, and `add_sketch_constraint` takes an optional `name` so a dimension can be bound by name. Named bindings survive constraints being added or deleted.                         | `tools/partdesign.py`, `tools/spreadsheet.py`        | Done, Upstream? |
| **Lazy connect (`FREECAD_LAZY_CONNECT`).** The server starts without FreeCAD running and connects on the first tool call instead of exiting.                                                                                                                                                                       | `server.py`, `config.py`, `resources/freecad.py`     | Done, Upstream? |
| **Show expected tool errors to the client.** `ValueError`, `ConnectionError`, `TimeoutError` and file errors reach the client with their real message instead of the MCP SDK's generic "Error executing tool". Messages are capped at 4,000 characters.                                                            | `tools/_registration.py`, `tools/__init__.py`        | Done, Upstream? |
| **Safe tool module registration.** Each tool module is imported and registered on its own. A module that fails to import or register is skipped and logged, and adds none of its tools, while the rest of the server starts. Duplicate tool names are logged as errors instead of being dropped silently. See 2.2. | `tools/__init__.py`, `tools/custom/loader.py`        | Done            |
| **`tools/custom/` package and `get_tool_registration_status`.** Home for our own tools, registered after upstream's. The first tool reports which modules loaded, which were skipped and why, and any name clashes. See 2.1.                                                                                       | `tools/custom/__init__.py`, `tools/custom/status.py` | Done            |
| **Tool-name collision test and generated-code compile test.** See 2.3 and 2.4.                                                                                                                                                                                                                                     | `tests/unit/custom/`                                 | Done            |

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

---

## 2. Structure and safety net

### 2.1 `tools/custom/` package for our own tools (Done, FreeCAD side still to come)

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
  `cam_runtime.py`, in its own module (e.g.
  `freecad/RobustMCPBridge/freecad_mcp_bridge/custom/`). Keep the hook into
  the bridge to a single line there too. Not created yet: the first custom
  tool that needs FreeCAD-side code will add it.
- Use a versioned dispatcher like CAM's (`CAM_API_VERSION`), so a server
  newer than the installed workbench gives a clear "update the workbench"
  error instead of a confusing failure. Changing this code means
  reinstalling the workbench and restarting FreeCAD.
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
- Sources: our own sessions so far, and usage feedback from a C13 chest
  modelling session (Nov 2026).

### 3.2 Decision: read-only geometry tools instead of Python

The feedback's main blocker was having no way to run Python, mostly to read
geometry. The workaround was putting formulas in a temporary spreadsheet,
which worked but was slow.

The unsafe tools (`execute_python` and friends) stay off by default. Instead,
each gap gets a specific read-only tool:

- **A "read-only Python" mode can't be enforced.** Any script can call
  something that changes the document, so it would only be read-only by
  convention.
- **Specific tools keep the tree tidy.** Read-only tools work on in-memory
  copies of shapes and never add objects to the document.
- **They can be approved automatically.** Read-only tools are marked as such,
  so a client can allow them without asking every time. Python always needs
  checking.
- **Results come back structured**, not as text to parse.

### 3.3 Next up

1. Geometry query tool (`get_shape_geometry`, 3.5).
2. `create_sketch` plane lookup (3.4).
3. Report FreeCAD's own error text from `recompute` and `validate_object` (3.4).
4. `edit_object` link properties and unknown names (3.4).
5. `delete_object` leaving a body with no Tip (3.4).
6. Screenshots returned as images, with an option to keep the current view (3.4).
7. Overlap check (`check_overlap`, 3.5), then the clash sweep that builds on it.
8. Sketch editing and closed polylines (3.5).

### 3.4 Bugs in existing tools

| Priority | Bug                                                                   | Cause and fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Status             |
| -------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| P1       | **`create_sketch` quietly makes an unattached sketch.**               | It looks the plane up by exact name (`XZ_Plane`) in the body's origin. Bodies whose origin planes are numbered (`XZ_Plane001`) get nothing, and any other plane name (a datum plane, say) is ignored without an error. Fix: find the body's origin plane by its role instead of its name, accept any plane or datum plane by name, and raise an error if nothing matches. This also covers the earlier "can `create_sketch` attach to a datum plane?" item: as written, it can't. | Backlog, Upstream? |
| P1       | **`edit_object` can't set link properties, and skips unknown names.** | Values are set exactly as given, so link properties (`Tip`, attachment, `UpToFace`, `Profile`) receive a string instead of an object and FreeCAD rejects them. Property names the object doesn't have are skipped silently. Fix: for link-type properties, look up object names, and accept `[object, [sub-elements]]` for face and edge links. Raise an error for unknown property names.                                                                                        | Backlog, Upstream? |
| P1       | **`recompute` reports success on a broken feature; no error text.**   | The `recompute` tool always returns success. `validate_object` checks FreeCAD's error flags and shape validity, but never reads FreeCAD's own error message for the object (`getStatusString()`), so a broken pocket shows "Invalid" with no reason. Fix: have `recompute`, `recompute_document` and `validate_object` list objects in error together with FreeCAD's message.                                                                                                     | Backlog, Upstream? |
| P1       | **Deleting a body's last feature leaves the body with no shape.**     | `delete_object` removes the object and nothing else. If it was the body's Tip, the Tip is left empty. Fix: move the Tip back to the previous feature, as the FreeCAD GUI does, and say so in the result.                                                                                                                                                                                                                                                                          | Backlog, Upstream? |
| P1       | **Small screenshots come back as unreadable text.**                   | `get_screenshot` returns the image as base64 text inside its result, so the client shows it as text. Large ones only work because the client saves long results to a file. Fix: return it as MCP image content, which clients display.                                                                                                                                                                                                                                            | Backlog, Upstream? |
| P2       | **`get_screenshot` always resets the view.**                          | Every capture applies an angle (default isometric) and zooms to fit, then puts the camera back. There's no way to capture the current view or a zoomed-in region. Fix: add a "current view" option and make fit-all optional. Zooming to a region depends on the camera fix below.                                                                                                                                                                                                | Backlog, Upstream? |
| P2       | **`set_camera_position` and the zoom tools fail.**                    | They use the 3D scene library (`pivy`), which a standard FreeCAD install doesn't make available here, hence "No SWIG wrapped library loaded". The zoom tools also act on the active view whatever `doc_name` says. Fix: use the view's own camera and zoom functions, which don't need `pivy` (check they exist in FreeCAD 1.0 and 1.1). Same cause as the earlier zoom bug, now merged into this row.                                                                            | Backlog, Upstream? |
| P2       | **`inspect_object` has no bounding box or centre of mass.**           | It reports volume and area only. Fix: add bounding box, centre of mass and global placement. Small change to an upstream tool.                                                                                                                                                                                                                                                                                                                                                    | Backlog, Upstream? |
| P2       | **`slice_shape` and `section_shape` only report counts.**             | They add a new object to the tree on every call and return how many pieces it has, not the outline. Fix: covered by the read-only `get_section_outline` tool (3.5), which leaves the tree alone. Optionally offer upstream a change to return the outline too.                                                                                                                                                                                                                    | Backlog            |
| P2       | **`spreadsheet_set_cell` reports failure when it succeeded.**         | Setting a value with units, e.g. `=40mm`, sets the cell correctly but returns a failure. A false failure could make the AI retry and apply a change twice.                                                                                                                                                                                                                                                                                                                        | Backlog, Upstream? |
| P2       | **`uv.lock` points at a private package mirror.**                     | Every package in the lockfile resolves from `pkgs.safetycli.com` (upstream's Safety mirror), so `uv sync` may fail for anyone without access to it. This blocks running the tests locally the standard way. Workaround: install into a fresh environment from public PyPI. Regenerating the lockfile on `custom` would conflict with every upstream merge, so raise it upstream instead.                                                                                          | Backlog, Upstream? |
| P3       | **The undo history is hard to read.**                                 | Edits made through the bridge get generic undo names ("Create Object", "Edit Object", "Delete Object"), and spreadsheet edits share the document's single undo history. The feedback saw every entry labelled "Delete Object"; not reproduced yet. Fix: name each undo step after what changed (e.g. "Edit Pocket.TaperAngle"), and check what was reported.                                                                                                                      | Backlog            |
| P3       | **Spreadsheet expressions with string literals break.**               | FreeCAD expressions quote text with `<<` and `>>`, not quote marks, so `";"` breaks the expression. Fix: say so in the spreadsheet tool descriptions.                                                                                                                                                                                                                                                                                                                             | Backlog, Upstream? |
| P3       | **Rotation convention isn't stated.**                                 | `set_placement` and `rotate_object` take `[yaw, pitch, roll]` in degrees but don't say which axes or in what order. Fix: check against FreeCAD's rotation and state it in the descriptions.                                                                                                                                                                                                                                                                                       | Backlog, Upstream? |
| P3       | **Generated code compares strings with `is`.**                        | 102 tools build lines like `'MyDoc' is None` when given a document name. Python warns about this but gives the right answer, so it is untidy, not broken. Fix by deciding in the tool which document expression to send, instead of sending the comparison.                                                                                                                                                                                                                       | Backlog, Upstream? |

### 3.5 New tools (in `tools/custom/`)

All read-only unless stated: they work on in-memory shape copies and never
add objects to the document.

| Priority | Tool                                               | What it does                                                                                                                                                                                                                                                                                                                                                                                                                | Status  |
| -------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| P1       | **`get_shape_geometry`** (working name)            | Returns an object's vertices (positions), edges (type, curve details such as radius and centre, end points, length, optional sample points) and faces (type, area, centre, normal, bounding box). Can be limited to listed sub-elements (`Edge3`, `Face2`) and reported in another object's local frame, such as a sketch's or body's. Caps the number of items so replies stay small. Replaces the spreadsheet workaround. | Backlog |
| P2       | **`check_overlap`**                                | Checks two objects for overlap: shared volume and where the contact is (centre and bounding box), or the minimum gap and closest points when they don't touch. For questions like "does this cut hit the support?". The clash sweep (3.6) builds on it.                                                                                                                                                                     | Backlog |
| P2       | **Sketch editing tools** (not read-only)           | Move or edit existing sketch geometry with the solver, keeping constraints (e.g. move a line's end point). Plus a closed polyline helper that adds connected lines with coincident corners in one call, optionally making axis-aligned lines horizontal or vertical. Saves the delete-and-redraw cycles from the feedback.                                                                                                  | Backlog |
| P2       | **`get_section_outline`**                          | Cross-section of a shape through a plane (or several parallel offsets), returning the outline geometry in the same format as `get_shape_geometry`. Replaces `slice_shape`/`section_shape` for inspection, without adding objects to the tree.                                                                                                                                                                               | Backlog |
| P2       | **Clash sweep**                                    | Rotate one part around a pivot and report where it starts to hit another. Design in 3.6.                                                                                                                                                                                                                                                                                                                                    | Backlog |
| P2       | **ShapeBinder and SubShapeBinder** (not read-only) | Needed for master-sketch setups and for using another body's geometry.                                                                                                                                                                                                                                                                                                                                                      | Backlog |
| P2       | **Projecting edges and faces from another body**   | `add_external_geometry` works within a body, but a sketch linked straight into a different body hits FreeCAD's "out of allowed scope" error. The usual fix is a SubShapeBinder first, then project from that, so this depends on the item above. Needed for aligning the C13 chest and pelvis cut-outs to the new waist.                                                                                                    | Backlog |
| P2       | **Datum planes attached to edges and vertices**    | `create_datum_plane` can only offset from XY, XZ or YZ. Needs attachment modes such as normal-to-edge and attachment offsets with rotation, e.g. angled planes along a curved path.                                                                                                                                                                                                                                         | Backlog |
| P2       | **Volume before and after for more features**      | `pocket_sketch` reporting volume before and after was the most useful single feature in the feedback, catching a cut into the ball support. Do the same for pad, groove, subtractive loft and pipe, fillet and chamfer. Small change to upstream tools, so a good one to offer upstream.                                                                                                                                    | Backlog |
| P3       | **Sweep and pipe orientation modes**               | `sweep_sketch` and `subtractive_pipe` take one profile and one path. Need the orientation mode (Frenet, Binormal, auxiliary spine) and extra cross-sections for variable-section sweeps.                                                                                                                                                                                                                                    | Backlog |
| P3       | **Create `App::Link` objects**                     | No tool creates links. They currently have to be made by hand in FreeCAD (Make Link), then pointed at the right target.                                                                                                                                                                                                                                                                                                     | Backlog |
| P3       | **MultiTransform**                                 | Not covered.                                                                                                                                                                                                                                                                                                                                                                                                                | Backlog |
| P3       | **PartDesign primitives and PartDesign helix**     | Additive and subtractive box, cylinder and so on. A Part-workbench helix exists, but not the PartDesign feature.                                                                                                                                                                                                                                                                                                            | Backlog |

### 3.6 Clash sweep design

A tool that rotates one solid around a pivot and reports when it starts to
overlap another. Prototyped with `execute_python` as a dry run. Design notes
from that:

- **Inputs:** moving object, fixed object(s), pivot point, axis, angle range
  and step, both directions.
- **Works on in-memory shape copies** (`Shape.copy()` then `rotate()`), so the
  document is never changed.
- **Speed:** check bounding boxes first and only run the full boolean
  (`common`) when the boxes intersect. Treat overlap below ~0.01 mm³ as noise.
- **Output per direction:** first clash angle, last clear angle, overlap
  volume, and the contact region's centre and bounding box. Optionally, the
  minimum gap and closest point pairs near the limit (`distToShape`).
- **Runtime:** a fine sweep on detailed parts can take minutes, so it needs a
  generous timeout or should report progress. Coarse-then-fine stepping would
  help.
- **Helper wanted:** a way to measure ball-joint centres or pivot points
  (e.g. from a spherical face), so pivots don't have to be entered by hand.
- **Caveat to document:** rigid geometry only. Print tolerances and joint
  friction aren't modelled.
- **Shares code with `check_overlap`,** so build that first.

### 3.7 What worked well (keep)

From the same feedback, so these don't get lost in later changes:

- `pocket_sketch` reporting volume before and after.
- `validate_object` and `undo`.
- Rotation as yaw, pitch and roll (once the convention is documented).

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

- [ ] Decide which "Upstream?" items to offer to spkane as PRs, and which to
      raise as issues only.
- [ ] Add each new custom change to section 1 when it merges.
